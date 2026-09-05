"""Add the evaluation time zone to scheduled tasks.

A recurring schedule's cron has no zone of its own, and until now the
scheduler evaluated every expression in UTC.  ``timezone`` names the IANA zone
(``America/New_York``) the cron's wall-clock fields mean, so "02:30 daily"
keeps firing at 02:30 local on either side of a daylight-saving change and a
weekly day is the local day — a client-side conversion to a UTC cron would
drift by an hour and shift the day near midnight.  ``next_run`` stays stored
in UTC; only the cron walk (``console/schedule_timing.py::next_cron_runs``)
moves into the zone.

``Text NOT NULL DEFAULT 'UTC'`` following the ``scheduled_tasks`` convention:
every existing row keeps its meaning byte-for-byte, since UTC is the zone it
was always evaluated in.  One-shot (``at``) schedules carry their own offset
and ignore the column.  Additive and reversible.

Revision ID: 073
Revises: 072
Create Date: 2026-09-04
"""

import sqlalchemy as sa
from alembic import op

revision = "073"
down_revision = "072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.add_column(sa.Column("timezone", sa.Text, nullable=False, server_default="UTC"))


def downgrade() -> None:
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("timezone")
