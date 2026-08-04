"""Tests for per-user OBO auth on model backends (auth_mode='entra_obo').

Covers the whole thin feature that lets a model backend authenticate to its
gateway with a per-user Entra On-Behalf-Of access token instead of one static
``api_key``:

* migration 068 — the two ``model_definitions`` columns, defaulting existing
  rows to the pre-feature ``static`` behaviour;
* storage + admin-load round-trip of ``auth_mode`` / ``obo_audience``;
* :func:`mint_obo_access_token` — the model-provider mint (reuses the MCP OBO
  grant legs + rotation write-back, but with an in-process token cache and no
  per-server machinery);
* ``ModelRegistry.get_client`` constructing an OBO backend that has no static
  fallback key;
* ``ChatSession._model_backend_auth_token`` — the per-call token resolve at the
  model call site. Ownerless OBO and keyless dynamic-auth failures refuse;
  explicit static keys remain an operator-controlled mint-failure fallback.
  The token is bound via ``client.with_options(api_key=...)`` at the call site
  (not injected as a header — the Anthropic SDK ignores an ``extra_headers``
  ``x-api-key`` override).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from tests._oidc_test_helpers import (
    ISSUER,
    TOKEN_ENDPOINT,
    make_oidc_config,
    mint_warn_state_reset,
)
from tests.conftest import make_mcp_token_cipher
from turnstone.core.judge import JudgeConfig
from turnstone.core.mcp_crypto import MCPTokenStore
from turnstone.core.mcp_oauth import mint_app_access_token, mint_obo_access_token
from turnstone.core.model_registry import (
    ModelAuthConfigError,
    ModelConfig,
    ModelRegistry,
    load_model_registry,
)
from turnstone.core.session import BackendAuthUnavailableError, ChatSession
from turnstone.core.storage._sqlite import SQLiteBackend

if TYPE_CHECKING:
    from collections.abc import Iterator

    from turnstone.core.oidc import OIDCConfig

USER = "user-1"
AUDIENCE = "https://models.example.com"

_MIGRATIONS_DIR = str(
    Path(__file__).resolve().parent.parent / "turnstone" / "core" / "storage" / "migrations"
)


@pytest.fixture(autouse=True)
def _reset_warn_dedup_state() -> Iterator[None]:
    """Per-test mint warn/cause reset — see ``mint_warn_state_reset``."""
    yield from mint_warn_state_reset()


# ---------------------------------------------------------------------------
# Migration 068
# ---------------------------------------------------------------------------


def _alembic_cfg(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS_DIR)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


class TestMigration068:
    def test_upgrade_adds_auth_columns(self, tmp_path: Path) -> None:
        db_path = tmp_path / "068-up.db"
        command.upgrade(_alembic_cfg(db_path), "068")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            cols = {c["name"] for c in sa.inspect(engine).get_columns("model_definitions")}
            assert {"auth_mode", "obo_audience"} <= cols
        finally:
            engine.dispose()

    def test_preexisting_row_defaults_to_static(self, tmp_path: Path) -> None:
        db_path = tmp_path / "068-default.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "067")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "INSERT INTO model_definitions "
                        "(definition_id, alias, model, created, updated) "
                        "VALUES ('d1', 'gpt', 'gpt-5', "
                        "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
                    )
                )
            command.upgrade(cfg, "068")
            with engine.connect() as conn:
                row = conn.execute(
                    sa.text(
                        "SELECT auth_mode, obo_audience FROM model_definitions "
                        "WHERE definition_id = 'd1'"
                    )
                ).fetchone()
            assert row is not None
            # A pre-068 row keeps byte-identical behaviour: static, no audience.
            assert row[0] == "static" and row[1] == ""
        finally:
            engine.dispose()

    def test_downgrade_removes_auth_columns(self, tmp_path: Path) -> None:
        db_path = tmp_path / "068-down.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "068")
        command.downgrade(cfg, "067")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            cols = {c["name"] for c in sa.inspect(engine).get_columns("model_definitions")}
            assert "auth_mode" not in cols and "obo_audience" not in cols
        finally:
            engine.dispose()

    def test_downgrade_then_upgrade_round_trip(self, tmp_path: Path) -> None:
        db_path = tmp_path / "068-roundtrip.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "068")
        command.downgrade(cfg, "067")
        command.upgrade(cfg, "068")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            cols = {c["name"] for c in sa.inspect(engine).get_columns("model_definitions")}
            assert {"auth_mode", "obo_audience"} <= cols
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# Storage + admin-load round-trip
# ---------------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path: Any) -> SQLiteBackend:
    return SQLiteBackend(str(tmp_path / "test.db"))


class TestModelDefinitionStorage:
    def test_create_and_read_back_obo_fields(self, storage: SQLiteBackend) -> None:
        storage.create_model_definition(
            definition_id="d1",
            alias="tf-opus",
            model="vmg/opus",
            provider="anthropic",
            base_url="https://gateway.example.com",
            auth_mode="entra_obo",
            obo_audience=AUDIENCE,
        )
        row = storage.get_model_definition_by_alias("tf-opus")
        assert row is not None
        assert row["auth_mode"] == "entra_obo"
        assert row["obo_audience"] == AUDIENCE

    def test_defaults_static_when_unspecified(self, storage: SQLiteBackend) -> None:
        storage.create_model_definition(definition_id="d2", alias="plain", model="gpt-5")
        row = storage.get_model_definition_by_alias("plain")
        assert row is not None
        assert row["auth_mode"] == "static"
        assert row["obo_audience"] == ""

    def test_update_toggles_auth_mode(self, storage: SQLiteBackend) -> None:
        storage.create_model_definition(definition_id="d3", alias="m3", model="gpt-5")
        assert storage.update_model_definition("d3", auth_mode="entra_obo", obo_audience=AUDIENCE)
        row = storage.get_model_definition("d3")
        assert row is not None
        assert row["auth_mode"] == "entra_obo" and row["obo_audience"] == AUDIENCE

    def test_load_model_registry_carries_obo_fields(self, storage: SQLiteBackend) -> None:
        storage.create_model_definition(
            definition_id="d4",
            alias="tf",
            model="vmg/opus",
            provider="anthropic",
            base_url="https://gateway.example.com",
            auth_mode="entra_obo",
            obo_audience=AUDIENCE,
        )
        registry = load_model_registry(storage=storage, allow_empty=True)
        cfg = registry.get_config("tf")
        assert cfg.auth_mode == "entra_obo"
        assert cfg.obo_audience == AUDIENCE

    def test_invalid_db_auth_mode_is_not_swallowed_as_storage_failure(
        self,
        storage: SQLiteBackend,
    ) -> None:
        storage.create_model_definition(
            definition_id="bad-mode",
            alias="bad",
            model="m",
            auth_mode="bogus",
            obo_audience=AUDIENCE,
        )

        with pytest.raises(ModelAuthConfigError, match="invalid auth_mode"):
            load_model_registry(storage=storage, allow_empty=True)

    def test_runtime_audience_rejects_control_characters(
        self,
        storage: SQLiteBackend,
    ) -> None:
        storage.create_model_definition(
            definition_id="bad-audience",
            alias="bad",
            model="m",
            auth_mode="entra_obo",
            obo_audience="api://gateway\ninjected",
        )

        with pytest.raises(ModelAuthConfigError, match="control characters"):
            load_model_registry(storage=storage, allow_empty=True)


# ---------------------------------------------------------------------------
# ModelRegistry.get_client — OBO backend with no static fallback key
# ---------------------------------------------------------------------------


class TestGetClientKeyInjection:
    """get_client feeds a placeholder key ONLY for an entra_obo backend with no
    static fallback — everything else passes ``cfg.api_key`` through unchanged.
    Spies on ``create_client`` so it's independent of SDK/env behaviour."""

    def _seen_api_key(self, cfg: ModelConfig, monkeypatch: Any) -> str:
        seen: dict[str, Any] = {}

        def _spy(provider: str, *, base_url: str, api_key: str) -> object:
            seen["api_key"] = api_key
            return object()

        monkeypatch.setattr("turnstone.core.model_registry.create_client", _spy)
        ModelRegistry(models={cfg.alias: cfg}, default=cfg.alias).get_client(cfg.alias)
        return seen["api_key"]

    def test_obo_blank_key_gets_placeholder(self, monkeypatch: Any) -> None:
        cfg = ModelConfig(
            alias="tf",
            base_url="u",
            api_key="",  # no static fallback — real credential injected per call
            model="m",
            provider="anthropic",
            auth_mode="entra_obo",
            obo_audience=AUDIENCE,
        )
        assert self._seen_api_key(cfg, monkeypatch) == "backend-auth-placeholder-unused"

    def test_obo_with_static_key_keeps_it(self, monkeypatch: Any) -> None:
        cfg = ModelConfig(
            alias="tf",
            base_url="u",
            api_key="real-key",
            model="m",
            provider="anthropic",
            auth_mode="entra_obo",
            obo_audience=AUDIENCE,
        )
        assert self._seen_api_key(cfg, monkeypatch) == "real-key"

    def test_static_blank_key_unchanged(self, monkeypatch: Any) -> None:
        cfg = ModelConfig(alias="a", base_url="u", api_key="", model="m", provider="anthropic")
        # No placeholder for a static alias — the empty key rides through exactly
        # as before (create_client then coerces it to an env-var fallback).
        assert self._seen_api_key(cfg, monkeypatch) == ""


# ---------------------------------------------------------------------------
# mint_obo_access_token — the model-provider mint
# ---------------------------------------------------------------------------


def _make_app_state(
    storage: SQLiteBackend, *, http_client: httpx.AsyncClient, oidc_config: OIDCConfig
) -> SimpleNamespace:
    return SimpleNamespace(
        auth_storage=storage,
        mcp_token_store=MCPTokenStore(storage, make_mcp_token_cipher(), node_id="test"),
        oidc_config=oidc_config,
        obo_http_client=http_client,
        mcp_oauth_refresh_locks={},
    )


def _mk_response(status_code: int = 200, json_body: Any = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = {}
    body = "" if json_body is None else str(json_body)
    resp.content = body.encode("utf-8")
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = ValueError("no body")
    resp.text = body
    return resp


def _seed_credential(state: SimpleNamespace, *, refresh_token: str = "rt-1") -> None:
    state.mcp_token_store.upsert_oidc_credential(USER, ISSUER, refresh_token=refresh_token)


def _mint(state: SimpleNamespace, **kwargs: Any) -> Any:
    async def _run() -> Any:
        return await mint_obo_access_token(
            app_state=state, user_id=USER, audience=AUDIENCE, **kwargs
        )

    return asyncio.run(_run())


class TestMintOboAccessToken:
    def test_happy_path_redeems_default_scope_and_caches(self, storage: SQLiteBackend) -> None:
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(200, {"access_token": "at-minted", "expires_in": 3600})
        )
        state = _make_app_state(storage, http_client=client, oidc_config=make_oidc_config())
        _seed_credential(state)
        state.mcp_token_store.get_user_token = MagicMock(  # type: ignore[method-assign]
            wraps=state.mcp_token_store.get_user_token
        )

        token = _mint(state)

        assert token == "at-minted"
        # Exact entra wire shape — scope pins <audience>/.default.
        assert client.post.call_count == 1
        call = client.post.call_args
        assert call.args == (TOKEN_ENDPOINT,)
        assert call.kwargs["data"] == {
            "grant_type": "refresh_token",
            "refresh_token": "rt-1",
            "client_id": "cid",
            "client_secret": "csecret",
            "scope": f"{AUDIENCE}/.default",
        }
        reads_after_mint = state.mcp_token_store.get_user_token.call_count
        # Second call serves the mcp-loop memo — no DB decrypt or IdP trip.
        token2 = _mint(state)
        assert token2 == "at-minted"
        assert client.post.call_count == 1
        assert state.mcp_token_store.get_user_token.call_count == reads_after_mint

    def test_minted_token_cached_in_db_and_shared_across_nodes(
        self, storage: SQLiteBackend
    ) -> None:
        # One shared enc key, as a cluster shares MCP_ENC_KEY across workers.
        cipher = make_mcp_token_cipher()
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(200, {"access_token": "at-minted", "expires_in": 3600})
        )
        node_a = SimpleNamespace(
            auth_storage=storage,
            mcp_token_store=MCPTokenStore(storage, cipher, node_id="A"),
            oidc_config=make_oidc_config(),
            obo_http_client=client,
            mcp_oauth_refresh_locks={},
        )
        node_a.mcp_token_store.upsert_oidc_credential(USER, ISSUER, refresh_token="rt-1")

        assert _mint(node_a) == "at-minted"
        assert client.post.call_count == 1

        # Persisted as a "cache, not custody" row (refresh_token NULL), decodable.
        cache_server = f"__model_obo__:{AUDIENCE}"
        raw = storage.get_mcp_user_token(USER, cache_server)
        assert raw is not None and raw["refresh_token_ct"] is None
        plain = node_a.mcp_token_store.get_user_token(USER, cache_server)
        assert plain is not None
        assert plain["access_token"] == "at-minted"
        assert plain["audience"] == AUDIENCE

        # A DIFFERENT worker (same DB + enc key) serves the cached token with NO
        # new IdP round-trip — no needless per-worker re-mint.
        node_b = SimpleNamespace(
            auth_storage=storage,
            mcp_token_store=MCPTokenStore(storage, cipher, node_id="B"),
            oidc_config=make_oidc_config(),
            obo_http_client=client,
            mcp_oauth_refresh_locks={},
        )
        assert _mint(node_b) == "at-minted"
        assert client.post.call_count == 1

    def test_rotated_refresh_token_persisted_to_credential(self, storage: SQLiteBackend) -> None:
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(
                200, {"access_token": "at", "expires_in": 3600, "refresh_token": "rt-2"}
            )
        )
        state = _make_app_state(storage, http_client=client, oidc_config=make_oidc_config())
        _seed_credential(state, refresh_token="rt-1")

        assert _mint(state) == "at"
        cred = state.mcp_token_store.get_oidc_credential(USER, ISSUER)
        assert cred is not None and cred["refresh_token"] == "rt-2"

    def test_force_refresh_bypasses_cache(self, storage: SQLiteBackend) -> None:
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            side_effect=[
                _mk_response(200, {"access_token": "at-1", "expires_in": 3600}),
                _mk_response(200, {"access_token": "at-2", "expires_in": 3600}),
            ]
        )
        state = _make_app_state(storage, http_client=client, oidc_config=make_oidc_config())
        _seed_credential(state)

        assert _mint(state) == "at-1"
        assert _mint(state, force_refresh=True) == "at-2"
        assert client.post.call_count == 2

    def test_missing_credential_returns_none_no_http(self, storage: SQLiteBackend) -> None:
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()
        state = _make_app_state(storage, http_client=client, oidc_config=make_oidc_config())
        # No captured credential seeded.
        assert _mint(state) is None
        assert client.post.call_count == 0

    def test_missing_credential_names_cause_once_per_user(
        self, storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The missing-credential cause is deduped per (user, audience)."""
        from turnstone.core import mcp_oauth as mcp_oauth_module

        warned: set[tuple[str, str]] = set()
        monkeypatch.setattr(mcp_oauth_module, "_MODEL_OBO_MISSING_CRED_WARNED", warned)
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()
        state = _make_app_state(storage, http_client=client, oidc_config=make_oidc_config())
        assert _mint(state) is None
        assert _mint(state) is None  # repeat turn: still exactly one entry
        # Tuple key, not a joined string — user ids and api:// audiences
        # can both contain ':'.
        assert warned == {(USER, AUDIENCE)}

    def test_missing_credential_cap_cannot_starve_operator_causes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two dedup namespaces are independent by construction."""
        from turnstone.core import mcp_oauth as mcp_oauth_module

        user_full = {(f"user-{i}", "api://aud") for i in range(512)}
        operator_fresh: set[str] = set()
        monkeypatch.setattr(mcp_oauth_module, "_MODEL_OBO_MISSING_CRED_WARNED", user_full)
        monkeypatch.setattr(mcp_oauth_module, "_MODEL_MINT_MISCONFIG_WARNED", operator_fresh)
        mcp_oauth_module._warn_model_mint_misconfig_once(
            "model_obo.oidc_not_enabled", "api://aud", "u-any"
        )
        assert operator_fresh == {"model_obo.oidc_not_enabled:api://aud"}

        operator_full = {f"cause-{i}:api://aud" for i in range(512)}
        user_fresh: set[tuple[str, str]] = set()
        monkeypatch.setattr(mcp_oauth_module, "_MODEL_MINT_MISCONFIG_WARNED", operator_full)
        monkeypatch.setattr(mcp_oauth_module, "_MODEL_OBO_MISSING_CRED_WARNED", user_fresh)
        mcp_oauth_module._warn_model_obo_missing_credential_once("api://aud", "u-new")
        assert user_fresh == {("u-new", "api://aud")}

    def test_success_clears_only_the_minting_users_cause(self, storage: SQLiteBackend) -> None:
        """The last-cause record is keyed per (prefix, audience, user)."""
        from turnstone.core.mcp_oauth import model_mint_refusal_cause

        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(200, {"access_token": "at-bob", "expires_in": 3600})
        )
        state = _make_app_state(storage, http_client=client, oidc_config=make_oidc_config())
        # bob has a captured credential; alice does not.
        state.mcp_token_store.upsert_oidc_credential("bob", ISSUER, refresh_token="rt-bob")

        async def _mint_as(user: str) -> Any:
            return await mint_obo_access_token(app_state=state, user_id=user, audience=AUDIENCE)

        assert asyncio.run(_mint_as("alice")) is None
        assert model_mint_refusal_cause("model_obo", AUDIENCE, "alice") == "missing_credential"

        assert asyncio.run(_mint_as("bob")) == "at-bob"
        assert model_mint_refusal_cause("model_obo", AUDIENCE, "bob") == ""
        assert model_mint_refusal_cause("model_obo", AUDIENCE, "alice") == "missing_credential"

    def test_cooldown_window_keeps_the_recorded_cause(self, storage: SQLiteBackend) -> None:
        """The record persists across cooldown short-circuits: only the
        recording user's successful mint clears a cause."""
        from turnstone.core.mcp_oauth import model_mint_refusal_cause

        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(200, {"access_token": "at-bob", "expires_in": 3600})
        )
        state = _make_app_state(storage, http_client=client, oidc_config=make_oidc_config())
        state.mcp_token_store.upsert_oidc_credential("bob", ISSUER, refresh_token="rt-bob")

        async def _mint_as(user: str) -> Any:
            return await mint_obo_access_token(app_state=state, user_id=user, audience=AUDIENCE)

        # First refusal records the cause and arms alice's cooldown.
        assert asyncio.run(_mint_as("alice")) is None
        # Another user's success on the shared audience must not disturb it.
        assert asyncio.run(_mint_as("bob")) == "at-bob"
        posts_after_bob = client.post.call_count

        # Alice's next turn lands inside the cooldown window: the mint
        # short-circuits (no IdP traffic) and the cause survives.
        assert asyncio.run(_mint_as("alice")) is None
        assert client.post.call_count == posts_after_bob
        assert model_mint_refusal_cause("model_obo", AUDIENCE, "alice") == "missing_credential"

    def test_oidc_disabled_returns_none_no_http(self, storage: SQLiteBackend) -> None:
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()
        state = _make_app_state(
            storage, http_client=client, oidc_config=make_oidc_config(enabled=False)
        )
        _seed_credential(state)
        assert _mint(state) is None
        assert client.post.call_count == 0

    def test_discovery_pending_posture_logs_pending_cause_not_disabled(
        self, storage: SQLiteBackend, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Discovery-pending self-heals, so it is not ``oidc_not_enabled``."""
        import logging

        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()
        state = _make_app_state(
            storage,
            http_client=client,
            oidc_config=make_oidc_config(enabled=False, discovery_retryable=True),
        )
        _seed_credential(state)
        with caplog.at_level(logging.WARNING):
            assert _mint(state) is None
        blob = " ".join(r.getMessage() + str(getattr(r, "__dict__", "")) for r in caplog.records)
        assert "model_obo.oidc_discovery_pending" in blob
        assert "model_obo.oidc_not_enabled" not in blob
        assert client.post.call_count == 0

    def test_missing_token_store_logs_store_cause_with_shape_fields(
        self, storage: SQLiteBackend, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The cause carries which half of the store/storage pair is absent."""
        import logging

        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()
        state = _make_app_state(storage, http_client=client, oidc_config=make_oidc_config())
        state.mcp_token_store = None
        with caplog.at_level(logging.WARNING):
            assert _mint(state) is None
        matching = [
            r
            for r in caplog.records
            if "model_obo.token_store_unavailable" in r.getMessage() + str(r.__dict__)
        ]
        assert matching, caplog.records
        blob = " ".join(r.getMessage() + str(r.__dict__) for r in matching)
        assert "has_token_store" in blob and "False" in blob
        assert "has_storage" in blob and "True" in blob
        assert client.post.call_count == 0

    def test_unusable_profile_returns_none_no_http(self, storage: SQLiteBackend) -> None:
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()
        state = _make_app_state(
            storage, http_client=client, oidc_config=make_oidc_config(obo_grant_profile="")
        )
        _seed_credential(state)
        assert _mint(state) is None
        assert client.post.call_count == 0

    def test_permanent_rejection_returns_none(self, storage: SQLiteBackend) -> None:
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(
                400, {"error": "invalid_grant", "error_description": "AADSTS65001"}
            )
        )
        state = _make_app_state(storage, http_client=client, oidc_config=make_oidc_config())
        _seed_credential(state)
        # A failed mint falls back to the static credential (None), and never
        # auto-deletes the shared credential.
        assert _mint(state) is None
        assert state.mcp_token_store.get_oidc_credential(USER, ISSUER) is not None
        # The audience-scoped cooldown suppresses a dead-grant retry storm.
        assert _mint(state) is None
        assert client.post.call_count == 1


# ---------------------------------------------------------------------------
# mint_app_access_token — app-identity (client-credentials) mint
# ---------------------------------------------------------------------------


def _mint_app(state: SimpleNamespace, **kwargs: Any) -> Any:
    async def _run() -> Any:
        return await mint_app_access_token(app_state=state, audience=AUDIENCE, **kwargs)

    return asyncio.run(_run())


class TestMintAppAccessToken:
    def test_happy_path_client_credentials_and_caches(self, storage: SQLiteBackend) -> None:
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(200, {"access_token": "app-at", "expires_in": 3600})
        )
        # NOTE: no captured user credential seeded — app identity needs none.
        state = _make_app_state(storage, http_client=client, oidc_config=make_oidc_config())

        assert _mint_app(state) == "app-at"
        # Exact client-credentials wire shape — scope pins <audience>/.default.
        assert client.post.call_count == 1
        call = client.post.call_args
        assert call.args == (TOKEN_ENDPOINT,)
        assert call.kwargs["data"] == {
            "grant_type": "client_credentials",
            "client_id": "cid",
            "client_secret": "csecret",
            "scope": f"{AUDIENCE}/.default",
        }
        # Cached in the DB under the synthetic __app__ user — second call, no IdP.
        assert _mint_app(state) == "app-at"
        assert client.post.call_count == 1
        cache_server = f"__model_app__:{AUDIENCE}"
        raw = storage.get_mcp_user_token("__app__", cache_server)
        assert raw is not None and raw["refresh_token_ct"] is None

    def test_works_with_zero_user_credentials(self, storage: SQLiteBackend) -> None:
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(200, {"access_token": "app-at", "expires_in": 3600})
        )
        state = _make_app_state(storage, http_client=client, oidc_config=make_oidc_config())
        # The credential store is empty; the app grant still succeeds.
        assert state.mcp_token_store.get_oidc_credential(USER, ISSUER) is None
        assert _mint_app(state) == "app-at"

    def test_oidc_disabled_returns_none_no_http(self, storage: SQLiteBackend) -> None:
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()
        state = _make_app_state(
            storage, http_client=client, oidc_config=make_oidc_config(enabled=False)
        )
        assert _mint_app(state) is None
        assert client.post.call_count == 0

    def test_rejected_grant_returns_none(self, storage: SQLiteBackend) -> None:
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(
                400, {"error": "invalid_client", "error_description": "AADSTS7000215"}
            )
        )
        state = _make_app_state(storage, http_client=client, oidc_config=make_oidc_config())
        assert _mint_app(state) is None
        assert _mint_app(state) is None
        assert client.post.call_count == 1

    def test_non_entra_profile_is_explicitly_refused(self, storage: SQLiteBackend) -> None:
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()
        state = _make_app_state(
            storage,
            http_client=client,
            oidc_config=make_oidc_config(obo_grant_profile="rfc8693"),
        )

        assert _mint_app(state) is None
        client.post.assert_not_called()

    def test_force_refresh_bypasses_cache(self, storage: SQLiteBackend) -> None:
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            side_effect=[
                _mk_response(200, {"access_token": "app-1", "expires_in": 3600}),
                _mk_response(200, {"access_token": "app-2", "expires_in": 3600}),
            ]
        )
        state = _make_app_state(storage, http_client=client, oidc_config=make_oidc_config())
        assert _mint_app(state) == "app-1"
        assert _mint_app(state, force_refresh=True) == "app-2"
        assert client.post.call_count == 2


# ---------------------------------------------------------------------------
# ChatSession._model_backend_auth_token — resolve at the model call site
# ---------------------------------------------------------------------------


def _fake_session(
    *,
    registry: ModelRegistry | None,
    user_id: str | None,
    mint_token: str | None,
    app_token: str | None = None,
) -> SimpleNamespace:
    """Minimal stand-in exposing exactly what the backend-auth resolver reads."""
    mcp = SimpleNamespace(
        mint_model_obo_token_sync=MagicMock(return_value=mint_token),
        mint_app_token_sync=MagicMock(return_value=app_token),
    )
    return SimpleNamespace(
        _registry=registry,
        _mcp_mint_client=mcp,
        _mcp_effective_user_id=user_id,
        _config_store=None,
    )


def _registry_with(cfg: ModelConfig) -> ModelRegistry:
    return ModelRegistry(models={cfg.alias: cfg}, default=cfg.alias)


class TestModelOboToken:
    def _obo_cfg(
        self,
        provider: str = "anthropic",
        *,
        api_key: str = "static-fallback",
    ) -> ModelConfig:
        return ModelConfig(
            alias="tf",
            base_url="https://gateway.example.com",
            api_key=api_key,
            model="vmg/opus",
            provider=provider,
            auth_mode="entra_obo",
            obo_audience=AUDIENCE,
        )

    def test_obo_alias_with_user_returns_token(self) -> None:
        reg = _registry_with(self._obo_cfg())
        sess = _fake_session(registry=reg, user_id=USER, mint_token="minted-jwt")
        assert ChatSession._model_backend_auth_token(sess, "tf") == "minted-jwt"
        sess._mcp_mint_client.mint_model_obo_token_sync.assert_called_once_with(
            user_id=USER, audience=AUDIENCE
        )

    def test_fallback_warn_names_last_recorded_mint_cause(
        self, storage: SQLiteBackend, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The per-turn fallback warn names the last recorded cause inline."""
        import logging

        # A refused mint records its cause (typo'd grant profile).
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()
        state = _make_app_state(
            storage,
            http_client=client,
            oidc_config=make_oidc_config(obo_grant_profile="bogus"),
        )
        _seed_credential(state)
        assert _mint(state) is None

        # The decision layer: the mint client yields nothing.
        reg = _registry_with(self._obo_cfg())
        sess = _fake_session(registry=reg, user_id=USER, mint_token=None)
        with caplog.at_level(logging.WARNING):
            token = ChatSession._model_backend_auth_token(sess, "tf")
        assert token is None  # explicit static key stands, fail-open
        matching = [
            r
            for r in caplog.records
            if "model_obo.fallback_to_static" in r.getMessage() + str(r.__dict__)
        ]
        assert matching, caplog.records
        blob = " ".join(r.getMessage() + str(r.__dict__) for r in matching)
        assert "unsupported_grant_profile" in blob

    def test_decrypt_failure_names_cause_on_heartbeat(
        self, storage: SQLiteBackend, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A key-rotation refusal must not render as cause=unknown."""
        import logging

        # Credential captured under one encryption key; the store then
        # runs under a different key (rotation) — the real decrypt path.
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()
        state = _make_app_state(storage, http_client=client, oidc_config=make_oidc_config())
        _seed_credential(state)
        state.mcp_token_store = MCPTokenStore(storage, make_mcp_token_cipher(), node_id="B")

        assert _mint(state) is None
        assert client.post.call_count == 0  # refused before any IdP traffic

        from turnstone.core.mcp_oauth import model_mint_refusal_cause

        assert model_mint_refusal_cause("model_obo", AUDIENCE, USER) == "credential_decrypt_failure"

        # And the per-turn heartbeat renders it inline.
        reg = _registry_with(self._obo_cfg())
        sess = _fake_session(registry=reg, user_id=USER, mint_token=None)
        with caplog.at_level(logging.WARNING):
            ChatSession._model_backend_auth_token(sess, "tf")
        matching = [
            r
            for r in caplog.records
            if "model_obo.fallback_to_static" in r.getMessage() + str(r.__dict__)
        ]
        assert matching, caplog.records
        blob = " ".join(r.getMessage() + str(r.__dict__) for r in matching)
        assert "credential_decrypt_failure" in blob

    def test_fallback_warn_cause_unknown_when_nothing_recorded(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No recorded refusal renders as ``cause=unknown``, never omitted."""
        import logging

        reg = _registry_with(self._obo_cfg())
        sess = _fake_session(registry=reg, user_id=USER, mint_token=None)
        with caplog.at_level(logging.WARNING):
            ChatSession._model_backend_auth_token(sess, "tf")
        matching = [
            r
            for r in caplog.records
            if "model_obo.fallback_to_static" in r.getMessage() + str(r.__dict__)
        ]
        assert matching, caplog.records
        blob = " ".join(r.getMessage() + str(r.__dict__) for r in matching)
        assert "unknown" in blob

    def test_token_is_provider_agnostic(self) -> None:
        # The raw token is returned regardless of provider surface — the caller
        # binds it via ``with_options(api_key=...)``, so there is no per-provider
        # header shaping here. (The old header-injection path returned x-api-key
        # for anthropic, which the SDK silently ignored → the prod 401.)
        for provider in ("anthropic", "openai-compatible"):
            reg = _registry_with(self._obo_cfg(provider=provider))
            sess = _fake_session(registry=reg, user_id=USER, mint_token="minted-jwt")
            assert ChatSession._model_backend_auth_token(sess, "tf") == "minted-jwt"

    def test_auxiliary_judges_inherit_the_session_obo_resolver(
        self,
        mock_openai_client: Any,
    ) -> None:
        """Judge lanes must not quietly regress to app-only authentication."""
        reg = _registry_with(self._obo_cfg(provider="openai"))
        session = ChatSession(
            client=mock_openai_client,
            model="vmg/opus",
            ui=MagicMock(),
            instructions=None,
            temperature=0.5,
            max_tokens=1024,
            tool_timeout=30,
            registry=reg,
            model_alias="tf",
            judge_config=JudgeConfig(
                enabled=True,
                output_guard_llm=True,
            ),
            user_id=USER,
        )
        session._mcp_mint_client = SimpleNamespace(
            mint_model_obo_token_sync=MagicMock(return_value="minted-jwt"),
            mint_app_token_sync=MagicMock(return_value="app-jwt"),
        )

        intent_judge = session._ensure_judge()
        output_guard = session._ensure_output_guard_judge()

        assert intent_judge is not None
        assert output_guard is not None
        assert intent_judge._backend_auth_resolver == session._model_backend_auth_token
        assert output_guard._backend_auth_resolver == session._model_backend_auth_token
        assert intent_judge._backend_auth_resolver("tf") == "minted-jwt"
        session._mcp_mint_client.mint_model_obo_token_sync.assert_called_once_with(
            user_id=USER,
            audience=AUDIENCE,
        )
        session._mcp_mint_client.mint_app_token_sync.assert_not_called()

    def test_primary_stream_binds_backend_token_once_before_retry_loop(self) -> None:
        """The main streaming path mirrors model_turn's SDK binding."""
        sess = MagicMock()
        sess._MAX_RETRIES = 2
        sess._provider = MagicMock()
        sess._provider.create_streaming.return_value = iter(())
        sess._get_capabilities.return_value = SimpleNamespace(default_reasoning_effort=None)
        sess._maybe_attach_vllm_chat_reasoning.side_effect = lambda messages, _provider, _alias: (
            messages
        )
        sess._model_backend_auth_token.return_value = "minted-jwt"
        sess._cancel_ref = []
        sess._get_active_tools.return_value = []
        sess.max_tokens = 1024
        sess.temperature = None
        sess.reasoning_effort = None
        sess._provider_extra_params.return_value = None
        sess._get_deferred_names.return_value = frozenset()
        sess._resolve_replay_reasoning_to_model.return_value = False
        base_client = MagicMock()
        base_client.base_url = "https://gateway.example.com"
        bound_client = object()
        base_client.with_options.return_value = bound_client

        stream = ChatSession._try_stream(
            sess,
            base_client,
            "vmg/opus",
            [{"role": "user", "content": "hi"}],
            model_alias="tf",
        )

        assert list(stream) == []
        sess._model_backend_auth_token.assert_called_once_with("tf")
        base_client.with_options.assert_called_once_with(api_key="minted-jwt")
        assert sess._provider.create_streaming.call_args.kwargs["client"] is bound_client

    def test_primary_stream_forwards_alias_for_obo(self) -> None:
        # Regression: the primary _create_stream_with_retry call must pass
        # model_alias, or the backend-auth resolver can't resolve the OBO token and an
        # entra_obo main turn goes out on the static client key. The fallback
        # path and utility (title) completions always passed the alias; the
        # primary path silently didn't.
        sess = MagicMock()
        sess._model_alias = "oboagent"
        ChatSession._create_stream_with_retry(sess, [{"role": "user", "content": "hi"}])
        sess._try_stream.assert_called_once()
        assert sess._try_stream.call_args.kwargs.get("model_alias") == "oboagent"

    def test_fail_closed_refusal_never_enters_model_fallback_chain(self) -> None:
        sess = MagicMock()
        sess._model_alias = "oboagent"
        sess._try_stream.side_effect = BackendAuthUnavailableError("mint failed")
        sess._registry.fallback = ["static-backup"]
        sess._get_health_tracker.return_value = None

        with pytest.raises(BackendAuthUnavailableError):
            ChatSession._create_stream_with_retry(sess, [{"role": "user", "content": "hi"}])

        sess._try_fallback.assert_not_called()

    def test_static_alias_returns_none_and_never_mints(self) -> None:
        static_cfg = ModelConfig(
            alias="plain",
            base_url="",
            api_key="k",
            model="gpt-5",
            provider="openai",
        )
        reg = _registry_with(static_cfg)
        sess = _fake_session(registry=reg, user_id=USER, mint_token="unused")
        assert ChatSession._model_backend_auth_token(sess, "plain") is None
        sess._mcp_mint_client.mint_model_obo_token_sync.assert_not_called()

    def test_no_user_context_refuses_and_never_mints(self) -> None:
        reg = _registry_with(self._obo_cfg())
        sess = _fake_session(registry=reg, user_id="", mint_token="unused")
        with pytest.raises(BackendAuthUnavailableError):
            ChatSession._model_backend_auth_token(sess, "tf")
        sess._mcp_mint_client.mint_model_obo_token_sync.assert_not_called()

    def test_failed_mint_falls_back_to_static(self) -> None:
        reg = _registry_with(self._obo_cfg())
        sess = _fake_session(registry=reg, user_id=USER, mint_token=None)
        # Mint returned None (no credential / rejected) → None so the static
        # client credential stands.
        assert ChatSession._model_backend_auth_token(sess, "tf") is None

    def test_failed_mint_refuses_when_operator_enables_fail_closed(self) -> None:
        reg = _registry_with(self._obo_cfg())
        sess = _fake_session(registry=reg, user_id=USER, mint_token=None)
        sess._config_store = SimpleNamespace(get=lambda key: key == "model.auth_fail_closed")

        with pytest.raises(BackendAuthUnavailableError):
            ChatSession._model_backend_auth_token(sess, "tf")

    def test_failed_mint_without_real_static_key_always_refuses(self) -> None:
        reg = _registry_with(self._obo_cfg(api_key=""))
        sess = _fake_session(registry=reg, user_id=USER, mint_token=None)

        with pytest.raises(BackendAuthUnavailableError):
            ChatSession._model_backend_auth_token(sess, "tf")

    def test_missing_mint_host_without_real_static_key_always_refuses(self) -> None:
        reg = _registry_with(self._obo_cfg(api_key=""))
        sess = _fake_session(registry=reg, user_id=USER, mint_token=None)
        sess._mcp_mint_client = None

        with pytest.raises(BackendAuthUnavailableError):
            ChatSession._model_backend_auth_token(sess, "tf")

    def test_unknown_alias_returns_none(self) -> None:
        reg = _registry_with(self._obo_cfg())
        sess = _fake_session(registry=reg, user_id=USER, mint_token="x")
        assert ChatSession._model_backend_auth_token(sess, "does-not-exist") is None

    # -- entra_app (app-identity / client-credentials) --------------------------

    def _app_cfg(self, *, api_key: str = "static-fallback") -> ModelConfig:
        return ModelConfig(
            alias="tf",
            base_url="https://gateway.example.com",
            api_key=api_key,
            model="vmg/opus",
            provider="anthropic",
            auth_mode="entra_app",
            obo_audience=AUDIENCE,
        )

    def test_app_alias_mints_without_user(self) -> None:
        # entra_app is an explicit model-definition choice for a service
        # principal; it is never inferred from a missing OBO user.
        reg = _registry_with(self._app_cfg())
        sess = _fake_session(registry=reg, user_id="", mint_token=None, app_token="app-jwt")
        assert ChatSession._model_backend_auth_token(sess, "tf") == "app-jwt"
        sess._mcp_mint_client.mint_app_token_sync.assert_called_once_with(audience=AUDIENCE)
        sess._mcp_mint_client.mint_model_obo_token_sync.assert_not_called()

    def test_app_alias_uses_app_identity_even_with_user(self) -> None:
        # A user is present, but entra_app deliberately uses the app identity,
        # not per-user OBO.
        reg = _registry_with(self._app_cfg())
        sess = _fake_session(registry=reg, user_id=USER, mint_token="obo-jwt", app_token="app-jwt")
        assert ChatSession._model_backend_auth_token(sess, "tf") == "app-jwt"
        sess._mcp_mint_client.mint_model_obo_token_sync.assert_not_called()

    def test_app_failed_mint_falls_back_to_static(self) -> None:
        reg = _registry_with(self._app_cfg())
        sess = _fake_session(registry=reg, user_id="", mint_token=None, app_token=None)
        assert ChatSession._model_backend_auth_token(sess, "tf") is None

    def test_app_failed_mint_without_real_static_key_always_refuses(self) -> None:
        reg = _registry_with(self._app_cfg(api_key=""))
        sess = _fake_session(registry=reg, user_id="", mint_token=None, app_token=None)

        with pytest.raises(BackendAuthUnavailableError):
            ChatSession._model_backend_auth_token(sess, "tf")
