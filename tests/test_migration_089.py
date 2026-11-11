"""Migration 089 indexes audit events by (resource_id, action, timestamp)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

_INDEX = "idx_audit_resource_action_ts"
_COLUMNS = ["resource_id", "action", "timestamp"]


def _audit_indexes(engine: sa.Engine) -> dict[str, list[str]]:
    return {
        index["name"]: [column for column in index["column_names"] if column]
        for index in sa.inspect(engine).get_indexes("audit_events")
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
    command.upgrade(cfg, "088")
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO audit_events (event_id, timestamp, action, resource_id, created) "
                    "VALUES ('e1', '2026-01-01T00:00:00', 'tool.auto_approved', 'ws-1', "
                    "'2026-01-01T00:00:00')"
                )
            )
        assert _INDEX not in _audit_indexes(engine)

        command.upgrade(cfg, "089")
        indexes = _audit_indexes(engine)
        assert indexes[_INDEX] == _COLUMNS
        assert indexes["idx_audit_action"] == ["action"]
        with engine.connect() as conn:
            assert (
                conn.execute(
                    sa.text(
                        "SELECT event_id FROM audit_events "
                        "WHERE action = 'tool.auto_approved' AND resource_id = 'ws-1'"
                    )
                ).scalar_one()
                == "e1"
            )

        command.downgrade(cfg, "088")
        assert _INDEX not in _audit_indexes(engine)

        command.upgrade(cfg, "089")
        assert _audit_indexes(engine)[_INDEX] == _COLUMNS
    finally:
        engine.dispose()


def test_audit_index_migration_sqlite(tmp_path: Path) -> None:
    _roundtrip(f"sqlite:///{tmp_path / 'audit-index.db'}")


def test_audit_index_migration_postgresql(fresh_pg_url: Any) -> None:
    _roundtrip(fresh_pg_url.render_as_string(hide_password=False))


def test_the_session_open_audit_lookup_searches_the_index_on_sqlite(tmp_path: Path) -> None:
    """The lookup every session open makes (``list_audit_events`` by action and resource,
    newest first) searches the new index instead of scanning every auto-approved row."""
    from turnstone.core.storage._schema import audit_events, metadata

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'plan.db'}")
    try:
        metadata.create_all(engine)
        query = (
            sa.select(audit_events.c.event_id)
            .where(audit_events.c.action == "tool.auto_approved")
            .where(audit_events.c.resource_id == "ws-1")
            .order_by(audit_events.c.timestamp.desc(), audit_events.c.event_id.desc())
            .limit(20)
        )
        sql = str(query.compile(engine, compile_kwargs={"literal_binds": True}))
        with engine.connect() as conn:
            plan = [row[3] for row in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + sql)]
    finally:
        engine.dispose()
    assert any(f"USING INDEX {_INDEX} (resource_id=? AND action=?)" in step for step in plan), plan


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
    command.upgrade(cfg, "088")
    engine = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            if leftover == "built":
                conn.execute(
                    sa.text(
                        f'CREATE INDEX {_INDEX} ON audit_events (resource_id, action, "timestamp")'
                    )
                )
            else:
                for event_id in ("e1", "e2"):
                    conn.execute(
                        sa.text(
                            "INSERT INTO audit_events "
                            "(event_id, timestamp, action, resource_id, created) "
                            "VALUES (:id, '2026-01-01T00:00:00', 'tool.auto_approved', 'ws-1', "
                            "'2026-01-01T00:00:00')"
                        ),
                        {"id": event_id},
                    )
                # Two rows share the key, so the unique build fails half done.
                with pytest.raises(sa.exc.IntegrityError):
                    conn.execute(
                        sa.text(
                            f"CREATE UNIQUE INDEX CONCURRENTLY {_INDEX} "
                            'ON audit_events (resource_id, action, "timestamp")'
                        )
                    )
        assert _index_flags(engine, _INDEX) == (leftover == "built", leftover == "invalid")

        command.upgrade(cfg, "089")

        assert _index_flags(engine, _INDEX) == (True, False)
        assert _audit_indexes(engine)[_INDEX] == _COLUMNS
    finally:
        engine.dispose()
