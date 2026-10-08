"""Migration 079 swaps the usage-event ws_id index for a (ws_id, timestamp) one."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


def _usage_indexes(engine: sa.Engine) -> dict[str, list[str]]:
    return {
        index["name"]: [column for column in index["column_names"] if column]
        for index in sa.inspect(engine).get_indexes("usage_events")
        if index["name"]
    }


def _config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[1] / "turnstone/core/storage/migrations"),
    )
    cfg.set_main_option("sqlalchemy.url", str(url).replace("%", "%%"))
    return cfg


def _roundtrip(url: str) -> None:
    cfg = _config(url)
    command.upgrade(cfg, "078")
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO usage_events (event_id, timestamp, ws_id, prompt_tokens, created) "
                    "VALUES ('e1', '2026-01-01T00:00:00', 'ws-1', 250, '2026-01-01T00:00:00')"
                )
            )
        assert _usage_indexes(engine)["idx_usage_events_ws"] == ["ws_id"]

        command.upgrade(cfg, "079")
        indexes = _usage_indexes(engine)
        assert indexes["idx_usage_events_ws_timestamp"] == ["ws_id", "timestamp"]
        assert "idx_usage_events_ws" not in indexes
        with engine.connect() as conn:
            assert (
                conn.execute(
                    sa.text("SELECT prompt_tokens FROM usage_events WHERE ws_id = 'ws-1'")
                ).scalar_one()
                == 250
            )

        command.downgrade(cfg, "078")
        indexes = _usage_indexes(engine)
        assert indexes["idx_usage_events_ws"] == ["ws_id"]
        assert "idx_usage_events_ws_timestamp" not in indexes

        command.upgrade(cfg, "079")
        assert _usage_indexes(engine)["idx_usage_events_ws_timestamp"] == ["ws_id", "timestamp"]
    finally:
        engine.dispose()


def test_usage_index_migration_sqlite(tmp_path: Path) -> None:
    _roundtrip(f"sqlite:///{tmp_path / 'usage-index.db'}")


def test_usage_index_migration_postgresql(fresh_pg_url: Any) -> None:
    _roundtrip(fresh_pg_url.render_as_string(hide_password=False))


def _index_flags(engine: sa.Engine, name: str) -> tuple[bool, bool] | None:
    """``(valid, unique)`` for the PostgreSQL index *name*, or ``None``."""
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT i.indisvalid, i.indisunique FROM pg_class AS c "
                "JOIN pg_index AS i ON i.indexrelid = c.oid WHERE c.relname = :name"
            ),
            {"name": name},
        ).one_or_none()
    return None if row is None else (bool(row[0]), bool(row[1]))


@pytest.mark.parametrize("leftover", ["built", "invalid"])
def test_postgresql_upgrade_resumes_after_an_interrupted_build(
    fresh_pg_url: Any, leftover: str
) -> None:
    """The concurrent build commits as it goes, so a rerun finds what an
    interrupted one left: a finished index it keeps, or an INVALID one (from a
    failed concurrent build) it drops and builds again."""
    url = fresh_pg_url.render_as_string(hide_password=False)
    cfg = _config(url)
    command.upgrade(cfg, "078")
    engine = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            if leftover == "built":
                conn.execute(
                    sa.text(
                        "CREATE INDEX idx_usage_events_ws_timestamp "
                        'ON usage_events (ws_id, "timestamp")'
                    )
                )
            else:
                for event_id in ("e1", "e2"):
                    conn.execute(
                        sa.text(
                            "INSERT INTO usage_events (event_id, timestamp, ws_id, created) "
                            "VALUES (:id, '2026-01-01T00:00:00', 'ws-1', '2026-01-01T00:00:00')"
                        ),
                        {"id": event_id},
                    )
                # Two rows share the key, so the unique build fails half done.
                with pytest.raises(sa.exc.IntegrityError):
                    conn.execute(
                        sa.text(
                            "CREATE UNIQUE INDEX CONCURRENTLY idx_usage_events_ws_timestamp "
                            'ON usage_events (ws_id, "timestamp")'
                        )
                    )
        assert _index_flags(engine, "idx_usage_events_ws_timestamp") == (
            leftover == "built",
            leftover == "invalid",
        )

        command.upgrade(cfg, "079")

        assert _index_flags(engine, "idx_usage_events_ws_timestamp") == (True, False)
        assert _usage_indexes(engine)["idx_usage_events_ws_timestamp"] == ["ws_id", "timestamp"]
        assert _index_flags(engine, "idx_usage_events_ws") is None
    finally:
        engine.dispose()
