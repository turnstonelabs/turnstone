"""Drop coord spawn-quota settings rows.

The coordinator spawn-quota subsystem (``SpawnBudget`` / ``TokenBucket``
gating ``spawn_workstream``) is gone — ``max_active`` slot exhaustion
already bounds runaway spawns and surfaces as a tool error. The three
registry keys that configured it have been removed from
``settings_registry``; this migration clears any persisted rows so
upgraded deployments don't log "Skipping invalid setting" warnings
for them on every startup.

Downgrade is a no-op: the deleted rows were operator-set values; once
gone they're gone. If a deployment rolls back to pre-1.5.0 code, the
registry at that point still has the keys and ``reload()`` falls back
to the registry default (20 / 5.0 / 10) for any key not present in
``system_settings``.

Revision ID: 047
Revises: 046
Create Date: 2026-04-23
"""

import sqlalchemy as sa
from alembic import op

revision = "047"
down_revision = "046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM system_settings WHERE key IN ("
            "'coordinator.spawn_budget', "
            "'coordinator.spawn_rate.tokens_per_minute', "
            "'coordinator.spawn_rate.burst')"
        )
    )


def downgrade() -> None:
    pass
