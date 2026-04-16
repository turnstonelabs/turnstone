"""Add reserved_at column to workstream_attachments.

The orphan-reservation sweep (see ``sweep_orphan_reservations``) needs a
staleness signal that reflects *reservation* age, not *upload* age. Using
``created`` (upload time) as a proxy can incorrectly clear active
reservations for attachments that sit pending a long time before being
reserved — a real race when a user uploads a file, returns hours later,
then sends. ``reserved_at`` is set on ``reserve_attachments`` and cleared
on ``mark_attachments_consumed`` / ``unreserve_attachments``, so the sweep
can target only reservations that have actually been held longer than the
configured threshold.

A partial index on ``(reserved_at)`` keeps the periodic scan cheap as the
table grows; pending and consumed rows (NULL reserved_at) don't bloat it.

Revision ID: 038
Revises: 037
Create Date: 2026-04-16
"""

import sqlalchemy as sa
from alembic import op

revision = "038"
down_revision = "037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workstream_attachments",
        sa.Column("reserved_at", sa.Text, nullable=True),
    )
    # Partial index — only reserved rows participate, so the sweep scan
    # stays small even as the consumed-history grows.  SQLite supports
    # partial indexes with the same syntax as PostgreSQL.
    op.create_index(
        "idx_ws_attachments_reserved_at",
        "workstream_attachments",
        ["reserved_at"],
        postgresql_where=sa.text("reserved_at IS NOT NULL"),
        sqlite_where=sa.text("reserved_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_ws_attachments_reserved_at", table_name="workstream_attachments")
    op.drop_column("workstream_attachments", "reserved_at")
