"""Grant direct file browsing to builtin admin and operator roles.

Only the role baseline changes. Existing grant/revoke overrides remain in
place, and viewer and custom roles are unchanged.

Revision ID: 086
Revises: 085
Create Date: 2026-10-30
"""

import sqlalchemy as sa
from alembic import op

revision = "086"
down_revision = "085"
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
            if "files.read" in permissions:
                continue
            permissions.append("files.read")
        else:
            permissions = [p for p in permissions if p != "files.read"]
        conn.execute(
            sa.text("UPDATE roles SET permissions = :permissions WHERE role_id = :role_id"),
            {"permissions": ",".join(permissions), "role_id": role_id},
        )


def upgrade() -> None:
    _set_permission(granted=True)


def downgrade() -> None:
    _set_permission(granted=False)
