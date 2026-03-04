"""Storage backend protocol — the contract every persistence adapter must implement."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class StorageBackend(Protocol):
    """Protocol that every storage backend adapter must implement.

    Provides session management, conversation persistence, key-value storage
    (for memories), and full-text search.
    """

    # -- Core session operations -----------------------------------------------

    def register_session(
        self,
        session_id: str,
        title: str | None = None,
        node_id: str | None = None,
        ws_id: str | None = None,
    ) -> None:
        """Create a sessions row for a new session (no-op if already exists)."""
        ...

    def save_message(
        self,
        session_id: str,
        role: str,
        content: str | None,
        tool_name: str | None = None,
        tool_args: str | None = None,
        tool_call_id: str | None = None,
        provider_data: str | None = None,
    ) -> None:
        """Log a message to the conversations table."""
        ...

    def load_session_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Load messages for a session and reconstruct OpenAI message format."""
        ...

    # -- Session management ----------------------------------------------------

    def list_sessions(self, limit: int = 20) -> list[Any]:
        """List recent sessions with message counts, ordered by updated DESC."""
        ...

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and all its messages. Returns True on success."""
        ...

    def prune_sessions(self, retention_days: int = 90) -> tuple[int, int]:
        """Remove orphaned + stale unnamed sessions. Returns (orphans, stale)."""
        ...

    def resolve_session(self, alias_or_id: str) -> str | None:
        """Resolve an alias or session_id (or prefix) to a full session_id."""
        ...

    # -- Session config --------------------------------------------------------

    def save_session_config(self, session_id: str, config: dict[str, str]) -> None:
        """Persist session configuration key/value pairs."""
        ...

    def load_session_config(self, session_id: str) -> dict[str, str]:
        """Load session configuration. Returns empty dict if none stored."""
        ...

    # -- Session metadata ------------------------------------------------------

    def set_session_alias(self, session_id: str, alias: str) -> bool:
        """Set a human-friendly alias. Returns False if alias is taken."""
        ...

    def get_session_name(self, session_id: str) -> str | None:
        """Return the alias (or title) for a session, or None if unset."""
        ...

    def update_session_title(self, session_id: str, title: str) -> None:
        """Set or update the auto-generated title for a session."""
        ...

    # -- Generic key-value store (backs memories table) ------------------------

    def kv_get(self, key: str) -> str | None:
        """Get a value by key. Returns None if not found."""
        ...

    def kv_set(self, key: str, value: str) -> str | None:
        """Set a key-value pair. Returns the previous value if it existed."""
        ...

    def kv_delete(self, key: str) -> bool:
        """Delete a key. Returns True if the key existed."""
        ...

    def kv_list(self) -> list[tuple[str, str]]:
        """Return all (key, value) pairs sorted by key."""
        ...

    def kv_search(self, query: str) -> list[tuple[str, str]]:
        """Search key-value pairs by query. Returns matching (key, value) pairs."""
        ...

    # -- Workstream operations -------------------------------------------------

    def register_workstream(
        self,
        ws_id: str,
        node_id: str | None = None,
        name: str = "",
        state: str = "idle",
    ) -> None:
        """Create a workstreams row (no-op if already exists)."""
        ...

    def update_workstream_state(self, ws_id: str, state: str) -> None:
        """Update a workstream's state and bump updated timestamp."""
        ...

    def update_workstream_name(self, ws_id: str, name: str) -> None:
        """Update a workstream's display name."""
        ...

    def delete_workstream(self, ws_id: str) -> bool:
        """Delete a workstream. Returns True on success."""
        ...

    def list_workstreams(self, node_id: str | None = None, limit: int = 100) -> list[Any]:
        """List workstreams, optionally filtered by node_id."""
        ...

    # -- Conversation search ---------------------------------------------------

    def search_history(self, query: str, limit: int = 20) -> list[Any]:
        """Search conversation history. Returns (timestamp, session_id, role, content, tool_name)."""
        ...

    def search_history_recent(self, limit: int = 20) -> list[Any]:
        """Return most recent conversation messages."""
        ...

    # -- Lifecycle -------------------------------------------------------------

    def close(self) -> None:
        """Release resources (connection pool, engine, etc.)."""
        ...
