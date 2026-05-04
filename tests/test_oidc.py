"""Tests for turnstone.core.oidc — OIDC authentication support."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import urllib.parse
from unittest.mock import MagicMock, patch

import httpx
import jwt as pyjwt
import pytest

from turnstone.core.oidc import (
    OIDCConfig,
    OIDCError,
    apply_role_mapping,
    build_authorize_url,
    discover_oidc,
    generate_pkce_pair,
    load_oidc_config,
    provision_oidc_user,
    validate_discovered_endpoint,
    validate_id_token,
    validate_issuer_url,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> OIDCConfig:
    """Build a test OIDCConfig with sensible defaults."""
    defaults = {
        "enabled": True,
        "issuer": "https://idp.example.com",
        "client_id": "my-client",
        "client_secret": "my-secret",
        "scopes": "openid email profile",
        "provider_name": "TestIDP",
        "role_claim": "",
        "role_map": {},
        "password_enabled": True,
        "authorization_endpoint": "https://idp.example.com/authorize",
        "token_endpoint": "https://idp.example.com/token",
        "userinfo_endpoint": "https://idp.example.com/userinfo",
        "jwks_uri": "https://idp.example.com/.well-known/jwks.json",
    }
    defaults.update(overrides)
    return OIDCConfig(**defaults)


def _mock_storage(**overrides):
    """Build a MagicMock with sensible storage defaults."""
    s = MagicMock()
    s.get_oidc_identity.return_value = overrides.get("identity")
    s.get_user.return_value = overrides.get("user")
    s.get_user_by_username.return_value = overrides.get("user_by_username")
    s.get_role.return_value = overrides.get("role")
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

    def _make_request(self, host="app.example.com", scheme="https", forwarded_proto=""):
        """Build a minimal mock Starlette Request."""
        req = MagicMock()
        headers = {"host": host}
        if forwarded_proto:
            headers["x-forwarded-proto"] = forwarded_proto
        req.headers = headers
        req.url.scheme = scheme
        return req

    def test_pinned_redirect_base(self):
        """When redirect_base is set, Host header is ignored."""
        from turnstone.core.auth import _build_oidc_redirect_uri

        config = _make_config(redirect_base="https://public.example.com")
        req = self._make_request(host="internal-host:8080", scheme="http")
        result = _build_oidc_redirect_uri(req, config)
        assert result == "https://public.example.com/v1/api/auth/oidc/callback"

    def test_fallback_to_host_header(self):
        """When redirect_base is empty, redirect URI uses Host header."""
        from turnstone.core.auth import _build_oidc_redirect_uri

        config = _make_config(redirect_base="")
        req = self._make_request(host="app.example.com", scheme="https")
        result = _build_oidc_redirect_uri(req, config)
        assert result == "https://app.example.com/v1/api/auth/oidc/callback"

    def test_fallback_x_forwarded_proto(self):
        """When redirect_base is empty and X-Forwarded-Proto is https, scheme is https."""
        from turnstone.core.auth import _build_oidc_redirect_uri

        config = _make_config(redirect_base="")
        req = self._make_request(host="app.example.com", scheme="http", forwarded_proto="https")
        result = _build_oidc_redirect_uri(req, config)
        assert result == "https://app.example.com/v1/api/auth/oidc/callback"


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


class TestPKCE:
    def test_generate_pkce_pair(self):
        verifier, challenge = generate_pkce_pair()

        # Verifier should be URL-safe base64
        assert isinstance(verifier, str)
        assert len(verifier) > 40  # 48 bytes -> ~64 chars

        # Challenge should be base64url SHA-256 of verifier
        expected_digest = hashlib.sha256(verifier.encode("ascii")).digest()
        expected_challenge = base64.urlsafe_b64encode(expected_digest).rstrip(b"=").decode("ascii")
        assert challenge == expected_challenge

    def test_pkce_challenge_matches_verifier(self):
        """Manually compute challenge and verify it matches."""
        verifier, challenge = generate_pkce_pair()
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        manual_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        assert challenge == manual_challenge

    def test_pkce_pair_uniqueness(self):
        """Each call should produce a unique pair."""
        v1, c1 = generate_pkce_pair()
        v2, c2 = generate_pkce_pair()
        assert v1 != v2
        assert c1 != c2


# ---------------------------------------------------------------------------
# Authorization URL
# ---------------------------------------------------------------------------


class TestBuildAuthorizeURL:
    def test_build_authorize_url_contains_required_params(self):
        config = _make_config()
        verifier, _ = generate_pkce_pair()
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
        verifier, _ = generate_pkce_pair()

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
        verifier, _ = generate_pkce_pair()
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
        storage.create_user.assert_not_called()
        storage.create_oidc_identity.assert_not_called()

    def test_provision_oidc_user_new(self):
        """No identity -> creates user + identity."""
        config = _make_config()
        storage = _mock_storage()

        # After create_user, get_user should return the new user
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
        storage.create_user.assert_called_once()
        storage.create_oidc_identity.assert_called_once()
        # Verify create_oidc_identity was called with correct issuer and sub
        call_args = storage.create_oidc_identity.call_args
        assert call_args[0][0] == "https://idp.example.com"  # issuer
        assert call_args[0][1] == "sub-456"  # subject

    def test_provision_oidc_user_username_dedup(self):
        """First username taken -> appends suffix."""
        config = _make_config()
        storage = _mock_storage()

        # First call: username "bob" exists; second call: "bob2" doesn't exist
        storage.get_user_by_username.side_effect = [
            {"user_id": "u-other", "username": "bob"},  # "bob" taken
            None,  # "bob2" available
        ]
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
        # create_user should have been called with "bob2" as username
        call_args = storage.create_user.call_args
        assert call_args[0][1] == "bob2"

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

        # create_user should have been called with "charlie" (email prefix)
        call_args = storage.create_user.call_args
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

        call_args = storage.create_user.call_args
        assert call_args[0][1] == "user"


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

        storage.assign_role.assert_called_once_with("u1", "builtin-admin", "oidc")

    def test_apply_role_mapping_list_claim(self):
        """Claim is a list of strings -> maps each."""
        config = _make_config(
            role_claim="roles",
            role_map={"admin": "builtin-admin", "editor": "builtin-operator"},
        )
        storage = _mock_storage()
        # get_role returns non-None for both roles
        storage.get_role.return_value = {"role_id": "some-role"}

        claims = {"sub": "u1", "roles": ["admin", "editor"]}
        apply_role_mapping(storage, "u1", claims, config)

        assert storage.assign_role.call_count == 2

    def test_apply_role_mapping_no_config(self):
        """No role_claim configured -> no-op."""
        config = _make_config(role_claim="", role_map={})
        storage = _mock_storage()

        claims = {"sub": "u1", "roles": "admin"}
        apply_role_mapping(storage, "u1", claims, config)

        storage.assign_role.assert_not_called()

    def test_apply_role_mapping_unknown_role(self):
        """Claim maps to nonexistent role -> skipped."""
        config = _make_config(
            role_claim="groups",
            role_map={"admin": "nonexistent-role"},
        )
        storage = _mock_storage(role=None)  # role doesn't exist

        claims = {"sub": "u1", "groups": "admin"}
        apply_role_mapping(storage, "u1", claims, config)

        storage.assign_role.assert_not_called()

    def test_apply_role_mapping_no_matching_claim_value(self):
        """Claim value not in role_map -> no assignment."""
        config = _make_config(
            role_claim="groups",
            role_map={"admin": "builtin-admin"},
        )
        storage = _mock_storage()

        claims = {"sub": "u1", "groups": "viewer"}  # "viewer" not in role_map
        apply_role_mapping(storage, "u1", claims, config)

        storage.assign_role.assert_not_called()

    def test_apply_role_mapping_claim_missing(self):
        """Claim key not present in claims -> no-op."""
        config = _make_config(
            role_claim="groups",
            role_map={"admin": "builtin-admin"},
        )
        storage = _mock_storage()

        claims = {"sub": "u1"}  # no "groups" key
        apply_role_mapping(storage, "u1", claims, config)

        storage.assign_role.assert_not_called()

    def test_apply_role_mapping_no_role_map(self):
        """role_claim set but role_map empty -> no-op (early return)."""
        config = _make_config(role_claim="groups", role_map={})
        storage = _mock_storage()

        claims = {"sub": "u1", "groups": "admin"}
        apply_role_mapping(storage, "u1", claims, config)

        storage.assign_role.assert_not_called()

    def test_apply_role_mapping_revokes_stale_oidc_roles(self):
        """Roles previously assigned by OIDC but no longer in claims are revoked."""
        config = _make_config(
            role_claim="groups",
            role_map={"admin": "builtin-admin", "eng": "builtin-operator"},
        )
        storage = _mock_storage()
        storage.get_role.return_value = {"role_id": "some-role"}
        # User currently has admin (via OIDC) and a manual role
        storage.list_user_roles.return_value = [
            {"role_id": "builtin-admin", "assigned_by": "oidc"},
            {"role_id": "custom-role", "assigned_by": "admin-ui"},
        ]

        # IdP now only says "eng", not "admin"
        claims = {"sub": "u1", "groups": ["eng"]}
        apply_role_mapping(storage, "u1", claims, config)

        # builtin-admin should be revoked (OIDC-assigned, no longer in claims)
        storage.unassign_role.assert_called_once_with("u1", "builtin-admin")
        # custom-role should NOT be revoked (not assigned by OIDC)

    def test_apply_role_mapping_preserves_manual_roles(self):
        """Manually assigned roles are never revoked by OIDC sync."""
        config = _make_config(
            role_claim="groups",
            role_map={"admin": "builtin-admin"},
        )
        storage = _mock_storage()
        storage.get_role.return_value = {"role_id": "some-role"}
        storage.list_user_roles.return_value = [
            {"role_id": "builtin-admin", "assigned_by": "admin-ui"},
        ]

        # Claims have no groups at all
        claims = {"sub": "u1"}
        apply_role_mapping(storage, "u1", claims, config)

        # Manual admin role must NOT be revoked
        storage.unassign_role.assert_not_called()

    def test_apply_role_mapping_revokes_all_oidc_roles_when_claim_absent(self):
        """When the claim is absent from the token, all OIDC-assigned roles are revoked."""
        config = _make_config(
            role_claim="groups",
            role_map={"admin": "builtin-admin"},
        )
        storage = _mock_storage()
        storage.get_role.return_value = {"role_id": "some-role"}
        storage.list_user_roles.return_value = [
            {"role_id": "builtin-admin", "assigned_by": "oidc"},
            {"role_id": "builtin-operator", "assigned_by": "oidc"},
        ]

        claims = {"sub": "u1"}  # no "groups" key
        apply_role_mapping(storage, "u1", claims, config)

        assert storage.unassign_role.call_count == 2


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


async def _async_return(value):
    """Helper: return a value from an async function."""
    return value
