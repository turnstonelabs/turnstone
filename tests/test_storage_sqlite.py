"""Tests for the SQLite storage backend."""

from __future__ import annotations

from typing import Any

# -- Workstream registration ---------------------------------------------------


class TestRegisterWorkstream:
    def test_register_creates_workstream(self, backend):
        backend.register_workstream("s1", title="Test")
        name = backend.get_workstream_display_name("s1")
        assert name == "Test"

    def test_register_idempotent(self, backend):
        backend.register_workstream("s1", title="First")
        backend.register_workstream("s1", title="Second")
        name = backend.get_workstream_display_name("s1")
        assert name == "First"  # INSERT OR IGNORE preserves first


class TestSaveAndLoadMessages:
    def test_roundtrip(self, backend):
        backend.register_workstream("s1")
        backend.save_message("s1", "user", "hello")
        backend.save_message("s1", "assistant", "world")
        msgs = backend.load_messages("s1")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "hello"
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["content"] == "world"

    def test_tool_call_grouping(self, backend):
        import json

        backend.register_workstream("s1")
        tc_json = json.dumps(
            [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"cmd":"ls"}'},
                }
            ]
        )
        backend.save_message("s1", "user", "do something")
        backend.save_message("s1", "assistant", None, tool_calls=tc_json)
        backend.save_message("s1", "tool", "file.txt", tool_call_id="c1")
        backend.save_message("s1", "assistant", "done")
        msgs = backend.load_messages("s1")
        assert len(msgs) == 4
        assert msgs[1]["role"] == "assistant"
        assert len(msgs[1]["tool_calls"]) == 1
        assert msgs[1]["tool_calls"][0]["id"] == "c1"
        assert msgs[2]["role"] == "tool"
        assert msgs[2]["content"] == "file.txt"

    def test_incomplete_turn_repair(self, backend):
        import json

        backend.register_workstream("s1")
        tc_json = json.dumps(
            [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"cmd":"ls"}'},
                },
                {
                    "id": "c2",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"a"}'},
                },
            ]
        )
        backend.save_message("s1", "user", "do something")
        backend.save_message("s1", "assistant", None, tool_calls=tc_json)
        # Only 1 result for 2 calls — incomplete turn
        backend.save_message("s1", "tool", "ok", tool_call_id="c1")
        msgs = backend.load_messages("s1")
        # Incomplete turn should be stripped
        assert len(msgs) == 1  # only the user message remains

    def test_provider_data_preserved(self, backend):
        import json

        backend.register_workstream("s1")
        pd = json.dumps({"encrypted": True})
        backend.save_message("s1", "assistant", "hi", provider_data=pd)
        msgs = backend.load_messages("s1")
        assert msgs[0].get("_provider_content") == {"encrypted": True}

    def test_empty_workstream_returns_empty(self, backend):
        assert backend.load_messages("nonexistent") == []


class TestSaveMessagesBulk:
    def test_bulk_roundtrip(self, backend):
        backend.register_workstream("s1")
        backend.save_messages_bulk(
            [
                {"ws_id": "s1", "role": "user", "content": "hello"},
                {"ws_id": "s1", "role": "assistant", "content": "hi there"},
                {"ws_id": "s1", "role": "user", "content": "bye"},
            ]
        )
        msgs = backend.load_messages("s1")
        assert len(msgs) == 3
        assert msgs[0]["content"] == "hello"
        assert msgs[2]["content"] == "bye"

    def test_bulk_preserves_tool_calls(self, backend):
        import json

        backend.register_workstream("s1")
        tc = json.dumps(
            [{"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]
        )
        backend.save_messages_bulk(
            [
                {"ws_id": "s1", "role": "user", "content": "do it"},
                {"ws_id": "s1", "role": "assistant", "content": None, "tool_calls": tc},
                {"ws_id": "s1", "role": "tool", "content": "ok", "tool_call_id": "c1"},
            ]
        )
        msgs = backend.load_messages("s1")
        assert len(msgs) == 3
        assert msgs[1]["tool_calls"][0]["id"] == "c1"

    def test_bulk_empty_is_noop(self, backend):
        backend.save_messages_bulk([])

    def test_bulk_updates_workstream_timestamp(self, backend):
        backend.register_workstream("s1")
        # Save a message to establish an initial updated timestamp
        backend.save_message("s1", "user", "seed")
        rows_before = backend.list_workstreams_with_history()
        updated_before = rows_before[0][5]  # updated column

        backend.save_messages_bulk([{"ws_id": "s1", "role": "user", "content": "bulk"}])
        rows_after = backend.list_workstreams_with_history()
        updated_after = rows_after[0][5]
        assert updated_after >= updated_before


class TestListWorkstreamsWithHistory:
    def test_lists_workstreams_with_messages(self, backend):
        backend.register_workstream("s1")
        backend.save_message("s1", "user", "hi")
        backend.register_workstream("s2")  # no messages
        rows = backend.list_workstreams_with_history()
        assert len(rows) == 1
        assert rows[0][0] == "s1"

    def test_respects_limit(self, backend):
        for i in range(5):
            sid = f"s{i}"
            backend.register_workstream(sid)
            backend.save_message(sid, "user", f"msg {i}")
        rows = backend.list_workstreams_with_history(limit=3)
        assert len(rows) == 3


class TestDeleteWorkstream:
    def test_deletes_all_data(self, backend):
        backend.register_workstream("s1")
        backend.save_message("s1", "user", "hi")
        backend.save_workstream_config("s1", {"temp": "0.5"})
        assert backend.delete_workstream("s1")
        assert backend.load_messages("s1") == []
        assert backend.load_workstream_config("s1") == {}
        assert backend.get_workstream_display_name("s1") is None


class TestPruneWorkstreams:
    def test_orphan_removed(self, backend):
        backend.register_workstream("orphan")
        orphans, stale = backend.prune_workstreams()
        assert orphans == 1

    def test_stale_removed(self, backend):
        import sqlalchemy as sa

        backend.register_workstream("old")
        backend.save_message("old", "user", "hi")
        # Force old timestamp
        with backend._engine.connect() as conn:
            conn.execute(
                sa.text("UPDATE workstreams SET updated = '2020-01-01' WHERE ws_id = 'old'")
            )
            conn.commit()
        _, stale = backend.prune_workstreams(retention_days=30)
        assert stale == 1


class TestResolveWorkstream:
    def test_exact_alias(self, backend):
        backend.register_workstream("s1")
        backend.set_workstream_alias("s1", "myalias")
        assert backend.resolve_workstream("myalias") == "s1"

    def test_exact_id(self, backend):
        backend.register_workstream("abc-123-def")
        assert backend.resolve_workstream("abc-123-def") == "abc-123-def"

    def test_prefix_match(self, backend):
        backend.register_workstream("abc-123-def")
        assert backend.resolve_workstream("abc") == "abc-123-def"

    def test_not_found(self, backend):
        assert backend.resolve_workstream("nonexistent") is None


# -- Workstream config ---------------------------------------------------------


class TestWorkstreamConfig:
    def test_roundtrip(self, backend):
        backend.register_workstream("s1")
        backend.save_workstream_config("s1", {"temperature": "0.7", "effort": "high"})
        cfg = backend.load_workstream_config("s1")
        assert cfg == {"temperature": "0.7", "effort": "high"}

    def test_empty_config(self, backend):
        assert backend.load_workstream_config("nonexistent") == {}


# -- Workstream metadata ------------------------------------------------------


class TestWorkstreamMetadata:
    def test_alias(self, backend):
        backend.register_workstream("s1")
        assert backend.set_workstream_alias("s1", "my-session")
        assert backend.get_workstream_display_name("s1") == "my-session"

    def test_alias_conflict(self, backend):
        backend.register_workstream("s1")
        backend.register_workstream("s2")
        backend.set_workstream_alias("s1", "taken")
        assert not backend.set_workstream_alias("s2", "taken")

    def test_title(self, backend):
        backend.register_workstream("s1")
        backend.update_workstream_title("s1", "My Title")
        assert backend.get_workstream_display_name("s1") == "My Title"

    def test_alias_preferred_over_title(self, backend):
        backend.register_workstream("s1")
        backend.update_workstream_title("s1", "Title")
        backend.set_workstream_alias("s1", "Alias")
        assert backend.get_workstream_display_name("s1") == "Alias"


# -- Conversation search -------------------------------------------------------


class TestSearch:
    def test_search_history(self, backend):
        backend.register_workstream("s1")
        backend.save_message("s1", "user", "hello world")
        backend.save_message("s1", "user", "goodbye world")
        results = backend.search_history("hello")
        assert len(results) >= 1
        assert any("hello" in str(r[3]) for r in results)

    def test_search_recent(self, backend):
        backend.register_workstream("s1")
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

    def test_workstream_with_messages_in_history(self, backend):
        backend.register_workstream("ws1", node_id="node-a")
        backend.save_message("ws1", "user", "hello")
        rows = backend.list_workstreams_with_history()
        assert len(rows) == 1
        # Columns: ws_id, alias, title, name, created, updated, count, node_id
        assert rows[0][0] == "ws1"
        assert rows[0][7] == "node-a"


# -- Structured memory touch ---------------------------------------------------


class TestTouchStructuredMemory:
    @staticmethod
    def _create_memory(
        backend: Any, name: str = "m1", scope: str = "global", scope_id: str = ""
    ) -> None:
        import uuid

        backend.create_structured_memory(
            memory_id=str(uuid.uuid4()),
            name=name,
            description="test desc",
            mem_type="project",
            scope=scope,
            scope_id=scope_id,
            content="test content",
        )

    def test_batch_touch_multiple(self, backend):
        self._create_memory(backend, name="a")
        self._create_memory(backend, name="b")
        self._create_memory(backend, name="c")

        count = backend.touch_structured_memories(
            [
                ("a", "global", ""),
                ("b", "global", ""),
                ("c", "global", ""),
            ]
        )
        assert count == 3

        for name in ("a", "b", "c"):
            mem = backend.get_structured_memory_by_name(name, "global", "")
            assert int(mem["access_count"]) == 1

    def test_batch_touch_empty_list(self, backend):
        assert backend.touch_structured_memories([]) == 0

    def test_batch_touch_partial_match(self, backend):
        self._create_memory(backend, name="exists")

        count = backend.touch_structured_memories(
            [
                ("exists", "global", ""),
                ("missing", "global", ""),
            ]
        )
        assert count == 1

        mem = backend.get_structured_memory_by_name("exists", "global", "")
        assert int(mem["access_count"]) == 1

    def test_batch_touch_with_duplicates(self, backend):
        """Duplicate keys in batch should each increment access_count once."""
        self._create_memory(backend, name="dup")

        # Two identical keys — storage gets called twice for the same row
        count = backend.touch_structured_memories([("dup", "global", ""), ("dup", "global", "")])
        assert count == 2

        mem = backend.get_structured_memory_by_name("dup", "global", "")
        assert int(mem["access_count"]) == 2


# -- Lifecycle -----------------------------------------------------------------


class TestLifecycle:
    def test_close(self, backend):
        backend.close()  # Should not raise

    def test_isinstance_check(self, backend):
        from turnstone.core.storage._protocol import StorageBackend

        assert isinstance(backend, StorageBackend)
