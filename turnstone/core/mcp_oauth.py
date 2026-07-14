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

Multi-node refresh contention: :func:`get_user_access_token` and the
classified variant :func:`get_user_access_token_classified` serialize
the read-current → exchange-at-AS → write-new sequence at two layers
— an outer Postgres advisory lock (cluster-wide) wraps an inner
``asyncio.Lock`` (intra-process). The advisory lock is no-op on SQLite
single-node deployments via :meth:`StorageBackend.acquire_advisory_lock_sync`.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import contextlib
import enum
import hashlib
import json
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

import httpx

from turnstone.core.audit import record_audit
from turnstone.core.log import get_logger
from turnstone.core.mcp_crypto import (
    MCPTokenDecryptError,
    OIDCCredentialPlain,
    is_user_scoped_auth,
)
from turnstone.core.mcp_http_parsers import (
    MAX_INSUFFICIENT_SCOPE_REPORTED,
    is_valid_scope_token,
    parse_www_authenticate_bearer,
)
from turnstone.core.oauth_ssrf import (
    OAuthSSRFError,
    sanitize_log_text,
    validate_discovered_endpoint_async,
    validate_url_no_ssrf_async,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response

    from turnstone.core.mcp_crypto import MCPTokenStore, MCPUserTokenPlain
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
# Fallback lifetime for a minted obo cache row when the IdP omits the
# RFC 8693-optional ``expires_in``. Unlike oauth_user tokens (a missing expiry
# means an opaque token cached until a 401), an obo minted access token is
# always short-lived, so a NULL expiry must NOT read as "never expires" in the
# freshness gate. Conservative so the row re-mints soon rather than being served
# long past its real lifetime (which would also defeat audience/scope narrowing
# that relies on TTL turnover).
_OBO_DEFAULT_TTL_SECONDS = 300

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


class _RefreshFailureClass(enum.Enum):
    """How the caller should react to a failed refresh-token grant.

    - ``PERMANENT`` — the AS rejected the grant as dead (``invalid_grant`` /
      ``invalid_scope`` / an OIDC interaction-required code): revoke the stored
      token and trigger re-consent.
    - ``TRANSIENT`` — an infrastructure or operator-fixable blip (network, 5xx,
      429, ``invalid_client``, malformed body): keep the token and surface a
      retryable error so a blip can never revoke a user's consent. Never
      escalates, so even a sustained AS outage can't strand consent.
    - ``AMBIGUOUS`` — a 400/401 token-endpoint rejection we couldn't pin to a
      standard code: keep the token, but the caller counts consecutive
      occurrences and escalates to re-consent past a threshold — so a dead grant
      delivered in a non-standard shape can't strand the user forever, while a
      one-off oddity still can't revoke consent.
    """

    PERMANENT = "permanent"
    TRANSIENT = "transient"
    AMBIGUOUS = "ambiguous"


class MCPOAuthRefreshFailed(MCPOAuthError):  # noqa: N818 — name reflects domain semantics
    """Refresh-token grant failed; ``failure_class`` tells the caller how to react.

    See :class:`_RefreshFailureClass` for the three handling classes. Defaults to
    ``TRANSIENT`` — the safe direction, since the caller then keeps the token
    rather than revoking a user's consent on an unclassified failure.
    """

    def __init__(
        self,
        message: str = "",
        *,
        failure_class: _RefreshFailureClass = _RefreshFailureClass.TRANSIENT,
    ) -> None:
        super().__init__(message)
        self.failure_class = failure_class


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
    revocation_endpoint: str | None
    jwks_uri: str | None
    code_challenge_methods_supported: tuple[str, ...]
    token_endpoint_auth_methods_supported: tuple[str, ...]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _parse_prm_url_from_www_authenticate(header: str) -> str | None:
    """Extract ``resource_metadata`` URL from a ``WWW-Authenticate: Bearer`` header.

    Returns the URL string or ``None`` when the header lacks the param,
    is malformed, or terminates the quoted-string prematurely. Delegates
    to :func:`parse_www_authenticate_bearer` so quoted-string handling
    (RFC 7230 §3.2.6 backslash escapes) and the multi-challenge
    defence-in-depth guard live in one place.
    """
    if not header:
        return None
    params = parse_www_authenticate_bearer(header)
    return params.get("resource_metadata") or None


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

    # MCP auth permits OpenID Connect discovery as a fallback to RFC 8414.
    # Major IdPs (notably Microsoft Entra) serve ONLY the OIDC document
    # (.well-known/openid-configuration) and 404 the oauth-authorization-server
    # path — refusing to support them would lock out the most common
    # enterprise AS. Try RFC 8414 first (preferred), then OIDC.
    base = issuer.rstrip("/")
    # (profile, url): ``profile`` records WHICH discovery document each candidate
    # is so the S256 PKCE check below can apply the correct per-document
    # defaulting rule — an absent ``code_challenge_methods_supported`` is only
    # treated as "S256 supported" for the OIDC document (see below).
    metadata_candidates = (
        ("rfc8414", base + "/.well-known/oauth-authorization-server"),
        ("oidc", base + "/.well-known/openid-configuration"),
    )
    resp = None
    winning_profile: str | None = None
    last_status: int | None = None
    for profile, metadata_url in metadata_candidates:
        try:
            r = await http_client.get(metadata_url, timeout=_DEFAULT_HTTP_TIMEOUT)
        except httpx.HTTPError as exc:
            raise MCPOAuthDiscoveryError(f"AS metadata fetch failed: {exc}") from exc
        if r.status_code == 200:
            resp = r
            winning_profile = profile
            break
        last_status = r.status_code
    if resp is None:
        raise MCPOAuthDiscoveryError(f"AS metadata returned HTTP {last_status}")
    # Which discovery profile answered (rfc8414 vs oidc) is the load-bearing
    # detail when debugging an enterprise AS (e.g. Entra serves only OIDC).
    log.debug("mcp_server.oauth.as_metadata_discovered", profile=winning_profile)

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
    revocation_endpoint_raw = doc.get("revocation_endpoint")
    revocation_endpoint = (
        str(revocation_endpoint_raw) if isinstance(revocation_endpoint_raw, str) else None
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

    for opt_name, opt_url in (
        ("registration_endpoint", registration_endpoint),
        ("revocation_endpoint", revocation_endpoint),
    ):
        if not opt_url:
            continue
        try:
            await validate_discovered_endpoint_async(
                opt_url,
                issuer_parsed,
                allow_http=allow_http,
                trusted_endpoint_hosts=trusted_hosts,
            )
        except OAuthSSRFError as exc:
            raise MCPOAuthDiscoveryError(f"AS {opt_name} rejected (url={opt_url}): {exc}") from exc

    code_methods_raw = doc.get("code_challenge_methods_supported", [])
    if not isinstance(code_methods_raw, list):
        code_methods_raw = []
    code_methods = tuple(str(m) for m in code_methods_raw)
    if not code_methods and winning_profile == "oidc":
        # OIDC document (openid-configuration) only: it does not require
        # advertising code_challenge_methods_supported, and some IdPs (Entra)
        # omit it despite fully supporting S256, so treat absence as "S256
        # supported" (mandated by OAuth 2.1 / MCP auth) rather than locking the
        # AS out.
        #
        # For the RFC 8414 oauth-authorization-server document we deliberately
        # do NOT assume: an omitted field there is taken at face value as "no
        # PKCE advertised", so code_methods stays empty and the check below
        # fails closed. The client always sends code_challenge_method=S256, so
        # this guard is the ONLY pre-flight that the AS actually enforces PKCE;
        # assuming S256 on a document that omitted it would silently admit a
        # non-enforcing AS and forfeit code-interception protection on the
        # on-behalf-of bearer. A NON-empty list missing S256 is always a hard
        # refusal, for both documents.
        log.info("mcp_server.oauth.s256_assumed_absent_advertisement")
        code_methods = ("S256",)
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
        revocation_endpoint=revocation_endpoint,
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


def _as_error_code(resp: httpx.Response) -> str | None:
    """Return the RFC 6749 ``error`` code from a token-endpoint JSON error body.

    Feeds :func:`_classify_refresh_failure`, which maps the code to a handling
    class. Returns ``None`` when the body isn't JSON or carries no ``error``
    field — an absent code is treated as an *ambiguous* rejection, not a
    permanent one, so a non-standard error shape can't revoke consent outright.
    """
    try:
        doc = resp.json()
    except ValueError:
        return None
    if isinstance(doc, dict):
        code = doc.get("error")
        if isinstance(code, str) and code:
            return code
    return None


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


# RFC 6749 / OIDC token-endpoint error codes that mean the grant is genuinely
# dead and the user must re-consent — the only PERMANENT (revoke) signals.
_PERMANENT_AS_ERRORS = frozenset(
    {
        "invalid_grant",  # refresh token expired/revoked (RFC 6749 §5.2)
        "invalid_scope",  # requested scope no longer grantable → re-consent
    }
)
# OIDC interaction-required family: the AS needs the user back in the loop
# (consent / login / account selection) — also a re-consent (PERMANENT) signal.
_INTERACTION_AS_ERRORS = frozenset(
    {
        "interaction_required",
        "login_required",
        "consent_required",
        "account_selection_required",
    }
)
# Operator-fixable or RFC-transient codes: keep the token and never escalate —
# re-consenting the user won't fix a bad client_secret, and
# ``temporarily_unavailable`` is explicitly retryable.
_TRANSIENT_AS_ERRORS = frozenset(
    {
        "invalid_client",
        "invalid_request",
        "unauthorized_client",
        "unsupported_grant_type",
        "temporarily_unavailable",
    }
)


def _classify_refresh_failure(resp: httpx.Response) -> _RefreshFailureClass:
    """Classify a non-200 refresh response into a handling class.

    Conservative by construction — the only path to a PERMANENT
    (consent-revoking) outcome is an explicit dead-grant / re-consent error code
    at a client-error status. Everything infrastructural (5xx, 429) or
    operator-fixable (``invalid_client`` …) is TRANSIENT and never escalates, so
    a sustained AS outage can't revoke consent. A 400/401 carrying an error code
    we don't recognise (or none at all) is AMBIGUOUS: the caller keeps the token
    but escalates to re-consent after an uninterrupted run, so a dead grant in a
    non-standard shape can't strand the user while a one-off can't revoke.
    """
    status = resp.status_code
    code = _as_error_code(resp)
    if status in (400, 401, 403) and (
        code in _PERMANENT_AS_ERRORS or code in _INTERACTION_AS_ERRORS
    ):
        return _RefreshFailureClass.PERMANENT
    if status in (400, 401) and code not in _TRANSIENT_AS_ERRORS:
        return _RefreshFailureClass.AMBIGUOUS
    return _RefreshFailureClass.TRANSIENT


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


async def _hardened_token_post(
    *,
    token_endpoint: str,
    data: dict[str, str],
    http_client: httpx.AsyncClient,
    request_label: str,
    endpoint_label: str,
    classify_oversized_by_status: bool = False,
) -> dict[str, Any]:
    """POST one token-grant request with the shared hardening skeleton.

    Single implementation of the POST → body-size cap →
    :func:`_classify_refresh_failure` → JSON-object-validation chain used by
    both the oauth_user refresh and the OBO mint legs, so the two grant
    paths cannot drift. Raises :class:`MCPOAuthRefreshFailed` on any
    failure; a non-200 carries the conservative classification — an explicit
    dead-grant / re-consent code revokes consent; infra (5xx/429) and
    operator-fixable codes keep the token; an unrecognised 400/401 is
    ambiguous and the caller escalates only after a sustained run.

    The two labels preserve each caller's historical error text verbatim
    (``refresh request failed`` vs ``refresh endpoint returned HTTP …``);
    the OBO wrapper passes one string for both. Callers always supply
    *http_client* — the oauth_user path its long-lived client, the OBO path a
    single per-mint client opened in :func:`get_obo_access_token_classified` so
    the rfc8693 legs reuse one connection.

    ``classify_oversized_by_status`` controls how an OVER-sized error body is
    classified. The oauth_user refresh path keeps the default (``False`` →
    TRANSIENT), byte-identical to the pre-refactor behavior, so a large upstream
    error can never escalate a pre-existing consent to re-consent. The OBO legs
    pass ``True`` so an over-sized client-error body is AMBIGUOUS (it can't read
    the body to pin PERMANENT without defeating the guard) and still escalates
    to the honest re-login/admin remedy instead of looping "please retry".
    """
    try:
        resp = await http_client.post(token_endpoint, data=data, timeout=_DEFAULT_HTTP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise MCPOAuthRefreshFailed(f"{request_label} request failed: {exc}") from exc

    if len(resp.content) > _MAX_TOKEN_BODY_BYTES:
        oversized_class = (
            _RefreshFailureClass.AMBIGUOUS
            if classify_oversized_by_status and resp.status_code in (400, 401, 403)
            else _RefreshFailureClass.TRANSIENT
        )
        raise MCPOAuthRefreshFailed(
            f"{endpoint_label} response body exceeds size limit",
            failure_class=oversized_class,
        )

    if resp.status_code != 200:
        raise MCPOAuthRefreshFailed(
            f"{endpoint_label} returned HTTP {resp.status_code}: {_format_as_error(resp)}",
            failure_class=_classify_refresh_failure(resp),
        )

    try:
        doc = resp.json()
    except ValueError as exc:
        raise MCPOAuthRefreshFailed(f"{request_label} body is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise MCPOAuthRefreshFailed(f"{request_label} body is not a JSON object")
    typed: dict[str, Any] = doc
    return typed


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

    return await _hardened_token_post(
        token_endpoint=as_metadata.token_endpoint,
        data=data,
        http_client=http_client,
        request_label="refresh",
        endpoint_label="refresh endpoint",
    )


async def revoke_token_at_as(
    *,
    as_metadata: ASMetadata,
    http_client: httpx.AsyncClient,
    refresh_token: str,
    client_id: str,
    client_secret: str | None,
    timeout_seconds: float = _DEFAULT_HTTP_TIMEOUT,
) -> None:
    """Best-effort RFC 7009 token revocation.

    Posts ``token=<refresh_token>&token_type_hint=refresh_token`` plus
    client credentials to ``as_metadata.revocation_endpoint``. This is
    fire-and-don't-care — the helper logs the outcome and never raises,
    so callers can fold it into a teardown path without try/except.

    When the AS metadata document carries no ``revocation_endpoint``
    (RFC 8414 makes it optional), the helper logs and returns. The
    timeout is enforced via ``asyncio.timeout`` (NOT ``asyncio.wait_for``)
    to avoid the Python 3.11 anyio cancel-scope hazard on cleanup paths.
    """
    if as_metadata.revocation_endpoint is None:
        log.info(
            "mcp_server.oauth.revocation_unsupported",
            as_issuer=as_metadata.issuer,
        )
        return

    data: dict[str, str] = {
        "token": refresh_token,
        "token_type_hint": "refresh_token",
        "client_id": client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret

    try:
        async with asyncio.timeout(timeout_seconds):
            resp = await http_client.post(
                as_metadata.revocation_endpoint,
                data=data,
            )
    except Exception as exc:
        # NOTE: never use ``exc_info=True`` here — chained ``__context__``
        # may carry an ``httpx.Request`` whose ``Authorization`` header
        # holds a bearer. Structured fields with ``type(exc).__name__``
        # only.
        log.info(
            "mcp_server.oauth.revocation_failed",
            as_issuer=as_metadata.issuer,
            error=type(exc).__name__,
        )
        return

    if 200 <= resp.status_code < 300:
        log.info(
            "mcp_server.oauth.revocation_succeeded",
            as_issuer=as_metadata.issuer,
            status=resp.status_code,
        )
        return

    log.info(
        "mcp_server.oauth.revocation_failed",
        as_issuer=as_metadata.issuer,
        status=resp.status_code,
    )


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

    This in-process ``asyncio.Lock`` provides intra-process
    serialization. Cluster-wide serialization is layered on top via the
    Postgres advisory lock acquired by
    :func:`_acquire_pg_refresh_lock`; both are taken together by
    :func:`get_user_access_token` so concurrent nodes don't
    double-refresh.
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


def _refresh_advisory_key(user_id: str, server_name: str) -> str:
    """Return the advisory-lock key for ``(user_id, server_name)``.

    Hashed via ``pg_advisory_xact_lock(hashtext(...))`` at the storage
    layer; the textual form is what the storage helper hashes, so the
    callers don't need to know about the digest.
    """
    return f"mcp_refresh:{user_id}:{server_name}"


async def _acquire_pg_refresh_lock(
    storage: StorageBackend, user_id: str, server_name: str
) -> contextlib.AbstractAsyncContextManager[None]:
    """Async context manager that serializes the refresh body across nodes.

    On Postgres, holds ``pg_advisory_xact_lock(hashtext(...))`` for the
    duration of the body via :meth:`StorageBackend.acquire_advisory_lock_sync`.
    On SQLite, the storage helper returns ``nullcontext`` and this is
    effectively a no-op — single-node deployments rely on the
    in-process ``asyncio.Lock`` for serialization.

    The blocking SQLAlchemy hops inside the storage context manager are
    routed through ``asyncio.to_thread`` so the mcp-loop stays
    responsive.
    """
    return _PgRefreshLock(storage, _refresh_advisory_key(user_id, server_name))


# Strong refs to in-flight drain tasks. asyncio holds tasks via a WeakSet,
# so a fire-and-forget ``loop.create_task(...)`` whose handle isn't stored
# can be GC'd before the worker settles — exactly the cleanup we rely on.
# Tasks register here on creation and discard themselves on completion via
# ``add_done_callback``.
_pg_refresh_drain_tasks: set[asyncio.Task[None]] = set()


async def _drain_orphan_pg_lock(
    loop: asyncio.AbstractEventLoop,
    executor: concurrent.futures.ThreadPoolExecutor,
    cm: contextlib.AbstractContextManager[None],
    cf_fut: concurrent.futures.Future[Any],
) -> None:
    """Best-effort cleanup of a ``_PgRefreshLock`` whose ``__aenter__`` raised.

    The worker thread that ran ``cm.__enter__`` may complete *after* the
    awaiter has propagated an exception (typically a cancellation). If
    it ultimately acquired the Postgres advisory lock, the paired
    ``cm.__exit__`` MUST run on the same executor (same OS thread) so
    the connection-bound transaction commits / rolls back on the thread
    that began it — psycopg2 connections are thread-affine. On loop or
    process shutdown, the engine's ``dispose()`` is the backstop.

    The ``cf_fut`` parameter is the *underlying* ``concurrent.futures.Future``
    — NOT the asyncio wrapper that ``__aenter__`` was awaiting. That asyncio
    wrapper is in CANCELLED state once the awaiter was cancelled, and
    re-awaiting a CANCELLED future raises ``CancelledError`` immediately
    instead of waiting for the worker. A fresh ``asyncio.wrap_future(cf_fut)``
    creates a new asyncio Future tied only to the worker's outcome, so the
    drain genuinely waits for the worker to settle.
    """
    try:
        try:
            await asyncio.wrap_future(cf_fut, loop=loop)
        except Exception:
            # ``cm.__enter__`` raised on the worker — nothing acquired,
            # nothing to release. ``CancelledError`` is intentionally NOT
            # caught: if the drain task itself is cancelled (loop
            # shutdown), it should record as cancelled rather than be
            # silently logged as 'completed normally with no acquire'.
            # The lock then leaks to ``Engine.dispose()`` cleanup, which
            # is the documented backstop.
            return
        try:
            await loop.run_in_executor(executor, lambda: cm.__exit__(None, None, None))
        except Exception:
            # Same rationale as above for ``CancelledError``: don't
            # mask drain-task cancellation as 'drain_exit_failed'.
            log.warning(
                "mcp_server.oauth.pg_refresh_lock_drain_exit_failed",
                exc_info=True,
            )
    finally:
        executor.shutdown(wait=False)


class _PgRefreshLock(contextlib.AbstractAsyncContextManager[None]):
    """Async wrapper around :meth:`StorageBackend.acquire_advisory_lock_sync`.

    psycopg2 connection transactions are thread-affine, so for a given
    lock instance ``__enter__`` (which begins the transaction) and
    ``__exit__`` (which commits / rolls back) MUST run on the same OS
    thread. Each instance allocates a private single-worker
    ``ThreadPoolExecutor`` to satisfy that constraint. A module-global
    single-worker executor would also satisfy thread-affinity, but at
    the cost of serializing every advisory-lock acquire on the node
    behind one thread — different ``(user, server)`` keys would queue
    against each other through the spin loop's 30s timeout window.
    Per-instance executors keep the affinity guarantee while letting
    unrelated refreshes spin on ``pg_try_advisory_xact_lock`` in
    parallel.

    Cancellation between submit and the worker completing
    ``cm.__enter__`` is handled by ``_drain_orphan_pg_lock``: ``__aenter__``
    submits via ``executor.submit`` directly so it holds the
    ``concurrent.futures.Future``, then awaits a fresh
    ``asyncio.wrap_future`` of it. If the awaiter is cancelled, only the
    asyncio wrapper goes to CANCELLED state — the underlying worker
    continues. The drain wraps ``cf_fut`` again (fresh) and so genuinely
    waits for the worker to settle, then runs ``cm.__exit__`` on the same
    executor when the lock did get acquired.
    """

    def __init__(self, storage: StorageBackend, key_text: str) -> None:
        self._storage = storage
        self._key_text = key_text
        self._sync_cm: contextlib.AbstractContextManager[None] | None = None
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None

    async def __aenter__(self) -> None:
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="mcp-pg-refresh-lock"
        )
        cm = self._storage.acquire_advisory_lock_sync(self._key_text)
        loop = asyncio.get_running_loop()
        # Submit directly to keep a handle on the underlying
        # ``concurrent.futures.Future``. Each ``asyncio.wrap_future`` here
        # and inside the drain creates an *independent* asyncio Future
        # tied to the same worker outcome, so cancellation of one wrapper
        # doesn't poison the other.
        cf_fut: concurrent.futures.Future[Any] = executor.submit(cm.__enter__)
        try:
            await asyncio.wrap_future(cf_fut, loop=loop)
        except BaseException:
            drain = loop.create_task(
                _drain_orphan_pg_lock(loop, executor, cm, cf_fut),
                name="mcp-pg-refresh-lock-drain",
            )
            _pg_refresh_drain_tasks.add(drain)
            drain.add_done_callback(_pg_refresh_drain_tasks.discard)
            raise
        self._sync_cm = cm
        self._executor = executor

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        cm = self._sync_cm
        executor = self._executor
        self._sync_cm = None
        self._executor = None
        if cm is None or executor is None:
            return
        loop = asyncio.get_running_loop()

        def _exit() -> None:
            cm.__exit__(exc_type, exc, tb)

        # No drain analogue here — unlike ``__aenter__``, cancellation
        # mid-await doesn't strand the lock. ``loop.run_in_executor``
        # submits ``_exit`` synchronously before yielding, so the worker
        # already has the work; cancelling our await doesn't cancel the
        # worker. The cm's ``__exit__`` runs to completion on the same
        # thread that ran ``__enter__`` (psycopg2 thread-affinity
        # preserved), commits or rolls back the transaction, and
        # releases the advisory lock. ``shutdown(wait=False)`` lets the
        # worker finish without blocking; the executor is then GC'd.
        try:
            await loop.run_in_executor(executor, _exit)
        finally:
            executor.shutdown(wait=False)


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


@dataclass(frozen=True)
class TokenLookupResult:
    """Tagged result of :func:`get_user_access_token_classified`.

    Lets the dispatch state machine distinguish "row missing" from
    "row present but undecryptable" from "row present but refresh
    rejected" — three states that the ``Optional[str]``-returning
    :func:`get_user_access_token` collapses to ``None``. The
    distinction matters because they map to different user-facing
    errors — emitting ``mcp_consent_required`` on a decrypt failure
    would be wrong (the user can't fix it; only an operator can).
    """

    kind: Literal[
        "token", "missing", "decrypt_failure", "refresh_failed", "refresh_failed_transient"
    ] = "missing"
    token: str | None = None
    decrypt_fingerprints: tuple[str, ...] = field(default_factory=tuple)


# In-process (per-node) backoff bookkeeping for transient refresh failures,
# keyed ``(user_id, server_name)`` on ``app_state.mcp_oauth_refresh_backoff``.
# The cooldown timer short-circuits the token-endpoint round-trip during a
# sustained AS outage (perf); the ambiguous streak escalates an
# unclassifiable-but-persistent rejection to re-consent so a dead grant in a
# non-standard shape can't strand the user forever.
_REFRESH_TRANSIENT_COOLDOWN_SECONDS = 30.0
_AMBIGUOUS_ESCALATION_THRESHOLD = 5


@dataclass
class _RefreshBackoffState:
    """Per-(user, server) transient-refresh backoff state (see the helpers below)."""

    last_failure_monotonic: float = 0.0
    ambiguous_streak: int = 0
    # True when the failure that armed the current cooldown was a PERMANENT
    # dead-grant (only the oauth_obo path arms a cooldown on permanent, because
    # its shared credential survives the per-server revoke). The in-cooldown
    # short-circuit reads this so it surfaces the honest permanent classification
    # (re-login / admin remedy) instead of a misleading "retry" transient for the
    # whole window. Reset to False whenever a transient/ambiguous failure arms.
    last_failure_permanent: bool = False


def _refresh_backoff_state(app_state: Any, user_id: str, server_name: str) -> _RefreshBackoffState:
    """Return (creating if absent) the backoff state for ``(user_id, server_name)``."""
    states = getattr(app_state, "mcp_oauth_refresh_backoff", None)
    if states is None:
        states = {}
        app_state.mcp_oauth_refresh_backoff = states
    key = (user_id, server_name)
    state = states.get(key)
    if state is None:
        state = _RefreshBackoffState()
        states[key] = state
    return state


def _arm_cooldown(
    app_state: Any, user_id: str, server_name: str, *, permanent: bool = False
) -> _RefreshBackoffState:
    """Stamp the per-(user, server) transient-failure cooldown clock to now.

    Single definition of the "back off this pair" operation (previously written
    inline at every failure site) so a change to how the cooldown is armed —
    jitter, a min-interval, a second timestamp — is one edit, not four, and a
    missed site can't silently keep hammering the AS/IdP on that path. Returns
    the backoff state so a caller that also mutates the ambiguous streak reuses
    the same object instead of re-fetching it.

    ``permanent`` records whether the failure that armed the cooldown was a
    dead-grant (obo only): the in-cooldown short-circuit reads it to surface the
    honest permanent vs. transient classification. A transient/ambiguous arm
    resets it to False so a later transient window can't inherit a stale
    permanent flag.
    """
    state = _refresh_backoff_state(app_state, user_id, server_name)
    state.last_failure_monotonic = time.monotonic()
    state.last_failure_permanent = permanent
    return state


def _clear_refresh_backoff(app_state: Any, user_id: str, server_name: str) -> None:
    """Drop the backoff state for ``(user_id, server_name)``.

    Called whenever a usable token is returned or the token is revoked, so a
    healthy grant resets the cooldown timer + ambiguous streak and the dict
    stays bounded to live ``(user, server)`` pairs.
    """
    states = getattr(app_state, "mcp_oauth_refresh_backoff", None)
    if isinstance(states, dict):
        states.pop((user_id, server_name), None)


def _refresh_in_cooldown(app_state: Any, user_id: str, server_name: str) -> bool:
    """Return True while within the post-transient-failure cooldown window."""
    states = getattr(app_state, "mcp_oauth_refresh_backoff", None)
    if not isinstance(states, dict):
        return False
    state: _RefreshBackoffState | None = states.get((user_id, server_name))
    if state is None or not state.last_failure_monotonic:
        return False
    elapsed = time.monotonic() - state.last_failure_monotonic
    return elapsed < _REFRESH_TRANSIENT_COOLDOWN_SECONDS


def _token_result(
    app_state: Any, user_id: str, server_name: str, token: str | None
) -> TokenLookupResult:
    """Return a ``kind="token"`` result, resetting any transient-refresh backoff.

    A usable token means the grant is healthy, so the cooldown timer and the
    ambiguous-failure streak are cleared here at the single success choke point.
    """
    _clear_refresh_backoff(app_state, user_id, server_name)
    return TokenLookupResult(kind="token", token=token)


def _no_token_result(
    app_state: Any, user_id: str, server_name: str, result: TokenLookupResult
) -> TokenLookupResult:
    """Drop per-(user, server) refresh state, then return a non-token *result*.

    A ``missing`` / ``decrypt_failure`` outcome means the grant is no longer live
    on this node (token deleted cluster-wide, key rotated), so BOTH sibling
    per-(user, server) dicts are pruned — the refresh lock and the transient
    backoff — keeping each bounded to live pairs (the mirror of
    :func:`_token_result` on success and :func:`_revoke_after_refresh_failure`
    on revoke). The transient keep-path deliberately retains the lock (so
    concurrent same-key refreshes stay serialized) and the backoff (for the
    cooldown), so without this prune a token that vanishes after a transient
    failure would strand both entries.
    """
    _drop_refresh_lock(app_state, user_id, server_name)
    _clear_refresh_backoff(app_state, user_id, server_name)
    return result


async def _revoke_after_refresh_failure(
    app_state: Any,
    token_store: MCPTokenStore,
    user_id: str,
    server_name: str,
    server_id_for_audit: str,
    *,
    reason: str,
    audit_when_absent: bool = True,
) -> TokenLookupResult:
    """Delete the stored token, emit a ``token_revoked`` audit, and drop locks.

    The single revoke choke point for every "the grant is dead" outcome
    (permanent rejection, ambiguous-streak escalation, expired-with-no-refresh).
    Drops the per-key refresh lock and backoff state — both safe to call when no
    entry exists — and returns ``refresh_failed`` so the dispatcher surfaces
    re-consent.

    ``audit_when_absent`` controls the audit when ``delete_user_token`` finds no
    row:

    - oauth_user (default ``True``): reaching a refresh-grant failure means a
      grant EXISTED (you cannot refresh without a token), so a real grant died —
      audit it even if a concurrent admin/user revoke deleted the row first, so
      an operator's SIEM never misses the AS-rejected-the-grant signal. This is
      the pre-refactor behavior (the audit was unconditional on main).
    - oauth_obo (``False``): a mint needs no pre-existing cache row, so a
      permanent rejection on a server the user never successfully minted for
      lands here with nothing to delete; its shared credential also survives, so
      every later dispatch/prime past the cooldown re-runs the doomed redemption
      and returns here again. Auditing those would append a "token_revoked" row
      for a token that never existed, forever — so obo audits only a real
      deletion.
    """
    deleted = await asyncio.to_thread(token_store.delete_user_token, user_id, server_name)
    if deleted or audit_when_absent:
        await _audit_event(
            app_state,
            server_id=server_id_for_audit,
            user_id=user_id,
            action="mcp_server.oauth.token_revoked",
            server_name=server_name,
            detail={"reason": reason},
        )
    _drop_refresh_lock(app_state, user_id, server_name)
    _clear_refresh_backoff(app_state, user_id, server_name)
    return TokenLookupResult(kind="refresh_failed")


def _decrypt_failure_result(
    app_state: Any,
    user_id: str,
    server_name: str,
    exc: MCPTokenDecryptError,
    *,
    event: str | None,
) -> TokenLookupResult:
    """Map an ``MCPTokenDecryptError`` to a classified ``decrypt_failure`` result.

    One construction of the (optional warning log + ``_no_token_result`` +
    fingerprint-carrying ``TokenLookupResult``) shape, shared by the five
    token/credential read sites in the oauth_user and oauth_obo state machines
    (the raw ``get_user_token`` / ``get_oidc_credential`` calls each raise this
    on a key rotated away, and each must keep the classified-result contract).
    Pass the site's structured *event* name to log, or ``None`` for a re-read
    whose first read already logged.
    """
    if event is not None:
        log.warning(event, user_id=user_id, server_name=server_name, exc_info=True)
    return _no_token_result(
        app_state,
        user_id,
        server_name,
        TokenLookupResult(
            kind="decrypt_failure",
            decrypt_fingerprints=tuple(exc.key_fingerprints_attempted),
        ),
    )


async def get_user_access_token(*, app_state: Any, user_id: str, server_name: str) -> str | None:
    """Return a valid plaintext access token, refreshing if needed.

    Returns ``None`` when:
      - no token exists for ``(user_id, server_name)``
      - the row exists but cannot be refreshed (the row is then deleted
        and an audit event emitted; the caller should surface
        ``mcp_consent_required``)
      - the cipher / token store is not configured

    Implemented as a thin wrapper around
    :func:`get_user_access_token_classified`: every non-``token`` result
    collapses back to ``None`` to preserve the legacy contract.
    """
    result = await get_user_access_token_classified(
        app_state=app_state, user_id=user_id, server_name=server_name
    )
    if result.kind == "token":
        return result.token
    return None


class _FailureEvents(NamedTuple):
    """Structured-log event names for :func:`_handle_refresh_failure`.

    Passed as whole literals (not composed from a prefix) so every emitted
    event name appears verbatim in source — operators' alerting greps for
    e.g. ``mcp_server.oauth.refresh_transient_failure`` and must find it here.
    ``permanent`` logs the IdP error text on a PERMANENT rejection before the
    revoke; ``None`` suppresses it (preserves oauth_user's exact prior output).
    """

    transient: str
    escalated: str
    deferred: str
    permanent: str | None


_REFRESH_FAILURE_EVENTS = _FailureEvents(
    transient="mcp_server.oauth.refresh_transient_failure",
    escalated="mcp_server.oauth.refresh_ambiguous_escalated",
    deferred="mcp_server.oauth.refresh_ambiguous_escalation_deferred",
    permanent=None,  # oauth_user's permanent path logged no error-text line
)
_OBO_MINT_FAILURE_EVENTS = _FailureEvents(
    transient="mcp_server.oauth.obo_mint_transient_failure",
    escalated="mcp_server.oauth.obo_mint_ambiguous_escalated",
    deferred="mcp_server.oauth.obo_mint_ambiguous_escalation_deferred",
    permanent="mcp_server.oauth.obo_mint_rejected",  # carries the AS error body
)


async def _handle_refresh_failure(
    exc: MCPOAuthRefreshFailed,
    *,
    app_state: Any,
    user_id: str,
    server_name: str,
    server_id_for_audit: str,
    token_store: MCPTokenStore,
    revoke_on_failure: bool,
    revoke_ambiguous_escalation: bool,
    events: _FailureEvents,
    permanent_reason: str,
    escalation_reason: str,
    arm_cooldown_on_permanent: bool = False,
) -> TokenLookupResult:
    """Shared failure classifier for the refresh-grant (oauth_user) and mint
    (oauth_obo) token-lookup state machines.

    Extracted so the two pool auth types can never drift on
    revoke/backoff/escalation semantics — a change to escalation policy applies
    to both. Behaviour for ``oauth_user`` is byte-identical to the previous
    inline block (``events=_REFRESH_FAILURE_EVENTS``, ``arm_cooldown_on_permanent=False``).

    ``arm_cooldown_on_permanent`` is the one divergence the two callers need:
    oauth_user's post-revoke lookup short-circuits to ``missing`` (its token row
    was deleted, so no AS call recurs), but oauth_obo's shared credential
    survives a per-server revoke by design — so without arming the cooldown here
    every subsequent dispatch would re-run the doomed IdP redemption and emit
    another ``token_revoked`` audit row. Arming it gives the permanent-rejection
    arm a terminal backstop (one redemption per cooldown window).
    """
    if not revoke_on_failure:
        # Observe-only (background sweep): never revoke, never mutate the
        # shared streak / cooldown. A permanent rejection surfaces as a
        # dead grant to badge; anything else is a retryable transient the
        # next tick (or a real dispatch) re-attempts. The 240s sweep
        # cadence is its own rate limit, so skipping the cooldown here
        # cannot hammer the AS.
        if exc.failure_class is _RefreshFailureClass.PERMANENT:
            return TokenLookupResult(kind="refresh_failed")
        return TokenLookupResult(kind="refresh_failed_transient")
    if exc.failure_class is _RefreshFailureClass.PERMANENT:
        # The AS rejected the grant as dead (invalid_grant / invalid_scope
        # / an OIDC interaction-required code): a reliable dead-grant
        # signal (RFC 6749 §5.2), so revoke unconditionally — even under
        # background priming. Deferring it would strand the catalog cold
        # with the token still reading "consented" and no re-consent path.
        if events.permanent is not None:
            # Record the IdP error body BEFORE the revoke: it is the only place
            # the AS's actual message survives (the token_revoked audit row
            # carries just a reason code), and it is what lets an operator tell a
            # missing tenant-grant / Conditional-Access challenge from a dead
            # credential without wire-level capture.
            log.warning(
                events.permanent,
                user_id=user_id,
                server_name=server_name,
                error=str(exc),
            )
        result = await _revoke_after_refresh_failure(
            app_state,
            token_store,
            user_id,
            server_name,
            server_id_for_audit,
            reason=permanent_reason,
            # ``arm_cooldown_on_permanent`` marks the obo path (shared credential
            # survives the revoke); there, audit only a real deletion to avoid
            # revocation rows for tokens that never existed. oauth_user audits
            # unconditionally (a refresh failure means a grant existed).
            audit_when_absent=not arm_cooldown_on_permanent,
        )
        if arm_cooldown_on_permanent:
            # _revoke_after_refresh_failure cleared the backoff; re-arm the
            # cooldown as the terminal backstop for the surviving credential, and
            # mark it permanent so the in-cooldown short-circuit surfaces the
            # honest dead-grant classification (not a misleading "retry").
            _arm_cooldown(app_state, user_id, server_name, permanent=True)
        return result
    # Transient or ambiguous: keep the token (a blip must never revoke a
    # user's consent) and arm the cooldown so a down AS isn't hit on
    # every later dispatch.
    backoff = _arm_cooldown(app_state, user_id, server_name)
    if exc.failure_class is _RefreshFailureClass.AMBIGUOUS:
        backoff.ambiguous_streak += 1
        if backoff.ambiguous_streak >= _AMBIGUOUS_ESCALATION_THRESHOLD:
            # A persistent 400/401 rejection we can't map to a standard
            # code most likely IS a dead grant the AS reports in a
            # non-standard shape. Escalate to re-consent so the user
            # isn't stranded on a retryable error forever. (Infra
            # transients never reach here, so an outage can't escalate.)
            if revoke_ambiguous_escalation:
                log.warning(
                    events.escalated,
                    user_id=user_id,
                    server_name=server_name,
                    streak=backoff.ambiguous_streak,
                    error=str(exc),
                )
                escalation_result = await _revoke_after_refresh_failure(
                    app_state,
                    token_store,
                    user_id,
                    server_name,
                    server_id_for_audit,
                    reason=escalation_reason,
                    audit_when_absent=not arm_cooldown_on_permanent,
                )
                if arm_cooldown_on_permanent:
                    # Same shared-credential backstop as the PERMANENT branch: an
                    # escalation is a treated-as-dead grant, but the revoke
                    # cleared the cooldown, and for obo the credential survives —
                    # so without re-arming, the very next dispatch immediately
                    # re-mints against the still-failing IdP and re-escalates
                    # each cycle. Re-arm (marked permanent for the honest
                    # in-cooldown classification).
                    _arm_cooldown(app_state, user_id, server_name, permanent=True)
                return escalation_result
            # Background priming: an UNCLASSIFIABLE sustained rejection is
            # exactly where a bulk prime of servers the user may not be
            # using must not revoke consent. Defer the escalation-revoke to
            # lazy dispatch — the streak + armed cooldown persist, so it
            # escalates on the user's next real call. Falls through to the
            # transient return below (token kept, lock retained).
            log.warning(
                events.deferred,
                user_id=user_id,
                server_name=server_name,
                streak=backoff.ambiguous_streak,
                error=str(exc),
            )
    else:
        # A clean infra/operator-fixable transient breaks any ambiguous
        # run — only an uninterrupted streak escalates.
        backoff.ambiguous_streak = 0
    log.warning(
        events.transient,
        user_id=user_id,
        server_name=server_name,
        failure_class=exc.failure_class.value,
        ambiguous_streak=backoff.ambiguous_streak,
        error=str(exc),
    )
    # Do NOT drop the refresh lock here: the token is kept, so the
    # per-key asyncio.Lock must stay registered to keep serializing
    # concurrent refreshes. Dropping it would let a second concurrent
    # caller mint a fresh lock and refresh the same token in parallel —
    # with refresh-token rotation that races to invalid_grant and a
    # spurious revoke (the exact bug this path prevents). The async-with
    # still releases the lock on return; the entry is pruned when the
    # token is later refreshed or revoked.
    return TokenLookupResult(kind="refresh_failed_transient")


async def get_user_access_token_classified(
    *,
    app_state: Any,
    user_id: str,
    server_name: str,
    force_refresh: bool = False,
    revoke_ambiguous_escalation: bool = True,
    revoke_on_failure: bool = True,
) -> TokenLookupResult:
    """Tagged token lookup with refresh-on-expiry.

    Walks the token-lookup state machine (token / missing /
    decrypt_failure / refresh_failed / refresh_failed_transient) and
    returns a tagged result so the dispatcher can map each failure mode
    to the right user-facing error. A transient refresh failure keeps the
    token and returns ``refresh_failed_transient`` (retryable, no revoke);
    only a permanent rejection — or a sustained run of ambiguous ones —
    revokes the stored token and returns ``refresh_failed``.

    Multi-node correctness: the refresh path is serialized at two
    layers — an outer ``asyncio.Lock`` from :func:`_refresh_lock_for`
    (intra-process) and an inner Postgres advisory lock acquired via
    :meth:`StorageBackend.acquire_advisory_lock_sync` (cluster-wide;
    no-op on SQLite). Order matters: the in-process lock is taken
    first so concurrent same-key callers on this node collapse to a
    single waiter at the cluster lock — this also collapses N
    per-instance ``ThreadPoolExecutor`` allocations down to one. The
    surviving local caller then serializes against other nodes via
    the cluster lock. The re-read inside the locked block collapses
    both contention windows.

    ``force_refresh=True`` bypasses the local freshness check and
    forces an AS round-trip — used by the dispatch path when the
    upstream AS rejected a token our cache still considered fresh
    (e.g., AS-side revocation). Concurrent ``force_refresh=True``
    callers still collapse to one round-trip via the dual-layer lock:
    the second caller sees ``last_refreshed > t_lock_request_started``
    and reuses the freshly-refreshed token.

    ``revoke_ambiguous_escalation=False`` narrows revocation for background
    session-start priming. A genuinely-dead grant is STILL revoked so it is
    cleaned up and the user gets a re-consent affordance: a PERMANENT AS
    rejection (``invalid_grant`` / ``invalid_scope`` — a reliable dead-grant
    signal per RFC 6749 §5.2) and an expired-with-no-refresh token both revoke
    unconditionally. Only the *sustained-ambiguous* escalation — an
    unclassifiable 400/401 the heuristic would treat as dead after a streak — is
    deferred: priming runs for EVERY consented server, so an unclassifiable AS
    hiccup must not revoke consent for a server the user may not even be using
    this session. The streak + cooldown persist, so the authoritative
    escalation-revoke happens on the lazy-dispatch path when the user actually
    invokes the tool.

    ``revoke_on_failure=False`` makes the lookup fully OBSERVE-ONLY: it still
    refreshes a healthy near-expiry token, but a failure NEVER mutates durable
    or cross-call state — no token deletion, no ``token_revoked`` audit, and no
    ambiguous-streak / cooldown bump. A permanent rejection is reported as
    ``refresh_failed`` and everything else as ``refresh_failed_transient``, but
    the row is left intact for the authoritative (revoking) lazy-dispatch path.
    Used by the background token-freshness sweep, which reconciles EVERY
    consented grant on a timer for users who aren't present: a timer must not be
    able to delete a token or move a foreground user's revoke threshold, and a
    spurious server-wide ``invalid_grant`` (e.g. an AS maintenance window) must
    self-heal on a later tick rather than force a fleet-wide re-consent.
    """
    token_store: MCPTokenStore | None = getattr(app_state, "mcp_token_store", None)
    if token_store is None:
        log.debug("mcp_server.oauth.token_store_unconfigured")
        return TokenLookupResult(kind="missing")

    # Captured BEFORE we acquire any lock so the inside-lock guard can
    # tell whether another caller refreshed under contention. Truncated
    # to seconds because ``last_refreshed`` storage has second-precision
    # ISO8601 — comparing microsecond-precision against second-precision
    # would race when the refresh and the contention occur in the same
    # wall-clock second.
    t_lock_request_started = datetime.now(UTC).replace(microsecond=0)

    try:
        plain = await asyncio.to_thread(token_store.get_user_token, user_id, server_name)
    except MCPTokenDecryptError as exc:
        return _decrypt_failure_result(
            app_state,
            user_id,
            server_name,
            exc,
            event="mcp_server.oauth.token_decrypt_failed_classified",
        )
    if plain is None:
        return _no_token_result(app_state, user_id, server_name, TokenLookupResult(kind="missing"))

    expires_at = plain.get("expires_at")
    needs_refresh = _token_needs_refresh(expires_at)
    if not force_refresh and not needs_refresh:
        return _token_result(app_state, user_id, server_name, plain["access_token"])

    # perf: during a sustained AS outage, short-circuit the token-endpoint
    # round-trip for a brief window after a transient failure rather than
    # re-attempting on every dispatch. Gated on the locally-read token being
    # itself expired — a force_refresh on a still-fresh-looking token (the 401
    # retry) falls through so the in-lock race check can still pick up a token a
    # cluster-mate just refreshed.
    if needs_refresh and _refresh_in_cooldown(app_state, user_id, server_name):
        return TokenLookupResult(kind="refresh_failed_transient")

    storage = _get_storage(app_state)
    if storage is None:
        return _no_token_result(app_state, user_id, server_name, TokenLookupResult(kind="missing"))

    # bug-6: load server_row BEFORE the no-refresh-token branch so the
    # pre-lock audit event carries the immutable server_id rather than
    # falling back to a name-keyed lookup that races admin renames.
    server_row = await asyncio.to_thread(storage.get_mcp_server_by_name, server_name)
    if server_row is None:
        return _no_token_result(app_state, user_id, server_name, TokenLookupResult(kind="missing"))
    server_id_for_audit = str(server_row.get("server_id") or "")

    refresh_value = plain.get("refresh_token")
    if not refresh_value:
        # Expired token with no refresh token: genuinely unusable, no
        # misclassification risk (a local check, not an AS response) — revoke.
        # Observe-only callers surface it as a dead grant without deleting.
        if not revoke_on_failure:
            return TokenLookupResult(kind="refresh_failed")
        return await _revoke_after_refresh_failure(
            app_state,
            token_store,
            user_id,
            server_name,
            server_id_for_audit,
            reason="expired_no_refresh",
        )

    lock = _refresh_lock_for(app_state, user_id, server_name)
    pg_lock = await _acquire_pg_refresh_lock(storage, user_id, server_name)
    # In-process lock outside the cross-node lock so concurrent same-key
    # callers serialize on the asyncio.Lock BEFORE allocating the
    # pg_lock's per-instance executor / spin loop. N concurrent callers
    # for one key collapse to one executor allocation.
    async with lock, pg_lock:
        try:
            plain2 = await asyncio.to_thread(token_store.get_user_token, user_id, server_name)
        except MCPTokenDecryptError as exc:
            return _decrypt_failure_result(
                app_state,
                user_id,
                server_name,
                exc,
                event="mcp_server.oauth.token_decrypt_failed_classified",
            )
        if plain2 is None:
            return _no_token_result(
                app_state, user_id, server_name, TokenLookupResult(kind="missing")
            )
        expires_at2 = plain2.get("expires_at")
        # Reuse the freshly-refreshed token under two conditions:
        #  1. ``force_refresh=False`` and the cached token is still fresh
        #     (existing fast path).
        #  2. ``force_refresh=True`` BUT another same-key caller already
        #     refreshed under contention since we started waiting for the
        #     lock. ``last_refreshed`` is the storage-side replacement
        #     timestamp; if it advanced past ``t_lock_request_started``,
        #     we lost the race and should reuse rather than refresh again.
        if not _token_needs_refresh(expires_at2):
            if not force_refresh:
                return _token_result(app_state, user_id, server_name, plain2["access_token"])
            last_refreshed = _parse_iso_to_utc(plain2.get("last_refreshed") or "")
            if last_refreshed is not None and last_refreshed >= t_lock_request_started:
                return _token_result(app_state, user_id, server_name, plain2["access_token"])
        refresh_value2 = plain2.get("refresh_token")
        if not refresh_value2:
            if not revoke_on_failure:
                return TokenLookupResult(kind="refresh_failed")
            return await _revoke_after_refresh_failure(
                app_state,
                token_store,
                user_id,
                server_name,
                server_id_for_audit,
                reason="expired_no_refresh",
            )

        try:
            new_access, _new_refresh, _new_expires_at = await _refresh_and_persist(
                app_state=app_state,
                storage=storage,
                token_store=token_store,
                user_id=user_id,
                server_name=server_name,
                server_row=server_row,
                refresh_value=refresh_value2,
                existing_scopes=plain2.get("scopes") or "",
            )
        except MCPOAuthRefreshFailed as exc:
            return await _handle_refresh_failure(
                exc,
                app_state=app_state,
                user_id=user_id,
                server_name=server_name,
                server_id_for_audit=server_id_for_audit,
                token_store=token_store,
                revoke_on_failure=revoke_on_failure,
                revoke_ambiguous_escalation=revoke_ambiguous_escalation,
                events=_REFRESH_FAILURE_EVENTS,
                permanent_reason="refresh_failed",
                escalation_reason="refresh_failed_ambiguous_escalated",
            )
        return _token_result(app_state, user_id, server_name, new_access)


# ---------------------------------------------------------------------------
# Single-credential on-behalf-of minting (auth_type='oauth_obo', issue #551)
#
# Servers with auth_type='oauth_obo' never run the per-server browser consent
# flow.  Instead the user's single captured IdP refresh token (see
# `[oidc] capture_user_credential`) is redeemed on demand for a short-lived
# server-audience access token via the deployment's grant leg:
#
#   entra    — one refresh-token grant with scope=<audience>/.default
#              (Entra RTs are client-bound, not resource-bound — verified)
#   rfc8693  — refresh grant for a subject token, then a standard
#              token-exchange with audience=<server client id> (verified on
#              Keycloak 26.3; per-server oauth_scopes activates optional
#              audience scopes)
#
# The minted token is cached in the existing per-(user, server)
# mcp_user_tokens row with refresh_token_ct=NULL — cache, not custody.  A
# permanent mint failure drops ONLY that cache row (re-consent UX for that
# server); the shared credential is NEVER auto-deleted here — a missing
# tenant grant for one server (AADSTS65001, verified) must not lock the user
# out of every other OBO server.  Credential lifecycle (logout/admin revoke)
# is handled elsewhere.
# ---------------------------------------------------------------------------


async def _obo_token_post(
    *,
    token_endpoint: str,
    data: dict[str, str],
    http_client: httpx.AsyncClient,
    leg: str,
) -> dict[str, Any]:
    """POST one OBO grant-leg request; classify failures like a refresh.

    Label-binding wrapper over :func:`_hardened_token_post` (the shared
    body-size-cap / JSON-object-validation / conservative
    :func:`_classify_refresh_failure` skeleton), so the OBO state machine
    reacts to AS rejections exactly like the oauth_user one — a verified
    AADSTS65001 (missing tenant grant) classifies PERMANENT via
    ``invalid_grant``.
    """
    label = f"obo {leg}"
    return await _hardened_token_post(
        token_endpoint=token_endpoint,
        data=data,
        http_client=http_client,
        request_label=label,
        endpoint_label=label,
        # OBO: an over-sized client-error body escalates (AMBIGUOUS) rather than
        # looping "please retry" — see _hardened_token_post. (oauth_user keeps
        # the TRANSIENT default.)
        classify_oversized_by_status=True,
    )


# A leg persists a rotated CREDENTIAL refresh token the instant it obtains one,
# via a caller-supplied ``persist_rotation`` callback bound to the credential
# under the held lock. It is the ONLY channel by which a mint updates the stored
# credential — the returned access-token dict's own ``refresh_token`` (if any) is
# never written back to the credential, so an audience-scoped exchange RT (RFC
# 8693 §2.2.1) cannot poison it.


async def _maybe_persist_rotation(
    resp: dict[str, Any],
    credential_refresh_token: str,
    persist_rotation: Callable[[str], Awaitable[None]],
) -> None:
    """Persist a rotated credential RT from *resp* when it differs from the current one."""
    rotated = resp.get("refresh_token")
    if isinstance(rotated, str) and rotated and rotated != credential_refresh_token:
        await persist_rotation(rotated)


#: Audiences already warned about ignored entra scopes — dedupes the warning to
#: once per audience per process (see _obo_mint_entra).
_ENTRA_SCOPE_IGNORED_WARNED: set[str] = set()


async def _obo_mint_entra(
    *,
    oidc_config: Any,
    credential_refresh_token: str,
    audience: str,
    scopes: str,
    http_client: httpx.AsyncClient,
    persist_rotation: Callable[[str], Awaitable[None]],
) -> dict[str, Any]:
    """Entra leg: redeem the client-bound RT directly for the audience.

    Wire shape verified against a real tenant (docs/design/obo-spike):
    ``grant_type=refresh_token`` + ``scope=<audience>/.default`` returns an
    audience-scoped access token and (usually) a rotated refresh token.

    ``scope`` is Entra's ONLY audience carrier, so it always pins
    ``<audience>/.default`` — the pre-consented-delegated-permissions model the
    feature targets. Per-server ``oauth_scopes`` do NOT apply here (a bare scope
    list would drop the audience and yield a wrong-audience token); they are a
    ``rfc8693``-only knob.
    """
    if scopes and audience not in _ENTRA_SCOPE_IGNORED_WARNED:
        # Entra ignores oauth_scopes (it pins <audience>/.default), so a
        # configured scope restriction silently does not apply on this
        # credential-minting path. The admin write path rejects NEW
        # scopes-with-entra, but a deployment-level profile switch
        # (rfc8693→entra) leaves pre-existing scoped rows — surface that ONCE
        # per audience per process (not per mint) so it's visible at default log
        # levels without flooding.
        _ENTRA_SCOPE_IGNORED_WARNED.add(audience)
        log.warning(
            "mcp_server.oauth.obo_entra_scopes_ignored",
            audience=audience,
            hint=(
                "oauth_scopes is not applied on the entra grant leg (it mints "
                "<audience>/.default); clear oauth_scopes or use the rfc8693 profile"
            ),
        )
    resp = await _obo_token_post(
        token_endpoint=oidc_config.token_endpoint,
        data={
            "grant_type": "refresh_token",
            "refresh_token": credential_refresh_token,
            "client_id": oidc_config.client_id,
            "client_secret": oidc_config.client_secret,
            "scope": f"{audience}/.default",
        },
        http_client=http_client,
        leg="entra-redemption",
    )
    await _maybe_persist_rotation(resp, credential_refresh_token, persist_rotation)
    return resp


async def _obo_mint_rfc8693(
    *,
    oidc_config: Any,
    credential_refresh_token: str,
    audience: str,
    scopes: str,
    http_client: httpx.AsyncClient,
    persist_rotation: Callable[[str], Awaitable[None]],
) -> dict[str, Any]:
    """RFC 8693 leg: refresh grant for a subject token, then token exchange.

    Chain verified on Keycloak 26.3 standard token exchange
    (docs/design/obo-spike). Rotation ordering is correctness-critical: the
    refresh leg may consume-and-rotate the credential RT, so its rotated value
    is persisted IMMEDIATELY (before the exchange leg) — if the exchange then
    fails, the stored credential already holds the live rotated RT rather than a
    consumed one (else the next mint for every obo server would fail and lock the
    user out). The exchange response's own ``refresh_token`` (RFC 8693 §2.2.1
    permits one, audience-scoped) is deliberately NOT persisted to the credential.
    """
    subject = await _obo_token_post(
        token_endpoint=oidc_config.token_endpoint,
        data={
            "grant_type": "refresh_token",
            "refresh_token": credential_refresh_token,
            "client_id": oidc_config.client_id,
            "client_secret": oidc_config.client_secret,
        },
        http_client=http_client,
        leg="rfc8693-refresh",
    )
    # Persist the credential rotation BEFORE the exchange call can fail.
    await _maybe_persist_rotation(subject, credential_refresh_token, persist_rotation)

    subject_at = subject.get("access_token")
    if not isinstance(subject_at, str) or not subject_at:
        raise MCPOAuthRefreshFailed("obo rfc8693-refresh response missing access_token")

    exchange_data: dict[str, str] = {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "client_id": oidc_config.client_id,
        "client_secret": oidc_config.client_secret,
        "subject_token": subject_at,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": audience,
    }
    if scopes:
        # e.g. Keycloak optional audience scopes must be requested explicitly
        # or the exchange fails "Requested audience not available" (verified).
        exchange_data["scope"] = scopes
    return await _obo_token_post(
        token_endpoint=oidc_config.token_endpoint,
        data=exchange_data,
        http_client=http_client,
        leg="rfc8693-exchange",
    )


_OBO_MINT_LEGS = {
    "entra": _obo_mint_entra,
    "rfc8693": _obo_mint_rfc8693,
}

#: Grant legs a deployment may select via ``[oidc] obo_grant_profile`` — derived
#: from the operative registry so the two never drift (used by oidc config
#: validation; there is no second hand-written copy).
OBO_GRANT_PROFILES: frozenset[str] = frozenset(_OBO_MINT_LEGS)


async def _persist_obo_cache_row(
    token_store: MCPTokenStore,
    user_id: str,
    server_name: str,
    *,
    access_token: str,
    expires_at: str | None,
    scopes: str,
    issuer: str,
    audience: str,
) -> None:
    """Write the per-(user, server) mint-cache row (refresh_token=NULL).

    Delete-then-create rather than update-in-place: the row is pure cache (no
    refresh token to preserve), and — crucially — a plain update would keep the
    OLD ``audience`` / ``as_issuer`` / ``scopes`` columns
    (``update_user_token_after_refresh`` rewrites only the token + expiry), so a
    re-mint after an audience change would store the new token under the stale
    audience and the read-side audience guard would re-mint on every dispatch
    forever. Deleting first guarantees the row's audience matches what was minted.
    Runs under the per-(user, server) lock, so the delete/create can't race a
    concurrent mint for this pair.
    """
    await asyncio.to_thread(token_store.delete_user_token, user_id, server_name)
    await asyncio.to_thread(
        token_store.create_user_token,
        user_id,
        server_name,
        access_token=access_token,
        refresh_token=None,
        expires_at=expires_at,
        scopes=scopes or None,
        as_issuer=issuer,
        audience=audience,
    )


async def _read_obo_credential(
    app_state: Any,
    token_store: MCPTokenStore,
    user_id: str,
    server_name: str,
    issuer: str,
) -> TokenLookupResult | OIDCCredentialPlain:
    """Read the captured IdP credential, classifying absence and undecryptability.

    Returns the plaintext credential dict, or a ``TokenLookupResult`` when it is
    ``missing`` (no credential → the dispatcher surfaces a re-login) or
    ``decrypt_failure`` (key rotated away → operator action). The bare
    ``get_oidc_credential`` raises ``MCPTokenDecryptError``; catching it here
    keeps the mint path's classified-result contract intact (a raw exception
    would escape ``_dispatch_pool`` into the session's generic error path).
    """
    try:
        credential = await asyncio.to_thread(token_store.get_oidc_credential, user_id, issuer)
    except MCPTokenDecryptError as exc:
        return _decrypt_failure_result(
            app_state,
            user_id,
            server_name,
            exc,
            event="mcp_server.oauth.obo_credential_decrypt_failed",
        )
    if credential is None:
        # No captured credential → the consent affordance is a re-login.
        return _no_token_result(app_state, user_id, server_name, TokenLookupResult(kind="missing"))
    return credential


def _is_fresh_obo_cache_row(
    plain: MCPUserTokenPlain | None, current_audience: str, current_scopes: str
) -> bool:
    """True when a cache row may be served as a minted obo access token.

    Four conditions, all required (single source of truth for the pre-lock read
    AND the post-lock re-read so they can't drift):

    - refresh_token is NULL — minted rows carry no refresh token; a
      refresh-bearing row is a stale oauth_user leftover (an in-flight refresh
      that landed after an auth_type-flip purge) and must never be served;
    - the row's audience equals the server's CURRENT audience — a token minted
      for a since-narrowed audience must NOT be served, so an operator's
      privilege reduction takes effect immediately rather than at token TTL
      (the audience-change purge is best-effort; this is the authoritative gate);
    - the row's scopes equal the server's CURRENT scopes — the same authoritative
      gate for the rfc8693 exchange scope (which shapes the minted bearer's
      privileges just like the audience): a scope NARROWING must take effect on
      the next dispatch even if the admin's best-effort cache purge failed,
      rather than serving the wider-privilege bearer until its TTL. Under the
      entra leg scopes are inert, so the stored and current values track the
      same server column and this term is a no-op there;
    - not at/near expiry.
    """
    return (
        plain is not None
        and plain["refresh_token"] is None
        and (plain.get("audience") or "") == current_audience
        and (plain.get("scopes") or "") == current_scopes
        and not _token_needs_refresh(plain["expires_at"])
    )


async def get_obo_access_token_classified(
    *,
    app_state: Any,
    user_id: str,
    server_name: str,
    force_refresh: bool = False,
    revoke_ambiguous_escalation: bool = True,
    server_row: dict[str, Any] | None = None,
    credential_present: bool | None = None,
) -> TokenLookupResult:
    """Tagged token lookup for ``auth_type='oauth_obo'`` servers.

    ``server_row`` may be passed by a caller that already holds the
    ``mcp_servers`` row (the dispatch path does) to save a per-call SQL
    round-trip on this per-LLM-turn hot path; it is loaded lazily otherwise.

    Sibling of :func:`get_user_access_token_classified` sharing its result
    vocabulary, cache table, locks, and backoff — but "refresh" here is a
    mint from the user's single captured credential, so:

    - a MISSING cache row is normal (first use mints; no consent
      prerequisite);
    - ``kind="missing"`` means the *credential* is absent → the dispatcher's
      consent path prompts a re-login rather than a per-server consent;
    - a PERMANENT mint rejection (dead grant / missing tenant grant /
      Conditional Access challenge) drops only the cache row via
      :func:`_revoke_after_refresh_failure` — the shared credential is never
      auto-deleted, so one mis-granted server can't lock the user out of the
      rest.

    Locking: outer per-(user, server) asyncio lock (same map as oauth_user),
    then a per-(user, issuer) asyncio + cluster advisory lock pair keyed
    ``__obo__:<issuer>`` — the credential is the shared mutable resource
    (rotation write-back), so concurrent mints for DIFFERENT servers
    serialize cluster-wide on the credential, single-flighting redemptions
    even where the IdP rotates strictly.  Order is always server → credential
    and nothing acquires the reverse, so the pair cannot deadlock.
    """
    token_store: MCPTokenStore | None = getattr(app_state, "mcp_token_store", None)
    if token_store is None:
        log.debug("mcp_server.oauth.token_store_unconfigured")
        return TokenLookupResult(kind="missing")

    # Resolve the server row + audience FIRST (callers on the dispatch/priming
    # path pass server_row, so this is normally no SQL) — the fast-path cache
    # serve must validate the row's audience against the CURRENT one, so it can't
    # run before the audience is known.
    storage = _get_storage(app_state)
    if storage is None:
        return _no_token_result(app_state, user_id, server_name, TokenLookupResult(kind="missing"))
    if server_row is None:
        server_row = await asyncio.to_thread(storage.get_mcp_server_by_name, server_name)
    if server_row is None:
        return _no_token_result(app_state, user_id, server_name, TokenLookupResult(kind="missing"))
    server_id_for_audit = str(server_row.get("server_id") or "")

    oidc_config = getattr(app_state, "oidc_config", None)
    profile = str(getattr(oidc_config, "obo_grant_profile", "") or "")
    audience = str(server_row.get("oauth_audience") or "")
    scopes = str(server_row.get("oauth_scopes") or "")
    # Scope the freshness gate + cache row to what the leg ACTUALLY mints, not to
    # the configured column: the entra leg pins ``<audience>/.default`` and
    # ignores oauth_scopes (an inert leftover after an rfc8693→entra profile
    # switch). Recording the configured "Files.Read" there would make
    # _is_fresh_obo_cache_row keep serving the broad ``.default`` bearer while
    # believing it is narrow. rfc8693 DOES apply the scope, so there the two are
    # the same. The RAW ``scopes`` is still passed to the mint below so the entra
    # leg's once-per-audience "oauth_scopes ignored" warning still surfaces the
    # misconfigured leftover to operators.
    effective_scopes = "" if profile == "entra" else scopes
    mint = _OBO_MINT_LEGS.get(profile)

    try:
        plain = await asyncio.to_thread(token_store.get_user_token, user_id, server_name)
    except MCPTokenDecryptError as exc:
        return _decrypt_failure_result(
            app_state,
            user_id,
            server_name,
            exc,
            event="mcp_server.oauth.obo_cache_decrypt_failed",
        )
    # Serve the cache only when it is a fresh, right-audience, right-scopes,
    # refresh-less row (see _is_fresh_obo_cache_row). A stale-audience/-scopes or
    # refresh-bearing row falls through to a fresh mint (which overwrites it via
    # _persist_obo_cache_row).
    fresh = _is_fresh_obo_cache_row(plain, audience, effective_scopes)
    if fresh and not force_refresh and plain is not None:
        return _token_result(app_state, user_id, server_name, plain["access_token"])

    # Gate the cooldown short-circuit on actually needing a mint (cache absent or
    # stale), mirroring the oauth_user path: a force_refresh 401-retry on a
    # still-fresh cache must fall through to the locked re-read so it can pick up
    # a token a cluster-mate just minted, rather than fail transient in-cooldown.
    if not fresh and _refresh_in_cooldown(app_state, user_id, server_name):
        # Surface the classification that armed the cooldown: obo arms it on a
        # PERMANENT dead-grant too (its credential survives the per-server
        # revoke), and reporting that as a retryable "transient" for the whole
        # window would tell the user to retry a permanently-broken server and
        # flap against the honest re-login/admin affordance the mint returned.
        backoff = _refresh_backoff_state(app_state, user_id, server_name)
        if backoff.last_failure_permanent:
            return TokenLookupResult(kind="refresh_failed")
        return TokenLookupResult(kind="refresh_failed_transient")
    if oidc_config is not None and not getattr(oidc_config, "enabled", False):
        # A node that booted during a transient IdP outage carries
        # enabled=False with discovery_retryable=True; without a runtime
        # retry, every obo mint on this node would fail "transient" until an
        # operator restarts it (the login path's lazy retry covers only JWKS,
        # not discovery). Cooldown-gated and single-flight; a no-op when OIDC
        # is operator-disabled or the boot failure was a config rejection.
        # Lazy import: oidc.load_oidc_config imports OBO_GRANT_PROFILES from
        # this module (also lazily), so neither module may import the other
        # at module level.
        from turnstone.core.oidc import maybe_rediscover_oidc

        await maybe_rediscover_oidc(app_state)
        # Re-read only the DISCOVERY-derived state (enabled / token_endpoint) that
        # rediscovery can change. ``obo_grant_profile`` is a static config field
        # rediscovery never touches, so ``profile`` / ``mint`` computed above still
        # hold — recomputing them would be dead work implying the profile can
        # change across a heal (it cannot).
        oidc_config = getattr(app_state, "oidc_config", None)
    if (
        oidc_config is None
        or not getattr(oidc_config, "enabled", False)
        or not getattr(oidc_config, "token_endpoint", "")
        or mint is None
        or not audience
    ):
        # Operator-fixable configuration problem — loud log, no revoke, and a
        # retryable classification so fixing the config heals without a
        # re-consent round. Arm the cooldown so a misconfigured server on a busy
        # deployment doesn't emit an error line + SQL per dispatch: the check is
        # loud once per 30s window per (user, server), and a fixed config heals
        # on the next tick after the window lapses. (The write path also rejects
        # audience-less oauth_obo rows, so this branch is normally a typo'd
        # grant profile, not a common state.)
        _arm_cooldown(app_state, user_id, server_name)
        log.error(
            "mcp_server.oauth.obo_misconfigured",
            server_name=server_name,
            oidc_enabled=bool(oidc_config is not None and getattr(oidc_config, "enabled", False)),
            grant_profile=profile or "<unset>",
            has_audience=bool(audience),
        )
        return TokenLookupResult(kind="refresh_failed_transient")
    issuer = str(getattr(oidc_config, "issuer", ""))

    # Cheap pre-lock presence check: a raw existence read (NO decrypt) is enough
    # to short-circuit the common "no captured credential" case before taking
    # the pg advisory lock. The authoritative decrypt happens exactly once under
    # the lock (credential2 below), where decrypt_failure is already classified —
    # so the refresh token is never Fernet-decrypted twice per mint. When the
    # caller already established presence for this issuer (``credential_present``
    # — the priming path does one existence read for ALL of a user's obo servers)
    # this per-server read is skipped, so session-start priming doesn't re-read
    # the credential N times.
    if credential_present is None:
        credential_present = (
            await asyncio.to_thread(storage.get_oidc_user_credential, user_id, issuer) is not None
        )
    if not credential_present:
        # No captured credential → the consent affordance is a re-login.
        # kind="missing" here means DURABLY absent (no row, not a read
        # blip) — the obo credential gate in MCPClientManager
        # ``_prime_user_pools`` synthesizes this exact verdict for its
        # retained-catalog drops when it skips this lookup wholesale;
        # keep that site in lock-step if this classification splits.
        return _no_token_result(app_state, user_id, server_name, TokenLookupResult(kind="missing"))

    lock = _refresh_lock_for(app_state, user_id, server_name)
    credential_key = f"__obo__:{issuer}"
    credential_lock = _refresh_lock_for(app_state, user_id, credential_key)
    pg_lock = await _acquire_pg_refresh_lock(storage, user_id, credential_key)
    async with lock, credential_lock, pg_lock:
        # Race check: another caller may have minted for this server while we
        # waited (same two-condition reuse rule as the oauth_user path).
        try:
            plain2 = await asyncio.to_thread(token_store.get_user_token, user_id, server_name)
        except MCPTokenDecryptError as exc:
            # First read already logged obo_cache_decrypt_failed; this re-read
            # under the lock stays silent (event=None) to avoid a double line.
            return _decrypt_failure_result(app_state, user_id, server_name, exc, event=None)
        # Same servability gate as the pre-lock read (refresh-less, right-audience,
        # right-scopes, not-expired) — a stale row falls through and re-mints.
        if _is_fresh_obo_cache_row(plain2, audience, effective_scopes) and plain2 is not None:
            if not force_refresh:
                return _token_result(app_state, user_id, server_name, plain2["access_token"])
            # force_refresh means the caller's bearer was rejected; serialized
            # waiters must single-flight the re-mint (avoid N redundant IdP
            # redemptions) WITHOUT re-serving the very token that was just
            # rejected. Distinguish by token IDENTITY, not mint time: the pre-lock
            # ``plain`` is the token this caller came in with (the rejected one);
            # if the under-lock row now holds a DIFFERENT token, a concurrent
            # waiter re-minted while we waited — reuse it. If it is the SAME
            # token, nothing has changed, so fall through and re-mint. (Mint time
            # can't distinguish these at the cache row's 1-second ``created``
            # granularity — a same-second own-mint would read as "fresh".)
            pre_lock_token = plain["access_token"] if plain is not None else None
            if plain2["access_token"] != pre_lock_token:
                return _token_result(app_state, user_id, server_name, plain2["access_token"])

        # Re-read the credential under the lock — a concurrent mint for a
        # different server may have rotated it; always redeem the newest.
        credential2 = await _read_obo_credential(
            app_state, token_store, user_id, server_name, issuer
        )
        if isinstance(credential2, TokenLookupResult):
            return credential2  # missing or decrypt_failure

        # The mint leg persists any credential-RT rotation the instant it obtains
        # one, via this callback under the held credential lock — so on rfc8693 a
        # rotation from the refresh leg survives an exchange-leg failure, and an
        # audience-scoped exchange RT never reaches the credential.
        async def _persist_rotation(new_credential_rt: str) -> None:
            # Swallow storage failures: the mint itself succeeded, so the
            # caller still gets a working access token. On a strict-rotation
            # IdP the stored credential may now hold a consumed RT — the NEXT
            # mint then fails invalid_grant and surfaces the re-login rail.
            # Raising here would be strictly worse: the rotated RT is lost
            # either way, and a raw storage exception would additionally
            # escape the classified-result contract (only
            # MCPOAuthRefreshFailed is caught around mint()) and break the
            # in-flight dispatch too.
            try:
                await asyncio.to_thread(
                    token_store.update_oidc_credential_after_redeem,
                    user_id,
                    issuer,
                    refresh_token=new_credential_rt,
                    # Value CAS against the RT this mint read: skips the write if
                    # a concurrent login capture already refreshed the credential
                    # (see update_oidc_credential_after_redeem), so a rotation
                    # can't clobber a fresh login token.
                    expected_current=credential2["refresh_token"],
                )
            except Exception:
                log.error(
                    "mcp_server.oauth.obo_rotation_persist_failed",
                    user_id=user_id,
                    server_name=server_name,
                    exc_info=True,
                )

        # The login flow's oidc_http_client lives on the uvicorn loop; this
        # function runs on the MCP loop thread. httpx pools connections per
        # client object and a pooled connection is bound to the loop that
        # created it, so sharing the login client here collides routinely —
        # the login exchange and the first mint hit the same IdP origin
        # seconds apart by design. No long-lived mint client is kept: a fresh
        # one is opened per mint below (mints are ~hourly per (user, server),
        # not hot-path). ``obo_http_client`` is an injection seam for tests /
        # e2e harnesses; when unset a per-mint client is created here.
        injected_client: httpx.AsyncClient | None = getattr(app_state, "obo_http_client", None)
        # One client for the whole mint: the rfc8693 leg makes TWO POSTs to the
        # same token endpoint, so a per-request transient would pay two TLS
        # handshakes. When no client is injected (production), open a single
        # transient here and thread it into both legs so the exchange leg reuses
        # the refresh leg's pooled connection.
        try:
            async with contextlib.AsyncExitStack() as mint_stack:
                mint_client: httpx.AsyncClient
                if injected_client is not None:
                    mint_client = injected_client
                else:
                    mint_client = await mint_stack.enter_async_context(
                        httpx.AsyncClient(timeout=_DEFAULT_HTTP_TIMEOUT)
                    )
                tokens = await mint(
                    oidc_config=oidc_config,
                    credential_refresh_token=credential2["refresh_token"],
                    audience=audience,
                    scopes=scopes,
                    http_client=mint_client,
                    persist_rotation=_persist_rotation,
                )
        except MCPOAuthRefreshFailed as exc:
            return await _handle_refresh_failure(
                exc,
                app_state=app_state,
                user_id=user_id,
                server_name=server_name,
                server_id_for_audit=server_id_for_audit,
                token_store=token_store,
                revoke_on_failure=True,
                revoke_ambiguous_escalation=revoke_ambiguous_escalation,
                events=_OBO_MINT_FAILURE_EVENTS,
                permanent_reason="obo_mint_rejected",
                escalation_reason="obo_mint_ambiguous_escalated",
                # The shared credential survives a per-server revoke, so arm the
                # cooldown as the terminal backstop (else every re-dispatch would
                # re-run the doomed redemption + emit another token_revoked row).
                arm_cooldown_on_permanent=True,
            )

        access_token = tokens.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            backoff = _arm_cooldown(app_state, user_id, server_name)
            # A malformed 200 is a clean transient (not a dead grant): reset the
            # ambiguous streak, matching the sibling's _refresh_and_persist path.
            backoff.ambiguous_streak = 0
            log.warning(
                "mcp_server.oauth.obo_mint_missing_access_token",
                user_id=user_id,
                server_name=server_name,
            )
            return TokenLookupResult(kind="refresh_failed_transient")

        # Credential rotation was already persisted by the mint leg (see
        # _persist_rotation); the cache write is the only remaining step. The
        # expiry is never NULL for an obo row (see _OBO_DEFAULT_TTL_SECONDS): a
        # missing expires_in falls back to a conservative default so the
        # freshness gate can never serve a short-lived minted token forever.
        #
        # Best-effort: the mint already SUCCEEDED and ``access_token`` is a
        # working bearer for THIS dispatch. A transient storage error on the
        # cache write (delete+create) must not discard that token or escape the
        # classified-result contract as a raw exception — return the token and
        # let the next dispatch re-mint (the un-written cache row just means one
        # extra mint, not a failed tool call).
        try:
            await _persist_obo_cache_row(
                token_store,
                user_id,
                server_name,
                access_token=access_token,
                expires_at=_expires_at_from_response(
                    tokens, default_ttl_seconds=_OBO_DEFAULT_TTL_SECONDS
                ),
                # Record the EFFECTIVE scope the leg minted (see effective_scopes),
                # so the freshness gate compares like-for-like and never serves a
                # broad entra .default token believing it is a narrow configured one.
                scopes=effective_scopes,
                issuer=issuer,
                audience=audience,
            )
        except Exception:
            log.warning(
                "mcp_server.oauth.obo_cache_persist_failed",
                user_id=user_id,
                server_name=server_name,
                exc_info=True,
            )
        return _token_result(app_state, user_id, server_name, access_token)


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

    # RFC 6749 section 6 — the AS MAY omit ``refresh_token`` from the refresh
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


def _expires_at_from_response(
    tokens: dict[str, Any], *, default_ttl_seconds: int | None = None
) -> str | None:
    """Convert an AS ``expires_in`` to an ISO timestamp.

    Accepts int, float, or string-serialised numerics — some real ASes
    return ``"3600"`` (string), some return ``3600.0`` (float). Returns
    ``None`` when the field is missing, malformed, or non-positive — UNLESS
    *default_ttl_seconds* is given, in which case that fallback lifetime is
    used (the obo mint path passes ``_OBO_DEFAULT_TTL_SECONDS`` so a minted
    row is never cached with a NULL, read-as-never-expiring expiry). One owner
    of the stored-expiry timestamp format.
    """
    expires_in = tokens.get("expires_in")
    seconds: int | None
    if isinstance(expires_in, bool):
        # ``bool`` is a subclass of ``int`` — reject explicitly so True
        # doesn't silently parse as 1 second.
        seconds = None
    elif isinstance(expires_in, int):
        seconds = expires_in
    elif isinstance(expires_in, float):
        seconds = int(expires_in)
    elif isinstance(expires_in, str):
        try:
            seconds = int(float(expires_in))
        except (TypeError, ValueError):
            seconds = None
    else:
        seconds = None
    if seconds is None or seconds <= 0:
        if default_ttl_seconds is None:
            return None
        seconds = default_ttl_seconds
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


async def emit_oauth_failure_audit(
    *,
    app_state: Any,
    user_id: str,
    server_name: str,
    server_row: dict[str, Any],
    kind: str,
    code: str,
    scopes: tuple[str, ...] = (),
) -> None:
    """Emit an ``mcp_server.oauth.*_emitted`` audit event for a 403 classification.

    The audit ``action`` is selected from ``code`` so downstream
    alerting / analytics can filter on a label that matches reality:

    - ``mcp_insufficient_scope`` →
      ``mcp_server.oauth.insufficient_scope_emitted``
    - ``mcp_tool_call_forbidden`` / ``mcp_resource_read_forbidden`` /
      ``mcp_prompt_get_forbidden`` →
      ``mcp_server.oauth.forbidden_emitted``

    Best-effort: :func:`_audit_event` already swallows storage / write
    failures internally so audit emission never breaks dispatch.
    Operators tracking step-up patterns and forbidden-policy hits
    consume this via the standard audit log. Called by the pool
    dispatcher after classifying a 403 — both
    ``WWW-Authenticate: error="insufficient_scope"`` and the generic
    forbidden branch route here so cross-tenant probing leaves an
    audit trail (Phase 7 left the generic 403 branch silent; Phase 7b
    closes that gap).

    The ``kind`` ("tool" / "resource" / "prompt") and ``code`` fields
    land in the audit detail so operators can distinguish tool-call vs
    resource-read vs prompt-get 403s for the same ``(user, server)``
    even within a single ``action`` bucket.
    """
    if app_state is None:
        return
    server_id = str(server_row.get("server_id") or "") if server_row else ""
    action = (
        "mcp_server.oauth.insufficient_scope_emitted"
        if code == "mcp_insufficient_scope"
        else "mcp_server.oauth.forbidden_emitted"
    )
    await _audit_event(
        app_state,
        server_id=server_id,
        user_id=user_id,
        action=action,
        server_name=server_name,
        detail={"scopes_required": list(scopes), "kind": kind, "code": code},
    )


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

    # Optional ``scopes`` query param — caller-supplied step-up scopes.
    # Validated against the RFC 6749 §3.3 grammar so a malicious or buggy
    # client can't smuggle CR/LF/tab/control bytes through the AS round-
    # trip into downstream log or notification paths. The cap matches
    # the per-call ceiling used in the WWW-Authenticate parser via the
    # shared ``MAX_INSUFFICIENT_SCOPE_REPORTED`` constant in
    # ``mcp_http_parsers``; over-capped input is rejected loudly so
    # callers don't silently lose state.
    #
    # Splitting on a single space (NOT ``str.split()``) is intentional:
    # Python's whitespace split would silently strip embedded CR/LF/tab,
    # masking hostile input that the grammar predicate is supposed to
    # catch.
    requested_scopes_raw = request.query_params.get("scopes", "")
    requested_scopes: list[str] = []
    if requested_scopes_raw:
        candidates = [tok for tok in requested_scopes_raw.split(" ") if tok]
        if len(candidates) > MAX_INSUFFICIENT_SCOPE_REPORTED:
            return JSONResponse({"error": "Invalid scope token"}, status_code=400)
        for tok in candidates:
            if not is_valid_scope_token(tok):
                return JSONResponse({"error": "Invalid scope token"}, status_code=400)
        requested_scopes = candidates

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
    configured_scopes = str(server_row.get("oauth_scopes") or "")
    if requested_scopes:
        # Union: configured scopes + caller-supplied step-up scopes, deduped
        # and sorted so the AS sees a stable string regardless of caller
        # ordering (cache-key stability, deterministic audit detail).
        merged = set(configured_scopes.split()) | set(requested_scopes)
        merged.discard("")
        scopes = " ".join(sorted(merged))
    else:
        scopes = configured_scopes
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

    # Prime the per-user pool so the just-consented server's tools populate
    # into this user's catalog immediately. Without this, oauth_user tools
    # are discovered only lazily on first dispatch — but the agent can't
    # emit a call for a tool it can't yet see, so the catalog stays empty
    # and the server is stuck "connecting". Best-effort: a prime failure
    # does not change consent success; lazy dispatch remains the backstop.
    mcp_client = getattr(request.app.state, "mcp_client", None)
    if mcp_client is not None and hasattr(mcp_client, "schedule_prime_user_server"):
        # Fire-and-forget so the consent redirect is not held on a slow or
        # unreachable MCP server; the warm runs on the mcp-loop in the
        # background and live sessions pick up the catalog via the listeners.
        mcp_client.schedule_prime_user_server(
            user_id=user_id,
            server_name=server_name,
            access_token=access_token,
            server_row=server_row,
        )

    # Phase 9 — clear any deferred-consent records for this (user,
    # server) now that consent has completed.  Best-effort: a storage
    # failure here doesn't change the user-observable callback success;
    # the worst case is a stale badge that the user can dismiss
    # manually.  ``delete_mcp_pending_consent`` returns False on
    # no-such-row (the common case for interactive consent flows that
    # never deferred), which is fine.
    try:
        await asyncio.to_thread(storage.delete_mcp_pending_consent, user_id, server_name)
    except Exception:
        log.debug(
            "mcp_server.oauth.pending_consent_clear_failed",
            server_name=server_name,
            exc_info=True,
        )

    return RedirectResponse(pending["return_url"] or "/", status_code=302)


def obo_server_names(storage: StorageBackend) -> set[str]:
    """Names of all ``auth_type='oauth_obo'`` MCP servers (raises on storage error).

    One definition of "which servers are sign-in passthrough", shared by the
    connections-list filter (which hides obo cache rows) and the identity-delete
    cache purge, so a change to how obo is recognised — or a second passthrough
    auth type — can't leave one path silently missing servers (which would
    expose obo rows in the connections list, or leave a deprovisioned user's
    minted-token cache un-purged). Callers wrap their own try/except so each
    keeps its context-specific fail-open logging.
    """
    return {
        str(row.get("name") or "")
        for row in storage.list_mcp_servers()
        if str(row.get("auth_type") or "") == "oauth_obo"
    }


async def handle_mcp_oauth_list_connections(request: Request) -> Response:
    """``GET /v1/api/mcp/oauth/connections``.

    Lists the authenticated user's MCP server consents. Returns the
    non-secret projection (no access/refresh ciphertext) so the
    settings UI can render a connections list without ever pulling
    decrypt material out of storage.
    """
    return _apply_security_headers(await _handle_mcp_oauth_list_connections_inner(request))


async def _handle_mcp_oauth_list_connections_inner(request: Request) -> Response:
    from starlette.responses import JSONResponse

    token_store = getattr(request.app.state, "mcp_token_store", None)
    if token_store is None:
        return _no_token_store_response("connections")

    user_id = _require_user_id(request)
    if user_id is None:
        return JSONResponse({"error": "Authentication required"}, status_code=401)

    rows = await asyncio.to_thread(token_store.list_user_token_metadata, user_id)
    # Hide oauth_obo mint-cache rows: they are cache artifacts of sign-in
    # passthrough, not per-server consents. Listing them offered a
    # "Disconnect" that silently undid itself — the row deletes, then
    # session-start priming re-mints from the surviving captured credential —
    # so the connections list shows only rows the user can actually revoke.
    # Classify by the authoritative server ``auth_type`` (one read on this cold
    # settings-page path) rather than inferring obo from a NULL refresh token:
    # the auth_type is the source of truth, and a token-shape heuristic would
    # silently hide any oauth_user row that ever lacked a refresh token.
    # Fail open on a server-list read error: worst case an obo row renders
    # and the revoke endpoint below still refuses it honestly.
    storage = _get_storage(request.app.state)
    obo_names: set[str] = set()
    if storage is not None:
        try:
            obo_names = await asyncio.to_thread(obo_server_names, storage)
        except Exception:
            obo_names = set()
    return JSONResponse({"connections": [r for r in rows if r["server_name"] not in obo_names]})


async def handle_mcp_oauth_revoke_connection(request: Request) -> Response:
    """``DELETE /v1/api/mcp/oauth/connections/{server_name}``.

    Best-effort RFC 7009 upstream revoke followed by the authoritative
    local delete. Cross-user attempts return 404 with the same body
    shape as a never-existed row to avoid leaking tenant existence.
    Pool sessions for the (user, server) pair are evicted so any
    in-flight dispatch reconnects with a fresh token at next call.
    """
    return _apply_security_headers(await _handle_mcp_oauth_revoke_connection_inner(request))


# Strong refs to in-flight upstream-revoke tasks. asyncio holds tasks via
# a WeakSet; a fire-and-forget ``loop.create_task`` whose handle isn't
# stored can be GC'd before the AS round-trip completes. Tasks register
# here on creation and discard themselves on completion via
# ``add_done_callback`` — same pattern as ``_pg_refresh_drain_tasks``.
_revoke_upstream_tasks: set[asyncio.Task[None]] = set()

# Soft cap on concurrent in-flight upstream revokes. A coordinated mass
# revoke (admin sweep, scripted cleanup, compromised account) could pile
# up arbitrarily many tasks each pinning storage / token_store / server_row
# / refresh-token plaintext until the AS round-trip completes (~30s
# worst case). When the set is full, the local delete still runs and
# the audit row records ``upstream_revoke_outcome="shed_by_cap"``; the
# operator can re-run revokes against any straggling AS-side tokens once
# the queue drains.
_REVOKE_UPSTREAM_TASKS_MAX = 256


async def _attempt_upstream_revoke(
    *,
    http_client: httpx.AsyncClient,
    metadata_cache: dict[str, Any] | None,
    storage: StorageBackend,
    token_store: MCPTokenStore,
    server_name: str,
    server_row: dict[str, Any],
    server_id_for_audit: str,
    refresh_token: str,
) -> None:
    """Best-effort RFC 7009 upstream revoke for ``user_revoked`` flow.

    Designed to be fired from :func:`asyncio.create_task` so the caller's
    204 isn't gated on the AS round-trip — the local delete is
    authoritative for this deployment, and the AS-side state is best-
    effort. Never raises. Each terminal state emits a structured log so
    operators can audit AS-side outcomes without parsing exception text:
    ``revoke_token_at_as`` logs ``revocation_succeeded`` /
    ``revocation_failed`` / ``revocation_unsupported`` on its branches;
    discovery failures emit ``upstream_revoke_discovery_failed``; an
    unexpected exception in the outer block emits
    ``upstream_revoke_failed``.

    The outer ``try/except Exception`` is load-bearing: this helper is
    fired as a background task whose handle goes into ``_revoke_upstream_tasks``
    with a ``set.discard`` done-callback that does NOT consume
    ``task.exception()``. An unhandled exception here would surface as
    ``Task exception was never retrieved`` from asyncio's default handler.
    Catching at the outer boundary keeps the helper's contract honest.
    Bearer-leak invariant: ``exc_info=True`` is forbidden on this path —
    the chained ``__context__`` may carry an ``httpx.Request`` whose
    ``Authorization`` header holds the per-user bearer.
    """
    try:
        try:
            as_metadata = await discover_authorization_server(
                server_name=server_name,
                server_url=str(server_row.get("url") or ""),
                override_url=server_row.get("oauth_authorization_server_url") or None,
                cached_issuer=server_row.get("oauth_as_issuer_cached") or None,
                http_client=http_client,
                storage=storage,
                server_id=server_id_for_audit,
                trusted_hosts=frozenset(),
                metadata_cache=metadata_cache,
            )
        except MCPOAuthDiscoveryError as exc:
            log.info(
                "mcp_server.oauth.upstream_revoke_discovery_failed",
                server_name=server_name,
                error=type(exc).__name__,
            )
            return
        # When the AS doesn't advertise a revocation_endpoint,
        # ``revoke_token_at_as`` itself logs ``revocation_unsupported``
        # and returns — no need for a redundant gate here. Letting the
        # call through keeps the observability story uniform.
        client_id = str(server_row.get("oauth_client_id") or "")
        client_secret: str | None = None
        if server_id_for_audit:
            client_secret_ct = await asyncio.to_thread(
                storage.get_mcp_oauth_client_secret_ct, server_id_for_audit
            )
            if client_secret_ct is not None:
                try:
                    client_secret = token_store.cipher.decrypt(client_secret_ct).decode("utf-8")
                except MCPTokenDecryptError:
                    client_secret = None
        # ``revoke_token_at_as`` never raises and never logs ``exc_info=True``;
        # the AS round-trip is fire-and-don't-care from the caller's vantage.
        await revoke_token_at_as(
            as_metadata=as_metadata,
            http_client=http_client,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
        )
    except Exception as exc:
        log.info(
            "mcp_server.oauth.upstream_revoke_failed",
            server_name=server_name,
            error=type(exc).__name__,
        )


async def _handle_mcp_oauth_revoke_connection_inner(request: Request) -> Response:
    from starlette.responses import JSONResponse, Response

    token_store = getattr(request.app.state, "mcp_token_store", None)
    if token_store is None:
        return _no_token_store_response("revoke_connection")

    user_id = _require_user_id(request)
    if user_id is None:
        return JSONResponse({"error": "Authentication required"}, status_code=401)

    server_name = request.path_params.get("server_name", "").strip()
    if not server_name:
        return JSONResponse({"error": "Missing server_name"}, status_code=400)

    storage = _get_storage(request.app.state)
    if storage is None:
        return JSONResponse({"error": "Storage unavailable"}, status_code=503)

    # Decrypt is best-effort: we can't perform an upstream revoke without
    # the plaintext refresh token, but the local delete is authoritative
    # so the consent is invalidated either way. Decrypt failure here is
    # not a hard error — the operator can still revoke locally.
    plain: Any = None
    try:
        plain = await asyncio.to_thread(token_store.get_user_token, user_id, server_name)
    except MCPTokenDecryptError:
        plain = None

    # Distinguish "row missing" from "decrypt failed" — a missing row
    # surfaces as 404 (with the same shape used for cross-user attempts
    # so existence is not leaked across tenants).
    if plain is None:
        storage_row = await asyncio.to_thread(storage.get_mcp_user_token, user_id, server_name)
        if storage_row is None:
            return JSONResponse({"error": "No such connection"}, status_code=404)

    server_row = await asyncio.to_thread(storage.get_mcp_server_by_name, server_name)
    server_id_for_audit = ""
    if server_row is not None:
        server_id_for_audit = str(server_row.get("server_id") or "")

    if server_row is not None and str(server_row.get("auth_type") or "") == "oauth_obo":
        # Sign-in passthrough rows are mint-cache, not per-server consent:
        # deleting the row here would 204, audit token_revoked, and then
        # session-start priming would silently re-mint from the surviving
        # captured credential — a "disconnect" that undoes itself. Refuse
        # honestly instead (the admin bulk path exposes the same truth as
        # effect=cache_flush_remints; removing the sign-in credential is the
        # real revocation lever). The listing endpoint hides these rows, so
        # this is a backstop for direct API calls.
        return JSONResponse(
            {
                "error": (
                    "This server uses your Turnstone sign-in, not a per-server "
                    "connection — there is nothing to disconnect here. Access "
                    "ends when your sign-in credential is removed or an "
                    "administrator disables the server."
                )
            },
            status_code=409,
        )

    # Local delete — authoritative. Even if the upstream revoke fails or
    # is unsupported, the consent is invalidated for this deployment.
    # Run BEFORE the AS round-trip so the user-visible 204 isn't gated
    # on a slow / unreachable AS.
    await asyncio.to_thread(token_store.delete_user_token, user_id, server_name)
    _drop_refresh_lock(request.app.state, user_id, server_name)

    # Best-effort pool eviction so any in-flight session backed by the
    # now-deleted row is closed before the next dispatch.
    mcp_client = getattr(request.app.state, "mcp_client", None)
    if mcp_client is not None and hasattr(mcp_client, "evict_user_session"):
        try:
            mcp_client.evict_user_session(user_id, server_name)
        except Exception as exc:
            # Best-effort: a closed loop or transient scheduling error
            # must not block the user-visible 204. Type name only — the
            # exception's chain may carry token-bearing context.
            log.info(
                "mcp_server.oauth.evict_user_session_failed",
                user_id=user_id,
                server_name=server_name,
                error=type(exc).__name__,
            )

    # Schedule the upstream RFC 7009 revoke as a fire-and-forget task so
    # the response isn't gated on the AS round-trip. ``upstream_revoke_outcome``
    # is the categorical audit field — operators can distinguish the
    # four terminal states (scheduled, no_refresh_token, no_http_client,
    # shed_by_cap) without parsing log streams.
    refresh_token_for_revoke: str | None = plain.get("refresh_token") if plain is not None else None
    if not refresh_token_for_revoke or server_row is None:
        upstream_revoke_outcome = "no_refresh_token"
    else:
        http_client = getattr(request.app.state, "mcp_oauth_http_client", None)
        if http_client is None:
            upstream_revoke_outcome = "no_http_client"
        elif len(_revoke_upstream_tasks) >= _REVOKE_UPSTREAM_TASKS_MAX:
            # Soft-cap shed: the local delete already ran (authoritative);
            # surface the dropped attempt in the audit detail so an
            # operator can re-run revokes once the queue drains.
            log.info(
                "mcp_server.oauth.upstream_revoke_shed",
                server_name=server_name,
                in_flight=len(_revoke_upstream_tasks),
                cap=_REVOKE_UPSTREAM_TASKS_MAX,
            )
            upstream_revoke_outcome = "shed_by_cap"
        else:
            metadata_cache = getattr(request.app.state, "mcp_oauth_metadata_cache", None)
            task = asyncio.create_task(
                _attempt_upstream_revoke(
                    http_client=http_client,
                    metadata_cache=metadata_cache,
                    storage=storage,
                    token_store=token_store,
                    server_name=server_name,
                    server_row=server_row,
                    server_id_for_audit=server_id_for_audit,
                    refresh_token=refresh_token_for_revoke,
                ),
                name="mcp-oauth-upstream-revoke",
            )
            _revoke_upstream_tasks.add(task)
            task.add_done_callback(_revoke_upstream_tasks.discard)
            upstream_revoke_outcome = "scheduled"

    await _audit_event(
        request.app.state,
        server_id=server_id_for_audit,
        user_id=user_id,
        action="mcp_server.oauth.token_revoked",
        server_name=server_name,
        detail={
            "reason": "user_revoked",
            "upstream_revoke_outcome": upstream_revoke_outcome,
        },
    )

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Pending-consent endpoints (Phase 9)
# ---------------------------------------------------------------------------


async def handle_mcp_oauth_list_pending(request: Request) -> Response:
    """``GET /v1/api/mcp/oauth/pending``.

    Returns the authenticated user's deferred-consent records — populated
    by the pool dispatchers when a non-interactive run (scheduled /
    channel) hits ``mcp_consent_required`` or ``mcp_insufficient_scope``.
    Used by the dashboard badge to surface deferred consent needs on
    next login.

    Install-level gate: when no ``mcp_servers`` row has
    ``auth_type='oauth_user'``, the entire feature is dark — we
    short-circuit to ``{pending: 0, servers: []}`` without querying the
    pending table at all.  This keeps local-auth installs on a
    zero-new-storage-query path.
    """
    return _apply_security_headers(await _handle_mcp_oauth_list_pending_inner(request))


_INSTALL_GATE_CACHE_TTL_S = 60.0


async def _install_gate_passes(app_state: Any, storage: Any) -> bool:
    """Cached install-level gate for OAuth-MCP features.

    Returns True iff at least one ``mcp_servers`` row has
    ``auth_type='oauth_user'``.  Result is cached on ``app_state`` for
    :data:`_INSTALL_GATE_CACHE_TTL_S` seconds — admin-rare transitions
    don't justify a per-request DB round-trip on every dashboard load.

    Reset semantics: cache is invalidated by time only.  Operators who
    just enabled an ``oauth_user`` row see the gate flip within the TTL
    window.  False positives (cache says True but the row was just
    deleted) are bounded by the same window — the downstream list
    query already filters by user, so the cost is at most one cheap
    user-scoped read.
    """
    now = time.monotonic()
    cached = getattr(app_state, "_mcp_install_gate_cache", None)
    if cached is not None:
        cached_value, cached_at = cached
        if (now - cached_at) < _INSTALL_GATE_CACHE_TTL_S:
            return bool(cached_value)
    value = bool(await asyncio.to_thread(storage.any_user_scoped_mcp_servers))
    app_state._mcp_install_gate_cache = (value, now)
    return value


async def _handle_mcp_oauth_list_pending_inner(request: Request) -> Response:
    from starlette.responses import JSONResponse

    user_id = _require_user_id(request)
    if user_id is None:
        return JSONResponse({"error": "Authentication required"}, status_code=401)

    storage = _get_storage(request.app.state)
    if storage is None:
        return JSONResponse({"pending": 0, "servers": []})

    if not await _install_gate_passes(request.app.state, storage):
        return JSONResponse({"pending": 0, "servers": []})

    rows = await asyncio.to_thread(storage.list_mcp_pending_consent_by_user, user_id)
    return JSONResponse({"pending": len(rows), "servers": list(rows)})


async def handle_mcp_oauth_clear_pending(request: Request) -> Response:
    """``DELETE /v1/api/mcp/oauth/pending/{server_name}``.

    Manual user-initiated dismissal of a single deferred-consent record.
    Called from the dashboard settings modal when the user opts to clear
    the entry without completing consent (e.g., the underlying
    auth_type was changed and the deferred record is now stale).

    Returns 204 in both the existed-and-deleted and never-existed cases
    to keep cross-tenant existence non-observable.
    """
    return _apply_security_headers(await _handle_mcp_oauth_clear_pending_inner(request))


async def _handle_mcp_oauth_clear_pending_inner(request: Request) -> Response:
    from starlette.responses import JSONResponse, Response

    user_id = _require_user_id(request)
    if user_id is None:
        return JSONResponse({"error": "Authentication required"}, status_code=401)

    server_name = request.path_params.get("server_name", "").strip()
    if not server_name:
        return JSONResponse({"error": "Missing server_name"}, status_code=400)

    storage = _get_storage(request.app.state)
    if storage is None:
        return JSONResponse({"error": "Storage unavailable"}, status_code=503)

    cleared = bool(
        await asyncio.to_thread(storage.delete_mcp_pending_consent, user_id, server_name)
    )
    # Audit even on no-op deletes (returns 204 either way for cross-tenant
    # non-observability) so an attacker who tries to scrub deferred-consent
    # breadcrumbs leaves an audit trail of the attempts.
    await _audit_event(
        request.app.state,
        user_id=user_id,
        action="mcp_server.oauth.pending_consent_dismissed",
        server_name=server_name,
        detail={"mode": "single", "cleared": 1 if cleared else 0},
    )
    return Response(status_code=204)


async def handle_mcp_oauth_clear_all_pending(request: Request) -> Response:
    """``DELETE /v1/api/mcp/oauth/pending``.

    Bulk dismiss of every deferred-consent record for the authenticated
    user.  Returns the count cleared so the dashboard can update its
    badge in one round-trip.
    """
    return _apply_security_headers(await _handle_mcp_oauth_clear_all_pending_inner(request))


async def _handle_mcp_oauth_clear_all_pending_inner(request: Request) -> Response:
    from starlette.responses import JSONResponse

    user_id = _require_user_id(request)
    if user_id is None:
        return JSONResponse({"error": "Authentication required"}, status_code=401)

    storage = _get_storage(request.app.state)
    if storage is None:
        return JSONResponse({"error": "Storage unavailable"}, status_code=503)

    cleared = await asyncio.to_thread(storage.delete_all_mcp_pending_consent_by_user, user_id)
    await _audit_event(
        request.app.state,
        user_id=user_id,
        action="mcp_server.oauth.pending_consent_dismissed",
        server_name="(bulk)",
        detail={"mode": "bulk", "cleared": cleared},
    )
    return JSONResponse({"cleared": cleared})


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
    "OBO_GRANT_PROFILES",
    "TokenLookupResult",
    "build_authorize_url",
    "close_mcp_oauth_state",
    "create_pending_state",
    "discover_authorization_server",
    "generate_pkce_pair",
    "get_obo_access_token_classified",
    "get_user_access_token",
    "get_user_access_token_classified",
    "handle_mcp_oauth_authorize",
    "handle_mcp_oauth_callback",
    "handle_mcp_oauth_clear_all_pending",
    "handle_mcp_oauth_clear_pending",
    "handle_mcp_oauth_list_connections",
    "handle_mcp_oauth_list_pending",
    "handle_mcp_oauth_revoke_connection",
    "initialize_mcp_oauth_state",
    "is_user_scoped_auth",
    "pop_pending_state",
    "revoke_token_at_as",
]
