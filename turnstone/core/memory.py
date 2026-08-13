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

import re
import unicodedata
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
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
    from contextlib import AbstractContextManager

    from turnstone.core.trajectory import Turn

log = get_logger(__name__)


MEMORY_NAME_PATTERN = r"[a-z0-9]+(?:_[a-z0-9]+)*"
_MEMORY_NAME_RE = re.compile(rf"\A{MEMORY_NAME_PATTERN}\Z")
_LATIN_FOLD_OVERRIDES = {
    "æ": "ae",
    "đ": "d",
    "ð": "d",
    "ħ": "h",
    "ı": "i",
    "ł": "l",
    "ŋ": "n",
    "œ": "oe",
    "ø": "o",
    "þ": "th",
    "ŧ": "t",
}


def normalize_memory_name(name: object) -> str:
    """Canonicalize one public memory name to an ASCII snake-case key.

    Latin letters are case-folded and stripped of supported diacritics.
    Spaces and Unicode hyphens become separators; underscores remain literal
    so leading, trailing, or repeated underscores are rejected rather than
    silently repaired. Unsupported scripts and punctuation fail closed.
    """
    if not isinstance(name, str):
        raise ValueError("memory name is required")

    # Trim only space separators. Tabs/newlines and other controls are invalid
    # name content, even at an edge.
    start = 0
    end = len(name)
    while start < end and unicodedata.category(name[start]) == "Zs":
        start += 1
    while end > start and unicodedata.category(name[end - 1]) == "Zs":
        end -= 1
    raw = name[start:end]
    if not raw:
        raise ValueError("memory name is required")

    output: list[str] = []
    in_separator_run = False
    for char in raw.casefold():
        category = unicodedata.category(char)
        if char.isascii() and char.isalnum():
            output.append(char)
            in_separator_run = False
            continue
        if char == "_":
            output.append(char)
            in_separator_run = False
            continue
        if category in {"Zs", "Pd"}:
            if not in_separator_run:
                output.append("_")
                in_separator_run = True
            continue
        replacement = _LATIN_FOLD_OVERRIDES.get(char)
        if replacement is not None:
            output.append(replacement)
            in_separator_run = False
            continue
        if category.startswith("M") and output and output[-1][-1:].isalpha():
            # Decomposed Latin diacritic attached to the preceding base.
            continue
        if category.startswith("L") and "LATIN" in unicodedata.name(char, ""):
            decomposed = unicodedata.normalize("NFKD", char)
            folded = "".join(part for part in decomposed if part.isascii() and part.isalpha())
            if folded:
                output.append(folded)
                in_separator_run = False
                continue
        if category.startswith("L"):
            raise ValueError(
                "memory name contains unsupported non-Latin characters; "
                "choose an ASCII semantic key and keep native-language wording "
                "in the description or content"
            )
        raise ValueError(
            "memory name may contain only Latin letters, ASCII digits, spaces, "
            "hyphens, and single underscores"
        )

    normalized = "".join(output)
    if len(normalized) > 256:
        raise ValueError("memory name exceeds 256 characters after normalization")
    if not _MEMORY_NAME_RE.fullmatch(normalized):
        raise ValueError(
            "memory name must normalize to ASCII snake_case without leading, "
            "trailing, or repeated underscores"
        )
    return normalized


def normalize_key(key: str) -> str:
    """Backward-compatible alias for the authoritative memory-name boundary."""
    return normalize_memory_name(key)


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


def _require_memory_description(description: str) -> str:
    """Return a normalized description or raise the public validation error."""
    from turnstone.core.memory_index import normalize_memory_description

    return normalize_memory_description(description)


def save_structured_memory(
    name: str,
    content: str,
    description: str,
    mem_type: str | None = None,
    scope: str = "global",
    scope_id: str = "",
    *,
    require_active_project: bool = False,
    acting_principal_id: str = "",
) -> tuple[dict[str, str] | None, bool]:
    """Save a structured memory as a single atomic upsert by name+scope+scope_id.

    Returns ``(row, was_update)`` where ``row`` is the full saved record (or
    ``None`` on failure).  The write is exactly one
    ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING`` statement (see
    :meth:`StorageBackend.upsert_structured_memory`) -- no preceding read, no
    IntegrityError round-trip, no TOCTOU window.  ``(row, was_update)`` comes
    straight from that upsert (this passes a fresh ``memory_id``, so a differing
    returned id means an existing row was updated in place). ``description``
    is required and must contain non-whitespace text for both inserts and
    updates. A ``None`` ``mem_type`` keeps the stored value on an update and
    uses the column default on insert.
    """
    # Validate outside the best-effort storage boundary. Backend/driver
    # ``ValueError`` instances remain operational failures; only this explicit
    # caller-input check propagates.
    normalized_description = _require_memory_description(description)
    normalized_name = normalize_memory_name(name)
    try:
        return save_structured_memory_strict(
            normalized_name,
            content,
            description=normalized_description,
            mem_type=mem_type,
            scope=scope,
            scope_id=scope_id,
            require_active_project=require_active_project,
            acting_principal_id=acting_principal_id,
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
    acting_principal_id: str = "",
) -> tuple[dict[str, str], bool]:
    """Strict structured-memory upsert for mutation-facing boundaries.

    Unlike :func:`save_structured_memory`, storage failures propagate so an
    API or tool cannot report a database outage as an ordinary failed/not-found
    result. Best-effort internal callers keep using the facade.
    """
    import uuid

    normalized = normalize_key(name)
    normalized_description = _require_memory_description(description)
    row, was_update = get_storage().upsert_structured_memory(
        str(uuid.uuid4()),
        normalized,
        normalized_description,
        mem_type,
        scope,
        scope_id,
        content,
        require_active_project=require_active_project,
        acting_principal_id=acting_principal_id,
    )
    if not row:
        raise RuntimeError("structured memory upsert returned no row")
    return row, was_update


def get_structured_memory_by_name(
    name: str,
    scope: str = "global",
    scope_id: str = "",
) -> dict[str, str] | None:
    """Retrieve a single structured memory by name+scope. Returns full content."""
    name = normalize_key(name)
    try:
        return get_storage().get_structured_memory_by_name(
            name,
            scope,
            scope_id,
        )
    except Exception:
        log.warning("Failed to get structured memory name=%s", name, exc_info=True)
        return None


def get_structured_memory_by_name_strict(
    name: str,
    scope: str = "global",
    scope_id: str = "",
) -> dict[str, str] | None:
    """Strict scoped-name lookup; storage failures propagate."""
    return get_storage().get_structured_memory_by_name(
        normalize_key(name),
        scope,
        scope_id,
    )


def get_and_touch_structured_memory_by_name_strict(
    name: str,
    scope: str = "global",
    scope_id: str = "",
    *,
    acting_principal_id: str = "",
) -> dict[str, str] | None:
    """Atomically fetch one full body and record exactly that row's access."""
    return get_storage().get_and_touch_structured_memory_by_name(
        normalize_key(name),
        scope,
        scope_id,
        acting_principal_id=acting_principal_id,
    )


def delete_structured_memory(
    name: str,
    scope: str = "global",
    scope_id: str = "",
) -> bool:
    """Delete a structured memory by name+scope. Returns True if existed."""
    name = normalize_key(name)
    try:
        return get_storage().delete_structured_memory(
            name,
            scope,
            scope_id,
        )
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
    name: str,
    scope: str = "global",
    scope_id: str = "",
    *,
    acting_principal_id: str = "",
) -> dict[str, str] | None:
    """Atomically delete and return one scoped-name memory.

    Storage failures propagate.  A ``None`` return therefore means only that
    no matching row existed at the mutation point.
    """
    return get_storage().delete_structured_memory_returning(
        normalize_key(name),
        scope,
        scope_id,
        acting_principal_id=acting_principal_id,
    )


def delete_structured_memory_by_id_returning_strict(
    memory_id: str,
) -> dict[str, str] | None:
    """Atomically delete and return one memory by id; failures propagate."""
    return get_storage().delete_structured_memory_by_id_returning(memory_id)


def find_structured_memory_scopes(
    name: str,
    scopes: list[tuple[str, str]],
    *,
    acting_principal_id: str = "",
) -> list[tuple[str, str]]:
    """Find visible same-name scope pairs in one metadata-only query."""
    try:
        return get_storage().find_structured_memory_scopes(
            normalize_key(name),
            scopes,
            acting_principal_id=acting_principal_id,
        )
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
            mem_type=mem_type,
            scope=scope,
            scope_id=scope_id,
            limit=limit,
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
            query,
            mem_type=mem_type,
            scope=scope,
            scope_id=scope_id,
            limit=limit,
        )
    except Exception:
        log.warning("Failed to search structured memories", exc_info=True)
        return []


def list_visible_structured_memories(
    scopes: list[tuple[str, str]],
    mem_type: str = "",
    limit: int = 100,
    *,
    acting_principal_id: str = "",
) -> list[dict[str, str]]:
    """Single-query union across visible (scope, scope_id) pairs."""
    try:
        return get_storage().list_visible_structured_memories(
            scopes,
            mem_type=mem_type,
            limit=limit,
            acting_principal_id=acting_principal_id,
        )
    except Exception:
        log.warning("Failed to list visible structured memories", exc_info=True)
        return []


def search_visible_structured_memories(
    query: str,
    scopes: list[tuple[str, str]],
    mem_type: str = "",
    limit: int = 20,
    *,
    acting_principal_id: str = "",
) -> list[dict[str, str]]:
    """OR-of-terms search joined with a single visibility OR-group."""
    try:
        return get_storage().search_visible_structured_memories(
            query,
            scopes,
            mem_type=mem_type,
            limit=limit,
            acting_principal_id=acting_principal_id,
        )
    except Exception:
        log.warning("Failed to search visible structured memories", exc_info=True)
        return []


def update_structured_memory_description_strict(
    memory_id: str,
    description: str,
    *,
    storage: Any | None = None,
) -> dict[str, str] | None:
    """Update one authored index hook; validation and storage failures propagate."""
    backend = storage or get_storage()
    return backend.update_structured_memory_description(
        memory_id,
        _require_memory_description(description),
    )


def acquire_memory_index_snapshot(
    ws_id: str,
    principal_id: str,
    *,
    commit_guard: Callable[[], AbstractContextManager[None]] | None = None,
) -> dict[str, Any]:
    """Atomically bind or load one workstream's immutable memory index.

    This boundary is deliberately strict. A storage failure must stop model
    admission rather than publish an empty block falsely described as complete.
    The backend resolves the live visibility envelope inside the same database
    transaction as its metadata read and first-writer insert.
    """
    storage = get_storage()
    snapshot = storage.acquire_memory_index_snapshot(
        ws_id,
        principal_id,
        commit_guard=commit_guard,
    )
    if snapshot is None:
        raise RuntimeError("memory index workstream is no longer active")
    return snapshot


def prospective_memory_index(
    scopes: list[tuple[str, str]],
    *,
    acting_principal_id: str = "",
) -> dict[str, int]:
    """Render the live metadata envelope for soft-cap/backpressure reporting."""
    from turnstone.core.memory_index import render_memory_index

    project_ids = sorted({scope_id for scope, scope_id in scopes if scope == "project"})
    if len(project_ids) > 1:
        raise ValueError("a memory index envelope may contain at most one project")
    rendered = render_memory_index(
        get_storage().list_visible_memory_index_entries(
            scopes,
            acting_principal_id=acting_principal_id,
        ),
        project_id=project_ids[0] if project_ids else "",
    )
    return {
        "entry_count": rendered.entry_count,
        "char_count": rendered.char_count,
        "invalid_description_count": rendered.invalid_description_count,
    }


@dataclass(frozen=True)
class _IndexBucket:
    entry_count: int = 0
    line_chars: int = 0

    def __add__(self, other: _IndexBucket) -> _IndexBucket:
        return _IndexBucket(
            self.entry_count + other.entry_count,
            self.line_chars + other.line_chars,
        )


class _PrincipalMetricSet:
    """Range-max index for exact envelope maxima over many principals."""

    def __init__(self, buckets: list[_IndexBucket]) -> None:
        # Anonymous/no-principal is a real interactive envelope and also makes
        # the empty-set behavior total for coordinator/project subsets.
        best_by_count: dict[int, int] = {0: 0}
        for bucket in buckets:
            best_by_count[bucket.entry_count] = max(
                best_by_count.get(bucket.entry_count, 0),
                bucket.line_chars,
            )
        self.counts = sorted(best_by_count)
        values = [best_by_count[count] for count in self.counts]
        size = 1
        while size < len(values):
            size *= 2
        self._size = size
        self._tree = [-1] * (2 * size)
        self._tree[size : size + len(values)] = values
        for index in range(size - 1, 0, -1):
            self._tree[index] = max(self._tree[2 * index], self._tree[2 * index + 1])

    @property
    def max_entries(self) -> int:
        return self.counts[-1]

    def _range_max(self, start: int, stop: int) -> int:
        result = -1
        left = start + self._size
        right = stop + self._size
        while left < right:
            if left & 1:
                result = max(result, self._tree[left])
                left += 1
            if right & 1:
                right -= 1
                result = max(result, self._tree[right])
            left //= 2
            right //= 2
        return result

    def max_rendered_chars(
        self,
        base: _IndexBucket,
        *,
        project_id: str = "",
    ) -> int:
        """Return the exact maximum without scanning every principal."""
        from turnstone.core.memory_index import memory_index_base_char_count

        maximum = 0
        max_total = base.entry_count + self.max_entries
        for digits in range(1, len(str(max_total)) + 1):
            # Zero has one decimal digit too.  Including it is material for an
            # empty project envelope because the project_id attribute still
            # contributes characters even when there are no entry lines.
            low = max(0, (0 if digits == 1 else 10 ** (digits - 1)) - base.entry_count)
            high = 10**digits - 1 - base.entry_count
            start = bisect_left(self.counts, low)
            stop = bisect_right(self.counts, high)
            if start == stop:
                continue
            line_chars = self._range_max(start, stop)
            sample_count = self.counts[start]
            maximum = max(
                maximum,
                base.line_chars
                + line_chars
                + memory_index_base_char_count(
                    base.entry_count + sample_count,
                    project_id=project_id,
                ),
            )
        return maximum


def memory_index_health(*, budget_chars: int, storage: Any | None = None) -> dict[str, Any]:
    """Return derived health over possible envelopes in the live topology.

    Memory rows are rendered to per-scope metrics once. Workstreams then add
    those buckets, so the calculation is linear in memories plus topology and
    does not depend on whether an old snapshot still happens to exist.
    """
    from turnstone.core.memory_index import (
        memory_index_base_char_count,
        memory_index_entry_metrics,
    )
    from turnstone.core.project_access import decide_project_access, fold_role_permissions

    backend = storage or get_storage()
    inputs = backend.get_memory_index_health_inputs()
    buckets: dict[tuple[str, str], _IndexBucket] = {}
    invalid_total = 0
    principal_ids = {str(row.get("user_id") or "") for row in inputs["users"] if row.get("user_id")}
    for row in inputs["entries"]:
        scope = str(row.get("scope") or "")
        scope_id = str(row.get("scope_id") or "")
        chars, invalid = memory_index_entry_metrics(row)
        current = buckets.get((scope, scope_id), _IndexBucket())
        buckets[(scope, scope_id)] = _IndexBucket(
            current.entry_count + 1,
            current.line_chars + chars,
        )
        invalid_total += invalid
        if scope in {"user", "coordinator"} and scope_id:
            principal_ids.add(scope_id)

    projects = {
        str(row.get("project_id") or ""): row for row in inputs["projects"] if row.get("project_id")
    }
    project_members: dict[str, set[str]] = {}
    for row in inputs["members"]:
        project_id = str(row.get("project_id") or "")
        user_id = str(row.get("user_id") or "")
        if project_id and user_id:
            project_members.setdefault(project_id, set()).add(user_id)
            principal_ids.add(user_id)
    for row in projects.values():
        owner_id = str(row.get("owner_id") or "")
        if owner_id:
            principal_ids.add(owner_id)
    for row in inputs["workstreams"]:
        owner_id = str(row.get("user_id") or "")
        if owner_id:
            principal_ids.add(owner_id)

    ordered_principals = sorted(principal_ids)

    overrides: dict[str, tuple[set[str], set[str]]] = {}
    for row in inputs["role_overrides"]:
        role_id = str(row.get("role_id") or "")
        permission = str(row.get("permission") or "")
        action = str(row.get("action") or "")
        grants, revokes = overrides.setdefault(role_id, (set(), set()))
        if action == "grant":
            grants.add(permission)
        elif action == "revoke":
            revokes.add(permission)
    role_permissions: dict[str, set[str]] = {}
    for row in inputs["roles"]:
        role_id = str(row.get("role_id") or "")
        grants, revokes = overrides.get(role_id, (set(), set()))
        if not row.get("builtin"):
            grants, revokes = set(), set()
        role_permissions[role_id] = fold_role_permissions(
            str(row.get("permissions") or ""),
            grants=grants,
            revokes=revokes,
        )
    permissions_by_principal: dict[str, set[str]] = {}
    for row in inputs["user_roles"]:
        user_id = str(row.get("user_id") or "")
        role_id = str(row.get("role_id") or "")
        if user_id:
            permissions_by_principal.setdefault(user_id, set()).update(
                role_permissions.get(role_id, set())
            )

    def _principal_metrics(scope: str, ids: set[str] | None = None) -> _PrincipalMetricSet:
        selected = ordered_principals if ids is None else sorted(ids & principal_ids)
        return _PrincipalMetricSet(
            [buckets.get((scope, user_id), _IndexBucket()) for user_id in selected]
        )

    all_users = _principal_metrics("user")
    all_coordinators = _principal_metrics("coordinator")
    project_user_metrics: dict[tuple[str, str], _PrincipalMetricSet] = {}

    def _project_principals(project_id: str) -> set[str]:
        project = projects[project_id]
        members = project_members.get(project_id, set())
        return {
            principal_id
            for principal_id in principal_ids
            if decide_project_access(
                principal_id=principal_id,
                owner_id=str(project.get("owner_id") or ""),
                visibility=str(project.get("visibility") or "private"),
                state=str(project.get("state") or "active"),
                is_member=principal_id in members,
                permissions=permissions_by_principal.get(principal_id, set()),
            ).can_read
        }

    max_chars = memory_index_base_char_count(0)
    max_entries = 0
    envelope_count = 1  # Global-only remains meaningful with no snapshots/workstreams.
    global_bucket = buckets.get(("global", ""), _IndexBucket())
    max_chars = global_bucket.line_chars + memory_index_base_char_count(global_bucket.entry_count)
    max_entries = global_bucket.entry_count

    def _consider(base: _IndexBucket, metrics: _PrincipalMetricSet, project_id: str = "") -> None:
        nonlocal max_chars, max_entries
        max_chars = max(
            max_chars,
            metrics.max_rendered_chars(base, project_id=project_id),
        )
        max_entries = max(max_entries, base.entry_count + metrics.max_entries)

    for workstream in inputs["workstreams"]:
        ws_id = str(workstream.get("ws_id") or "")
        kind = str(workstream.get("kind") or WorkstreamKind.INTERACTIVE.value)
        attached_project = str(workstream.get("project_id") or "")
        live_project = attached_project if attached_project in projects else ""
        if kind == WorkstreamKind.COORDINATOR.value:
            _consider(_IndexBucket(), all_coordinators)
            envelope_count += len(principal_ids)
            if live_project:
                allowed = _project_principals(live_project)
                if allowed:
                    key = ("coordinator", live_project)
                    metrics = project_user_metrics.setdefault(
                        key,
                        _principal_metrics("coordinator", allowed),
                    )
                    _consider(
                        buckets.get(("project", live_project), _IndexBucket()),
                        metrics,
                        live_project,
                    )
            continue

        base = global_bucket + buckets.get(("workstream", ws_id), _IndexBucket())
        _consider(base, all_users)
        # One anonymous envelope plus one exact user scope per known principal.
        envelope_count += len(principal_ids) + 1
        if live_project:
            allowed = _project_principals(live_project)
            if allowed:
                key = ("user", live_project)
                metrics = project_user_metrics.setdefault(
                    key,
                    _principal_metrics("user", allowed),
                )
                _consider(
                    base + buckets.get(("project", live_project), _IndexBucket()),
                    metrics,
                    live_project,
                )

    return {
        "budget_chars": budget_chars,
        "over_budget": max_chars > budget_chars,
        "max_char_count": max_chars,
        "max_entry_count": max_entries,
        "over_by_chars": max(0, max_chars - budget_chars),
        "invalid_description_count": invalid_total,
        "envelope_count": envelope_count,
    }


def count_structured_memories(
    mem_type: str = "",
    scope: str = "",
    scope_id: str = "",
    *,
    acting_principal_id: str = "",
) -> int:
    """Count structured memories with optional type/scope filter."""
    try:
        return get_storage().count_structured_memories(
            mem_type=mem_type,
            scope=scope,
            scope_id=scope_id,
            acting_principal_id=acting_principal_id,
        )
    except Exception:
        log.warning("Failed to count structured memories", exc_info=True)
        return 0
