"""Tests for turnstone.core.memory — database operations."""

import turnstone.core.memory as memory
from turnstone.core.memory import (
    open_db,
    save_message,
    search_history,
    search_history_recent,
    normalize_key,
)


class TestOpenDb:
    def test_creates_tables(self, tmp_db):
        conn = open_db()
        try:
            # Check memories table exists
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
            ).fetchall()
            assert len(rows) == 1

            # Check conversations table exists
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='conversations'"
            ).fetchall()
            assert len(rows) == 1
        finally:
            conn.close()

    def test_idempotent_open(self, tmp_db):
        # Opening twice should not raise
        conn1 = open_db()
        conn1.close()
        conn2 = open_db()
        conn2.close()


class TestSaveAndSearchHistory:
    def test_save_and_search_roundtrip(self, tmp_db):
        save_message("sess1", "user", "hello world test message")
        results = search_history("hello")
        assert len(results) >= 1
        # Result tuple: (timestamp, session_id, role, content, tool_name)
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
