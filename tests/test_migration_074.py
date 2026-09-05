"""Tests for alembic migration 074 (one-shot next_run folded to naive UTC).

Drives ``command.upgrade``/``downgrade`` against an isolated SQLite database per
test (the 066/073 harness pattern), then asserts:

* a pending one-shot whose ``next_run`` carries an offset is rewritten to the
  naive-UTC instant it names, with ``at_time`` untouched;
* a one-shot already in the naive shape, an empty ``next_run`` (a fired or
  disabled one-shot), a value that does not parse and a cron row are left
  alone;
* downgrade is a no-op — the rewritten value is what the due query always
  read, and ``at_time`` still holds the submitted value.
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


_ROWS = [
    # task_id, schedule_type, at_time, next_run
    ("offset", "at", "2030-01-01T12:00:00+05:30", "2030-01-01T12:00:00+05:30"),
    ("negative", "at", "2030-01-01T12:00:00-05:00", "2030-01-01T12:00:00-05:00"),
    ("zulu", "at", "2030-01-01T12:00:00Z", "2030-01-01T12:00:00Z"),
    ("naive", "at", "2030-01-01T12:00:00+00:00", "2030-01-01T12:00:00"),
    ("fired", "at", "2020-01-01T12:00:00+05:30", ""),
    ("junk", "at", "2030-01-01T12:00:00+05:30", "not-a-timestamp"),
    ("cron", "cron", "", "2030-01-01T09:00:00"),
]


def _insert_pre074_rows(engine: sa.Engine) -> None:
    with engine.begin() as conn:
        for task_id, schedule_type, at_time, next_run in _ROWS:
            conn.execute(
                sa.text(
                    "INSERT INTO scheduled_tasks "
                    "(task_id, name, schedule_type, at_time, next_run, initial_message, "
                    "created, updated) VALUES (:task_id, :task_id, :schedule_type, :at_time, "
                    ":next_run, 'run', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
                ),
                {
                    "task_id": task_id,
                    "schedule_type": schedule_type,
                    "at_time": at_time,
                    "next_run": next_run,
                },
            )


def _rows(engine: sa.Engine) -> dict[str, tuple[str, str]]:
    with engine.connect() as conn:
        return {
            r[0]: (r[1], r[2])
            for r in conn.execute(sa.text("SELECT task_id, at_time, next_run FROM scheduled_tasks"))
        }


_EXPECTED = {
    "offset": ("2030-01-01T12:00:00+05:30", "2030-01-01T06:30:00"),
    "negative": ("2030-01-01T12:00:00-05:00", "2030-01-01T17:00:00"),
    "zulu": ("2030-01-01T12:00:00Z", "2030-01-01T12:00:00"),
    "naive": ("2030-01-01T12:00:00+00:00", "2030-01-01T12:00:00"),
    "fired": ("2020-01-01T12:00:00+05:30", ""),
    # A value that does not parse is left alone rather than failing the upgrade.
    "junk": ("2030-01-01T12:00:00+05:30", "not-a-timestamp"),
    "cron": ("", "2030-01-01T09:00:00"),
}


class TestMigration074:
    def test_upgrade_folds_the_offset_and_leaves_the_rest(self, tmp_path: Path) -> None:
        db_path = tmp_path / "074-up.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "073")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            _insert_pre074_rows(engine)
            command.upgrade(cfg, "074")
            assert _rows(engine) == _EXPECTED
        finally:
            engine.dispose()

    def test_downgrade_is_a_no_op(self, tmp_path: Path) -> None:
        db_path = tmp_path / "074-down.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "073")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            _insert_pre074_rows(engine)
            command.upgrade(cfg, "074")
            command.downgrade(cfg, "073")
            assert _rows(engine) == _EXPECTED
            command.upgrade(cfg, "074")
            assert _rows(engine) == _EXPECTED
        finally:
            engine.dispose()
