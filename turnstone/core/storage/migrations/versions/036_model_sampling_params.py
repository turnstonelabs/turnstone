"""Add per-model sampling parameters to model_definitions.

Adds nullable temperature, max_tokens, and reasoning_effort columns
so each model can override the global defaults.  NULL means "inherit
the cluster-wide setting from system_settings".

Revision ID: 036
Revises: 035
Create Date: 2026-04-13
"""

import sqlalchemy as sa
from alembic import op

revision = "036"
down_revision = "035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_definitions") as batch:
        batch.add_column(sa.Column("temperature", sa.Float, nullable=True))
        batch.add_column(sa.Column("max_tokens", sa.Integer, nullable=True))
        batch.add_column(sa.Column("reasoning_effort", sa.Text, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("model_definitions") as batch:
        batch.drop_column("reasoning_effort")
        batch.drop_column("max_tokens")
        batch.drop_column("temperature")
