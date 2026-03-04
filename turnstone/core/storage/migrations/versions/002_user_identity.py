"""User identity and API tokens.

Revision ID: 002
Revises: 001
Create Date: 2026-03-04
"""

import sqlalchemy as sa
from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- New tables ---

    op.create_table(
        "users",
        sa.Column("user_id", sa.Text, primary_key=True),
        sa.Column("username", sa.Text, nullable=False, unique=True),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("created", sa.Text, nullable=False),
    )
    op.create_index("idx_users_username", "users", ["username"])

    op.create_table(
        "api_tokens",
        sa.Column("token_id", sa.Text, primary_key=True),
        sa.Column("token_hash", sa.Text, nullable=False, unique=True),
        sa.Column("token_prefix", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False, server_default=""),
        sa.Column("scopes", sa.Text, nullable=False),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("expires", sa.Text),
    )
    op.create_index("idx_api_tokens_user_id", "api_tokens", ["user_id"])
    op.create_index("idx_api_tokens_token_hash", "api_tokens", ["token_hash"])

    op.create_table(
        "channel_users",
        sa.Column("channel_type", sa.Text, nullable=False),
        sa.Column("channel_user_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("created", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("channel_type", "channel_user_id"),
    )
    op.create_index("idx_channel_users_user_id", "channel_users", ["user_id"])

    # --- Add user_id to existing tables ---

    op.add_column("sessions", sa.Column("user_id", sa.Text))
    op.create_index("idx_sessions_user_id", "sessions", ["user_id"])

    op.add_column("workstreams", sa.Column("user_id", sa.Text))
    op.create_index("idx_workstreams_user_id", "workstreams", ["user_id"])


def downgrade() -> None:
    op.drop_index("idx_workstreams_user_id", "workstreams")
    op.drop_column("workstreams", "user_id")
    op.drop_index("idx_sessions_user_id", "sessions")
    op.drop_column("sessions", "user_id")
    op.drop_table("channel_users")
    op.drop_table("api_tokens")
    op.drop_table("users")
