"""Channel routing table.

Revision ID: 003
Revises: 002
Create Date: 2026-03-04
"""

import sqlalchemy as sa
from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channel_routes",
        sa.Column("channel_type", sa.Text, nullable=False),
        sa.Column("channel_id", sa.Text, nullable=False),
        sa.Column("ws_id", sa.Text, nullable=False),
        sa.Column("node_id", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("channel_type", "channel_id"),
    )
    op.create_index("idx_channel_routes_ws", "channel_routes", ["ws_id"])


def downgrade() -> None:
    op.drop_index("idx_channel_routes_ws", "channel_routes")
    op.drop_table("channel_routes")
