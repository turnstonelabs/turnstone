"""Add dynamic backend-auth columns to model_definitions.

Lets a model backend authenticate with a caller-delegated Entra token
(``entra_obo``), a shared app-identity token (``entra_app``), or the existing
static ``api_key``. Dynamic modes mint for ``obo_audience`` at call time and
bind that credential through the provider SDK. ``static`` remains the default,
so existing rows are untouched.

Revision ID: 068
Revises: 067
Create Date: 2026-07-21
"""

import sqlalchemy as sa
from alembic import op

revision = "068"
down_revision = "067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_definitions",
        sa.Column("auth_mode", sa.Text, nullable=False, server_default="static"),
    )
    op.add_column(
        "model_definitions",
        sa.Column("obo_audience", sa.Text, nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("model_definitions", "obo_audience")
    op.drop_column("model_definitions", "auth_mode")
