"""Add the workstream owner lease.

A live session holds a time-bounded lease on its workstream row. The holder id
is boot-scoped, expiry is judged against the database clock, and every
acquisition increments a fencing epoch that session-owned writes must present.
Existing rows start unleased at epoch 0.

Revision ID: 078
Revises: 077
Create Date: 2026-10-02
"""

import sqlalchemy as sa
from alembic import op

revision = "078"
down_revision = "077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workstreams") as batch_op:
        batch_op.add_column(sa.Column("lease_holder", sa.Text, nullable=True))
        batch_op.add_column(sa.Column("lease_node_id", sa.Text, nullable=True))
        batch_op.add_column(
            sa.Column("lease_epoch", sa.BigInteger, nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("lease_expires_ms", sa.BigInteger, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workstreams") as batch_op:
        batch_op.drop_column("lease_expires_ms")
        batch_op.drop_column("lease_epoch")
        batch_op.drop_column("lease_node_id")
        batch_op.drop_column("lease_holder")
