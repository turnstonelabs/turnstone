"""Add per-model reasoning-persistence flags to model_definitions.

Adds two boolean (integer-coded) operator knobs:

* ``persist_reasoning`` (default ``1``) — when true, the history-build
  path extracts stored reasoning text from ``provider_data`` and surfaces
  it on each assistant message dict so a page refresh re-renders the
  reasoning bubble. Storage of the reasoning bytes happens regardless;
  this flag only controls the extract-and-include step in
  ``_build_history`` / ``decorate_history_messages``.
* ``replay_reasoning_to_model`` (default ``0``) — when true, the
  wire-build path keeps reasoning blocks in the outgoing
  ``_provider_content`` lane on subsequent provider calls. False is the
  conservative default: spec compliance, lower per-turn cost, no behaviour
  change vs. the pre-flag default. **Phase 1 stores the column but does
  not consume it on the wire**; Phase 2 wires the strip branch in
  ``_anthropic.py``'s ``_convert_messages``.

Mirrors the ``enabled`` column pattern (``_schema.py:659``):
``NOT NULL`` with an integer ``server_default`` so existing rows pick up
the conservative defaults silently on upgrade. Distinct from
``temperature`` / ``max_tokens`` / ``reasoning_effort`` (migration 036)
which are nullable inherit-from-cluster sampling overrides — these are
operator-toggle booleans, never NULL.

Revision ID: 052
Revises: 051
Create Date: 2026-05-08
"""

import sqlalchemy as sa
from alembic import op

revision = "052"
down_revision = "051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_definitions") as batch:
        batch.add_column(
            sa.Column(
                "persist_reasoning",
                sa.Integer,
                nullable=False,
                server_default="1",
            )
        )
        batch.add_column(
            sa.Column(
                "replay_reasoning_to_model",
                sa.Integer,
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("model_definitions") as batch:
        batch.drop_column("replay_reasoning_to_model")
        batch.drop_column("persist_reasoning")
