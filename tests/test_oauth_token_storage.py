"""Shared token lifecycle and MCP projections on the selected storage backend."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa

from turnstone.core.model_oauth import model_app_cache_server, model_obo_cache_server
from turnstone.core.token_store.store import MODEL_APP_MINT_PRINCIPAL

if TYPE_CHECKING:
    from turnstone.core.storage._protocol import StorageBackend


def _seed_token(
    storage: StorageBackend, user: str, key: str, *, expires: str | None = None
) -> None:
    storage.create_oauth_token(
        user,
        key,
        access_token_ct=b"access",
        refresh_token_ct=None,
        expires_at=expires,
        scopes="scope",
        as_issuer="https://idp.example.com",
        audience="api://resource",
    )


def _seed_auth_state(storage: StorageBackend, key: str) -> None:
    storage.create_mcp_oauth_pending_state(f"state-{key}", "u1", key, "verifier", "/settings")
    storage.upsert_mcp_pending_consent("u1", key, "needs_auth", None, "2026-01-01T00:00:00")


def test_model_lifecycle_purge_only_deletes_the_alias_tokens(backend: StorageBackend) -> None:
    from turnstone.console.server import _purge_model_mint_cache

    obo_key = model_obo_cache_server("gateway")
    app_key = model_app_cache_server("gateway")
    sibling = model_obo_cache_server("sibling")
    for key in (obo_key, app_key, sibling, "mcp-server"):
        for user in ("u1", "u2", MODEL_APP_MINT_PRINCIPAL):
            _seed_token(backend, user, key)
        _seed_auth_state(backend, key)
    backend.upsert_oidc_user_credential(
        "u1", "https://idp.example.com", refresh_token_ct=b"credential"
    )
    credential = backend.get_oidc_user_credential("u1", "https://idp.example.com")
    pending_consent = backend.list_mcp_pending_consent_by_user("u1")

    _purge_model_mint_cache(backend, "definition-1", "gateway")

    for user in ("u1", "u2", MODEL_APP_MINT_PRINCIPAL):
        assert backend.get_oauth_token(user, obo_key) is None
        assert backend.get_oauth_token(user, app_key) is None
        assert backend.get_oauth_token(user, sibling) is not None
        assert backend.get_oauth_token(user, "mcp-server") is not None
    assert backend.get_oidc_user_credential("u1", "https://idp.example.com") == credential
    assert backend.list_mcp_pending_consent_by_user("u1") == pending_consent
    for key in (obo_key, app_key, sibling, "mcp-server"):
        assert backend.pop_mcp_oauth_pending_state(f"state-{key}") is not None
    assert backend.delete_oauth_tokens_by_key(obo_key) == 0
    assert backend.delete_oauth_tokens_by_key(sibling) == 3


def test_mcp_purge_rolls_back_both_deletes_and_preserves_pending_consent(backend: Any) -> None:
    for key in ("mcp-server", "other-server"):
        for user in ("u1", "u2"):
            _seed_token(backend, user, key)
        _seed_auth_state(backend, key)
    backend.upsert_oidc_user_credential(
        "u1", "https://idp.example.com", refresh_token_ct=b"credential"
    )
    pending_consent = backend.list_mcp_pending_consent_by_user("u1")
    credential = backend.get_oidc_user_credential("u1", "https://idp.example.com")
    rows = [backend.get_oauth_token(user, "mcp-server") for user in ("u1", "u2")]

    def fail_pending_delete(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        if statement.startswith("DELETE FROM mcp_oauth_pending"):
            raise RuntimeError("controlled pending-state delete failure")

    sa.event.listen(backend._engine, "before_cursor_execute", fail_pending_delete)
    try:
        with pytest.raises(RuntimeError, match="controlled pending-state delete failure"):
            backend.delete_mcp_oauth_rows_by_server_name("mcp-server")
    finally:
        sa.event.remove(backend._engine, "before_cursor_execute", fail_pending_delete)
    assert [backend.get_oauth_token(user, "mcp-server") for user in ("u1", "u2")] == rows

    assert backend.delete_mcp_oauth_rows_by_server_name("mcp-server") == 3
    assert backend.delete_mcp_oauth_rows_by_server_name("mcp-server") == 0
    for user in ("u1", "u2"):
        assert backend.get_oauth_token(user, "mcp-server") is None
        assert backend.get_oauth_token(user, "other-server") is not None
    assert backend.pop_mcp_oauth_pending_state("state-mcp-server") is None
    assert backend.pop_mcp_oauth_pending_state("state-other-server") is not None
    assert backend.list_mcp_pending_consent_by_user("u1") == pending_consent
    assert backend.get_oidc_user_credential("u1", "https://idp.example.com") == credential


def test_user_delete_removes_shared_tokens_and_credentials(backend: StorageBackend) -> None:
    keys = (
        "mcp-custodial",
        "mcp-obo",
        model_obo_cache_server("gateway"),
        "__model_obo__:api://legacy",
    )
    for user in ("u1", "u2"):
        backend.create_user(user, user, user, "hash")
        for key in keys:
            _seed_token(backend, user, key)
        backend.upsert_oidc_user_credential(
            user, "https://idp.example.com", refresh_token_ct=b"credential"
        )
    app_key = model_app_cache_server("gateway")
    _seed_token(backend, MODEL_APP_MINT_PRINCIPAL, app_key)
    other_rows = backend.list_oauth_token_metadata_by_user("u2")
    app_row = backend.get_oauth_token(MODEL_APP_MINT_PRINCIPAL, app_key)

    assert backend.delete_user("u1") is True

    assert backend.list_oauth_token_metadata_by_user("u1") == []
    assert backend.get_oidc_user_credential("u1", "https://idp.example.com") is None
    assert backend.list_oauth_token_metadata_by_user("u2") == other_rows
    assert backend.get_oidc_user_credential("u2", "https://idp.example.com") is not None
    assert backend.get_oauth_token(MODEL_APP_MINT_PRINCIPAL, app_key) == app_row


def test_mcp_counts_and_sweep_use_the_renamed_token_key(backend: StorageBackend) -> None:
    for key, auth_type in (("mcp-custodial", "oauth_user"), ("mcp-obo", "oauth_obo")):
        backend.create_mcp_server(
            key, key, "streamable-http", url="https://mcp.example.com", auth_type=auth_type
        )
    _seed_token(backend, "no-expiry", "mcp-custodial")
    _seed_token(backend, "fresh", "mcp-custodial", expires="2099-01-01T00:00:00")
    _seed_token(backend, "expired", "mcp-custodial", expires="2000-01-01T00:00:00")
    _seed_token(backend, "u1", "mcp-obo")
    _seed_token(backend, "u1", model_obo_cache_server("gateway"))
    _seed_token(backend, MODEL_APP_MINT_PRINCIPAL, model_app_cache_server("gateway"))

    assert backend.count_mcp_consented_users_by_server("mcp-custodial") == 2
    assert backend.count_mcp_consented_users_grouped_by_server()["mcp-custodial"] == 2
    targets = backend.list_mcp_user_token_reconcile_targets()
    assert {(user, key) for user, key, _ in targets} == {
        ("no-expiry", "mcp-custodial"),
        ("fresh", "mcp-custodial"),
        ("expired", "mcp-custodial"),
    }
    for user, key, exercised in targets:
        row = backend.get_oauth_token(user, key)
        assert row is not None and exercised == row["created"]
