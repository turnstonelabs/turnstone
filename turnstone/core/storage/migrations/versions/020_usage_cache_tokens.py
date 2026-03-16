"""Add prompt cache token columns to usage_events.

Tracks cache_creation_tokens and cache_read_tokens per LLM call so
operators can monitor prompt caching effectiveness across providers.

Revision ID: 020
Revises: 019
Create Date: 2026-03-16
"""

import sqlalchemy as sa
from alembic import op

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "usage_events",
        sa.Column("cache_creation_tokens", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column(
        "usage_events",
        sa.Column("cache_read_tokens", sa.Integer, nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("usage_events", "cache_read_tokens")
    op.drop_column("usage_events", "cache_creation_tokens")
