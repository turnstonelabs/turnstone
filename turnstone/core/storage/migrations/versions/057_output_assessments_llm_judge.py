"""Extend output_assessments with LLM-judge fields.

Adds four columns to ``output_assessments`` so the same table holds both
heuristic (regex) verdicts and the new LLM-judge verdicts introduced for
issue #560 mitigation #1:

* ``tier`` — ``'heuristic'`` (the regex stage) or ``'llm'`` (the new
  capability-gated semantic evaluator). Existing rows backfill to
  ``'heuristic'`` because that is what the table held before this
  migration. One row per ``(call_id, tier)`` from this point on, mirroring
  the ``intent_verdicts`` table's row model (migration 012).
* ``reasoning`` — the LLM's free-form explanation. Empty for heuristic rows.
* ``judge_model`` — the model alias used. Empty for heuristic rows.
* ``latency_ms`` — wall-clock cost. ``0`` for heuristic rows (regex is
  microseconds-scale and not separately tracked).

Revision ID: 057
Revises: 056
Create Date: 2026-05-23

Originally drafted as 056 alongside PR #574 (skill spec uplift); bumped
to 057 to keep the Alembic chain linear after the conflict.  There is
no ordering dependency between the two — output_assessments and
prompt_templates are independent tables — but Alembic requires a
single head, and the skills uplift was opened first.
"""

import sqlalchemy as sa
from alembic import op

revision = "057"
down_revision = "056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("output_assessments") as batch:
        batch.add_column(sa.Column("tier", sa.Text, nullable=False, server_default="heuristic"))
        batch.add_column(sa.Column("reasoning", sa.Text, nullable=False, server_default=""))
        batch.add_column(sa.Column("judge_model", sa.Text, nullable=False, server_default=""))
        batch.add_column(sa.Column("latency_ms", sa.Integer, nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("output_assessments") as batch:
        batch.drop_column("latency_ms")
        batch.drop_column("judge_model")
        batch.drop_column("reasoning")
        batch.drop_column("tier")
