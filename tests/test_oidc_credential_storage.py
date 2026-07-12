"""Storage CRUD tests for ``oidc_user_credentials`` (single-credential MCP minting, #551).

Validates the storage-protocol additions for the captured per-(user, issuer)
IdP refresh token:

- ``upsert_oidc_user_credential`` (create-or-replace semantics)
- ``get_oidc_user_credential``
- ``update_oidc_user_credential_refresh`` (rotation write-back)
- ``delete_oidc_user_credential``
- ``delete_user`` cascade

plus the ``MCPTokenStore`` encrypt/decrypt wrappers over the same rows.
"""

from __future__ import annotations

from tests.conftest import make_mcp_token_cipher
from turnstone.core.mcp_crypto import MCPTokenStore

ISS = "https://login.example.test/tenant-1/v2.0"


class TestUpsertAndGet:
    def test_round_trip(self, backend) -> None:
        backend.upsert_oidc_user_credential("u1", ISS, refresh_token_ct=b"ct-1")
        row = backend.get_oidc_user_credential("u1", ISS)
        assert row is not None
        assert row["user_id"] == "u1"
        assert row["issuer"] == ISS
        assert row["refresh_token_ct"] == b"ct-1"
        assert row["created"] == row["last_refreshed"]

    def test_get_missing_returns_none(self, backend) -> None:
        assert backend.get_oidc_user_credential("nobody", ISS) is None

    def test_keyed_by_user_and_issuer(self, backend) -> None:
        backend.upsert_oidc_user_credential("u1", ISS, refresh_token_ct=b"ct-1")
        assert backend.get_oidc_user_credential("u1", "https://other.test") is None
        assert backend.get_oidc_user_credential("u2", ISS) is None

    def test_upsert_replaces_on_conflict(self, backend) -> None:
        """A fresh login must overwrite a stale credential; ``created`` survives."""
        backend.upsert_oidc_user_credential("u1", ISS, refresh_token_ct=b"ct-old")
        first = backend.get_oidc_user_credential("u1", ISS)
        assert first is not None
        backend.upsert_oidc_user_credential("u1", ISS, refresh_token_ct=b"ct-new")
        second = backend.get_oidc_user_credential("u1", ISS)
        assert second is not None
        assert second["refresh_token_ct"] == b"ct-new"
        assert second["created"] == first["created"]


class TestRotationWriteBack:
    def test_update_rewrites_token(self, backend) -> None:
        backend.upsert_oidc_user_credential("u1", ISS, refresh_token_ct=b"ct-1")
        assert backend.update_oidc_user_credential_refresh("u1", ISS, refresh_token_ct=b"ct-2")
        row = backend.get_oidc_user_credential("u1", ISS)
        assert row is not None
        assert row["refresh_token_ct"] == b"ct-2"

    def test_update_missing_returns_false(self, backend) -> None:
        assert not backend.update_oidc_user_credential_refresh(
            "nobody", ISS, refresh_token_ct=b"ct"
        )


class TestDelete:
    def test_delete_existing(self, backend) -> None:
        backend.upsert_oidc_user_credential("u1", ISS, refresh_token_ct=b"ct-1")
        assert backend.delete_oidc_user_credential("u1", ISS)
        assert backend.get_oidc_user_credential("u1", ISS) is None

    def test_delete_missing_returns_false(self, backend) -> None:
        assert not backend.delete_oidc_user_credential("nobody", ISS)

    def test_delete_user_cascades_credential(self, backend) -> None:
        backend.upsert_oidc_user_credential("u-doomed", ISS, refresh_token_ct=b"ct-1")
        backend.delete_user("u-doomed")
        assert backend.get_oidc_user_credential("u-doomed", ISS) is None


class TestTokenStoreWrappers:
    def test_encrypt_decrypt_round_trip(self, backend) -> None:
        store = MCPTokenStore(backend, make_mcp_token_cipher())
        store.upsert_oidc_credential("u1", ISS, refresh_token="rt-plaintext")
        plain = store.get_oidc_credential("u1", ISS)
        assert plain is not None
        assert plain["refresh_token"] == "rt-plaintext"
        # Ciphertext at rest — the raw row must not contain the plaintext.
        raw = backend.get_oidc_user_credential("u1", ISS)
        assert raw is not None
        assert b"rt-plaintext" not in raw["refresh_token_ct"]

    def test_redeem_write_back_round_trip(self, backend) -> None:
        store = MCPTokenStore(backend, make_mcp_token_cipher())
        store.upsert_oidc_credential("u1", ISS, refresh_token="rt-first")
        assert store.update_oidc_credential_after_redeem("u1", ISS, refresh_token="rt-rotated")
        plain = store.get_oidc_credential("u1", ISS)
        assert plain is not None
        assert plain["refresh_token"] == "rt-rotated"

    def test_get_missing_returns_none(self, backend) -> None:
        store = MCPTokenStore(backend, make_mcp_token_cipher())
        assert store.get_oidc_credential("nobody", ISS) is None

    def test_delete_via_store(self, backend) -> None:
        store = MCPTokenStore(backend, make_mcp_token_cipher())
        store.upsert_oidc_credential("u1", ISS, refresh_token="rt")
        assert store.delete_oidc_credential("u1", ISS)
        assert store.get_oidc_credential("u1", ISS) is None
