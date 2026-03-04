"""Tests for the SQLite storage backend."""

import pytest

from turnstone.core.storage import init_storage, reset_storage


@pytest.fixture
def backend(tmp_path):
    """Create a fresh SQLiteBackend for each test."""
    reset_storage()
    b = init_storage("sqlite", path=str(tmp_path / "test.db"), run_migrations=False)
    yield b
    reset_storage()


# -- Session operations --------------------------------------------------------


class TestRegisterSession:
    def test_register_creates_session(self, backend):
        backend.register_session("s1", title="Test")
        name = backend.get_session_name("s1")
        assert name == "Test"

    def test_register_idempotent(self, backend):
        backend.register_session("s1", title="First")
        backend.register_session("s1", title="Second")
        name = backend.get_session_name("s1")
        assert name == "First"  # INSERT OR IGNORE preserves first


class TestSaveAndLoadMessages:
    def test_roundtrip(self, backend):
        backend.register_session("s1")
        backend.save_message("s1", "user", "hello")
        backend.save_message("s1", "assistant", "world")
        msgs = backend.load_session_messages("s1")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "hello"
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["content"] == "world"

    def test_tool_call_grouping(self, backend):
        backend.register_session("s1")
        backend.save_message("s1", "user", "do something")
        backend.save_message("s1", "tool_call", None, "bash", '{"cmd":"ls"}', tool_call_id="c1")
        backend.save_message("s1", "tool_result", "file.txt", tool_call_id="c1")
        backend.save_message("s1", "assistant", "done")
        msgs = backend.load_session_messages("s1")
        assert len(msgs) == 4
        assert msgs[1]["role"] == "assistant"
        assert len(msgs[1]["tool_calls"]) == 1
        assert msgs[1]["tool_calls"][0]["id"] == "c1"
        assert msgs[2]["role"] == "tool"
        assert msgs[2]["content"] == "file.txt"

    def test_incomplete_turn_repair(self, backend):
        backend.register_session("s1")
        backend.save_message("s1", "user", "do something")
        backend.save_message("s1", "tool_call", None, "bash", '{"cmd":"ls"}', tool_call_id="c1")
        backend.save_message("s1", "tool_call", None, "read", '{"path":"a"}', tool_call_id="c2")
        # Only 1 result for 2 calls — incomplete turn
        backend.save_message("s1", "tool_result", "ok", tool_call_id="c1")
        msgs = backend.load_session_messages("s1")
        # Incomplete turn should be stripped
        assert len(msgs) == 1  # only the user message remains

    def test_provider_data_preserved(self, backend):
        import json

        backend.register_session("s1")
        pd = json.dumps({"encrypted": True})
        backend.save_message("s1", "assistant", "hi", provider_data=pd)
        msgs = backend.load_session_messages("s1")
        assert msgs[0].get("_provider_content") == {"encrypted": True}

    def test_empty_session_returns_empty(self, backend):
        assert backend.load_session_messages("nonexistent") == []


class TestListSessions:
    def test_lists_sessions_with_messages(self, backend):
        backend.register_session("s1")
        backend.save_message("s1", "user", "hi")
        backend.register_session("s2")  # no messages
        rows = backend.list_sessions()
        assert len(rows) == 1
        assert rows[0][0] == "s1"

    def test_respects_limit(self, backend):
        for i in range(5):
            sid = f"s{i}"
            backend.register_session(sid)
            backend.save_message(sid, "user", f"msg {i}")
        rows = backend.list_sessions(limit=3)
        assert len(rows) == 3


class TestDeleteSession:
    def test_deletes_all_data(self, backend):
        backend.register_session("s1")
        backend.save_message("s1", "user", "hi")
        backend.save_session_config("s1", {"temp": "0.5"})
        assert backend.delete_session("s1")
        assert backend.load_session_messages("s1") == []
        assert backend.load_session_config("s1") == {}
        assert backend.get_session_name("s1") is None


class TestPruneSessions:
    def test_orphan_removed(self, backend):
        backend.register_session("orphan")
        orphans, stale = backend.prune_sessions()
        assert orphans == 1

    def test_stale_removed(self, backend):
        import sqlalchemy as sa

        backend.register_session("old")
        backend.save_message("old", "user", "hi")
        # Force old timestamp
        with backend._engine.connect() as conn:
            conn.execute(
                sa.text("UPDATE sessions SET updated = '2020-01-01' WHERE session_id = 'old'")
            )
            conn.commit()
        _, stale = backend.prune_sessions(retention_days=30)
        assert stale == 1


class TestResolveSession:
    def test_exact_alias(self, backend):
        backend.register_session("s1")
        backend.set_session_alias("s1", "myalias")
        assert backend.resolve_session("myalias") == "s1"

    def test_exact_id(self, backend):
        backend.register_session("abc-123-def")
        assert backend.resolve_session("abc-123-def") == "abc-123-def"

    def test_prefix_match(self, backend):
        backend.register_session("abc-123-def")
        assert backend.resolve_session("abc") == "abc-123-def"

    def test_not_found(self, backend):
        assert backend.resolve_session("nonexistent") is None


# -- Session config ------------------------------------------------------------


class TestSessionConfig:
    def test_roundtrip(self, backend):
        backend.register_session("s1")
        backend.save_session_config("s1", {"temperature": "0.7", "effort": "high"})
        cfg = backend.load_session_config("s1")
        assert cfg == {"temperature": "0.7", "effort": "high"}

    def test_empty_config(self, backend):
        assert backend.load_session_config("nonexistent") == {}


# -- Session metadata ----------------------------------------------------------


class TestSessionMetadata:
    def test_alias(self, backend):
        backend.register_session("s1")
        assert backend.set_session_alias("s1", "my-session")
        assert backend.get_session_name("s1") == "my-session"

    def test_alias_conflict(self, backend):
        backend.register_session("s1")
        backend.register_session("s2")
        backend.set_session_alias("s1", "taken")
        assert not backend.set_session_alias("s2", "taken")

    def test_title(self, backend):
        backend.register_session("s1")
        backend.update_session_title("s1", "My Title")
        assert backend.get_session_name("s1") == "My Title"

    def test_alias_preferred_over_title(self, backend):
        backend.register_session("s1")
        backend.update_session_title("s1", "Title")
        backend.set_session_alias("s1", "Alias")
        assert backend.get_session_name("s1") == "Alias"


# -- Key-value store -----------------------------------------------------------


class TestKVStore:
    def test_set_and_get(self, backend):
        assert backend.kv_set("key1", "value1") is None  # no previous
        assert backend.kv_get("key1") == "value1"

    def test_set_returns_old_value(self, backend):
        backend.kv_set("key1", "v1")
        old = backend.kv_set("key1", "v2")
        assert old == "v1"
        assert backend.kv_get("key1") == "v2"

    def test_delete(self, backend):
        backend.kv_set("key1", "v1")
        assert backend.kv_delete("key1")
        assert backend.kv_get("key1") is None

    def test_delete_nonexistent(self, backend):
        assert not backend.kv_delete("nope")

    def test_list(self, backend):
        backend.kv_set("b", "2")
        backend.kv_set("a", "1")
        assert backend.kv_list() == [("a", "1"), ("b", "2")]

    def test_search(self, backend):
        backend.kv_set("project_name", "turnstone")
        backend.kv_set("version", "0.3")
        results = backend.kv_search("turnstone")
        assert len(results) == 1
        assert results[0] == ("project_name", "turnstone")

    def test_search_empty_lists_all(self, backend):
        backend.kv_set("a", "1")
        backend.kv_set("b", "2")
        assert len(backend.kv_search("")) == 2


# -- Conversation search -------------------------------------------------------


class TestSearch:
    def test_search_history(self, backend):
        backend.register_session("s1")
        backend.save_message("s1", "user", "hello world")
        backend.save_message("s1", "user", "goodbye world")
        results = backend.search_history("hello")
        assert len(results) >= 1
        assert any("hello" in str(r[3]) for r in results)

    def test_search_recent(self, backend):
        backend.register_session("s1")
        backend.save_message("s1", "user", "msg1")
        backend.save_message("s1", "user", "msg2")
        results = backend.search_history_recent(limit=1)
        assert len(results) == 1


# -- Workstream operations -----------------------------------------------------


class TestWorkstreams:
    def test_register_and_list(self, backend):
        backend.register_workstream("ws1", node_id="node-a", name="first")
        backend.register_workstream("ws2", node_id="node-a", name="second")
        rows = backend.list_workstreams()
        assert len(rows) == 2
        ws_ids = {r[0] for r in rows}
        assert ws_ids == {"ws1", "ws2"}

    def test_register_idempotent(self, backend):
        backend.register_workstream("ws1", name="first")
        backend.register_workstream("ws1", name="overwrite")
        rows = backend.list_workstreams()
        assert len(rows) == 1
        assert rows[0][2] == "first"  # name preserved from first insert

    def test_update_state(self, backend):
        backend.register_workstream("ws1")
        backend.update_workstream_state("ws1", "running")
        rows = backend.list_workstreams()
        assert rows[0][3] == "running"

    def test_update_name(self, backend):
        backend.register_workstream("ws1", name="old")
        backend.update_workstream_name("ws1", "new")
        rows = backend.list_workstreams()
        assert rows[0][2] == "new"

    def test_delete(self, backend):
        backend.register_workstream("ws1")
        assert backend.delete_workstream("ws1") is True
        assert backend.list_workstreams() == []
        assert backend.delete_workstream("ws1") is False

    def test_list_by_node(self, backend):
        backend.register_workstream("ws1", node_id="node-a")
        backend.register_workstream("ws2", node_id="node-b")
        rows = backend.list_workstreams(node_id="node-a")
        assert len(rows) == 1
        assert rows[0][0] == "ws1"

    def test_session_with_ws_id(self, backend):
        backend.register_workstream("ws1", node_id="node-a")
        backend.register_session("s1", node_id="node-a", ws_id="ws1")
        backend.save_message("s1", "user", "hello")
        rows = backend.list_sessions()
        assert len(rows) == 1
        # Columns: sid, alias, title, created, updated, count, node_id, ws_id
        assert rows[0][6] == "node-a"
        assert rows[0][7] == "ws1"


# -- Lifecycle -----------------------------------------------------------------


class TestLifecycle:
    def test_close(self, backend):
        backend.close()  # Should not raise

    def test_isinstance_check(self, backend):
        from turnstone.core.storage._protocol import StorageBackend

        assert isinstance(backend, StorageBackend)
