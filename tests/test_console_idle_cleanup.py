"""Tests for the console's coordinator idle-cleanup thread helper.

The helper itself is a tiny loop wrapping ``mgr.close_idle``; the heavy
lifting is in ``SessionManager.close_idle`` (covered in
``test_session_manager.py``) and ``bulk_close_stale_orphans`` (covered
in ``test_storage_sqlite.py``).  These tests verify the glue:

- the helper runs an initial sweep BEFORE its first sleep (cold-start
  cleanup without blocking the lifespan),
- the helper swallows exceptions so a transient DB blip can't kill the
  daemon thread,
- the helper exits cleanly when ``stop_event`` is set.

The ``stop_event`` parameter is exclusively for tests — production
callers pass ``None`` and the daemon runs for process lifetime.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

from turnstone.console.server import _coord_idle_cleanup_thread


class _StubMgr:
    def __init__(
        self, *, stop_event: threading.Event, expected_calls: int, raise_after: int = -1
    ) -> None:
        self.calls: list[float] = []
        self.sleep_calls_at_each_close: list[int] = []
        self._stop_event = stop_event
        self._expected = expected_calls
        self._raise_after = raise_after
        self._sleep_count = 0

    def close_idle(self, timeout_sec: float) -> list[str]:
        # Snapshot how many sleeps preceded this close — lets the
        # "initial sweep" test verify the first close_idle ran with
        # zero preceding sleeps.
        self.sleep_calls_at_each_close.append(self._sleep_count)
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

    def record_sleep(self, _seconds: float) -> None:
        self._sleep_count += 1


def _run_until_done(mgr: _StubMgr, stop_event: threading.Event, timeout_sec: float) -> None:
    with patch("turnstone.console.server.time.sleep", mgr.record_sleep):
        thread = threading.Thread(
            target=_coord_idle_cleanup_thread,
            args=(mgr, timeout_sec, stop_event),
            daemon=True,
        )
        thread.start()
        thread.join(timeout=2.0)
        assert not thread.is_alive(), "helper failed to exit on stop_event"


def test_coord_idle_cleanup_runs_initial_sweep_before_sleep() -> None:
    """The first close_idle call must happen BEFORE the first time.sleep —
    otherwise cold-start orphans wait one ``check_every`` interval (~30 min
    on default 2h timeout) for the first reap.  Crucial because the
    lifespan no longer does a synchronous initial sweep."""
    stop_event = threading.Event()
    mgr = _StubMgr(stop_event=stop_event, expected_calls=1)
    _run_until_done(mgr, stop_event, timeout_sec=120.0)
    assert mgr.sleep_calls_at_each_close == [0], "first close_idle should run before any sleep"


def test_coord_idle_cleanup_calls_close_idle_each_tick() -> None:
    stop_event = threading.Event()
    mgr = _StubMgr(stop_event=stop_event, expected_calls=3)
    _run_until_done(mgr, stop_event, timeout_sec=120.0)
    assert len(mgr.calls) == 3
    assert all(t == 120.0 for t in mgr.calls)


def test_coord_idle_cleanup_survives_close_idle_exceptions() -> None:
    """A transient DB error must not kill the daemon thread — the next
    tick should still fire close_idle.  Without the try/except, a single
    blip would silently leak orphans forever."""
    stop_event = threading.Event()
    mgr = _StubMgr(stop_event=stop_event, expected_calls=4, raise_after=1)
    _run_until_done(mgr, stop_event, timeout_sec=120.0)
    # All four calls must have fired despite calls 2-4 raising.
    assert len(mgr.calls) == 4


def test_coord_idle_cleanup_exits_cleanly_on_stop_event() -> None:
    """The stop_event mechanism is the test contract; verify the thread
    actually exits when the event is set, without needing exceptions or
    daemon-process termination."""
    stop_event = threading.Event()
    mgr = _StubMgr(stop_event=stop_event, expected_calls=2)
    _run_until_done(mgr, stop_event, timeout_sec=120.0)
    assert stop_event.is_set()
