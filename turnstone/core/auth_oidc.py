"""OpenID Connect (OIDC) authentication provider for Turnstone.

Enables single sign-on via any OIDC-compliant identity provider (Hydra,
Keycloak, Okta, Auth0, etc.).  On first login the user is auto-provisioned
in the local database and assigned a configurable default role.

Configuration is via environment variables:

- ``TURNSTONE_OIDC_PROVIDER_URL`` -- OIDC discovery URL (e.g.,
  ``https://oauth.example.com/.well-known/openid-configuration``)
- ``TURNSTONE_OIDC_CLIENT_ID`` -- OAuth2 client ID
- ``TURNSTONE_OIDC_CLIENT_SECRET`` -- OAuth2 client secret
- ``TURNSTONE_OIDC_REDIRECT_URI`` -- callback URL (e.g.,
  ``https://turnstone.example.com/api/auth/oidc/callback``)
- ``TURNSTONE_OIDC_SCOPES`` -- space-separated scopes (default:
  ``openid email profile``)
- ``TURNSTONE_OIDC_DEFAULT_ROLE`` -- role assigned on first login
  (default: ``builtin-operator``)

Flow::

    Browser --> /api/auth/oidc/login  (redirect to provider)
            <-- 302 Location: provider authorize URL
    Browser --> provider login page
            <-- 302 Location: /api/auth/oidc/callback?code=...&state=...
    Browser --> /api/auth/oidc/callback
            --> exchange code for tokens, extract identity
            --> auto-create user if new
            --> issue Turnstone JWT, set cookie
            <-- 302 redirect to /
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class OIDCConfig:
    """OIDC provider configuration loaded from environment variables."""

    provider_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    scopes: str = "openid email profile"
    default_role: str = "builtin-operator"

    @property
    def enabled(self) -> bool:
        """Return True if OIDC is configured with minimum required fields."""
        return bool(self.provider_url and self.client_id and self.redirect_uri)

    @classmethod
    def from_env(cls) -> OIDCConfig:
        """Load OIDC configuration from environment variables."""
        return cls(
            provider_url=os.environ.get("TURNSTONE_OIDC_PROVIDER_URL", ""),
            client_id=os.environ.get("TURNSTONE_OIDC_CLIENT_ID", ""),
            client_secret=os.environ.get("TURNSTONE_OIDC_CLIENT_SECRET", ""),
            redirect_uri=os.environ.get("TURNSTONE_OIDC_REDIRECT_URI", ""),
            scopes=os.environ.get("TURNSTONE_OIDC_SCOPES", "openid email profile"),
            default_role=os.environ.get("TURNSTONE_OIDC_DEFAULT_ROLE", "builtin-operator"),
        )


# ---------------------------------------------------------------------------
# OIDC Discovery cache
# ---------------------------------------------------------------------------

_discovery_cache: dict[str, Any] = {}
_discovery_lock = threading.Lock()
_DISCOVERY_TTL = 3600  # 1 hour


def _discover(provider_url: str) -> dict[str, Any]:
    """Fetch and cache the OIDC discovery document."""
    now = time.time()
    with _discovery_lock:
        cached = _discovery_cache.get(provider_url)
        if cached and now - cached["fetched_at"] < _DISCOVERY_TTL:
            return cached["doc"]

    # Fetch discovery document
    discovery_url = provider_url
    if not discovery_url.endswith("/.well-known/openid-configuration"):
        discovery_url = discovery_url.rstrip("/") + "/.well-known/openid-configuration"

    resp = httpx.get(discovery_url, timeout=10, verify=True)
    resp.raise_for_status()
    doc = resp.json()

    with _discovery_lock:
        _discovery_cache[provider_url] = {"doc": doc, "fetched_at": now}
    return doc


# ---------------------------------------------------------------------------
# State management (CSRF protection)
# ---------------------------------------------------------------------------

_pending_states: dict[str, float] = {}
_states_lock = threading.Lock()
_STATE_TTL = 600  # 10 minutes


def _create_state() -> str:
    """Generate a cryptographic state parameter and store it."""
    state = secrets.token_urlsafe(32)
    now = time.time()
    with _states_lock:
        # Prune expired states
        expired = [k for k, v in _pending_states.items() if now - v > _STATE_TTL]
        for k in expired:
            del _pending_states[k]
        _pending_states[state] = now
    return state


def _validate_state(state: str) -> bool:
    """Validate and consume a state parameter. Returns True if valid."""
    with _states_lock:
        ts = _pending_states.pop(state, None)
    if ts is None:
        return False
    return time.time() - ts < _STATE_TTL


# ---------------------------------------------------------------------------
# Request handlers
# ---------------------------------------------------------------------------


async def handle_oidc_login(request: Request) -> Response:
    """GET /api/auth/oidc/login -- redirect to OIDC provider."""
    from starlette.responses import RedirectResponse

    oidc_config: OIDCConfig = getattr(request.app.state, "oidc_config", None)
    if not oidc_config or not oidc_config.enabled:
        from starlette.responses import JSONResponse

        return JSONResponse({"error": "OIDC not configured"}, status_code=503)

    try:
        doc = _discover(oidc_config.provider_url)
    except Exception as exc:
        log.error("OIDC discovery failed: %s", exc)
        from starlette.responses import JSONResponse

        return JSONResponse({"error": "OIDC discovery failed"}, status_code=502)

    authorize_url = doc["authorization_endpoint"]
    state = _create_state()

    params = {
        "response_type": "code",
        "client_id": oidc_config.client_id,
        "redirect_uri": oidc_config.redirect_uri,
        "scope": oidc_config.scopes,
        "state": state,
    }
    query = "&".join(f"{k}={httpx.URL('', params={k: v}).params}" for k, v in params.items())
    # Build URL properly
    sep = "&" if "?" in authorize_url else "?"
    redirect_url = f"{authorize_url}{sep}" + "&".join(
        f"{k}={_url_encode(v)}" for k, v in params.items()
    )

    return RedirectResponse(url=redirect_url, status_code=302)


async def handle_oidc_callback(request: Request) -> Response:
    """GET /api/auth/oidc/callback -- exchange code for tokens, create session."""
    from starlette.responses import JSONResponse, RedirectResponse

    from turnstone.core.auth import (
        JWT_AUD_SERVER,
        create_jwt,
        is_secure_request,
        make_set_cookie,
    )

    oidc_config: OIDCConfig = getattr(request.app.state, "oidc_config", None)
    if not oidc_config or not oidc_config.enabled:
        return JSONResponse({"error": "OIDC not configured"}, status_code=503)

    # Validate state parameter (CSRF protection)
    state = request.query_params.get("state", "")
    if not state or not _validate_state(state):
        return JSONResponse({"error": "Invalid or expired state parameter"}, status_code=400)

    # Check for error response from provider
    error = request.query_params.get("error", "")
    if error:
        desc = request.query_params.get("error_description", "")
        log.warning("OIDC provider error: %s: %s", error, desc)
        return JSONResponse({"error": f"OIDC error: {error}", "description": desc}, status_code=400)

    code = request.query_params.get("code", "")
    if not code:
        return JSONResponse({"error": "Missing authorization code"}, status_code=400)

    # Discover endpoints
    try:
        doc = _discover(oidc_config.provider_url)
    except Exception as exc:
        log.error("OIDC discovery failed: %s", exc)
        return JSONResponse({"error": "OIDC discovery failed"}, status_code=502)

    token_url = doc["token_endpoint"]

    # Exchange code for tokens
    try:
        token_resp = httpx.post(
            token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": oidc_config.redirect_uri,
            },
            auth=(oidc_config.client_id, oidc_config.client_secret),
            timeout=15,
            verify=True,
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()
    except Exception as exc:
        log.error("OIDC token exchange failed: %s", exc)
        return JSONResponse({"error": "Token exchange failed"}, status_code=502)

    # Extract identity from ID token or userinfo endpoint
    identity = _extract_identity(token_data, doc, oidc_config)
    if not identity:
        return JSONResponse({"error": "Could not determine user identity"}, status_code=400)

    email = identity.get("email", "")
    display_name = identity.get("name", "") or identity.get("preferred_username", "") or email
    if not email:
        return JSONResponse({"error": "OIDC provider did not return email"}, status_code=400)

    # Sanitize email into a valid username (letters, digits, ., _, -)
    username = _email_to_username(email)

    # Auto-provision user if not exists
    storage = getattr(request.app.state, "auth_storage", None)
    if storage is None:
        return JSONResponse({"error": "Auth storage not available"}, status_code=500)

    user = storage.get_user_by_username(username)
    if user is None:
        # Create new user with a random password hash (OIDC users don't use passwords)
        user_id = uuid.uuid4().hex
        random_pw_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        storage.create_user(user_id, username, display_name, random_pw_hash)

        # Assign default role
        try:
            storage.assign_role(user_id, oidc_config.default_role, "oidc-auto-provision")
        except Exception:
            log.warning("Failed to assign default role %s to user %s", oidc_config.default_role, username)

        log.info("OIDC auto-provisioned user: %s (%s) with role %s", username, user_id, oidc_config.default_role)
        user = {"user_id": user_id, "username": username, "display_name": display_name}
    else:
        user_id = user["user_id"]
        log.info("OIDC login: existing user %s (%s)", username, user_id)

    # Load permissions and issue Turnstone JWT
    from turnstone.core.auth import _load_user_permissions, _permissions_to_scopes

    perms = _load_user_permissions(storage, user_id)
    scopes = _permissions_to_scopes(perms)

    jwt_secret = getattr(request.app.state, "jwt_secret", "")
    if not jwt_secret:
        return JSONResponse({"error": "JWT secret not configured"}, status_code=500)

    jwt_token = create_jwt(
        user_id=user_id,
        scopes=scopes,
        source="oidc",
        secret=jwt_secret,
        audience=JWT_AUD_SERVER,
        permissions=frozenset(perms),
    )

    # Set cookie and redirect to app
    secure = is_secure_request(dict(request.headers), request.url.scheme)
    response = RedirectResponse(url="/", status_code=302)
    response.headers["Set-Cookie"] = make_set_cookie(jwt_token, secure=secure)
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_identity(
    token_data: dict[str, Any],
    discovery_doc: dict[str, Any],
    config: OIDCConfig,
) -> dict[str, Any] | None:
    """Extract user identity from OIDC token response.

    Tries the ID token first (decoding claims without signature verification
    since we just received it from the token endpoint over TLS), then falls
    back to the userinfo endpoint.
    """
    # Try ID token claims (JWT payload)
    id_token = token_data.get("id_token", "")
    if id_token:
        claims = _decode_jwt_claims(id_token)
        if claims and claims.get("email"):
            return claims

    # Fall back to userinfo endpoint
    userinfo_url = discovery_doc.get("userinfo_endpoint", "")
    access_token = token_data.get("access_token", "")
    if userinfo_url and access_token:
        try:
            resp = httpx.get(
                userinfo_url,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
                verify=True,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            log.warning("OIDC userinfo request failed: %s", exc)

    return None


def _decode_jwt_claims(token: str) -> dict[str, Any] | None:
    """Decode JWT claims without signature verification.

    Safe here because the token was received directly from the token endpoint
    over TLS -- not from an untrusted source.
    """
    try:
        # JWT is header.payload.signature -- we need the payload
        parts = token.split(".")
        if len(parts) != 3:
            return None
        # Add padding
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        import base64

        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception:
        return None


def _email_to_username(email: str) -> str:
    """Convert an email address to a valid Turnstone username.

    Replaces @ and invalid characters with safe alternatives.
    """
    # Use the local part of the email, replacing invalid chars
    local = email.split("@")[0] if "@" in email else email
    # Replace any non-alphanumeric/dot/underscore/dash chars
    safe = ""
    for ch in local.lower():
        if ch.isalnum() or ch in "._-":
            safe += ch
        else:
            safe += "_"
    return safe or "oidc-user"


def _url_encode(value: str) -> str:
    """URL-encode a value for query string parameters."""
    import urllib.parse

    return urllib.parse.quote(value, safe="")
