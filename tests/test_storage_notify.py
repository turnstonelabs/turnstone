"""Tests for the storage layer's cross-process ``notify`` / ``listen`` API.

Covers SQLite (synthetic-sweep + in-process fan-out) and PostgreSQL
(real ``LISTEN``/``NOTIFY``).  The PG-only cases are gated on the
``--storage-backend=postgresql`` flag so they no-op on default CI runs.
"""

from __future__ import annotations

import threading
import time

import pytest


def _drain_until(stream, predicate, deadline_sec: float = 5.0):
    """Poll ``stream`` until ``predicate`` matches one of the drained notifies.

    Returns the matching notify or raises ``TimeoutError``.  Tests use
    this so timing flakes against the bounded-blocking ``poll`` shape
    don't masquerade as logic bugs.
    """
    deadline = time.monotonic() + deadline_sec
    while time.monotonic() < deadline:
        remaining = max(0.05, deadline - time.monotonic())
        for n in stream.poll(min(0.5, remaining)):
            if predicate(n):
                return n
    msg = "no matching notify drained before deadline"
    raise TimeoutError(msg)


class TestSqliteNotify:
    """SQLite path: in-process fan-out + synthetic sweep."""

    def test_notify_no_listeners_is_noop(self, storage):
        # No exception, no side effect — safe to always call from dispatch.
        storage.notify("services", '{"op": "INSERT"}')

    def test_notify_delivers_to_in_process_listener(self, storage):
        with storage.listen(["services"]) as stream:
            storage.notify("services", '{"op": "INSERT"}')
            got = _drain_until(stream, lambda n: n.payload == '{"op": "INSERT"}')
            assert got.channel == "services"
            assert got.pid == 0

    def test_notify_filters_by_channel(self, storage):
        with storage.listen(["services"]) as stream:
            storage.notify("other_channel", "ignored")
            storage.notify("services", "wanted")
            got = _drain_until(stream, lambda n: True)
            assert got.payload == "wanted"

    def test_multiple_listeners_each_get_event(self, storage):
        # Two streams open on the same channel; each gets its own copy.
        with storage.listen(["services"]) as s1, storage.listen(["services"]) as s2:
            storage.notify("services", "broadcast")
            got1 = _drain_until(s1, lambda n: True)
            got2 = _drain_until(s2, lambda n: True)
            assert got1.payload == "broadcast"
            assert got2.payload == "broadcast"

    def test_close_stops_stream(self, storage):
        with storage.listen(["services"]) as stream:
            pass
        # After context exit, the stream is closed; poll returns [] without
        # blocking.  A second close() is idempotent.
        assert stream.poll(0.05) == []
        stream.close()

    def test_synthetic_sweep_emits_after_interval(self, storage):
        # Force a short sweep interval via direct attribute override —
        # the production default (``_SQLITE_NOTIFY_SWEEP_INTERVAL``) is
        # tuned for an idle dev backstop and is way too long for a test.
        with storage.listen(["services"]) as stream:
            stream._sweep_interval = 0.1
            # First poll: not yet at the interval, so likely empty.
            stream.poll(0.05)
            # Wait past the interval, then poll again — should emit a
            # synthetic-sweep notify per declared channel.
            time.sleep(0.15)
            got = _drain_until(stream, lambda n: n.payload == "sweep")
            assert got.channel == "services"
            assert got.payload == "sweep"

    def test_empty_channel_list_yields_empty_stream(self, storage):
        with storage.listen([]) as stream:
            # No channels — poll returns [] regardless of how long we wait.
            assert stream.poll(0.05) == []


# ---------------------------------------------------------------------------
# PostgreSQL path — gated on --storage-backend=postgresql.
# ---------------------------------------------------------------------------


@pytest.fixture
def _is_postgres(storage):
    """Skip the wrapped test when the active backend isn't Postgres."""
    if storage.__class__.__name__ != "PostgreSQLBackend":
        pytest.skip("PostgreSQL-specific test")
    return True


class TestPostgresNotify:
    def test_round_trip(self, storage, _is_postgres):
        # Open a listener, fire a notify on a regular pooled connection,
        # drain the listener within a reasonable bound (PG NOTIFY is
        # typically sub-100ms on a local socket).
        with storage.listen(["pytest_round_trip"]) as stream:
            # Tiny sleep so the LISTEN settles before the NOTIFY fires —
            # otherwise the notify can arrive on the connection before
            # the LISTEN is registered (race only visible in tests).
            time.sleep(0.05)
            storage.notify("pytest_round_trip", '{"hello": "world"}')
            got = _drain_until(stream, lambda n: True, deadline_sec=3.0)
            assert got.channel == "pytest_round_trip"
            assert got.payload == '{"hello": "world"}'
            assert got.pid > 0

    def test_concurrent_notifies_all_arrive(self, storage, _is_postgres):
        with storage.listen(["pytest_concurrent"]) as stream:
            time.sleep(0.05)
            for i in range(5):
                storage.notify("pytest_concurrent", str(i))
            seen: set[str] = set()
            deadline = time.monotonic() + 3.0
            while len(seen) < 5 and time.monotonic() < deadline:
                for n in stream.poll(0.2):
                    seen.add(n.payload)
            assert seen == {"0", "1", "2", "3", "4"}

    def test_close_aborts_blocked_poll(self, storage, _is_postgres):
        # poll() should return promptly once close() runs on another thread.
        with storage.listen(["pytest_close"]) as stream:
            done = threading.Event()
            result: list[list] = []

            def _poll_long():
                result.append(stream.poll(5.0))
                done.set()

            t = threading.Thread(target=_poll_long, daemon=True)
            t.start()
            time.sleep(0.1)
            stream.close()
            assert done.wait(2.0), "close() did not unblock poll()"
            # No notify arrived, so the polled batch is empty — but the
            # poll loop must have exited well under the 5 s timeout.
            assert result == [[]]


class TestServicesTriggerFilter:
    """Migration 053's trigger: fires on real changes, quiet on heartbeats.

    PG-only — the SQLite path has no trigger and is covered by
    :class:`TestSqliteNotify`.  Verifies the in-trigger ``IS NOT DISTINCT
    FROM`` filter — a heartbeat-only UPDATE (same url + same metadata,
    only ``last_heartbeat`` changed) must NOT emit a NOTIFY, since
    ``register_service`` runs the same UPSERT on every 30 s tick × N
    nodes and the channel would otherwise flood.
    """

    def test_insert_fires_notify(self, storage, _is_postgres):
        with storage.listen(["services"]) as stream:
            time.sleep(0.05)
            storage.register_service("server", "pytest-trigger-node", "http://127.0.0.1:1")
            got = _drain_until(stream, lambda n: True, deadline_sec=3.0)
            assert got.channel == "services"
            assert '"op": "INSERT"' in got.payload or "INSERT" in got.payload
            # Cleanup so concurrent suites don't pick up the row.
            storage.deregister_service("server", "pytest-trigger-node")

    def test_delete_fires_notify(self, storage, _is_postgres):
        storage.register_service("server", "pytest-trigger-node-del", "http://127.0.0.1:2")
        with storage.listen(["services"]) as stream:
            time.sleep(0.05)
            storage.deregister_service("server", "pytest-trigger-node-del")
            got = _drain_until(stream, lambda n: True, deadline_sec=3.0)
            assert "DELETE" in got.payload

    def test_url_change_update_fires_notify(self, storage, _is_postgres):
        storage.register_service("server", "pytest-trigger-node-url", "http://127.0.0.1:3")
        with storage.listen(["services"]) as stream:
            time.sleep(0.05)
            # UPSERT with different url — UPDATE path with url diff,
            # trigger must fire.
            storage.register_service("server", "pytest-trigger-node-url", "http://127.0.0.1:9")
            got = _drain_until(stream, lambda n: True, deadline_sec=3.0)
            assert "UPDATE" in got.payload
            storage.deregister_service("server", "pytest-trigger-node-url")

    def test_heartbeat_only_update_is_quiet(self, storage, _is_postgres):
        # Open the LISTEN session FIRST so PG delivers the INSERT NOTIFY
        # to this connection — pg_notify routes only to sessions that
        # have LISTENed at COMMIT time, so an INSERT committed before the
        # listen opens would be lost and the drain would time out instead
        # of exercising the heartbeat-quiet check below.
        with storage.listen(["services"]) as stream:
            time.sleep(0.05)
            storage.register_service("server", "pytest-trigger-node-hb", "http://127.0.0.1:4")
            # Drain the INSERT notify so subsequent polls see only what
            # heartbeats emit (if anything).
            _drain_until(stream, lambda n: True, deadline_sec=2.0)
            # Now fire a heartbeat tick — same url + same metadata,
            # only last_heartbeat updates.  Trigger must NOT emit.
            storage.heartbeat_service("server", "pytest-trigger-node-hb")
            # Poll long enough that any spurious notify would have
            # arrived; the channel must stay silent.
            spurious = stream.poll(0.5)
            assert spurious == [], f"heartbeat-only update emitted unexpected notify: {spurious}"
            storage.deregister_service("server", "pytest-trigger-node-hb")
