"""Grant admin.coordinator + admin.cluster.inspect to the builtin-admin role.

Phase 1 added ``admin.coordinator`` (coordinator workstream lifecycle)
and phase 3 added ``admin.cluster.inspect`` (cluster-wide live
workstream inspect) to the permission set.  Both were left off
``builtin-admin`` on introduction with the expectation that operators
would opt in per-user, but the console exposes no UI for modifying
built-in roles — granting either permission required creating a
custom role or running manual SQL.

Append both to ``builtin-admin`` so admins have out-of-box access to
the features they're reasonably expected to use, following the pattern
established by migrations 028 (admin.models) and 035 (admin.nodes).
Idempotent: each UPDATE is guarded by NOT LIKE so re-runs don't stack
the permission string.

Revision ID: 040
Revises: 039
Create Date: 2026-04-17
"""

import sqlalchemy as sa
from alembic import op

revision = "040"
down_revision = "039"
branch_labels = None
depends_on = None


def _append_permission(conn: sa.engine.Connection, perm: str) -> None:
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = permissions || :sep "
            "WHERE role_id = 'builtin-admin' "
            "AND permissions NOT LIKE :needle"
        ),
        {"sep": "," + perm, "needle": "%" + perm + "%"},
    )


def _remove_permission(conn: sa.engine.Connection, perm: str) -> None:
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = REPLACE(permissions, :needle, '') "
            "WHERE role_id = 'builtin-admin'"
        ),
        {"needle": "," + perm},
    )


def upgrade() -> None:
    conn = op.get_bind()
    for perm in ("admin.coordinator", "admin.cluster.inspect"):
        _append_permission(conn, perm)


def downgrade() -> None:
    conn = op.get_bind()
    for perm in ("admin.cluster.inspect", "admin.coordinator"):
        _remove_permission(conn, perm)
