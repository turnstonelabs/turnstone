"""Tests for ``MCPTokenStore.list_user_token_metadata``.

Validates the non-secret projection used by the settings UI: ciphertext
columns are stripped, ordering is preserved, and the empty case returns
``[]``. Decrypt is intentionally skipped — the list view must never need
the access/refresh secrets.
"""

from __future__ import annotations

import base64

import sqlalchemy as sa
from cryptography.fernet import Fernet

from turnstone.core.mcp_crypto import (
    MCPTokenCipher,
    MCPTokenCipherConfig,
    MCPTokenStore,
)


def _make_cipher() -> MCPTokenCipher:
    raw = base64.urlsafe_b64decode(Fernet.generate_key())
    return MCPTokenCipher(MCPTokenCipherConfig(keys=(raw,)))


def _make_store(backend) -> MCPTokenStore:
    return MCPTokenStore(backend, _make_cipher(), node_id="test-node")


def _seed_token(
    store: MCPTokenStore,
    backend,
    *,
    user_id: str,
    server_name: str,
    created: str,
) -> None:
    """Create a token via the store and backdate ``created`` for ordering."""
    store.create_user_token(
        user_id,
        server_name,
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at="2026-05-04T12:00:00",
        scopes="openid profile",
        as_issuer="https://auth.example.com",
        audience="https://mcp.example.com",
    )
    with backend._engine.connect() as conn:
        conn.execute(
            sa.text(
                "UPDATE mcp_user_tokens SET created = :created "
                "WHERE user_id = :uid AND server_name = :sn"
            ),
            {"created": created, "uid": user_id, "sn": server_name},
        )
        conn.commit()


class TestListUserTokenMetadata:
    def test_list_user_token_metadata_returns_non_secret_fields_only(self, backend) -> None:
        store = _make_store(backend)
        _seed_token(
            store, backend, user_id="u1", server_name="srv-a", created="2026-05-01T00:00:00"
        )
        rows = store.list_user_token_metadata("u1")
        assert len(rows) == 1
        meta = rows[0]
        # Secrets MUST be absent.
        assert "access_token" not in meta
        assert "refresh_token" not in meta
        assert "access_token_ct" not in meta
        assert "refresh_token_ct" not in meta
        # Non-secret columns surface verbatim.
        assert meta["user_id"] == "u1"
        assert meta["server_name"] == "srv-a"
        assert meta["scopes"] == "openid profile"
        assert meta["as_issuer"] == "https://auth.example.com"
        assert meta["audience"] == "https://mcp.example.com"
        assert meta["expires_at"] == "2026-05-04T12:00:00"
        assert meta["created"] == "2026-05-01T00:00:00"
        assert meta["last_refreshed"] is None

    def test_list_user_token_metadata_empty(self, backend) -> None:
        store = _make_store(backend)
        assert store.list_user_token_metadata("nobody") == []

    def test_list_user_token_metadata_preserves_creation_order(self, backend) -> None:
        store = _make_store(backend)
        _seed_token(
            store, backend, user_id="u1", server_name="srv-c", created="2026-05-03T00:00:00"
        )
        _seed_token(
            store, backend, user_id="u1", server_name="srv-a", created="2026-05-01T00:00:00"
        )
        _seed_token(
            store, backend, user_id="u1", server_name="srv-b", created="2026-05-02T00:00:00"
        )
        rows = store.list_user_token_metadata("u1")
        assert [r["server_name"] for r in rows] == ["srv-a", "srv-b", "srv-c"]
        assert [r["created"] for r in rows] == [
            "2026-05-01T00:00:00",
            "2026-05-02T00:00:00",
            "2026-05-03T00:00:00",
        ]
