"""Storage backend protocol — the contract every persistence adapter must implement."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class StorageBackend(Protocol):
    """Protocol that every storage backend adapter must implement.

    Provides workstream management, conversation persistence, key-value storage
    (for memories), and full-text search.
    """

    # -- Core conversation operations ------------------------------------------

    def save_message(
        self,
        ws_id: str,
        role: str,
        content: str | None,
        tool_name: str | None = None,
        tool_args: str | None = None,
        tool_call_id: str | None = None,
        provider_data: str | None = None,
    ) -> None:
        """Log a message to the conversations table."""
        ...

    def load_messages(self, ws_id: str) -> list[dict[str, Any]]:
        """Load messages for a workstream and reconstruct OpenAI message format."""
        ...

    # -- Workstream management -------------------------------------------------

    def list_workstreams_with_history(self, limit: int = 20) -> list[Any]:
        """List workstreams that have messages, ordered by updated DESC."""
        ...

    def prune_workstreams(self, retention_days: int = 90) -> tuple[int, int]:
        """Remove orphaned + stale unnamed workstreams. Returns (orphans, stale)."""
        ...

    def resolve_workstream(self, alias_or_id: str) -> str | None:
        """Resolve an alias or ws_id (or prefix) to a full ws_id."""
        ...

    # -- Workstream config -----------------------------------------------------

    def save_workstream_config(self, ws_id: str, config: dict[str, str]) -> None:
        """Persist workstream configuration key/value pairs."""
        ...

    def load_workstream_config(self, ws_id: str) -> dict[str, str]:
        """Load workstream configuration. Returns empty dict if none stored."""
        ...

    # -- Workstream metadata ---------------------------------------------------

    def set_workstream_alias(self, ws_id: str, alias: str) -> bool:
        """Set a human-friendly alias. Returns False if alias is taken."""
        ...

    def get_workstream_display_name(self, ws_id: str) -> str | None:
        """Return the alias (or title) for a workstream, or None if unset."""
        ...

    def update_workstream_title(self, ws_id: str, title: str) -> None:
        """Set or update the auto-generated title for a workstream."""
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
        user_id: str | None = None,
        alias: str | None = None,
        title: str | None = None,
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
        """Delete a workstream and all its conversations + config."""
        ...

    def list_workstreams(self, node_id: str | None = None, limit: int = 100) -> list[Any]:
        """List workstreams, optionally filtered by node_id."""
        ...

    # -- Conversation search ---------------------------------------------------

    def search_history(self, query: str, limit: int = 20) -> list[Any]:
        """Search conversation history. Returns (timestamp, ws_id, role, content, tool_name)."""
        ...

    def search_history_recent(self, limit: int = 20) -> list[Any]:
        """Return most recent conversation messages."""
        ...

    # -- User identity operations -----------------------------------------------

    def create_user(
        self, user_id: str, username: str, display_name: str, password_hash: str
    ) -> None:
        """Create a user row. No-op if user_id already exists."""
        ...

    def create_first_user(
        self, user_id: str, username: str, display_name: str, password_hash: str
    ) -> bool:
        """Atomically create a user only if no users exist. Returns True if created."""
        ...

    def get_user(self, user_id: str) -> dict[str, str] | None:
        """Return user dict {user_id, username, display_name, password_hash, created} or None."""
        ...

    def get_user_by_username(self, username: str) -> dict[str, str] | None:
        """Lookup user by username. Returns same dict as get_user or None."""
        ...

    def list_users(self) -> list[dict[str, str]]:
        """Return all users ordered by created DESC."""
        ...

    def delete_user(self, user_id: str) -> bool:
        """Delete user and cascade-delete all their tokens. Returns True if existed."""
        ...

    def create_api_token(
        self,
        token_id: str,
        token_hash: str,
        token_prefix: str,
        user_id: str,
        name: str,
        scopes: str,
        expires: str | None = None,
    ) -> None:
        """Store a hashed API token."""
        ...

    def get_api_token_by_hash(self, token_hash: str) -> dict[str, str] | None:
        """Lookup token by SHA-256 hash. Returns dict with all columns or None."""
        ...

    def list_api_tokens(self, user_id: str) -> list[dict[str, str]]:
        """List tokens for a user (no hash in results, prefix only)."""
        ...

    def delete_api_token(self, token_id: str) -> bool:
        """Revoke/delete a token by ID. Returns True if existed."""
        ...

    # -- Channel user mapping ---------------------------------------------------

    def create_channel_user(self, channel_type: str, channel_user_id: str, user_id: str) -> None:
        """Map an external channel user to a turnstone user_id. No-op if exists."""
        ...

    def get_channel_user(self, channel_type: str, channel_user_id: str) -> dict[str, str] | None:
        """Lookup turnstone user for a channel user. Returns dict or None."""
        ...

    def list_channel_users_by_user(self, user_id: str) -> list[dict[str, str]]:
        """List all channel mappings for a turnstone user."""
        ...

    def delete_channel_user(self, channel_type: str, channel_user_id: str) -> bool:
        """Remove a channel user mapping. Returns True if existed."""
        ...

    # -- Channel routing -------------------------------------------------------

    def create_channel_route(
        self, channel_type: str, channel_id: str, ws_id: str, node_id: str = ""
    ) -> None:
        """Map a channel/thread to a workstream. No-op if exists."""
        ...

    def get_channel_route(self, channel_type: str, channel_id: str) -> dict[str, str] | None:
        """Lookup workstream for a channel/thread."""
        ...

    def get_channel_route_by_ws(self, ws_id: str) -> dict[str, str] | None:
        """Reverse lookup: find channel/thread for a workstream."""
        ...

    def list_channel_routes_by_type(self, channel_type: str) -> list[dict[str, str]]:
        """List all routes for a channel type, ordered by created DESC."""
        ...

    def delete_channel_route(self, channel_type: str, channel_id: str) -> bool:
        """Remove a channel route. Returns True if existed."""
        ...

    # -- Scheduled tasks -------------------------------------------------------

    def create_scheduled_task(
        self,
        task_id: str,
        name: str,
        description: str,
        schedule_type: str,
        cron_expr: str,
        at_time: str,
        target_mode: str,
        model: str,
        initial_message: str,
        auto_approve: bool,
        auto_approve_tools: list[str],
        created_by: str,
        next_run: str,
    ) -> None:
        """Create a scheduled task. No-op if task_id already exists."""
        ...

    def get_scheduled_task(self, task_id: str) -> dict[str, Any] | None:
        """Return scheduled task dict or None."""
        ...

    def list_scheduled_tasks(self) -> list[dict[str, Any]]:
        """Return all scheduled tasks ordered by created DESC."""
        ...

    def update_scheduled_task(self, task_id: str, **fields: Any) -> bool:
        """Update specified fields on a scheduled task. Returns True if found."""
        ...

    def delete_scheduled_task(self, task_id: str) -> bool:
        """Delete a scheduled task and its run history. Returns True if found."""
        ...

    def list_due_tasks(self, now: str) -> list[dict[str, Any]]:
        """Return enabled tasks whose next_run <= now, ordered by next_run."""
        ...

    def record_task_run(
        self,
        run_id: str,
        task_id: str,
        node_id: str,
        ws_id: str,
        correlation_id: str,
        started: str,
        status: str,
        error: str,
    ) -> None:
        """Record a scheduled task execution."""
        ...

    def list_task_runs(self, task_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """List run history for a task, ordered by started DESC."""
        ...

    def prune_task_runs(self, retention_days: int = 90) -> int:
        """Delete task runs older than retention_days. Returns count deleted."""
        ...

    # -- Service registry ------------------------------------------------------

    def register_service(
        self, service_type: str, service_id: str, url: str, metadata: str = "{}"
    ) -> None:
        """Register or update a service instance. Upserts by (service_type, service_id)."""
        ...

    def heartbeat_service(self, service_type: str, service_id: str) -> bool:
        """Update last_heartbeat for a registered service. Returns False if not found."""
        ...

    def list_services(self, service_type: str, max_age_seconds: int = 120) -> list[dict[str, str]]:
        """Return healthy services of a given type (heartbeat within max_age_seconds)."""
        ...

    def deregister_service(self, service_type: str, service_id: str) -> bool:
        """Remove a service registration. Returns True if existed."""
        ...

    # -- Lifecycle -------------------------------------------------------------

    def close(self) -> None:
        """Release resources (connection pool, engine, etc.)."""
        ...
