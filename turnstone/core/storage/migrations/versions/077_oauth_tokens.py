"""Give the shared OAuth token store consumer-neutral names.

Rename the table and its identity column without changing keys, ciphertext,
or expiry metadata. Older processes cannot query this storage until upgraded.

Revision ID: 077
Revises: 076
Create Date: 2026-09-11
"""

from alembic import op

revision = "077"
down_revision = "076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("mcp_user_tokens", "oauth_tokens")
    op.execute("ALTER TABLE oauth_tokens RENAME COLUMN server_name TO token_key")
    if op.get_bind().dialect.name == "postgresql":
        # PostgreSQL renames the PK's backing index with the constraint.
        op.execute(
            "ALTER TABLE oauth_tokens RENAME CONSTRAINT mcp_user_tokens_pkey TO oauth_tokens_pkey"
        )
        op.execute("ALTER INDEX idx_mcp_user_tokens_server RENAME TO idx_oauth_tokens_key")
    else:
        # SQLite's unnamed PK autoindex follows the table rename; its named
        # secondary index does not. SQLite has no ALTER INDEX RENAME syntax.
        op.drop_index("idx_mcp_user_tokens_server", table_name="oauth_tokens")
        op.create_index("idx_oauth_tokens_key", "oauth_tokens", ["token_key", "expires_at"])


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER INDEX idx_oauth_tokens_key RENAME TO idx_mcp_user_tokens_server")
        op.execute(
            "ALTER TABLE oauth_tokens RENAME CONSTRAINT oauth_tokens_pkey TO mcp_user_tokens_pkey"
        )
    else:
        op.drop_index("idx_oauth_tokens_key", table_name="oauth_tokens")
        op.create_index("idx_mcp_user_tokens_server", "oauth_tokens", ["token_key", "expires_at"])
    op.execute("ALTER TABLE oauth_tokens RENAME COLUMN token_key TO server_name")
    op.rename_table("oauth_tokens", "mcp_user_tokens")
