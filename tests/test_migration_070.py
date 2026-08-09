"""Migration coverage for per-alias model concurrency."""

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


class TestMigration070:
    def test_upgrade_defaults_preexisting_rows_to_unlimited(self, tmp_path: Path) -> None:
        db_path = tmp_path / "070-up.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "069")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "INSERT INTO model_definitions "
                        "(definition_id, alias, model, created, updated) "
                        "VALUES ('d1', 'local', 'm', "
                        "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
                    )
                )
            command.upgrade(cfg, "070")
            with engine.connect() as conn:
                value = conn.execute(
                    sa.text(
                        "SELECT max_concurrency FROM model_definitions WHERE definition_id = 'd1'"
                    )
                ).scalar_one()
            assert value == 0
        finally:
            engine.dispose()

    def test_downgrade_then_upgrade_round_trip(self, tmp_path: Path) -> None:
        db_path = tmp_path / "070-roundtrip.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "070")
        command.downgrade(cfg, "069")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            columns = {c["name"] for c in sa.inspect(engine).get_columns("model_definitions")}
            assert "max_concurrency" not in columns
        finally:
            engine.dispose()

        command.upgrade(cfg, "070")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            columns = {c["name"] for c in sa.inspect(engine).get_columns("model_definitions")}
            assert "max_concurrency" in columns
        finally:
            engine.dispose()
