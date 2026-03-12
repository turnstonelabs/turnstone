"""Add MCP origin tracking columns to prompt_templates.

Revision ID: 009
Revises: 008
Create Date: 2026-03-12
"""

import sqlalchemy as sa
from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("prompt_templates") as batch_op:
        batch_op.add_column(sa.Column("origin", sa.Text, nullable=False, server_default="manual"))
        batch_op.add_column(sa.Column("mcp_server", sa.Text, nullable=False, server_default=""))
        batch_op.add_column(sa.Column("readonly", sa.Integer, nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("prompt_templates") as batch_op:
        batch_op.drop_column("readonly")
        batch_op.drop_column("mcp_server")
        batch_op.drop_column("origin")
