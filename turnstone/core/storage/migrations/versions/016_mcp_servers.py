"""Create mcp_servers table and grant admin.mcp permission.

Revision ID: 016
Revises: 015
Create Date: 2026-03-14
"""

import sqlalchemy as sa
from alembic import op

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("server_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("transport", sa.Text, nullable=False),
        sa.Column("command", sa.Text, nullable=False, server_default=""),
        sa.Column("args", sa.Text, nullable=False, server_default="[]"),
        sa.Column("url", sa.Text, nullable=False, server_default=""),
        sa.Column("headers", sa.Text, nullable=False, server_default="{}"),
        sa.Column("env", sa.Text, nullable=False, server_default="{}"),
        sa.Column("auto_approve", sa.Integer, nullable=False, server_default="0"),
        sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_by", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )
    op.create_index("idx_mcp_servers_enabled", "mcp_servers", ["enabled"])

    # Grant admin.mcp permission to the built-in admin role
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = permissions || ',admin.mcp' "
            "WHERE role_id = 'builtin-admin' "
            "AND permissions NOT LIKE '%admin.mcp%'"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = REPLACE(permissions, ',admin.mcp', '') "
            "WHERE role_id = 'builtin-admin'"
        )
    )
    op.drop_table("mcp_servers")
