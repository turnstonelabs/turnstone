"""Tune migration 039's workstream indexes for the real query mix.

Follow-up to the retrospective review of the workstream-kind feature.
Migration 039 added two single-column btree indexes on the workstreams
table — ``idx_workstreams_kind`` (on ``kind``) and
``idx_workstreams_parent`` (on ``parent_ws_id``).  Profiling every
``list_workstreams(...)`` call site revealed:

- **kind has 2 values** (``"interactive"`` / ``"coordinator"``).  Every
  query that filters by ``kind`` also supplies a more selective predicate
  (``parent_ws_id``, ``node_id``, or ``user_id``), so the planner never
  chooses the low-cardinality ``kind`` index — it's write-amplification
  overhead on every INSERT/UPDATE to workstreams for zero read benefit.
  Drop it.

- **parent_ws_id is mostly NULL** (the migration's own docstring notes
  "most workstreams have no parent").  The existing full btree indexes
  all those NULL rows.  Every real query is ``parent_ws_id = <coord_ws>``,
  never ``IS NULL``.  Replace with a partial index ``WHERE parent_ws_id
  IS NOT NULL`` — halves the btree size and write cost on interactive
  workstreams without changing any query plan.

On PostgreSQL the rebuild follows a gap-free pattern so the ``workstreams``
table is never unindexed on ``parent_ws_id`` during the migration:

1. ``CREATE INDEX CONCURRENTLY`` a new partial index under a temporary
   name (``idx_workstreams_parent_new``).  Builds without blocking writes.
2. ``DROP INDEX CONCURRENTLY`` the old full index.  Also non-blocking.
3. ``ALTER INDEX ... RENAME`` the new index into the canonical slot.

A naïve "drop then create" in-transaction would leave a minutes-long window
with no parent_ws_id index at all — severe query slowdowns for any reader
filtering on that column.  The concurrent-build/drop/rename dance keeps
at least one index serving ``parent_ws_id`` queries for the full duration.

Both concurrent ops require stepping out of Alembic's managed transaction
via an autocommit block.

SQLite has no concurrent-index concept and its table-level write lock
already serializes readers and writers, so the partial rebuild there is
a straight drop-then-create (no gap-free concern).  Partial-index support
has been stable since SQLite 3.8 (predates any modern Python stdlib).

Revision ID: 041
Revises: 040
Create Date: 2026-04-18
"""

import sqlalchemy as sa
from alembic import op

revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        # Gap-free rebuild — see module docstring for the rationale.
        # Every op runs inside the autocommit block; ``DROP INDEX
        # CONCURRENTLY`` and ``CREATE INDEX CONCURRENTLY`` both require
        # running outside any transaction.
        with op.get_context().autocommit_block():
            op.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                "idx_workstreams_parent_new ON workstreams (parent_ws_id) "
                "WHERE parent_ws_id IS NOT NULL"
            )
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_workstreams_parent")
            op.execute("ALTER INDEX idx_workstreams_parent_new RENAME TO idx_workstreams_parent")
            # Kind index has no replacement — drop concurrently to avoid
            # the brief ACCESS EXCLUSIVE lock a plain DROP INDEX would take.
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_workstreams_kind")
    else:
        # SQLite: partial indexes stable since 3.8; no concurrent concept.
        # The table-level write lock means there's no useful distinction
        # between gap-free and drop-then-create here.
        op.drop_index("idx_workstreams_kind", table_name="workstreams")
        op.drop_index("idx_workstreams_parent", table_name="workstreams")
        op.create_index(
            "idx_workstreams_parent",
            "workstreams",
            ["parent_ws_id"],
            sqlite_where=sa.text("parent_ws_id IS NOT NULL"),
        )


def downgrade() -> None:
    """Restore migration 039's non-partial full indexes.

    Symmetric rollback that also preserves the gap-free invariant on
    PostgreSQL: build the full replacement under a temp name, drop the
    partial index concurrently, then rename.  Data is untouched — indexes
    are pure read-path structures.
    """
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                "idx_workstreams_parent_old ON workstreams (parent_ws_id)"
            )
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_workstreams_parent")
            op.execute("ALTER INDEX idx_workstreams_parent_old RENAME TO idx_workstreams_parent")
            op.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_workstreams_kind ON workstreams (kind)"
            )
    else:
        op.drop_index("idx_workstreams_parent", table_name="workstreams")
        op.create_index("idx_workstreams_parent", "workstreams", ["parent_ws_id"])
        op.create_index("idx_workstreams_kind", "workstreams", ["kind"])
