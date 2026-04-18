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


class TestLoadMessagesLimit:
    """Phase 3 added ``limit=N`` so cluster-inspect can avoid reading
    thousands of rows to return a tail-20 preview.  The contract: fetch
    the last N conversation rows (DESC + LIMIT at the SQL layer), then
    reverse into chronological order for reconstruction.  Approximate
    tail-N — a tool-call group straddling the cut produces an
    incomplete turn that the existing repair step strips."""

    def test_limit_none_fetches_all(self, backend):
        backend.register_workstream("s1")
        for i in range(10):
            backend.save_message("s1", "user", f"msg-{i}")
        msgs = backend.load_messages("s1", limit=None)
        assert len(msgs) == 10

    def test_limit_fetches_tail_in_chronological_order(self, backend):
        backend.register_workstream("s1")
        for i in range(10):
            backend.save_message("s1", "user", f"msg-{i:02d}")
        msgs = backend.load_messages("s1", limit=3)
        assert len(msgs) == 3
        # Chronological order preserved even though SQL fetched DESC.
        assert msgs[0]["content"] == "msg-07"
        assert msgs[1]["content"] == "msg-08"
        assert msgs[2]["content"] == "msg-09"

    def test_limit_exceeds_total_returns_all(self, backend):
        backend.register_workstream("s1")
        for i in range(5):
            backend.save_message("s1", "user", f"msg-{i}")
        msgs = backend.load_messages("s1", limit=100)
        assert len(msgs) == 5

    def test_limit_zero_fetches_all(self, backend):
        """limit<=0 matches the ``None`` branch — the SQL LIMIT is
        skipped, full history returned.  Belt-and-suspenders against
        callers that pass the clamped ``max(0, limit)`` result."""
        backend.register_workstream("s1")
        for i in range(5):
            backend.save_message("s1", "user", f"msg-{i}")
        assert len(backend.load_messages("s1", limit=0)) == 5

    def test_limit_boundary_straddles_tool_call_group(self, backend):
        """Document the approximate-tail-N semantics the ``load_messages``
        docstring warns about: when the tail slice opens mid-tool-call-
        group, the orphaned ``role=tool`` row is returned verbatim
        (the incomplete-turn repair at ``_reconstruct_messages`` only
        strips incomplete *assistant-with-tool_calls* groups, not
        orphaned tool-response rows).

        Callers that need strict tail-N semantics (e.g. re-hydrating a
        session to resume generation) must either request more than
        they need and post-filter, or do a full load.  The cluster-
        inspect preview path tolerates orphan tool rows because the
        UI renders them as standalone tool-output blocks.

        Seed: [user, assistant w/ 1 tool_call, tool result, assistant].
        Fetch tail=2 → [tool result, assistant].  Orphan tool row
        survives; this is expected behavior, not a bug."""
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
        backend.save_message("s1", "user", "do it")
        backend.save_message("s1", "assistant", None, tool_calls=tc_json)
        backend.save_message("s1", "tool", "output", tool_call_id="c1")
        backend.save_message("s1", "assistant", "done")

        # Full load: 4 messages (complete turn, assistant reply).
        assert len(backend.load_messages("s1")) == 4

        # Tail=2: orphan tool row + final assistant reply.
        tail = backend.load_messages("s1", limit=2)
        assert len(tail) == 2
        assert tail[0]["role"] == "tool"
        assert tail[0]["content"] == "output"
        assert tail[1]["role"] == "assistant"
        assert tail[1]["content"] == "done"

    def test_limit_keeps_complete_tool_call_group_when_fully_contained(self, backend):
        """Tool-call groups entirely inside the tail slice survive intact."""
        import json

        backend.register_workstream("s1")
        tc_json = json.dumps(
            [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"p":"a"}'},
                }
            ]
        )
        backend.save_message("s1", "user", "older message")
        backend.save_message("s1", "assistant", None, tool_calls=tc_json)
        backend.save_message("s1", "tool", "contents", tool_call_id="c1")
        backend.save_message("s1", "assistant", "summarized")

        # Tail=3 captures the full group + assistant reply (drops
        # only the oldest user message).
        tail = backend.load_messages("s1", limit=3)
        assert len(tail) == 3
        assert tail[0]["role"] == "assistant"
        assert len(tail[0]["tool_calls"]) == 1
        assert tail[1]["role"] == "tool"
        assert tail[1]["content"] == "contents"
        assert tail[2]["content"] == "summarized"

    def test_limit_bounds_attachment_scan(self, backend):
        """When ``limit=N`` is set, ``load_attachments_for_messages``
        receives only the fetched message ids — the attachment query
        must not fall back to a full-workstream scan.  Otherwise the
        tail-N optimization on conversations is partly undone for
        workstreams with many attachments."""
        from unittest.mock import patch

        backend.register_workstream("s1")
        for i in range(20):
            backend.save_message("s1", "user", f"msg-{i:02d}")

        captured: dict[str, list[int] | None] = {}
        orig = backend.load_attachments_for_messages

        def _spy(ws_id, *, message_ids=None):
            captured["message_ids"] = list(message_ids) if message_ids is not None else None
            return orig(ws_id, message_ids=message_ids)

        with patch.object(backend, "load_attachments_for_messages", side_effect=_spy):
            backend.load_messages("s1", limit=5)
        # Tail-N request passed a bounded list of exactly 5 ids.
        assert captured["message_ids"] is not None
        assert len(captured["message_ids"]) == 5

        with patch.object(backend, "load_attachments_for_messages", side_effect=_spy):
            backend.load_messages("s1")
        # Full-load request passes None → backend scans all attachments.
        assert captured["message_ids"] is None


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

    def test_kind_filter_excludes_coordinators(self, backend):
        """The interactive 'saved workstreams' sidebar calls this with
        kind=INTERACTIVE so coordinator rows (which also persist
        conversation history) don't leak into the interactive UI."""
        from turnstone.core.workstream import WorkstreamKind

        backend.register_workstream("interactive-1", kind=WorkstreamKind.INTERACTIVE)
        backend.save_message("interactive-1", "user", "hi")
        backend.register_workstream("coord-1", kind=WorkstreamKind.COORDINATOR)
        backend.save_message("coord-1", "user", "plan something")

        # Default (no filter) returns both — preserves legacy behaviour.
        rows_all = backend.list_workstreams_with_history()
        assert {r[0] for r in rows_all} == {"interactive-1", "coord-1"}

        # kind=INTERACTIVE drops the coordinator row at the SQL layer.
        rows_i = backend.list_workstreams_with_history(kind=WorkstreamKind.INTERACTIVE)
        assert {r[0] for r in rows_i} == {"interactive-1"}

        # kind=COORDINATOR symmetric — for admin tooling that wants
        # the opposite view.
        rows_c = backend.list_workstreams_with_history(kind=WorkstreamKind.COORDINATOR)
        assert {r[0] for r in rows_c} == {"coord-1"}

    def test_kind_filter_accepts_string(self, backend):
        """String form (``"interactive"``) works too — matches how the
        memory.py helper forwards caller-supplied values."""
        from turnstone.core.workstream import WorkstreamKind

        backend.register_workstream("interactive-1", kind=WorkstreamKind.INTERACTIVE)
        backend.save_message("interactive-1", "user", "hi")
        backend.register_workstream("coord-1", kind=WorkstreamKind.COORDINATOR)
        backend.save_message("coord-1", "user", "plan")

        rows = backend.list_workstreams_with_history(kind="interactive")
        assert {r[0] for r in rows} == {"interactive-1"}


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


# -- Per-workstream usage aggregation -----------------------------------------


class TestSumWorkstreamTokens:
    """``sum_workstream_tokens`` powers the inspect-time token fallback for
    idle children — a regression here would surface as wrong tokens in
    the coordinator's inspect output rather than a focused test failure,
    so guard it directly."""

    def test_empty_ws_id_returns_zero(self, backend):
        assert backend.sum_workstream_tokens("") == 0

    def test_no_events_returns_zero(self, backend):
        assert backend.sum_workstream_tokens("never-seen") == 0

    def test_sums_prompt_and_completion_across_events(self, backend):
        backend.record_usage_event(
            event_id="e1", ws_id="ws-a", prompt_tokens=10, completion_tokens=5
        )
        backend.record_usage_event(
            event_id="e2", ws_id="ws-a", prompt_tokens=200, completion_tokens=80
        )
        assert backend.sum_workstream_tokens("ws-a") == 10 + 5 + 200 + 80

    def test_scoped_to_requested_ws_id(self, backend):
        """Other workstreams' usage events must not leak into the sum."""
        backend.record_usage_event(
            event_id="e1", ws_id="ws-a", prompt_tokens=100, completion_tokens=50
        )
        backend.record_usage_event(
            event_id="e2", ws_id="ws-b", prompt_tokens=999, completion_tokens=999
        )
        assert backend.sum_workstream_tokens("ws-a") == 150
        assert backend.sum_workstream_tokens("ws-b") == 1998


class TestBatchPrimitives:
    """``get_workstreams_batch`` and ``sum_workstream_tokens_batch`` power
    ``wait_for_workstream``'s per-tick polling.  Direct backend coverage
    here so a regression surfaces as a focused failure rather than as
    wrong tokens / spurious denied states in a coordinator session."""

    def test_get_workstreams_batch_empty_input(self, backend):
        assert backend.get_workstreams_batch([]) == {}

    def test_get_workstreams_batch_returns_row_per_id(self, backend):
        backend.register_workstream("a", title="A", kind="interactive")
        backend.register_workstream("b", title="B", kind="interactive", parent_ws_id="a")
        result = backend.get_workstreams_batch(["a", "b"])
        assert set(result.keys()) == {"a", "b"}
        assert result["a"]["ws_id"] == "a"
        assert result["b"]["parent_ws_id"] == "a"

    def test_get_workstreams_batch_missing_id_returns_none(self, backend):
        backend.register_workstream("a")
        result = backend.get_workstreams_batch(["a", "missing"])
        assert result["a"] is not None
        assert result["missing"] is None

    def test_get_workstreams_batch_drops_empty_strings(self, backend):
        """Empty / non-string ids must not pollute the IN clause."""
        backend.register_workstream("a")
        result = backend.get_workstreams_batch(["a", "", "  "])
        # Only the non-empty id is kept; whitespace-only strings are
        # passed through (the helper only strips truly-empty entries).
        assert "a" in result
        assert result["a"] is not None

    def test_sum_workstream_tokens_batch_empty_input(self, backend):
        assert backend.sum_workstream_tokens_batch([]) == {}

    def test_sum_workstream_tokens_batch_aggregates_per_id(self, backend):
        backend.record_usage_event(event_id="e1", ws_id="a", prompt_tokens=10, completion_tokens=5)
        backend.record_usage_event(event_id="e2", ws_id="a", prompt_tokens=20, completion_tokens=10)
        backend.record_usage_event(
            event_id="e3", ws_id="b", prompt_tokens=100, completion_tokens=50
        )
        result = backend.sum_workstream_tokens_batch(["a", "b", "c"])
        assert result == {"a": 45, "b": 150, "c": 0}

    def test_sum_workstream_tokens_batch_missing_id_defaults_zero(self, backend):
        result = backend.sum_workstream_tokens_batch(["never-seen"])
        assert result == {"never-seen": 0}


# -- Lifecycle -----------------------------------------------------------------


class TestLifecycle:
    def test_close(self, backend):
        backend.close()  # Should not raise

    def test_isinstance_check(self, backend):
        from turnstone.core.storage._protocol import StorageBackend

        assert isinstance(backend, StorageBackend)
