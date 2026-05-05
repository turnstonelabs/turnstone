"""Tests for ``initialize_mcp_crypto_state`` startup gate.

Phase 3 of the OAuth-MCP RFC: validates fail-loud behavior when an
operator forgets the encryption key on a node that hosts OAuth-protected
MCP server rows.
"""

from __future__ import annotations

import types

import pytest
from cryptography.fernet import Fernet

import turnstone.core.config as cfg_mod
from turnstone.core.mcp_crypto import (
    MCPTokenCipher,
    MCPTokenStore,
    initialize_mcp_crypto_state,
)


def _patch_security(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    """Override ``load_config('security')`` to return ``payload``."""

    def fake(section: str | None = None) -> dict:
        if section == "security":
            return payload
        return {}

    monkeypatch.setattr(cfg_mod, "load_config", fake)


class TestInitializeMcpCryptoState:
    def test_startup_succeeds_with_no_oauth_user_rows_and_no_key(
        self, backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Common case: no key, no oauth_user rows -> sentinels installed."""
        _patch_security(monkeypatch, {})

        state = types.SimpleNamespace()
        initialize_mcp_crypto_state(state, node_id="n1")

        assert state.mcp_token_cipher is None
        assert state.mcp_token_store is None

    def test_startup_succeeds_with_key_and_oauth_user_row(
        self, backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Operator has wired a key and at least one oauth_user row.

        Cipher + store should land on app_state.
        """
        # Plant an oauth_user row.
        backend.create_mcp_server(
            server_id="srv-1",
            name="oauth-srv",
            transport="streamable-http",
            url="https://mcp.example.com/sse",
            auth_type="oauth_user",
        )
        _patch_security(
            monkeypatch,
            {"mcp_token_encryption_key": Fernet.generate_key().decode()},
        )

        state = types.SimpleNamespace()
        initialize_mcp_crypto_state(state, node_id="n1")

        assert isinstance(state.mcp_token_cipher, MCPTokenCipher)
        assert isinstance(state.mcp_token_store, MCPTokenStore)

    def test_startup_aborts_with_oauth_user_row_and_no_key(
        self, backend, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Misconfiguration: oauth_user row exists, no key -> SystemExit(1)."""
        backend.create_mcp_server(
            server_id="srv-1",
            name="oauth-srv",
            transport="streamable-http",
            url="https://mcp.example.com/sse",
            auth_type="oauth_user",
        )
        _patch_security(monkeypatch, {})

        state = types.SimpleNamespace()
        with (
            caplog.at_level("ERROR", logger="turnstone.core.mcp_crypto"),
            pytest.raises(SystemExit) as exc_info,
        ):
            initialize_mcp_crypto_state(state, node_id="n1")
        assert exc_info.value.code == 1
        # Operator-actionable error message names the missing config key.
        assert any("mcp_token_encryption_key" in record.message for record in caplog.records)

    def test_startup_aborts_with_invalid_key(
        self, backend, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Malformed key material should fail loud at startup, not at first use."""
        _patch_security(monkeypatch, {"mcp_token_encryption_key": "###not-base64###"})

        state = types.SimpleNamespace()
        with (
            caplog.at_level("ERROR", logger="turnstone.core.mcp_crypto"),
            pytest.raises(SystemExit) as exc_info,
        ):
            initialize_mcp_crypto_state(state, node_id="n1")
        assert exc_info.value.code == 1
