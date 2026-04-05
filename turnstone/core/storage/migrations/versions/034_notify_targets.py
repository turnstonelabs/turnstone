"""Add notify_targets column to scheduled_tasks.

Revision ID: 034
Revises: 033
Create Date: 2026-04-05
"""

import sqlalchemy as sa
from alembic import op

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scheduled_tasks",
        sa.Column("notify_targets", sa.Text, nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("scheduled_tasks", "notify_targets")
