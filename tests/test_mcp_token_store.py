"""Tests for ``MCPTokenStore`` ciphertext-aware CRUD.

Phase 3 of the OAuth-MCP RFC: validates the encrypt/decrypt boundary
between :class:`MCPTokenStore` and the storage protocol's ciphertext-only
columns.  Exercises the row-not-deleted-on-decrypt-failure invariant.
"""

from __future__ import annotations

import base64

import pytest
from cryptography.fernet import Fernet

from turnstone.core.mcp_crypto import (
    MCPTokenCipher,
    MCPTokenCipherConfig,
    MCPTokenDecryptError,
    MCPTokenStore,
)


def _make_cipher() -> MCPTokenCipher:
    raw = base64.urlsafe_b64decode(Fernet.generate_key())
    return MCPTokenCipher(MCPTokenCipherConfig(keys=(raw,)))


def _make_store(backend, *, audit: bool = False) -> tuple[MCPTokenStore, MCPTokenCipher]:
    cipher = _make_cipher()
    store = MCPTokenStore(
        backend,
        cipher,
        node_id="test-node",
        audit_storage=backend if audit else None,
    )
    return store, cipher


def _seed_server(backend, *, server_id: str = "srv-id-1", name: str = "srv-a") -> str:
    backend.create_mcp_server(
        server_id=server_id,
        name=name,
        transport="streamable-http",
        url="https://mcp.example.com/sse",
        auth_type="oauth_user",
    )
    return server_id


# ---------------------------------------------------------------------------
# User-token CRUD
# ---------------------------------------------------------------------------


class TestUserTokenCRUD:
    def test_create_and_get_round_trip(self, backend) -> None:
        store, _ = _make_store(backend)
        store.create_user_token(
            "u1",
            "srv-a",
            access_token="access-aaa",
            refresh_token="refresh-bbb",
            expires_at="2026-05-04T12:00:00",
            scopes="openid profile",
            as_issuer="https://auth.example.com",
            audience="https://mcp.example.com",
        )
        plain = store.get_user_token("u1", "srv-a")
        assert plain is not None
        assert plain["user_id"] == "u1"
        assert plain["server_name"] == "srv-a"
        assert plain["access_token"] == "access-aaa"
        assert plain["refresh_token"] == "refresh-bbb"
        assert plain["scopes"] == "openid profile"
        assert plain["audience"] == "https://mcp.example.com"

    def test_create_with_no_refresh_token(self, backend) -> None:
        store, _ = _make_store(backend)
        store.create_user_token(
            "u1",
            "srv-a",
            access_token="access-only",
            refresh_token=None,
            expires_at=None,
            scopes=None,
            as_issuer="https://auth.example.com",
            audience="https://mcp.example.com",
        )
        plain = store.get_user_token("u1", "srv-a")
        assert plain is not None
        assert plain["access_token"] == "access-only"
        assert plain["refresh_token"] is None

    def test_get_missing_returns_none(self, backend) -> None:
        store, _ = _make_store(backend)
        assert store.get_user_token("nobody", "srv-a") is None

    def test_update_after_refresh(self, backend) -> None:
        store, _ = _make_store(backend)
        store.create_user_token(
            "u1",
            "srv-a",
            access_token="old-access",
            refresh_token="old-refresh",
            expires_at="2026-05-04T12:00:00",
            scopes="openid",
            as_issuer="https://auth.example.com",
            audience="https://mcp.example.com",
        )
        ok = store.update_user_token_after_refresh(
            "u1",
            "srv-a",
            access_token="new-access",
            refresh_token="new-refresh",
            expires_at="2026-05-04T13:00:00",
        )
        assert ok is True
        plain = store.get_user_token("u1", "srv-a")
        assert plain is not None
        assert plain["access_token"] == "new-access"
        assert plain["refresh_token"] == "new-refresh"
        assert plain["expires_at"] == "2026-05-04T13:00:00"
        # Preserved columns:
        assert plain["scopes"] == "openid"
        assert plain["as_issuer"] == "https://auth.example.com"
        # last_refreshed got stamped:
        assert plain["last_refreshed"] is not None

    def test_update_after_refresh_missing_row_returns_false(self, backend) -> None:
        store, _ = _make_store(backend)
        ok = store.update_user_token_after_refresh(
            "u1",
            "srv-a",
            access_token="x",
            refresh_token=None,
            expires_at=None,
        )
        assert ok is False

    def test_delete(self, backend) -> None:
        store, _ = _make_store(backend)
        store.create_user_token(
            "u1",
            "srv-a",
            access_token="a",
            refresh_token=None,
            expires_at=None,
            scopes=None,
            as_issuer="https://auth.example.com",
            audience="https://mcp.example.com",
        )
        assert store.delete_user_token("u1", "srv-a") is True
        assert store.get_user_token("u1", "srv-a") is None
        # Idempotent: deleting again returns False.
        assert store.delete_user_token("u1", "srv-a") is False


# ---------------------------------------------------------------------------
# Client-secret writer
# ---------------------------------------------------------------------------


class TestClientSecretWriter:
    def test_set_oauth_client_secret_round_trip(self, backend) -> None:
        store, cipher = _make_store(backend)
        server_id = _seed_server(backend)
        ok = store.set_oauth_client_secret(server_id, "plaintext-secret")
        assert ok is True
        # Read raw via get_mcp_server: ciphertext != plaintext, decrypts back.
        raw = backend.get_mcp_server(server_id)
        assert raw is not None
        ct = raw["oauth_client_secret_ct"]
        assert isinstance(ct, (bytes, bytearray, memoryview))
        ct_bytes = bytes(ct)
        assert ct_bytes != b"plaintext-secret"
        assert cipher.decrypt(ct_bytes) == b"plaintext-secret"

    def test_set_oauth_client_secret_clear_with_none(self, backend) -> None:
        store, _ = _make_store(backend)
        server_id = _seed_server(backend)
        store.set_oauth_client_secret(server_id, "x")
        assert store.set_oauth_client_secret(server_id, None) is True
        raw = backend.get_mcp_server(server_id)
        assert raw is not None
        assert raw["oauth_client_secret_ct"] is None

    def test_set_oauth_client_secret_missing_server_returns_false(self, backend) -> None:
        store, _ = _make_store(backend)
        ok = store.set_oauth_client_secret("does-not-exist", "x")
        assert ok is False


# ---------------------------------------------------------------------------
# Decrypt failure: row preservation invariant
# ---------------------------------------------------------------------------


class TestDecryptFailureInvariant:
    def test_get_user_token_with_wrong_key_raises_decrypt_error(self, backend) -> None:
        """CRITICAL: when no installed key can decrypt a stored row,
        ``get_user_token`` MUST NOT auto-delete the row.  The row is
        still valid; this node just doesn't have the right key.
        """
        # Write under cipher A.
        store_a, _cipher_a = _make_store(backend)
        store_a.create_user_token(
            "u1",
            "srv-a",
            access_token="secret-access",
            refresh_token="secret-refresh",
            expires_at="2026-05-04T12:00:00",
            scopes="openid",
            as_issuer="https://auth.example.com",
            audience="https://mcp.example.com",
        )
        raw_before = backend.get_mcp_user_token("u1", "srv-a")
        assert raw_before is not None
        ct_before = bytes(raw_before["access_token_ct"])

        # Read under cipher B (different key).
        store_b, cipher_b = _make_store(backend)
        with pytest.raises(MCPTokenDecryptError) as exc_info:
            store_b.get_user_token("u1", "srv-a")
        # The exception carries the keys we tried — useful for audit.
        assert exc_info.value.key_fingerprints_attempted == cipher_b.key_fingerprints

        # Row MUST still exist with ciphertext intact.
        raw_after = backend.get_mcp_user_token("u1", "srv-a")
        assert raw_after is not None
        assert bytes(raw_after["access_token_ct"]) == ct_before

    def test_decrypt_failure_emits_audit_when_configured(self, backend) -> None:
        """When ``audit_storage`` is set, decrypt failures emit a
        ``mcp_server.oauth.token_decrypt_failure`` audit event."""
        store_a, _ = _make_store(backend)
        store_a.create_user_token(
            "u1",
            "srv-a",
            access_token="x",
            refresh_token=None,
            expires_at=None,
            scopes=None,
            as_issuer="https://a",
            audience="https://m",
        )

        store_b, cipher_b = _make_store(backend, audit=True)
        with pytest.raises(MCPTokenDecryptError):
            store_b.get_user_token("u1", "srv-a")

        events = backend.list_audit_events(limit=10)
        actions = {ev.get("action") for ev in events}
        assert "mcp_server.oauth.token_decrypt_failure" in actions
