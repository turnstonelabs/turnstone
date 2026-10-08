"""Index usage events by workstream and time.

The saved-session list reads each workstream's latest usage row. With only a
``ws_id`` index, that lookup sorts the workstream's whole usage history, and
PostgreSQL may instead walk the global ``timestamp`` index backwards, filtering
every newer event of every other workstream. A ``(ws_id, timestamp)`` index
answers it with one short backward scan. It also serves every ``ws_id``
lookup, so it replaces the single-column index.

PostgreSQL builds the new index concurrently before dropping the old one, so a
large live ``usage_events`` table keeps accepting writes and never goes without
a ``ws_id`` index. SQLite uses its ordinary index DDL.

Revision ID: 079
Revises: 078
Create Date: 2026-10-07
"""

import sqlalchemy as sa
from alembic import op

revision = "079"
down_revision = "078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # ``autocommit_block`` commits as it goes, so every statement must be
        # restart-safe. An interrupted CREATE INDEX CONCURRENTLY can leave an
        # INVALID index behind; drop only that residue before rebuilding.
        with op.get_context().autocommit_block():
            invalid_index = (
                op.get_bind()
                .execute(
                    sa.text(
                        "SELECT NOT i.indisvalid "
                        "FROM pg_class AS c "
                        "JOIN pg_index AS i ON i.indexrelid = c.oid "
                        "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                        "WHERE c.relname = 'idx_usage_events_ws_timestamp' "
                        "AND n.nspname = current_schema()"
                    )
                )
                .scalar_one_or_none()
            )
            if invalid_index:
                op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_usage_events_ws_timestamp")
            op.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_usage_events_ws_timestamp "
                'ON usage_events (ws_id, "timestamp")'
            )
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_usage_events_ws")
    else:
        op.create_index("idx_usage_events_ws_timestamp", "usage_events", ["ws_id", "timestamp"])
        op.drop_index("idx_usage_events_ws", table_name="usage_events")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_usage_events_ws "
                "ON usage_events (ws_id)"
            )
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_usage_events_ws_timestamp")
    else:
        op.create_index("idx_usage_events_ws", "usage_events", ["ws_id"])
        op.drop_index("idx_usage_events_ws_timestamp", table_name="usage_events")
