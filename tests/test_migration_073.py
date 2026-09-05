"""Tests for alembic migration 073 (evaluation time zone on scheduled_tasks).

Drives ``command.upgrade``/``downgrade`` against an isolated SQLite database per
test (the 066 harness pattern), then asserts:

* upgrade adds the ``timezone`` column to ``scheduled_tasks``;
* a pre-073 scheduled task migrates to ``UTC`` — the zone its cron was always
  evaluated in, so its firing times are unchanged;
* downgrade removes the column, returning ``scheduled_tasks`` to its exact
  pre-073 shape;
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


def _insert_pre073_task(engine: sa.Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_tasks "
                "(task_id, name, schedule_type, cron_expr, initial_message, created, updated) "
                "VALUES ('t1', 'Nightly', 'cron', '30 2 * * *', 'run', "
                "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
            )
        )


class TestMigration073:
    def test_upgrade_adds_timezone_column(self, tmp_path: Path) -> None:
        db_path = tmp_path / "073-up.db"
        command.upgrade(_alembic_cfg(db_path), "073")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            cols = {c["name"] for c in sa.inspect(engine).get_columns("scheduled_tasks")}
            assert "timezone" in cols
        finally:
            engine.dispose()

    def test_preexisting_row_migrates_to_utc(self, tmp_path: Path) -> None:
        db_path = tmp_path / "073-default.db"
        cfg = _alembic_cfg(db_path)
        # Stop at 072, insert a pre-073 scheduled task, THEN upgrade to 073.
        command.upgrade(cfg, "072")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            _insert_pre073_task(engine)
            command.upgrade(cfg, "073")
            with engine.connect() as conn:
                row = conn.execute(
                    sa.text("SELECT timezone, cron_expr FROM scheduled_tasks WHERE task_id = 't1'")
                ).fetchone()
            assert row is not None
            assert row[0] == "UTC"
            assert row[1] == "30 2 * * *", "the expression itself is never rewritten"
        finally:
            engine.dispose()

    def test_downgrade_removes_timezone_column(self, tmp_path: Path) -> None:
        db_path = tmp_path / "073-down.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "073")
        command.downgrade(cfg, "072")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            cols = {c["name"] for c in sa.inspect(engine).get_columns("scheduled_tasks")}
            assert "timezone" not in cols
        finally:
            engine.dispose()

    def test_downgrade_then_upgrade_round_trip(self, tmp_path: Path) -> None:
        """up -> down -> up must land cleanly (no leftover column conflict)."""
        db_path = tmp_path / "073-roundtrip.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "073")
        command.downgrade(cfg, "072")
        command.upgrade(cfg, "073")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            cols = {c["name"] for c in sa.inspect(engine).get_columns("scheduled_tasks")}
            assert "timezone" in cols
        finally:
            engine.dispose()
