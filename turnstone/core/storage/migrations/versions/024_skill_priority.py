"""Add priority column to prompt_templates for skill ordering.

Multiple ``activation="default"`` skills are concatenated in priority
order (ascending), falling back to name for ties.  Previously the only
ordering lever was the skill name itself.

Revision ID: 024
Revises: 023
Create Date: 2026-03-21
"""

import sqlalchemy as sa
from alembic import op

revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "prompt_templates",
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("prompt_templates", "priority")
