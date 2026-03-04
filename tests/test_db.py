"""Tests for turnstone.core.memory — database operations."""

import sqlalchemy as sa

from turnstone.core.memory import (
    normalize_key,
    save_message,
    search_history,
    search_history_recent,
)
from turnstone.core.storage import get_storage


class TestSchemaCreation:
    def test_creates_tables(self, tmp_db):
        engine = get_storage()._engine  # noqa: SLF001
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name='memories'")
            ).fetchall()
            assert len(rows) == 1
            rows = conn.execute(
                sa.text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='conversations'"
                )
            ).fetchall()
            assert len(rows) == 1


class TestSaveAndSearchHistory:
    def test_save_and_search_roundtrip(self, tmp_db):
        save_message("sess1", "user", "hello world test message")
        results = search_history("hello")
        assert len(results) >= 1
        found = any(r[3] == "hello world test message" for r in results)
        assert found

    def test_search_empty_query_returns_empty(self, tmp_db):
        save_message("sess1", "user", "something")
        assert search_history("") == []
        assert search_history("   ") == []

    def test_search_no_match(self, tmp_db):
        save_message("sess1", "user", "hello world")
        results = search_history("zzzznotfound")
        assert results == []


class TestSearchHistoryRecent:
    def test_returns_recent_messages(self, tmp_db):
        save_message("sess1", "user", "first message")
        save_message("sess1", "assistant", "second message")
        results = search_history_recent(limit=10)
        assert len(results) == 2

    def test_respects_limit(self, tmp_db):
        for i in range(5):
            save_message("sess1", "user", f"message {i}")
        results = search_history_recent(limit=3)
        assert len(results) == 3


class TestNormalizeKey:
    def test_lowercase(self):
        assert normalize_key("Hello") == "hello"

    def test_hyphens_to_underscores(self):
        assert normalize_key("my-key") == "my_key"

    def test_spaces_to_underscores(self):
        assert normalize_key("my key") == "my_key"

    def test_combined(self):
        assert normalize_key("My-Key Name") == "my_key_name"

    def test_already_normalized(self):
        assert normalize_key("my_key") == "my_key"
