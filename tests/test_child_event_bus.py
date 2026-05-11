"""Unit tests for :class:`turnstone.core.child_event_bus.ChildEventBus`.

The bus is the in-process wakeup primitive for ``wait_for_workstream``
(see :mod:`turnstone.console.coordinator_client`).  It's a small dict
of ws_id → set[threading.Event] under a lock — focused tests for
register/notify symmetry, no-subscriber notify, multi-waiter fan-out,
multi-child waiter, and concurrent register/notify (smoke).  End-to-end
integration with the dispatch sink lives in
``test_coordinator_adapter.py`` and ``test_coordinator_client.py``.
"""

from __future__ import annotations

import threading
import time

import pytest

from turnstone.core.child_event_bus import ChildEventBus


def test_register_returns_event_that_starts_unset() -> None:
    """A waiter must not see leftover state from before it registered —
    a fresh wait should always block until the first notify."""
    bus = ChildEventBus()
    event = bus.register_waiter(["ws-1"])
    assert isinstance(event, threading.Event)
    assert not event.is_set()


def test_notify_wakes_waiter_on_matching_ws_id() -> None:
    bus = ChildEventBus()
    event = bus.register_waiter(["ws-1"])
    bus.notify("ws-1")
    assert event.is_set()


def test_notify_does_not_wake_waiter_on_unrelated_ws_id() -> None:
    """Different ws_ids must keep independent waiter sets — a notify on
    a stranger ws can't wake the wait or the bus stops being keyed."""
    bus = ChildEventBus()
    event = bus.register_waiter(["ws-1"])
    bus.notify("ws-other")
    assert not event.is_set()


def test_notify_with_no_subscribers_is_noop() -> None:
    """The dispatch sink calls notify on every translated event; the
    steady state has no wait tool active.  Must not raise."""
    bus = ChildEventBus()
    bus.notify("ws-nobody-cares")  # no exception


def test_multi_waiter_each_gets_independent_event() -> None:
    """Two waits on the same ws_id must wake independently — clearing
    one Event must not silence the other."""
    bus = ChildEventBus()
    e1 = bus.register_waiter(["ws-1"])
    e2 = bus.register_waiter(["ws-1"])
    assert e1 is not e2
    bus.notify("ws-1")
    assert e1.is_set()
    assert e2.is_set()


def test_multi_child_waiter_fires_on_any_listed_ws_id() -> None:
    """A wait on [A, B, C] returns a single Event registered against
    all three.  Notify on ANY of A/B/C must wake the wait — the
    caller's snapshot re-read disambiguates which one changed."""
    bus = ChildEventBus()
    event = bus.register_waiter(["ws-a", "ws-b", "ws-c"])
    bus.notify("ws-b")
    assert event.is_set()


def test_unregister_removes_event_from_all_listed_ws_ids() -> None:
    """After unregister, notify on any of the previously-watched ws_ids
    must NOT wake the Event — leaks would mean every future notify on
    that ws_id wakes a long-dead wait."""
    bus = ChildEventBus()
    event = bus.register_waiter(["ws-a", "ws-b"])
    bus.unregister_waiter(["ws-a", "ws-b"], event)
    bus.notify("ws-a")
    bus.notify("ws-b")
    assert not event.is_set()


def test_unregister_is_idempotent() -> None:
    """A double-unregister must silently no-op — finally blocks may
    run twice in odd shutdown paths, the bus must not raise."""
    bus = ChildEventBus()
    event = bus.register_waiter(["ws-1"])
    bus.unregister_waiter(["ws-1"], event)
    bus.unregister_waiter(["ws-1"], event)  # no exception


def test_unregister_pops_empty_buckets() -> None:
    """Empty per-ws_id buckets must be popped so a long-lived bus
    doesn't accumulate dead keys after many waits have churned through.
    Reaches into the private state — the property is structural, not
    behavioral, so the assertion is also."""
    bus = ChildEventBus()
    event = bus.register_waiter(["ws-1"])
    assert "ws-1" in bus._waiters
    bus.unregister_waiter(["ws-1"], event)
    assert "ws-1" not in bus._waiters


def test_unregister_keeps_bucket_with_remaining_waiters() -> None:
    """Removing one waiter from a multi-waiter bucket must not drop
    the others — popping the bucket would silently disable notifies
    for every concurrent wait on the same ws_id."""
    bus = ChildEventBus()
    e1 = bus.register_waiter(["ws-1"])
    e2 = bus.register_waiter(["ws-1"])
    bus.unregister_waiter(["ws-1"], e1)
    bus.notify("ws-1")
    assert not e1.is_set()
    assert e2.is_set()


def test_empty_and_falsy_ws_ids_are_skipped_on_register() -> None:
    """Defensive: ``wait_for_workstream`` cleans its inputs but the bus
    is reachable from other callers in future use; falsy ids should be
    silently dropped, not registered against an empty-string key."""
    bus = ChildEventBus()
    event = bus.register_waiter(["", "ws-1", ""])
    # Only the real ws_id should bucket the waiter.
    assert list(bus._waiters.keys()) == ["ws-1"]
    bus.notify("")  # no crash, no spurious wake
    assert not event.is_set()
    bus.notify("ws-1")
    assert event.is_set()


def test_notify_wakes_waiter_blocking_on_event_wait() -> None:
    """End-to-end wake-up latency: a wait blocked on ``Event.wait``
    must return promptly after a notify on a watched ws_id.  This is
    the property that retires the 0.5s polling cadence."""
    bus = ChildEventBus()
    event = bus.register_waiter(["ws-1"])
    woken_at = [0.0]

    def _waiter() -> None:
        event.wait(timeout=2.0)
        woken_at[0] = time.monotonic()

    t = threading.Thread(target=_waiter, daemon=True)
    t.start()
    # Give the waiter a beat to enter Event.wait, then notify.
    time.sleep(0.05)
    notified_at = time.monotonic()
    bus.notify("ws-1")
    t.join(timeout=1.0)
    assert not t.is_alive(), "waiter did not wake within 1s of notify"
    # Latency budget is generous; the contract is "well under the legacy
    # 0.5s poll cadence", not microsecond timing.
    assert woken_at[0] - notified_at < 0.2


def test_clear_before_check_race_does_not_lose_wake() -> None:
    """The wait-loop pattern is ``clear(); snapshot(); ...; wait()``.
    A notify between clear and wait must leave the Event set, so the
    next wait returns immediately and the loop re-snapshots.  Same
    standard subscribe/check race the wait loop guards against."""
    bus = ChildEventBus()
    event = bus.register_waiter(["ws-1"])
    # Simulate wait-loop ordering: clear, then notify "between" clear
    # and the next wait.
    event.clear()
    bus.notify("ws-1")
    # The next wait must return True immediately (set is sticky until
    # the next clear).
    assert event.wait(timeout=0.1) is True


def test_concurrent_register_and_notify_is_safe() -> None:
    """Smoke test: many threads registering / notifying / unregistering
    in parallel must not raise or deadlock.  Doesn't assert specific
    interleavings — only structural safety of the lock discipline."""
    bus = ChildEventBus()
    stop = threading.Event()
    errors: list[BaseException] = []

    def _worker(ws_id: str) -> None:
        try:
            for _ in range(200):
                if stop.is_set():
                    return
                ev = bus.register_waiter([ws_id])
                bus.notify(ws_id)
                bus.unregister_waiter([ws_id], ev)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_worker, args=(f"ws-{i}",), daemon=True) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
    stop.set()
    assert not errors, f"worker threads raised: {errors!r}"
    # All buckets should have been popped (every register paired with
    # unregister).
    assert bus._waiters == {}


@pytest.mark.parametrize("ws_id", ["", None])
def test_notify_silently_ignores_falsy_ws_id(ws_id: object) -> None:
    """Defensive: the dispatch sink already guards against empty
    ws_ids, but a falsy slip-through must not raise."""
    bus = ChildEventBus()
    event = bus.register_waiter(["ws-1"])
    bus.notify(ws_id)  # type: ignore[arg-type]
    assert not event.is_set()
