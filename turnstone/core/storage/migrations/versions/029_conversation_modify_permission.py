"""Grant conversation.modify permission to admin and operator roles.

Revision ID: 029
Revises: 028
Create Date: 2026-03-29
"""

import sqlalchemy as sa
from alembic import op

revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    # Grant to admin role
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = permissions || ',conversation.modify' "
            "WHERE role_id = 'builtin-admin' "
            "AND permissions NOT LIKE '%conversation.modify%'"
        )
    )
    # Grant to operator role
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = permissions || ',conversation.modify' "
            "WHERE role_id = 'builtin-operator' "
            "AND permissions NOT LIKE '%conversation.modify%'"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = REPLACE(permissions, ',conversation.modify', '') "
            "WHERE role_id IN ('builtin-admin', 'builtin-operator')"
        )
    )
