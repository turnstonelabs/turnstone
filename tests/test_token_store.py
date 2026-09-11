"""The generic ciphertext boundary works without MCP server lookups or audit policy."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.conftest import make_mcp_token_cipher
from turnstone.core.token_store.crypto import TokenDecryptError
from turnstone.core.token_store.store import TokenStore

if TYPE_CHECKING:
    from turnstone.core.storage._protocol import StorageBackend


def test_generic_store_round_trip_and_decrypt_hook(
    backend: StorageBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_mcp_lookup(*args: object) -> None:
        pytest.fail("generic token storage consulted an MCP server")

    monkeypatch.setattr(backend, "get_mcp_server_by_name", unexpected_mcp_lookup)
    store = TokenStore(backend, make_mcp_token_cipher())
    store.create_user_token(
        "u1",
        "generic-key",
        access_token="access",
        refresh_token="refresh",
        expires_at=None,
        scopes=None,
        as_issuer="https://idp.example.com",
        audience="api://resource",
    )
    store.upsert_oidc_credential("u1", "https://idp.example.com", refresh_token="credential")
    token = store.get_user_token("u1", "generic-key")
    credential = store.get_oidc_credential("u1", "https://idp.example.com")
    assert token is not None and token["access_token"] == "access"
    assert credential is not None and credential["refresh_token"] == "credential"
    token_row = backend.get_mcp_user_token("u1", "generic-key")
    credential_row = backend.get_oidc_user_credential("u1", "https://idp.example.com")
    assert token_row is not None and token_row["access_token_ct"] != b"access"
    assert credential_row is not None and credential_row["refresh_token_ct"] != b"credential"

    events: list[tuple[str, tuple[str, ...]]] = []
    wrong_cipher = make_mcp_token_cipher()
    reader = TokenStore(
        backend,
        wrong_cipher,
        on_decrypt_failure=lambda key, fingerprints: events.append((key, fingerprints)),
    )
    with pytest.raises(TokenDecryptError):
        reader.get_user_token("u1", "generic-key")
    with pytest.raises(TokenDecryptError):
        reader.get_oidc_credential("u1", "https://idp.example.com")
    assert events == [
        ("generic-key", wrong_cipher.key_fingerprints),
        ("oidc:https://idp.example.com", wrong_cipher.key_fingerprints),
    ]
    assert backend.get_mcp_user_token("u1", "generic-key") == token_row
    assert backend.get_oidc_user_credential("u1", "https://idp.example.com") == credential_row
