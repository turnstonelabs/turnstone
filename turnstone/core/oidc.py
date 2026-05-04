"""OpenID Connect (OIDC) authentication support for Turnstone.

Implements the Authorization Code Flow with PKCE for secure SSO login.
All external HTTP calls use ``httpx.AsyncClient`` to avoid blocking the
event loop.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import ipaddress
import os
import re
import secrets
import socket
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Mapping

from turnstone.core.log import get_logger

log = get_logger(__name__)

# Sentinel password hash for OIDC-provisioned users.
# Not a valid bcrypt hash -- verify_password() always rejects it.
OIDC_PASSWORD_SENTINEL = "!oidc"

# Lifetime of an OIDC authorization-flow pending-state row. Bounds the window
# between /authorize and /callback; longer than typical IdP latency, shorter
# than a stale browser tab.
OIDC_STATE_TTL_SECONDS = 300

# Sanitisation pattern: only keep safe username characters.
_USERNAME_SAFE_RE = re.compile(r"[^a-zA-Z0-9._-]")

# Asymmetric algorithms accepted for ID token signatures.
# Symmetric (HMAC) algorithms are deliberately excluded to prevent
# algorithm confusion attacks where the IdP's public key is used as
# an HMAC secret.
_ALLOWED_ID_TOKEN_ALGS = [
    "RS256",
    "RS384",
    "RS512",
    "ES256",
    "ES384",
    "ES512",
    "PS256",
    "PS384",
    "PS512",
]

# Well-known IdPs whose discovery documents legitimately reference endpoints on
# hostnames distinct from the issuer hostname. Keys are issuer hostnames; values
# are the set of additional endpoint hostnames the issuer is allowed to delegate
# to. eTLD+1 matching does not work here (e.g. google.com vs googleapis.com),
# so an explicit allow-map is the only safe option.
_KNOWN_TRUSTED_ENDPOINT_HOSTS: dict[str, frozenset[str]] = {
    "accounts.google.com": frozenset(
        {
            "accounts.google.com",
            "oauth2.googleapis.com",
            "www.googleapis.com",
            "openidconnect.googleapis.com",
        }
    ),
}


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class OIDCError(Exception):
    """Raised when an OIDC operation fails."""


class OIDCKeyNotFoundError(OIDCError):
    """Raised when an ID token's signing key is absent from the cached JWKS.

    Distinguishing this from generic OIDCError lets the callback retry once
    after re-fetching JWKS (key rotation), without depending on substring
    matching of the error message.
    """


def _sanitize_log_text(s: str, limit: int) -> str:
    """Escape control characters and truncate untrusted text for log inclusion.

    Untrusted bytes (e.g. an IdP error body) embedded in log lines must not be
    able to forge fake log records via CR/LF or hide content via NULs / other
    control characters. ``unicode_escape`` renders these as visible ``\\r``,
    ``\\n``, ``\\x00`` etc., and the limit caps the *rendered* length.
    """
    return s.encode("unicode_escape").decode("ascii")[:limit]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OIDCConfig:
    """OIDC provider configuration -- immutable after startup.

    The dataclass has a two-phase lifecycle:

    Startup-config fields (set by :func:`load_oidc_config`):
        ``enabled``, ``issuer``, ``client_id``, ``client_secret``, ``scopes``,
        ``provider_name``, ``role_claim``, ``role_map``, ``password_enabled``,
        ``redirect_base``, ``trusted_endpoint_hosts``.

    Discovery-derived fields (set by :func:`discover_oidc`; empty before
    discovery completes):
        ``authorization_endpoint``, ``token_endpoint``, ``userinfo_endpoint``,
        ``jwks_uri``.
    """

    enabled: bool = False
    issuer: str = ""
    client_id: str = ""
    client_secret: str = ""
    scopes: str = "openid email profile"
    provider_name: str = "SSO"
    role_claim: str = ""
    role_map: dict[str, str] = field(default_factory=dict)
    password_enabled: bool = True
    redirect_base: str = ""
    trusted_endpoint_hosts: tuple[str, ...] = ()
    # Discovered from .well-known/openid-configuration
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    userinfo_endpoint: str = ""
    jwks_uri: str = ""


def _parse_role_map(raw: str) -> dict[str, str]:
    """Parse ``"admin:builtin-admin,eng:builtin-operator"`` into a dict."""
    result: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if ":" in pair:
            k, v = pair.split(":", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                result[k] = v
    return result


def _parse_trusted_endpoint_hosts(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated host list into a normalised tuple."""
    hosts: list[str] = []
    for entry in raw.split(","):
        host = entry.strip().lower()
        if host:
            hosts.append(host)
    return tuple(hosts)


def _env_or_cfg_str(env_name: str, cfg: Mapping[str, Any], key: str, default: str = "") -> str:
    """Resolve a string field: env var (stripped, non-empty) wins, else config, else default."""
    val = os.environ.get(env_name, "").strip()
    if not val:
        val = str(cfg.get(key, default)).strip()
    return val


def _env_or_cfg_bool(env_name: str, cfg: Mapping[str, Any], key: str, default: bool) -> bool:
    """Resolve a boolean field: env var (stripped) wins when set, else config, else default."""
    raw = os.environ.get(env_name, "").strip().lower()
    if raw:
        return raw in ("true", "1", "yes")
    return bool(cfg.get(key, default))


def load_oidc_config() -> OIDCConfig:
    """Build :class:`OIDCConfig` from env vars with config.toml fallback.

    Returns ``OIDCConfig(enabled=False)`` when the required fields
    (issuer, client_id, client_secret) are not all present.
    """
    from turnstone.core.config import load_config

    cfg = load_config("oidc")

    issuer = _env_or_cfg_str("TURNSTONE_OIDC_ISSUER", cfg, "issuer")
    client_id = _env_or_cfg_str("TURNSTONE_OIDC_CLIENT_ID", cfg, "client_id")
    client_secret = _env_or_cfg_str("TURNSTONE_OIDC_CLIENT_SECRET", cfg, "client_secret")
    scopes = _env_or_cfg_str("TURNSTONE_OIDC_SCOPES", cfg, "scopes", "openid email profile")
    provider_name = _env_or_cfg_str("TURNSTONE_OIDC_PROVIDER_NAME", cfg, "provider_name", "SSO")
    role_claim = _env_or_cfg_str("TURNSTONE_OIDC_ROLE_CLAIM", cfg, "role_claim")
    password_enabled = _env_or_cfg_bool(
        "TURNSTONE_OIDC_PASSWORD_ENABLED", cfg, "password_enabled", True
    )

    # Role map: env var is "admin:builtin-admin,eng:builtin-operator"
    role_map_raw = os.environ.get("TURNSTONE_OIDC_ROLE_MAP", "").strip()
    if role_map_raw:
        role_map = _parse_role_map(role_map_raw)
    else:
        cfg_role_map = cfg.get("role_map", {})
        role_map = dict(cfg_role_map) if isinstance(cfg_role_map, dict) else {}

    trusted_hosts_raw = os.environ.get("TURNSTONE_OIDC_TRUSTED_ENDPOINT_HOSTS", "").strip()
    if trusted_hosts_raw:
        trusted_endpoint_hosts = _parse_trusted_endpoint_hosts(trusted_hosts_raw)
    else:
        cfg_trusted = cfg.get("trusted_endpoint_hosts", "")
        if isinstance(cfg_trusted, list):
            trusted_endpoint_hosts = _parse_trusted_endpoint_hosts(
                ",".join(str(h) for h in cfg_trusted)
            )
        else:
            trusted_endpoint_hosts = _parse_trusted_endpoint_hosts(str(cfg_trusted))

    redirect_base = _env_or_cfg_str("TURNSTONE_OIDC_REDIRECT_BASE", cfg, "redirect_base").rstrip(
        "/"
    )
    if redirect_base:
        parsed = urllib.parse.urlparse(redirect_base)
        if parsed.scheme not in ("https", "http"):
            log.warning(
                "TURNSTONE_OIDC_REDIRECT_BASE has invalid scheme, ignoring: %s",
                redirect_base,
            )
            redirect_base = ""
        elif not parsed.hostname:
            log.warning(
                "TURNSTONE_OIDC_REDIRECT_BASE missing hostname, ignoring: %s",
                redirect_base,
            )
            redirect_base = ""
        elif parsed.username or parsed.password:
            log.warning(
                "TURNSTONE_OIDC_REDIRECT_BASE must not contain userinfo, ignoring: %s",
                redirect_base,
            )
            redirect_base = ""
        elif parsed.path or parsed.query or parsed.fragment:
            log.warning(
                "TURNSTONE_OIDC_REDIRECT_BASE must be scheme://host[:port] only, ignoring: %s",
                redirect_base,
            )
            redirect_base = ""
        else:
            # Validate port is numeric (urlparse accepts "host:abc" silently).
            try:
                parsed.port  # noqa: B018 — triggers ValueError on non-numeric port
            except ValueError:
                log.warning(
                    "TURNSTONE_OIDC_REDIRECT_BASE has invalid port, ignoring: %s",
                    redirect_base,
                )
                redirect_base = ""
        if redirect_base and parsed.scheme != "https":
            log.warning(
                "TURNSTONE_OIDC_REDIRECT_BASE should use https:// in production: %s",
                redirect_base,
            )

    # OIDC is enabled when all three required fields are non-empty.
    enabled = bool(issuer and client_id and client_secret)

    if enabled:
        log.info("OIDC enabled: issuer=%s provider=%s", issuer, provider_name)
    else:
        log.debug("OIDC not configured (issuer/client_id/client_secret incomplete)")

    return OIDCConfig(
        enabled=enabled,
        issuer=issuer,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
        provider_name=provider_name,
        role_claim=role_claim,
        role_map=role_map,
        password_enabled=password_enabled,
        redirect_base=redirect_base,
        trusted_endpoint_hosts=trusted_endpoint_hosts,
    )


# ---------------------------------------------------------------------------
# SSRF validation
# ---------------------------------------------------------------------------


def _is_localhost(hostname: str) -> bool:
    """Return True if *hostname* refers to the loopback interface."""
    return hostname in ("localhost", "127.0.0.1", "::1") or hostname.endswith(".localhost")


def _effective_port(parsed: urllib.parse.ParseResult) -> int | None:
    """Return the explicit port if set, else the scheme default."""
    if parsed.port is not None:
        return parsed.port
    return {"http": 80, "https": 443}.get(parsed.scheme)


def _validate_url_no_ssrf(url: str, *, allow_http: bool) -> urllib.parse.ParseResult:
    """Run the scheme/userinfo/SSRF checks shared by issuer and discovered URLs.

    Returns the parsed URL on success. Raises :class:`OIDCError` on failure.
    The ``allow_http`` flag is the only knob: when ``True``, ``http://`` is
    accepted *if* the hostname is also a localhost form; when ``False``,
    only ``https://`` is accepted.
    """
    parsed = urllib.parse.urlparse(url)

    hostname = parsed.hostname
    if not hostname:
        raise OIDCError(f"OIDC URL has no hostname: {url}")

    if parsed.username or parsed.password:
        raise OIDCError("OIDC URL must not contain embedded credentials (userinfo)")

    if parsed.scheme != "https":
        if allow_http and parsed.scheme == "http" and _is_localhost(hostname):
            pass
        else:
            raise OIDCError(f"OIDC URL must use HTTPS (got {parsed.scheme}://): {url}")

    try:
        addr_infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise OIDCError(f"OIDC hostname cannot be resolved: {hostname}") from exc

    for _family, _type, _proto, _canonname, sockaddr in addr_infos:
        try:
            addr = ipaddress.ip_address(sockaddr[0])
        except ValueError as exc:
            raise OIDCError(
                f"OIDC hostname resolved to invalid IP {sockaddr[0]!r}: {hostname}"
            ) from exc
        if not addr.is_global and not _is_localhost(hostname):
            raise OIDCError(f"OIDC URL resolves to non-public address ({addr}): {url}")

    return parsed


def validate_issuer_url(url: str) -> None:
    """Validate an OIDC issuer URL to prevent SSRF.

    Rejects:
    - Non-HTTPS URLs (except localhost for development)
    - URLs with embedded credentials (userinfo)
    - Hostnames that resolve to private/internal/loopback IP addresses

    Raises :class:`OIDCError` on validation failure.
    """
    _validate_url_no_ssrf(url, allow_http=True)


def validate_discovered_endpoint(
    url: str,
    issuer_parsed: urllib.parse.ParseResult,
    *,
    allow_http: bool,
    trusted_endpoint_hosts: frozenset[str],
) -> None:
    """Validate an endpoint pulled from an IdP discovery document.

    Applies the same scheme/userinfo/SSRF rules as :func:`validate_issuer_url`,
    then constrains the host: by default the endpoint must share the issuer's
    hostname. Strict equality is intentional — a hostile or compromised IdP
    must not be able to redirect ``token_endpoint`` to a third-party host where
    ``client_secret`` would leak. Multi-origin IdPs (e.g. Google) are
    accommodated via :data:`_KNOWN_TRUSTED_ENDPOINT_HOSTS` plus an
    operator-configurable ``trusted_endpoint_hosts`` list.

    The scheme must match the issuer's scheme, and the *effective* port (with
    scheme defaults applied) must match — so ``https://host`` and
    ``https://host:443`` are treated as identical.

    ``allow_http`` should track whether the *issuer* URL was localhost, so the
    whole flow is allowed to be HTTP only in dev mode.

    Raises :class:`OIDCError` on validation failure.
    """
    parsed = _validate_url_no_ssrf(url, allow_http=allow_http)

    issuer_hostname = (issuer_parsed.hostname or "").lower()
    endpoint_hostname = (parsed.hostname or "").lower()

    if parsed.scheme != issuer_parsed.scheme:
        raise OIDCError(
            f"OIDC discovered endpoint scheme ({parsed.scheme}) "
            f"does not match issuer ({issuer_parsed.scheme}): {url}"
        )

    known_trusted = _KNOWN_TRUSTED_ENDPOINT_HOSTS.get(issuer_hostname, frozenset())
    host_allowed = (
        endpoint_hostname == issuer_hostname
        or endpoint_hostname in known_trusted
        or endpoint_hostname in trusted_endpoint_hosts
    )
    if not host_allowed:
        raise OIDCError(
            f"OIDC discovered endpoint host ({endpoint_hostname}) "
            f"does not match issuer ({issuer_hostname}) and is not trusted: {url}"
        )

    endpoint_port = _effective_port(parsed)
    issuer_port = _effective_port(issuer_parsed)
    if endpoint_port != issuer_port:
        raise OIDCError(
            f"OIDC discovered endpoint port ({endpoint_port}) "
            f"does not match issuer ({issuer_port}): {url}"
        )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


async def discover_oidc(
    config: OIDCConfig,
    *,
    client: httpx.AsyncClient | None = None,
) -> OIDCConfig:
    """Fetch OIDC discovery document and return updated config with endpoints.

    On failure, logs a warning and returns config with ``enabled=False``.

    A long-lived ``client`` may be supplied to amortise TLS / connection
    setup across calls; when ``None`` a transient client is used (the
    legacy shape, kept so tests don't need lifecycle management).
    """
    if not config.issuer:
        return dataclasses.replace(config, enabled=False)

    try:
        issuer_parsed = _validate_url_no_ssrf(config.issuer, allow_http=True)
    except OIDCError as exc:
        log.warning("OIDC issuer URL rejected: %s", exc)
        return dataclasses.replace(config, enabled=False)

    url = config.issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        if client is not None:
            resp = await client.get(url, timeout=10.0)
            resp.raise_for_status()
            doc = resp.json()
        else:
            async with httpx.AsyncClient(timeout=10.0) as transient:
                resp = await transient.get(url)
                resp.raise_for_status()
                doc = resp.json()
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        # ValueError covers json.JSONDecodeError (subclass).
        log.warning("OIDC discovery failed for %s: %s", config.issuer, exc, exc_info=True)
        return dataclasses.replace(config, enabled=False)

    if not isinstance(doc, dict):
        log.warning(
            "OIDC discovery document for %s is not a JSON object (got %s)",
            config.issuer,
            type(doc).__name__,
        )
        return dataclasses.replace(config, enabled=False)

    authorization_endpoint = str(doc.get("authorization_endpoint", ""))
    token_endpoint = str(doc.get("token_endpoint", ""))
    userinfo_endpoint = str(doc.get("userinfo_endpoint", ""))
    jwks_uri = str(doc.get("jwks_uri", ""))

    if not authorization_endpoint or not token_endpoint or not jwks_uri:
        log.warning(
            "OIDC discovery document missing required endpoints for %s",
            config.issuer,
        )
        return dataclasses.replace(config, enabled=False)

    allow_http = _is_localhost(issuer_parsed.hostname or "")
    trusted_hosts = frozenset(h.lower() for h in config.trusted_endpoint_hosts)
    required = (
        ("authorization_endpoint", authorization_endpoint),
        ("token_endpoint", token_endpoint),
        ("jwks_uri", jwks_uri),
    )
    for name, endpoint_url in required:
        try:
            validate_discovered_endpoint(
                endpoint_url,
                issuer_parsed,
                allow_http=allow_http,
                trusted_endpoint_hosts=trusted_hosts,
            )
        except OIDCError as exc:
            log.warning("OIDC discovered %s rejected (url=%s): %s", name, endpoint_url, exc)
            return dataclasses.replace(config, enabled=False)

    if userinfo_endpoint:
        try:
            validate_discovered_endpoint(
                userinfo_endpoint,
                issuer_parsed,
                allow_http=allow_http,
                trusted_endpoint_hosts=trusted_hosts,
            )
        except OIDCError as exc:
            log.warning(
                "OIDC discovered userinfo_endpoint rejected (url=%s): %s",
                userinfo_endpoint,
                exc,
            )
            return dataclasses.replace(config, enabled=False)

    log.info("OIDC discovery complete: %s", config.issuer)
    return dataclasses.replace(
        config,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        userinfo_endpoint=userinfo_endpoint,
        jwks_uri=jwks_uri,
    )


# ---------------------------------------------------------------------------
# JWKS key management
# ---------------------------------------------------------------------------


async def fetch_jwks(
    jwks_uri: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch the JWKS key set from the IdP.

    Returns the parsed JSON document (``{"keys": [...]}``) .  Called during
    startup discovery and on-demand when an unknown ``kid`` is encountered
    (key rotation).  Uses ``httpx.AsyncClient`` — never blocks the event loop.

    A long-lived ``client`` may be supplied to share connection pooling;
    when ``None`` a transient client is used.

    Raises :class:`OIDCError` on HTTP error or malformed JSON.
    """
    try:
        if client is not None:
            resp = await client.get(jwks_uri, timeout=10.0)
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
        else:
            async with httpx.AsyncClient(timeout=10.0) as transient:
                resp = await transient.get(jwks_uri)
                resp.raise_for_status()
                result = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        # ValueError covers json.JSONDecodeError (subclass).
        raise OIDCError(f"JWKS fetch failed: {exc}") from exc
    if not isinstance(result.get("keys"), list):
        raise OIDCError("JWKS document missing 'keys' array")
    return result


# ---------------------------------------------------------------------------
# Lifespan integration
# ---------------------------------------------------------------------------


async def initialize_oidc_state(app_state: Any) -> None:
    """Run OIDC discovery + JWKS prefetch and stash results on ``app_state``.

    Reads ``app_state.oidc_config`` (already set to a non-discovered config
    by the lifespan), runs :func:`discover_oidc` and :func:`fetch_jwks`, and
    writes back the populated ``oidc_config`` plus ``jwks_data``. On any
    failure the helper guarantees ``oidc_config.enabled is False`` and
    ``jwks_data is None`` -- the caller does not need defensive logic.

    Also installs a long-lived ``httpx.AsyncClient`` at
    ``app_state.oidc_http_client`` so subsequent OIDC outbound calls can
    reuse a single connection pool.  Pair with :func:`close_oidc_state`
    in the lifespan teardown.
    """
    cfg: OIDCConfig = app_state.oidc_config
    if not cfg.enabled:
        app_state.jwks_data = None
        app_state.oidc_http_client = None
        app_state.jwks_refetch_lock = asyncio.Lock()
        return

    http_client = httpx.AsyncClient(timeout=10.0)
    app_state.oidc_http_client = http_client
    app_state.jwks_refetch_lock = asyncio.Lock()

    # Discovery is operator-controlled config; any unexpected failure must
    # disable OIDC rather than escape and bring down the whole service.
    try:
        cfg = await discover_oidc(cfg, client=http_client)
    except Exception:
        log.warning("OIDC discovery failed -- OIDC login disabled", exc_info=True)
        app_state.oidc_config = dataclasses.replace(cfg, enabled=False)
        app_state.jwks_data = None
        return

    if not cfg.enabled:
        app_state.oidc_config = cfg
        app_state.jwks_data = None
        return

    if not cfg.redirect_base:
        log.error(
            "OIDC enabled but TURNSTONE_OIDC_REDIRECT_BASE is unset. "
            "This is required to prevent Host-header-derived redirect_uri spoofing. "
            "Set it to your service's externally-visible URL "
            "(e.g. https://idp.example.com). OIDC will be disabled."
        )
        app_state.oidc_config = dataclasses.replace(cfg, enabled=False)
        app_state.jwks_data = None
        return

    try:
        jwks_data = await fetch_jwks(cfg.jwks_uri, client=http_client)
    except OIDCError:
        # Keep enabled=True so the callback's lazy-fetch retry path can
        # recover if the IdP transiently failed during startup.
        log.warning("OIDC JWKS prefetch failed -- will retry on first login", exc_info=True)
        app_state.oidc_config = cfg
        app_state.jwks_data = None
        return

    app_state.oidc_config = cfg
    app_state.jwks_data = jwks_data
    log.info("OIDC enabled: %s (%s)", cfg.provider_name, cfg.issuer)


async def close_oidc_state(app_state: Any) -> None:
    """Close the long-lived OIDC HTTP client installed by :func:`initialize_oidc_state`.

    Safe to call when OIDC was never enabled — does nothing.
    """
    client = getattr(app_state, "oidc_http_client", None)
    if client is not None:
        try:
            await client.aclose()
        except Exception:
            log.debug("OIDC http client close failed", exc_info=True)
        app_state.oidc_http_client = None


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------


def generate_pkce_verifier() -> str:
    """Generate a PKCE code_verifier.

    The matching code_challenge is recomputed from the verifier inside
    :func:`build_authorize_url`, so callers that only need the verifier
    (the value stored in pending state for the callback's token exchange)
    don't have to discard a separately-returned challenge.
    """
    return secrets.token_urlsafe(48)


# ---------------------------------------------------------------------------
# Authorization URL
# ---------------------------------------------------------------------------


def build_authorize_url(
    config: OIDCConfig,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_verifier: str,
) -> str:
    """Build the OIDC authorization URL with PKCE."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "scope": config.scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return config.authorization_endpoint + "?" + urllib.parse.urlencode(params)


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------


async def exchange_code(
    config: OIDCConfig,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Exchange authorization code for tokens at the token endpoint.

    A long-lived ``client`` may be supplied; when ``None`` a transient
    client is used.

    Raises :class:`OIDCError` on non-200 response.
    """
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "code_verifier": code_verifier,
    }
    try:
        if client is not None:
            resp = await client.post(config.token_endpoint, data=data, timeout=10.0)
        else:
            async with httpx.AsyncClient(timeout=10.0) as transient:
                resp = await transient.post(config.token_endpoint, data=data)
    except Exception as exc:
        raise OIDCError(f"Token exchange request failed: {exc}") from exc

    if resp.status_code != 200:
        raise OIDCError(
            f"Token endpoint returned {resp.status_code}: {_sanitize_log_text(resp.text, 500)}"
        )

    result = resp.json()
    if not isinstance(result, dict):
        raise OIDCError("Token endpoint returned non-dict body")
    typed: dict[str, Any] = result
    return typed


# ---------------------------------------------------------------------------
# ID token validation
# ---------------------------------------------------------------------------


def validate_id_token(
    raw_token: str,
    jwks_data: dict[str, Any],
    config: OIDCConfig,
    nonce: str,
) -> dict[str, Any]:
    """Validate and decode an OIDC ID token.  Returns decoded claims.

    *jwks_data* is the pre-fetched JWKS document (the ``{"keys": [...]}``
    dict).  No network I/O happens here — the signing key is resolved
    locally from the cached key set.

    Raises :class:`OIDCError` on validation failure.
    """
    import jwt
    from jwt import PyJWK

    # Extract kid from the token header to find the matching key.
    try:
        header = jwt.get_unverified_header(raw_token)
    except jwt.DecodeError as exc:
        raise OIDCError(f"Invalid ID token header: {exc}") from exc

    kid = header.get("kid")  # None if absent, not ""

    # Find matching key in the JWKS by kid.
    # PyJWK infers the key's algorithm from the JWKS ``alg``/``kty``
    # fields.  jwt.decode() requires the token header's ``alg`` to be in
    # our _ALLOWED_ID_TOKEN_ALGS allowlist (asymmetric only) AND to match
    # the key type — preventing algorithm confusion attacks.
    signing_key = None
    for key_dict in jwks_data.get("keys", []):
        if kid is not None and key_dict.get("kid") == kid:
            try:
                signing_key = PyJWK(key_dict)
            except Exception as exc:
                raise OIDCError(f"Failed to parse signing key: {exc}") from exc
            break

    # Fallback: if token has no kid and JWKS has exactly one key, use it.
    if signing_key is None and kid is None:
        keys = jwks_data.get("keys", [])
        if len(keys) == 1:
            try:
                signing_key = PyJWK(keys[0])
            except Exception as exc:
                raise OIDCError(f"Failed to parse signing key: {exc}") from exc

    if signing_key is None:
        raise OIDCKeyNotFoundError(f"Signing key '{kid}' not found in JWKS")

    try:
        claims: dict[str, Any] = jwt.decode(
            raw_token,
            signing_key.key,
            algorithms=_ALLOWED_ID_TOKEN_ALGS,
            audience=config.client_id,
            issuer=config.issuer,
        )
    except jwt.InvalidTokenError as exc:
        raise OIDCError(f"ID token validation failed: {exc}") from exc

    if claims.get("nonce") != nonce:
        raise OIDCError("ID token nonce mismatch")

    return claims


# ---------------------------------------------------------------------------
# User provisioning
# ---------------------------------------------------------------------------


def provision_oidc_user(
    storage: Any,
    config: OIDCConfig,
    claims: dict[str, Any],
) -> dict[str, str]:
    """Match or create a user from OIDC claims. Returns user dict.

    Looks up an existing OIDC identity by (issuer, sub).  If found,
    updates ``last_login`` and applies role mapping.  Otherwise creates
    a new user and OIDC identity record atomically.

    Raises :class:`OIDCError` if user creation fails or if a concurrent
    callback wins the username / identity race.
    """
    from turnstone.core.storage import StorageConflictError

    issuer = config.issuer
    sub = str(claims["sub"])
    email = str(claims.get("email", ""))
    display_name = str(claims.get("name", "") or claims.get("preferred_username", "") or email)

    # Try to find existing identity
    identity = storage.get_oidc_identity(issuer, sub)
    if identity is not None:
        user_id = identity["user_id"]
        storage.update_oidc_identity_login(issuer, sub)
        apply_role_mapping(storage, user_id, claims, config)
        user: dict[str, str] | None = storage.get_user(user_id)
        if user is None:
            raise OIDCError(f"OIDC identity references missing user: {user_id}")
        return user

    # New user -- derive username, then create user + identity atomically.
    username = _derive_username(storage, claims)
    user_id = uuid.uuid4().hex

    try:
        storage.create_oidc_user(
            user_id,
            username,
            display_name,
            OIDC_PASSWORD_SENTINEL,
            issuer,
            sub,
            email,
        )
    except StorageConflictError as exc:
        raise OIDCError(f"OIDC provisioning failed: {exc}") from exc

    desired_role_ids = apply_role_mapping(storage, user_id, claims, config)

    # Fresh user with no IdP-mapped roles: fall back to builtin-viewer so
    # they can access the app.  Skipping the per-row list_user_roles
    # round-trip is safe because nothing else has had a chance to assign
    # a role to this just-created user_id.
    #
    # ``assigned_by="oidc-default"`` deliberately differs from the
    # ``"oidc"`` marker used by claim-driven role mapping: ``apply_role_mapping``
    # only revokes rows tagged ``"oidc"`` when the corresponding claim
    # disappears, so the safety-net role survives every subsequent login
    # regardless of what the IdP sends in the claim.
    if not desired_role_ids and storage.get_role("builtin-viewer") is not None:
        storage.assign_role(user_id, "builtin-viewer", "oidc-default")

    created_user: dict[str, str] | None = storage.get_user(user_id)
    if created_user is None:
        raise OIDCError(f"Failed to retrieve newly created user: {user_id}")

    log.info("Provisioned OIDC user: %s (%s) from %s", username, user_id, issuer)
    return created_user


def _derive_username(storage: Any, claims: dict[str, Any]) -> str:
    """Derive a unique, valid username from OIDC claims."""
    from turnstone.core.auth import is_valid_username

    raw = str(claims.get("preferred_username", ""))
    if not raw:
        email = str(claims.get("email", ""))
        raw = email.split("@")[0] if email else ""
    if not raw:
        raw = "user"

    # Sanitise: keep only safe chars, truncate.
    sanitised = _USERNAME_SAFE_RE.sub("", raw)[:64]
    if not sanitised:
        sanitised = "user"

    # Build the full set of bounded candidates (base + 2..10 suffixes), strip
    # invalid forms, then ask storage which ones are already taken in one query.
    candidates = [sanitised, *(f"{sanitised[:60]}{n}" for n in range(2, 11))]
    valid_candidates = [c for c in candidates if is_valid_username(c)]
    existing = storage.find_existing_usernames(valid_candidates)
    for candidate in valid_candidates:
        if candidate not in existing:
            return candidate

    # Last resort: full UUID suffix with validation + uniqueness check.
    for _ in range(3):
        candidate = f"{sanitised[:32]}{uuid.uuid4().hex}"
        if not is_valid_username(candidate):
            candidate = f"user{uuid.uuid4().hex}"
        if storage.get_user_by_username(candidate) is None:
            return candidate
    raise OIDCError("Failed to generate unique username")


# ---------------------------------------------------------------------------
# Role mapping
# ---------------------------------------------------------------------------


def apply_role_mapping(
    storage: Any,
    user_id: str,
    claims: dict[str, Any],
    config: OIDCConfig,
) -> set[str]:
    """Sync Turnstone roles from OIDC claims.  Returns desired role id set.

    If ``config.role_claim`` is set, reads the corresponding claim value,
    normalises it to a list, and maps each value via ``config.role_map``
    to a Turnstone role ID.  Roles assigned by OIDC on previous logins
    that are no longer present in the claims are revoked (IdP demotions
    propagate).  Roles assigned manually or by other sources (including
    the ``oidc-default`` builtin-viewer fallback) are never touched.

    The returned ``desired_role_ids`` lets the caller decide whether to
    apply the new-user fallback role without a second ``list_user_roles``
    round-trip.
    """
    if not config.role_claim or not config.role_map:
        return set()

    claim_value = claims.get(config.role_claim)

    # Normalise to list (could be string, list, or absent from IdP).
    if claim_value is None:
        values: list[str] = []
    elif isinstance(claim_value, str):
        values = [claim_value]
    elif isinstance(claim_value, list):
        values = [str(v) for v in claim_value]
    else:
        values = [str(claim_value)]

    # Compute the set of roles the IdP says this user should have.
    desired_role_ids: set[str] = set()
    for value in values:
        role_id = config.role_map.get(value)
        if role_id and storage.get_role(role_id) is not None:
            desired_role_ids.add(role_id)

    added, removed = storage.replace_oidc_roles(user_id, desired_role_ids)
    for role_id in added:
        log.debug("Assigned role %s to user %s via OIDC claim", role_id, user_id)
    for role_id in removed:
        log.info("Revoked role %s from user %s (removed from IdP claims)", role_id, user_id)

    return desired_role_ids
