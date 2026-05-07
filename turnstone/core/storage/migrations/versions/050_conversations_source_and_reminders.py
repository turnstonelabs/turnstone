"""Add ``_source`` and ``_reminders`` columns to ``conversations``.

Persisting these two fields lets multi-tab / multi-device replay show
the same metacognitive bubble shape the originating tab saw live.
Until now, the side-channel keys lived only in the in-memory
``ChatSession.messages`` list, so a second browser tab connecting via
``/history`` saw a synthetic empty wake row missing entirely (skipped
at save time) and any preceding tab's reminder bubbles missing as well.

* ``_source`` — TEXT NULL.  Today only ``"system_nudge"`` is written
  (the wake-driven empty user turn marker).  Future producers can
  extend (e.g. ``"external_webhook"``) without another migration.
* ``_reminders`` — TEXT NULL holding a JSON array of reminder dicts
  ``{type, text, ...optional}``.  Empty / missing column means no
  reminders for that row.

**SQLite upgrade cost.**  Alembic env.py runs migrations with
``render_as_batch=True``, which on SQLite implements ``add_column`` by
recreating the table.  Two ``add_column`` calls = two full-table
copies on first deployment after upgrade.  For installs with months
of chat history (millions of rows) the migration takes seconds to
minutes.  PostgreSQL is unaffected (metadata-only ALTER).

Revision ID: 050
Revises: 049
Create Date: 2026-05-06
"""

import sqlalchemy as sa
from alembic import op

revision = "050"
down_revision = "049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("_source", sa.Text, nullable=True))
    op.add_column("conversations", sa.Column("_reminders", sa.Text, nullable=True))


def downgrade() -> None:
    # Reverse order of upgrade.  ``op.drop_column`` works on SQLite via
    # Alembic batch-mode auto-rebuild and on PostgreSQL natively.
    op.drop_column("conversations", "_reminders")
    op.drop_column("conversations", "_source")
