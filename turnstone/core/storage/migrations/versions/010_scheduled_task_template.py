"""Add template column to scheduled_tasks.

Revision ID: 010
Revises: 009
Create Date: 2026-03-12
"""

import sqlalchemy as sa
from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.add_column(sa.Column("template", sa.Text, nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("template")
