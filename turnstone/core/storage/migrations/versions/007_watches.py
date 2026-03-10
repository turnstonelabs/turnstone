"""Watches table for in-session periodic command polling.

Revision ID: 007
Revises: 006
Create Date: 2026-03-09
"""

import sqlalchemy as sa
from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "watches",
        sa.Column("watch_id", sa.Text, primary_key=True),
        sa.Column("ws_id", sa.Text, nullable=False),
        sa.Column("node_id", sa.Text, nullable=False, server_default=""),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("command", sa.Text, nullable=False),
        sa.Column("interval_secs", sa.Float, nullable=False),
        sa.Column("stop_on", sa.Text),
        sa.Column("max_polls", sa.Integer, nullable=False, server_default="100"),
        sa.Column("poll_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_output", sa.Text),
        sa.Column("last_exit_code", sa.Integer),
        sa.Column("last_poll", sa.Text),
        sa.Column("next_poll", sa.Text),
        sa.Column("active", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_by", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )
    op.create_index("idx_watches_active_next", "watches", ["active", "next_poll"])
    op.create_index("idx_watches_ws_id", "watches", ["ws_id"])
    op.create_index("idx_watches_node_id", "watches", ["node_id"])


def downgrade() -> None:
    op.drop_index("idx_watches_node_id", "watches")
    op.drop_index("idx_watches_ws_id", "watches")
    op.drop_index("idx_watches_active_next", "watches")
    op.drop_table("watches")
