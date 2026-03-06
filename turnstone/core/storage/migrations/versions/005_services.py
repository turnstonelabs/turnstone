"""Service registry table.

Revision ID: 005
Revises: 004
Create Date: 2026-03-05
"""

import sqlalchemy as sa
from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "services",
        sa.Column("service_type", sa.Text, nullable=False),
        sa.Column("service_id", sa.Text, nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("metadata", sa.Text, nullable=False, server_default="{}"),
        sa.Column("last_heartbeat", sa.Text, nullable=False),
        sa.Column("created", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("service_type", "service_id"),
    )
    op.create_index("idx_services_type_heartbeat", "services", ["service_type", "last_heartbeat"])


def downgrade() -> None:
    op.drop_index("idx_services_type_heartbeat", "services")
    op.drop_table("services")
