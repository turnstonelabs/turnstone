"""Drop dead columns from mcp_pending_consent.

``last_ws_id`` and ``last_tool_call_id`` were added in migration 054 but
were never populated (the sole writer hardcodes None) and never read by
any query, dashboard, or API.  Drop them so the schema matches reality.

Revision ID: 064
Revises: 063
Create Date: 2026-07-04
"""

from alembic import op

revision = "064"
down_revision = "063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("mcp_pending_consent") as batch_op:
        batch_op.drop_column("last_ws_id")
        batch_op.drop_column("last_tool_call_id")


def downgrade() -> None:
    import sqlalchemy as sa

    with op.batch_alter_table("mcp_pending_consent") as batch_op:
        batch_op.add_column(sa.Column("last_ws_id", sa.Text, nullable=True))
        batch_op.add_column(sa.Column("last_tool_call_id", sa.Text, nullable=True))
