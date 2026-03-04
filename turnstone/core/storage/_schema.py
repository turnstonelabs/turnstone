"""SQLAlchemy Core schema — single source of truth for all table definitions.

Used by both storage backends and Alembic migrations.
"""

from __future__ import annotations

import sqlalchemy as sa

metadata = sa.MetaData()

memories = sa.Table(
    "memories",
    metadata,
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("value", sa.Text, nullable=False),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
)

conversations = sa.Table(
    "conversations",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("session_id", sa.Text, nullable=False, index=True),
    sa.Column("timestamp", sa.Text, nullable=False),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("content", sa.Text),
    sa.Column("tool_name", sa.Text),
    sa.Column("tool_args", sa.Text),
    sa.Column("tool_call_id", sa.Text),
    sa.Column("provider_data", sa.Text),
)

sessions = sa.Table(
    "sessions",
    metadata,
    sa.Column("session_id", sa.Text, primary_key=True),
    sa.Column("alias", sa.Text, unique=True),
    sa.Column("title", sa.Text),
    sa.Column("node_id", sa.Text),
    sa.Column("ws_id", sa.Text),
    sa.Column("user_id", sa.Text),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
)

# Additional indexes on sessions (name-based to avoid duplication with SA's auto-index)
sa.Index("idx_sessions_alias", sessions.c.alias)
sa.Index("idx_sessions_updated", sessions.c.updated)
sa.Index("idx_sessions_node_id", sessions.c.node_id)
sa.Index("idx_sessions_ws_id", sessions.c.ws_id)
sa.Index("idx_sessions_user_id", sessions.c.user_id)

workstreams = sa.Table(
    "workstreams",
    metadata,
    sa.Column("ws_id", sa.Text, primary_key=True),
    sa.Column("node_id", sa.Text),
    sa.Column("user_id", sa.Text),
    sa.Column("name", sa.Text, nullable=False, server_default=""),
    sa.Column("state", sa.Text, nullable=False, server_default="idle"),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("updated", sa.Text, nullable=False),
)

sa.Index("idx_workstreams_node_id", workstreams.c.node_id)
sa.Index("idx_workstreams_state", workstreams.c.state)
sa.Index("idx_workstreams_user_id", workstreams.c.user_id)

session_config = sa.Table(
    "session_config",
    metadata,
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("key", sa.Text, nullable=False),
    sa.Column("value", sa.Text),
    sa.PrimaryKeyConstraint("session_id", "key"),
)

# ---------------------------------------------------------------------------
# User identity tables
# ---------------------------------------------------------------------------

users = sa.Table(
    "users",
    metadata,
    sa.Column("user_id", sa.Text, primary_key=True),
    sa.Column("username", sa.Text, nullable=False, unique=True),
    sa.Column("display_name", sa.Text, nullable=False),
    sa.Column("password_hash", sa.Text, nullable=False),
    sa.Column("created", sa.Text, nullable=False),
)

sa.Index("idx_users_username", users.c.username)

api_tokens = sa.Table(
    "api_tokens",
    metadata,
    sa.Column("token_id", sa.Text, primary_key=True),
    sa.Column("token_hash", sa.Text, nullable=False, unique=True),
    sa.Column("token_prefix", sa.Text, nullable=False),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("name", sa.Text, nullable=False, server_default=""),
    sa.Column("scopes", sa.Text, nullable=False),
    sa.Column("created", sa.Text, nullable=False),
    sa.Column("expires", sa.Text),
)

sa.Index("idx_api_tokens_user_id", api_tokens.c.user_id)
sa.Index("idx_api_tokens_token_hash", api_tokens.c.token_hash)

channel_users = sa.Table(
    "channel_users",
    metadata,
    sa.Column("channel_type", sa.Text, nullable=False),
    sa.Column("channel_user_id", sa.Text, nullable=False),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("created", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("channel_type", "channel_user_id"),
)

sa.Index("idx_channel_users_user_id", channel_users.c.user_id)
