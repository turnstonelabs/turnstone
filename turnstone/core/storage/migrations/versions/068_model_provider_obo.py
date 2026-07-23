"""Add per-model OBO auth columns to model_definitions.

Lets a model backend authenticate to its gateway with a per-user Entra
On-Behalf-Of access token instead of a single static ``api_key``.  When
``auth_mode='entra_obo'`` the worker mints a token for ``obo_audience`` from
the calling user's captured refresh credential (the same credential the
``oauth_obo`` MCP servers redeem — see migration 067) and sends it as the
backend's credential, falling back to the static ``api_key`` when there is no
user context.  ``auth_mode='static'`` (the default) is the pre-existing
behaviour, so existing rows are untouched.

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
