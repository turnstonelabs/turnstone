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

Callers pass no-arg closures. For a real ChatSession, dispatch captures its
slot-time cancellation witness through a narrow optional method; session
stubs retain the historical callback-only path. The exit backstop only PEEKS
session nudge state defensively (``getattr`` for ``_nudge_queue``, bail on
stubs) — watch-style dispatchers can still drive a session that isn't
installed on ``ws``.
"""

from __future__ import annotations

import collections
import queue
import threading
from typing import Any

import pytest

from tests._helpers import wait_until as _wait_until
from tests._session_helpers import make_session
from turnstone.core import session_worker
from turnstone.core.idle_nudge_watcher import wake_workstream_if_pending
from turnstone.core.nudge_queue import USER_DRAIN, NudgeQueue
from turnstone.core.session import ChatSession
from turnstone.core.session_routes import SessionEndpointConfig, _make_dispatch_attempt
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


class _ClaimingSendSession(_SendSession):
    """Dispatch stub exposing the production worker-claim protocol."""

    def __init__(self) -> None:
        super().__init__()
        self._cancel_event = threading.Event()

    def _capture_worker_claim(self, principal_id: str = "") -> session_worker.WorkerClaim:
        return session_worker.WorkerClaim(
            session=self,
            principal_id=principal_id,
            cancel_epoch=0,
            cancel_event=self._cancel_event,
            cancel_event_was_set=False,
        )

    def _worker_claim_is_current(self, claim: session_worker.WorkerClaim) -> bool:
        return claim.session is self and not claim.cancel_event.is_set()

    def queue_message(self, message: str, **_kwargs: Any) -> tuple[str, str, str]:
        super().queue_message(message)
        return message, "notice", "queued-message"


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


def _make_blocked_principal_session(
    *, previous_actor: str
) -> tuple[ChatSession, threading.Event, threading.Event, list[tuple[str, str | None]]]:
    """Build the smallest real ``queue_message`` surface for claim-race tests."""
    session = ChatSession.__new__(ChatSession)
    session._acting_user_id = previous_actor
    session._mcp_user_id = "owner"
    session._queued_lock = threading.Lock()
    session._queued_messages = collections.OrderedDict()
    session._retracted_while_popped = set()
    session._popped_in_flight = set()
    # The worker-slot claim these tests are about is captured from real
    # lifecycle state, so seed the fields ``__init__`` would: an open session
    # with no cancel edge, no close in flight, and no owed TOOL receipts.
    session._generation_lock = threading.RLock()
    session._cancel_event = threading.Event()
    session._approval_cancel_epoch = 0
    session._publication_shutdown = False
    session._soft_close_preparing = False
    session._tool_structural_debt = None

    bind_entered = threading.Event()
    release_bind = threading.Event()
    ran_as: list[tuple[str, str | None]] = []

    def _delayed_bind(user_id: str) -> None:
        bind_entered.set()
        assert release_bind.wait(5), "test did not release acting-user bind"
        session._acting_user_id = user_id

    def _record_send(message: str, **_kwargs: Any) -> None:
        ran_as.append((message, session._mcp_effective_user_id))

    session.bind_acting_user = _delayed_bind  # type: ignore[method-assign]
    session.send = _record_send  # type: ignore[method-assign]
    return session, bind_entered, release_bind, ran_as


def _principal_dispatch_attempt(
    ws: Workstream,
    session: ChatSession,
    *,
    message: str,
    acting_user_id: str,
) -> tuple[bool, dict[str, Any]]:
    """Dispatch through the production HTTP send worker/queue decision."""
    cfg = SessionEndpointConfig(
        permission_gate=None,
        manager_lookup=lambda _request: (None, None),
        tenant_check=None,
        not_found_label="missing",
        audit_action_prefix="workstream",
    )
    attempt = _make_dispatch_attempt(
        ws,
        cfg,
        ws.ui,
        message=message,
        resolved_atts=[],
        ordered_taken=[],
        send_id=f"send-{acting_user_id}",
        acting_uid=acting_user_id,
    )
    return attempt(session)


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


def test_send_barrier_active_truth_table() -> None:
    """The two-term order barrier (Workstream.send_barrier_active): list
    non-empty OR drain alive.  The drain-alive term covers the CLAIMED-
    entry window (entry popped, dispatch in flight) — the _PendingSend
    invariant "drain not alive ⇒ nothing claimed" is what makes the pair
    exhaustive; a one-term copy at any consumer re-opens the wake-jumps-
    an-acked-send hole."""
    from types import SimpleNamespace

    ws = _make_ws()
    assert ws.send_barrier_active() is False  # empty list, no drain
    ws._pending_sends.append(object())  # type: ignore[arg-type]
    assert ws.send_barrier_active() is True  # list term
    ws._pending_drain = SimpleNamespace(is_alive=lambda: True)  # type: ignore[assignment]
    assert ws.send_barrier_active() is True  # both terms
    ws._pending_sends.clear()
    assert ws.send_barrier_active() is True  # drain-alive term alone (claimed window)
    ws._pending_drain = SimpleNamespace(is_alive=lambda: False)  # type: ignore[assignment]
    assert ws.send_barrier_active() is False  # dead drain, empty list
    ws._pending_drain = None
    assert ws.send_barrier_active() is False


def test_spawn_failure_releases_slot_and_reraises(monkeypatch) -> None:
    """``Thread.start`` raising (thread exhaustion, MemoryError) must not
    wedge the slot: the claim ``(worker_thread, _worker_running)`` taken
    under the lock is rolled back and the exception propagates.  Without
    the rollback the flag's only clearer is a finally on a thread that
    never started — the workstream looks idle forever (no state change
    ever fired) while every dispatch takes the reuse path into a queue
    no worker will drain, until an operator force-cancel."""
    import pytest

    session = _SendSession()
    ws = _make_ws(session)

    class _ExhaustedThread(threading.Thread):
        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    class _ThreadNS:
        # Shim only what session_worker.send touches; patching the real
        # threading module's Thread attribute would break every other
        # test's spawns.
        Thread = _ExhaustedThread
        current_thread = staticmethod(threading.current_thread)

    monkeypatch.setattr(session_worker, "threading", _ThreadNS)
    with pytest.raises(RuntimeError, match="can't start new thread"):
        _send_message(ws, session, "doomed")
    assert ws._worker_running is False
    assert ws.worker_thread is None
    assert session.send_calls == []

    # The slot is genuinely reusable once resources recover.
    monkeypatch.setattr(session_worker, "threading", threading)
    assert _send_message(ws, session, "recovered") is True
    ws.worker_thread.join(timeout=2.0)
    assert session.send_calls == ["recovered"]
    assert ws._worker_running is False


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


def test_worker_claim_rejects_previous_actor_before_new_actor_bind_completes() -> None:
    """Queue admission follows the worker claim, never the stale session bind.

    A fresh send claims ``_worker_running`` before its worker thread can finish
    ``bind_acting_user``.  If the queue guard reads the prior mutable session
    actor in that window, the prior actor can inject text into the new actor's
    turn and have it run under the new actor's credentials.
    """
    session, bind_entered, release_bind, ran_as = _make_blocked_principal_session(
        previous_actor="alice"
    )
    ws = _make_ws(session)
    ws.ui = object()  # type: ignore[assignment]

    try:
        first_ok, first_queue = _principal_dispatch_attempt(
            ws,
            session,
            message="bob starts",
            acting_user_id="bob",
        )
        assert first_ok is True
        assert first_queue == {}
        assert bind_entered.wait(5), "new worker never entered acting-user bind"

        second_ok, second_queue = _principal_dispatch_attempt(
            ws,
            session,
            message="alice must not enter bob's turn",
            acting_user_id="alice",
        )

        assert second_ok is True
        assert second_queue == {"rejected": "cross_user_interjection"}
        assert session._queued_messages == {}
    finally:
        release_bind.set()
        worker = ws.worker_thread
        if worker is not None:
            worker.join(timeout=5)

    assert ran_as == [("bob starts", "bob")]


def test_worker_claim_accepts_new_actor_followup_before_session_bind_completes() -> None:
    """The claimed actor may queue a follow-up during its own bind window.

    Fixing the cross-user hole by merely rejecting every pre-bind enqueue would
    turn two rapid sends from the same browser into a false 409.  The immutable
    worker claim already knows who owns the admitted turn, so that identity is
    authoritative for both the allow and deny decisions.
    """
    session, bind_entered, release_bind, ran_as = _make_blocked_principal_session(
        previous_actor="alice"
    )
    ws = _make_ws(session)
    ws.ui = object()  # type: ignore[assignment]

    try:
        first_ok, first_queue = _principal_dispatch_attempt(
            ws,
            session,
            message="bob starts",
            acting_user_id="bob",
        )
        assert first_ok is True
        assert first_queue == {}
        assert bind_entered.wait(5), "new worker never entered acting-user bind"

        second_ok, second_queue = _principal_dispatch_attempt(
            ws,
            session,
            message="bob follows up",
            acting_user_id="bob",
        )

        assert second_ok is True
        assert second_queue == {
            "cleaned": "bob follows up",
            "priority": "notice",
            "msg_id": "send-bob",
        }
        assert list(session._queued_messages) == ["send-bob"]
    finally:
        release_bind.set()
        worker = ws.worker_thread
        if worker is not None:
            worker.join(timeout=5)

    assert ran_as == [("bob starts", "bob")]


def test_dispatch_refuses_session_swapped_after_caller_capture() -> None:
    """A closure-bound session must be the one the worker claim fences.

    The HTTP pending-send drain captures ``ws.session`` before entering the
    shared dispatcher.  A resume-style object swap can land in that gap.  The
    dispatcher must refuse both queue and spawn arms instead of capturing a
    valid claim for the replacement while its callbacks still mutate the
    detached predecessor.
    """
    cfg = SessionEndpointConfig(
        permission_gate=None,
        manager_lookup=lambda _request: (None, None),
        tenant_check=None,
        not_found_label="missing",
        audit_action_prefix="workstream",
    )
    observed: list[tuple[bool, list[str], list[str]]] = []

    for worker_running in (False, True):
        captured_session = _ClaimingSendSession()
        replacement_session = _ClaimingSendSession()
        ws = _make_ws(captured_session)
        ws._worker_running = worker_running
        attempt = _make_dispatch_attempt(
            ws,
            cfg,
            None,
            message="must follow the replacement",
            resolved_atts=[],
            ordered_taken=[],
            send_id="",
            acting_uid="",
        )

        # Exact caller-capture -> dispatcher-capture crossing.
        ws.session = replacement_session  # type: ignore[assignment]
        accepted, _outcome = attempt(captured_session)  # type: ignore[arg-type]
        owner = ws.worker_thread
        if owner is not None:
            owner.join(timeout=2)
        observed.append(
            (
                accepted,
                list(captured_session.send_calls),
                list(captured_session.queue_calls),
            )
        )

    assert observed == [(False, [], []), (False, [], [])]


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


def test_abandoned_worker_does_not_clear_successor_principal_claim() -> None:
    """A stale predecessor's finally cannot erase its successor's actor.

    Force cancellation permits the successor to claim the slot before the old
    thread returns.  Principal cleanup therefore needs the same worker-identity
    guard as ``_worker_running`` cleanup; a blind clear reopens both false-allow
    and false-reject queue decisions during the successor turn.
    """
    send_gate = threading.Event()
    session = _SendSession(send_gate=send_gate)
    ws = _make_ws(session)

    assert (
        session_worker.send(
            ws,
            enqueue=lambda: session.queue_message("first"),
            run=lambda: session.send("first"),
            principal_id="alice",
        )
        is True
    )
    abandoned = ws.worker_thread
    assert abandoned is not None
    assert ws._worker_principal_id == "alice"

    sentinel = threading.Thread(target=lambda: None, name="successor")
    with ws._lock:
        ws.worker_thread = sentinel
        ws._worker_running = True
        ws._worker_principal_id = "bob"

    send_gate.set()
    abandoned.join(timeout=3)
    assert not abandoned.is_alive()
    assert ws.worker_thread is sentinel
    assert ws._worker_running is True
    assert ws._worker_principal_id == "bob"


def test_force_during_nonabandonable_mutation_keeps_slot_until_owner_exits() -> None:
    """A destructive mutation cannot overlap a force-admitted successor."""

    mutation_entered = threading.Event()
    release_mutation = threading.Event()
    queued = threading.Event()
    ws = _make_ws(_SendSession())

    def _mutation() -> None:
        mutation_entered.set()
        assert release_mutation.wait(5), "test did not release mutation"

    assert session_worker.send(
        ws,
        enqueue=queued.set,
        run=_mutation,
        worker_kind="command",
        force_abandonable=False,
        thread_name="nonabandonable-mutation",
    )
    owner = ws.worker_thread
    assert owner is not None and mutation_entered.wait(5)

    # Exact ownership decision the force-cancel route performs under ws._lock.
    with ws._lock:
        if ws._worker_force_abandonable:
            ws.worker_thread = None
            ws._worker_running = False
            ws._worker_principal_id = ""

    assert ws.worker_thread is owner
    assert ws._worker_running is True
    assert ws._worker_force_abandonable is False

    # A would-be successor takes the existing-slot path; no concurrent worker
    # can start while the destructive mutation remains inside its transaction.
    assert session_worker.send(ws, enqueue=queued.set, run=lambda: None)
    assert queued.is_set()
    assert ws.worker_thread is owner

    release_mutation.set()
    owner.join(5)
    assert not owner.is_alive()
    assert ws._worker_running is False
    assert ws._worker_force_abandonable is True


def test_claim_capture_never_holds_workstream_lock_behind_generation_lock() -> None:
    """Worker admission cannot invert generation -> UI -> workstream order."""

    generation_lock = threading.Lock()
    generation_held = threading.Event()
    capture_attempted = threading.Event()
    worker_ran = threading.Event()
    commit_acquired_ws: list[bool] = []
    dispatch_results: list[bool] = []

    class _ClaimSession:
        def _capture_worker_claim(self, _principal_id: str = "") -> None:
            capture_attempted.set()
            with generation_lock:
                return None

    ws = _make_ws(_ClaimSession())

    def _generation_commit() -> None:
        with generation_lock:
            generation_held.set()
            assert capture_attempted.wait(5), "dispatcher never attempted claim capture"
            acquired = ws._lock.acquire(timeout=1)
            commit_acquired_ws.append(acquired)
            if acquired:
                ws._lock.release()

    def _dispatch() -> None:
        dispatch_results.append(
            session_worker.send(
                ws,
                enqueue=lambda: None,
                run=worker_ran.set,
                thread_name="lock-order-worker",
            )
        )

    commit = threading.Thread(target=_generation_commit, daemon=True)
    commit.start()
    assert generation_held.wait(5)

    dispatcher = threading.Thread(target=_dispatch, daemon=True)
    dispatcher.start()
    commit.join(5)
    dispatcher.join(5)

    assert not commit.is_alive() and not dispatcher.is_alive()
    assert commit_acquired_ws == [True]
    assert dispatch_results == [True]
    owner = ws.worker_thread
    assert owner is not None
    owner.join(5)
    assert worker_ran.is_set()


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


class TestWorkerExitInterjectionBackstop:
    """The ownership-clear seam cannot strand a just-accepted interjection."""

    @staticmethod
    def _session() -> ChatSession:
        session = make_session(user_id="alice")
        session._acting_user_id = "alice"
        return session

    def test_enqueue_after_last_flush_hands_off_same_principal_and_correlation(
        self,
    ) -> None:
        """A reuse enqueue at run's tail is delivered by a successor wake."""
        session = self._session()
        ws = _make_ws(session)
        delivered = threading.Event()
        calls: list[tuple[str, tuple[str, ...], str]] = []

        def _record_send(message: str, *, client_send_ids: tuple[str, ...] = ()) -> None:
            claim = session_worker.current_worker_claim(session)
            assert claim is not None
            calls.append((message, client_send_ids, claim.principal_id))
            delivered.set()

        session.send = _record_send  # type: ignore[method-assign]

        def _outgoing_run() -> None:
            # This nested dispatch is the deterministic equivalent of another
            # request winning ws._lock after ChatSession's final queue flush
            # but before this runner enters its ownership-clear finally.
            assert session_worker.send(
                ws,
                enqueue=lambda: session.queue_message(
                    "late follow-up",
                    queue_msg_id="late-message",
                    interjector_user_id="alice",
                    turn_principal_id="alice",
                    client_send_id="client-late",
                ),
                run=lambda: pytest.fail("reuse dispatch spawned a second worker"),
                expected_session=session,
                principal_id="alice",
            )

        assert len(session._nudge_queue) == 0
        assert session_worker.send(
            ws,
            enqueue=lambda: None,
            run=_outgoing_run,
            expected_session=session,
            principal_id="alice",
        )
        assert delivered.wait(5), "worker-exit handoff did not deliver the queued row"
        _wait_until(lambda: ws._worker_running is False)

        assert calls == [("late follow-up", ("client-late",), "alice")]
        assert session._queued_messages == {}
        assert len(session._nudge_queue) == 0

    def test_non_exit_gate_remains_nudge_only(self) -> None:
        session = self._session()
        ws = _make_ws(session)
        session.queue_message(
            "wait for owner clear",
            queue_msg_id="nudge-only-control",
            interjector_user_id="alice",
            turn_principal_id="alice",
        )

        assert wake_workstream_if_pending(ws, trigger="idle-transition") is False
        assert list(session._queued_messages) == ["nudge-only-control"]
        assert ws._worker_running is False

    def test_retraction_before_wake_pop_converges_without_send(self) -> None:
        session = self._session()
        ws = _make_ws(session)
        session.queue_message(
            "retract me",
            queue_msg_id="retracted-message",
            interjector_user_id="alice",
            turn_principal_id="alice",
        )
        pop_entered = threading.Event()
        release_pop = threading.Event()
        real_pop = session._pop_queued_messages
        send_calls: list[str] = []

        def _blocked_pop(**kwargs: Any) -> Any:
            # kwargs passthrough so signature growth never narrows the shim.
            pop_entered.set()
            assert release_pop.wait(5), "test did not release queue pop"
            return real_pop(**kwargs)

        session._pop_queued_messages = _blocked_pop  # type: ignore[method-assign]
        session.send = lambda message, **_kwargs: send_calls.append(message)  # type: ignore[method-assign]

        assert wake_workstream_if_pending(
            ws,
            trigger="test-worker-exit",
            include_interjections=True,
        )
        assert pop_entered.wait(5), "wake worker never reached the queue pop"
        assert session.dequeue_message("retracted-message") is True
        release_pop.set()
        _wait_until(lambda: ws._worker_running is False)

        assert send_calls == []
        assert session._queued_messages == {}
        assert (
            wake_workstream_if_pending(
                ws,
                trigger="test-worker-exit",
                include_interjections=True,
            )
            is False
        )

    def test_empty_interjection_is_discarded_without_a_wake_loop(self) -> None:
        session = self._session()
        ws = _make_ws(session)
        cleaned, _priority, _msg_id = session.queue_message(
            "!!!",
            queue_msg_id="empty-message",
            interjector_user_id="alice",
            turn_principal_id="alice",
        )
        send_calls: list[str] = []
        session.send = lambda message, **_kwargs: send_calls.append(message)  # type: ignore[method-assign]

        assert cleaned == ""
        assert wake_workstream_if_pending(
            ws,
            trigger="test-worker-exit",
            include_interjections=True,
        )
        _wait_until(lambda: ws._worker_running is False)

        assert send_calls == []
        assert session._queued_messages == {}

    def test_restored_failed_wake_is_retained_without_retry_loop(self) -> None:
        session = self._session()
        ws = _make_ws(session)
        session.queue_message(
            "keep after failure",
            queue_msg_id="restored-message",
            interjector_user_id="alice",
            turn_principal_id="alice",
            client_send_id="client-restored",
        )
        attempts: list[str] = []

        def _fail_before_append(message: str, **_kwargs: Any) -> None:
            attempts.append(message)
            raise RuntimeError("injected preamble failure")

        session.send = _fail_before_append  # type: ignore[method-assign]

        assert wake_workstream_if_pending(
            ws,
            trigger="test-worker-exit",
            include_interjections=True,
        )
        wake_worker = ws.worker_thread
        assert wake_worker is not None
        _wait_until(lambda: ws._worker_running is False)

        assert attempts == ["keep after failure"]
        assert list(session._queued_messages) == ["restored-message"]
        # The failed queue-only wake's own exit carried the exact snapshot
        # token and suppressed a retry. No successor worker was spawned.
        assert ws.worker_thread is wake_worker
        assert attempts == ["keep after failure"]

    def test_deferred_wake_token_does_not_suppress_competing_worker_exit(self) -> None:
        """Only a spawned wake owns the snapshot exclusion token.

        A same-principal worker can claim the slot after the queue predicate
        but before the wake dispatch. The wake then takes the reuse/no-op arm;
        if that pre-spawn snapshot were sticky, the competing worker's exit
        could not recover the older queued row after failing before its drain.
        """
        session = self._session()
        ws = _make_ws(session)
        session.queue_message(
            "older stranded row",
            queue_msg_id="older-row",
            interjector_user_id="alice",
            turn_principal_id="alice",
            client_send_id="client-older",
        )
        competing_started = threading.Event()
        release_competing = threading.Event()
        delivered = threading.Event()
        calls: list[tuple[str, tuple[str, ...], str]] = []

        def _record_send(message: str, *, client_send_ids: tuple[str, ...] = ()) -> None:
            claim = session_worker.current_worker_claim(session)
            assert claim is not None
            calls.append((message, client_send_ids, claim.principal_id))
            delivered.set()

        session.send = _record_send  # type: ignore[method-assign]
        real_claim = session.claim_pending_interjection_wake
        injected_competitor = False

        def _claim_then_compete(*, exclude_signature: object | None = None) -> Any:
            nonlocal injected_competitor
            signature = real_claim(exclude_signature=exclude_signature)
            if signature is not None and not injected_competitor:
                injected_competitor = True

                def _competing_run() -> None:
                    competing_started.set()
                    assert release_competing.wait(5), "test did not release competing worker"
                    # Return before touching the retained queue: this models a
                    # fresh worker failing in its pre-drain setup.

                assert session_worker.send(
                    ws,
                    enqueue=lambda: pytest.fail("competitor unexpectedly reused a worker"),
                    run=_competing_run,
                    expected_session=session,
                    principal_id="alice",
                )
                assert competing_started.wait(5), "competing worker did not claim the slot"
            return signature

        session.claim_pending_interjection_wake = _claim_then_compete  # type: ignore[method-assign]

        # The queue-only wake loses the slot race and takes its no-op enqueue
        # arm. It must not leave suppression behind for the real owner.
        assert wake_workstream_if_pending(
            ws,
            trigger="test-worker-exit",
            include_interjections=True,
        )
        assert calls == []
        assert list(session._queued_messages) == ["older-row"]

        release_competing.set()
        assert delivered.wait(5), "competing worker exit did not retry the older row"
        _wait_until(lambda: ws._worker_running is False)

        assert calls == [("older stranded row", ("client-older",), "alice")]
        assert session._queued_messages == {}

    @pytest.mark.parametrize(
        "blocker",
        ["budget", "abandoned", "unresolved", "foreign_principal", "gone"],
    )
    def test_queue_only_wake_refuses_unattended_retry_blockers(self, blocker: str) -> None:
        session = self._session()
        ws = _make_ws(session)
        owner = "bob" if blocker == "foreign_principal" else "alice"
        session.queue_message(
            "wait for an explicit seam",
            queue_msg_id=f"blocked-{blocker}",
            interjector_user_id=owner,
            turn_principal_id=owner,
        )
        if blocker == "budget":
            session._budget_exhausted = True
        elif blocker == "abandoned":
            session._generation_abandoned = True
        elif blocker == "unresolved":
            session.has_unresolved_conversation_persistence = lambda: True  # type: ignore[method-assign]
        elif blocker == "gone":
            # The terminal hard-delete latch: the gone discovery clears the
            # pending-commit journal, so the unresolved-persistence blocker
            # reads False exactly when no turn can ever land — the latch
            # needs its own refusal arm or the wake spawns doomed.
            session._workstream_gone_ws = session._ws_id

        assert (
            wake_workstream_if_pending(
                ws,
                trigger="test-worker-exit",
                include_interjections=True,
            )
            is False
        )
        assert list(session._queued_messages) == [f"blocked-{blocker}"]
        assert ws._worker_running is False

    def test_interjection_claim_itself_refuses_a_gone_workstream(self) -> None:
        """The claim gate carries its own gone arm — it must refuse for ANY
        caller, not only behind the watcher's spawn gate (a future caller
        that consults the claim directly gets the same fail-closed answer)."""
        session = self._session()
        session.queue_message(
            "never wake for this",
            queue_msg_id="gone-q",
            interjector_user_id="alice",
            turn_principal_id="alice",
        )
        session._workstream_gone_ws = session._ws_id
        assert session.claim_pending_interjection_wake() is None
        assert list(session._queued_messages) == ["gone-q"]


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
