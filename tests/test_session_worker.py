"""Unit tests for ``turnstone.core.session_worker``.

The shared worker dispatch is load-bearing for both the interactive
``/v1/api/workstreams/{ws_id}/send`` HTTP handler and the coordinator
``CoordinatorAdapter.send`` path. Tests cover the four invariants the
module must hold:

* live worker → enqueue, no thread spawn
* queue.Full → ``False`` (caller surfaces 429)
* concurrent ``send`` calls produce exactly one worker thread
  (Stage 1 bug-1 — the racy ``Thread.is_alive()`` gate stays caught)
* ``_worker_running`` cleared in ``finally`` even on uncaught exception

Callers pass no-arg closures, so this module never touches
``ws.session`` — keeps the contract narrow and lets watch-style
dispatchers drive a session that isn't installed on ``ws``.
"""

from __future__ import annotations

import queue
import threading
from typing import Any

from turnstone.core import session_worker
from turnstone.core.workstream import Workstream


class _SendSession:
    """ChatSession-shaped stub recording send / queue_message calls."""

    def __init__(
        self,
        *,
        queue_full: bool = False,
        queue_raises: BaseException | None = None,
        send_gate: threading.Event | None = None,
        send_raises: BaseException | None = None,
    ) -> None:
        self.send_calls: list[str] = []
        self.queue_calls: list[str] = []
        self._queue_full = queue_full
        self._queue_raises = queue_raises
        # Lets a test pin a worker inside ``run`` while a second thread
        # races through ``send`` — proves the lock gate (not
        # Thread.is_alive) is what serialises them.
        self._send_gate = send_gate
        self._send_raises = send_raises

    def send(self, message: str) -> None:
        if self._send_gate is not None:
            self._send_gate.wait(timeout=2.0)
        if self._send_raises is not None:
            raise self._send_raises
        self.send_calls.append(message)

    def queue_message(self, message: str) -> None:
        if self._queue_full:
            raise queue.Full
        if self._queue_raises is not None:
            raise self._queue_raises
        self.queue_calls.append(message)


def _make_ws(session: Any = None) -> Workstream:
    ws = Workstream(id="ws-aaaaaaaa", name="ws-aaaa")
    ws.session = session  # type: ignore[assignment]
    return ws


def _send_message(ws: Workstream, session: _SendSession, msg: str) -> bool:
    """Convenience wrapper mirroring the canonical caller shape."""
    return session_worker.send(
        ws,
        enqueue=lambda: session.queue_message(msg),
        run=lambda: session.send(msg),
        thread_name=f"test-worker-{ws.id[:8]}",
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_spawn_worker_runs_target_and_clears_flag() -> None:
    session = _SendSession()
    ws = _make_ws(session)
    ok = _send_message(ws, session, "hello")
    assert ok is True
    assert ws.worker_thread is not None
    ws.worker_thread.join(timeout=2.0)
    assert session.send_calls == ["hello"]
    assert ws._worker_running is False


def test_reuse_path_when_worker_running_takes_enqueue() -> None:
    session = _SendSession()
    ws = _make_ws(session)
    ws._worker_running = True  # simulate a live worker

    ok = _send_message(ws, session, "queued")
    assert ok is True
    # No thread spawned on the reuse path.
    assert ws.worker_thread is None
    assert session.send_calls == []
    assert session.queue_calls == ["queued"]
    # Flag stays True — the caller didn't claim ownership.
    assert ws._worker_running is True


# ---------------------------------------------------------------------------
# Queue.Full / enqueue failure
# ---------------------------------------------------------------------------


def test_enqueue_queue_full_returns_false_no_spawn() -> None:
    session = _SendSession(queue_full=True)
    ws = _make_ws(session)
    ws._worker_running = True

    ok = _send_message(ws, session, "hello")
    assert ok is False
    assert session.send_calls == []
    assert session.queue_calls == []
    assert ws.worker_thread is None
    # _worker_running unchanged — the live worker still owns it.
    assert ws._worker_running is True


def test_enqueue_unexpected_exception_returns_false_logged() -> None:
    session = _SendSession(queue_raises=RuntimeError("boom"))
    ws = _make_ws(session)
    ws._worker_running = True

    ok = _send_message(ws, session, "hello")
    assert ok is False
    assert session.send_calls == []
    assert ws.worker_thread is None
    assert ws._worker_running is True


# ---------------------------------------------------------------------------
# _worker_running lifecycle
# ---------------------------------------------------------------------------


def test_worker_finally_clears_running_flag_on_exception() -> None:
    session = _SendSession(send_raises=RuntimeError("worker-failed"))
    ws = _make_ws(session)

    ok = _send_message(ws, session, "hello")
    assert ok is True
    assert ws.worker_thread is not None
    ws.worker_thread.join(timeout=2.0)
    # Defense-in-depth: even though run() raised, _worker_running is False.
    assert ws._worker_running is False


def test_worker_finally_clears_flag_when_run_swallows() -> None:
    """Mirrors the call-site contract: run() catches its own exceptions
    for UI surfacing; we still clear the flag in finally."""
    session = _SendSession()
    ws = _make_ws(session)

    captured: list[BaseException] = []

    def run() -> None:
        try:
            session.send("hello")
            raise RuntimeError("after-send")
        except Exception as exc:
            captured.append(exc)

    ok = session_worker.send(
        ws,
        enqueue=lambda: session.queue_message("hello"),
        run=run,
    )
    assert ok is True
    assert ws.worker_thread is not None
    ws.worker_thread.join(timeout=2.0)
    assert isinstance(captured[0], RuntimeError)
    assert ws._worker_running is False


# ---------------------------------------------------------------------------
# Concurrency — Stage 1 bug-1 regression
# ---------------------------------------------------------------------------


def test_concurrent_send_produces_exactly_one_worker_thread() -> None:
    """Two simultaneous send() calls must land as exactly one worker
    spawn and one queued message — not two parallel workers on the
    same ChatSession.

    The send_gate pins the worker inside session.send while the second
    caller races through; the only way the second caller can succeed
    is via the enqueue path. If the lock gate were keyed on
    Thread.is_alive instead of _worker_running, the loser could spawn
    a second worker before the winner reaches session.send.
    """
    send_gate = threading.Event()
    session = _SendSession(send_gate=send_gate)
    ws = _make_ws(session)

    results: list[bool] = []
    results_lock = threading.Lock()
    start_barrier = threading.Barrier(2)

    def _caller(msg: str) -> None:
        start_barrier.wait(timeout=1.0)
        ok = _send_message(ws, session, msg)
        with results_lock:
            results.append(ok)

    t1 = threading.Thread(target=_caller, args=("first",))
    t2 = threading.Thread(target=_caller, args=("second",))
    t1.start()
    t2.start()
    t1.join(timeout=3.0)
    t2.join(timeout=3.0)
    assert not t1.is_alive() and not t2.is_alive()

    # At this point session.send is still pinned on send_gate; the
    # second caller MUST have taken the enqueue path.
    assert len(session.queue_calls) == 1, (
        f"expected exactly one queued message; got {session.queue_calls}"
    )

    # Release the worker, verify final state.
    send_gate.set()
    assert ws.worker_thread is not None
    ws.worker_thread.join(timeout=3.0)

    assert results == [True, True]
    assert len(session.send_calls) == 1
    assert set(session.send_calls + session.queue_calls) == {"first", "second"}
    assert ws._worker_running is False


def test_thread_name_default_uses_ws_prefix() -> None:
    session = _SendSession()
    ws = _make_ws(session)
    ok = session_worker.send(
        ws,
        enqueue=lambda: session.queue_message("hello"),
        run=lambda: session.send("hello"),
    )
    assert ok is True
    assert ws.worker_thread is not None
    assert ws.worker_thread.name.startswith("session-worker-")
    ws.worker_thread.join(timeout=2.0)


def test_thread_name_explicit_override() -> None:
    session = _SendSession()
    ws = _make_ws(session)
    ok = session_worker.send(
        ws,
        enqueue=lambda: session.queue_message("hello"),
        run=lambda: session.send("hello"),
        thread_name="custom-name",
    )
    assert ok is True
    assert ws.worker_thread is not None
    assert ws.worker_thread.name == "custom-name"
    ws.worker_thread.join(timeout=2.0)


def test_does_not_deadlock_when_run_briefly_grabs_ws_lock() -> None:
    """Sanity check: ``run`` is invoked OUTSIDE ``ws._lock``. A worker
    body that briefly takes the lock (e.g. to update worker state)
    must not deadlock with the dispatch path."""
    session = _SendSession()
    ws = _make_ws(session)

    def run() -> None:
        with ws._lock:
            pass  # would deadlock if dispatch held the lock here
        session.send("hello")

    ok = session_worker.send(
        ws,
        enqueue=lambda: session.queue_message("hello"),
        run=run,
    )
    assert ok is True
    assert ws.worker_thread is not None
    ws.worker_thread.join(timeout=2.0)
    assert session.send_calls == ["hello"]
    assert ws._worker_running is False
