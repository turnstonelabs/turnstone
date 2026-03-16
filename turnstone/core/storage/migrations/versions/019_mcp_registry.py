"""Add registry provenance columns to mcp_servers.

Revision ID: 019
Revises: 018
Create Date: 2026-03-15
"""

import sqlalchemy as sa
from alembic import op

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mcp_servers", sa.Column("registry_name", sa.Text, nullable=True))
    op.add_column(
        "mcp_servers",
        sa.Column("registry_version", sa.Text, nullable=False, server_default=""),
    )
    op.add_column(
        "mcp_servers",
        sa.Column("registry_meta", sa.Text, nullable=False, server_default="{}"),
    )
    op.execute(
        "CREATE UNIQUE INDEX idx_mcp_servers_registry_name "
        "ON mcp_servers(registry_name) WHERE registry_name IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("idx_mcp_servers_registry_name")
    op.drop_column("mcp_servers", "registry_meta")
    op.drop_column("mcp_servers", "registry_version")
    op.drop_column("mcp_servers", "registry_name")
