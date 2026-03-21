"""Add index on conversations.timestamp for search_history_recent.

The ``search_history_recent`` query orders by ``timestamp DESC`` without
an index, causing a full table scan.  This migration adds the missing
index to match the schema definition in ``_schema.py``.

Revision ID: 025
Revises: 024
Create Date: 2026-03-21
"""

from alembic import op

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_conversations_timestamp",
        "conversations",
        ["timestamp"],
    )


def downgrade() -> None:
    op.drop_index("idx_conversations_timestamp", table_name="conversations")
