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

    __slots__ = (
        "_aborted",
        "_cancel_event",
        "_timing_lock",
        "_admission_wait_started",
        "_admission_wait_credit",
        "_dispatch_count",
        "_last_dispatch_at",
    )

    def __init__(self, cancel_event: threading.Event | None = None) -> None:
        super().__init__()
        self._aborted = False
        self._cancel_event = cancel_event
        self._timing_lock = threading.Lock()
        self._admission_wait_started: float | None = None
        self._admission_wait_credit = 0.0
        self._dispatch_count = 0
        self._last_dispatch_at: float | None = None

    def append(self, stream: Any) -> None:
        super().append(stream)
        if self.aborted:
            with contextlib.suppress(Exception):
                stream.close()

    def abort(self) -> None:
        """Close any captured stream; late arrivals close on append."""
        self._aborted = True
        for stream in list(self):
            with contextlib.suppress(Exception):
                stream.close()

    def begin_admission_wait(self) -> None:
        """Freeze this call's deadline while it waits for model admission."""
        with self._timing_lock:
            if self._admission_wait_started is None:
                self._admission_wait_started = time.monotonic()

    def end_admission_wait(self) -> None:
        """Resume the deadline and retain the elapsed admission credit."""
        now = time.monotonic()
        with self._timing_lock:
            started = self._admission_wait_started
            if started is None:
                return
            self._admission_wait_credit += max(0.0, now - started)
            self._admission_wait_started = None

    def admission_wait_credit(self) -> float:
        """Return completed plus currently accruing admission-wait time."""
        now = time.monotonic()
        with self._timing_lock:
            credit = self._admission_wait_credit
            if self._admission_wait_started is not None:
                credit += max(0.0, now - self._admission_wait_started)
            return credit

    def mark_dispatch(self) -> None:
        """Record a provider dispatch without changing deadline semantics."""
        with self._timing_lock:
            self._dispatch_count += 1
            self._last_dispatch_at = time.monotonic()

    @property
    def dispatch_count(self) -> int:
        """Number of provider dispatch attempts observed by this ref."""
        with self._timing_lock:
            return self._dispatch_count

    @property
    def last_dispatch_at(self) -> float | None:
        """Monotonic timestamp of the latest provider dispatch, if any."""
        with self._timing_lock:
            return self._last_dispatch_at

    @property
    def aborted(self) -> bool:
        """Whether :meth:`abort` has fired.

        ``model_turn`` reads this (duck-typed off any ``cancel_ref``) in
        three roles, all load-bearing.  On entry, so an abandoned call
        skips the lowering and the credential resolve.  Immediately
        before it dispatches, so an abort observed by then costs no
        request.  And at its drain-retry gates, where an aborted stream
        dies with a transport error that looks retryable and re-issuing
        would resurrect a call its deadline already abandoned.  No read
        closes the window — an abort firing after the last one still
        meets the arriving handle at :meth:`append`, which is why that
        hook is not redundant with them.
        """
        return self._aborted or bool(self._cancel_event is not None and self._cancel_event.is_set())


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
    abort_ref = StreamAbortRef(cancel_event)
    return run_with_deadline(
        lambda: fn(abort_ref),
        timeout=timeout,
        cancel_event=cancel_event,
        poll=poll,
        thread_name=thread_name,
        on_abandon=abort_ref.abort,
        deadline_credit=abort_ref.admission_wait_credit,
    )


def run_with_deadline(
    fn: Callable[[], _T],
    *,
    timeout: float,
    cancel_event: threading.Event | None = None,
    poll: float = 1.0,
    thread_name: str = "deadline-worker",
    on_abandon: Callable[[], None] | None = None,
    deadline_credit: Callable[[], float] | None = None,
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

    ``deadline_credit`` may dynamically extend the original deadline.  The
    abortable wrapper uses it only for time spent queued at model admission;
    lowering, attachment materialization, credential minting, dispatch,
    draining, and retry backoff continue to consume the original budget.
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
        credit = 0.0
        if deadline_credit is not None:
            with contextlib.suppress(Exception):
                credit = max(0.0, float(deadline_credit()))
        remaining = deadline + credit - time.monotonic()
        if remaining <= 0:
            _abandon(DeadlineExceededError())
        try:
            ok, payload = box.get(timeout=min(remaining, poll))
        except queue.Empty:
            continue
        if ok:
            return payload  # type: ignore[return-value]
        raise payload  # type: ignore[misc]
