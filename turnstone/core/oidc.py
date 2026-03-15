"""OpenID Connect (OIDC) authentication support for Turnstone.

Implements the Authorization Code Flow with PKCE for secure SSO login.
All external HTTP calls use ``httpx.AsyncClient`` to avoid blocking the
event loop.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import logging
import os
import re
import secrets
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Sentinel password hash for OIDC-provisioned users.
# Not a valid bcrypt hash -- verify_password() always rejects it.
OIDC_PASSWORD_SENTINEL = "!oidc"

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


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class OIDCError(Exception):
    """Raised when an OIDC operation fails."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OIDCConfig:
    """OIDC provider configuration -- immutable after startup."""

    enabled: bool = False
    issuer: str = ""
    client_id: str = ""
    client_secret: str = ""
    scopes: str = "openid email profile"
    provider_name: str = "SSO"
    role_claim: str = ""
    role_map: dict[str, str] = field(default_factory=dict)
    password_enabled: bool = True
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


def load_oidc_config() -> OIDCConfig:
    """Build :class:`OIDCConfig` from env vars with config.toml fallback.

    Returns ``OIDCConfig(enabled=False)`` when the required fields
    (issuer, client_id, client_secret) are not all present.
    """
    from turnstone.core.config import load_config

    cfg = load_config("oidc")

    # Start with config.toml values, then override with env vars.
    issuer = os.environ.get("TURNSTONE_OIDC_ISSUER", "").strip()
    if not issuer:
        issuer = str(cfg.get("issuer", "")).strip()

    client_id = os.environ.get("TURNSTONE_OIDC_CLIENT_ID", "").strip()
    if not client_id:
        client_id = str(cfg.get("client_id", "")).strip()

    client_secret = os.environ.get("TURNSTONE_OIDC_CLIENT_SECRET", "").strip()
    if not client_secret:
        client_secret = str(cfg.get("client_secret", "")).strip()

    scopes = os.environ.get("TURNSTONE_OIDC_SCOPES", "").strip()
    if not scopes:
        scopes = str(cfg.get("scopes", "openid email profile")).strip()

    provider_name = os.environ.get("TURNSTONE_OIDC_PROVIDER_NAME", "").strip()
    if not provider_name:
        provider_name = str(cfg.get("provider_name", "SSO")).strip()

    role_claim = os.environ.get("TURNSTONE_OIDC_ROLE_CLAIM", "").strip()
    if not role_claim:
        role_claim = str(cfg.get("role_claim", "")).strip()

    # Role map: env var is "admin:builtin-admin,eng:builtin-operator"
    role_map_raw = os.environ.get("TURNSTONE_OIDC_ROLE_MAP", "").strip()
    if role_map_raw:
        role_map = _parse_role_map(role_map_raw)
    else:
        cfg_role_map = cfg.get("role_map", {})
        role_map = dict(cfg_role_map) if isinstance(cfg_role_map, dict) else {}

    password_raw = os.environ.get("TURNSTONE_OIDC_PASSWORD_ENABLED", "").strip().lower()
    if password_raw:
        password_enabled = password_raw in ("true", "1", "yes")
    else:
        password_enabled = bool(cfg.get("password_enabled", True))

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
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


async def discover_oidc(config: OIDCConfig) -> OIDCConfig:
    """Fetch OIDC discovery document and return updated config with endpoints.

    On failure, logs a warning and returns config with ``enabled=False``.
    """
    if not config.issuer:
        return dataclasses.replace(config, enabled=False)

    url = config.issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            doc = resp.json()
    except Exception as exc:
        log.warning("OIDC discovery failed for %s: %s", config.issuer, exc)
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


async def fetch_jwks(jwks_uri: str) -> dict[str, Any]:
    """Fetch the JWKS key set from the IdP.

    Returns the parsed JSON document (``{"keys": [...]}``) .  Called during
    startup discovery and on-demand when an unknown ``kid`` is encountered
    (key rotation).  Uses ``httpx.AsyncClient`` — never blocks the event loop.

    Raises :class:`OIDCError` on network failures or malformed responses.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(jwks_uri)
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
    except Exception as exc:
        raise OIDCError(f"JWKS fetch failed: {exc}") from exc
    if not isinstance(result.get("keys"), list):
        raise OIDCError("JWKS document missing 'keys' array")
    return result


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------


def generate_pkce_pair() -> tuple[str, str]:
    """Generate a PKCE code_verifier and code_challenge pair."""
    code_verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


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
) -> dict[str, Any]:
    """Exchange authorization code for tokens at the token endpoint.

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
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(config.token_endpoint, data=data)
    except Exception as exc:
        raise OIDCError(f"Token exchange request failed: {exc}") from exc

    if resp.status_code != 200:
        raise OIDCError(f"Token endpoint returned {resp.status_code}: {resp.text[:500]}")

    result: dict[str, Any] = resp.json()
    return result


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
        raise OIDCError(f"Signing key '{kid}' not found in JWKS")

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
    a new user and OIDC identity record.

    Raises :class:`OIDCError` if user creation fails.
    """
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

    # New user -- derive username
    username = _derive_username(storage, claims)
    user_id = uuid.uuid4().hex

    storage.create_user(user_id, username, display_name, OIDC_PASSWORD_SENTINEL)
    storage.create_oidc_identity(issuer, sub, user_id, email)
    apply_role_mapping(storage, user_id, claims, config)

    # Ensure new OIDC users have at least a default role so they can
    # access the application.  builtin-viewer grants read-only access.
    user_roles = storage.list_user_roles(user_id)
    if not user_roles and storage.get_role("builtin-viewer") is not None:
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

    # Check validity and uniqueness.
    if is_valid_username(sanitised) and storage.get_user_by_username(sanitised) is None:
        return sanitised

    # Deduplicate: append suffix.
    for suffix in range(2, 11):
        candidate = f"{sanitised[:60]}{suffix}"
        if is_valid_username(candidate) and storage.get_user_by_username(candidate) is None:
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
) -> None:
    """Sync Turnstone roles from OIDC claims.

    If ``config.role_claim`` is set, reads the corresponding claim value,
    normalises it to a list, and maps each value via ``config.role_map``
    to a Turnstone role ID.  Roles assigned by OIDC on previous logins
    that are no longer present in the claims are revoked (IdP demotions
    propagate).  Roles assigned manually or by other sources are never
    touched.
    """
    if not config.role_claim or not config.role_map:
        return

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

    # Add new roles from claims.
    for role_id in desired_role_ids:
        storage.assign_role(user_id, role_id, "oidc")
        log.debug("Assigned role %s to user %s via OIDC claim", role_id, user_id)

    # Revoke OIDC-assigned roles no longer present in claims.
    current_roles = storage.list_user_roles(user_id)
    for role in current_roles:
        if role.get("assigned_by") == "oidc" and role["role_id"] not in desired_role_ids:
            storage.unassign_role(user_id, role["role_id"])
            log.info(
                "Revoked role %s from user %s (removed from IdP claims)", role["role_id"], user_id
            )
