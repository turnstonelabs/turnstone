"""Create workstream_templates and workstream_template_versions tables.

Revision ID: 011
Revises: 010
Create Date: 2026-03-12
"""

import sqlalchemy as sa
from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workstream_templates",
        sa.Column("ws_template_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("system_prompt", sa.Text, nullable=False, server_default=""),
        sa.Column("prompt_template", sa.Text, nullable=False, server_default=""),
        sa.Column("prompt_template_hash", sa.Text, nullable=False, server_default=""),
        sa.Column("model", sa.Text, nullable=False, server_default=""),
        sa.Column("auto_approve", sa.Integer, nullable=False, server_default="0"),
        sa.Column("auto_approve_tools", sa.Text, nullable=False, server_default=""),
        sa.Column("temperature", sa.Float),
        sa.Column("reasoning_effort", sa.Text, nullable=False, server_default=""),
        sa.Column("max_tokens", sa.Integer),
        sa.Column("token_budget", sa.Integer, nullable=False, server_default="0"),
        sa.Column("agent_max_turns", sa.Integer),
        sa.Column("notify_on_complete", sa.Text, nullable=False, server_default="{}"),
        sa.Column("org_id", sa.Text, nullable=False, server_default=""),
        sa.Column("created_by", sa.Text, nullable=False, server_default=""),
        sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )
    op.create_index("idx_ws_templates_enabled", "workstream_templates", ["enabled"])
    op.create_index("idx_ws_templates_org", "workstream_templates", ["org_id"])

    op.create_table(
        "workstream_template_versions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ws_template_id", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("snapshot", sa.Text, nullable=False),
        sa.Column("changed_by", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
    )
    op.create_index("idx_ws_tpl_versions_tpl", "workstream_template_versions", ["ws_template_id"])
    op.create_index(
        "uq_ws_tpl_versions_tpl_ver",
        "workstream_template_versions",
        ["ws_template_id", "version"],
        unique=True,
    )

    # Add ws_template tracking to workstreams table
    with op.batch_alter_table("workstreams") as batch_op:
        batch_op.add_column(sa.Column("ws_template_id", sa.Text, nullable=False, server_default=""))
        batch_op.add_column(
            sa.Column("ws_template_version", sa.Integer, nullable=False, server_default="0")
        )

    # Add ws_template to scheduled_tasks
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.add_column(sa.Column("ws_template", sa.Text, nullable=False, server_default=""))

    # Grant admin.ws_templates permission to the built-in admin role
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = permissions || ',admin.ws_templates' "
            "WHERE role_id = 'builtin-admin' "
            "AND permissions NOT LIKE '%admin.ws_templates%'"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("ws_template")
    with op.batch_alter_table("workstreams") as batch_op:
        batch_op.drop_column("ws_template_version")
        batch_op.drop_column("ws_template_id")
    op.drop_table("workstream_template_versions")
    op.drop_table("workstream_templates")
