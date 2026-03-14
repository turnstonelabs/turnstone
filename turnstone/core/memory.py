"""Persistence facade — delegates to the pluggable storage backend.

All functions maintain their existing signatures for consumers (session.py,
server.py, cli.py). The actual storage implementation lives in
``turnstone.core.storage``.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from turnstone.core.storage import get_storage

if TYPE_CHECKING:
    from collections.abc import Callable


def normalize_key(key: str) -> str:
    """Normalize a memory key for consistent lookup."""
    return key.lower().replace("-", "_").replace(" ", "_")


# -- Core conversation operations ---------------------------------------------


def save_message(
    ws_id: str,
    role: str,
    content: str | None,
    tool_name: str | None = None,
    tool_args: str | None = None,
    tool_call_id: str | None = None,
    provider_data: str | None = None,
    tool_calls: str | None = None,
) -> None:
    """Log a message to the conversations table."""
    with contextlib.suppress(Exception):
        get_storage().save_message(
            ws_id,
            role,
            content,
            tool_name,
            tool_args,
            tool_call_id,
            provider_data,
            tool_calls=tool_calls,
        )


def load_messages(ws_id: str) -> list[dict[str, Any]]:
    """Load messages for a workstream and reconstruct OpenAI message format."""
    try:
        return get_storage().load_messages(ws_id)
    except Exception:
        return []


# -- Workstream management ----------------------------------------------------


def register_workstream(
    ws_id: str, node_id: str | None = None, name: str = "", state: str = "idle"
) -> None:
    """Persist a new workstream (no-op if already exists)."""
    with contextlib.suppress(Exception):
        get_storage().register_workstream(ws_id, node_id, name, state)


def update_workstream_state(ws_id: str, state: str) -> None:
    """Update a workstream's state."""
    with contextlib.suppress(Exception):
        get_storage().update_workstream_state(ws_id, state)


def update_workstream_name(ws_id: str, name: str) -> None:
    """Update a workstream's display name."""
    with contextlib.suppress(Exception):
        get_storage().update_workstream_name(ws_id, name)


def update_workstream_template(ws_id: str, ws_template_id: str, ws_template_version: int) -> None:
    """Set ws_template_id and ws_template_version on the workstreams row."""
    with contextlib.suppress(Exception):
        get_storage().update_workstream_template(ws_id, ws_template_id, ws_template_version)


def list_workstreams(node_id: str | None = None, limit: int = 100) -> list[Any]:
    """List workstreams, optionally filtered by node_id."""
    try:
        return get_storage().list_workstreams(node_id, limit)
    except Exception:
        return []


def list_workstreams_with_history(limit: int = 20) -> list[Any]:
    """List workstreams that have conversation messages."""
    try:
        return get_storage().list_workstreams_with_history(limit)
    except Exception:
        return []


def delete_workstream(ws_id: str) -> bool:
    """Delete a workstream and all its conversations + config."""
    try:
        return get_storage().delete_workstream(ws_id)
    except Exception:
        return False


def prune_workstreams(
    retention_days: int = 90,
    log_fn: Callable[[str], None] | None = None,
) -> tuple[int, int]:
    """Prune orphaned and stale workstreams."""
    try:
        orphans, stale = get_storage().prune_workstreams(retention_days)
    except Exception:
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
        return None


# -- Workstream config --------------------------------------------------------


def save_workstream_config(ws_id: str, config: dict[str, str]) -> None:
    """Persist workstream configuration key/value pairs."""
    with contextlib.suppress(Exception):
        get_storage().save_workstream_config(ws_id, config)


def load_workstream_config(ws_id: str) -> dict[str, str]:
    """Load workstream configuration."""
    try:
        return get_storage().load_workstream_config(ws_id)
    except Exception:
        return {}


# -- Prompt templates ---------------------------------------------------------


def list_default_templates(org_id: str = "") -> list[dict[str, Any]]:
    """Return all templates where is_default=True, ordered by name."""
    try:
        return get_storage().list_default_templates(org_id)
    except Exception:
        return []


def get_prompt_template_by_name(name: str) -> dict[str, Any] | None:
    """Lookup prompt template by name."""
    try:
        return get_storage().get_prompt_template_by_name(name)
    except Exception:
        return None


# -- Workstream templates -----------------------------------------------------


def get_ws_template_by_name(name: str) -> dict[str, Any] | None:
    """Lookup workstream template by name."""
    try:
        return get_storage().get_ws_template_by_name(name)
    except Exception:
        return None


def list_ws_templates(enabled_only: bool = False) -> list[dict[str, Any]]:
    """Return all workstream templates, optionally enabled only."""
    try:
        return get_storage().list_ws_templates(enabled_only=enabled_only)
    except Exception:
        return []


# -- Workstream metadata ------------------------------------------------------


def set_workstream_alias(ws_id: str, alias: str) -> bool:
    """Set a human-friendly alias. Returns False if alias is taken."""
    try:
        return get_storage().set_workstream_alias(ws_id, alias)
    except Exception:
        return False


def get_workstream_display_name(ws_id: str) -> str | None:
    """Return the alias (or title) for a workstream, or None if unset."""
    try:
        return get_storage().get_workstream_display_name(ws_id)
    except Exception:
        return None


def update_workstream_title(ws_id: str, title: str) -> None:
    """Set or update the auto-generated title for a workstream."""
    with contextlib.suppress(Exception):
        get_storage().update_workstream_title(ws_id, title)


# -- Conversation search -------------------------------------------------------


def search_history(query: str, limit: int = 20) -> list[Any]:
    """Search conversation history."""
    try:
        return get_storage().search_history(query, limit)
    except Exception:
        return []


def search_history_recent(limit: int = 20) -> list[Any]:
    """Return most recent conversation messages."""
    try:
        return get_storage().search_history_recent(limit)
    except Exception:
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
        return "", None


def delete_structured_memory(name: str, scope: str = "global", scope_id: str = "") -> bool:
    """Delete a structured memory by name+scope. Returns True if existed."""
    name = normalize_key(name)
    try:
        return get_storage().delete_structured_memory(name, scope, scope_id)
    except Exception:
        return False


def delete_structured_memory_by_id(memory_id: str) -> bool:
    """Delete a structured memory by its primary key. Returns True if existed."""
    try:
        return get_storage().delete_structured_memory_by_id(memory_id)
    except Exception:
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
        return []


def count_structured_memories(mem_type: str = "", scope: str = "", scope_id: str = "") -> int:
    """Count structured memories with optional type/scope filter."""
    try:
        return get_storage().count_structured_memories(
            mem_type=mem_type, scope=scope, scope_id=scope_id
        )
    except Exception:
        return 0
