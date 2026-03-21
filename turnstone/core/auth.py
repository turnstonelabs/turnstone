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
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.types import ASGIApp, Receive, Scope, Send

    from turnstone.core.oidc import OIDCConfig

from turnstone.core.log import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AUTH_COOKIE = "turnstone_auth"
TOKEN_PREFIX = "ts_"
TOKEN_BYTES = 32  # 64 hex chars after prefix

JWT_ISSUER = "turnstone"
JWT_AUD_SERVER = "turnstone-server"
JWT_AUD_CONSOLE = "turnstone-console"
JWT_AUD_CHANNEL = "turnstone-channel"
_MIN_SECRET_LENGTH = 32  # 256 bits minimum for HMAC-SHA256

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
# RBAC helpers
# ---------------------------------------------------------------------------


def _load_user_permissions(storage: Any, user_id: str) -> set[str]:
    """Load the union of all permissions from a user's assigned roles."""
    try:
        result: set[str] = storage.get_user_permissions(user_id)
        return result
    except Exception:
        log.warning("Failed to load permissions for user %s", user_id)
        return set()


def _permissions_to_scopes(permissions: set[str]) -> frozenset[str]:
    """Derive legacy scopes from a granular permission set."""
    scopes: set[str] = set()
    if not permissions:
        scopes.add("read")
        return frozenset(scopes)
    for perm in permissions:
        if perm in VALID_SCOPES:
            scopes.update(SCOPE_HIERARCHY.get(perm, {perm}))
    # Any admin.* permission requires access to admin endpoints → approve scope
    if any(p.startswith("admin.") for p in permissions):
        scopes.update(SCOPE_HIERARCHY["approve"])
    if not scopes:
        scopes.add("read")
    return frozenset(scopes)


def require_permission(request: Request, permission: str) -> JSONResponse | None:
    """Return a 403 JSONResponse if the user lacks *permission*, else None.

    Call from admin handlers after the middleware scope check passes.
    Config-file tokens (no user_id) are treated as full-access.
    """
    from starlette.responses import JSONResponse

    auth_result: AuthResult | None = getattr(getattr(request, "state", None), "auth_result", None)
    if auth_result is None:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    # Config-file tokens (no user_id) are treated as full-access
    if not auth_result.user_id:
        return None
    if auth_result.has_permission(permission):
        return None
    return JSONResponse(
        {"error": f"Forbidden: missing '{permission}' permission"},
        status_code=403,
    )


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
        "/api/auth/oidc/authorize",
        "/api/auth/oidc/callback",
    }
)
PUBLIC_PREFIXES: tuple[str, ...] = ("/static/", "/shared/")

WRITE_PATHS: frozenset[str] = frozenset(
    {
        "/api/send",
        "/api/plan",
        "/api/command",
        "/api/cancel",
        "/api/workstreams/new",
        "/api/workstreams/close",
        "/api/cluster/workstreams/new",
        "/api/memories",
    }
)

APPROVE_PATHS: frozenset[str] = frozenset(
    {"/api/approve", "/api/_internal/config-reload", "/api/_internal/mcp-reload"}
)
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
    permissions: frozenset[str] = frozenset()

    def has_scope(self, scope: str) -> bool:
        """Return True if this result includes *scope*."""
        return scope in self.scopes

    def has_permission(self, permission: str) -> bool:
        """Return True if this result includes *permission*."""
        return permission in self.permissions


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
    """Verify a password against a bcrypt hash.

    Returns ``False`` immediately for non-bcrypt hashes (e.g. the ``!oidc``
    sentinel used for OIDC-provisioned users) to avoid ``ValueError`` from
    ``bcrypt.checkpw``.
    """
    import bcrypt

    if not password_hash.startswith("$2"):
        return False  # Not a bcrypt hash (e.g. OIDC sentinel)
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


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
    if not secret:
        from turnstone.core.config import load_config

        auth_cfg = load_config("auth")
        secret = str(auth_cfg.get("jwt_secret", "")).strip()

    if not secret:
        # Auto-generate an ephemeral secret
        secret = secrets.token_hex(32)
        log.warning(
            "No JWT secret configured — using ephemeral secret (tokens will not survive restart)"
        )
        return secret

    if len(secret) < _MIN_SECRET_LENGTH:
        log.warning(
            "JWT secret is shorter than %d characters — consider using a stronger secret",
            _MIN_SECRET_LENGTH,
        )
    return secret


def create_jwt(
    user_id: str,
    scopes: frozenset[str],
    source: str,
    secret: str,
    expiry_hours: int = 24,
    audience: str = "",
    permissions: frozenset[str] = frozenset(),
) -> str:
    """Create a signed JWT with user identity, scopes, and permissions."""
    import jwt

    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": user_id,
        "scopes": ",".join(sorted(scopes)),
        "src": source,
        "iss": JWT_ISSUER,
        "iat": now,
        "exp": now + expiry_hours * 3600,
    }
    if audience:
        payload["aud"] = audience
    if permissions:
        payload["permissions"] = ",".join(sorted(permissions))
    return jwt.encode(payload, secret, algorithm="HS256")


def validate_jwt(token: str, secret: str, audience: str = "") -> AuthResult | None:
    """Validate a JWT and return an AuthResult, or None on failure.

    When *audience* is non-empty the ``aud`` claim is verified.  Tokens
    without an ``aud`` claim are accepted when *audience* is empty (backward
    compatibility during the rollout window).
    """
    import jwt

    decode_opts: Any = None
    if not audience:
        decode_opts = {"verify_aud": False}
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            audience=audience if audience else None,
            options=decode_opts,
        )
    except jwt.InvalidTokenError:
        return None

    user_id = payload.get("sub", "")
    scopes_str = payload.get("scopes", "")
    source = payload.get("src", "jwt")
    perms_str = payload.get("permissions", "")

    perms = frozenset(p for p in perms_str.split(",") if p) if perms_str else frozenset()

    return AuthResult(
        user_id=user_id,
        scopes=parse_scopes(scopes_str),
        token_source=source,
        permissions=perms,
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
    # Watch cancel has a path parameter: /api/watches/{id}/cancel
    if (
        method == "POST"
        and normalized.startswith("/api/watches/")
        and normalized.endswith("/cancel")
    ):
        return "write"
    # Memory delete: /api/memories/{name}
    if method == "DELETE" and normalized.startswith("/api/memories/"):
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
    jwt_audience: str = "",
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
    result = _authenticate_token(
        raw_token, auth_config, jwt_secret=jwt_secret, jwt_audience=jwt_audience, storage=storage
    )
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
    jwt_audience: str = "",
    storage: Any = None,
) -> AuthResult | None:
    """Identify token type and authenticate it."""
    # 1. JWT (contains dots) — attempt validation, fall through on failure
    if "." in token and jwt_secret:
        try:
            jwt_result = validate_jwt(token, jwt_secret, audience=jwt_audience)
        except Exception:
            jwt_result = None
        if jwt_result is not None:
            return jwt_result

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

    perms = _load_user_permissions(storage, row["user_id"]) if storage else set()
    return AuthResult(
        user_id=row["user_id"],
        scopes=parse_scopes(row["scopes"]),
        token_source="database",
        permissions=frozenset(perms),
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


def make_set_cookie(token: str, max_age: int = 86400, *, secure: bool | None = None) -> str:
    """Return a ``Set-Cookie`` header value that stores the auth token.

    When *secure* is ``None`` (default) the ``Secure`` flag is set
    unconditionally.  Pass ``secure=False`` only for plaintext development.
    *max_age* defaults to 24 hours to match the default JWT expiry.
    """
    val = f"{AUTH_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"
    if secure is None or secure:
        val += "; Secure"
    return val


def make_clear_cookie() -> str:
    """Return a ``Set-Cookie`` header value that expires the auth cookie."""
    return f"{AUTH_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


def is_secure_request(headers: dict[str, str], scheme: str = "") -> bool:
    """Return ``True`` if the request arrived over HTTPS.

    Checks the URL scheme and the ``X-Forwarded-Proto`` header (set by
    reverse proxies and load balancers).
    """
    if scheme == "https":
        return True
    proto = headers.get("x-forwarded-proto", "")
    return proto.lower() == "https"


# ---------------------------------------------------------------------------
# Login rate limiter
# ---------------------------------------------------------------------------


class LoginRateLimiter:
    """Sliding-window rate limiter for login attempts.

    Tracks per-key (IP or username) attempt timestamps and rejects when
    *max_attempts* are exceeded within *window_seconds*.
    """

    MAX_KEYS: int = 50_000

    def __init__(self, max_attempts: int = 5, window_seconds: int = 300) -> None:
        self._max_attempts = max_attempts
        self._window = window_seconds
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        """Return ``(allowed, retry_after_seconds)``.

        Does **not** record a new attempt — call :meth:`record` after a
        failed login so successful logins don't consume the budget.
        """
        now = time.monotonic()
        with self._lock:
            timestamps = self._attempts.get(key)
            if timestamps is None:
                return True, 0
            # Prune expired
            cutoff = now - self._window
            timestamps[:] = [t for t in timestamps if t > cutoff]
            if not timestamps:
                del self._attempts[key]
                return True, 0
            if len(timestamps) >= self._max_attempts:
                retry_after = int(timestamps[0] - cutoff) + 1
                return False, max(retry_after, 1)
            return True, 0

    def record(self, key: str) -> None:
        """Record a failed login attempt."""
        now = time.monotonic()
        with self._lock:
            if len(self._attempts) >= self.MAX_KEYS and key not in self._attempts:
                return  # prevent memory exhaustion
            self._attempts.setdefault(key, []).append(now)

    def cleanup(self, max_age: float = 600.0) -> int:
        """Remove stale entries older than *max_age* seconds."""
        now = time.monotonic()
        cutoff = now - max_age
        with self._lock:
            stale = [k for k, ts in self._attempts.items() if all(t <= cutoff for t in ts)]
            for k in stale:
                del self._attempts[k]
        return len(stale)


# ---------------------------------------------------------------------------
# Service token manager (auto-rotating JWTs for service-to-service auth)
# ---------------------------------------------------------------------------


class ServiceTokenManager:
    """Auto-rotating service JWT.  Thread-safe.

    The :attr:`token` property returns a valid JWT, re-minting transparently
    when the current token is within *refresh_margin* of expiry.
    """

    def __init__(
        self,
        user_id: str,
        scopes: frozenset[str],
        source: str,
        secret: str,
        audience: str = "",
        expiry_hours: int = 1,
        refresh_margin: float = 0.2,
    ) -> None:
        self._user_id = user_id
        self._scopes = scopes
        self._source = source
        self._secret = secret
        self._audience = audience
        self._expiry_hours = expiry_hours
        self._margin_seconds = expiry_hours * 3600 * refresh_margin
        self._token: str = ""
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    def _mint(self) -> None:
        self._token = create_jwt(
            user_id=self._user_id,
            scopes=self._scopes,
            source=self._source,
            secret=self._secret,
            expiry_hours=self._expiry_hours,
            audience=self._audience,
        )
        self._expires_at = time.time() + self._expiry_hours * 3600
        log.debug("Service JWT minted for %s (expires in %dh)", self._user_id, self._expiry_hours)

    @property
    def token(self) -> str:
        """Return current token, re-minting if near expiry."""
        with self._lock:
            if time.time() >= self._expires_at - self._margin_seconds:
                self._mint()
            return self._token

    @property
    def bearer_header(self) -> dict[str, str]:
        """Return an ``Authorization`` header dict with the current token."""
        return {"Authorization": f"Bearer {self.token}"}


# ---------------------------------------------------------------------------
# Shared ASGI middleware
# ---------------------------------------------------------------------------


class AuthMiddleware:
    """ASGI middleware that enforces bearer-token / cookie authentication.

    Parameterized by *jwt_audience* so the same class serves both the node
    server (``JWT_AUD_SERVER``) and the console (``JWT_AUD_CONSOLE``).
    """

    def __init__(self, app: ASGIApp, jwt_audience: str = "") -> None:
        self.app = app
        self._jwt_audience = jwt_audience

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from starlette.requests import Request
        from starlette.responses import JSONResponse

        request = Request(scope)
        # Skip auth for CORS preflight — CORSMiddleware handles it
        if request.method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        auth_config = request.app.state.auth_config
        jwt_secret = getattr(request.app.state, "jwt_secret", "")
        storage = getattr(request.app.state, "auth_storage", None)
        method = request.method
        path = request.url.path
        auth_header = request.headers.get("Authorization")
        cookie_header = request.headers.get("Cookie")
        allowed, status, msg, auth_result = check_request(
            auth_config,
            method,
            path,
            auth_header,
            cookie_header,
            jwt_secret=jwt_secret,
            jwt_audience=self._jwt_audience,
            storage=storage,
        )
        if not allowed:
            response = JSONResponse({"error": msg}, status_code=status)
            await response(scope, receive, send)
            return

        # Set user_id in log context and stash auth result for handlers
        if auth_result and auth_result.user_id:
            from turnstone.core.log import ctx_user_id

            ctx_user_id.set(auth_result.user_id)
        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["auth_result"] = auth_result
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Shared auth endpoint handlers
# ---------------------------------------------------------------------------


async def handle_auth_login(request: Request, audience: str) -> Response:
    """Shared ``POST /api/auth/login`` handler.

    Authenticates via username:password or legacy token exchange, returning
    a JWT and setting the auth cookie.  *audience* selects the JWT ``aud``
    claim (``JWT_AUD_SERVER`` or ``JWT_AUD_CONSOLE``).
    """
    from starlette.responses import JSONResponse

    try:
        body: dict[str, Any] = await request.json()
    except (ValueError, json.JSONDecodeError):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    auth_config = request.app.state.auth_config
    jwt_secret = getattr(request.app.state, "jwt_secret", "")
    storage = getattr(request.app.state, "auth_storage", None)
    login_limiter: LoginRateLimiter | None = getattr(request.app.state, "login_limiter", None)

    username = body.get("username", "")
    client_ip = request.client.host if request.client else "unknown"

    # Check login rate limits (per-IP and per-username)
    if login_limiter is not None:
        ip_ok, ip_retry = login_limiter.check(f"ip:{client_ip}")
        if not ip_ok:
            return JSONResponse(
                {"error": "Too many login attempts"},
                status_code=429,
                headers={"Retry-After": str(ip_retry)},
            )
        if username:
            user_ok, user_retry = login_limiter.check(f"user:{username}")
            if not user_ok:
                return JSONResponse(
                    {"error": "Too many login attempts"},
                    status_code=429,
                    headers={"Retry-After": str(user_retry)},
                )

    result: AuthResult | None = None
    password = body.get("password", "")

    if username and password and storage is not None:
        # Enforce OIDC-only mode: reject password login when disabled
        oidc_config = getattr(request.app.state, "oidc_config", None)
        if oidc_config and oidc_config.enabled and not oidc_config.password_enabled:
            return JSONResponse(
                {"error": "Password login is disabled — use SSO"},
                status_code=403,
            )
        user = storage.get_user_by_username(username)
        if user and verify_password(password, user["password_hash"]):
            # Derive scopes and permissions from assigned roles
            perms = _load_user_permissions(storage, user["user_id"])
            scopes = _permissions_to_scopes(perms)
            result = AuthResult(
                user_id=user["user_id"],
                scopes=scopes,
                token_source="password",
                permissions=frozenset(perms),
            )
    elif body.get("token"):
        result = _authenticate_token(
            body["token"],
            auth_config,
            jwt_secret=jwt_secret,
            jwt_audience=audience,
            storage=storage,
        )

    if result is None:
        # Record failed attempt for rate limiting
        if login_limiter is not None:
            login_limiter.record(f"ip:{client_ip}")
            if username:
                login_limiter.record(f"user:{username}")
        return JSONResponse({"error": "Invalid credentials"}, status_code=401)

    jwt_token = ""
    if jwt_secret:
        jwt_token = create_jwt(
            user_id=result.user_id,
            scopes=result.scopes,
            source=result.token_source,
            secret=jwt_secret,
            audience=audience,
            permissions=result.permissions,
        )

    role = "full" if result.has_scope("write") else "read"
    scopes_str = ",".join(sorted(result.scopes))
    resp_body: dict[str, str] = {"status": "ok", "role": role, "scopes": scopes_str}
    if result.permissions:
        resp_body["permissions"] = ",".join(sorted(result.permissions))
    if jwt_token:
        resp_body["jwt"] = jwt_token
    if result.user_id:
        resp_body["user_id"] = result.user_id

    secure = is_secure_request(dict(request.headers), request.url.scheme)
    response = JSONResponse(resp_body)
    cookie_value = jwt_token if jwt_token else body.get("token", "")
    if cookie_value:
        response.headers["Set-Cookie"] = make_set_cookie(cookie_value, secure=secure)
    return response


async def handle_auth_logout(request: Request) -> Response:
    """Shared ``POST /api/auth/logout`` handler — clear auth cookie."""
    from starlette.responses import JSONResponse

    response = JSONResponse({"status": "ok"})
    response.headers["Set-Cookie"] = make_clear_cookie()
    return response


async def handle_auth_status(request: Request) -> Response:
    """Shared ``GET /api/auth/status`` handler — login UI state detection."""
    from starlette.responses import JSONResponse

    auth_config = request.app.state.auth_config
    storage = getattr(request.app.state, "auth_storage", None)

    has_users = False
    if storage is not None:
        try:
            users = storage.list_users()
            has_users = len(users) > 0
        except Exception:
            log.warning("Failed to check user existence for auth status", exc_info=True)

    # OIDC configuration
    oidc_config = getattr(request.app.state, "oidc_config", None)
    oidc_enabled = bool(oidc_config and oidc_config.enabled)

    resp: dict[str, Any] = {
        "auth_enabled": auth_config.enabled,
        "has_users": has_users,
        "setup_required": auth_config.enabled and not has_users,
    }
    if oidc_enabled and oidc_config is not None:
        resp["oidc_enabled"] = True
        resp["oidc_provider_name"] = oidc_config.provider_name
        resp["password_enabled"] = oidc_config.password_enabled

    return JSONResponse(resp)


async def handle_auth_setup(request: Request, audience: str) -> Response:
    """Shared ``POST /api/auth/setup`` handler — create first admin user.

    Only works when zero users exist.  Returns JWT on success.
    """
    from starlette.responses import JSONResponse

    storage = getattr(request.app.state, "auth_storage", None)
    jwt_secret = getattr(request.app.state, "jwt_secret", "")

    if storage is None:
        return JSONResponse({"error": "Storage not available"}, status_code=503)

    try:
        body: dict[str, Any] = await request.json()
    except (ValueError, json.JSONDecodeError):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    username = body.get("username", "").strip()
    display_name = body.get("display_name", "").strip()
    password = body.get("password", "")

    if not is_valid_username(username):
        return JSONResponse(
            {"error": "Invalid username (1-64 chars: letters, digits, . _ -)"},
            status_code=400,
        )
    if not display_name:
        return JSONResponse({"error": "display_name is required"}, status_code=400)
    if len(password) < 8:
        return JSONResponse({"error": "Password must be at least 8 characters"}, status_code=400)

    user_id = uuid.uuid4().hex
    pw_hash = hash_password(password)

    # Atomic: insert only if no users exist (prevents TOCTOU race)
    try:
        created = storage.create_first_user(user_id, username, display_name, pw_hash)
    except Exception:
        return JSONResponse({"error": "Storage error"}, status_code=503)
    if not created:
        return JSONResponse({"error": "Setup already completed"}, status_code=409)

    # Assign admin role to the first user — fail setup if this breaks,
    # otherwise the admin is created with read-only access and locked out.
    try:
        storage.assign_role(user_id, "builtin-admin", "")
    except Exception:
        log.error("Failed to assign admin role to first user %s — aborting setup", user_id)
        # Roll back the user creation so setup can be retried
        try:
            storage.delete_user(user_id)
        except Exception:
            log.error("Failed to roll back user %s during setup abort", user_id, exc_info=True)
        return JSONResponse(
            {"error": "Failed to assign admin role. Ensure migrations have run."},
            status_code=503,
        )

    # Derive permissions from roles
    perms = _load_user_permissions(storage, user_id)
    if not perms:
        log.error(
            "First user %s has no permissions after role assignment — aborting setup", user_id
        )
        try:
            storage.delete_user(user_id)
        except Exception:
            log.error("Failed to roll back user %s during setup abort", user_id, exc_info=True)
        return JSONResponse(
            {"error": "Failed to load permissions. Ensure migrations have run."},
            status_code=503,
        )
    scopes = _permissions_to_scopes(perms)
    jwt_token = ""
    if jwt_secret:
        jwt_token = create_jwt(
            user_id=user_id,
            scopes=scopes,
            source="password",
            secret=jwt_secret,
            audience=audience,
            permissions=frozenset(perms),
        )

    resp_body: dict[str, str] = {
        "status": "ok",
        "user_id": user_id,
        "username": username,
        "role": "full",
        "scopes": ",".join(sorted(scopes)),
    }
    if perms:
        resp_body["permissions"] = ",".join(sorted(perms))
    if jwt_token:
        resp_body["jwt"] = jwt_token

    secure = is_secure_request(dict(request.headers), request.url.scheme)
    response = JSONResponse(resp_body)
    if jwt_token:
        response.headers["Set-Cookie"] = make_set_cookie(jwt_token, secure=secure)
    return response


async def handle_auth_whoami(request: Request) -> Response:
    """Shared ``GET /api/auth/whoami`` handler — return authenticated user info."""
    from starlette.responses import JSONResponse

    auth_result: AuthResult | None = getattr(request.state, "auth_result", None)
    if not auth_result or not auth_result.user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    resp: dict[str, str] = {
        "user_id": auth_result.user_id,
    }
    if auth_result.permissions:
        resp["permissions"] = ",".join(sorted(auth_result.permissions))
    return JSONResponse(resp)


def _build_oidc_redirect_uri(request: Request, oidc_config: OIDCConfig) -> str:
    """Build the OIDC callback redirect URI.

    Uses ``redirect_base`` from OIDC config when set (recommended for
    reverse-proxy deployments), otherwise falls back to the request Host header.
    """
    if oidc_config.redirect_base:
        return f"{oidc_config.redirect_base}/v1/api/auth/oidc/callback"
    scheme = "https" if is_secure_request(dict(request.headers), request.url.scheme) else "http"
    host = request.headers.get("host", "localhost")
    return f"{scheme}://{host}/v1/api/auth/oidc/callback"


async def handle_oidc_authorize(request: Request, audience: str) -> Response:
    """Shared ``GET /api/auth/oidc/authorize`` handler — redirect to IdP."""
    from starlette.responses import JSONResponse, RedirectResponse

    oidc_config = getattr(request.app.state, "oidc_config", None)
    if not oidc_config or not oidc_config.enabled:
        return JSONResponse({"error": "OIDC not configured"}, status_code=404)

    # Rate limit — prevents flooding oidc_pending_states table
    login_limiter: LoginRateLimiter | None = getattr(request.app.state, "login_limiter", None)
    client_ip = request.client.host if request.client else "unknown"
    if login_limiter is not None:
        ip_ok, _ip_retry = login_limiter.check(f"ip:{client_ip}")
        if not ip_ok:
            return RedirectResponse("/?oidc_error=Too+many+login+attempts", status_code=302)
        login_limiter.record(f"ip:{client_ip}")  # Count every authorize to bound pending states

    storage = getattr(request.app.state, "auth_storage", None)
    if storage is None:
        return JSONResponse({"error": "Storage not available"}, status_code=503)

    # Require setup to be complete before allowing OIDC login
    try:
        users = storage.list_users()
    except Exception:
        return JSONResponse({"error": "Storage unavailable"}, status_code=503)
    if not users:
        return JSONResponse(
            {"error": "Initial setup required before OIDC login"},
            status_code=403,
        )

    from turnstone.core.oidc import build_authorize_url, generate_pkce_pair

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier, _code_challenge = generate_pkce_pair()

    # Store pending state in database
    storage.create_oidc_pending_state(state, nonce, code_verifier, audience)

    # Build redirect URI (pinned by TURNSTONE_OIDC_REDIRECT_BASE when set)
    redirect_uri = _build_oidc_redirect_uri(request, oidc_config)

    url = build_authorize_url(oidc_config, redirect_uri, state, nonce, code_verifier)
    return RedirectResponse(url, status_code=302)


async def handle_oidc_callback(request: Request, audience: str) -> Response:
    """Shared ``GET /api/auth/oidc/callback`` handler — exchange code, provision user, issue JWT."""
    from starlette.responses import JSONResponse, RedirectResponse

    oidc_config = getattr(request.app.state, "oidc_config", None)
    if not oidc_config or not oidc_config.enabled:
        return JSONResponse({"error": "OIDC not configured"}, status_code=404)

    storage = getattr(request.app.state, "auth_storage", None)
    jwt_secret = getattr(request.app.state, "jwt_secret", "")

    if storage is None:
        return JSONResponse({"error": "Storage not available"}, status_code=503)

    # Rate limiting
    login_limiter: LoginRateLimiter | None = getattr(request.app.state, "login_limiter", None)
    client_ip = request.client.host if request.client else "unknown"
    if login_limiter is not None:
        ip_ok, ip_retry = login_limiter.check(f"ip:{client_ip}")
        if not ip_ok:
            return RedirectResponse("/?oidc_error=Too+many+login+attempts", status_code=302)

    # Lazy cleanup of expired pending states
    try:
        storage.cleanup_expired_oidc_states(300)
    except Exception:
        log.debug("OIDC state cleanup failed", exc_info=True)

    def _record_oidc_failure() -> None:
        if login_limiter is not None:
            login_limiter.record(f"ip:{client_ip}")

    # Check for IdP error
    error = request.query_params.get("error", "")
    if error:
        _record_oidc_failure()
        desc = request.query_params.get("error_description", error)
        return RedirectResponse(f"/?oidc_error={urllib.parse.quote(desc)}", status_code=302)

    # Validate state
    state = request.query_params.get("state", "")
    pending = storage.pop_oidc_pending_state(state, max_age_seconds=300)
    if not pending:
        _record_oidc_failure()
        return RedirectResponse("/?oidc_error=Login+session+expired", status_code=302)

    # Build redirect URI (must match what was sent in authorize)
    redirect_uri = _build_oidc_redirect_uri(request, oidc_config)

    try:
        from turnstone.core.oidc import (
            OIDCError,
            exchange_code,
            fetch_jwks,
            provision_oidc_user,
            validate_id_token,
        )

        # Exchange code for tokens
        code = request.query_params.get("code", "")
        tokens = await exchange_code(oidc_config, code, redirect_uri, pending["code_verifier"])

        # Validate ID token against cached JWKS keys (no I/O).
        # On unknown kid, refresh JWKS once (async) for key rotation.
        jwks_data: dict[str, Any] | None = getattr(request.app.state, "jwks_data", None)
        if jwks_data is None and oidc_config.jwks_uri:
            # Lazy fetch: JWKS may have failed at startup but IdP recovered
            try:
                jwks_data = await fetch_jwks(oidc_config.jwks_uri)
                request.app.state.jwks_data = jwks_data
            except OIDCError:
                pass
        if jwks_data is None:
            return RedirectResponse("/?oidc_error=OIDC+temporarily+unavailable", status_code=302)

        try:
            id_claims = validate_id_token(
                tokens["id_token"],
                jwks_data,
                oidc_config,
                pending["nonce"],
            )
        except OIDCError as first_err:
            if "not found in JWKS" not in str(first_err):
                raise
            # Key rotation: re-fetch JWKS and retry once.
            log.info("JWKS key not found — refreshing for possible key rotation")
            jwks_data = await fetch_jwks(oidc_config.jwks_uri)
            request.app.state.jwks_data = jwks_data
            id_claims = validate_id_token(
                tokens["id_token"],
                jwks_data,
                oidc_config,
                pending["nonce"],
            )

        # Verify setup is complete
        users = storage.list_users()
        if not users:
            return RedirectResponse("/?oidc_error=Initial+setup+required", status_code=302)

        # Provision or match user
        user = provision_oidc_user(storage, oidc_config, id_claims)

    except OIDCError as exc:
        log.warning("OIDC callback failed: %s", exc)
        _record_oidc_failure()
        return RedirectResponse("/?oidc_error=Authentication+failed", status_code=302)
    except Exception:
        log.exception("OIDC callback error")
        _record_oidc_failure()
        return RedirectResponse("/?oidc_error=Authentication+failed", status_code=302)

    # Load permissions and issue Turnstone JWT
    perms = _load_user_permissions(storage, user["user_id"])
    scopes = _permissions_to_scopes(perms)
    jwt_token = ""
    if jwt_secret:
        # Use the audience stored during authorize (not the handler param)
        # to bind the JWT to the service that initiated the flow
        jwt_audience = pending.get("audience", audience)
        jwt_token = create_jwt(
            user_id=user["user_id"],
            scopes=scopes,
            source="oidc",
            secret=jwt_secret,
            audience=jwt_audience,
            permissions=frozenset(perms),
        )

    # Set cookie and redirect to app
    response = RedirectResponse("/?oidc_success=1", status_code=302)
    if jwt_token:
        secure = is_secure_request(dict(request.headers), request.url.scheme)
        response.headers["Set-Cookie"] = make_set_cookie(jwt_token, secure=secure)
    return response
