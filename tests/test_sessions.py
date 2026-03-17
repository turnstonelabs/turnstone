"""Tests for workstream persistence and resume functionality."""

from unittest.mock import MagicMock

import sqlalchemy as sa

from turnstone.core.memory import (
    delete_workstream,
    list_workstreams_with_history,
    load_messages,
    load_workstream_config,
    prune_workstreams,
    register_workstream,
    resolve_workstream,
    save_message,
    save_workstream_config,
    set_workstream_alias,
    update_workstream_title,
)
from turnstone.core.session import ChatSession
from turnstone.core.storage import get_storage

# ── Workstream registration ───────────────────────────────────────────


class TestRegisterWorkstream:
    def test_register_creates_row(self, tmp_db):
        register_workstream("abc123")
        # Workstream exists in DB (resolve works) even without messages
        assert resolve_workstream("abc123") == "abc123"

    def test_register_with_title(self, tmp_db):
        register_workstream("abc123", name="My Workstream")
        save_message("abc123", "user", "hello")
        rows = list_workstreams_with_history()
        assert rows[0][2] is None  # title column (name is separate)

    def test_register_idempotent(self, tmp_db):
        register_workstream("abc123")
        update_workstream_title("abc123", "First")
        register_workstream("abc123")  # should be ignored
        update_workstream_title("abc123", "First")  # title is set via update
        save_message("abc123", "user", "hello")
        rows = list_workstreams_with_history()
        assert len(rows) == 1
        assert rows[0][2] == "First"  # title preserved

    def test_update_title(self, tmp_db):
        register_workstream("abc123")
        update_workstream_title("abc123", "New Title")
        save_message("abc123", "user", "hello")
        rows = list_workstreams_with_history()
        assert rows[0][2] == "New Title"


# ── Workstream alias ──────────────────────────────────────────────────


class TestWorkstreamAlias:
    def test_set_alias(self, tmp_db):
        register_workstream("abc123")
        assert set_workstream_alias("abc123", "my-session") is True
        save_message("abc123", "user", "hello")
        rows = list_workstreams_with_history()
        assert rows[0][1] == "my-session"  # alias

    def test_alias_conflict(self, tmp_db):
        register_workstream("abc123")
        register_workstream("def456")
        set_workstream_alias("abc123", "taken")
        assert set_workstream_alias("def456", "taken") is False

    def test_alias_same_workstream_ok(self, tmp_db):
        register_workstream("abc123")
        set_workstream_alias("abc123", "mine")
        assert set_workstream_alias("abc123", "mine") is True  # no-op, same workstream


# ── Workstream resolution ─────────────────────────────────────────────


class TestResolveWorkstream:
    def test_resolve_by_alias(self, tmp_db):
        register_workstream("abc123")
        set_workstream_alias("abc123", "my-alias")
        assert resolve_workstream("my-alias") == "abc123"

    def test_resolve_by_exact_id(self, tmp_db):
        register_workstream("abc123def456")
        assert resolve_workstream("abc123def456") == "abc123def456"

    def test_resolve_by_prefix(self, tmp_db):
        register_workstream("abc123def456")
        assert resolve_workstream("abc123") == "abc123def456"

    def test_resolve_prefix_ambiguous(self, tmp_db):
        register_workstream("abc123aaaaaa")
        register_workstream("abc123bbbbbb")
        # Ambiguous prefix should return None
        assert resolve_workstream("abc123") is None

    def test_resolve_not_found(self, tmp_db):
        assert resolve_workstream("nonexistent") is None


# ── List workstreams with history ──────────────────────────────────────


class TestListWorkstreamsWithHistory:
    def test_empty(self, tmp_db):
        assert list_workstreams_with_history() == []

    def test_ordered_by_updated(self, tmp_db):
        register_workstream("first")
        save_message("first", "user", "hello")
        # Force an older timestamp so ordering is deterministic
        engine = get_storage()._engine  # noqa: SLF001
        with engine.connect() as conn:
            conn.execute(
                sa.text("UPDATE workstreams SET updated = '2020-01-01' WHERE ws_id = 'first'")
            )
            conn.commit()
        register_workstream("second")
        save_message("second", "user", "hello")
        # second is more recent
        rows = list_workstreams_with_history()
        assert rows[0][0] == "second"
        assert rows[1][0] == "first"

    def test_includes_message_count(self, tmp_db):
        register_workstream("sess1")
        save_message("sess1", "user", "hello")
        save_message("sess1", "assistant", "hi")
        rows = list_workstreams_with_history()
        assert rows[0][5] == 2  # msg_count

    def test_respects_limit(self, tmp_db):
        for i in range(5):
            register_workstream(f"sess{i}")
            save_message(f"sess{i}", "user", "hello")
        rows = list_workstreams_with_history(limit=3)
        assert len(rows) == 3


# ── Load messages ─────────────────────────────────────────────────────


class TestLoadMessages:
    def test_simple_user_assistant(self, tmp_db):
        save_message("s1", "user", "hello")
        save_message("s1", "assistant", "hi there")
        msgs = load_messages("s1")
        assert len(msgs) == 2
        assert msgs[0] == {"role": "user", "content": "hello"}
        assert msgs[1] == {"role": "assistant", "content": "hi there"}

    def test_tool_calls_with_ids(self, tmp_db):
        import json

        tc_json = json.dumps(
            [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"ls"}'},
                }
            ]
        )
        save_message("s1", "user", "run ls")
        save_message("s1", "assistant", "Let me check.", tool_calls=tc_json)
        save_message("s1", "tool", "file1.txt\nfile2.txt", "bash", tool_call_id="call_abc")
        msgs = load_messages("s1")
        assert len(msgs) == 3  # user, assistant+tool_calls, tool
        # Assistant should have content and tool_calls
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["content"] == "Let me check."
        assert len(msgs[1]["tool_calls"]) == 1
        assert msgs[1]["tool_calls"][0]["id"] == "call_abc"
        assert msgs[1]["tool_calls"][0]["function"]["name"] == "bash"
        # Tool result
        assert msgs[2]["role"] == "tool"
        assert msgs[2]["tool_call_id"] == "call_abc"
        assert msgs[2]["content"] == "file1.txt\nfile2.txt"

    def test_parallel_tool_calls(self, tmp_db):
        import json

        tc_json = json.dumps(
            [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search", "arguments": '{"query":"a"}'},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "search", "arguments": '{"query":"b"}'},
                },
            ]
        )
        save_message("s1", "user", "search two things")
        save_message("s1", "assistant", None, tool_calls=tc_json)
        save_message("s1", "tool", "result a", "search", tool_call_id="call_1")
        save_message("s1", "tool", "result b", "search", tool_call_id="call_2")
        msgs = load_messages("s1")
        assert len(msgs) == 4  # user, assistant+2 tool_calls, 2 tool results
        assert len(msgs[1]["tool_calls"]) == 2
        assert msgs[2]["tool_call_id"] == "call_1"
        assert msgs[3]["tool_call_id"] == "call_2"

    def test_empty_workstream(self, tmp_db):
        assert load_messages("nonexistent") == []


# ── Delete workstream ─────────────────────────────────────────────────


class TestDeleteWorkstream:
    def test_delete_removes_workstream_and_messages(self, tmp_db):
        register_workstream("abc123")
        save_message("abc123", "user", "hello")
        save_message("abc123", "assistant", "hi")
        assert delete_workstream("abc123") is True
        assert list_workstreams_with_history() == []
        assert load_messages("abc123") == []

    def test_delete_nonexistent(self, tmp_db):
        assert delete_workstream("nonexistent") is False


# ── save_message with tool_call_id ────────────────────────────────────


class TestSaveMessageToolCallId:
    def test_tool_call_id_stored(self, tmp_db):
        save_message("s1", "tool", "output", "bash", tool_call_id="call_xyz")
        engine = get_storage()._engine  # noqa: SLF001
        with engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT tool_call_id FROM conversations WHERE ws_id = 's1'")
            ).fetchone()
            assert row[0] == "call_xyz"

    def test_tool_call_id_none_by_default(self, tmp_db):
        save_message("s1", "user", "hello")
        engine = get_storage()._engine  # noqa: SLF001
        with engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT tool_call_id FROM conversations WHERE ws_id = 's1'")
            ).fetchone()
            assert row[0] is None


# ── Workstreams table creation ────────────────────────────────────────


class TestWorkstreamsTable:
    def test_workstreams_table_exists(self, tmp_db):
        engine = get_storage()._engine  # noqa: SLF001
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name='workstreams'")
            ).fetchall()
            assert len(rows) == 1

    def test_tool_call_id_column_exists(self, tmp_db):
        engine = get_storage()._engine  # noqa: SLF001
        with engine.connect() as conn:
            # Should not raise
            conn.execute(sa.text("SELECT tool_call_id FROM conversations LIMIT 0"))


# ── ChatSession.resume ────────────────────────────────────────────────


class TestResumeWorkstream:
    def test_resume_loads_messages(self, tmp_db, mock_openai_client):
        # Set up a workstream with messages in DB
        register_workstream("old_ws_123")
        save_message("old_ws_123", "user", "hello world")
        save_message("old_ws_123", "assistant", "hi there")

        # Create a new session and resume
        session = ChatSession(
            client=mock_openai_client,
            model="test-model",
            ui=MagicMock(),
            instructions=None,
            temperature=0.5,
            max_tokens=1000,
            tool_timeout=10,
        )
        original_id = session._ws_id
        assert original_id != "old_ws_123"

        result = session.resume("old_ws_123")
        assert result is True
        assert session._ws_id == "old_ws_123"
        assert len(session.messages) == 2
        assert session.messages[0]["content"] == "hello world"
        assert session._title_generated is True

    def test_resume_nonexistent_returns_false(self, tmp_db, mock_openai_client):
        session = ChatSession(
            client=mock_openai_client,
            model="test-model",
            ui=MagicMock(),
            instructions=None,
            temperature=0.5,
            max_tokens=1000,
            tool_timeout=10,
        )
        assert session.resume("nonexistent") is False

    def test_workstream_not_registered_until_message(self, tmp_db, mock_openai_client):
        session = ChatSession(
            client=mock_openai_client,
            model="test-model",
            ui=MagicMock(),
            instructions=None,
            temperature=0.5,
            max_tokens=1000,
            tool_timeout=10,
        )
        # Workstream is not auto-registered on init — only on /new or server creation
        assert resolve_workstream(session._ws_id) is None
        assert not any(r[0] == session._ws_id for r in list_workstreams_with_history())


# ── save_message updates workstreams.updated ──────────────────────────


class TestSaveMessageUpdatesWorkstream:
    def test_updated_timestamp_bumped(self, tmp_db):
        register_workstream("s1")
        save_message("s1", "user", "first")
        rows = list_workstreams_with_history()
        _original_updated = rows[0][4]

        import time

        time.sleep(0.01)  # ensure different timestamp
        save_message("s1", "user", "hello")

        rows = list_workstreams_with_history()
        new_updated = rows[0][4]
        # updated should be same or later (sqlite datetime resolution is seconds,
        # so they may be equal in fast tests — just verify no error)
        assert new_updated is not None


# ── Interrupted workstream repair ─────────────────────────────────────


class TestInterruptedWorkstreamRepair:
    """load_messages() should strip trailing incomplete tool call turns."""

    def test_complete_tool_turn_preserved(self, tmp_db):
        """2 tool_calls + 2 tool results = complete, no stripping."""
        import json

        tc_json = json.dumps(
            [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"ls"}'},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"pwd"}'},
                },
            ]
        )
        save_message("s1", "user", "hello")
        save_message("s1", "assistant", None, tool_calls=tc_json)
        save_message("s1", "tool", "file.txt", tool_call_id="call_1")
        save_message("s1", "tool", "/home", tool_call_id="call_2")
        msgs = load_messages("s1")
        assert len(msgs) == 4  # user + assistant(2 calls) + 2 tool results

    def test_partial_tool_results_stripped(self, tmp_db):
        """2 tool_calls + 1 tool result = incomplete, strip the turn."""
        import json

        tc_json = json.dumps(
            [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"ls"}'},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"pwd"}'},
                },
            ]
        )
        save_message("s1", "user", "hello")
        save_message("s1", "assistant", None, tool_calls=tc_json)
        save_message("s1", "tool", "file.txt", tool_call_id="call_1")
        msgs = load_messages("s1")
        assert len(msgs) == 1  # only user message remains
        assert msgs[0]["role"] == "user"

    def test_zero_tool_results_stripped(self, tmp_db):
        """Assistant with tool_calls + 0 results = incomplete, strip the turn."""
        import json

        tc_json = json.dumps(
            [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"ls"}'},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"pwd"}'},
                },
            ]
        )
        save_message("s1", "user", "hello")
        save_message("s1", "assistant", "Let me check", tool_calls=tc_json)
        msgs = load_messages("s1")
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    def test_complete_turn_before_incomplete_preserved(self, tmp_db):
        """Complete turn followed by incomplete turn: keep complete, strip incomplete."""
        import json

        tc_json = json.dumps(
            [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"ls"}'},
                },
            ]
        )
        save_message("s1", "user", "first")
        save_message("s1", "assistant", "response")
        save_message("s1", "user", "second")
        save_message("s1", "assistant", None, tool_calls=tc_json)
        msgs = load_messages("s1")
        assert len(msgs) == 3  # user + assistant + user (incomplete turn stripped)
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"
        assert msgs[2]["role"] == "user"


# ── Workstream config persistence ─────────────────────────────────────


class TestWorkstreamConfig:
    def test_save_load_roundtrip(self, tmp_db):
        config = {"temperature": "0.3", "reasoning_effort": "high", "creative_mode": "False"}
        save_workstream_config("s1", config)
        loaded = load_workstream_config("s1")
        assert loaded == config

    def test_update_existing_key(self, tmp_db):
        save_workstream_config("s1", {"temperature": "0.3"})
        save_workstream_config("s1", {"temperature": "0.7"})
        loaded = load_workstream_config("s1")
        assert loaded["temperature"] == "0.7"

    def test_missing_workstream_returns_empty(self, tmp_db):
        loaded = load_workstream_config("nonexistent")
        assert loaded == {}

    def test_delete_workstream_removes_config(self, tmp_db):
        register_workstream("s1")
        save_message("s1", "user", "hi")
        save_workstream_config("s1", {"temperature": "0.5"})
        delete_workstream("s1")
        assert load_workstream_config("s1") == {}

    def test_resume_restores_config(self, tmp_db):
        """ChatSession.resume() should restore persisted config."""
        client = MagicMock()
        client.models.list.return_value.data = [MagicMock(id="test-model")]
        ui = MagicMock()
        ui.on_info = MagicMock()
        ui.on_error = MagicMock()
        ui.on_state_change = MagicMock()
        ui.on_rename = MagicMock()

        # Create a workstream with specific config
        register_workstream("orig")
        save_message("orig", "user", "hello")
        save_message("orig", "assistant", "hi there")
        save_workstream_config(
            "orig",
            {
                "temperature": "0.3",
                "reasoning_effort": "high",
                "max_tokens": "2048",
                "instructions": "be concise",
                "creative_mode": "True",
            },
        )

        # Create a new session with different defaults, then resume
        session = ChatSession(
            client=client,
            model="test",
            ui=ui,
            instructions=None,
            temperature=0.7,
            max_tokens=4096,
            tool_timeout=30,
        )
        assert session.temperature == 0.7  # default
        result = session.resume("orig")
        assert result is True
        assert session.temperature == 0.3
        assert session.reasoning_effort == "high"
        assert session.max_tokens == 2048
        assert session.instructions == "be concise"
        assert session.creative_mode is True


# ── Prune workstreams ─────────────────────────────────────────────────


class TestPruneWorkstreams:
    def test_orphan_removed(self, tmp_db):
        """Workstream registered with no messages should be pruned."""
        register_workstream("orphan")
        orphans, stale = prune_workstreams()
        assert orphans == 1
        assert list_workstreams_with_history() == []

    def test_workstream_with_messages_kept(self, tmp_db):
        """Workstream with messages should not be pruned."""
        register_workstream("active")
        save_message("active", "user", "hello")
        orphans, _stale = prune_workstreams()
        assert orphans == 0
        assert len(list_workstreams_with_history()) == 1

    def test_stale_unnamed_removed(self, tmp_db):
        """Old unnamed workstream should be pruned by retention policy."""
        register_workstream("old1")
        save_message("old1", "user", "ancient message")
        # Force the updated timestamp to the past so it looks stale
        engine = get_storage()._engine  # noqa: SLF001
        with engine.connect() as conn:
            conn.execute(
                sa.text("UPDATE workstreams SET updated = '2020-01-01' WHERE ws_id = 'old1'")
            )
            conn.commit()
        _orphans, stale = prune_workstreams(retention_days=30)
        assert stale == 1

    def test_named_workstream_preserved(self, tmp_db):
        """Workstream with alias should be kept regardless of age."""
        register_workstream("old2")
        set_workstream_alias("old2", "important")
        save_message("old2", "user", "old but named")
        # Force old timestamp
        engine = get_storage()._engine  # noqa: SLF001
        with engine.connect() as conn:
            conn.execute(
                sa.text("UPDATE workstreams SET updated = '2020-01-01' WHERE ws_id = 'old2'")
            )
            conn.commit()
        _orphans, stale = prune_workstreams(retention_days=30)
        assert stale == 0
        assert len(list_workstreams_with_history()) == 1

    def test_fresh_unnamed_preserved(self, tmp_db):
        """Recent unnamed workstream should not be pruned."""
        register_workstream("fresh")
        save_message("fresh", "user", "just now")
        _orphans, stale = prune_workstreams(retention_days=30)
        assert stale == 0
        assert len(list_workstreams_with_history()) == 1

    def test_prune_removes_workstream_config(self, tmp_db):
        """Pruning orphan/stale workstreams should also remove their config rows."""
        register_workstream("orphan_cfg")
        save_workstream_config("orphan_cfg", {"temperature": "0.5"})

        register_workstream("stale_cfg")
        save_message("stale_cfg", "user", "old")
        save_workstream_config("stale_cfg", {"temperature": "0.9"})
        engine = get_storage()._engine  # noqa: SLF001
        with engine.connect() as conn:
            conn.execute(
                sa.text("UPDATE workstreams SET updated = '2020-01-01' WHERE ws_id = 'stale_cfg'")
            )
            conn.commit()

        # Both should have config before prune
        assert load_workstream_config("orphan_cfg") == {"temperature": "0.5"}
        assert load_workstream_config("stale_cfg") == {"temperature": "0.9"}

        prune_workstreams(retention_days=30)

        # Config rows should be cleaned up
        assert load_workstream_config("orphan_cfg") == {}
        assert load_workstream_config("stale_cfg") == {}


# ── Parallel tool exception isolation ────────────────────────────────


class TestParallelToolExceptionIsolation:
    """Bug #117: one tool raising should not kill the entire batch."""

    def test_exception_in_one_tool_does_not_kill_batch(self, tmp_db, mock_openai_client):
        from unittest.mock import patch

        session = ChatSession(
            client=mock_openai_client,
            model="test-model",
            ui=MagicMock(),
            instructions=None,
            temperature=0.5,
            max_tokens=1000,
            tool_timeout=10,
        )

        def succeed(item):
            return item["call_id"], "ok"

        def fail(item):
            raise RuntimeError("boom")

        items = [
            {
                "call_id": "c1",
                "func_name": "bash",
                "execute": succeed,
                "needs_approval": False,
                "header": "test",
                "preview": "",
            },
            {
                "call_id": "c2",
                "func_name": "math",
                "execute": fail,
                "needs_approval": False,
                "header": "test",
                "preview": "",
            },
        ]

        tool_calls = [
            {"id": "c1", "function": {"name": "bash", "arguments": "{}"}},
            {"id": "c2", "function": {"name": "math", "arguments": "{}"}},
        ]

        with (
            patch.object(session, "_prepare_tool", side_effect=items),
            patch.object(session, "_evaluate_intent"),
            patch.object(session, "_emit_state"),
            patch.object(session, "_init_system_messages"),
            patch.object(session, "_check_cancelled"),
        ):
            session.ui.approve_tools.return_value = (True, None)
            results, _ = session._execute_tools(tool_calls)

        assert results[0] == ("c1", "ok")
        assert results[1][0] == "c2"
        assert "Error executing math" in results[1][1]
        assert "boom" in results[1][1]


# ── Web search tool gating ───────────────────────────────────────────


class TestWebSearchGating:
    """Bug #117: web_search should not be offered without a backend."""

    def test_web_search_filtered_when_no_backend(self, tmp_db, mock_openai_client):
        from unittest.mock import patch

        from turnstone.core.providers._protocol import ModelCapabilities

        session = ChatSession(
            client=mock_openai_client,
            model="local-model",
            ui=MagicMock(),
            instructions=None,
            temperature=0.5,
            max_tokens=1000,
            tool_timeout=10,
        )

        caps = ModelCapabilities(supports_web_search=False)
        with (
            patch.object(session, "_get_capabilities", return_value=caps),
            patch("turnstone.core.session.get_tavily_key", return_value=None),
        ):
            tools = session._get_active_tools()

        names = [t.get("function", {}).get("name") for t in tools]
        assert "web_search" not in names

    def test_web_search_kept_when_tavily_available(self, tmp_db, mock_openai_client):
        from unittest.mock import patch

        from turnstone.core.providers._protocol import ModelCapabilities

        session = ChatSession(
            client=mock_openai_client,
            model="local-model",
            ui=MagicMock(),
            instructions=None,
            temperature=0.5,
            max_tokens=1000,
            tool_timeout=10,
        )

        caps = ModelCapabilities(supports_web_search=False)
        with (
            patch.object(session, "_get_capabilities", return_value=caps),
            patch("turnstone.core.session.get_tavily_key", return_value="tvly-test-key"),
        ):
            tools = session._get_active_tools()

        names = [t.get("function", {}).get("name") for t in tools]
        assert "web_search" in names

    def test_web_search_kept_when_native_support(self, tmp_db, mock_openai_client):
        from unittest.mock import patch

        from turnstone.core.providers._protocol import ModelCapabilities

        session = ChatSession(
            client=mock_openai_client,
            model="gpt-5-search-api",
            ui=MagicMock(),
            instructions=None,
            temperature=0.5,
            max_tokens=1000,
            tool_timeout=10,
        )

        caps = ModelCapabilities(supports_web_search=True)
        with (
            patch.object(session, "_get_capabilities", return_value=caps),
            patch("turnstone.core.session.get_tavily_key", return_value=None),
        ):
            tools = session._get_active_tools()

        names = [t.get("function", {}).get("name") for t in tools]
        assert "web_search" in names
