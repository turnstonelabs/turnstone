"""Tests for generation cancellation (cooperative cancel via threading.Event)."""

import contextlib
import json
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests._session_helpers import (
    arm_session,
    make_registered_session,
    make_result,
    make_session,
    provider_shell,
    replace_session_lane,
    scripted_chat_client,
)
from turnstone.cli import WorkstreamTerminalUI
from turnstone.console.coordinator_ui import ConsoleCoordinatorUI
from turnstone.core.providers import (
    IncompleteStreamError,
    StreamChunk,
    ToolCallDelta,
    UsageInfo,
)
from turnstone.core.session import (
    GenerationCancelled,
    _active_shell_owner,
    _CancelledToolResult,
    _CancelRef,
    _StreamTurnConsumer,
    _TaskExecutionJournal,
    _tool_turn_meta,
)
from turnstone.core.session_manager import SessionManager
from turnstone.core.session_ui_base import SessionUIBase
from turnstone.core.trajectory import (
    EffectStatus,
    Role,
    ToolCall,
    Turn,
    dicts_from_turns,
    turn_from_dict,
)
from turnstone.core.workstream import WorkstreamKind, WorkstreamState


def _bind_storage_mock() -> MagicMock:
    """Replace the process-global backend for one storage-boundary test."""
    from turnstone.core.storage import _registry

    storage = MagicMock()
    _registry._storage = storage
    return storage


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


class _ToolResultTrackingUI(NullUI):
    """Capture the public live receipt surface used by cancellation repair."""

    def __init__(self) -> None:
        super().__init__()
        self.tool_results: list[tuple[str, str, str, bool]] = []

    def on_tool_result(self, call_id, name, output, **kwargs):
        self.tool_results.append(
            (call_id, name, output, bool(kwargs.get("is_error", False))),
        )


class _StreamRecordingUI(NullUI):
    """Record every display channel guarded by the main stream rail."""

    def __init__(self) -> None:
        super().__init__()
        self.content_tokens: list[str] = []
        self.reasoning_tokens: list[str] = []

    def on_content_token(self, text):
        self.content_tokens.append(text)

    def on_reasoning_token(self, text):
        self.reasoning_tokens.append(text)


class _DeferredStateStorageUI(NullUI):
    """Expose a blocking durable tail followed by its state publication."""

    def __init__(self) -> None:
        super().__init__()
        self.storage_started = threading.Event()
        self.release_storage = threading.Event()
        self.persisted_states: list[str] = []

    def on_state_change_deferred(self, state, *, deferred_persistence, owner_valid):
        def persist_then_publish_state() -> None:
            if not owner_valid():
                return
            self.storage_started.set()
            if not self.release_storage.wait(2):
                raise RuntimeError("test state storage was not released")
            if owner_valid():
                self.persisted_states.append(state)
                self.states.append(state)

        deferred_persistence.append(persist_then_publish_state)


def _make_session(ui=None, **kwargs):
    """Wrap the shared session factory; this suite defaults to its
    recording NullUI.  The defaults live in
    tests/_session_helpers.make_session — duplicating them here is
    exactly the drift its docstring warns about."""
    return make_session(ui=ui or NullUI(), **kwargs)


def _make_registered_session(ui=None, **kwargs):
    """Build the durable variant for tests that reach model admission."""
    return make_registered_session(ui=ui or NullUI(), **kwargs)


class _BlockingAgentStream:
    """Close-unblocked provider iterator for task-agent cancellation tests."""

    def __init__(self) -> None:
        self.read_started = threading.Event()
        self.closed = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        self.read_started.set()
        if not self.closed.wait(2):
            raise RuntimeError("test stream was not closed")
        raise IncompleteStreamError("stream closed by Stop")

    def close(self) -> None:
        self.closed.set()


class _ObservedRLock:
    """RLock wrapper that exposes when one selected thread tries to enter."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._watched_thread: threading.Thread | None = None
        self.waiting = threading.Event()

    def watch(self, thread: threading.Thread) -> None:
        self._watched_thread = thread
        self.waiting.clear()

    # ``acquire``/``release`` (not just the context-manager pair) so this
    # wrapper can back a ``threading.Condition``, which binds those two
    # methods off the lock it is given.
    def acquire(self, *args, **kwargs):
        if threading.current_thread() is self._watched_thread:
            self.waiting.set()
        return self._lock.acquire(*args, **kwargs)

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


class _GatedRLock:
    """Pause one selected thread immediately before it acquires an RLock."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self._watched_thread: threading.Thread | None = None
        self.waiting = threading.Event()
        self.release = threading.Event()

    def watch(self, thread: threading.Thread) -> None:
        self._watched_thread = thread
        self.waiting.clear()
        self.release.clear()

    def __enter__(self):
        if threading.current_thread() is self._watched_thread:
            self.waiting.set()
            if not self.release.wait(2):
                raise RuntimeError("test generation-lock entrant was not released")
        self._delegate.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._delegate.release()


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

    def test_close_approval_sweep_preserves_legacy_single_slot_wake(self, tmp_db):
        ui = NullUI()
        ui._approval_event = threading.Event()
        ui._approval_result = (True, "stale")
        session = _make_session(ui=ui)

        session.resolve_close_approvals()

        assert ui._approval_event.is_set()
        assert ui._approval_result == (False, None)

    def test_cancel_event_cleared_on_send_start(self, tmp_db):
        """send() clears a stale cancel flag before starting."""
        ui = NullUI()
        session = _make_registered_session(ui=ui)
        session.cancel()  # Set stale flag

        fake_stream = iter([StreamChunk(content_delta="Hello", finish_reason="stop")])

        arm_session(session, fake_stream)
        session.send("test")

        # Should complete normally — cancel flag was cleared
        assert "idle" in ui.states

    def test_budget_gate_witness_ignores_old_idle_stop_and_sees_new_cancel(self, tmp_db):
        """The pre-generation budget gate observes an edge, not stale state."""
        captured: dict[str, Any] = {}

        class BudgetUI(NullUI):
            def __init__(self) -> None:
                super().__init__()
                self.errors: list[str] = []

            def approve_tools(self, items):
                witness = items[0]["_approval_cancel_witness"]
                captured["witness"] = witness
                # The idle Stop before this send is not a cancellation of this
                # new gate.  A fresh Stop during it is.
                assert witness.aborted is False
                session.cancel()
                assert witness.aborted is True
                return False, "Cancelled by user"

            def on_error(self, message):
                self.errors.append(message)

        ui = BudgetUI()
        session = _make_session(ui=ui)
        session.cancel()  # old idle Stop; send has not claimed a generation
        session._budget_exhausted = True
        messages_before = list(session.messages)

        session.send("do not append this turn")

        witness = captured["witness"]
        assert witness.aborted is True
        assert session.messages == messages_before
        assert ui.errors == []
        assert session._budget_exhausted is True

    def test_old_consumer_cannot_clear_successor_cancel_event(self, tmp_db):
        """Consume and successor claim serialize on the transition lock."""
        session = _make_session()
        old_generation = session._claim_generation()
        old_event = session._cancel_event
        old_event.set()
        consume_entered = threading.Event()
        release_consume = threading.Event()
        transition_lock = _ObservedRLock()
        session._generation_transition_lock = transition_lock
        consumed: list[bool] = []
        successor: list[tuple[int, threading.Event]] = []
        errors: list[BaseException] = []
        real_is_set = old_event.is_set

        def blocking_is_set():
            consume_entered.set()
            if not release_consume.wait(2):
                raise RuntimeError("cancel consumer was not released")
            return real_is_set()

        def consume_old():
            try:
                consumed.append(session._consume_cancel(old_generation))
            except BaseException as exc:
                errors.append(exc)

        def claim_and_cancel_successor():
            try:
                generation = session._claim_generation()
                fresh_event = session._cancel_event
                session.cancel()
                successor.append((generation, fresh_event))
            except BaseException as exc:
                errors.append(exc)

        consumer = threading.Thread(target=consume_old)
        claimant = threading.Thread(target=claim_and_cancel_successor)
        transition_lock.watch(claimant)
        with patch.object(old_event, "is_set", side_effect=blocking_is_set):
            consumer.start()
            assert consume_entered.wait(2)
            claimant.start()
            assert transition_lock.waiting.wait(2)
            release_consume.set()
            consumer.join(2)
            claimant.join(2)

        assert not consumer.is_alive()
        assert not claimant.is_alive()
        assert errors == []
        assert consumed == [True]
        assert not old_event.is_set()
        assert len(successor) == 1
        successor_generation, fresh_event = successor[0]
        assert successor_generation == old_generation + 1
        assert fresh_event is session._cancel_event
        assert fresh_event is not old_event
        assert fresh_event.is_set()

        # A predecessor finally block that arrives after the handoff is a
        # no-op, including against the successor's independently set event.
        assert session._consume_cancel(old_generation) is False
        assert fresh_event.is_set()


class TestCancelDuringStreaming:
    """Cancel while the streaming consumer is observing chunks."""

    def test_preserves_partial_content(self, tmp_db):
        """Partial content already streamed should be preserved in messages."""
        ui = NullUI()
        session = _make_registered_session(ui=ui)

        def cancelling_stream():
            """Yield a few chunks then cancel."""
            yield StreamChunk(content_delta="Hello ")
            yield StreamChunk(content_delta="world")
            session.cancel()
            yield StreamChunk(content_delta=" — this should not appear")

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
        session = _make_registered_session(ui=ui)

        # First call: return content with a tool call
        def stream_with_tool():
            yield StreamChunk(
                tool_call_deltas=[ToolCallDelta(index=0, id="tc_1", name="bash")],
            )
            yield StreamChunk(
                tool_call_deltas=[ToolCallDelta(index=0, arguments_delta='{"command":"echo hi"}')],
                finish_reason="tool_calls",
            )

        def cancel_before_execute(
            tool_calls,
            *,
            principal_id: str = "",
            my_generation: int = 0,
        ):
            """Simulate cancel happening before tool execution."""
            assert principal_id == ""
            assert my_generation == 1
            session.cancel()
            raise GenerationCancelled()

        provider = arm_session(session, stream_with_tool())
        with patch.object(session, "_execute_tools", side_effect=cancel_before_execute):
            session.send("run something")

        # The stream must not be re-created after the cancel landed.
        assert provider is session._model_binding.lane.provider
        assert provider.create_streaming.call_count == 1

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
        session = _make_registered_session()
        session.cancel()
        # Next send should work normally (cancel cleared at start)

        fake_stream = iter([StreamChunk(content_delta="ok", finish_reason="stop")])
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
        session = _make_registered_session(ui=ui)

        barrier = threading.Event()

        def slow_stream():
            yield StreamChunk(content_delta="Start")
            barrier.set()  # Signal that streaming has started
            time.sleep(2)  # Simulate slow streaming
            yield StreamChunk(content_delta=" end", finish_reason="stop")

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
        session = _make_registered_session(ui=ui)

        def stream_content_then_tool():
            # Content long enough to leave chars in the tag-scan carry
            # buffer (ThinkTagSplitter retains the last MAX_TAG_LEN = 12
            # chars until a flush)
            yield StreamChunk(content_delta="Hello world, this is a test message")
            yield StreamChunk(
                tool_call_deltas=[ToolCallDelta(index=0, id="tc_1", name="bash")],
            )
            yield StreamChunk(
                tool_call_deltas=[ToolCallDelta(index=0, arguments_delta='{"command":"echo hi"}')],
                finish_reason="tool_calls",
            )

        # Two scripted turns: the tool round-trip makes send() loop, and
        # the follow-up turn must carry a real finish reason — the strict
        # finish gate (RULED, #832) rejects an exhausted iterator that
        # used to commit as an empty turn.
        arm_session(
            session,
            stream_content_then_tool(),
            iter([StreamChunk(finish_reason="stop")]),
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
        session = _make_registered_session(ui=ui)

        seen: dict = {}

        def observing_stream():
            # Runs at first next(): the armed handle must ALREADY be
            # registered (append happens inside create_streaming, before
            # the iterator is handed back).
            seen["handle_at_first_chunk"] = session._cancel_stream
            yield StreamChunk(content_delta="hi", finish_reason="stop")

        provider = arm_session(session, observing_stream())
        session.send("test")

        assert seen["handle_at_first_chunk"] is provider._armed_handle
        # After stream completes, cancel_stream should be cleared
        assert session._cancel_stream is None

    def test_transport_error_during_cancel_becomes_generation_cancelled(self, tmp_db):
        """When cancel() closes the stream, the resulting transport error
        is converted to GenerationCancelled."""
        ui = NullUI()
        session = _make_registered_session(ui=ui)

        def stream_that_errors():
            yield StreamChunk(content_delta="Hello")
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
        session = _make_registered_session(ui=ui)

        def stream_that_errors():
            yield StreamChunk(content_delta="Hello")
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


class TestTaskAgentStreamAbort:
    """#975: Stop owns every parallel child model stream, not only the main one."""

    @staticmethod
    def _install_blocking_provider(session, streams):
        provider = provider_shell()
        remaining = list(streams)
        take_lock = threading.Lock()

        def create_streaming(**kwargs):
            with take_lock:
                assert remaining
                stream = remaining.pop(0)
            cancel_ref = kwargs.get("cancel_ref")
            assert cancel_ref is not None
            cancel_ref.append(stream)
            return stream

        provider.create_streaming = MagicMock(side_effect=create_streaming)
        replace_session_lane(session, provider=provider)
        return provider

    @staticmethod
    def _start_agent(session, turns, outcomes):
        def run():
            try:
                outcomes.append(session._run_agent(turns, label="task"))
            except BaseException as exc:
                outcomes.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        return thread

    @staticmethod
    def _start_web_fetch(session, item, outcomes):
        def run():
            try:
                outcomes.append(session._exec_web_fetch(item))
            except BaseException as exc:
                outcomes.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        return thread

    @staticmethod
    def _start_task(session, item, outcomes):
        def run():
            try:
                outcomes.append(session._exec_task(item))
            except BaseException as exc:
                outcomes.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        return thread

    def test_cancel_closes_parallel_agent_streams_without_claiming_main_slot(self, tmp_db):
        session = _make_session()
        streams = [_BlockingAgentStream(), _BlockingAgentStream()]
        provider = self._install_blocking_provider(session, streams)
        main_stream = MagicMock()
        session._cancel_stream = main_stream
        outcomes = []
        threads = [
            self._start_agent(
                session,
                [Turn.user("start"), Turn.assistant("partial"), Turn.user("continue")],
                outcomes,
            )
            for _ in streams
        ]

        try:
            assert all(stream.read_started.wait(2) for stream in streams)
            assert session._cancel_stream is main_stream
            session.cancel()
        finally:
            session.cancel()
            for thread in threads:
                thread.join(2)

        assert all(not thread.is_alive() for thread in threads)
        assert all(stream.closed.is_set() for stream in streams)
        assert len(outcomes) == 2
        assert all(isinstance(outcome, GenerationCancelled) for outcome in outcomes)
        assert provider.create_streaming.call_count == 2
        assert session._cancel_stream is main_stream
        main_stream.close.assert_called()
        assert session._parallel_model_cancel_scopes == {}

    def test_close_unblocks_task_agent_waiting_for_approval(self, tmp_db):
        """Direct close denies a registered task-agent gate without timeout."""
        from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider

        ui = SessionUIBase(ws_id="ws-close-gate", user_id="u1")
        session = _make_session(ui=ui)
        client = replace_session_lane(session, provider=OpenAIChatCompletionsProvider()).client
        client.chat.completions.create = scripted_chat_client(
            {
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "read_file",
                        "arguments": '{"path": "/tmp/test"}',
                    }
                ],
                "finish_reason": "tool_calls",
            }
        )
        outcomes: list[object] = []
        executor = MagicMock(return_value=("call_1", "must not run"))

        def fake_prepare(tc_dict, **_kwargs):
            return {
                "call_id": tc_dict["id"],
                "func_name": "read_file",
                "needs_approval": True,
                "execute": executor,
            }

        def run_agent() -> None:
            try:
                outcomes.append(
                    session._run_agent(
                        [Turn.user("test")],
                        tools=[{"type": "function", "function": {"name": "read_file"}}],
                        auto_tools=set(),
                        label="test",
                    )
                )
            except BaseException as exc:
                outcomes.append(exc)

        agent = threading.Thread(target=run_agent)
        with (
            patch.object(session, "_prepare_tool", side_effect=fake_prepare),
            patch.object(session, "_evaluate_intent", return_value=None),
            patch("turnstone.core.storage._registry.get_storage", return_value=None),
            patch("turnstone.core.policy.evaluate_tool_policies_batch", return_value={}),
        ):
            agent.start()
            try:
                stop = time.monotonic() + 2
                while time.monotonic() < stop:
                    with ui._ws_lock:
                        if ui._approval_cycles:
                            break
                    time.sleep(0.005)
                with ui._ws_lock:
                    assert len(ui._approval_cycles) == 1
                session.close()
                agent.join(2)
            finally:
                session.close()
                if agent.is_alive():
                    ui.resolve_all_approvals(False, "test teardown")
                    agent.join(2)

        assert not agent.is_alive()
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], GenerationCancelled)
        executor.assert_not_called()
        assert ui._approval_cycles == {}

    def test_soft_close_wakes_main_tool_approval_before_structural_wait(self, tmp_db):
        """Soft close lets the owning send journal denied TOOL receipts.

        The accepted assistant row owns structural debt before the manual
        approval gate opens. Cancellation alone advances the gate witness but
        does not wake its wait; preparation must sweep approvals before it
        waits for the worker to synthesize the matching TOOL receipt.
        """
        ui = ConsoleCoordinatorUI(ws_id="ws-soft-close-gate", user_id="u1")
        session = _make_registered_session(
            ui=ui,
            ws_id="ws-soft-close-gate",
            user_id="u1",
        )
        session._title_generated = True
        tool_call = {
            "id": "call-soft-close",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }
        executor = MagicMock(return_value=("call-soft-close", "must not run"))
        send_errors: list[BaseException] = []

        def _prepare(tc: dict[str, Any]) -> dict[str, Any]:
            return {
                "call_id": tc["id"],
                "func_name": "read_file",
                "header": "Read file",
                "preview": "",
                "needs_approval": True,
                "execute": executor,
            }

        def _send() -> None:
            try:
                session.send("use the tool", acting_user_id="u1")
            except BaseException as exc:  # pragma: no cover - surfaced below
                send_errors.append(exc)

        with (
            patch.object(
                session,
                "_stream_response",
                return_value=make_result(tool_calls=[tool_call]),
            ),
            patch.object(session, "_prepare_tool", side_effect=_prepare),
            patch.object(session, "_evaluate_intent", return_value=None),
            patch("turnstone.core.policy.evaluate_tool_policies_batch", return_value={}),
        ):
            worker = threading.Thread(target=_send, daemon=True)
            worker.start()
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    with ui._ws_lock:
                        if ui._approval_cycles:
                            break
                    time.sleep(0.005)
                with ui._ws_lock:
                    assert len(ui._approval_cycles) == 1
                assert session.has_tool_structural_debt() is True

                assert session.prepare_soft_close() is True
                worker.join(2)
            finally:
                session.cancel()
                ui.resolve_all_approvals(False, "test teardown")
                worker.join(2)

        assert not worker.is_alive()
        assert send_errors == []
        executor.assert_not_called()
        assert ui._approval_cycles == {}
        assert session.has_tool_structural_debt() is False
        assert [turn.role for turn in session.messages][-2:] == [Role.ASSISTANT, Role.TOOL]

    def test_soft_close_cancels_background_cli_approval_without_prompt(self, tmp_db):
        """A background CLI gate observes close without changing foreground.

        WorkstreamTerminalUI parks on ``_fg_event`` until its workstream is
        selected. Soft close must still let the worker journal its denied TOOL
        receipt, but setting the foreground event as the wake mechanism would
        corrupt live UI state if durability later refused the close.
        """
        manager = MagicMock()
        manager.active_id = "another-workstream"
        manager.set_state_deferred.return_value = True
        ui = WorkstreamTerminalUI("cli-background-close", manager)
        ui.set_foreground(False)
        session = _make_registered_session(
            ui=ui,
            ws_id="cli-background-close",
        )
        session._title_generated = True
        tool_call = {
            "id": "call-cli-close",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }
        executor = MagicMock(return_value=("call-cli-close", "must not run"))
        send_errors: list[BaseException] = []

        def _prepare(tc: dict[str, Any]) -> dict[str, Any]:
            return {
                "call_id": tc["id"],
                "func_name": "read_file",
                "header": "Read file",
                "preview": "",
                "needs_approval": True,
                "execute": executor,
            }

        def _send() -> None:
            try:
                session.send("use the tool")
            except BaseException as exc:  # pragma: no cover - surfaced below
                send_errors.append(exc)

        with (
            patch.object(
                session,
                "_stream_response",
                return_value=make_result(tool_calls=[tool_call]),
            ),
            patch.object(session, "_prepare_tool", side_effect=_prepare),
            patch.object(session, "_evaluate_intent", return_value=None),
            patch("turnstone.core.policy.evaluate_tool_policies_batch", return_value={}),
            patch("builtins.input", return_value="y") as prompt,
        ):
            worker = threading.Thread(target=_send, daemon=True)
            worker.start()
            try:
                deadline = time.monotonic() + 2
                waiting = False
                while time.monotonic() < deadline:
                    with ui._print_lock:
                        waiting = any(
                            event_type == "info" and "Waiting for approval" in text
                            for event_type, text in ui._output_buffer
                        )
                    if waiting and session.has_tool_structural_debt():
                        break
                    time.sleep(0.005)
                assert waiting is True
                assert session.has_tool_structural_debt() is True
                assert ui._fg_event.is_set() is False

                assert session.prepare_soft_close() is True
                worker.join(2)
            finally:
                session.cancel()
                ui._fg_event.set()
                worker.join(2)

        assert not worker.is_alive()
        assert send_errors == []
        prompt.assert_not_called()
        executor.assert_not_called()
        assert session.has_tool_structural_debt() is False
        assert [turn.role for turn in session.messages][-2:] == [Role.ASSISTANT, Role.TOOL]

    def test_stop_at_task_agent_approval_folds_confirmed_no_effect(self, tmp_db):
        """Stop at consent records a denied, never-executed child as NONE.

        This drives the real task wrapper and approval cycle.  The provider has
        issued ``write_file``, but execution authority has not crossed the human
        gate: Stop must deny the cycle, never call the executor, and fold a
        typed NONE child result into the parent task-agent cancellation ledger.
        An issued-but-unanswered UNKNOWN would falsely imply the write may have
        landed and require reconciliation.
        """
        ui = SessionUIBase(ws_id="ws-stop-gate", user_id="u1")
        session = _make_session(ui=ui)
        generation = session._claim_generation()
        generation_event = session._cancel_event
        parent_call_id = "parent-stop-at-approval"
        session.messages.append(
            Turn.assistant(
                "delegating",
                tool_calls=(
                    ToolCall(
                        id=parent_call_id,
                        name="task_agent",
                        arguments='{"prompt":"inspect"}',
                    ),
                ),
            )
        )
        session._msg_tokens.append(1)
        provider = arm_session(
            session,
            [
                StreamChunk(
                    tool_call_deltas=[
                        ToolCallDelta(index=0, id="provider-child", name="write_file")
                    ]
                ),
                StreamChunk(
                    tool_call_deltas=[
                        ToolCallDelta(
                            index=0,
                            arguments_delta='{"path":"ignored","content":"never written"}',
                        )
                    ],
                    finish_reason="tool_calls",
                ),
            ],
        )
        executor = MagicMock(return_value=("provider-child", "must not run"))
        outcomes: list[object] = []

        def prepare_tool(tc):
            return {
                "call_id": tc["id"],
                "func_name": "write_file",
                "needs_approval": True,
                "execute": executor,
            }

        item = {
            "call_id": parent_call_id,
            "prompt": "inspect",
            "_origin_cancel_event": generation_event,
            "_origin_generation": generation,
        }
        with (
            patch.object(session, "_prepare_tool", side_effect=prepare_tool),
            patch.object(session, "_evaluate_intent", return_value=None),
            patch("turnstone.core.storage._registry.get_storage", return_value=None),
            patch("turnstone.core.policy.evaluate_tool_policies_batch", return_value={}),
        ):
            task = self._start_task(session, item, outcomes)
            try:
                stop = time.monotonic() + 2
                while time.monotonic() < stop:
                    with ui._ws_lock:
                        if ui._approval_cycles:
                            break
                    time.sleep(0.005)
                with ui._ws_lock:
                    assert len(ui._approval_cycles) == 1

                # Match the HTTP route ordering: abort operation resources,
                # then deny every registered approval cycle.
                session.cancel()
                assert ui.resolve_all_approvals(False, "Cancelled by user") == 1
                task.join(2)
            finally:
                session.cancel()
                ui.resolve_all_approvals(False, "test teardown")
                task.join(2)

        assert not task.is_alive()
        assert provider.create_streaming.call_count == 1
        executor.assert_not_called()
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], tuple)
        assert outcomes[0][0] == parent_call_id
        disposition = outcomes[0][1]
        assert isinstance(disposition, str)
        assert "Confirmed no effect before cancel: write_file." in disposition
        assert "UNKNOWN" not in disposition
        assert session._cancelled_tool_results[parent_call_id] == _CancelledToolResult(
            detail=disposition,
            effect_status=EffectStatus.NONE,
            is_error=True,
            preview=None,
            live_emitted=True,
        )

        # Exercise the outer cancellation repair too: the staged recursive
        # ledger becomes the durable parent tool result without inventing an
        # UNKNOWN effect or emitting a duplicate live result.
        session._synthesize_cancelled_results("Cancelled by user.")
        folded = [
            turn
            for turn in session.messages
            if turn.role is Role.TOOL and turn.tool_call_id == parent_call_id
        ]
        assert len(folded) == 1
        assert folded[0].text == disposition
        assert folded[0].effect_status is EffectStatus.NONE
        assert parent_call_id not in session._cancelled_tool_results

    def test_cancel_snapshot_cannot_sweep_successor_child_scope(self, tmp_db):
        """A predecessor Stop snapshots before a successor can register."""

        class _BlockingScopeMap(dict):
            def __init__(self, *args):
                super().__init__(*args)
                self.watched_thread: threading.Thread | None = None
                self.snapshot_entered = threading.Event()
                self.release_snapshot = threading.Event()
                self._blocked = False

            def values(self):
                if threading.current_thread() is self.watched_thread and not self._blocked:
                    self._blocked = True
                    self.snapshot_entered.set()
                    if not self.release_snapshot.wait(2):
                        raise RuntimeError("cancel snapshot was not released")
                return super().values()

        session = _make_session()
        old_generation = session._claim_generation()
        old_event = session._cancel_event
        old_stream = _BlockingAgentStream()
        successor_stream = _BlockingAgentStream()
        provider = self._install_blocking_provider(session, [old_stream, successor_stream])
        old_outcomes: list[object] = []
        successor_outcomes: list[object] = []
        cancel_errors: list[BaseException] = []
        successor_generations: list[int] = []
        successor_events: list[threading.Event] = []
        old_abort_entered = threading.Event()
        release_old_abort = threading.Event()

        class _BlockingAbortScope:
            def __init__(self, delegate):
                self.delegate = delegate
                self.calls = 0

            def abort(self):
                self.calls += 1
                old_abort_entered.set()
                if not release_old_abort.wait(2):
                    raise RuntimeError("old scope abort was not released")
                self.delegate.abort()

        def run_old():
            try:
                old_outcomes.append(
                    session._run_agent(
                        [Turn.user("old")],
                        label="task",
                        origin_cancel_event=old_event,
                        origin_generation=old_generation,
                    )
                )
            except BaseException as exc:
                old_outcomes.append(exc)

        old_thread = threading.Thread(target=run_old)
        old_thread.start()
        assert old_stream.read_started.wait(2)
        with session._parallel_model_cancel_lock:
            old_scopes = list(session._parallel_model_cancel_scopes.values())
            assert len(old_scopes) == 1
            old_scope = old_scopes[0]
            blocking_scopes = _BlockingScopeMap(session._parallel_model_cancel_scopes)
            old_token = next(iter(blocking_scopes))
            blocking_old_scope = _BlockingAbortScope(old_scope)
            blocking_scopes[old_token] = blocking_old_scope
            session._parallel_model_cancel_scopes = blocking_scopes

        transition_lock = _ObservedRLock()
        session._generation_transition_lock = transition_lock

        def cancel_old():
            try:
                session.cancel()
            except BaseException as exc:
                cancel_errors.append(exc)

        def run_successor():
            try:
                generation = session._claim_generation()
                event = session._cancel_event
                successor_generations.append(generation)
                successor_events.append(event)
                successor_outcomes.append(
                    session._run_agent(
                        [Turn.user("successor")],
                        label="task",
                        origin_cancel_event=event,
                        origin_generation=generation,
                    )
                )
            except BaseException as exc:
                successor_outcomes.append(exc)

        cancel_thread = threading.Thread(target=cancel_old)
        successor_thread = threading.Thread(target=run_successor)
        blocking_scopes.watched_thread = cancel_thread
        transition_lock.watch(successor_thread)
        try:
            cancel_thread.start()
            assert blocking_scopes.snapshot_entered.wait(2)
            assert old_event.is_set()

            successor_thread.start()
            assert transition_lock.waiting.wait(2)
            assert successor_generations == []

            # Finish the fixed predecessor snapshot, but hold its first
            # abort so the successor deterministically registers before
            # the old Stop sweep resumes.
            blocking_scopes.release_snapshot.set()
            assert old_abort_entered.wait(2)
            assert successor_stream.read_started.wait(2)
            assert successor_generations == [old_generation + 1]
            assert len(successor_events) == 1
            assert not successor_events[0].is_set()
            assert not old_stream.closed.is_set()
            assert not successor_stream.closed.is_set()

            release_old_abort.set()
            cancel_thread.join(2)
            old_thread.join(2)

            assert not cancel_thread.is_alive()
            assert not old_thread.is_alive()
            assert cancel_errors == []
            assert len(old_outcomes) == 1
            assert isinstance(old_outcomes[0], GenerationCancelled)
            assert old_stream.closed.is_set()
            assert not successor_stream.closed.is_set()
            assert successor_thread.is_alive()
            assert blocking_old_scope.calls == 1

            with session._parallel_model_cancel_lock:
                live_scopes = list(session._parallel_model_cancel_scopes.values())
            assert len(live_scopes) == 1
            successor_scope = live_scopes[0]
            assert successor_scope is not old_scope

            # Its own scope teardown, not the predecessor Stop, ends the
            # successor request.
            successor_scope.abort()
            successor_thread.join(2)
        finally:
            blocking_scopes.release_snapshot.set()
            release_old_abort.set()
            old_stream.close()
            successor_stream.close()
            cancel_thread.join(2)
            old_thread.join(2)
            successor_thread.join(2)

        assert not successor_thread.is_alive()
        assert len(successor_outcomes) == 1
        assert isinstance(successor_outcomes[0], GenerationCancelled)
        assert successor_stream.closed.is_set()
        assert provider.create_streaming.call_count == 2
        assert session._parallel_model_cancel_scopes == {}

    def test_cancel_closes_parallel_foreground_web_fetch_streams(self, tmp_db):
        session = _make_session()
        generation = session._claim_generation()
        generation_event = session._cancel_event
        streams = [_BlockingAgentStream(), _BlockingAgentStream()]
        provider = self._install_blocking_provider(session, streams)
        main_stream = MagicMock()
        session._cancel_stream = main_stream
        response = MagicMock()
        response.headers = {"content-type": "text/plain"}
        response.text = "page body"
        response.content = response.text.encode()
        outcomes = []
        items = [
            {
                "call_id": f"web-{index}",
                "url": f"https://example.com/{index}",
                "question": "summarize",
                "allow_private_origin": False,
                "_origin_cancel_event": generation_event,
                "_origin_generation": generation,
            }
            for index in range(2)
        ]

        with (
            patch("turnstone.core.session.fetch_with_ssrf_guard", return_value=response),
            patch.object(session, "_report_tool_result") as report,
        ):
            threads = [self._start_web_fetch(session, item, outcomes) for item in items]
            try:
                assert all(stream.read_started.wait(2) for stream in streams)
                assert session._cancel_stream is main_stream
                session.cancel()
            finally:
                session.cancel()
                for thread in threads:
                    thread.join(2)

        assert all(not thread.is_alive() for thread in threads)
        assert all(stream.closed.is_set() for stream in streams)
        assert len(outcomes) == 2
        assert all(isinstance(outcome, GenerationCancelled) for outcome in outcomes)
        assert provider.create_streaming.call_count == 2
        assert session._cancel_stream is main_stream
        main_stream.close.assert_called()
        report.assert_not_called()
        assert session._parallel_model_cancel_scopes == {}

    def test_foreground_web_fetch_rejects_clean_result_after_successor_claim(self, tmp_db):
        session = _make_session()
        generation = session._claim_generation()
        generation_event = session._cancel_event
        response = MagicMock()
        response.headers = {"content-type": "text/plain"}
        response.text = "page body"
        response.content = response.text.encode()
        seen_refs = []

        def complete_after_cancel(*_args, **kwargs):
            seen_refs.append(kwargs["cancel_ref"])
            session.cancel()
            session._claim_generation()
            return MagicMock(content="stale answer")

        with (
            patch("turnstone.core.session.fetch_with_ssrf_guard", return_value=response),
            patch.object(session, "_utility_completion", side_effect=complete_after_cancel),
            patch.object(session, "_report_tool_result") as report,
            pytest.raises(GenerationCancelled),
        ):
            session._exec_web_fetch(
                {
                    "call_id": "web-stale",
                    "url": "https://example.com/stale",
                    "question": "summarize",
                    "allow_private_origin": False,
                    "_origin_cancel_event": generation_event,
                    "_origin_generation": generation,
                }
            )

        assert len(seen_refs) == 1
        assert seen_refs[0] is not None
        assert seen_refs[0].aborted
        assert not session._cancel_event.is_set()
        report.assert_not_called()
        assert session._parallel_model_cancel_scopes == {}

    def test_cancel_before_agent_handle_arrives_closes_late_handle(self, tmp_db):
        session = _make_session()
        provider = provider_shell()
        stream = _BlockingAgentStream()
        create_entered = threading.Event()
        release_create = threading.Event()

        def create_streaming(**kwargs):
            create_entered.set()
            if not release_create.wait(2):
                raise RuntimeError("test provider was not released")
            cancel_ref = kwargs.get("cancel_ref")
            assert cancel_ref is not None
            cancel_ref.append(stream)
            return stream

        provider.create_streaming = MagicMock(side_effect=create_streaming)
        replace_session_lane(session, provider=provider)
        outcomes = []
        thread = self._start_agent(session, [Turn.user("go")], outcomes)

        try:
            assert create_entered.wait(2)
            session.cancel()
            release_create.set()
        finally:
            release_create.set()
            session.cancel()
            thread.join(2)

        assert not thread.is_alive()
        assert stream.closed.is_set()
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], GenerationCancelled)
        assert provider.create_streaming.call_count == 1
        assert session._parallel_model_cancel_scopes == {}

    def test_force_cancelled_error_is_not_retried_or_salvaged(self, tmp_db):
        session = _make_session()

        def force_cancel_then_fail():
            session.cancel()
            session._claim_generation()
            yield from ()
            raise IncompleteStreamError("old task-agent stream died")

        provider = arm_session(session, force_cancel_then_fail())
        turns = [Turn.user("start"), Turn.assistant("salvage me"), Turn.user("continue")]

        with pytest.raises(GenerationCancelled):
            session._run_agent(turns, label="task")

        assert provider.create_streaming.call_count == 1
        assert [turn.text for turn in turns if turn.role == Role.ASSISTANT] == ["salvage me"]
        assert session._parallel_model_cancel_scopes == {}

    def test_force_cancelled_completed_result_is_rejected(self, tmp_db):
        session = _make_session()

        def force_cancel_then_finish():
            session.cancel()
            session._claim_generation()
            yield StreamChunk(content_delta="stale success", finish_reason="stop")

        provider = arm_session(session, force_cancel_then_finish())
        turns = [Turn.user("go")]

        with pytest.raises(GenerationCancelled):
            session._run_agent(turns, label="task")

        assert provider.create_streaming.call_count == 1
        assert turns == [Turn.user("go")]
        assert session._parallel_model_cancel_scopes == {}

    def test_force_successor_old_task_wrapper_cannot_publish_or_merge_state(self, tmp_db):
        """A stale outer ``_exec_task`` frame has no publication authority.

        Provider call ids may repeat in the successor generation.  Hold that
        successor live under the same parent id while the cancelled predecessor
        unwinds, then pin every non-resource side effect from the old wrapper:
        no result event, typed-status overwrite, trajectory stash, or read-set
        merge may land in the successor's namespace.
        """
        ui = SessionUIBase(ws_id="ws-race", user_id="user-race")
        session = _make_session(ui=ui)
        listener = ui._register_listener()
        call_id = "call_0"
        old_generation = session._claim_generation()
        old_event = session._cancel_event
        old_running = threading.Event()
        successor_running = threading.Event()
        release_old = threading.Event()
        release_successor = threading.Event()
        old_outcomes: list[object] = []
        successor_outcomes: list[object] = []
        threads: list[threading.Thread] = []

        def fake_run(agent_turns, **kwargs):
            generation = kwargs["origin_generation"]
            if generation == old_generation:
                session._current_read_files.add("old-generation.txt")
                issued = Turn.assistant(
                    "",
                    tool_calls=(ToolCall(id="old-action", name="bash", arguments="{}"),),
                )
                agent_turns.append(issued)
                kwargs["execution_journal"].record_assistant(issued)
                kwargs["execution_journal"].mark_started("old-action")
                old_running.set()
                if not release_old.wait(2):
                    raise RuntimeError("old task wrapper was not released")
                raise GenerationCancelled()

            session._current_read_files.add("successor-generation.txt")
            successor_running.set()
            if not release_successor.wait(2):
                raise RuntimeError("successor task wrapper was not released")
            return "fresh result"

        old_item = {
            "call_id": call_id,
            "prompt": "old",
            "_origin_cancel_event": old_event,
            "_origin_generation": old_generation,
        }
        with patch.object(session, "_run_agent", side_effect=fake_run):
            try:
                threads.append(self._start_task(session, old_item, old_outcomes))
                assert old_running.wait(2)

                session.cancel()
                successor_generation = session._claim_generation()
                successor_item = {
                    "call_id": call_id,
                    "prompt": "successor",
                    "_origin_cancel_event": session._cancel_event,
                    "_origin_generation": successor_generation,
                }
                threads.append(self._start_task(session, successor_item, successor_outcomes))
                assert successor_running.wait(2)

                # Stand in for state already owned by the live successor.  The
                # predecessor's cancel disposition must not overwrite it.
                session._tool_status[call_id] = EffectStatus.PARTIAL
                release_old.set()
                threads[0].join(2)
                assert not threads[0].is_alive()

                assert session._tool_status[call_id] is EffectStatus.PARTIAL
                assert "old-generation.txt" not in session._read_files
                assert "successor-generation.txt" not in session._read_files
                assert call_id not in ui._agent_trajectories
                assert listener.empty()
            finally:
                release_old.set()
                release_successor.set()
                for thread in threads:
                    thread.join(2)

        assert all(not thread.is_alive() for thread in threads)
        assert len(old_outcomes) == 1
        assert isinstance(old_outcomes[0], tuple)
        assert old_outcomes[0][0] == call_id
        assert "UNKNOWN" in old_outcomes[0][1]
        assert successor_outcomes == [(call_id, "fresh result")]
        assert "old-generation.txt" not in session._read_files
        assert "successor-generation.txt" in session._read_files

    def test_force_successor_task_cleanup_is_exact_per_invocation(self, tmp_db):
        """A predecessor reaps only resources carrying its unique run owner.

        The old and new invocations deliberately reuse the provider's parent
        call id.  While the successor remains live, the old unwind must remove
        its own child registration and shell scope without touching either of
        the successor's corresponding resources.
        """
        ui = SessionUIBase(ws_id="ws-cleanup", user_id="user-cleanup")
        session = _make_session(ui=ui)
        call_id = "call_0"
        old_child = f"{call_id}::old-child"
        successor_child = f"{call_id}::successor-child"
        old_generation = session._claim_generation()
        old_event = session._cancel_event
        old_running = threading.Event()
        successor_running = threading.Event()
        release_old = threading.Event()
        release_successor = threading.Event()
        old_outcomes: list[object] = []
        successor_outcomes: list[object] = []
        owners: dict[str, object] = {}
        owners_lock = threading.Lock()
        reaped: list[object] = []
        reap_lock = threading.Lock()
        threads: list[threading.Thread] = []

        def fake_run(agent_turns, **kwargs):
            generation = kwargs["origin_generation"]
            parent_call_id = kwargs["parent_call_id"]
            if generation == old_generation:
                key, child_id = "old", old_child
                ready, release = old_running, release_old
            else:
                key, child_id = "successor", successor_child
                ready, release = successor_running, release_successor

            with owners_lock:
                owners[key] = _active_shell_owner.get()
            issued = Turn.assistant(
                "",
                tool_calls=(ToolCall(id=child_id, name="bash", arguments="{}"),),
            )
            agent_turns.append(issued)
            kwargs["execution_journal"].record_assistant(issued)
            kwargs["execution_journal"].mark_started(child_id)
            kwargs["execution_journal"].register_child(child_id)
            session._note_agent_child(child_id, parent_call_id)
            ready.set()
            if not release.wait(2):
                raise RuntimeError(f"{key} task wrapper was not released")
            if key == "old":
                raise GenerationCancelled()
            agent_turns.append(Turn.tool(child_id, "done"))
            kwargs["execution_journal"].record_result(
                child_id,
                "done",
                is_error=False,
                effect_status=None,
            )
            return "fresh result"

        def record_reap(*, owner):
            with reap_lock:
                reaped.append(owner)

        old_item = {
            "call_id": call_id,
            "prompt": "old",
            "_origin_cancel_event": old_event,
            "_origin_generation": old_generation,
        }
        with (
            patch.object(session, "_run_agent", side_effect=fake_run),
            patch.object(session._background_shells, "reap", side_effect=record_reap),
        ):
            try:
                threads.append(self._start_task(session, old_item, old_outcomes))
                assert old_running.wait(2)

                session.cancel()
                successor_generation = session._claim_generation()
                successor_item = {
                    "call_id": call_id,
                    "prompt": "successor",
                    "_origin_cancel_event": session._cancel_event,
                    "_origin_generation": successor_generation,
                }
                threads.append(self._start_task(session, successor_item, successor_outcomes))
                assert successor_running.wait(2)
                with ui._agent_children_lock:
                    assert set(ui._agent_children) == {old_child, successor_child}

                release_old.set()
                threads[0].join(2)
                assert not threads[0].is_alive()

                with owners_lock:
                    assert owners["old"] is not None
                    assert owners["successor"] is not None
                    assert owners["old"] != owners["successor"]
                with reap_lock:
                    assert reaped == [owners["old"]]
                with ui._agent_children_lock:
                    assert old_child not in ui._agent_children
                    assert ui._agent_children.get(successor_child) == call_id
            finally:
                release_old.set()
                release_successor.set()
                for thread in threads:
                    thread.join(2)

        assert all(not thread.is_alive() for thread in threads)
        assert len(old_outcomes) == 1
        assert isinstance(old_outcomes[0], tuple)
        assert old_outcomes[0][0] == call_id
        assert "UNKNOWN" in old_outcomes[0][1]
        assert successor_outcomes == [(call_id, "fresh result")]
        with reap_lock:
            assert reaped == [owners["old"], owners["successor"]]
        with ui._agent_children_lock:
            assert ui._agent_children == {}

    def test_cancel_after_last_tool_never_dispatches_forced_synthesis(self, tmp_db):
        session = _make_session()
        session.agent_max_turns = 1

        def tool_call_stream():
            yield StreamChunk(
                tool_call_deltas=[ToolCallDelta(index=0, id="child-1", name="read_file")]
            )
            yield StreamChunk(
                tool_call_deltas=[ToolCallDelta(index=0, arguments_delta='{"path":"x"}')],
                finish_reason="tool_calls",
            )

        provider = arm_session(session, tool_call_stream())

        def execute(_prepared):
            session.cancel()
            return "child-1", "known tool result"

        prepared = {
            "call_id": "child-1",
            "func_name": "read_file",
            "needs_approval": False,
            "execute": execute,
        }
        with (
            patch.object(session, "_prepare_tool", return_value=prepared),
            pytest.raises(GenerationCancelled),
        ):
            session._run_agent(
                [Turn.user("go")],
                label="task",
                tools=[{"type": "function", "function": {"name": "read_file"}}],
                auto_tools={"read_file"},
            )

        assert provider.create_streaming.call_count == 1

    def test_cancel_during_agent_web_fetch_skips_extraction_after_successor_claim(self, tmp_db):
        session = _make_session()
        session.agent_max_turns = 1

        def web_fetch_call():
            yield StreamChunk(
                tool_call_deltas=[ToolCallDelta(index=0, id="web-1", name="web_fetch")]
            )
            yield StreamChunk(
                tool_call_deltas=[
                    ToolCallDelta(
                        index=0,
                        arguments_delta='{"url":"https://example.com","question":"what?"}',
                    )
                ],
                finish_reason="tool_calls",
            )

        provider = arm_session(session, web_fetch_call())
        response = MagicMock()
        response.headers = {"content-type": "text/plain"}
        response.text = "page body"
        response.content = response.text.encode()

        def fetch(*_args, **_kwargs):
            session.cancel()
            session._claim_generation()
            return response

        utility = MagicMock()
        with (
            patch.object(
                session,
                "_prepare_tool",
                return_value={
                    "call_id": "web-1",
                    "func_name": "web_fetch",
                    "needs_approval": False,
                    "execute": session._exec_web_fetch,
                    "url": "https://example.com",
                    "question": "what?",
                    "allow_private_origin": False,
                },
            ),
            patch("turnstone.core.session.fetch_with_ssrf_guard", side_effect=fetch),
            patch.object(session, "_utility_completion", utility),
            pytest.raises(GenerationCancelled),
        ):
            session._run_agent(
                [Turn.user("go")],
                label="task",
                tools=[{"type": "function", "function": {"name": "web_fetch"}}],
                auto_tools={"web_fetch"},
            )

        response.raise_for_status.assert_called_once()
        utility.assert_not_called()
        assert provider.create_streaming.call_count == 1

    def test_agent_web_fetch_abort_is_control_flow_not_extraction_error(self, tmp_db):
        session = _make_session()
        session.agent_max_turns = 1

        def web_fetch_call():
            yield StreamChunk(
                tool_call_deltas=[ToolCallDelta(index=0, id="web-1", name="web_fetch")]
            )
            yield StreamChunk(
                tool_call_deltas=[ToolCallDelta(index=0, arguments_delta='{"url":"https://x"}')],
                finish_reason="tool_calls",
            )

        provider = arm_session(session, web_fetch_call())
        response = MagicMock()
        response.headers = {"content-type": "text/plain"}
        response.text = "page body"
        response.content = response.text.encode()
        seen_refs = []

        def cancelled_utility(*_args, **kwargs):
            seen_refs.append(kwargs.get("cancel_ref"))
            session.cancel()
            raise ConnectionError("stream closed")

        with (
            patch.object(
                session,
                "_prepare_tool",
                return_value={
                    "call_id": "web-1",
                    "func_name": "web_fetch",
                    "needs_approval": False,
                    "execute": session._exec_web_fetch,
                    "url": "https://x",
                    "question": "summarize",
                    "allow_private_origin": False,
                },
            ),
            patch("turnstone.core.session.fetch_with_ssrf_guard", return_value=response),
            patch.object(session, "_utility_completion", side_effect=cancelled_utility),
            pytest.raises(GenerationCancelled),
        ):
            session._run_agent(
                [Turn.user("go")],
                label="task",
                tools=[{"type": "function", "function": {"name": "web_fetch"}}],
                auto_tools={"web_fetch"},
            )

        assert len(seen_refs) == 1
        assert seen_refs[0] is not None
        assert seen_refs[0].aborted
        assert provider.create_streaming.call_count == 1

    def test_stale_or_closed_agent_is_refused_before_auth_and_dispatch(self, tmp_db):
        session = _make_session()
        provider = arm_session(
            session,
            [StreamChunk(content_delta="must not run", finish_reason="stop")],
        )
        old_generation = session._claim_generation()
        old_event = session._cancel_event
        session.cancel()
        session._claim_generation()
        auth = MagicMock(return_value=None)

        with (
            patch.object(session, "_model_backend_auth_token_for_principal", auth),
            pytest.raises(GenerationCancelled),
        ):
            session._run_agent(
                [Turn.user("old")],
                origin_cancel_event=old_event,
                origin_generation=old_generation,
            )

        auth.assert_not_called()
        provider.create_streaming.assert_not_called()

        session.close()
        with (
            patch.object(session, "_model_backend_auth_token_for_principal", auth),
            pytest.raises(GenerationCancelled),
        ):
            session._run_agent([Turn.user("after close")])

        auth.assert_not_called()
        provider.create_streaming.assert_not_called()
        assert session._parallel_model_cancel_scopes == {}

    def test_completed_agent_unregisters_before_later_stop(self, tmp_db):
        session = _make_session()
        provider = arm_session(
            session,
            [StreamChunk(content_delta="done", finish_reason="stop")],
        )

        assert session._run_agent([Turn.user("go")], label="task") == "done"
        assert session._parallel_model_cancel_scopes == {}
        completed_handle = provider.handles[0]
        assert not completed_handle.closed

        session.cancel()

        assert not completed_handle.closed

    def test_close_refuses_task_publication_but_preserves_resource_teardown(self, tmp_db):
        """Close is terminal for publications, not for invocation cleanup."""
        ui = SessionUIBase(ws_id="ws-close-task", user_id="user-close-task")
        session = _make_session(ui=ui)
        listener = ui._register_listener()
        generation = session._claim_generation()
        generation_event = session._cancel_event
        parent_call_id = "parent-task"
        execute_entered = threading.Event()
        release_execute = threading.Event()
        outcomes: list[object] = []
        child_ids: list[str] = []
        shell_owners: list[str | None] = []
        read_path = "/close-owned-agent-read"

        provider = arm_session(
            session,
            [
                StreamChunk(
                    tool_call_deltas=[ToolCallDelta(index=0, id="provider-child", name="read_file")]
                ),
                StreamChunk(
                    tool_call_deltas=[ToolCallDelta(index=0, arguments_delta='{"path":"ignored"}')],
                    finish_reason="tool_calls",
                ),
            ],
        )

        def blocked_execute(item):
            child_ids.append(item["call_id"])
            shell_owners.append(_active_shell_owner.get())
            session._current_read_files.add(read_path)
            execute_entered.set()
            if not release_execute.wait(2):
                raise RuntimeError("task tool was not released")
            return item["call_id"], "late child result"

        def prepare_tool(tc):
            return {
                "call_id": tc["id"],
                "func_name": "read_file",
                "needs_approval": False,
                "execute": blocked_execute,
            }

        item = {
            "call_id": parent_call_id,
            "prompt": "perform one read",
            "_origin_cancel_event": generation_event,
            "_origin_generation": generation,
        }
        auth_impl = session._model_backend_auth_token_for_principal
        reap_impl = session._background_shells.reap
        with (
            patch.object(session, "_prepare_tool", side_effect=prepare_tool),
            patch.object(
                session,
                "_model_backend_auth_token_for_principal",
                wraps=auth_impl,
            ) as auth,
            patch.object(
                session, "_report_tool_result", wraps=session._report_tool_result
            ) as report,
            patch.object(session._background_shells, "reap", wraps=reap_impl) as reap,
        ):
            thread = self._start_task(session, item, outcomes)
            try:
                assert execute_entered.wait(2)
                assert len(child_ids) == 1
                issued_child_id = child_ids[0]
                assert issued_child_id.startswith(f"{parent_call_id}::")
                survivor_child_id = f"{parent_call_id}::survivor"
                ui.note_agent_child(survivor_child_id, parent_call_id)
                with ui._agent_children_lock:
                    assert ui._agent_children == {
                        issued_child_id: parent_call_id,
                        survivor_child_id: parent_call_id,
                    }
                assert session._parallel_model_cancel_scopes
                assert provider.create_streaming.call_count == 1
                assert auth.call_count == 1

                # Discard the pre-close child-pending event.  Any event left
                # after unwind would be a forbidden post-close publication.
                while not listener.empty():
                    listener.get_nowait()

                session.close()
                assert provider.handles[0].closed
                assert session._publication_shutdown
                assert session._cancel_event is generation_event
                assert generation_event.is_set()

                provider_calls_at_close = provider.create_streaming.call_count
                auth_calls_at_close = auth.call_count
                with pytest.raises(RuntimeError, match="closed session"):
                    session._claim_generation()
                with pytest.raises(GenerationCancelled):
                    session._run_agent([Turn.user("must not dispatch")], label="task")
                assert provider.create_streaming.call_count == provider_calls_at_close
                assert auth.call_count == auth_calls_at_close
            finally:
                release_execute.set()
                session.close()
                thread.join(2)

        assert not thread.is_alive()
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], tuple)
        assert outcomes[0][0] == parent_call_id
        # The child executor returned a result after close released it.  The
        # recursive ledger therefore records an observed completion, even
        # though the terminal publication fence correctly discards it.  UNKNOWN
        # is reserved for an issued child whose result was never observed.
        disposition = outcomes[0][1]
        assert isinstance(disposition, str)
        assert "Completed before cancel: read_file." in disposition
        assert "UNKNOWN" not in disposition
        report.assert_not_called()
        assert parent_call_id not in session._cancelled_tool_results
        assert read_path not in session._read_files
        assert parent_call_id not in ui._agent_trajectories
        assert listener.empty()
        assert shell_owners[0] is not None
        reap.assert_called_once_with(owner=shell_owners[0])
        with ui._agent_children_lock:
            assert ui._agent_children == {survivor_child_id: parent_call_id}
        assert session._parallel_model_cancel_shutdown
        assert session._parallel_model_cancel_scopes == {}
        assert session._cancel_event is generation_event
        assert generation_event.is_set()

    def test_close_aborts_live_agent_and_latches_future_registration(self, tmp_db):
        session = _make_session()
        stream = _BlockingAgentStream()
        provider = self._install_blocking_provider(session, [stream])
        outcomes = []
        thread = self._start_agent(session, [Turn.user("go")], outcomes)

        try:
            assert stream.read_started.wait(2)
            session.close()
        finally:
            session.close()
            thread.join(2)

        assert not thread.is_alive()
        assert stream.closed.is_set()
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], GenerationCancelled)
        assert provider.create_streaming.call_count == 1
        assert session._parallel_model_cancel_shutdown
        assert session._parallel_model_cancel_scopes == {}

    def test_cancelled_task_preserves_answered_unknown_child_effect(self, tmp_db):
        """A returned UNKNOWN child result remains unresolved in the parent ledger."""
        session = _make_session()
        session._task_tools = [
            {"type": "function", "function": {"name": "read_file", "parameters": {}}}
        ]
        generation = session._claim_generation()
        generation_event = session._cancel_event
        parent_call_id = "task-with-unknown-child"
        issued_child_ids: list[str] = []
        stashed_steps: list[dict[str, object]] = []
        outcomes: list[object] = []
        second_request = _BlockingAgentStream()

        first_request = [
            StreamChunk(
                tool_call_deltas=[ToolCallDelta(index=0, id="provider-child", name="read_file")]
            ),
            StreamChunk(
                tool_call_deltas=[ToolCallDelta(index=0, arguments_delta='{"path":"ignored"}')],
                finish_reason="tool_calls",
            ),
        ]
        provider = provider_shell()
        first_handle = MagicMock()
        requests = [iter(first_request), second_request]

        def create_streaming(**kwargs):
            stream = requests.pop(0)
            cancel_ref = kwargs["cancel_ref"]
            cancel_ref.append(first_handle if stream is not second_request else second_request)
            return stream

        provider.create_streaming = MagicMock(side_effect=create_streaming)
        replace_session_lane(session, provider=provider)

        def execute_child(item):
            child_id = item["call_id"]
            issued_child_ids.append(child_id)
            session._report_tool_result(
                child_id,
                "read_file",
                "Timed out. Outcome UNKNOWN; reconcile before retrying.",
                is_error=True,
                status=EffectStatus.UNKNOWN,
            )
            return child_id, "Timed out. Outcome UNKNOWN; reconcile before retrying."

        def prepare_child(tc):
            return {
                "call_id": tc["id"],
                "func_name": "read_file",
                "needs_approval": False,
                "execute": execute_child,
            }

        item = {
            "call_id": parent_call_id,
            "prompt": "inspect the file",
            "_origin_cancel_event": generation_event,
            "_origin_generation": generation,
        }
        with (
            patch.object(session, "_prepare_tool", side_effect=prepare_child),
            patch.object(
                session,
                "_stash_agent_steps",
                side_effect=lambda _call_id, steps: stashed_steps.extend(steps),
            ),
        ):
            thread = self._start_task(session, item, outcomes)
            try:
                assert second_request.read_started.wait(2)
                session.cancel()
            finally:
                session.cancel()
                thread.join(2)

        assert not thread.is_alive()
        assert provider.create_streaming.call_count == 2
        assert second_request.closed.is_set()
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], tuple)
        assert outcomes[0][0] == parent_call_id
        disposition = outcomes[0][1]
        assert isinstance(disposition, str)
        assert "Results received with UNKNOWN effects before cancel: read_file." in disposition
        assert "Completed before cancel: read_file." not in disposition

        assert len(stashed_steps) == 1
        assert stashed_steps[0]["id"] == issued_child_ids[0]
        assert stashed_steps[0]["is_error"] is True
        assert "Outcome UNKNOWN" in str(stashed_steps[0]["output"])
        assert issued_child_ids[0] not in session._tool_status
        assert session._cancelled_tool_results[parent_call_id] == _CancelledToolResult(
            detail=disposition,
            effect_status=EffectStatus.UNKNOWN,
            is_error=True,
            preview=None,
            live_emitted=True,
        )


class TestToolResultGenerationPublication:
    """Abandoned generic workers cannot publish into a successor generation."""

    def test_stale_open_preview_cannot_repopulate_reused_call_id(self, tmp_db, tmp_path):
        ui = NullUI()
        ui.on_tool_result = MagicMock()
        session = _make_session(ui=ui)
        old_generation = session._claim_generation()
        call_id = "provider-reused-id"
        preview_path = tmp_path / "late.md"
        preview_path.write_text("# late preview\n", encoding="utf-8")
        execute_entered = threading.Event()
        release_execute = threading.Event()
        old_outcomes: list[object] = []

        def stale_execute(item):
            execute_entered.set()
            if not release_execute.wait(2):
                raise RuntimeError("stale tool was not released")
            result = session._exec_open_preview(item)
            session._report_tool_result(
                call_id,
                "open_preview",
                "stale failure",
                is_error=True,
                status=EffectStatus.UNKNOWN,
            )
            return result

        stale_item = {
            "call_id": call_id,
            "func_name": "open_preview",
            "needs_approval": False,
            "execute": stale_execute,
            "target_kind": "path",
            "path": str(preview_path),
        }

        def fresh_execute(_item):
            session._report_tool_result(call_id, "read_file", "fresh result")
            return call_id, "fresh result"

        fresh_item = {
            "call_id": call_id,
            "func_name": "read_file",
            "needs_approval": False,
            "execute": fresh_execute,
        }

        def prepare(tc):
            return stale_item if tc["_test_generation"] == "old" else fresh_item

        def run_old():
            try:
                old_outcomes.append(
                    session._execute_tools(
                        [
                            {
                                "id": call_id,
                                "_test_generation": "old",
                                "function": {"name": "open_preview", "arguments": "{}"},
                            }
                        ],
                        my_generation=old_generation,
                    )
                )
            except BaseException as exc:
                old_outcomes.append(exc)

        with patch.object(session, "_safe_prepare_tool", side_effect=prepare):
            worker = threading.Thread(target=run_old)
            worker.start()
            try:
                assert execute_entered.wait(2)
                session.cancel()
                successor_generation = session._claim_generation()
                successor_result = session._execute_tools(
                    [
                        {
                            "id": call_id,
                            "_test_generation": "successor",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }
                    ],
                    my_generation=successor_generation,
                )
                release_execute.set()
            finally:
                release_execute.set()
                worker.join(2)

        assert not worker.is_alive()
        assert len(old_outcomes) == 1
        assert not isinstance(old_outcomes[0], BaseException)
        assert successor_result == ([(call_id, "fresh result")], None)
        ui.on_tool_result.assert_called_once_with(
            call_id,
            "read_file",
            "fresh result",
            is_error=False,
            preview=None,
        )
        assert call_id not in session._tool_error_flags
        assert call_id not in session._tool_status
        assert call_id not in session._tool_previews


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

    def test_cancel_only_handle_is_closeable_without_arming_attempt(self, tmp_db):
        session = _make_session()
        fired: list[int] = []
        ref = _CancelRef(session, on_first_append=lambda: fired.append(1))
        handle = MagicMock()

        ref.register_cancel_handle(handle)

        assert session._cancel_stream is handle
        assert ref == []
        assert not ref.armed
        assert fired == []
        session.cancel()
        handle.close.assert_called_once_with()

        ref.unregister_cancel_handle(handle)
        assert session._cancel_stream is None

    def test_superseded_cancel_only_handle_cannot_replace_successor(self, tmp_db):
        session = _make_session()
        session._generation = 5
        successor = MagicMock()
        session._cancel_stream = successor
        zombie = MagicMock()

        ref = _CancelRef(session, 4)
        ref.register_cancel_handle(zombie)

        assert session._cancel_stream is successor
        assert not ref.armed
        zombie.close.assert_called_once_with()
        successor.close.assert_not_called()

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
        session = _make_registered_session(ui=ui)

        arm_session(session, iter([StreamChunk(content_delta="hi", finish_reason="stop")]))
        session.send("test")

        # During the stream the armed handle was registered; after send()
        # completes the finally block cleared it.
        assert session._cancel_stream is None

    def test_late_old_append_cannot_replace_successor_stream(self, tmp_db):
        """Registration and successor claim have one generation ordering.

        Pause the old ref immediately before it enters the generation lock,
        let a force successor claim the session and install its own stream,
        then release the old append.  The late handle must refuse registration
        and close itself instead of overwriting the successor-owned slot.
        """
        session = _make_session()
        old_generation = session._claim_generation()
        old_ref = _CancelRef(session, old_generation)
        late_old_stream = MagicMock()
        successor_stream = MagicMock()
        append_errors: list[BaseException] = []
        generation_lock = _GatedRLock(session._generation_lock)
        session._generation_lock = generation_lock

        def append_old() -> None:
            try:
                old_ref.append(late_old_stream)
            except BaseException as exc:
                append_errors.append(exc)

        appender = threading.Thread(target=append_old)
        generation_lock.watch(appender)
        appender.start()
        try:
            assert generation_lock.waiting.wait(2)
            session.cancel()
            successor_generation = session._claim_generation()
            with session._generation_lock:
                session._cancel_stream = successor_stream
            generation_lock.release.set()
        finally:
            generation_lock.release.set()
            appender.join(2)

        assert not appender.is_alive()
        assert append_errors == []
        assert successor_generation == old_generation + 1
        assert session._cancel_stream is successor_stream
        successor_stream.close.assert_not_called()
        late_old_stream.close.assert_called_once_with()
        assert not old_ref.armed

    def test_direct_close_unblocks_registered_main_stream_and_refuses_late_handle(
        self,
        tmp_db,
    ) -> None:
        """Close aborts the foreground SDK read and latches future arrivals."""
        session = _make_registered_session()
        blocking_stream = _BlockingAgentStream()
        provider = provider_shell()

        def create_streaming(**kwargs):
            kwargs["cancel_ref"].append(blocking_stream)
            return blocking_stream

        provider.create_streaming = MagicMock(side_effect=create_streaming)
        replace_session_lane(session, provider=provider)
        session._title_generated = True
        send_errors: list[BaseException] = []

        def send() -> None:
            try:
                session.send("wait on the main stream")
            except BaseException as exc:
                send_errors.append(exc)

        worker = threading.Thread(target=send)
        worker.start()
        try:
            assert blocking_stream.read_started.wait(2)
            assert session._cancel_stream is blocking_stream
            session.close()
        finally:
            session.close()
            worker.join(2)

        assert not worker.is_alive()
        assert send_errors == []
        assert blocking_stream.closed.is_set()
        assert provider.create_streaming.call_count == 1
        assert session._cancel_stream is None

        late_stream = MagicMock()
        _CancelRef(session, session._generation).append(late_stream)

        late_stream.close.assert_called_once_with()
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


class TestStreamTurnConsumerPublicationRail:
    """Every main-stream display/fold callback revalidates ownership."""

    @pytest.mark.parametrize("retirement", ["successor", "close"])
    def test_retired_consumer_refuses_chunk_finish_and_partial_publication(
        self,
        tmp_db,
        retirement: str,
    ) -> None:
        """A successor or terminal close makes all old consumer tails inert."""
        ui = _StreamRecordingUI()
        session = _make_session(ui=ui)
        old_generation = session._claim_generation()
        consumer = _StreamTurnConsumer(session, old_generation)

        # Prime every terminal path without emitting anything yet.  If either
        # finish/cancel publication bypassed the ownership fence, these carries
        # would immediately surface as content and a stream_end event.
        consumer._content_parts.append("old partial ")
        consumer._boundary_carry = "carry "
        consumer._splitter.pending = "tail"
        consumer._trailing_info.append("old citation")
        usage_sentinel = {"prompt_tokens": 97, "completion_tokens": 11}
        partial_sentinel = {"role": "assistant", "content": "successor partial"}
        session._last_usage = usage_sentinel
        session._cancelled_partial_msg = partial_sentinel

        if retirement == "successor":
            session.cancel()
            session._claim_generation()
        else:
            session.close()

        late_chunk = StreamChunk(
            content_delta="late content",
            reasoning_delta="late reasoning",
            info_delta="late info",
            usage=UsageInfo(
                prompt_tokens=101,
                completion_tokens=13,
                total_tokens=114,
            ),
        )
        with pytest.raises(GenerationCancelled):
            consumer(late_chunk)
        with pytest.raises(GenerationCancelled):
            consumer.finish_stream()
        consumer.record_cancelled_partial()

        assert ui.content_tokens == []
        assert ui.reasoning_tokens == []
        assert ui.infos == []
        assert ui.stream_ends == 0
        assert consumer._usage_acc is None
        assert session._last_usage is usage_sentinel
        assert session._cancelled_partial_msg is partial_sentinel

    def test_same_generation_stop_still_records_cancelled_partial(self, tmp_db) -> None:
        """Stop suppresses later chunks but preserves content already received."""
        ui = _StreamRecordingUI()
        session = _make_session(ui=ui)
        generation = session._claim_generation()
        consumer = _StreamTurnConsumer(session, generation)

        consumer(StreamChunk(content_delta="partial answer"))
        session.cancel()
        consumer.record_cancelled_partial()

        assert session._generation == generation
        assert session._cancelled_partial_msg == {
            "role": "assistant",
            "content": "partial answer",
        }
        assert "".join(ui.content_tokens) == "partial answer"
        assert ui.stream_ends == 1


class TestForceCancelOrphanNoReissue:
    """A force-cancelled generation's mid-stream death is never re-issued
    on the orphan's behalf.  A gen-0 ref would read ``aborted`` False
    after a force-cancel (the successor installs a fresh unset event and
    gen 0 never reads superseded), and the retry machinery would spend
    tokens for a generation nobody owns."""

    def test_orphan_death_not_reissued_no_ui_finalize(self, tmp_db):
        ui = NullUI()
        session = _make_registered_session(ui=ui)

        def dying_orphan_stream():
            yield StreamChunk(content_delta="old ")
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
        session = _make_registered_session(ui=ui)

        original_event = session._cancel_event

        arm_session(session, iter([StreamChunk(content_delta="hi", finish_reason="stop")]))
        session.send("test")

        # After send() completes, _cancel_event should be a NEW Event
        # (not the same object as before the call).
        assert session._cancel_event is not original_event
        assert not session._cancel_event.is_set()


class TestSendGenerationInitializationPublication:
    """The claimed generation owns every pre-stream send mutation."""

    @pytest.mark.parametrize("takeover", ["successor", "close"])
    def test_owner_lost_during_memory_pointer_plan_cannot_publish(
        self,
        tmp_db,
        takeover: str,
    ) -> None:
        """Storage-backed pointer planning is inert until its owner commits."""
        session = _make_session()
        _bind_storage_mock()
        session._title_generated = True
        generation = session._claim_generation()
        planning_started = threading.Event()
        release_planning = threading.Event()
        errors: list[BaseException] = []

        session._metacog_state["reflection"] = 123.0
        session._nudge_queue.enqueue("successor", "keep this advisory", "user")
        prior_metacog = dict(session._metacog_state)
        prior_nudges = tuple(session._nudge_queue.pending())

        def blocked_pointer_plan(*_args: Any, **_kwargs: Any) -> str:
            planning_started.set()
            if not release_planning.wait(2):
                raise RuntimeError("test memory pointer plan was not released")
            return "stale private pointer"

        def initialize() -> None:
            try:
                session._initialize_send_generation(
                    my_generation=generation,
                    user_input="stale predecessor request",
                    attachments=None,
                    send_id="stale-send",
                    from_wake=False,
                    turn_principal_id="stale-principal",
                    wire_part_cache={},
                )
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=initialize)
        with (
            patch.object(session, "_plan_memory_pointer", side_effect=blocked_pointer_plan),
            patch.object(
                session,
                "_plan_metacognitive_nudge",
                return_value=("reflection", "stale advisory"),
            ) as plan_nudge,
            patch("turnstone.core.session.save_message") as save,
        ):
            worker.start()
            try:
                assert planning_started.wait(2)
                if takeover == "successor":
                    assert session._claim_generation() == generation + 1
                else:
                    session.close()
            finally:
                release_planning.set()
                worker.join(2)

        assert not worker.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], GenerationCancelled)
        assert session._metacog_state == prior_metacog
        assert tuple(session._nudge_queue.pending()) == prior_nudges
        plan_nudge.assert_not_called()
        save.assert_not_called()

    def test_stop_does_not_wait_for_blocked_user_turn_storage(self, tmp_db) -> None:
        """Durable opening-turn storage cannot delay provider cancellation."""
        session = _make_session()
        storage = _bind_storage_mock()
        session._title_generated = True
        generation = session._claim_generation()
        storage_started = threading.Event()
        release_storage = threading.Event()
        stop_started = threading.Event()
        stop_returned = threading.Event()
        main_closed = threading.Event()
        child_closed = threading.Event()
        publication_errors: list[BaseException] = []
        stop_errors: list[BaseException] = []

        main_handle = MagicMock()
        child_handle = MagicMock()
        main_handle.close.side_effect = main_closed.set
        child_handle.close.side_effect = child_closed.set

        def blocked_save_message(*_args: Any, **_kwargs: Any) -> int:
            storage_started.set()
            if not release_storage.wait(5):
                raise RuntimeError("test user-turn storage was not released")
            return 1

        def publish_opening_turn() -> None:
            try:
                session._initialize_send_generation(
                    my_generation=generation,
                    user_input="persist this opening turn",
                    attachments=None,
                    send_id="blocked-send",
                    from_wake=False,
                    turn_principal_id="turn-principal",
                    wire_part_cache={},
                )
            except BaseException as exc:
                publication_errors.append(exc)

        def request_stop() -> None:
            stop_started.set()
            try:
                session.cancel()
            except BaseException as exc:
                stop_errors.append(exc)
            finally:
                stop_returned.set()

        publisher = threading.Thread(target=publish_opening_turn)
        canceller = threading.Thread(target=request_stop)
        with session._registered_parallel_model_cancel_scope(
            session._cancel_event,
            generation,
        ) as child_scope:
            child_scope.cancel_ref.append(child_handle)
            _CancelRef(session, generation).append(main_handle)
            with (
                patch.object(
                    storage,
                    "save_message",
                    side_effect=blocked_save_message,
                ) as save,
                patch.object(session, "_check_metacognitive_nudge", return_value=None),
                patch.object(session, "_maybe_note_new_participant"),
                patch.object(session, "_emit_pending_user_nudges"),
            ):
                try:
                    publisher.start()
                    assert storage_started.wait(2)
                    canceller.start()
                    assert stop_started.wait(2)
                    stop_returned_while_blocked = stop_returned.wait(1)
                    main_closed_while_blocked = main_closed.is_set()
                    child_closed_while_blocked = child_closed.is_set()
                finally:
                    release_storage.set()
                    publisher.join(2)
                    if canceller.ident is not None:
                        canceller.join(2)

        assert stop_returned_while_blocked, "Stop waited for user-turn storage"
        assert main_closed_while_blocked
        assert child_closed_while_blocked
        assert not publisher.is_alive()
        assert not canceller.is_alive()
        assert stop_errors == []
        assert all(isinstance(exc, GenerationCancelled) for exc in publication_errors)
        save.assert_called_once()

    @pytest.mark.parametrize("takeover", ["successor", "close"])
    def test_lost_owner_cannot_publish_any_initialization_state(
        self,
        tmp_db,
        takeover: str,
    ) -> None:
        session = _make_session()
        _bind_storage_mock()
        origin_generation = session._claim_generation()

        if takeover == "successor":
            assert session._claim_generation() == origin_generation + 1
        else:
            session.close()

        # These values represent state already owned by the successor (or the
        # terminal closed session).  A delayed predecessor must not clear or
        # replace any of them when it reaches post-claim initialization.
        prior_messages = tuple(session.messages)
        prior_cache = {("successor", (False, False, False)): {"content": "live"}}
        prior_partial = {"role": "assistant", "content": "successor partial"}
        session._notify_count = 7
        session._generation_abandoned = True
        session._compaction_advised = True
        session._cancelled_partial_msg = prior_partial
        session._wire_part_cache = prior_cache
        session._metacog_state["reflection"] = 123.0
        prior_metacog = dict(session._metacog_state)
        session._nudge_queue.enqueue("successor", "keep this advisory", "user")
        prior_nudges = tuple(session._nudge_queue.pending())

        with (
            patch("turnstone.core.session.save_message") as save,
            patch("turnstone.core.session.threading.Thread") as title_thread,
            patch.object(
                session,
                "_check_metacognitive_nudge",
                return_value=("reflection", "stale advisory"),
            ) as check_metacog,
            patch.object(session, "_init_system_messages") as init_system,
            pytest.raises(GenerationCancelled),
        ):
            session._initialize_send_generation(
                my_generation=origin_generation,
                user_input="stale predecessor request",
                attachments=None,
                send_id="stale-send",
                from_wake=False,
                turn_principal_id="stale-principal",
                wire_part_cache={},
            )

        assert tuple(session.messages) == prior_messages
        assert session._notify_count == 7
        assert session._generation_abandoned is True
        assert session._compaction_advised is True
        assert session._cancelled_partial_msg is prior_partial
        assert session._wire_part_cache is prior_cache
        assert session._metacog_state == prior_metacog
        assert tuple(session._nudge_queue.pending()) == prior_nudges
        assert origin_generation not in session._generation_principals
        save.assert_not_called()
        title_thread.assert_not_called()
        check_metacog.assert_not_called()
        init_system.assert_not_called()

    def test_stale_admission_cannot_commit_or_publish_private_memory_index(
        self,
        tmp_db,
    ) -> None:
        """A superseded capture rolls back before its durable commit."""
        session = _make_session()
        storage = _bind_storage_mock()
        storage.get_memory_index_snapshot.return_value = None
        old_generation = session._claim_generation()
        session._memory_index_admission_generation = old_generation
        old_capture_started = threading.Event()
        release_old_capture = threading.Event()
        committed_principals: list[str] = []
        errors: list[BaseException] = []

        def capture_snapshot(
            _ws_id: str,
            principal_id: str,
            *,
            commit_context: Any,
        ) -> dict[str, Any]:
            if principal_id == "old-private-user":
                old_capture_started.set()
                if not release_old_capture.wait(2):
                    raise RuntimeError("test old index capture was not released")
                content = "<memory-index>old_private_memory</memory-index>"
            else:
                assert principal_id == "successor-user"
                content = "<memory-index>successor_memory</memory-index>"
            candidate = {
                "content": content,
                "principal_id": principal_id,
                "entry_count": 1,
                "char_count": len(content),
                "invalid_description_count": 0,
                "project_id": "",
                "project_name": "",
            }
            with commit_context(candidate):
                committed_principals.append(principal_id)
            return candidate

        def admit_old() -> None:
            try:
                session._admit_memory_index_request(
                    session._primary_lane(),
                    my_generation=old_generation,
                    principal_id="old-private-user",
                )
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=admit_old)
        with patch.object(
            storage,
            "acquire_memory_index_snapshot",
            side_effect=capture_snapshot,
        ):
            worker.start()
            try:
                assert old_capture_started.wait(2)
                successor_generation = session._claim_generation()
                session._memory_index_admission_generation = successor_generation
                session._admit_memory_index_request(
                    session._primary_lane(),
                    my_generation=successor_generation,
                    principal_id="successor-user",
                )
                successor_wire = list(session.system_messages)
            finally:
                release_old_capture.set()
                worker.join(2)

        assert not worker.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], GenerationCancelled)
        assert committed_principals == ["successor-user"]
        assert "successor_memory" in str(successor_wire)
        rendered = "\n".join(str(message.get("content", "")) for message in session.system_messages)
        assert "successor_memory" in rendered
        assert "old_private_memory" not in rendered

    def test_deferred_title_launch_keeps_pre_rebind_identity_and_history(
        self,
        tmp_db,
    ) -> None:
        """A resume during the user save cannot retarget deferred title work."""
        session = _make_session(ws_id="opening-ws", user_id="opening-principal")
        storage = _bind_storage_mock()
        storage.get_workstream.return_value = None
        storage.ensure_workstream_incarnation_snapshot.return_value = None
        generation = session._claim_generation()
        successor_turn = turn_from_dict(
            {"role": "user", "content": "successor workstream history"},
        )

        def save_then_resume(*args: Any, **_kwargs: Any) -> int:
            assert args[:3] == ("opening-ws", "user", "original opening message")
            assert session.resume("resumed-ws") is True
            return 1

        with (
            patch.object(session, "_nudges_enabled", return_value=False),
            patch.object(
                session,
                "_plan_shared_state",
                return_value=("opening-ws", {"opening-principal"}, True),
            ),
            patch.object(session, "_init_system_messages") as init_system,
            patch("turnstone.core.session.load_message_turns", return_value=[successor_turn]),
            patch("turnstone.core.session.load_workstream_config", return_value={}),
            patch("turnstone.core.session.save_message", side_effect=save_then_resume),
            patch("turnstone.core.session.threading.Thread") as title_thread,
        ):
            session._initialize_send_generation(
                my_generation=generation,
                user_input="original opening message",
                attachments=None,
                send_id="opening-send",
                from_wake=False,
                turn_principal_id="opening-principal",
                wire_part_cache={},
            )

        assert session.ws_id == "resumed-ws"
        assert tuple(turn.text for turn in session.messages) == ("successor workstream history",)
        init_system.assert_called_once_with()
        title_thread.assert_called_once()
        title_call = title_thread.call_args
        assert title_call.kwargs["target"] == session._generate_title
        assert title_call.kwargs["kwargs"]["principal_id"] == "opening-principal"
        assert title_call.kwargs["kwargs"]["captured_ws_id"] == "opening-ws"
        assert title_call.kwargs["kwargs"]["origin_generation"] == generation
        assert tuple(turn.text for turn in title_call.kwargs["kwargs"]["captured_messages"]) == (
            "original opening message",
        )
        title_thread.return_value.start.assert_called_once_with()

    def test_close_refuses_title_launch_delayed_behind_opening_storage(
        self,
        tmp_db,
    ) -> None:
        """A durable user row cannot launch auxiliary work past close."""
        session = _make_session(ws_id="opening-ws", user_id="opening-principal")
        _bind_storage_mock()
        generation = session._claim_generation()
        save_started = threading.Event()
        release_save = threading.Event()
        errors: list[BaseException] = []

        def blocked_save(*_args: Any, **_kwargs: Any) -> int:
            save_started.set()
            if not release_save.wait(2):
                raise RuntimeError("test opening storage was not released")
            return 1

        def initialize() -> None:
            try:
                session._initialize_send_generation(
                    my_generation=generation,
                    user_input="original opening message",
                    attachments=None,
                    send_id="opening-send",
                    from_wake=False,
                    turn_principal_id="opening-principal",
                    wire_part_cache={},
                )
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=initialize)
        with (
            patch.object(session, "_nudges_enabled", return_value=False),
            patch("turnstone.core.session.save_message", side_effect=blocked_save),
            patch.object(session, "_generate_title") as generate_title,
        ):
            worker.start()
            try:
                assert save_started.wait(2)
                session.close()
            finally:
                release_save.set()
                worker.join(2)

        assert not worker.is_alive()
        assert errors == []
        generate_title.assert_not_called()
        assert session._title_generated is False

    def test_shared_sender_read_failure_is_not_retried_under_generation_lock(
        self,
        tmp_db,
    ) -> None:
        """A failed sender seed remains retryable, but not in this commit."""
        session = _make_session(user_id="principal")
        session._title_generated = True
        session._db_senders_loaded = False
        session._senders_dirty = True
        generation = session._claim_generation()
        storage = _bind_storage_mock()
        lock_owned_during_reads: list[bool] = []

        def fail_sender_read(_ws_id: str) -> list[str]:
            is_owned = getattr(session._generation_lock, "_is_owned", lambda: False)
            lock_owned_during_reads.append(bool(is_owned()))
            raise RuntimeError("sender storage unavailable")

        storage.list_message_senders.side_effect = fail_sender_read
        with (
            patch.object(session, "_visible_memory_count", return_value=0),
            patch("turnstone.core.session.save_message", return_value=1),
        ):
            session._initialize_send_generation(
                my_generation=generation,
                user_input="opening message",
                attachments=None,
                send_id="opening-send",
                from_wake=False,
                turn_principal_id="principal",
                wire_part_cache={},
            )

        assert lock_owned_during_reads == [False]
        storage.list_message_senders.assert_called_once_with(session.ws_id)
        assert session._db_senders_loaded is False


class TestMainToolCancellationDisposition:
    """Unstarted main-loop tools are durably NONE, never guessed UNKNOWN."""

    @staticmethod
    def _prepared_item(call_id: str, execute) -> dict[str, Any]:
        return {
            "call_id": call_id,
            "func_name": "test_tool",
            "header": call_id,
            "preview": "",
            "needs_approval": False,
            "execute": execute,
        }

    def test_stop_at_phase_three_boundary_stages_none_for_every_unstarted_call(
        self,
        tmp_db,
    ) -> None:
        session = _make_session()
        generation = session._claim_generation()
        executes = {call_id: MagicMock() for call_id in ("call-a", "call-b")}
        items = [
            self._prepared_item(call_id, executes[call_id]) for call_id in ("call-a", "call-b")
        ]
        tool_calls = [
            {"id": call_id, "function": {"name": "test_tool", "arguments": "{}"}}
            for call_id in executes
        ]
        original_emit_state = session._emit_state

        def cancel_at_running(state: str, **kwargs) -> None:
            original_emit_state(state, **kwargs)
            if state == "running":
                session.cancel()

        with (
            patch.object(session, "_safe_prepare_tool", side_effect=items),
            patch.object(session, "_evaluate_intent", return_value=None),
            patch.object(session, "_emit_state", side_effect=cancel_at_running),
            pytest.raises(GenerationCancelled),
        ):
            session._execute_tools(tool_calls, my_generation=generation)

        for call_id, execute in executes.items():
            execute.assert_not_called()
            receipt = session._cancelled_tool_results[call_id]
            assert "no side effects" in receipt.detail
            assert receipt.effect_status is EffectStatus.NONE
            assert receipt.is_error is True
            assert receipt.live_emitted is False

    @pytest.mark.parametrize(
        "cancel_surface",
        ["inside_prepare", "after_prepare_return"],
    )
    def test_stop_during_prepare_stages_every_original_call_none(
        self,
        tmp_db,
        cancel_surface: str,
    ) -> None:
        """Preparation-time Stop closes every issued call as never started.

        Cover both ways the cooperative edge can surface: the blocked
        preparer observes Stop itself, or it returns and the batch-level
        checkpoint observes it.  The ledger is seeded from the provider's
        original call list, so even calls whose preparation never began get a
        truthful NONE disposition.
        """
        session = _make_session()
        generation = session._claim_generation()
        call_ids = ("call-a", "call-b")
        executors = {call_id: MagicMock() for call_id in call_ids}
        tool_calls = [
            {"id": call_id, "function": {"name": "test_tool", "arguments": "{}"}}
            for call_id in call_ids
        ]
        prepare_entered = threading.Event()
        release_prepare = threading.Event()
        prepared_ids: list[str] = []
        outcomes: list[BaseException | object] = []

        def prepare(tool_call: dict[str, Any]) -> dict[str, Any]:
            call_id = str(tool_call["id"])
            prepared_ids.append(call_id)
            if call_id == call_ids[0]:
                prepare_entered.set()
                if not release_prepare.wait(2):
                    raise RuntimeError("test preparer was not released")
                if cancel_surface == "inside_prepare":
                    session._check_cancelled(generation)
            return self._prepared_item(call_id, executors[call_id])

        def run_batch() -> None:
            try:
                outcomes.append(session._execute_tools(tool_calls, my_generation=generation))
            except BaseException as exc:
                outcomes.append(exc)

        with patch.object(session, "_safe_prepare_tool", side_effect=prepare):
            worker = threading.Thread(target=run_batch)
            worker.start()
            try:
                assert prepare_entered.wait(2)
                session.cancel()
                release_prepare.set()
                worker.join(2)
            finally:
                release_prepare.set()
                worker.join(2)

        assert not worker.is_alive()
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], GenerationCancelled)
        expected_prepared = [call_ids[0]] if cancel_surface == "inside_prepare" else list(call_ids)
        assert prepared_ids == expected_prepared
        for call_id, execute in executors.items():
            execute.assert_not_called()
            receipt = session._cancelled_tool_results[call_id]
            assert "no side effects" in receipt.detail
            assert receipt.effect_status is EffectStatus.NONE
            assert receipt.is_error is True
            assert receipt.live_emitted is False

    @pytest.mark.parametrize("cancel_seam", ["intent_evaluation", "attention_boundary"])
    def test_stop_before_approval_stages_every_original_call_none(
        self,
        tmp_db,
        cancel_seam: str,
    ) -> None:
        """Judge/attention cancellation cannot leave unstarted calls UNKNOWN."""
        session = _make_session()
        generation = session._claim_generation()
        call_ids = ("call-a", "call-b")
        executors = {call_id: MagicMock() for call_id in call_ids}
        items = [self._prepared_item(call_id, executors[call_id]) for call_id in call_ids]
        tool_calls = [
            {"id": call_id, "function": {"name": "test_tool", "arguments": "{}"}}
            for call_id in call_ids
        ]
        approve = MagicMock(return_value=(True, None))

        def evaluate_intent(*_args: Any, **_kwargs: Any) -> None:
            if cancel_seam == "intent_evaluation":
                session.cancel()
                raise GenerationCancelled()

        def push_smart_approval_config(_items: list[dict[str, Any]]) -> None:
            if cancel_seam == "attention_boundary":
                # Cancel immediately before the generation-fenced attention
                # publication.  That publication must refuse, and approval
                # must never open for the abandoned batch.
                session.cancel()

        with (
            patch.object(session, "_safe_prepare_tool", side_effect=items),
            patch.object(session, "_evaluate_intent", side_effect=evaluate_intent),
            patch.object(
                session,
                "_push_smart_approval_config",
                side_effect=push_smart_approval_config,
            ),
            patch.object(session.ui, "approve_tools", approve),
            pytest.raises(GenerationCancelled),
        ):
            session._execute_tools(tool_calls, my_generation=generation)

        approve.assert_not_called()
        for call_id, execute in executors.items():
            execute.assert_not_called()
            receipt = session._cancelled_tool_results[call_id]
            assert "no side effects" in receipt.detail
            assert receipt.effect_status is EffectStatus.NONE
            assert receipt.is_error is True
            assert receipt.live_emitted is False

    @pytest.mark.parametrize("cancel_seam", ["prepare", "intent"])
    def test_unstarted_receipts_emit_once_when_cancel_repair_persists_them(
        self,
        tmp_db,
        cancel_seam: str,
    ) -> None:
        """Pre-execution NONE receipts remain live until repair emits them.

        Preparation and intent evaluation can both lose the live publication
        race to Stop.  The batch has not admitted either executor, so repair
        must persist the exact NONE disposition and complete each live card
        once.  Treating every staged receipt as already emitted leaves the UI
        spinning forever even though the durable transcript is repaired.
        """
        ui = _ToolResultTrackingUI()
        session = _make_session(ui=ui)
        _bind_storage_mock()
        generation = session._claim_generation()
        call_ids = ("call-a", "call-b")
        detail = "Cancelled before tool execution; no side effects."
        executors = {call_id: MagicMock() for call_id in call_ids}
        tool_calls = [
            {"id": call_id, "function": {"name": "test_tool", "arguments": "{}"}}
            for call_id in call_ids
        ]
        session.messages.append(
            Turn.assistant(
                "calling tools",
                tool_calls=tuple(
                    ToolCall(id=call_id, name="test_tool", arguments="{}") for call_id in call_ids
                ),
            )
        )
        session._msg_tokens.append(1)

        def prepare(tool_call: dict[str, Any]) -> dict[str, Any]:
            call_id = str(tool_call["id"])
            if cancel_seam == "prepare":
                session.cancel()
                raise GenerationCancelled()
            return self._prepared_item(call_id, executors[call_id])

        def evaluate_intent(*_args: Any, **_kwargs: Any) -> None:
            if cancel_seam == "intent":
                session.cancel()
                raise GenerationCancelled()

        with (
            patch.object(session, "_safe_prepare_tool", side_effect=prepare),
            patch.object(session, "_evaluate_intent", side_effect=evaluate_intent),
            pytest.raises(GenerationCancelled),
        ):
            session._execute_tools(tool_calls, my_generation=generation)

        assert ui.tool_results == []
        for execute in executors.values():
            execute.assert_not_called()

        with patch("turnstone.core.session.save_message", return_value=1) as save:
            session._synthesize_cancelled_results("Cancelled by user.")

        assert ui.tool_results == [(call_id, "test_tool", detail, True) for call_id in call_ids]
        tool_turns = [turn for turn in session.messages if turn.role is Role.TOOL]
        assert [turn.tool_call_id for turn in tool_turns] == list(call_ids)
        assert all(turn.text == detail for turn in tool_turns)
        assert all(turn.is_error is True for turn in tool_turns)
        assert all(turn.effect_status is EffectStatus.NONE for turn in tool_turns)
        assert save.call_count == len(call_ids)
        assert all(
            json.loads(call.kwargs["meta"]) == {"effect_status": "none"}
            and call.kwargs["is_error"] is True
            for call in save.call_args_list
        )

        # Repair is idempotent: answered calls produce neither another row nor
        # a duplicate live completion if cleanup is entered a second time.
        with patch("turnstone.core.session.save_message") as second_save:
            session._synthesize_cancelled_results("Cancelled by user.")
        second_save.assert_not_called()
        assert len(ui.tool_results) == len(call_ids)

    @pytest.mark.parametrize(
        ("report_order", "is_error", "effect_status"),
        [
            ("cancel_before_report", True, EffectStatus.PARTIAL),
            ("report_before_cancel", False, EffectStatus.COMMITTED),
            ("cancel_before_report", False, None),
        ],
    )
    def test_owned_result_receipt_survives_cancel_report_ordering(
        self,
        tmp_db,
        report_order: str,
        is_error: bool,
        effect_status: EffectStatus | None,
    ) -> None:
        """An observed executor receipt beats generic UNKNOWN in either race.

        If Stop wins before the report, synthesis owes the live event.  If the
        report wins first, synthesis owes only persistence.  The latter keeps
        the exact live event that already escaped; the former completes the
        live card with neutral controller prose.  Neither path retains raw,
        pre-output-guard bytes for late emission or model replay.  Error/effect
        classifications survive on the neutral durable receipt.
        """
        ui = _ToolResultTrackingUI()
        session = _make_session(ui=ui)
        _bind_storage_mock()
        generation = session._claim_generation()
        status_label = effect_status.value if effect_status is not None else "unclassified"
        call_id = f"call-{report_order}-{status_label}"
        output = "committed result"
        tool_calls = [{"id": call_id, "function": {"name": "test_tool", "arguments": "{}"}}]
        session.messages.append(
            Turn.assistant(
                "calling a tool",
                tool_calls=(ToolCall(id=call_id, name="test_tool", arguments="{}"),),
            )
        )
        session._msg_tokens.append(1)

        def execute(_item: dict[str, Any]) -> tuple[str, str]:
            if report_order == "cancel_before_report":
                session.cancel()
            session._report_tool_result(
                call_id,
                "test_tool",
                output,
                is_error=is_error,
                status=effect_status,
            )
            if report_order == "report_before_cancel":
                session.cancel()
            return call_id, output

        prepared = self._prepared_item(call_id, execute)
        with (
            patch.object(session, "_safe_prepare_tool", return_value=prepared),
            patch.object(session, "_evaluate_intent", return_value=None),
        ):
            results, feedback = session._execute_tools(tool_calls, my_generation=generation)

        assert results == [(call_id, output)]
        assert feedback is None
        expected_live_before_repair = 0 if report_order == "cancel_before_report" else 1
        assert len(ui.tool_results) == expected_live_before_repair

        with patch("turnstone.core.session.save_message", return_value=1) as save:
            session._synthesize_cancelled_results("Cancelled by user.")

        tool_turns = [turn for turn in session.messages if turn.role is Role.TOOL]
        assert len(tool_turns) == 1
        assert tool_turns[0].tool_call_id == call_id
        durable_detail = tool_turns[0].text
        assert durable_detail != output
        assert output not in durable_detail
        assert "UNKNOWN" not in durable_detail
        assert "Output review did not complete" in durable_detail
        if is_error:
            assert durable_detail.startswith("Tool error was observed before cancellation.")
        else:
            assert durable_detail.startswith("Tool result was observed before cancellation.")
        if effect_status is None:
            assert "unclassified; do not infer no effect" in durable_detail
        else:
            assert f"Effect status: {effect_status.value.replace('_', ' ')}." in durable_detail
        expected_live_detail = durable_detail if report_order == "cancel_before_report" else output
        assert ui.tool_results == [
            (call_id, "test_tool", expected_live_detail, is_error),
        ]
        assert "UNKNOWN" not in ui.tool_results[0][2]
        if report_order == "cancel_before_report":
            assert output not in ui.tool_results[0][2]
        else:
            assert ui.tool_results[0][2] == output
        assert tool_turns[0].is_error is is_error
        assert tool_turns[0].effect_status is effect_status
        save.assert_called_once()
        assert save.call_args.args[2] == durable_detail
        assert output not in save.call_args.args[2]
        assert "UNKNOWN" not in save.call_args.args[2]
        assert save.call_args.kwargs["is_error"] is is_error
        if effect_status is None:
            assert save.call_args.kwargs["meta"] is None
        else:
            assert json.loads(save.call_args.kwargs["meta"]) == {
                "effect_status": effect_status.value,
            }

    def test_parallel_stop_marks_only_never_started_sibling_none(self, tmp_db) -> None:
        session = _make_session()
        generation = session._claim_generation()
        call_ids = [f"call-{index}" for index in range(5)]
        all_workers_started = threading.Event()
        release_workers = threading.Event()
        starts_lock = threading.Lock()
        started: list[str] = []

        def execute(item):
            with starts_lock:
                started.append(item["call_id"])
                if len(started) == 4:
                    all_workers_started.set()
            if not release_workers.wait(2):
                raise RuntimeError("test workers were not released")
            session._check_cancelled(generation)
            raise AssertionError("cancelled worker continued")

        items = [self._prepared_item(call_id, execute) for call_id in call_ids]
        tool_calls = [
            {"id": call_id, "function": {"name": "test_tool", "arguments": "{}"}}
            for call_id in call_ids
        ]
        session.messages.append(
            Turn.assistant(
                "",
                tool_calls=tuple(
                    ToolCall(id=call_id, name="test_tool", arguments="{}") for call_id in call_ids
                ),
            )
        )
        session._msg_tokens.append(1)
        errors: list[BaseException] = []

        def run_batch() -> None:
            try:
                session._execute_tools(tool_calls, my_generation=generation)
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=run_batch)
        with (
            patch.object(session, "_safe_prepare_tool", side_effect=items),
            patch.object(session, "_evaluate_intent", return_value=None),
        ):
            worker.start()
            assert all_workers_started.wait(2)
            session.cancel()
            release_workers.set()
            worker.join(2)

        assert not worker.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], GenerationCancelled)
        assert started == call_ids[:4]
        assert call_ids[-1] not in started
        assert call_ids[-1] in session._cancelled_tool_results
        assert session._cancelled_tool_results[call_ids[-1]].effect_status is EffectStatus.NONE
        assert all(call_id not in session._cancelled_tool_results for call_id in started)

        session._synthesize_cancelled_results("Cancelled by user.")
        dispositions = {
            turn.tool_call_id: turn.effect_status
            for turn in session.messages
            if turn.role is Role.TOOL
        }
        assert dispositions == {
            **{call_id: EffectStatus.UNKNOWN for call_id in started},
            call_ids[-1]: EffectStatus.NONE,
        }


class TestGenerationDurabilityFIFO:
    """Generation handoff stays responsive while persistence remains ordered."""

    def test_stale_recovery_tail_leaves_error_clear_for_successor(self, tmp_db) -> None:
        """A superseded recovery cannot consume the durable-error latch."""
        ui = NullUI()
        session = _make_session(ui=ui)
        storage = _bind_storage_mock()
        session._has_persisted_error = True
        session._persisted_error_revision = 1
        old_generation = session._claim_generation()
        old_storage_started = threading.Event()
        release_old_storage = threading.Event()
        successor_admitted = threading.Event()
        results: dict[str, bool] = {}
        errors: list[BaseException] = []
        clear_calls: list[str] = []

        def block_old_storage() -> None:
            old_storage_started.set()
            if not release_old_storage.wait(2):
                raise RuntimeError("test predecessor recovery was not released")

        def run_old() -> None:
            try:

                def commit_old(durable) -> None:
                    durable.append(block_old_storage)
                    session._emit_state("idle", deferred_persistence=durable)

                results["old"] = session._commit_for_generation(old_generation, commit_old)
            except BaseException as exc:
                errors.append(exc)

        predecessor = threading.Thread(target=run_old)
        successor: threading.Thread | None = None
        with patch.object(
            storage,
            "save_workstream_config",
            side_effect=lambda ws_id, _config: clear_calls.append(ws_id),
        ):
            predecessor.start()
            try:
                assert old_storage_started.wait(2)
                assert session._has_persisted_error is True
                successor_generation = session._claim_generation()

                def run_successor() -> None:
                    try:

                        def commit_successor(durable) -> None:
                            session._emit_state("idle", deferred_persistence=durable)
                            successor_admitted.set()

                        results["successor"] = session._commit_for_generation(
                            successor_generation,
                            commit_successor,
                        )
                    except BaseException as exc:
                        errors.append(exc)

                successor = threading.Thread(target=run_successor)
                successor.start()
                assert successor_admitted.wait(2)
                assert session._has_persisted_error is True
                release_old_storage.set()
            finally:
                release_old_storage.set()
                predecessor.join(2)
                if successor is not None:
                    successor.join(2)

        assert not predecessor.is_alive()
        assert successor is not None and not successor.is_alive()
        assert errors == []
        assert results == {"old": True, "successor": True}
        assert clear_calls == [session._ws_id]
        assert session._has_persisted_error is False
        assert ui.states == ["idle"]

    def test_stale_state_tail_cannot_publish_after_responsive_stop_and_handoff(
        self,
        tmp_db,
    ) -> None:
        """A blocked predecessor write cannot pin Stop or publish stale state."""
        old_storage_started = threading.Event()
        release_old_storage = threading.Event()
        successor_live_admitted = threading.Event()
        stop_done = threading.Event()
        storage_states: list[str] = []
        storage_lock = threading.Lock()

        storage = MagicMock()
        storage.get_workstream.return_value = None

        def update_state(_ws_id: str, state: str) -> None:
            if state == WorkstreamState.RUNNING.value:
                old_storage_started.set()
                if not release_old_storage.wait(2):
                    raise RuntimeError("test predecessor state write was not released")
            with storage_lock:
                storage_states.append(state)

        storage.update_workstream_state.side_effect = update_state

        class _Adapter:
            kind = WorkstreamKind.INTERACTIVE

            def __init__(self) -> None:
                self.observer_states: list[str] = []

            def build_ui(self, _ws):
                return MagicMock()

            def build_session(self, _ws, **_kwargs):
                return MagicMock()

            def cleanup_ui(self, _ws) -> None:
                return None

            def emit_created(self, _ws) -> None:
                return None

            def emit_closed(self, _ws_id, **_kwargs) -> None:
                return None

            def prepare_state_event(self, _ws, state):
                state_value = state.value
                return lambda: self.observer_states.append(state_value)

        adapter = _Adapter()
        manager = SessionManager(
            adapter,
            storage=storage,
            max_active=1,
            event_emitter=adapter,
        )
        ws = manager.create(user_id="u1", ws_id="state-race")
        subscriber_states: list[str] = []
        manager.subscribe_to_state(lambda _ws_id, state: subscriber_states.append(state.value))

        class _ManagerUI(NullUI):
            def on_state_change_deferred(self, state, *, deferred_persistence, owner_valid):
                admitted = manager.set_state_deferred(
                    ws.id,
                    WorkstreamState(state),
                    deferred_persistence=deferred_persistence,
                    after_persist=lambda: self.states.append(state),
                    owner_valid=owner_valid,
                )
                if not admitted:
                    raise RuntimeError("test state transition was not admitted")

        ui = _ManagerUI()
        ws.ui = ui
        session = _make_session(ui=ui)
        old_generation = session._claim_generation()
        results: dict[str, bool] = {}
        errors: list[BaseException] = []

        def run_old() -> None:
            try:
                results["old"] = session._commit_for_generation(
                    old_generation,
                    lambda durable: session._emit_state(
                        WorkstreamState.RUNNING.value,
                        deferred_persistence=durable,
                    ),
                    allow_cancelled=False,
                )
            except BaseException as exc:
                errors.append(exc)

        def run_stop() -> None:
            try:
                session.cancel()
            except BaseException as exc:
                errors.append(exc)
            finally:
                stop_done.set()

        predecessor = threading.Thread(target=run_old)
        stopper = threading.Thread(target=run_stop)
        successor: threading.Thread | None = None
        predecessor.start()
        try:
            assert old_storage_started.wait(2)

            stopper.start()
            assert stop_done.wait(2), "Stop waited on predecessor state storage"
            successor_generation = session._claim_generation()

            def run_successor() -> None:
                try:

                    def commit_successor(durable) -> None:
                        session._emit_state(
                            WorkstreamState.IDLE.value,
                            deferred_persistence=durable,
                        )
                        successor_live_admitted.set()

                    results["successor"] = session._commit_for_generation(
                        successor_generation,
                        commit_successor,
                        allow_cancelled=False,
                    )
                except BaseException as exc:
                    errors.append(exc)

            successor = threading.Thread(target=run_successor)
            successor.start()
            assert successor_live_admitted.wait(2), (
                "successor waited on the ChatSession generation lock while "
                "predecessor storage was blocked"
            )
            assert ws.state is WorkstreamState.IDLE
            assert predecessor.is_alive()
            assert successor.is_alive()
            assert adapter.observer_states == []
            assert subscriber_states == []
            assert ui.states == []

            release_old_storage.set()
        finally:
            release_old_storage.set()
            predecessor.join(2)
            if stopper.ident is not None:
                stopper.join(2)
            if successor is not None:
                successor.join(2)

        assert not predecessor.is_alive()
        assert not stopper.is_alive()
        assert successor is not None and not successor.is_alive()
        assert errors == []
        assert results == {"old": True, "successor": True}
        assert storage_states == [
            WorkstreamState.RUNNING.value,
            WorkstreamState.IDLE.value,
        ]
        assert adapter.observer_states == [WorkstreamState.IDLE.value]
        assert subscriber_states == [WorkstreamState.IDLE.value]
        assert ui.states == [WorkstreamState.IDLE.value]

    def test_force_successor_commits_live_before_waiting_for_predecessor_save(
        self,
        tmp_db,
    ) -> None:
        """A force handoff admits live state without letting its save overtake."""
        session = _make_session()
        old_generation = session._claim_generation()
        old_save_started = threading.Event()
        release_old_save = threading.Event()
        successor_claimed = threading.Event()
        successor_live_committed = threading.Event()
        successor_save_started = threading.Event()
        successor_done = threading.Event()
        live_order: list[tuple[str, int]] = []
        persistence_order: list[str] = []
        results: dict[str, bool] = {}
        successor_generations: list[int] = []
        errors: list[BaseException] = []

        def persist_old() -> None:
            old_save_started.set()
            if not release_old_save.wait(2):
                raise RuntimeError("test predecessor save was not released")
            persistence_order.append("old")

        def commit_old(durable) -> None:
            live_order.append(("old", old_generation))
            durable.append(persist_old)

        def run_old() -> None:
            try:
                results["old"] = session._commit_for_generation(
                    old_generation,
                    commit_old,
                    allow_cancelled=False,
                )
            except BaseException as exc:
                errors.append(exc)

        def run_successor() -> None:
            try:
                session.cancel()
                generation = session._claim_generation()
                successor_generations.append(generation)
                successor_claimed.set()

                def persist_successor() -> None:
                    successor_save_started.set()
                    persistence_order.append("successor")

                def commit_successor(durable) -> None:
                    live_order.append(("successor", generation))
                    durable.append(persist_successor)
                    successor_live_committed.set()

                results["successor"] = session._commit_for_generation(
                    generation,
                    commit_successor,
                    allow_cancelled=False,
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                successor_done.set()

        predecessor = threading.Thread(target=run_old)
        successor = threading.Thread(target=run_successor)
        predecessor.start()
        try:
            assert old_save_started.wait(2)
            successor.start()
            assert successor_claimed.wait(2)
            assert successor_live_committed.wait(2)

            # Generation ownership and the successor's whole live transaction
            # advance while the predecessor is still blocked in storage.
            assert successor_generations == [old_generation + 1]
            assert session._generation == old_generation + 1
            assert live_order == [
                ("old", old_generation),
                ("successor", old_generation + 1),
            ]

            # Its synchronous durable tail waits on the predecessor's ticket;
            # no newer row may overtake the blocked save.
            assert not successor_save_started.is_set()
            assert not successor_done.is_set()
            assert persistence_order == []
            release_old_save.set()
        finally:
            release_old_save.set()
            predecessor.join(2)
            if successor.ident is not None:
                successor.join(2)

        assert not predecessor.is_alive()
        assert not successor.is_alive()
        assert errors == []
        assert results == {"old": True, "successor": True}
        assert persistence_order == ["old", "successor"]
        assert successor_save_started.is_set()
        assert successor_done.is_set()

    def test_close_does_not_wait_for_admitted_durable_save(self, tmp_db) -> None:
        """Close latches publication shutdown independently of the FIFO tail."""
        session = _make_session()
        generation = session._claim_generation()
        save_started = threading.Event()
        release_save = threading.Event()
        close_done = threading.Event()
        live_commits: list[str] = []
        persistence_order: list[str] = []
        commit_results: list[bool] = []
        errors: list[BaseException] = []

        def persist() -> None:
            save_started.set()
            if not release_save.wait(2):
                raise RuntimeError("test durable save was not released")
            persistence_order.append("admitted")

        def commit(durable) -> None:
            live_commits.append("admitted")
            durable.append(persist)

        def run_commit() -> None:
            try:
                commit_results.append(
                    session._commit_for_generation(
                        generation,
                        commit,
                        allow_cancelled=False,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        def run_close() -> None:
            try:
                session.close()
            except BaseException as exc:
                errors.append(exc)
            finally:
                close_done.set()

        worker = threading.Thread(target=run_commit)
        closer = threading.Thread(target=run_close)
        worker.start()
        try:
            assert save_started.wait(2)
            closer.start()
            assert close_done.wait(2)
            assert worker.is_alive()
            assert persistence_order == []
            assert session._publication_shutdown is True

            rejected_live: list[str] = []
            assert (
                session._commit_for_generation(
                    generation,
                    lambda _durable: rejected_live.append("late"),
                )
                is False
            )
            assert rejected_live == []
            release_save.set()
        finally:
            release_save.set()
            worker.join(2)
            if closer.ident is not None:
                closer.join(2)

        assert not worker.is_alive()
        assert not closer.is_alive()
        assert errors == []
        assert live_commits == ["admitted"]
        assert commit_results == [True]
        assert persistence_order == ["admitted"]

    def test_legacy_state_callback_runs_outside_generation_lock(self, tmp_db) -> None:
        """A UI without the split hook cannot smuggle sync work under G."""
        lock_observations: list[bool] = []

        class LegacyUI(NullUI):
            def on_state_change(self, state):
                is_owned = getattr(session._generation_lock, "_is_owned", lambda: False)
                lock_observations.append(bool(is_owned()))
                super().on_state_change(state)

        ui = LegacyUI()
        session = _make_session(ui=ui)
        generation = session._claim_generation()

        assert session._commit_for_generation(
            generation,
            lambda durable: session._emit_state(
                "running",
                deferred_persistence=durable,
            ),
        )

        assert lock_observations == [False]
        assert ui.states == ["running"]

    def test_close_refuses_delayed_legacy_state_callback(self, tmp_db) -> None:
        """Direct ChatSession close is a terminal state-tail fence."""
        ui = NullUI()
        session = _make_session(ui=ui)
        generation = session._claim_generation()
        save_started = threading.Event()
        release_save = threading.Event()
        errors: list[BaseException] = []

        def blocked_save() -> None:
            save_started.set()
            if not release_save.wait(2):
                raise RuntimeError("test state predecessor was not released")

        def commit_state(durable) -> None:
            durable.append(blocked_save)
            session._emit_state("idle", deferred_persistence=durable)

        def run_commit() -> None:
            try:
                session._commit_for_generation(generation, commit_state)
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=run_commit)
        worker.start()
        try:
            assert save_started.wait(2)
            session.close()
        finally:
            release_save.set()
            worker.join(2)

        assert not worker.is_alive()
        assert errors == []
        assert ui.states == []

    @pytest.mark.parametrize("state", ["error", "idle"])
    def test_stop_does_not_wait_for_deferred_generation_state_storage(
        self,
        tmp_db,
        state: str,
    ) -> None:
        """Fatal and recovery state storage run outside the lifecycle lock."""
        ui = _DeferredStateStorageUI()
        session = _make_session(ui=ui)
        generation = session._claim_generation()
        cancel_done = threading.Event()
        commit_results: list[bool] = []
        errors: list[BaseException] = []

        def commit_state(durable) -> None:
            if state == "error":
                session._record_fatal_error(
                    RuntimeError("fatal state split test"),
                    deferred_persistence=durable,
                )
            else:
                session._emit_state("idle", deferred_persistence=durable)

        def run_commit() -> None:
            try:
                commit_results.append(
                    session._commit_for_generation(
                        generation,
                        commit_state,
                        allow_cancelled=False,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        def run_cancel() -> None:
            try:
                session.cancel()
            except BaseException as exc:
                errors.append(exc)
            finally:
                cancel_done.set()

        worker = threading.Thread(target=run_commit)
        canceller = threading.Thread(target=run_cancel)
        persist_error = (
            patch("turnstone.core.memory.persist_last_error")
            if state == "error"
            else contextlib.nullcontext()
        )
        with persist_error:
            worker.start()
            try:
                assert ui.storage_started.wait(2)
                assert ui.states == []
                assert ui.persisted_states == []

                canceller.start()
                assert cancel_done.wait(2)
                assert session._cancel_event.is_set()
                assert worker.is_alive()
                assert ui.persisted_states == []
                ui.release_storage.set()
            finally:
                ui.release_storage.set()
                worker.join(2)
                if canceller.ident is not None:
                    canceller.join(2)

        assert not worker.is_alive()
        assert not canceller.is_alive()
        assert errors == []
        assert commit_results == [True]
        assert ui.persisted_states == [state]
        assert ui.states == [state]


class TestCancelledSendCleanupOwnership:
    """The cancelled-send repair block is one generation-owned transaction."""

    @staticmethod
    def _cancel_with_pending_cleanup(session) -> None:
        call_id = "old-tool"
        session.messages.append(
            Turn.assistant(
                "",
                tool_calls=(ToolCall(id=call_id, name="bash", arguments="{}"),),
            )
        )
        session._msg_tokens.append(1)
        with session._queued_lock:
            session._queued_messages["queued-old"] = ("queued for next seam", "normal")
        session._nudge_queue.enqueue("old_tool", "old advisory", "tool")
        session.cancel()
        raise GenerationCancelled()

    def test_cancel_cleanup_skips_remote_rerank_and_finishes(self, tmp_db) -> None:
        """A set cancel event cannot re-enter reranking from its own repair path."""
        from turnstone.core.admission import ModelAdmission
        from turnstone.core.memory import save_structured_memory_strict
        from turnstone.core.rerank import RerankLane, RerankRuntime

        class _NeverBackend:
            calls = 0

            def rerank(self, *args: Any, **kwargs: Any):
                self.calls += 1
                raise AssertionError("cancel cleanup dispatched reranking")

        ui = NullUI()
        session = _make_registered_session(ui=ui, user_id="owner")
        session._title_generated = True
        save_structured_memory_strict(
            "kafka_runbook",
            "private body",
            description="queued for next seam",
            scope="global",
        )
        backend = _NeverBackend()
        lane = RerankLane(
            RerankRuntime(backend, alias="rr", model="m"),
            "rr",
            "m",
            ModelAdmission("rr"),
            0,
        )

        try:
            with (
                patch.object(session, "_resolve_rerank_lane", return_value=lane) as resolve,
                patch.object(
                    session,
                    "_stream_response",
                    side_effect=lambda _generation: self._cancel_with_pending_cleanup(session),
                ),
            ):
                session.send("unrelated opening request")
        finally:
            lane.runtime.retire()

        resolve.assert_not_called()
        assert backend.calls == 0
        assert any(turn.role is Role.TOOL for turn in session.messages)
        assert session.messages[-2].role is Role.USER
        assert session.messages[-2].text == "queued for next seam"
        assert session.messages[-1].role is Role.SYSTEM
        assert session.messages[-1].source == "memory_pointer"
        assert "kafka_runbook" in session.messages[-1].text
        assert ui.states[-1] == "idle"

    def test_queued_turn_rerank_is_fenced_by_generation_owner(self, tmp_db) -> None:
        """A force successor prevents the abandoned queue planner from dispatching."""
        from turnstone.core.admission import ModelAdmission
        from turnstone.core.memory import save_structured_memory_strict
        from turnstone.core.rerank import RerankHit, RerankLane, RerankRuntime

        class _CountingBackend:
            def __init__(self) -> None:
                self.calls = 0

            def rerank(self, _query: str, documents: list[str], **_kwargs: Any):
                self.calls += 1
                return [RerankHit(index=i, score=1.0) for i in range(len(documents))]

        session = _make_registered_session(user_id="owner")
        save_structured_memory_strict(
            "kafka_runbook",
            "private body",
            description="restart kafka brokers",
            scope="global",
        )
        backend = _CountingBackend()
        lane = RerankLane(
            RerankRuntime(backend, alias="rr", model="m"),
            "rr",
            "m",
            ModelAdmission("rr"),
            0,
        )
        resolve_started = threading.Event()
        release_resolve = threading.Event()
        send_errors: list[BaseException] = []

        def resolve_lane() -> RerankLane:
            resolve_started.set()
            if not release_resolve.wait(2):
                raise RuntimeError("rerank lane resolution was not released")
            return lane

        def stream():
            yield StreamChunk(content_delta="done")
            with session._queued_lock:
                session._queued_messages["queued-old"] = (
                    "restart kafka brokers",
                    "normal",
                )
            yield StreamChunk(finish_reason="stop")

        arm_session(session, stream())

        def run_send() -> None:
            try:
                session.send("zzzxxyy")
            except GenerationCancelled as exc:
                send_errors.append(exc)
            except Exception as exc:
                send_errors.append(exc)

        sender = threading.Thread(target=run_send)
        try:
            with patch.object(session, "_resolve_rerank_lane", side_effect=resolve_lane):
                sender.start()
                assert resolve_started.wait(2)
                assert session._claim_generation() == 2
                release_resolve.set()
                sender.join(2)
        finally:
            release_resolve.set()
            sender.join(2)
            lane.runtime.retire()

        assert not sender.is_alive()
        assert send_errors == []
        assert backend.calls == 0

    def test_successor_waits_for_complete_cancel_cleanup_transaction(self, tmp_db):
        """A claim already waiting on the lock observes every cleanup effect."""
        ui = NullUI()
        session = _make_registered_session(ui=ui)
        session._title_generated = True
        observed_lock = _ObservedRLock()
        session._generation_lock = observed_lock
        # This test replaces the generation lock to observe ownership. The
        # production truncation condition is constructed from that same lock,
        # so rebuild it on the replacement: leaving the fixture's condition
        # bound to the old lock would split the ownership domain and let the
        # claimant cross the cleanup transaction.
        session._history_truncation_condition = threading.Condition(observed_lock)
        cleanup_entered = threading.Event()
        release_cleanup = threading.Event()
        send_errors: list[BaseException] = []
        claim_errors: list[BaseException] = []
        claim_snapshots: list[dict[str, object]] = []
        original_synthesize = session._synthesize_cancelled_results

        def blocked_synthesize(reason: str, **kwargs) -> None:
            cleanup_entered.set()
            if not release_cleanup.wait(2):
                raise RuntimeError("cancel cleanup was not released")
            original_synthesize(reason, **kwargs)

        def run_cancelled_send():
            try:
                session.send("old request")
            except BaseException as exc:
                send_errors.append(exc)

        def claim_successor():
            try:
                generation = session._claim_generation()
                with session._queued_lock:
                    queued = dict(session._queued_messages)
                claim_snapshots.append(
                    {
                        "generation": generation,
                        "turns": tuple(session.messages),
                        "queued": queued,
                        "nudges": tuple(session._nudge_queue.pending()),
                        "infos": tuple(ui.infos),
                        "states": tuple(ui.states),
                    }
                )
            except BaseException as exc:
                claim_errors.append(exc)

        sender = threading.Thread(target=run_cancelled_send)
        claimant = threading.Thread(target=claim_successor)
        with (
            patch.object(
                session,
                "_stream_response",
                side_effect=lambda _generation: self._cancel_with_pending_cleanup(session),
            ),
            patch.object(
                session,
                "_synthesize_cancelled_results",
                side_effect=blocked_synthesize,
            ),
        ):
            sender.start()
            assert cleanup_entered.wait(2)
            observed_lock.watch(claimant)
            claimant.start()
            assert observed_lock.waiting.wait(2)
            release_cleanup.set()
            sender.join(2)
            claimant.join(2)

        assert not sender.is_alive()
        assert not claimant.is_alive()
        assert send_errors == []
        assert claim_errors == []
        assert len(claim_snapshots) == 1
        snapshot = claim_snapshots[0]
        assert snapshot["generation"] == 2
        turns = snapshot["turns"]
        assert isinstance(turns, tuple)
        tool_turns = [turn for turn in turns if turn.role is Role.TOOL]
        assert len(tool_turns) == 1
        assert tool_turns[0].tool_call_id == "old-tool"
        assert tool_turns[0].effect_status is EffectStatus.UNKNOWN
        assert turns[-1].role is Role.USER
        assert turns[-1].text == "queued for next seam"
        assert snapshot["queued"] == {}
        assert snapshot["nudges"] == ()
        assert any("cancelled" in info.lower() for info in snapshot["infos"])
        # The durable state tail runs after the generation lock is released.
        # This successor won that handoff, so the predecessor's delayed IDLE
        # observer callback is correctly fenced rather than repainting it.
        assert snapshot["states"][-1] == "thinking"

    def test_successor_claim_before_cleanup_refuses_entire_transaction(self, tmp_db):
        """Once a successor owns the session, no old cleanup action starts."""
        ui = NullUI()
        session = _make_registered_session(ui=ui)
        session._title_generated = True
        publish_entered = threading.Event()
        release_publish = threading.Event()
        send_errors: list[BaseException] = []
        publish_generations: list[int] = []
        blocked_cleanup_calls = 0
        original_commit = session._commit_for_generation

        def blocked_commit(origin_generation, commit, *, allow_cancelled=True, **kwargs):
            # Pass through every admission flag (e.g. allow_workstream_gone on
            # the cancel finalizer) — the shim only sequences, never narrows.
            nonlocal blocked_cleanup_calls
            publish_generations.append(origin_generation)
            publish_name = getattr(commit, "__name__", "")
            if publish_name != "_finalize_cancelled_generation":
                return original_commit(
                    origin_generation,
                    commit,
                    allow_cancelled=allow_cancelled,
                    **kwargs,
                )
            blocked_cleanup_calls += 1
            publish_entered.set()
            if not release_publish.wait(2):
                raise RuntimeError("generation publication was not released")
            return original_commit(
                origin_generation,
                commit,
                allow_cancelled=allow_cancelled,
                **kwargs,
            )

        def run_cancelled_send():
            try:
                session.send("old request")
            except BaseException as exc:
                send_errors.append(exc)

        sender = threading.Thread(target=run_cancelled_send)
        with (
            patch.object(
                session,
                "_stream_response",
                side_effect=lambda _generation: self._cancel_with_pending_cleanup(session),
            ),
            patch.object(session, "_commit_for_generation", side_effect=blocked_commit),
            patch.object(
                session,
                "_synthesize_cancelled_results",
                wraps=session._synthesize_cancelled_results,
            ) as synthesize,
            patch.object(
                session,
                "_flush_queued_messages",
                wraps=session._flush_queued_messages,
            ) as flush,
            patch.object(
                session,
                "_drain_pending_advisories",
                wraps=session._drain_pending_advisories,
            ) as drain,
        ):
            try:
                sender.start()
                assert publish_entered.wait(2)
                successor_generation = session._claim_generation()
            finally:
                release_publish.set()
                sender.join(2)

        assert not sender.is_alive()
        assert send_errors == []
        assert publish_generations and set(publish_generations) == {1}
        assert blocked_cleanup_calls == 1
        assert successor_generation == 2
        synthesize.assert_not_called()
        flush.assert_not_called()
        drain.assert_not_called()
        assert all(turn.role is not Role.TOOL for turn in session.messages)
        with session._queued_lock:
            assert session._queued_messages == {"queued-old": ("queued for next seam", "normal")}
        assert session._nudge_queue.pending() == [("old_tool", "old advisory")]
        assert not any("cancelled" in info.lower() for info in ui.infos)
        assert "idle" not in ui.states
        assert not session._cancel_event.is_set()


class TestForceCancelThreaded:
    """Force cancel with actual threads — verifies orphaned thread behavior."""

    def test_force_cancel_orphan_does_not_mutate_messages(self, tmp_db):
        """After force cancel + new send(), the orphaned thread must not
        append stale content to session.messages."""
        ui = NullUI()
        session = _make_registered_session(ui=ui)

        barrier = threading.Event()
        old_done = threading.Event()

        def slow_stream():
            yield StreamChunk(content_delta="Old content")
            barrier.set()  # signal: first chunk delivered
            time.sleep(2)  # simulate stuck stream
            yield StreamChunk(content_delta=" more", finish_reason="stop")

        # Start generation 1 (will get stuck)
        arm_session(session, slow_stream())

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
        session = _make_registered_session(ui=ui)

        barrier = threading.Event()

        def stuck_stream():
            yield StreamChunk(content_delta="stuck", finish_reason="stop")
            barrier.set()
            time.sleep(2)
            yield StreamChunk(content_delta=" end", finish_reason="stop")

        # Start stuck generation
        arm_session(session, stuck_stream())
        t = threading.Thread(target=lambda: session.send("old"), daemon=True)
        t.start()
        assert barrier.wait(timeout=5), "stream did not start"

        # Force cancel
        session.cancel()

        # New generation should work
        arm_session(
            session, iter([StreamChunk(content_delta="Fresh response", finish_reason="stop")])
        )
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
        return _ToolResultTrackingUI()

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

    def test_staged_agent_disposition_is_persisted_once_and_consumed(self, tmp_db):
        """A task agent's precise cancellation ledger wins over stale side maps.

        Provider call IDs may be reused by a successor generation.  Synthesis
        must consume every ephemeral entry for that ID while retaining the
        task wrapper's exact disposition/status in the durable tool turn.  The
        wrapper already published the live result, so synthesis must not emit
        a duplicate.
        """
        ui = self._ui_with_tool_result_tracking()
        session = _make_session(ui=ui)
        _bind_storage_mock()
        call_id = "reused-call"
        disposition = "Completed before cancel: read_file. Task was interrupted."
        session.messages.append(
            Turn.assistant(
                "calling task agent",
                tool_calls=(ToolCall(id=call_id, name="task_agent", arguments="{}"),),
            )
        )
        session._msg_tokens.append(1)

        # _exec_task publishes this result before staging it for the durable
        # cancellation fold.  Seed stale per-call state to prove a reused
        # provider ID cannot leak either value into a successor.
        ui.on_tool_result(call_id, "task_agent", disposition)
        session._cancelled_tool_results[call_id] = _CancelledToolResult(
            detail=disposition,
            effect_status=EffectStatus.PARTIAL,
            is_error=True,
            preview=None,
            live_emitted=True,
        )
        session._tool_status[call_id] = EffectStatus.UNKNOWN
        session._tool_error_flags[call_id] = True

        with patch("turnstone.core.session.save_message", return_value=1) as save:
            session._synthesize_cancelled_results("Cancelled by user.")

        # The existing live result is the only one; synthesis is persistence
        # and trajectory repair for a staged task-agent disposition.
        assert ui.tool_results == [(call_id, "task_agent", disposition, False)]
        tool_turns = [turn for turn in session.messages if turn.role is Role.TOOL]
        assert len(tool_turns) == 1
        assert tool_turns[0].tool_call_id == call_id
        assert tool_turns[0].text == disposition
        assert tool_turns[0].is_error is True
        assert tool_turns[0].effect_status is EffectStatus.PARTIAL

        save.assert_called_once()
        assert json.loads(save.call_args.kwargs["meta"]) == {"effect_status": "partial"}
        assert call_id not in session._cancelled_tool_results
        assert call_id not in session._tool_status
        assert call_id not in session._tool_error_flags

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
            {
                "call_id": "c1",
                "mcp_func_name": "send_email",
                "mcp_args": {},
                "_principal_id": "",
            }
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
            {"call_id": "c1", "resource_uri": "file:///doc", "_principal_id": ""}
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

    @staticmethod
    def _journal(turns, *started_ids):
        journal = _TaskExecutionJournal(turns)
        for call_id in started_ids:
            journal.mark_started(call_id)
        journal.materialize_unstarted(turns)
        return journal

    def test_status_none_when_no_actions(self):
        """Typed twin of the disposition: a task cancelled before any action is
        NONE, not UNKNOWN — the complement of the in-flight case."""
        assert self._journal([]).cancelled_status() is EffectStatus.NONE

    def test_status_unknown_when_in_flight(self):
        msgs = [self._assistant("t1", "bash")]  # issued, no result → in flight
        assert self._journal(msgs, "t1").cancelled_status() is EffectStatus.UNKNOWN

    def test_status_partial_when_all_answered(self):
        """Every issued call returned but the agent was stopped before finishing
        — effects are known (not UNKNOWN) yet the task is incomplete: PARTIAL."""
        msgs = [self._assistant("t1", "bash"), self._result("t1")]
        assert self._journal(msgs).cancelled_status() is EffectStatus.PARTIAL

    def test_no_actions_reports_no_side_effects(self, tmp_db):
        out = self._journal([]).cancelled_disposition("task")
        assert "no side effects" in out
        assert "UNKNOWN" not in out

    def test_marks_in_flight_action_unknown(self, tmp_db):
        # bash completed; web_fetch was in flight (issued, no result yet) —
        # the first unanswered call is the in-flight boundary.
        msgs = [
            self._assistant("t1", "bash"),
            self._result("t1"),
            self._assistant("t2", "web_fetch"),
        ]
        out = self._journal(msgs, "t2").cancelled_disposition("task")
        assert out != "(task interrupted by user)"
        assert "Completed before cancel: bash." in out
        assert "In flight at cancel: web_fetch" in out
        assert "UNKNOWN" in out

    def test_unanswered_tool_is_in_flight_unknown(self, tmp_db):
        # An output-flowing bash SIGKILL'd mid-stream raises (no result row) —
        # it is the in-flight boundary and must read UNKNOWN, never completed.
        msgs = [self._assistant("t1", "bash")]  # issued, no result
        out = self._journal(msgs, "t1").cancelled_disposition("task")
        assert "In flight at cancel: bash" in out
        assert "UNKNOWN" in out
        assert "Completed before cancel" not in out

    def test_all_answered_reports_completed_no_in_flight(self, tmp_db):
        # Every issued call returned a result — cancel landed between turns,
        # nothing in flight. Each result carries its own disposition; the
        # summary just lists what completed, with no UNKNOWN boundary.
        msgs = [self._assistant("t1", "bash"), self._result("t1", "(killed)")]
        out = self._journal(msgs).cancelled_disposition("task")
        assert "Completed before cancel: bash." in out
        assert "In flight at cancel" not in out

    def test_boundary_is_first_unanswered_not_last(self, tmp_db):
        # Regression (bug-1): a turn issues [bash, web_fetch] executed
        # sequentially; cancel hits during bash (unanswered, side effects
        # possible) and web_fetch never runs. The in-flight UNKNOWN must be
        # bash, and the journal's executor witness must record web_fetch as
        # confirmed no-effect — NOT
        # the inverse. The old code took the LAST issued call, labelling the
        # never-run web_fetch UNKNOWN and the actually-in-flight bash "not
        # started" — inviting a re-run of the destructive bash.
        msgs = [
            Turn.assistant(
                "",
                tool_calls=(
                    ToolCall(id="t1", name="bash", arguments=""),
                    ToolCall(id="t2", name="web_fetch", arguments=""),
                ),
            )
        ]  # neither answered: bash raised mid-flight, web_fetch never ran
        out = self._journal(msgs, "t1").cancelled_disposition("task")
        assert "In flight at cancel: bash" in out
        assert "In flight at cancel: web_fetch" not in out
        assert "Confirmed no effect before cancel: web_fetch." in out

    def test_counts_and_confirmed_unstarted(self, tmp_db):
        # Turn 1 completes [bash, bash, read_file]; turn 2 issues
        # [web_fetch (in flight), search (never ran)]. Exercises the ×N
        # count summary, the first-gap boundary, and not-started.
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
        out = self._journal(msgs, "t4").cancelled_disposition("task")
        assert "Completed before cancel: bash×2, read_file." in out
        assert "In flight at cancel: web_fetch" in out
        assert "Confirmed no effect before cancel: search." in out

    def test_exec_task_routes_cancel_to_disposition(self, tmp_db):
        """_exec_task converts a GenerationCancelled from _run_agent into the
        honest disposition from the production execution journal."""
        session = _make_session()

        def fake_run_agent(agent_turns, **kwargs):
            journal = kwargs["execution_journal"]
            first = self._assistant("t1", "bash")
            first_result = self._result("t1")
            second = self._assistant("t2", "web_fetch")
            agent_turns.extend((first, first_result, second))
            journal.record_assistant(first)
            journal.mark_started("t1")
            journal.record_result(
                "t1",
                first_result.text,
                is_error=first_result.is_error,
                effect_status=first_result.effect_status,
            )
            journal.record_assistant(second)
            journal.mark_started("t2")
            raise GenerationCancelled()

        with patch.object(session, "_run_agent", side_effect=fake_run_agent):
            call_id, result = session._exec_task({"call_id": "c1", "prompt": "do x"})

        assert call_id == "c1"
        assert result != "(task interrupted by user)"
        assert "UNKNOWN" in result
        assert "web_fetch" in result  # in-flight boundary
        assert "bash" in result  # completed
        # The wrapper already published this exact result and stages its typed
        # status for the outer cancellation synthesizer's durable fold.
        assert session._cancelled_tool_results.get("c1") == _CancelledToolResult(
            detail=result,
            effect_status=EffectStatus.UNKNOWN,
            is_error=True,
            preview=None,
            live_emitted=True,
        )
        assert "c1" not in session._tool_status

    def test_execute_tools_keeps_exact_task_cancel_receipt_when_live_callback_raises(self, tmp_db):
        """The outer executor cannot replace the task shell's exact ledger.

        ``_exec_task`` publishes from inside the parent tool's nonzero
        generation context.  If its live callback fails, the real
        ``_execute_tools`` wrapper must still return the controller-authored
        cancellation disposition instead of routing the callback exception
        through its generic tool-error publisher and overwriting the receipt.
        """
        ui = NullUI()
        ui.on_tool_result = MagicMock(
            side_effect=[RuntimeError("first task result callback failed"), None]
        )
        session = _make_session(ui=ui)
        generation = session._claim_generation()
        issued_child = self._assistant("child-1", "bash")
        expected_journal = self._journal([issued_child], "child-1")
        expected_disposition = expected_journal.cancelled_disposition("task")

        def fake_run_agent(agent_turns, **kwargs):
            agent_turns.append(issued_child)
            journal = kwargs["execution_journal"]
            journal.record_assistant(issued_child)
            journal.mark_started("child-1")
            raise GenerationCancelled()

        prepared = {
            "call_id": "parent-1",
            "func_name": "task_agent",
            "prompt": "run a shell action",
            "needs_approval": False,
            "execute": session._exec_task,
        }
        with (
            patch.object(session, "_safe_prepare_tool", return_value=prepared),
            patch.object(session, "_run_agent", side_effect=fake_run_agent),
        ):
            results, feedback = session._execute_tools(
                [
                    {
                        "id": "parent-1",
                        "function": {
                            "name": "task_agent",
                            "arguments": '{"prompt": "run a shell action"}',
                        },
                    }
                ],
                my_generation=generation,
            )

        assert results == [("parent-1", expected_disposition)]
        assert feedback is None
        ui.on_tool_result.assert_called_once()
        assert session._cancelled_tool_results["parent-1"] == _CancelledToolResult(
            detail=expected_disposition,
            effect_status=EffectStatus.UNKNOWN,
            is_error=True,
            preview=None,
            live_emitted=False,
        )


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

    def test_acting_principal_is_a_sibling_channel_omitted_when_empty(self):
        """The audit identity joins the same envelope, never a fifth axis.

        An unattributed lane (wake / internal / CLI) writes NO key rather than
        an empty string, so a revocation query reading the column can treat
        presence as attribution — the convention the USER row's ``sender``
        already follows.
        """
        assert _tool_turn_meta(None, None, acting_principal="") is None
        assert json.loads(_tool_turn_meta(None, None, acting_principal="user-alice")) == {
            "acting_principal": "user-alice",
        }
        assert json.loads(
            _tool_turn_meta(EffectStatus.UNKNOWN, {"kind": "web"}, acting_principal="user-alice")
        ) == {
            "effect_status": "unknown",
            "preview": {"kind": "web"},
            "acting_principal": "user-alice",
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
        session = _make_registered_session(ui=ui)
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

    def test_unscoped_generation_still_runs_arm_duties(self, tmp_db):
        """Generation 0 means UNSCOPED, the convention ``_check_cancelled``
        and ``_CancelRef._superseded`` share: the ref fires the hook for a
        direct seam caller, so the hook's own gate must not refuse it.  A
        bare ``!=`` compare skips the duties on any session whose
        generation was ever claimed, silently recycling the previous
        turn's usage into this turn's estimate and losing the lane's
        health success."""
        from turnstone.core.session import _StreamTurnConsumer

        session = _make_session()
        session._generation = 3  # a prior send claimed generations
        consumer = _StreamTurnConsumer(session, my_generation=0)
        tracker = MagicMock()
        ref = _CancelRef(session, 0, on_first_append=consumer.on_stream_armed)
        consumer.begin_attempt(ref, tracker, MagicMock())

        session._last_usage = {"prompt_tokens": 99}
        session._assistant_pending_tokens = 42
        ref.append(MagicMock())

        assert session._last_usage is None
        assert session._assistant_pending_tokens == 0
        tracker.record_success.assert_called_once()


class TestOlderSitesAskTheSharedPredicate:
    """The two supersession sites that PREDATE the shared predicate ask it
    too, so generation 0 stays unscoped at both.

    Each carried its own inline copy of the formula, so the drift the
    helper exists to prevent had two live places to start from.  A bare
    ``!=`` in either reads a direct seam caller (generation 0) as an
    orphan: ``_check_cancelled`` would raise a cancel on a live turn, and
    ``_compaction_event`` would stamp a live compaction ``superseded`` —
    which suppresses its end notice, so the operator watching a real
    compaction fail would be told nothing at all.
    """

    def test_check_cancelled_leaves_an_unscoped_generation_alone(self, tmp_db):
        session = _make_session()
        session._generation = 7
        session._check_cancelled(0)  # unscoped — a direct seam caller
        session._check_cancelled(7)  # the live generation
        with pytest.raises(GenerationCancelled):
            session._check_cancelled(2)  # a real orphan

    def test_compaction_event_calls_an_unscoped_generation_live(self, tmp_db):
        class _Recorder(NullUI):
            def __init__(self):
                super().__init__()
                self.events = []

            def on_compaction(self, event):
                self.events.append(event)

        ui = _Recorder()
        session = _make_session(ui=ui)
        session._generation = 7
        failed_end = {"phase": "end", "ok": False, "reason": "cancelled", "trigger": "manual"}
        session._compaction_event(0, dict(failed_end))
        session._compaction_event(2, dict(failed_end))

        assert ui.events[0]["superseded"] is False
        assert ui.events[0]["notice"] is True  # the operator is told
        assert ui.events[1]["superseded"] is True
        assert ui.events[1]["notice"] is False


class TestSupersessionVerdictAgreement:
    """Every arm of one streaming turn must reach the SAME supersession
    verdict for the same generation shape.  Generation 0 is unscoped, so
    on a session whose generation was claimed earlier a Stop and a Ctrl-C
    must both finalize the display — a per-arm spelling once split them,
    finalizing on one path and not the other."""

    def _session_at_generation(self, gen, ui):
        session = _make_registered_session(ui=ui)
        session._generation = gen
        session.messages.append(Turn.user("hi"))
        return session

    def _drive(self, gen, kind):
        ui = NullUI()
        session = self._session_at_generation(gen, ui)

        def stream():
            yield StreamChunk(content_delta="partial answer")
            if kind == "stop":
                session.cancel()
                yield StreamChunk(content_delta=" unreachable")
            else:
                raise KeyboardInterrupt

        provider = arm_session(session, stream())
        assert provider is session._model_binding.lane.provider
        raised = None
        try:
            session._stream_response(0)
        except BaseException as exc:  # noqa: BLE001 — the class IS the observation
            raised = type(exc).__name__
        return raised, ui.stream_ends, session._cancelled_partial_msg

    def test_stop_and_ctrl_c_agree_on_an_unscoped_generation(self, tmp_db):
        stop_raised, stop_ends, partial = self._drive(3, "stop")
        kb_raised, kb_ends, _ = self._drive(3, "ctrl_c")

        assert stop_raised == "GenerationCancelled"
        assert kb_raised == "KeyboardInterrupt"
        # The verdict is "live" on BOTH arms: each finalizes the display.
        assert stop_ends == 1
        assert kb_ends == 1
        assert partial and partial["content"] == "partial answer"

    def test_superseded_generation_finalizes_on_neither_arm(self, tmp_db):
        # The scoped counterpart: a real orphan (its generation lost the
        # claim) must touch the UI on no arm at all.  It never reaches
        # one: the ref reads superseded, so ``model_turn`` refuses to
        # dispatch and the ladder converts that to a cancel — an orphan
        # issues no request and finalizes nothing.
        ui = NullUI()
        session = self._session_at_generation(5, ui)

        def stream():
            raise KeyboardInterrupt
            yield  # unreachable; makes this a generator

        provider = arm_session(session, stream())
        with pytest.raises(GenerationCancelled):
            session._stream_response(2)  # generation 2 lost the claim to 5
        assert ui.stream_ends == 0
        provider.create_streaming.assert_not_called()

    def test_unscoped_generation_finalizes_on_the_ctrl_c_arm(self, tmp_db):
        # Same arm, unscoped generation: the verdict flips to "live", so
        # the display IS finalized — the agreement this class pins.
        ui = NullUI()
        session = self._session_at_generation(5, ui)

        def stream():
            raise KeyboardInterrupt
            yield

        arm_session(session, stream())
        with pytest.raises(KeyboardInterrupt):
            session._stream_response(0)
        assert ui.stream_ends == 1


class TestOrphanGuardsBelowTheLadder:
    """The two supersession guards in ``_stream_response``'s own arms.

    They fire only when a force-cancel lands in the window BELOW the
    ladder's conversion: ``_model_turn_with_retry`` re-checks the
    generation before it classifies a death, so on every deterministic
    path an orphan's failure has already become ``GenerationCancelled``
    by the time it leaves the ladder.  These arms cover the sub-statement
    race where supersession arrives after that check — the same
    accepted-width window ``_CancelRef`` documents.  Reaching them means
    simulating the race at the seam directly; a scripted stream cannot,
    which is why the suite left both branches unexercised.

    What they protect: an orphaned thread must emit NOTHING, because the
    successor generation is already streaming into the same UI.
    """

    def _armed_death(self, session, exc):
        """A death observed as if supersession landed after the ladder's
        own generation check: the attempt streamed, a newer generation
        claimed the session, and only then does the failure surface."""

        def _seam(consumer, prepare_wire, my_generation, *, principal_id=None):
            assert principal_id is None
            consumer.begin_attempt(_CancelRef(session, my_generation), None, MagicMock())
            consumer._saw_chunk = True  # the attempt reached the display
            session._generation = my_generation + 1  # force-cancel lands
            raise exc

        return _seam

    def test_superseded_stream_death_emits_nothing(self, tmp_db):
        from turnstone.core.providers import IncompleteStreamError

        ui = NullUI()
        session = _make_session(ui=ui)
        session._generation = 1
        with (
            patch.object(
                session,
                "_model_turn_with_fallback",
                side_effect=self._armed_death(session, IncompleteStreamError("wire died")),
            ),
            pytest.raises(IncompleteStreamError),
        ):
            session._stream_response(1)

        # No retry theater, no finalize, no partial stashed: the live
        # successor owns the UI now.
        assert ui.stream_ends == 0
        assert ui.infos == []
        assert session._cancelled_partial_msg is None

    def test_superseded_keyboard_interrupt_emits_nothing(self, tmp_db):
        ui = NullUI()
        session = _make_session(ui=ui)
        session._generation = 1
        with (
            patch.object(
                session,
                "_model_turn_with_fallback",
                side_effect=self._armed_death(session, KeyboardInterrupt()),
            ),
            pytest.raises(KeyboardInterrupt),
        ):
            session._stream_response(1)

        assert ui.stream_ends == 0

    def test_live_generation_still_finalizes_on_ctrl_c(self, tmp_db):
        # The other side of the same branch, without the supersession.
        ui = NullUI()
        session = _make_session(ui=ui)
        session._generation = 1

        def _seam(consumer, prepare_wire, my_generation, *, principal_id=None):
            assert principal_id is None
            consumer.begin_attempt(_CancelRef(session, my_generation), None, MagicMock())
            consumer._saw_chunk = True
            raise KeyboardInterrupt

        with (
            patch.object(session, "_model_turn_with_fallback", side_effect=_seam),
            pytest.raises(KeyboardInterrupt),
        ):
            session._stream_response(1)

        assert ui.stream_ends == 1
