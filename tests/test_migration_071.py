"""Migration coverage for idempotent conversation commit keys."""

from __future__ import annotations

import contextlib
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

if TYPE_CHECKING:
    from collections.abc import Iterator

_MIGRATIONS_DIR = str(
    Path(__file__).resolve().parent.parent / "turnstone" / "core" / "storage" / "migrations"
)


def _alembic_cfg(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS_DIR)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


class TestMigration071:
    def test_upgrade_preserves_legacy_rows_and_enforces_scoped_key_uniqueness(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "071-up.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "070")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "INSERT INTO conversations (ws_id, timestamp, role, content) "
                        "VALUES ('legacy', '2026-01-01T00:00:00', 'assistant', 'same'), "
                        "('legacy', '2026-01-01T00:00:01', 'assistant', 'same')"
                    )
                )

            command.upgrade(cfg, "071")

            with engine.begin() as conn:
                assert (
                    conn.execute(
                        sa.text("SELECT COUNT(*) FROM conversations WHERE commit_key IS NULL")
                    ).scalar_one()
                    == 2
                )
                # NULL keeps append-only legacy semantics under the unique index.
                conn.execute(
                    sa.text(
                        "INSERT INTO conversations "
                        "(ws_id, timestamp, role, content, commit_key) VALUES "
                        "('legacy', '2026-01-01T00:00:02', 'assistant', 'same', NULL)"
                    )
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO conversations "
                        "(ws_id, timestamp, role, content, commit_key) VALUES "
                        "('keyed-a', '2026-01-01T00:00:03', 'assistant', 'a', 'key-1'), "
                        "('keyed-b', '2026-01-01T00:00:04', 'assistant', 'b', 'key-1')"
                    )
                )
                with pytest.raises(sa.exc.IntegrityError):
                    conn.execute(
                        sa.text(
                            "INSERT INTO conversations "
                            "(ws_id, timestamp, role, content, commit_key) VALUES "
                            "('keyed-a', '2026-01-01T00:00:05', 'assistant', 'dup', 'key-1')"
                        )
                    )
        finally:
            engine.dispose()

    @pytest.mark.parametrize("invalid_index", [False, True])
    def test_postgresql_upgrade_is_restart_safe_and_repairs_invalid_index(
        self,
        monkeypatch: pytest.MonkeyPatch,
        invalid_index: bool,
    ) -> None:
        migration = importlib.import_module(
            "turnstone.core.storage.migrations.versions.071_conversations_commit_key"
        )

        class _Result:
            def __init__(self, value: bool) -> None:
                self.value = value

            def scalar_one_or_none(self) -> bool:
                return self.value

        class _Bind:
            dialect = SimpleNamespace(name="postgresql")

            def __init__(self) -> None:
                self.queries: list[str] = []
                self.invalid_index = invalid_index

            def execute(self, statement: Any) -> _Result:
                self.queries.append(str(statement))
                return _Result(self.invalid_index)

        class _Context:
            @contextlib.contextmanager
            def autocommit_block(self) -> Iterator[None]:
                yield

        class _Op:
            def __init__(self, bind: _Bind) -> None:
                self.bind = bind
                self.ddl: list[str] = []

            def get_bind(self) -> _Bind:
                return self.bind

            def get_context(self) -> _Context:
                return _Context()

            def execute(self, statement: str) -> None:
                self.ddl.append(statement)
                if statement.startswith(("DROP INDEX", "CREATE UNIQUE INDEX")):
                    self.bind.invalid_index = False

        bind = _Bind()
        fake_op = _Op(bind)
        monkeypatch.setattr(migration, "op", fake_op)

        # Running twice models a revision left unstamped after either durable
        # DDL operation. Both statements remain safe on the second attempt.
        migration.upgrade()
        migration.upgrade()

        assert (
            fake_op.ddl.count("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS commit_key TEXT")
            == 2
        )
        creates = [statement for statement in fake_op.ddl if statement.startswith("CREATE UNIQUE")]
        assert len(creates) == 2
        assert all("CONCURRENTLY IF NOT EXISTS" in statement for statement in creates)
        drops = [statement for statement in fake_op.ddl if statement.startswith("DROP INDEX")]
        assert len(drops) == (1 if invalid_index else 0)
        assert bind.queries and all("NOT i.indisvalid" in query for query in bind.queries)

    def test_downgrade_then_upgrade_round_trip(self, tmp_path: Path) -> None:
        db_path = tmp_path / "071-roundtrip.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "071")
        command.downgrade(cfg, "070")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            columns = {c["name"] for c in sa.inspect(engine).get_columns("conversations")}
            indexes = {i["name"] for i in sa.inspect(engine).get_indexes("conversations")}
            assert "commit_key" not in columns
            assert "uq_conversations_ws_commit_key" not in indexes
        finally:
            engine.dispose()

        command.upgrade(cfg, "071")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            columns = {c["name"] for c in sa.inspect(engine).get_columns("conversations")}
            indexes = {i["name"]: i for i in sa.inspect(engine).get_indexes("conversations")}
            assert "commit_key" in columns
            assert bool(indexes["uq_conversations_ws_commit_key"]["unique"])
            assert "commit_key IS NOT NULL" in str(
                indexes["uq_conversations_ws_commit_key"]["dialect_options"]["sqlite_where"]
            )
        finally:
            engine.dispose()
