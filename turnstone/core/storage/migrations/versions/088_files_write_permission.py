"""Grant direct file uploads to builtin admin and operator roles.

Only the role baseline changes. Existing grant/revoke overrides remain in
place, and viewer and custom roles are unchanged.

Revision ID: 088
Revises: 087
Create Date: 2026-11-01
"""

import sqlalchemy as sa
from alembic import op

revision = "088"
down_revision = "087"
branch_labels = None
depends_on = None


def _set_permission(*, granted: bool) -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT role_id, permissions FROM roles "
            "WHERE role_id IN ('builtin-admin', 'builtin-operator')"
        )
    ).fetchall()
    for role_id, baseline in rows:
        permissions = [p.strip() for p in baseline.split(",") if p.strip()]
        if granted:
            if "files.write" in permissions:
                continue
            permissions.append("files.write")
        else:
            permissions = [p for p in permissions if p != "files.write"]
        conn.execute(
            sa.text("UPDATE roles SET permissions = :permissions WHERE role_id = :role_id"),
            {"permissions": ",".join(permissions), "role_id": role_id},
        )


def upgrade() -> None:
    _set_permission(granted=True)


def downgrade() -> None:
    _set_permission(granted=False)
