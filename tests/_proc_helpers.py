"""Shared process/polling helpers for the bash + background-shell suites.

One copy instead of three: ``test_bash_tool_background_hang``,
``test_background_shells`` and ``test_bash_background_tool`` all assert on
process liveness and poll for asynchronous state.  Leading underscore so
pytest doesn't collect it.
"""

from __future__ import annotations

import contextlib
import os
import signal
import time


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_pid(pid: int) -> None:
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)


def poll_until(predicate, timeout=10.0, interval=0.05):
    """Poll ``predicate`` until truthy or ``timeout``; RETURNS the last value
    (falsy on timeout — assert at the call site).  Deliberately named apart
    from ``tests/_helpers.wait_until``, which RAISES on timeout: two
    same-named helpers with opposite failure semantics invite silently-green
    tests."""
    deadline = time.monotonic() + timeout
    value = predicate()
    while not value and time.monotonic() < deadline:
        time.sleep(interval)
        value = predicate()
    return value
