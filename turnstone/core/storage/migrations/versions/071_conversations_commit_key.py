"""Add idempotency keys for conversation commits.

``commit_key`` identifies one admitted live conversation row independently of
its content.  A retry after an ambiguous database acknowledgement uses the
same key and resolves to the already-committed row instead of appending a
duplicate.  The column is nullable so legacy, bulk, and offline writers retain
their append-only semantics; SQLite and PostgreSQL both allow multiple NULLs
under the partial composite unique index. PostgreSQL builds the index
concurrently so upgrading a large live conversation table does not block
writes; SQLite uses its ordinary partial-index DDL.

Revision ID: 071
Revises: 070
Create Date: 2026-08-09
"""

import sqlalchemy as sa
from alembic import op

revision = "071"
down_revision = "070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # ``autocommit_block`` commits every preceding operation, so the column
        # add must itself be restart-safe: a crash during the concurrent index
        # build leaves revision 070 stamped but the column already durable.
        # PostgreSQL can also retain an INVALID index after an interrupted
        # CREATE INDEX CONCURRENTLY; remove only that unusable residue before
        # the idempotent rebuild. The migration runner's session advisory lock
        # still serializes competing schema upgrades.
        with op.get_context().autocommit_block():
            op.execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS commit_key TEXT")
            invalid_index = (
                op.get_bind()
                .execute(
                    sa.text(
                        "SELECT NOT i.indisvalid "
                        "FROM pg_class AS c "
                        "JOIN pg_index AS i ON i.indexrelid = c.oid "
                        "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                        "WHERE c.relname = 'uq_conversations_ws_commit_key' "
                        "AND n.nspname = current_schema()"
                    )
                )
                .scalar_one_or_none()
            )
            if invalid_index:
                op.execute("DROP INDEX CONCURRENTLY IF EXISTS uq_conversations_ws_commit_key")
            op.execute(
                "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
                "uq_conversations_ws_commit_key "
                "ON conversations (ws_id, commit_key) WHERE commit_key IS NOT NULL"
            )
    else:
        op.add_column("conversations", sa.Column("commit_key", sa.Text(), nullable=True))
        op.create_index(
            "uq_conversations_ws_commit_key",
            "conversations",
            ["ws_id", "commit_key"],
            unique=True,
            sqlite_where=sa.text("commit_key IS NOT NULL"),
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS uq_conversations_ws_commit_key")
    else:
        op.drop_index("uq_conversations_ws_commit_key", table_name="conversations")
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("commit_key")
