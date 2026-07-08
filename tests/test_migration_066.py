"""Tests for alembic migration 066 (persona + project on scheduled_tasks).

Drives ``command.upgrade``/``downgrade`` against an isolated SQLite database per
test (the 060/062/063/065 harness pattern), then asserts:

* upgrade adds the ``persona`` and ``project_id`` columns to ``scheduled_tasks``;
* a pre-066 scheduled task migrates cleanly, gaining ``""`` for both new columns
  — the empty default that means "kind default persona" / "no project" and
  preserves byte-identical dispatch behaviour to pre-066;
* downgrade removes both columns, returning ``scheduled_tasks`` to its exact
  pre-066 shape — pinning the clean-rollback guarantee;
* up -> down -> up lands cleanly with no leftover-column conflict.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

_MIGRATIONS_DIR = str(
    Path(__file__).resolve().parent.parent / "turnstone" / "core" / "storage" / "migrations"
)


def _alembic_cfg(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS_DIR)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _insert_pre066_task(engine: sa.Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_tasks "
                "(task_id, name, schedule_type, initial_message, created, updated) "
                "VALUES ('t1', 'Nightly', 'cron', 'run', "
                "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
            )
        )


class TestMigration066:
    def test_upgrade_adds_persona_and_project_columns(self, tmp_path: Path) -> None:
        db_path = tmp_path / "066-up.db"
        command.upgrade(_alembic_cfg(db_path), "066")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            cols = {c["name"] for c in sa.inspect(engine).get_columns("scheduled_tasks")}
            assert {"persona", "project_id"} <= cols
        finally:
            engine.dispose()

    def test_preexisting_row_migrates_with_empty_default(self, tmp_path: Path) -> None:
        db_path = tmp_path / "066-default.db"
        cfg = _alembic_cfg(db_path)
        # Stop at 065, insert a pre-066 scheduled task, THEN upgrade to 066.
        command.upgrade(cfg, "065")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            _insert_pre066_task(engine)
            command.upgrade(cfg, "066")
            with engine.connect() as conn:
                row = conn.execute(
                    sa.text("SELECT persona, project_id FROM scheduled_tasks WHERE task_id = 't1'")
                ).fetchone()
            assert row is not None
            assert row[0] == "" and row[1] == ""
        finally:
            engine.dispose()

    def test_downgrade_removes_persona_and_project_columns(self, tmp_path: Path) -> None:
        db_path = tmp_path / "066-down.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "066")
        command.downgrade(cfg, "065")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            cols = {c["name"] for c in sa.inspect(engine).get_columns("scheduled_tasks")}
            assert "persona" not in cols and "project_id" not in cols
        finally:
            engine.dispose()

    def test_downgrade_then_upgrade_round_trip(self, tmp_path: Path) -> None:
        """up -> down -> up must land cleanly (no leftover column conflict)."""
        db_path = tmp_path / "066-roundtrip.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "066")
        command.downgrade(cfg, "065")
        command.upgrade(cfg, "066")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            cols = {c["name"] for c in sa.inspect(engine).get_columns("scheduled_tasks")}
            assert {"persona", "project_id"} <= cols
        finally:
            engine.dispose()
