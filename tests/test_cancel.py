"""Tests for generation cancellation (cooperative cancel via threading.Event)."""

import contextlib
import json
import threading
import time
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from tests._session_helpers import arm_session, make_session
from turnstone.core.session import (
    GenerationCancelled,
    _CancelRef,
    _tool_turn_meta,
)
from turnstone.core.trajectory import (
    EffectStatus,
    Role,
    ToolCall,
    Turn,
    dicts_from_turns,
    turn_from_dict,
)


class NullUI:
    """UI adapter that records state changes and discards other output."""

    def __init__(self):
        self.states = []
        self.infos = []
        self.stream_ends = 0

    def on_turn_start(self):
        pass

    def on_turn_committed(self):
        pass

    def on_stream_discarded(self):
        pass

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

    def record_output_assessment(
        self,
        call_id,
        assessment,
        *,
        tier="heuristic",
        reasoning="",
        judge_model="",
        latency_ms=0,
        confidence=0.0,
    ):
        pass


def _make_session(ui=None, **kwargs):
    """Wrap the shared session factory; this suite defaults to its
    recording NullUI.  The defaults live in
    tests/_session_helpers.make_session — duplicating them here is
    exactly the drift its docstring warns about."""
    return make_session(ui=ui or NullUI(), **kwargs)


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

        arm_session(session, fake_stream)
        session.send("test")

        # Should complete normally — cancel flag was cleared
        assert "idle" in ui.states


class TestCancelDuringStreaming:
    """Cancel while the streaming consumer is observing chunks."""

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

        arm_session(session, cancelling_stream())
        session.send("test")

        # Session should be idle (not error)
        assert ui.states[-1] == "idle"
        # Check that "[Generation cancelled]" was emitted
        assert any("cancelled" in i.lower() for i in ui.infos)
        # The partial content should be preserved as an assistant
        # message AND annotated with a marker that downstream readers
        # (inspect_workstream, the next coord turn) can use to
        # distinguish a cancelled fragment from a completed turn — the
        # raw "Hello world" without a marker would look like the
        # final assistant answer to a coord LLM reading the child's
        # transcript.
        assistant_msgs = [m for m in dicts_from_turns(session.messages) if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        content = assistant_msgs[0]["content"]
        assert content.startswith("Hello world")
        assert "[generation cancelled before completion]" in content
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

        def cancel_before_execute(tool_calls):
            """Simulate cancel happening before tool execution."""
            session.cancel()
            raise GenerationCancelled()

        arm_session(session, stream_with_tool())
        with patch.object(session, "_execute_tools", side_effect=cancel_before_execute):
            session.send("run something")

        # The stream must not be re-created after the cancel landed.
        assert session._provider.create_streaming.call_count == 1

        # Session should be idle
        assert ui.states[-1] == "idle"
        # Cancelled tool calls should have synthesized results
        msgs = dicts_from_turns(session.messages)
        tool_msgs = [m for m in msgs if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "tc_1"
        assert "Cancelled by user" in tool_msgs[0]["content"]
        assert tool_msgs[0].get("is_error") is True
        # The assistant message with tool_calls should still be present
        assistant_msgs = [m for m in msgs if m.get("tool_calls")]
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
        arm_session(session, fake_stream)
        session.send("hello")

        # Should complete normally
        assistant_msgs = [m for m in dicts_from_turns(session.messages) if m["role"] == "assistant"]
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

        arm_session(session, slow_stream())
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
            # Content long enough to leave chars in the tag-scan carry
            # buffer (ThinkTagSplitter retains the last MAX_TAG_LEN = 12
            # chars until a flush)
            yield FakeChunk(content_delta="Hello world, this is a test message")
            yield FakeChunk(
                tool_call_deltas=[FakeToolDelta(index=0, id="tc_1", name="bash")],
            )
            yield FakeChunk(
                tool_call_deltas=[FakeToolDelta(index=0, arguments_delta='{"command":"echo hi"}')],
                finish_reason="tool_calls",
            )

        # Two scripted turns: the tool round-trip makes send() loop, and
        # the follow-up turn must carry a real finish reason — the strict
        # finish gate (RULED, #832) rejects an exhausted iterator that
        # used to commit as an empty turn.
        arm_session(
            session,
            stream_content_then_tool(),
            iter([FakeChunk(finish_reason="stop")]),
        )
        with (
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

    def test_stream_handle_registered_during_stream_cleared_after(self, tmp_db):
        """The per-attempt ref's eager append registers the SDK handle in
        ``_cancel_stream`` before the first chunk is consumed — the handle
        ``cancel()`` closes to unblock a stuck read — and send()'s finally
        clears it."""
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

        seen: dict = {}

        def observing_stream():
            # Runs at first next(): the armed handle must ALREADY be
            # registered (append happens inside create_streaming, before
            # the iterator is handed back).
            seen["handle_at_first_chunk"] = session._cancel_stream
            yield FakeChunk(content_delta="hi", finish_reason="stop")

        provider = arm_session(session, observing_stream())
        session.send("test")

        assert seen["handle_at_first_chunk"] is provider._armed_handle
        # After stream completes, cancel_stream should be cleared
        assert session._cancel_stream is None

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

        arm_session(session, stream_that_errors())
        session.send("test")

        # Should complete as cancelled, not error
        assert "idle" in ui.states
        assert any("cancelled" in i.lower() for i in ui.infos)
        # Partial content preserved AND annotated with the
        # cancelled-before-completion marker.
        assistant_msgs = [m for m in dicts_from_turns(session.messages) if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        content = assistant_msgs[0]["content"]
        assert content.startswith("Hello")
        assert "[generation cancelled before completion]" in content

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

        arm_session(session, stream_that_errors())
        with pytest.raises(ValueError, match="unexpected error"):
            session.send("test")

    def test_pre_set_cancel_no_mint_no_dispatch(self, tmp_db):
        """A Stop already set when the streaming phase is entered issues NO
        request and — on a dynamically authenticated alias — mints NO
        credential (the #972 pre-dispatch rule on the interactive path).
        A mint on a cache miss is a network round trip under the
        cluster-wide advisory lock, so an abandoned turn must not pay one:
        the resolver runs inside ``model_turn`` after its entry abort
        read, and the ladder's loop-top check precedes even the lane
        build."""
        session = _make_session()
        session._cancel_event.set()
        resolver = MagicMock(return_value=None)
        provider = arm_session(session, iter(()))

        with (
            patch.object(session, "_model_backend_auth_token", resolver),
            pytest.raises(GenerationCancelled),
        ):
            session._stream_response(0)

        resolver.assert_not_called()
        provider.create_streaming.assert_not_called()


class TestCancelRef:
    """Tests for the _CancelRef list proxy."""

    def test_append_sets_cancel_stream(self, tmp_db):
        """Appending a stream handle to _CancelRef sets _cancel_stream eagerly."""
        session = _make_session()
        mock_stream = MagicMock()
        assert session._cancel_stream is None

        _CancelRef(session).append(mock_stream)

        assert session._cancel_stream is mock_stream

    def test_append_closes_stream_when_already_cancelled(self, tmp_db):
        """If cancel is already set when a stream is appended, it is closed immediately."""
        session = _make_session()
        session.cancel()  # Set cancel event before stream is created

        mock_stream = MagicMock()
        _CancelRef(session).append(mock_stream)

        mock_stream.close.assert_called_once()

    def test_append_does_not_close_stream_when_not_cancelled(self, tmp_db):
        """Stream is not closed if cancel hasn't been requested."""
        session = _make_session()
        mock_stream = MagicMock()

        _CancelRef(session).append(mock_stream)

        mock_stream.close.assert_not_called()
        assert session._cancel_stream is mock_stream

    def test_append_close_error_suppressed(self, tmp_db):
        """Errors from stream.close() during eager close are suppressed."""
        session = _make_session()
        session.cancel()

        mock_stream = MagicMock()
        mock_stream.close.side_effect = RuntimeError("already closed")

        _CancelRef(session).append(mock_stream)  # Should not raise

    def test_no_shared_cancel_ref_attribute(self, tmp_db):
        """The long-lived shared ref is GONE (#832): every model-call site
        builds a fresh per-attempt, generation-scoped _CancelRef, so a
        force-cancelled generation's ref reads aborted via supersession.
        The pin holds the line against a gen-0 shared instance returning."""
        session = _make_session()
        assert not hasattr(session, "_cancel_ref")

    def test_cancel_ref_cleared_after_stream_ends(self, tmp_db):
        """send()'s finally clears _cancel_stream after streaming (the
        handle registered by the per-attempt ref's eager append must not
        linger into tool execution, where cancel() would close a dead
        handle instead of nothing)."""
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

        arm_session(session, iter([FakeChunk(content_delta="hi", finish_reason="stop")]))
        session.send("test")

        # During the stream the armed handle was registered; after send()
        # completes the finally block cleared it.
        assert session._cancel_stream is None


class TestOnFirstAppendHook:
    """``_CancelRef.on_first_append`` — the request-accepted observation
    point (#832): health success, the creation-vs-midstream classifier,
    and the usage-slot resets all key on it."""

    def test_fires_once_and_arms(self, tmp_db):
        session = _make_session()
        fired = []
        ref = _CancelRef(session, on_first_append=lambda: fired.append(1))
        assert not ref.armed
        ref.append(MagicMock())
        ref.append(MagicMock())
        assert fired == [1]
        assert ref.armed

    def test_superseded_append_neither_fires_nor_registers(self, tmp_db):
        """A superseded ref's zombie stream is closed on arrival and must
        neither fire the hook (an orphan must not record health or reset
        the successor's usage slots) nor hijack ``_cancel_stream`` from
        the successor generation's live stream."""
        session = _make_session()
        session._generation = 5
        fired = []
        ref = _CancelRef(session, 4, on_first_append=lambda: fired.append(1))
        zombie = MagicMock()
        ref.append(zombie)
        assert fired == []
        assert not ref.armed
        zombie.close.assert_called_once()
        assert session._cancel_stream is None


class TestForceCancelOrphanNoReissue:
    """A force-cancelled generation's mid-stream death is never re-issued
    on the orphan's behalf.  A gen-0 ref would read ``aborted`` False
    after a force-cancel (the successor installs a fresh unset event and
    gen 0 never reads superseded), and the retry machinery would spend
    tokens for a generation nobody owns."""

    def test_orphan_death_not_reissued_no_ui_finalize(self, tmp_db):
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

        def dying_orphan_stream():
            yield FakeChunk(content_delta="old ")
            # Force-cancel: a successor claims the generation (bumped
            # counter + fresh UNSET event) while this stream is mid-body.
            session._claim_generation()
            raise ConnectionError("connection reset")

        provider = arm_session(session, dying_orphan_stream())
        my_generation = session._claim_generation()
        ends_before = ui.stream_ends

        with pytest.raises(GenerationCancelled):
            session._stream_response(my_generation)

        # ONE dispatch — the death was not re-issued for the orphan.
        assert provider.create_streaming.call_count == 1
        # The orphan touched no UI finalize (a stream_end here would
        # clobber the successor generation's in-flight stream state).
        assert ui.stream_ends == ends_before


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
        # is stale — the per-chunk check the streaming consumer runs, which
        # guards orphaned (force-cancelled) threads out of mutating messages.
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

        arm_session(session, iter([FakeChunk(content_delta="hi", finish_reason="stop")]))
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
        arm_session(session, slow_stream())
        if True:

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
        assistant_msgs = [m for m in dicts_from_turns(session.messages) if m["role"] == "assistant"]
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
        arm_session(session, stuck_stream())
        t = threading.Thread(target=lambda: session.send("old"), daemon=True)
        t.start()
        assert barrier.wait(timeout=5), "stream did not start"

        # Force cancel
        session.cancel()

        # New generation should work
        arm_session(session, iter([FakeChunk(content_delta="Fresh response")]))
        session.send("new message")

        # The new generation should have completed successfully
        assert "idle" in ui.states
        assistant_msgs = [m for m in dicts_from_turns(session.messages) if m["role"] == "assistant"]
        assert any("Fresh response" in m.get("content", "") for m in assistant_msgs)


class TestSynthesizeCancelledResults:
    """Regression coverage for ``_synthesize_cancelled_results`` — must
    fire ``on_tool_result`` for each synthesized cancellation so live
    SSE listeners (e.g. coord's ``--running`` indicator added by
    tool_info) can complete the in-DOM tool batch. Without this, the
    coord JS would spin the running indicator forever on cancelled
    batches because ``state_change`` doesn't strip ``--running`` from
    individual batches."""

    def _ui_with_tool_result_tracking(self):
        class _TrackingUI(NullUI):
            def __init__(self) -> None:
                super().__init__()
                self.tool_results: list[tuple[str, str, str, bool]] = []

            def on_tool_result(self, call_id, name, output, **kwargs):
                self.tool_results.append(
                    (call_id, name, output, bool(kwargs.get("is_error", False))),
                )

        return _TrackingUI()

    def test_synthesizes_tool_result_for_unanswered_calls(self, tmp_db):
        ui = self._ui_with_tool_result_tracking()
        session = _make_session(ui=ui)
        session.messages.append(
            turn_from_dict(
                {
                    "role": "assistant",
                    "content": "calling tools",
                    "tool_calls": [
                        {"id": "call_a", "function": {"name": "search", "arguments": "{}"}},
                        {"id": "call_b", "function": {"name": "compute", "arguments": "{}"}},
                    ],
                },
            )
        )
        session._msg_tokens.append(1)

        session._synthesize_cancelled_results("Cancelled by user.")

        # Both unanswered calls fired ``on_tool_result``.
        assert len(ui.tool_results) == 2
        ids = {tr[0] for tr in ui.tool_results}
        assert ids == {"call_a", "call_b"}
        # All emitted as errors so the live UI renders them as
        # ``coord-tool-row-result--error``.
        assert all(tr[3] is True for tr in ui.tool_results)
        # Reason text propagates as a prefix, now followed by an explicit
        # UNKNOWN-outcome clause (unknown, never none — see HYPOTHESIS.md):
        # the call may have begun executing before cancel, so the synthetic
        # result must not read as "it didn't happen."
        assert all(tr[2].startswith("Cancelled by user.") for tr in ui.tool_results)
        assert all("UNKNOWN" in tr[2] for tr in ui.tool_results)
        # And the message list has the synthesized tool entries
        # (preserves the prior contract).
        tool_msgs = [m for m in dicts_from_turns(session.messages) if m.get("role") == "tool"]
        assert len(tool_msgs) == 2
        # Typed twin of the prose (Thread A): each synthesized turn is UNKNOWN.
        tool_turns = [m for m in session.messages if m.role is Role.TOOL]
        assert tool_turns and all(t.effect_status is EffectStatus.UNKNOWN for t in tool_turns)

    def test_skips_calls_already_answered(self, tmp_db):
        ui = self._ui_with_tool_result_tracking()
        session = _make_session(ui=ui)
        session.messages.append(
            turn_from_dict(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "call_a", "function": {"name": "search", "arguments": "{}"}},
                        {"id": "call_b", "function": {"name": "compute", "arguments": "{}"}},
                    ],
                },
            )
        )
        session._msg_tokens.append(1)
        # call_a already answered.
        session.messages.append(
            turn_from_dict(
                {"role": "tool", "tool_call_id": "call_a", "content": "result"},
            )
        )
        session._msg_tokens.append(1)

        session._synthesize_cancelled_results("Cancelled by user.")

        # Only call_b synthesized.
        assert len(ui.tool_results) == 1
        assert ui.tool_results[0][0] == "call_b"

    def test_ui_emit_failure_does_not_break_synthesis(self, tmp_db):
        """The UI hook is wrapped in try/except — a hook failure
        during cancel must NOT compound the problem. Synthesis still
        appends to messages + storage."""

        class _ExplodingUI(NullUI):
            def on_tool_result(self, call_id, name, output, **kwargs):
                raise RuntimeError("ui hook blew up")

        ui = _ExplodingUI()
        session = _make_session(ui=ui)
        session.messages.append(
            turn_from_dict(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "call_a", "function": {"name": "search", "arguments": "{}"}},
                    ],
                },
            )
        )
        session._msg_tokens.append(1)

        # Must not raise.
        session._synthesize_cancelled_results("Cancelled by user.")

        tool_msgs = [m for m in dicts_from_turns(session.messages) if m.get("role") == "tool"]
        assert len(tool_msgs) == 1


class TestTimeoutDisposition:
    """A tool stopped at its deadline has unobserved side effects, so its
    result must read UNKNOWN — the same ``unknown, never none`` discipline as
    cancellation (HYPOTHESIS.md effect-record appendix), applied to timeouts.
    Read-only timeouts stay a plain failure: an idempotent read has nothing to
    reconcile, and "reconcile before re-issuing" would be misleading there.
    """

    def test_bash_timeout_reads_unknown(self):
        """A bash command is SIGKILL'd at its deadline — the same mid-flight
        kill as cancel — so it may have run partially or had side effects and
        must read UNKNOWN, not a flat 'timed out' that invites a blind re-run."""
        session = _make_session(tool_timeout=1)
        # Sleeps silently past the 1s deadline → watchdog SIGKILL → TimeoutExpired.
        call_id, result = session._exec_bash({"call_id": "c1", "command": "sleep 30"})
        assert call_id == "c1"
        assert "timed out" in result.lower()
        assert "UNKNOWN" in result
        # Typed twin of the prose (Thread A): the producer records UNKNOWN.
        assert session._tool_status.get("c1") is EffectStatus.UNKNOWN

    def test_mcp_tool_timeout_reads_unknown(self):
        """An MCP tool is an opaque action — the server may have run it to
        completion before we stopped waiting, so the outcome reads UNKNOWN."""
        session = _make_session()
        session._mcp_client = MagicMock()
        session._mcp_client.call_tool_sync.side_effect = TimeoutError()
        call_id, result = session._exec_mcp_tool(
            {"call_id": "c1", "mcp_func_name": "send_email", "mcp_args": {}}
        )
        assert call_id == "c1"
        assert "timed out" in result.lower()
        assert "UNKNOWN" in result
        assert session._tool_status.get("c1") is EffectStatus.UNKNOWN

    def test_mcp_resource_read_timeout_stays_plain(self):
        """A resource read is an idempotent read with nothing to reconcile, so
        its timeout stays a plain failure — no UNKNOWN/reconcile advice and no
        typed status."""
        session = _make_session()
        session._mcp_client = MagicMock()
        session._mcp_client.read_resource_sync.side_effect = TimeoutError()
        call_id, result = session._exec_read_resource(
            {"call_id": "c1", "resource_uri": "file:///doc"}
        )
        assert call_id == "c1"
        assert "timed out" in result.lower()
        assert "UNKNOWN" not in result
        assert session._tool_status.get("c1") is None


class TestCancelledAgentDisposition:
    """A cancelled task_agent folds back an honest ledger, not a bare string.

    Regression guard for the HYPOTHESIS.md cancellation appendix: ρ may
    fabricate the acknowledgment but must not fabricate the outcome —
    ``unknown``, never ``none``.
    """

    @staticmethod
    def _assistant(call_id, name):
        return Turn.assistant("", tool_calls=(ToolCall(id=call_id, name=name, arguments=""),))

    @staticmethod
    def _result(call_id, text="ok"):
        return Turn.tool(call_id, text)

    def test_status_none_when_no_actions(self):
        """Typed twin of the disposition: a task cancelled before any action is
        NONE, not UNKNOWN — the complement of the in-flight case."""
        session = _make_session()
        assert session._cancelled_agent_status([]) is EffectStatus.NONE

    def test_status_unknown_when_in_flight(self):
        session = _make_session()
        msgs = [self._assistant("t1", "bash")]  # issued, no result → in flight
        assert session._cancelled_agent_status(msgs) is EffectStatus.UNKNOWN

    def test_status_partial_when_all_answered(self):
        """Every issued call returned but the agent was stopped before finishing
        — effects are known (not UNKNOWN) yet the task is incomplete: PARTIAL."""
        session = _make_session()
        msgs = [self._assistant("t1", "bash"), self._result("t1")]
        assert session._cancelled_agent_status(msgs) is EffectStatus.PARTIAL

    def test_no_actions_reports_no_side_effects(self, tmp_db):
        session = _make_session()
        out = session._cancelled_agent_disposition([], "task")
        assert "no side effects" in out
        assert "UNKNOWN" not in out

    def test_marks_in_flight_action_unknown(self, tmp_db):
        session = _make_session()
        # bash completed; web_fetch was in flight (issued, no result yet) —
        # the first unanswered call is the in-flight boundary.
        msgs = [
            self._assistant("t1", "bash"),
            self._result("t1"),
            self._assistant("t2", "web_fetch"),
        ]
        out = session._cancelled_agent_disposition(msgs, "task")
        assert out != "(task interrupted by user)"
        assert "Completed before cancel: bash." in out
        assert "In flight at cancel: web_fetch" in out
        assert "UNKNOWN" in out

    def test_unanswered_tool_is_in_flight_unknown(self, tmp_db):
        # An output-flowing bash SIGKILL'd mid-stream raises (no result row) —
        # it is the in-flight boundary and must read UNKNOWN, never completed.
        session = _make_session()
        msgs = [self._assistant("t1", "bash")]  # issued, no result
        out = session._cancelled_agent_disposition(msgs, "task")
        assert "In flight at cancel: bash" in out
        assert "UNKNOWN" in out
        assert "Completed before cancel" not in out

    def test_all_answered_reports_completed_no_in_flight(self, tmp_db):
        # Every issued call returned a result — cancel landed between turns,
        # nothing in flight. Each result carries its own disposition; the
        # summary just lists what completed, with no UNKNOWN boundary.
        session = _make_session()
        msgs = [self._assistant("t1", "bash"), self._result("t1", "(killed)")]
        out = session._cancelled_agent_disposition(msgs, "task")
        assert "Completed before cancel: bash." in out
        assert "In flight at cancel" not in out

    def test_boundary_is_first_unanswered_not_last(self, tmp_db):
        # Regression (bug-1): a turn issues [bash, web_fetch] executed
        # sequentially; cancel hits during bash (unanswered, side effects
        # possible) and web_fetch never runs. The in-flight UNKNOWN must be
        # bash (the FIRST gap), and web_fetch must read "not started" — NOT
        # the inverse. The old code took the LAST issued call, labelling the
        # never-run web_fetch UNKNOWN and the actually-in-flight bash "not
        # started" — inviting a re-run of the destructive bash.
        session = _make_session()
        msgs = [
            Turn.assistant(
                "",
                tool_calls=(
                    ToolCall(id="t1", name="bash", arguments=""),
                    ToolCall(id="t2", name="web_fetch", arguments=""),
                ),
            )
        ]  # neither answered: bash raised mid-flight, web_fetch never ran
        out = session._cancelled_agent_disposition(msgs, "task")
        assert "In flight at cancel: bash" in out
        assert "In flight at cancel: web_fetch" not in out
        assert "Not started (cancelled first): web_fetch." in out

    def test_counts_and_not_started(self, tmp_db):
        # Turn 1 completes [bash, bash, read_file]; turn 2 issues
        # [web_fetch (in flight), search (never ran)]. Exercises the ×N
        # count summary, the first-gap boundary, and not-started.
        session = _make_session()
        msgs = [
            Turn.assistant(
                "",
                tool_calls=(
                    ToolCall(id="t1", name="bash", arguments=""),
                    ToolCall(id="t2", name="bash", arguments=""),
                    ToolCall(id="t3", name="read_file", arguments=""),
                ),
            ),
            self._result("t1"),
            self._result("t2"),
            self._result("t3"),
            Turn.assistant(
                "",
                tool_calls=(
                    ToolCall(id="t4", name="web_fetch", arguments=""),
                    ToolCall(id="t5", name="search", arguments=""),
                ),
            ),
        ]
        out = session._cancelled_agent_disposition(msgs, "task")
        assert "Completed before cancel: bash×2, read_file." in out
        assert "In flight at cancel: web_fetch" in out
        assert "Not started (cancelled first): search." in out

    def test_exec_task_routes_cancel_to_disposition(self, tmp_db):
        """_exec_task converts a GenerationCancelled from _run_agent into the
        honest disposition, reading the in-place-mutated agent_turns."""
        session = _make_session()

        def fake_run_agent(agent_turns, **kwargs):
            agent_turns.append(self._assistant("t1", "bash"))
            agent_turns.append(self._result("t1"))
            agent_turns.append(self._assistant("t2", "web_fetch"))
            raise GenerationCancelled()

        with patch.object(session, "_run_agent", side_effect=fake_run_agent):
            call_id, result = session._exec_task({"call_id": "c1", "prompt": "do x"})

        assert call_id == "c1"
        assert result != "(task interrupted by user)"
        assert "UNKNOWN" in result
        assert "web_fetch" in result  # in-flight boundary
        assert "bash" in result  # completed
        # Thread A: the task call's typed status is UNKNOWN (web_fetch in flight).
        assert session._tool_status.get("c1") is EffectStatus.UNKNOWN


class TestEffectStatusPersistence:
    """Typed effect status rides the role-exclusive ``meta`` column and
    round-trips through ``reconstruct_turns`` without disturbing the SYSTEM
    ``source_meta`` that shares the column (no migration; HYPOTHESIS.md
    effect-record appendix — the ledger persists for audit)."""

    def test_effect_status_meta_envelope(self):
        assert _tool_turn_meta(None) is None
        assert json.loads(_tool_turn_meta(EffectStatus.UNKNOWN)) == {"effect_status": "unknown"}
        assert json.loads(_tool_turn_meta(None, {"kind": "web"})) == {"preview": {"kind": "web"}}
        assert json.loads(_tool_turn_meta(EffectStatus.UNKNOWN, {"kind": "web"})) == {
            "effect_status": "unknown",
            "preview": {"kind": "web"},
        }

    def test_reconstruct_routes_tool_effect_status(self):
        from turnstone.core.storage._utils import reconstruct_turns

        # row: (id, role, content, tool_name, tc_id, provider_data,
        #       tool_calls, source, event_id, is_error, meta)
        tool_row = (
            1,
            "tool",
            "timed out. Outcome UNKNOWN ...",
            None,
            "call_a",
            None,
            None,
            None,
            None,
            True,
            json.dumps({"effect_status": "unknown"}),
        )
        turns = reconstruct_turns([tool_row], "ws1")
        assert turns[0].effect_status is EffectStatus.UNKNOWN
        assert turns[0].is_error is True

    def test_reconstruct_leaves_system_source_meta_untouched(self):
        from turnstone.core.storage._utils import reconstruct_turns

        sys_row = (
            2,
            "system",
            "watch fired",
            None,
            None,
            None,
            None,
            "watch_triggered",
            None,
            False,
            json.dumps({"watch_name": "x"}),
        )
        turns = reconstruct_turns([sys_row], "ws1")
        assert turns[0].meta.extra.get("source_meta") == {"watch_name": "x"}
        assert turns[0].effect_status is None


class TestNeverArmedStopLeavesNoRow:
    def test_stop_during_creation_persists_nothing(self, tmp_db):
        """A Stop landing while creation is still connecting — nothing
        armed, zero tokens streamed — must not write an assistant row: a
        marker-only row would replay to the model as context on every
        later turn.  (An ARMED zero-token Stop still records its marker
        via record_cancelled_partial — TestCancelDuringStreaming pins
        that side.)"""
        ui = NullUI()
        session = _make_session(ui=ui)
        provider = arm_session(session)  # provider shell; create scripted below

        def create_cancel_then_fail(**kwargs):
            session._cancel_event.set()
            raise ConnectionError("connect blew up mid-dial")

        provider.create_streaming = MagicMock(side_effect=create_cancel_then_fail)
        session.send("test")

        assert ui.states[-1] == "idle"
        assert any("cancelled" in i.lower() for i in ui.infos)
        assistant = [m for m in dicts_from_turns(session.messages) if m["role"] == "assistant"]
        assert assistant == []


class TestOrphanArmDutiesGate:
    def test_superseded_arrival_in_toctou_window_fires_no_duties(self, tmp_db):
        """The ref's supersession read and the arm hook are two lockless
        steps; a force-cancel can claim a new generation between them.
        The duties self-gate on the generation, so even an append whose
        supersession read went stale cannot null the successor's usage
        slots or record health for the abandoned lane."""
        from turnstone.core.session import _StreamTurnConsumer

        session = _make_session()
        consumer = _StreamTurnConsumer(session, my_generation=1)
        tracker = MagicMock()

        class _StaleReadRef(_CancelRef):
            # The stale supersession read the accepted bytecode-width
            # window produces — the hook itself must hold the line.
            def _superseded(self) -> bool:
                return False

        ref = _StaleReadRef(session, 1, on_first_append=consumer.on_stream_armed)
        consumer.begin_attempt(ref, tracker, MagicMock())

        session._generation = 1
        sentinel = {"prompt_tokens": 7}
        session._last_usage = sentinel
        session._assistant_pending_tokens = 42

        # The force-cancel claims a newer generation before the append.
        session._generation = 2
        ref.append(MagicMock())

        assert session._last_usage is sentinel
        assert session._assistant_pending_tokens == 42
        tracker.record_success.assert_not_called()
