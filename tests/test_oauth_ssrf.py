"""Direct tests for the shared SSRF helpers in :mod:`turnstone.core.oauth.ssrf`.

The OIDC test suite already exercises these via the OIDC adapter
(``OIDCError`` re-raises). This file pins the canonical
:class:`OAuthSSRFError` exception so callers that don't go through OIDC
(notably ``mcp_oauth``) can rely on a stable contract.
"""

from __future__ import annotations

import urllib.parse
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from turnstone.core.oauth.ssrf import (
    OAuthSSRFError,
    OAuthSSRFPrivateAddressError,
    effective_port,
    is_localhost,
    url_is_localhost,
    validate_discovered_endpoint,
    validate_url_no_ssrf,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _resolver_like_cpython(addresses: dict[str, str]) -> Callable[..., list[Any]]:
    """A ``getaddrinfo`` stand-in that, as CPython's does, encodes a str host with the IDNA 2003
    ``idna`` codec before the lookup; *addresses* is keyed by the name the lookup sends."""

    def getaddrinfo(host: str | bytes, *args: Any, **kwargs: Any) -> list[Any]:
        name = host.encode("idna") if isinstance(host, str) else host
        return [(2, 1, 6, "", (addresses[name.decode("ascii")], 0))]

    return getaddrinfo


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

    def test_private_address_raises_distinct_subclass(self) -> None:
        # Callers with an operator opt-in (OIDC) catch the subclass to
        # append the remediation hint; plain OAuthSSRFError catches still work.
        with (
            patch("socket.getaddrinfo", return_value=self._PRIVATE_ADDR),
            pytest.raises(OAuthSSRFPrivateAddressError),
        ):
            validate_url_no_ssrf("https://corp.example.com", allow_http=False)

    def test_allow_private_accepts_rfc1918(self) -> None:
        with patch("socket.getaddrinfo", return_value=self._PRIVATE_ADDR):
            parsed = validate_url_no_ssrf(
                "https://auth.corp.example.com", allow_http=False, allow_private=True
            )
        assert parsed.hostname == "auth.corp.example.com"

    def test_allow_private_accepts_cgnat(self) -> None:
        # 100.64/10 (RFC 6598, shared address space) — e.g. an overlay-VPN IdP.
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("100.64.0.7", 0))]):
            validate_url_no_ssrf("https://idp.tail.example", allow_http=False, allow_private=True)

    def test_allow_private_accepts_loopback_hostname(self) -> None:
        # A non-localhost hostname resolving to loopback (IdP behind a
        # local reverse proxy) is operator-trusted under the opt-in.
        with patch("socket.getaddrinfo", return_value=self._LOOPBACK_ADDR):
            validate_url_no_ssrf("https://auth.internal", allow_http=False, allow_private=True)

    def test_allow_private_still_rejects_link_local(self) -> None:
        # Cloud metadata services live on link-local; no legitimate IdP does.
        with (
            patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("169.254.169.254", 0))]),
            pytest.raises(OAuthSSRFError, match="refused even with private"),
        ):
            validate_url_no_ssrf("https://md.example.com", allow_http=False, allow_private=True)

    def test_allow_private_still_rejects_unspecified(self) -> None:
        # The message names the class so 0.0.0.0/:: rejections are unambiguous.
        with (
            patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("0.0.0.0", 0))]),
            pytest.raises(OAuthSSRFError, match="unspecified"),
        ):
            validate_url_no_ssrf("https://zero.example.com", allow_http=False, allow_private=True)

    def test_allow_private_does_not_relax_https(self) -> None:
        with pytest.raises(OAuthSSRFError, match="must use HTTPS"):
            validate_url_no_ssrf(
                "http://auth.corp.example.com", allow_http=False, allow_private=True
            )

    def test_rejects_unresolvable(self) -> None:
        import socket

        with (
            patch("socket.getaddrinfo", side_effect=socket.gaierror("fail")),
            pytest.raises(OAuthSSRFError, match="cannot be resolved"),
        ):
            validate_url_no_ssrf("https://no.such.host.invalid", allow_http=False)

    @pytest.mark.parametrize(
        "char", [chr(9), chr(10), chr(13), chr(1), chr(0x7F)], ids=["tab", "lf", "cr", "soh", "del"]
    )
    def test_refuses_control_characters_before_resolving(self, char: str) -> None:
        """urllib drops TAB, CR and LF and keeps the other control characters, so the checks
        would judge a different URL from the one the client sends, and the client refuses it."""
        with (
            patch("socket.getaddrinfo") as resolve,
            pytest.raises(OAuthSSRFError, match="control characters"),
        ):
            validate_url_no_ssrf(f"https://idp.example.com/to{char}ken", allow_http=False)
        resolve.assert_not_called()

    def test_refuses_a_host_the_client_cannot_encode(self) -> None:
        """This host passes the resolver's IDNA 2003 encoding and fails the client's IDNA 2008."""
        snowman = chr(0x2603)
        with (
            patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR),
            pytest.raises(OAuthSSRFError, match="malformed"),
        ):
            validate_url_no_ssrf(f"https://{snowman}.example.com/token", allow_http=False)

    @pytest.mark.parametrize(
        "url",
        [
            "https://idp.example.com/a" + chr(0xD800),
            "https://idp.example.com/?q=" + chr(0xD800),
            "https://u" + chr(0xD800) + "@idp.example.com/",
        ],
        ids=["path", "query", "userinfo"],
    )
    def test_refuses_a_lone_surrogate(self, url: str) -> None:
        """A JSON document can carry a lone surrogate, which httpx2 can't encode into a URL."""
        with (
            patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR),
            pytest.raises(OAuthSSRFError, match="malformed"),
        ):
            validate_url_no_ssrf(url, allow_http=False)

    def test_refuses_an_out_of_range_port(self) -> None:
        """httpx2 accepts this port and urllib refuses it; the refusal is the guard's own error."""
        with pytest.raises(OAuthSSRFError, match="malformed"):
            validate_url_no_ssrf("https://idp.example.com:99999/token", allow_http=False)

    def test_a_space_the_client_encodes_is_still_accepted(self) -> None:
        """Accepted, and returned in the form the client sends."""
        with patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR):
            parsed = validate_url_no_ssrf("https://idp.example.com/a b", allow_http=False)
        assert parsed.path == "/a%20b"

    def test_returns_the_host_the_client_dials(self) -> None:
        """Callers compare hosts (same-origin, the localhost lane) on this result, so it carries
        the dialed name: lowercase IDNA 2008 ASCII."""
        sharp_s = chr(0xDF)
        with patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR):
            parsed = validate_url_no_ssrf(f"https://STRA{sharp_s}E.Example.com/x", allow_http=False)
        assert parsed.hostname == "xn--strae-oqa.example.com"

    def test_without_the_localhost_lane_http_to_localhost_is_refused(self) -> None:
        with (
            patch("socket.getaddrinfo", return_value=self._LOOPBACK_ADDR),
            pytest.raises(OAuthSSRFError, match="must use HTTPS"),
        ):
            validate_url_no_ssrf("http://localhost:6379/x", allow_http=True, allow_localhost=False)

    def test_without_the_localhost_lane_loopback_is_not_public(self) -> None:
        with (
            patch("socket.getaddrinfo", return_value=self._LOOPBACK_ADDR),
            pytest.raises(OAuthSSRFPrivateAddressError),
        ):
            validate_url_no_ssrf("https://localhost:6379/x", allow_http=True, allow_localhost=False)

    @pytest.mark.parametrize(
        ("url", "local"),
        [
            ("http://127.0.0.1:8093/mcp", True),
            ("http://kc.localhost:8080/realms/x", True),
            ("http://kc" + chr(0x3002) + "localhost:8080/realms/x", True),
            ("https://mcp.example.com/sse", False),
            ("https://" + chr(0x2603) + ".example.com/", False),
        ],
        ids=["loopback-literal", "localhost-subdomain", "ideographic-dot", "public", "unparseable"],
    )
    def test_url_is_localhost_judges_the_dialed_host(self, url: str, local: bool) -> None:
        assert url_is_localhost(url) is local

    def test_refuses_a_non_ascii_ipv6_zone_before_resolving(self) -> None:
        """httpx2 parses this host but can't send it as ASCII. The refusal must not depend on the
        resolver failing for it, so even one that answers for any name doesn't change it."""
        with (
            patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR),
            pytest.raises(OAuthSSRFError, match="malformed"),
        ):
            validate_url_no_ssrf("https://[fe80::1%" + chr(0xFC) + "]/x", allow_http=False)

    def test_resolves_the_name_the_client_dials(self) -> None:
        """A host whose IDNA 2003 and IDNA 2008 encodings differ names two hosts. The client
        dials the 2008 form; resolving the Unicode name would check the 2003 one's addresses."""
        sharp_s = chr(0xDF)
        addresses = {
            "strasse.example.com": "93.184.216.34",
            "xn--strae-oqa.example.com": "10.0.0.12",
        }
        with (
            patch("socket.getaddrinfo", side_effect=_resolver_like_cpython(addresses)),
            pytest.raises(OAuthSSRFPrivateAddressError, match="non-public address"),
        ):
            validate_url_no_ssrf(f"https://stra{sharp_s}e.example.com/token", allow_http=False)


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

    def test_allow_private_passes_through(self) -> None:
        # Same-origin endpoint on a private-resolving issuer host is accepted
        # when the operator opted in.
        issuer = urllib.parse.urlparse("https://auth.corp.example.com")
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.5", 0))]):
            validate_discovered_endpoint(
                "https://auth.corp.example.com/token",
                issuer,
                allow_http=False,
                trusted_endpoint_hosts=frozenset(),
                allow_private=True,
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

    def test_trusted_endpoint_host_matches_in_either_spelling(self) -> None:
        """Hosts compare as the client dials them, so an operator may write a trusted host in
        Unicode or as its ``xn--`` name, and a document may spell the endpoint either way."""
        issuer = urllib.parse.urlparse("https://idp.example.com")
        unicode_host = "b" + chr(0xFC) + "cher.example.com"
        ascii_host = "xn--bcher-kva.example.com"
        with patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR):
            for endpoint_host, trusted_host in (
                (unicode_host, unicode_host),
                (ascii_host, unicode_host),
                (unicode_host, ascii_host),
            ):
                validate_discovered_endpoint(
                    f"https://{endpoint_host}/token",
                    issuer,
                    allow_http=False,
                    trusted_endpoint_hosts=frozenset({trusted_host}),
                )

    def test_trusted_endpoint_host_holding_a_port_matches_nothing(self) -> None:
        issuer = urllib.parse.urlparse("https://idp.example.com")
        with (
            patch("socket.getaddrinfo", return_value=self._PUBLIC_ADDR),
            pytest.raises(OAuthSSRFError, match="not trusted"),
        ):
            validate_discovered_endpoint(
                "https://shard.example.com/token",
                issuer,
                allow_http=False,
                trusted_endpoint_hosts=frozenset({"shard.example.com:443"}),
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
