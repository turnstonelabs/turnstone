"""Bearer token authentication and authorization for turnstone HTTP servers.

Supports three token types:

1. **Config-file tokens** — static tokens in ``config.toml`` or the
   ``TURNSTONE_AUTH_TOKEN`` env var.  Validated in-memory via
   ``hmac.compare_digest``.  Map to scopes via their role.
2. **API tokens** — database-backed, prefixed ``ts_``, stored as SHA-256
   hashes.  Exchanged for JWTs via ``/api/auth/login``.
3. **JWTs** — short-lived session tokens issued after API token validation.
   Validated locally via shared HMAC-SHA256 secret.  Contain user_id and
   scopes in claims.

Public paths (``/``, ``/static/*``, ``/shared/*``, ``/health``, ``/metrics``,
``/openapi.json``, ``/docs``, ``/api/auth/login``, ``/api/auth/logout``) are
always accessible without authentication.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AUTH_COOKIE = "turnstone_auth"
TOKEN_PREFIX = "ts_"
TOKEN_BYTES = 32  # 64 hex chars after prefix

VALID_SCOPES: frozenset[str] = frozenset({"read", "write", "approve"})

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
USERNAME_MAX_LEN = 64


def is_valid_username(username: str) -> bool:
    """Return True if *username* contains only safe characters (letters, digits, `.`, `_`, `-`)."""
    return (
        bool(username)
        and len(username) <= USERNAME_MAX_LEN
        and _USERNAME_RE.match(username) is not None
    )


# Hierarchical: each scope implies all lower scopes.
SCOPE_HIERARCHY: dict[str, frozenset[str]] = {
    "read": frozenset({"read"}),
    "write": frozenset({"read", "write"}),
    "approve": frozenset({"read", "write", "approve"}),
}

# Map old role names to scope sets.
_ROLE_TO_SCOPES: dict[str, frozenset[str]] = {
    "read": frozenset({"read"}),
    "full": frozenset({"read", "write", "approve"}),
}

# ---------------------------------------------------------------------------
# Path classification
# ---------------------------------------------------------------------------

PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/",
        "/health",
        "/metrics",
        "/openapi.json",
        "/docs",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/status",
        "/api/auth/setup",
    }
)
PUBLIC_PREFIXES: tuple[str, ...] = ("/static/", "/shared/")

WRITE_PATHS: frozenset[str] = frozenset(
    {
        "/api/send",
        "/api/plan",
        "/api/command",
        "/api/workstreams/new",
        "/api/workstreams/close",
        "/api/cluster/workstreams/new",
    }
)

APPROVE_PATHS: frozenset[str] = frozenset({"/api/approve"})
ADMIN_PREFIX = "/api/admin/"


def _strip_version_prefix(path: str) -> str:
    """Strip ``/v1`` prefix for path classification."""
    if path.startswith("/v1/"):
        return path[3:]
    return path


# ---------------------------------------------------------------------------
# AuthResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthResult:
    """Result of successful authentication."""

    user_id: str  # empty string for config-file tokens
    scopes: frozenset[str]
    token_source: str  # "config", "jwt", "database"

    def has_scope(self, scope: str) -> bool:
        """Return True if this result includes *scope*."""
        return scope in self.scopes


# ---------------------------------------------------------------------------
# AuthConfig (unchanged from before — static config-file tokens)
# ---------------------------------------------------------------------------


@dataclass
class AuthConfig:
    """Auth configuration loaded once at startup (not modified after creation)."""

    enabled: bool = False
    tokens: dict[str, str] = field(default_factory=dict)  # token_value → role

    def check(self, token: str | None) -> str | None:
        """Return the role for a valid config token, or *None*."""
        if not token:
            return None
        for known_token, role in self.tokens.items():
            if hmac.compare_digest(token, known_token):
                return role
        return None


# ---------------------------------------------------------------------------
# Token generation and hashing
# ---------------------------------------------------------------------------


def generate_token() -> str:
    """Generate a new API token: ``ts_`` + 64 hex chars (32 random bytes)."""
    return TOKEN_PREFIX + secrets.token_hex(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """Return the SHA-256 hex digest of *token*."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_prefix(token: str) -> str:
    """Return the first 8 characters of a raw token (for display in listings)."""
    return token[:8]


# ---------------------------------------------------------------------------
# Password hashing (bcrypt)
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Hash a password with bcrypt. Returns the hash as a string."""
    import bcrypt

    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against a bcrypt hash."""
    import bcrypt

    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def parse_scopes(scopes_str: str) -> frozenset[str]:
    """Parse comma-separated scopes and expand via hierarchy.

    ``"approve"`` expands to ``{"read", "write", "approve"}``.
    """
    raw = {s.strip() for s in scopes_str.split(",") if s.strip()}
    expanded: set[str] = set()
    for scope in raw:
        expanded |= SCOPE_HIERARCHY.get(scope, frozenset({scope}))
    return frozenset(expanded & VALID_SCOPES)


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def load_jwt_secret() -> str:
    """Load JWT signing secret from env or config, or auto-generate."""
    secret = os.environ.get("TURNSTONE_JWT_SECRET", "").strip()
    if secret:
        return secret

    from turnstone.core.config import load_config

    auth_cfg = load_config("auth")
    secret = str(auth_cfg.get("jwt_secret", "")).strip()
    if secret:
        return secret

    # Auto-generate an ephemeral secret
    secret = secrets.token_hex(32)
    log.warning(
        "No JWT secret configured — using ephemeral secret (tokens will not survive restart)"
    )
    return secret


def create_jwt(
    user_id: str,
    scopes: frozenset[str],
    source: str,
    secret: str,
    expiry_hours: int = 24,
) -> str:
    """Create a signed JWT with user identity and scopes."""
    import jwt

    now = int(time.time())
    payload = {
        "sub": user_id,
        "scopes": ",".join(sorted(scopes)),
        "src": source,
        "iat": now,
        "exp": now + expiry_hours * 3600,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def validate_jwt(token: str, secret: str) -> AuthResult | None:
    """Validate a JWT and return an AuthResult, or None on failure."""
    import jwt

    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None

    user_id = payload.get("sub", "")
    scopes_str = payload.get("scopes", "")
    source = payload.get("src", "jwt")

    return AuthResult(
        user_id=user_id,
        scopes=parse_scopes(scopes_str),
        token_source=source,
    )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_auth_config() -> AuthConfig:
    """Build :class:`AuthConfig` from ``config.toml`` ``[auth]`` + env vars.

    Auth is **enabled by default**.  Set ``[auth] enabled = false`` or
    ``TURNSTONE_AUTH_ENABLED=0`` to disable.

    Config format::

        [auth]
        enabled = false   # opt out

        [[auth.tokens]]
        value = "tok_abc123"
        role = "full"

    Environment variables:

    - ``TURNSTONE_AUTH_ENABLED=0`` — disables auth
    - ``TURNSTONE_AUTH_ENABLED=1`` — enables auth (default)
    - ``TURNSTONE_AUTH_TOKEN=<token>`` — registers a single full-access token
    """
    from turnstone.core.config import load_config

    auth_cfg = load_config("auth")
    enabled = bool(auth_cfg.get("enabled", True))
    tokens: dict[str, str] = {}

    # Tokens from config file (TOML array-of-tables)
    for entry in auth_cfg.get("tokens", []):
        value = entry.get("value", "") if isinstance(entry, dict) else ""
        role = entry.get("role", "read") if isinstance(entry, dict) else ""
        if value and role in ("read", "full"):
            tokens[value] = role

    # Environment variable overrides
    env_enabled = os.environ.get("TURNSTONE_AUTH_ENABLED", "").strip().lower()
    if env_enabled in ("1", "true", "yes"):
        enabled = True
    elif env_enabled in ("0", "false", "no"):
        enabled = False

    env_token = os.environ.get("TURNSTONE_AUTH_TOKEN", "").strip()
    if env_token:
        tokens[env_token] = "full"

    if enabled and not tokens:
        log.info("Auth enabled (no config tokens — use /api/auth/setup or turnstone-admin)")

    return AuthConfig(enabled=enabled, tokens=tokens)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def is_public_path(path: str) -> bool:
    """Return *True* if the path should be accessible without authentication."""
    normalized = _strip_version_prefix(path)
    if normalized in PUBLIC_PATHS:
        return True
    return any(normalized.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def required_scope(method: str, path: str) -> str:
    """Return the minimum scope needed for *method* + *path*.

    Returns ``"approve"`` for the approve endpoint and admin paths,
    ``"write"`` for other state-modifying POST endpoints, ``"read"`` otherwise.
    """
    normalized = _strip_version_prefix(path)
    normalized = normalized.rstrip("/") if normalized != "/" else normalized

    # Admin endpoints require approve scope
    if normalized.startswith(ADMIN_PREFIX):
        return "approve"

    # Approve endpoint
    if method == "POST" and normalized in APPROVE_PATHS:
        return "approve"

    # Write endpoints
    if method == "POST" and normalized in WRITE_PATHS:
        return "write"

    # Console proxy routes: /node/{node_id}/api/{tail} or /node/{node_id}/v1/api/{tail}
    if method == "POST" and normalized.startswith("/node/"):
        proxied = _extract_proxied_path(normalized)
        if proxied:
            if proxied in APPROVE_PATHS:
                return "approve"
            if proxied in WRITE_PATHS:
                return "write"

    return "read"


def required_role(method: str, path: str) -> str:
    """Return the minimum role needed (legacy — maps scope to old role name)."""
    scope = required_scope(method, path)
    return "full" if scope in ("write", "approve") else "read"


def _extract_proxied_path(normalized: str) -> str | None:
    """Extract the inner API path from a console proxy route."""
    parts = normalized.split("/", 4)  # ['', 'node', '{id}', 'api'|'v1', ...]
    if len(parts) < 5:
        return None
    if parts[3] == "api":
        return "/api/" + parts[4]
    if parts[3] == "v1":
        remainder = parts[4]
        if remainder.startswith("api/"):
            return "/api/" + remainder[4:]
    return None


# ---------------------------------------------------------------------------
# Request checking
# ---------------------------------------------------------------------------


def check_request(
    auth_config: AuthConfig,
    method: str,
    path: str,
    auth_header: str | None,
    cookie_header: str | None = None,
    *,
    jwt_secret: str = "",
    storage: Any = None,
) -> tuple[bool, int, str, AuthResult | None]:
    """Validate a request against the auth config.

    Checks ``Authorization: Bearer <token>`` first, then falls back to the
    ``turnstone_auth`` cookie.  Token types are auto-detected:

    - Contains ``.`` → JWT (validated with *jwt_secret*)
    - Starts with ``ts_`` → API token (looked up in *storage* by hash)
    - Otherwise → config-file token (hmac check)

    Returns ``(allowed, status_code, message, auth_result)``.
    """
    if not auth_config.enabled:
        return True, 200, "", None

    if is_public_path(path):
        return True, 200, "", None

    # Extract token from header or cookie
    raw_token = _extract_bearer(auth_header)
    if raw_token is None:
        raw_token = _extract_cookie(cookie_header, AUTH_COOKIE)

    if not raw_token:
        return False, 401, "Unauthorized: missing or invalid token", None

    # Authenticate
    result = _authenticate_token(raw_token, auth_config, jwt_secret=jwt_secret, storage=storage)
    if result is None:
        return False, 401, "Unauthorized: missing or invalid token", None

    # Check scope
    needed = required_scope(method, path)
    if not result.has_scope(needed):
        return False, 403, f"Forbidden: token lacks '{needed}' scope", None

    return True, 200, "", result


def _authenticate_token(
    token: str,
    auth_config: AuthConfig,
    *,
    jwt_secret: str = "",
    storage: Any = None,
) -> AuthResult | None:
    """Identify token type and authenticate it."""
    # 1. JWT (contains dots)
    if "." in token and jwt_secret:
        return validate_jwt(token, jwt_secret)

    # 2. API token (starts with ts_) — look up in storage
    if token.startswith(TOKEN_PREFIX) and storage is not None:
        return _authenticate_api_token(token, storage)

    # 3. Config-file token (hmac comparison)
    role = auth_config.check(token)
    if role is not None:
        scopes = _ROLE_TO_SCOPES.get(role, frozenset({"read"}))
        return AuthResult(user_id="", scopes=scopes, token_source="config")

    return None


def _authenticate_api_token(token: str, storage: Any) -> AuthResult | None:
    """Validate an API token against the database."""
    tok_hash = hash_token(token)
    row = storage.get_api_token_by_hash(tok_hash)
    if row is None:
        return None

    # Check expiry
    expires = row.get("expires")
    if expires:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        try:
            exp_dt = datetime.fromisoformat(expires).replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return None  # malformed expiry → treat as expired
        if exp_dt < now:
            return None

    return AuthResult(
        user_id=row["user_id"],
        scopes=parse_scopes(row["scopes"]),
        token_source="database",
    )


# ---------------------------------------------------------------------------
# Token extraction helpers
# ---------------------------------------------------------------------------


def _extract_bearer(header: str | None) -> str | None:
    """Extract the token from ``Bearer <token>`` header value."""
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


def _extract_cookie(cookie_header: str | None, name: str) -> str | None:
    """Extract a named value from a ``Cookie`` header."""
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
    """Return a ``Set-Cookie`` header value that stores the auth token."""
    val = f"{AUTH_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"
    if secure:
        val += "; Secure"
    return val


def make_clear_cookie() -> str:
    """Return a ``Set-Cookie`` header value that expires the auth cookie."""
    return f"{AUTH_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
