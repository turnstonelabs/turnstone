"""Tests for the storage backend registry."""

from unittest.mock import patch

import pytest
import sqlalchemy as sa

from turnstone.core.storage import (
    StorageUnavailableError,
    get_storage,
    init_storage,
    reset_storage,
)
from turnstone.core.storage._postgresql import PostgreSQLBackend
from turnstone.core.storage._sqlite import SQLiteBackend


@pytest.fixture(autouse=True)
def _clean_registry():
    """Reset the storage registry before and after each test."""
    reset_storage()
    yield
    reset_storage()


class TestInitStorage:
    def test_sqlite_default(self, tmp_path):
        backend = init_storage("sqlite", path=str(tmp_path / "test.db"), run_migrations=False)
        assert isinstance(backend, SQLiteBackend)

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown storage backend"):
            init_storage("mongodb")

    def test_postgresql_requires_url(self):
        with pytest.raises(ValueError, match="requires a connection URL"):
            init_storage("postgresql")

    def test_migrations_take_a_postgresql_url_that_holds_percent_escapes(self, monkeypatch):
        # A certificate path is encoded into the URL (/ as %2F), and so is punctuation in a
        # password; Alembic's config reads a bare % as interpolation.
        from turnstone.core.storage import _migrate

        handed: list[str] = []
        monkeypatch.setattr(
            _migrate,
            "_run_with_pg_lock",
            lambda engine, cfg: handed.append(
                cfg.get_section(cfg.config_ini_section, {})["sqlalchemy.url"]
            ),
        )
        storage = init_storage(
            "postgresql",
            url="postgresql+psycopg://user:p%40ss@db.example.com/db",
            sslmode="verify-full",
            sslrootcert="/etc/turnstone/db-ca.pem",
            run_migrations=True,
        )

        assert [sa.engine.make_url(url) for url in handed] == [storage._engine.url]

    @pytest.mark.parametrize("directory", ["50%", "a%20b"])
    def test_sqlite_migrations_run_under_a_directory_that_holds_a_percent_sign(
        self, tmp_path, directory
    ):
        path = tmp_path / directory / "test.db"
        path.parent.mkdir()
        storage = init_storage("sqlite", path=str(path), run_migrations=True)

        assert path.exists()
        with storage._engine.connect() as conn:
            assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()


class TestMigrateEntrypoint:
    def test_it_applies_the_database_tls_settings(self, monkeypatch):
        # The Docker entrypoint runs it before the services start, from the same environment.
        import types

        import turnstone.core.log
        import turnstone.core.storage
        from turnstone.core.storage import _migrate

        handed: dict[str, object] = {}
        monkeypatch.setattr(turnstone.core.log, "configure_logging", lambda **kwargs: None)
        monkeypatch.setattr(
            turnstone.core.storage,
            "init_storage",
            lambda backend, **kwargs: handed.update(kwargs, backend=backend),
        )
        monkeypatch.setattr(
            turnstone.core.storage, "get_storage", lambda: types.SimpleNamespace(close=lambda: None)
        )
        for name, value in {
            "TURNSTONE_DB_BACKEND": "postgresql",
            "TURNSTONE_DB_URL": "postgresql+psycopg://user@db.example.com/db",
            "TURNSTONE_DB_SSLMODE": "verify-full",
            "TURNSTONE_DB_SSLROOTCERT": "/etc/turnstone/db-ca.pem",
            "TURNSTONE_DB_SSLCERT": "/etc/turnstone/db.crt",
            "TURNSTONE_DB_SSLKEY": "/etc/turnstone/db.key",
        }.items():
            monkeypatch.setenv(name, value)

        _migrate._main()

        assert handed["backend"] == "postgresql"
        assert handed["sslmode"] == "verify-full"
        assert handed["sslrootcert"] == "/etc/turnstone/db-ca.pem"
        assert handed["sslcert"] == "/etc/turnstone/db.crt"
        assert handed["sslkey"] == "/etc/turnstone/db.key"
        assert handed["run_migrations"] is True


class TestGetStorage:
    def test_auto_init(self, tmp_path, monkeypatch):
        """get_storage() auto-initializes with SQLite if not yet initialized."""
        monkeypatch.chdir(tmp_path)
        storage = get_storage()
        assert isinstance(storage, SQLiteBackend)

    def test_returns_same_instance(self, tmp_path):
        init_storage("sqlite", path=str(tmp_path / "test.db"), run_migrations=False)
        s1 = get_storage()
        s2 = get_storage()
        assert s1 is s2


class TestResetStorage:
    def test_reset_clears_singleton(self, tmp_path):
        init_storage("sqlite", path=str(tmp_path / "test.db"), run_migrations=False)
        s1 = get_storage()
        reset_storage()
        # After reset, get_storage() auto-inits a new instance
        init_storage("sqlite", path=str(tmp_path / "test2.db"), run_migrations=False)
        s2 = get_storage()
        assert s1 is not s2


class TestConnUnavailableLogging:
    """Test that _conn() deduplicates DB unavailable/restored logging."""

    def _make_backend(self, tmp_path, sqlite_backend_factory):
        """Create a minimal SQLite backend for testing _conn()."""

        return sqlite_backend_factory(str(tmp_path / "test.db"), create_tables=True)

    def test_logs_unavailable_once(
        self, tmp_path, caplog: pytest.LogCaptureFixture, sqlite_backend_factory
    ) -> None:
        backend = self._make_backend(tmp_path, sqlite_backend_factory=sqlite_backend_factory)
        with patch.object(backend, "_engine") as mock_engine:
            mock_engine.connect.side_effect = sa.exc.OperationalError(
                "conn", {}, Exception("refused")
            )
            for _ in range(3):
                with pytest.raises(StorageUnavailableError), backend._conn():
                    pass  # pragma: no cover
        unavailable_msgs = [r for r in caplog.records if "database.unavailable" in r.message]
        assert len(unavailable_msgs) == 1

    def test_logs_restored_on_recovery(
        self, tmp_path, caplog: pytest.LogCaptureFixture, sqlite_backend_factory
    ) -> None:
        import logging

        caplog.set_level(logging.INFO)
        backend = self._make_backend(tmp_path, sqlite_backend_factory=sqlite_backend_factory)
        # Simulate outage
        with patch.object(backend, "_engine") as mock_engine:
            mock_engine.connect.side_effect = sa.exc.OperationalError(
                "conn", {}, Exception("refused")
            )
            with pytest.raises(StorageUnavailableError), backend._conn():
                pass  # pragma: no cover
        assert backend._db_unavailable is True
        # Real connection — should log restored
        caplog.clear()
        with backend._conn():
            pass
        restored_msgs = [r for r in caplog.records if "database.connection_restored" in r.message]
        assert len(restored_msgs) == 1
        assert backend._db_unavailable is False

    def test_postgresql_conn_raises_storage_unavailable(self) -> None:
        import threading

        backend = PostgreSQLBackend.__new__(PostgreSQLBackend)
        backend._db_unavailable = False
        backend._db_unavailable_lock = threading.Lock()

        def _raise_op_error():
            raise sa.exc.OperationalError("conn", {}, Exception("refused"))

        mock_engine = type(
            "E",
            (),
            {
                "connect": staticmethod(_raise_op_error),
                "url": sa.engine.make_url("postgresql://user:pass@localhost/db"),
            },
        )()
        backend._engine = mock_engine
        with pytest.raises(StorageUnavailableError), backend._conn():
            pass  # pragma: no cover
        assert backend._db_unavailable is True


class TestStatementErrors:
    """A failed statement's error text, which callers log, leaves out the values it bound."""

    def test_a_failed_statement_names_no_bound_values(self, tmp_path, sqlite_backend_factory):
        backend = sqlite_backend_factory(str(tmp_path / "test.db"))
        with backend._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "CREATE TRIGGER refuse_settings BEFORE INSERT ON system_settings "
                    "BEGIN SELECT RAISE(ABORT, 'refused'); END"
                )
            )

        with pytest.raises(sa.exc.DBAPIError) as exc_info:
            backend.upsert_system_setting(key="tools.searxng_url", value="bound-value-sentinel")

        assert "refused" in str(exc_info.value)
        assert "bound-value-sentinel" not in str(exc_info.value)
        assert "bound-value-sentinel" in repr(exc_info.value.params)

    def test_a_failed_migration_statement_names_no_bound_values(self, tmp_path):
        # Migration 060 rewrites stored tool output; the engine Alembic builds hides it too.
        from alembic import command
        from alembic.config import Config

        from turnstone.core.storage._migrate import _MIGRATIONS_DIR

        db_path = tmp_path / "migrate.db"
        cfg = Config()
        cfg.set_main_option("script_location", _MIGRATIONS_DIR)
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
        command.upgrade(cfg, "059")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "INSERT INTO conversations (ws_id, timestamp, role, content, tool_call_id) "
                        "VALUES ('ws1', '2026-06-01T00:00:00', 'tool', :content, 'call_a')"
                    ),
                    {
                        "content": (
                            "<tool_output>\nbound-value-sentinel\n</tool_output>\n\n"
                            "<system-reminder>\nadvisory\n</system-reminder>"
                        )
                    },
                )
                conn.execute(
                    sa.text(
                        "CREATE TRIGGER refuse_rewrite BEFORE UPDATE ON conversations "
                        "BEGIN SELECT RAISE(ABORT, 'refused'); END"
                    )
                )
        finally:
            engine.dispose()

        with pytest.raises(sa.exc.DBAPIError) as exc_info:
            command.upgrade(cfg, "060")

        assert "refused" in str(exc_info.value)
        assert "bound-value-sentinel" not in str(exc_info.value)
        assert "bound-value-sentinel" in repr(exc_info.value.params)

    def test_the_postgresql_engine_hides_bound_values_too(self) -> None:
        # Building the backend doesn't connect while create_tables is off.
        backend = PostgreSQLBackend(
            "postgresql+psycopg://user:pass@db.example.com/db", create_tables=False
        )
        try:
            assert backend._engine.hide_parameters is True
        finally:
            backend._engine.dispose()
