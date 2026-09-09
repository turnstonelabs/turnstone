"""Shared database fixtures retain isolation, seed policy, and full-text indexing."""

from __future__ import annotations

import contextlib
import sqlite3

import pytest

from turnstone.core.storage import get_storage
from turnstone.core.storage._sqlite import SQLiteBackend


@pytest.mark.parametrize("fixture_name", ["tmp_db", "storage_backend"])
@pytest.mark.parametrize("iteration", range(2))
def test_storage_fixtures_start_empty_and_index_new_rows(request, fixture_name, iteration):
    # Reusing the same keys in separate invocations catches a shared writable DB.
    request.getfixturevalue(fixture_name)
    storage = get_storage()
    assert storage.get_user("fixture-user") is None
    assert storage.get_role("builtin-admin") is None
    storage.create_user("fixture-user", "fixture-user", "Fixture User", "")
    assert storage.get_user("fixture-user") is not None
    storage.register_workstream("fixture-ws")
    storage.save_message("fixture-ws", "user", "fixturetoken")
    assert storage.search_history("fixturetoken")

    if isinstance(storage, SQLiteBackend):
        with contextlib.closing(sqlite3.connect(storage._path)) as connection:
            fts5 = connection.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()
            if fts5[0]:
                # A copied schema opened with create_tables=False would leave the
                # backend's FTS flag disabled and silently use fallback search.
                assert storage._fts5_available
                rows = connection.execute(
                    "SELECT rowid FROM conversations_fts WHERE conversations_fts MATCH ?",
                    ("fixturetoken",),
                ).fetchall()
                assert len(rows) == 1


@pytest.mark.parametrize(
    "first,second", [("tmp_db", "storage_backend"), ("storage_backend", "tmp_db")]
)
def test_sqlite_fixtures_preserve_rows_when_reopening_the_same_path(request, first, second):
    if request.config.getoption("--storage-backend") != "sqlite":
        pytest.skip("the two fixtures only share a path in the SQLite lane")
    request.getfixturevalue(first)
    get_storage().create_user("fixture-user", "fixture-user", "Fixture User", "")
    request.getfixturevalue(second)
    assert get_storage().get_user("fixture-user") is not None
