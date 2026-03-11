"""Governance tables — RBAC roles, orgs, tool policies, prompt templates, usage, audit.

Revision ID: 008
Revises: 007
Create Date: 2026-03-10
"""

import sqlalchemy as sa
from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None

# Built-in roles seeded on upgrade
_ADMIN_PERMS = (
    "read,write,approve,admin.users,admin.roles,admin.orgs,"
    "admin.policies,admin.templates,admin.audit,admin.usage,"
    "admin.schedules,admin.watches,"
    "tools.approve,workstreams.create,workstreams.close"
)
_OPERATOR_PERMS = "read,write,workstreams.create,workstreams.close"
_VIEWER_PERMS = "read"


def upgrade() -> None:
    # -- Organizations ---------------------------------------------------------
    op.create_table(
        "orgs",
        sa.Column("org_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("settings", sa.Text, nullable=False, server_default="{}"),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )

    # -- Roles -----------------------------------------------------------------
    op.create_table(
        "roles",
        sa.Column("role_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("permissions", sa.Text, nullable=False),
        sa.Column("builtin", sa.Integer, nullable=False, server_default="0"),
        sa.Column("org_id", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )

    # -- User ↔ Role assignments -----------------------------------------------
    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("role_id", sa.Text, nullable=False),
        sa.Column("assigned_by", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("user_id", "role_id"),
    )
    op.create_index("idx_user_roles_role_id", "user_roles", ["role_id"])

    # -- Tool policies ---------------------------------------------------------
    op.create_table(
        "tool_policies",
        sa.Column("policy_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("tool_pattern", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column("org_id", sa.Text, nullable=False, server_default=""),
        sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_by", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )
    op.create_index("idx_tool_policies_priority", "tool_policies", [sa.text("priority DESC")])
    op.create_index("idx_tool_policies_org", "tool_policies", ["org_id"])

    # -- Prompt templates ------------------------------------------------------
    op.create_table(
        "prompt_templates",
        sa.Column("template_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("category", sa.Text, nullable=False, server_default="general"),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("variables", sa.Text, nullable=False, server_default="[]"),
        sa.Column("is_default", sa.Integer, nullable=False, server_default="0"),
        sa.Column("org_id", sa.Text, nullable=False, server_default=""),
        sa.Column("created_by", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )

    # -- Usage events ----------------------------------------------------------
    op.create_table(
        "usage_events",
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
    op.create_index("idx_usage_events_timestamp", "usage_events", ["timestamp"])
    op.create_index("idx_usage_events_user", "usage_events", ["user_id", "timestamp"])
    op.create_index("idx_usage_events_model", "usage_events", ["model", "timestamp"])
    op.create_index("idx_usage_events_ws", "usage_events", ["ws_id"])

    # -- Audit events ----------------------------------------------------------
    op.create_table(
        "audit_events",
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
    op.create_index("idx_audit_timestamp", "audit_events", ["timestamp"])
    op.create_index("idx_audit_action", "audit_events", ["action"])
    op.create_index("idx_audit_user", "audit_events", ["user_id"])

    # -- Add org_id to users ---------------------------------------------------
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("org_id", sa.Text, nullable=False, server_default=""))

    # -- Seed default org and built-in roles -----------------------------------
    conn = op.get_bind()
    import datetime

    now_str = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S")

    conn.execute(
        sa.text(
            "INSERT INTO orgs (org_id, name, display_name, settings, created, updated) "
            "VALUES (:oid, :name, :dname, '{}', :now, :now)"
        ),
        {"oid": "default", "name": "default", "dname": "Default", "now": now_str},
    )
    for role_id, name, dname, perms in [
        ("builtin-admin", "admin", "Admin", _ADMIN_PERMS),
        ("builtin-operator", "operator", "Operator", _OPERATOR_PERMS),
        ("builtin-viewer", "viewer", "Viewer", _VIEWER_PERMS),
    ]:
        conn.execute(
            sa.text(
                "INSERT INTO roles (role_id, name, display_name, permissions, builtin, org_id, created, updated) "
                "VALUES (:rid, :name, :dname, :perms, 1, '', :now, :now)"
            ),
            {"rid": role_id, "name": name, "dname": dname, "perms": perms, "now": now_str},
        )

    # Assign admin role to all existing users
    conn.execute(
        sa.text(
            "INSERT INTO user_roles (user_id, role_id, assigned_by, created) "
            "SELECT user_id, 'builtin-admin', '', :now FROM users"
        ),
        {"now": now_str},
    )


def downgrade() -> None:
    op.drop_index("idx_audit_user", "audit_events")
    op.drop_index("idx_audit_action", "audit_events")
    op.drop_index("idx_audit_timestamp", "audit_events")
    op.drop_table("audit_events")

    op.drop_index("idx_usage_events_ws", "usage_events")
    op.drop_index("idx_usage_events_model", "usage_events")
    op.drop_index("idx_usage_events_user", "usage_events")
    op.drop_index("idx_usage_events_timestamp", "usage_events")
    op.drop_table("usage_events")

    op.drop_table("prompt_templates")

    op.drop_index("idx_tool_policies_org", "tool_policies")
    op.drop_index("idx_tool_policies_priority", "tool_policies")
    op.drop_table("tool_policies")

    op.drop_index("idx_user_roles_role_id", "user_roles")
    op.drop_table("user_roles")
    op.drop_table("roles")
    op.drop_table("orgs")

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("org_id")
