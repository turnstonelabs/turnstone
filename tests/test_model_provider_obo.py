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
* :func:`obo_auth_headers` — provider→credential-header mapping;
* ``ModelRegistry.get_client`` constructing an OBO backend that has no static
  fallback key;
* ``ChatSession._model_auth_headers`` — the per-call resolve at the model call
  site (static alias / no user / failed mint all fall back to the static key).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from tests.conftest import make_mcp_token_cipher
from turnstone.core.mcp_crypto import MCPTokenStore
from turnstone.core.mcp_oauth import mint_obo_access_token
from turnstone.core.model_registry import ModelConfig, ModelRegistry, load_model_registry
from turnstone.core.oidc import OIDCConfig
from turnstone.core.providers import obo_auth_headers
from turnstone.core.session import ChatSession
from turnstone.core.storage._sqlite import SQLiteBackend

USER = "user-1"
ISSUER = "https://idp.test"
TOKEN_ENDPOINT = "https://idp.test/token"
AUDIENCE = "https://models.example.com"

_MIGRATIONS_DIR = str(
    Path(__file__).resolve().parent.parent / "turnstone" / "core" / "storage" / "migrations"
)


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
        assert storage.update_model_definition(
            "d3", auth_mode="entra_obo", obo_audience=AUDIENCE
        )
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


# ---------------------------------------------------------------------------
# obo_auth_headers — provider → credential header
# ---------------------------------------------------------------------------


class TestOboAuthHeaders:
    def test_anthropic_uses_x_api_key(self) -> None:
        assert obo_auth_headers("anthropic", "TOK") == {"x-api-key": "TOK"}
        assert obo_auth_headers("anthropic-compatible", "TOK") == {"x-api-key": "TOK"}

    def test_openai_style_uses_bearer(self) -> None:
        # Capital "Authorization" — must match the OpenAI SDK's own default-auth
        # header key, else the SDK emits two Authorization headers (dup 400 /
        # stale client key wins) instead of the override replacing it.
        for prov in ("openai", "openai-compatible", "google", "xai"):
            assert obo_auth_headers(prov, "TOK") == {"Authorization": "Bearer TOK"}


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
        assert self._seen_api_key(cfg, monkeypatch) == "obo-placeholder-unused"

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


def _make_oidc_config(**overrides: Any) -> OIDCConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "issuer": ISSUER,
        "client_id": "cid",
        "client_secret": "csecret",
        "token_endpoint": TOKEN_ENDPOINT,
    }
    defaults.update(overrides)
    return OIDCConfig(**defaults)


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
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        _seed_credential(state)

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
        # Second call serves the DB mint-cache row — zero extra IdP round-trips.
        token2 = _mint(state)
        assert token2 == "at-minted"
        assert client.post.call_count == 1

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
            oidc_config=_make_oidc_config(),
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
            oidc_config=_make_oidc_config(),
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
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
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
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        _seed_credential(state)

        assert _mint(state) == "at-1"
        assert _mint(state, force_refresh=True) == "at-2"
        assert client.post.call_count == 2

    def test_missing_credential_returns_none_no_http(self, storage: SQLiteBackend) -> None:
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        # No captured credential seeded.
        assert _mint(state) is None
        assert client.post.call_count == 0

    def test_oidc_disabled_returns_none_no_http(self, storage: SQLiteBackend) -> None:
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()
        state = _make_app_state(
            storage, http_client=client, oidc_config=_make_oidc_config(enabled=False)
        )
        _seed_credential(state)
        assert _mint(state) is None
        assert client.post.call_count == 0

    def test_unusable_profile_returns_none_no_http(self, storage: SQLiteBackend) -> None:
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock()
        state = _make_app_state(
            storage, http_client=client, oidc_config=_make_oidc_config(obo_grant_profile="")
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
        state = _make_app_state(storage, http_client=client, oidc_config=_make_oidc_config())
        _seed_credential(state)
        # A failed mint falls back to the static credential (None), and never
        # auto-deletes the shared credential.
        assert _mint(state) is None
        assert state.mcp_token_store.get_oidc_credential(USER, ISSUER) is not None


# ---------------------------------------------------------------------------
# ChatSession._model_auth_headers — resolve at the model call site
# ---------------------------------------------------------------------------


def _fake_session(
    *, registry: ModelRegistry | None, user_id: str | None, mint_token: str | None
) -> SimpleNamespace:
    """Minimal stand-in exposing exactly what ``_model_auth_headers`` reads."""
    mcp = SimpleNamespace(
        mint_model_obo_token_sync=MagicMock(return_value=mint_token),
    )
    return SimpleNamespace(
        _registry=registry,
        _mcp_client=mcp,
        _mcp_effective_user_id=user_id,
    )


def _registry_with(cfg: ModelConfig) -> ModelRegistry:
    return ModelRegistry(models={cfg.alias: cfg}, default=cfg.alias)


class TestModelAuthHeaders:
    def _obo_cfg(self, provider: str = "anthropic") -> ModelConfig:
        return ModelConfig(
            alias="tf",
            base_url="https://gateway.example.com",
            api_key="static-fallback",
            model="vmg/opus",
            provider=provider,
            auth_mode="entra_obo",
            obo_audience=AUDIENCE,
        )

    def test_obo_alias_with_user_returns_minted_header(self) -> None:
        reg = _registry_with(self._obo_cfg())
        sess = _fake_session(registry=reg, user_id=USER, mint_token="minted-jwt")
        headers = ChatSession._model_auth_headers(sess, "tf")
        assert headers == {"x-api-key": "minted-jwt"}
        sess._mcp_client.mint_model_obo_token_sync.assert_called_once_with(
            user_id=USER, audience=AUDIENCE
        )

    def test_openai_surface_obo_uses_bearer(self) -> None:
        reg = _registry_with(self._obo_cfg(provider="openai-compatible"))
        sess = _fake_session(registry=reg, user_id=USER, mint_token="minted-jwt")
        headers = ChatSession._model_auth_headers(sess, "tf")
        assert headers == {"Authorization": "Bearer minted-jwt"}

    def test_primary_stream_forwards_alias_for_obo(self) -> None:
        # Regression: the primary _create_stream_with_retry call must pass
        # model_alias, or _model_auth_headers("") can't resolve the OBO override
        # and an entra_obo main turn goes out on the static client key. The
        # fallback path and utility (title) completions always passed the alias;
        # the primary path silently didn't.
        sess = MagicMock()
        sess._model_alias = "oboagent"
        ChatSession._create_stream_with_retry(sess, [{"role": "user", "content": "hi"}])
        sess._try_stream.assert_called_once()
        assert sess._try_stream.call_args.kwargs.get("model_alias") == "oboagent"

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
        assert ChatSession._model_auth_headers(sess, "plain") is None
        sess._mcp_client.mint_model_obo_token_sync.assert_not_called()

    def test_no_user_context_returns_none_and_never_mints(self) -> None:
        reg = _registry_with(self._obo_cfg())
        sess = _fake_session(registry=reg, user_id="", mint_token="unused")
        assert ChatSession._model_auth_headers(sess, "tf") is None
        sess._mcp_client.mint_model_obo_token_sync.assert_not_called()

    def test_failed_mint_falls_back_to_static(self) -> None:
        reg = _registry_with(self._obo_cfg())
        sess = _fake_session(registry=reg, user_id=USER, mint_token=None)
        # Mint returned None (no credential / rejected) → None so the static
        # client credential stands.
        assert ChatSession._model_auth_headers(sess, "tf") is None

    def test_unknown_alias_returns_none(self) -> None:
        reg = _registry_with(self._obo_cfg())
        sess = _fake_session(registry=reg, user_id=USER, mint_token="x")
        assert ChatSession._model_auth_headers(sess, "does-not-exist") is None
