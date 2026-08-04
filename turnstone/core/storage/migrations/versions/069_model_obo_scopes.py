"""Add per-alias exchange scopes to model_definitions.

``obo_scopes`` carries the space-separated scope list the RFC 8693
token-exchange mint leg requests for an ``auth_mode='rfc8693_obo'`` alias.
The exchange-capable IdPs this profile targets refuse an audience whose
scope was not requested, and model definitions previously had no scope
source at all, so the delegated-user mint could never succeed on that
profile (issue #955). Empty default keeps every existing row untouched.

Revision ID: 069
Revises: 068
Create Date: 2026-08-03
"""

import sqlalchemy as sa
from alembic import op

revision = "069"
down_revision = "068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_definitions",
        sa.Column("obo_scopes", sa.Text, nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("model_definitions", "obo_scopes")
