"""SQLAlchemy Core schema — single source of truth for all table definitions.

Used by both storage backends and Alembic migrations.
"""

from __future__ import annotations

import sqlalchemy as sa

metadata = sa.MetaData()

structured_memories = sa.Table(
    "structured_memories",
    metadata,
    sa.Column("memory_id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("description", sa.Text, nullable=False, server_default=""),
    sa.Column("type", sa.Text, nullable=False, server_default="general"),
    sa.Column("scope", sa.Text, nullable=False, server_default="global"),
    sa.Column("scope_id", sa.Text, nullable=False, server_default=""),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
    sa.Column("last_accessed", sa.Text, nullable=False, server_default=""),
    sa.Column("access_count", sa.Integer, nullable=False, server_default="0"),
    sa.UniqueConstraint("name", "scope", "scope_id", name="uq_smem_name_scope"),
)

conversations = sa.Table(
    "conversations",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ws_id", sa.Text, nullable=False, index=True),
    sa.Column("timestamp", sa.Text, nullable=False),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("content", sa.Text),
    sa.Column("tool_name", sa.Text),
    sa.Column("tool_call_id", sa.Text),
    sa.Column("provider_data", sa.Text),
    sa.Column("tool_calls", sa.Text),
    # ``_source`` mirrors the in-memory side channel: which producer
    # synthesised the row — a ``system_nudge`` wake turn, or one of the
    # operator-context kinds on a first-class ``system`` turn (output_guard /
    # user_interjection / tool_error / watch_triggered / … — see
    # ``tool_advisory.SYSTEM_TURN_SOURCES``).  (The sibling ``_reminders`` column
    # that rode here was dropped in migration 060 — operator context lives in
    # ``system`` turns now, so it was dead weight.)
    sa.Column("_source", sa.Text),
    # SSE ``Last-Event-ID`` resume cursor: the per-ws ``_event_id``
    # ring-buffer high-water mark at the moment this row was saved (see
    # ``SessionUIBase._enqueue``).  Distinct id-space from the ``id`` PK
    # (counts SSE events, not messages; per-ws, not table-global).
    # ``/history`` returns ``max(event_id)`` of the resolved turns as a
    # cursor so the client's initial SSE fast-forwards the in-flight turn
    # through the existing delta replay.  Nullable: historical/bulk rows
    # stay NULL → cursor logic falls back to the snapshot floor.  See
    # migration 059.
    sa.Column("event_id", sa.BigInteger),
    # Tool-result error flag (persisted; migration 060).  Set on ``tool`` rows
    # whose tool raised or was cancelled, so a reload preserves the error state
    # (history rendering + the Anthropic ``is_error`` result block) instead of
    # re-deriving it from a text heuristic.  Non-tool rows are always False.
    sa.Column("is_error", sa.Boolean, nullable=False, server_default=sa.false()),
    # Ordered list of content-addressed attachment_id references for this turn
    # (JSON; NULL for turns with no attachments) — the sole message->blob link in
    # the content-addressed model; bytes resolve from workstream_attachments by id.
    sa.Column("attachments", sa.Text, nullable=True),
    # Structured per-kind operator-context metadata for a first-class ``system``
    # turn (JSON object; NULL for ordinary turns and for operator turns with no
    # extra fields).  The persisted twin of the in-memory
    # ``Turn.meta.extra["source_meta"]`` / the ``_source_meta`` side channel:
    # ``watch_triggered`` carries ``{watch_name, command, poll_count, max_polls,
    # is_final}`` so ``/history`` can rebuild the structured watch-result card;
    # other kinds (``user_interjection`` → ``{priority}``) ride generically.
    # Stripped before the LLM wire (it is a ``_``-prefixed key by the time it
    # reaches a provider).  Added in migration 060.
    sa.Column("meta", sa.Text, nullable=True),
)

sa.Index("idx_conversations_timestamp", conversations.c.timestamp)
# Composite index serving the per-ws ``MAX(event_id)`` reseed (an index
# seek, not a row scan) and per-ws event-cursor range queries.  See
# migration 059.
sa.Index("idx_conversations_ws_event", conversations.c.ws_id, conversations.c.event_id)

workstreams = sa.Table(
    "workstreams",
    metadata,
    sa.Column("ws_id", sa.Text, primary_key=True),
    sa.Column("node_id", sa.Text),
    sa.Column("user_id", sa.Text),
    sa.Column("alias", sa.Text, unique=True),
    sa.Column("title", sa.Text),
    sa.Column("name", sa.Text, nullable=False, server_default=""),
    sa.Column("state", sa.Text, nullable=False, server_default="idle"),
    sa.Column("skill_id", sa.Text, nullable=False, server_default=""),
    sa.Column("skill_version", sa.Integer, nullable=False, server_default="0"),
    # kind: "interactive" (default) | "coordinator".  See WorkstreamKind in
    # turnstone.core.workstream.  Added in migration 039; coordinator entries
    # live only on the console.
    sa.Column("kind", sa.Text, nullable=False, server_default="interactive"),
    # parent_ws_id: non-NULL for children spawned by a coordinator.  NULL for
    # top-level workstreams (including the coordinators themselves).  See
    # migration 039.
    sa.Column("parent_ws_id", sa.Text, nullable=True),
    # project_id: the project this workstream is attached to (NULL = none).
    # Resolves the ('project', project_id) recall rung; children inherit the
    # parent's project_id at spawn.  Nullable like parent_ws_id; no FK
    # constraint (this schema family declares none — see migration 058).
    # Added in migration 062.
    sa.Column("project_id", sa.Text, nullable=True),
    # persona: SLUG of the persona the workstream was created with (NULL =
    # pre-persona workstream) — personas.name, not display_name; clients
    # resolve the display label.  Display/forensics only — the full persona
    # snapshot lives in workstream_config; nothing reads this column to
    # build a session.  Added in migration 063.
    sa.Column("persona", sa.Text, nullable=True),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
)

sa.Index("idx_workstreams_node_id", workstreams.c.node_id)
sa.Index("idx_workstreams_state", workstreams.c.state)
sa.Index("idx_workstreams_user_id", workstreams.c.user_id)
sa.Index("idx_workstreams_alias", workstreams.c.alias)
sa.Index("idx_workstreams_kind", workstreams.c.kind)
sa.Index("idx_workstreams_parent", workstreams.c.parent_ws_id)
sa.Index("idx_workstreams_project", workstreams.c.project_id)

workstream_config = sa.Table(
    "workstream_config",
    metadata,
    sa.Column("ws_id", sa.Text, nullable=False),
    sa.Column("key", sa.Text, nullable=False),
    sa.Column("value", sa.Text),
    sa.PrimaryKeyConstraint("ws_id", "key"),
)

# ---------------------------------------------------------------------------
# Projects — governed, shareable resource containers (migration 062)
# ---------------------------------------------------------------------------
# A project groups workstreams and owns a ``('project', project_id)`` memory
# recall rung.  Access = RBAC perm (``project.{create,read,write}``) AND a
# per-project ACL (owner / member / public).  Thin entity; no FK constraints
# (schema-family convention — see migration 058).

projects = sa.Table(
    "projects",
    metadata,
    sa.Column("project_id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    # owner_id: the creating user (users.user_id).  NOT NULL — a project always
    # has an authenticated owner; the owner has implicit full access.
    sa.Column("owner_id", sa.Text, nullable=False),
    # visibility: "private" (owner + members) | "public" (any project.read holder
    # may read; writes stay member-gated).
    sa.Column("visibility", sa.Text, nullable=False, server_default="private"),
    # state: "active" | "archived" (archived = not recalled, not offered for attach).
    sa.Column("state", sa.Text, nullable=False, server_default="active"),
    # parent_project_id: reserved for a future hierarchy; unused in v1.
    sa.Column("parent_project_id", sa.Text, nullable=True),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
)

sa.Index("idx_projects_owner", projects.c.owner_id)
sa.Index("idx_projects_visibility", projects.c.visibility)

project_members = sa.Table(
    "project_members",
    metadata,
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("created", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("project_id", "user_id"),
)

sa.Index("idx_project_members_user", project_members.c.user_id)

# ---------------------------------------------------------------------------
# User identity tables
# ---------------------------------------------------------------------------

users = sa.Table(
    "users",
    metadata,
    sa.Column("user_id", sa.Text, primary_key=True),
    sa.Column("username", sa.Text, nullable=False, unique=True),
    sa.Column("display_name", sa.Text, nullable=False),
    sa.Column("password_hash", sa.Text, nullable=False),
    sa.Column("org_id", sa.Text, nullable=False, server_default=""),
    sa.Column("created", sa.Text, nullable=False),
)

sa.Index("idx_users_username", users.c.username)

api_tokens = sa.Table(
    "api_tokens",
    metadata,
    sa.Column("token_id", sa.Text, primary_key=True),
    sa.Column("token_hash", sa.Text, nullable=False, unique=True),
    sa.Column("token_prefix", sa.Text, nullable=False),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("name", sa.Text, nullable=False, server_default=""),
    sa.Column("scopes", sa.Text, nullable=False),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("expires", sa.Text),
)

sa.Index("idx_api_tokens_user_id", api_tokens.c.user_id)
sa.Index("idx_api_tokens_token_hash", api_tokens.c.token_hash)

channel_users = sa.Table(
    "channel_users",
    metadata,
    sa.Column("channel_type", sa.Text, nullable=False),
    sa.Column("channel_user_id", sa.Text, nullable=False),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("created", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("channel_type", "channel_user_id"),
)

sa.Index("idx_channel_users_user_id", channel_users.c.user_id)

# ---------------------------------------------------------------------------
# Channel routing tables
# ---------------------------------------------------------------------------

channel_routes = sa.Table(
    "channel_routes",
    metadata,
    sa.Column("channel_type", sa.Text, nullable=False),
    sa.Column("channel_id", sa.Text, nullable=False),
    sa.Column("ws_id", sa.Text, nullable=False),
    sa.Column("node_id", sa.Text, nullable=False, server_default=""),
    sa.Column("created", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("channel_type", "channel_id"),
)

sa.Index("idx_channel_routes_ws", channel_routes.c.ws_id)

# ---------------------------------------------------------------------------
# Scheduled task tables
# ---------------------------------------------------------------------------

scheduled_tasks = sa.Table(
    "scheduled_tasks",
    metadata,
    sa.Column("task_id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("description", sa.Text, nullable=False, server_default=""),
    sa.Column("schedule_type", sa.Text, nullable=False),  # "cron" or "at"
    sa.Column("cron_expr", sa.Text, nullable=False, server_default=""),
    sa.Column("at_time", sa.Text, nullable=False, server_default=""),  # ISO8601
    sa.Column("target_mode", sa.Text, nullable=False, server_default="auto"),
    sa.Column("model", sa.Text, nullable=False, server_default=""),
    sa.Column("initial_message", sa.Text, nullable=False),
    sa.Column("auto_approve", sa.Integer, nullable=False, server_default="0"),
    sa.Column("auto_approve_tools", sa.Text, nullable=False, server_default=""),
    sa.Column("skill", sa.Text, nullable=False, server_default=""),
    sa.Column("notify_targets", sa.Text, nullable=False, server_default="[]"),
    sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
    sa.Column("created_by", sa.Text, nullable=False, server_default=""),
    sa.Column("last_run", sa.Text),
    sa.Column("next_run", sa.Text),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
)

sa.Index("idx_scheduled_tasks_enabled", scheduled_tasks.c.enabled)
sa.Index("idx_scheduled_tasks_next_run", scheduled_tasks.c.next_run)

scheduled_task_runs = sa.Table(
    "scheduled_task_runs",
    metadata,
    sa.Column("run_id", sa.Text, primary_key=True),
    sa.Column("task_id", sa.Text, nullable=False),
    sa.Column("node_id", sa.Text, nullable=False, server_default=""),
    sa.Column("ws_id", sa.Text, nullable=False, server_default=""),
    sa.Column("correlation_id", sa.Text, nullable=False, server_default=""),
    sa.Column("started", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="dispatched"),
    sa.Column("error", sa.Text, nullable=False, server_default=""),
)

sa.Index("idx_scheduled_task_runs_task_id", scheduled_task_runs.c.task_id)
sa.Index("idx_scheduled_task_runs_started", scheduled_task_runs.c.started)

# ---------------------------------------------------------------------------
# Service registry
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Watches — in-session periodic command polling
# ---------------------------------------------------------------------------

watches = sa.Table(
    "watches",
    metadata,
    sa.Column("watch_id", sa.Text, primary_key=True),
    sa.Column("ws_id", sa.Text, nullable=False),
    sa.Column("node_id", sa.Text, nullable=False, server_default=""),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("command", sa.Text, nullable=False),
    sa.Column("interval_secs", sa.Float, nullable=False),
    sa.Column("stop_on", sa.Text),  # Python expression, NULL = change detection
    sa.Column("max_polls", sa.Integer, nullable=False, server_default="100"),
    sa.Column("poll_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("last_output", sa.Text),
    sa.Column("last_exit_code", sa.Integer),
    sa.Column("last_poll", sa.Text),  # ISO8601
    sa.Column("next_poll", sa.Text),  # ISO8601
    sa.Column("active", sa.Integer, nullable=False, server_default="1"),
    sa.Column("created_by", sa.Text, nullable=False, server_default=""),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
)

sa.Index("idx_watches_active_next", watches.c.active, watches.c.next_poll)
sa.Index("idx_watches_ws_id", watches.c.ws_id)
sa.Index("idx_watches_node_id", watches.c.node_id)

# ---------------------------------------------------------------------------
# Service registry
# ---------------------------------------------------------------------------

services = sa.Table(
    "services",
    metadata,
    sa.Column("service_type", sa.Text, nullable=False),
    sa.Column("service_id", sa.Text, nullable=False),
    sa.Column("url", sa.Text, nullable=False),
    sa.Column("metadata", sa.Text, nullable=False, server_default="{}"),
    sa.Column("last_heartbeat", sa.Text, nullable=False),
    sa.Column("created", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("service_type", "service_id"),
)

sa.Index("idx_services_type_heartbeat", services.c.service_type, services.c.last_heartbeat)


# -- Postgres NOTIFY trigger on services -----------------------------------
#
# Producer side of the ``services`` channel that the console
# ``NotifyDispatcher`` listens on for reactive node discovery.  Fires on
# real registry changes (INSERT, DELETE, UPDATE that changes ``url`` or
# ``metadata``) and stays quiet on heartbeat-only UPDATEs so the 30s × N
# nodes heartbeat tick doesn't flood the channel.
#
# Declared in the schema (not just in migration 053) so the ``after_create``
# DDL event installs the trigger any time ``metadata.create_all`` builds
# the ``services`` table — covering fresh dev databases and the test
# fixture path (``run_migrations=False``).  Migration 053 covers the
# upgrade-on-existing-DB path; the two are mutually exclusive given the
# ``create_tables = not run_migrations`` switch in ``init_storage``, so
# neither double-installs.  SQLite has no equivalent — the in-process
# notify fan-out and synthetic-sweep covers the dev path consumer-side.

SERVICES_NOTIFY_TRIGGER_FN_NAME = "turnstone_notify_services"
SERVICES_NOTIFY_TRIGGER_NAME = "services_notify"

SERVICES_NOTIFY_TRIGGER_FN_SQL = f"""
CREATE OR REPLACE FUNCTION {SERVICES_NOTIFY_TRIGGER_FN_NAME}() RETURNS trigger AS $$
BEGIN
    -- Skip heartbeat-only UPDATEs: same url and metadata, only
    -- ``last_heartbeat`` changed.  ``register_service`` is an UPSERT
    -- (on_conflict_do_update), so node restarts that change url or
    -- metadata MUST still fire — only no-op heartbeat ticks stay
    -- quiet.  IS NOT DISTINCT FROM treats NULLs as equal so a row
    -- with NULL metadata before/after doesn't trip the diff.
    IF TG_OP = 'UPDATE'
       AND OLD.url IS NOT DISTINCT FROM NEW.url
       AND OLD.metadata IS NOT DISTINCT FROM NEW.metadata THEN
        RETURN NULL;
    END IF;

    PERFORM pg_notify(
        'services',
        json_build_object(
            'service_type', COALESCE(NEW.service_type, OLD.service_type),
            'service_id',   COALESCE(NEW.service_id,   OLD.service_id),
            'op',           TG_OP
        )::text
    );
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""

SERVICES_NOTIFY_TRIGGER_SQL = f"""
CREATE TRIGGER {SERVICES_NOTIFY_TRIGGER_NAME}
AFTER INSERT OR UPDATE OR DELETE ON services
FOR EACH ROW EXECUTE FUNCTION {SERVICES_NOTIFY_TRIGGER_FN_NAME}();
"""

sa.event.listen(
    services,
    "after_create",
    sa.DDL(SERVICES_NOTIFY_TRIGGER_FN_SQL).execute_if(  # type: ignore[no-untyped-call]
        dialect="postgresql"
    ),
)
sa.event.listen(
    services,
    "after_create",
    sa.DDL(SERVICES_NOTIFY_TRIGGER_SQL).execute_if(  # type: ignore[no-untyped-call]
        dialect="postgresql"
    ),
)

# ---------------------------------------------------------------------------
# Node metadata (per-node key/value with source tracking)
# ---------------------------------------------------------------------------

node_metadata = sa.Table(
    "node_metadata",
    metadata,
    sa.Column("node_id", sa.Text, nullable=False),
    sa.Column("key", sa.Text, nullable=False),
    sa.Column("value", sa.Text, nullable=False),
    sa.Column("source", sa.Text, nullable=False, server_default="user"),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("node_id", "key"),
)

sa.Index("idx_node_metadata_key", node_metadata.c.key)

# ---------------------------------------------------------------------------
# Routing — per-workstream pinning overrides
# ---------------------------------------------------------------------------

workstream_overrides = sa.Table(
    "workstream_overrides",
    metadata,
    sa.Column("ws_id", sa.Text, primary_key=True),
    sa.Column("node_id", sa.Text, nullable=False),
    sa.Column("reason", sa.Text, nullable=False, server_default="targeted"),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
)

sa.Index("idx_ws_overrides_node", workstream_overrides.c.node_id)

# ---------------------------------------------------------------------------
# Governance tables — RBAC, orgs, policies, skills, usage, audit
# ---------------------------------------------------------------------------

orgs = sa.Table(
    "orgs",
    metadata,
    sa.Column("org_id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False, unique=True),
    sa.Column("display_name", sa.Text, nullable=False),
    sa.Column("settings", sa.Text, nullable=False, server_default="{}"),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
)

roles = sa.Table(
    "roles",
    metadata,
    sa.Column("role_id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False, unique=True),
    sa.Column("display_name", sa.Text, nullable=False),
    sa.Column("permissions", sa.Text, nullable=False),  # comma-separated
    sa.Column("builtin", sa.Integer, nullable=False, server_default="0"),
    sa.Column("org_id", sa.Text, nullable=False, server_default=""),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
)

user_roles = sa.Table(
    "user_roles",
    metadata,
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("role_id", sa.Text, nullable=False),
    sa.Column("assigned_by", sa.Text, nullable=False, server_default=""),
    sa.Column("created", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("user_id", "role_id"),
)

sa.Index("idx_user_roles_role_id", user_roles.c.role_id)

role_permission_overrides = sa.Table(
    "role_permission_overrides",
    metadata,
    sa.Column("role_id", sa.Text, nullable=False),
    sa.Column("permission", sa.Text, nullable=False),
    sa.Column("action", sa.Text, nullable=False),  # 'grant' | 'revoke'
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("created_by", sa.Text, nullable=False, server_default=""),
    sa.PrimaryKeyConstraint("role_id", "permission"),
)

sa.Index("idx_role_permission_overrides_role", role_permission_overrides.c.role_id)

tool_policies = sa.Table(
    "tool_policies",
    metadata,
    sa.Column("policy_id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("tool_pattern", sa.Text, nullable=False),
    sa.Column("action", sa.Text, nullable=False),  # allow / deny / ask
    sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
    sa.Column("org_id", sa.Text, nullable=False, server_default=""),
    sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
    sa.Column("created_by", sa.Text, nullable=False, server_default=""),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
)

sa.Index("idx_tool_policies_priority", tool_policies.c.priority.desc())
sa.Index("idx_tool_policies_org", tool_policies.c.org_id)

prompt_templates = sa.Table(
    "prompt_templates",
    metadata,
    sa.Column("template_id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False, unique=True),
    sa.Column("category", sa.Text, nullable=False, server_default="general"),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("variables", sa.Text, nullable=False, server_default="[]"),  # JSON array
    sa.Column("is_default", sa.Integer, nullable=False, server_default="0"),
    sa.Column("org_id", sa.Text, nullable=False, server_default=""),
    sa.Column("created_by", sa.Text, nullable=False, server_default=""),
    sa.Column("origin", sa.Text, nullable=False, server_default="manual"),
    sa.Column("mcp_server", sa.Text, nullable=False, server_default=""),
    sa.Column("readonly", sa.Integer, nullable=False, server_default="0"),
    sa.Column("description", sa.Text, nullable=False, server_default=""),
    sa.Column("tags", sa.Text, nullable=False, server_default="[]"),
    sa.Column("source_url", sa.Text, nullable=False, server_default=""),
    sa.Column("version", sa.Text, nullable=False, server_default="1.0.0"),
    sa.Column("author", sa.Text, nullable=False, server_default=""),
    sa.Column("activation", sa.Text, nullable=False, server_default="named"),
    # SKILL.md spec ``user-invocable: false`` — hide from /-menu picker.
    sa.Column("hidden_from_menu", sa.Integer, nullable=False, server_default="0"),
    sa.Column("token_estimate", sa.Integer, nullable=False, server_default="0"),
    sa.Column("allowed_tools", sa.Text, nullable=False, server_default="[]"),  # JSON array
    # SKILL.md spec ``paths:`` — glob patterns gating autoload.
    # Consumer (filter logic) lands in a follow-up PR; field is
    # parsed/stored/editable but not yet acted on.
    sa.Column("paths", sa.Text, nullable=False, server_default="[]"),  # JSON array
    # SKILL.md spec ``arguments:`` + ``argument-hint:`` —
    # named positional slots and autocomplete display string.  Consumed
    # by the $N / $<name> substitution PR (issue #572).
    sa.Column("arguments", sa.Text, nullable=False, server_default="[]"),  # JSON array
    sa.Column("argument_hint", sa.Text, nullable=False, server_default=""),
    sa.Column("license", sa.Text, nullable=False, server_default=""),
    sa.Column("compatibility", sa.Text, nullable=False, server_default=""),
    # interactive / coordinator / any — governs which list_skills call
    # sees this row.  Defaults to "any" so existing schemas keep working.
    sa.Column("kind", sa.Text, nullable=False, server_default="any"),
    sa.Column("risk_level", sa.Text, nullable=False, server_default=""),
    sa.Column("scan_report", sa.Text, nullable=False, server_default="{}"),  # JSON
    sa.Column("installed_at", sa.Text, nullable=False, server_default=""),
    sa.Column("installed_by", sa.Text, nullable=False, server_default=""),
    sa.Column("scan_version", sa.Text, nullable=False, server_default=""),
    # Session config (merged from workstream templates)
    sa.Column("model", sa.Text, nullable=False, server_default=""),
    sa.Column("auto_approve", sa.Integer, nullable=False, server_default="0"),
    sa.Column("temperature", sa.Float, nullable=True),
    sa.Column("reasoning_effort", sa.Text, nullable=False, server_default=""),
    sa.Column("max_tokens", sa.Integer, nullable=True),
    sa.Column("token_budget", sa.Integer, nullable=False, server_default="0"),
    sa.Column("agent_max_turns", sa.Integer, nullable=True),
    sa.Column("notify_on_complete", sa.Text, nullable=False, server_default="[]"),
    sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
    sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
)

# ---------------------------------------------------------------------------
# Skill resources — bundled files (scripts/, references/, assets/)
# ---------------------------------------------------------------------------

skill_resources = sa.Table(
    "skill_resources",
    metadata,
    sa.Column("resource_id", sa.Text, primary_key=True),
    sa.Column("skill_id", sa.Text, nullable=False),  # prompt_templates.template_id
    sa.Column("path", sa.Text, nullable=False),  # e.g. "scripts/search.py"
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("content_type", sa.Text, nullable=False, server_default="text/plain"),
    sa.Column("created", sa.Text, nullable=False),
)

sa.Index("idx_skill_resources_skill_id", skill_resources.c.skill_id)
sa.Index(
    "idx_skill_resources_skill_path",
    skill_resources.c.skill_id,
    skill_resources.c.path,
    unique=True,
)

# ---------------------------------------------------------------------------
# Workstream attachments — content-addressed, refcounted blob store.
#
# In the content-addressed model the primary key IS the content hash
# (sha256 hex): identical bytes dedupe to one row regardless of how many
# messages reference them.  The store is GLOBAL — identical bytes dedupe
# across workstreams and users, so there are no ``ws_id`` / ``user_id`` scope
# columns; a committed blob is authorised by proving the (already ws-gated)
# requester has a turn in that workstream whose ref-list names the id (see
# ``attachment_referenced_in_ws``).  A blob is written only at send-commit (or
# when a tool produces an image), so every stored row is born referenced
# (``refcount >= 1``); GC decrements ``refcount`` as referencing messages are
# deleted and prunes the row at 0.  Pending (uploaded-but-unsent) bytes live
# in the per-node in-memory buffer (``attachment_buffer``), NOT here — the
# persisted pending/reserved/consumed lifecycle (message_id / reserved_* and
# its orphan-sweep) was retired by the content-addressing cutover.  The
# message->blob link is the ordered ``conversations.attachments`` ref-list.
# ---------------------------------------------------------------------------

workstream_attachments = sa.Table(
    "workstream_attachments",
    metadata,
    # PK is the content hash (sha256 hex) — content-addressed dedup.
    sa.Column("attachment_id", sa.Text, primary_key=True),
    sa.Column("filename", sa.Text, nullable=False),
    sa.Column("mime_type", sa.Text, nullable=False),
    sa.Column("size_bytes", sa.Integer, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),  # 'image' | 'text'
    sa.Column("content", sa.LargeBinary, nullable=False),
    sa.Column("created", sa.Text, nullable=False),
    # A deduped blob's live-reference count (pruned at 0) and its origin
    # ('upload' | 'tool').  The sole message->blob link is the ordered
    # ``conversations.attachments`` ref-list, not a column here.
    sa.Column("refcount", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("origin", sa.Text, nullable=False, server_default=sa.text("'upload'")),
)

# ---------------------------------------------------------------------------
# Skill versions — version history for skills
# ---------------------------------------------------------------------------

skill_versions = sa.Table(
    "skill_versions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("skill_id", sa.Text, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("snapshot", sa.Text, nullable=False),
    sa.Column("changed_by", sa.Text, nullable=False, server_default=""),
    sa.Column("created", sa.Text, nullable=False),
)

sa.Index("idx_skill_versions_skill_id", skill_versions.c.skill_id)

usage_events = sa.Table(
    "usage_events",
    metadata,
    sa.Column("event_id", sa.Text, primary_key=True),
    sa.Column("timestamp", sa.Text, nullable=False),
    sa.Column("user_id", sa.Text, nullable=False, server_default=""),
    sa.Column("ws_id", sa.Text, nullable=False, server_default=""),
    sa.Column("node_id", sa.Text, nullable=False, server_default=""),
    sa.Column("model", sa.Text, nullable=False, server_default=""),
    sa.Column("prompt_tokens", sa.Integer, nullable=False, server_default="0"),
    sa.Column("completion_tokens", sa.Integer, nullable=False, server_default="0"),
    sa.Column("tool_calls_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("cache_creation_tokens", sa.Integer, nullable=False, server_default="0"),
    sa.Column("cache_read_tokens", sa.Integer, nullable=False, server_default="0"),
    sa.Column("created", sa.Text, nullable=False),
)

sa.Index("idx_usage_events_timestamp", usage_events.c.timestamp)
sa.Index("idx_usage_events_user", usage_events.c.user_id, usage_events.c.timestamp)
sa.Index("idx_usage_events_model", usage_events.c.model, usage_events.c.timestamp)
sa.Index("idx_usage_events_ws", usage_events.c.ws_id)

audit_events = sa.Table(
    "audit_events",
    metadata,
    sa.Column("event_id", sa.Text, primary_key=True),
    sa.Column("timestamp", sa.Text, nullable=False),
    sa.Column("user_id", sa.Text, nullable=False, server_default=""),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("resource_type", sa.Text, nullable=False, server_default=""),
    sa.Column("resource_id", sa.Text, nullable=False, server_default=""),
    sa.Column("detail", sa.Text, nullable=False, server_default="{}"),
    sa.Column("ip_address", sa.Text, nullable=False, server_default=""),
    sa.Column("created", sa.Text, nullable=False),
)

sa.Index("idx_audit_timestamp", audit_events.c.timestamp)
sa.Index("idx_audit_action", audit_events.c.action)
sa.Index("idx_audit_user", audit_events.c.user_id)

# ---------------------------------------------------------------------------
# Intent verdicts — LLM judge verdicts for tool call validation
# ---------------------------------------------------------------------------

intent_verdicts = sa.Table(
    "intent_verdicts",
    metadata,
    sa.Column("verdict_id", sa.Text, primary_key=True),
    sa.Column("ws_id", sa.Text, nullable=False),
    sa.Column("call_id", sa.Text, nullable=False),
    sa.Column("func_name", sa.Text, nullable=False),
    sa.Column("func_args", sa.Text, nullable=False, server_default=""),
    sa.Column("intent_summary", sa.Text, nullable=False),
    sa.Column("risk_level", sa.Text, nullable=False),
    sa.Column("confidence", sa.Float, nullable=False),
    sa.Column("recommendation", sa.Text, nullable=False),
    sa.Column("reasoning", sa.Text, nullable=False),
    sa.Column("evidence", sa.Text, nullable=False, server_default="[]"),
    sa.Column("tier", sa.Text, nullable=False),
    sa.Column("judge_model", sa.Text, nullable=False, server_default=""),
    sa.Column("user_decision", sa.Text, nullable=False, server_default=""),
    sa.Column("latency_ms", sa.Integer, nullable=False, server_default="0"),
    sa.Column("created", sa.Text, nullable=False),
)

sa.Index("idx_intent_verdicts_ws", intent_verdicts.c.ws_id)
sa.Index("idx_intent_verdicts_created", intent_verdicts.c.created)
sa.Index("idx_intent_verdicts_risk", intent_verdicts.c.risk_level)

# ---------------------------------------------------------------------------
# Output assessments — output guard assessment persistence
# ---------------------------------------------------------------------------

output_assessments = sa.Table(
    "output_assessments",
    metadata,
    sa.Column("assessment_id", sa.Text, primary_key=True),
    sa.Column("ws_id", sa.Text, nullable=False),
    sa.Column("call_id", sa.Text, nullable=False),
    sa.Column("func_name", sa.Text, nullable=False),
    sa.Column("flags", sa.Text, nullable=False, server_default="[]"),
    sa.Column("risk_level", sa.Text, nullable=False, server_default="none"),
    sa.Column("annotations", sa.Text, nullable=False, server_default="[]"),
    sa.Column("output_length", sa.Integer, nullable=False, server_default="0"),
    sa.Column("redacted", sa.Integer, nullable=False, server_default="0"),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("tier", sa.Text, nullable=False, server_default="heuristic"),
    sa.Column("reasoning", sa.Text, nullable=False, server_default=""),
    sa.Column("judge_model", sa.Text, nullable=False, server_default=""),
    sa.Column("latency_ms", sa.Integer, nullable=False, server_default="0"),
    sa.Column("confidence", sa.Float, nullable=False, server_default="0.0"),
)

sa.Index("ix_oa_ws_id", output_assessments.c.ws_id)
sa.Index("ix_oa_created", output_assessments.c.created)
sa.Index("ix_oa_risk", output_assessments.c.risk_level)

# ---------------------------------------------------------------------------
# System settings — database-backed configuration
# ---------------------------------------------------------------------------

system_settings = sa.Table(
    "system_settings",
    metadata,
    sa.Column("key", sa.Text, nullable=False),
    sa.Column("value", sa.Text, nullable=False),
    sa.Column("node_id", sa.Text, nullable=False, server_default=""),
    sa.Column("is_secret", sa.Integer, nullable=False, server_default="0"),
    sa.Column("changed_by", sa.Text, nullable=False, server_default=""),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("key", "node_id"),
)

sa.Index("idx_system_settings_node", system_settings.c.node_id)

# ---------------------------------------------------------------------------
# MCP server definitions — database-backed MCP configuration
# ---------------------------------------------------------------------------

mcp_servers = sa.Table(
    "mcp_servers",
    metadata,
    sa.Column("server_id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False, unique=True),
    sa.Column("transport", sa.Text, nullable=False),  # "stdio" | "streamable-http"
    sa.Column("command", sa.Text, nullable=False, server_default=""),
    sa.Column("args", sa.Text, nullable=False, server_default="[]"),  # JSON array
    sa.Column("url", sa.Text, nullable=False, server_default=""),
    sa.Column("headers", sa.Text, nullable=False, server_default="{}"),  # JSON object
    sa.Column("env", sa.Text, nullable=False, server_default="{}"),  # JSON object
    sa.Column("auto_approve", sa.Integer, nullable=False, server_default="0"),
    sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
    sa.Column("created_by", sa.Text, nullable=False, server_default=""),
    sa.Column("registry_name", sa.Text, nullable=True),
    sa.Column("registry_version", sa.Text, nullable=False, server_default=""),
    sa.Column("registry_meta", sa.Text, nullable=False, server_default="{}"),
    # Per-(user, server) OAuth 2.1 columns.
    # `auth_type` is one of: 'none', 'static', 'oauth_user'.  The other
    # `oauth_*` columns are NULL when auth_type != 'oauth_user'.
    # `oauth_client_secret_ct` is Fernet ciphertext; never decrypted on
    # the read path (write-only field, masked as "***" in responses).
    sa.Column("auth_type", sa.Text, nullable=False, server_default="static"),
    sa.Column("oauth_client_id", sa.Text, nullable=True),
    sa.Column("oauth_client_secret_ct", sa.LargeBinary, nullable=True),
    sa.Column("oauth_scopes", sa.Text, nullable=True),
    sa.Column("oauth_audience", sa.Text, nullable=True),
    sa.Column("oauth_registration_mode", sa.Text, nullable=True),
    sa.Column("oauth_authorization_server_url", sa.Text, nullable=True),
    sa.Column("oauth_as_issuer_cached", sa.Text, nullable=True),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
)

sa.Index("idx_mcp_servers_enabled", mcp_servers.c.enabled)
sa.Index(
    "idx_mcp_servers_registry_name",
    mcp_servers.c.registry_name,
    unique=True,
    sqlite_where=mcp_servers.c.registry_name.isnot(None),
    postgresql_where=mcp_servers.c.registry_name.isnot(None),
)

# ---------------------------------------------------------------------------
# Model definitions — database-backed model configuration
# ---------------------------------------------------------------------------

model_definitions = sa.Table(
    "model_definitions",
    metadata,
    sa.Column("definition_id", sa.Text, primary_key=True),
    sa.Column("alias", sa.Text, nullable=False, unique=True),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column("provider", sa.Text, nullable=False, server_default="openai"),
    sa.Column("base_url", sa.Text, nullable=False, server_default=""),
    sa.Column("api_key", sa.Text, nullable=False, server_default=""),
    sa.Column("context_window", sa.Integer, nullable=False, server_default="32768"),
    sa.Column("capabilities", sa.Text, nullable=False, server_default="{}"),
    sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
    sa.Column("temperature", sa.Float, nullable=True),
    sa.Column("max_tokens", sa.Integer, nullable=True),
    sa.Column("reasoning_effort", sa.Text, nullable=True),
    sa.Column("surface_persisted_reasoning", sa.Integer, nullable=False, server_default="1"),
    sa.Column("replay_reasoning_to_model", sa.Integer, nullable=False, server_default="0"),
    sa.Column("created_by", sa.Text, nullable=False, server_default=""),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
)

sa.Index("idx_model_definitions_enabled", model_definitions.c.enabled)

# ---------------------------------------------------------------------------
# Prompt policies — system message behavioral rules (admin-managed)
# ---------------------------------------------------------------------------

prompt_policies = sa.Table(
    "prompt_policies",
    metadata,
    sa.Column("policy_id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False, unique=True),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("tool_gate", sa.Text, nullable=False, server_default=""),
    sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
    sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
    sa.Column("org_id", sa.Text, nullable=False, server_default=""),
    sa.Column("created_by", sa.Text, nullable=False, server_default=""),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
)

# ---------------------------------------------------------------------------
# Personas — named capability/prompt bundles stamped onto workstreams at
# creation (migration 063)
# ---------------------------------------------------------------------------
# A persona controls system-message composition and the capability envelope
# via four levers: base-prompt override, tool visibility set, MCP on/off,
# memory toggle.  Workstreams snapshot the persona into workstream_config at
# creation; this table is a template shelf, never read post-create.

personas = sa.Table(
    "personas",
    metadata,
    sa.Column("persona_id", sa.Text, primary_key=True),
    # name: stable slug used on create requests (`persona=scribe`); unique.
    sa.Column("name", sa.Text, nullable=False, unique=True),
    sa.Column("display_name", sa.Text, nullable=False, server_default=""),
    sa.Column("description", sa.Text, nullable=False, server_default=""),
    # base_prompt: replaces the BASE module in compose_system_message().
    # NULL = use the kind's stock base (base.md / base_coordinator.md).
    sa.Column("base_prompt", sa.Text, nullable=True),
    # tool_allowlist: JSON, tri-state — NULL = unrestricted (tracks tool
    # growth + MCP dynamics), "[]" = hard empty, '["name", ...]' = exact
    # visibility set (tool_search membership decides soft vs hard).
    sa.Column("tool_allowlist", sa.Text, nullable=True),
    sa.Column("mcp_enabled", sa.Integer, nullable=False, server_default="1"),
    sa.Column("memory_enabled", sa.Integer, nullable=False, server_default="1"),
    # applies_to_kinds: JSON list, subset of ["interactive", "coordinator"].
    sa.Column("applies_to_kinds", sa.Text, nullable=False, server_default='["interactive"]'),
    # is_default: exactly one per kind (storage-enforced); defaults are
    # un-archivable.  The default is what an empty `persona=` resolves to.
    sa.Column("is_default", sa.Integer, nullable=False, server_default="0"),
    # enabled: archive = 0.  No hard delete — stamped workstreams stay
    # explicable forever.
    sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
    sa.Column("org_id", sa.Text, nullable=False, server_default=""),
    sa.Column("created_by", sa.Text, nullable=False, server_default=""),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
)

sa.Index("idx_personas_enabled", personas.c.enabled)

# ---------------------------------------------------------------------------
# OIDC identity tables
# ---------------------------------------------------------------------------

oidc_identities = sa.Table(
    "oidc_identities",
    metadata,
    sa.Column("issuer", sa.Text, nullable=False),
    sa.Column("subject", sa.Text, nullable=False),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("email", sa.Text, nullable=False, server_default=""),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("last_login", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("issuer", "subject"),
)

sa.Index("idx_oidc_identities_user_id", oidc_identities.c.user_id)

oidc_pending_states = sa.Table(
    "oidc_pending_states",
    metadata,
    sa.Column("state", sa.Text, primary_key=True),
    sa.Column("nonce", sa.Text, nullable=False),
    sa.Column("code_verifier", sa.Text, nullable=False),
    sa.Column("audience", sa.Text, nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
)

# ---------------------------------------------------------------------------
# MCP per-(user, server) OAuth tokens and pending authorization-flow state.
# No FKs at the schema level (matches `oidc_*` tables; tests avoid orphan
# rows via fixtures).
# ---------------------------------------------------------------------------

mcp_user_tokens = sa.Table(
    "mcp_user_tokens",
    metadata,
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("server_name", sa.Text, nullable=False),
    sa.Column("access_token_ct", sa.LargeBinary, nullable=False),
    sa.Column("refresh_token_ct", sa.LargeBinary, nullable=True),
    sa.Column("expires_at", sa.Text, nullable=True),
    sa.Column("scopes", sa.Text, nullable=True),
    sa.Column("as_issuer", sa.Text, nullable=False),
    sa.Column("audience", sa.Text, nullable=False),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("last_refreshed", sa.Text, nullable=True),
    sa.PrimaryKeyConstraint("user_id", "server_name"),
)
# Phase 9: covers the ``WHERE server_name = ? AND (expires_at IS NULL
# OR expires_at > now)`` shape used by ``count_mcp_consented_users_*``
# for the admin status pill.  The composite PK can't satisfy filters
# that don't lead with ``user_id``.
sa.Index(
    "idx_mcp_user_tokens_server",
    mcp_user_tokens.c.server_name,
    mcp_user_tokens.c.expires_at,
)

mcp_oauth_pending = sa.Table(
    "mcp_oauth_pending",
    metadata,
    sa.Column("state", sa.Text, primary_key=True),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("server_name", sa.Text, nullable=False),
    sa.Column("code_verifier", sa.Text, nullable=False),
    sa.Column("return_url", sa.Text, nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
)

sa.Index("idx_mcp_pending_created", mcp_oauth_pending.c.created_at)

# Per-(user, server) pending-consent state for non-interactive contexts.
# Populated by the pool dispatchers when a scheduled / channel-driven run
# hits ``mcp_consent_required`` or ``mcp_insufficient_scope`` and the user
# can't be prompted in the moment.  Read on dashboard load to render the
# "N MCP servers need consent" badge.  Cleared by the OAuth callback when
# the matching ``(user, server)`` completes consent.
#
# Composite PK ``(user_id, server_name)`` collapses repeat occurrences for
# the same server into one row; ``occurrence_count`` + ``last_*`` fields
# carry recency metadata for the dashboard without inflating row count.
mcp_pending_consent = sa.Table(
    "mcp_pending_consent",
    metadata,
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("server_name", sa.Text, nullable=False),
    sa.Column("error_code", sa.Text, nullable=False),
    sa.Column("scopes_required", sa.Text, nullable=True),
    sa.Column("last_ws_id", sa.Text, nullable=True),
    sa.Column("last_tool_call_id", sa.Text, nullable=True),
    sa.Column("first_seen_at", sa.Text, nullable=False),
    sa.Column("last_seen_at", sa.Text, nullable=False),
    sa.Column("occurrence_count", sa.Integer, nullable=False, server_default="1"),
    sa.PrimaryKeyConstraint("user_id", "server_name"),
)
sa.Index("idx_mcp_pending_consent_user", mcp_pending_consent.c.user_id)

# ── TLS / ACME (lacme integration) ──────────────────────────────────────────

tls_account_keys = sa.Table(
    "tls_account_keys",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("key_pem", sa.Text, nullable=False),
    sa.Column("created", sa.Text, nullable=False),
)

tls_ca = sa.Table(
    "tls_ca",
    metadata,
    sa.Column("name", sa.Text, primary_key=True),
    sa.Column("cert_pem", sa.Text, nullable=False),
    sa.Column("key_pem", sa.Text, nullable=False),
    sa.Column("created", sa.Text, nullable=False),
)

tls_certificates = sa.Table(
    "tls_certificates",
    metadata,
    sa.Column("domain", sa.Text, primary_key=True),
    sa.Column("cert_pem", sa.Text, nullable=False),
    sa.Column("fullchain_pem", sa.Text, nullable=False),
    sa.Column("key_pem", sa.Text, nullable=False),
    sa.Column("issued_at", sa.Text, nullable=False),
    sa.Column("expires_at", sa.Text, nullable=False),
    sa.Column("meta", sa.Text, nullable=True),
)

# ---------------------------------------------------------------------------
# Heuristic rules — configurable intent validation patterns (admin-managed)
# ---------------------------------------------------------------------------

heuristic_rules = sa.Table(
    "heuristic_rules",
    metadata,
    sa.Column("rule_id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False, unique=True),
    sa.Column("risk_level", sa.Text, nullable=False),
    sa.Column("confidence", sa.Float, nullable=False),
    sa.Column("recommendation", sa.Text, nullable=False),
    sa.Column("tool_pattern", sa.Text, nullable=False),
    sa.Column("arg_patterns", sa.Text, nullable=False, server_default="[]"),
    sa.Column("intent_template", sa.Text, nullable=False),
    sa.Column("reasoning_template", sa.Text, nullable=False),
    sa.Column("tier", sa.Text, nullable=False),
    sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
    sa.Column("builtin", sa.Integer, nullable=False, server_default="0"),
    sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
    sa.Column("created_by", sa.Text, nullable=False, server_default=""),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
)

sa.Index("idx_heuristic_rules_enabled", heuristic_rules.c.enabled)
sa.Index("idx_heuristic_rules_tier", heuristic_rules.c.tier)

# ---------------------------------------------------------------------------
# Output guard patterns — configurable output scanning patterns (admin-managed)
# ---------------------------------------------------------------------------

output_guard_patterns = sa.Table(
    "output_guard_patterns",
    metadata,
    sa.Column("pattern_id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False, unique=True),
    sa.Column("category", sa.Text, nullable=False),
    sa.Column("risk_level", sa.Text, nullable=False),
    sa.Column("pattern", sa.Text, nullable=False),
    sa.Column("pattern_flags", sa.Text, nullable=False, server_default=""),
    sa.Column("flag_name", sa.Text, nullable=False),
    sa.Column("annotation", sa.Text, nullable=False),
    sa.Column("is_credential", sa.Integer, nullable=False, server_default="0"),
    sa.Column("redact_label", sa.Text, nullable=False, server_default=""),
    sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
    sa.Column("builtin", sa.Integer, nullable=False, server_default="0"),
    sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
    sa.Column("created_by", sa.Text, nullable=False, server_default=""),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
)

sa.Index("idx_ogp_enabled", output_guard_patterns.c.enabled)
sa.Index("idx_ogp_category", output_guard_patterns.c.category)
