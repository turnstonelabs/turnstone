"""Re-key ``coordinator``-scope memories from ws_id to the owner's user_id.

The ``coordinator`` memory scope used to anchor on the coordinator session's
``ws_id``, so the namespace was born empty with every new coordinator session
and its rows were orphaned the moment that session closed — coordinator
memory never actually persisted.  The scope now anchors on the coordinator's
creator ``user_id`` (one durable orchestration namespace per user, shared by
all of that user's coordinator sessions).  This migration carries the
existing rows across:

1. **Delete unattributable rows** — ``scope='coordinator'`` rows whose
   ``scope_id`` matches no ``workstreams.ws_id``, or whose owning workstream
   has a NULL/empty ``user_id`` (a pre-auth-guard anomaly: coordinator
   sessions now refuse to construct without an authenticated user).  Such
   rows cannot be assigned to any user and would be unreachable forever
   under user keying — for a private, fail-closed scope they are deleted
   rather than left as permanent dead rows.  This is intentionally lossy.
2. **Dedup colliding names** — two coordinator sessions of the same user
   could each hold a memory with the same ``name`` (distinct ws_id
   scope_ids).  After re-keying both would map to the same
   ``(name, 'coordinator', user_id)`` identity and violate
   ``uq_smem_name_scope``; keep the most recently ``updated`` row (ties
   broken by ``memory_id`` for determinism) and delete the rest.
   ``updated`` is a fixed-width ``%Y-%m-%dT%H:%M:%S`` string, so
   lexicographic order is chronological.
3. **Re-key** — set ``scope_id`` to the owning workstream's ``user_id``.

``downgrade()`` is a documented no-op: there is no schema delta, and the
data transform is not reversible (deleted orphans are gone; deduped rows are
gone; the many-old-namespaces → one-user-namespace collapse cannot be
unsplit).  Pre-061 code simply sees the user-keyed rows as not-visible, the
same way it saw any closed session's rows.

Revision ID: 061
Revises: 060
Create Date: 2026-06-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "061"
down_revision = "060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Drop rows that cannot be attributed to a user.
    conn.execute(
        sa.text(
            "DELETE FROM structured_memories "
            "WHERE scope = 'coordinator' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM workstreams w "
            "  WHERE w.ws_id = structured_memories.scope_id "
            "    AND w.user_id IS NOT NULL AND w.user_id != ''"
            ")"
        )
    )

    # 2. Within each post-rekey identity (name, owner user_id), keep only
    #    the newest row.  Every remaining row joins to an owning
    #    workstream with a non-empty user_id (step 1 guarantees it).
    conn.execute(
        sa.text(
            "DELETE FROM structured_memories "
            "WHERE scope = 'coordinator' "
            "AND EXISTS ("
            "  SELECT 1 "
            "  FROM structured_memories s2 "
            "  JOIN workstreams w2 ON w2.ws_id = s2.scope_id "
            "  JOIN workstreams w1 ON w1.ws_id = structured_memories.scope_id "
            "  WHERE s2.scope = 'coordinator' "
            "    AND s2.name = structured_memories.name "
            "    AND w2.user_id = w1.user_id "
            "    AND s2.memory_id != structured_memories.memory_id "
            "    AND (s2.updated > structured_memories.updated "
            "         OR (s2.updated = structured_memories.updated "
            "             AND s2.memory_id > structured_memories.memory_id))"
            ")"
        )
    )

    # 3. Re-key the survivors onto their owner's user_id.
    conn.execute(
        sa.text(
            "UPDATE structured_memories "
            "SET scope_id = ("
            "  SELECT w.user_id FROM workstreams w "
            "  WHERE w.ws_id = structured_memories.scope_id"
            ") "
            "WHERE scope = 'coordinator'"
        )
    )


def downgrade() -> None:
    # Irreversible data transform — see module docstring.  No schema delta,
    # so there is nothing structural to undo either.
    pass
