"""Create system_settings table for database-backed configuration.

Revision ID: 015
Revises: 014
Create Date: 2026-03-14
"""

import sqlalchemy as sa
from alembic import op

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_settings",
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("node_id", sa.Text, nullable=False, server_default=""),
        sa.Column("is_secret", sa.Integer, nullable=False, server_default="0"),
        sa.Column("changed_by", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("key", "node_id"),
    )
    op.create_index("idx_system_settings_node", "system_settings", ["node_id"])

    # Grant admin.settings permission to the built-in admin role
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = permissions || ',admin.settings' "
            "WHERE role_id = 'builtin-admin' "
            "AND permissions NOT LIKE '%admin.settings%'"
        )
    )


def downgrade() -> None:
    # Remove admin.settings permission from builtin-admin role
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = REPLACE(permissions, ',admin.settings', '') "
            "WHERE role_id = 'builtin-admin'"
        )
    )
    op.drop_table("system_settings")
