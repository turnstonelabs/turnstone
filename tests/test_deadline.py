"""Tests for turnstone.core.deadline.run_with_deadline.

The load-bearing property is the daemon worker: on timeout or cancel the call
is abandoned, and the abandoned thread must be a daemon so it can never block
interpreter exit (the bug that motivated the helper — a non-daemon
ThreadPoolExecutor worker is joined by concurrent.futures' atexit hook).
"""

from __future__ import annotations

import threading
import time

import pytest

from turnstone.core.deadline import (
    DeadlineCancelledError,
    DeadlineExceededError,
    run_with_deadline,
)


def test_returns_result_on_success() -> None:
    assert run_with_deadline(lambda: 42, timeout=1.0) == 42


def test_reraises_callable_exception() -> None:
    def boom() -> None:
        raise ValueError("upstream failed")

    with pytest.raises(ValueError, match="upstream failed"):
        run_with_deadline(boom, timeout=1.0)


def test_timeout_returns_promptly_and_abandons_a_daemon_worker() -> None:
    # The worker sleeps far past the deadline; the call must return promptly
    # via DeadlineExceededError, and the abandoned worker must be a daemon so
    # it cannot pin interpreter exit.
    start = time.monotonic()
    with pytest.raises(DeadlineExceededError):
        run_with_deadline(lambda: time.sleep(2.0), timeout=0.2, poll=0.05, thread_name="dl-timeout")
    assert time.monotonic() - start < 1.0
    stragglers = [t for t in threading.enumerate() if t.name == "dl-timeout" and not t.daemon]
    assert stragglers == [], f"non-daemon worker survived: {stragglers}"


def test_cancel_returns_promptly() -> None:
    cancel = threading.Event()

    def _fire() -> None:
        time.sleep(0.1)
        cancel.set()

    threading.Thread(target=_fire, daemon=True).start()
    start = time.monotonic()
    with pytest.raises(DeadlineCancelledError):
        run_with_deadline(
            lambda: time.sleep(2.0),
            timeout=10.0,
            cancel_event=cancel,
            poll=0.05,
            thread_name="dl-cancel",
        )
    assert time.monotonic() - start < 1.0
    stragglers = [t for t in threading.enumerate() if t.name == "dl-cancel" and not t.daemon]
    assert stragglers == [], f"non-daemon worker survived: {stragglers}"
