"""Owner-lease migration starts saved rows unleased at epoch 0 and round-trips."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

_LEASE_COLUMNS = "lease_holder, lease_node_id, lease_epoch, lease_expires_ms"


def _roundtrip(url: str) -> None:
    cfg = Config()
    cfg.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[1] / "turnstone/core/storage/migrations"),
    )
    cfg.set_main_option("sqlalchemy.url", str(url).replace("%", "%%"))
    command.upgrade(cfg, "077")
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO workstreams (ws_id, node_id, created, updated) "
                    "VALUES ('saved', 'old-origin', '2026-01-01', '2026-01-01')"
                )
            )
        command.upgrade(cfg, "078")
        with engine.begin() as conn:
            assert conn.execute(
                sa.text(f"SELECT {_LEASE_COLUMNS} FROM workstreams WHERE ws_id = 'saved'")
            ).one() == (None, None, 0, None)
            conn.execute(
                sa.text(
                    "UPDATE workstreams SET lease_holder = 'host-1/abc', "
                    "lease_node_id = 'host-1', lease_epoch = 7, "
                    "lease_expires_ms = 4102444800000 WHERE ws_id = 'saved'"
                )
            )
        command.downgrade(cfg, "077")
        with engine.connect() as conn:
            assert (
                conn.execute(
                    sa.text("SELECT node_id FROM workstreams WHERE ws_id = 'saved'")
                ).scalar_one()
                == "old-origin"
            )
        command.upgrade(cfg, "078")
        with engine.connect() as conn:
            assert conn.execute(
                sa.text(f"SELECT {_LEASE_COLUMNS} FROM workstreams WHERE ws_id = 'saved'")
            ).one() == (None, None, 0, None)
    finally:
        engine.dispose()


def test_owner_lease_migration_sqlite(tmp_path):
    _roundtrip(f"sqlite:///{tmp_path / 'lease.db'}")


def test_owner_lease_migration_postgresql(fresh_pg_url):
    _roundtrip(fresh_pg_url.render_as_string(hide_password=False))
