"""Tests for generation cancellation (cooperative cancel via threading.Event)."""

import contextlib
import threading
import time
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from turnstone.core.session import ChatSession, GenerationCancelled, _CancelRef


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

    def on_tool_result(self, call_id, name, output, **kwargs):
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

    def on_output_warning(self, call_id, assessment):
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
        """When cancelled during tool execution, synthesized results replace missing tool outputs."""
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
        # Cancelled tool calls should have synthesized results
        tool_msgs = [m for m in session.messages if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "tc_1"
        assert "Cancelled by user" in tool_msgs[0]["content"]
        assert tool_msgs[0].get("is_error") is True
        # The assistant message with tool_calls should still be present
        assistant_msgs = [m for m in session.messages if m.get("tool_calls")]
        assert len(assistant_msgs) == 1


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


class TestStreamFlushBeforeToolCalls:
    """Content pending buffer must be flushed before tool call processing."""

    def test_pending_content_flushed_before_tool_calls(self, tmp_db):
        """All content tokens arrive via on_content_token before tool calls."""
        events: list[tuple[str, ...]] = []

        class TrackingUI(NullUI):
            def on_content_token(self, text):
                events.append(("content", text))

            def on_stream_end(self):
                events.append(("stream_end",))
                super().on_stream_end()

        ui = TrackingUI()
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

        def stream_content_then_tool():
            # Content long enough to leave chars in pending buffer
            # (_MAX_TAG_LEN = 13, so _drain_pending retains last 13 chars)
            yield FakeChunk(content_delta="Hello world, this is a test message")
            yield FakeChunk(
                tool_call_deltas=[FakeToolDelta(index=0, id="tc_1", name="bash")],
            )
            yield FakeChunk(
                tool_call_deltas=[FakeToolDelta(index=0, arguments_delta='{"command":"echo hi"}')],
                finish_reason="tool_calls",
            )

        with (
            patch.object(
                session,
                "_create_stream_with_retry",
                return_value=stream_content_then_tool(),
            ),
            patch.object(session, "_full_messages", return_value=[]),
            # Prevent real tool execution (e.g., bash) during this test.
            patch.object(session, "_execute_tools", return_value=([], None)),
        ):
            session.send("test")

        # All content should have been emitted
        total = "".join(e[1] for e in events if e[0] == "content")
        assert total == "Hello world, this is a test message"

        # No content events after stream_end
        stream_end_idx = next(i for i, e in enumerate(events) if e[0] == "stream_end")
        late_content = [e for e in events[stream_end_idx + 1 :] if e[0] == "content"]
        assert late_content == [], f"Content after stream_end: {late_content}"


class TestStreamAbort:
    """Tests for cancel() closing the underlying SDK stream."""

    def test_cancel_closes_cancel_stream(self, tmp_db):
        """cancel() calls .close() on the stored SDK stream handle."""
        session = _make_session()
        mock_stream = MagicMock()
        session._cancel_stream = mock_stream
        session.cancel()
        mock_stream.close.assert_called_once()
        assert session._cancel_event.is_set()

    def test_cancel_without_stream_is_safe(self, tmp_db):
        """cancel() with no active stream just sets the event."""
        session = _make_session()
        assert session._cancel_stream is None
        session.cancel()  # Should not raise
        assert session._cancel_event.is_set()

    def test_cancel_stream_close_error_suppressed(self, tmp_db):
        """Errors from stream.close() are suppressed."""
        session = _make_session()
        mock_stream = MagicMock()
        mock_stream.close.side_effect = RuntimeError("already closed")
        session._cancel_stream = mock_stream
        session.cancel()  # Should not raise
        assert session._cancel_event.is_set()

    def test_cancel_ref_populated_after_first_chunk(self, tmp_db):
        """_cancel_ref is populated by the provider after the first chunk
        arrives (lazy generator evaluation)."""
        ui = NullUI()
        session = _make_session(ui=ui)

        @dataclass
        class FakeChunk:
            content_delta: str = ""
            reasoning_delta: str = ""
            tool_call_deltas: list = field(default_factory=list)
            usage: None = None
            finish_reason: str = "stop"
            info_delta: str = ""
            provider_blocks: list = field(default_factory=list)

        sdk_stream = MagicMock()

        def fake_provider_stream():
            # Simulate provider appending to cancel_ref before first yield
            session._cancel_ref.append(sdk_stream)
            yield FakeChunk(content_delta="hi", finish_reason="stop")

        with (
            patch.object(
                session,
                "_create_stream_with_retry",
                return_value=fake_provider_stream(),
            ),
            patch.object(session, "_full_messages", return_value=[]),
        ):
            session.send("test")

        # After stream completes, cancel_stream should be cleared
        assert session._cancel_stream is None
        assert len(session._cancel_ref) == 0

    def test_transport_error_during_cancel_becomes_generation_cancelled(self, tmp_db):
        """When cancel() closes the stream, the resulting transport error
        is converted to GenerationCancelled."""
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

        def stream_that_errors():
            yield FakeChunk(content_delta="Hello")
            session._cancel_event.set()
            raise ConnectionError("stream closed")

        with (
            patch.object(
                session,
                "_create_stream_with_retry",
                return_value=stream_that_errors(),
            ),
            patch.object(session, "_full_messages", return_value=[]),
        ):
            session.send("test")

        # Should complete as cancelled, not error
        assert "idle" in ui.states
        assert any("cancelled" in i.lower() for i in ui.infos)
        # Partial content preserved
        assistant_msgs = [m for m in session.messages if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0]["content"] == "Hello"

    def test_non_cancel_exception_not_swallowed(self, tmp_db):
        """Exceptions during streaming that aren't caused by cancel
        should propagate normally."""
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

        def stream_that_errors():
            yield FakeChunk(content_delta="Hello")
            raise ValueError("unexpected error")

        with (
            patch.object(
                session,
                "_create_stream_with_retry",
                return_value=stream_that_errors(),
            ),
            patch.object(session, "_full_messages", return_value=[]),
            pytest.raises(ValueError, match="unexpected error"),
        ):
            session.send("test")

    def test_check_cancelled_between_retries(self, tmp_db):
        """_try_stream checks for cancellation between retry attempts."""
        session = _make_session()
        session.cancel()

        with pytest.raises(GenerationCancelled):
            session._try_stream(
                client=MagicMock(),
                model="test",
                msgs=[],
            )


class TestCancelRef:
    """Tests for the _CancelRef list proxy."""

    def test_append_sets_cancel_stream(self, tmp_db):
        """Appending a stream handle to _CancelRef sets _cancel_stream eagerly."""
        session = _make_session()
        mock_stream = MagicMock()
        assert session._cancel_stream is None

        session._cancel_ref.append(mock_stream)

        assert session._cancel_stream is mock_stream

    def test_append_closes_stream_when_already_cancelled(self, tmp_db):
        """If cancel is already set when a stream is appended, it is closed immediately."""
        session = _make_session()
        session.cancel()  # Set cancel event before stream is created

        mock_stream = MagicMock()
        session._cancel_ref.append(mock_stream)

        mock_stream.close.assert_called_once()

    def test_append_does_not_close_stream_when_not_cancelled(self, tmp_db):
        """Stream is not closed if cancel hasn't been requested."""
        session = _make_session()
        mock_stream = MagicMock()

        session._cancel_ref.append(mock_stream)

        mock_stream.close.assert_not_called()
        assert session._cancel_stream is mock_stream

    def test_append_close_error_suppressed(self, tmp_db):
        """Errors from stream.close() during eager close are suppressed."""
        session = _make_session()
        session.cancel()

        mock_stream = MagicMock()
        mock_stream.close.side_effect = RuntimeError("already closed")

        session._cancel_ref.append(mock_stream)  # Should not raise

    def test_cancel_ref_is_cancel_ref_instance(self, tmp_db):
        """ChatSession._cancel_ref is a _CancelRef instance."""
        session = _make_session()
        assert isinstance(session._cancel_ref, _CancelRef)

    def test_cancel_ref_cleared_after_stream_ends(self, tmp_db):
        """_cancel_ref is cleared in the send() finally block after streaming."""
        ui = NullUI()
        session = _make_session(ui=ui)
        mock_stream = MagicMock()
        session._cancel_ref.append(mock_stream)
        assert len(session._cancel_ref) == 1

        @dataclass
        class FakeChunk:
            content_delta: str = ""
            reasoning_delta: str = ""
            tool_call_deltas: list = field(default_factory=list)
            usage: None = None
            finish_reason: str = "stop"
            info_delta: str = ""
            provider_blocks: list = field(default_factory=list)

        with (
            patch.object(
                session,
                "_create_stream_with_retry",
                return_value=iter([FakeChunk(content_delta="hi", finish_reason="stop")]),
            ),
            patch.object(session, "_full_messages", return_value=[]),
        ):
            session.send("test")

        # After send() completes, _cancel_ref is cleared in the finally block
        assert len(session._cancel_ref) == 0


class TestForceCancelGeneration:
    """Tests for per-generation tracking that prevents orphaned-thread side-effects."""

    def test_check_cancelled_raises_for_orphaned_generation(self, tmp_db):
        """_check_cancelled raises GenerationCancelled when my_generation is stale."""
        session = _make_session()
        session._generation = 2  # Simulate two generations having run

        with pytest.raises(GenerationCancelled):
            session._check_cancelled(my_generation=1)  # Generation 1 is orphaned

    def test_check_cancelled_ok_for_current_generation(self, tmp_db):
        """_check_cancelled does not raise when my_generation matches current."""
        session = _make_session()
        session._generation = 3
        session._check_cancelled(my_generation=3)  # Should not raise

    def test_force_cancel_orphaned_thread_does_not_mutate_messages(self, tmp_db):
        """An abandoned generation (force-cancel) cannot append to session.messages."""
        ui = NullUI()
        session = _make_session(ui=ui)

        # We can't trivially test the full threading scenario in a unit test,
        # so directly verify that _check_cancelled raises when my_generation
        # is stale, which is what guards _stream_response against orphaned
        # (force-cancelled) threads continuing to mutate messages.
        session._generation = 5
        with pytest.raises(GenerationCancelled):
            session._check_cancelled(my_generation=4)  # orphaned generation

    def test_new_cancel_event_per_generation_in_send(self, tmp_db):
        """send() replaces _cancel_event with a fresh Event each generation."""
        ui = NullUI()
        session = _make_session(ui=ui)

        @dataclass
        class FakeChunk:
            content_delta: str = ""
            reasoning_delta: str = ""
            tool_call_deltas: list = field(default_factory=list)
            usage: None = None
            finish_reason: str = "stop"
            info_delta: str = ""
            provider_blocks: list = field(default_factory=list)

        original_event = session._cancel_event

        with (
            patch.object(
                session,
                "_create_stream_with_retry",
                return_value=iter([FakeChunk(content_delta="hi", finish_reason="stop")]),
            ),
            patch.object(session, "_full_messages", return_value=[]),
        ):
            session.send("test")

        # After send() completes, _cancel_event should be a NEW Event
        # (not the same object as before the call).
        assert session._cancel_event is not original_event
        assert not session._cancel_event.is_set()


class TestForceCancelThreaded:
    """Force cancel with actual threads — verifies orphaned thread behavior."""

    def test_force_cancel_orphan_does_not_mutate_messages(self, tmp_db):
        """After force cancel + new send(), the orphaned thread must not
        append stale content to session.messages."""
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
        old_done = threading.Event()

        def slow_stream():
            yield FakeChunk(content_delta="Old content")
            barrier.set()  # signal: first chunk delivered
            time.sleep(2)  # simulate stuck stream
            yield FakeChunk(content_delta=" more", finish_reason="stop")

        # Start generation 1 (will get stuck)
        with (
            patch.object(session, "_create_stream_with_retry", return_value=slow_stream()),
            patch.object(session, "_full_messages", return_value=[]),
        ):

            def run_old():
                with contextlib.suppress(Exception):
                    session.send("old message")
                old_done.set()

            t1 = threading.Thread(target=run_old, daemon=True)
            t1.start()
            assert barrier.wait(timeout=5), "stream did not start"

        # Force cancel: simulate what the server does
        session.cancel()
        # Increment generation as new send() would
        session._generation += 1
        session._cancel_event = threading.Event()

        # Wait for old thread to notice generation mismatch and exit
        assert old_done.wait(timeout=10), "orphaned thread did not exit"

        # The orphaned thread should NOT have appended its content
        assistant_msgs = [m for m in session.messages if m["role"] == "assistant"]
        # May have partial content from before cancel, but NOT the full
        # "Old content more" that would appear without the generation guard
        for msg in assistant_msgs:
            assert "more" not in msg.get("content", "")

    def test_force_cancel_then_new_send_succeeds(self, tmp_db):
        """A new send() after force cancel works cleanly."""
        ui = NullUI()
        session = _make_session(ui=ui)

        @dataclass
        class FakeChunk:
            content_delta: str = ""
            reasoning_delta: str = ""
            tool_call_deltas: list = field(default_factory=list)
            usage: None = None
            finish_reason: str = "stop"
            info_delta: str = ""
            provider_blocks: list = field(default_factory=list)

        barrier = threading.Event()

        def stuck_stream():
            yield FakeChunk(content_delta="stuck")
            barrier.set()
            time.sleep(2)
            yield FakeChunk(content_delta=" end", finish_reason="stop")

        # Start stuck generation
        with (
            patch.object(session, "_create_stream_with_retry", return_value=stuck_stream()),
            patch.object(session, "_full_messages", return_value=[]),
        ):
            t = threading.Thread(target=lambda: session.send("old"), daemon=True)
            t.start()
            assert barrier.wait(timeout=5), "stream did not start"

        # Force cancel
        session.cancel()

        # New generation should work
        fresh_stream = iter([FakeChunk(content_delta="Fresh response")])
        with (
            patch.object(session, "_create_stream_with_retry", return_value=fresh_stream),
            patch.object(session, "_full_messages", return_value=[]),
        ):
            session.send("new message")

        # The new generation should have completed successfully
        assert "idle" in ui.states
        assistant_msgs = [m for m in session.messages if m["role"] == "assistant"]
        assert any("Fresh response" in m.get("content", "") for m in assistant_msgs)
