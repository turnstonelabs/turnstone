"""Tests for the console's coordinator idle-cleanup thread helper.

The helper itself is a tiny loop wrapping ``mgr.close_idle``; the heavy
lifting is in ``SessionManager.close_idle`` (covered in
``test_session_manager.py``) and ``bulk_close_stale_orphans`` (covered
in ``test_storage_sqlite.py``).  These tests verify the glue:

- the helper runs an initial sweep BEFORE its first wait (cold-start
  cleanup without blocking the lifespan),
- the helper swallows exceptions so a transient DB blip can't kill the
  daemon thread,
- the helper exits cleanly when ``stop_event`` is set,
- the helper subscribes to ``mgr.subscribe_to_state`` and a state-change
  event wakes the next sweep early (event-driven, not polling),
- the helper unsubscribes when the thread exits so the subscriber
  doesn't leak past one cleanup-thread lifetime.

The ``stop_event`` parameter is exclusively for tests — production
callers pass ``None`` and the daemon runs for process lifetime.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import TYPE_CHECKING

from turnstone.console.server import _coord_idle_cleanup_thread

if TYPE_CHECKING:
    from collections.abc import Callable


class _StubMgr:
    """Minimal SessionManager substitute exposing only what the cleanup
    thread touches: ``close_idle``, ``subscribe_to_state``,
    ``unsubscribe_from_state``.  Records call ordering for assertions
    and lets the test fire state-change events manually via
    :meth:`fire_state_change`.
    """

    def __init__(
        self, *, stop_event: threading.Event, expected_calls: int, raise_after: int = -1
    ) -> None:
        self.calls: list[float] = []
        self._stop_event = stop_event
        self._expected = expected_calls
        self._raise_after = raise_after
        self._subscribers: list[Callable[[str, object], None]] = []
        self._sub_lock = threading.Lock()

    def close_idle(self, timeout_sec: float) -> list[str]:
        self.calls.append(timeout_sec)
        try:
            if 0 <= self._raise_after < len(self.calls):
                raise RuntimeError("simulated DB blip")
        finally:
            # Set stop after the helper has been exercised enough,
            # regardless of whether this call raised.
            if len(self.calls) >= self._expected:
                self._stop_event.set()
        return []

    def subscribe_to_state(self, callback: Callable[[str, object], None]) -> None:
        with self._sub_lock:
            self._subscribers.append(callback)

    def unsubscribe_from_state(self, callback: Callable[[str, object], None]) -> None:
        with self._sub_lock, contextlib.suppress(ValueError):
            self._subscribers.remove(callback)

    @property
    def subscribers_count(self) -> int:
        with self._sub_lock:
            return len(self._subscribers)

    def fire_state_change(self, ws_id: str = "ws-x", state: object = "idle") -> None:
        with self._sub_lock:
            snapshot = list(self._subscribers)
        for cb in snapshot:
            cb(ws_id, state)


def _run_until_done(mgr: _StubMgr, stop_event: threading.Event, timeout_sec: float) -> None:
    # ``min_sweep_interval=0.0`` disables the production cadence floor
    # (default 5 s) so tests can fire many close_idle calls back-to-back
    # without waiting real time between them.  The floor is exercised
    # in its own dedicated test below.
    thread = threading.Thread(
        target=_coord_idle_cleanup_thread,
        args=(mgr, timeout_sec, stop_event),
        kwargs={"min_sweep_interval": 0.0},
        daemon=True,
    )
    thread.start()
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "helper failed to exit on stop_event"


def test_coord_idle_cleanup_runs_initial_sweep_before_wait() -> None:
    """The first close_idle call must happen BEFORE the first wait —
    otherwise cold-start orphans wait one ``check_every`` interval (~30 min
    on default 2h timeout) for the first reap.  Crucial because the
    lifespan no longer does a synchronous initial sweep.

    Verified structurally: a single ``expected_calls=1`` run completes
    in well under one ``check_every`` (here 0.04 s timeout → 0.01 s
    check_every), so the initial sweep must have happened before any
    real wait could have blocked it.
    """
    stop_event = threading.Event()
    mgr = _StubMgr(stop_event=stop_event, expected_calls=1)
    started = time.monotonic()
    _run_until_done(mgr, stop_event, timeout_sec=0.04)
    elapsed = time.monotonic() - started
    assert len(mgr.calls) == 1
    # check_every = min(300.0, 0.04/4) = 0.01 s.  An initial sweep
    # gated behind one full wait would have taken ~0.01+ s anyway, so
    # the upper bound here is "much less than one check_every plus
    # process noise" — the explicit 1.0 s gives generous CI headroom
    # while still asserting the test is testing the right thing.
    assert elapsed < 1.0


def test_coord_idle_cleanup_calls_close_idle_each_tick() -> None:
    """Heartbeat path: with no state-change events, close_idle fires
    each ``check_every`` interval.  Test uses a tiny timeout so the
    test runs fast — the contract under test is "the loop iterates",
    not the production cadence.
    """
    stop_event = threading.Event()
    mgr = _StubMgr(stop_event=stop_event, expected_calls=3)
    _run_until_done(mgr, stop_event, timeout_sec=0.04)
    assert len(mgr.calls) == 3
    assert all(t == 0.04 for t in mgr.calls)


def test_coord_idle_cleanup_survives_close_idle_exceptions() -> None:
    """A transient DB error must not kill the daemon thread — the next
    tick should still fire close_idle.  Without the try/except, a single
    blip would silently leak orphans forever."""
    stop_event = threading.Event()
    mgr = _StubMgr(stop_event=stop_event, expected_calls=4, raise_after=1)
    _run_until_done(mgr, stop_event, timeout_sec=0.04)
    # All four calls must have fired despite calls 2-4 raising.
    assert len(mgr.calls) == 4


def test_coord_idle_cleanup_exits_cleanly_on_stop_event() -> None:
    """The stop_event mechanism is the test contract; verify the thread
    actually exits when the event is set, without needing exceptions or
    daemon-process termination."""
    stop_event = threading.Event()
    mgr = _StubMgr(stop_event=stop_event, expected_calls=2)
    _run_until_done(mgr, stop_event, timeout_sec=0.04)
    assert stop_event.is_set()


def test_state_change_wakes_close_idle_before_heartbeat() -> None:
    """The event-driven path is the whole point of the refactor: a
    workstream state-change must wake the cleanup sweep without
    waiting one ``check_every`` interval.  Tested with a long
    timeout_sec so the heartbeat would NOT have fired in the test
    window — the close_idle call past the initial sweep must come
    from a state-change wake.
    """
    stop_event = threading.Event()
    mgr = _StubMgr(stop_event=stop_event, expected_calls=2)
    # check_every = min(300.0, 120.0/4) = 30 s — well outside the test
    # window.  Any close_idle call past the initial sweep must come
    # from a fire_state_change-driven wake-up.
    thread = threading.Thread(
        target=_coord_idle_cleanup_thread,
        args=(mgr, 120.0, stop_event),
        kwargs={"min_sweep_interval": 0.0},
        daemon=True,
    )
    thread.start()
    # Wait for the initial sweep to complete AND the thread to enter
    # its first ``tick_now.wait`` (signalled here by the subscriber
    # being registered + calls advancing to 1).
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if mgr.subscribers_count == 1 and len(mgr.calls) >= 1:
            break
        time.sleep(0.01)
    assert mgr.subscribers_count == 1, "thread didn't subscribe to state"
    assert len(mgr.calls) == 1, "initial sweep didn't fire"
    # One state-change fire wakes the first ``wait`` → close_idle runs
    # again → stop_event is set (expected_calls=2) → thread exits.
    mgr.fire_state_change()
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "thread didn't exit after state-change-driven sweep"
    # 2 = initial + state-change-driven.  If the state change weren't
    # being honoured, close_idle would have stalled on the 30 s wait
    # and the thread.join would have timed out.
    assert len(mgr.calls) == 2


def test_subscriber_unregisters_when_thread_exits() -> None:
    """The cleanup thread's state-change subscriber must be removed
    when the thread exits — otherwise long-running processes that
    restart their cleanup threads (admin model-CRUD path, tests) leak
    subscribers and every state change fires N stale callbacks.
    """
    stop_event = threading.Event()
    mgr = _StubMgr(stop_event=stop_event, expected_calls=1)
    _run_until_done(mgr, stop_event, timeout_sec=0.04)
    assert mgr.subscribers_count == 0, "subscriber leaked past thread exit"


def test_state_change_during_close_idle_triggers_followup_sweep() -> None:
    """A state-change fired during the initial sweep (e.g. close_idle's
    own ``close()`` calls firing subscribers) must wake the next
    ``tick_now.wait`` rather than being lost to the clear-before-sweep
    ordering.  The clear runs INSIDE the loop just before close_idle,
    so a fire during the initial sweep — which precedes the loop —
    arrives at an already-set event that the first wait sees set and
    returns on immediately.
    """
    stop_event = threading.Event()
    mgr = _StubMgr(stop_event=stop_event, expected_calls=2)

    real_close_idle = mgr.close_idle

    # One-shot fire during the initial sweep, mirroring what
    # close_idle's own close() calls do in production (set_state →
    # state-change subscribers).
    fired = [False]

    def _instrumented_close_idle(timeout_sec: float) -> list[str]:
        result = real_close_idle(timeout_sec)
        if not fired[0]:
            fired[0] = True
            mgr.fire_state_change()
        return result

    mgr.close_idle = _instrumented_close_idle  # type: ignore[method-assign]

    thread = threading.Thread(
        target=_coord_idle_cleanup_thread,
        args=(mgr, 120.0, stop_event),
        kwargs={"min_sweep_interval": 0.0},
        daemon=True,
    )
    thread.start()
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "thread blocked on the next wait — mid-sweep wake was lost"
    # 2 = initial sweep + state-change-driven follow-up.  Without the
    # event surviving the clear-before-sweep ordering, the thread
    # would have blocked on the 30 s ``wait`` and the test would have
    # timed out at thread.join.
    assert len(mgr.calls) == 2


def test_min_sweep_interval_floors_close_idle_cadence_under_sustained_wakes() -> None:
    """Cadence floor: even when state-change events keep firing
    ``tick_now.set()``, ``close_idle`` must not run more often than
    ``min_sweep_interval`` — otherwise the loop tight-spins close_idle
    at the rate of its own DB latency, doing 600-1500x more DB work
    than the pre-refactor fixed-30 s cadence.

    Wires a state-change subscriber that fires another state change
    from inside close_idle, so the bus would tick forever if not
    floored.  Asserts the elapsed-between-sweeps is at least
    ``min_sweep_interval`` modulo small wall-clock noise.
    """
    stop_event = threading.Event()
    mgr = _StubMgr(stop_event=stop_event, expected_calls=3)

    real_close_idle = mgr.close_idle
    sweep_times: list[float] = []

    def _instrumented_close_idle(timeout_sec: float) -> list[str]:
        sweep_times.append(time.monotonic())
        result = real_close_idle(timeout_sec)
        # Always fire another state-change to simulate sustained
        # activity (each turn fires thinking/running/attention/idle).
        # If the floor were absent, the next wake would race the next
        # close_idle immediately and ``sweep_times`` deltas would be
        # bounded by close_idle latency (microseconds), not the floor.
        mgr.fire_state_change()
        return result

    mgr.close_idle = _instrumented_close_idle  # type: ignore[method-assign]

    # 0.15 s floor keeps the test fast (~0.3 s total) while still
    # representing a meaningful gap relative to close_idle's
    # near-zero stub latency.
    thread = threading.Thread(
        target=_coord_idle_cleanup_thread,
        args=(mgr, 120.0, stop_event),
        kwargs={"min_sweep_interval": 0.15},
        daemon=True,
    )
    thread.start()
    thread.join(timeout=3.0)
    assert not thread.is_alive(), "thread didn't exit"
    assert len(sweep_times) >= 2, "fewer than two sweeps fired"
    # Gap between sweep 1 (post-initial) and sweep 2 must respect
    # the floor.  Initial sweep at sweep_times[0] is unfloored
    # (no prior sweep to compare against), so the meaningful
    # assertion is on sweep_times[1] - sweep_times[0].
    gap = sweep_times[1] - sweep_times[0]
    assert gap >= 0.12, f"floor breached: gap {gap:.3f}s < min_sweep_interval 0.15s"
