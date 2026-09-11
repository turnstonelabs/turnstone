"""Application account provisioning and role mapping for OIDC login."""

from __future__ import annotations

import re
import uuid
from typing import Any

from turnstone.core.log import get_logger
from turnstone.core.oauth import oidc as oauth_oidc

log = get_logger(__name__)

# Sentinel password hash for OIDC-provisioned users.
# Not a valid bcrypt hash -- verify_password() always rejects it.
OIDC_PASSWORD_SENTINEL = "!oidc"


# Sanitisation pattern: only keep safe username characters.
_USERNAME_SAFE_RE = re.compile(r"[^a-zA-Z0-9._-]")


# ---------------------------------------------------------------------------
# User provisioning
# ---------------------------------------------------------------------------


def _ensure_default_role(
    storage: Any,
    user_id: str,
    desired_role_ids: set[str] | None = None,
) -> None:
    """Self-heal safety-net: assign builtin-viewer if the user has zero roles.

    Runs after :func:`apply_role_mapping` on both the new-user and
    existing-identity paths so a user stranded by a transient failure
    during initial role mapping (e.g. a DB blip after ``create_oidc_user``
    committed) recovers on next login.  ``assigned_by="oidc-default"``
    deliberately differs from ``"oidc"`` so claim-driven revocation in
    ``apply_role_mapping`` leaves it alone.

    The optional ``desired_role_ids`` is a hint: when the caller already
    knows claim-driven mapping populated at least one role, we skip the
    ``list_user_roles`` query.

    No-op when builtin-viewer is unavailable (admin removed it from the
    role table) or the user already has at least one role.

    Note: if an admin manually strips all roles from an OIDC user, this
    helper will re-grant viewer on the next login.  The documented way to
    deny an OIDC user access is to unlink their OIDC identity, not to
    strip roles.
    """
    if desired_role_ids:
        return
    if storage.get_role("builtin-viewer") is None:
        return
    if storage.list_user_roles(user_id):
        return
    storage.assign_role(user_id, "builtin-viewer", "oidc-default")


def provision_oidc_user(
    storage: Any,
    config: oauth_oidc.OIDCConfig,
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
    # Entra `oid`+`tid` are the STABLE, cross-app user key. The `sub` above is
    # pairwise (a different value per application), so it cannot correlate this
    # user across services; oid+tid can. Captured here (present in every v2.0 ID
    # token, no extra scope). "" for IdPs that don't emit them.
    # `or ""` (not a get default) so a present-but-null claim collapses to the
    # same "" sentinel — `.get(k, "")` returns None when the key is present with
    # a JSON null, and str(None) would store the bogus non-empty value "None".
    oid = str(claims.get("oid") or "")
    tid = str(claims.get("tid") or "")
    display_name = str(claims.get("name", "") or claims.get("preferred_username", "") or email)

    # Try to find existing identity
    identity = storage.get_oidc_identity(issuer, sub)
    if identity is not None:
        user_id = identity["user_id"]
        # Passing oid/tid backfills them onto identities created before this
        # change, on the user's next login.
        storage.update_oidc_identity_login(issuer, sub, oid=oid, tid=tid)
        desired_role_ids = apply_role_mapping(storage, user_id, claims, config)
        _ensure_default_role(storage, user_id, desired_role_ids)
        user: dict[str, str] | None = storage.get_user(user_id)
        if user is None:
            raise oauth_oidc.OIDCError(f"OIDC identity references missing user: {user_id}")
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
            oid=oid,
            tid=tid,
        )
    except StorageConflictError as exc:
        raise oauth_oidc.OIDCError(f"OIDC provisioning failed: {exc}") from exc

    desired_role_ids = apply_role_mapping(storage, user_id, claims, config)
    _ensure_default_role(storage, user_id, desired_role_ids)

    created_user: dict[str, str] | None = storage.get_user(user_id)
    if created_user is None:
        raise oauth_oidc.OIDCError(f"Failed to retrieve newly created user: {user_id}")

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
    raise oauth_oidc.OIDCError("Failed to generate unique username")


# ---------------------------------------------------------------------------
# Role mapping
# ---------------------------------------------------------------------------


def apply_role_mapping(
    storage: Any,
    user_id: str,
    claims: dict[str, Any],
    config: oauth_oidc.OIDCConfig,
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
