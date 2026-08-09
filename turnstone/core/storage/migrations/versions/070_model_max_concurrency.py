"""Add the per-alias model concurrency limit.

``max_concurrency`` bounds concurrent model generations for one alias in one
process.  Zero preserves the pre-feature unlimited behavior for every
existing definition.

Revision ID: 070
Revises: 069
Create Date: 2026-08-08
"""

import sqlalchemy as sa
from alembic import op

revision = "070"
down_revision = "069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_definitions",
        sa.Column("max_concurrency", sa.Integer, nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("model_definitions", "max_concurrency")
