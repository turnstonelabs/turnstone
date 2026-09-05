"""Persist the external user who initiated a channel conversation.

Legacy routes retain an empty owner; platform-proven ownership is still usable,
but a bot-owned Discord thread must not be claimed by an arbitrary linked user.

Revision ID: 075
Revises: 074
Create Date: 2026-09-05
"""

import sqlalchemy as sa
from alembic import op

revision = "075"
down_revision = "074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("channel_routes") as batch_op:
        batch_op.add_column(
            sa.Column("channel_user_id", sa.Text, nullable=False, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("channel_routes") as batch_op:
        batch_op.drop_column("channel_user_id")
