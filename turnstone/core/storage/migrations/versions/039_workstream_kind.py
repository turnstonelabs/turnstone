"""Add kind + parent_ws_id columns to workstreams.

The 1.5 release promotes "coordinator" behavior to a first-class workstream
kind hosted by the console.  Two new columns gate the classification:

- ``kind``         — ``"interactive"`` (existing) | ``"coordinator"`` (new).
- ``parent_ws_id`` — non-NULL on children spawned by a coordinator; NULL on
                     top-level workstreams (including coordinators themselves).

Both columns are indexed so ``list_workstreams(kind=..., parent_ws_id=...)``
filters stay cheap on both SQLite and PostgreSQL backends.  ``NOT NULL``
default ``'interactive'`` on ``kind`` keeps existing rows valid; ``parent_ws_id``
is nullable because most workstreams have no parent.

.. warning::

   ``downgrade()`` drops both columns, which **irreversibly destroys**
   every ``parent_ws_id`` value.  Coordinator → child lineage cannot be
   reconstructed after a downgrade on a populated DB.  If you need to
   roll back on a production cluster, export the workstreams table
   first (``SELECT ws_id, kind, parent_ws_id FROM workstreams``) so
   the relationships can be re-applied after re-upgrading.

   The index-tuning follow-up (migration 041) assumes this migration
   is applied — downgrading past 039 also invalidates 041's downgrade
   path, so revert migrations in order (041 → 040 → 039) rather than
   skipping.

Revision ID: 039
Revises: 038
Create Date: 2026-04-16
"""

import sqlalchemy as sa
from alembic import op

revision = "039"
down_revision = "038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workstreams",
        sa.Column(
            "kind",
            sa.Text,
            nullable=False,
            server_default="interactive",
        ),
    )
    op.add_column(
        "workstreams",
        sa.Column("parent_ws_id", sa.Text, nullable=True),
    )
    op.create_index(
        "idx_workstreams_kind",
        "workstreams",
        ["kind"],
    )
    op.create_index(
        "idx_workstreams_parent",
        "workstreams",
        ["parent_ws_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_workstreams_parent", table_name="workstreams")
    op.drop_index("idx_workstreams_kind", table_name="workstreams")
    op.drop_column("workstreams", "parent_ws_id")
    op.drop_column("workstreams", "kind")
