"""Remove the retired coordinator capacity setting.

Coordinator sessions no longer have a capacity limit. Delete stored global
and node overrides so settings reload does not report an unknown key.

Downgrade leaves the deleted operator values unset; earlier code falls back
to its registry default.

Revision ID: 087
Revises: 086
Create Date: 2026-10-31
"""

import sqlalchemy as sa
from alembic import op

revision = "087"
down_revision = "086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM system_settings WHERE key = 'coordinator.max_active'")
    )


def downgrade() -> None:
    pass
