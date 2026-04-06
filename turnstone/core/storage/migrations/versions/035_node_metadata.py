"""Add node_metadata table for per-node key/value metadata.

Revision ID: 035
Revises: 034
Create Date: 2026-04-05
"""

import sqlalchemy as sa
from alembic import op

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "node_metadata",
        sa.Column("node_id", sa.Text, nullable=False),
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False, server_default="user"),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("node_id", "key"),
    )
    op.create_index("idx_node_metadata_key", "node_metadata", ["key"])

    # Grant admin.nodes permission to the built-in admin role
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = permissions || ',admin.nodes' "
            "WHERE role_id = 'builtin-admin' "
            "AND permissions NOT LIKE '%admin.nodes%'"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = REPLACE(permissions, ',admin.nodes', '') "
            "WHERE role_id = 'builtin-admin'"
        )
    )
    op.drop_index("idx_node_metadata_key", table_name="node_metadata")
    op.drop_table("node_metadata")
