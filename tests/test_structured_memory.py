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


class TestScopeIsolation:
    """Verify that list/search without scope only returns visible memories.

    Reproduces the cross-workstream leak: unscoped list/search must not
    return workstream-scoped memories from other workstreams or
    user-scoped memories from other users.
    """

    def _seed(self):
        """Create memories across multiple scopes."""
        save_structured_memory("global_note", "visible to all", scope="global")
        save_structured_memory("ws1_note", "belongs to ws1", scope="workstream", scope_id="ws1")
        save_structured_memory("ws2_note", "belongs to ws2", scope="workstream", scope_id="ws2")
        save_structured_memory("u1_note", "belongs to user1", scope="user", scope_id="u1")
        save_structured_memory("u2_note", "belongs to user2", scope="user", scope_id="u2")

    @staticmethod
    def _list_visible(ws_id: str, user_id: str, mem_type: str = "", limit: int = 50):
        """Replicate the scope-filtered list logic from ChatSession."""
        global_mems = list_structured_memories(mem_type=mem_type, scope="global", limit=limit)
        ws_mems = list_structured_memories(
            mem_type=mem_type, scope="workstream", scope_id=ws_id, limit=limit
        )
        user_mems = (
            list_structured_memories(mem_type=mem_type, scope="user", scope_id=user_id, limit=limit)
            if user_id
            else []
        )
        combined = global_mems + ws_mems + user_mems
        combined.sort(key=lambda m: m.get("updated", ""), reverse=True)
        return combined[:limit]

    @staticmethod
    def _search_visible(query: str, ws_id: str, user_id: str, mem_type: str = "", limit: int = 20):
        """Replicate the scope-filtered search logic from ChatSession."""
        global_mems = search_structured_memories(
            query, mem_type=mem_type, scope="global", limit=limit
        )
        ws_mems = search_structured_memories(
            query, mem_type=mem_type, scope="workstream", scope_id=ws_id, limit=limit
        )
        user_mems = (
            search_structured_memories(
                query, mem_type=mem_type, scope="user", scope_id=user_id, limit=limit
            )
            if user_id
            else []
        )
        combined = global_mems + ws_mems + user_mems
        combined.sort(key=lambda m: m.get("updated", ""), reverse=True)
        return combined[:limit]

    def test_unscoped_list_returns_all_scopes(self, tmp_db):
        """Demonstrate the leak: unscoped list returns everything."""
        self._seed()
        all_mems = list_structured_memories()
        assert len(all_mems) == 5  # no scope filter → all memories

    def test_visible_list_excludes_other_workstreams(self, tmp_db):
        """Scope-filtered list for ws1/u1 excludes ws2 and u2 memories."""
        self._seed()
        visible = self._list_visible("ws1", "u1")
        names = {m["name"] for m in visible}
        assert "global_note" in names
        assert "ws1_note" in names
        assert "u1_note" in names
        assert "ws2_note" not in names
        assert "u2_note" not in names

    def test_visible_list_no_user(self, tmp_db):
        """Scope-filtered list with no user_id excludes all user memories."""
        self._seed()
        visible = self._list_visible("ws1", "")
        names = {m["name"] for m in visible}
        assert "global_note" in names
        assert "ws1_note" in names
        assert "u1_note" not in names
        assert "u2_note" not in names

    def test_visible_search_no_user(self, tmp_db):
        """Scope-filtered search with no user_id excludes all user memories."""
        self._seed()
        visible = self._search_visible("belongs", "ws1", "")
        names = {m["name"] for m in visible}
        assert "ws1_note" in names
        assert "u1_note" not in names
        assert "u2_note" not in names

    def test_visible_search_excludes_other_workstreams(self, tmp_db):
        """Scope-filtered search for ws1/u1 excludes ws2 and u2 memories."""
        self._seed()
        visible = self._search_visible("belongs", "ws1", "u1")
        names = {m["name"] for m in visible}
        assert "ws1_note" in names
        assert "u1_note" in names
        assert "ws2_note" not in names
        assert "u2_note" not in names

    def test_explicit_scope_still_works(self, tmp_db):
        """Explicit scope filter continues to work as before."""
        self._seed()
        ws2_only = list_structured_memories(scope="workstream", scope_id="ws2")
        assert len(ws2_only) == 1
        assert ws2_only[0]["name"] == "ws2_note"
