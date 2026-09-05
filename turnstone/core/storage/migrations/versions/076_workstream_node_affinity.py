"""Persist explicit execution-node requirements.

Revision ID: 076
Revises: 075
Create Date: 2026-09-05
"""

import sqlalchemy as sa
from alembic import op

revision = "076"
down_revision = "075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workstreams") as batch_op:
        batch_op.add_column(sa.Column("required_node_id", sa.Text, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workstreams") as batch_op:
        batch_op.drop_column("required_node_id")
