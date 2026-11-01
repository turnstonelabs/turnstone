"""Removing coordinator capacity preserves other global and node settings."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config


def test_drop_coordinator_cap_preserves_other_settings(tmp_path: Path) -> None:
    path = tmp_path / "coordinator-cap.db"
    cfg = Config()
    cfg.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[1] / "turnstone/core/storage/migrations"),
    )
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(cfg, "086")
    engine = sa.create_engine(f"sqlite:///{path}")
    kept = [
        ("coordinator.model_alias", '"fixture"', ""),
        ("server.max_workstreams", "50", ""),
        ("server.max_workstreams", "7", "node-1"),
    ]
    removed = [
        ("coordinator.max_active", "5", ""),
        ("coordinator.max_active", "12", "console"),
    ]
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO system_settings (key, value, node_id, created, updated) "
                    "VALUES (:key, :value, :node_id, '', '')"
                ),
                [
                    dict(zip(("key", "value", "node_id"), row, strict=True))
                    for row in kept + removed
                ],
            )
        command.upgrade(cfg, "087")
        with engine.connect() as conn:
            assert sorted(
                conn.execute(sa.text("SELECT key, value, node_id FROM system_settings"))
            ) == sorted(kept)

        # Removed operator values cannot be reconstructed on downgrade.
        command.downgrade(cfg, "086")
        command.upgrade(cfg, "087")
        with engine.connect() as conn:
            assert sorted(
                conn.execute(sa.text("SELECT key, value, node_id FROM system_settings"))
            ) == sorted(kept)
    finally:
        engine.dispose()
