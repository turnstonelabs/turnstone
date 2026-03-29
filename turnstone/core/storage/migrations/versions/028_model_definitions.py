"""Create model_definitions table and grant admin.models permission.

Revision ID: 028
Revises: 027
Create Date: 2026-03-29
"""

import sqlalchemy as sa
from alembic import op

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_definitions",
        sa.Column("definition_id", sa.Text, primary_key=True),
        sa.Column("alias", sa.Text, nullable=False, unique=True),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("provider", sa.Text, nullable=False, server_default="openai"),
        sa.Column("base_url", sa.Text, nullable=False, server_default=""),
        sa.Column("api_key", sa.Text, nullable=False, server_default=""),
        sa.Column("context_window", sa.Integer, nullable=False, server_default="32768"),
        sa.Column("capabilities", sa.Text, nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_by", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )
    op.create_index("idx_model_definitions_enabled", "model_definitions", ["enabled"])

    # Grant admin.models permission to the built-in admin role
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = permissions || ',admin.models' "
            "WHERE role_id = 'builtin-admin' "
            "AND permissions NOT LIKE '%admin.models%'"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = REPLACE(permissions, ',admin.models', '') "
            "WHERE role_id = 'builtin-admin'"
        )
    )
    op.drop_table("model_definitions")
