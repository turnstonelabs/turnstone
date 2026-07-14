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


def test_on_abandon_fires_on_timeout_and_cancel_but_not_success() -> None:
    calls: list[str] = []

    with pytest.raises(DeadlineExceededError):
        run_with_deadline(
            lambda: time.sleep(2.0),
            timeout=0.1,
            poll=0.05,
            thread_name="dl-abandon-t",
            on_abandon=lambda: calls.append("timeout"),
        )
    assert calls == ["timeout"]

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(DeadlineCancelledError):
        run_with_deadline(
            lambda: time.sleep(2.0),
            timeout=10.0,
            cancel_event=cancel,
            poll=0.05,
            thread_name="dl-abandon-c",
            on_abandon=lambda: calls.append("cancel"),
        )
    assert calls == ["timeout", "cancel"]

    result = run_with_deadline(lambda: 7, timeout=1.0, on_abandon=lambda: calls.append("no"))
    assert result == 7
    assert calls == ["timeout", "cancel"]


def test_on_abandon_errors_do_not_mask_the_deadline_error() -> None:
    def _boom() -> None:
        raise RuntimeError("abort hook broke")

    with pytest.raises(DeadlineExceededError):
        run_with_deadline(
            lambda: time.sleep(2.0),
            timeout=0.1,
            poll=0.05,
            thread_name="dl-abandon-e",
            on_abandon=_boom,
        )


class TestStreamAbortRef:
    def test_abort_closes_captured_stream(self) -> None:
        from unittest.mock import MagicMock

        from turnstone.core.deadline import StreamAbortRef

        ref = StreamAbortRef()
        stream = MagicMock()
        ref.append(stream)
        stream.close.assert_not_called()
        ref.abort()
        stream.close.assert_called_once()

    def test_late_arriving_stream_closes_on_append(self) -> None:
        # The arrival race: abort fires while the worker is still inside the
        # SDK connect — the handle must close the moment it is captured.
        from unittest.mock import MagicMock

        from turnstone.core.deadline import StreamAbortRef

        ref = StreamAbortRef()
        ref.abort()
        stream = MagicMock()
        ref.append(stream)
        stream.close.assert_called_once()
