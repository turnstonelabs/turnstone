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
        monkeypatch_not_needed = True  # noqa: F841
        init_storage("sqlite", path=str(tmp_path / "test2.db"), run_migrations=False)
        s2 = get_storage()
        assert s1 is not s2


class TestConnUnavailableLogging:
    """Test that _conn() deduplicates DB unavailable/restored logging."""

    def _make_backend(self, tmp_path):
        """Create a minimal SQLite backend for testing _conn()."""
        from turnstone.core.storage._sqlite import SQLiteBackend

        return SQLiteBackend(str(tmp_path / "test.db"), create_tables=True)

    def test_logs_unavailable_once(self, tmp_path, caplog: pytest.LogCaptureFixture) -> None:
        backend = self._make_backend(tmp_path)
        with patch.object(backend, "_engine") as mock_engine:
            mock_engine.connect.side_effect = sa.exc.OperationalError(
                "conn", {}, Exception("refused")
            )
            for _ in range(3):
                with pytest.raises(StorageUnavailableError), backend._conn():
                    pass  # pragma: no cover
        unavailable_msgs = [r for r in caplog.records if "database.unavailable" in r.message]
        assert len(unavailable_msgs) == 1

    def test_logs_restored_on_recovery(self, tmp_path, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        caplog.set_level(logging.INFO)
        backend = self._make_backend(tmp_path)
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
