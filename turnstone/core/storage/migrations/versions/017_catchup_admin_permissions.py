"""Catch-up: ensure builtin-admin role has all current permissions.

Migrations 011-016 each appended a permission to the builtin-admin role,
but on some deployments these UPDATE statements did not take effect
(e.g. due to version stamping without running, or create_all bypassing
Alembic). This migration idempotently ensures the builtin-admin role
has the complete permission set.

Revision ID: 017
Revises: 016
Create Date: 2026-03-14
"""

import sqlalchemy as sa
from alembic import op

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None

# The complete set of permissions the builtin-admin role should have.
# Must stay in sync with _VALID_PERMISSIONS in console/server.py.
_EXPECTED_ADMIN_PERMS = (
    "read,write,approve,"
    "admin.users,admin.roles,admin.orgs,"
    "admin.policies,admin.templates,admin.ws_templates,"
    "admin.audit,admin.usage,"
    "admin.schedules,admin.watches,"
    "admin.judge,admin.memories,admin.settings,admin.mcp,"
    "tools.approve,workstreams.create,workstreams.close"
)


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("UPDATE roles SET permissions = :perms WHERE role_id = 'builtin-admin'"),
        {"perms": _EXPECTED_ADMIN_PERMS},
    )


def downgrade() -> None:
    # No-op: we don't remove permissions on downgrade since we can't
    # know which subset the deployment originally had.
    pass
