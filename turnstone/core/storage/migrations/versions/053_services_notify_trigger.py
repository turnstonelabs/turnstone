"""Trigger ``pg_notify('services', ...)`` on service registry changes.

The console-side :class:`NotifyDispatcher` (`turnstone/console/notify_dispatcher.py`)
holds a dedicated LISTEN connection and fans channel events out to handlers.
This migration installs the producer side for the ``services`` channel —
the cluster collector subscribes so new-node discovery is reactive instead
of polling every 60 s.

The trigger filters heartbeat-only UPDATEs in-trigger (same url + same
metadata, only ``last_heartbeat`` changed): ``register_service`` is an
UPSERT, so a node restart that changes url/metadata still fires; a plain
heartbeat tick stays quiet to avoid flooding the channel on every
30 s × N-nodes cluster tick.  Channel payload is a small JSON object —
service_type, service_id, op — well below PG's 8 KiB NOTIFY limit; the
handler reconciles by re-reading ``services`` rather than relying on
the payload content.

SQLite is a no-op for this migration — the SQLite backend's in-process
:meth:`notify` doesn't go through a trigger, and the synthetic-sweep
fallback in :meth:`listen` covers consumer parity.

What this trigger does NOT cover: crashed-node detection.  A node that
dies without running its deregister handshake leaves a stale row that
ages out via the existing 120 s heartbeat-expiry filter.  The 60 s
discovery loop in the collector keeps running as the backstop for
crash-shaped node loss.

Revision ID: 053
Revises: 052
Create Date: 2026-05-10
"""

import sqlalchemy as sa
from alembic import op

from turnstone.core.storage._schema import (
    SERVICES_NOTIFY_TRIGGER_FN_NAME,
    SERVICES_NOTIFY_TRIGGER_FN_SQL,
    SERVICES_NOTIFY_TRIGGER_NAME,
    SERVICES_NOTIFY_TRIGGER_SQL,
)

revision = "053"
down_revision = "052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(sa.text(SERVICES_NOTIFY_TRIGGER_FN_SQL))
    op.execute(sa.text(SERVICES_NOTIFY_TRIGGER_SQL))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {SERVICES_NOTIFY_TRIGGER_NAME} ON services"))
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {SERVICES_NOTIFY_TRIGGER_FN_NAME}()"))
