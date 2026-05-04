"""Tests for turnstone.core.oidc — OIDC authentication support."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import types
import urllib.parse
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import jwt as pyjwt
import pytest

from tests.conftest import make_oidc_test_config as _make_config
from turnstone.core.oidc import (
    OIDCError,
    OIDCKeyNotFoundError,
    _ensure_default_role,
    _sanitize_log_text,
    apply_role_mapping,
    build_authorize_url,
    discover_oidc,
    exchange_code,
    generate_pkce_verifier,
    initialize_oidc_state,
    load_oidc_config,
    provision_oidc_user,
    validate_discovered_endpoint,
    validate_id_token,
    validate_issuer_url,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_storage(**overrides):
    """Build a MagicMock with sensible storage defaults."""
    s = MagicMock()
    s.get_oidc_identity.return_value = overrides.get("identity")
    s.get_user.return_value = overrides.get("user")
    s.get_user_by_username.return_value = overrides.get("user_by_username")
    s.get_role.return_value = overrides.get("role")
    s.find_existing_usernames.return_value = overrides.get("existing_usernames", set())
    s.replace_oidc_roles.return_value = overrides.get("replace_oidc_roles", (set(), set()))
    return s


def _mock_async_client(mock_get):
    """Build a patched httpx.AsyncClient context manager for async tests."""

    class _AsyncCtx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url):
            return await mock_get(url)

    return _AsyncCtx()


# ---------------------------------------------------------------------------
# Config Loading
# ---------------------------------------------------------------------------


class TestLoadOIDCConfig:
    def test_load_oidc_config_from_env(self, monkeypatch):
        monkeypatch.setenv("TURNSTONE_OIDC_ISSUER", "https://auth.example.com")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_ID", "cid")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("TURNSTONE_OIDC_SCOPES", "openid")
        monkeypatch.setenv("TURNSTONE_OIDC_PROVIDER_NAME", "Okta")

        with patch("turnstone.core.config.load_config", return_value={}):
            cfg = load_oidc_config()

        assert cfg.enabled is True
        assert cfg.issuer == "https://auth.example.com"
        assert cfg.client_id == "cid"
        assert cfg.client_secret == "csecret"
        assert cfg.scopes == "openid"
        assert cfg.provider_name == "Okta"

    def test_load_oidc_config_disabled_when_missing(self, monkeypatch):
        monkeypatch.delenv("TURNSTONE_OIDC_ISSUER", raising=False)
        monkeypatch.delenv("TURNSTONE_OIDC_CLIENT_ID", raising=False)
        monkeypatch.delenv("TURNSTONE_OIDC_CLIENT_SECRET", raising=False)

        with patch("turnstone.core.config.load_config", return_value={}):
            cfg = load_oidc_config()

        assert cfg.enabled is False

    def test_load_oidc_config_partial_env(self, monkeypatch):
        """Only issuer set, no client_id -> enabled=False."""
        monkeypatch.setenv("TURNSTONE_OIDC_ISSUER", "https://auth.example.com")
        monkeypatch.delenv("TURNSTONE_OIDC_CLIENT_ID", raising=False)
        monkeypatch.delenv("TURNSTONE_OIDC_CLIENT_SECRET", raising=False)

        with patch("turnstone.core.config.load_config", return_value={}):
            cfg = load_oidc_config()

        assert cfg.enabled is False
        assert cfg.issuer == "https://auth.example.com"
        assert cfg.client_id == ""

    def test_load_oidc_config_role_map_parsing(self, monkeypatch):
        monkeypatch.setenv("TURNSTONE_OIDC_ISSUER", "https://auth.example.com")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_ID", "cid")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("TURNSTONE_OIDC_ROLE_CLAIM", "roles")
        monkeypatch.setenv("TURNSTONE_OIDC_ROLE_MAP", "admin:builtin-admin,eng:builtin-operator")

        with patch("turnstone.core.config.load_config", return_value={}):
            cfg = load_oidc_config()

        assert cfg.role_claim == "roles"
        assert cfg.role_map == {"admin": "builtin-admin", "eng": "builtin-operator"}

    def test_load_oidc_config_password_enabled_false(self, monkeypatch):
        monkeypatch.setenv("TURNSTONE_OIDC_ISSUER", "https://auth.example.com")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_ID", "cid")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("TURNSTONE_OIDC_PASSWORD_ENABLED", "false")

        with patch("turnstone.core.config.load_config", return_value={}):
            cfg = load_oidc_config()

        assert cfg.enabled is True
        assert cfg.password_enabled is False

    def test_load_oidc_config_password_enabled_true(self, monkeypatch):
        monkeypatch.setenv("TURNSTONE_OIDC_ISSUER", "https://auth.example.com")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_ID", "cid")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("TURNSTONE_OIDC_PASSWORD_ENABLED", "true")

        with patch("turnstone.core.config.load_config", return_value={}):
            cfg = load_oidc_config()

        assert cfg.password_enabled is True

    def test_load_oidc_config_role_map_empty_entries(self, monkeypatch):
        """Role map with empty/whitespace entries should be silently skipped."""
        monkeypatch.setenv("TURNSTONE_OIDC_ISSUER", "https://auth.example.com")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_ID", "cid")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("TURNSTONE_OIDC_ROLE_MAP", "admin:builtin-admin, , :, foo:")

        with patch("turnstone.core.config.load_config", return_value={}):
            cfg = load_oidc_config()

        assert cfg.role_map == {"admin": "builtin-admin"}

    def test_load_oidc_config_defaults(self, monkeypatch):
        """Defaults for scopes and provider_name when not set."""
        monkeypatch.setenv("TURNSTONE_OIDC_ISSUER", "https://auth.example.com")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_ID", "cid")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_SECRET", "csecret")
        monkeypatch.delenv("TURNSTONE_OIDC_SCOPES", raising=False)
        monkeypatch.delenv("TURNSTONE_OIDC_PROVIDER_NAME", raising=False)

        with patch("turnstone.core.config.load_config", return_value={}):
            cfg = load_oidc_config()

        assert cfg.scopes == "openid email profile"
        assert cfg.provider_name == "SSO"

    def test_load_oidc_config_redirect_base_from_env(self, monkeypatch):
        """TURNSTONE_OIDC_REDIRECT_BASE populates redirect_base."""
        monkeypatch.setenv("TURNSTONE_OIDC_ISSUER", "https://auth.example.com")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_ID", "cid")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("TURNSTONE_OIDC_REDIRECT_BASE", "https://app.example.com")

        with patch("turnstone.core.config.load_config", return_value={}):
            cfg = load_oidc_config()

        assert cfg.redirect_base == "https://app.example.com"

    def test_load_oidc_config_redirect_base_strips_trailing_slash(self, monkeypatch):
        """Trailing slashes are stripped from redirect_base."""
        monkeypatch.setenv("TURNSTONE_OIDC_ISSUER", "https://auth.example.com")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_ID", "cid")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("TURNSTONE_OIDC_REDIRECT_BASE", "https://app.example.com/")

        with patch("turnstone.core.config.load_config", return_value={}):
            cfg = load_oidc_config()

        assert cfg.redirect_base == "https://app.example.com"

    def test_load_oidc_config_redirect_base_default_empty(self, monkeypatch):
        """redirect_base defaults to empty string when not set."""
        monkeypatch.setenv("TURNSTONE_OIDC_ISSUER", "https://auth.example.com")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_ID", "cid")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_SECRET", "csecret")
        monkeypatch.delenv("TURNSTONE_OIDC_REDIRECT_BASE", raising=False)

        with patch("turnstone.core.config.load_config", return_value={}):
            cfg = load_oidc_config()

        assert cfg.redirect_base == ""

    def test_load_oidc_config_redirect_base_rejects_path(self, monkeypatch):
        """redirect_base with a path component is rejected (falls back to empty)."""
        monkeypatch.setenv("TURNSTONE_OIDC_ISSUER", "https://auth.example.com")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_ID", "cid")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("TURNSTONE_OIDC_REDIRECT_BASE", "https://app.example.com/subpath")

        with patch("turnstone.core.config.load_config", return_value={}):
            cfg = load_oidc_config()

        assert cfg.redirect_base == ""

    def test_load_oidc_config_redirect_base_rejects_no_scheme(self, monkeypatch):
        """redirect_base without a scheme is rejected."""
        monkeypatch.setenv("TURNSTONE_OIDC_ISSUER", "https://auth.example.com")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_ID", "cid")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("TURNSTONE_OIDC_REDIRECT_BASE", "app.example.com")

        with patch("turnstone.core.config.load_config", return_value={}):
            cfg = load_oidc_config()

        assert cfg.redirect_base == ""

    def test_load_oidc_config_redirect_base_rejects_userinfo(self, monkeypatch):
        """redirect_base with userinfo (user:pass@host) is rejected."""
        monkeypatch.setenv("TURNSTONE_OIDC_ISSUER", "https://auth.example.com")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_ID", "cid")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("TURNSTONE_OIDC_REDIRECT_BASE", "https://user:pass@app.example.com")

        with patch("turnstone.core.config.load_config", return_value={}):
            cfg = load_oidc_config()

        assert cfg.redirect_base == ""

    def test_load_oidc_config_redirect_base_rejects_invalid_port(self, monkeypatch):
        """redirect_base with non-numeric port is rejected."""
        monkeypatch.setenv("TURNSTONE_OIDC_ISSUER", "https://auth.example.com")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_ID", "cid")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("TURNSTONE_OIDC_REDIRECT_BASE", "https://app.example.com:abc")

        with patch("turnstone.core.config.load_config", return_value={}):
            cfg = load_oidc_config()

        assert cfg.redirect_base == ""

    def test_load_oidc_config_redirect_base_rejects_missing_hostname(self, monkeypatch):
        """redirect_base without a hostname is rejected."""
        monkeypatch.setenv("TURNSTONE_OIDC_ISSUER", "https://auth.example.com")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_ID", "cid")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("TURNSTONE_OIDC_REDIRECT_BASE", "https://")

        with patch("turnstone.core.config.load_config", return_value={}):
            cfg = load_oidc_config()

        assert cfg.redirect_base == ""

    def test_load_oidc_config_redirect_base_allows_http(self, monkeypatch):
        """http:// redirect_base is allowed (with warning) for local dev."""
        monkeypatch.setenv("TURNSTONE_OIDC_ISSUER", "https://auth.example.com")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_ID", "cid")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("TURNSTONE_OIDC_REDIRECT_BASE", "http://localhost:8000")

        with patch("turnstone.core.config.load_config", return_value={}):
            cfg = load_oidc_config()

        assert cfg.redirect_base == "http://localhost:8000"

    def test_load_oidc_config_parses_trusted_endpoint_hosts(self, monkeypatch):
        """TURNSTONE_OIDC_TRUSTED_ENDPOINT_HOSTS is split, lowercased, and trimmed."""
        monkeypatch.setenv("TURNSTONE_OIDC_ISSUER", "https://auth.example.com")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_ID", "cid")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_SECRET", "csecret")
        monkeypatch.setenv(
            "TURNSTONE_OIDC_TRUSTED_ENDPOINT_HOSTS",
            "Foo.Example.com, BAR.example.com  ,, ",
        )

        with patch("turnstone.core.config.load_config", return_value={}):
            cfg = load_oidc_config()

        assert cfg.trusted_endpoint_hosts == ("foo.example.com", "bar.example.com")

    def test_load_oidc_config_trusted_endpoint_hosts_default_empty(self, monkeypatch):
        """trusted_endpoint_hosts defaults to () when env var is absent."""
        monkeypatch.setenv("TURNSTONE_OIDC_ISSUER", "https://auth.example.com")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_ID", "cid")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_SECRET", "csecret")
        monkeypatch.delenv("TURNSTONE_OIDC_TRUSTED_ENDPOINT_HOSTS", raising=False)

        with patch("turnstone.core.config.load_config", return_value={}):
            cfg = load_oidc_config()

        assert cfg.trusted_endpoint_hosts == ()

    def test_load_oidc_config_trusted_endpoint_hosts_from_toml_list(self, monkeypatch):
        """config.toml may provide trusted_endpoint_hosts as a list."""
        monkeypatch.setenv("TURNSTONE_OIDC_ISSUER", "https://auth.example.com")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_ID", "cid")
        monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_SECRET", "csecret")
        monkeypatch.delenv("TURNSTONE_OIDC_TRUSTED_ENDPOINT_HOSTS", raising=False)

        with patch(
            "turnstone.core.config.load_config",
            return_value={"trusted_endpoint_hosts": ["FOO.example.com", "bar.example.com"]},
        ):
            cfg = load_oidc_config()

        assert cfg.trusted_endpoint_hosts == ("foo.example.com", "bar.example.com")


# ---------------------------------------------------------------------------
# SSRF Validation
# ---------------------------------------------------------------------------


class TestValidateIssuerURL:
    """Tests for ``validate_issuer_url`` SSRF protection."""

    def test_valid_https_url(self):
        """Public HTTPS issuer URL passes validation."""
        # Should not raise -- mock DNS to return a public IP.
        with patch(
            "socket.getaddrinfo",
            return_value=[
                (2, 1, 6, "", ("93.184.216.34", 0)),
            ],
        ):
            validate_issuer_url("https://idp.example.com")

    def test_rejects_http_non_localhost(self):
        """HTTP is rejected for non-localhost hosts."""
        with pytest.raises(OIDCError, match="must use HTTPS"):
            validate_issuer_url("http://idp.example.com")

    def test_allows_http_localhost(self):
        """HTTP is allowed for localhost (development)."""
        with patch(
            "socket.getaddrinfo",
            return_value=[
                (2, 1, 6, "", ("127.0.0.1", 0)),
            ],
        ):
            validate_issuer_url("http://localhost:8080")

    def test_allows_http_localhost_subdomain(self):
        """HTTP is allowed for *.localhost subdomains."""
        with patch(
            "socket.getaddrinfo",
            return_value=[
                (2, 1, 6, "", ("127.0.0.1", 0)),
            ],
        ):
            validate_issuer_url("http://keycloak.localhost:8080")

    def test_rejects_embedded_credentials(self):
        """URLs with userinfo (user:pass@host) are rejected."""
        with pytest.raises(OIDCError, match="embedded credentials"):
            validate_issuer_url("https://admin:secret@idp.example.com")

    def test_rejects_username_only(self):
        """URLs with just a username are rejected."""
        with pytest.raises(OIDCError, match="embedded credentials"):
            validate_issuer_url("https://admin@idp.example.com")

    def test_rejects_no_hostname(self):
        """URLs without a hostname are rejected."""
        with pytest.raises(OIDCError, match="no hostname"):
            validate_issuer_url("https://")

    def test_rejects_private_10_range(self):
        """Hostnames resolving to 10.x.x.x are rejected."""
        with (
            patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.1", 0))]),
            pytest.raises(OIDCError, match="non-public address.*10.0.0.1"),
        ):
            validate_issuer_url("https://internal.corp.example.com")

    def test_rejects_private_172_range(self):
        """Hostnames resolving to 172.16-31.x.x are rejected."""
        with (
            patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("172.16.0.1", 0))]),
            pytest.raises(OIDCError, match="non-public address.*172.16.0.1"),
        ):
            validate_issuer_url("https://internal.corp.example.com")

    def test_rejects_private_192_168_range(self):
        """Hostnames resolving to 192.168.x.x are rejected."""
        with (
            patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("192.168.1.1", 0))]),
            pytest.raises(OIDCError, match="non-public address.*192.168.1.1"),
        ):
            validate_issuer_url("https://internal.corp.example.com")

    def test_rejects_loopback_127(self):
        """Hostnames resolving to 127.x.x.x are rejected (non-localhost host)."""
        with (
            patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 0))]),
            pytest.raises(OIDCError, match="non-public address.*127.0.0.1"),
        ):
            validate_issuer_url("https://evil.example.com")

    def test_rejects_ipv6_loopback(self):
        """Hostnames resolving to ::1 are rejected (non-localhost host)."""
        with (
            patch("socket.getaddrinfo", return_value=[(10, 1, 6, "", ("::1", 0, 0, 0))]),
            pytest.raises(OIDCError, match="non-public address.*::1"),
        ):
            validate_issuer_url("https://evil.example.com")

    def test_rejects_ipv6_private(self):
        """Hostnames resolving to fc00::/7 are rejected."""
        with (
            patch("socket.getaddrinfo", return_value=[(10, 1, 6, "", ("fd00::1", 0, 0, 0))]),
            pytest.raises(OIDCError, match="non-public address.*fd00::1"),
        ):
            validate_issuer_url("https://evil.example.com")

    def test_rejects_link_local(self):
        """Hostnames resolving to link-local addresses are rejected."""
        with (
            patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("169.254.169.254", 0))]),
            pytest.raises(OIDCError, match="non-public address.*169.254.169.254"),
        ):
            validate_issuer_url("https://metadata.internal")

    def test_rejects_unresolvable_hostname(self):
        """DNS resolution failure is rejected."""
        import socket as _socket

        with (
            patch("socket.getaddrinfo", side_effect=_socket.gaierror("not found")),
            pytest.raises(OIDCError, match="cannot be resolved"),
        ):
            validate_issuer_url("https://nonexistent.invalid")

    def test_rejects_mixed_addresses(self):
        """If any resolved address is private, the URL is rejected."""
        with (
            patch(
                "socket.getaddrinfo",
                return_value=[
                    (2, 1, 6, "", ("93.184.216.34", 0)),
                    (2, 1, 6, "", ("10.0.0.1", 0)),
                ],
            ),
            pytest.raises(OIDCError, match="non-public address.*10.0.0.1"),
        ):
            validate_issuer_url("https://dual-homed.example.com")

    def test_discover_rejects_ssrf(self):
        """discover_oidc returns enabled=False when issuer URL fails SSRF check."""
        config = _make_config(
            issuer="http://10.0.0.1:8080",
            authorization_endpoint="",
            token_endpoint="",
            userinfo_endpoint="",
            jwks_uri="",
        )

        async def _run():
            result = await discover_oidc(config)
            assert result.enabled is False

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Discovered Endpoint Validation
# ---------------------------------------------------------------------------


class TestValidateDiscoveredEndpoint:
    """Tests for ``validate_discovered_endpoint`` SSRF + same-origin protection."""

    _PUBLIC_ADDR = [(2, 1, 6, "", ("93.184.216.34", 0))]
    _PRIVATE_ADDR = [(2, 1, 6, "", ("169.254.169.254", 0))]
    _LOOPBACK_ADDR = [(2, 1, 6, "", ("127.0.0.1", 0))]

    @staticmethod
    def _issuer(url: str = "https://idp.example.com") -> urllib.parse.ParseResult:
        return urllib.parse.urlparse(url)

    def test_valid_same_origin(self):
        """Same scheme/host/port as issuer passes."""
        with patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR):
            validate_discovered_endpoint(
                "https://idp.example.com/token",
                self._issuer(),
                allow_http=False,
                trusted_endpoint_hosts=frozenset(),
            )

    def test_rejects_http_when_issuer_is_https(self):
        """http:// discovered endpoint rejected when issuer is https://."""
        with (
            patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR),
            pytest.raises(OIDCError, match="must use HTTPS"),
        ):
            validate_discovered_endpoint(
                "http://idp.example.com/token",
                self._issuer(),
                allow_http=False,
                trusted_endpoint_hosts=frozenset(),
            )

    def test_allows_http_when_issuer_is_localhost(self):
        """http:// discovered endpoint allowed in dev when issuer is http://localhost."""
        with patch("socket.getaddrinfo", return_value=self._LOOPBACK_ADDR):
            validate_discovered_endpoint(
                "http://localhost:8080/token",
                self._issuer("http://localhost:8080"),
                allow_http=True,
                trusted_endpoint_hosts=frozenset(),
            )

    def test_rejects_private_ip(self):
        """Endpoint resolving to a private/link-local IP is rejected."""
        with (
            patch("socket.getaddrinfo", return_value=self._PRIVATE_ADDR),
            pytest.raises(OIDCError, match="non-public address.*169.254.169.254"),
        ):
            validate_discovered_endpoint(
                "https://idp.example.com/token",
                self._issuer(),
                allow_http=False,
                trusted_endpoint_hosts=frozenset(),
            )

    def test_rejects_embedded_credentials(self):
        """Endpoint with userinfo (user:pass@host) rejected."""
        with pytest.raises(OIDCError, match="embedded credentials"):
            validate_discovered_endpoint(
                "https://user:pass@idp.example.com/token",
                self._issuer(),
                allow_http=False,
                trusted_endpoint_hosts=frozenset(),
            )

    def test_rejects_different_host(self):
        """Endpoint on a different host than the issuer is rejected."""
        with (
            patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR),
            pytest.raises(OIDCError, match="not trusted"),
        ):
            validate_discovered_endpoint(
                "https://attacker.com/token",
                self._issuer(),
                allow_http=False,
                trusted_endpoint_hosts=frozenset(),
            )

    def test_rejects_subdomain(self):
        """Sibling-subdomain endpoint is rejected (strict equality)."""
        with (
            patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR),
            pytest.raises(OIDCError, match="not trusted"),
        ):
            validate_discovered_endpoint(
                "https://login.idp.example.com/token",
                self._issuer(),
                allow_http=False,
                trusted_endpoint_hosts=frozenset(),
            )

    def test_rejects_different_port(self):
        """Endpoint on a different port than the issuer is rejected."""
        with (
            patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR),
            pytest.raises(OIDCError, match="port.*does not match issuer"),
        ):
            validate_discovered_endpoint(
                "https://idp.example.com:9443/token",
                self._issuer(),
                allow_http=False,
                trusted_endpoint_hosts=frozenset(),
            )

    def test_rejects_different_scheme(self):
        """Endpoint scheme must match the issuer's scheme."""
        with (
            patch("socket.getaddrinfo", return_value=self._LOOPBACK_ADDR),
            pytest.raises(OIDCError, match="scheme.*does not match issuer"),
        ):
            validate_discovered_endpoint(
                "https://localhost:8080/token",
                self._issuer("http://localhost:8080"),
                allow_http=True,
                trusted_endpoint_hosts=frozenset(),
            )

    def test_accepts_google_known_endpoints(self):
        """Issuer accounts.google.com accepts the four well-known multi-origin hosts."""
        google_endpoints = (
            "https://accounts.google.com/o/oauth2/v2/auth",
            "https://oauth2.googleapis.com/token",
            "https://www.googleapis.com/oauth2/v3/certs",
            "https://openidconnect.googleapis.com/v1/userinfo",
        )
        with patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR):
            for endpoint in google_endpoints:
                validate_discovered_endpoint(
                    endpoint,
                    self._issuer("https://accounts.google.com"),
                    allow_http=False,
                    trusted_endpoint_hosts=frozenset(),
                )

    def test_rejects_unknown_host_for_known_issuer(self):
        """Even with a known issuer, foreign endpoints outside the allow-map are rejected."""
        with (
            patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR),
            pytest.raises(OIDCError, match="not trusted"),
        ):
            validate_discovered_endpoint(
                "https://attacker.com/token",
                self._issuer("https://accounts.google.com"),
                allow_http=False,
                trusted_endpoint_hosts=frozenset(),
            )

    def test_accepts_operator_trusted_endpoint_host(self):
        """Operator-supplied trusted_endpoint_hosts permits cross-host endpoints."""
        with patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR):
            validate_discovered_endpoint(
                "https://idp-token.example.net/token",
                self._issuer("https://idp.example.com"),
                allow_http=False,
                trusted_endpoint_hosts=frozenset({"idp-token.example.net"}),
            )

    def test_accepts_explicit_default_port_match(self):
        """Issuer omits :443; endpoint includes :443 explicitly -> still same origin."""
        with patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR):
            validate_discovered_endpoint(
                "https://idp.example.com:443/token",
                self._issuer("https://idp.example.com"),
                allow_http=False,
                trusted_endpoint_hosts=frozenset(),
            )

    def test_accepts_implicit_default_port_match_reverse(self):
        """Issuer includes :443; endpoint omits the port -> still same origin."""
        with patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR):
            validate_discovered_endpoint(
                "https://idp.example.com/token",
                self._issuer("https://idp.example.com:443"),
                allow_http=False,
                trusted_endpoint_hosts=frozenset(),
            )

    def test_discover_rejects_endpoint_on_other_host(self):
        """discover_oidc returns enabled=False when token_endpoint targets a foreign host."""
        config = _make_config(
            authorization_endpoint="",
            token_endpoint="",
            userinfo_endpoint="",
            jwks_uri="",
        )
        discovery_doc = {
            "authorization_endpoint": "https://idp.example.com/authorize",
            "token_endpoint": "https://attacker.com/token",
            "userinfo_endpoint": "https://idp.example.com/userinfo",
            "jwks_uri": "https://idp.example.com/.well-known/jwks.json",
        }
        mock_response = MagicMock()
        mock_response.json.return_value = discovery_doc
        mock_response.raise_for_status = MagicMock()

        async def _run():
            client = _mock_async_client(lambda url: _async_return(mock_response))
            with (
                patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR),
                patch("httpx.AsyncClient", return_value=client),
            ):
                result = await discover_oidc(config)
            assert result.enabled is False

        asyncio.run(_run())

    def test_discover_rejects_foreign_userinfo(self):
        """A foreign userinfo_endpoint is rejected even when other endpoints are same-origin.

        The required-endpoint loop runs first, so this test pins the userinfo branch's
        reject path and proves it executes.
        """
        config = _make_config(
            authorization_endpoint="",
            token_endpoint="",
            userinfo_endpoint="",
            jwks_uri="",
        )
        discovery_doc = {
            "authorization_endpoint": "https://idp.example.com/authorize",
            "token_endpoint": "https://idp.example.com/token",
            "userinfo_endpoint": "https://attacker.com/userinfo",
            "jwks_uri": "https://idp.example.com/.well-known/jwks.json",
        }
        mock_response = MagicMock()
        mock_response.json.return_value = discovery_doc
        mock_response.raise_for_status = MagicMock()

        async def _run():
            client = _mock_async_client(lambda url: _async_return(mock_response))
            with (
                patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR),
                patch("httpx.AsyncClient", return_value=client),
            ):
                result = await discover_oidc(config)
            assert result.enabled is False

        asyncio.run(_run())

    def test_discover_accepts_google_multi_origin(self):
        """discover_oidc accepts Google's legitimate multi-origin discovery doc."""
        config = _make_config(
            issuer="https://accounts.google.com",
            authorization_endpoint="",
            token_endpoint="",
            userinfo_endpoint="",
            jwks_uri="",
        )
        discovery_doc = {
            "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_endpoint": "https://oauth2.googleapis.com/token",
            "userinfo_endpoint": "https://openidconnect.googleapis.com/v1/userinfo",
            "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
        }
        mock_response = MagicMock()
        mock_response.json.return_value = discovery_doc
        mock_response.raise_for_status = MagicMock()

        async def _run():
            client = _mock_async_client(lambda url: _async_return(mock_response))
            with (
                patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR),
                patch("httpx.AsyncClient", return_value=client),
            ):
                result = await discover_oidc(config)
            assert result.enabled is True
            assert result.token_endpoint == "https://oauth2.googleapis.com/token"
            assert result.jwks_uri == "https://www.googleapis.com/oauth2/v3/certs"

        asyncio.run(_run())

    def test_discover_rejects_http_endpoint(self):
        """discover_oidc returns enabled=False when an endpoint is http:// in prod."""
        config = _make_config(
            authorization_endpoint="",
            token_endpoint="",
            userinfo_endpoint="",
            jwks_uri="",
        )
        discovery_doc = {
            "authorization_endpoint": "https://idp.example.com/authorize",
            "token_endpoint": "http://idp.example.com/token",
            "userinfo_endpoint": "https://idp.example.com/userinfo",
            "jwks_uri": "https://idp.example.com/.well-known/jwks.json",
        }
        mock_response = MagicMock()
        mock_response.json.return_value = discovery_doc
        mock_response.raise_for_status = MagicMock()

        async def _run():
            client = _mock_async_client(lambda url: _async_return(mock_response))
            with (
                patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR),
                patch("httpx.AsyncClient", return_value=client),
            ):
                result = await discover_oidc(config)
            assert result.enabled is False

        asyncio.run(_run())

    def test_discover_rejects_endpoint_resolving_to_private_ip(self):
        """discover_oidc returns enabled=False when an endpoint host resolves privately.

        DNS may legitimately rotate between the issuer check and the per-endpoint
        re-resolution; defence-in-depth requires we re-validate each discovered URL.
        """
        config = _make_config(
            authorization_endpoint="",
            token_endpoint="",
            userinfo_endpoint="",
            jwks_uri="",
        )
        discovery_doc = {
            "authorization_endpoint": "https://idp.example.com/authorize",
            "token_endpoint": "https://idp.example.com/token",
            "userinfo_endpoint": "https://idp.example.com/userinfo",
            "jwks_uri": "https://idp.example.com/.well-known/jwks.json",
        }
        mock_response = MagicMock()
        mock_response.json.return_value = discovery_doc
        mock_response.raise_for_status = MagicMock()

        # Issuer validation passes (public), then DNS rotates so each discovered
        # endpoint resolves to a link-local address.
        results = iter(
            [
                self._PUBLIC_ADDR,
                self._PRIVATE_ADDR,
                self._PRIVATE_ADDR,
                self._PRIVATE_ADDR,
                self._PRIVATE_ADDR,
            ]
        )

        def _resolve(*_args, **_kwargs):
            return next(results)

        async def _run():
            client = _mock_async_client(lambda url: _async_return(mock_response))
            with (
                patch("socket.getaddrinfo", side_effect=_resolve),
                patch("httpx.AsyncClient", return_value=client),
            ):
                result = await discover_oidc(config)
            assert result.enabled is False

        asyncio.run(_run())

    def test_discover_accepts_localhost_http_flow(self):
        """Localhost issuer with http:// endpoints is accepted in dev mode."""
        config = _make_config(
            issuer="http://localhost:8080",
            authorization_endpoint="",
            token_endpoint="",
            userinfo_endpoint="",
            jwks_uri="",
        )
        discovery_doc = {
            "authorization_endpoint": "http://localhost:8080/authorize",
            "token_endpoint": "http://localhost:8080/token",
            "userinfo_endpoint": "http://localhost:8080/userinfo",
            "jwks_uri": "http://localhost:8080/jwks",
        }
        mock_response = MagicMock()
        mock_response.json.return_value = discovery_doc
        mock_response.raise_for_status = MagicMock()

        async def _run():
            client = _mock_async_client(lambda url: _async_return(mock_response))
            with (
                patch("socket.getaddrinfo", return_value=self._LOOPBACK_ADDR),
                patch("httpx.AsyncClient", return_value=client),
            ):
                result = await discover_oidc(config)
            assert result.enabled is True
            assert result.token_endpoint == "http://localhost:8080/token"

        asyncio.run(_run())

    def test_discover_accepts_empty_userinfo(self):
        """Empty userinfo_endpoint is allowed (skipped) when other endpoints are valid."""
        config = _make_config(
            authorization_endpoint="",
            token_endpoint="",
            userinfo_endpoint="",
            jwks_uri="",
        )
        discovery_doc = {
            "authorization_endpoint": "https://idp.example.com/authorize",
            "token_endpoint": "https://idp.example.com/token",
            "jwks_uri": "https://idp.example.com/.well-known/jwks.json",
        }
        mock_response = MagicMock()
        mock_response.json.return_value = discovery_doc
        mock_response.raise_for_status = MagicMock()

        async def _run():
            client = _mock_async_client(lambda url: _async_return(mock_response))
            with (
                patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR),
                patch("httpx.AsyncClient", return_value=client),
            ):
                result = await discover_oidc(config)
            assert result.enabled is True
            assert result.userinfo_endpoint == ""

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Redirect URI Builder
# ---------------------------------------------------------------------------


class TestBuildOIDCRedirectURI:
    """Tests for ``_build_oidc_redirect_uri`` in auth.py."""

    def test_build_redirect_uri_uses_redirect_base_only(self):
        """The redirect URI is built solely from ``redirect_base``."""
        from turnstone.core.auth import _build_oidc_redirect_uri

        config = _make_config(redirect_base="https://example.com")
        result = _build_oidc_redirect_uri(config)
        assert result == "https://example.com/v1/api/auth/oidc/callback"


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


class TestPKCE:
    def test_generate_pkce_verifier_shape(self):
        verifier = generate_pkce_verifier()

        # Verifier should be URL-safe base64
        assert isinstance(verifier, str)
        assert len(verifier) > 40  # 48 bytes -> ~64 chars

    def test_pkce_verifier_uniqueness(self):
        """Each call should produce a unique verifier."""
        v1 = generate_pkce_verifier()
        v2 = generate_pkce_verifier()
        assert v1 != v2


# ---------------------------------------------------------------------------
# Authorization URL
# ---------------------------------------------------------------------------


class TestBuildAuthorizeURL:
    def test_build_authorize_url_contains_required_params(self):
        config = _make_config()
        verifier = generate_pkce_verifier()
        url = build_authorize_url(
            config=config,
            redirect_uri="https://app.example.com/callback",
            state="test-state",
            nonce="test-nonce",
            code_verifier=verifier,
        )

        assert url.startswith("https://idp.example.com/authorize?")
        assert "response_type=code" in url
        assert "client_id=my-client" in url
        assert "redirect_uri=" in url
        assert "scope=openid" in url
        assert "state=test-state" in url
        assert "nonce=test-nonce" in url
        assert "code_challenge=" in url
        assert "code_challenge_method=S256" in url

    def test_build_authorize_url_pkce(self):
        """code_challenge in URL should be correct S256 of the verifier."""
        config = _make_config()
        verifier = generate_pkce_verifier()

        url = build_authorize_url(
            config=config,
            redirect_uri="https://app.example.com/callback",
            state="s",
            nonce="n",
            code_verifier=verifier,
        )

        # Extract code_challenge from URL
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        actual_challenge = params["code_challenge"][0]

        # Compute expected challenge
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        assert actual_challenge == expected

    def test_build_authorize_url_redirect_uri_encoded(self):
        config = _make_config()
        verifier = generate_pkce_verifier()
        redirect = "https://app.example.com/callback?extra=1"

        url = build_authorize_url(
            config=config,
            redirect_uri=redirect,
            state="s",
            nonce="n",
            code_verifier=verifier,
        )

        # The redirect_uri should be URL-encoded
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        assert params["redirect_uri"][0] == redirect


# ---------------------------------------------------------------------------
# ID Token Validation
# ---------------------------------------------------------------------------


class TestValidateIDToken:
    _FAKE_JWKS = {"keys": [{"kid": "key1", "kty": "RSA", "n": "abc", "e": "AQAB"}]}

    def test_validate_id_token_nonce_mismatch(self):
        """Nonce mismatch should raise OIDCError."""
        config = _make_config()

        mock_pyjwk = MagicMock()
        mock_pyjwk.return_value.key = "fake-key"

        with (
            patch("jwt.get_unverified_header", return_value={"kid": "key1", "alg": "RS256"}),
            patch("jwt.PyJWK", mock_pyjwk),
            patch("jwt.decode", return_value={"sub": "user1", "nonce": "wrong-nonce"}),
            pytest.raises(OIDCError, match="nonce mismatch"),
        ):
            validate_id_token(
                raw_token="fake.jwt.token",
                jwks_data=self._FAKE_JWKS,
                config=config,
                nonce="expected-nonce",
            )

    def test_validate_id_token_success(self):
        """Successful validation returns decoded claims."""
        config = _make_config()

        mock_pyjwk = MagicMock()
        mock_pyjwk.return_value.key = "fake-key"

        expected_claims = {
            "sub": "user1",
            "email": "user@example.com",
            "nonce": "test-nonce",
        }

        with (
            patch("jwt.get_unverified_header", return_value={"kid": "key1", "alg": "RS256"}),
            patch("jwt.PyJWK", mock_pyjwk),
            patch("jwt.decode", return_value=expected_claims) as mock_decode,
        ):
            claims = validate_id_token(
                raw_token="fake.jwt.token",
                jwks_data=self._FAKE_JWKS,
                config=config,
                nonce="test-nonce",
            )

        assert claims == expected_claims
        mock_decode.assert_called_once_with(
            "fake.jwt.token",
            "fake-key",
            algorithms=[
                "RS256",
                "RS384",
                "RS512",
                "ES256",
                "ES384",
                "ES512",
                "PS256",
                "PS384",
                "PS512",
            ],
            audience="my-client",
            issuer="https://idp.example.com",
        )

    def test_validate_id_token_kid_not_found(self):
        """Unknown kid raises OIDCError with descriptive message."""
        config = _make_config()
        jwks_data = {"keys": [{"kid": "other-key", "kty": "RSA"}]}

        with (
            patch("jwt.get_unverified_header", return_value={"kid": "unknown", "alg": "RS256"}),
            pytest.raises(OIDCError, match="not found in JWKS"),
        ):
            validate_id_token(
                raw_token="bad.token",
                jwks_data=jwks_data,
                config=config,
                nonce="n",
            )

    def test_validate_id_token_raises_keynotfound_for_unknown_kid(self):
        """Unknown kid raises the OIDCKeyNotFoundError subclass, not generic OIDCError."""
        config = _make_config()
        jwks_data = {"keys": [{"kid": "other-key", "kty": "RSA"}]}

        with (
            patch("jwt.get_unverified_header", return_value={"kid": "unknown", "alg": "RS256"}),
            pytest.raises(OIDCKeyNotFoundError) as exc_info,
        ):
            validate_id_token(
                raw_token="bad.token",
                jwks_data=jwks_data,
                config=config,
                nonce="n",
            )
        assert isinstance(exc_info.value, OIDCError)
        assert "not found in JWKS" in str(exc_info.value)

    def test_validate_id_token_invalid_jwt(self):
        """Invalid JWT raises OIDCError."""
        config = _make_config()

        mock_pyjwk = MagicMock()
        mock_pyjwk.return_value.key = "fake-key"

        with (
            patch("jwt.get_unverified_header", return_value={"kid": "key1", "alg": "RS256"}),
            patch("jwt.PyJWK", mock_pyjwk := MagicMock(return_value=MagicMock(key="fake-key"))),
            patch("jwt.decode", side_effect=pyjwt.InvalidTokenError("expired")),
            pytest.raises(OIDCError, match="ID token validation failed"),
        ):
            validate_id_token(
                raw_token="expired.token",
                jwks_data=self._FAKE_JWKS,
                config=config,
                nonce="n",
            )

    def test_validate_id_token_invalid_header(self):
        """Malformed token header raises OIDCError."""
        config = _make_config()

        with (
            patch("jwt.get_unverified_header", side_effect=pyjwt.DecodeError("bad header")),
            pytest.raises(OIDCError, match="Invalid ID token header"),
        ):
            validate_id_token(
                raw_token="garbage",
                jwks_data=self._FAKE_JWKS,
                config=config,
                nonce="n",
            )

    def test_validate_id_token_retry_after_kid_rotation(self):
        """Sign with a fresh key, miss old JWKS, succeed against rotated JWKS.

        Drives the kid-not-found -> JWKS rotation -> retry path at the
        ``validate_id_token`` unit level.  Uses real RSA signing so the
        decoded claims come back through pyjwt rather than from a mock.
        """
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from jwt.algorithms import RSAAlgorithm

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        priv_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

        new_jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
        new_jwk["kid"] = "new-key"
        new_jwk["alg"] = "RS256"

        config = _make_config(issuer="https://idp.example.com")
        token = pyjwt.encode(
            {
                "sub": "user1",
                "aud": config.client_id,
                "iss": config.issuer,
                "nonce": "test-nonce",
            },
            priv_pem,
            algorithm="RS256",
            headers={"kid": "new-key"},
        )

        # Stale JWKS with an unrelated old key -> kid lookup fails.
        old_jwks = {
            "keys": [{"kid": "old-key", "kty": "RSA", "n": "abc", "e": "AQAB"}],
        }
        with pytest.raises(OIDCKeyNotFoundError):
            validate_id_token(token, old_jwks, config, "test-nonce")

        # Rotated JWKS includes the new key -> validation succeeds.
        new_jwks = {"keys": [new_jwk]}
        claims = validate_id_token(token, new_jwks, config, "test-nonce")
        assert claims["sub"] == "user1"
        assert claims["nonce"] == "test-nonce"


# ---------------------------------------------------------------------------
# Token Exchange
# ---------------------------------------------------------------------------


def _mock_async_post_client(mock_post):
    """Build a patched httpx.AsyncClient context manager that supports POST."""

    class _AsyncCtx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, data=None):
            return await mock_post(url, data)

    return _AsyncCtx()


class TestExchangeCode:
    def test_exchange_code_rejects_non_dict_body(self):
        """A 200 response whose JSON body isn't a JSON object must be rejected."""
        config = _make_config()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [1, 2, 3]

        async def _post(_url, _data):
            return mock_response

        async def _run():
            client = _mock_async_post_client(_post)
            with (
                patch("httpx.AsyncClient", return_value=client),
                pytest.raises(OIDCError, match="non-dict body"),
            ):
                await exchange_code(config, "code", "https://app.example.com/cb", "verifier")

        asyncio.run(_run())

    def test_exchange_code_error_body_is_sanitized(self):
        """A 4xx response body containing CR/LF must not appear raw in the error message."""
        config = _make_config()

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "evil\nlog injection\rline"

        async def _post(_url, _data):
            return mock_response

        async def _run():
            client = _mock_async_post_client(_post)
            with (
                patch("httpx.AsyncClient", return_value=client),
                pytest.raises(OIDCError) as exc_info,
            ):
                await exchange_code(config, "code", "https://app.example.com/cb", "verifier")

            msg = str(exc_info.value)
            assert "\n" not in msg
            assert "\r" not in msg
            assert "evil" in msg
            assert "log injection" in msg

        asyncio.run(_run())

    def test_exchange_code_4xx_status_in_error(self):
        """A 4xx response surfaces the status code in the OIDCError message."""
        config = _make_config()

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "invalid_grant"

        async def _post(_url, _data):
            return mock_response

        async def _run():
            client = _mock_async_post_client(_post)
            with (
                patch("httpx.AsyncClient", return_value=client),
                pytest.raises(OIDCError, match="returned 400"),
            ):
                await exchange_code(config, "code", "https://app.example.com/cb", "verifier")

        asyncio.run(_run())

    def test_exchange_code_5xx_status_in_error(self):
        """A 5xx response surfaces the status code in the OIDCError message."""
        config = _make_config()

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "internal error"

        async def _post(_url, _data):
            return mock_response

        async def _run():
            client = _mock_async_post_client(_post)
            with (
                patch("httpx.AsyncClient", return_value=client),
                pytest.raises(OIDCError, match="returned 500"),
            ):
                await exchange_code(config, "code", "https://app.example.com/cb", "verifier")

        asyncio.run(_run())

    def test_exchange_code_network_error_wraps_to_oidc_error(self):
        """``httpx.RequestError`` from the transport surfaces as ``OIDCError``."""
        config = _make_config()

        async def _post(_url, _data):
            raise httpx.ConnectError("connection refused")

        async def _run():
            client = _mock_async_post_client(_post)
            with (
                patch("httpx.AsyncClient", return_value=client),
                pytest.raises(OIDCError, match="Token exchange request failed"),
            ):
                await exchange_code(config, "code", "https://app.example.com/cb", "verifier")

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# JWKS Fetch
# ---------------------------------------------------------------------------


def _mock_async_get_client(mock_get):
    """Build a patched httpx.AsyncClient context manager that supports GET-with-timeout."""

    class _AsyncCtx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, timeout=None):
            return await mock_get(url)

    return _AsyncCtx()


class TestFetchJWKS:
    """Coverage for ``fetch_jwks`` failure modes — malformed responses must
    surface as ``OIDCError`` rather than ``AttributeError``/``KeyError``.
    """

    def test_fetch_jwks_non_200_raises(self):
        """Non-2xx response (raise_for_status fires) -> OIDCError."""
        from turnstone.core.oidc import fetch_jwks

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=mock_response
        )

        async def _get(_url):
            return mock_response

        async def _run():
            client = _mock_async_get_client(_get)
            with (
                patch("httpx.AsyncClient", return_value=client),
                pytest.raises(OIDCError, match="JWKS fetch failed"),
            ):
                await fetch_jwks("https://idp.example.com/jwks")

        asyncio.run(_run())

    def test_fetch_jwks_non_dict_body_raises(self):
        """Body decoded as a list rather than a JSON object -> OIDCError."""
        from turnstone.core.oidc import fetch_jwks

        mock_response = MagicMock()
        mock_response.json.return_value = [1, 2, 3]
        mock_response.raise_for_status = MagicMock()

        async def _get(_url):
            return mock_response

        async def _run():
            client = _mock_async_get_client(_get)
            with (
                patch("httpx.AsyncClient", return_value=client),
                pytest.raises(OIDCError, match="not a JSON object"),
            ):
                await fetch_jwks("https://idp.example.com/jwks")

        asyncio.run(_run())

    def test_fetch_jwks_dict_missing_keys_raises(self):
        """Body is a dict but lacks the ``keys`` array -> OIDCError."""
        from turnstone.core.oidc import fetch_jwks

        mock_response = MagicMock()
        mock_response.json.return_value = {"not_keys": []}
        mock_response.raise_for_status = MagicMock()

        async def _get(_url):
            return mock_response

        async def _run():
            client = _mock_async_get_client(_get)
            with (
                patch("httpx.AsyncClient", return_value=client),
                pytest.raises(OIDCError, match="missing 'keys' array"),
            ):
                await fetch_jwks("https://idp.example.com/jwks")

        asyncio.run(_run())

    def test_fetch_jwks_keys_not_a_list_raises(self):
        """``keys`` present but not a list -> OIDCError (not iterable type)."""
        from turnstone.core.oidc import fetch_jwks

        mock_response = MagicMock()
        mock_response.json.return_value = {"keys": "definitely-not-a-list"}
        mock_response.raise_for_status = MagicMock()

        async def _get(_url):
            return mock_response

        async def _run():
            client = _mock_async_get_client(_get)
            with (
                patch("httpx.AsyncClient", return_value=client),
                pytest.raises(OIDCError, match="missing 'keys' array"),
            ):
                await fetch_jwks("https://idp.example.com/jwks")

        asyncio.run(_run())

    def test_fetch_jwks_network_error_raises(self):
        """``httpx.RequestError`` from the transport -> OIDCError."""
        from turnstone.core.oidc import fetch_jwks

        async def _get(_url):
            raise httpx.ConnectError("connection refused")

        async def _run():
            client = _mock_async_get_client(_get)
            with (
                patch("httpx.AsyncClient", return_value=client),
                pytest.raises(OIDCError, match="JWKS fetch failed"),
            ):
                await fetch_jwks("https://idp.example.com/jwks")

        asyncio.run(_run())


class TestSanitizeLogText:
    def test_sanitize_log_text_escapes_control_chars(self):
        """CR/LF, NUL, and tab characters must be escaped, not preserved."""
        out = _sanitize_log_text("a\r\nb\tc\x00d", 500)
        assert "\r" not in out
        assert "\n" not in out
        assert "\t" not in out
        assert "\x00" not in out
        assert "a" in out and "b" in out and "c" in out and "d" in out

    def test_sanitize_log_text_truncates_after_escape(self):
        """The limit caps the rendered length, including the escape sequences."""
        out = _sanitize_log_text("\n" * 100, 10)
        assert len(out) == 10


# ---------------------------------------------------------------------------
# User Provisioning
# ---------------------------------------------------------------------------


class TestProvisionOIDCUser:
    def test_provision_oidc_user_existing(self):
        """Existing identity -> returns existing user, updates last_login."""
        config = _make_config()
        existing_user = {
            "user_id": "u1",
            "username": "alice",
            "display_name": "Alice",
            "password_hash": "!oidc",
        }
        existing_identity = {
            "issuer": "https://idp.example.com",
            "subject": "sub-123",
            "user_id": "u1",
            "email": "alice@example.com",
            "created": "2024-01-01T00:00:00",
            "last_login": "2024-01-01T00:00:00",
        }
        storage = _mock_storage(identity=existing_identity, user=existing_user)

        claims = {"sub": "sub-123", "email": "alice@example.com", "name": "Alice"}
        user = provision_oidc_user(storage, config, claims)

        assert user["user_id"] == "u1"
        assert user["username"] == "alice"
        storage.update_oidc_identity_login.assert_called_once()
        # Should not create a new user
        storage.create_oidc_user.assert_not_called()

    def test_provision_oidc_user_new(self):
        """No identity -> creates user + identity atomically."""
        config = _make_config()
        storage = _mock_storage()

        # After create_oidc_user, get_user should return the new user
        new_user = {
            "user_id": "u-new",
            "username": "bob",
            "display_name": "Bob",
            "password_hash": "!oidc",
        }
        storage.get_user.return_value = new_user

        claims = {"sub": "sub-456", "preferred_username": "bob", "email": "bob@example.com"}

        with patch("turnstone.core.oidc.uuid") as mock_uuid:
            mock_uuid.uuid4.return_value = MagicMock(hex="u-new-hex-00000000000000000000")
            user = provision_oidc_user(storage, config, claims)

        assert user["username"] == "bob"
        storage.create_oidc_user.assert_called_once()
        # Positional args: user_id, username, display_name, password_hash, issuer, subject, email
        call_args = storage.create_oidc_user.call_args
        assert call_args[0][1] == "bob"
        assert call_args[0][3] == "!oidc"
        assert call_args[0][4] == "https://idp.example.com"
        assert call_args[0][5] == "sub-456"
        assert call_args[0][6] == "bob@example.com"

    def test_provision_oidc_user_username_dedup(self):
        """First username taken -> appends suffix."""
        config = _make_config()
        # Bulk lookup reports "bob" already taken; "bob2" is free.
        storage = _mock_storage(existing_usernames={"bob"})
        new_user = {
            "user_id": "u-new",
            "username": "bob2",
            "display_name": "Bob",
            "password_hash": "!oidc",
        }
        storage.get_user.return_value = new_user

        claims = {"sub": "sub-789", "preferred_username": "bob", "email": "bob@example.com"}
        user = provision_oidc_user(storage, config, claims)

        assert user["username"] == "bob2"
        # create_oidc_user should have been called with "bob2" as username
        call_args = storage.create_oidc_user.call_args
        assert call_args[0][1] == "bob2"
        # Single bulk query rather than per-candidate get_user_by_username
        storage.find_existing_usernames.assert_called_once()
        storage.get_user_by_username.assert_not_called()

    def test_provision_oidc_user_email_prefix(self):
        """No preferred_username -> uses email prefix."""
        config = _make_config()
        storage = _mock_storage()

        new_user = {
            "user_id": "u-new",
            "username": "charlie",
            "display_name": "charlie@example.com",
            "password_hash": "!oidc",
        }
        storage.get_user.return_value = new_user

        claims = {"sub": "sub-abc", "email": "charlie@example.com"}
        provision_oidc_user(storage, config, claims)

        call_args = storage.create_oidc_user.call_args
        assert call_args[0][1] == "charlie"

    def test_provision_oidc_user_missing_user_raises(self):
        """Identity references missing user -> raises OIDCError."""
        config = _make_config()
        existing_identity = {
            "issuer": "https://idp.example.com",
            "subject": "sub-orphan",
            "user_id": "u-gone",
            "email": "gone@example.com",
            "created": "2024-01-01T00:00:00",
            "last_login": "2024-01-01T00:00:00",
        }
        storage = _mock_storage(identity=existing_identity, user=None)

        claims = {"sub": "sub-orphan", "email": "gone@example.com"}
        with pytest.raises(OIDCError, match="missing user"):
            provision_oidc_user(storage, config, claims)

    def test_provision_oidc_user_fallback_username(self):
        """No preferred_username and no email -> falls back to 'user'."""
        config = _make_config()
        storage = _mock_storage()
        new_user = {
            "user_id": "u-new",
            "username": "user",
            "display_name": "",
            "password_hash": "!oidc",
        }
        storage.get_user.return_value = new_user

        claims = {"sub": "sub-noemail"}
        provision_oidc_user(storage, config, claims)

        call_args = storage.create_oidc_user.call_args
        assert call_args[0][1] == "user"

    def test_provision_oidc_user_username_conflict_raises(self):
        """Username TOCTOU -> create_oidc_user raises StorageConflictError;
        provision_oidc_user wraps it as OIDCError without leaving role rows.
        """
        from turnstone.core.storage import StorageConflictError

        config = _make_config()
        storage = _mock_storage()
        storage.create_oidc_user.side_effect = StorageConflictError("username already taken: bob")

        claims = {"sub": "sub-race", "preferred_username": "bob", "email": "bob@example.com"}
        with pytest.raises(OIDCError, match="username already taken"):
            provision_oidc_user(storage, config, claims)

        storage.assign_role.assert_not_called()

    def test_provision_oidc_user_identity_conflict_raises(self):
        """Concurrent (issuer, subject) creates -> StorageConflictError -> OIDCError."""
        from turnstone.core.storage import StorageConflictError

        config = _make_config()
        storage = _mock_storage()
        storage.create_oidc_user.side_effect = StorageConflictError(
            "OIDC identity already linked: (https://idp.example.com, sub-race)"
        )

        claims = {"sub": "sub-race", "preferred_username": "bob", "email": "bob@example.com"}
        with pytest.raises(OIDCError, match="OIDC identity already linked"):
            provision_oidc_user(storage, config, claims)

        storage.assign_role.assert_not_called()

    def test_existing_identity_self_heals_zero_roles(self):
        """Existing identity user with zero roles -> safety-net assigns builtin-viewer.

        Models the bug-1 strand: a prior login committed user + identity but
        ``apply_role_mapping`` raised before reaching the safety-net.  On the
        next login the existing-identity branch must self-heal.
        """
        config = _make_config()
        existing_user = {
            "user_id": "u-stranded",
            "username": "alice",
            "display_name": "Alice",
            "password_hash": "!oidc",
        }
        existing_identity = {
            "issuer": "https://idp.example.com",
            "subject": "sub-stranded",
            "user_id": "u-stranded",
            "email": "alice@example.com",
            "created": "2024-01-01T00:00:00",
            "last_login": "2024-01-01T00:00:00",
        }
        storage = _mock_storage(
            identity=existing_identity,
            user=existing_user,
            role={"role_id": "builtin-viewer", "name": "Viewer"},
        )
        storage.list_user_roles.return_value = []

        claims = {"sub": "sub-stranded", "email": "alice@example.com", "name": "Alice"}
        user = provision_oidc_user(storage, config, claims)

        assert user["user_id"] == "u-stranded"
        storage.list_user_roles.assert_called_once_with("u-stranded")
        storage.assign_role.assert_called_once_with("u-stranded", "builtin-viewer", "oidc-default")

    def test_existing_identity_does_not_re_assign_when_user_has_roles(self):
        """User already has at least one role -> safety-net no-ops."""
        config = _make_config()
        existing_user = {
            "user_id": "u1",
            "username": "alice",
            "display_name": "Alice",
            "password_hash": "!oidc",
        }
        existing_identity = {
            "issuer": "https://idp.example.com",
            "subject": "sub-123",
            "user_id": "u1",
            "email": "alice@example.com",
            "created": "2024-01-01T00:00:00",
            "last_login": "2024-01-01T00:00:00",
        }
        storage = _mock_storage(
            identity=existing_identity,
            user=existing_user,
            role={"role_id": "builtin-viewer", "name": "Viewer"},
        )
        storage.list_user_roles.return_value = [{"role_id": "builtin-operator"}]

        claims = {"sub": "sub-123", "email": "alice@example.com"}
        provision_oidc_user(storage, config, claims)

        storage.list_user_roles.assert_called_once_with("u1")
        storage.assign_role.assert_not_called()

    def test_existing_identity_with_claim_mapped_roles_skips_default(self):
        """Claim-driven mapping populated roles -> hint short-circuits the helper."""
        config = _make_config(
            role_claim="groups",
            role_map={"admin": "builtin-admin"},
        )
        existing_user = {
            "user_id": "u1",
            "username": "alice",
            "display_name": "Alice",
            "password_hash": "!oidc",
        }
        existing_identity = {
            "issuer": "https://idp.example.com",
            "subject": "sub-123",
            "user_id": "u1",
            "email": "alice@example.com",
            "created": "2024-01-01T00:00:00",
            "last_login": "2024-01-01T00:00:00",
        }
        storage = _mock_storage(
            identity=existing_identity,
            user=existing_user,
            role={"role_id": "builtin-admin", "name": "Admin"},
        )

        claims = {"sub": "sub-123", "groups": "admin"}
        provision_oidc_user(storage, config, claims)

        storage.list_user_roles.assert_not_called()
        storage.assign_role.assert_not_called()

    def test_new_user_safety_net_still_fires(self):
        """Fresh user with no IdP-mapped roles -> builtin-viewer fallback applied."""
        config = _make_config()
        storage = _mock_storage(role={"role_id": "builtin-viewer", "name": "Viewer"})
        storage.list_user_roles.return_value = []
        new_user = {
            "user_id": "u-new",
            "username": "bob",
            "display_name": "Bob",
            "password_hash": "!oidc",
        }
        storage.get_user.return_value = new_user

        claims = {"sub": "sub-new", "preferred_username": "bob", "email": "bob@example.com"}
        provision_oidc_user(storage, config, claims)

        storage.assign_role.assert_called_once()
        called_args = storage.assign_role.call_args[0]
        assert called_args[1] == "builtin-viewer"
        assert called_args[2] == "oidc-default"

    def test_new_user_safety_net_skipped_when_apply_role_mapping_assigns_role(self):
        """Claim-driven mapping populates roles -> safety-net hint short-circuits."""
        config = _make_config(
            role_claim="groups",
            role_map={"admin": "builtin-admin"},
        )
        storage = _mock_storage(role={"role_id": "builtin-admin", "name": "Admin"})
        new_user = {
            "user_id": "u-new",
            "username": "bob",
            "display_name": "Bob",
            "password_hash": "!oidc",
        }
        storage.get_user.return_value = new_user

        claims = {
            "sub": "sub-new",
            "preferred_username": "bob",
            "email": "bob@example.com",
            "groups": "admin",
        }
        provision_oidc_user(storage, config, claims)

        storage.list_user_roles.assert_not_called()
        storage.assign_role.assert_not_called()

    def test_ensure_default_role_noop_when_builtin_viewer_missing(self):
        """builtin-viewer absent from role table -> helper does nothing."""
        storage = _mock_storage(role=None)

        _ensure_default_role(storage, "u1")

        storage.get_role.assert_called_once_with("builtin-viewer")
        storage.list_user_roles.assert_not_called()
        storage.assign_role.assert_not_called()


# ---------------------------------------------------------------------------
# Username derivation — UUID-retry fallback tiers
# ---------------------------------------------------------------------------


class TestDeriveUsername:
    """Tier-3 (UUID-retry) and tier-4 (give-up) coverage for ``_derive_username``.

    Tier 1 (sanitised candidate) and tier 2 (numeric suffix dedup) are
    already exercised through ``TestProvisionOIDCUser``; these tests
    drive the post-batch-6 fallback path that runs after every numeric
    suffix collides.
    """

    def _claims(self) -> dict[str, str]:
        return {
            "sub": "sub-x",
            "preferred_username": "bob",
            "email": "bob@example.com",
        }

    def _all_numeric_candidates_taken(self) -> set[str]:
        """The full set of tier-1 + tier-2 candidates ``_derive_username`` enumerates."""
        return {"bob", *(f"bob{n}" for n in range(2, 11))}

    def test_derive_username_falls_into_uuid_retry_when_all_suffixes_taken(self):
        """All 10 numeric candidates taken -> tier 3 returns a UUID-suffixed username."""
        config = _make_config()
        storage = _mock_storage(existing_usernames=self._all_numeric_candidates_taken())
        # First UUID candidate is free.
        storage.get_user_by_username.return_value = None
        new_user = {
            "user_id": "u-new",
            "username": "ignored-by-test",
            "display_name": "Bob",
            "password_hash": "!oidc",
        }
        storage.get_user.return_value = new_user

        provision_oidc_user(storage, config, self._claims())

        # The username actually persisted is the tier-3 UUID candidate.
        called_with = storage.create_oidc_user.call_args[0][1]
        assert called_with.startswith("bob")
        # bob + 32-hex UUID hex == 35 chars total.
        suffix = called_with[len("bob") :]
        assert len(suffix) == 32
        assert all(c in "0123456789abcdef" for c in suffix)
        # Exactly one tier-3 lookup happened (no retry collision).
        assert storage.get_user_by_username.call_count == 1

    def test_derive_username_uuid_retry_succeeds_on_second_attempt(self):
        """First UUID candidate collides; second attempt is free."""
        config = _make_config()
        storage = _mock_storage(existing_usernames=self._all_numeric_candidates_taken())
        # First lookup: collision (existing user); second: free.
        storage.get_user_by_username.side_effect = [
            {"user_id": "other", "username": "bob-already-used"},
            None,
        ]
        new_user = {
            "user_id": "u-new",
            "username": "ignored-by-test",
            "display_name": "Bob",
            "password_hash": "!oidc",
        }
        storage.get_user.return_value = new_user

        provision_oidc_user(storage, config, self._claims())

        assert storage.get_user_by_username.call_count == 2
        # The username persisted is the second UUID candidate.
        persisted = storage.create_oidc_user.call_args[0][1]
        assert persisted.startswith("bob")
        assert len(persisted) == len("bob") + 32

    def test_derive_username_uuid_retry_exhausted_raises(self):
        """All 3 UUID-retry attempts collide -> OIDCError raised."""
        config = _make_config()
        storage = _mock_storage(existing_usernames=self._all_numeric_candidates_taken())
        # Every UUID candidate hits an existing user.
        storage.get_user_by_username.return_value = {"user_id": "other", "username": "x"}

        with pytest.raises(OIDCError, match="Failed to generate unique username"):
            provision_oidc_user(storage, config, self._claims())

        # Tier 3 is bounded to 3 attempts.
        assert storage.get_user_by_username.call_count == 3
        storage.create_oidc_user.assert_not_called()


# ---------------------------------------------------------------------------
# Role Mapping
# ---------------------------------------------------------------------------


class TestApplyRoleMapping:
    def test_apply_role_mapping_basic(self):
        """Maps claim value to role."""
        config = _make_config(
            role_claim="groups",
            role_map={"admin": "builtin-admin"},
        )
        storage = _mock_storage(role={"role_id": "builtin-admin", "name": "Admin"})

        claims = {"sub": "u1", "groups": "admin"}
        apply_role_mapping(storage, "u1", claims, config)

        storage.replace_oidc_roles.assert_called_once_with("u1", {"builtin-admin"})
        storage.assign_role.assert_not_called()
        storage.unassign_role.assert_not_called()
        storage.list_user_roles.assert_not_called()

    def test_apply_role_mapping_list_claim(self):
        """Claim is a list of strings -> single replace call."""
        config = _make_config(
            role_claim="roles",
            role_map={"admin": "builtin-admin", "editor": "builtin-operator"},
        )
        storage = _mock_storage()
        # get_role returns non-None for both roles
        storage.get_role.return_value = {"role_id": "some-role"}

        claims = {"sub": "u1", "roles": ["admin", "editor"]}
        apply_role_mapping(storage, "u1", claims, config)

        storage.replace_oidc_roles.assert_called_once_with(
            "u1", {"builtin-admin", "builtin-operator"}
        )

    def test_apply_role_mapping_no_config(self):
        """No role_claim configured -> no-op."""
        config = _make_config(role_claim="", role_map={})
        storage = _mock_storage()

        claims = {"sub": "u1", "roles": "admin"}
        apply_role_mapping(storage, "u1", claims, config)

        storage.replace_oidc_roles.assert_not_called()

    def test_apply_role_mapping_unknown_role(self):
        """Claim maps to nonexistent role -> empty replace set."""
        config = _make_config(
            role_claim="groups",
            role_map={"admin": "nonexistent-role"},
        )
        storage = _mock_storage(role=None)  # role doesn't exist

        claims = {"sub": "u1", "groups": "admin"}
        apply_role_mapping(storage, "u1", claims, config)

        storage.replace_oidc_roles.assert_called_once_with("u1", set())

    def test_apply_role_mapping_no_matching_claim_value(self):
        """Claim value not in role_map -> empty replace set."""
        config = _make_config(
            role_claim="groups",
            role_map={"admin": "builtin-admin"},
        )
        storage = _mock_storage()

        claims = {"sub": "u1", "groups": "viewer"}  # "viewer" not in role_map
        apply_role_mapping(storage, "u1", claims, config)

        storage.replace_oidc_roles.assert_called_once_with("u1", set())

    def test_apply_role_mapping_claim_missing(self):
        """Claim key not present in claims -> empty replace set."""
        config = _make_config(
            role_claim="groups",
            role_map={"admin": "builtin-admin"},
        )
        storage = _mock_storage()

        claims = {"sub": "u1"}  # no "groups" key
        apply_role_mapping(storage, "u1", claims, config)

        storage.replace_oidc_roles.assert_called_once_with("u1", set())

    def test_apply_role_mapping_no_role_map(self):
        """role_claim set but role_map empty -> no-op (early return)."""
        config = _make_config(role_claim="groups", role_map={})
        storage = _mock_storage()

        claims = {"sub": "u1", "groups": "admin"}
        apply_role_mapping(storage, "u1", claims, config)

        storage.replace_oidc_roles.assert_not_called()

    def test_apply_role_mapping_revokes_stale_oidc_roles(self, caplog):
        """Roles previously assigned by OIDC but no longer in claims are revoked.

        Storage owns the diff via ``replace_oidc_roles``; ``apply_role_mapping``
        only logs the returned ``(added, removed)`` sets.  Manual roles are
        invisible to ``apply_role_mapping`` post-perf-3 — protection now lives
        in the storage layer's ``WHERE assigned_by = 'oidc'`` filter.
        """
        config = _make_config(
            role_claim="groups",
            role_map={"admin": "builtin-admin", "eng": "builtin-operator"},
        )
        storage = _mock_storage(
            replace_oidc_roles=({"builtin-operator"}, {"builtin-admin"}),
        )
        storage.get_role.return_value = {"role_id": "some-role"}

        # IdP now only says "eng", not "admin"
        claims = {"sub": "u1", "groups": ["eng"]}
        with caplog.at_level("INFO", logger="turnstone.core.oidc"):
            apply_role_mapping(storage, "u1", claims, config)

        storage.replace_oidc_roles.assert_called_once_with("u1", {"builtin-operator"})
        assert any(
            "Revoked role" in record.getMessage() and "builtin-admin" in record.getMessage()
            for record in caplog.records
        )

    def test_apply_role_mapping_revokes_all_oidc_roles_when_claim_absent(self):
        """Empty desired set propagates to storage (diff happens server-side)."""
        config = _make_config(
            role_claim="groups",
            role_map={"admin": "builtin-admin"},
        )
        storage = _mock_storage(
            replace_oidc_roles=(set(), {"builtin-admin", "builtin-operator"}),
        )
        storage.get_role.return_value = {"role_id": "some-role"}

        claims = {"sub": "u1"}  # no "groups" key
        apply_role_mapping(storage, "u1", claims, config)

        storage.replace_oidc_roles.assert_called_once_with("u1", set())

    def test_apply_role_mapping_int_claim(self):
        """Numeric claim values stringify before role_map lookup (no crash)."""
        config = _make_config(
            role_claim="roles",
            role_map={"42": "builtin-viewer"},
        )
        storage = _mock_storage(role={"role_id": "builtin-viewer", "name": "Viewer"})

        claims = {"sub": "u1", "roles": 42}
        apply_role_mapping(storage, "u1", claims, config)

        storage.replace_oidc_roles.assert_called_once_with("u1", {"builtin-viewer"})

    def test_apply_role_mapping_dict_claim(self):
        """Dict claim values stringify but won't reliably hit role_map -> empty set."""
        config = _make_config(
            role_claim="roles",
            role_map={"admin": "builtin-admin"},
        )
        storage = _mock_storage()

        # Dict reprs are unstable across runtime versions, so callers cannot
        # reasonably configure role_map keys to match.  The contract is just
        # "don't crash".
        claims = {"sub": "u1", "roles": {"role": "admin"}}
        apply_role_mapping(storage, "u1", claims, config)

        storage.replace_oidc_roles.assert_called_once_with("u1", set())


# ---------------------------------------------------------------------------
# Discovery (async)
# ---------------------------------------------------------------------------


class TestDiscoverOIDC:
    # Mock DNS result for a public IP — reused across discovery tests.
    _PUBLIC_ADDR = [(2, 1, 6, "", ("93.184.216.34", 0))]

    def test_discover_oidc_success(self):
        """Mock httpx response, verify endpoints populated."""
        config = _make_config(
            authorization_endpoint="",
            token_endpoint="",
            userinfo_endpoint="",
            jwks_uri="",
        )

        discovery_doc = {
            "authorization_endpoint": "https://idp.example.com/authorize",
            "token_endpoint": "https://idp.example.com/token",
            "userinfo_endpoint": "https://idp.example.com/userinfo",
            "jwks_uri": "https://idp.example.com/.well-known/jwks.json",
        }

        mock_response = MagicMock()
        mock_response.json.return_value = discovery_doc
        mock_response.raise_for_status = MagicMock()

        async def _run():
            client = _mock_async_client(lambda url: _async_return(mock_response))
            with (
                patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR),
                patch("httpx.AsyncClient", return_value=client),
            ):
                result = await discover_oidc(config)

            assert result.authorization_endpoint == "https://idp.example.com/authorize"
            assert result.token_endpoint == "https://idp.example.com/token"
            assert result.userinfo_endpoint == "https://idp.example.com/userinfo"
            assert result.jwks_uri == "https://idp.example.com/.well-known/jwks.json"
            assert result.enabled is True

        asyncio.run(_run())

    def test_discover_oidc_failure(self):
        """Mock httpx error -> enabled=False returned."""
        config = _make_config(
            authorization_endpoint="",
            token_endpoint="",
            userinfo_endpoint="",
            jwks_uri="",
        )

        async def _failing_get(url):
            raise httpx.ConnectError("connection refused")

        async def _run():
            client = _mock_async_client(_failing_get)
            with (
                patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR),
                patch("httpx.AsyncClient", return_value=client),
            ):
                result = await discover_oidc(config)

            assert result.enabled is False

        asyncio.run(_run())

    def test_discover_oidc_no_issuer(self):
        """Empty issuer -> enabled=False."""
        config = _make_config(issuer="")

        async def _run():
            result = await discover_oidc(config)
            assert result.enabled is False

        asyncio.run(_run())

    def test_discover_oidc_missing_required_endpoints(self):
        """Discovery doc missing authorization_endpoint -> enabled=False."""
        config = _make_config(
            authorization_endpoint="",
            token_endpoint="",
            userinfo_endpoint="",
            jwks_uri="",
        )

        # Document missing authorization_endpoint
        discovery_doc = {
            "token_endpoint": "https://idp.example.com/token",
            "jwks_uri": "https://idp.example.com/.well-known/jwks.json",
        }

        mock_response = MagicMock()
        mock_response.json.return_value = discovery_doc
        mock_response.raise_for_status = MagicMock()

        async def _run():
            client = _mock_async_client(lambda url: _async_return(mock_response))
            with (
                patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR),
                patch("httpx.AsyncClient", return_value=client),
            ):
                result = await discover_oidc(config)

            assert result.enabled is False

        asyncio.run(_run())

    def test_discover_oidc_handles_non_dict_response(self):
        """IdP returning a list/null body must not raise AttributeError."""
        config = _make_config(
            authorization_endpoint="",
            token_endpoint="",
            userinfo_endpoint="",
            jwks_uri="",
        )

        for payload in (["not", "a", "dict"], None, "string-body", 42):
            mock_response = MagicMock()
            mock_response.json.return_value = payload
            mock_response.raise_for_status = MagicMock()

            async def _run(resp=mock_response):
                client = _mock_async_client(lambda url: _async_return(resp))
                with (
                    patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR),
                    patch("httpx.AsyncClient", return_value=client),
                ):
                    return await discover_oidc(config)

            result = asyncio.run(_run())
            assert result.enabled is False


# ---------------------------------------------------------------------------
# Lifespan integration: initialize_oidc_state
# ---------------------------------------------------------------------------


class TestInitializeOIDCState:
    def test_initialize_skips_when_disabled(self):
        """Disabled config: jwks_data set to None, oidc_config unchanged."""
        cfg = _make_config(enabled=False)
        state = types.SimpleNamespace(oidc_config=cfg, jwks_data="stale")

        asyncio.run(initialize_oidc_state(state))

        assert state.oidc_config is cfg
        assert state.jwks_data is None

    def test_initialize_disables_on_discovery_exception(self):
        """Unexpected exception in discovery -> enabled flipped to False."""
        cfg = _make_config(
            authorization_endpoint="",
            token_endpoint="",
            jwks_uri="",
        )
        state = types.SimpleNamespace(oidc_config=cfg, jwks_data=None)

        async def _boom(_cfg):
            raise RuntimeError("boom")

        with patch("turnstone.core.oidc.discover_oidc", side_effect=_boom):
            asyncio.run(initialize_oidc_state(state))

        assert state.oidc_config.enabled is False
        assert state.jwks_data is None

    def test_initialize_disables_on_discovery_returning_disabled(self):
        """Discovery returns enabled=False (e.g. SSRF reject) -> propagate."""
        cfg = _make_config(
            authorization_endpoint="",
            token_endpoint="",
            jwks_uri="",
        )
        state = types.SimpleNamespace(oidc_config=cfg, jwks_data=None)

        disabled_cfg = dataclasses.replace(cfg, enabled=False)

        async def _disabled(_cfg, *, client=None):
            return disabled_cfg

        with patch("turnstone.core.oidc.discover_oidc", side_effect=_disabled):
            asyncio.run(initialize_oidc_state(state))

        assert state.oidc_config is disabled_cfg
        assert state.oidc_config.enabled is False
        assert state.jwks_data is None

    def test_initialize_disables_when_redirect_base_unset(self, caplog):
        """Discovery succeeds but redirect_base is empty -> disable + log error."""
        cfg = _make_config(redirect_base="")
        state = types.SimpleNamespace(oidc_config=cfg, jwks_data=None)

        async def _ok(c, *, client=None):
            return c

        async def _jwks_unexpected(_uri, *, client=None):
            raise AssertionError("fetch_jwks must not be called when redirect_base is empty")

        with (
            patch("turnstone.core.oidc.discover_oidc", side_effect=_ok),
            patch("turnstone.core.oidc.fetch_jwks", side_effect=_jwks_unexpected),
            caplog.at_level("ERROR", logger="turnstone.core.oidc"),
        ):
            asyncio.run(initialize_oidc_state(state))

        assert state.oidc_config.enabled is False
        assert state.jwks_data is None
        assert any("TURNSTONE_OIDC_REDIRECT_BASE" in record.message for record in caplog.records)

    def test_initialize_keeps_enabled_but_no_jwks_on_jwks_failure(self):
        """JWKS fetch failure preserves enabled=True for lazy retry."""
        cfg = _make_config(redirect_base="https://app.example.com")
        state = types.SimpleNamespace(oidc_config=cfg, jwks_data=None)

        async def _ok(c, *, client=None):
            return c

        async def _jwks_boom(_uri, *, client=None):
            raise OIDCError("jwks down")

        with (
            patch("turnstone.core.oidc.discover_oidc", side_effect=_ok),
            patch("turnstone.core.oidc.fetch_jwks", side_effect=_jwks_boom),
        ):
            asyncio.run(initialize_oidc_state(state))

        assert state.oidc_config.enabled is True
        assert state.oidc_config is cfg
        assert state.jwks_data is None

    def test_initialize_success(self):
        """Both discovery and JWKS prefetch succeed."""
        cfg = _make_config(redirect_base="https://app.example.com")
        state = types.SimpleNamespace(oidc_config=cfg, jwks_data=None)

        jwks = {"keys": [{"kid": "k1", "kty": "RSA"}]}

        async def _ok(c, *, client=None):
            return c

        async def _jwks(_uri, *, client=None):
            return jwks

        with (
            patch("turnstone.core.oidc.discover_oidc", side_effect=_ok),
            patch("turnstone.core.oidc.fetch_jwks", side_effect=_jwks),
        ):
            asyncio.run(initialize_oidc_state(state))

        assert state.oidc_config is cfg
        assert state.oidc_config.enabled is True
        assert state.jwks_data == jwks
        assert state.oidc_http_client is not None


async def _async_return(value):
    """Helper: return a value from an async function."""
    return value


class TestCloseOIDCState:
    def test_close_when_never_initialised(self):
        """close_oidc_state on bare state must not raise."""
        from turnstone.core.oidc import close_oidc_state

        state = types.SimpleNamespace()
        asyncio.run(close_oidc_state(state))

    def test_close_releases_long_lived_client(self):
        """The long-lived client installed by initialize_oidc_state is aclosed."""
        from turnstone.core.oidc import close_oidc_state

        client = MagicMock()

        async def _aclose():
            client.aclose_called = True

        client.aclose = _aclose
        state = types.SimpleNamespace(oidc_http_client=client)
        asyncio.run(close_oidc_state(state))
        assert getattr(client, "aclose_called", False) is True
        assert state.oidc_http_client is None


class TestLongLivedHTTPClientPassthrough:
    def test_initialize_passes_long_lived_client_to_jwks_only(self):
        """Discovery uses a transient client; JWKS uses the long-lived one.

        Splitting the two avoids leaking the long-lived client when a
        disable check (discovery exception, discovery-returned-disabled,
        missing redirect_base) returns before JWKS prefetch — see the
        post-condition contract on initialize_oidc_state.
        """
        cfg = _make_config(redirect_base="https://app.example.com")
        state = types.SimpleNamespace(oidc_config=cfg, jwks_data=None)

        seen_clients: list[Any] = []

        async def _discover(c, *, client=None):
            seen_clients.append(("discover", client))
            return c

        async def _jwks(_uri, *, client=None):
            seen_clients.append(("jwks", client))
            return {"keys": [{"kid": "k1"}]}

        with (
            patch("turnstone.core.oidc.discover_oidc", side_effect=_discover),
            patch("turnstone.core.oidc.fetch_jwks", side_effect=_jwks),
        ):
            asyncio.run(initialize_oidc_state(state))

        assert state.oidc_http_client is not None
        kinds = {kind for kind, _ in seen_clients}
        assert {"discover", "jwks"} <= kinds

        jwks_clients = [c for kind, c in seen_clients if kind == "jwks"]
        assert all(c is state.oidc_http_client for c in jwks_clients)
        # Discovery client is a separate transient (already closed).
        discover_clients = [c for kind, c in seen_clients if kind == "discover"]
        assert all(c is not state.oidc_http_client for c in discover_clients)

    def test_initialize_does_not_leak_client_on_discovery_failure(self):
        """Discovery exception path must close the transient and leave http_client None."""
        cfg = _make_config(redirect_base="https://app.example.com")
        state = types.SimpleNamespace(oidc_config=cfg, jwks_data=None)

        async def _boom(_cfg, *, client=None):
            raise RuntimeError("boom")

        with patch("turnstone.core.oidc.discover_oidc", side_effect=_boom):
            asyncio.run(initialize_oidc_state(state))

        assert state.oidc_config.enabled is False
        assert state.oidc_http_client is None
        assert state.jwks_data is None

    def test_initialize_does_not_leak_client_on_missing_redirect_base(self):
        """Missing redirect_base path returns before creating the long-lived client."""
        cfg = _make_config(redirect_base="")
        state = types.SimpleNamespace(oidc_config=cfg, jwks_data=None)

        async def _discover(c, *, client=None):
            return c

        with patch("turnstone.core.oidc.discover_oidc", side_effect=_discover):
            asyncio.run(initialize_oidc_state(state))

        assert state.oidc_config.enabled is False
        assert state.oidc_http_client is None
        assert state.jwks_data is None
