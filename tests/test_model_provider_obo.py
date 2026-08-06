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
# The owning model-definition alias the mint's cache and cause records key
# on (identity-keyed, like mcp_servers.name for MCP rows).
MODEL_ALIAS = "gw-model"
APP_ALIAS = "gw-app-model"

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


class TestMigration069:
    def test_upgrade_defaults_preexisting_rows_to_empty_scopes(self, tmp_path: Path) -> None:
        db_path = tmp_path / "069-up.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "068")
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
            command.upgrade(cfg, "069")
            with engine.connect() as conn:
                row = conn.execute(
                    sa.text("SELECT obo_scopes FROM model_definitions WHERE definition_id = 'd1'")
                ).fetchone()
            assert row is not None and row[0] == ""
        finally:
            engine.dispose()

    def test_downgrade_then_upgrade_round_trip(self, tmp_path: Path) -> None:
        db_path = tmp_path / "069-roundtrip.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "069")
        command.downgrade(cfg, "068")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            cols = {c["name"] for c in sa.inspect(engine).get_columns("model_definitions")}
            assert "obo_scopes" not in cols
        finally:
            engine.dispose()
        command.upgrade(cfg, "069")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            cols = {c["name"] for c in sa.inspect(engine).get_columns("model_definitions")}
            assert "obo_scopes" in cols
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

    def test_rfc8693_row_round_trips_scopes_into_registry(self, storage: SQLiteBackend) -> None:
        storage.create_model_definition(
            definition_id="d5",
            alias="tf-kc",
            model="vmg/opus",
            auth_mode="rfc8693_obo",
            obo_audience=AUDIENCE,
            obo_scopes="aud-gw   openid",
        )
        row = storage.get_model_definition_by_alias("tf-kc")
        assert row is not None and row["obo_scopes"] == "aud-gw   openid"
        registry = load_model_registry(storage=storage, allow_empty=True)
        cfg = registry.get_config("tf-kc")
        assert cfg.auth_mode == "rfc8693_obo"
        # The registry normalizer collapses whitespace runs so the value is a
        # stable mint-cache key component.
        assert cfg.obo_scopes == "aud-gw openid"

    def test_rfc8693_mode_requires_audience(self, storage: SQLiteBackend) -> None:
        storage.create_model_definition(
            definition_id="d6",
            alias="no-aud",
            model="m",
            auth_mode="rfc8693_obo",
        )
        with pytest.raises(ModelAuthConfigError, match="requires obo_audience"):
            load_model_registry(storage=storage, allow_empty=True)

    def test_scopes_residue_on_entra_mode_still_loads(self, storage: SQLiteBackend) -> None:
        """A stored scopes value on a mode that never reads it must not make
        the alias unloadable — the dispatch keeps it inert, mirroring the
        stale-audience-on-static tolerance.
        """
        storage.create_model_definition(
            definition_id="d7",
            alias="residue",
            model="m",
            auth_mode="entra_obo",
            obo_audience=AUDIENCE,
            obo_scopes="stale-scope",
        )
        registry = load_model_registry(storage=storage, allow_empty=True)
        assert registry.get_config("residue").obo_scopes == "stale-scope"

    def test_runtime_scopes_reject_control_characters(self, storage: SQLiteBackend) -> None:
        storage.create_model_definition(
            definition_id="bad-scopes",
            alias="bad-scopes",
            model="m",
            auth_mode="rfc8693_obo",
            obo_audience=AUDIENCE,
            obo_scopes="aud-gw\x01injected",
        )
        with pytest.raises(ModelAuthConfigError, match="obo_scopes contains control"):
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
    kwargs.setdefault("alias", MODEL_ALIAS)
    kwargs.setdefault("audience", AUDIENCE)

    async def _run() -> Any:
        return await mint_obo_access_token(app_state=state, user_id=USER, **kwargs)

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

        # Persisted as a "cache, not custody" row (refresh_token NULL),
        # decodable, identity-keyed on the owning alias.
        cache_server = f"__model_obo__:{MODEL_ALIAS}"
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
            "model_obo.oidc_not_enabled", "api://aud", "u-any", cause_key="api://aud"
        )
        assert operator_fresh == {"model_obo.oidc_not_enabled:api://aud"}

        operator_full = {f"cause-{i}:api://aud" for i in range(512)}
        user_fresh: set[tuple[str, str]] = set()
        monkeypatch.setattr(mcp_oauth_module, "_MODEL_MINT_MISCONFIG_WARNED", operator_full)
        monkeypatch.setattr(mcp_oauth_module, "_MODEL_OBO_MISSING_CRED_WARNED", user_fresh)
        mcp_oauth_module._warn_model_obo_missing_credential_once(
            "api://aud", "u-new", cause_key="api://aud"
        )
        assert user_fresh == {("u-new", "api://aud")}

    def test_success_clears_only_the_minting_users_cause(self, storage: SQLiteBackend) -> None:
        """The last-cause record is keyed per (prefix, alias, user)."""
        from turnstone.core.mcp_oauth import model_mint_refusal_cause, model_obo_cache_server

        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(200, {"access_token": "at-bob", "expires_in": 3600})
        )
        state = _make_app_state(storage, http_client=client, oidc_config=make_oidc_config())
        # bob has a captured credential; alice does not.
        state.mcp_token_store.upsert_oidc_credential("bob", ISSUER, refresh_token="rt-bob")

        async def _mint_as(user: str) -> Any:
            return await mint_obo_access_token(
                app_state=state, user_id=user, alias=MODEL_ALIAS, audience=AUDIENCE
            )

        assert asyncio.run(_mint_as("alice")) is None
        assert (
            model_mint_refusal_cause("model_obo", model_obo_cache_server(MODEL_ALIAS), "alice")
            == "missing_credential"
        )

        assert asyncio.run(_mint_as("bob")) == "at-bob"
        assert (
            model_mint_refusal_cause("model_obo", model_obo_cache_server(MODEL_ALIAS), "bob") == ""
        )
        assert (
            model_mint_refusal_cause("model_obo", model_obo_cache_server(MODEL_ALIAS), "alice")
            == "missing_credential"
        )

    def test_cooldown_window_keeps_the_recorded_cause(self, storage: SQLiteBackend) -> None:
        """The record persists across cooldown short-circuits: only the
        recording user's successful mint clears a cause."""
        from turnstone.core.mcp_oauth import model_mint_refusal_cause, model_obo_cache_server

        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(200, {"access_token": "at-bob", "expires_in": 3600})
        )
        state = _make_app_state(storage, http_client=client, oidc_config=make_oidc_config())
        state.mcp_token_store.upsert_oidc_credential("bob", ISSUER, refresh_token="rt-bob")

        async def _mint_as(user: str) -> Any:
            return await mint_obo_access_token(
                app_state=state, user_id=user, alias=MODEL_ALIAS, audience=AUDIENCE
            )

        # First refusal records the cause and arms alice's cooldown.
        assert asyncio.run(_mint_as("alice")) is None
        # Another user's success on the shared alias must not disturb it.
        assert asyncio.run(_mint_as("bob")) == "at-bob"
        posts_after_bob = client.post.call_count

        # Alice's next turn lands inside the cooldown window: the mint
        # short-circuits (no IdP traffic) and the cause survives.
        assert asyncio.run(_mint_as("alice")) is None
        assert client.post.call_count == posts_after_bob
        assert (
            model_mint_refusal_cause("model_obo", model_obo_cache_server(MODEL_ALIAS), "alice")
            == "missing_credential"
        )

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

    def test_rfc8693_scoped_exchange_mints_and_caches(self, storage: SQLiteBackend) -> None:
        """The #955 wire-through: the exchange leg requests the caller's
        scopes, the row keys on the OWNING ALIAS, and the requested scopes
        land in the row's legible ``scopes`` column for the freshness gate.
        """
        from turnstone.core.mcp_oauth import model_obo_cache_server

        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            side_effect=[
                _mk_response(200, {"access_token": "subject-at", "expires_in": 300}),
                _mk_response(200, {"access_token": "exchanged-at", "expires_in": 3600}),
            ]
        )
        state = _make_app_state(
            storage,
            http_client=client,
            oidc_config=make_oidc_config(obo_grant_profile="rfc8693"),
        )
        _seed_credential(state)

        token = _mint(state, scopes="aud-gw openid", grant_leg="rfc8693")

        assert token == "exchanged-at"
        assert client.post.call_count == 2
        exchange = client.post.call_args_list[1]
        assert exchange.kwargs["data"]["scope"] == "aud-gw openid"
        assert exchange.kwargs["data"]["audience"] == AUDIENCE

        plain = state.mcp_token_store.get_user_token(USER, model_obo_cache_server(MODEL_ALIAS))
        assert plain is not None
        assert plain["access_token"] == "exchanged-at"
        assert plain["scopes"] == "aud-gw openid"
        assert plain["audience"] == AUDIENCE
        # Identity keys: another alias holds no row — one owner per key.
        assert storage.get_mcp_user_token(USER, model_obo_cache_server("other-model")) is None

        # Warm re-mint with the same scopes serves the cache, zero IdP calls.
        assert _mint(state, scopes="aud-gw openid", grant_leg="rfc8693") == "exchanged-at"
        assert client.post.call_count == 2

    def test_changed_scopes_refuse_the_stale_row_and_overwrite_in_place(
        self, storage: SQLiteBackend
    ) -> None:
        """The freshness gate compares the row's stored scopes against the
        CURRENT dispatch scopes, so a re-scoped alias never serves the
        superseded bearer — the next mint overwrites the SAME identity key
        in place, leaving no stranded row behind.
        """
        from turnstone.core.mcp_oauth import model_obo_cache_server

        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            side_effect=[
                _mk_response(200, {"access_token": "subject-1", "expires_in": 300}),
                _mk_response(200, {"access_token": "wide-at", "expires_in": 3600}),
                _mk_response(200, {"access_token": "subject-2", "expires_in": 300}),
                _mk_response(200, {"access_token": "narrow-at", "expires_in": 3600}),
            ]
        )
        state = _make_app_state(
            storage,
            http_client=client,
            oidc_config=make_oidc_config(obo_grant_profile="rfc8693"),
        )
        _seed_credential(state)

        assert _mint(state, scopes="aud-gw wide", grant_leg="rfc8693") == "wide-at"
        # The operator narrows the alias's scopes: the wide-scope row fails
        # the freshness compare (no serving), a fresh mint runs, and the
        # one identity-keyed row now holds the narrow bearer.
        assert _mint(state, scopes="aud-gw", grant_leg="rfc8693") == "narrow-at"
        assert client.post.call_count == 4
        plain = state.mcp_token_store.get_user_token(USER, model_obo_cache_server(MODEL_ALIAS))
        assert plain is not None
        assert plain["access_token"] == "narrow-at"
        assert plain["scopes"] == "aud-gw"

    def test_scopes_whitespace_runs_hit_the_same_cache_row(self, storage: SQLiteBackend) -> None:
        """Entry-path normalization: a caller spelling the scopes with
        different interior whitespace must hit the same cache row, not
        re-mint — the row's stored scopes and the freshness compare's
        current side both pass through the one normalizer.
        """
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            side_effect=[
                _mk_response(200, {"access_token": "subject-at", "expires_in": 300}),
                _mk_response(200, {"access_token": "exchanged-at", "expires_in": 3600}),
            ]
        )
        state = _make_app_state(
            storage,
            http_client=client,
            oidc_config=make_oidc_config(obo_grant_profile="rfc8693"),
        )
        _seed_credential(state)

        assert _mint(state, scopes="aud-gw openid", grant_leg="rfc8693") == "exchanged-at"
        assert _mint(state, scopes="  aud-gw   openid ", grant_leg="rfc8693") == "exchanged-at"
        assert client.post.call_count == 2

    def test_grant_leg_mismatch_refuses_before_idp(self, storage: SQLiteBackend) -> None:
        """A mode's pinned leg contradicting the deployment profile refuses
        with the recorded cause and ZERO IdP traffic, in both directions.
        """
        from turnstone.core.mcp_oauth import model_mint_refusal_cause, model_obo_cause_key

        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()
        state = _make_app_state(storage, http_client=client, oidc_config=make_oidc_config())
        _seed_credential(state)

        assert _mint(state, grant_leg="rfc8693") is None
        assert client.post.call_count == 0
        assert (
            model_mint_refusal_cause(
                "model_obo", model_obo_cause_key(MODEL_ALIAS, grant_leg="rfc8693"), USER
            )
            == "grant_profile_mismatch"
        )

        rfc_state = _make_app_state(
            storage,
            http_client=client,
            oidc_config=make_oidc_config(obo_grant_profile="rfc8693"),
        )
        assert _mint(rfc_state, grant_leg="entra") is None
        assert client.post.call_count == 0
        assert (
            model_mint_refusal_cause(
                "model_obo", model_obo_cause_key(MODEL_ALIAS, grant_leg="entra"), USER
            )
            == "grant_profile_mismatch"
        )

    def test_identity_cache_keys_stay_index_safe_and_disjoint(self) -> None:
        """Console-written aliases (≤64 ASCII) key literally; a pathological
        DB-direct alias — over-bound multibyte, or control-embedded — still
        yields an index-safe key (PostgreSQL btree tuple limit: 2704 bytes)
        under the builder's OWN prefix, distinct per alias, and a control
        character can never forge the digest spelling's separator shape.
        """
        from turnstone.core.mcp_oauth import (
            MODEL_APP_CACHE_PREFIX,
            MODEL_OBO_CACHE_PREFIX,
            model_app_cache_server,
            model_obo_cache_server,
        )

        # The whole console-legal range keys literally.
        assert model_obo_cache_server("gw.model-1") == f"{MODEL_OBO_CACHE_PREFIX}gw.model-1"
        assert model_app_cache_server("gw.model-1") == f"{MODEL_APP_CACHE_PREFIX}gw.model-1"
        # Same alias, different mode prefixes: distinct rows by construction.
        assert model_obo_cache_server("gw.model-1") != model_app_cache_server("gw.model-1")

        # Pathological DB-direct aliases: over-bound multibyte collapses to
        # the digest spelling, still under the index bound, prefix kept,
        # distinct per alias.
        multibyte = "ü" * 3000
        mb_key = model_obo_cache_server(multibyte)
        assert len(mb_key.encode("utf-8")) < 2704
        assert mb_key.startswith(MODEL_OBO_CACHE_PREFIX)
        assert mb_key != model_obo_cache_server("ö" * 3000)
        app_key = model_app_cache_server(multibyte)
        assert len(app_key.encode("utf-8")) < 2704
        assert app_key.startswith(MODEL_APP_CACHE_PREFIX)

        # Control characters strip at the key build, so a crafted alias can
        # never spell the digest form's separator-after-prefix shape and
        # alias another identity's bounded key.
        forged = chr(0x1F) + "a" * 48
        assert model_obo_cache_server(forged) == f"{MODEL_OBO_CACHE_PREFIX}" + "a" * 48
        assert chr(0x1F) not in model_obo_cache_server(forged)
        assert chr(0x1F) not in model_app_cache_server(forged)

    def test_scopes_without_exchange_leg_is_a_caller_error(self, storage: SQLiteBackend) -> None:
        """Only the token-exchange leg reads scopes; passing them without
        pinning that leg is a dispatch bug at the call site, not an operator
        state, so it raises instead of returning the fallback-eligible None.
        """
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()
        state = _make_app_state(storage, http_client=client, oidc_config=make_oidc_config())
        _seed_credential(state)
        with pytest.raises(ValueError, match="grant_leg='rfc8693'"):
            _mint(state, scopes="aud-gw")
        with pytest.raises(ValueError, match="grant_leg='rfc8693'"):
            _mint(state, scopes="aud-gw", grant_leg="entra")
        assert client.post.call_count == 0

    def test_over_length_scopes_is_a_caller_error(self, storage: SQLiteBackend) -> None:
        """Every production path bounds scopes at the registry/console before
        the mint sees them, so an over-cap value here is a raw call site's
        bug — raised as the dispatch contract error, never silently sliced
        into a narrower privilege request than the caller asked for.
        """
        from turnstone.core.mcp_oauth import MintDispatchContractError
        from turnstone.core.model_registry import MODEL_AUTH_TEXT_MAX_LEN

        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()
        state = _make_app_state(storage, http_client=client, oidc_config=make_oidc_config())
        _seed_credential(state)
        with pytest.raises(MintDispatchContractError, match=str(MODEL_AUTH_TEXT_MAX_LEN)):
            _mint(state, scopes="s" * (MODEL_AUTH_TEXT_MAX_LEN + 1), grant_leg="rfc8693")
        assert client.post.call_count == 0

    def test_sibling_aliases_on_one_audience_keep_separate_causes(
        self, storage: SQLiteBackend
    ) -> None:
        """The refusal-cause record keys on the OWNING ALIAS, so two
        definitions fronting the same gateway audience are separate mint
        identities end to end: one's successful mint never clears — or
        overwrites — the other's recorded cause.
        """
        from turnstone.core.mcp_oauth import model_mint_refusal_cause, model_obo_cause_key

        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            side_effect=[
                # Broken sibling: refresh lands, exchange refused.
                _mk_response(200, {"access_token": "subject-at", "expires_in": 300}),
                _mk_response(400, {"error": "invalid_request"}),
                # Healthy sibling: both legs succeed.
                _mk_response(200, {"access_token": "subject-at2", "expires_in": 300}),
                _mk_response(200, {"access_token": "exchanged", "expires_in": 3600}),
            ]
        )
        state = _make_app_state(
            storage,
            http_client=client,
            oidc_config=make_oidc_config(obo_grant_profile="rfc8693"),
        )
        _seed_credential(state)

        assert _mint(state, alias="broken-model", scopes="aud-gw", grant_leg="rfc8693") is None
        broken_key = model_obo_cause_key("broken-model", "rfc8693")
        assert model_mint_refusal_cause("model_obo", broken_key, USER) == "mint_failed"

        assert _mint(state, alias="healthy-model", grant_leg="rfc8693") == "exchanged"
        # The sibling's success cleared only ITS key; the broken record holds.
        assert model_mint_refusal_cause("model_obo", broken_key, USER) == "mint_failed"
        assert (
            model_mint_refusal_cause(
                "model_obo", model_obo_cause_key("healthy-model", grant_leg="rfc8693"), USER
            )
            == ""
        )

    def test_shared_audience_mode_variants_keep_separate_causes(
        self, storage: SQLiteBackend
    ) -> None:
        """The refusal-cause record also carries the pinned leg, so two
        mode-variants sharing an audience never overwrite each other's
        recorded cause — the leg axis of the scope-variant pin above.
        """
        from turnstone.core.mcp_oauth import model_mint_refusal_cause, model_obo_cause_key

        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            side_effect=[
                # rfc8693-leg mint: refresh lands, exchange refused.
                _mk_response(200, {"access_token": "subject-at", "expires_in": 300}),
                _mk_response(400, {"error": "invalid_request"}),
            ]
        )
        state = _make_app_state(
            storage,
            http_client=client,
            oidc_config=make_oidc_config(obo_grant_profile="rfc8693"),
        )
        _seed_credential(state)

        assert _mint(state, grant_leg="rfc8693") is None
        rfc_key = model_obo_cause_key(MODEL_ALIAS, grant_leg="rfc8693")
        assert model_mint_refusal_cause("model_obo", rfc_key, USER) == "mint_failed"

        # An entra-leg mint under the SAME alias (an edit history crossing
        # modes) refuses before the IdP — and must stamp only ITS leg's key,
        # never the rfc8693 record.
        assert _mint(state, grant_leg="entra") is None
        assert model_mint_refusal_cause("model_obo", rfc_key, USER) == "mint_failed"
        assert (
            model_mint_refusal_cause(
                "model_obo", model_obo_cause_key(MODEL_ALIAS, grant_leg="entra"), USER
            )
            == "grant_profile_mismatch"
        )

    def test_config_repair_clears_the_cooldown_immediately(self, storage: SQLiteBackend) -> None:
        """Cooldown keys on (alias, shape), not the alias alone: a mint
        failure arms the cooldown for the shape that failed, and an
        operator's config repair — a different audience/scopes — is a clean
        slate whose first retry mints immediately. The in-process cooldown
        is per node and the console purge reaches only DB rows, so without
        the shape axis a fail-closed deployment would keep failing user
        turns for the full window after the fix.
        """
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            side_effect=[
                _mk_response(400, {"error": "invalid_grant", "error_description": "bad aud"}),
                _mk_response(200, {"access_token": "at-fixed", "expires_in": 3600}),
            ]
        )
        state = _make_app_state(storage, http_client=client, oidc_config=make_oidc_config())
        _seed_credential(state)

        # The misconfigured audience fails and arms the cooldown.
        assert _mint(state, audience="api://wrong") is None
        assert client.post.call_count == 1
        # Same shape inside the window: short-circuit, zero IdP traffic.
        assert _mint(state, audience="api://wrong") is None
        assert client.post.call_count == 1
        # The repaired audience is a different shape: mints on the FIRST try.
        assert _mint(state, audience="api://right") == "at-fixed"
        assert client.post.call_count == 2

    def test_broken_alias_cooldown_does_not_suppress_sibling_alias(
        self, storage: SQLiteBackend
    ) -> None:
        """Cooldown arms on the identity cache key, so a broken alias cannot
        suppress a sibling alias sharing its gateway audience.
        """
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            side_effect=[
                # Broken alias: refresh leg lands, exchange leg is refused.
                _mk_response(200, {"access_token": "subject-at", "expires_in": 300}),
                _mk_response(400, {"error": "invalid_request"}),
                # Sibling alias afterwards: both legs succeed.
                _mk_response(200, {"access_token": "subject-at2", "expires_in": 300}),
                _mk_response(200, {"access_token": "exchanged-at", "expires_in": 3600}),
            ]
        )
        state = _make_app_state(
            storage,
            http_client=client,
            oidc_config=make_oidc_config(obo_grant_profile="rfc8693"),
        )
        _seed_credential(state)

        assert _mint(state, alias="broken-model", scopes="aud-gw", grant_leg="rfc8693") is None
        # The broken alias is in cooldown; the sibling still mints.
        assert _mint(state, alias="healthy-model", grant_leg="rfc8693") == "exchanged-at"
        assert client.post.call_count == 4
        # And the broken alias's cooldown still holds.
        assert _mint(state, alias="broken-model", scopes="aud-gw", grant_leg="rfc8693") is None
        assert client.post.call_count == 4

    def test_cause_record_map_evicts_least_recently_stamped_at_cap(self) -> None:
        """The cause map is readback state, not a log-dedup set: when full it
        evicts the LEAST-RECENTLY-STAMPED record and always records the
        newest refusal. Recency is stamp order, not first-insertion order —
        a re-stamped record moves to the newest position, so the hottest
        record (the one an operator is actively debugging) is evicted last,
        never first.
        """
        from turnstone.core import mcp_oauth as mcp_oauth_module

        for i in range(mcp_oauth_module._CAUSE_RECORD_CAP):
            mcp_oauth_module._record_mint_refusal_cause("model_obo", f"k{i}", "u", "c")
        assert len(mcp_oauth_module._MODEL_MINT_LAST_CAUSE) == mcp_oauth_module._CAUSE_RECORD_CAP
        # Re-stamp the OLDEST record: dict overwrite alone would leave it at
        # its original insertion slot and the next eviction would hit it.
        mcp_oauth_module._record_mint_refusal_cause("model_obo", "k0", "u", "hot")
        mcp_oauth_module._record_mint_refusal_cause("model_obo", "k-new", "u", "newest")
        assert len(mcp_oauth_module._MODEL_MINT_LAST_CAUSE) == mcp_oauth_module._CAUSE_RECORD_CAP
        assert mcp_oauth_module.model_mint_refusal_cause("model_obo", "k-new", "u") == "newest"
        # The re-stamped record survives; the least-recently-stamped (k1) went.
        assert mcp_oauth_module.model_mint_refusal_cause("model_obo", "k0", "u") == "hot"
        assert mcp_oauth_module.model_mint_refusal_cause("model_obo", "k1", "u") == ""


# ---------------------------------------------------------------------------
# mint_app_access_token — app-identity (client-credentials) mint
# ---------------------------------------------------------------------------


def _mint_app(state: SimpleNamespace, **kwargs: Any) -> Any:
    kwargs.setdefault("alias", APP_ALIAS)
    kwargs.setdefault("audience", AUDIENCE)

    async def _run() -> Any:
        return await mint_app_access_token(app_state=state, **kwargs)

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
        # Cached in the DB under the synthetic __app__ user, identity-keyed
        # on the owning alias — second call, no IdP.
        assert _mint_app(state) == "app-at"
        assert client.post.call_count == 1
        cache_server = f"__model_app__:{APP_ALIAS}"
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

    def test_control_characters_stripped_from_audience_and_alias(
        self, storage: SQLiteBackend
    ) -> None:
        """Raw-caller hygiene, matching the OBO twin: control characters
        strip from the audience before the wire request and the cache row,
        and from the alias inside the key builder — so no control byte ever
        reaches the IdP, the row columns, or the identity key. Controls
        strip BEFORE the whitespace trim: the edge control below shields a
        space that the trim must still remove afterwards.
        """
        from turnstone.core.mcp_oauth import model_app_cache_server

        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            return_value=_mk_response(200, {"access_token": "app-at", "expires_in": 3600})
        )
        state = _make_app_state(storage, http_client=client, oidc_config=make_oidc_config())

        dirty_audience = chr(0x01) + " " + AUDIENCE[:8] + chr(0x1F) + AUDIENCE[8:]
        dirty_alias = "gw-" + chr(0x01) + "app"
        assert _mint_app(state, alias=dirty_alias, audience=dirty_audience) == "app-at"
        # The wire request carries the stripped audience.
        assert client.post.call_args.kwargs["data"]["scope"] == f"{AUDIENCE}/.default"
        # The cache row lives under the stripped identity key with the
        # stripped audience column.
        raw = storage.get_mcp_user_token("__app__", model_app_cache_server("gw-app"))
        assert raw is not None
        plain = state.mcp_token_store.get_user_token("__app__", model_app_cache_server("gw-app"))
        assert plain is not None and plain["audience"] == AUDIENCE


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
        alias: str = "tf",
        auth_mode: str = "entra_obo",
        obo_audience: str = AUDIENCE,
        obo_scopes: str = "",
    ) -> ModelConfig:
        return ModelConfig(
            alias=alias,
            base_url="https://gateway.example.com",
            api_key=api_key,
            model="vmg/opus",
            provider=provider,
            auth_mode=auth_mode,
            obo_audience=obo_audience,
            obo_scopes=obo_scopes,
        )

    def test_obo_alias_with_user_returns_token(self) -> None:
        reg = _registry_with(self._obo_cfg())
        sess = _fake_session(registry=reg, user_id=USER, mint_token="minted-jwt")
        assert ChatSession._model_backend_auth_token(sess, "tf") == "minted-jwt"
        # The mode pins its grant leg; entra_obo never forwards scopes. The
        # owning alias rides along — the mint's cache and cause key.
        sess._mcp_mint_client.mint_model_obo_token_sync.assert_called_once_with(
            user_id=USER, alias="tf", audience=AUDIENCE, scopes="", grant_leg="entra"
        )

    def test_rfc8693_alias_passes_scopes_and_leg(self) -> None:
        cfg = self._obo_cfg(alias="tf-kc", auth_mode="rfc8693_obo", obo_scopes="aud-gw openid")
        sess = _fake_session(registry=_registry_with(cfg), user_id=USER, mint_token="minted-jwt")
        assert ChatSession._model_backend_auth_token(sess, "tf-kc") == "minted-jwt"
        sess._mcp_mint_client.mint_model_obo_token_sync.assert_called_once_with(
            user_id=USER,
            alias="tf-kc",
            audience=AUDIENCE,
            scopes="aud-gw openid",
            grant_leg="rfc8693",
        )

    def test_rfc8693_no_user_context_refuses_and_never_mints(self) -> None:
        """The no-user guard derives from the app-identity complement, so the
        new delegated mode inherits it rather than needing its own arm."""
        cfg = self._obo_cfg(alias="tf-kc", auth_mode="rfc8693_obo", obo_scopes="aud-gw")
        sess = _fake_session(registry=_registry_with(cfg), user_id=None, mint_token="never")
        with pytest.raises(BackendAuthUnavailableError):
            ChatSession._model_backend_auth_token(sess, "tf-kc")
        sess._mcp_mint_client.mint_model_obo_token_sync.assert_not_called()

    def test_rfc8693_fallback_warn_reads_scoped_cause(
        self, storage: SQLiteBackend, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The heartbeat keys its cause readback by the OWNING ALIAS, so an
        alias's refusal names ITS cause rather than a sibling definition's
        record (or unknown)."""
        import logging

        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(
            side_effect=[
                _mk_response(200, {"access_token": "subject-at", "expires_in": 300}),
                _mk_response(400, {"error": "invalid_request"}),
            ]
        )
        state = _make_app_state(
            storage,
            http_client=client,
            oidc_config=make_oidc_config(obo_grant_profile="rfc8693"),
        )
        _seed_credential(state)
        assert _mint(state, alias="tf-kc", scopes="aud-gw", grant_leg="rfc8693") is None

        cfg = self._obo_cfg(alias="tf-kc", auth_mode="rfc8693_obo", obo_scopes="aud-gw")
        sess = _fake_session(registry=_registry_with(cfg), user_id=USER, mint_token=None)
        with caplog.at_level(logging.WARNING):
            assert ChatSession._model_backend_auth_token(sess, "tf-kc") is None
        matching = [
            r
            for r in caplog.records
            if "model_obo.fallback_to_static" in r.getMessage() + str(r.__dict__)
        ]
        assert matching, caplog.records
        blob = " ".join(r.getMessage() + str(r.__dict__) for r in matching)
        assert "mint_failed" in blob

    def test_entra_obo_scopes_residue_stays_inert(self) -> None:
        """Stored scopes on a mode outside SCOPES_MODEL_AUTH_MODES never reach
        the mint — the dispatch, not the store, is what keeps residue inert."""
        cfg = self._obo_cfg(obo_scopes="stale-scope")
        sess = _fake_session(registry=_registry_with(cfg), user_id=USER, mint_token="minted-jwt")
        assert ChatSession._model_backend_auth_token(sess, "tf") == "minted-jwt"
        sess._mcp_mint_client.mint_model_obo_token_sync.assert_called_once_with(
            user_id=USER, alias="tf", audience=AUDIENCE, scopes="", grant_leg="entra"
        )

    def test_fallback_warn_names_last_recorded_mint_cause(
        self, storage: SQLiteBackend, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The per-turn fallback warn names the last recorded cause inline."""
        import logging

        # A refused mint records its cause (typo'd grant profile), under the
        # same entra leg the entra_obo dispatch below pins.
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()
        state = _make_app_state(
            storage,
            http_client=client,
            oidc_config=make_oidc_config(obo_grant_profile="bogus"),
        )
        _seed_credential(state)
        assert _mint(state, alias="tf", grant_leg="entra") is None

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

        # Aliased and legged like the entra_obo dispatch below, so the
        # record lands on the key its heartbeat reads.
        assert _mint(state, alias="tf", grant_leg="entra") is None
        assert client.post.call_count == 0  # refused before any IdP traffic

        from turnstone.core.mcp_oauth import model_mint_refusal_cause, model_obo_cause_key

        assert (
            model_mint_refusal_cause(
                "model_obo", model_obo_cause_key("tf", grant_leg="entra"), USER
            )
            == "credential_decrypt_failure"
        )

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
            alias="tf",
            audience=AUDIENCE,
            scopes="",
            grant_leg="entra",
        )
        session._mcp_mint_client.mint_app_token_sync.assert_not_called()

    def test_main_lane_carries_backend_auth_resolver(self) -> None:
        """The main loop's lane wires the session's mint resolver; the
        credential then resolves and binds INSIDE model_turn per attempt,
        after its entry abort read (the #972 ordering — a pre-set Stop
        mints nothing, pinned in test_cancel)."""
        sess = MagicMock()
        sess._registry = None
        sess._config_store = None
        sess.temperature = 0.5
        sess.reasoning_effort = None

        lane = ChatSession._build_main_lane(
            sess,
            provider=MagicMock(provider_name="openai-compatible"),
            client=MagicMock(),
            model="vmg/opus",
            alias="tf",
            capabilities=SimpleNamespace(),
        )

        assert lane.backend_auth_resolver is sess._model_backend_auth_token
        assert lane.alias == "tf"
        # The session's own sampling knobs override the lane's operator
        # rungs.
        assert lane.temperature == 0.5
        assert lane.reasoning_effort is None

    def test_main_lane_never_consults_config_store(self) -> None:
        """The alias/global sampling rungs must not reach the main loop:
        the session's own knobs replace both values ``config_store`` would
        feed, so ``_build_main_lane`` omits the store entirely and a store
        with configured rungs contributes nothing to the lane."""
        sess = MagicMock()
        sess._registry = None
        store = MagicMock()
        sess._config_store = store
        sess.temperature = None
        sess.reasoning_effort = "high"

        lane = ChatSession._build_main_lane(
            sess,
            provider=MagicMock(provider_name="openai-compatible"),
            client=MagicMock(),
            model="m",
            alias="a",
            capabilities=SimpleNamespace(),
        )

        assert not store.mock_calls
        assert lane.temperature is None
        assert lane.reasoning_effort == "high"

    def test_primary_lane_built_with_session_alias_for_obo(self) -> None:
        # Regression: the primary lane must carry the session alias, or the
        # backend-auth resolver can't resolve the OBO token and an
        # entra_obo main turn goes out on the static client key.  The lane
        # build is the one place the alias enters.
        sess = MagicMock()
        sess._model_alias = "oboagent"
        ChatSession._model_turn_with_fallback(sess, MagicMock(), lambda wire: wire)
        sess._build_main_lane.assert_called_once()
        assert sess._build_main_lane.call_args.kwargs["alias"] == "oboagent"

    def test_fail_closed_refusal_never_enters_model_fallback_chain(self) -> None:
        sess = MagicMock()
        sess._model_alias = "oboagent"
        sess._model_turn_with_retry.side_effect = BackendAuthUnavailableError("mint failed")
        sess._registry.fallback = ["static-backup"]
        tracker = MagicMock()
        sess._get_health_tracker.return_value = tracker

        with pytest.raises(BackendAuthUnavailableError):
            ChatSession._model_turn_with_fallback(sess, MagicMock(), lambda wire: wire)

        sess._try_fallback_lane.assert_not_called()
        # An auth refusal is never reinterpreted as backend health.
        tracker.record_failure.assert_not_called()

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

    def test_unclassified_delegated_mode_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A delegated mode with no registered grant-profile pairing cannot
        pin a leg, so the dispatch refuses loudly before the mint bridge —
        minting with leg=None would run the pre-dedicated-mode overload."""
        monkeypatch.setattr("turnstone.core.session.MODEL_AUTH_MODE_PROFILES", {})
        reg = _registry_with(self._obo_cfg())
        sess = _fake_session(registry=reg, user_id=USER, mint_token="never")
        with pytest.raises(BackendAuthUnavailableError, match="grant-profile pairing"):
            ChatSession._model_backend_auth_token(sess, "tf")
        sess._mcp_mint_client.mint_model_obo_token_sync.assert_not_called()

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
        sess._mcp_mint_client.mint_app_token_sync.assert_called_once_with(
            alias="tf", audience=AUDIENCE
        )
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


class TestMintBridgeContractViolation:
    def test_contract_error_returns_none_and_logs_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The sync bridge demotes ordinary mint failures to debug, but a
        MintDispatchContractError is a caller-contract violation (scopes
        without the exchange leg pinned) and must surface at ERROR — while
        still returning the fallback-eligible None."""
        import logging
        import threading

        from turnstone.core import mcp_client as mcp_client_module
        from turnstone.core.mcp_oauth import MintDispatchContractError

        async def _raiser(**_kwargs: Any) -> str | None:
            raise MintDispatchContractError(
                "mint_obo_access_token: scopes require grant_leg='rfc8693'"
            )

        monkeypatch.setattr(mcp_client_module, "mint_obo_access_token", _raiser)

        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        try:
            stub = SimpleNamespace(_loop=loop, _app_state=object())
            with caplog.at_level(logging.DEBUG):
                token = mcp_client_module.MCPClientManager.mint_model_obo_token_sync(
                    stub, user_id=USER, alias=MODEL_ALIAS, audience=AUDIENCE, scopes="aud-gw"
                )
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            loop.close()

        assert token is None
        errors = [
            r
            for r in caplog.records
            if r.levelno == logging.ERROR and "contract violation" in r.getMessage()
        ]
        assert errors, caplog.records
