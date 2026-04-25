"""Tests for _make_watch_dispatch error/cancel handling and concurrency guards."""

import queue
import threading
import time

from turnstone.core.session import GenerationCancelled
from turnstone.core.workstream import Workstream
from turnstone.server import _make_watch_dispatch


class _StubSession:
    """Minimal ChatSession stand-in with controllable send() behaviour."""

    def __init__(self, *, side_effect=None):
        self._watch_pending: queue.Queue = queue.Queue(maxsize=20)
        self._side_effect = side_effect

    def send(self, msg: str) -> None:
        if self._side_effect is not None:
            raise self._side_effect


class _RecordingUI:
    """Track calls made by the dispatch error handlers."""

    def __init__(self):
        self.errors: list[str] = []
        self.state_changes: list[str] = []
        self.stream_end_calls: int = 0

    # -- SessionUI protocol stubs used by the dispatch code --

    def on_error(self, message: str) -> None:
        self.errors.append(message)

    def on_state_change(self, state: str) -> None:
        self.state_changes.append(state)

    def on_stream_end(self) -> None:
        self.stream_end_calls += 1


# ── helpers ──────────────────────────────────────────────────────────────────


def _wait_for_worker(ws: Workstream, timeout: float = 2.0) -> None:
    """Block until the worker thread started by dispatch() finishes."""
    t = ws.worker_thread
    if t is not None:
        t.join(timeout)
        assert not t.is_alive(), "worker thread did not finish in time"


# ── GenerationCancelled path ────────────────────────────────────────────────


def test_cancelled_emits_stream_end_and_idle():
    session = _StubSession(side_effect=GenerationCancelled())
    ws = Workstream()
    ui = _RecordingUI()

    dispatch = _make_watch_dispatch(ws, session, ui)
    dispatch("hello")
    _wait_for_worker(ws)

    assert ui.stream_end_calls == 1
    assert ui.state_changes == ["idle"]
    assert ui.errors == []


# ── Generic exception path ──────────────────────────────────────────────────


def test_exception_emits_stream_end_and_error():
    session = _StubSession(side_effect=RuntimeError("boom"))
    ws = Workstream()
    ui = _RecordingUI()

    dispatch = _make_watch_dispatch(ws, session, ui)
    dispatch("hello")
    _wait_for_worker(ws)

    assert ui.stream_end_calls == 1
    assert ui.state_changes == ["error"]
    assert len(ui.errors) == 1
    assert "boom" in ui.errors[0]


# ── Worker-thread identity guard ────────────────────────────────────────────


def test_abandoned_thread_emits_no_events():
    """After force-cancel sets worker_thread=None, the old thread must not
    emit stream_end or state changes."""
    barrier = threading.Event()

    class _BlockingSession(_StubSession):
        def send(self, msg: str) -> None:
            barrier.wait(timeout=5)
            raise RuntimeError("late error")

    session = _BlockingSession()
    ws = Workstream()
    ui = _RecordingUI()

    dispatch = _make_watch_dispatch(ws, session, ui)
    dispatch("hello")

    # Simulate force-cancel: clear the worker_thread reference.
    ws.worker_thread = None
    barrier.set()

    # Wait for the thread to actually complete (it's still running).
    time.sleep(0.3)

    assert ui.stream_end_calls == 0
    assert ui.state_changes == []
    assert ui.errors == []


# ── Path A: busy workstream enqueue ─────────────────────────────────────────


def test_busy_workstream_enqueues_message():
    """When the workstream already has a live worker, dispatch enqueues."""
    session = _StubSession()
    ws = Workstream()
    ui = _RecordingUI()

    # Simulate a live worker — session_worker.send gates on
    # ``_worker_running``, not ``Thread.is_alive``.
    ws._worker_running = True

    dispatch = _make_watch_dispatch(ws, session, ui)
    dispatch("queued msg")

    item = session._watch_pending.get_nowait()
    assert item == {"message": "queued msg"}


def test_busy_workstream_drops_on_full_queue():
    """When the pending queue is full, dispatch drops the message."""
    session = _StubSession()
    # Fill the queue to capacity.
    for i in range(20):
        session._watch_pending.put_nowait({"message": f"msg{i}"})

    ws = Workstream()
    ui = _RecordingUI()

    ws._worker_running = True  # simulate a live worker

    dispatch = _make_watch_dispatch(ws, session, ui)
    # Should not block or raise — just log a warning and drop.
    dispatch("overflow msg")

    assert session._watch_pending.full()


# ── Lock guard ───────────────────────────────────────────────────────────────


def test_dispatch_holds_lock_during_thread_start():
    """Dispatch acquires ws._lock before checking/starting the worker."""
    session = _StubSession()
    ws = Workstream()
    ui = _RecordingUI()

    acquire_count = 0
    inner = ws._lock

    class _CountingLock:
        def __enter__(self):
            nonlocal acquire_count
            acquire_count += 1
            return inner.__enter__()

        def __exit__(self, *args):
            return inner.__exit__(*args)

    ws._lock = _CountingLock()  # type: ignore[assignment]

    dispatch = _make_watch_dispatch(ws, session, ui)
    dispatch("hello")
    _wait_for_worker(ws)

    assert acquire_count >= 1


# ── Happy path ───────────────────────────────────────────────────────────────


def test_successful_send_no_error_events():
    """Normal send() completion should not trigger error/cancel events."""
    session = _StubSession()  # send() does nothing (success)
    ws = Workstream()
    ui = _RecordingUI()

    dispatch = _make_watch_dispatch(ws, session, ui)
    dispatch("hello")
    _wait_for_worker(ws)

    assert ui.stream_end_calls == 0
    assert ui.state_changes == []
    assert ui.errors == []
