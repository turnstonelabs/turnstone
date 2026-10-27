"""Migration coverage for the per-server switch that turns off MCP certificate verification."""

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


def _columns(db_path: Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        return {c["name"] for c in sa.inspect(engine).get_columns("mcp_servers")}
    finally:
        engine.dispose()


class TestMigration084:
    def test_upgrade_keeps_verification_on_for_existing_servers(self, tmp_path: Path) -> None:
        db_path = tmp_path / "084-up.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "083")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "INSERT INTO mcp_servers "
                        "(server_id, name, transport, url, created, updated) "
                        "VALUES ('s1', 'remote', 'streamable-http', 'https://mcp.example.com/mcp', "
                        "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
                    )
                )
            command.upgrade(cfg, "084")
            with engine.connect() as conn:
                value = conn.execute(
                    sa.text("SELECT tls_skip_verify FROM mcp_servers WHERE server_id = 's1'")
                ).scalar_one()
        finally:
            engine.dispose()

        assert value == 0

    def test_downgrade_then_upgrade_round_trip(self, tmp_path: Path) -> None:
        db_path = tmp_path / "084-roundtrip.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "084")
        command.downgrade(cfg, "083")
        downgraded = _columns(db_path)
        command.upgrade(cfg, "084")
        upgraded = _columns(db_path)

        assert "tls_skip_verify" not in downgraded
        assert "tls_skip_verify" in upgraded
