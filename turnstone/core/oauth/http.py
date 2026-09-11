"""Shared OAuth HTTP posture and token-endpoint failure classification."""

from __future__ import annotations

import contextlib
import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from turnstone.core.log import get_logger
from turnstone.core.oauth import ssrf as oauth_ssrf
from turnstone.core.oauth.work import OAuthUnavailableError

if TYPE_CHECKING:
    from turnstone.core.oauth.context import OAuthContext

log = get_logger(__name__)

_DEFAULT_HTTP_TIMEOUT = 10.0


# Tighter cap for token-endpoint and DCR responses — these never carry
# JWKS-style payloads and are bounded in well-formed AS implementations.
_MAX_TOKEN_BODY_BYTES = 64 * 1024


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OAuthError(Exception):
    """Base class for OAuth protocol errors."""


class RefreshFailureClass(enum.Enum):
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


class OAuthRefreshError(OAuthError):
    """Refresh-token grant failed; ``failure_class`` tells the caller how to react.

    See :class:`RefreshFailureClass` for the three handling classes. Defaults to
    ``TRANSIENT`` — the safe direction, since the caller then keeps the token
    rather than revoking a user's consent on an unclassified failure.
    """

    def __init__(
        self,
        message: str = "",
        *,
        failure_class: RefreshFailureClass = RefreshFailureClass.TRANSIENT,
    ) -> None:
        super().__init__(message)
        self.failure_class = failure_class


def json_http_client(timeout: float = _DEFAULT_HTTP_TIMEOUT) -> httpx.AsyncClient:
    """Return the JSON-preferring HTTP client used by OAuth consumers.

    Every request here consumes a JSON body: discovery documents, dynamic
    client registration, token exchange, refresh, revocation, and the OBO
    grant legs. Some token endpoints content-negotiate and answer a bare
    ``Accept: */*`` with a form-encoded body the parsers cannot read, so
    the JSON preference is the client default rather than a per-call
    literal that the next call site would forget. httpx applies a
    per-request ``headers=`` over this default, so a caller can still
    override it.
    """
    return httpx.AsyncClient(timeout=timeout, headers={"Accept": "application/json"})


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
        return oauth_ssrf.sanitize_log_text(resp.text, 200)
    composite = " ".join(parts)
    return oauth_ssrf.sanitize_log_text(redact_credentials(composite), 200)


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


def _classify_refresh_failure(resp: httpx.Response) -> RefreshFailureClass:
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
        return RefreshFailureClass.PERMANENT
    if status in (400, 401) and code not in _TRANSIENT_AS_ERRORS:
        return RefreshFailureClass.AMBIGUOUS
    return RefreshFailureClass.TRANSIENT


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
    paths cannot drift. Raises :class:`OAuthRefreshError` for HTTP transport,
    status, or response-validation failures; a non-200 carries the conservative
    classification — an explicit
    dead-grant / re-consent code revokes consent; infra (5xx/429) and
    operator-fixable codes keep the token; an unrecognised 400/401 is
    ambiguous and the caller escalates only after a sustained run.

    The two labels preserve each caller's historical error text verbatim
    (``refresh request failed`` vs ``refresh endpoint returned HTTP …``);
    the OBO wrapper passes one string for both. Callers always supply
    *http_client*. Mint and per-user refresh orchestration select the current
    loop's client through :func:`_enter_mint_client`; the same client spans
    both RFC 8693 legs, or refresh discovery and its token POST.

    ``classify_oversized_by_status`` controls how an OVER-sized error body is
    classified. The oauth_user refresh path keeps the default (``False`` →
    TRANSIENT), byte-identical to the pre-refactor behavior, so a large upstream
    error can never escalate a pre-existing consent to re-consent. The OBO legs
    pass ``True`` so an over-sized client-error body is AMBIGUOUS (it can't read
    the body to pin PERMANENT without defeating the guard) and still escalates
    to the honest re-login/admin remedy instead of looping "please retry".
    """
    try:
        resp = await http_client.post(
            token_endpoint,
            data=data,
            timeout=_DEFAULT_HTTP_TIMEOUT,
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        if http_client.is_closed:
            raise OAuthUnavailableError("OAuth HTTP client closed during shutdown") from exc
        if isinstance(exc, RuntimeError):
            raise
        raise OAuthRefreshError(f"{request_label} request failed: {exc}") from exc

    if http_client.is_closed:
        raise OAuthUnavailableError("OAuth HTTP client closed during request")

    if len(resp.content) > _MAX_TOKEN_BODY_BYTES:
        oversized_class = (
            RefreshFailureClass.AMBIGUOUS
            if classify_oversized_by_status and resp.status_code in (400, 401, 403)
            else RefreshFailureClass.TRANSIENT
        )
        raise OAuthRefreshError(
            f"{endpoint_label} response body exceeds size limit",
            failure_class=oversized_class,
        )

    if resp.status_code != 200:
        raise OAuthRefreshError(
            f"{endpoint_label} returned HTTP {resp.status_code}: {_format_as_error(resp)}",
            failure_class=_classify_refresh_failure(resp),
        )

    try:
        doc = resp.json()
    except ValueError as exc:
        raise OAuthRefreshError(f"{request_label} body is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise OAuthRefreshError(f"{request_label} body is not a JSON object")
    typed: dict[str, Any] = doc
    return typed


@contextlib.asynccontextmanager
async def _enter_mint_client(context: OAuthContext) -> Any:
    """Yield the runtime's owner-loop client for discovery and token grants."""
    client = context.http_client
    if client is None or client.is_closed:
        raise OAuthUnavailableError("OAuth runtime HTTP client is not configured")
    yield client


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
