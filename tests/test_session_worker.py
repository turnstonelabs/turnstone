"""Unit tests for ``turnstone.core.session_worker``.

The shared worker dispatch is load-bearing for both the interactive
``/v1/api/workstreams/{ws_id}/send`` HTTP handler and the coordinator
``CoordinatorAdapter.send`` path. Tests cover the five invariants the
module must hold:

* live worker → enqueue, no thread spawn
* queue.Full → ``False`` (caller surfaces 429)
* concurrent ``send`` calls produce exactly one worker thread
  (Stage 1 bug-1 — the racy ``Thread.is_alive()`` gate stays caught)
* ``_worker_running`` cleared in ``finally`` even on uncaught exception
* ownership-clear wake backstop: a worker exiting with USER_DRAIN
  nudges queued on an IDLE workstream spawns the wake send that the
  IDLE fan-out (which ran on this worker's own thread) had to drop

Callers pass no-arg closures, so dispatch never touches ``ws.session``;
the exit backstop only PEEKS it defensively (``getattr`` for
``_nudge_queue``, bail on stubs) — watch-style dispatchers can still
drive a session that isn't installed on ``ws``.
"""

from __future__ import annotations

import queue
import threading
from typing import Any

from tests._helpers import wait_until as _wait_until
from turnstone.core import session_worker
from turnstone.core.nudge_queue import USER_DRAIN, NudgeQueue
from turnstone.core.workstream import Workstream, WorkstreamState


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


def test_closed_workstream_refused_no_spawn() -> None:
    """Authoritative closed-check: ``close()`` sets ``_closed`` under
    ``ws._lock``, so a wake (or send) racing it must be refused HERE —
    the wake gate's lockless peek can go stale, and a spawn past this
    point would run a full unattended turn (inference, tool calls,
    storage writes) on a workstream whose ``ws_closed`` already fired.
    """
    session = _SendSession()
    ws = _make_ws(session)
    ws._closed = True

    ok = _send_message(ws, session, "hello")

    assert ok is False
    assert session.send_calls == []
    assert session.queue_calls == []
    assert ws.worker_thread is None
    assert ws._worker_running is False


def test_closed_workstream_refused_on_reuse_path_too() -> None:
    """The refusal precedes the enqueue branch: no interjection is queued
    onto a session whose workstream is already closed."""
    session = _SendSession()
    ws = _make_ws(session)
    ws._worker_running = True
    ws._closed = True

    ok = _send_message(ws, session, "hello")

    assert ok is False
    assert session.queue_calls == []
    assert ws._worker_running is True  # untouched — not ours to clear


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


def test_abandoned_worker_does_not_clear_successor_running_flag() -> None:
    """A force-cancel abandons the worker (``ws.worker_thread`` is cleared /
    reassigned to a successor).  When the abandoned thread finishes late, its
    ``finally`` must NOT clear ``_worker_running`` out from under the live
    successor — otherwise a third send sees ``_worker_running=False`` and
    spawns a second concurrent worker on the same session."""
    send_gate = threading.Event()
    session = _SendSession(send_gate=send_gate)
    ws = _make_ws(session)

    ok = _send_message(ws, session, "hello")
    assert ok is True
    abandoned = ws.worker_thread
    assert abandoned is not None

    # Simulate force-abandon + a successor send claiming ownership while the
    # original worker is still pinned inside run().
    sentinel = threading.Thread(target=lambda: None, name="successor")
    with ws._lock:
        ws.worker_thread = sentinel
        ws._worker_running = True

    # Release the abandoned worker; it runs its finally.
    send_gate.set()
    abandoned.join(timeout=3.0)
    assert not abandoned.is_alive()

    # The successor's ownership is intact — the abandoned worker did not
    # clobber the flag or the thread handle.
    assert ws._worker_running is True
    assert ws.worker_thread is sentinel


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


class _WakeCapableSession(_SendSession):
    """Adds the ChatSession surface the exit backstop peeks at."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._nudge_queue = NudgeQueue()
        self.deliver_calls = 0
        self.deliver_thread_names: list[str] = []
        self.delivered = threading.Event()

    def deliver_wake_nudge_from_queue(self) -> None:
        # Mirror the real contract: the wake drains its own queue, so
        # the wake worker's OWN exit backstop sees nothing pending and
        # the chain converges instead of spawning wakes forever.
        self.deliver_calls += 1
        self.deliver_thread_names.append(threading.current_thread().name)
        self._nudge_queue.drain(USER_DRAIN)
        self.delivered.set()


class TestWorkerExitWakeBackstop:
    """A worker exiting while its (idle) workstream has USER_DRAIN
    nudges queued spawns the wake send the IDLE fan-out had to drop.

    Production shape being modelled: ``set_state(IDLE)`` fires its
    subscribers on the worker thread from inside ``run()`` —
    ``CoordinatorIdleObserver`` enqueues ``idle_children``, then
    ``IdleNudgeWatcher``'s wake dispatch lands on the reuse path
    (this very worker still owns the flag) and no-ops.  The enqueue
    inside ``run`` below stands in for that observer enqueue.
    """

    def test_worker_exit_delivers_pending_wake(self) -> None:
        session = _WakeCapableSession()
        ws = _make_ws(session)
        assert ws.state is WorkstreamState.IDLE  # dataclass default

        def run() -> None:
            # What the IDLE fan-out's observer does, on this thread.
            session._nudge_queue.enqueue("idle_children", "kids waiting", "any")

        ok = session_worker.send(ws, enqueue=lambda: None, run=run)
        assert ok is True

        # The wake is delivered on a fresh wake-named worker thread…
        assert session.delivered.wait(timeout=2.0), (
            "exit backstop did not deliver the pending nudge"
        )
        assert session.deliver_thread_names[0].startswith("wake-nudge-")
        # …after which the wake worker's own exit backstop sees an empty
        # queue and the chain converges: flag at rest, exactly one deliver.
        _wait_until(lambda: ws._worker_running is False)
        assert session.deliver_calls == 1
        assert len(session._nudge_queue) == 0

    def test_worker_exit_no_wake_when_queue_empty(self) -> None:
        session = _WakeCapableSession()
        ws = _make_ws(session)

        ok = session_worker.send(ws, enqueue=lambda: None, run=lambda: None)
        assert ok is True
        original = ws.worker_thread
        assert original is not None
        original.join(timeout=2.0)

        assert ws.worker_thread is original  # no wake spawned
        assert session.deliver_calls == 0
        assert ws._worker_running is False

    def test_worker_exit_no_wake_for_stub_session_without_queue(self) -> None:
        """The narrow-contract escape hatch: a session without a
        ``_nudge_queue`` (watch-style stubs) is skipped by the shared
        wake gate's own defensive peek — no AttributeError, no wake."""
        session = _SendSession()
        ws = _make_ws(session)

        ok = _send_message(ws, session, "hello")
        assert ok is True
        original = ws.worker_thread
        assert original is not None
        original.join(timeout=2.0)

        assert ws.worker_thread is original
        assert ws._worker_running is False

    def test_worker_exit_no_wake_when_state_not_idle(self) -> None:
        """An ERROR exit stays parked for the operator — pending nudges
        wait for the next real interaction rather than burning
        unattended inference on a failed session."""
        session = _WakeCapableSession()
        ws = _make_ws(session)

        def run() -> None:
            session._nudge_queue.enqueue("idle_children", "kids waiting", "any")
            ws.state = WorkstreamState.ERROR

        ok = session_worker.send(ws, enqueue=lambda: None, run=run)
        assert ok is True
        original = ws.worker_thread
        assert original is not None
        original.join(timeout=2.0)

        assert ws.worker_thread is original
        assert session.deliver_calls == 0
        assert len(session._nudge_queue) == 1  # still queued for later seams

    def test_abandoned_worker_does_not_run_wake_backstop(self) -> None:
        """Only the owner retries: an abandoned worker (successor claimed
        the flag) finishing late must not spawn a wake — the successor's
        own exit runs the backstop."""
        send_gate = threading.Event()
        session = _WakeCapableSession(send_gate=send_gate)
        ws = _make_ws(session)

        ok = _send_message(ws, session, "hello")
        assert ok is True
        abandoned = ws.worker_thread
        assert abandoned is not None

        session._nudge_queue.enqueue("idle_children", "kids waiting", "any")
        sentinel = threading.Thread(target=lambda: None, name="successor")
        with ws._lock:
            ws.worker_thread = sentinel
            ws._worker_running = True

        send_gate.set()
        abandoned.join(timeout=3.0)
        assert not abandoned.is_alive()

        # No wake spawned by the abandoned thread; ownership intact.
        assert ws.worker_thread is sentinel
        assert session.deliver_calls == 0
        assert ws._worker_running is True


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
