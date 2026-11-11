"""Index audit events by resource, action and time.

Every session open replays the workstream's recent auto-approvals from the audit log: the
newest ``tool.auto_approved`` rows for one ``resource_id``. With only single-column indexes,
that lookup reads every auto-approved row in the cluster (through ``idx_audit_action``, or the
whole table) and sorts them, and it runs while the manager builds the session's UI. A
``(resource_id, action, timestamp)`` index answers it with a search of one workstream's rows;
the ``event_id`` tie-break sorts only those.

PostgreSQL builds the index concurrently, so a large live ``audit_events`` table keeps accepting
writes. SQLite uses its ordinary index DDL.

Revision ID: 089
Revises: 088
Create Date: 2026-11-10
"""

import sqlalchemy as sa
from alembic import op

revision = "089"
down_revision = "088"
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
                        "WHERE c.relname = 'idx_audit_resource_action_ts' "
                        "AND n.nspname = current_schema()"
                    )
                )
                .scalar_one_or_none()
            )
            if invalid_index:
                op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_audit_resource_action_ts")
            op.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_audit_resource_action_ts "
                'ON audit_events (resource_id, action, "timestamp")'
            )
    else:
        op.create_index(
            "idx_audit_resource_action_ts",
            "audit_events",
            ["resource_id", "action", "timestamp"],
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_audit_resource_action_ts")
    else:
        op.drop_index("idx_audit_resource_action_ts", table_name="audit_events")
