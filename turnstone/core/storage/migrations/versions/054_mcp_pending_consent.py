"""Add mcp_pending_consent table.

Stores per-(user, server) deferred-consent records emitted by the pool
dispatchers when a non-interactive run (scheduled / channel) hits
``mcp_consent_required`` or ``mcp_insufficient_scope``.  Read on
dashboard load to render the "N MCP servers need consent" badge; cleared
by the OAuth callback handler when consent completes.

Composite PK ``(user_id, server_name)`` collapses repeat occurrences for
the same server into one row.  No FKs (matches the rest of the
oauth_user schema in migration 049).

Revision ID: 054
Revises: 053
Create Date: 2026-05-11
"""

import sqlalchemy as sa
from alembic import op

revision = "054"
down_revision = "053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_pending_consent",
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("server_name", sa.Text, nullable=False),
        sa.Column("error_code", sa.Text, nullable=False),
        sa.Column("scopes_required", sa.Text, nullable=True),
        sa.Column("last_ws_id", sa.Text, nullable=True),
        sa.Column("last_tool_call_id", sa.Text, nullable=True),
        sa.Column("first_seen_at", sa.Text, nullable=False),
        sa.Column("last_seen_at", sa.Text, nullable=False),
        sa.Column("occurrence_count", sa.Integer, nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint("user_id", "server_name"),
    )
    op.create_index(
        "idx_mcp_pending_consent_user",
        "mcp_pending_consent",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_mcp_pending_consent_user", table_name="mcp_pending_consent")
    op.drop_table("mcp_pending_consent")
