"""Tests for structured memory storage backend operations."""


class TestCreateAndGet:
    def test_create_and_get_by_id(self, backend):
        backend.create_structured_memory("m1", "test_key", "desc", "project", "global", "", "data")
        mem = backend.get_structured_memory("m1")
        assert mem is not None
        assert mem["name"] == "test_key"
        assert mem["content"] == "data"
        assert mem["type"] == "project"

    def test_get_nonexistent(self, backend):
        assert backend.get_structured_memory("nope") is None

    def test_get_by_name(self, backend):
        backend.create_structured_memory("m1", "mykey", "d", "project", "global", "", "val")
        mem = backend.get_structured_memory_by_name("mykey", "global", "")
        assert mem is not None
        assert mem["memory_id"] == "m1"

    def test_get_by_name_scoped(self, backend):
        backend.create_structured_memory("m1", "key", "d", "project", "global", "", "g")
        backend.create_structured_memory("m2", "key", "d", "project", "workstream", "ws1", "w")
        g = backend.get_structured_memory_by_name("key", "global", "")
        w = backend.get_structured_memory_by_name("key", "workstream", "ws1")
        assert g["content"] == "g"
        assert w["content"] == "w"


class TestUpdate:
    def test_update_content(self, backend):
        backend.create_structured_memory("m1", "k", "d", "project", "global", "", "old")
        assert backend.update_structured_memory("m1", content="new")
        mem = backend.get_structured_memory("m1")
        assert mem["content"] == "new"

    def test_update_nonexistent(self, backend):
        assert not backend.update_structured_memory("nope", content="x")

    def test_update_no_fields(self, backend):
        backend.create_structured_memory("m1", "k", "d", "project", "global", "", "data")
        assert not backend.update_structured_memory("m1", bogus="val")

    def test_update_bumps_timestamp(self, backend):
        backend.create_structured_memory("m1", "k", "d", "project", "global", "", "data")
        old = backend.get_structured_memory("m1")["updated"]
        import time

        time.sleep(0.01)
        backend.update_structured_memory("m1", content="new")
        new = backend.get_structured_memory("m1")["updated"]
        assert new >= old


class TestDelete:
    def test_delete_existing(self, backend):
        backend.create_structured_memory("m1", "k", "d", "project", "global", "", "data")
        assert backend.delete_structured_memory("k", "global", "")
        assert backend.get_structured_memory("m1") is None

    def test_delete_nonexistent(self, backend):
        assert not backend.delete_structured_memory("nope", "global", "")

    def test_delete_scoped(self, backend):
        backend.create_structured_memory("m1", "k", "d", "project", "workstream", "ws1", "data")
        assert not backend.delete_structured_memory("k", "global", "")
        assert backend.delete_structured_memory("k", "workstream", "ws1")


class TestList:
    def test_list_all(self, backend):
        backend.create_structured_memory("m1", "a", "", "project", "global", "", "1")
        backend.create_structured_memory("m2", "b", "", "user", "global", "", "2")
        mems = backend.list_structured_memories()
        assert len(mems) == 2

    def test_list_by_type(self, backend):
        backend.create_structured_memory("m1", "a", "", "project", "global", "", "1")
        backend.create_structured_memory("m2", "b", "", "user", "global", "", "2")
        mems = backend.list_structured_memories(mem_type="user")
        assert len(mems) == 1
        assert mems[0]["name"] == "b"

    def test_list_by_scope(self, backend):
        backend.create_structured_memory("m1", "a", "", "project", "global", "", "1")
        backend.create_structured_memory("m2", "b", "", "project", "workstream", "ws1", "2")
        mems = backend.list_structured_memories(scope="workstream")
        assert len(mems) == 1

    def test_list_respects_limit(self, backend):
        for i in range(10):
            backend.create_structured_memory(f"m{i}", f"k{i}", "", "project", "global", "", f"{i}")
        mems = backend.list_structured_memories(limit=3)
        assert len(mems) == 3


class TestSearch:
    def test_search_by_name(self, backend):
        backend.create_structured_memory("m1", "database_config", "", "project", "global", "", "pg")
        backend.create_structured_memory("m2", "api_key", "", "project", "global", "", "secret")
        results = backend.search_structured_memories("database")
        assert len(results) == 1
        assert results[0]["name"] == "database_config"

    def test_search_by_content(self, backend):
        backend.create_structured_memory("m1", "a", "", "project", "global", "", "postgresql host")
        results = backend.search_structured_memories("postgresql")
        assert len(results) == 1

    def test_search_empty_lists_all(self, backend):
        backend.create_structured_memory("m1", "a", "", "project", "global", "", "1")
        backend.create_structured_memory("m2", "b", "", "project", "global", "", "2")
        results = backend.search_structured_memories("")
        assert len(results) == 2


class TestCount:
    def test_count_all(self, backend):
        backend.create_structured_memory("m1", "a", "", "project", "global", "", "1")
        backend.create_structured_memory("m2", "b", "", "project", "global", "", "2")
        assert backend.count_structured_memories() == 2

    def test_count_by_scope(self, backend):
        backend.create_structured_memory("m1", "a", "", "project", "global", "", "1")
        backend.create_structured_memory("m2", "b", "", "project", "workstream", "ws1", "2")
        assert backend.count_structured_memories(scope="global") == 1
        assert backend.count_structured_memories(scope="workstream") == 1
