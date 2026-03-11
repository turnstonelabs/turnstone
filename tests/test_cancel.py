"""Tests for generation cancellation (cooperative cancel via threading.Event)."""

import threading
import time
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from turnstone.core.session import ChatSession, GenerationCancelled


class NullUI:
    """UI adapter that records state changes and discards other output."""

    def __init__(self):
        self.states = []
        self.infos = []
        self.stream_ends = 0

    def on_thinking_start(self):
        pass

    def on_thinking_stop(self):
        pass

    def on_reasoning_token(self, text):
        pass

    def on_content_token(self, text):
        pass

    def on_stream_end(self):
        self.stream_ends += 1

    def approve_tools(self, items):
        return True, None

    def on_tool_result(self, call_id, name, output):
        pass

    def on_tool_output_chunk(self, call_id, chunk):
        pass

    def on_status(self, usage, context_window, effort):
        pass

    def on_plan_review(self, content):
        return ""

    def on_info(self, message):
        self.infos.append(message)

    def on_error(self, message):
        pass

    def on_state_change(self, state):
        self.states.append(state)

    def on_rename(self, name):
        pass


def _make_session(ui=None, **kwargs):
    """Helper to construct a ChatSession with minimal setup."""
    defaults = dict(
        client=MagicMock(),
        model="test-model",
        ui=ui or NullUI(),
        instructions=None,
        temperature=0.5,
        max_tokens=4096,
        tool_timeout=30,
    )
    defaults.update(kwargs)
    return ChatSession(**defaults)


class TestCancelEvent:
    """Basic cancel event mechanics."""

    def test_cancel_sets_event(self, tmp_db):
        session = _make_session()
        assert not session._cancel_event.is_set()
        session.cancel()
        assert session._cancel_event.is_set()

    def test_check_cancelled_raises_when_set(self, tmp_db):
        session = _make_session()
        session.cancel()
        with pytest.raises(GenerationCancelled):
            session._check_cancelled()

    def test_check_cancelled_noop_when_clear(self, tmp_db):
        session = _make_session()
        session._check_cancelled()  # Should not raise

    def test_cancel_is_idempotent(self, tmp_db):
        session = _make_session()
        session.cancel()
        session.cancel()  # Double call is harmless
        assert session._cancel_event.is_set()

    def test_cancel_event_cleared_on_send_start(self, tmp_db):
        """send() clears a stale cancel flag before starting."""
        ui = NullUI()
        session = _make_session(ui=ui)
        session.cancel()  # Set stale flag

        @dataclass
        class FakeChunk:
            content_delta: str = ""
            reasoning_delta: str = ""
            tool_call_deltas: list = field(default_factory=list)
            usage: None = None
            finish_reason: str = "stop"
            info_delta: str = ""
            provider_blocks: list = field(default_factory=list)

        fake_stream = iter([FakeChunk(content_delta="Hello", finish_reason="stop")])

        with (
            patch.object(session, "_create_stream_with_retry", return_value=fake_stream),
            patch.object(session, "_full_messages", return_value=[]),
        ):
            session.send("test")

        # Should complete normally — cancel flag was cleared
        assert "idle" in ui.states


class TestCancelDuringStreaming:
    """Cancel while _stream_response is iterating chunks."""

    def test_preserves_partial_content(self, tmp_db):
        """Partial content already streamed should be preserved in messages."""
        ui = NullUI()
        session = _make_session(ui=ui)

        @dataclass
        class FakeChunk:
            content_delta: str = ""
            reasoning_delta: str = ""
            tool_call_deltas: list = field(default_factory=list)
            usage: None = None
            finish_reason: str = ""
            info_delta: str = ""
            provider_blocks: list = field(default_factory=list)

        def cancelling_stream():
            """Yield a few chunks then cancel."""
            yield FakeChunk(content_delta="Hello ")
            yield FakeChunk(content_delta="world")
            session.cancel()
            yield FakeChunk(content_delta=" — this should not appear")

        with (
            patch.object(session, "_create_stream_with_retry", return_value=cancelling_stream()),
            patch.object(session, "_full_messages", return_value=[]),
        ):
            session.send("test")

        # Session should be idle (not error)
        assert ui.states[-1] == "idle"
        # Check that "[Generation cancelled]" was emitted
        assert any("cancelled" in i.lower() for i in ui.infos)
        # The partial content should be preserved as an assistant message
        assistant_msgs = [m for m in session.messages if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0]["content"] == "Hello world"
        # No tool_calls in the partial message
        assert "tool_calls" not in assistant_msgs[0]


class TestCancelDuringToolExecution:
    """Cancel while tools are being executed."""

    def test_rollback_incomplete_tool_results(self, tmp_db):
        """When cancelled during tool execution, incomplete results are rolled back."""
        ui = NullUI()
        session = _make_session(ui=ui)

        @dataclass
        class FakeChunk:
            content_delta: str = ""
            reasoning_delta: str = ""
            tool_call_deltas: list = field(default_factory=list)
            usage: None = None
            finish_reason: str = ""
            info_delta: str = ""
            provider_blocks: list = field(default_factory=list)

        @dataclass
        class FakeToolDelta:
            index: int = 0
            id: str = ""
            name: str = ""
            arguments_delta: str = ""

        # First call: return content with a tool call
        def stream_with_tool():
            yield FakeChunk(
                tool_call_deltas=[FakeToolDelta(index=0, id="tc_1", name="bash")],
                finish_reason="",
            )
            yield FakeChunk(
                tool_call_deltas=[FakeToolDelta(index=0, arguments_delta='{"command":"echo hi"}')],
                finish_reason="tool_calls",
            )

        call_count = 0

        def fake_create_stream(msgs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return stream_with_tool()
            # Should not be called a second time since cancel happens before phase 3
            raise AssertionError("Should not stream again after cancel")

        def cancel_before_execute(tool_calls):
            """Simulate cancel happening before tool execution."""
            session.cancel()
            raise GenerationCancelled()

        with (
            patch.object(session, "_create_stream_with_retry", side_effect=fake_create_stream),
            patch.object(session, "_full_messages", return_value=[]),
            patch.object(session, "_execute_tools", side_effect=cancel_before_execute),
        ):
            session.send("run something")

        # Session should be idle
        assert ui.states[-1] == "idle"
        # No tool result messages should remain (rolled back)
        roles = [m["role"] for m in session.messages]
        assert "tool" not in roles
        # The assistant message with tool_calls should also be rolled back
        for m in session.messages:
            if m["role"] == "assistant":
                assert "tool_calls" not in m or not m["tool_calls"]


class TestCancelWhenIdle:
    """Cancelling when no generation is active is harmless."""

    def test_cancel_when_idle_is_noop(self, tmp_db):
        session = _make_session()
        session.cancel()
        # Next send should work normally (cancel cleared at start)

        @dataclass
        class FakeChunk:
            content_delta: str = ""
            reasoning_delta: str = ""
            tool_call_deltas: list = field(default_factory=list)
            usage: None = None
            finish_reason: str = "stop"
            info_delta: str = ""
            provider_blocks: list = field(default_factory=list)

        fake_stream = iter([FakeChunk(content_delta="ok", finish_reason="stop")])
        with (
            patch.object(session, "_create_stream_with_retry", return_value=fake_stream),
            patch.object(session, "_full_messages", return_value=[]),
        ):
            session.send("hello")

        # Should complete normally
        assistant_msgs = [m for m in session.messages if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0]["content"] == "ok"


class TestCancelThreadSafety:
    """Cancel from a different thread while generation is running."""

    def test_cancel_from_another_thread(self, tmp_db):
        ui = NullUI()
        session = _make_session(ui=ui)

        @dataclass
        class FakeChunk:
            content_delta: str = ""
            reasoning_delta: str = ""
            tool_call_deltas: list = field(default_factory=list)
            usage: None = None
            finish_reason: str = ""
            info_delta: str = ""
            provider_blocks: list = field(default_factory=list)

        barrier = threading.Event()

        def slow_stream():
            yield FakeChunk(content_delta="Start")
            barrier.set()  # Signal that streaming has started
            time.sleep(2)  # Simulate slow streaming
            yield FakeChunk(content_delta=" end", finish_reason="stop")

        with (
            patch.object(session, "_create_stream_with_retry", return_value=slow_stream()),
            patch.object(session, "_full_messages", return_value=[]),
        ):
            # Run send() in a thread
            error = []

            def run():
                try:
                    session.send("test")
                except Exception as e:
                    error.append(e)

            t = threading.Thread(target=run)
            t.start()
            barrier.wait(timeout=5)
            # Cancel from main thread
            session.cancel()
            t.join(timeout=5)

        assert not error
        assert ui.states[-1] == "idle"
        assert any("cancelled" in i.lower() for i in ui.infos)


class TestGenerationCancelledException:
    """GenerationCancelled is a BaseException, not Exception."""

    def test_is_base_exception(self):
        assert issubclass(GenerationCancelled, BaseException)

    def test_not_caught_by_except_exception(self):
        """Verify GenerationCancelled is NOT caught by except Exception."""
        with pytest.raises(GenerationCancelled):
            try:
                raise GenerationCancelled()
            except Exception:
                pytest.fail("GenerationCancelled was caught by except Exception")
