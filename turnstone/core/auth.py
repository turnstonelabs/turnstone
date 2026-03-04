"""Bearer token authentication and authorization for turnstone HTTP servers.

Opt-in via the ``[auth]`` section in ``config.toml``.  When auth is disabled
(the default), all requests pass through unchecked.  When enabled, API
requests must include a valid ``Authorization: Bearer <token>`` header or
a ``turnstone_auth`` cookie (set via the ``/api/auth/login`` endpoint).
Each token has a role: ``"read"`` or ``"full"``.

Public paths (``/``, ``/static/*``, ``/health``, ``/metrics``,
``/api/auth/login``, ``/api/auth/logout``) are always accessible
without authentication.
"""

from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public / write path classification
# ---------------------------------------------------------------------------

AUTH_COOKIE = "turnstone_auth"

PUBLIC_PATHS: frozenset[str] = frozenset(
    {"/", "/health", "/metrics", "/api/auth/login", "/api/auth/logout"}
)
PUBLIC_PREFIXES: tuple[str, ...] = ("/static/",)

WRITE_PATHS: frozenset[str] = frozenset(
    {
        "/api/send",
        "/api/approve",
        "/api/plan",
        "/api/command",
        "/api/workstreams/new",
        "/api/workstreams/close",
        "/api/cluster/workstreams/new",
    }
)


# ---------------------------------------------------------------------------
# AuthConfig
# ---------------------------------------------------------------------------


@dataclass
class AuthConfig:
    """Auth configuration loaded once at startup (not modified after creation)."""

    enabled: bool = False
    tokens: dict[str, str] = field(default_factory=dict)  # token_value → role

    def check(self, token: str | None) -> str | None:
        """Return the role (``"read"`` or ``"full"``) for a valid token, or *None*."""
        if not token:
            return None
        for known_token, role in self.tokens.items():
            if hmac.compare_digest(token, known_token):
                return role
        return None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_auth_config() -> AuthConfig:
    """Build :class:`AuthConfig` from ``config.toml`` ``[auth]`` + env vars.

    Config format::

        [auth]
        enabled = true

        [[auth.tokens]]
        value = "tok_abc123"
        role = "full"

    Environment variable fallbacks:

    - ``TURNSTONE_AUTH_ENABLED=1`` — enables auth
    - ``TURNSTONE_AUTH_TOKEN=<token>`` — registers a single full-access token
    """
    from turnstone.core.config import load_config

    auth_cfg = load_config("auth")
    enabled = bool(auth_cfg.get("enabled", False))
    tokens: dict[str, str] = {}

    # Tokens from config file (TOML array-of-tables)
    for entry in auth_cfg.get("tokens", []):
        value = entry.get("value", "") if isinstance(entry, dict) else ""
        role = entry.get("role", "read") if isinstance(entry, dict) else ""
        if value and role in ("read", "full"):
            tokens[value] = role

    # Environment variable fallbacks
    if os.environ.get("TURNSTONE_AUTH_ENABLED", "").strip() in ("1", "true", "yes"):
        enabled = True

    env_token = os.environ.get("TURNSTONE_AUTH_TOKEN", "").strip()
    if env_token:
        tokens[env_token] = "full"

    if enabled and not tokens:
        log.warning("Auth enabled but no tokens configured — all API requests will be rejected")

    return AuthConfig(enabled=enabled, tokens=tokens)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def is_public_path(path: str) -> bool:
    """Return *True* if the path should be accessible without authentication."""
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def required_role(method: str, path: str) -> str:
    """Return the minimum role needed for *method* + *path*.

    Returns ``"full"`` for state-modifying POST endpoints, ``"read"`` otherwise.
    Handles console proxy routes (``/node/{id}/api/...``) by extracting the
    proxied path and checking it against ``WRITE_PATHS``.
    """
    normalized = path.rstrip("/") if path != "/" else path
    if method == "POST" and normalized in WRITE_PATHS:
        return "full"
    # Console proxy routes: /node/{node_id}/api/{tail}
    if method == "POST" and normalized.startswith("/node/"):
        parts = normalized.split("/", 4)  # ['', 'node', '{id}', 'api', '{tail}']
        if len(parts) >= 5 and parts[3] == "api":
            proxied_path = "/api/" + parts[4]
            if proxied_path in WRITE_PATHS:
                return "full"
    return "read"


# ---------------------------------------------------------------------------
# Request checking — single entry point for HTTP handlers
# ---------------------------------------------------------------------------


def check_request(
    auth_config: AuthConfig,
    method: str,
    path: str,
    auth_header: str | None,
    cookie_header: str | None = None,
) -> tuple[bool, int, str]:
    """Validate a request against the auth config.

    Checks ``Authorization: Bearer <token>`` first, then falls back to the
    ``turnstone_auth`` cookie (set by ``/api/auth/login``).

    Returns ``(allowed, status_code, message)``.
    On success: ``(True, 200, "")``.
    On failure: ``(False, 401|403, "error message")``.
    """
    if not auth_config.enabled:
        return True, 200, ""

    if is_public_path(path):
        return True, 200, ""

    # Try Bearer header first, then cookie
    token = _extract_bearer(auth_header)
    if token is None:
        token = _extract_cookie(cookie_header, AUTH_COOKIE)
    role = auth_config.check(token)

    if role is None:
        return False, 401, "Unauthorized: missing or invalid token"

    needed = required_role(method, path)
    if needed == "full" and role != "full":
        return False, 403, "Forbidden: read-only token cannot access this endpoint"

    return True, 200, ""


def _extract_bearer(header: str | None) -> str | None:
    """Extract the token from ``Bearer <token>`` header value."""
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


def _extract_cookie(cookie_header: str | None, name: str) -> str | None:
    """Extract a named value from a ``Cookie`` header.

    Assumes token values are simple ASCII (no URL-encoding).
    """
    if not cookie_header:
        return None
    for pair in cookie_header.split(";"):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            if k.strip() == name:
                return v.strip()
    return None


# ---------------------------------------------------------------------------
# Cookie helpers for login/logout endpoints
# ---------------------------------------------------------------------------


def make_set_cookie(token: str, max_age: int = 86400 * 30, secure: bool = False) -> str:
    """Return a ``Set-Cookie`` header value that stores the auth token.

    Set *secure* to ``True`` when serving over HTTPS to add the ``Secure``
    flag (prevents cookie from being sent over plain HTTP).
    """
    val = f"{AUTH_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"
    if secure:
        val += "; Secure"
    return val


def make_clear_cookie() -> str:
    """Return a ``Set-Cookie`` header value that expires the auth cookie."""
    return f"{AUTH_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
