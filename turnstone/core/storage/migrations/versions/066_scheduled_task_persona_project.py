"""Add persona + project settings to scheduled tasks.

A scheduled task dispatches a fresh workstream every firing.  Until now it
could pin the model and skill of that workstream but not its **persona** or
**project** — the two levers a manually-created workstream already carries
(``workstreams.persona`` from migration 063, ``workstreams.project_id`` from
062).  These two columns close that gap so a schedule can run under, say, the
``researcher`` persona attached to a specific project's memory bucket.

Both are ``Text NOT NULL DEFAULT ''`` following the ``scheduled_tasks``
convention (``model``/``skill`` use the same shape): empty = "kind default
persona" / "no project", exactly as an empty model means "default model".  The
values are stamped verbatim onto ``create_workstream`` at dispatch, where the
node resolves the persona for the workstream kind and gates the project attach
(``console/scheduler.py::_dispatch_to_node`` → ``/v1/api/workstreams/new``);
nothing is resolved or enforced at migration time.  Existing rows migrate
cleanly to the empty default — byte-identical dispatch behaviour to pre-066.
Additive and reversible.

Revision ID: 066
Revises: 065
Create Date: 2026-07-08
"""

import sqlalchemy as sa
from alembic import op

revision = "066"
down_revision = "065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.add_column(sa.Column("persona", sa.Text, nullable=False, server_default=""))
        batch_op.add_column(sa.Column("project_id", sa.Text, nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("project_id")
        batch_op.drop_column("persona")
