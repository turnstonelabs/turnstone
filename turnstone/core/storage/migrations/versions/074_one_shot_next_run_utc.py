"""Fold a one-shot schedule's offset into its stored next run.

A one-shot's ``at_time`` (offset-bearing ISO 8601) was stored verbatim as
its ``next_run``, while the due query compares ``next_run`` as a string
against a naive-UTC clock, so a pending one-shot with a non-UTC offset was
due at the wrong instant (``2030-01-01T12:00:00+05:30`` became due at 12:00
UTC, not 06:30 UTC).  The console handlers fold the offset in from this
release on; this rewrites the rows already stored, so a one-shot created
before the upgrade fires at the instant it names.  ``at_time`` is untouched.

The downgrade is a no-op: a naive-UTC ``next_run`` is what the due query
always read, and ``at_time`` still holds the submitted value.

Revision ID: 074
Revises: 073
Create Date: 2026-09-05
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "074"
down_revision = "073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT task_id, next_run FROM scheduled_tasks "
            "WHERE schedule_type = 'at' AND next_run IS NOT NULL AND next_run != ''"
        )
    ).fetchall()
    for task_id, next_run in rows:
        try:
            when = datetime.fromisoformat(next_run)
        except ValueError:
            continue
        if when.tzinfo is None:
            continue  # already the naive-UTC shape the due query reads
        bind.execute(
            sa.text("UPDATE scheduled_tasks SET next_run = :next_run WHERE task_id = :task_id"),
            {
                "next_run": when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
                "task_id": task_id,
            },
        )


def downgrade() -> None:
    pass
