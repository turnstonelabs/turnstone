"""Direct tests for the shared SSRF helpers in :mod:`turnstone.core.oauth_ssrf`.

The OIDC test suite already exercises these via the OIDC adapter
(``OIDCError`` re-raises). This file pins the canonical
:class:`OAuthSSRFError` exception so callers that don't go through OIDC
(notably ``mcp_oauth``) can rely on a stable contract.
"""

from __future__ import annotations

import urllib.parse
from unittest.mock import patch

import pytest

from turnstone.core.oauth_ssrf import (
    OAuthSSRFError,
    effective_port,
    is_localhost,
    validate_discovered_endpoint,
    validate_url_no_ssrf,
)


class TestIsLocalhost:
    def test_loopback_names(self) -> None:
        assert is_localhost("localhost")
        assert is_localhost("127.0.0.1")
        assert is_localhost("::1")
        assert is_localhost("foo.localhost")

    def test_non_loopback(self) -> None:
        assert not is_localhost("example.com")
        assert not is_localhost("internal.corp")


class TestEffectivePort:
    def test_explicit_port(self) -> None:
        p = urllib.parse.urlparse("https://idp.example.com:9443/foo")
        assert effective_port(p) == 9443

    def test_default_https(self) -> None:
        p = urllib.parse.urlparse("https://idp.example.com/foo")
        assert effective_port(p) == 443

    def test_default_http(self) -> None:
        p = urllib.parse.urlparse("http://idp.example.com/foo")
        assert effective_port(p) == 80

    def test_unknown_scheme(self) -> None:
        p = urllib.parse.urlparse("ftp://idp.example.com/foo")
        assert effective_port(p) is None


class TestValidateUrlNoSSRF:
    _PUBLIC_ADDR = [(2, 1, 6, "", ("93.184.216.34", 0))]
    _PRIVATE_ADDR = [(2, 1, 6, "", ("10.0.0.1", 0))]
    _LOOPBACK_ADDR = [(2, 1, 6, "", ("127.0.0.1", 0))]

    def test_valid_https(self) -> None:
        with patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR):
            parsed = validate_url_no_ssrf("https://idp.example.com/foo", allow_http=False)
        assert parsed.scheme == "https"
        assert parsed.hostname == "idp.example.com"

    def test_rejects_http_when_not_allowed(self) -> None:
        with pytest.raises(OAuthSSRFError, match="must use HTTPS"):
            validate_url_no_ssrf("http://idp.example.com", allow_http=False)

    def test_allows_http_localhost_with_flag(self) -> None:
        with patch("socket.getaddrinfo", return_value=self._LOOPBACK_ADDR):
            validate_url_no_ssrf("http://localhost:8080", allow_http=True)

    def test_rejects_http_non_localhost_even_with_flag(self) -> None:
        with pytest.raises(OAuthSSRFError, match="must use HTTPS"):
            validate_url_no_ssrf("http://idp.example.com", allow_http=True)

    def test_rejects_userinfo(self) -> None:
        with pytest.raises(OAuthSSRFError, match="embedded credentials"):
            validate_url_no_ssrf("https://user:pass@idp.example.com", allow_http=False)

    def test_rejects_private_address(self) -> None:
        with (
            patch("socket.getaddrinfo", return_value=self._PRIVATE_ADDR),
            pytest.raises(OAuthSSRFError, match="non-public address"),
        ):
            validate_url_no_ssrf("https://corp.example.com", allow_http=False)

    def test_rejects_unresolvable(self) -> None:
        import socket

        with (
            patch("socket.getaddrinfo", side_effect=socket.gaierror("fail")),
            pytest.raises(OAuthSSRFError, match="cannot be resolved"),
        ):
            validate_url_no_ssrf("https://no.such.host.invalid", allow_http=False)


class TestValidateDiscoveredEndpoint:
    _PUBLIC_ADDR = [(2, 1, 6, "", ("93.184.216.34", 0))]

    def test_same_origin_passes(self) -> None:
        issuer = urllib.parse.urlparse("https://idp.example.com")
        with patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR):
            validate_discovered_endpoint(
                "https://idp.example.com/token",
                issuer,
                allow_http=False,
                trusted_endpoint_hosts=frozenset(),
            )

    def test_third_party_host_rejected(self) -> None:
        issuer = urllib.parse.urlparse("https://idp.example.com")
        with (
            patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR),
            pytest.raises(OAuthSSRFError, match="not trusted"),
        ):
            validate_discovered_endpoint(
                "https://attacker.example.com/token",
                issuer,
                allow_http=False,
                trusted_endpoint_hosts=frozenset(),
            )

    def test_trusted_endpoint_host_passes(self) -> None:
        issuer = urllib.parse.urlparse("https://idp.example.com")
        with patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR):
            validate_discovered_endpoint(
                "https://shard.example.com/token",
                issuer,
                allow_http=False,
                trusted_endpoint_hosts=frozenset({"shard.example.com"}),
            )

    def test_known_google_alias_passes(self) -> None:
        """The hard-coded Google alias map covers oauth2.googleapis.com."""
        issuer = urllib.parse.urlparse("https://accounts.google.com")
        with patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR):
            validate_discovered_endpoint(
                "https://oauth2.googleapis.com/token",
                issuer,
                allow_http=False,
                trusted_endpoint_hosts=frozenset(),
            )

    def test_scheme_mismatch_rejected(self) -> None:
        # When the issuer is http://localhost (allow_http=True), an
        # https:// endpoint must still be rejected as a scheme mismatch.
        issuer = urllib.parse.urlparse("http://localhost:8080")
        with (
            patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 0))]),
            pytest.raises(OAuthSSRFError, match="scheme"),
        ):
            validate_discovered_endpoint(
                "https://localhost:8080/token",
                issuer,
                allow_http=True,
                trusted_endpoint_hosts=frozenset(),
            )

    def test_port_mismatch_rejected(self) -> None:
        issuer = urllib.parse.urlparse("https://idp.example.com")
        with (
            patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR),
            pytest.raises(OAuthSSRFError, match="port"),
        ):
            validate_discovered_endpoint(
                "https://idp.example.com:9443/token",
                issuer,
                allow_http=False,
                trusted_endpoint_hosts=frozenset(),
            )
