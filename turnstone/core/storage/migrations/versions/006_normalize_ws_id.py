"""Normalize session_id into ws_id — merge sessions table into workstreams.

Conversations and config are now keyed by ws_id (workstream identity) instead
of a separate session_id.  The sessions table is dropped; its alias/title
columns move to workstreams.

Revision ID: 006
Revises: 005
Create Date: 2026-03-07
"""

import sqlalchemy as sa
from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add alias and title columns to workstreams.
    op.add_column("workstreams", sa.Column("alias", sa.Text))
    op.add_column("workstreams", sa.Column("title", sa.Text))
    op.create_index("idx_workstreams_alias", "workstreams", ["alias"], unique=True)

    conn = op.get_bind()

    # 2. Copy alias/title from sessions → workstreams (for rows that have a ws_id).
    conn.execute(
        sa.text(
            "UPDATE workstreams SET "
            "  alias = (SELECT s.alias FROM sessions s WHERE s.ws_id = workstreams.ws_id), "
            "  title = (SELECT s.title FROM sessions s WHERE s.ws_id = workstreams.ws_id) "
            "WHERE EXISTS (SELECT 1 FROM sessions s WHERE s.ws_id = workstreams.ws_id)"
        )
    )

    # 3. Create workstream rows for sessions that have a ws_id but no
    #    corresponding workstream row yet.
    #    The NOT EXISTS guard makes this safe on both SQLite and PostgreSQL.
    conn.execute(
        sa.text(
            "INSERT INTO workstreams "
            "(ws_id, node_id, alias, title, state, created, updated) "
            "SELECT s.ws_id, s.node_id, s.alias, s.title, 'closed', s.created, s.updated "
            "FROM sessions s "
            "WHERE s.ws_id IS NOT NULL AND s.ws_id != '' "
            "  AND NOT EXISTS (SELECT 1 FROM workstreams w WHERE w.ws_id = s.ws_id)"
        )
    )

    # 4. Create workstream rows for sessions WITHOUT a ws_id
    #    (use session_id as ws_id).
    conn.execute(
        sa.text(
            "INSERT INTO workstreams "
            "(ws_id, node_id, alias, title, state, created, updated) "
            "SELECT s.session_id, s.node_id, s.alias, s.title, 'closed', s.created, s.updated "
            "FROM sessions s "
            "WHERE (s.ws_id IS NULL OR s.ws_id = '') "
            "  AND NOT EXISTS (SELECT 1 FROM workstreams w WHERE w.ws_id = s.session_id)"
        )
    )

    # 5. Rename conversations.session_id → conversations.ws_id and remap values.
    #    For sessions with ws_id: map session_id → ws_id.
    #    For sessions without ws_id: session_id stays (used as ws_id).
    op.alter_column("conversations", "session_id", new_column_name="ws_id")

    conn.execute(
        sa.text(
            "UPDATE conversations SET ws_id = ("
            "  SELECT COALESCE(NULLIF(s.ws_id, ''), s.session_id) "
            "  FROM sessions s WHERE s.session_id = conversations.ws_id"
            ") "
            "WHERE EXISTS ("
            "  SELECT 1 FROM sessions s WHERE s.session_id = conversations.ws_id"
            ")"
        )
    )

    # 6. Rename session_config → workstream_config with ws_id column.
    op.rename_table("session_config", "workstream_config")
    op.alter_column("workstream_config", "session_id", new_column_name="ws_id")

    conn.execute(
        sa.text(
            "UPDATE workstream_config SET ws_id = ("
            "  SELECT COALESCE(NULLIF(s.ws_id, ''), s.session_id) "
            "  FROM sessions s WHERE s.session_id = workstream_config.ws_id"
            ") "
            "WHERE EXISTS ("
            "  SELECT 1 FROM sessions s WHERE s.session_id = workstream_config.ws_id"
            ")"
        )
    )

    # 7. Drop the sessions table.
    op.drop_table("sessions")


def downgrade() -> None:
    # Recreate the sessions table.
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.Text, primary_key=True),
        sa.Column("alias", sa.Text, unique=True),
        sa.Column("title", sa.Text),
        sa.Column("node_id", sa.Text),
        sa.Column("ws_id", sa.Text),
        sa.Column("user_id", sa.Text),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )
    op.create_index("idx_sessions_alias", "sessions", ["alias"])
    op.create_index("idx_sessions_updated", "sessions", ["updated"])
    op.create_index("idx_sessions_node_id", "sessions", ["node_id"])
    op.create_index("idx_sessions_ws_id", "sessions", ["ws_id"])

    # Reverse config table rename.
    op.alter_column("workstream_config", "ws_id", new_column_name="session_id")
    op.rename_table("workstream_config", "session_config")

    # Reverse conversations column rename.
    op.alter_column("conversations", "ws_id", new_column_name="session_id")

    # Drop alias/title from workstreams.
    op.drop_index("idx_workstreams_alias", "workstreams")
    op.drop_column("workstreams", "title")
    op.drop_column("workstreams", "alias")
