"""Migration coverage for immutable memory-index snapshots."""

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


class TestMigration072:
    def test_upgrade_cleans_retired_settings_and_only_resets_dirty_access_rows(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "072-up.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "071")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "INSERT INTO system_settings "
                        "(key, value, node_id, is_secret, changed_by, created, updated) VALUES "
                        "(:key, :value, :node_id, 0, 'migration-test', "
                        "'2026-01-01', '2026-01-01')"
                    ),
                    [
                        {"key": "memory.fetch_limit", "value": "7", "node_id": ""},
                        {
                            "key": "memory.fetch_limit",
                            "value": "19",
                            "node_id": "node-a",
                        },
                        {
                            "key": "memory.index_budget_chars",
                            "value": "65536",
                            "node_id": "",
                        },
                        {
                            "key": "memory.index_budget_chars",
                            "value": "70000",
                            "node_id": "node-a",
                        },
                        {"key": "judge.enabled", "value": "true", "node_id": ""},
                    ],
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO structured_memories "
                        "(memory_id, name, description, type, scope, scope_id, content, "
                        "created, updated, last_accessed, access_count) VALUES "
                        "(:memory_id, :name, 'hook', 'general', 'global', '', 'body', "
                        "'2026-01-01', '2026-01-01', :last_accessed, :access_count)"
                    ),
                    [
                        {
                            "memory_id": "dirty-both",
                            "name": "dirty_both",
                            "last_accessed": "2026-01-02",
                            "access_count": 7,
                        },
                        {
                            "memory_id": "dirty-time",
                            "name": "dirty_time",
                            "last_accessed": "2026-01-03",
                            "access_count": 0,
                        },
                        {
                            "memory_id": "dirty-count",
                            "name": "dirty_count",
                            "last_accessed": "",
                            "access_count": 5,
                        },
                        {
                            "memory_id": "already-zero",
                            "name": "already_zero",
                            "last_accessed": "",
                            "access_count": 0,
                        },
                    ],
                )
                conn.execute(
                    sa.text("CREATE TABLE structured_memory_update_log (memory_id TEXT NOT NULL)")
                )
                conn.execute(
                    sa.text(
                        "CREATE TRIGGER count_structured_memory_updates "
                        "AFTER UPDATE ON structured_memories BEGIN "
                        "INSERT INTO structured_memory_update_log(memory_id) "
                        "VALUES (NEW.memory_id); END"
                    )
                )
            command.upgrade(cfg, "072")
            with engine.connect() as conn:
                rows = conn.execute(
                    sa.text(
                        "SELECT memory_id, last_accessed, access_count "
                        "FROM structured_memories ORDER BY memory_id"
                    )
                ).all()
                assert [tuple(row) for row in rows] == [
                    ("already-zero", "", 0),
                    ("dirty-both", "", 0),
                    ("dirty-count", "", 0),
                    ("dirty-time", "", 0),
                ]
                updated_ids = conn.execute(
                    sa.text("SELECT memory_id FROM structured_memory_update_log ORDER BY memory_id")
                ).scalars()
                assert list(updated_ids) == ["dirty-both", "dirty-count", "dirty-time"]
                settings = conn.execute(
                    sa.text("SELECT key, value, node_id FROM system_settings ORDER BY key, node_id")
                ).all()
                assert [tuple(row) for row in settings] == [
                    ("judge.enabled", "true", ""),
                    ("memory.index_budget_chars", "65536", ""),
                    ("memory.index_budget_chars", "70000", "node-a"),
                ]
            assert sa.inspect(engine).has_table("memory_index_snapshots")
            verdict_columns = {
                column["name"] for column in sa.inspect(engine).get_columns("intent_verdicts")
            }
            assert {"resolver_principal_id", "execution_principal_id"} <= verdict_columns
            columns = {
                column["name"]
                for column in sa.inspect(engine).get_columns("memory_index_snapshots")
            }
            assert {
                "ws_id",
                "principal_id",
                "project_id",
                "project_name",
                "visibility_key",
                "content",
                "entry_count",
                "char_count",
            } <= columns

            command.downgrade(cfg, "071")
            with engine.connect() as conn:
                assert (
                    conn.execute(
                        sa.text(
                            "SELECT COUNT(*) FROM system_settings WHERE key = 'memory.fetch_limit'"
                        )
                    ).scalar_one()
                    == 0
                )
                assert conn.execute(
                    sa.text(
                        "SELECT last_accessed, access_count FROM structured_memories "
                        "WHERE memory_id = 'dirty-both'"
                    )
                ).one() == ("", 0)
        finally:
            engine.dispose()

    def test_downgrade_drops_072_snapshot_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "072-down.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "072")
        command.downgrade(cfg, "071")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            inspector = sa.inspect(engine)
            assert not inspector.has_table("memory_index_snapshots")
            assert inspector.has_table("structured_memories")
            verdict_columns = {
                column["name"] for column in inspector.get_columns("intent_verdicts")
            }
            assert "resolver_principal_id" not in verdict_columns
            assert "execution_principal_id" not in verdict_columns
        finally:
            engine.dispose()
