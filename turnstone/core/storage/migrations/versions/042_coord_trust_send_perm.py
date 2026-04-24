"""Add ``coordinator.trust.send`` permission to the builtin-admin role.

Gates the trusted-session mode on ``POST /v1/api/workstreams/{ws_id}/trust``:
when set on a coordinator session, sends to the coordinator's own
children skip the approval prompt.  Foreign ws_ids still go through
approval regardless — the permission only unlocks the toggle, it does
not widen the scope of what trust applies to.

Append to ``builtin-admin`` so admins can opt in without hand-editing
role rows.  Both upgrade and downgrade anchor the permission token to
the three positions it can occupy in a comma-delimited list (sole /
start / mid / end) so a future permission sharing the prefix
(e.g. ``coordinator.trust.send.v2``) cannot collide with the base
token in either direction.

Revision ID: 042
Revises: 041
Create Date: 2026-04-18
"""

import sqlalchemy as sa
from alembic import op

revision = "042"
down_revision = "041"
branch_labels = None
depends_on = None


_PERM = "coordinator.trust.send"


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE roles SET permissions = permissions || :sep "
            "WHERE role_id = 'builtin-admin' "
            "AND permissions NOT LIKE :mid "
            "AND permissions NOT LIKE :start "
            "AND permissions NOT LIKE :end "
            "AND permissions <> :sole"
        ),
        {
            "sep": "," + _PERM,
            "mid": "%," + _PERM + ",%",
            "start": _PERM + ",%",
            "end": "%," + _PERM,
            "sole": _PERM,
        },
    )


def downgrade() -> None:
    conn = op.get_bind()
    # Parse, filter, and rejoin in Python to avoid the SQL-REPLACE
    # prefix-collision hazard (the upgrade anchors against it; the
    # downgrade must mirror that treatment).
    rows = conn.execute(
        sa.text("SELECT role_id, permissions FROM roles WHERE role_id = 'builtin-admin'")
    ).fetchall()
    for row in rows:
        mapping = row._mapping
        raw = mapping.get("permissions") or ""
        current = [p for p in raw.split(",") if p]
        filtered = [p for p in current if p != _PERM]
        if filtered == current:
            continue
        conn.execute(
            sa.text("UPDATE roles SET permissions = :perms WHERE role_id = :role_id"),
            {"perms": ",".join(filtered), "role_id": mapping.get("role_id")},
        )
