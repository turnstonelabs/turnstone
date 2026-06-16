"""Run a blocking call under a wall-clock deadline on a daemon thread.

The motivating constraint comes from the judges (:mod:`turnstone.core.judge`,
:mod:`turnstone.core.output_guard_judge`): an upstream LLM call must be
*abandonable* the instant its timeout or cancel fires, without the abandoned
call being able to block process or interpreter exit.

A :class:`~concurrent.futures.ThreadPoolExecutor` worker is **non-daemon**, and
``concurrent.futures`` joins every executor worker from an ``atexit`` hook
(``_python_exit``) regardless of ``shutdown(wait=False)``.  So an upstream call
wedged with no socket timeout hangs interpreter shutdown forever — which is
exactly how a single slow judge call can deadlock a whole test run at exit.

A **daemon** worker is never joined at exit, so abandoning one is always safe:
the call keeps running until it returns or the process dies, whichever comes
first, and never pins shutdown.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

_T = TypeVar("_T")


class DeadlineExceededError(Exception):
    """The call did not complete before its wall-clock deadline."""


class DeadlineCancelledError(Exception):
    """The cancel event fired before the call completed."""


def run_with_deadline(
    fn: Callable[[], _T],
    *,
    timeout: float,
    cancel_event: threading.Event | None = None,
    poll: float = 1.0,
    thread_name: str = "deadline-worker",
) -> _T:
    """Run ``fn()`` on a daemon thread, bounded by ``timeout``/``cancel_event``.

    Returns ``fn()``'s result, or re-raises whatever ``fn`` raised.  Raises
    :class:`DeadlineExceededError` if ``timeout`` seconds elapse first, or
    :class:`DeadlineCancelledError` if ``cancel_event`` fires first.  On either
    abort the worker thread is abandoned; being a daemon it cannot block
    process or interpreter exit.

    ``poll`` bounds how often ``cancel_event`` is checked (and thus the worst-
    case latency from a cancel to this function returning).
    """
    box: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def _runner() -> None:
        try:
            box.put((True, fn()))
        except BaseException as exc:  # noqa: BLE001 - relayed to the caller verbatim
            box.put((False, exc))

    threading.Thread(target=_runner, name=thread_name, daemon=True).start()

    deadline = time.monotonic() + timeout
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise DeadlineCancelledError
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeadlineExceededError
        try:
            ok, payload = box.get(timeout=min(remaining, poll))
        except queue.Empty:
            continue
        if ok:
            return payload  # type: ignore[return-value]  # ok=True ⇒ payload is _T
        raise payload  # type: ignore[misc]  # ok=False ⇒ payload is the raised exc
