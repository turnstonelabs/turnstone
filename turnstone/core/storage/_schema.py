"""SQLAlchemy Core schema — single source of truth for all table definitions.

Used by both storage backends and Alembic migrations.
"""

from __future__ import annotations

import sqlalchemy as sa

metadata = sa.MetaData()

memories = sa.Table(
    "memories",
    metadata,
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("value", sa.Text, nullable=False),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
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
    sa.Column("tool_args", sa.Text),
    sa.Column("tool_call_id", sa.Text),
    sa.Column("provider_data", sa.Text),
)

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
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
)

sa.Index("idx_workstreams_node_id", workstreams.c.node_id)
sa.Index("idx_workstreams_state", workstreams.c.state)
sa.Index("idx_workstreams_user_id", workstreams.c.user_id)
sa.Index("idx_workstreams_alias", workstreams.c.alias)

workstream_config = sa.Table(
    "workstream_config",
    metadata,
    sa.Column("ws_id", sa.Text, nullable=False),
    sa.Column("key", sa.Text, nullable=False),
    sa.Column("value", sa.Text),
    sa.PrimaryKeyConstraint("ws_id", "key"),
)

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

# ---------------------------------------------------------------------------
# Governance tables — RBAC, orgs, policies, templates, usage, audit
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
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
)

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
