"""Tests for ``initialize_mcp_crypto_state`` startup gate.

Phase 3 of the OAuth-MCP RFC: validates fail-loud behavior when an
operator forgets the encryption key on a node that hosts OAuth-protected
MCP server rows.
"""

from __future__ import annotations

import re
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
        # Operator-actionable error message names BOTH supported config-key
        # forms so an operator using the rotation list (plural) is not
        # misled into thinking only the singular form is valid.
        messages = " ".join(record.message for record in caplog.records)
        assert "mcp_token_encryption_keys" in messages
        assert re.search(r"mcp_token_encryption_key(?!s)", messages) is not None

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

    def test_capture_key_guard_fires_even_when_oidc_discovery_failed(
        self, backend, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Review finding: the capture-credential key guard must NOT depend on
        oidc_config.enabled. A node that boots while the IdP is unreachable comes
        up enabled=False (discovery_retryable=True); gating the guard on enabled
        would silently skip the loud boot-time failure exactly then, and runtime
        rediscovery later re-enables OIDC so the first login persists a refresh
        token with no key. capture_user_credential=True + no key must SystemExit
        regardless of the (transient) discovery state — no oauth_obo rows exist,
        so only the capture guard can catch this."""
        _patch_security(monkeypatch, {})  # no encryption key
        # enabled=False models a boot-time discovery failure; capture opt-in on.
        state = types.SimpleNamespace(
            oidc_config=types.SimpleNamespace(
                enabled=False,
                issuer="https://idp.example.com",
                capture_user_credential=True,
                discovery_retryable=True,
            )
        )
        with (
            caplog.at_level("ERROR", logger="turnstone.core.mcp_crypto"),
            pytest.raises(SystemExit) as exc_info,
        ):
            initialize_mcp_crypto_state(state, node_id="n1")
        assert exc_info.value.code == 1
        messages = " ".join(record.message for record in caplog.records)
        assert "capture_user_credential" in messages

    def test_capture_with_key_starts_even_when_oidc_disabled(
        self, backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The converse: capture opt-in WITH a key installed boots cleanly even
        while discovery is down — the cipher/store land so a later rediscovery's
        first capture has somewhere encrypted to persist."""
        _patch_security(monkeypatch, {"mcp_token_encryption_key": Fernet.generate_key().decode()})
        state = types.SimpleNamespace(
            oidc_config=types.SimpleNamespace(
                enabled=False,
                issuer="https://idp.example.com",
                capture_user_credential=True,
                discovery_retryable=True,
            )
        )
        initialize_mcp_crypto_state(state, node_id="n1")
        assert isinstance(state.mcp_token_cipher, MCPTokenCipher)
        assert isinstance(state.mcp_token_store, MCPTokenStore)
