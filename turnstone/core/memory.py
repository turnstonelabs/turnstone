"""Persistence facade — delegates to the pluggable storage backend.

All functions maintain their existing signatures for consumers (session.py,
server.py, cli.py). The actual storage implementation lives in
``turnstone.core.storage``.

The established best-effort contract is preserved for operational failures.
Operations whose callers require positive durability return an explicit
failure sentinel rather than swallowing an exception into an indistinguishable
successful ``None``.  Typed invariant conflicts remain exceptions: callers
must never mistake a different immutable commit for a transient storage blip.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger
from turnstone.core.storage import (
    AttachmentWrite,
    ConversationCommitConflictError,
    ConversationCommitWorkstreamGoneError,
    get_storage,
)
from turnstone.core.workstream import WorkstreamKind

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.trajectory import Turn

log = get_logger(__name__)


def normalize_key(key: str) -> str:
    """Normalize a memory key for consistent lookup."""
    return key.lower().replace("-", "_").replace(" ", "_")


# -- Core conversation operations ---------------------------------------------


_TYPED_COMMIT_ERRORS = (ConversationCommitConflictError, ConversationCommitWorkstreamGoneError)


def _keyed_save(operation: Callable[[], int], describe: str) -> int:
    """Run one keyed save with the shared typed-passthrough frame.

    Typed commit outcomes must reach the session journal un-coerced — the
    next typed class belongs in ``_TYPED_COMMIT_ERRORS`` ONCE, for every
    wrapper (a missed wrapper would convert a permanent invariant conflict
    into a logged return-0 the journal retries forever). Operational
    failures log and return ``0``; the journal classifies and retries them.
    """
    try:
        return operation()
    except _TYPED_COMMIT_ERRORS:
        raise
    except Exception:
        log.warning("%s", describe, exc_info=True)
        return 0


def save_message(
    ws_id: str,
    role: str,
    content: str | None,
    tool_name: str | None = None,
    tool_call_id: str | None = None,
    provider_data: str | None = None,
    tool_calls: str | None = None,
    source: str | None = None,
    event_id: int | None = None,
    is_error: bool = False,
    producer: str | None = None,
    meta: str | None = None,
    commit_key: str | None = None,
) -> int:
    """Log a message to the conversations table.

    Returns the inserted row id, or ``0`` on an operational failure.  A keyed
    retry whose immutable payload conflicts with the committed row raises
    :class:`ConversationCommitConflictError` so the durability journal can
    classify the permanent invariant failure without retrying it.

    ``source`` is the persisted twin of the in-memory ``_source``
    side-channel (which producer synthesised the row); ``None`` for the
    common case of an ordinary user/assistant/tool row.

    ``event_id`` is the per-ws SSE ring-buffer high-water mark at save
    time (``SessionUIBase._event_id``); the caller in ``session.py``
    passes ``self.ui._event_id`` so ``/history`` can return it as the
    ``Last-Event-ID`` resume cursor.  ``None`` for offline / bulk saves.

    ``meta`` is pre-serialized role-specific conversation metadata: structured
    operator context on system turns, effect/preview fields plus the acting
    principal on tool turns, sender identity on shared-workstream user turns,
    or the immutable model provenance envelope on accepted assistant turns.
    It is opaque to the backend and decoded only at the row-to-Turn boundary.

    ``commit_key`` is the per-workstream idempotency identity for one admitted
    conversation row. Retrying the same non-NULL key returns the original row
    id without appending a duplicate.
    """
    return _keyed_save(
        lambda: get_storage().save_message(
            ws_id,
            role,
            content,
            tool_name,
            tool_call_id,
            provider_data,
            tool_calls=tool_calls,
            source=source,
            event_id=event_id,
            is_error=is_error,
            producer=producer,
            meta=meta,
            commit_key=commit_key,
        ),
        f"Failed to save message for ws={ws_id} role={role}",
    )


def save_user_message_with_attachments(
    ws_id: str,
    content: str,
    attachments: list[AttachmentWrite] | tuple[AttachmentWrite, ...],
    *,
    source: str | None = None,
    event_id: int | None = None,
    meta: str | None = None,
    commit_key: str,
) -> int:
    """Atomically persist a keyed USER row and its attachment ownership.

    Returns the positively acknowledged row id, or ``0`` on an operational
    failure.  An immutable commit mismatch raises
    :class:`ConversationCommitConflictError`.  The session handoff journal may
    acknowledge the row only when the backend has confirmed the row, blobs,
    exact refcount increments, and ordered ref-list as one transaction.
    Retrying the same immutable commit is safe.
    """
    return _keyed_save(
        lambda: get_storage().save_user_message_with_attachments(
            ws_id,
            content,
            attachments,
            source=source,
            event_id=event_id,
            meta=meta,
            commit_key=commit_key,
        ),
        f"Failed atomic user attachment commit for ws={ws_id} commit_key={commit_key}",
    )


def save_tool_message_with_attachments(
    ws_id: str,
    content: str,
    tool_name: str,
    tool_call_id: str,
    attachments: list[AttachmentWrite] | tuple[AttachmentWrite, ...],
    *,
    event_id: int | None = None,
    is_error: bool = False,
    meta: str | None = None,
    commit_key: str,
) -> int:
    """Atomically persist a keyed TOOL row and its attachment ownership.

    Returns the positively acknowledged row id, or ``0`` on an operational
    failure.  An immutable commit mismatch raises
    :class:`ConversationCommitConflictError`.  This is the storage seam for
    ordinary tool-image rows and cancelled rows whose already-published preview
    blob must survive with the synthesized result.
    """
    return _keyed_save(
        lambda: get_storage().save_tool_message_with_attachments(
            ws_id,
            content,
            tool_name,
            tool_call_id,
            attachments,
            event_id=event_id,
            is_error=is_error,
            meta=meta,
            commit_key=commit_key,
        ),
        f"Failed atomic tool attachment commit for ws={ws_id} commit_key={commit_key}",
    )


def save_messages_bulk(rows: list[dict[str, Any]]) -> bool:
    """Insert multiple conversation rows in a single transaction.

    Returns whether the transaction committed. Most single-row persistence is
    deliberately best-effort, but fork callers need an explicit durability
    result so a missing attachment cannot be reported as a successful copy.
    """
    try:
        get_storage().save_messages_bulk(rows)
        return True
    except Exception:
        log.warning("Failed to bulk-save %d messages", len(rows), exc_info=True)
        return False


def load_messages(ws_id: str, *, repair: bool = True) -> list[dict[str, Any]]:
    """Load messages for a workstream and reconstruct OpenAI message format."""
    try:
        return get_storage().load_messages(ws_id, repair=repair)
    except Exception:
        log.warning("Failed to load messages for ws=%s", ws_id, exc_info=True)
        return []


def load_message_turns(ws_id: str, *, checkpointed: bool = True) -> list[Turn]:
    """Load a workstream's history as canonical ``Turn``s (by-reference content).

    The resume path — see :meth:`StorageBackend.load_message_turns`.  Returns an
    empty list on any storage error (a failed resume must not crash the session).

    ``checkpointed=True`` (resume default) returns the bounded ``[summary]+[tail]``
    view when a compaction marker exists; ``checkpointed=False`` returns the full
    transcript (markers dropped) for export/audit.
    """
    try:
        return get_storage().load_message_turns(ws_id, checkpointed=checkpointed)
    except Exception:
        log.warning("Failed to load message turns for ws=%s", ws_id, exc_info=True)
        return []


def get_compaction_watermark(ws_id: str, preserve_tail: int = 0) -> int | None:
    """Boundary id for a compaction checkpoint marker — see
    :meth:`StorageBackend.get_compaction_watermark`.  Returns ``None`` on any
    storage error (a failed watermark just skips the checkpoint write — the next
    reopen reloads more history, the pre-checkpoint behavior, rather than crash)."""
    try:
        return get_storage().get_compaction_watermark(ws_id, preserve_tail)
    except Exception:
        log.warning("Failed to get compaction watermark for ws=%s", ws_id, exc_info=True)
        return None


# -- Workstream attachments ---------------------------------------------------


def save_attachment(
    attachment_id: str,
    filename: str,
    mime_type: str,
    size_bytes: int,
    kind: str,
    content: bytes,
    origin: str = "upload",
) -> None:
    """Write a content-addressed blob (INSERT-OR-IGNORE) and bump its refcount.

    ``attachment_id`` is the content hash; ``origin`` is ``'upload'`` (user
    attachment) or ``'tool'`` (e.g. a ``read_file`` image).  A blob is only
    ever written referenced (refcount ≥ 1).
    """
    try:
        get_storage().save_attachment(
            attachment_id,
            filename,
            mime_type,
            size_bytes,
            kind,
            content,
            origin,
        )
    except Exception:
        log.warning("Failed to save attachment id=%s", attachment_id, exc_info=True)


def set_message_attachments(ws_id: str, message_id: int, attachment_ids: list[str]) -> None:
    """Record a turn's ordered content-addressed ref-list on its conversations row."""
    if not attachment_ids or not message_id:
        return
    try:
        get_storage().set_message_attachments(ws_id, message_id, attachment_ids)
    except Exception:
        log.warning("Failed to set message attachments ws=%s", ws_id, exc_info=True)


def get_attachments(attachment_ids: list[str]) -> list[dict[str, Any]]:
    """Bulk fetch attachments by id (includes content bytes)."""
    if not attachment_ids:
        return []
    try:
        return get_storage().get_attachments(attachment_ids)
    except Exception:
        log.warning("Failed to fetch attachments", exc_info=True)
        return []


def get_attachment(attachment_id: str) -> dict[str, Any] | None:
    """Return a single attachment row (with content) or None."""
    try:
        return get_storage().get_attachment(attachment_id)
    except Exception:
        log.warning("Failed to fetch attachment id=%s", attachment_id, exc_info=True)
        return None


def attachment_referenced_in_ws(attachment_id: str, ws_id: str) -> bool:
    """True iff some conversations row in ``ws_id`` references ``attachment_id``.

    The committed-attachment ownership gate for ``get_content`` (the per-row
    ws/user scope columns are gone — scope rebases onto referencing-row
    ownership).
    """
    try:
        return get_storage().attachment_referenced_in_ws(attachment_id, ws_id)
    except Exception:
        log.warning("Failed to check attachment reference id=%s", attachment_id, exc_info=True)
        return False


def count_messages(ws_id: str) -> int:
    """Total conversation rows for ``ws_id`` (markers included).

    Returns ``0`` on error — callers that truncate on this count (rewind/retry)
    must treat ``0`` as "unknown, do not delete" rather than "empty", so a
    transient count failure never turns into a wrong deletion.
    """
    try:
        return get_storage().count_messages(ws_id)
    except Exception:
        log.warning("Failed to count messages for ws=%s", ws_id, exc_info=True)
        return 0


def get_compaction_floor(ws_id: str) -> int:
    """Rows backing the latest compaction summary that rewind/retry must keep —
    see :meth:`StorageBackend.get_compaction_floor`.  Returns ``-1`` on error: a
    sentinel distinct from a legitimate ``0`` (never compacted), because a ``0``
    floor on a *compacted* ws would let an over-deep trim delete the summary's
    backing.  Callers that floor a deletion on this (rewind/retry) MUST skip the
    delete when it is negative."""
    try:
        return get_storage().get_compaction_floor(ws_id)
    except Exception:
        log.warning("Failed to get compaction floor for ws=%s", ws_id, exc_info=True)
        return -1


def delete_messages_after(ws_id: str, keep_count: int) -> int:
    """Delete conversation rows beyond the first *keep_count* rows.

    Returns the number of rows deleted, or 0 on error.
    """
    try:
        return get_storage().delete_messages_after(ws_id, keep_count)
    except Exception:
        log.warning(
            "Failed to delete messages after count=%d for ws=%s",
            keep_count,
            ws_id,
            exc_info=True,
        )
        return 0


# -- Workstream management ----------------------------------------------------


def register_workstream(
    ws_id: str,
    node_id: str | None = None,
    name: str = "",
    state: str = "idle",
    skill_id: str = "",
    skill_version: int = 0,
    user_id: str | None = None,
    kind: WorkstreamKind | str = WorkstreamKind.INTERACTIVE,
    parent_ws_id: str | None = None,
    project_id: str | None = None,
    persona: str | None = None,
) -> None:
    """Persist a new workstream (no-op if already exists)."""
    try:
        get_storage().register_workstream(
            ws_id,
            node_id,
            name,
            state,
            user_id=user_id,
            skill_id=skill_id,
            skill_version=skill_version,
            kind=kind,
            parent_ws_id=parent_ws_id,
            project_id=project_id,
            persona=persona,
        )
    except Exception:
        log.warning("Failed to register workstream ws=%s", ws_id, exc_info=True)


def update_workstream_state(ws_id: str, state: str) -> None:
    """Update a workstream's state."""
    try:
        get_storage().update_workstream_state(ws_id, state)
    except Exception:
        log.warning("Failed to update workstream state ws=%s state=%s", ws_id, state, exc_info=True)


def delete_workstream_override(ws_id: str) -> None:
    """Fire-and-forget override deletion."""
    try:
        get_storage().delete_workstream_override(ws_id)
    except Exception:
        log.warning("override delete failed for %s", ws_id[:8], exc_info=True)


def update_workstream_name(ws_id: str, name: str) -> None:
    """Update a workstream's display name."""
    try:
        get_storage().update_workstream_name(ws_id, name)
    except Exception:
        log.warning("Failed to update workstream name ws=%s", ws_id, exc_info=True)


def list_workstreams_with_history(
    limit: int = 20,
    *,
    kind: WorkstreamKind | str | None = None,
    user_id: str | None = None,
    state: str | None = None,
    offset: int = 0,
) -> list[Any]:
    """List workstreams that have conversation messages.

    ``kind`` forwards to the storage layer's SQL-side filter — pass
    ``WorkstreamKind.INTERACTIVE`` from the interactive "saved
    workstreams" endpoint so coordinator rows (which persist
    conversation history too) don't leak into that sidebar.  Default
    ``None`` preserves legacy all-kinds behaviour.

    ``user_id`` enforces tenant scoping at the SQL layer.  Pass the
    authenticated caller's uid from any tenant-visible endpoint;
    leaving it as ``None`` means cluster-wide (service-scoped
    callers only).

    ``state`` filters by lifecycle state — pass ``"closed"`` from the
    coordinator-saved surface so deleted / currently-active rows don't
    end up in the saved cards.  Default ``None`` preserves all-states.
    """
    try:
        return get_storage().list_workstreams_with_history(
            limit,
            kind=kind,
            user_id=user_id,
            state=state,
            offset=offset,
        )
    except Exception:
        log.warning("Failed to list workstreams with history", exc_info=True)
        return []


def delete_workstream(ws_id: str) -> bool:
    """Delete a workstream and all its conversations + config."""
    try:
        return get_storage().delete_workstream(ws_id)
    except Exception:
        log.warning("Failed to delete workstream ws=%s", ws_id, exc_info=True)
        return False


def delete_workstream_if_fork_reserved(ws_id: str, fork_reservation_token: str) -> bool:
    """Delete exactly one uncommitted fork destination incarnation."""
    try:
        return get_storage().delete_workstream_if_fork_reserved(
            ws_id,
            fork_reservation_token,
        )
    except Exception:
        log.warning("Failed to delete reserved fork ws=%s", ws_id, exc_info=True)
        return False


def prune_workstreams(
    retention_days: int = 90,
    log_fn: Callable[[str], None] | None = None,
) -> tuple[int, int]:
    """Prune orphaned and stale workstreams."""
    try:
        orphans, stale = get_storage().prune_workstreams(retention_days)
    except Exception:
        log.warning("Failed to prune workstreams", exc_info=True)
        return (0, 0)

    if log_fn and (orphans or stale):
        parts = []
        if orphans:
            parts.append(f"{orphans} empty workstream{'s' if orphans != 1 else ''}")
        if stale:
            parts.append(
                f"{stale} workstream{'s' if stale != 1 else ''} older than {retention_days} days"
            )
        log_fn(f"[turnstone] Cleanup: removed {', '.join(parts)}.")

    return (orphans, stale)


def resolve_workstream(alias_or_id: str) -> str | None:
    """Resolve an alias or ws_id (or prefix) to a full ws_id."""
    try:
        return get_storage().resolve_workstream(alias_or_id)
    except Exception:
        log.warning("Failed to resolve workstream alias=%s", alias_or_id, exc_info=True)
        return None


# -- Workstream config --------------------------------------------------------


def save_workstream_config(ws_id: str, config: dict[str, str]) -> None:
    """Persist workstream configuration key/value pairs."""
    try:
        get_storage().save_workstream_config(ws_id, config)
    except Exception:
        log.warning("Failed to save workstream config ws=%s", ws_id, exc_info=True)


def load_workstream_config(ws_id: str) -> dict[str, str]:
    """Load workstream configuration."""
    try:
        return get_storage().load_workstream_config(ws_id)
    except Exception:
        log.warning("Failed to load workstream config ws=%s", ws_id, exc_info=True)
        return {}


def finalize_deferred_create(
    ws_id: str,
    fork_reservation_token: str,
    *,
    alias: str | None = None,
    config: dict[str, str] | None = None,
    node_id: str | None = None,
    override_reason: str = "local",
) -> bool:
    """Atomically finalize storage writes for one reserved fork create."""
    return get_storage().finalize_deferred_create(
        ws_id,
        fork_reservation_token,
        alias=alias,
        config=config,
        node_id=node_id,
        override_reason=override_reason,
    )


def publish_deferred_create(ws_id: str, fork_reservation_token: str) -> bool:
    """Atomically expose one exact reserved workstream incarnation."""
    return get_storage().publish_deferred_create(ws_id, fork_reservation_token)


def get_workstream_reservation_token(ws_id: str) -> str:
    """Return the private durable incarnation fence for ``ws_id``."""
    return get_storage().get_workstream_reservation_token(ws_id)


# -- Workstream last_error ---------------------------------------------------
#
# Worker-thread exception text persisted under workstream_config so the
# coordinator's ``inspect_workstream`` and ``wait_for_workstream`` tools
# can surface the actual cause (provider 4xx/5xx after retries, model
# misconfig, MCP outage, etc.) instead of falling back to the
# assistant-tail "(no recent assistant output)" sentinel.

# Single source of truth for the workstream_config key — readers in
# ``turnstone.console.coordinator_client`` import this so a future rename
# can't desync writer and readers.
LAST_ERROR_CONFIG_KEY = "last_error"

# Hard cap on persisted error text. Provider error bodies are sometimes
# multi-KiB JSON blobs (full request echo + headers); without a cap one
# such error per workstream would bloat workstream_config and the model
# prompt the coord LLM ingests on inspect.  1024 chars matches the
# practical "useful for triage" length while staying well under the
# WAIT_MESSAGE_MAX_BYTES (10 KiB) cap so the truncate happens here at
# write time, not later at the wait surface.
LAST_ERROR_MAX_LEN = 1024


def sanitize_error_text(text: str, *, max_len: int = LAST_ERROR_MAX_LEN) -> str:
    """Strip credentials and cap length on a worker-thread fatal-error
    string before it flows into storage / UI broadcasts / the coord
    LLM's prompt.

    Credential redaction delegates to
    :func:`turnstone.core.output_guard.redact_credentials` — the same
    pattern set the audit log + post-tool guard use.  Reusing it keeps
    a single source of truth for "what counts as a secret" instead of
    drifting two parallel regex lists.  Length capping then trims the
    output to ``max_len`` chars (truncation from the START — the lead
    is usually more informative than the tail).

    Sanitisation is best-effort defence-in-depth — pairs with redaction
    at the provider boundary, doesn't replace it.  Operators who care
    deeply should also configure their provider SDKs to redact at log
    time.
    """
    if not text:
        return text
    # Local import — the output_guard module pulls in a moderate set of
    # regex tables we don't want to load at module-import time for
    # every consumer of ``turnstone.core.memory``.  The fatal-error
    # path is cold enough that import-on-first-call is fine.
    from turnstone.core.output_guard import redact_credentials

    cleaned = redact_credentials(text)
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 3] + "..."
    return cleaned


def persist_last_error(ws_id: str, err_msg: str) -> None:
    """Persist (sanitized) exception text so the coordinator's inspect /
    wait_for_workstream can surface it on the next poll.

    Best-effort: storage failures log + swallow.  No-op when ``ws_id``
    or ``err_msg`` are empty.  Sanitization is applied unconditionally —
    no caller currently has a use for the raw text in storage, and a
    bug in a future caller that forgot to sanitize would silently leak
    credentials.
    """
    if not ws_id or not err_msg:
        return
    sanitized = sanitize_error_text(err_msg)
    try:
        get_storage().save_workstream_config(ws_id, {LAST_ERROR_CONFIG_KEY: sanitized})
    except Exception:
        log.warning("Failed to persist last_error ws=%s", ws_id, exc_info=True)


def clear_last_error(ws_id: str) -> None:
    """Clear the persisted ``last_error`` row.

    Called on successful recovery (state transitions from ``error`` back
    to ``running`` or ``idle``) so a once-leaked exception body doesn't
    persist for the workstream lifetime.  Writes an empty string rather
    than deleting the row so the upsert idiom matches every other
    workstream_config writer (``close_reason``, ``tasks``); other keys
    on the row survive.
    """
    if not ws_id:
        return
    try:
        get_storage().save_workstream_config(ws_id, {LAST_ERROR_CONFIG_KEY: ""})
    except Exception:
        log.warning("Failed to clear last_error ws=%s", ws_id, exc_info=True)


def load_last_error(ws_id: str) -> str:
    """Return the persisted ``last_error`` for ``ws_id`` or empty string.

    Storage failures and missing rows both collapse to ``""`` so callers
    can treat empty as "no error to surface".
    """
    if not ws_id:
        return ""
    try:
        cfg = get_storage().load_workstream_config(ws_id) or {}
    except Exception:
        log.warning("Failed to load last_error ws=%s", ws_id, exc_info=True)
        return ""
    raw = cfg.get(LAST_ERROR_CONFIG_KEY)
    return str(raw) if raw else ""


# -- Skills -------------------------------------------------------------------


def get_skill_by_name(name: str) -> dict[str, Any] | None:
    """Lookup skill by name (reads from prompt_templates table)."""
    try:
        return get_storage().get_prompt_template_by_name(name)
    except Exception:
        log.warning("Failed to get skill name=%s", name, exc_info=True)
        return None


def list_default_skills(org_id: str = "") -> list[dict[str, Any]]:
    """Return all skills where is_default=True, ordered by name."""
    try:
        return get_storage().list_default_templates(org_id)
    except Exception:
        log.warning("Failed to list default skills", exc_info=True)
        return []


def list_skills_by_activation(
    activation: str,
    *,
    enabled_only: bool = False,
    limit: int = 0,
) -> list[dict[str, Any]]:
    """Return skills filtered by activation value, ordered by name."""
    try:
        return get_storage().list_skills_by_activation(
            activation, enabled_only=enabled_only, limit=limit
        )
    except Exception:
        log.warning("Failed to list skills by activation=%s", activation, exc_info=True)
        return []


# -- Workstream metadata ------------------------------------------------------


def set_workstream_alias(ws_id: str, alias: str) -> bool:
    """Set a human-friendly alias. Returns False if alias is taken."""
    try:
        return get_storage().set_workstream_alias(ws_id, alias)
    except Exception:
        log.warning("Failed to set alias ws=%s alias=%s", ws_id, alias, exc_info=True)
        return False


def get_workstream_display_name(ws_id: str) -> str | None:
    """Return the alias (or title) for a workstream, or None if unset."""
    try:
        return get_storage().get_workstream_display_name(ws_id)
    except Exception:
        log.warning("Failed to get display name ws=%s", ws_id, exc_info=True)
        return None


def get_workstream_display_names(ws_ids: list[str]) -> dict[str, str | None]:
    """Bulk variant of :func:`get_workstream_display_name`.

    One ``SELECT ... WHERE ws_id IN (...)`` instead of N. Used by the
    lifted ``list`` verb to resolve aliases for every active row in a
    single round-trip. Returns a dict with every requested ws_id —
    missing rows map to ``None``; the caller falls back to ``ws.name``
    per-row. Errors return an empty dict so the caller falls back to
    ``ws.name`` on every row.
    """
    if not ws_ids:
        return {}
    try:
        return get_storage().get_workstream_display_names(ws_ids)
    except Exception:
        log.warning("Failed to get display names count=%d", len(ws_ids), exc_info=True)
        return {}


def get_workstream_metadata(ws_id: str) -> dict[str, Any] | None:
    """Return workstream metadata dict or None if not found."""
    try:
        return get_storage().get_workstream_metadata(ws_id)
    except Exception:
        log.warning("Failed to get workstream metadata ws=%s", ws_id, exc_info=True)
        return None


def get_workstream_owner(ws_id: str) -> str | None:
    """Return the workstream's owner ``user_id`` (or ``""`` when unowned)."""
    try:
        return get_storage().get_workstream_owner(ws_id)
    except Exception:
        log.warning("Failed to get workstream owner ws=%s", ws_id, exc_info=True)
        return None


def get_workstream_row(ws_id: str) -> dict[str, Any] | None:
    """Return the full workstreams row dict, or None when missing/unreadable.

    Same fail-soft shape as :func:`get_workstream_owner` — access gates
    treat ``None`` as not-found, so a storage blip degrades to a 404
    rather than a 500.
    """
    try:
        return get_storage().get_workstream(ws_id)
    except Exception:
        log.warning("Failed to get workstream row ws=%s", ws_id, exc_info=True)
        return None


def update_workstream_title(ws_id: str, title: str) -> None:
    """Set or update the auto-generated title for a workstream."""
    try:
        get_storage().update_workstream_title(ws_id, title)
    except Exception:
        log.warning("Failed to update title ws=%s", ws_id, exc_info=True)


# -- Conversation search -------------------------------------------------------


def search_history(
    query: str,
    limit: int = 20,
    offset: int = 0,
    *,
    user_id: str | None = None,
    exclude_ws_id: str | None = None,
    exclude_after: int | None = None,
) -> list[Any]:
    """Search conversation history.

    ``user_id`` scopes rows by project tenancy (private-project workstreams
    hidden unless creator/owner/member — see
    :meth:`StorageBackend.search_history`); ``None`` = unscoped, for
    single-user lanes only.  ``exclude_ws_id``/``exclude_after`` drop the
    excluded workstream's live segment (rows above its compaction
    checkpoint; the whole workstream when ``exclude_after`` is ``None``) —
    the model-facing recall path passes its own ws so results never
    duplicate what is already in context.
    """
    try:
        return get_storage().search_history(
            query,
            limit,
            offset,
            user_id=user_id,
            exclude_ws_id=exclude_ws_id,
            exclude_after=exclude_after,
        )
    except Exception:
        log.warning("Failed to search history", exc_info=True)
        return []


def get_compaction_checkpoint(ws_id: str) -> int | None:
    """Latest persisted compaction marker's watermark for ``ws_id`` — see
    :meth:`StorageBackend.get_compaction_checkpoint`.  Returns ``None`` on any
    storage error, which callers must read as "the whole workstream is live"
    (recall then excludes it entirely — degraded to less information, never
    to duplicated or leaked rows)."""
    try:
        return get_storage().get_compaction_checkpoint(ws_id)
    except Exception:
        log.warning("Failed to get compaction checkpoint for ws=%s", ws_id, exc_info=True)
        return None


def search_history_recent(limit: int = 20, *, user_id: str | None = None) -> list[Any]:
    """Return most recent conversation messages, tenancy-scoped like
    :func:`search_history`."""
    try:
        return get_storage().search_history_recent(limit, user_id=user_id)
    except Exception:
        log.warning("Failed to search recent history", exc_info=True)
        return []


# -- Structured memories -------------------------------------------------------


def save_structured_memory(
    name: str,
    content: str,
    description: str,
    mem_type: str | None = None,
    scope: str = "global",
    scope_id: str = "",
    *,
    require_active_project: bool = False,
) -> tuple[dict[str, str] | None, bool]:
    """Save a structured memory as a single atomic upsert by name+scope+scope_id.

    Returns ``(row, was_update)`` where ``row`` is the full saved record (or
    ``None`` on failure).  The write is exactly one
    ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING`` statement (see
    :meth:`StorageBackend.upsert_structured_memory`) -- no preceding read, no
    IntegrityError round-trip, no TOCTOU window.  ``(row, was_update)`` comes
    straight from that upsert (this passes a fresh ``memory_id``, so a differing
    returned id means an existing row was updated in place).  A ``None``
    ``description`` is required and must contain non-whitespace text for both
    inserts and updates. A ``None`` ``mem_type`` keeps the stored value on an
    update and uses the column default on insert.
    """
    try:
        return save_structured_memory_strict(
            name,
            content,
            description=description,
            mem_type=mem_type,
            scope=scope,
            scope_id=scope_id,
            require_active_project=require_active_project,
        )
    except Exception:
        log.warning("Failed to save structured memory name=%s", name, exc_info=True)
        return None, False


def save_structured_memory_strict(
    name: str,
    content: str,
    description: str,
    mem_type: str | None = None,
    scope: str = "global",
    scope_id: str = "",
    *,
    require_active_project: bool = False,
) -> tuple[dict[str, str], bool]:
    """Strict structured-memory upsert for mutation-facing boundaries.

    Unlike :func:`save_structured_memory`, storage failures propagate so an
    API or tool cannot report a database outage as an ordinary failed/not-found
    result.  Prompt composition keeps using the best-effort facade.
    """
    import uuid

    normalized = normalize_key(name)
    normalized_description = (description or "").strip()
    if not normalized_description:
        raise ValueError("memory description is required and must be non-empty")
    row, was_update = get_storage().upsert_structured_memory(
        str(uuid.uuid4()),
        normalized,
        normalized_description,
        mem_type,
        scope,
        scope_id,
        content,
        require_active_project=require_active_project,
    )
    if not row:
        raise RuntimeError("structured memory upsert returned no row")
    return row, was_update


def get_structured_memory_by_name(
    name: str, scope: str = "global", scope_id: str = ""
) -> dict[str, str] | None:
    """Retrieve a single structured memory by name+scope. Returns full content."""
    name = normalize_key(name)
    try:
        return get_storage().get_structured_memory_by_name(name, scope, scope_id)
    except Exception:
        log.warning("Failed to get structured memory name=%s", name, exc_info=True)
        return None


def get_structured_memory_by_name_strict(
    name: str, scope: str = "global", scope_id: str = ""
) -> dict[str, str] | None:
    """Strict scoped-name lookup; storage failures propagate."""
    return get_storage().get_structured_memory_by_name(normalize_key(name), scope, scope_id)


def delete_structured_memory(name: str, scope: str = "global", scope_id: str = "") -> bool:
    """Delete a structured memory by name+scope. Returns True if existed."""
    name = normalize_key(name)
    try:
        return get_storage().delete_structured_memory(name, scope, scope_id)
    except Exception:
        log.warning("Failed to delete structured memory name=%s", name, exc_info=True)
        return False


def delete_structured_memory_by_id(memory_id: str) -> bool:
    """Delete a structured memory by its primary key. Returns True if existed."""
    try:
        return get_storage().delete_structured_memory_by_id(memory_id)
    except Exception:
        log.warning("Failed to delete structured memory id=%s", memory_id, exc_info=True)
        return False


def delete_structured_memory_returning_strict(
    name: str, scope: str = "global", scope_id: str = ""
) -> dict[str, str] | None:
    """Atomically delete and return one scoped-name memory.

    Storage failures propagate.  A ``None`` return therefore means only that
    no matching row existed at the mutation point.
    """
    return get_storage().delete_structured_memory_returning(normalize_key(name), scope, scope_id)


def delete_structured_memory_by_id_returning_strict(
    memory_id: str,
) -> dict[str, str] | None:
    """Atomically delete and return one memory by id; failures propagate."""
    return get_storage().delete_structured_memory_by_id_returning(memory_id)


def find_structured_memory_scopes(
    name: str,
    scopes: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Find visible same-name scope pairs in one metadata-only query."""
    try:
        return get_storage().find_structured_memory_scopes(normalize_key(name), scopes)
    except Exception:
        log.warning("Failed to find structured memory scopes name=%s", name, exc_info=True)
        return []


def list_structured_memories(
    mem_type: str = "",
    scope: str = "",
    scope_id: str = "",
    limit: int = 100,
) -> list[dict[str, str]]:
    """List structured memories with optional filters."""
    try:
        return get_storage().list_structured_memories(
            mem_type=mem_type, scope=scope, scope_id=scope_id, limit=limit
        )
    except Exception:
        log.warning("Failed to list structured memories", exc_info=True)
        return []


def search_structured_memories(
    query: str,
    mem_type: str = "",
    scope: str = "",
    scope_id: str = "",
    limit: int = 20,
) -> list[dict[str, str]]:
    """Search structured memories by query."""
    try:
        return get_storage().search_structured_memories(
            query, mem_type=mem_type, scope=scope, scope_id=scope_id, limit=limit
        )
    except Exception:
        log.warning("Failed to search structured memories", exc_info=True)
        return []


def list_visible_structured_memories(
    scopes: list[tuple[str, str]],
    mem_type: str = "",
    limit: int = 100,
) -> list[dict[str, str]]:
    """Single-query union across visible (scope, scope_id) pairs."""
    try:
        return get_storage().list_visible_structured_memories(
            scopes, mem_type=mem_type, limit=limit
        )
    except Exception:
        log.warning("Failed to list visible structured memories", exc_info=True)
        return []


def search_visible_structured_memories(
    query: str,
    scopes: list[tuple[str, str]],
    mem_type: str = "",
    limit: int = 20,
) -> list[dict[str, str]]:
    """OR-of-terms search joined with a single visibility OR-group."""
    try:
        return get_storage().search_visible_structured_memories(
            query, scopes, mem_type=mem_type, limit=limit
        )
    except Exception:
        log.warning("Failed to search visible structured memories", exc_info=True)
        return []


def touch_structured_memories(keys: list[tuple[str, str, str]]) -> int:
    """Batch-touch memories (bump last_accessed, increment access_count).

    Each key is ``(name, scope, scope_id)``.  Duplicates are removed so each
    distinct memory is touched at most once.  Returns count of rows updated.
    """
    if not keys:
        return 0
    seen: set[tuple[str, str, str]] = set()
    unique: list[tuple[str, str, str]] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    try:
        return get_storage().touch_structured_memories(unique)
    except Exception:
        log.warning("Failed to touch structured memories", exc_info=True)
        return 0


def count_structured_memories(mem_type: str = "", scope: str = "", scope_id: str = "") -> int:
    """Count structured memories with optional type/scope filter."""
    try:
        return get_storage().count_structured_memories(
            mem_type=mem_type, scope=scope, scope_id=scope_id
        )
    except Exception:
        log.warning("Failed to count structured memories", exc_info=True)
        return 0
