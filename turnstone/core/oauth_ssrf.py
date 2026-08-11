"""Shared SSRF and same-origin validation for OAuth/OIDC endpoint URLs.

Extracted from :mod:`turnstone.core.oidc` so the per-(user, server) MCP
OAuth flow (see :mod:`turnstone.core.mcp_oauth`) can reuse the exact same
guards without depending on the OIDC module.

The canonical exception is :class:`OAuthSSRFError`. The OIDC module wraps
calls to these helpers and re-raises ``OIDCError`` so its public API is
unchanged. The MCP OAuth module catches :class:`OAuthSSRFError` directly.

DNS-rebinding limitation: this module resolves the hostname during
validation, but the subsequent ``httpx`` call resolves again. A hostname
the operator points at could in principle rebind between the two resolves
to expose an internal address. Callers must ensure the AS / IdP hostname
is operator-controlled — the SSRF guard prevents private-IP responses for
hostnames the operator points at, but does not prevent rebinding by a
hostile DNS authority. Pinning a single resolution into the ``httpx``
transport is a future hardening step.
"""

from __future__ import annotations

import asyncio
import urllib.parse

from turnstone.core.ip_classify import (
    AddressLane,
    ResolutionError,
    describe_address,
    reaches_only_loopback,
    resolve_and_classify,
)

# ---------------------------------------------------------------------------
# Trusted-host allowlist for well-known multi-origin IdPs / authorization
# servers whose discovery documents legitimately reference endpoints on
# hostnames distinct from the issuer hostname. eTLD+1 matching does not
# work here (e.g. google.com vs googleapis.com), so an explicit allow-map
# is the only safe option.
# ---------------------------------------------------------------------------

KNOWN_TRUSTED_OAUTH_ENDPOINT_HOSTS: dict[str, frozenset[str]] = {
    "accounts.google.com": frozenset(
        {
            "accounts.google.com",
            "oauth2.googleapis.com",
            "www.googleapis.com",
            "openidconnect.googleapis.com",
        }
    ),
    # Microsoft Entra ID (commercial cloud). The tenant issuer is
    # login.microsoftonline.com/<tenant>/v2.0, but its discovery document
    # advertises userinfo_endpoint on graph.microsoft.com — a distinct host.
    # Without this entry, discovery rejects the cross-host userinfo and
    # disables OIDC, so every Azure AD deployment would need a manual
    # trusted_endpoint_hosts override to log in. (Sovereign clouds —
    # login.microsoftonline.us / *.chinacloudapi.cn — use their own graph
    # hosts and can be added the same way.)
    "login.microsoftonline.com": frozenset(
        {
            "login.microsoftonline.com",
            "graph.microsoft.com",
        }
    ),
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OAuthSSRFError(Exception):
    """Raised when an SSRF/same-origin validation fails.

    OIDC callers wrap this and re-raise as ``OIDCError`` to preserve the
    existing public API.
    """


class OAuthSSRFPrivateAddressError(OAuthSSRFError):
    """A hostname resolved to a non-public address, specifically.

    A distinct subclass so callers with an operator-facing opt-in
    (``[oidc] allow_private_network``) can catch this case and append the
    remediation hint, while callers with no such opt-in (``mcp_oauth``,
    where endpoint URLs come from untrusted remote-server metadata) keep
    catching :class:`OAuthSSRFError` and stay strict.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def is_localhost(hostname: str) -> bool:
    """Return True if *hostname* refers to the loopback interface."""
    return hostname in ("localhost", "127.0.0.1", "::1") or hostname.endswith(".localhost")


def sanitize_log_text(text: str, limit: int = 200) -> str:
    """Escape control characters and truncate untrusted text for log/audit inclusion.

    Untrusted bytes (e.g. an AS error body, ``error_description`` from a
    callback redirect) embedded in log lines or exception messages must not
    be able to forge fake log records via CR/LF or hide content via NULs /
    other control characters. ``unicode_escape`` renders these as visible
    ``\\r``, ``\\n``, ``\\x00`` etc., and *limit* caps the *rendered* length.

    Shared with the OIDC module — its private ``_sanitize_log_text`` is a
    legacy alias that forwards here.
    """
    if not text:
        return ""
    return text.encode("unicode_escape").decode("ascii")[:limit]


def effective_port(parsed: urllib.parse.ParseResult) -> int | None:
    """Return the explicit port if set, else the scheme default."""
    if parsed.port is not None:
        return parsed.port
    return {"http": 80, "https": 443}.get(parsed.scheme)


def validate_url_no_ssrf(
    url: str, *, allow_http: bool, allow_private: bool = False
) -> urllib.parse.ParseResult:
    """Run the scheme/userinfo/SSRF checks shared by issuer and discovered URLs.

    Returns the parsed URL on success. Raises :class:`OAuthSSRFError` on
    failure. Two knobs: ``allow_http=True`` accepts ``http://`` *if* the
    hostname is also a localhost form (when ``False``, only ``https://``
    is accepted); ``allow_private=True`` accepts hostnames resolving to
    private-range addresses (RFC 1918, ULA, CGNAT, loopback) for
    operator-trusted URLs — a self-hosted IdP on an internal network.
    Even with ``allow_private``, link-local, multicast, unspecified, and
    reserved addresses stay refused: cloud metadata services
    (169.254.169.254) are the canonical SSRF target, and no legitimate
    IdP lives in those ranges. Non-public rejections raise the
    :class:`OAuthSSRFPrivateAddressError` subclass so callers that *have*
    an opt-in can point the operator at it.

    Both knobs judge an IPv6 transition address (NAT64, 6to4, Teredo,
    IPv4-mapped, IPv4-compatible) by the IPv4 address it routes to rather
    than by the wrapper — see :mod:`turnstone.core.ip_classify`. A NAT64
    address embedding 192.168.1.5 is therefore treated exactly as
    192.168.1.5 would be: allowed under ``allow_private``, refused
    without it.

    ``http://`` on a localhost hostname additionally requires every resolved
    address to *be* loopback. ``*.localhost`` is ordinary DNS, so a hostile
    authority can point it anywhere; the name alone is not evidence, and
    accepting it would put an OIDC token exchange — ``client_secret`` and
    authorization code included — on the wire in the clear.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname
        _ = parsed.port  # parsed lazily; raises for an out-of-range value
    except ValueError as exc:
        # Callers catch OAuthSSRFError only, and the URL can come from a
        # hostile MCP server's discovery document — a bare ValueError here
        # would escape discovery instead of becoming a clean refusal.
        raise OAuthSSRFError(f"endpoint URL is malformed: {url}") from exc

    if not hostname:
        raise OAuthSSRFError(f"endpoint URL has no hostname: {url}")

    if parsed.username or parsed.password:
        raise OAuthSSRFError("endpoint URL must not contain embedded credentials (userinfo)")

    # Cleartext is permitted only for a genuine loopback dev server. The
    # hostname is not evidence of that — ``*.localhost`` is ordinary DNS a
    # hostile authority answers however it likes — so the verdict is deferred
    # until resolution proves loopback below. Deciding it here on the name
    # alone would send an OIDC token exchange, client_secret and all, in the
    # clear to whatever address that name happens to return.
    http_pending_loopback_proof = False
    if parsed.scheme != "https":
        if allow_http and parsed.scheme == "http" and is_localhost(hostname):
            http_pending_loopback_proof = True
        else:
            raise OAuthSSRFError(f"endpoint URL must use HTTPS (got {parsed.scheme}://): {url}")

    # ``resolve_and_classify`` raises rather than returning an empty list, so the
    # deferred cleartext verdict below cannot be granted by default when a name
    # answers with nothing.
    try:
        classified = resolve_and_classify(hostname)
    except ResolutionError as exc:
        raise OAuthSSRFError(f"endpoint {exc}: {url}") from exc

    for lane, addr in classified:
        # Ordered so the refusal names the real problem. Deciding the scheme
        # first reports a metadata address as merely "must use HTTPS", and an
        # operator acting on that re-registers the same endpoint over TLS
        # without ever learning it pointed at the instance metadata service.
        if lane is AddressLane.NEVER:
            raise OAuthSSRFError(
                f"endpoint URL resolves to a link-local/multicast/unspecified/"
                f"reserved/metadata address ({describe_address(addr)}), refused "
                f"even with private addresses allowed: {url}"
            )
        # Cleartext must reach loopback and nothing else, on every record.
        if http_pending_loopback_proof and not reaches_only_loopback(addr):
            raise OAuthSSRFError(
                f"endpoint URL must use HTTPS: {hostname} does not resolve to "
                f"loopback ({describe_address(addr)}): {url}"
            )
        if lane is AddressLane.PUBLIC or allow_private:
            continue
        # The localhost development lane, gated on the RESOLVED address rather
        # than the hostname — and on the same predicate as the cleartext gate,
        # since two spellings of "reaches loopback" disagree for a Teredo
        # wrapper whose server half is loopback and whose client half is not.
        if is_localhost(hostname) and reaches_only_loopback(addr):
            continue
        raise OAuthSSRFPrivateAddressError(
            f"endpoint URL resolves to non-public address ({describe_address(addr)}): {url}"
        )

    return parsed


def validate_discovered_endpoint(
    url: str,
    issuer_parsed: urllib.parse.ParseResult,
    *,
    allow_http: bool,
    trusted_endpoint_hosts: frozenset[str],
    allow_private: bool = False,
) -> None:
    """Validate an endpoint URL pulled from an OIDC/OAuth discovery document.

    Applies :func:`validate_url_no_ssrf` plus the same-origin / trusted-host
    constraint: the endpoint host must equal the issuer host, be in the
    well-known trust map, or be in the operator-supplied
    ``trusted_endpoint_hosts``.  Effective port (with scheme defaults
    applied) and scheme must match the issuer.  ``allow_private`` forwards
    to :func:`validate_url_no_ssrf`.

    Raises :class:`OAuthSSRFError` on validation failure.
    """
    parsed = validate_url_no_ssrf(url, allow_http=allow_http, allow_private=allow_private)

    issuer_hostname = (issuer_parsed.hostname or "").lower()
    endpoint_hostname = (parsed.hostname or "").lower()

    if parsed.scheme != issuer_parsed.scheme:
        raise OAuthSSRFError(
            f"discovered endpoint scheme ({parsed.scheme}) "
            f"does not match issuer ({issuer_parsed.scheme}): {url}"
        )

    known_trusted = KNOWN_TRUSTED_OAUTH_ENDPOINT_HOSTS.get(issuer_hostname, frozenset())
    host_allowed = (
        endpoint_hostname == issuer_hostname
        or endpoint_hostname in known_trusted
        or endpoint_hostname in trusted_endpoint_hosts
    )
    if not host_allowed:
        raise OAuthSSRFError(
            f"discovered endpoint host ({endpoint_hostname}) "
            f"does not match issuer ({issuer_hostname}) and is not trusted: {url}"
        )

    endpoint_port = effective_port(parsed)
    issuer_port = effective_port(issuer_parsed)
    if endpoint_port != issuer_port:
        raise OAuthSSRFError(
            f"discovered endpoint port ({endpoint_port}) "
            f"does not match issuer ({issuer_port}): {url}"
        )


async def validate_url_no_ssrf_async(
    url: str, *, allow_http: bool, allow_private: bool = False
) -> urllib.parse.ParseResult:
    """Async variant of :func:`validate_url_no_ssrf` for hot-path callers.

    The synchronous variant calls ``socket.getaddrinfo``, which blocks
    the event loop. Async OAuth flows (notably
    :mod:`turnstone.core.mcp_oauth`) wrap their validation calls in
    :func:`asyncio.to_thread` to keep the loop responsive. This wrapper
    centralises that wrapping so callers don't repeat the idiom.
    """
    return await asyncio.to_thread(
        validate_url_no_ssrf, url, allow_http=allow_http, allow_private=allow_private
    )


async def validate_discovered_endpoint_async(
    url: str,
    issuer_parsed: urllib.parse.ParseResult,
    *,
    allow_http: bool,
    trusted_endpoint_hosts: frozenset[str],
    allow_private: bool = False,
) -> None:
    """Async variant of :func:`validate_discovered_endpoint`."""
    await asyncio.to_thread(
        validate_discovered_endpoint,
        url,
        issuer_parsed,
        allow_http=allow_http,
        trusted_endpoint_hosts=trusted_endpoint_hosts,
        allow_private=allow_private,
    )


__all__ = [
    "KNOWN_TRUSTED_OAUTH_ENDPOINT_HOSTS",
    "OAuthSSRFError",
    "OAuthSSRFPrivateAddressError",
    "effective_port",
    "is_localhost",
    "sanitize_log_text",
    "validate_discovered_endpoint",
    "validate_discovered_endpoint_async",
    "validate_url_no_ssrf",
    "validate_url_no_ssrf_async",
]
