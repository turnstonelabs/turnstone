"""Model-provider OAuth orchestration, cache identity, and refusal policy."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Any

from turnstone.core.log import get_logger
from turnstone.core.model_registry import (
    MODEL_AUTH_TEXT_MAX_LEN,
    sanitize_backend_auth_scopes,
    strip_control_characters,
)
from turnstone.core.oauth import grants as oauth_grants
from turnstone.core.oauth import http as oauth_http
from turnstone.core.oauth import locking as oauth_locking
from turnstone.core.oauth import tokens as oauth_tokens
from turnstone.core.token_store import crypto as token_store_crypto
from turnstone.core.token_store import store as token_store_store

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Model-provider dynamic authentication
# ---------------------------------------------------------------------------
# Deliberately lighter than get_obo_access_token_classified: a model backend is
# ONE resource addressed by a single audience, redeemed identically for every
# alias that points at it — not a per-server grant graph.  So this owns no
# dead-grant classification or re-consent affordance.  It reuses the same mint
# legs, credential store, RT-rotation CAS, cluster credential lock, AND the same
# ``mcp_user_tokens`` mint-cache row the classified path uses (refresh_token=NULL,
# "cache, not custody") — keyed under a synthetic ``__model_obo__:<alias>``
# server name. The DB row shares the token across workers; a loop-local memo
# avoids a SQL read + decrypt on every warm model turn.

# Once-per-(cause, audience) dedup for the OPERATOR-CONFIG model-mint
# misconfiguration warnings: the conditions are deployment-stable and the
# mints run per model call per lane, so repeating them is amplification, not
# signal. The caller's per-turn fallback/refusal line remains the heartbeat
# and names the cause inline via ``model_mint_refusal_cause``, so dedup here
# never costs mid-incident visibility.
#
# SPLIT namespaces, deliberately: this set holds only deployment-config
# causes, bounded by causes x configured audiences, while the one PER-USER
# cause lives in its own bounded set below. Shared, enough users without an
# OIDC sign-in would saturate the cap and permanently silence every later
# operator-config cause; split, neither class can starve the other.
_MODEL_MINT_MISCONFIG_WARNED: set[str] = set()


# Per-(user, audience) dedup for model_obo.missing_credential; its
# saturation silences only THIS cause. Tuple keys, not joined strings: user
# ids and api:// audiences can both contain ``:``, so a concatenated key
# could collide two distinct pairs.
_MODEL_OBO_MISSING_CRED_WARNED: set[tuple[str, str]] = set()


# Last-known mint refusal cause per (prefix, cause-key, user). The CAUSE
# layer above is deduped to once per process, so this record lets the
# DECISION layer (the session's per-turn heartbeat) name the cause inline on
# every occurrence without re-amplifying the deduped warning. The cause key
# is the alias-keyed cache key (plus the pinned leg for OBO mints), so two
# aliases can never cross-stamp. The key carries the USER: one user's
# successful mint must not clear another's recorded cause, nor stamp its own
# into another's heartbeat — app-identity mints record under the shared
# MODEL_APP_MINT_PRINCIPAL. Written at every refusal diagnosis even when the
# warning was dedup-suppressed; cleared only by the recording user's
# successful mint. Tuple keys — user ids and cache keys can both contain
# ``:``.
_MODEL_MINT_LAST_CAUSE: dict[tuple[str, str, str], str] = {}


# The cause map's OWN bound, not the warn-set's: keys are per
# (prefix, cause-key, user) and the cause key carries the alias and leg
# axes, so cardinality is users x aliases — the 512-entry warn-set cap
# starves real diagnostics at deployment scale. Records are readback state,
# not log lines, so when full the LEAST-RECENTLY-STAMPED entry is evicted
# rather than the newest dropped: the map always records the latest refusal.
_CAUSE_RECORD_CAP = 4096


def _record_mint_refusal_cause(prefix: str, cause_key: str, user_id: str, cause: str) -> None:
    # ``cause_key`` matches the reader's contract: the alias-keyed cache key
    # for model_app, the model_obo_cause_key spelling for model_obo.
    key = (prefix, cause_key, user_id)
    # Re-stamps move the record to the newest insertion position (dict
    # overwrite would keep it at its ORIGINAL slot), so eviction below hits
    # the least-recently-stamped record — never the hottest one an operator
    # is actively debugging.
    _MODEL_MINT_LAST_CAUSE.pop(key, None)
    if len(_MODEL_MINT_LAST_CAUSE) >= _CAUSE_RECORD_CAP:
        del _MODEL_MINT_LAST_CAUSE[next(iter(_MODEL_MINT_LAST_CAUSE))]
    _MODEL_MINT_LAST_CAUSE[key] = cause


# Cooldown short-circuits need no re-stamp: the cause persists until its user's mint succeeds.


def _clear_mint_refusal_cause(prefix: str, cause_key: str, user_id: str) -> None:
    _MODEL_MINT_LAST_CAUSE.pop((prefix, cause_key, user_id), None)


def model_mint_refusal_cause(prefix: str, cause_key: str, user_id: str) -> str:
    """Best-effort cause of the most recent refused mint under *cause_key*.

    ``prefix`` is ``"model_obo"`` or ``"model_app"``; ``cause_key`` is
    :func:`model_app_cache_server`'s key for app mints and the full
    :func:`model_obo_cause_key` key for OBO mints — the record shares the
    cache key's per-alias granularity plus the pinned leg, so two aliases
    (or two mode-variants of one alias history) never cross-stamp, and
    readers must build the key with those helpers. ``user_id`` is the
    minting principal the cause was recorded under — the acting user for
    OBO mints, :data:`MODEL_APP_MINT_PRINCIPAL` for app-identity mints.
    Returns ``""`` when no refusal has been recorded in this process for
    that principal (or their mint has succeeded since); callers render that
    as unknown.
    """
    return _MODEL_MINT_LAST_CAUSE.get((prefix, cause_key, user_id), "")


def reset_model_mint_warn_state_for_tests() -> None:
    """Empty every process-global mint warn/dedup/cause namespace.

    Resets model-owned state and the shared grant warning namespace. Test modules
    reach it through ``tests/_oidc_test_helpers.mint_warn_state_reset()``
    rather than hand-listing namespaces, which drifts.
    """
    _MODEL_MINT_MISCONFIG_WARNED.clear()
    _MODEL_OBO_MISSING_CRED_WARNED.clear()
    oauth_grants._ENTRA_SCOPE_IGNORED_WARNED.clear()
    _MODEL_MINT_LAST_CAUSE.clear()


def _warn_model_mint_misconfig_once(
    event: str, audience: str, user_id: str, *, cause_key: str, **fields: Any
) -> None:
    # Record the cause FIRST, unconditionally: the warning below is deduped,
    # but the heartbeat's readback must reflect every occurrence, attributed
    # to the principal whose mint was refused. ``cause_key`` is REQUIRED and
    # names the reader's exact key — model_app_cache_server's key for app
    # mints, the model_obo_cause_key spelling for OBO mints — because the
    # cause record must share the reader's granularity or two callers
    # cross-stamp and cross-clear each other's causes. The human-facing warn
    # keeps the PLAIN audience for its dedup key and log field.
    prefix, _, cause = event.partition(".")
    _record_mint_refusal_cause(prefix, cause_key, user_id, cause)
    oauth_grants._warn_dedup_once(
        _MODEL_MINT_MISCONFIG_WARNED, f"{event}:{audience}", event, audience=audience, **fields
    )


def _warn_model_obo_missing_credential_once(audience: str, user_id: str, *, cause_key: str) -> None:
    """Name the missing-credential cause once per (user, audience).

    ``user_id`` deliberately stays IN the log line: this is an auth event,
    and remediation — link THIS user's OIDC sign-in — needs the principal,
    the same practice as the audit rows, which carry ids.
    """
    _record_mint_refusal_cause("model_obo", cause_key, user_id, "missing_credential")
    oauth_grants._warn_dedup_once(
        _MODEL_OBO_MISSING_CRED_WARNED,
        (user_id, audience),
        "model_obo.missing_credential",
        audience=audience,
        user_id=user_id,
    )


def _warn_mint_oidc_cause(
    prefix: str, oidc_config: Any, audience: str, user_id: str, *, cause_key: str
) -> None:
    """Name the OIDC-not-ready cause for a model mint, once per (cause, audience).

    Single-sourced for both mints (``model_obo``/``model_app`` prefix) so the
    healing-state condition and the cause taxonomy cannot diverge. The
    healing state stays distinct: ``discovery_retryable`` means OIDC is
    configured and self-heals via ordinary auth traffic, so reporting it as
    "not enabled" would aim the operator at healthy config and burn the
    not-enabled dedup slot on a transient.
    """
    if getattr(oidc_config, "discovery_retryable", False):
        _warn_model_mint_misconfig_once(
            f"{prefix}.oidc_discovery_pending", audience, user_id, cause_key=cause_key
        )
    else:
        _warn_model_mint_misconfig_once(
            f"{prefix}.oidc_not_enabled", audience, user_id, cause_key=cause_key
        )


def _warn_mint_store_unavailable(
    prefix: str,
    audience: str,
    user_id: str,
    token_store: Any,
    storage: Any,
    *,
    cause_key: str,
) -> None:
    """Name the missing token-store/storage cause, once per (cause, audience).

    Single-sourced for both mints, same rationale as
    :func:`_warn_mint_oidc_cause`.
    """
    _warn_model_mint_misconfig_once(
        f"{prefix}.token_store_unavailable",
        audience,
        user_id,
        cause_key=cause_key,
        has_token_store=token_store is not None,
        has_storage=storage is not None,
    )


def _model_mint_memo(app_state: Any) -> dict[tuple[str, str], token_store_store.UserTokenPlain]:
    """Return the mcp-loop-owned model-token memo."""
    memo = getattr(app_state, "model_auth_token_cache", None)
    if not isinstance(memo, dict):
        memo = {}
        app_state.model_auth_token_cache = memo
    return memo


def invalidate_model_mint_memo(
    app_state: Any,
    *,
    user_id: str,
    server_prefix: str = token_store_store.MODEL_OBO_CACHE_PREFIX,
) -> int:
    """Remove memo entries for one principal and synthetic-key prefix.

    This must run on the manager's MCP loop. OIDC unlink schedules it there
    alongside deleting the corresponding DB rows, so the in-process fast path
    cannot extend a revoked bearer beyond the purge.
    """
    memo = _model_mint_memo(app_state)
    keys = [key for key in memo if key[0] == user_id and key[1].startswith(server_prefix)]
    for key in keys:
        memo.pop(key, None)
    return len(keys)


async def _serve_fresh_mint_cache(
    *,
    app_state: Any,
    token_store: token_store_store.TokenStore,
    user_id: str,
    cache_server: str,
    audience: str,
    scopes: str,
) -> str | None:
    """Serve a fresh model-mint token from the loop memo or encrypted DB row."""
    key = (user_id, cache_server)
    memo = _model_mint_memo(app_state)
    plain = memo.get(key)
    if oauth_tokens._is_fresh_obo_cache_row(plain, audience, scopes) and plain is not None:
        return plain["access_token"]
    memo.pop(key, None)
    try:
        plain = await asyncio.to_thread(token_store.get_user_token, user_id, cache_server)
    except token_store_crypto.TokenDecryptError:
        # A mint-cache row encrypted under a retired key is only a cache miss.
        return None
    if not oauth_tokens._is_fresh_obo_cache_row(plain, audience, scopes) or plain is None:
        return None
    memo[key] = plain
    return plain["access_token"]


def _memoize_minted_token(
    app_state: Any,
    *,
    user_id: str,
    cache_server: str,
    access_token: str,
    expires_at: str | None,
    scopes: str,
    issuer: str,
    audience: str,
) -> None:
    """Install a freshly minted token in the loop-local memo."""
    now = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
    _model_mint_memo(app_state)[(user_id, cache_server)] = {
        "user_id": user_id,
        "server_name": cache_server,
        "access_token": access_token,
        "refresh_token": None,
        "expires_at": expires_at,
        "scopes": scopes or None,
        "as_issuer": issuer,
        "audience": audience,
        "created": now,
        "last_refreshed": now,
    }


# The unit separator joins the structural axes of synthetic key spellings:
# the digest marker in _bounded_synthetic_key's over-bound arm, and the
# grant-leg suffix in model_obo_cause_key. It cannot appear in the joined
# values — the key builders strip control characters from the alias and the
# leg names are literals — so a separator-joined spelling can never collide
# with a plain one. A PRINTABLE separator could: aliases legally contain
# ``.`` and ``-``, and DB-direct rows could carry anything. Rendered
# surfaces show it JSON-escaped (U+001F); the cache row's own
# ``audience``/``scopes`` columns stay the legible record.
_SYNTHETIC_KEY_SEP = chr(0x1F)


# Byte bound for the synthetic mint-cache keys: ``mcp_user_tokens.server_name``
# is half the table's PRIMARY KEY and btree-indexed, and PostgreSQL's index
# tuple limit is ~2704 bytes — a key at most 2600 UTF-8 bytes stays safely
# under it. Console-written aliases are capped at 64 ASCII characters, so
# only a DB-direct row can ever reach the bound.
_SYNTHETIC_KEY_MAX_BYTES = 2600


def _bounded_synthetic_key(prefix: str, remainder: str) -> str:
    """``prefix + remainder``, or its digest spelling past the byte bound.

    Takes the prefix and remainder separately and joins internally, so the
    digest spelling keeps the builder's *prefix* by construction — bounded
    keys stay classified (model-OBO vs model-app rows) and prefix-scan
    consumers (deprovisioning, obo_server_names) always match them. The
    digest arm's separator-after-prefix shape is disjoint from every
    literal key — the callers strip control characters from *remainder*.
    """
    candidate = f"{prefix}{remainder}"
    if len(candidate.encode("utf-8")) <= _SYNTHETIC_KEY_MAX_BYTES:
        return candidate
    whole = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:48]
    return f"{prefix}{_SYNTHETIC_KEY_SEP}{whole}"


class MintDispatchContractError(ValueError):
    """A model-mint call site violated the dispatch contract.

    Raised ONLY for call-site contract violations — scopes without the
    exchange leg pinned, over-length scopes from a raw caller — never for
    operator state: misconfiguration, missing credentials and rejected
    grants all return ``None`` with a recorded cause instead. The sync
    bridge surfaces this type at ERROR while demoting ordinary mint
    failures to debug.
    """


def _normalized_mint_scopes(scopes: Any) -> str:
    """The mint stack's one scopes normalization.

    Delegates to :func:`sanitize_backend_auth_scopes` — the shared spelling,
    under which whitespace separators become single spaces (never
    concatenation) and no control character survives from any caller.
    Idempotent, so registry- and console-normalized values pass through
    unchanged; raw values from direct callers (harness, tests) land on the
    same spelling the mint sends to the IdP, stores in the cache row's
    ``scopes`` column, and compares in the freshness gate. Over-length
    input raises
    :class:`MintDispatchContractError` rather than silently truncating:
    every production path is registry/console-bounded first, so an over-cap
    value here is a raw call site's bug, and a silent slice would mint a
    narrower privilege request than the caller asked for.
    """
    if not scopes:
        return ""
    normalized = sanitize_backend_auth_scopes(scopes)
    if len(normalized) > MODEL_AUTH_TEXT_MAX_LEN:
        raise MintDispatchContractError(
            f"mint scopes exceed MODEL_AUTH_TEXT_MAX_LEN ({MODEL_AUTH_TEXT_MAX_LEN} characters)"
        )
    return normalized


def model_obo_cache_server(alias: str) -> str:
    """Synthetic ``mcp_user_tokens`` server key for a model alias's mint-cache row.

    Public: the session heartbeat and the e2e harness build the same key to
    read the per-alias refusal-cause record, which shares this key's
    granularity.

    IDENTITY-KEYED, exactly like real MCP rows: ``mcp_servers.name`` is
    unique and its OAuth rows key on it, and a model definition's unique
    ``alias`` plays the same role here — one owning definition per key, so
    the admin lifecycle purge (rename, re-aim, delete) deletes only rows the
    edited definition owns, never a sibling's. The minted bearer's SHAPE
    (audience + scopes) deliberately does NOT enter the key: it lives in the
    row's ``audience``/``scopes`` columns, where the freshness gate compares
    it against the definition's current values on every read — so a re-aimed
    alias refuses its old row immediately and the next mint overwrites it in
    place, with no stranded key. Memoization and the single-flight lock
    derive from this key, so one broken alias can never suppress another —
    even two aliases fronting the same gateway; cooldown and backoff key on
    this key PLUS the dispatch shape (see the mint bodies), so a config
    repair is an instant clean slate rather than waiting out a cooldown the
    superseded shape armed.

    Console-written aliases match ``[a-zA-Z0-9._-]{1,64}``, so the key is
    short ASCII and always literal. The strip + byte bound below only defend
    the raw-caller seam (DB-direct rows, tests): control characters are
    removed so the digest spelling of :func:`_bounded_synthetic_key` stays
    disjoint from every literal key, and an over-bound alias collapses to
    that digest spelling rather than breaking index persistence.
    """
    return _bounded_synthetic_key(
        token_store_store.MODEL_OBO_CACHE_PREFIX, strip_control_characters(alias)
    )


def model_obo_cause_key(alias: str, grant_leg: str | None = None) -> str:
    """The refusal-cause record's key for a model-OBO mint.

    :func:`model_obo_cache_server`'s per-alias granularity plus the pinned
    ``grant_leg``, so an alias edited across modes cannot cross-stamp its
    own history — a legged mint's refusal is recorded, cleared, and read
    under its own leg. Leg-``None`` callers get the plain cache key, so
    legacy profile-driven mints keep their records.
    """
    key = model_obo_cache_server(alias)
    if grant_leg:
        return f"{key}{_SYNTHETIC_KEY_SEP}{grant_leg}"
    return key


async def mint_obo_access_token(
    *,
    app_state: Any,
    user_id: str,
    alias: str,
    audience: str,
    scopes: str = "",
    grant_leg: str | None = None,
    force_refresh: bool = False,
) -> str | None:
    """Per-user delegated access token for the model definition *alias*.

    Redeems the user's captured refresh credential (``oidc_user_credentials``)
    for *audience* via the configured OBO grant profile, persists any rotated
    refresh token (value CAS, cluster-locked exactly like the MCP mint), and
    caches the minted access token in an ``mcp_user_tokens`` mint-cache row
    (``refresh_token=NULL``) keyed ``__model_obo__:<alias>`` — the same
    "cache, not custody" row the classified path uses — so the token is shared
    across worker nodes and inspectable, until shortly before expiry. The key
    is IDENTITY-keyed on the owning definition (see
    :func:`model_obo_cache_server`); the minted bearer's shape lives in the
    row's ``audience``/``scopes`` columns, which the freshness gate compares
    against the values passed here — so after a definition is re-aimed the
    old row refuses immediately and the next mint overwrites it in place.

    ``scopes`` is threaded to the mint leg exactly as an MCP row's
    ``oauth_scopes`` is: the rfc8693 exchange leg requests it (exchange-capable
    IdPs refuse an audience whose scope was not requested), the entra leg
    ignores it by design.

    ``grant_leg`` pins the leg the CALLER's auth mode names (``"entra"`` /
    ``"rfc8693"``); when the deployment's configured profile differs, the mint
    refuses with a ``grant_profile_mismatch`` cause and no IdP traffic — a
    mode is a dialect commitment, not a hint. ``None`` keeps the legacy
    profile-driven dispatch.

    Returns ``None`` — the signal for callers to fall back to their static
    credential — when OIDC is disabled/unconfigured, the profile has no mint
    leg or contradicts ``grant_leg``, the user has no captured credential (or
    it won't decrypt), or the mint is rejected.  This is the model-provider
    entry point; the MCP-server path uses
    :func:`get_obo_access_token_classified`, which additionally owns
    per-server cache rows, dead-grant classification, and consent affordances
    this helper deliberately omits.
    """
    # The registry normalizer rejects control characters and the console
    # write path strips them; stripping here too closes the raw-direct-caller
    # seam (harness, tests) exactly like the scopes strip below, so no
    # control byte can reach the cache row or the IdP request. Controls
    # first, THEN whitespace — an edge control character must not shield
    # the edge whitespace behind it (the same ordering _clean_oauth_text
    # uses). The alias is stripped inside the key builder for the same
    # reason.
    audience = strip_control_characters(str(audience or "")).strip()
    alias = str(alias or "").strip()
    if not user_id or not alias or not audience:
        return None
    scopes = _normalized_mint_scopes(scopes)
    if scopes and grant_leg != "rfc8693":
        # Caller contract, not operator config: only the token-exchange leg
        # reads a scope request, so scopes without that leg pinned means the
        # call site's dispatch is wrong — minting anyway would either run an
        # unpinned leg the row's mode never committed to, or hand the entra
        # leg scopes it ignores while the cache row claims them. Raise
        # rather than the operator-facing None every config refusal uses.
        raise MintDispatchContractError("mint_obo_access_token: scopes require grant_leg='rfc8693'")
    # Cache row, cooldown and single-flight lock all key on the owning
    # alias: two definitions are separate mint identities end to end even
    # when they front the same gateway, so one's success or failure can
    # never clear, suppress, or serve the other's.
    cache_server = model_obo_cache_server(alias)
    # The refusal-cause record additionally keys on the pinned leg: an
    # alias edited across modes must not overwrite its other leg's cause,
    # and the session heartbeat builds the same key.
    cause_key = model_obo_cause_key(alias, grant_leg)
    # Cooldown/backoff key on (alias, SHAPE), never the shared credential
    # key: the alias axis keeps one broken definition from suppressing a
    # sibling, and the shape axis makes an operator's config repair an
    # instant clean slate — a corrected audience or scopes spells a
    # different key with no armed state, so the first retry mints
    # immediately instead of waiting out a cooldown armed by the superseded
    # shape (in-process state is per node; the console purge reaches only
    # DB rows). The cache row and the single-flight lock deliberately stay
    # on the identity key: one row, one mint at a time, whatever the shape.
    cooldown_key = f"{cache_server}{_SYNTHETIC_KEY_SEP}{audience}{_SYNTHETIC_KEY_SEP}{scopes}"
    # The caller's ``fallback_to_static`` is the DECISION layer and fires per
    # turn but names no cause; these branches are the CAUSE layer, deduped to
    # once per (cause, audience) per process.
    oidc_config = getattr(app_state, "oidc_config", None)
    if oidc_config is None or not getattr(oidc_config, "enabled", False):
        _warn_mint_oidc_cause("model_obo", oidc_config, audience, user_id, cause_key=cause_key)
        return None
    token_store: token_store_store.TokenStore | None = getattr(app_state, "mcp_token_store", None)
    storage = oauth_tokens._get_storage(app_state)
    if token_store is None or storage is None:
        _warn_mint_store_unavailable(
            "model_obo", audience, user_id, token_store, storage, cause_key=cause_key
        )
        return None
    profile = str(getattr(oidc_config, "obo_grant_profile", "") or "")
    mint = oauth_grants._OBO_MINT_LEGS.get(profile)
    if mint is None:
        # Deployment-stable, per-call-per-lane — same dedup rationale as the
        # sibling causes.
        _warn_model_mint_misconfig_once(
            "model_obo.unsupported_grant_profile",
            audience,
            user_id,
            cause_key=cause_key,
            alias=alias,
            profile=profile,
        )
        return None
    if grant_leg is not None and profile != grant_leg:
        # The mode's dialect and the deployment's dialect disagree — running
        # the profile's leg anyway would send a request the mode's IdP shape
        # never satisfies (the pre-dedicated-mode overload #955 closed).
        # Refused before any IdP traffic, same cause taxonomy as the app
        # mint's profile refusal. Surfaces as None like every refusal here:
        # the session's static-fallback policy (model.auth_fail_closed)
        # governs what happens next, deliberately not a special case — a
        # RULED disposition, including for pre-#955 rows that relied on the
        # profile-driven overload and land here after upgrade (they never
        # minted on a scope-gating IdP; docs/oidc.md's pairing section and
        # the per-turn heartbeat's cause= readback carry the operator
        # signal).
        _warn_model_mint_misconfig_once(
            "model_obo.grant_profile_mismatch",
            audience,
            user_id,
            cause_key=cause_key,
            alias=alias,
            profile=profile,
            required_profile=grant_leg,
        )
        return None
    issuer = str(getattr(oidc_config, "issuer", "") or "")
    cached_token = await _serve_fresh_mint_cache(
        app_state=app_state,
        token_store=token_store,
        user_id=user_id,
        cache_server=cache_server,
        audience=audience,
        scopes=scopes,
    )
    if cached_token and not force_refresh:
        oauth_tokens._clear_refresh_backoff(app_state, user_id, cooldown_key)
        return cached_token
    if not cached_token and oauth_tokens._refresh_in_cooldown(app_state, user_id, cooldown_key):
        return None

    # Single-flight the mint: a per-(user, alias) asyncio lock for local
    # coalescing, then the SAME per-(user, issuer) credential lock + cluster
    # advisory lock the MCP mint takes — the refresh credential is the shared
    # mutable resource (rotation write-back), so a model mint and an MCP mint for
    # the same user serialise on it cluster-wide.  Order is always
    # audience → credential and nothing takes the reverse, so no deadlock.
    lock = oauth_locking._refresh_lock_for(app_state, user_id, cache_server)
    credential_key = f"__obo__:{issuer}"
    credential_lock = oauth_locking._refresh_lock_for(app_state, user_id, credential_key)
    pg_lock = await oauth_locking._acquire_pg_refresh_lock(storage, user_id, credential_key)
    try:
        async with lock, credential_lock, pg_lock:
            cached_token = await _serve_fresh_mint_cache(
                app_state=app_state,
                token_store=token_store,
                user_id=user_id,
                cache_server=cache_server,
                audience=audience,
                scopes=scopes,
            )
            if cached_token and not force_refresh:
                oauth_tokens._clear_refresh_backoff(app_state, user_id, cooldown_key)
                return cached_token
            if not cached_token and oauth_tokens._refresh_in_cooldown(
                app_state, user_id, cooldown_key
            ):
                return None

            try:
                credential = await asyncio.to_thread(
                    token_store.get_oidc_credential, user_id, issuer
                )
            except token_store_crypto.TokenDecryptError:
                log.warning(
                    "mcp_server.oauth.obo_credential_decrypt_failed",
                    user_id=user_id,
                    server_name=cache_server,
                    exc_info=True,
                )
                _record_mint_refusal_cause(
                    "model_obo", cause_key, user_id, "credential_decrypt_failure"
                )
                oauth_tokens._arm_cooldown(app_state, user_id, cooldown_key)
                return None
            if credential is None:
                _warn_model_obo_missing_credential_once(audience, user_id, cause_key=cause_key)
                oauth_tokens._arm_cooldown(app_state, user_id, cooldown_key)
                return None

            async def _persist_rotation(new_credential_rt: str) -> None:
                # Best-effort, same contract as the classified path: the mint
                # already produced a working token, so rotation write-back
                # failure must not discard it.
                try:
                    await asyncio.to_thread(
                        token_store.update_oidc_credential_after_redeem,
                        user_id,
                        issuer,
                        refresh_token=new_credential_rt,
                        expected_current=credential["refresh_token"],
                    )
                except Exception:
                    log.error(
                        "model_obo.rotation_persist_failed",
                        user_id=user_id,
                        audience=audience,
                        exc_info=True,
                    )

            try:
                async with oauth_http._enter_mint_client(app_state) as mint_client:
                    tokens = await mint(
                        oidc_config=oidc_config,
                        credential_refresh_token=credential["refresh_token"],
                        audience=audience,
                        scopes=scopes,
                        http_client=mint_client,
                        persist_rotation=_persist_rotation,
                    )
            except oauth_http.OAuthRefreshError:
                oauth_tokens._arm_cooldown(app_state, user_id, cooldown_key)
                _record_mint_refusal_cause("model_obo", cause_key, user_id, "mint_failed")
                log.warning(
                    "model_obo.mint_failed",
                    user_id=user_id,
                    alias=alias,
                    audience=audience,
                    exc_info=True,
                )
                return None

            access_token = tokens.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                oauth_tokens._arm_cooldown(app_state, user_id, cooldown_key)
                _record_mint_refusal_cause(
                    "model_obo", cause_key, user_id, "mint_missing_access_token"
                )
                log.warning(
                    "model_obo.mint_missing_access_token",
                    user_id=user_id,
                    audience=audience,
                )
                return None
            expires_at = oauth_tokens._expires_at_from_response(
                tokens, default_ttl_seconds=oauth_tokens._OBO_DEFAULT_TTL_SECONDS
            )
            try:
                await oauth_tokens._persist_obo_cache_row(
                    token_store,
                    user_id,
                    cache_server,
                    access_token=access_token,
                    expires_at=expires_at,
                    scopes=scopes,
                    issuer=issuer,
                    audience=audience,
                )
            except Exception:
                log.warning(
                    "model_obo.cache_persist_failed",
                    user_id=user_id,
                    audience=audience,
                    exc_info=True,
                )
            _memoize_minted_token(
                app_state,
                user_id=user_id,
                cache_server=cache_server,
                access_token=access_token,
                expires_at=expires_at,
                scopes=scopes,
                issuer=issuer,
                audience=audience,
            )
            oauth_tokens._clear_refresh_backoff(app_state, user_id, cooldown_key)
            _clear_mint_refusal_cause("model_obo", cause_key, user_id)
            log.info(
                "model_obo.minted",
                user_id=user_id,
                alias=alias,
                audience=audience,
                cache_server=cache_server,
            )
            return access_token
    finally:
        oauth_locking._prune_token_lock_when_idle(app_state, user_id, cache_server, lock)


# ---------------------------------------------------------------------------
# App-identity (client-credentials) model token — Turnstone's own SSO app reg
# ---------------------------------------------------------------------------
# The ``auth_mode='entra_app'`` sibling of the OBO path: instead of a per-user
# On-Behalf-Of token it mints an APP token from the ``[oidc]`` client id + secret
# via the client-credentials grant. No user, no captured refresh token, no
# rotation — one token per owning definition, shared by everyone, so a gateway
# resolves it to a single machine (virtual-account) identity with no per-user
# attribution. It reuses the same DB mint-cache under a synthetic ``__app__``
# user.

_APP_CACHE_USER = token_store_store.MODEL_APP_MINT_PRINCIPAL


def model_app_cache_server(alias: str) -> str:
    """Synthetic ``mcp_user_tokens`` key for an app-credential mint-cache row.

    App tokens carry no user, so they cache once per owning definition under
    the shared ``__app__`` pseudo-user; the ``__model_app__:`` prefix keeps
    them distinct from per-user OBO rows (``__model_obo__:``) and real
    oauth_user server rows. Identity-keyed on the definition's unique
    ``alias`` exactly like :func:`model_obo_cache_server` (whose docstring
    carries the keying rationale, the freshness-gate rebind defense, and the
    strip + byte-bound raw-caller seam this builder shares). Public: this
    key doubles as the app mint's refusal-cause key, which the session
    heartbeat rebuilds.
    """
    return _bounded_synthetic_key(
        token_store_store.MODEL_APP_CACHE_PREFIX, strip_control_characters(alias)
    )


async def mint_app_access_token(
    *,
    app_state: Any,
    alias: str,
    audience: str,
    force_refresh: bool = False,
) -> str | None:
    """App-identity Entra access token for *audience* via client-credentials.

    Uses Turnstone's own SSO app registration (``[oidc]`` ``client_id`` +
    ``client_secret``) — no user, no captured refresh token, no rotation. One
    token per owning definition *alias*, shared by every caller and cached in
    an ``mcp_user_tokens`` row under the synthetic ``__app__`` user until
    shortly before expiry (identity-keyed like the OBO twin; the freshness
    gate compares the row's stored audience against the current one, so a
    re-aimed alias refuses its old row and overwrites it on the next mint).
    This is the ``auth_mode='entra_app'`` backend entry point —
    the "we already have SSO, let the app call the gateway as its own managed
    identity" path (a gateway resolves it to one virtual account, no per-user
    attribution). Because it needs no user context it also serves utility /
    coordinator / service / CLI turns that OBO cannot. Returns ``None`` — the
    signal to fall back to the static credential — when OIDC is
    disabled/unconfigured, the app has no secret, or the grant is rejected.
    """
    # Same raw-caller hygiene as the OBO twin: strip control characters —
    # controls first, then whitespace, so an edge control cannot shield
    # edge whitespace — so no control byte reaches the cache row or the
    # IdP request (the alias is stripped inside the key builder).
    audience = strip_control_characters(str(audience or "")).strip()
    alias = str(alias or "").strip()
    if not alias or not audience:
        return None
    # The refusal-cause key IS the alias-keyed cache key — the session
    # heartbeat rebuilds it via the public builder.
    cause_key = model_app_cache_server(alias)
    # The CAUSE layer, as in mint_obo_access_token: same
    # once-per-(cause, audience) dedup.
    oidc_config = getattr(app_state, "oidc_config", None)
    if oidc_config is None or not getattr(oidc_config, "enabled", False):
        _warn_mint_oidc_cause(
            "model_app", oidc_config, audience, _APP_CACHE_USER, cause_key=cause_key
        )
        return None
    profile = str(getattr(oidc_config, "obo_grant_profile", "") or "")
    if profile != "entra":
        _warn_model_mint_misconfig_once(
            "model_app.unsupported_grant_profile",
            audience,
            _APP_CACHE_USER,
            cause_key=cause_key,
            alias=alias,
            profile=profile,
        )
        return None
    client_id = str(getattr(oidc_config, "client_id", "") or "")
    client_secret = str(getattr(oidc_config, "client_secret", "") or "")
    token_endpoint = str(getattr(oidc_config, "token_endpoint", "") or "")
    token_store: token_store_store.TokenStore | None = getattr(app_state, "mcp_token_store", None)
    storage = oauth_tokens._get_storage(app_state)
    if token_store is None or storage is None:
        _warn_mint_store_unavailable(
            "model_app", audience, _APP_CACHE_USER, token_store, storage, cause_key=cause_key
        )
        return None
    issuer = str(getattr(oidc_config, "issuer", "") or "")

    cache_server = cause_key
    # Cooldown/backoff key on (alias, SHAPE), matching the OBO twin: a
    # corrected audience is an instant clean slate instead of waiting out a
    # cooldown the superseded audience armed.
    cooldown_key = f"{cache_server}{_SYNTHETIC_KEY_SEP}{audience}"
    cached_token = await _serve_fresh_mint_cache(
        app_state=app_state,
        token_store=token_store,
        user_id=_APP_CACHE_USER,
        cache_server=cache_server,
        audience=audience,
        scopes="",
    )
    if cached_token and not force_refresh:
        oauth_tokens._clear_refresh_backoff(app_state, _APP_CACHE_USER, cooldown_key)
        return cached_token
    if not cached_token and oauth_tokens._refresh_in_cooldown(
        app_state, _APP_CACHE_USER, cooldown_key
    ):
        return None
    if not (client_id and client_secret and token_endpoint):
        oauth_tokens._arm_cooldown(app_state, _APP_CACHE_USER, cooldown_key)
        _record_mint_refusal_cause(
            "model_app", cause_key, _APP_CACHE_USER, "credentials_unavailable"
        )
        log.warning(
            "model_app.credentials_unavailable",
            alias=alias,
            has_client_id=bool(client_id),
            has_client_secret=bool(client_secret),
            has_token_endpoint=bool(token_endpoint),
        )
        return None

    # Single-flight the mint. No per-user credential to rotate, so only the
    # per-alias local + cluster lock is taken (no credential lock).
    lock = oauth_locking._refresh_lock_for(app_state, _APP_CACHE_USER, cache_server)
    pg_lock = await oauth_locking._acquire_pg_refresh_lock(storage, _APP_CACHE_USER, cache_server)
    try:
        async with lock, pg_lock:
            cached_token = await _serve_fresh_mint_cache(
                app_state=app_state,
                token_store=token_store,
                user_id=_APP_CACHE_USER,
                cache_server=cache_server,
                audience=audience,
                scopes="",
            )
            if cached_token and not force_refresh:
                oauth_tokens._clear_refresh_backoff(app_state, _APP_CACHE_USER, cooldown_key)
                return cached_token
            if not cached_token and oauth_tokens._refresh_in_cooldown(
                app_state, _APP_CACHE_USER, cooldown_key
            ):
                return None

            try:
                async with oauth_http._enter_mint_client(app_state) as mint_client:
                    tokens = await oauth_grants.mint_client_credentials(
                        token_endpoint=token_endpoint,
                        client_id=client_id,
                        client_secret=client_secret,
                        audience=audience,
                        http_client=mint_client,
                    )
            except oauth_http.OAuthRefreshError:
                oauth_tokens._arm_cooldown(app_state, _APP_CACHE_USER, cooldown_key)
                _record_mint_refusal_cause("model_app", cause_key, _APP_CACHE_USER, "mint_failed")
                log.warning("model_app.mint_failed", alias=alias, audience=audience, exc_info=True)
                return None

            access_token = tokens.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                oauth_tokens._arm_cooldown(app_state, _APP_CACHE_USER, cooldown_key)
                _record_mint_refusal_cause(
                    "model_app", cause_key, _APP_CACHE_USER, "mint_missing_access_token"
                )
                log.warning("model_app.mint_missing_access_token", alias=alias, audience=audience)
                return None
            expires_at = oauth_tokens._expires_at_from_response(
                tokens, default_ttl_seconds=oauth_tokens._OBO_DEFAULT_TTL_SECONDS
            )
            try:
                await oauth_tokens._persist_obo_cache_row(
                    token_store,
                    _APP_CACHE_USER,
                    cache_server,
                    access_token=access_token,
                    expires_at=expires_at,
                    scopes="",
                    issuer=issuer,
                    audience=audience,
                )
            except Exception:
                log.warning("model_app.cache_persist_failed", audience=audience, exc_info=True)
            _memoize_minted_token(
                app_state,
                user_id=_APP_CACHE_USER,
                cache_server=cache_server,
                access_token=access_token,
                expires_at=expires_at,
                scopes="",
                issuer=issuer,
                audience=audience,
            )
            oauth_tokens._clear_refresh_backoff(app_state, _APP_CACHE_USER, cooldown_key)
            _clear_mint_refusal_cause("model_app", cause_key, _APP_CACHE_USER)
            log.info("model_app.minted", alias=alias, audience=audience, cache_server=cache_server)
            return access_token
    finally:
        oauth_locking._prune_token_lock_when_idle(app_state, _APP_CACHE_USER, cache_server, lock)
