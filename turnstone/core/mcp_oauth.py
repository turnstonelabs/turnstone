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
the read-current → exchange-at-AS → write-new sequence at two layers:
an outer ``asyncio.Lock`` (intra-process), then a Postgres advisory lock
(cluster-wide). The advisory lock is no-op on SQLite single-node deployments
via :meth:`StorageBackend.acquire_advisory_lock_sync`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import ipaddress
import json
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast

import httpx

from turnstone.core.audit import record_audit
from turnstone.core.log import get_logger
from turnstone.core.mcp_http_parsers import (
    MAX_INSUFFICIENT_SCOPE_REPORTED,
    is_valid_scope_token,
    parse_www_authenticate_bearer,
)
from turnstone.core.oauth import grants as oauth_grants
from turnstone.core.oauth import http as oauth_http
from turnstone.core.oauth import locking as oauth_locking
from turnstone.core.oauth import oidc as oauth_oidc
from turnstone.core.oauth import ssrf as oauth_ssrf
from turnstone.core.oauth import tokens as oauth_tokens
from turnstone.core.oauth.context import OAuthContext, TokenCoordination, oauth_context
from turnstone.core.oauth.runtime import ensure_oauth_runtime
from turnstone.core.oauth.work import OAuthUnavailableError, durable_write
from turnstone.core.storage._protocol import USER_SCOPED_AUTH_TYPES
from turnstone.core.token_store import crypto as token_store_crypto
from turnstone.core.token_store import store as token_store_store

if TYPE_CHECKING:
    from collections.abc import Mapping

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


_PENDING_CLEANUP_INTERVAL_S = 60.0


# Limits on PRM/AS body sizes — defensive against runaway responses.
_MAX_DISCOVERY_BODY_BYTES = 256 * 1024


# Whole-discovery probe budget. Each candidate location gets the standard
# per-request timeout, but a hard-down host must not let a browser-facing
# consent handler (or a refresh holding the per-(user, server) lock) sit
# through one timeout per candidate.
_DISCOVERY_TOTAL_BUDGET = 20.0


# Appended to a private-address refusal so the operator learns the remedy from
# the error rather than from the source. Named for the setting, not the file:
# this one is DB-backed and takes effect on the next attempt.
# Operator-visible refusals travel through ``sanitize_log_text``, which caps
# at 200 characters and escapes non-ASCII, so a remedy appended to the tail of
# an SSRF message (already ~150 characters with the URL in it) is cut off
# before it is ever read. These are built remedy-first and short, with the
# underlying detail logged rather than rendered.
_PRIVATE_REMEDY_ENABLE_SETTING = (
    "enable mcp.oauth_allow_private_network in console settings to reach it"
)


_PRIVATE_REMEDY_SET_OVERRIDE = (
    "set this server's Authorization Server URL, which is the only way to name "
    "a private authorization server"
)


_PRIVATE_REMEDY_NEVER_FOLLOWED = (
    "the server pointed discovery at a private address of its own choosing, which is never followed"
)


def _private_network_refusal(
    what: str,
    exc: oauth_ssrf.OAuthSSRFPrivateAddressError,
    *,
    remedy: str,
    server_name: str = "",
) -> MCPOAuthDiscoveryError:
    """Build a short, remedy-first refusal and log the address detail.

    *remedy* is chosen by WHY the strict verdict was reached, not by the
    exception's class: telling an operator to enable a setting that is
    already on, and that could not have applied to this URL anyway, is
    worse than saying nothing.
    """
    log.warning(
        "mcp_server.oauth.private_address_refused",
        server_name=server_name,
        what=what,
        reason=oauth_ssrf.sanitize_log_text(str(exc)),
    )
    # ASCII separator: the operator-facing renderers unicode-escape, so an
    # em dash would reach the console as a literal "\u2014".
    return MCPOAuthDiscoveryError(f"{what} is on a private network - {remedy}")


def is_user_scoped_auth(auth_type: str | None) -> bool:
    """True when *auth_type* uses per-(user, server) connections and tokens."""
    return auth_type in USER_SCOPED_AUTH_TYPES


class MCPOAuthDiscoveryError(oauth_http.OAuthError):
    """Discovery (PRM, AS metadata) failed or returned unsuitable values."""


class MCPOAuthExchangeError(oauth_http.OAuthError):
    """Authorization-code exchange failed."""


# ---------------------------------------------------------------------------
# Authorization-server metadata
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


# ``urlsplit`` is non-validating: it silently DELETES tabs and newlines
# anywhere in the input and ignores leading control characters, so
# ``https://mcp.exa\nmple.com`` parses as ``https://mcp.example.com`` — a
# different host than the bytes say. Every identity check in this module
# compares parsed output, so an unusable identifier must be refused on the
# raw string before it is parsed, or the comparison is made against
# something the peer never sent. Space is included: a URI has none.
_URL_UNSAFE_CHARS = frozenset(chr(code) for code in range(0x21)) | {chr(0x7F)}


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin_key(url: str) -> tuple[str, str, int | None] | None:
    """Scheme, host and effective port for *url*, or ``None`` if unparseable.

    Compared by the parts rather than the raw authority: an operator's host
    typed one way and echoed back another — a different case, an explicit
    ``:443``, another spelling of the same IPv6 address — is the same origin,
    and treating it as a different one would refuse the very deployments this
    exists to serve. Total by construction: these URLs arrive from remote
    documents and headers, where ``urlsplit`` raises on a malformed address
    literal, and a parse failure must be an ordinary refusal rather than an
    exception escaping discovery.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not parts.scheme or not host:
        return None
    scheme = parts.scheme.lower()
    host = host.lower()
    with contextlib.suppress(ValueError):
        # ``2001:db8::1`` and ``2001:0DB8:0:0:0:0:0:1`` are one address; only
        # the parsed form says so, and an internal server is exactly where a
        # long-form literal turns up.
        host = ipaddress.ip_address(host).compressed
    return scheme, host, port if port is not None else _DEFAULT_PORTS.get(scheme)


def _same_origin(one: str, other: str) -> bool:
    """True when two URLs share a scheme, host and effective port.

    A URL that cannot be parsed is never the same origin as anything, so a
    caller gating an opt-in on this stays strict.
    """
    key = _origin_key(one)
    return key is not None and key == _origin_key(other)


def _has_unsafe_url_syntax(raw: str) -> bool:
    """True when *raw* holds a character ``urlsplit`` would drop or ignore."""
    return any(ch in _URL_UNSAFE_CHARS for ch in raw)


def canonical_resource(server_url: str, *, strict: bool = False) -> str:
    """Return the canonical RFC 8707 resource identifier for an MCP server URL.

    One definition feeds the three places the identifier is used: deriving
    the RFC 9728 protected-resource metadata URL, comparing that document's
    declared ``resource``, and the ``resource=`` parameter sent on every
    authorize, code-exchange, and refresh request. Reading the row value
    independently at each site is how they came to disagree.

    Applies only the normalization the specifications define for the
    identifier. Scheme and host are case-folded and the scheme's default
    port dropped (RFC 3986 §6.2.2.1 and §6.2.3; MCP authorization tells
    clients to accept uppercase scheme and host). A root-only ``/`` is
    removed (RFC 9728 §3.1 removes a terminating slash before inserting the
    well-known string; MCP prefers the form without it). Every other path
    is kept byte-for-byte, trailing slash included: it is also the
    connection path, and ``/MCP`` and ``/mcp`` are different resources. The
    query is kept.

    Surrounding whitespace, a fragment and embedded credentials have no
    place in a resource identifier (RFC 8707 §2 forbids the fragment; an
    audience value must never carry a credential). *strict* decides how
    they are handled:
    ``True`` at a write boundary, and for any identifier a remote server
    declares, raises :class:`ValueError`; the default strips them, so a row
    stored before this validation existed still resolves — and resolves to
    a more correct identifier than earlier releases sent. A missing scheme
    or host, or an invalid port, always raises.
    """
    if strict and server_url != server_url.strip():
        raise ValueError("server URL must not be surrounded by whitespace")
    candidate = server_url.strip()
    # Before parsing, and in both modes: a URL whose bytes do not survive
    # parsing intact is malformed, not merely unfashionable.
    if _has_unsafe_url_syntax(candidate):
        raise ValueError("server URL must not contain control characters or spaces")
    parsed = urllib.parse.urlsplit(candidate)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError(f"server URL is not absolute: {server_url}")
    if strict:
        # By the delimiter's presence, not by a non-empty component: a bare
        # trailing ``#`` is a fragment component, and RFC 8707 §2 forbids it.
        if "#" in candidate:
            raise ValueError("server URL must not contain a fragment")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("server URL must not contain embedded credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"server URL port is invalid: {server_url}") from exc
    scheme = parsed.scheme.lower()
    # ``hostname`` is already lower-cased and, for IPv6, stripped of its
    # brackets; the authority needs them back.
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    default_port = _DEFAULT_PORTS.get(scheme)
    netloc = host if port is None or port == default_port else f"{host}:{port}"
    path = "" if parsed.path == "/" else parsed.path
    return urllib.parse.urlunsplit((scheme, netloc, path, parsed.query, ""))


def oauth_allow_private_network(app_state: Any) -> bool:
    """Live read of ``mcp.oauth_allow_private_network``.

    Read per attempt, so an operator flipping it in the console applies to
    the next discovery on any surface whose store has the new value. A node
    learns it through the config-reload fan-out, which is best-effort: a
    node that missed it keeps the old value until it restarts, and the
    refusal will name a setting the console already shows as enabled.

    A surface with no ``config_store`` reads as not opted in. That is a
    conservative default rather than a statement about the surface: the CLI
    opens the same database the console writes this setting to, so a CLI
    that grew a store would pick the value up.
    """
    config_store = getattr(app_state, "config_store", None)
    if config_store is None:
        return False
    try:
        return bool(config_store.get("mcp.oauth_allow_private_network"))
    except Exception:
        # A settings read must never be the reason a token refresh fails.
        log.debug("mcp_server.oauth.private_network_setting_read_failed", exc_info=True)
        return False


def _canonical_server_url(server_row: Mapping[str, Any]) -> str:
    """Canonical resource identifier for a server row, in the flow's error type.

    Every flow reads the row once, here, and hands the canonical string to
    discovery, the token builders, and the audience check, so a stored
    value that predates canonicalization at the console boundary is still
    normalized exactly once. Lenient by design: a legacy row carrying a
    fragment or embedded credentials is healed rather than refused, since
    refusing here would strand a working server on a value the console
    accepted at the time. An empty URL passes through as ``""`` and is
    refused by PRM discovery or ignored by the token builders as before.
    """
    raw = str(server_row.get("url") or "")
    if not raw:
        return raw
    try:
        return canonical_resource(raw)
    except ValueError as exc:
        raise MCPOAuthDiscoveryError(f"server URL rejected: {exc}") from exc


def _well_known_url(identifier: str, suffix: str) -> str:
    """Insert a well-known suffix before an identifier's path.

    RFC 8414 and RFC 9728 use the same transformation: an identifier such
    as ``https://example.com/tenant/`` becomes
    ``https://example.com/.well-known/<suffix>/tenant/``.  The root-only
    slash is removed, while a resource path's trailing slash and query are
    significant and therefore preserved. Callers normalize identifiers whose
    own specification requires a trailing slash to be removed.
    """
    parsed = urllib.parse.urlsplit(identifier)
    identifier_path = "" if parsed.path == "/" else parsed.path
    metadata_path = f"/.well-known/{suffix}{identifier_path}"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, metadata_path, parsed.query, ""))


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


def _json_object_body(resp: httpx.Response, label: str) -> dict[str, Any]:
    """Return a bounded, parsed JSON-object body, or raise the discovery error."""
    if len(resp.content) > _MAX_DISCOVERY_BODY_BYTES:
        raise MCPOAuthDiscoveryError(f"{label} response body exceeds size limit")
    try:
        doc = resp.json()
    except ValueError as exc:
        raise MCPOAuthDiscoveryError(f"{label} body is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise MCPOAuthDiscoveryError(f"{label} body is not a JSON object")
    return doc


def _prm_issuer_from_document(resp: httpx.Response, accepted_resources: frozenset[str]) -> str:
    """Validate one protected-resource metadata response and return its issuer.

    Raises :class:`MCPOAuthDiscoveryError` for an oversized, non-JSON, or
    non-object body, a missing or invalid ``resource``, a ``resource`` that
    does not canonicalize to a member of *accepted_resources*, or a missing
    or empty ``authorization_servers``. Each cause has its own message
    because the operator's remedy differs: a missing field is the server's
    document, a mismatch is the row's URL.
    """
    doc = _json_object_body(resp, "PRM")

    resource = doc.get("resource")
    if not isinstance(resource, str) or not resource:
        raise MCPOAuthDiscoveryError("PRM document is missing its resource identifier")
    try:
        # Strict: the leniency that heals a row written before this validation
        # existed has no business accepting surrounding whitespace, a
        # fragment, or embedded credentials from a document a remote server
        # just handed us. RFC 9728 §3.3 wants the identifier discovery used,
        # and none of those forms is it.
        declared = canonical_resource(resource, strict=True)
    except ValueError as exc:
        raise MCPOAuthDiscoveryError(
            f"PRM resource identifier is not a valid resource URL: {exc}"
        ) from exc
    if declared not in accepted_resources:
        raise MCPOAuthDiscoveryError("PRM resource identifier does not match the server URL")

    auth_servers = doc.get("authorization_servers")
    if not isinstance(auth_servers, list) or not auth_servers:
        raise MCPOAuthDiscoveryError(
            "PRM document missing 'authorization_servers' or list is empty"
        )
    issuer_url = auth_servers[0]
    if not isinstance(issuer_url, str) or not issuer_url:
        raise MCPOAuthDiscoveryError("PRM authorization_servers[0] is empty or non-string")
    return issuer_url


@dataclass(frozen=True)
class _PRMCandidate:
    """One protected-resource metadata location and what it may declare.

    *accepted* is bound to the location because RFC 9728 §3.3 ties the
    identifier to the URL it was derived from; carrying it alongside the
    URL is what keeps a path-specific document from being validated
    against the origin's expectation. *hops* is this candidate's own
    ``WWW-Authenticate`` budget, so a chain of challenges off one location
    cannot starve another location's challenge. *allow_private* travels the
    same way: a location derived from the operator's own server URL may be
    private when the deployment has opted in, while one a remote document
    named may never be, whatever the setting says.
    """

    url: str
    accepted: frozenset[str]
    hops: int
    allow_private: bool


async def _fetch_prm_issuer(
    server_url: str,
    *,
    http_client: httpx.AsyncClient,
    deadline: float,
    allow_private: bool = False,
    server_name: str = "",
) -> str:
    """Fetch the protected-resource metadata document and return its ``authorization_servers[0]``.

    One loop over candidate locations: the RFC 9728 path-specific
    well-known URL first, then the origin-level URL MCP retains as a
    compatibility fallback. Whatever a candidate does short of yielding a
    usable document (SSRF rejection, transport error, non-200, unusable
    body) is recorded and the loop advances, so a server whose
    path-specific probe misbehaves in any way still reaches its origin
    document. A 401 carrying ``WWW-Authenticate: Bearer
    resource_metadata="..."`` queues that URL with the challenged
    candidate's remaining hop budget and its accepted set; a URL already
    fetched is never fetched twice, but its response is re-judged when a
    later candidate expects something different of the same location.

    What a document may declare is a property of where it was found. RFC
    9728 §3.3 requires the ``resource`` to be the identifier the well-known
    string was inserted into: the path-specific location must declare the
    server's canonical identifier and the origin-level location must
    declare the bare origin. Comparison is by
    :func:`canonical_resource` on both sides, which is the case, port and
    root-slash folding MCP tells clients to accept.

    When no candidate yields a usable document, the error raised is the
    first document-level rejection if any candidate returned a 200, since
    that is the message an operator can act on, and otherwise the first
    candidate's error.

    *allow_private* is the deployment's private-network opt-in and reaches
    only the locations derived from the operator's own server URL. A URL a
    remote document named — a challenge target, or the issuer this returns —
    is validated strictly however the setting is set, so a resource server
    can never steer the deployment at an address the operator did not type.

    The caller is responsible for passing the resulting issuer to
    :func:`_fetch_as_metadata` along with its ``trusted_hosts`` list —
    PRM itself is anchored on the resource-server origin, so trust
    expansion only matters for AS-metadata endpoint validation.

    Raises :class:`MCPOAuthDiscoveryError` on validation failure.
    """
    try:
        canonical = canonical_resource(server_url)
    except ValueError as exc:
        raise MCPOAuthDiscoveryError(str(exc)) from exc
    parsed = urllib.parse.urlsplit(canonical)
    origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    path_prm_url = _well_known_url(canonical, "oauth-protected-resource")
    candidates = [_PRMCandidate(path_prm_url, frozenset({canonical}), 1, allow_private)]
    origin_prm_url = _well_known_url(origin, "oauth-protected-resource")
    if origin_prm_url != path_prm_url:
        # RFC 9728 §3.3 binds a document to the identifier its URL was
        # derived from, and this URL was derived from the origin, so the
        # origin is the only thing it may declare. A server that serves
        # only here and declares its path-bearing URL predates the
        # path-specific location; it fails with the resource-mismatch
        # message and the operator's remedy is the Authorization Server URL
        # override, which skips PRM entirely.
        candidates.append(_PRMCandidate(origin_prm_url, frozenset({origin}), 1, allow_private))

    fetched: dict[tuple[str, bool], httpx.Response] = {}
    document_error: MCPOAuthDiscoveryError | None = None
    private_error: MCPOAuthDiscoveryError | None = None
    first_error: MCPOAuthDiscoveryError | None = None

    def _record(exc: MCPOAuthDiscoveryError) -> None:
        nonlocal first_error, private_error
        first_error = first_error or exc

    while candidates:
        candidate = candidates.pop(0)
        # A location already fetched is not fetched again, but the response
        # is re-judged: the same URL can be reached as a challenge target
        # carrying one expectation and as a derived candidate carrying
        # another, and the second expectation is a real second chance.
        # Keyed by the candidate's verdict as well as its URL: a response
        # fetched under the opt-in must not be reused for a candidate that
        # was denied it, or the verdict the record carries never applies.
        cached = fetched.get((candidate.url, candidate.allow_private))
        if cached is not None:
            resp = cached
        else:
            budget = _remaining_budget(deadline)
            if budget <= 0:
                _record(MCPOAuthDiscoveryError("PRM discovery exceeded its time budget"))
                break
            try:
                await oauth_ssrf.validate_url_no_ssrf_async(
                    candidate.url,
                    allow_http=True,
                    allow_private=candidate.allow_private,
                    private_requires_all=True,
                )
            except oauth_ssrf.OAuthSSRFPrivateAddressError as exc:
                private_error = private_error or _private_network_refusal(
                    "the MCP server's metadata location",
                    exc,
                    remedy=(
                        _PRIVATE_REMEDY_NEVER_FOLLOWED
                        if allow_private and not candidate.allow_private
                        else _PRIVATE_REMEDY_ENABLE_SETTING
                    ),
                    server_name=server_name,
                )
                continue
            except oauth_ssrf.OAuthSSRFError as exc:
                _record(_discovery_error(f"PRM URL rejected: {exc}", exc))
                continue
            try:
                resp = await http_client.get(candidate.url, timeout=budget)
            except httpx.HTTPError as exc:
                _record(_discovery_error(f"PRM fetch failed: {exc}", exc))
                continue
            fetched[(candidate.url, candidate.allow_private)] = resp

        if resp.status_code == 401:
            challenge_url = _parse_prm_url_from_www_authenticate(
                resp.headers.get("www-authenticate", "")
            )
            if challenge_url is None:
                _record(
                    MCPOAuthDiscoveryError(
                        "server returned 401 without resource_metadata in WWW-Authenticate"
                    )
                )
            elif candidate.hops > 0:
                candidates.insert(
                    0,
                    # A challenge URL is named by the server, so it inherits
                    # the opt-in only when it stays on the origin the
                    # operator typed: pointing at another location of the
                    # same host adds no reach, while pointing anywhere else
                    # is precisely the steering the scoping refuses.
                    _PRMCandidate(
                        challenge_url,
                        candidate.accepted,
                        candidate.hops - 1,
                        candidate.allow_private and _same_origin(challenge_url, candidate.url),
                    ),
                )
            else:
                _record(MCPOAuthDiscoveryError("PRM challenge chain exceeded its hop budget"))
            continue
        if resp.status_code != 200:
            _record(MCPOAuthDiscoveryError(f"PRM returned HTTP {resp.status_code}"))
            continue

        try:
            issuer_url = _prm_issuer_from_document(resp, candidate.accepted)
            # SSRF protection on the issuer URL itself — same-origin / trust-list
            # checks happen in ``_fetch_as_metadata``.
            await oauth_ssrf.validate_url_no_ssrf_async(issuer_url, allow_http=True)
        except MCPOAuthDiscoveryError as exc:
            document_error = document_error or exc
            continue
        except oauth_ssrf.OAuthSSRFPrivateAddressError as exc:
            # Named by the document, so the setting can never reach it however
            # it is set; the override is the only remedy.
            private_error = private_error or _private_network_refusal(
                "the authorization server this MCP server names",
                exc,
                remedy=_PRIVATE_REMEDY_SET_OVERRIDE,
                server_name=server_name,
            )
            continue
        except oauth_ssrf.OAuthSSRFError as exc:
            document_error = document_error or _discovery_error(
                f"PRM issuer URL rejected: {exc}", exc
            )
            continue
        return issuer_url

    # An address refusal names a setting or a field the operator can act on,
    # so it outranks a document-level complaint about a location that may not
    # even be the right one.
    raise (
        private_error
        or document_error
        or first_error
        or MCPOAuthDiscoveryError("PRM discovery found no candidate")
    )


def _remaining_budget(deadline: float) -> float:
    """Seconds left before *deadline*, clamped to the per-request timeout.

    Each probe gets the smaller of the standard timeout and what is left of
    the whole-discovery budget, so the budget is a real ceiling on wall
    clock rather than a check between requests that a slow probe overruns.
    """
    return min(oauth_http._DEFAULT_HTTP_TIMEOUT, max(0.0, deadline - time.monotonic()))


def _discovery_error(message: str, cause: BaseException) -> MCPOAuthDiscoveryError:
    """Build a discovery error that keeps *cause* on its chain when raised later."""
    err = MCPOAuthDiscoveryError(message)
    err.__cause__ = cause
    return err


def _issuer_template_matches(declared: str, requested: str) -> bool:
    """True when *declared* is a placeholder template that expands to *requested*.

    Multi-tenant providers publish a tenant-agnostic metadata document whose
    ``issuer`` carries a placeholder (``https://as.example.com/{tenantid}/v2.0``)
    rather than the tenant that was asked for, which a literal RFC 8414 §3.3
    equality check would lock out.

    The tolerance is deliberately narrow. Scheme and authority must match
    exactly, so a placeholder can never stand in for the host or the
    scheme — that would be the mix-up the equality rule exists to prevent.
    A placeholder is only recognised as a WHOLE path segment, the segment
    counts must be equal, every other segment must match literally, and at
    least one placeholder must actually have been used, and a declared value
    carrying a query, a fragment, or anything ``urlsplit`` would silently
    drop never matches.
    Comparison walks the segments rather than building a pattern, so a
    document with many placeholders costs time linear in its length.
    """
    if _has_unsafe_url_syntax(declared):
        return False
    # By the delimiter's presence, as everywhere else: a bare trailing ``?``
    # or ``#`` parses to an EMPTY component, which a truthiness check reads
    # as absent, and RFC 8414 §2 forbids the components themselves. The
    # template exception tolerates a placeholder, nothing more.
    if "?" in declared or "#" in declared:
        return False
    try:
        declared_parts = urllib.parse.urlsplit(declared)
        requested_parts = urllib.parse.urlsplit(requested)
    except ValueError:
        # A malformed address literal in a document is a mismatch, not a
        # crash: this runs on values a remote server chose.
        return False
    if declared_parts.scheme != requested_parts.scheme:
        return False
    if declared_parts.netloc != requested_parts.netloc:
        return False
    declared_segments = declared_parts.path.split("/")
    requested_segments = requested_parts.path.split("/")
    if len(declared_segments) != len(requested_segments):
        return False
    used_placeholder = False
    for declared_segment, requested_segment in zip(
        declared_segments, requested_segments, strict=True
    ):
        inner = declared_segment[1:-1]
        if (
            len(declared_segment) > 2
            and declared_segment.startswith("{")
            and declared_segment.endswith("}")
            and "{" not in inner
            and "}" not in inner
        ):
            if not requested_segment:
                return False
            used_placeholder = True
            continue
        if declared_segment != requested_segment:
            return False
    return used_placeholder


class _DiscoveryDocumentRejectedError(MCPOAuthDiscoveryError):
    """This document is not the one we are looking for; try the next candidate.

    Separate from a plain :class:`MCPOAuthDiscoveryError` so the candidate
    loops can distinguish "not a usable metadata document" (advance) from a
    refusal that must end discovery (a rejected endpoint, an AS that does
    not advertise S256). Shopping further candidates after a security
    refusal would let a laxer document win.
    """


def _as_metadata_from_document(
    doc: dict[str, Any],
    *,
    profile: str,
    issuer: str,
    normalized_issuer: str,
) -> oauth_http.ASMetadata:
    """Validate one AS metadata document completely and build :class:`oauth_http.ASMetadata`.

    A candidate wins only after this returns, so a catch-all 200 cannot
    shadow the document that actually describes the issuer.

    Raises :class:`_DiscoveryDocumentRejectedError` when the body is not this
    issuer's metadata document (absent or mismatched ``issuer``, missing
    endpoints) and the loop should try the next location. Raises a plain
    :class:`MCPOAuthDiscoveryError` for a refusal that must stop discovery:
    an endpoint that fails the same-origin or SSRF check, or an AS that
    does not advertise S256.
    """
    # RFC 8414 §3.3: the document's ``issuer`` MUST equal the requested one,
    # which is what prevents a catch-all document at a neighbouring path from
    # deciding this issuer's endpoints. "Identical" is meant literally and the
    # comparison is against the issuer AS REQUESTED, not the slash-stripped
    # form used to build the well-known URL: ``…/tenant`` and ``…/tenant/``
    # are different identifiers, and accepting either for the other is the
    # leniency the rule exists to deny. The one tolerated deviation is a
    # templated issuer, which multi-tenant providers publish on their
    # tenant-agnostic document; every other mismatch means "not this issuer's
    # document" and the loop moves on.
    declared_issuer = doc.get("issuer")
    if not isinstance(declared_issuer, str) or not declared_issuer:
        raise _DiscoveryDocumentRejectedError("AS metadata document has no issuer")
    exact = declared_issuer == issuer
    templated = not exact and _issuer_template_matches(declared_issuer, issuer)
    if not exact and not templated:
        raise _DiscoveryDocumentRejectedError(
            "AS metadata issuer does not match the requested issuer "
            f"(declared={oauth_ssrf.sanitize_log_text(declared_issuer, 120)!r}, "
            f"requested={issuer!r})"
        )
    if templated:
        log.info(
            "mcp_server.oauth.as_metadata_templated_issuer",
            requested=issuer,
            declared=oauth_ssrf.sanitize_log_text(declared_issuer),
        )

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
        raise _DiscoveryDocumentRejectedError("AS metadata missing required endpoints")

    code_methods_raw = doc.get("code_challenge_methods_supported", [])
    if not isinstance(code_methods_raw, list):
        code_methods_raw = []
    code_methods = tuple(str(m) for m in code_methods_raw)
    if not code_methods and profile == "oidc":
        # OIDC document (openid-configuration) only: it does not require
        # advertising code_challenge_methods_supported, and some IdPs omit it
        # despite fully supporting S256, so treat absence as "S256 supported"
        # (mandated by OAuth 2.1 / MCP auth) rather than locking the AS out.
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
        # Fail closed rather than advancing: a laxer document at another
        # location must not be able to win after this one refused.
        raise MCPOAuthDiscoveryError(
            "AS metadata does not advertise S256 PKCE — refusing to proceed"
        )

    auth_methods_raw = doc.get("token_endpoint_auth_methods_supported", [])
    if not isinstance(auth_methods_raw, list):
        auth_methods_raw = []
    auth_methods = tuple(str(m) for m in auth_methods_raw)

    return oauth_http.ASMetadata(
        issuer=issuer if templated else declared_issuer,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        registration_endpoint=registration_endpoint,
        revocation_endpoint=revocation_endpoint,
        jwks_uri=jwks_uri,
        code_challenge_methods_supported=code_methods,
        token_endpoint_auth_methods_supported=auth_methods,
    )


async def _fetch_as_metadata(
    issuer: str,
    *,
    http_client: httpx.AsyncClient,
    trusted_hosts: frozenset[str],
    deadline: float,
    allow_private: bool = False,
) -> oauth_http.ASMetadata:
    """Fetch and validate the authorization-server metadata document for *issuer*.

    Tries each metadata location in turn and accepts a candidate only after
    the whole document validates, so a catch-all 200 at one location cannot
    shadow the document that actually describes the issuer. A location is
    skipped on a transport error, a non-200, an unusable body, or a document
    that is not this issuer's (see :func:`_as_metadata_from_document`); a
    rejected endpoint or a missing S256 advertisement ends discovery rather
    than shopping for a laxer document.

    Validates the discovered ``authorization_endpoint`` and
    ``token_endpoint`` are same-origin (or in ``trusted_hosts``).

    *allow_private* is set only when the issuer is the operator's own
    override; an issuer a protected-resource document named is validated
    strictly however the deployment's setting is set. The document's own
    endpoints inherit the issuer's verdict, which is safe because they are
    already bound same-origin to it.
    """
    try:
        issuer_parsed = await oauth_ssrf.validate_url_no_ssrf_async(
            issuer, allow_http=True, allow_private=allow_private, private_requires_all=True
        )
    except oauth_ssrf.OAuthSSRFPrivateAddressError as exc:
        raise _private_network_refusal(
            "the authorization server",
            exc,
            remedy=(
                _PRIVATE_REMEDY_ENABLE_SETTING if allow_private else _PRIVATE_REMEDY_SET_OVERRIDE
            ),
        ) from exc
    except oauth_ssrf.OAuthSSRFError as exc:
        raise MCPOAuthDiscoveryError(f"AS issuer URL rejected: {exc}") from exc

    # RFC 8414 §2 forbids a query or fragment in an issuer identifier, and
    # neither can be spliced into a well-known URL, so they are refused here
    # with a message that names the cause rather than a misleading 404 —
    # judged by the delimiter's presence, since a bare trailing ``?`` or
    # ``#`` still parses to an empty component. The raw guard runs first:
    # this string is also what a document's ``issuer`` is compared against.
    if _has_unsafe_url_syntax(issuer):
        raise MCPOAuthDiscoveryError("AS issuer URL must not contain control characters or spaces")
    if "?" in issuer or "#" in issuer:
        raise MCPOAuthDiscoveryError("AS issuer URL must not contain a query or fragment")
    normalized_issuer = issuer.rstrip("/")

    # MCP lists three locations for a path-bearing issuer, in this order:
    # the RFC 8414 document with the well-known segment inserted before the
    # issuer path, then the OpenID Connect document inserted, then the
    # OpenID Connect document appended (the legacy transformation major
    # enterprise IdPs still serve). RFC 8414 §5 likewise says to try its own
    # transformation before falling back to OpenID Connect Discovery.
    # The appended RFC 8414 form is in no specification, but some realm-based
    # servers answer only there and earlier releases probed it, so it stays
    # as a compatibility candidate LAST, where it cannot preempt or delay a
    # standard location. For a root issuer the inserted and appended forms
    # coincide and are deduplicated, so such an issuer costs two requests.
    # ``profile`` records WHICH document each candidate is, so the S256 check
    # can apply the correct per-document defaulting rule.
    metadata_candidates: list[tuple[str, str]] = []
    for profile, suffix, append in (
        ("rfc8414", "oauth-authorization-server", False),
        ("oidc", "openid-configuration", False),
        ("oidc", "openid-configuration", True),
        ("rfc8414", "oauth-authorization-server", True),
    ):
        metadata_url = (
            f"{normalized_issuer}/.well-known/{suffix}"
            if append
            else _well_known_url(normalized_issuer, suffix)
        )
        if all(metadata_url != seen for _, seen in metadata_candidates):
            metadata_candidates.append((profile, metadata_url))

    document_error: MCPOAuthDiscoveryError | None = None
    first_error: MCPOAuthDiscoveryError | None = None
    for profile, metadata_url in metadata_candidates:
        budget = _remaining_budget(deadline)
        if budget <= 0:
            first_error = first_error or MCPOAuthDiscoveryError(
                "AS metadata discovery exceeded its time budget"
            )
            break
        try:
            r = await http_client.get(metadata_url, timeout=budget)
        except httpx.HTTPError as exc:
            first_error = first_error or _discovery_error(f"AS metadata fetch failed: {exc}", exc)
            continue
        if r.status_code != 200:
            first_error = first_error or MCPOAuthDiscoveryError(
                f"AS metadata returned HTTP {r.status_code}"
            )
            continue
        # A body that is not a JSON object is not a metadata document, so the
        # loop advances; a document that IS one but fails validation is
        # judged by ``_as_metadata_from_document``, which decides whether the
        # loop may advance or discovery must stop.
        try:
            doc = _json_object_body(r, "AS metadata")
        except MCPOAuthDiscoveryError as exc:
            document_error = document_error or exc
            continue
        try:
            metadata = _as_metadata_from_document(
                doc,
                profile=profile,
                issuer=issuer,
                normalized_issuer=normalized_issuer,
            )
        except _DiscoveryDocumentRejectedError as exc:
            document_error = document_error or exc
            continue

        # Same-origin / trusted-host validation on each discovered endpoint.
        # A rejection here ends discovery: the document named this issuer, so
        # a neighbouring location cannot be a better answer for it.
        allow_http = _is_localhost_issuer(issuer_parsed.hostname or "")
        for name, endpoint_url in (
            ("authorization_endpoint", metadata.authorization_endpoint),
            ("token_endpoint", metadata.token_endpoint),
            ("registration_endpoint", metadata.registration_endpoint),
            ("revocation_endpoint", metadata.revocation_endpoint),
        ):
            if not endpoint_url:
                continue
            try:
                # The opt-in reaches an endpoint only on the issuer's own
                # origin. ``validate_discovered_endpoint`` also admits the
                # trusted-host lists, so forwarding it unconditionally would
                # let a document name a private host from those lists — the
                # invariant the docstring states, enforced rather than
                # assumed.
                await oauth_ssrf.validate_discovered_endpoint_async(
                    endpoint_url,
                    issuer_parsed,
                    allow_http=allow_http,
                    trusted_endpoint_hosts=trusted_hosts,
                    allow_private=allow_private and _same_origin(endpoint_url, issuer),
                )
            except oauth_ssrf.OAuthSSRFError as exc:
                raise MCPOAuthDiscoveryError(
                    f"AS {name} rejected (url={endpoint_url}): {exc}"
                ) from exc

        # Which discovery profile answered (rfc8414 vs oidc) is the load-bearing
        # detail when debugging an enterprise AS that serves only one of them.
        log.debug("mcp_server.oauth.as_metadata_discovered", profile=profile)
        return metadata

    raise document_error or first_error or MCPOAuthDiscoveryError("AS metadata found no candidate")


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
    metadata_cache: dict[str, tuple[oauth_http.ASMetadata, float]] | None = None,
    allow_private_network: bool = False,
) -> oauth_http.ASMetadata:
    """Resolve the issuer URL and load AS metadata for an MCP server.

    *allow_private_network* is the deployment opt-in
    (``mcp.oauth_allow_private_network``). It reaches only the values the
    operator typed on the row: the server URL that PRM locations are derived
    from, and the authorization-server override. An issuer or endpoint named
    by a fetched document is validated strictly whatever the setting says, so
    a remote server can never expand the deployment's reach into private
    space; the always-refused lanes (link-local, multicast, reserved, cloud
    metadata) are refused under the opt-in too.

    Resolution order:

    1. ``override_url`` (operator override) — used directly as the issuer.
    2. ``cached_issuer`` (from ``mcp_servers.oauth_as_issuer_cached``) —
       trusted across requests until invalidated by an admin edit.
    3. PRM discovery via the resource server URL.

    The fetched :class:`oauth_http.ASMetadata` is also memoised in *metadata_cache*
    keyed by issuer URL when the cache is supplied — this is the
    in-process layer that bypasses the network on subsequent calls.
    Persistent caching of the issuer (resolution step 2) lives on the
    ``mcp_servers`` row.
    """
    # One budget for the whole resolution: PRM probes and AS probes share it,
    # so a hard-down host cannot spend it twice. The per-request deadline
    # gives each probe an actionable error; the outer timeout in
    # ``_resolve_issuer_and_metadata`` is what makes the budget a real
    # wall-clock ceiling, since httpx timeouts bound inactivity per phase
    # rather than the whole call, and DNS, SSRF and endpoint validation run
    # outside them entirely.
    deadline = time.monotonic() + _DISCOVERY_TOTAL_BUDGET
    try:
        async with asyncio.timeout(_DISCOVERY_TOTAL_BUDGET):
            issuer: str
            if override_url:
                try:
                    await oauth_ssrf.validate_url_no_ssrf_async(
                        override_url,
                        allow_http=True,
                        allow_private=allow_private_network,
                        private_requires_all=True,
                    )
                except oauth_ssrf.OAuthSSRFPrivateAddressError as exc:
                    raise _private_network_refusal(
                        "the Authorization Server URL",
                        exc,
                        remedy=_PRIVATE_REMEDY_ENABLE_SETTING,
                        server_name=server_name,
                    ) from exc
                except oauth_ssrf.OAuthSSRFError as exc:
                    raise MCPOAuthDiscoveryError(f"override AS URL rejected: {exc}") from exc
                issuer = override_url
            elif cached_issuer:
                # Defense-in-depth: re-run SSRF validation on the cached value.
                # If the issuer's hostname has rebound to a private address since
                # we cached it (or the operator edited the row to point at a
                # private host), drop the cache and fall through to PRM.
                try:
                    await oauth_ssrf.validate_url_no_ssrf_async(cached_issuer, allow_http=True)
                except oauth_ssrf.OAuthSSRFError as exc:
                    log.warning(
                        "mcp_server.oauth.cached_issuer_rejected",
                        server_name=server_name,
                        reason=oauth_ssrf.sanitize_log_text(str(exc)),
                    )
                    cached_issuer = None
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
                    issuer = await _fetch_prm_issuer(
                        server_url,
                        http_client=http_client,
                        deadline=deadline,
                        allow_private=allow_private_network,
                        server_name=server_name,
                    )
                else:
                    issuer = cached_issuer
            else:
                issuer = await _fetch_prm_issuer(
                    server_url,
                    http_client=http_client,
                    deadline=deadline,
                    allow_private=allow_private_network,
                    server_name=server_name,
                )

            cached = metadata_cache.get(issuer) if metadata_cache is not None else None
            if (
                cached is not None
                and time.monotonic() - cached[1] < MCP_OAUTH_DISCOVERY_CACHE_TTL_SECONDS
            ):
                metadata = cached[0]
            else:
                # Only an operator-typed issuer carries the opt-in. A cached
                # issuer was resolved from a PRM document, never from the
                # override, so it is remote-named and stays strict.
                metadata = await _fetch_as_metadata(
                    issuer,
                    http_client=http_client,
                    trusted_hosts=trusted_hosts,
                    deadline=deadline,
                    allow_private=allow_private_network and bool(override_url),
                )
                if metadata_cache is not None:
                    metadata_cache[issuer] = (metadata, time.monotonic())
    except TimeoutError as exc:
        raise MCPOAuthDiscoveryError("discovery exceeded its time budget") from exc

    # Persist the resolved issuer when the row had no valid cached value,
    # including when another row already warmed the AS metadata cache,
    # so subsequent calls skip PRM. We never overwrite an existing
    # valid cached_issuer; the console clears it when the server URL or the AS
    # override changes, which is when the cached value stops describing
    # the row.
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
    as_metadata: oauth_http.ASMetadata,
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


# ---------------------------------------------------------------------------
# DCR (RFC 7591 minimal one-shot)
# ---------------------------------------------------------------------------


async def register_dynamic_client(
    *,
    as_metadata: oauth_http.ASMetadata,
    redirect_uri: str,
    http_client: httpx.AsyncClient,
    scopes: str = "",
) -> tuple[str, str | None]:
    """Register a public client using RFC 7591 client metadata.

    Returns ``(client_id, client_secret_or_None)``. Empty ``client_secret``
    means the AS issued a public client (preferred for the per-user flow).
    Currently registers once and persists the result; the
    re-register-on-401 path is wired by the upcoming dispatch integration
    that surfaces the 401 from the token endpoint.

    Raises :class:`OAuthError` when the AS doesn't expose
    ``registration_endpoint`` or returns a non-2xx response.
    """
    if not as_metadata.registration_endpoint:
        raise oauth_http.OAuthError("AS does not advertise registration_endpoint")

    body: dict[str, Any] = {
        "redirect_uris": [redirect_uri],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    if scopes:
        body["scope"] = scopes

    try:
        resp = await http_client.post(
            as_metadata.registration_endpoint,
            json=body,
            timeout=oauth_http._DEFAULT_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise oauth_http.OAuthError(f"DCR request failed: {exc}") from exc

    if len(resp.content) > oauth_http._MAX_TOKEN_BODY_BYTES:
        raise oauth_http.OAuthError("DCR response body exceeds size limit")

    if resp.status_code not in (200, 201):
        raise oauth_http.OAuthError(
            f"DCR returned HTTP {resp.status_code}: {oauth_http._format_as_error(resp)}"
        )

    try:
        doc = resp.json()
    except ValueError as exc:
        raise oauth_http.OAuthError(f"DCR body is not valid JSON: {exc}") from exc

    if not isinstance(doc, dict):
        raise oauth_http.OAuthError("DCR body is not a JSON object")

    client_id = doc.get("client_id")
    client_secret = doc.get("client_secret")
    if not isinstance(client_id, str) or not client_id:
        raise oauth_http.OAuthError("DCR response missing client_id")
    if client_secret is not None and not isinstance(client_secret, str):
        raise oauth_http.OAuthError("DCR response client_secret is not a string")
    return client_id, client_secret


# ---------------------------------------------------------------------------
# Token exchange (authorization-code) and refresh
# ---------------------------------------------------------------------------


async def exchange_code(
    *,
    as_metadata: oauth_http.ASMetadata,
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
            timeout=oauth_http._DEFAULT_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise MCPOAuthExchangeError(f"token exchange request failed: {exc}") from exc

    if len(resp.content) > oauth_http._MAX_TOKEN_BODY_BYTES:
        raise MCPOAuthExchangeError("token endpoint response body exceeds size limit")

    if resp.status_code != 200:
        raise MCPOAuthExchangeError(
            f"token endpoint returned HTTP {resp.status_code}: {oauth_http._format_as_error(resp)}"
        )

    try:
        doc = resp.json()
    except ValueError as exc:
        raise MCPOAuthExchangeError(f"token body is not valid JSON: {exc}") from exc

    if not isinstance(doc, dict):
        raise MCPOAuthExchangeError("token body is not a JSON object")
    return doc


async def revoke_token_at_as(
    *,
    as_metadata: oauth_http.ASMetadata,
    http_client: httpx.AsyncClient,
    refresh_token: str,
    client_id: str,
    client_secret: str | None,
    timeout_seconds: float = oauth_http._DEFAULT_HTTP_TIMEOUT,
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


_AMBIGUOUS_ESCALATION_THRESHOLD = 5


@dataclass(frozen=True)
class _BackoffSnapshot:
    last_failure_monotonic: float = 0.0
    ambiguous_streak: int = 0
    last_failure_permanent: bool = False


@dataclass(frozen=True)
class _LookupDisposition:
    apply: bool = True
    backoff: _BackoffSnapshot | None = None
    prune: bool = False


@dataclass(frozen=True)
class _LookupOutcome:
    result: TokenLookupResult
    disposition: _LookupDisposition = _LookupDisposition()


def _mcp_coordination(app_state: Any) -> TokenCoordination:
    state = getattr(app_state, "mcp_oauth_coordination", None)
    if state is None:
        state = TokenCoordination()
        app_state.mcp_oauth_coordination = state
    return state


async def _clear_server_coordination(app_state: Any, user_id: str, server_name: str) -> None:
    """Apply browser disconnect cleanup on the loop that owns MCP state."""
    state = _mcp_coordination(app_state)
    loop = state.loop
    if loop is None or loop.is_closed() or not loop.is_running():
        return

    async def clear() -> None:
        lock = oauth_locking._refresh_lock_for(state, user_id, server_name)
        try:
            async with lock:
                oauth_tokens._clear_refresh_backoff(state, user_id, server_name)
        finally:
            oauth_locking._prune_token_lock_when_idle(state, user_id, server_name, lock)

    if loop is asyncio.get_running_loop():
        await clear()
    else:
        operation = clear()
        try:
            future = asyncio.run_coroutine_threadsafe(operation, loop)
        except RuntimeError:
            operation.close()
            return
        try:
            async with asyncio.timeout(5.0):
                await asyncio.wrap_future(future)
        except TimeoutError:
            log.warning("mcp_server.oauth.disconnect_coordination_timeout")


def _backoff_snapshot(state: TokenCoordination, user_id: str, server_name: str) -> _BackoffSnapshot:
    backoff = state.backoff.get((user_id, server_name))
    if backoff is None:
        return _BackoffSnapshot()
    return _BackoffSnapshot(
        backoff.last_failure_monotonic, backoff.ambiguous_streak, backoff.last_failure_permanent
    )


def _apply_lookup_outcome(
    state: TokenCoordination, user_id: str, server_name: str, outcome: _LookupOutcome
) -> TokenLookupResult:
    """Apply a returned disposition on the MCP loop while its server lock is held."""
    lock = state.locks.get((user_id, server_name))
    if lock is None or not lock.locked():
        raise RuntimeError("MCP OAuth disposition requires its server coordination lock")
    disposition = outcome.disposition
    if disposition.apply:
        if disposition.backoff is None:
            oauth_tokens._clear_refresh_backoff(state, user_id, server_name)
        else:
            backoff = disposition.backoff
            state.backoff[(user_id, server_name)] = oauth_tokens._RefreshBackoffState(
                backoff.last_failure_monotonic,
                backoff.ambiguous_streak,
                backoff.last_failure_permanent,
            )
        if disposition.prune:
            oauth_locking._drop_refresh_lock(state, user_id, server_name)
    return outcome.result


async def _refresh_user_operation(
    *,
    context: OAuthContext,
    storage: StorageBackend,
    token_store: MCPTokenStore,
    user_id: str,
    server_name: str,
    server_row: dict[str, Any],
    started: datetime,
    force_refresh: bool,
    backoff: _BackoffSnapshot,
    revoke_on_failure: bool,
    revoke_ambiguous_escalation: bool,
    allow_private_network: bool,
) -> _LookupOutcome:
    """Serialize custodial durable work where it runs, including on SQLite."""
    lock = oauth_locking._refresh_lock_for(context.coordination, user_id, server_name)
    pg_lock = await oauth_locking._acquire_pg_refresh_lock(storage, user_id, server_name)
    server_id = str(server_row.get("server_id") or "")
    try:
        async with lock, pg_lock:
            try:
                plain = await asyncio.to_thread(token_store.get_user_token, user_id, server_name)
            except token_store_crypto.TokenDecryptError as exc:
                return _decrypt_failure_result(
                    user_id,
                    server_name,
                    exc,
                    event="mcp_server.oauth.token_decrypt_failed_classified",
                )
            if plain is None:
                return _no_token_result(TokenLookupResult(kind="missing"))
            if not oauth_tokens._token_needs_refresh(plain.get("expires_at")):
                if not force_refresh:
                    return _token_result(plain["access_token"])
                refreshed = oauth_tokens._parse_iso_to_utc(plain.get("last_refreshed") or "")
                if refreshed is not None and refreshed >= started:
                    return _token_result(plain["access_token"])
            refresh_value = plain.get("refresh_token")
            if not refresh_value:
                if not revoke_on_failure:
                    return _LookupOutcome(
                        TokenLookupResult(kind="refresh_failed"), _LookupDisposition(apply=False)
                    )
                return await _revoke_after_refresh_failure(
                    context,
                    token_store,
                    user_id,
                    server_name,
                    server_id,
                    reason="expired_no_refresh",
                )
            try:
                access, _, _ = await _refresh_and_persist(
                    app_state=context,
                    storage=storage,
                    token_store=token_store,
                    user_id=user_id,
                    server_name=server_name,
                    server_row=server_row,
                    refresh_value=refresh_value,
                    existing_scopes=plain.get("scopes") or "",
                    allow_private_network=allow_private_network,
                )
            except oauth_http.OAuthRefreshError as exc:
                return await _handle_refresh_failure(
                    exc,
                    app_state=context,
                    user_id=user_id,
                    server_name=server_name,
                    server_id_for_audit=server_id,
                    token_store=token_store,
                    backoff=backoff,
                    revoke_on_failure=revoke_on_failure,
                    revoke_ambiguous_escalation=revoke_ambiguous_escalation,
                    events=_REFRESH_FAILURE_EVENTS,
                    permanent_reason="refresh_failed",
                    escalation_reason="refresh_failed_ambiguous_escalated",
                )
            return _token_result(access)
    finally:
        oauth_locking._prune_token_lock_when_idle(context.coordination, user_id, server_name, lock)


async def _prepare_obo_lookup(
    *,
    context: OAuthContext,
    token_store: MCPTokenStore,
    state: TokenCoordination,
    user_id: str,
    server_name: str,
    force_refresh: bool,
    revoke_ambiguous_escalation: bool,
    server_row: dict[str, Any] | None,
    credential_present: bool | None,
    pre_lock_token: str | None,
) -> _LookupOutcome:
    """MCP-loop admission and snapshotting while the per-server lock is held."""
    storage = oauth_tokens._get_storage(context)
    if storage is None:
        return _no_token_result(TokenLookupResult(kind="missing"))
    if server_row is None:
        server_row = await asyncio.to_thread(storage.get_mcp_server_by_name, server_name)
    if server_row is None:
        return _no_token_result(TokenLookupResult(kind="missing"))
    config = context.oidc_config
    profile = str(getattr(config, "obo_grant_profile", "") or "")
    audience = str(server_row.get("oauth_audience") or "")
    scopes = str(server_row.get("oauth_scopes") or "")
    effective_scopes = "" if profile == "entra" else scopes
    mint = oauth_grants._OBO_MINT_LEGS.get(profile)
    try:
        plain = await asyncio.to_thread(token_store.get_user_token, user_id, server_name)
    except token_store_crypto.TokenDecryptError as exc:
        return _decrypt_failure_result(
            user_id, server_name, exc, event="mcp_server.oauth.obo_cache_decrypt_failed"
        )
    fresh = oauth_tokens._is_fresh_obo_cache_row(plain, audience, effective_scopes)
    if fresh and not force_refresh and plain is not None:
        return _token_result(plain["access_token"])
    backoff = _backoff_snapshot(state, user_id, server_name)
    if not fresh and oauth_tokens._refresh_in_cooldown(state, user_id, server_name):
        kind: Literal["refresh_failed", "refresh_failed_transient"] = (
            "refresh_failed" if backoff.last_failure_permanent else "refresh_failed_transient"
        )
        return _LookupOutcome(TokenLookupResult(kind=kind), _LookupDisposition(apply=False))
    if config is not None and not config.enabled:
        await oauth_oidc.maybe_rediscover_oidc(context)
        config = context.oidc_config
    if (
        config is None
        or not config.enabled
        or not config.token_endpoint
        or mint is None
        or not audience
    ):
        log.error(
            "mcp_server.oauth.obo_misconfigured",
            server_name=server_name,
            oidc_enabled=bool(config is not None and config.enabled),
            grant_profile=profile or "<unset>",
            has_audience=bool(audience),
        )
        return _LookupOutcome(
            TokenLookupResult(kind="refresh_failed_transient"),
            _LookupDisposition(
                backoff=_BackoffSnapshot(time.monotonic(), backoff.ambiguous_streak)
            ),
        )
    issuer = config.issuer
    if credential_present is None:
        credential_present = (
            await asyncio.to_thread(storage.get_oidc_user_credential, user_id, issuer) is not None
        )
    if not credential_present:
        return _no_token_result(TokenLookupResult(kind="missing"))
    try:
        runtime = ensure_oauth_runtime(context)
        if runtime is None:
            raise OAuthUnavailableError("OAuth runtime is not configured")
        return await runtime.call(
            lambda: _mint_mcp_obo_operation(
                context=context,
                storage=storage,
                token_store=token_store,
                user_id=user_id,
                server_name=server_name,
                server_id=str(server_row.get("server_id") or ""),
                config=config,
                profile=profile,
                audience=audience,
                scopes=scopes,
                effective_scopes=effective_scopes,
                force_refresh=force_refresh,
                pre_lock_token=pre_lock_token,
                backoff=backoff,
                revoke_ambiguous_escalation=revoke_ambiguous_escalation,
            )
        )
    except OAuthUnavailableError:
        return _LookupOutcome(
            TokenLookupResult(kind="refresh_failed_transient"), _LookupDisposition(apply=False)
        )


async def _mint_mcp_obo_operation(
    *,
    context: OAuthContext,
    storage: StorageBackend,
    token_store: MCPTokenStore,
    user_id: str,
    server_name: str,
    server_id: str,
    config: oauth_oidc.OIDCConfig,
    profile: str,
    audience: str,
    scopes: str,
    effective_scopes: str,
    force_refresh: bool,
    pre_lock_token: str | None,
    backoff: _BackoffSnapshot,
    revoke_ambiguous_escalation: bool,
) -> _LookupOutcome:
    """Protected MCP mint; model OBO uses this exact runtime credential key too."""
    issuer = config.issuer
    credential_key = f"__obo__:{issuer}"
    credential_lock = oauth_locking._refresh_lock_for(context.coordination, user_id, credential_key)
    pg_lock = await oauth_locking._acquire_pg_refresh_lock(storage, user_id, credential_key)
    async with credential_lock, pg_lock:
        try:
            plain = await asyncio.to_thread(token_store.get_user_token, user_id, server_name)
        except token_store_crypto.TokenDecryptError as exc:
            return _decrypt_failure_result(user_id, server_name, exc, event=None)
        if (
            oauth_tokens._is_fresh_obo_cache_row(plain, audience, effective_scopes)
            and plain is not None
        ) and (not force_refresh or plain["access_token"] != pre_lock_token):
            return _token_result(plain["access_token"])
        credential = await _read_obo_credential(token_store, user_id, server_name, issuer)
        if isinstance(credential, _LookupOutcome):
            return credential

        async def persist_rotation(new_credential_rt: str) -> None:
            try:
                await durable_write(
                    token_store.update_oidc_credential_after_redeem,
                    user_id,
                    issuer,
                    refresh_token=new_credential_rt,
                    expected_current=credential["refresh_token"],
                )
            except Exception:
                log.error(
                    "mcp_server.oauth.obo_rotation_persist_failed",
                    user_id=user_id,
                    server_name=server_name,
                    exc_info=True,
                )

        try:
            async with oauth_http._enter_mint_client(context) as client:
                tokens = await oauth_grants._OBO_MINT_LEGS[profile](
                    oidc_config=config,
                    credential_refresh_token=credential["refresh_token"],
                    audience=audience,
                    scopes=scopes,
                    http_client=client,
                    persist_rotation=persist_rotation,
                )
        except oauth_http.OAuthRefreshError as exc:
            return await _handle_refresh_failure(
                exc,
                app_state=context,
                user_id=user_id,
                server_name=server_name,
                server_id_for_audit=server_id,
                token_store=token_store,
                backoff=backoff,
                revoke_on_failure=True,
                revoke_ambiguous_escalation=revoke_ambiguous_escalation,
                events=_OBO_MINT_FAILURE_EVENTS,
                permanent_reason="obo_mint_rejected",
                escalation_reason="obo_mint_ambiguous_escalated",
                arm_cooldown_on_permanent=True,
            )
        access = tokens.get("access_token")
        if not isinstance(access, str) or not access:
            log.warning(
                "mcp_server.oauth.obo_mint_missing_access_token",
                user_id=user_id,
                server_name=server_name,
            )
            return _LookupOutcome(
                TokenLookupResult(kind="refresh_failed_transient"),
                _LookupDisposition(backoff=_BackoffSnapshot(time.monotonic())),
            )
        try:
            await oauth_tokens._persist_obo_cache_row(
                token_store,
                user_id,
                server_name,
                access_token=access,
                expires_at=oauth_tokens._expires_at_from_response(
                    tokens, default_ttl_seconds=oauth_tokens._OBO_DEFAULT_TTL_SECONDS
                ),
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
        return _token_result(access)


def _token_result(token: str | None) -> _LookupOutcome:
    """A usable token clears the MCP cooldown and ambiguous streak on return."""
    return _LookupOutcome(TokenLookupResult(kind="token", token=token))


def _no_token_result(result: TokenLookupResult) -> _LookupOutcome:
    """Missing/decrypt/revoked grants request MCP state cleanup after the operation."""
    return _LookupOutcome(result, _LookupDisposition(prune=True))


async def _revoke_after_refresh_failure(
    app_state: OAuthContext,
    token_store: MCPTokenStore,
    user_id: str,
    server_name: str,
    server_id_for_audit: str,
    *,
    reason: str,
    audit_when_absent: bool = True,
) -> _LookupOutcome:
    """Delete under runtime locking, then request MCP backoff/lock cleanup.

    Custodial failures audit even a concurrently removed row: a grant really
    existed. OBO failures audit only actual deletions because an unsuccessful
    mint need not have created a cache row and its shared credential survives.
    """
    deleted = await durable_write(token_store.delete_user_token, user_id, server_name)
    if deleted or audit_when_absent:
        await _audit_event(
            app_state,
            server_id=server_id_for_audit,
            user_id=user_id,
            action="mcp_server.oauth.token_revoked",
            server_name=server_name,
            detail={"reason": reason},
        )
    return _no_token_result(TokenLookupResult(kind="refresh_failed"))


def _decrypt_failure_result(
    user_id: str,
    server_name: str,
    exc: token_store_crypto.TokenDecryptError,
    *,
    event: str | None,
) -> _LookupOutcome:
    if event is not None:
        log.warning(event, user_id=user_id, server_name=server_name, exc_info=True)
    return _no_token_result(
        TokenLookupResult(
            kind="decrypt_failure", decrypt_fingerprints=tuple(exc.key_fingerprints_attempted)
        )
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
    exc: oauth_http.OAuthRefreshError,
    *,
    app_state: OAuthContext,
    user_id: str,
    server_name: str,
    server_id_for_audit: str,
    token_store: MCPTokenStore,
    backoff: _BackoffSnapshot,
    revoke_on_failure: bool,
    revoke_ambiguous_escalation: bool,
    events: _FailureEvents,
    permanent_reason: str,
    escalation_reason: str,
    arm_cooldown_on_permanent: bool = False,
) -> _LookupOutcome:
    """Classify under runtime locking, returning an immutable MCP disposition."""
    if not revoke_on_failure:
        kind: Literal["refresh_failed", "refresh_failed_transient"] = (
            "refresh_failed"
            if exc.failure_class is oauth_http.RefreshFailureClass.PERMANENT
            else "refresh_failed_transient"
        )
        return _LookupOutcome(TokenLookupResult(kind=kind), _LookupDisposition(apply=False))
    if exc.failure_class is oauth_http.RefreshFailureClass.PERMANENT:
        if events.permanent is not None:
            log.warning(events.permanent, user_id=user_id, server_name=server_name, error=str(exc))
        result = await _revoke_after_refresh_failure(
            app_state,
            token_store,
            user_id,
            server_name,
            server_id_for_audit,
            reason=permanent_reason,
            audit_when_absent=not arm_cooldown_on_permanent,
        )
        if arm_cooldown_on_permanent:
            return _LookupOutcome(
                result.result,
                _LookupDisposition(backoff=_BackoffSnapshot(time.monotonic(), 0, True), prune=True),
            )
        return result

    # Only uninterrupted ambiguous failures escalate. Infrastructure failures
    # reset the streak; both keep the token and arm a cooldown.
    streak = (
        backoff.ambiguous_streak + 1
        if exc.failure_class is oauth_http.RefreshFailureClass.AMBIGUOUS
        else 0
    )
    updated = _BackoffSnapshot(time.monotonic(), streak)
    if streak >= _AMBIGUOUS_ESCALATION_THRESHOLD:
        if revoke_ambiguous_escalation:
            log.warning(
                events.escalated,
                user_id=user_id,
                server_name=server_name,
                streak=streak,
                error=str(exc),
            )
            result = await _revoke_after_refresh_failure(
                app_state,
                token_store,
                user_id,
                server_name,
                server_id_for_audit,
                reason=escalation_reason,
                audit_when_absent=not arm_cooldown_on_permanent,
            )
            if arm_cooldown_on_permanent:
                return _LookupOutcome(
                    result.result,
                    _LookupDisposition(
                        backoff=_BackoffSnapshot(time.monotonic(), 0, True), prune=True
                    ),
                )
            return result
        log.warning(
            events.deferred, user_id=user_id, server_name=server_name, streak=streak, error=str(exc)
        )
    log.warning(
        events.transient,
        user_id=user_id,
        server_name=server_name,
        failure_class=exc.failure_class.value,
        ambiguous_streak=streak,
        error=str(exc),
    )
    return _LookupOutcome(
        TokenLookupResult(kind="refresh_failed_transient"), _LookupDisposition(backoff=updated)
    )


async def get_user_access_token_classified(
    *,
    app_state: Any,
    user_id: str,
    server_name: str,
    force_refresh: bool = False,
    revoke_ambiguous_escalation: bool = True,
    revoke_on_failure: bool = True,
) -> TokenLookupResult:
    """Coordinate MCP state locally and refresh custodial grants on the OAuth runtime.

    Observe-only sweep failures never change grant/backoff state. Priming may
    revoke permanent failures but defers ambiguous escalation to dispatch.
    Both the initial lookup and returned disposition hold the MCP server lock;
    durable refresh/revocation independently holds the runtime server/advisory key.
    """
    context = oauth_context(app_state)
    token_store = cast("MCPTokenStore | None", context.token_store)
    if token_store is None:
        log.debug("mcp_server.oauth.token_store_unconfigured")
        return TokenLookupResult(kind="missing")
    started = datetime.now(UTC).replace(microsecond=0)
    state = _mcp_coordination(app_state)
    lock = oauth_locking._refresh_lock_for(state, user_id, server_name)
    async with lock:
        try:
            plain = await asyncio.to_thread(token_store.get_user_token, user_id, server_name)
        except token_store_crypto.TokenDecryptError as exc:
            outcome = _decrypt_failure_result(
                user_id,
                server_name,
                exc,
                event="mcp_server.oauth.token_decrypt_failed_classified",
            )
            return _apply_lookup_outcome(state, user_id, server_name, outcome)
        if plain is None:
            return _apply_lookup_outcome(
                state, user_id, server_name, _no_token_result(TokenLookupResult(kind="missing"))
            )
        needs_refresh = oauth_tokens._token_needs_refresh(plain.get("expires_at"))
        if not force_refresh and not needs_refresh:
            return _apply_lookup_outcome(
                state, user_id, server_name, _token_result(plain["access_token"])
            )
        if needs_refresh and oauth_tokens._refresh_in_cooldown(state, user_id, server_name):
            return TokenLookupResult(kind="refresh_failed_transient")
        storage = oauth_tokens._get_storage(context)
        server_row = (
            await asyncio.to_thread(storage.get_mcp_server_by_name, server_name)
            if storage is not None
            else None
        )
        if storage is None or server_row is None:
            return _apply_lookup_outcome(
                state, user_id, server_name, _no_token_result(TokenLookupResult(kind="missing"))
            )
        snapshot = _backoff_snapshot(state, user_id, server_name)
        allow_private = oauth_allow_private_network(app_state)
        try:
            runtime = ensure_oauth_runtime(context)
            if runtime is None:
                raise OAuthUnavailableError("OAuth runtime is not configured")
            outcome = await runtime.call(
                lambda: _refresh_user_operation(
                    context=context,
                    storage=storage,
                    token_store=token_store,
                    user_id=user_id,
                    server_name=server_name,
                    server_row=dict(server_row),
                    started=started,
                    force_refresh=force_refresh,
                    backoff=snapshot,
                    revoke_on_failure=revoke_on_failure,
                    revoke_ambiguous_escalation=revoke_ambiguous_escalation,
                    allow_private_network=allow_private,
                )
            )
        except OAuthUnavailableError:
            return TokenLookupResult(kind="refresh_failed_transient")
        return _apply_lookup_outcome(state, user_id, server_name, outcome)


async def _read_obo_credential(
    token_store: MCPTokenStore, user_id: str, server_name: str, issuer: str
) -> _LookupOutcome | token_store_store.OIDCCredentialPlain:
    try:
        credential = await asyncio.to_thread(token_store.get_oidc_credential, user_id, issuer)
    except token_store_crypto.TokenDecryptError as exc:
        return _decrypt_failure_result(
            user_id, server_name, exc, event="mcp_server.oauth.obo_credential_decrypt_failed"
        )
    if credential is None:
        return _no_token_result(TokenLookupResult(kind="missing"))
    return credential


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
    """Coordinate MCP state, then mint under the runtime's shared credential lock.

    A missing cache is normal; a missing credential requests login. A rejected
    server grant may delete its own cache but never the shared credential. Only
    immutable request/backoff data and the typed result cross the loop bridge.
    """
    context = oauth_context(app_state)
    token_store = cast("MCPTokenStore | None", context.token_store)
    if token_store is None:
        log.debug("mcp_server.oauth.token_store_unconfigured")
        return TokenLookupResult(kind="missing")

    # Preserve the rejected token's identity before queuing on the MCP lock:
    # concurrent force-refresh callers can reuse a different token minted by
    # the winner. The read itself does not mutate MCP backoff or lock state.
    pre_lock_token: str | None = None
    if force_refresh:
        try:
            before = await asyncio.to_thread(token_store.get_user_token, user_id, server_name)
        except token_store_crypto.TokenDecryptError:
            before = None
        if before is not None:
            pre_lock_token = before["access_token"]
    state = _mcp_coordination(app_state)
    lock = oauth_locking._refresh_lock_for(state, user_id, server_name)
    async with lock:
        outcome = await _prepare_obo_lookup(
            context=context,
            token_store=token_store,
            state=state,
            user_id=user_id,
            server_name=server_name,
            force_refresh=force_refresh,
            revoke_ambiguous_escalation=revoke_ambiguous_escalation,
            server_row=server_row,
            credential_present=credential_present,
            pre_lock_token=pre_lock_token,
        )
        return _apply_lookup_outcome(state, user_id, server_name, outcome)


async def _refresh_and_persist(
    *,
    app_state: OAuthContext,
    storage: StorageBackend,
    token_store: MCPTokenStore,
    user_id: str,
    server_name: str,
    server_row: dict[str, Any],
    refresh_value: str,
    existing_scopes: str,
    allow_private_network: bool = False,
) -> tuple[str, str | None, str | None]:
    """Run the refresh-grant exchange and persist the new token row.

    Returns ``(access_token, refresh_token_or_none, expires_at_or_none)``.
    Raises :class:`OAuthRefreshError` on failure.
    """
    server_id = str(server_row["server_id"])
    override_url = server_row.get("oauth_authorization_server_url") or None
    cached_issuer = server_row.get("oauth_as_issuer_cached") or None
    client_id = server_row.get("oauth_client_id") or ""
    if not isinstance(client_id, str) or not client_id:
        raise oauth_http.OAuthRefreshError("server has no oauth_client_id")
    metadata_cache = app_state.metadata_cache
    # Refresh runs on the OAuth loop, while browser handlers own the ASGI client.
    # Discovery and the token POST must share the mint client's loop ownership.
    async with oauth_http._enter_mint_client(app_state) as http_client:
        try:
            server_url = _canonical_server_url(server_row)
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
                allow_private_network=allow_private_network,
            )
        except MCPOAuthDiscoveryError as exc:
            if http_client.is_closed:
                raise OAuthUnavailableError("OAuth HTTP client closed during discovery") from exc
            raise oauth_http.OAuthRefreshError(f"discovery failed during refresh: {exc}") from exc

        client_secret: str | None = None
        if token_store is not None:
            try:
                client_secret = await asyncio.to_thread(
                    token_store.get_oauth_client_secret, server_id
                )
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
        tokens = await oauth_grants.refresh_token(
            as_metadata=as_metadata,
            refresh_token_value=refresh_value,
            client_id=client_id,
            client_secret=client_secret,
            resource=server_url,
            scopes=existing_scopes,
            http_client=http_client,
        )

    new_access = tokens.get("access_token")
    if not isinstance(new_access, str) or not new_access:
        raise oauth_http.OAuthRefreshError("refresh response missing access_token")

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
    new_expires_at = oauth_tokens._expires_at_from_response(tokens)

    await durable_write(
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
    storage = oauth_tokens._get_storage(app_state)
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
    """503 response when ``OAuthContext.token_store`` is unconfigured."""
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
    oidc_config = oauth_context(request.app.state).oidc_config
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
    as_metadata: oauth_http.ASMetadata,
    server_row: dict[str, Any],
    server_id: str,
    server_name: str,
    user_id: str,
    redirect_uri: str,
    registration_mode: str,
) -> tuple[str | None, oauth_http.OAuthError | None]:
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
                scopes=server_row.get("oauth_scopes") or "",
            )
        except oauth_http.OAuthError as exc:
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

    token_store = cast("MCPTokenStore | None", oauth_context(request.app.state).token_store)
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

    storage = oauth_tokens._get_storage(request.app.state)
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
    override_url = server_row.get("oauth_authorization_server_url") or None
    cached_issuer = server_row.get("oauth_as_issuer_cached") or None

    try:
        server_url = _canonical_server_url(server_row)
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
            allow_private_network=oauth_allow_private_network(request.app.state),
        )
    except MCPOAuthDiscoveryError as exc:
        log.warning("mcp_server.oauth.discovery_failed", server_name=server_name, exc_info=True)
        return JSONResponse(
            {"error": f"OAuth discovery failed: {oauth_ssrf.sanitize_log_text(str(exc))}"},
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
        server_name=server_name,
        user_id=user_id,
        redirect_uri=redirect_uri,
        registration_mode=registration_mode,
    )
    if dcr_error is not None:
        log.warning("mcp_server.oauth.dcr_failed", server_name=server_name, exc_info=True)
        return JSONResponse(
            {
                "error": (
                    f"Dynamic client registration failed: {oauth_ssrf.sanitize_log_text(str(dcr_error))}"
                )
            },
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

    audience = server_row.get("oauth_audience") or str(server_row.get("url") or "")
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

    token_store = cast("MCPTokenStore | None", oauth_context(request.app.state).token_store)
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

    storage = oauth_tokens._get_storage(request.app.state)
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
            detail={
                "error": oauth_ssrf.sanitize_log_text(error),
                "description": oauth_ssrf.sanitize_log_text(desc),
            },
        )
        return RedirectResponse(
            f"/?mcp_oauth_error={urllib.parse.quote(oauth_ssrf.sanitize_log_text(desc))}",
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
    scopes = server_row.get("oauth_scopes") or ""
    client_id = server_row.get("oauth_client_id") or ""
    if not client_id:
        return RedirectResponse("/?mcp_oauth_error=client_id+missing", status_code=302)

    metadata_cache = getattr(request.app.state, "mcp_oauth_metadata_cache", None)
    override_url = server_row.get("oauth_authorization_server_url") or None
    cached_issuer = server_row.get("oauth_as_issuer_cached") or None

    try:
        server_url = _canonical_server_url(server_row)
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
            allow_private_network=oauth_allow_private_network(request.app.state),
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
            redirect_query=(
                f"mcp_oauth_error={urllib.parse.quote(oauth_ssrf.sanitize_log_text(str(exc)))}"
            ),
        )

    # ``audience=`` is a registered API identifier at the authorization
    # servers that use it instead of ``resource=``, not a resource URL, so
    # its default stays the row's stored spelling —
    # canonicalizing it could rename an identifier the operator registered.
    # ``resource=`` uses the canonical value; the accepted-aud set below
    # carries both.
    audience = server_row.get("oauth_audience") or str(server_row.get("url") or "")
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
            redirect_query=(
                f"mcp_oauth_error={urllib.parse.quote(oauth_ssrf.sanitize_log_text(str(exc)))}"
            ),
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

    # Accept ``aud`` matching the canonical resource URL (what ``resource=``
    # carried) OR the ``oauth_audience`` value, which
    # defaults to the row's stored spelling — so a token minted for either
    # spelling of a not-yet-canonical row still passes.
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
    expires_at = oauth_tokens._expires_at_from_response(tokens)
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

    token_store = cast("MCPTokenStore | None", oauth_context(request.app.state).token_store)
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
    storage = oauth_tokens._get_storage(request.app.state)
    obo_names: set[str] = set()
    if storage is not None:
        try:
            obo_names = await asyncio.to_thread(obo_server_names, storage)
        except Exception:
            obo_names = set()
    # Keep the MCP API's server_name field at the boundary of the shared store.
    return JSONResponse(
        {
            "connections": [
                {
                    "user_id": r["user_id"],
                    "server_name": r["token_key"],
                    "expires_at": r["expires_at"],
                    "scopes": r["scopes"],
                    "as_issuer": r["as_issuer"],
                    "audience": r["audience"],
                    "created": r["created"],
                    "last_refreshed": r["last_refreshed"],
                }
                for r in rows
                if r["token_key"] not in obo_names
                and not str(r["token_key"]).startswith(token_store_store.SYNTHETIC_TOKEN_PREFIXES)
            ]
        }
    )


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
    # Strict unless the caller passes the deployment's opt-in, so a direct
    # caller cannot widen the deployment's reach by omission.
    allow_private_network: bool = False,
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
                server_url=_canonical_server_url(server_row),
                override_url=server_row.get("oauth_authorization_server_url") or None,
                cached_issuer=server_row.get("oauth_as_issuer_cached") or None,
                http_client=http_client,
                storage=storage,
                server_id=server_id_for_audit,
                trusted_hosts=frozenset(),
                metadata_cache=metadata_cache,
                allow_private_network=allow_private_network,
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
                except token_store_crypto.TokenDecryptError:
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

    token_store = cast("MCPTokenStore | None", oauth_context(request.app.state).token_store)
    if token_store is None:
        return _no_token_store_response("revoke_connection")

    user_id = _require_user_id(request)
    if user_id is None:
        return JSONResponse({"error": "Authentication required"}, status_code=401)

    server_name = request.path_params.get("server_name", "").strip()
    if not server_name:
        return JSONResponse({"error": "Missing server_name"}, status_code=400)
    # Synthetic model rows are mint caches, not user-revocable MCP
    # connections. Check before row existence to avoid a 404/409 oracle for
    # whether this user currently has a token for a guessed audience.
    if server_name.startswith(token_store_store.SYNTHETIC_TOKEN_PREFIXES):
        return JSONResponse(
            {
                "error": (
                    "This is an internal model-authentication cache, not an MCP "
                    "connection. It cannot be disconnected from this endpoint."
                )
            },
            status_code=409,
        )

    storage = oauth_tokens._get_storage(request.app.state)
    if storage is None:
        return JSONResponse({"error": "Storage unavailable"}, status_code=503)

    # Decrypt is best-effort: we can't perform an upstream revoke without
    # the plaintext refresh token, but the local delete is authoritative
    # so the consent is invalidated either way. Decrypt failure here is
    # not a hard error — the operator can still revoke locally.
    plain: Any = None
    try:
        plain = await asyncio.to_thread(token_store.get_user_token, user_id, server_name)
    except token_store_crypto.TokenDecryptError:
        plain = None

    # Distinguish "row missing" from "decrypt failed" — a missing row
    # surfaces as 404 (with the same shape used for cross-user attempts
    # so existence is not leaked across tenants).
    if plain is None:
        storage_row = await asyncio.to_thread(storage.get_oauth_token, user_id, server_name)
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
    await _clear_server_coordination(request.app.state, user_id, server_name)

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
                    allow_private_network=oauth_allow_private_network(request.app.state),
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

    storage = oauth_tokens._get_storage(request.app.state)
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

    storage = oauth_tokens._get_storage(request.app.state)
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

    storage = oauth_tokens._get_storage(request.app.state)
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

    Mirrors :func:`turnstone.core.oauth.oidc.initialize_oidc_state` so the
    server / console lifespans can register/teardown symmetrically. Always
    safe to call — installs sentinel state even when no MCP OAuth row
    exists (the route handlers fast-path to 503 when ``OAuthContext.token_store``
    is None).
    """
    app_state.mcp_oauth_http_client = oauth_http.json_http_client()
    app_state.mcp_oauth_coordination = TokenCoordination()
    app_state.mcp_oauth_dcr_locks = {}
    # Metadata is immutable data shared across loops; only HTTP clients are
    # loop-owned. Consent discovery must also warm subsequent runtime refreshes.
    app_state.mcp_oauth_metadata_cache = oauth_context(app_state).metadata_cache
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
    if hasattr(app_state, "mcp_oauth_coordination"):
        app_state.mcp_oauth_coordination = TokenCoordination()
    if hasattr(app_state, "mcp_oauth_dcr_locks"):
        app_state.mcp_oauth_dcr_locks = {}
    if hasattr(app_state, "mcp_oauth_metadata_cache"):
        app_state.mcp_oauth_metadata_cache.clear()
