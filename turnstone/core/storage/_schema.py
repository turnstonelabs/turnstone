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
    sa.Column("type", sa.Text, nullable=False, server_default="project"),
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
)

sa.Index("idx_conversations_timestamp", conversations.c.timestamp)

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
    sa.Column("skill", sa.Text, nullable=False, server_default=""),
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
    sa.Column("token_estimate", sa.Integer, nullable=False, server_default="0"),
    sa.Column("allowed_tools", sa.Text, nullable=False, server_default="[]"),  # JSON array
    sa.Column("license", sa.Text, nullable=False, server_default=""),
    sa.Column("compatibility", sa.Text, nullable=False, server_default=""),
    sa.Column("scan_status", sa.Text, nullable=False, server_default=""),
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
    sa.Column("notify_on_complete", sa.Text, nullable=False, server_default="{}"),
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
