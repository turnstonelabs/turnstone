"""Create hash ring routing tables.

Revision ID: 030
Revises: 029
Create Date: 2026-03-30
"""

import sqlalchemy as sa
from alembic import op

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hash_ring_buckets",
        sa.Column("bucket", sa.Integer, primary_key=True),
        sa.Column("node_id", sa.Text, nullable=False),
    )
    op.create_index("idx_ring_buckets_node", "hash_ring_buckets", ["node_id"])

    op.create_table(
        "bucket_stats",
        sa.Column("bucket", sa.Integer, primary_key=True),
        sa.Column("ws_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("active_count", sa.Integer, nullable=False, server_default="0"),
    )

    op.create_table(
        "workstream_overrides",
        sa.Column("ws_id", sa.Text, primary_key=True),
        sa.Column("node_id", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False, server_default="targeted"),
        sa.Column("created", sa.Text, nullable=False),
    )
    op.create_index("idx_ws_overrides_node", "workstream_overrides", ["node_id"])


def downgrade() -> None:
    op.drop_table("workstream_overrides")
    op.drop_table("bucket_stats")
    op.drop_table("hash_ring_buckets")
