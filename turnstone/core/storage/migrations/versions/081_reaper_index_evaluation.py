"""Rebuild the orphan-reaper partial index to cover the ``evaluation`` state.

``BULK_CLOSE_STATE_VALUES`` gained ``evaluation`` (the intent judge holds a
tool batch in a Smart Approvals wait), so the reaper's predicate is now
``state IN ('idle', 'thinking', 'running', 'attention', 'evaluation')``.
``idx_workstreams_reaper`` (migration 048) still covers the old four states,
and a partial index serves a query only when the query's WHERE implies the
index's: on PostgreSQL the five-state ``IN`` no longer does, and the planner
falls back to ``idx_workstreams_state`` plus a filter.  This rebuilds the
index with the five states.

The state names are written out here rather than read from the live
constant, so a later change to the set does not rewrite this migration; a
test compares the built index with ``BULK_CLOSE_STATE_VALUES``.

PostgreSQL drops and rebuilds ``CONCURRENTLY`` (non-blocking on a live
system; the reaper runs without the index for the length of the build).
SQLite has no concurrent build, and the table-level write lock already
serializes, so a plain drop and create is fine.

Revision ID: 081
Revises: 080
Create Date: 2026-10-09
"""

import sqlalchemy as sa
from alembic import op

revision = "081"
down_revision = "080"
branch_labels = None
depends_on = None


_WHERE_WITH_EVALUATION = "state IN ('idle', 'thinking', 'attention', 'evaluation', 'running')"
_WHERE_BEFORE = "state IN ('idle', 'thinking', 'attention', 'running')"


def _rebuild(where: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_workstreams_reaper")
            op.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_workstreams_reaper "
                "ON workstreams (kind, updated) "
                f"WHERE {where}"
            )
    else:
        op.drop_index("idx_workstreams_reaper", table_name="workstreams")
        op.create_index(
            "idx_workstreams_reaper",
            "workstreams",
            ["kind", "updated"],
            sqlite_where=sa.text(where),
        )


def upgrade() -> None:
    _rebuild(_WHERE_WITH_EVALUATION)


def downgrade() -> None:
    _rebuild(_WHERE_BEFORE)
