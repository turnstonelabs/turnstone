"""Index mcp_user_tokens by (server_name, expires_at).

Phase 9 admin pill (``count_mcp_consented_users_*``) filters
``mcp_user_tokens`` by ``server_name`` and ``expires_at``.  The table's
only existing index is the composite PK ``(user_id, server_name)`` —
``user_id`` is the leading column, so a filter on ``server_name`` alone
must full-scan the table.  The bulk ``GROUP BY server_name`` variant in
the admin list handler benefits from the same index.

PostgreSQL uses ``CREATE INDEX CONCURRENTLY`` inside an
``autocommit_block`` so the build is non-blocking on a live system —
``mcp_user_tokens`` is on the token-refresh hot path and an
ACCESS EXCLUSIVE lock during build would stall refresh writers on
installs with non-trivial row counts.  SQLite has no concurrent build
concept and the table-level write lock already serializes, so a plain
``op.create_index`` is fine.  Pattern mirrors migration 048
(``idx_workstreams_reaper``).

Revision ID: 055
Revises: 054
Create Date: 2026-05-11
"""

from alembic import op

revision = "055"
down_revision = "054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                "idx_mcp_user_tokens_server ON mcp_user_tokens "
                "(server_name, expires_at)"
            )
    else:
        op.create_index(
            "idx_mcp_user_tokens_server",
            "mcp_user_tokens",
            ["server_name", "expires_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_mcp_user_tokens_server")
    else:
        op.drop_index("idx_mcp_user_tokens_server", table_name="mcp_user_tokens")
