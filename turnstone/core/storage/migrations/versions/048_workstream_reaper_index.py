"""Add a partial composite index for the orphan-reaper query.

``StorageBackend.bulk_close_stale_orphans`` (introduced alongside the
workstream-lifecycle leak fix) runs every ``min(300s, idle_timeout/4)``
on every server and console process.  Its WHERE shape is:

    WHERE kind = ?
      AND state IN ('idle', 'thinking', 'attention', 'running')
      AND updated < ?
      AND (node_id IS NULL OR node_id NOT IN (alive_service_ids))

At current scale (low-thousands of workstream rows) the existing single-
column indexes are sufficient — ``idx_workstreams_state`` prunes to the
non-closed subset, and the planner filters the rest sequentially.  At
100k+ rows that filter becomes a tablescan-shaped cost on the reaper's
periodic run.

A **partial** index covering only ``BULK_CLOSE_STATE_VALUES`` rows
matches the reaper's query exactly while staying tiny — closed rows
(typically 95%+ of the table per empirical diagnosis) and ``error``
rows are excluded, so the index is roughly 5% the size a full multi-
column index would be.  Write amplification only kicks in for
transitions that touch one of the four covered states.

Column order ``(kind, updated)``:

- ``kind`` first because the reaper always supplies it as an equality
  predicate; partitions the partial index into interactive vs
  coordinator subtrees.
- ``updated`` last so the range comparison rides the trailing column —
  classic composite-index pattern for ``WHERE eq AND range``.

``node_id`` is intentionally NOT in the index.  The reaper's predicate
on it is ``NOT IN (small list)`` against an unbounded-cardinality
column, which planners don't index well; including it would just add
write cost for negligible read benefit.

PostgreSQL uses ``CREATE INDEX CONCURRENTLY`` so the build is
non-blocking on a live system; SQLite has no concurrent concept and
the table-level write lock already serializes, so a plain
``CREATE INDEX`` is fine.

Revision ID: 048
Revises: 047
Create Date: 2026-04-30
"""

import sqlalchemy as sa
from alembic import op

revision = "048"
down_revision = "047"
branch_labels = None
depends_on = None


_REAPER_PARTIAL_WHERE = "state IN ('idle', 'thinking', 'attention', 'running')"


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_workstreams_reaper "
                "ON workstreams (kind, updated) "
                f"WHERE {_REAPER_PARTIAL_WHERE}"
            )
    else:
        op.create_index(
            "idx_workstreams_reaper",
            "workstreams",
            ["kind", "updated"],
            sqlite_where=sa.text(_REAPER_PARTIAL_WHERE),
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_workstreams_reaper")
    else:
        op.drop_index("idx_workstreams_reaper", table_name="workstreams")
