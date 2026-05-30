"""Add ``event_id`` to ``conversations`` for SSE cursor-resume.

The SSE layer stamps every fanned-out event with a per-workstream
monotonic ``_event_id`` — the ``Last-Event-ID`` replay coordinate held
in the in-memory ring buffer on ``SessionUIBase``.  Persisting that
high-water mark on each saved message lets ``/history`` hand back a
*resume cursor* in the same id-space the ring buffer slices on: the
client opens its initial SSE with the cursor as ``Last-Event-ID`` and
the existing delta-replay path fast-forwards the in-flight turn (tool
blocks, results, approve/plan prompts) instead of the lossy synthetic
snapshot.  It is also the durable anchor that reseeds the in-memory
counter across process restarts (it resets to 0 otherwise, which would
collide ids post-reopen).

Nullable: historical rows (and bulk/fork re-saves) predate or omit the
counter and stay NULL; the ``/history`` cursor logic treats a
missing/old cursor as "no fast-forward available" and falls back to the
synthetic snapshot floor.  ``BigInteger`` because the per-ws counter is
monotonic across the workstream's whole life (reseeded from
``MAX(event_id)`` on reopen), so a long-lived, high-throughput
workstream can exceed 2**31 — distinct from the autoincrement ``id``
PK, which counts messages, not events.

Revision ID: 059
Revises: 058
Create Date: 2026-05-30
"""

import sqlalchemy as sa
from alembic import op

revision = "059"
down_revision = "058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(sa.Column("event_id", sa.BigInteger(), nullable=True))
    # Composite (ws_id, event_id) index: makes the per-ws
    # ``SELECT MAX(event_id)`` reseed an index seek instead of a scan over
    # the workstream's rows, and is the index the per-ws event-cursor
    # queries want.  Cheap on this LLM-paced insert workload.
    op.create_index("idx_conversations_ws_event", "conversations", ["ws_id", "event_id"])


def downgrade() -> None:
    op.drop_index("idx_conversations_ws_event", table_name="conversations")
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("event_id")
