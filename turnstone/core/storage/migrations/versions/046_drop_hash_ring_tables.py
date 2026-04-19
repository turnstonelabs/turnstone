"""Drop hash ring bucket tables.

Routing is now rendezvous (HRW) hashing over the live ``services``
table; ``hash_ring_buckets`` and ``bucket_stats`` are no longer read
or written.  ``workstream_overrides`` stays — manual per-ws pinning
still takes priority over the rendezvous select.

Also clears the ``rebalancer_version`` and ``rebalancer_lock`` rows in
``system_settings`` so they don't linger as orphan keys.

Revision ID: 046
Revises: 045
Create Date: 2026-04-18
"""

import sqlalchemy as sa
from alembic import op

revision = "046"
down_revision = "045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("idx_ring_buckets_node", table_name="hash_ring_buckets")
    op.drop_table("hash_ring_buckets")
    op.drop_table("bucket_stats")
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM system_settings WHERE key IN ('rebalancer_version', 'rebalancer_lock')"
        )
    )


def downgrade() -> None:
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
