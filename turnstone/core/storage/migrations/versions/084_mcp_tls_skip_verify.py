"""Add the per-server switch that turns off TLS certificate verification for an MCP server.

``tls_skip_verify`` lets an operator connect to a streamable-http server whose certificate
cannot be added to the trust store, such as a device that generates its own self-signed
certificate. Zero keeps verification on for every existing server.

Revision ID: 084
Revises: 083
Create Date: 2026-10-25
"""

import sqlalchemy as sa
from alembic import op

revision = "084"
down_revision = "083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("mcp_servers") as batch_op:
        batch_op.add_column(
            sa.Column("tls_skip_verify", sa.Integer, nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("mcp_servers") as batch_op:
        batch_op.drop_column("tls_skip_verify")
