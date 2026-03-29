"""Tests for conversation rewind and retry functionality."""

from __future__ import annotations

from unittest.mock import MagicMock

from turnstone.core.session import ChatSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class NullUI:
    """UI adapter that discards all output."""

    def on_thinking_start(self):
        pass

    def on_thinking_stop(self):
        pass

    def on_reasoning_token(self, text):
        pass

    def on_content_token(self, text):
        pass

    def on_stream_end(self):
        pass

    def approve_tools(self, items):
        return True, None

    def on_tool_result(self, call_id, name, output, **kwargs):
        pass

    def on_tool_output_chunk(self, call_id, chunk):
        pass

    def on_status(self, usage, context_window, effort):
        pass

    def on_plan_review(self, content):
        return ""

    def on_info(self, message):
        pass

    def on_error(self, message):
        pass

    def on_state_change(self, state):
        pass

    def on_rename(self, name):
        pass

    def on_output_warning(self, call_id, assessment):
        pass


def _make_session(tmp_db) -> ChatSession:
    return ChatSession(
        client=MagicMock(),
        model="test-model",
        ui=NullUI(),
        instructions="",
        temperature=0.5,
        max_tokens=4096,
        tool_timeout=30,
    )


def _populate_simple(session: ChatSession) -> None:
    """Populate with 2 simple turns (no tool calls)."""
    session.messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "user", "content": "How are you?"},
        {"role": "assistant", "content": "I'm fine."},
    ]
    session._msg_tokens = [10, 20, 10, 20]


def _populate_with_tools(session: ChatSession) -> None:
    """Populate with 2 turns, first has tool calls."""
    session.messages = [
        {"role": "user", "content": "Write a test"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "tc1", "function": {"name": "bash", "arguments": '{"cmd":"echo hi"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "tc1", "content": "hi"},
        {"role": "assistant", "content": "Done."},
        {"role": "user", "content": "Fix the import"},
        {"role": "assistant", "content": "Fixed."},
    ]
    session._msg_tokens = [10, 20, 10, 20, 10, 20]


# ---------------------------------------------------------------------------
# _find_turn_boundaries
# ---------------------------------------------------------------------------


class TestFindTurnBoundaries:
    def test_empty_messages(self, tmp_db):
        session = _make_session(tmp_db)
        assert session._find_turn_boundaries() == []

    def test_single_turn(self, tmp_db):
        session = _make_session(tmp_db)
        session.messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
        ]
        assert session._find_turn_boundaries() == [0]

    def test_multi_turn(self, tmp_db):
        session = _make_session(tmp_db)
        _populate_simple(session)
        assert session._find_turn_boundaries() == [0, 2]

    def test_with_tool_calls(self, tmp_db):
        session = _make_session(tmp_db)
        _populate_with_tools(session)
        assert session._find_turn_boundaries() == [0, 4]


# ---------------------------------------------------------------------------
# rewind
# ---------------------------------------------------------------------------


class TestRewind:
    def test_rewind_zero(self, tmp_db):
        session = _make_session(tmp_db)
        _populate_simple(session)
        assert session.rewind(0) == 0
        assert len(session.messages) == 4

    def test_rewind_one_turn(self, tmp_db):
        session = _make_session(tmp_db)
        _populate_simple(session)
        removed = session.rewind(1)
        assert removed == 2  # user + assistant
        assert len(session.messages) == 2
        assert session.messages[0]["content"] == "Hello"
        assert session.messages[1]["content"] == "Hi there!"
        assert len(session._msg_tokens) == 2

    def test_rewind_all_turns(self, tmp_db):
        session = _make_session(tmp_db)
        _populate_simple(session)
        removed = session.rewind(2)
        assert removed == 4
        assert len(session.messages) == 0
        assert len(session._msg_tokens) == 0

    def test_rewind_clamped(self, tmp_db):
        """Rewinding more turns than exist should clamp to available."""
        session = _make_session(tmp_db)
        _populate_simple(session)
        removed = session.rewind(999)
        assert removed == 4
        assert len(session.messages) == 0

    def test_rewind_empty(self, tmp_db):
        session = _make_session(tmp_db)
        assert session.rewind(1) == 0

    def test_rewind_with_tools(self, tmp_db):
        """Rewinding 1 turn on a multi-sub-turn conversation."""
        session = _make_session(tmp_db)
        _populate_with_tools(session)
        removed = session.rewind(1)
        assert removed == 2  # user "Fix the import" + assistant "Fixed."
        assert len(session.messages) == 4
        assert session.messages[-1]["content"] == "Done."

    def test_rewind_tokens_sync(self, tmp_db):
        """_msg_tokens stays in sync with messages."""
        session = _make_session(tmp_db)
        _populate_simple(session)
        session.rewind(1)
        assert len(session._msg_tokens) == len(session.messages)


# ---------------------------------------------------------------------------
# retry
# ---------------------------------------------------------------------------


class TestRetry:
    def test_retry_returns_user_message(self, tmp_db):
        session = _make_session(tmp_db)
        _populate_simple(session)
        msg = session.retry()
        assert msg == "How are you?"
        # Only Turn 1 remains, without the second user message
        assert len(session.messages) == 2
        assert session.messages[-1]["content"] == "Hi there!"

    def test_retry_empty(self, tmp_db):
        session = _make_session(tmp_db)
        assert session.retry() is None

    def test_retry_with_tools(self, tmp_db):
        session = _make_session(tmp_db)
        _populate_with_tools(session)
        msg = session.retry()
        assert msg == "Fix the import"
        # Only Turn 1 remains (user + assistant w/tools + tool result + assistant)
        assert len(session.messages) == 4

    def test_retry_sets_pending(self, tmp_db):
        """handle_command for /retry should set _pending_retry."""
        session = _make_session(tmp_db)
        _populate_simple(session)
        session.handle_command("/retry")
        assert session._pending_retry == "How are you?"

    def test_retry_tokens_sync(self, tmp_db):
        session = _make_session(tmp_db)
        _populate_simple(session)
        session.retry()
        assert len(session._msg_tokens) == len(session.messages)

    def test_retry_multipart_content_returns_none(self, tmp_db):
        """retry() should refuse multipart (vision/image) messages."""
        session = _make_session(tmp_db)
        session.messages = [
            {"role": "user", "content": [{"type": "text", "text": "describe this"}]},
            {"role": "assistant", "content": "It's an image."},
        ]
        session._msg_tokens = [10, 20]
        assert session.retry() is None
        # Messages should be unchanged
        assert len(session.messages) == 2

    def test_retry_none_content_returns_none(self, tmp_db):
        """retry() should handle content=None gracefully."""
        session = _make_session(tmp_db)
        session.messages = [
            {"role": "user", "content": None},
            {"role": "assistant", "content": "Ok."},
        ]
        session._msg_tokens = [10, 20]
        assert session.retry() is None


# ---------------------------------------------------------------------------
# handle_command integration
# ---------------------------------------------------------------------------


class TestHandleCommand:
    def test_rewind_command(self, tmp_db):
        session = _make_session(tmp_db)
        _populate_simple(session)
        session.handle_command("/rewind 1")
        assert len(session.messages) == 2

    def test_rewind_no_arg(self, tmp_db):
        session = _make_session(tmp_db)
        ui = session.ui
        ui.on_info = MagicMock()
        session.handle_command("/rewind")
        ui.on_info.assert_called_once()
        assert "Usage" in ui.on_info.call_args[0][0]

    def test_rewind_invalid_arg(self, tmp_db):
        session = _make_session(tmp_db)
        ui = session.ui
        ui.on_info = MagicMock()
        session.handle_command("/rewind abc")
        ui.on_info.assert_called_once()
        assert "integer" in ui.on_info.call_args[0][0]

    def test_retry_nothing_to_retry(self, tmp_db):
        session = _make_session(tmp_db)
        ui = session.ui
        ui.on_info = MagicMock()
        session.handle_command("/retry")
        ui.on_info.assert_called_once()
        assert "Nothing" in ui.on_info.call_args[0][0]


# ---------------------------------------------------------------------------
# Storage integration — delete_messages_after
# ---------------------------------------------------------------------------


class TestDeleteMessagesAfter:
    def test_delete_truncates_db(self, tmp_db):
        from turnstone.core.memory import (
            delete_messages_after,
            load_messages,
            register_workstream,
            save_message,
        )

        ws_id = "test-ws-delete"
        register_workstream(ws_id)
        save_message(ws_id, "user", "Hello")
        save_message(ws_id, "assistant", "Hi!")
        save_message(ws_id, "user", "Bye")
        save_message(ws_id, "assistant", "Goodbye!")

        deleted = delete_messages_after(ws_id, 2)
        assert deleted == 2

        msgs = load_messages(ws_id)
        assert len(msgs) == 2
        assert msgs[0]["content"] == "Hello"
        assert msgs[1]["content"] == "Hi!"

    def test_delete_nothing(self, tmp_db):
        from turnstone.core.memory import (
            delete_messages_after,
            register_workstream,
            save_message,
        )

        ws_id = "test-ws-noop"
        register_workstream(ws_id)
        save_message(ws_id, "user", "Hello")

        deleted = delete_messages_after(ws_id, 10)
        assert deleted == 0

    def test_delete_all(self, tmp_db):
        from turnstone.core.memory import (
            delete_messages_after,
            load_messages,
            register_workstream,
            save_message,
        )

        ws_id = "test-ws-all"
        register_workstream(ws_id)
        save_message(ws_id, "user", "Hello")
        save_message(ws_id, "assistant", "Hi!")

        deleted = delete_messages_after(ws_id, 0)
        assert deleted == 2
        assert load_messages(ws_id) == []


# ---------------------------------------------------------------------------
# End-to-end: rewind + DB sync
# ---------------------------------------------------------------------------


class TestRewindDBSync:
    def test_rewind_persists_to_db(self, tmp_db):
        from turnstone.core.memory import load_messages, register_workstream, save_message

        session = _make_session(tmp_db)
        ws_id = session.ws_id
        register_workstream(ws_id)

        # Persist messages to DB and set in-memory state
        save_message(ws_id, "user", "Hello")
        save_message(ws_id, "assistant", "Hi!")
        save_message(ws_id, "user", "Bye")
        save_message(ws_id, "assistant", "Goodbye!")

        session.messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
            {"role": "user", "content": "Bye"},
            {"role": "assistant", "content": "Goodbye!"},
        ]
        session._msg_tokens = [5, 5, 5, 5]

        session.rewind(1)

        # Verify DB matches in-memory state
        db_msgs = load_messages(ws_id)
        assert len(db_msgs) == 2
        assert db_msgs[0]["content"] == "Hello"
        assert db_msgs[1]["content"] == "Hi!"
