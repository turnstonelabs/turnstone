"""Affinity migration preserves saved rows and leaves legacy intent unspecified."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config


def _roundtrip(url):
    cfg = Config()
    cfg.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[1] / "turnstone/core/storage/migrations"),
    )
    cfg.set_main_option("sqlalchemy.url", str(url).replace("%", "%%"))
    command.upgrade(cfg, "075")
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO workstreams (ws_id, node_id, created, updated) VALUES ('saved', 'old-origin', '2026-01-01', '2026-01-01')"
                )
            )
        command.upgrade(cfg, "076")
        with engine.begin() as conn:
            assert conn.execute(
                sa.text("SELECT node_id, required_node_id FROM workstreams WHERE ws_id = 'saved'")
            ).one() == ("old-origin", None)
            conn.execute(
                sa.text("UPDATE workstreams SET required_node_id = 'host-1' WHERE ws_id = 'saved'")
            )
        command.downgrade(cfg, "075")
        with engine.connect() as conn:
            assert (
                conn.execute(
                    sa.text("SELECT node_id FROM workstreams WHERE ws_id = 'saved'")
                ).scalar_one()
                == "old-origin"
            )
        command.upgrade(cfg, "076")
        with engine.connect() as conn:
            assert (
                conn.execute(
                    sa.text("SELECT required_node_id FROM workstreams WHERE ws_id = 'saved'")
                ).scalar_one()
                is None
            )
    finally:
        engine.dispose()


def test_affinity_migration_sqlite(tmp_path):
    _roundtrip(f"sqlite:///{tmp_path / 'affinity.db'}")


def test_affinity_migration_postgresql(fresh_pg_url):
    _roundtrip(fresh_pg_url.render_as_string(hide_password=False))
