"""Tests for the console-side ``NotifyDispatcher``.

Exercises the dispatcher against the SQLite synthetic-sweep path so the
suite runs without a Postgres dependency.  The PG path is shaped the
same way (same handler invocation semantics) — the only difference is
the underlying stream's wake-up source, which is covered separately in
``test_storage_notify.py::TestPostgresNotify``.
"""

from __future__ import annotations

import threading
import time

import pytest


@pytest.fixture
def dispatcher_factory(storage):
    """Yield a factory that constructs + tracks dispatchers for teardown."""
    from turnstone.console.notify_dispatcher import NotifyDispatcher

    created: list[NotifyDispatcher] = []

    def _make(*, channels: list[str]) -> NotifyDispatcher:
        d = NotifyDispatcher(storage, channels=channels)
        created.append(d)
        return d

    yield _make

    for d in created:
        d.stop(timeout=2.0)


def _wait_for(predicate, deadline_sec: float = 3.0) -> bool:
    """Poll ``predicate`` until True or timeout.  Returns bool."""
    deadline = time.monotonic() + deadline_sec
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class TestSubscribe:
    def test_subscribe_registers_handler(self, dispatcher_factory, storage):
        d = dispatcher_factory(channels=["alpha"])
        seen: list = []
        d.subscribe("alpha", lambda n: seen.append(n))
        d.start()
        # Fire a notify via the storage layer — dispatcher delivers to handler.
        storage.notify("alpha", "hello")
        assert _wait_for(lambda: any(n.payload == "hello" for n in seen))

    def test_subscribe_undeclared_channel_raises(self, dispatcher_factory):
        d = dispatcher_factory(channels=["alpha"])
        with pytest.raises(ValueError, match="not declared"):
            d.subscribe("beta", lambda n: None)

    def test_subscribe_returns_unsubscribe_callable(self, dispatcher_factory, storage):
        d = dispatcher_factory(channels=["alpha"])
        seen: list = []
        unsub = d.subscribe("alpha", lambda n: seen.append(n))
        d.start()
        storage.notify("alpha", "first")
        assert _wait_for(lambda: any(n.payload == "first" for n in seen))
        unsub()
        # After unsubscribe, the handler no longer fires.  Drain old hits
        # so the next notify-vs-handler-count check is unambiguous.
        seen.clear()
        storage.notify("alpha", "second")
        # Give the dispatcher a beat to deliver if it were going to.
        time.sleep(0.2)
        assert not any(n.payload == "second" for n in seen)

    def test_construction_requires_at_least_one_channel(self, storage):
        from turnstone.console.notify_dispatcher import NotifyDispatcher

        with pytest.raises(ValueError, match="at least one"):
            NotifyDispatcher(storage, channels=[])

    def test_duplicate_channels_deduplicated(self, dispatcher_factory):
        d = dispatcher_factory(channels=["alpha", "alpha", "beta"])
        assert d.channels == ["alpha", "beta"]


class TestDispatch:
    def test_multiple_handlers_each_invoked(self, dispatcher_factory, storage):
        d = dispatcher_factory(channels=["alpha"])
        seen_a: list = []
        seen_b: list = []
        d.subscribe("alpha", lambda n: seen_a.append(n))
        d.subscribe("alpha", lambda n: seen_b.append(n))
        d.start()
        storage.notify("alpha", "shared")
        assert _wait_for(lambda: seen_a and seen_b)
        assert seen_a[0].payload == "shared"
        assert seen_b[0].payload == "shared"

    def test_handler_exception_does_not_break_dispatch(self, dispatcher_factory, storage):
        d = dispatcher_factory(channels=["alpha"])
        survived: list = []

        def _broken(_n):
            msg = "boom"
            raise RuntimeError(msg)

        d.subscribe("alpha", _broken)
        d.subscribe("alpha", lambda n: survived.append(n))
        d.start()
        storage.notify("alpha", "after_broken")
        # The second handler runs even though the first raised.
        assert _wait_for(lambda: any(n.payload == "after_broken" for n in survived))

    def test_dispatch_filters_by_channel(self, dispatcher_factory, storage):
        d = dispatcher_factory(channels=["alpha", "beta"])
        seen_a: list = []
        seen_b: list = []
        d.subscribe("alpha", lambda n: seen_a.append(n))
        d.subscribe("beta", lambda n: seen_b.append(n))
        d.start()
        storage.notify("alpha", "for_a")
        storage.notify("beta", "for_b")
        assert _wait_for(lambda: seen_a and seen_b)
        assert all(n.payload == "for_a" for n in seen_a)
        assert all(n.payload == "for_b" for n in seen_b)


class TestReconnect:
    """Reconnect + synthetic ``reconcile`` notify on stream-open success.

    Uses a stub storage that owns its own listen stream so the test can
    drive a controlled stream-error sequence — the SQLite path can't
    raise :class:`NotifyConnectionError`, and the PG path requires a
    real database outage to exercise this code, neither of which fits a
    unit test.  The dispatcher's threading and reconcile-pending logic
    are storage-agnostic — the dispatcher sees the same
    :class:`NotifyStream` Protocol regardless of backend.
    """

    def test_reconcile_fires_after_reopen_not_before(self):
        from turnstone.console.notify_dispatcher import NotifyDispatcher
        from turnstone.core.storage._notify import Notify, NotifyConnectionError

        # State machine: open -> first poll raises NotifyConnectionError
        # -> dispatcher waits backoff then reopens -> second open's first
        # poll blocks forever (test stops the dispatcher before then).
        # The fix: synthetic reconcile fires AFTER the second open
        # succeeds, not after the first open fails.
        sequence: list[str] = []
        reopen_event = threading.Event()

        class _StubStream:
            def __init__(self, fail_first_poll: bool):
                self._fail = fail_first_poll
                self._closed = False

            def poll(self, _timeout):
                if self._closed:
                    return []
                if self._fail:
                    self._fail = False
                    sequence.append("poll_raises")
                    msg = "fake-disconnect"
                    raise NotifyConnectionError(msg)
                sequence.append("poll_returns")
                # Block until close to simulate a quiet steady-state.
                time.sleep(0.5)
                return []

            def close(self):
                self._closed = True

        class _StubStorage:
            def __init__(self):
                self._open_count = 0

            def listen(self, _channels):
                import contextlib as _contextlib

                @_contextlib.contextmanager
                def _cm():
                    self._open_count += 1
                    sequence.append(f"open_{self._open_count}")
                    if self._open_count == 2:
                        reopen_event.set()
                    stream = _StubStream(fail_first_poll=(self._open_count == 1))
                    try:
                        yield stream
                    finally:
                        stream.close()

                return _cm()

        # Speed up backoff so the reopen happens promptly in the test.
        import turnstone.console.notify_dispatcher as nd_mod

        original_backoff = nd_mod._RECONNECT_BACKOFF_INITIAL
        nd_mod._RECONNECT_BACKOFF_INITIAL = 0.05
        try:
            d = NotifyDispatcher(_StubStorage(), channels=["alpha"])
            got: list[Notify] = []
            d.subscribe("alpha", lambda n: got.append(n))
            d.start()
            try:
                # Wait for the second open (post-reconnect).
                assert reopen_event.wait(3.0), "dispatcher did not reopen after disconnect"
                # Reconcile should be delivered shortly after the reopen.
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if any(n.payload == "reconcile" for n in got):
                        break
                    time.sleep(0.02)
                assert any(n.payload == "reconcile" for n in got), (
                    f"no reconcile delivered; sequence={sequence}, got={got}"
                )
                # The reconcile must NOT fire before the second open —
                # if it did, the index of 'open_2' in sequence would
                # come after any reconcile-emitting work.  Check ordering:
                # 'open_1' < 'poll_raises' < 'open_2' (synthesize happens
                # inside the with-block of the SECOND open).
                ix_open_1 = sequence.index("open_1")
                ix_raises = sequence.index("poll_raises")
                ix_open_2 = sequence.index("open_2")
                assert ix_open_1 < ix_raises < ix_open_2
            finally:
                d.stop(timeout=2.0)
        finally:
            nd_mod._RECONNECT_BACKOFF_INITIAL = original_backoff

    def test_generic_exception_path_also_synthesizes_reconcile(self):
        """Exceptions thrown during ``listen()`` (not via stream.poll) still trigger reconcile.

        Models the ``psycopg.connect()`` / initial ``LISTEN`` failure
        shape, which doesn't go through the stream's exception
        translator and would hit the generic ``except Exception``
        branch.  Pre-fix, that branch emitted no reconcile.
        """
        from turnstone.console.notify_dispatcher import NotifyDispatcher

        reopen_event = threading.Event()

        class _StubStream:
            def __init__(self):
                self._closed = False

            def poll(self, _timeout):
                if self._closed:
                    return []
                time.sleep(0.5)
                return []

            def close(self):
                self._closed = True

        class _StubStorage:
            def __init__(self):
                self._open_count = 0

            def listen(self, _channels):
                import contextlib as _contextlib

                self._open_count += 1
                if self._open_count == 1:
                    # First open raises a generic exception (e.g.
                    # ``psycopg.OperationalError`` from a failed connect)
                    # — landing in the dispatcher's generic except branch.
                    msg = "fake-connect-failure"
                    raise RuntimeError(msg)

                @_contextlib.contextmanager
                def _cm():
                    reopen_event.set()
                    stream = _StubStream()
                    try:
                        yield stream
                    finally:
                        stream.close()

                return _cm()

        import turnstone.console.notify_dispatcher as nd_mod

        original_backoff = nd_mod._RECONNECT_BACKOFF_INITIAL
        nd_mod._RECONNECT_BACKOFF_INITIAL = 0.05
        try:
            d = NotifyDispatcher(_StubStorage(), channels=["alpha"])
            got: list = []
            d.subscribe("alpha", lambda n: got.append(n))
            d.start()
            try:
                assert reopen_event.wait(3.0), "dispatcher did not reopen after generic exception"
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if any(n.payload == "reconcile" for n in got):
                        break
                    time.sleep(0.02)
                assert any(n.payload == "reconcile" for n in got), (
                    "no reconcile delivered after generic-exception recovery"
                )
            finally:
                d.stop(timeout=2.0)
        finally:
            nd_mod._RECONNECT_BACKOFF_INITIAL = original_backoff


class TestCoalescing:
    """Same-channel burst collapses to one handler invocation per batch."""

    def test_burst_coalesces_to_one_handler_call_per_channel(self, dispatcher_factory, storage):
        d = dispatcher_factory(channels=["alpha"])
        invocations: list = []
        # Slow handler to ensure all bursts queue up before the first
        # call returns — gives the dispatch loop time to coalesce.
        coalesce_gate = threading.Event()

        def _slow_handler(n):
            invocations.append(n)
            coalesce_gate.wait(0.05)

        d.subscribe("alpha", _slow_handler)
        d.start()
        # Burst of 10 notifies on the same channel — should coalesce
        # down to many fewer handler invocations.
        for i in range(10):
            storage.notify("alpha", str(i))
        # Wait until the dispatch settles (handler is called at least once
        # and the queue empties).
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if invocations and d._dispatch_queue.empty():
                time.sleep(0.1)  # allow any final coalesced call to land
                break
            time.sleep(0.02)
        coalesce_gate.set()
        # At least one handler call; well fewer than 10 (coalescing
        # collapsed the burst).  Exact count depends on timing — typical
        # is 1-2 invocations per burst on a fast machine.
        assert invocations, "handler never fired"
        assert len(invocations) < 10, (
            f"expected coalescing to collapse burst of 10; got {len(invocations)} invocations"
        )


class TestLifecycle:
    def test_start_is_idempotent(self, dispatcher_factory):
        d = dispatcher_factory(channels=["alpha"])
        d.start()
        d.start()  # No-op, no thread doubling
        # Single listener + single dispatch thread are spawned regardless.
        # Inspect by name so we don't depend on the exact thread count of
        # the test runner.
        listener_threads = [
            t for t in threading.enumerate() if t.name == "notify-dispatcher-listener"
        ]
        dispatch_threads = [
            t for t in threading.enumerate() if t.name == "notify-dispatcher-dispatch"
        ]
        assert len(listener_threads) == 1
        assert len(dispatch_threads) == 1

    def test_stop_is_idempotent(self, dispatcher_factory):
        d = dispatcher_factory(channels=["alpha"])
        d.start()
        d.stop(timeout=2.0)
        d.stop(timeout=2.0)  # No-op, no error

    def test_stop_without_start_is_noop(self, dispatcher_factory):
        d = dispatcher_factory(channels=["alpha"])
        d.stop(timeout=1.0)  # No-op, no thread to join

    def test_stop_joins_threads(self, dispatcher_factory):
        d = dispatcher_factory(channels=["alpha"])
        d.start()
        # Capture thread references then stop and assert they exited.
        threads_before = [
            t
            for t in threading.enumerate()
            if t.name in {"notify-dispatcher-listener", "notify-dispatcher-dispatch"}
        ]
        assert threads_before
        d.stop(timeout=3.0)
        time.sleep(0.05)
        for t in threads_before:
            assert not t.is_alive(), f"{t.name} still alive after stop"
