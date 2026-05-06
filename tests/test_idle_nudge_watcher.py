"""Unit tests for :class:`IdleNudgeWatcher`.

Drives a fake :class:`SessionManager` that mimics the real one's
``subscribe_to_state`` / ``get`` contract.  The watcher itself
dispatches via ``turnstone.core.session_worker.send``; we patch that
module-level function to capture calls without spawning real threads.
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any
from unittest.mock import patch

import pytest

from turnstone.core.metacognition import IdleNudgeWatcher
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
