"""Create prompt_policies table for system message composition.

Revision ID: 031
Revises: 030
Create Date: 2026-03-31
"""

import sqlalchemy as sa
from alembic import op

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prompt_policies",
        sa.Column("policy_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("tool_gate", sa.Text, nullable=False, server_default=""),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
        sa.Column("org_id", sa.Text, nullable=False, server_default=""),
        sa.Column("created_by", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("prompt_policies")
