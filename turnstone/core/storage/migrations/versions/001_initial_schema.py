"""Initial schema — baseline for all tables.

Revision ID: 001
Revises:
Create Date: 2026-03-03
"""

import sqlalchemy as sa
from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # memories — persistent key-value store
    op.create_table(
        "memories",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )

    # conversations — message history
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("timestamp", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("content", sa.Text),
        sa.Column("tool_name", sa.Text),
        sa.Column("tool_args", sa.Text),
        sa.Column("tool_call_id", sa.Text),
        sa.Column("provider_data", sa.Text),
    )
    op.create_index("idx_conv_session", "conversations", ["session_id"])

    # sessions — session metadata
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.Text, primary_key=True),
        sa.Column("alias", sa.Text, unique=True),
        sa.Column("title", sa.Text),
        sa.Column("node_id", sa.Text),
        sa.Column("ws_id", sa.Text),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )
    op.create_index("idx_sessions_alias", "sessions", ["alias"])
    op.create_index("idx_sessions_updated", "sessions", ["updated"])
    op.create_index("idx_sessions_node_id", "sessions", ["node_id"])
    op.create_index("idx_sessions_ws_id", "sessions", ["ws_id"])

    # workstreams — persistent workstream lifecycle tracking
    op.create_table(
        "workstreams",
        sa.Column("ws_id", sa.Text, primary_key=True),
        sa.Column("node_id", sa.Text),
        sa.Column("name", sa.Text, nullable=False, server_default=""),
        sa.Column("state", sa.Text, nullable=False, server_default="idle"),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )
    op.create_index("idx_workstreams_node_id", "workstreams", ["node_id"])
    op.create_index("idx_workstreams_state", "workstreams", ["state"])

    # session_config — per-session LLM parameters
    op.create_table(
        "session_config",
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("value", sa.Text),
        sa.PrimaryKeyConstraint("session_id", "key"),
    )


def downgrade() -> None:
    op.drop_table("session_config")
    op.drop_table("workstreams")
    op.drop_table("sessions")
    op.drop_table("conversations")
    op.drop_table("memories")
