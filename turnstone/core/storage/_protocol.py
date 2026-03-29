"""Storage backend protocol — the contract every persistence adapter must implement."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class StorageBackend(Protocol):
    """Protocol that every storage backend adapter must implement.

    Provides workstream management, conversation persistence, structured
    memories, and full-text search.
    """

    # -- Core conversation operations ------------------------------------------

    def save_message(
        self,
        ws_id: str,
        role: str,
        content: str | None,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        provider_data: str | None = None,
        tool_calls: str | None = None,
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

    # -- Structured memories ---------------------------------------------------

    def create_structured_memory(
        self,
        memory_id: str,
        name: str,
        description: str,
        mem_type: str,
        scope: str,
        scope_id: str,
        content: str,
    ) -> None:
        """Create a structured memory record."""
        ...

    def get_structured_memory(self, memory_id: str) -> dict[str, str] | None:
        """Return structured memory dict or None."""
        ...

    def get_structured_memory_by_name(
        self, name: str, scope: str = "global", scope_id: str = ""
    ) -> dict[str, str] | None:
        """Lookup structured memory by (name, scope, scope_id). Returns dict or None."""
        ...

    def update_structured_memory(self, memory_id: str, **fields: str) -> bool:
        """Update specified fields on a structured memory. Returns True if found."""
        ...

    def delete_structured_memory(
        self, name: str, scope: str = "global", scope_id: str = ""
    ) -> bool:
        """Delete a structured memory by (name, scope, scope_id). Returns True if existed."""
        ...

    def delete_structured_memory_by_id(self, memory_id: str) -> bool:
        """Delete a structured memory by its primary key. Returns True if existed."""
        ...

    def list_structured_memories(
        self,
        mem_type: str = "",
        scope: str = "",
        scope_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, str]]:
        """Return structured memories with optional filters, ordered by updated DESC."""
        ...

    def search_structured_memories(
        self,
        query: str,
        mem_type: str = "",
        scope: str = "",
        scope_id: str = "",
        limit: int = 20,
    ) -> list[dict[str, str]]:
        """Search structured memories by query. Returns matching memory dicts."""
        ...

    def touch_structured_memories(self, keys: list[tuple[str, str, str]]) -> int:
        """Batch-touch multiple memories.

        Each key is ``(name, scope, scope_id)``.  Callers should deduplicate
        before calling; each key increments ``access_count`` once per call.
        Returns count of rows found and updated.
        """
        ...

    def count_structured_memories(
        self, mem_type: str = "", scope: str = "", scope_id: str = ""
    ) -> int:
        """Count structured memories with optional type and scope filters."""
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
        skill_id: str = "",
        skill_version: int = 0,
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

    # -- OIDC identity ---------------------------------------------------------

    def create_oidc_identity(self, issuer: str, subject: str, user_id: str, email: str) -> None:
        """Link an OIDC subject to a turnstone user. No-op if exists."""
        ...

    def get_oidc_identity(self, issuer: str, subject: str) -> dict[str, str] | None:
        """Lookup turnstone user by OIDC issuer+subject. Returns dict or None."""
        ...

    def update_oidc_identity_login(self, issuer: str, subject: str) -> bool:
        """Update last_login timestamp. Returns True if row existed."""
        ...

    def list_oidc_identities_for_user(self, user_id: str) -> list[dict[str, str]]:
        """List all OIDC identities linked to a turnstone user."""
        ...

    def delete_oidc_identity(self, issuer: str, subject: str) -> bool:
        """Remove an OIDC identity link. Returns True if existed."""
        ...

    # -- OIDC pending state ----------------------------------------------------

    def create_oidc_pending_state(
        self, state: str, nonce: str, code_verifier: str, audience: str
    ) -> None:
        """Store OIDC authorization flow state for callback validation."""
        ...

    def pop_oidc_pending_state(
        self, state: str, max_age_seconds: int = 300
    ) -> dict[str, str] | None:
        """Fetch and delete pending state atomically. Returns None if expired or missing."""
        ...

    def cleanup_expired_oidc_states(self, max_age_seconds: int = 300) -> int:
        """Delete expired pending states. Returns count of deleted rows."""
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
        skill: str = "",
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

    # -- Watches ---------------------------------------------------------------

    def create_watch(
        self,
        watch_id: str,
        ws_id: str,
        node_id: str,
        name: str,
        command: str,
        interval_secs: float,
        stop_on: str | None,
        max_polls: int,
        created_by: str,
        next_poll: str,
    ) -> None:
        """Create a watch. No-op if watch_id already exists."""
        ...

    def get_watch(self, watch_id: str) -> dict[str, Any] | None:
        """Return watch dict or None."""
        ...

    def list_watches_for_ws(self, ws_id: str) -> list[dict[str, Any]]:
        """Return active watches for a workstream, ordered by created DESC."""
        ...

    def list_watches_for_node(self, node_id: str) -> list[dict[str, Any]]:
        """Return all active watches on a node, ordered by created DESC."""
        ...

    def list_due_watches(self, now: str) -> list[dict[str, Any]]:
        """Return active watches whose next_poll <= now, ordered by next_poll."""
        ...

    def update_watch(self, watch_id: str, **fields: Any) -> bool:
        """Update specified fields on a watch. Returns True if found."""
        ...

    def delete_watch(self, watch_id: str) -> bool:
        """Delete a watch. Returns True if found."""
        ...

    def delete_watches_for_ws(self, ws_id: str) -> int:
        """Delete all watches for a workstream. Returns count deleted."""
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

    # -- Roles (RBAC) ----------------------------------------------------------

    def create_role(
        self,
        role_id: str,
        name: str,
        display_name: str,
        permissions: str,
        builtin: bool,
        org_id: str,
    ) -> None:
        """Create a role. No-op if role_id already exists."""
        ...

    def get_role(self, role_id: str) -> dict[str, Any] | None:
        """Return role dict or None."""
        ...

    def get_role_by_name(self, name: str) -> dict[str, Any] | None:
        """Lookup role by name. Returns same dict as get_role or None."""
        ...

    def list_roles(self, org_id: str = "") -> list[dict[str, Any]]:
        """Return all roles, optionally filtered by org_id. Ordered by name."""
        ...

    def update_role(self, role_id: str, **fields: Any) -> bool:
        """Update specified fields on a role. Returns True if found."""
        ...

    def delete_role(self, role_id: str) -> bool:
        """Delete a custom role. Returns True if found."""
        ...

    def assign_role(self, user_id: str, role_id: str, assigned_by: str) -> None:
        """Assign a role to a user. No-op if already assigned."""
        ...

    def unassign_role(self, user_id: str, role_id: str) -> bool:
        """Unassign a role from a user. Returns True if existed."""
        ...

    def list_user_roles(self, user_id: str) -> list[dict[str, Any]]:
        """List roles assigned to a user (joins user_roles with roles)."""
        ...

    def get_user_permissions(self, user_id: str) -> set[str]:
        """Return the union of all permissions from the user's assigned roles."""
        ...

    # -- Organizations ---------------------------------------------------------

    def create_org(self, org_id: str, name: str, display_name: str, settings: str = "{}") -> None:
        """Create an organization. No-op if org_id already exists."""
        ...

    def get_org(self, org_id: str) -> dict[str, Any] | None:
        """Return org dict or None."""
        ...

    def list_orgs(self) -> list[dict[str, Any]]:
        """Return all organizations ordered by name."""
        ...

    def update_org(self, org_id: str, **fields: Any) -> bool:
        """Update specified fields on an org. Returns True if found."""
        ...

    # -- Tool policies ---------------------------------------------------------

    def create_tool_policy(
        self,
        policy_id: str,
        name: str,
        tool_pattern: str,
        action: str,
        priority: int,
        org_id: str,
        enabled: bool,
        created_by: str,
    ) -> None:
        """Create a tool policy."""
        ...

    def get_tool_policy(self, policy_id: str) -> dict[str, Any] | None:
        """Return tool policy dict or None."""
        ...

    def list_tool_policies(self, org_id: str = "") -> list[dict[str, Any]]:
        """Return all tool policies ordered by priority DESC."""
        ...

    def update_tool_policy(self, policy_id: str, **fields: Any) -> bool:
        """Update specified fields on a tool policy. Returns True if found."""
        ...

    def delete_tool_policy(self, policy_id: str) -> bool:
        """Delete a tool policy. Returns True if found."""
        ...

    # -- Prompt templates ------------------------------------------------------

    def create_prompt_template(
        self,
        template_id: str,
        name: str,
        category: str,
        content: str,
        variables: str,
        is_default: bool,
        org_id: str,
        created_by: str,
        origin: str = "manual",
        mcp_server: str = "",
        readonly: bool = False,
        description: str = "",
        tags: str = "[]",
        source_url: str = "",
        version: str = "1.0.0",
        author: str = "",
        activation: str = "named",
        token_estimate: int = 0,
        model: str = "",
        auto_approve: bool = False,
        temperature: float | None = None,
        reasoning_effort: str = "",
        max_tokens: int | None = None,
        token_budget: int = 0,
        agent_max_turns: int | None = None,
        notify_on_complete: str = "{}",
        enabled: bool = True,
        allowed_tools: str = "[]",
        skill_license: str = "",
        compatibility: str = "",
        priority: int = 0,
    ) -> None:
        """Create a prompt template (skill)."""
        ...

    def get_prompt_template(self, template_id: str) -> dict[str, Any] | None:
        """Return prompt template dict or None."""
        ...

    def get_prompt_template_by_name(self, name: str) -> dict[str, Any] | None:
        """Lookup prompt template by name. Returns same dict as get_prompt_template or None."""
        ...

    def list_prompt_templates(
        self, org_id: str = "", limit: int = 0, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Return all prompt templates ordered by name."""
        ...

    def list_default_templates(self, org_id: str = "") -> list[dict[str, Any]]:
        """Return all templates where is_default=True, ordered by name."""
        ...

    def list_prompt_templates_by_origin(self, origin: str) -> list[dict[str, Any]]:
        """Return all prompt templates with the given origin, ordered by name."""
        ...

    def update_prompt_template(self, template_id: str, **fields: Any) -> bool:
        """Update specified fields on a prompt template. Returns True if found."""
        ...

    def delete_prompt_template(self, template_id: str) -> bool:
        """Delete a prompt template. Returns True if found."""
        ...

    def count_prompt_templates(self, org_id: str = "") -> int:
        """Count prompt templates, optionally filtered by org_id."""
        ...

    def list_skills_by_activation(
        self,
        activation: str,
        *,
        enabled_only: bool = False,
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        """Return prompt templates filtered by activation value, ordered by priority then name."""
        ...

    def get_skill_by_name(self, name: str) -> dict[str, Any] | None:
        """Lookup skill (prompt template) by name. Returns dict or None."""
        ...

    def get_skill_by_source_url(self, source_url: str) -> dict[str, Any] | None:
        """Lookup skill (prompt template) by source_url. Returns dict or None."""
        ...

    def list_installed_skill_urls(self) -> list[dict[str, str]]:
        """Return [{source_url, template_id, scan_status}] for skills with non-empty source_url."""
        ...

    # -- Skill resources -------------------------------------------------------

    def create_skill_resource(
        self,
        resource_id: str,
        skill_id: str,
        path: str,
        content: str,
        content_type: str = "text/plain",
    ) -> None:
        """Create a bundled resource file for a skill."""
        ...

    def list_skill_resources(self, skill_id: str) -> list[dict[str, Any]]:
        """Return all resource files for a skill, ordered by path."""
        ...

    def get_skill_resource(self, skill_id: str, path: str) -> dict[str, Any] | None:
        """Return a single resource file by skill ID and path."""
        ...

    def delete_skill_resources(self, skill_id: str) -> int:
        """Delete all resource files for a skill. Returns count deleted."""
        ...

    def delete_skill_resource_by_path(self, skill_id: str, path: str) -> bool:
        """Delete a single resource file by skill_id and path. Returns True if found."""
        ...

    def count_skill_resources_bulk(self, skill_ids: list[str]) -> dict[str, int]:
        """Count resources per skill in a single query. Returns {skill_id: count}."""
        ...

    # -- Skill versions --------------------------------------------------------

    def create_skill_version(
        self,
        skill_id: str,
        version: int,
        snapshot: str,
        changed_by: str = "",
    ) -> None:
        """Create a version snapshot for a skill."""
        ...

    def list_skill_versions(self, skill_id: str) -> list[dict[str, Any]]:
        """List version history for a skill, ordered by version DESC."""
        ...

    def delete_skill_versions(self, skill_id: str) -> int:
        """Delete all version snapshots for a skill. Returns count deleted."""
        ...

    # -- Usage events ----------------------------------------------------------

    def record_usage_event(
        self,
        event_id: str,
        user_id: str,
        ws_id: str,
        node_id: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        tool_calls_count: int,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> None:
        """Record a usage event (token counts, tool calls for one LLM request)."""
        ...

    def query_usage(
        self,
        since: str,
        until: str = "",
        user_id: str = "",
        model: str = "",
        group_by: str = "",
    ) -> list[dict[str, Any]]:
        """Query aggregated usage data. group_by: 'day', 'hour', 'model', 'user'."""
        ...

    def prune_usage_events(self, retention_days: int = 90) -> int:
        """Delete usage events older than retention_days. Returns count deleted."""
        ...

    # -- Audit events ----------------------------------------------------------

    def record_audit_event(
        self,
        event_id: str,
        user_id: str,
        action: str,
        resource_type: str,
        resource_id: str,
        detail: str,
        ip_address: str,
    ) -> None:
        """Record an audit event."""
        ...

    def list_audit_events(
        self,
        action: str = "",
        user_id: str = "",
        since: str = "",
        until: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List audit events with optional filters, ordered by timestamp DESC."""
        ...

    def count_audit_events(
        self,
        action: str = "",
        user_id: str = "",
        since: str = "",
        until: str = "",
    ) -> int:
        """Count audit events matching the filters."""
        ...

    def prune_audit_events(self, retention_days: int = 365) -> int:
        """Delete audit events older than retention_days. Returns count deleted."""
        ...

    # -- Intent verdicts -------------------------------------------------------

    def create_intent_verdict(
        self,
        verdict_id: str,
        ws_id: str,
        call_id: str,
        func_name: str,
        func_args: str,
        intent_summary: str,
        risk_level: str,
        confidence: float,
        recommendation: str,
        reasoning: str,
        evidence: str,
        tier: str,
        judge_model: str,
        latency_ms: int,
    ) -> None:
        """Record an intent validation verdict."""
        ...

    def get_intent_verdict(self, verdict_id: str) -> dict[str, Any] | None:
        """Return intent verdict dict or None."""
        ...

    def list_intent_verdicts(
        self,
        ws_id: str = "",
        since: str = "",
        until: str = "",
        risk_level: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List intent verdicts with optional filters, ordered by created DESC."""
        ...

    def update_intent_verdict(self, verdict_id: str, **fields: Any) -> bool:
        """Update fields on an intent verdict (e.g. user_decision). Returns True if found."""
        ...

    def count_intent_verdicts(
        self,
        ws_id: str = "",
        since: str = "",
        until: str = "",
        risk_level: str = "",
    ) -> int:
        """Count intent verdicts matching the filters."""
        ...

    # -- Output assessments ----------------------------------------------------

    def record_output_assessment(
        self,
        assessment_id: str,
        ws_id: str,
        call_id: str,
        func_name: str,
        flags: str,
        risk_level: str,
        annotations: str,
        output_length: int,
        redacted: bool,
    ) -> None:
        """Record an output guard assessment."""
        ...

    def list_output_assessments(
        self,
        ws_id: str = "",
        risk_level: str = "",
        since: str = "",
        until: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List output assessments with optional filters, ordered by created DESC."""
        ...

    def count_output_assessments(
        self,
        ws_id: str = "",
        risk_level: str = "",
        since: str = "",
        until: str = "",
    ) -> int:
        """Count output assessments matching the filters."""
        ...

    # -- System settings -------------------------------------------------------

    def get_system_setting(self, key: str, node_id: str = "") -> dict[str, Any] | None:
        """Return setting dict or None."""
        ...

    def list_system_settings(self, node_id: str = "") -> list[dict[str, Any]]:
        """Return settings ordered by key.

        When *node_id* is provided, returns both global (node_id="")
        and node-specific settings.  When empty, returns all settings.
        """
        ...

    def upsert_system_setting(
        self,
        key: str,
        value: str,
        node_id: str = "",
        is_secret: bool = False,
        changed_by: str = "",
    ) -> None:
        """Create or update a system setting. Value is JSON-encoded."""
        ...

    def delete_system_setting(self, key: str, node_id: str = "") -> bool:
        """Delete a setting by (key, node_id). Returns True if existed."""
        ...

    def get_system_settings_bulk(self, node_id: str = "") -> dict[str, str]:
        """Return all settings as {key: json_value} dict.

        Loads global settings (node_id="") first, then overlays per-node
        overrides if node_id is provided.
        """
        ...

    # -- MCP server definitions ------------------------------------------------

    def create_mcp_server(
        self,
        server_id: str,
        name: str,
        transport: str,
        command: str = "",
        args: str = "[]",
        url: str = "",
        headers: str = "{}",
        env: str = "{}",
        auto_approve: bool = False,
        enabled: bool = True,
        created_by: str = "",
        registry_name: str | None = None,
        registry_version: str = "",
        registry_meta: str = "{}",
    ) -> None:
        """Create an MCP server definition. No-op if server_id already exists."""
        ...

    def get_mcp_server(self, server_id: str) -> dict[str, Any] | None:
        """Return MCP server dict or None."""
        ...

    def get_mcp_server_by_name(self, name: str) -> dict[str, Any] | None:
        """Return MCP server dict by name or None."""
        ...

    def list_mcp_servers(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        """Return MCP servers ordered by name."""
        ...

    def update_mcp_server(self, server_id: str, **fields: Any) -> bool:
        """Update specified fields on an MCP server. Returns True if found."""
        ...

    def get_mcp_server_by_registry_name(self, registry_name: str) -> dict[str, Any] | None:
        """Return MCP server dict by registry name or None."""
        ...

    def delete_mcp_server(self, server_id: str) -> bool:
        """Delete an MCP server definition. Returns True if existed."""
        ...

    # -- TLS / ACME (lacme Store) ----------------------------------------------

    def save_tls_account_key(self, key_id: str, key_pem: str) -> None:
        """Persist an ACME account private key."""
        ...

    def load_tls_account_key(self, key_id: str) -> str | None:
        """Load an ACME account key PEM by ID. Returns None if not found."""
        ...

    def save_tls_ca(self, name: str, cert_pem: str, key_pem: str) -> None:
        """Persist a CA root certificate and key."""
        ...

    def load_tls_ca(self, name: str) -> dict[str, Any] | None:
        """Load CA cert+key by name. Returns dict with cert_pem, key_pem or None."""
        ...

    def save_tls_cert(
        self,
        domain: str,
        cert_pem: str,
        fullchain_pem: str,
        key_pem: str,
        issued_at: str,
        expires_at: str,
        meta: str | None = None,
    ) -> None:
        """Persist an issued certificate (upsert by domain)."""
        ...

    def load_tls_cert(self, domain: str) -> dict[str, Any] | None:
        """Load certificate by domain. Returns dict or None."""
        ...

    def list_tls_certs(self) -> list[dict[str, Any]]:
        """List all stored certificates."""
        ...

    def delete_tls_cert(self, domain: str) -> bool:
        """Delete a certificate by domain. Returns True if existed."""
        ...

    # -- Lifecycle -------------------------------------------------------------

    def close(self) -> None:
        """Release resources (connection pool, engine, etc.)."""
        ...
