"""Unit tests for :class:`IdleNudgeWatcher`.

Drives a fake :class:`SessionManager` that mimics the real one's
``subscribe_to_state`` / ``get`` contract.  The watcher itself
dispatches via ``turnstone.core.session_worker.send``; we patch that
module-level function to capture calls without spawning real threads.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import Any
from unittest.mock import patch

import pytest

from turnstone.core.idle_nudge_watcher import IdleNudgeWatcher, wake_workstream_if_pending
from turnstone.core.nudge_queue import NudgeQueue
from turnstone.core.workstream import WorkstreamState


class _FakeSession:
    def __init__(self) -> None:
        self._nudge_queue = NudgeQueue()
        self.deliver_wake_nudge_from_queue_called = 0

    def deliver_wake_nudge_from_queue(self) -> None:
        self.deliver_wake_nudge_from_queue_called += 1


class _FakeWorkstream:
    def __init__(self, ws_id: str = "ws-test") -> None:
        self.id = ws_id
        self.state = WorkstreamState.IDLE
        self.session: _FakeSession | None = _FakeSession()
        self._lock = threading.Lock()
        self._worker_running = False
        self._closed = False
        self.worker_thread: Any = None


class _FakeManager:
    """Mimics SessionManager's subscribe-to-state surface without a DB."""

    def __init__(self) -> None:
        self._workstreams: dict[str, _FakeWorkstream] = {}
        self._subscribers: list[Any] = []
        self._subscribers_lock = threading.Lock()

    def add_ws(self, ws: _FakeWorkstream) -> None:
        self._workstreams[ws.id] = ws

    def get(self, ws_id: str) -> _FakeWorkstream | None:
        return self._workstreams.get(ws_id)

    def subscribe_to_state(self, callback: Any) -> None:
        with self._subscribers_lock:
            self._subscribers.append(callback)

    def unsubscribe_from_state(self, callback: Any) -> None:
        with self._subscribers_lock, contextlib.suppress(ValueError):
            self._subscribers.remove(callback)

    def fire_state(self, ws_id: str, state: WorkstreamState) -> None:
        """Mirror SessionManager.set_state's subscriber-fan-out behaviour."""
        with self._subscribers_lock:
            subs = list(self._subscribers)
        for cb in subs:
            # Match contextlib.suppress(Exception) in real SessionManager.
            with contextlib.suppress(Exception):
                cb(ws_id, state)


@pytest.fixture
def fake_mgr_and_ws() -> tuple[_FakeManager, _FakeWorkstream]:
    mgr = _FakeManager()
    ws = _FakeWorkstream()
    mgr.add_ws(ws)
    return mgr, ws


class TestIdleNudgeWatcher:
    def test_idle_event_with_empty_queue_no_op(self, fake_mgr_and_ws):
        mgr, ws = fake_mgr_and_ws
        watcher = IdleNudgeWatcher(mgr)
        watcher.start()
        with patch("turnstone.core.session_worker.send") as mock_send:
            mgr.fire_state(ws.id, WorkstreamState.IDLE)
            assert mock_send.call_count == 0

    def test_idle_event_with_pending_nudge_dispatches(self, fake_mgr_and_ws):
        mgr, ws = fake_mgr_and_ws
        ws.session._nudge_queue.enqueue("idle_children", "your kids", "any")
        watcher = IdleNudgeWatcher(mgr)
        watcher.start()
        with patch("turnstone.core.session_worker.send") as mock_send:
            mgr.fire_state(ws.id, WorkstreamState.IDLE)
            assert mock_send.call_count == 1
            kwargs = mock_send.call_args.kwargs
            # `enqueue=lambda: None` — verify by calling and checking no-op.
            assert kwargs["enqueue"]() is None
            # `run` should call deliver_wake_nudge_from_queue when invoked.
            kwargs["run"]()
            assert ws.session.deliver_wake_nudge_from_queue_called == 1
            assert kwargs["thread_name"].startswith("wake-nudge-")

    def test_non_idle_state_ignored(self, fake_mgr_and_ws):
        mgr, ws = fake_mgr_and_ws
        ws.session._nudge_queue.enqueue("foo", "bar", "any")
        watcher = IdleNudgeWatcher(mgr)
        watcher.start()
        with patch("turnstone.core.session_worker.send") as mock_send:
            for state in (
                WorkstreamState.RUNNING,
                WorkstreamState.THINKING,
                WorkstreamState.ATTENTION,
                WorkstreamState.ERROR,
            ):
                mgr.fire_state(ws.id, state)
            assert mock_send.call_count == 0

    def test_unknown_ws_ignored(self, fake_mgr_and_ws):
        mgr, _ws = fake_mgr_and_ws
        watcher = IdleNudgeWatcher(mgr)
        watcher.start()
        with patch("turnstone.core.session_worker.send") as mock_send:
            mgr.fire_state("ghost", WorkstreamState.IDLE)
            assert mock_send.call_count == 0

    def test_session_none_ignored(self, fake_mgr_and_ws):
        mgr, ws = fake_mgr_and_ws
        ws.session = None  # workstream loaded but session not yet built
        watcher = IdleNudgeWatcher(mgr)
        watcher.start()
        with patch("turnstone.core.session_worker.send") as mock_send:
            mgr.fire_state(ws.id, WorkstreamState.IDLE)
            assert mock_send.call_count == 0

    def test_start_is_idempotent(self, fake_mgr_and_ws):
        mgr, ws = fake_mgr_and_ws
        watcher = IdleNudgeWatcher(mgr)
        watcher.start()
        watcher.start()  # no-op
        ws.session._nudge_queue.enqueue("foo", "bar", "any")
        with patch("turnstone.core.session_worker.send") as mock_send:
            mgr.fire_state(ws.id, WorkstreamState.IDLE)
            # Only one subscriber was registered despite the double-start.
            assert mock_send.call_count == 1

    def test_shutdown_unsubscribes(self, fake_mgr_and_ws):
        mgr, ws = fake_mgr_and_ws
        ws.session._nudge_queue.enqueue("foo", "bar", "any")
        watcher = IdleNudgeWatcher(mgr)
        watcher.start()
        watcher.shutdown()
        with patch("turnstone.core.session_worker.send") as mock_send:
            mgr.fire_state(ws.id, WorkstreamState.IDLE)
            assert mock_send.call_count == 0

    def test_shutdown_is_idempotent(self, fake_mgr_and_ws):
        mgr, _ws = fake_mgr_and_ws
        watcher = IdleNudgeWatcher(mgr)
        watcher.start()
        watcher.shutdown()
        watcher.shutdown()  # no error


class TestWakeWorkstreamIfPending:
    """Direct tests for the shared wake gate.

    The IDLE-transition path (via the watcher) is covered above; these
    pin the gates the watch dispatch closure relies on when it calls
    the helper directly, with no state event involved.
    """

    def test_wakes_idle_ws_with_pending_entry(self, fake_mgr_and_ws):
        _mgr, ws = fake_mgr_and_ws
        ws.session._nudge_queue.enqueue("watch_triggered", "output", "any")
        with patch("turnstone.core.session_worker.send", return_value=True) as mock_send:
            assert wake_workstream_if_pending(ws) is True
            assert mock_send.call_count == 1
            kwargs = mock_send.call_args.kwargs
            assert kwargs["enqueue"]() is None
            kwargs["run"]()
            assert ws.session.deliver_wake_nudge_from_queue_called == 1
            assert kwargs["thread_name"].startswith("wake-nudge-")

    def test_skips_session_none(self, fake_mgr_and_ws):
        _mgr, ws = fake_mgr_and_ws
        ws.session = None
        with patch("turnstone.core.session_worker.send") as mock_send:
            assert wake_workstream_if_pending(ws) is False
            assert mock_send.call_count == 0

    def test_skips_closed_ws(self, fake_mgr_and_ws):
        """A workstream mid-``close()`` must not get a wake spawned on
        its torn-down session, even while its ``state`` field still
        reads IDLE (there is no CLOSED member — close uses the
        ``_closed`` tombstone)."""
        _mgr, ws = fake_mgr_and_ws
        ws.session._nudge_queue.enqueue("watch_triggered", "output", "any")
        ws._closed = True
        with patch("turnstone.core.session_worker.send") as mock_send:
            assert wake_workstream_if_pending(ws) is False
            assert mock_send.call_count == 0

    def test_skips_non_idle_states(self, fake_mgr_and_ws):
        """Busy states imply a live worker that drains at its own seams;
        ERROR stays parked for the operator — neither gets a wake."""
        _mgr, ws = fake_mgr_and_ws
        ws.session._nudge_queue.enqueue("watch_triggered", "output", "any")
        with patch("turnstone.core.session_worker.send") as mock_send:
            for state in (
                WorkstreamState.RUNNING,
                WorkstreamState.THINKING,
                WorkstreamState.ATTENTION,
                WorkstreamState.ERROR,
            ):
                ws.state = state
                assert wake_workstream_if_pending(ws) is False
            assert mock_send.call_count == 0

    def test_skips_tool_only_entries(self, fake_mgr_and_ws):
        """Tool-channel entries belong to the next tool-result seam — a
        synthetic empty user turn can't drain them, so no wake."""
        _mgr, ws = fake_mgr_and_ws
        ws.session._nudge_queue.enqueue("tool_error", "check memories", "tool")
        with patch("turnstone.core.session_worker.send") as mock_send:
            assert wake_workstream_if_pending(ws) is False
            assert mock_send.call_count == 0

    def test_refuses_non_nudgequeue_stub(self, fake_mgr_and_ws):
        """The gate refuses on TYPE, not just presence: a mock session's
        auto-created ``_nudge_queue`` answers ``has_pending`` truthily
        while its ``deliver_wake_nudge_from_queue`` consumes nothing —
        with the worker-exit backstop re-running this gate after every
        exit, one worker on such a session would respawn wake workers
        forever (the storm that took down the full-suite CI run).  Only
        a real :class:`NudgeQueue` carries the drain semantics the wake
        contract needs."""
        from unittest.mock import MagicMock

        _mgr, ws = fake_mgr_and_ws
        ws.session._nudge_queue = MagicMock()  # truthy has_pending, no real drain
        with patch("turnstone.core.session_worker.send") as mock_send:
            assert wake_workstream_if_pending(ws) is False
            assert mock_send.call_count == 0

    def test_dispatched_path_logs_trigger(self, fake_mgr_and_ws, caplog):
        """A fresh spawn — ``send`` returns True without touching the
        passed ``enqueue`` — emits ``nudge_wake.dispatched`` tagged with
        the trigger label (structlog renders the event name + ``%s``
        placeholders into ``msg``; substring-match like the sibling
        nudge_queue tests)."""
        _mgr, ws = fake_mgr_and_ws
        ws.session._nudge_queue.enqueue("watch_triggered", "output", "any")
        with (
            patch("turnstone.core.session_worker.send", return_value=True) as mock_send,
            caplog.at_level(logging.INFO, logger="turnstone.core.idle_nudge_watcher"),
        ):
            assert wake_workstream_if_pending(ws, trigger="idle-transition") is True
            assert mock_send.call_count == 1
        dispatched = [r for r in caplog.records if "nudge_wake.dispatched" in r.getMessage()]
        assert len(dispatched) == 1
        assert dispatched[0].levelno == logging.INFO
        assert "trigger=" in dispatched[0].getMessage()
        # The reuse-path drop line must not appear on a fresh spawn.
        assert not any("nudge_wake.deferred_worker_busy" in r.getMessage() for r in caplog.records)

    def test_deferred_path_logs_worker_busy(self, fake_mgr_and_ws, caplog):
        """The reuse path — ``send`` invokes the passed ``enqueue`` and
        returns True — emits ``nudge_wake.deferred_worker_busy`` instead
        of ``dispatched``.  The entry stays owed to the owning worker's
        exit backstop; the return value is still True."""
        _mgr, ws = fake_mgr_and_ws
        ws.session._nudge_queue.enqueue("watch_triggered", "output", "any")

        def _reuse_send(_ws: Any, *, enqueue: Any, run: Any, thread_name: Any) -> bool:
            # Mimic a live worker owning the workstream: send routes the
            # wake to the no-op enqueue rather than spawning a daemon.
            enqueue()
            return True

        with (
            patch("turnstone.core.session_worker.send", side_effect=_reuse_send) as mock_send,
            caplog.at_level(logging.INFO, logger="turnstone.core.idle_nudge_watcher"),
        ):
            assert wake_workstream_if_pending(ws, trigger="idle-transition") is True
            assert mock_send.call_count == 1
        deferred = [
            r for r in caplog.records if "nudge_wake.deferred_worker_busy" in r.getMessage()
        ]
        assert len(deferred) == 1
        assert deferred[0].levelno == logging.INFO
        assert "trigger=" in deferred[0].getMessage()
        assert not any("nudge_wake.dispatched" in r.getMessage() for r in caplog.records)

    def test_refused_path_logs_refusal(self, fake_mgr_and_ws, caplog):
        """``send`` refusing outright — its authoritative under-lock
        ``_closed`` re-check caught a teardown the gate's lockless peek
        missed — emits ``nudge_wake.refused``: a dropped wake must stay
        traceable to its trigger, not vanish silently."""
        _mgr, ws = fake_mgr_and_ws
        ws.session._nudge_queue.enqueue("watch_triggered", "output", "any")
        with (
            patch("turnstone.core.session_worker.send", return_value=False) as mock_send,
            caplog.at_level(logging.INFO, logger="turnstone.core.idle_nudge_watcher"),
        ):
            assert wake_workstream_if_pending(ws, trigger="watch-fire") is False
            assert mock_send.call_count == 1
        refused = [r for r in caplog.records if "nudge_wake.refused" in r.getMessage()]
        assert len(refused) == 1
        assert refused[0].levelno == logging.INFO
        assert "trigger=" in refused[0].getMessage()
        assert not any("nudge_wake.dispatched" in r.getMessage() for r in caplog.records)
        assert not any("nudge_wake.deferred_worker_busy" in r.getMessage() for r in caplog.records)
