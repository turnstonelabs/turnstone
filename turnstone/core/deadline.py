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

import contextlib
import queue
import threading
import time
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

_T = TypeVar("_T")


class DeadlineExceededError(Exception):
    """The call did not complete before its wall-clock deadline."""


class DeadlineCancelledError(Exception):
    """The cancel event fired before the call completed."""


class StreamAbortRef(list[Any]):
    """A provider ``cancel_ref`` that can abort the abandoned call's stream.

    Providers append the live SDK stream handle (which has ``.close()``)
    before yielding the first chunk.  A caller that abandons the daemon
    worker (deadline/cancel) then calls :meth:`abort` — the captured
    stream is closed so the worker's blocked HTTP read raises promptly and
    the thread exits, instead of staying pinned until the provider sends
    its next SSE chunk (or forever, on a wedged upstream).

    The append hook covers the arrival race: if the abort fires while the
    worker is still inside the SDK's connect (no handle captured yet), the
    handle is closed the moment it arrives.  Both paths tolerate double
    close (SDK ``close()`` is idempotent) so no lock is needed — mirrors
    ``ChatSession``'s ``_CancelRef``, which adopts this class when the
    main loop moves onto ``model_turn`` (#832); until then a hardening
    fix here must be mirrored there.
    """

    __slots__ = ("_aborted",)

    def __init__(self) -> None:
        super().__init__()
        self._aborted = False

    def append(self, stream: Any) -> None:
        super().append(stream)
        if self._aborted:
            with contextlib.suppress(Exception):
                stream.close()

    def abort(self) -> None:
        """Close any captured stream; late arrivals close on append."""
        self._aborted = True
        for stream in list(self):
            with contextlib.suppress(Exception):
                stream.close()

    @property
    def aborted(self) -> bool:
        """Whether :meth:`abort` has fired.

        ``model_turn``'s drain-retry gate reads this (duck-typed off any
        ``cancel_ref``): an aborted stream dies with a transport error
        that looks retryable, and re-issuing the request would resurrect
        a call its deadline already abandoned.
        """
        return self._aborted


def run_abortable_with_deadline(
    fn: Callable[[StreamAbortRef], _T],
    *,
    timeout: float,
    cancel_event: threading.Event | None = None,
    poll: float = 1.0,
    thread_name: str = "deadline-worker",
) -> _T:
    """:func:`run_with_deadline` with the stream-abort wiring built in.

    Mints a :class:`StreamAbortRef`, hands it to *fn* (thread it into the
    provider call as ``cancel_ref``), and aborts it on either abandonment
    path — the three-point pairing (ref + ``cancel_ref`` + ``on_abandon``)
    cannot be half-wired.  The canonical deadline-bounded sampling shape::

        run_abortable_with_deadline(
            lambda ref: model_turn(lane, turns, cancel_ref=ref, ...),
            timeout=...,
        )
    """
    abort_ref = StreamAbortRef()
    return run_with_deadline(
        lambda: fn(abort_ref),
        timeout=timeout,
        cancel_event=cancel_event,
        poll=poll,
        thread_name=thread_name,
        on_abandon=abort_ref.abort,
    )


def run_with_deadline(
    fn: Callable[[], _T],
    *,
    timeout: float,
    cancel_event: threading.Event | None = None,
    poll: float = 1.0,
    thread_name: str = "deadline-worker",
    on_abandon: Callable[[], None] | None = None,
) -> _T:
    """Run ``fn()`` on a daemon thread, bounded by ``timeout``/``cancel_event``.

    Returns ``fn()``'s result, or re-raises whatever ``fn`` raised.  Raises
    :class:`DeadlineExceededError` if ``timeout`` seconds elapse first, or
    :class:`DeadlineCancelledError` if ``cancel_event`` fires first.  On either
    abort the worker thread is abandoned; being a daemon it cannot block
    process or interpreter exit.

    ``on_abandon`` runs (best-effort) right before either abandonment raise —
    the one hook for releasing whatever the worker is blocked on, so callers
    can't wire one abort path and forget the other.  The canonical use is
    ``on_abandon=abort_ref.abort`` with a :class:`StreamAbortRef` threaded
    into the provider call as ``cancel_ref``: the abandoned worker's blocked
    HTTP read raises promptly instead of pinning the thread until the next
    upstream chunk.

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

    def _abandon(exc: Exception) -> None:
        if on_abandon is not None:
            with contextlib.suppress(Exception):
                on_abandon()
        raise exc

    deadline = time.monotonic() + timeout
    while True:
        # Prefer a result that has already arrived over a deadline or cancel
        # firing in the same scheduling window — otherwise a completed call
        # could be reported as a spurious timeout/cancel under jitter.
        try:
            ok, payload = box.get_nowait()
        except queue.Empty:
            pass
        else:
            if ok:
                return payload  # type: ignore[return-value]  # ok=True ⇒ payload is _T
            raise payload  # type: ignore[misc]  # ok=False ⇒ payload is the raised exc

        if cancel_event is not None and cancel_event.is_set():
            _abandon(DeadlineCancelledError())
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _abandon(DeadlineExceededError())
        try:
            ok, payload = box.get(timeout=min(remaining, poll))
        except queue.Empty:
            continue
        if ok:
            return payload  # type: ignore[return-value]
        raise payload  # type: ignore[misc]
