"""Drop vestigial tool_args column from conversations table.

The tool_args column has not been written since migration 013 moved
tool call data into the tool_calls JSON column on assistant rows.
All existing rows have NULL in this column.

Revision ID: 027
Revises: 026
Create Date: 2026-03-28
"""

import sqlalchemy as sa
from alembic import op

revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("tool_args")


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(sa.Column("tool_args", sa.Text))
