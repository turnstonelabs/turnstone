"""Add license and compatibility columns to prompt_templates.

Agent Skills standard (agentskills.io) defines license and compatibility
as optional SKILL.md frontmatter fields. These were parsed but discarded
prior to this migration.

Revision ID: 023
Revises: 022
Create Date: 2026-03-17
"""

import sqlalchemy as sa
from alembic import op

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "prompt_templates",
        sa.Column("license", sa.Text, nullable=False, server_default=""),
    )
    op.add_column(
        "prompt_templates",
        sa.Column("compatibility", sa.Text, nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("prompt_templates", "compatibility")
    op.drop_column("prompt_templates", "license")
