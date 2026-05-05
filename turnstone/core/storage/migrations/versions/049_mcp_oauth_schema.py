"""Add OAuth-MCP schema (Phase 2).

Adds two new tables (``mcp_user_tokens``, ``mcp_oauth_pending``) and
eight new columns on ``mcp_servers`` to support per-(user, server)
OAuth 2.1 + PKCE authorization for MCP servers.  See
``docs/design/oauth-mcp.md`` §5.1 / §5.2.

Existing rows continue working: every new column is either nullable or
defaults to ``'static'`` (the existing behavior).  After the new
``auth_type`` column lands, rows with empty ``headers`` are normalized
to ``auth_type='none'`` so the operator UI can hide the static-headers
field for those.

Revision ID: 049
Revises: 048
Create Date: 2026-05-04
"""

import sqlalchemy as sa
from alembic import op

revision = "049"
down_revision = "048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_user_tokens",
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("server_name", sa.Text, nullable=False),
        sa.Column("access_token_ct", sa.LargeBinary, nullable=False),
        sa.Column("refresh_token_ct", sa.LargeBinary, nullable=True),
        sa.Column("expires_at", sa.Text, nullable=True),
        sa.Column("scopes", sa.Text, nullable=True),
        sa.Column("as_issuer", sa.Text, nullable=False),
        sa.Column("audience", sa.Text, nullable=False),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("last_refreshed", sa.Text, nullable=True),
        sa.PrimaryKeyConstraint("user_id", "server_name"),
    )

    op.create_table(
        "mcp_oauth_pending",
        sa.Column("state", sa.Text, primary_key=True),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("server_name", sa.Text, nullable=False),
        sa.Column("code_verifier", sa.Text, nullable=False),
        sa.Column("return_url", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
    )
    op.create_index("idx_mcp_pending_created", "mcp_oauth_pending", ["created_at"])

    op.add_column(
        "mcp_servers",
        sa.Column("auth_type", sa.Text, nullable=False, server_default="static"),
    )
    op.add_column("mcp_servers", sa.Column("oauth_client_id", sa.Text, nullable=True))
    op.add_column(
        "mcp_servers",
        sa.Column("oauth_client_secret_ct", sa.LargeBinary, nullable=True),
    )
    op.add_column("mcp_servers", sa.Column("oauth_scopes", sa.Text, nullable=True))
    op.add_column("mcp_servers", sa.Column("oauth_audience", sa.Text, nullable=True))
    op.add_column("mcp_servers", sa.Column("oauth_registration_mode", sa.Text, nullable=True))
    op.add_column(
        "mcp_servers",
        sa.Column("oauth_authorization_server_url", sa.Text, nullable=True),
    )
    op.add_column("mcp_servers", sa.Column("oauth_as_issuer_cached", sa.Text, nullable=True))

    # Normalize the no-static-headers case after the column exists.
    # Restricted to streamable-http rows — for stdio rows the column
    # value is opaque (auth_type is meaningless when there is no HTTP
    # transport to attach headers to), so leave them at the 'static'
    # default.
    # NOTE: lossy on downgrade — once a row is rewritten to 'none', the
    # previous distinction (server_default 'static' vs explicit 'none')
    # cannot be recovered from the DB alone.
    op.execute(
        "UPDATE mcp_servers SET auth_type = 'none' "
        "WHERE transport = 'streamable-http' "
        "AND (headers IS NULL OR headers = '' OR headers = '{}')"
    )


def downgrade() -> None:
    # Reverse order of upgrade; column drops mirror add_column calls.
    op.drop_column("mcp_servers", "oauth_as_issuer_cached")
    op.drop_column("mcp_servers", "oauth_authorization_server_url")
    op.drop_column("mcp_servers", "oauth_registration_mode")
    op.drop_column("mcp_servers", "oauth_audience")
    op.drop_column("mcp_servers", "oauth_scopes")
    op.drop_column("mcp_servers", "oauth_client_secret_ct")
    op.drop_column("mcp_servers", "oauth_client_id")
    op.drop_column("mcp_servers", "auth_type")

    op.drop_index("idx_mcp_pending_created", table_name="mcp_oauth_pending")
    op.drop_table("mcp_oauth_pending")
    op.drop_table("mcp_user_tokens")
