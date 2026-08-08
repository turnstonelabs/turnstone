"""Add per-server tool-call timeout to mcp_servers.

Revision ID: 068
Revises: 067
Create Date: 2026-08-03
"""

import sqlalchemy as sa
from alembic import op

revision = "068"
down_revision = "067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mcp_servers",
        sa.Column("timeout", sa.Integer, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mcp_servers", "timeout")
