"""Grant admin.prompt_policies permission to builtin-admin role.

Migration 031 created the prompt_policies table but did not add the
corresponding permission to the builtin-admin role, causing 403 on
/v1/api/admin/prompt-policies for all users.

Revision ID: 032
Revises: 031
Create Date: 2026-04-05
"""

import sqlalchemy as sa
from alembic import op

revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = permissions || ',admin.prompt_policies' "
            "WHERE role_id = 'builtin-admin' "
            "AND permissions NOT LIKE '%admin.prompt_policies%'"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = REPLACE(permissions, ',admin.prompt_policies', '') "
            "WHERE role_id = 'builtin-admin'"
        )
    )
