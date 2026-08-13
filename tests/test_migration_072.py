"""Migration coverage for immutable memory-index snapshots."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from turnstone.core.storage._sqlite import SQLiteBackend

_MIGRATIONS_DIR = str(
    Path(__file__).resolve().parent.parent / "turnstone" / "core" / "storage" / "migrations"
)


def _alembic_cfg(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS_DIR)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


class TestMigration072:
    def test_upgrade_creates_snapshot_and_id_registry_and_resets_access_semantics(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "072-up.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "071")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "INSERT INTO workstreams "
                        "(ws_id, name, state, kind, created, updated) VALUES "
                        "('ws-legacy', 'legacy', 'idle', 'interactive', "
                        "'2026-01-01', '2026-01-01')"
                    )
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO structured_memories "
                        "(memory_id, name, description, type, scope, scope_id, content, "
                        "created, updated, last_accessed, access_count) VALUES "
                        "('m1', 'legacy', 'hook', 'general', 'workstream', 'ws-legacy', 'body', "
                        "'2026-01-01', '2026-01-01', '2026-01-02', 7)"
                    )
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO conversations (ws_id, timestamp, role, content) VALUES "
                        "('ws-legacy', '2025-01-01', 'user', 'older transcript'), "
                        "('ws-conversation-only', '2025-02-01', 'user', 'orphan transcript')"
                    )
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO structured_memories "
                        "(memory_id, name, description, type, scope, scope_id, content, "
                        "created, updated) VALUES "
                        "('m-orphan', 'orphan', 'hook', 'general', 'workstream', "
                        "'ws-memory-only', 'body', '2025-03-01', '2025-03-01')"
                    )
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO workstream_config (ws_id, key, value) VALUES "
                        "('ws-config-only', 'model', 'test')"
                    )
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO channel_routes "
                        "(channel_type, channel_id, ws_id, created) VALUES "
                        "('test', 'route', 'ws-route-only', '2025-04-01')"
                    )
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO scheduled_task_runs (run_id, task_id, ws_id, started) "
                        "VALUES ('run-1', 'task-1', 'ws-run-only', '2025-05-01')"
                    )
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO watches "
                        "(watch_id, ws_id, name, command, interval_secs, created, updated) VALUES "
                        "('watch-1', 'ws-watch-only', 'watch', 'true', 1, "
                        "'2025-06-01', '2025-06-01')"
                    )
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO workstream_overrides "
                        "(ws_id, node_id, created, updated) VALUES "
                        "('ws-override-only', 'node-1', '2025-07-01', '2025-07-01')"
                    )
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO usage_events (event_id, timestamp, ws_id, created) VALUES "
                        "('usage-1', '2025-08-01', 'ws-usage-only', '2025-08-01')"
                    )
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO intent_verdicts "
                        "(verdict_id, ws_id, call_id, func_name, intent_summary, risk_level, "
                        "confidence, recommendation, reasoning, tier, created) VALUES "
                        "('verdict-1', 'ws-verdict-only', 'call-1', 'tool', 'summary', 'low', "
                        "1.0, 'allow', 'reason', 'heuristic', '2025-09-01')"
                    )
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO output_assessments "
                        "(assessment_id, ws_id, call_id, func_name, created) VALUES "
                        "('assessment-1', 'ws-assessment-only', 'call-2', 'tool', '2025-10-01')"
                    )
                )
            command.upgrade(cfg, "072")
            with engine.connect() as conn:
                row = conn.execute(
                    sa.text(
                        "SELECT last_accessed, access_count FROM structured_memories "
                        "WHERE memory_id = 'm1'"
                    )
                ).one()
                assert tuple(row) == ("", 0)
                registry = dict(
                    conn.execute(
                        sa.text("SELECT ws_id, created FROM workstream_id_registry")
                    ).fetchall()
                )
                assert {
                    "ws-legacy",
                    "ws-conversation-only",
                    "ws-memory-only",
                    "ws-config-only",
                    "ws-route-only",
                    "ws-run-only",
                    "ws-watch-only",
                    "ws-override-only",
                    "ws-usage-only",
                    "ws-verdict-only",
                    "ws-assessment-only",
                } <= registry.keys()
                assert registry["ws-legacy"] == "2025-01-01"
                assert registry["ws-config-only"]
            assert sa.inspect(engine).has_table("memory_index_snapshots")
            assert sa.inspect(engine).has_table("workstream_id_registry")
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
            backend = SQLiteBackend(str(db_path))
            try:
                assert not backend.register_workstream(
                    "ws-conversation-only", user_id="later-owner"
                )
            finally:
                backend.close()
            with engine.connect() as conn:
                assert (
                    conn.execute(
                        sa.text(
                            "SELECT COUNT(*) FROM workstreams WHERE ws_id = 'ws-conversation-only'"
                        )
                    ).scalar_one()
                    == 0
                )
                assert (
                    conn.execute(
                        sa.text(
                            "SELECT content FROM conversations WHERE ws_id = 'ws-conversation-only'"
                        )
                    ).scalar_one()
                    == "orphan transcript"
                )
        finally:
            engine.dispose()

    def test_downgrade_drops_072_tables(self, tmp_path: Path) -> None:
        db_path = tmp_path / "072-down.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "072")
        command.downgrade(cfg, "071")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            inspector = sa.inspect(engine)
            assert not inspector.has_table("memory_index_snapshots")
            assert not inspector.has_table("workstream_id_registry")
            assert inspector.has_table("structured_memories")
            verdict_columns = {
                column["name"] for column in inspector.get_columns("intent_verdicts")
            }
            assert "resolver_principal_id" not in verdict_columns
            assert "execution_principal_id" not in verdict_columns
        finally:
            engine.dispose()
