"""Tests for the storage backend registry."""

import pytest

from turnstone.core.storage import get_storage, init_storage, reset_storage
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
