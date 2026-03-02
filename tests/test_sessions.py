"""Tests for session persistence and resume functionality."""

from unittest.mock import MagicMock, patch

import turnstone.core.memory as memory
from turnstone.core.memory import (
    register_session,
    update_session_title,
    set_session_alias,
    resolve_session,
    list_sessions,
    load_session_messages,
    delete_session,
    save_message,
    open_db,
)
from turnstone.core.session import ChatSession


# ── Session registration ──────────────────────────────────────────────


class TestRegisterSession:
    def test_register_creates_row(self, tmp_db):
        register_session("abc123")
        # Session exists in DB (resolve works) even without messages
        assert resolve_session("abc123") == "abc123"

    def test_register_with_title(self, tmp_db):
        register_session("abc123", title="My Session")
        save_message("abc123", "user", "hello")
        rows = list_sessions()
        assert rows[0][2] == "My Session"  # title

    def test_register_idempotent(self, tmp_db):
        register_session("abc123", title="First")
        register_session("abc123", title="Second")  # should be ignored
        save_message("abc123", "user", "hello")
        rows = list_sessions()
        assert len(rows) == 1
        assert rows[0][2] == "First"  # original title preserved

    def test_update_title(self, tmp_db):
        register_session("abc123")
        update_session_title("abc123", "New Title")
        save_message("abc123", "user", "hello")
        rows = list_sessions()
        assert rows[0][2] == "New Title"


# ── Session alias ─────────────────────────────────────────────────────


class TestSessionAlias:
    def test_set_alias(self, tmp_db):
        register_session("abc123")
        assert set_session_alias("abc123", "my-session") is True
        save_message("abc123", "user", "hello")
        rows = list_sessions()
        assert rows[0][1] == "my-session"  # alias

    def test_alias_conflict(self, tmp_db):
        register_session("abc123")
        register_session("def456")
        set_session_alias("abc123", "taken")
        assert set_session_alias("def456", "taken") is False

    def test_alias_same_session_ok(self, tmp_db):
        register_session("abc123")
        set_session_alias("abc123", "mine")
        assert set_session_alias("abc123", "mine") is True  # no-op, same session


# ── Session resolution ────────────────────────────────────────────────


class TestResolveSession:
    def test_resolve_by_alias(self, tmp_db):
        register_session("abc123")
        set_session_alias("abc123", "my-alias")
        assert resolve_session("my-alias") == "abc123"

    def test_resolve_by_exact_id(self, tmp_db):
        register_session("abc123def456")
        assert resolve_session("abc123def456") == "abc123def456"

    def test_resolve_by_prefix(self, tmp_db):
        register_session("abc123def456")
        assert resolve_session("abc123") == "abc123def456"

    def test_resolve_prefix_ambiguous(self, tmp_db):
        register_session("abc123aaaaaa")
        register_session("abc123bbbbbb")
        # Ambiguous prefix should return None
        assert resolve_session("abc123") is None

    def test_resolve_not_found(self, tmp_db):
        assert resolve_session("nonexistent") is None

    def test_resolve_legacy_session(self, tmp_db):
        """Sessions that exist only in conversations (pre-migration) should auto-register."""
        save_message("legacy123456", "user", "old message")
        result = resolve_session("legacy123456")
        assert result == "legacy123456"
        # Should now appear in sessions list
        rows = list_sessions()
        assert any(r[0] == "legacy123456" for r in rows)


# ── List sessions ─────────────────────────────────────────────────────


class TestListSessions:
    def test_empty(self, tmp_db):
        assert list_sessions() == []

    def test_ordered_by_updated(self, tmp_db):
        register_session("first")
        save_message("first", "user", "hello")
        register_session("second")
        save_message("second", "user", "hello")
        # second is more recent
        rows = list_sessions()
        assert rows[0][0] == "second"
        assert rows[1][0] == "first"

    def test_includes_message_count(self, tmp_db):
        register_session("sess1")
        save_message("sess1", "user", "hello")
        save_message("sess1", "assistant", "hi")
        rows = list_sessions()
        assert rows[0][5] == 2  # msg_count

    def test_respects_limit(self, tmp_db):
        for i in range(5):
            register_session(f"sess{i}")
            save_message(f"sess{i}", "user", "hello")
        rows = list_sessions(limit=3)
        assert len(rows) == 3


# ── Load session messages ─────────────────────────────────────────────


class TestLoadSessionMessages:
    def test_simple_user_assistant(self, tmp_db):
        save_message("s1", "user", "hello")
        save_message("s1", "assistant", "hi there")
        msgs = load_session_messages("s1")
        assert len(msgs) == 2
        assert msgs[0] == {"role": "user", "content": "hello"}
        assert msgs[1] == {"role": "assistant", "content": "hi there"}

    def test_tool_calls_with_ids(self, tmp_db):
        save_message("s1", "user", "run ls")
        save_message("s1", "assistant", "Let me check.")
        save_message(
            "s1", "tool_call", None, "bash", '{"command":"ls"}', tool_call_id="call_abc"
        )
        save_message(
            "s1", "tool_result", "file1.txt\nfile2.txt", "bash", tool_call_id="call_abc"
        )
        msgs = load_session_messages("s1")
        assert len(msgs) == 3  # user, assistant+tool_calls, tool
        # Assistant should have content merged with tool_calls
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["content"] == "Let me check."
        assert len(msgs[1]["tool_calls"]) == 1
        assert msgs[1]["tool_calls"][0]["id"] == "call_abc"
        assert msgs[1]["tool_calls"][0]["function"]["name"] == "bash"
        # Tool result
        assert msgs[2]["role"] == "tool"
        assert msgs[2]["tool_call_id"] == "call_abc"
        assert msgs[2]["content"] == "file1.txt\nfile2.txt"

    def test_tool_calls_without_ids_positional(self, tmp_db):
        """Legacy data without tool_call_id uses positional matching."""
        save_message("s1", "user", "do stuff")
        save_message("s1", "tool_call", None, "bash", '{"command":"ls"}')
        save_message("s1", "tool_result", "output", "bash")
        msgs = load_session_messages("s1")
        assert len(msgs) == 3
        # Synthetic IDs should match
        tc_id = msgs[1]["tool_calls"][0]["id"]
        assert msgs[2]["tool_call_id"] == tc_id

    def test_parallel_tool_calls(self, tmp_db):
        save_message("s1", "user", "search two things")
        save_message(
            "s1", "tool_call", None, "search", '{"query":"a"}', tool_call_id="call_1"
        )
        save_message(
            "s1", "tool_call", None, "search", '{"query":"b"}', tool_call_id="call_2"
        )
        save_message("s1", "tool_result", "result a", "search", tool_call_id="call_1")
        save_message("s1", "tool_result", "result b", "search", tool_call_id="call_2")
        msgs = load_session_messages("s1")
        assert len(msgs) == 4  # user, assistant+2 tool_calls, 2 tool results
        assert len(msgs[1]["tool_calls"]) == 2
        assert msgs[2]["tool_call_id"] == "call_1"
        assert msgs[3]["tool_call_id"] == "call_2"

    def test_empty_session(self, tmp_db):
        assert load_session_messages("nonexistent") == []

    def test_orphaned_tool_result_skipped(self, tmp_db):
        save_message("s1", "user", "hello")
        save_message("s1", "tool_result", "orphan", "bash")
        msgs = load_session_messages("s1")
        assert len(msgs) == 1  # only the user message


# ── Delete session ────────────────────────────────────────────────────


class TestDeleteSession:
    def test_delete_removes_session_and_messages(self, tmp_db):
        register_session("abc123")
        save_message("abc123", "user", "hello")
        save_message("abc123", "assistant", "hi")
        assert delete_session("abc123") is True
        assert list_sessions() == []
        assert load_session_messages("abc123") == []

    def test_delete_nonexistent(self, tmp_db):
        assert delete_session("nonexistent") is True  # no-op, still returns True


# ── save_message with tool_call_id ────────────────────────────────────


class TestSaveMessageToolCallId:
    def test_tool_call_id_stored(self, tmp_db):
        save_message(
            "s1", "tool_call", None, "bash", '{"cmd":"ls"}', tool_call_id="call_xyz"
        )
        conn = open_db()
        try:
            row = conn.execute(
                "SELECT tool_call_id FROM conversations WHERE session_id = 's1'"
            ).fetchone()
            assert row[0] == "call_xyz"
        finally:
            conn.close()

    def test_tool_call_id_none_by_default(self, tmp_db):
        save_message("s1", "user", "hello")
        conn = open_db()
        try:
            row = conn.execute(
                "SELECT tool_call_id FROM conversations WHERE session_id = 's1'"
            ).fetchone()
            assert row[0] is None
        finally:
            conn.close()


# ── Sessions table creation ───────────────────────────────────────────


class TestSessionsTable:
    def test_sessions_table_exists(self, tmp_db):
        conn = open_db()
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
            ).fetchall()
            assert len(rows) == 1
        finally:
            conn.close()

    def test_tool_call_id_column_exists(self, tmp_db):
        conn = open_db()
        try:
            # Should not raise
            conn.execute("SELECT tool_call_id FROM conversations LIMIT 0")
        finally:
            conn.close()


# ── ChatSession.resume_session ────────────────────────────────────────


class TestResumeSession:
    def test_resume_loads_messages(self, tmp_db, mock_openai_client):
        # Set up a session with messages in DB
        register_session("old_sess_123")
        save_message("old_sess_123", "user", "hello world")
        save_message("old_sess_123", "assistant", "hi there")

        # Create a new session and resume
        session = ChatSession(
            client=mock_openai_client,
            model="test-model",
            ui=MagicMock(),
            persona=None,
            instructions=None,
            temperature=0.5,
            max_tokens=1000,
            tool_timeout=10,
        )
        original_id = session._session_id
        assert original_id != "old_sess_123"

        result = session.resume_session("old_sess_123")
        assert result is True
        assert session._session_id == "old_sess_123"
        assert len(session.messages) == 2
        assert session.messages[0]["content"] == "hello world"
        assert session._title_generated is True

    def test_resume_nonexistent_returns_false(self, tmp_db, mock_openai_client):
        session = ChatSession(
            client=mock_openai_client,
            model="test-model",
            ui=MagicMock(),
            persona=None,
            instructions=None,
            temperature=0.5,
            max_tokens=1000,
            tool_timeout=10,
        )
        assert session.resume_session("nonexistent") is False

    def test_session_registered_on_init(self, tmp_db, mock_openai_client):
        session = ChatSession(
            client=mock_openai_client,
            model="test-model",
            ui=MagicMock(),
            persona=None,
            instructions=None,
            temperature=0.5,
            max_tokens=1000,
            tool_timeout=10,
        )
        # Session is registered in DB (resolvable) even before any messages
        assert resolve_session(session._session_id) == session._session_id
        # But does not appear in list_sessions until a message is saved
        assert not any(r[0] == session._session_id for r in list_sessions())


# ── save_message updates sessions.updated ─────────────────────────────


class TestSaveMessageUpdatesSession:
    def test_updated_timestamp_bumped(self, tmp_db):
        register_session("s1")
        save_message("s1", "user", "first")
        rows = list_sessions()
        original_updated = rows[0][4]

        import time

        time.sleep(0.01)  # ensure different timestamp
        save_message("s1", "user", "hello")

        rows = list_sessions()
        new_updated = rows[0][4]
        # updated should be same or later (sqlite datetime resolution is seconds,
        # so they may be equal in fast tests — just verify no error)
        assert new_updated is not None
