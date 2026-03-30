"""Tests for turnstone.core.memory — structured memory facade functions."""

from turnstone.core.memory import (
    count_structured_memories,
    delete_structured_memory,
    get_structured_memory_by_name,
    list_structured_memories,
    normalize_key,
    save_structured_memory,
    search_structured_memories,
)


class TestSaveStructuredMemory:
    def test_save_new(self, tmp_db):
        mid, old = save_structured_memory("test_key", "hello world")
        assert mid != ""
        assert old is None

    def test_save_upsert(self, tmp_db):
        save_structured_memory("test_key", "first")
        mid, old = save_structured_memory("test_key", "second")
        assert old == "first"
        assert mid != ""

    def test_save_normalizes_key(self, tmp_db):
        save_structured_memory("My-Key", "value")
        mems = list_structured_memories()
        assert any(m["name"] == "my_key" for m in mems)

    def test_save_with_type_and_scope(self, tmp_db):
        save_structured_memory("k", "v", mem_type="user", scope="workstream", scope_id="ws1")
        mems = list_structured_memories(scope="workstream", scope_id="ws1")
        assert len(mems) == 1
        assert mems[0]["type"] == "user"


class TestDeleteStructuredMemory:
    def test_delete_existing(self, tmp_db):
        save_structured_memory("mykey", "val")
        assert delete_structured_memory("mykey")

    def test_delete_nonexistent(self, tmp_db):
        assert not delete_structured_memory("nope")

    def test_delete_normalizes_key(self, tmp_db):
        save_structured_memory("my_key", "val")
        assert delete_structured_memory("My-Key")


class TestListStructuredMemories:
    def test_list_empty(self, tmp_db):
        assert list_structured_memories() == []

    def test_list_returns_saved(self, tmp_db):
        save_structured_memory("a", "alpha")
        save_structured_memory("b", "beta")
        mems = list_structured_memories()
        assert len(mems) == 2


class TestSearchStructuredMemories:
    def test_search_finds_match(self, tmp_db):
        save_structured_memory("db_host", "localhost", description="database hostname")
        save_structured_memory("api_url", "http://example.com")
        results = search_structured_memories("database")
        assert len(results) >= 1
        assert any(r["name"] == "db_host" for r in results)


class TestGetStructuredMemoryByName:
    def test_get_existing(self, tmp_db):
        save_structured_memory("my_mem", "full content here that is quite long")
        mem = get_structured_memory_by_name("my_mem", "global", "")
        assert mem is not None
        assert mem["content"] == "full content here that is quite long"
        assert mem["name"] == "my_mem"

    def test_get_nonexistent(self, tmp_db):
        assert get_structured_memory_by_name("nope", "global", "") is None

    def test_get_wrong_scope(self, tmp_db):
        save_structured_memory("ws_mem", "data", scope="workstream", scope_id="ws1")
        assert get_structured_memory_by_name("ws_mem", "global", "") is None
        assert get_structured_memory_by_name("ws_mem", "workstream", "ws1") is not None

    def test_get_normalizes_key(self, tmp_db):
        save_structured_memory("My-Key", "value")
        mem = get_structured_memory_by_name("My-Key", "global", "")
        assert mem is not None
        assert mem["name"] == "my_key"


class TestCountStructuredMemories:
    def test_count_zero(self, tmp_db):
        assert count_structured_memories() == 0

    def test_count_after_save(self, tmp_db):
        save_structured_memory("a", "1")
        save_structured_memory("b", "2")
        assert count_structured_memories() == 2


class TestNormalizeKey:
    def test_basic(self):
        assert normalize_key("My-Key Name") == "my_key_name"
