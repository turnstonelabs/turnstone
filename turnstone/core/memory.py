"""Persistence facade — delegates to the pluggable storage backend.

All functions maintain their existing signatures for consumers (session.py,
server.py, cli.py). The actual storage implementation lives in
``turnstone.core.storage``.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from turnstone.core.storage import get_storage

if TYPE_CHECKING:
    from collections.abc import Callable


def normalize_key(key: str) -> str:
    """Normalize a memory key for consistent lookup."""
    return key.lower().replace("-", "_").replace(" ", "_")


# -- Core session operations ---------------------------------------------------


def register_session(
    session_id: str,
    title: str | None = None,
    node_id: str | None = None,
    ws_id: str | None = None,
) -> None:
    """Create a sessions row for a new session (no-op if already exists)."""
    with contextlib.suppress(Exception):
        get_storage().register_session(session_id, title, node_id=node_id, ws_id=ws_id)


def save_message(
    session_id: str,
    role: str,
    content: str | None,
    tool_name: str | None = None,
    tool_args: str | None = None,
    tool_call_id: str | None = None,
    provider_data: str | None = None,
) -> None:
    """Log a message to the conversations table."""
    with contextlib.suppress(Exception):
        get_storage().save_message(
            session_id, role, content, tool_name, tool_args, tool_call_id, provider_data
        )


def load_session_messages(session_id: str) -> list[dict[str, Any]]:
    """Load messages for a session and reconstruct OpenAI message format."""
    try:
        return get_storage().load_session_messages(session_id)
    except Exception:
        return []


# -- Session management --------------------------------------------------------


def list_sessions(limit: int = 20) -> list[Any]:
    """List recent sessions with message counts."""
    try:
        return get_storage().list_sessions(limit)
    except Exception:
        return []


def delete_session(session_id: str) -> bool:
    """Delete a session and all its messages."""
    try:
        return get_storage().delete_session(session_id)
    except Exception:
        return False


def prune_sessions(
    retention_days: int = 90,
    log_fn: Callable[[str], None] | None = None,
) -> tuple[int, int]:
    """Prune orphaned and stale sessions."""
    try:
        orphans, stale = get_storage().prune_sessions(retention_days)
    except Exception:
        return (0, 0)

    if log_fn and (orphans or stale):
        parts = []
        if orphans:
            parts.append(f"{orphans} empty session{'s' if orphans != 1 else ''}")
        if stale:
            parts.append(
                f"{stale} session{'s' if stale != 1 else ''} older than {retention_days} days"
            )
        log_fn(f"[turnstone] Session cleanup: removed {', '.join(parts)}.")

    return (orphans, stale)


def resolve_session(alias_or_id: str) -> str | None:
    """Resolve an alias or session_id (or prefix) to a full session_id."""
    try:
        return get_storage().resolve_session(alias_or_id)
    except Exception:
        return None


# -- Session config ------------------------------------------------------------


def save_session_config(session_id: str, config: dict[str, str]) -> None:
    """Persist session configuration key/value pairs."""
    with contextlib.suppress(Exception):
        get_storage().save_session_config(session_id, config)


def load_session_config(session_id: str) -> dict[str, str]:
    """Load session configuration."""
    try:
        return get_storage().load_session_config(session_id)
    except Exception:
        return {}


# -- Session metadata ----------------------------------------------------------


def set_session_alias(session_id: str, alias: str) -> bool:
    """Set a human-friendly alias. Returns False if alias is taken."""
    try:
        return get_storage().set_session_alias(session_id, alias)
    except Exception:
        return False


def get_session_name(session_id: str) -> str | None:
    """Return the alias (or title) for a session, or None if unset."""
    try:
        return get_storage().get_session_name(session_id)
    except Exception:
        return None


def update_session_title(session_id: str, title: str) -> None:
    """Set or update the auto-generated title for a session."""
    with contextlib.suppress(Exception):
        get_storage().update_session_title(session_id, title)


# -- Workstream operations -----------------------------------------------------


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


def list_workstreams(node_id: str | None = None, limit: int = 100) -> list[Any]:
    """List workstreams, optionally filtered by node_id."""
    try:
        return get_storage().list_workstreams(node_id, limit)
    except Exception:
        return []


# -- Key-value store (memories) ------------------------------------------------


def save_memory(key: str, value: str) -> str | None:
    """Save a memory. Returns the previous value if it existed."""
    try:
        return get_storage().kv_set(key, value)
    except Exception:
        return None


def delete_memory(key: str) -> bool:
    """Delete a memory by key. Returns True if the key existed."""
    try:
        return get_storage().kv_delete(key)
    except Exception:
        return False


def load_memories() -> list[tuple[str, str]]:
    """Return all (key, value) memory pairs sorted by key."""
    try:
        return get_storage().kv_list()
    except Exception:
        return []


def search_memories(query: str) -> list[tuple[str, str]]:
    """Search memories by query. Returns matching (key, value) pairs."""
    try:
        return get_storage().kv_search(query)
    except Exception:
        return []


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
