"""Token-cache persistence, freshness, and shared refresh backoff mechanics."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from turnstone.core.oauth.context import TokenCoordination, oauth_context
from turnstone.core.oauth.work import durable_write

if TYPE_CHECKING:
    from turnstone.core.storage._protocol import StorageBackend
    from turnstone.core.token_store import store as token_store_store

_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 60


# Fallback lifetime for a minted obo cache row when the IdP omits the
# RFC 8693-optional ``expires_in``. Unlike oauth_user tokens (a missing expiry
# means an opaque token cached until a 401), an obo minted access token is
# always short-lived, so a NULL expiry must NOT read as "never expires" in the
# freshness gate. Conservative so the row re-mints soon rather than being served
# long past its real lifetime (which would also defeat audience/scope narrowing
# that relies on TTL turnover).
_OBO_DEFAULT_TTL_SECONDS = 300


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


# In-process (per-node) backoff bookkeeping for transient refresh failures,
# keyed ``(user_id, server_name)`` on the owning loop's ``TokenCoordination.backoff``.
# The cooldown timer short-circuits the token-endpoint round-trip during a
# sustained AS outage (perf); the ambiguous streak escalates an
# unclassifiable-but-persistent rejection to re-consent so a dead grant in a
# non-standard shape can't strand the user forever.
_REFRESH_TRANSIENT_COOLDOWN_SECONDS = 30.0


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


def _refresh_backoff_state(
    coordination: TokenCoordination, user_id: str, server_name: str
) -> _RefreshBackoffState:
    """Return (creating if absent) the backoff state for ``(user_id, server_name)``."""
    states = coordination.backoff
    key = (user_id, server_name)
    state = states.get(key)
    if state is None:
        state = _RefreshBackoffState()
        states[key] = state
    return state


def _arm_cooldown(
    coordination: TokenCoordination, user_id: str, server_name: str, *, permanent: bool = False
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
    state = _refresh_backoff_state(coordination, user_id, server_name)
    state.last_failure_monotonic = time.monotonic()
    state.last_failure_permanent = permanent
    return state


def _clear_refresh_backoff(coordination: TokenCoordination, user_id: str, server_name: str) -> None:
    """Drop the backoff state for ``(user_id, server_name)``.

    Called whenever a usable token is returned or the token is revoked, so a
    healthy grant resets the cooldown timer + ambiguous streak and the dict
    stays bounded to live ``(user, server)`` pairs.
    """
    coordination.backoff.pop((user_id, server_name), None)


def _refresh_in_cooldown(coordination: TokenCoordination, user_id: str, server_name: str) -> bool:
    """Return True while within the post-transient-failure cooldown window."""
    states = coordination.backoff
    state: _RefreshBackoffState | None = states.get((user_id, server_name))
    if state is None or not state.last_failure_monotonic:
        return False
    elapsed = time.monotonic() - state.last_failure_monotonic
    return elapsed < _REFRESH_TRANSIENT_COOLDOWN_SECONDS


async def _persist_obo_cache_row(
    token_store: token_store_store.TokenStore,
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
    await durable_write(token_store.delete_user_token, user_id, server_name)
    await durable_write(
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


def _is_fresh_obo_cache_row(
    plain: token_store_store.UserTokenPlain | None, current_audience: str, current_scopes: str
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
    context = oauth_context(app_state)
    storage = context.storage
    if storage is None:
        from turnstone.core.storage import get_storage

        try:
            storage = get_storage()
        except Exception:
            return None
        context.storage = storage
    return storage


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
