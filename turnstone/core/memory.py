"""Persistence facade — delegates to the pluggable storage backend.

All functions maintain their existing signatures for consumers (session.py,
server.py, cli.py). The actual storage implementation lives in
``turnstone.core.storage``.

The no-raise contract is preserved — callers never see exceptions from this
module.  All failures are logged so storage issues are visible in logs
rather than silently swallowed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from turnstone.core.log import get_logger
from turnstone.core.storage import get_storage
from turnstone.core.workstream import WorkstreamKind

if TYPE_CHECKING:
    from collections.abc import Callable

log = get_logger(__name__)


def normalize_key(key: str) -> str:
    """Normalize a memory key for consistent lookup."""
    return key.lower().replace("-", "_").replace(" ", "_")


# -- Core conversation operations ---------------------------------------------


def save_message(
    ws_id: str,
    role: str,
    content: str | None,
    tool_name: str | None = None,
    tool_call_id: str | None = None,
    provider_data: str | None = None,
    tool_calls: str | None = None,
) -> int:
    """Log a message to the conversations table.

    Returns the inserted row id, or ``0`` on failure (preserving the
    module's no-raise contract).
    """
    try:
        return get_storage().save_message(
            ws_id,
            role,
            content,
            tool_name,
            tool_call_id,
            provider_data,
            tool_calls=tool_calls,
        )
    except Exception:
        log.warning("Failed to save message for ws=%s role=%s", ws_id, role, exc_info=True)
        return 0


def save_messages_bulk(rows: list[dict[str, Any]]) -> None:
    """Insert multiple conversation rows in a single transaction."""
    try:
        get_storage().save_messages_bulk(rows)
    except Exception:
        log.warning("Failed to bulk-save %d messages", len(rows), exc_info=True)


def load_messages(ws_id: str) -> list[dict[str, Any]]:
    """Load messages for a workstream and reconstruct OpenAI message format."""
    try:
        return get_storage().load_messages(ws_id)
    except Exception:
        log.warning("Failed to load messages for ws=%s", ws_id, exc_info=True)
        return []


# -- Workstream attachments ---------------------------------------------------


def save_attachment(
    attachment_id: str,
    ws_id: str,
    user_id: str,
    filename: str,
    mime_type: str,
    size_bytes: int,
    kind: str,
    content: bytes,
) -> None:
    """Persist an uploaded attachment in pending state."""
    try:
        get_storage().save_attachment(
            attachment_id,
            ws_id,
            user_id,
            filename,
            mime_type,
            size_bytes,
            kind,
            content,
        )
    except Exception:
        log.warning("Failed to save attachment ws=%s", ws_id, exc_info=True)


def list_pending_attachments(ws_id: str, user_id: str) -> list[dict[str, Any]]:
    """List un-consumed attachments for ``(ws_id, user_id)``."""
    try:
        return get_storage().list_pending_attachments(ws_id, user_id)
    except Exception:
        log.warning("Failed to list pending attachments ws=%s", ws_id, exc_info=True)
        return []


def get_attachments(attachment_ids: list[str]) -> list[dict[str, Any]]:
    """Bulk fetch attachments by id (includes content bytes)."""
    if not attachment_ids:
        return []
    try:
        return get_storage().get_attachments(attachment_ids)
    except Exception:
        log.warning("Failed to fetch attachments", exc_info=True)
        return []


def get_pending_attachments_with_content(ws_id: str, user_id: str) -> list[dict[str, Any]]:
    """Single-query fetch of pending attachments + their bytes for the
    auto-consume path on send.  Never expose this to user-facing listing
    endpoints — use ``list_pending_attachments`` there instead.
    """
    try:
        return get_storage().get_pending_attachments_with_content(ws_id, user_id)
    except Exception:
        log.warning(
            "Failed to fetch pending attachments with content ws=%s",
            ws_id,
            exc_info=True,
        )
        return []


def get_attachment(attachment_id: str) -> dict[str, Any] | None:
    """Return a single attachment row (with content) or None."""
    try:
        return get_storage().get_attachment(attachment_id)
    except Exception:
        log.warning("Failed to fetch attachment id=%s", attachment_id, exc_info=True)
        return None


def delete_attachment(attachment_id: str, ws_id: str, user_id: str) -> bool:
    """Delete a pending attachment. Returns True if deleted."""
    try:
        return get_storage().delete_attachment(attachment_id, ws_id, user_id)
    except Exception:
        log.warning("Failed to delete attachment id=%s", attachment_id, exc_info=True)
        return False


def mark_attachments_consumed(
    attachment_ids: list[str],
    message_id: int,
    ws_id: str,
    user_id: str,
    reserved_for_msg_id: str | None = None,
) -> None:
    """Link attachments to a saved user message (scoped to ws_id+user_id).

    When ``reserved_for_msg_id`` is set, the UPDATE also requires the
    attachment's reservation token to match — prevents a stale send from
    consuming rows reserved for a different one.
    """
    if not attachment_ids:
        return
    try:
        get_storage().mark_attachments_consumed(
            attachment_ids,
            message_id,
            ws_id,
            user_id,
            reserved_for_msg_id=reserved_for_msg_id,
        )
    except Exception:
        log.warning("Failed to mark attachments consumed", exc_info=True)


def reserve_attachments(
    attachment_ids: list[str],
    queue_msg_id: str,
    ws_id: str,
    user_id: str,
) -> list[str]:
    """Soft-lock pending attachments to a queued user message.

    Returns the list of ids that were actually reserved for ``queue_msg_id``
    (others silently skipped — e.g. already consumed or reserved).
    """
    if not attachment_ids or not queue_msg_id:
        return []
    try:
        return get_storage().reserve_attachments(attachment_ids, queue_msg_id, ws_id, user_id)
    except Exception:
        log.warning("Failed to reserve attachments", exc_info=True)
        return []


def unreserve_attachments(queue_msg_id: str, ws_id: str, user_id: str) -> None:
    """Release the reservation held by ``queue_msg_id`` on this (ws, user)."""
    if not queue_msg_id:
        return
    try:
        get_storage().unreserve_attachments(queue_msg_id, ws_id, user_id)
    except Exception:
        log.warning("Failed to unreserve attachments", exc_info=True)


def sweep_orphan_reservations(older_than_seconds: int) -> int:
    """Clear ``reserved_for_msg_id`` on stale attachment rows.

    Defensive cleanup for reservations leaked by process crashes between
    ``reserve_attachments`` and ``mark_attachments_consumed`` /
    ``unreserve_attachments``.  Returns count of rows swept.
    """
    if older_than_seconds <= 0:
        return 0
    try:
        return get_storage().sweep_orphan_reservations(older_than_seconds)
    except Exception:
        log.warning("Failed to sweep orphan reservations", exc_info=True)
        return 0


def load_attachments_for_messages(ws_id: str) -> dict[int, list[dict[str, Any]]]:
    """Return attachments grouped by ``message_id`` for history replay."""
    try:
        return get_storage().load_attachments_for_messages(ws_id)
    except Exception:
        log.warning("Failed to load attachments for ws=%s", ws_id, exc_info=True)
        return {}


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


def update_workstream_title(ws_id: str, title: str) -> None:
    """Set or update the auto-generated title for a workstream."""
    try:
        get_storage().update_workstream_title(ws_id, title)
    except Exception:
        log.warning("Failed to update title ws=%s", ws_id, exc_info=True)


# -- Conversation search -------------------------------------------------------


def search_history(query: str, limit: int = 20, offset: int = 0) -> list[Any]:
    """Search conversation history."""
    try:
        return get_storage().search_history(query, limit, offset)
    except Exception:
        log.warning("Failed to search history", exc_info=True)
        return []


def search_history_recent(limit: int = 20) -> list[Any]:
    """Return most recent conversation messages."""
    try:
        return get_storage().search_history_recent(limit)
    except Exception:
        log.warning("Failed to search recent history", exc_info=True)
        return []


# -- Structured memories -------------------------------------------------------


def save_structured_memory(
    name: str,
    content: str,
    description: str = "",
    mem_type: str = "project",
    scope: str = "global",
    scope_id: str = "",
) -> tuple[str, str | None]:
    """Save a structured memory (upsert by name+scope+scope_id).

    Returns (memory_id, old_content_or_None).  Uses create-first to
    avoid TOCTOU races under concurrent access.
    """
    import uuid

    name = normalize_key(name)
    try:
        storage = get_storage()
        # Try create first — if it hits the unique constraint, fall back to update
        memory_id = str(uuid.uuid4())
        try:
            storage.create_structured_memory(
                memory_id, name, description, mem_type, scope, scope_id, content
            )
            return memory_id, None
        except sa.exc.IntegrityError:
            # Unique constraint violation — row already exists, update it
            existing = storage.get_structured_memory_by_name(name, scope, scope_id)
            if existing:
                old_content = existing["content"]
                updates: dict[str, str] = {"content": content}
                if description:
                    updates["description"] = description
                if mem_type != "project":
                    updates["type"] = mem_type
                storage.update_structured_memory(existing["memory_id"], **updates)
                return existing["memory_id"], old_content
            return "", None
    except Exception:
        log.warning("Failed to save structured memory name=%s", name, exc_info=True)
        return "", None


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
