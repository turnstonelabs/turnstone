"""Per-(user, server) OAuth 2.1 + PKCE flow for MCP servers.

Audience validation note: the spec says clients MUST verify ``aud`` on
JWT access tokens. Opaque tokens have no inspection surface, so for
those we log + trust the AS contract (matching Cursor / Claude Desktop
behavior). The ``aud`` mismatch on a JWT is a hard fail, but a missing
``aud`` claim or an opaque token only logs a warning. The set of
acceptable audience values is the resolved
``oauth_audience or server_url`` — this matches non-RFC-8707 ASes
(Auth0) that issue tokens with ``aud=oauth_audience`` rather than the
canonical resource URL.

Multi-node refresh contention: this module currently relies on an
``asyncio.Lock`` keyed by ``(user_id, server_name)`` for in-process
serialization. This is correct for SQLite single-node deployments but
NOT sufficient for multi-node setups. A later iteration wraps the
multi-node case in a postgres advisory lock — see the note in
:func:`get_user_access_token`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx

from turnstone.core.audit import record_audit
from turnstone.core.log import get_logger
from turnstone.core.mcp_crypto import MCPTokenDecryptError
from turnstone.core.oauth_ssrf import (
    OAuthSSRFError,
    sanitize_log_text,
    validate_discovered_endpoint_async,
    validate_url_no_ssrf_async,
)

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

    from turnstone.core.mcp_crypto import MCPTokenStore
    from turnstone.core.storage._protocol import StorageBackend

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MCP_OAUTH_STATE_TTL_SECONDS = 600
MCP_OAUTH_DISCOVERY_CACHE_TTL_SECONDS = 86400
_DEFAULT_HTTP_TIMEOUT = 10.0
_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 60
_PENDING_CLEANUP_INTERVAL_S = 60.0

# Limits on PRM/AS body sizes — defensive against runaway responses.
_MAX_DISCOVERY_BODY_BYTES = 256 * 1024
# Tighter cap for token-endpoint and DCR responses — these never carry
# JWKS-style payloads and are bounded in well-formed AS implementations.
_MAX_TOKEN_BODY_BYTES = 64 * 1024


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MCPOAuthError(Exception):
    """Base class for MCP OAuth flow errors."""


class MCPOAuthDiscoveryError(MCPOAuthError):
    """Discovery (PRM, AS metadata) failed or returned unsuitable values."""


class MCPOAuthExchangeError(MCPOAuthError):
    """Authorization-code exchange failed."""


class MCPOAuthRefreshFailed(MCPOAuthError):  # noqa: N818 — name reflects domain semantics
    """Refresh-token grant failed.

    Caller should treat this as a re-consent trigger: delete the user
    token row and emit ``mcp_consent_required``.
    """


# ---------------------------------------------------------------------------
# Authorization-server metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ASMetadata:
    """Subset of RFC 8414 authorization-server metadata that this module uses.

    All fields are populated from the AS's ``.well-known/oauth-authorization-server``
    document (or whatever ``issuer`` resolves to). Only the fields the
    flow actually consumes are surfaced — the document itself can carry
    arbitrarily many keys.
    """

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    jwks_uri: str | None
    code_challenge_methods_supported: tuple[str, ...]
    token_endpoint_auth_methods_supported: tuple[str, ...]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


_PRM_RESOURCE_METADATA_KEY_RE = re.compile(
    r"resource_metadata\s*=\s*",
    re.IGNORECASE,
)


def _parse_quoted_string(text: str, start: int) -> tuple[str, int] | None:
    """Parse an RFC 7230 ``quoted-string`` starting at ``text[start]``.

    Returns ``(value, end_index)`` where ``end_index`` is the index just
    past the closing quote, or ``None`` if the input is malformed (no
    opening quote, unterminated string).

    Handles ``\\"`` and ``\\\\`` escapes per RFC 7230 §3.2.6 — the prior
    naive ``([^"]+)`` regex truncated the URL at the first unescaped quote
    and silently dropped backslash escapes from the value.
    """
    if start >= len(text) or text[start] != '"':
        return None
    out: list[str] = []
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            out.append(text[i + 1])
            i += 2
            continue
        if ch == '"':
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    return None


def _parse_prm_url_from_www_authenticate(header: str) -> str | None:
    """Extract ``resource_metadata`` URL from a ``WWW-Authenticate: Bearer`` header.

    Returns the URL string or ``None`` when the header lacks the param,
    is malformed, or terminates the quoted-string prematurely.
    """
    if not header:
        return None
    for match in _PRM_RESOURCE_METADATA_KEY_RE.finditer(header):
        parsed = _parse_quoted_string(header, match.end())
        if parsed is not None:
            return parsed[0]
    return None


async def _fetch_prm_issuer(
    server_url: str,
    *,
    http_client: httpx.AsyncClient,
) -> str:
    """Fetch the protected-resource metadata document and return its ``authorization_servers[0]``.

    Tries the well-known PRM URL relative to the server URL first; if
    the server returns 401 with a ``WWW-Authenticate: Bearer
    resource_metadata="..."`` header, follows that URL.

    The caller is responsible for passing the resulting issuer to
    :func:`_fetch_as_metadata` along with its ``trusted_hosts`` list —
    PRM itself is anchored on the resource-server origin, so trust
    expansion only matters for AS-metadata endpoint validation.

    Raises :class:`MCPOAuthDiscoveryError` on validation failure.
    """
    parsed = urllib.parse.urlparse(server_url)
    if not parsed.scheme or not parsed.hostname:
        raise MCPOAuthDiscoveryError(f"server URL is not absolute: {server_url}")
    base = f"{parsed.scheme}://{parsed.netloc}"
    prm_url = base.rstrip("/") + "/.well-known/oauth-protected-resource"

    try:
        await validate_url_no_ssrf_async(prm_url, allow_http=True)
    except OAuthSSRFError as exc:
        raise MCPOAuthDiscoveryError(f"PRM URL rejected: {exc}") from exc

    try:
        resp = await http_client.get(prm_url, timeout=_DEFAULT_HTTP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise MCPOAuthDiscoveryError(f"PRM fetch failed: {exc}") from exc

    if resp.status_code == 401:
        # 401 with Bearer challenge — follow resource_metadata URL.
        challenge_url = _parse_prm_url_from_www_authenticate(
            resp.headers.get("www-authenticate", "")
        )
        if challenge_url is None:
            raise MCPOAuthDiscoveryError(
                "server returned 401 without resource_metadata in WWW-Authenticate"
            )
        try:
            await validate_url_no_ssrf_async(challenge_url, allow_http=True)
        except OAuthSSRFError as exc:
            raise MCPOAuthDiscoveryError(f"PRM challenge URL rejected: {exc}") from exc
        try:
            resp = await http_client.get(challenge_url, timeout=_DEFAULT_HTTP_TIMEOUT)
        except httpx.HTTPError as exc:
            raise MCPOAuthDiscoveryError(f"PRM challenge fetch failed: {exc}") from exc
    if resp.status_code != 200:
        raise MCPOAuthDiscoveryError(f"PRM returned HTTP {resp.status_code}")

    if len(resp.content) > _MAX_DISCOVERY_BODY_BYTES:
        raise MCPOAuthDiscoveryError("PRM response body exceeds size limit")

    try:
        doc = resp.json()
    except ValueError as exc:
        raise MCPOAuthDiscoveryError(f"PRM body is not valid JSON: {exc}") from exc

    if not isinstance(doc, dict):
        raise MCPOAuthDiscoveryError("PRM body is not a JSON object")

    auth_servers = doc.get("authorization_servers")
    if not isinstance(auth_servers, list) or not auth_servers:
        raise MCPOAuthDiscoveryError(
            "PRM document missing 'authorization_servers' or list is empty"
        )
    issuer_url = auth_servers[0]
    if not isinstance(issuer_url, str) or not issuer_url:
        raise MCPOAuthDiscoveryError("PRM authorization_servers[0] is empty or non-string")

    # SSRF protection on the issuer URL itself — same-origin / trust-list
    # checks happen in ``_fetch_as_metadata``.
    try:
        await validate_url_no_ssrf_async(issuer_url, allow_http=True)
    except OAuthSSRFError as exc:
        raise MCPOAuthDiscoveryError(f"PRM issuer URL rejected: {exc}") from exc

    return issuer_url


async def _fetch_as_metadata(
    issuer: str,
    *,
    http_client: httpx.AsyncClient,
    trusted_hosts: frozenset[str],
) -> ASMetadata:
    """Fetch ``.well-known/oauth-authorization-server`` for *issuer*.

    Validates the discovered ``authorization_endpoint`` and
    ``token_endpoint`` are same-origin (or in ``trusted_hosts``).
    Requires S256 code challenge support — raises
    :class:`MCPOAuthDiscoveryError` otherwise.
    """
    try:
        issuer_parsed = await validate_url_no_ssrf_async(issuer, allow_http=True)
    except OAuthSSRFError as exc:
        raise MCPOAuthDiscoveryError(f"AS issuer URL rejected: {exc}") from exc

    metadata_url = issuer.rstrip("/") + "/.well-known/oauth-authorization-server"
    try:
        resp = await http_client.get(metadata_url, timeout=_DEFAULT_HTTP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise MCPOAuthDiscoveryError(f"AS metadata fetch failed: {exc}") from exc

    if resp.status_code != 200:
        raise MCPOAuthDiscoveryError(f"AS metadata returned HTTP {resp.status_code}")

    if len(resp.content) > _MAX_DISCOVERY_BODY_BYTES:
        raise MCPOAuthDiscoveryError("AS metadata response body exceeds size limit")

    try:
        doc = resp.json()
    except ValueError as exc:
        raise MCPOAuthDiscoveryError(f"AS metadata body is not valid JSON: {exc}") from exc

    if not isinstance(doc, dict):
        raise MCPOAuthDiscoveryError("AS metadata body is not a JSON object")

    authorization_endpoint = str(doc.get("authorization_endpoint", ""))
    token_endpoint = str(doc.get("token_endpoint", ""))
    registration_endpoint_raw = doc.get("registration_endpoint")
    registration_endpoint = (
        str(registration_endpoint_raw) if isinstance(registration_endpoint_raw, str) else None
    )
    jwks_uri_raw = doc.get("jwks_uri")
    jwks_uri = str(jwks_uri_raw) if isinstance(jwks_uri_raw, str) else None

    if not authorization_endpoint or not token_endpoint:
        raise MCPOAuthDiscoveryError("AS metadata missing required endpoints")

    # Same-origin / trusted-host validation on each discovered endpoint.
    allow_http = _is_localhost_issuer(issuer_parsed.hostname or "")
    for name, endpoint_url in (
        ("authorization_endpoint", authorization_endpoint),
        ("token_endpoint", token_endpoint),
    ):
        try:
            await validate_discovered_endpoint_async(
                endpoint_url,
                issuer_parsed,
                allow_http=allow_http,
                trusted_endpoint_hosts=trusted_hosts,
            )
        except OAuthSSRFError as exc:
            raise MCPOAuthDiscoveryError(f"AS {name} rejected (url={endpoint_url}): {exc}") from exc

    if registration_endpoint:
        try:
            await validate_discovered_endpoint_async(
                registration_endpoint,
                issuer_parsed,
                allow_http=allow_http,
                trusted_endpoint_hosts=trusted_hosts,
            )
        except OAuthSSRFError as exc:
            raise MCPOAuthDiscoveryError(
                f"AS registration_endpoint rejected (url={registration_endpoint}): {exc}"
            ) from exc

    code_methods_raw = doc.get("code_challenge_methods_supported", [])
    if not isinstance(code_methods_raw, list):
        code_methods_raw = []
    code_methods = tuple(str(m) for m in code_methods_raw)
    if "S256" not in code_methods:
        raise MCPOAuthDiscoveryError(
            "AS metadata does not advertise S256 PKCE — refusing to proceed"
        )

    auth_methods_raw = doc.get("token_endpoint_auth_methods_supported", [])
    if not isinstance(auth_methods_raw, list):
        auth_methods_raw = []
    auth_methods = tuple(str(m) for m in auth_methods_raw)

    return ASMetadata(
        issuer=str(doc.get("issuer", issuer)),
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        registration_endpoint=registration_endpoint,
        jwks_uri=jwks_uri,
        code_challenge_methods_supported=code_methods,
        token_endpoint_auth_methods_supported=auth_methods,
    )


def _is_localhost_issuer(hostname: str) -> bool:
    """Return True if *hostname* is a localhost form."""
    return hostname in ("localhost", "127.0.0.1", "::1") or hostname.endswith(".localhost")


async def discover_authorization_server(
    *,
    server_name: str,
    server_url: str,
    override_url: str | None,
    cached_issuer: str | None,
    http_client: httpx.AsyncClient,
    storage: StorageBackend,
    server_id: str,
    trusted_hosts: frozenset[str],
    metadata_cache: dict[str, tuple[ASMetadata, float]] | None = None,
) -> ASMetadata:
    """Resolve the issuer URL and load AS metadata for an MCP server.

    Resolution order:

    1. ``override_url`` (operator override) — used directly as the issuer.
    2. ``cached_issuer`` (from ``mcp_servers.oauth_as_issuer_cached``) —
       trusted across requests until invalidated by an admin edit.
    3. PRM discovery via the resource server URL.

    The fetched :class:`ASMetadata` is also memoised in *metadata_cache*
    keyed by issuer URL when the cache is supplied — this is the
    in-process layer that bypasses the network on subsequent calls.
    Persistent caching of the issuer (resolution step 2) lives on the
    ``mcp_servers`` row.
    """
    issuer: str
    if override_url:
        try:
            await validate_url_no_ssrf_async(override_url, allow_http=True)
        except OAuthSSRFError as exc:
            raise MCPOAuthDiscoveryError(f"override AS URL rejected: {exc}") from exc
        issuer = override_url
    elif cached_issuer:
        # Defense-in-depth: re-run SSRF validation on the cached value.
        # If the issuer's hostname has rebound to a private address since
        # we cached it (or the operator edited the row to point at a
        # private host), drop the cache and fall through to PRM.
        try:
            await validate_url_no_ssrf_async(cached_issuer, allow_http=True)
        except OAuthSSRFError as exc:
            log.warning(
                "mcp_server.oauth.cached_issuer_rejected",
                server_name=server_name,
                reason=sanitize_log_text(str(exc)),
            )
            if server_id:
                try:
                    await asyncio.to_thread(
                        storage.update_mcp_server,
                        server_id,
                        oauth_as_issuer_cached=None,
                    )
                except Exception:
                    log.debug(
                        "mcp_server.oauth.cached_issuer_clear_failed",
                        server_name=server_name,
                        exc_info=True,
                    )
            issuer = await _fetch_prm_issuer(server_url, http_client=http_client)
        else:
            issuer = cached_issuer
    else:
        issuer = await _fetch_prm_issuer(server_url, http_client=http_client)

    if metadata_cache is not None:
        cached = metadata_cache.get(issuer)
        if cached is not None:
            metadata, fetched_at = cached
            if time.monotonic() - fetched_at < MCP_OAUTH_DISCOVERY_CACHE_TTL_SECONDS:
                return metadata

    metadata = await _fetch_as_metadata(
        issuer, http_client=http_client, trusted_hosts=trusted_hosts
    )

    if metadata_cache is not None:
        metadata_cache[issuer] = (metadata, time.monotonic())

    # Persist the resolved issuer when the row had no cached value yet,
    # so subsequent calls skip PRM. We never overwrite an existing
    # cached_issuer (admin clears it via re-edit).
    if not cached_issuer and not override_url and server_id:
        try:
            await asyncio.to_thread(
                storage.update_mcp_server,
                server_id,
                oauth_as_issuer_cached=issuer,
            )
        except Exception:
            log.debug(
                "mcp_server.oauth.cache_issuer_failed",
                server_name=server_name,
                exc_info=True,
            )

    return metadata


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def generate_pkce_pair() -> tuple[str, str]:
    """Return a fresh ``(code_verifier, code_challenge_S256)`` pair.

    The verifier is 43 chars of urlsafe base64 (32 random bytes), the
    challenge is the SHA-256 digest of the verifier, base64-urlsafe
    encoded with no padding.
    """
    verifier = secrets.token_urlsafe(32)  # 43 ASCII chars
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ---------------------------------------------------------------------------
# Authorize URL construction
# ---------------------------------------------------------------------------


def build_authorize_url(
    *,
    as_metadata: ASMetadata,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scopes: str,
    audience: str,
    mcp_server_canonical_url: str,
) -> str:
    """Build the AS ``/authorize`` URL with PKCE + RFC 8707 ``resource``.

    Two audience-binding parameters are emitted because real-world ASes
    diverge on which one they honor:

    * ``resource`` is RFC 8707 — most spec-compliant ASes (Okta, Azure
      AD, Google's stricter modes) read this and populate ``aud`` from
      it.
    * ``audience`` is the operator-supplied override (typically the
      Auth0 API identifier — Auth0 does not honor ``resource`` and
      requires this instead). Empty when the operator left
      ``oauth_audience`` blank, in which case spec-compliant ASes still
      fall back on ``resource``.

    Sending both is harmless — ASes that only know one ignore the
    other. The downstream JWT validator
    (:func:`_validate_token_audience`) accepts ``aud`` matching either
    value.
    """
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if scopes:
        params["scope"] = scopes
    # RFC 8707 — resource indicator binds the token's audience.
    if mcp_server_canonical_url:
        params["resource"] = mcp_server_canonical_url
    if audience:
        params["audience"] = audience
    return as_metadata.authorization_endpoint + "?" + urllib.parse.urlencode(params)


# ---------------------------------------------------------------------------
# Pending state helpers
# ---------------------------------------------------------------------------


async def create_pending_state(
    *,
    storage: StorageBackend,
    user_id: str,
    server_name: str,
    code_verifier: str,
    return_url: str,
) -> str:
    """Insert a fresh pending row and return the new ``state`` value."""
    state = secrets.token_urlsafe(32)
    await asyncio.to_thread(
        storage.create_mcp_oauth_pending_state,
        state,
        user_id,
        server_name,
        code_verifier,
        return_url,
    )
    return state


async def pop_pending_state(*, storage: StorageBackend, state: str) -> dict[str, str] | None:
    """Atomically pop a pending state row, applying TTL.

    Returns the row dict on hit, ``None`` on miss/expiry.
    """
    row = await asyncio.to_thread(
        storage.pop_mcp_oauth_pending_state,
        state,
        MCP_OAUTH_STATE_TTL_SECONDS,
    )
    if row is None:
        return None
    return {str(k): str(v) for k, v in row.items()}


# Cap on individual standard-field length when echoing AS error text.
_AS_ERROR_FIELD_MAX = 80


def _format_as_error(resp: httpx.Response) -> str:
    """Build a safe, redacted summary of an AS error response.

    Pulls only the RFC 6749 standard error fields (``error``,
    ``error_description``, ``error_uri``) when the body is JSON, caps
    each field at :data:`_AS_ERROR_FIELD_MAX` characters, and runs the
    composite through ``redact_credentials`` so any echoed payload
    bytes (some ASes mirror the request body in their error response)
    can't drag plaintext tokens into operator logs.

    Falls back to a length-capped, sanitised ``resp.text`` when the
    body is not JSON or doesn't carry the standard keys.
    """
    from turnstone.core.output_guard import redact_credentials

    parts: list[str] = []
    try:
        doc = resp.json()
    except ValueError:
        doc = None
    if isinstance(doc, dict):
        for key in ("error", "error_description", "error_uri"):
            value = doc.get(key)
            if isinstance(value, str) and value:
                parts.append(f"{key}={value[:_AS_ERROR_FIELD_MAX]}")
    if not parts:
        # Fall back to the raw body, sanitised + length-capped.
        return sanitize_log_text(resp.text, 200)
    composite = " ".join(parts)
    return sanitize_log_text(redact_credentials(composite), 200)


# ---------------------------------------------------------------------------
# DCR (RFC 7591 minimal one-shot)
# ---------------------------------------------------------------------------


async def register_dynamic_client(
    *,
    as_metadata: ASMetadata,
    redirect_uri: str,
    http_client: httpx.AsyncClient,
    mcp_server_canonical_url: str,
    scopes: str = "",
) -> tuple[str, str | None]:
    """Register a public client at the AS ``registration_endpoint``.

    Returns ``(client_id, client_secret_or_None)``. Empty ``client_secret``
    means the AS issued a public client (preferred for the per-user flow).
    Currently registers once and persists the result; the
    re-register-on-401 path is wired by the upcoming dispatch integration
    that surfaces the 401 from the token endpoint.

    Raises :class:`MCPOAuthError` when the AS doesn't expose
    ``registration_endpoint`` or returns a non-2xx response.
    """
    if not as_metadata.registration_endpoint:
        raise MCPOAuthError("AS does not advertise registration_endpoint")

    body: dict[str, Any] = {
        "redirect_uris": [redirect_uri],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    if scopes:
        body["scope"] = scopes
    if mcp_server_canonical_url:
        # RFC 8707 audience hint — many AS impls echo this back into
        # token aud claims.
        body["resource"] = mcp_server_canonical_url

    try:
        resp = await http_client.post(
            as_metadata.registration_endpoint,
            json=body,
            timeout=_DEFAULT_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise MCPOAuthError(f"DCR request failed: {exc}") from exc

    if len(resp.content) > _MAX_TOKEN_BODY_BYTES:
        raise MCPOAuthError("DCR response body exceeds size limit")

    if resp.status_code not in (200, 201):
        raise MCPOAuthError(f"DCR returned HTTP {resp.status_code}: {_format_as_error(resp)}")

    try:
        doc = resp.json()
    except ValueError as exc:
        raise MCPOAuthError(f"DCR body is not valid JSON: {exc}") from exc

    if not isinstance(doc, dict):
        raise MCPOAuthError("DCR body is not a JSON object")

    client_id = doc.get("client_id")
    client_secret = doc.get("client_secret")
    if not isinstance(client_id, str) or not client_id:
        raise MCPOAuthError("DCR response missing client_id")
    if client_secret is not None and not isinstance(client_secret, str):
        raise MCPOAuthError("DCR response client_secret is not a string")
    return client_id, client_secret


# ---------------------------------------------------------------------------
# Token exchange (authorization-code) and refresh
# ---------------------------------------------------------------------------


async def exchange_code(
    *,
    as_metadata: ASMetadata,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    client_id: str,
    client_secret: str | None,
    mcp_server_canonical_url: str,
    http_client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Exchange an authorization code for an access (and optional refresh) token.

    Sends ``grant_type=authorization_code`` with PKCE verifier and the
    RFC 8707 ``resource`` parameter. ``client_secret=None`` selects the
    public-client (PKCE-only) auth method.

    Raises :class:`MCPOAuthExchangeError` on non-200 responses.
    """
    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
        "client_id": client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret
    if mcp_server_canonical_url:
        data["resource"] = mcp_server_canonical_url

    try:
        resp = await http_client.post(
            as_metadata.token_endpoint,
            data=data,
            timeout=_DEFAULT_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise MCPOAuthExchangeError(f"token exchange request failed: {exc}") from exc

    if len(resp.content) > _MAX_TOKEN_BODY_BYTES:
        raise MCPOAuthExchangeError("token endpoint response body exceeds size limit")

    if resp.status_code != 200:
        raise MCPOAuthExchangeError(
            f"token endpoint returned HTTP {resp.status_code}: {_format_as_error(resp)}"
        )

    try:
        doc = resp.json()
    except ValueError as exc:
        raise MCPOAuthExchangeError(f"token body is not valid JSON: {exc}") from exc

    if not isinstance(doc, dict):
        raise MCPOAuthExchangeError("token body is not a JSON object")
    return doc


async def refresh_token(
    *,
    as_metadata: ASMetadata,
    refresh_token_value: str,
    client_id: str,
    client_secret: str | None,
    mcp_server_canonical_url: str,
    scopes: str,
    http_client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Refresh an access token via the ``refresh_token`` grant.

    Raises :class:`MCPOAuthRefreshFailed` on non-200 — caller treats as
    re-consent trigger.
    """
    data: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token_value,
        "client_id": client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret
    if mcp_server_canonical_url:
        data["resource"] = mcp_server_canonical_url
    if scopes:
        data["scope"] = scopes

    try:
        resp = await http_client.post(
            as_metadata.token_endpoint,
            data=data,
            timeout=_DEFAULT_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise MCPOAuthRefreshFailed(f"refresh request failed: {exc}") from exc

    if len(resp.content) > _MAX_TOKEN_BODY_BYTES:
        raise MCPOAuthRefreshFailed("refresh endpoint response body exceeds size limit")

    if resp.status_code != 200:
        raise MCPOAuthRefreshFailed(
            f"refresh endpoint returned HTTP {resp.status_code}: {_format_as_error(resp)}"
        )

    try:
        doc = resp.json()
    except ValueError as exc:
        raise MCPOAuthRefreshFailed(f"refresh body is not valid JSON: {exc}") from exc

    if not isinstance(doc, dict):
        raise MCPOAuthRefreshFailed("refresh body is not a JSON object")
    return doc


# ---------------------------------------------------------------------------
# JWT audience validation
# ---------------------------------------------------------------------------


def _validate_token_audience(
    access_token: str,
    accepted_audiences: str | tuple[str, ...],
) -> bool:
    """Best-effort audience check on a JWT access token.

    For dot-separated tokens (JWT shape), decodes the payload and
    verifies the ``aud`` claim contains ANY value in *accepted_audiences*
    (or equals it, when ``aud`` is a single string). The JWT signature is
    NOT verified here — that's the resource server's job. We only inspect
    the claim.

    *accepted_audiences* is the set of values the AS may legitimately have
    populated into ``aud``. Operators set ``oauth_audience`` (e.g. an
    Auth0 API identifier) when their AS does not honor the canonical
    RFC 8707 ``resource`` parameter; we accept either form. Pass a single
    string when the resolved value is unambiguous (typical at all current
    call sites: ``oauth_audience or server_url``); the tuple form is a
    forward-looking handle.

    For opaque tokens (no dots / undecodable payload), returns True with
    a log entry per the Cursor / Claude Desktop contract.

    Returns True when the token passes (JWT with matching ``aud``, or
    opaque). Returns False on a JWT with a mismatched ``aud`` — caller
    treats this as an authentication failure.
    """
    accepted: tuple[str, ...]
    if isinstance(accepted_audiences, str):
        accepted = (accepted_audiences,) if accepted_audiences else ()
    else:
        accepted = tuple(a for a in accepted_audiences if a)

    if not access_token or "." not in access_token:
        log.info(
            "mcp_server.oauth.opaque_token_aud_unverified",
            audience=accepted,
        )
        return True

    payload_b64 = access_token.split(".")[1]
    # Restore padding for base64 decode.
    padding = "=" * (-len(payload_b64) % 4)
    try:
        payload_bytes = base64.urlsafe_b64decode((payload_b64 + padding).encode("ascii"))
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        log.info(
            "mcp_server.oauth.opaque_token_aud_unverified",
            audience=accepted,
            reason="payload_decode_failed",
        )
        return True

    if not isinstance(payload, dict):
        return True

    aud = payload.get("aud")
    if aud is None:
        log.warning(
            "mcp_server.oauth.jwt_no_aud_claim",
            audience=accepted,
        )
        return True

    if not accepted:
        # No audience to compare against — fall through to "trust" so we
        # don't accidentally reject every token when the operator has not
        # set ``oauth_audience`` and the canonical URL is empty.
        return True

    if isinstance(aud, str):
        return aud in accepted
    if isinstance(aud, list):
        return any(a in accepted for a in aud)
    return False


# ---------------------------------------------------------------------------
# get_user_access_token — main entry point for the upcoming dispatch
# integration (per-user MCP-server pool).
# ---------------------------------------------------------------------------


def _parse_iso_to_utc(value: str) -> datetime | None:
    """Parse an ISO8601 timestamp (no tz) as UTC; returns None on failure."""
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _refresh_lock_for(app_state: Any, user_id: str, server_name: str) -> asyncio.Lock:
    """Return the shared refresh lock for ``(user_id, server_name)``.

    Multi-node correctness note: this in-process ``asyncio.Lock`` is
    sufficient for SQLite single-node deployments; multi-node Postgres
    deployments need a postgres advisory lock keyed on
    ``hashtext('mcp_refresh:' || user_id || ':' || server_name)`` so
    concurrent nodes don't double-refresh.
    """
    locks = getattr(app_state, "mcp_oauth_refresh_locks", None)
    if locks is None:
        locks = {}
        app_state.mcp_oauth_refresh_locks = locks
    key = (user_id, server_name)
    lock = locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        locks[key] = lock
    return lock


def _drop_refresh_lock(app_state: Any, user_id: str, server_name: str) -> None:
    """Drop the cached refresh lock for ``(user_id, server_name)``.

    Called whenever a token row is deleted (revoked, refresh failure) so
    the in-process lock dict doesn't grow unboundedly across the lifetime
    of the process. Safe to call when no entry exists; a fresh lock will
    be lazily reinstalled on the next refresh attempt.
    """
    locks = getattr(app_state, "mcp_oauth_refresh_locks", None)
    if not isinstance(locks, dict):
        return
    locks.pop((user_id, server_name), None)


def _dcr_lock_for(app_state: Any, server_id: str) -> asyncio.Lock:
    """Return the per-server DCR registration lock.

    Two concurrent ``/start`` calls against a DCR-mode server with a
    NULL ``oauth_client_id`` would otherwise both register, with the
    second client_id overwriting the first. The user redirected with
    client-A then sees ``/callback`` look up client-B and fails the
    code exchange. Serialize via this lock so only one registration
    completes per process; subsequent waiters re-check the row and reuse
    the freshly-persisted client_id.
    """
    locks = getattr(app_state, "mcp_oauth_dcr_locks", None)
    if not isinstance(locks, dict):
        locks = {}
        app_state.mcp_oauth_dcr_locks = locks
    lock = locks.get(server_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[server_id] = lock
    return lock


async def get_user_access_token(*, app_state: Any, user_id: str, server_name: str) -> str | None:
    """Return a valid plaintext access token, refreshing if needed.

    Returns ``None`` when:
      - no token exists for ``(user_id, server_name)``
      - the row exists but cannot be refreshed (the row is then deleted
        and an audit event emitted; the caller should surface
        ``mcp_consent_required``)
      - the cipher / token store is not configured

    Multi-node correctness note: the refresh path is serialized via the
    in-process ``asyncio.Lock`` returned by :func:`_refresh_lock_for`. A
    multi-node deployment running against Postgres needs that same
    serialization across nodes via ``pg_advisory_lock`` keyed on
    ``hashtext('mcp_refresh:' || user_id || ':' || server_name)`` so
    concurrent nodes don't double-refresh.
    """
    token_store: MCPTokenStore | None = getattr(app_state, "mcp_token_store", None)
    if token_store is None:
        log.debug("mcp_server.oauth.token_store_unconfigured")
        return None

    try:
        plain = await asyncio.to_thread(token_store.get_user_token, user_id, server_name)
    except MCPTokenDecryptError:
        # No installed key can decrypt the row (e.g. operator dropped
        # the prior key after rotation). Falling through to None forces
        # the caller to re-consent rather than crashing the dispatch.
        log.warning(
            "mcp_server.oauth.token_decrypt_failed_falling_through",
            user_id=user_id,
            server_name=server_name,
            exc_info=True,
        )
        return None
    if plain is None:
        return None

    # Fast path — non-expired access token.
    expires_at = plain.get("expires_at")
    if not _token_needs_refresh(expires_at):
        return plain["access_token"]

    refresh_value = plain.get("refresh_token")
    if not refresh_value:
        # Expired access token, no refresh token — revoke + force re-consent.
        await asyncio.to_thread(token_store.delete_user_token, user_id, server_name)
        _drop_refresh_lock(app_state, user_id, server_name)
        await _audit_event(
            app_state,
            user_id=user_id,
            action="mcp_server.oauth.token_revoked",
            server_name=server_name,
            detail={"reason": "expired_no_refresh"},
        )
        return None

    storage = _get_storage(app_state)
    if storage is None:
        return None

    server_row = await asyncio.to_thread(storage.get_mcp_server_by_name, server_name)
    if server_row is None:
        return None
    server_id_for_audit = str(server_row.get("server_id") or "")

    lock = _refresh_lock_for(app_state, user_id, server_name)
    async with lock:
        # Re-read after lock — another caller may have refreshed already.
        try:
            plain2 = await asyncio.to_thread(token_store.get_user_token, user_id, server_name)
        except MCPTokenDecryptError:
            log.warning(
                "mcp_server.oauth.token_decrypt_failed_falling_through",
                user_id=user_id,
                server_name=server_name,
                exc_info=True,
            )
            _drop_refresh_lock(app_state, user_id, server_name)
            return None
        if plain2 is None:
            return None
        expires_at2 = plain2.get("expires_at")
        if not _token_needs_refresh(expires_at2):
            return plain2["access_token"]
        refresh_value2 = plain2.get("refresh_token")
        if not refresh_value2:
            await asyncio.to_thread(token_store.delete_user_token, user_id, server_name)
            await _audit_event(
                app_state,
                server_id=server_id_for_audit,
                user_id=user_id,
                action="mcp_server.oauth.token_revoked",
                server_name=server_name,
                detail={"reason": "expired_no_refresh"},
            )
            # Drop the lock entry AFTER we exit the ``async with`` block —
            # we still hold ``lock`` here, so dropping the dict reference
            # only matters for the next caller.
            _drop_refresh_lock(app_state, user_id, server_name)
            return None

        try:
            new_access, new_refresh, new_expires_at = await _refresh_and_persist(
                app_state=app_state,
                storage=storage,
                token_store=token_store,
                user_id=user_id,
                server_name=server_name,
                server_row=server_row,
                refresh_value=refresh_value2,
                existing_scopes=plain2.get("scopes") or "",
            )
        except MCPOAuthRefreshFailed:
            await asyncio.to_thread(token_store.delete_user_token, user_id, server_name)
            await _audit_event(
                app_state,
                server_id=server_id_for_audit,
                user_id=user_id,
                action="mcp_server.oauth.token_revoked",
                server_name=server_name,
                detail={"reason": "refresh_failed"},
            )
            _drop_refresh_lock(app_state, user_id, server_name)
            return None
        _ = new_refresh
        _ = new_expires_at
        return new_access


def _token_needs_refresh(expires_at: str | None) -> bool:
    """Return True when *expires_at* is missing, malformed, or within the skew window."""
    if not expires_at:
        # No expiry recorded — treat as fresh; AS issued an opaque
        # token without lifetime info.
        return False
    parsed = _parse_iso_to_utc(expires_at)
    if parsed is None:
        return True
    threshold = datetime.now(UTC) + timedelta(seconds=_ACCESS_TOKEN_REFRESH_SKEW_SECONDS)
    return parsed <= threshold


def _get_storage(app_state: Any) -> StorageBackend | None:
    """Pull the storage backend off ``app_state``; tests stash it as ``auth_storage``."""
    storage = getattr(app_state, "auth_storage", None)
    if storage is None:
        from turnstone.core.storage import get_storage

        try:
            storage = get_storage()
        except Exception:
            return None
    return storage


async def _refresh_and_persist(
    *,
    app_state: Any,
    storage: StorageBackend,
    token_store: MCPTokenStore,
    user_id: str,
    server_name: str,
    server_row: dict[str, Any],
    refresh_value: str,
    existing_scopes: str,
) -> tuple[str, str | None, str | None]:
    """Run the refresh-grant exchange and persist the new token row.

    Returns ``(access_token, refresh_token_or_none, expires_at_or_none)``.
    Raises :class:`MCPOAuthRefreshFailed` on failure.
    """
    http_client: httpx.AsyncClient | None = getattr(app_state, "mcp_oauth_http_client", None)
    if http_client is None:
        raise MCPOAuthRefreshFailed("mcp_oauth_http_client is not configured")

    server_id = str(server_row["server_id"])
    server_url = str(server_row.get("url") or "")
    override_url = server_row.get("oauth_authorization_server_url") or None
    cached_issuer = server_row.get("oauth_as_issuer_cached") or None
    client_id = server_row.get("oauth_client_id") or ""
    if not isinstance(client_id, str) or not client_id:
        raise MCPOAuthRefreshFailed("server has no oauth_client_id")
    metadata_cache = getattr(app_state, "mcp_oauth_metadata_cache", None)
    try:
        as_metadata = await discover_authorization_server(
            server_name=server_name,
            server_url=server_url,
            override_url=override_url if isinstance(override_url, str) else None,
            cached_issuer=cached_issuer if isinstance(cached_issuer, str) else None,
            http_client=http_client,
            storage=storage,
            server_id=server_id,
            trusted_hosts=frozenset(),
            metadata_cache=metadata_cache,
        )
    except MCPOAuthDiscoveryError as exc:
        raise MCPOAuthRefreshFailed(f"discovery failed during refresh: {exc}") from exc

    client_secret: str | None = None
    if token_store is not None:
        try:
            client_secret = await asyncio.to_thread(token_store.get_oauth_client_secret, server_id)
        except Exception:
            log.warning(
                "mcp_server.oauth.client_secret_decrypt_failed",
                server_name=server_name,
                exc_info=True,
            )
            client_secret = None

    # RFC 8707 ``resource=`` parameter is the canonical MCP server URL,
    # not the audience. Audience (Auth0-style ``audience=``) is a
    # separate concept — the authorize URL passes both, but the
    # token-grant uses ``resource=`` only.
    tokens = await refresh_token(
        as_metadata=as_metadata,
        refresh_token_value=refresh_value,
        client_id=client_id,
        client_secret=client_secret,
        mcp_server_canonical_url=server_url,
        scopes=existing_scopes,
        http_client=http_client,
    )

    new_access = tokens.get("access_token")
    if not isinstance(new_access, str) or not new_access:
        raise MCPOAuthRefreshFailed("refresh response missing access_token")

    # RFC 6749 §6 — the AS MAY omit ``refresh_token`` from the refresh
    # response. Most production ASes (Google, default Auth0, default
    # Okta) do not rotate the refresh token; clearing the column on
    # every refresh would force the user to re-consent every hour.
    # Preserve the prior refresh_value when the AS omits, replace only
    # when it issues a fresh one.
    raw_new_refresh = tokens.get("refresh_token")
    rotated_refresh: str | None
    if isinstance(raw_new_refresh, str) and raw_new_refresh:
        rotated_refresh = raw_new_refresh
        persisted_refresh: str | None = rotated_refresh
    else:
        rotated_refresh = None
        # Storage contract: ``refresh_token=None`` CLEARS the column. To
        # preserve, pass the existing value through.
        persisted_refresh = refresh_value
    new_expires_at = _expires_at_from_response(tokens)

    await asyncio.to_thread(
        token_store.update_user_token_after_refresh,
        user_id,
        server_name,
        access_token=new_access,
        refresh_token=persisted_refresh,
        expires_at=new_expires_at,
    )

    await _audit_event(
        app_state,
        server_id=server_id,
        user_id=user_id,
        action="mcp_server.oauth.token_refreshed",
        server_name=server_name,
        detail={"refresh_token_rotated": rotated_refresh is not None},
    )

    return new_access, rotated_refresh, new_expires_at


def _expires_at_from_response(tokens: dict[str, Any]) -> str | None:
    """Convert an AS ``expires_in`` to an ISO timestamp.

    Accepts int, float, or string-serialised numerics — some real ASes
    return ``"3600"`` (string), some return ``3600.0`` (float). Returns
    ``None`` when the field is missing, malformed, or non-positive.
    """
    expires_in = tokens.get("expires_in")
    seconds: int
    if isinstance(expires_in, bool):
        # ``bool`` is a subclass of ``int`` — reject explicitly so True
        # doesn't silently parse as 1 second.
        return None
    if isinstance(expires_in, int):
        seconds = expires_in
    elif isinstance(expires_in, float):
        seconds = int(expires_in)
    elif isinstance(expires_in, str):
        try:
            seconds = int(float(expires_in))
        except (TypeError, ValueError):
            return None
    else:
        return None
    if seconds <= 0:
        return None
    return (datetime.now(UTC) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S")


async def _audit_event(
    app_state: Any,
    *,
    user_id: str,
    action: str,
    server_name: str,
    detail: dict[str, Any],
    server_id: str | None = None,
) -> None:
    """Emit an audit event from the OAuth flow. Best-effort, never raises.

    The underlying :func:`record_audit` does a blocking SQL write, which
    we route through :func:`asyncio.to_thread` to keep the event loop
    responsive on hot paths (refresh, callback). Failures are swallowed
    at debug level — audit emission must never break the OAuth flow.

    ``resource_id`` on the audit row is the immutable ``server_id`` (PK
    UUID) so admin-driven server renames don't break event correlation;
    ``server_name`` is exposed in ``detail`` for cross-reference. When
    *server_id* is not supplied, falls back to a name-keyed lookup so
    callers that only have the server name can still emit; the operator
    rename window between rename and lookup is the only timing where the
    pre-rename name appears as the resource_id.
    """
    storage = _get_storage(app_state)
    if storage is None:
        return
    resolved_server_id: str = server_id or ""
    if not resolved_server_id and server_name and server_name != "(unknown)":
        try:
            row = await asyncio.to_thread(storage.get_mcp_server_by_name, server_name)
        except Exception:
            row = None
        if row is not None:
            resolved_server_id = str(row.get("server_id") or "")
    resource_id = resolved_server_id or server_name
    enriched_detail = dict(detail)
    enriched_detail.setdefault("server_name", server_name)
    try:
        await asyncio.to_thread(
            record_audit,
            storage,
            user_id,
            action,
            "mcp_server",
            resource_id,
            enriched_detail,
        )
    except Exception:
        log.debug("mcp_server.oauth.audit_emit_failed", action=action, exc_info=True)


# ---------------------------------------------------------------------------
# HTTP handlers — /api/mcp/oauth/start and /api/mcp/oauth/callback
# ---------------------------------------------------------------------------


def _no_token_store_response(action: str) -> Response:
    """503 response when ``mcp_token_store`` is unconfigured."""
    from starlette.responses import JSONResponse

    return JSONResponse(
        {
            "error": "MCP OAuth is not configured on this node.",
            "action": action,
            "hint": ("Configure [security] mcp_token_encryption_key in config.toml and restart."),
        },
        status_code=503,
    )


def _require_user_id(request: Request) -> str | None:
    """Pull the authenticated user id off the request. Returns None when absent."""
    auth = getattr(request.state, "auth_result", None)
    if auth is None:
        return None
    user_id = getattr(auth, "user_id", "")
    if not isinstance(user_id, str) or not user_id:
        return None
    return user_id


def _resolve_redirect_base(request: Request) -> str | None:
    """Pick the externally-visible base URL for the OAuth callback.

    Reuses the existing ``oidc_config.redirect_base`` since both OAuth
    flows live on the same host — keeping a single per-deployment
    setting avoids drift. Returns ``None`` when ``oidc_config`` is
    missing or ``redirect_base`` is unset; callers must respond 503 in
    that case.

    Building the redirect_uri from ``request.url.scheme/netloc`` (the
    Host header) is unsafe behind a permissive front proxy: an attacker
    can spoof ``Host`` and mint an authorize URL pointing at an
    attacker-controlled callback origin. The OIDC module pinned this in
    PR #476 — mirror that behavior here.
    """
    oidc_config = getattr(request.app.state, "oidc_config", None)
    redirect_base = getattr(oidc_config, "redirect_base", "") if oidc_config else ""
    if not redirect_base:
        return None
    return str(redirect_base).rstrip("/")


def _build_redirect_uri(redirect_base: str) -> str:
    """Compose the callback URL from a validated *redirect_base*.

    Caller must have resolved *redirect_base* via
    :func:`_resolve_redirect_base` and rejected (503) when ``None``.
    """
    return f"{redirect_base}/v1/api/mcp/oauth/callback"


async def _register_dynamic_client_if_needed(
    *,
    request: Request,
    storage: StorageBackend,
    token_store: MCPTokenStore,
    http_client: httpx.AsyncClient,
    as_metadata: ASMetadata,
    server_row: dict[str, Any],
    server_id: str,
    server_url: str,
    server_name: str,
    user_id: str,
    redirect_uri: str,
    registration_mode: str,
) -> tuple[str | None, MCPOAuthError | None]:
    """Lazy DCR — register a client only when one is missing.

    Returns ``(client_id, None)`` on success / no-op (the row already
    has a ``client_id``), or ``(None, exc)`` when DCR was needed but
    failed at the AS.

    Per-server lock + re-fetch resolves the concurrent-/start race that
    would otherwise overwrite a freshly-persisted client_id with a
    second registration's value, leaving the first user's authorize
    flow pointing at a code-mismatched client at /callback.
    """
    existing = server_row.get("oauth_client_id") or ""
    if existing:
        return existing, None
    if registration_mode != "dcr":
        return "", None

    lock = _dcr_lock_for(request.app.state, server_id)
    async with lock:
        # Re-fetch under the lock — another caller may have registered
        # while we were waiting. If the row now has a client_id, reuse
        # it without making a second registration call.
        latest_row = await asyncio.to_thread(storage.get_mcp_server_by_name, server_name)
        if latest_row is not None:
            persisted_client_id = latest_row.get("oauth_client_id") or ""
            if persisted_client_id:
                return persisted_client_id, None

        try:
            client_id, client_secret = await register_dynamic_client(
                as_metadata=as_metadata,
                redirect_uri=redirect_uri,
                http_client=http_client,
                mcp_server_canonical_url=server_url,
                scopes=server_row.get("oauth_scopes") or "",
            )
        except MCPOAuthError as exc:
            return None, exc

        try:
            await asyncio.to_thread(
                storage.update_mcp_server,
                server_id,
                oauth_client_id=client_id,
            )
        except Exception:
            log.warning(
                "mcp_server.oauth.persist_dcr_client_id_failed",
                server_name=server_name,
                exc_info=True,
            )
        secret_persisted = False
        if client_secret:
            try:
                await asyncio.to_thread(
                    token_store.set_oauth_client_secret,
                    server_id,
                    client_secret,
                )
                secret_persisted = True
            except Exception:
                log.warning(
                    "mcp_server.oauth.persist_dcr_client_secret_failed",
                    server_name=server_name,
                    exc_info=True,
                )
        await _audit_event(
            request.app.state,
            server_id=server_id,
            user_id=user_id,
            action="mcp_server.oauth.dcr_registered",
            server_name=server_name,
            detail={
                "client_id": client_id,
                "has_secret": secret_persisted,
            },
        )
        return client_id, None


def _validate_return_url(return_url: str, redirect_base: str) -> str | None:
    """Ensure ``return_url`` is same-origin with the configured *redirect_base*.

    Pinning to ``redirect_base`` (rather than ``request.url``) is the
    same defense as :func:`_resolve_redirect_base`: a permissive front
    proxy can let an attacker spoof ``Host`` and pass a same-origin
    check derived from the request, turning the callback into an open
    redirect. The OIDC module pinned this in PR #476.

    Backslashes and protocol-relative ``//`` prefixes are rejected up
    front: WHATWG-conformant browsers normalise ``\\`` to ``/``, so a
    path-only value like ``/\\evil.example/foo`` becomes the
    protocol-relative ``//evil.example/foo`` after the 302 — slipping
    past ``urlparse`` (which leaves the backslash inside ``path``) and
    re-introducing the open redirect.
    """
    if not return_url:
        return None
    if "\\" in return_url or return_url.startswith("//"):
        return None
    parsed = urllib.parse.urlparse(return_url)
    # Allow path-only return URLs.
    if not parsed.scheme and not parsed.netloc:
        if parsed.path.startswith("/"):
            return return_url
        return None
    base = urllib.parse.urlparse(redirect_base)
    if _origin_tuple(parsed) != _origin_tuple(base):
        return None
    return return_url


def _origin_tuple(parsed: urllib.parse.ParseResult) -> tuple[str, str, int | None]:
    """Canonicalise (scheme, host, port) for same-origin comparison.

    Lowercases scheme and hostname, and collapses the scheme's default
    port — so ``https://Host`` matches ``https://host:443`` instead of
    silently failing the same-origin check on a cosmetic difference.
    """
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    default_port = {"https": 443, "http": 80}.get(scheme)
    port = parsed.port if parsed.port is not None else default_port
    return (scheme, host, port)


def _apply_security_headers(response: Response) -> Response:
    """Stamp framing protection on OAuth responses.

    The /start and /callback handlers can return a 302 to an AS or a
    302 carrying a query-string error. Setting ``X-Frame-Options: DENY``
    means the redirected page can't be framed by an attacker site that
    proxies its own user through the OAuth flow. Idempotent — safe to
    call on JSON or redirect responses.
    """
    response.headers["X-Frame-Options"] = "DENY"
    return response


async def handle_mcp_oauth_authorize(request: Request) -> Response:
    """``GET /api/mcp/oauth/start?server={name}&return_url={...}``.

    Begins a per-(user, server) OAuth flow. The caller must be
    authenticated; the session's user_id is bound to the pending state.
    """
    return _apply_security_headers(await _handle_mcp_oauth_authorize_inner(request))


async def _handle_mcp_oauth_authorize_inner(request: Request) -> Response:
    from starlette.responses import JSONResponse, RedirectResponse

    token_store = getattr(request.app.state, "mcp_token_store", None)
    if token_store is None:
        return _no_token_store_response("start")

    user_id = _require_user_id(request)
    if user_id is None:
        return JSONResponse({"error": "Authentication required"}, status_code=401)

    redirect_base = _resolve_redirect_base(request)
    if redirect_base is None:
        return JSONResponse(
            {
                "error": "OAuth redirect base is not configured.",
                "hint": (
                    "Set [oidc] redirect_base in config.toml (or "
                    "TURNSTONE_OIDC_REDIRECT_BASE) to the service's "
                    "externally-visible URL. The MCP OAuth callback "
                    "URL is derived from this value."
                ),
            },
            status_code=503,
        )

    server_name = request.query_params.get("server", "").strip()
    if not server_name:
        return JSONResponse({"error": "Missing 'server' query parameter"}, status_code=400)

    return_url = _validate_return_url(
        request.query_params.get("return_url", "").strip(), redirect_base
    )
    if return_url is None:
        # Fall back to root — operators often hit /start without a hint.
        return_url = "/"

    storage = _get_storage(request.app.state)
    if storage is None:
        return JSONResponse({"error": "Storage unavailable"}, status_code=503)

    server_row = await asyncio.to_thread(storage.get_mcp_server_by_name, server_name)
    if server_row is None:
        return JSONResponse({"error": "Unknown MCP server"}, status_code=404)

    if server_row.get("auth_type") != "oauth_user":
        return JSONResponse(
            {"error": "Server is not configured for per-user OAuth"},
            status_code=400,
        )

    http_client: httpx.AsyncClient | None = getattr(
        request.app.state, "mcp_oauth_http_client", None
    )
    if http_client is None:
        return JSONResponse({"error": "OAuth HTTP client not initialised"}, status_code=503)

    metadata_cache = getattr(request.app.state, "mcp_oauth_metadata_cache", None)
    server_id = str(server_row["server_id"])
    server_url = str(server_row.get("url") or "")
    override_url = server_row.get("oauth_authorization_server_url") or None
    cached_issuer = server_row.get("oauth_as_issuer_cached") or None

    try:
        as_metadata = await discover_authorization_server(
            server_name=server_name,
            server_url=server_url,
            override_url=override_url if isinstance(override_url, str) else None,
            cached_issuer=cached_issuer if isinstance(cached_issuer, str) else None,
            http_client=http_client,
            storage=storage,
            server_id=server_id,
            trusted_hosts=frozenset(),
            metadata_cache=metadata_cache,
        )
    except MCPOAuthDiscoveryError as exc:
        log.warning("mcp_server.oauth.discovery_failed", server_name=server_name, exc_info=True)
        return JSONResponse(
            {"error": f"OAuth discovery failed: {sanitize_log_text(str(exc))}"},
            status_code=502,
        )

    redirect_uri = _build_redirect_uri(redirect_base)

    # Resolve / register client_id (DCR-mode lazy registration).
    registration_mode = server_row.get("oauth_registration_mode") or ""
    client_id, dcr_error = await _register_dynamic_client_if_needed(
        request=request,
        storage=storage,
        token_store=token_store,
        http_client=http_client,
        as_metadata=as_metadata,
        server_row=server_row,
        server_id=server_id,
        server_url=server_url,
        server_name=server_name,
        user_id=user_id,
        redirect_uri=redirect_uri,
        registration_mode=registration_mode,
    )
    if dcr_error is not None:
        log.warning("mcp_server.oauth.dcr_failed", server_name=server_name, exc_info=True)
        return JSONResponse(
            {"error": (f"Dynamic client registration failed: {sanitize_log_text(str(dcr_error))}")},
            status_code=502,
        )

    if not client_id:
        return JSONResponse(
            {"error": "Server has no oauth_client_id and DCR is not enabled"},
            status_code=400,
        )

    # Build PKCE pair and persist the pending state.
    code_verifier, code_challenge = generate_pkce_pair()
    state = await create_pending_state(
        storage=storage,
        user_id=user_id,
        server_name=server_name,
        code_verifier=code_verifier,
        return_url=return_url,
    )

    audience = server_row.get("oauth_audience") or server_url
    scopes = server_row.get("oauth_scopes") or ""
    url = build_authorize_url(
        as_metadata=as_metadata,
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        state=state,
        scopes=scopes,
        audience=audience,
        mcp_server_canonical_url=server_url,
    )

    await _audit_event(
        request.app.state,
        server_id=server_id,
        user_id=user_id,
        action="mcp_server.oauth.consent_started",
        server_name=server_name,
        detail={"issuer": as_metadata.issuer},
    )

    return RedirectResponse(url, status_code=302)


async def _consent_failed(
    request: Request,
    *,
    user_id: str,
    server_name: str,
    reason: str,
    redirect_query: str,
    detail_extra: dict[str, Any] | None = None,
    server_id: str | None = None,
) -> Response:
    """Emit a ``consent_failed`` audit event and 302 to the dashboard.

    Centralises the six-times-repeated "audit + redirect" idiom in
    :func:`handle_mcp_oauth_callback`. *redirect_query* is the
    URL-encoded query suffix (without the leading ``?``); *reason* is
    a stable machine-readable identifier persisted into the audit
    detail.
    """
    from starlette.responses import RedirectResponse

    detail: dict[str, Any] = {"reason": reason}
    if detail_extra:
        detail.update(detail_extra)
    await _audit_event(
        request.app.state,
        server_id=server_id,
        user_id=user_id,
        action="mcp_server.oauth.consent_failed",
        server_name=server_name,
        detail=detail,
    )
    return RedirectResponse(f"/?{redirect_query}", status_code=302)


async def handle_mcp_oauth_callback(request: Request) -> Response:
    """``GET /api/mcp/oauth/callback?code=...&state=...``.

    AS-redirected callback. Pops the pending state, exchanges the code,
    audience-validates the access token (logs + trusts opaque), persists,
    and redirects to ``return_url``.
    """
    return _apply_security_headers(await _handle_mcp_oauth_callback_inner(request))


async def _handle_mcp_oauth_callback_inner(request: Request) -> Response:
    from starlette.responses import JSONResponse, RedirectResponse

    token_store = getattr(request.app.state, "mcp_token_store", None)
    if token_store is None:
        return _no_token_store_response("callback")

    user_id = _require_user_id(request)
    if user_id is None:
        return JSONResponse({"error": "Authentication required"}, status_code=401)

    redirect_base = _resolve_redirect_base(request)
    if redirect_base is None:
        return JSONResponse(
            {
                "error": "OAuth redirect base is not configured.",
                "hint": (
                    "Set [oidc] redirect_base in config.toml (or "
                    "TURNSTONE_OIDC_REDIRECT_BASE) to the service's "
                    "externally-visible URL."
                ),
            },
            status_code=503,
        )

    storage = _get_storage(request.app.state)
    if storage is None:
        return JSONResponse({"error": "Storage unavailable"}, status_code=503)

    # Lazy cleanup of expired pending rows.
    last_cleanup = getattr(request.app.state, "mcp_oauth_last_cleanup_monotonic", 0.0)
    now_mono = time.monotonic()
    if now_mono - last_cleanup > _PENDING_CLEANUP_INTERVAL_S:
        request.app.state.mcp_oauth_last_cleanup_monotonic = now_mono
        try:
            await asyncio.to_thread(
                storage.cleanup_expired_mcp_oauth_pending_states,
                MCP_OAUTH_STATE_TTL_SECONDS,
            )
        except Exception:
            log.debug("mcp_server.oauth.cleanup_failed", exc_info=True)

    state = request.query_params.get("state", "")

    error = request.query_params.get("error", "")
    if error:
        desc = request.query_params.get("error_description", error)
        # Pop the pending row so the state can't be replayed within the
        # TTL even though the AS already declared the flow failed.
        if state:
            try:
                await pop_pending_state(storage=storage, state=state)
            except Exception:
                log.debug("mcp_server.oauth.callback_error_pop_failed", exc_info=True)
        await _audit_event(
            request.app.state,
            user_id=user_id,
            action="mcp_server.oauth.consent_failed",
            server_name="(unknown)",
            detail={"error": sanitize_log_text(error), "description": sanitize_log_text(desc)},
        )
        return RedirectResponse(
            f"/?mcp_oauth_error={urllib.parse.quote(sanitize_log_text(desc))}",
            status_code=302,
        )

    pending = await pop_pending_state(storage=storage, state=state)
    if pending is None:
        return RedirectResponse("/?mcp_oauth_error=session+expired", status_code=302)

    if pending["user_id"] != user_id:
        # Cross-user state stuffing — fail loudly.
        return await _consent_failed(
            request,
            user_id=user_id,
            server_name=pending.get("server_name", ""),
            reason="user_id_mismatch",
            redirect_query="mcp_oauth_error=user+mismatch",
        )

    server_name = pending["server_name"]
    server_row = await asyncio.to_thread(storage.get_mcp_server_by_name, server_name)
    if server_row is None:
        return RedirectResponse("/?mcp_oauth_error=server+missing", status_code=302)

    http_client: httpx.AsyncClient | None = getattr(
        request.app.state, "mcp_oauth_http_client", None
    )
    if http_client is None:
        return JSONResponse({"error": "OAuth HTTP client not initialised"}, status_code=503)

    server_id = str(server_row["server_id"])
    server_url = str(server_row.get("url") or "")
    # Resolve once — used for ``resource=`` (RFC 8707), ``audience=``
    # (Auth0 form), JWT aud validation, and persistence on the user
    # token row.
    audience = server_row.get("oauth_audience") or server_url
    scopes = server_row.get("oauth_scopes") or ""
    client_id = server_row.get("oauth_client_id") or ""
    if not client_id:
        return RedirectResponse("/?mcp_oauth_error=client_id+missing", status_code=302)

    metadata_cache = getattr(request.app.state, "mcp_oauth_metadata_cache", None)
    override_url = server_row.get("oauth_authorization_server_url") or None
    cached_issuer = server_row.get("oauth_as_issuer_cached") or None

    try:
        as_metadata = await discover_authorization_server(
            server_name=server_name,
            server_url=server_url,
            override_url=override_url if isinstance(override_url, str) else None,
            cached_issuer=cached_issuer if isinstance(cached_issuer, str) else None,
            http_client=http_client,
            storage=storage,
            server_id=server_id,
            trusted_hosts=frozenset(),
            metadata_cache=metadata_cache,
        )
    except MCPOAuthDiscoveryError as exc:
        log.warning(
            "mcp_server.oauth.callback_discovery_failed",
            server_name=server_name,
            exc_info=True,
        )
        return await _consent_failed(
            request,
            server_id=server_id,
            user_id=user_id,
            server_name=server_name,
            reason="discovery_failed",
            redirect_query=(f"mcp_oauth_error={urllib.parse.quote(sanitize_log_text(str(exc)))}"),
        )

    redirect_uri = _build_redirect_uri(redirect_base)

    client_secret: str | None
    try:
        client_secret = await asyncio.to_thread(token_store.get_oauth_client_secret, server_id)
    except Exception:
        log.warning(
            "mcp_server.oauth.client_secret_decrypt_failed",
            server_name=server_name,
            exc_info=True,
        )
        client_secret = None

    code = request.query_params.get("code", "")
    try:
        tokens = await exchange_code(
            as_metadata=as_metadata,
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=pending["code_verifier"],
            client_id=client_id,
            client_secret=client_secret,
            mcp_server_canonical_url=server_url,
            http_client=http_client,
        )
    except MCPOAuthExchangeError as exc:
        log.warning("mcp_server.oauth.exchange_failed", server_name=server_name, exc_info=True)
        return await _consent_failed(
            request,
            server_id=server_id,
            user_id=user_id,
            server_name=server_name,
            reason="exchange_failed",
            redirect_query=(f"mcp_oauth_error={urllib.parse.quote(sanitize_log_text(str(exc)))}"),
        )

    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return await _consent_failed(
            request,
            server_id=server_id,
            user_id=user_id,
            server_name=server_name,
            reason="missing_access_token",
            redirect_query="mcp_oauth_error=missing+access+token",
        )

    # Accept ``aud`` matching either the canonical resource URL (RFC
    # 8707 form) OR the operator-set ``oauth_audience`` (Auth0 form).
    accepted_audiences = tuple({a for a in (server_url, audience) if a})
    if not _validate_token_audience(access_token, accepted_audiences):
        log.warning(
            "mcp_server.oauth.jwt_audience_mismatch",
            server_name=server_name,
            audience=accepted_audiences,
        )
        return await _consent_failed(
            request,
            server_id=server_id,
            user_id=user_id,
            server_name=server_name,
            reason="audience_mismatch",
            redirect_query="mcp_oauth_error=audience+mismatch",
        )

    new_refresh = tokens.get("refresh_token")
    if new_refresh is not None and not isinstance(new_refresh, str):
        new_refresh = None
    expires_at = _expires_at_from_response(tokens)
    issued_scopes_raw = tokens.get("scope")
    issued_scopes = (
        issued_scopes_raw if isinstance(issued_scopes_raw, str) and issued_scopes_raw else scopes
    )

    # Replace existing row (idempotent re-consent).
    try:
        await asyncio.to_thread(token_store.delete_user_token, user_id, server_name)
        await asyncio.to_thread(
            token_store.create_user_token,
            user_id,
            server_name,
            access_token=access_token,
            refresh_token=new_refresh,
            expires_at=expires_at,
            scopes=issued_scopes,
            as_issuer=as_metadata.issuer,
            audience=audience,
        )
    except Exception:
        log.exception("mcp_server.oauth.persist_failed", server_name=server_name)
        return RedirectResponse("/?mcp_oauth_error=storage+failure", status_code=302)

    await _audit_event(
        request.app.state,
        server_id=server_id,
        user_id=user_id,
        action="mcp_server.oauth.consent_completed",
        server_name=server_name,
        detail={
            "has_refresh_token": new_refresh is not None,
            "expires_at": expires_at,
        },
    )

    return RedirectResponse(pending["return_url"] or "/", status_code=302)


# ---------------------------------------------------------------------------
# Lifespan integration
# ---------------------------------------------------------------------------


async def initialize_mcp_oauth_state(app_state: Any) -> None:
    """Install the long-lived HTTP client + per-(user, server) lock + metadata cache.

    Mirrors :func:`turnstone.core.oidc.initialize_oidc_state` so the
    server / console lifespans can register/teardown symmetrically. Always
    safe to call — installs sentinel state even when no MCP OAuth row
    exists (the route handlers fast-path to 503 when ``mcp_token_store``
    is None).
    """
    app_state.mcp_oauth_http_client = httpx.AsyncClient(timeout=_DEFAULT_HTTP_TIMEOUT)
    app_state.mcp_oauth_refresh_locks = {}
    app_state.mcp_oauth_dcr_locks = {}
    app_state.mcp_oauth_metadata_cache = {}
    app_state.mcp_oauth_last_cleanup_monotonic = 0.0


async def close_mcp_oauth_state(app_state: Any) -> None:
    """Close the long-lived HTTP client. Safe to call when never initialised."""
    client = getattr(app_state, "mcp_oauth_http_client", None)
    if client is not None:
        try:
            await client.aclose()
        except Exception:
            log.debug("mcp_server.oauth.http_client_close_failed", exc_info=True)
        app_state.mcp_oauth_http_client = None
    if hasattr(app_state, "mcp_oauth_refresh_locks"):
        app_state.mcp_oauth_refresh_locks = {}
    if hasattr(app_state, "mcp_oauth_dcr_locks"):
        app_state.mcp_oauth_dcr_locks = {}
    if hasattr(app_state, "mcp_oauth_metadata_cache"):
        app_state.mcp_oauth_metadata_cache = {}


__all__ = [
    "ASMetadata",
    "MCPOAuthDiscoveryError",
    "MCPOAuthError",
    "MCPOAuthExchangeError",
    "MCPOAuthRefreshFailed",
    "MCP_OAUTH_DISCOVERY_CACHE_TTL_SECONDS",
    "MCP_OAUTH_STATE_TTL_SECONDS",
    "build_authorize_url",
    "close_mcp_oauth_state",
    "create_pending_state",
    "discover_authorization_server",
    "generate_pkce_pair",
    "get_user_access_token",
    "handle_mcp_oauth_authorize",
    "handle_mcp_oauth_callback",
    "initialize_mcp_oauth_state",
    "pop_pending_state",
]
