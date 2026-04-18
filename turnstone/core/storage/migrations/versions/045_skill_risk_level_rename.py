"""Rename ``prompt_templates.scan_status`` to ``risk_level``.

Pure column rename.  ``risk_level`` aligns with the terminology already
used by :class:`turnstone.core.judge.IntentVerdict` (which carries its
own ``risk_level``) and reads better in the admin UI than
``scan_status`` — the value shape didn't change, the name did.

No index changes — the scanner table has no index involving this
column.  Data is preserved by the RENAME operation on both backends.

Revision ID: 045
Revises: 044
Create Date: 2026-04-18
"""

from alembic import op

revision = "045"
down_revision = "044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("prompt_templates") as batch:
        batch.alter_column("scan_status", new_column_name="risk_level")


def downgrade() -> None:
    with op.batch_alter_table("prompt_templates") as batch:
        batch.alter_column("risk_level", new_column_name="scan_status")
