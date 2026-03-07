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
