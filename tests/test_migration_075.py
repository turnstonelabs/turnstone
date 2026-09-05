"""Channel ownership survives migration without claiming legacy conversations."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config


def test_channel_route_owner_upgrade_and_downgrade(tmp_path):
    path = tmp_path / "channels.db"
    cfg = Config()
    cfg.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[1] / "turnstone/core/storage/migrations"),
    )
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(cfg, "074")
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO channel_routes (channel_type, channel_id, ws_id, created) "
                    "VALUES ('discord', '123', 'ws-old', '2026-01-01')"
                )
            )
        command.upgrade(cfg, "075")
        with engine.begin() as conn:
            assert conn.execute(
                sa.text("SELECT ws_id, channel_user_id FROM channel_routes")
            ).one() == ("ws-old", "")
            conn.execute(sa.text("UPDATE channel_routes SET channel_user_id = '456'"))
        command.downgrade(cfg, "074")
        with engine.connect() as conn:
            assert (
                conn.execute(sa.text("SELECT ws_id FROM channel_routes")).scalar_one() == "ws-old"
            )
        command.upgrade(cfg, "075")
        with engine.connect() as conn:
            assert (
                conn.execute(sa.text("SELECT channel_user_id FROM channel_routes")).scalar_one()
                == ""
            )
    finally:
        engine.dispose()
