"""Add ``role_permission_overrides`` for editing builtin role permissions.

Builtin roles (``builtin-admin``, ``builtin-operator``, ``builtin-viewer``)
are seeded by migration 008 and treated as immutable — the ``roles.permissions``
column on those rows is the *baseline* that subsequent feature migrations
extend (most recently ``040_coord_cluster_admin_perms`` and
``042_coord_trust_send_perm``).  Some permissions are deliberately
default-ungranted — ``model.skills.write`` is the motivating case: it gates
the ``skills(action=create|update|...)`` in-process tool path and an
operator should consciously opt themselves in before a coordinator session
can mutate the skill catalog.  Until now there was no UX to grant such a
permission without dropping into SQL.

This table stores per-(role_id, permission) grant/revoke deltas.  The
effective set for a role is computed at permission-load time as
``baseline ∪ {action=grant} − {action=revoke}``; ``roles.permissions``
stays as today (still the baseline on builtin rows, still the full set on
custom rows where overrides do not apply).

Composite PK ``(role_id, permission)`` collapses repeat toggles for the
same permission onto one row.  No FK to ``roles`` — matches the rest of
the governance schema (migration 008 does not declare FKs either) and
keeps the postgres dialect aligned with sqlite.

Revision ID: 057
Revises: 056
Create Date: 2026-05-24
"""

import sqlalchemy as sa
from alembic import op

revision = "057"
down_revision = "056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "role_permission_overrides",
        sa.Column("role_id", sa.Text, nullable=False),
        sa.Column("permission", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("role_id", "permission"),
    )
    op.create_index(
        "idx_role_permission_overrides_role",
        "role_permission_overrides",
        ["role_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_role_permission_overrides_role",
        table_name="role_permission_overrides",
    )
    op.drop_table("role_permission_overrides")
