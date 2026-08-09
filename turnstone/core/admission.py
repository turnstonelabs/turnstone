"""Per-alias admission control for model dispatch.

The registry owns one :class:`ModelAdmission` object per alias and keeps that
object stable across hot reloads.  A zero limit is unlimited, but calls are
still counted so a live ``0 -> N`` resize can drain already-running work before
admitting more.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from turnstone.core.deadline import DeadlineCancelledError
from turnstone.core.log import get_logger

log = get_logger(__name__)

_CANCEL_POLL_SECONDS = 0.25
_STALL_WARNING_SECONDS = 5.0


def _call_hook(target: Any, name: str) -> None:
    hook = getattr(target, name, None)
    if callable(hook):
        with contextlib.suppress(Exception):
            hook()


@dataclass(frozen=True, slots=True)
class AdmissionSnapshot:
    """Non-sensitive instantaneous state for logs and tests."""

    alias: str
    limit: int
    in_flight: int
    queued: int


class AdmissionLease:
    """One idempotently releasable admission hold."""

    __slots__ = ("_gate", "wait_seconds", "queued_ahead", "_released")

    def __init__(
        self,
        gate: ModelAdmission,
        *,
        wait_seconds: float,
        queued_ahead: int,
    ) -> None:
        self._gate = gate
        self.wait_seconds = wait_seconds
        self.queued_ahead = queued_ahead
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._gate._release()

    def __enter__(self) -> AdmissionLease:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class ModelAdmission:
    """FIFO, hot-resizable in-flight gate for one model alias.

    ``limit == 0`` preserves unlimited behavior.  Unlimited calls still take
    leases and increment ``in_flight`` so narrowing the gate during a hot reload
    observes and drains them rather than starting from a false zero.
    """

    def __init__(self, alias: str, limit: int = 0) -> None:
        if type(limit) is not int or limit < 0:
            raise ValueError("model admission limit must be a non-negative integer")
        self.alias = alias
        self._limit = limit
        self._in_flight = 0
        self._waiters: deque[object] = deque()
        self._cv = threading.Condition()

    @property
    def limit(self) -> int:
        with self._cv:
            return self._limit

    def snapshot(self) -> AdmissionSnapshot:
        with self._cv:
            return AdmissionSnapshot(
                alias=self.alias,
                limit=self._limit,
                in_flight=self._in_flight,
                queued=len(self._waiters),
            )

    def set_limit(self, limit: int) -> None:
        """Resize in place; narrowing lets current holders drain naturally."""
        if type(limit) is not int or limit < 0:
            raise ValueError("model admission limit must be a non-negative integer")
        with self._cv:
            if limit == self._limit:
                return
            previous = self._limit
            self._limit = limit
            self._cv.notify_all()
            log.info(
                "model.admission_resized",
                alias=self.alias,
                previous_limit=previous,
                limit=limit,
                in_flight=self._in_flight,
                queued=len(self._waiters),
            )

    def _available(self) -> bool:
        return self._limit == 0 or self._in_flight < self._limit

    def acquire(self, *, cancel_ref: Any = None) -> AdmissionLease:
        """Wait FIFO for a slot, abandoning promptly when *cancel_ref* aborts."""
        if bool(getattr(cancel_ref, "aborted", False)):
            raise DeadlineCancelledError("cancel_ref aborted during model admission")

        started = time.monotonic()
        ticket = object()
        queued_ahead = 0
        waiting = False
        did_wait = False
        warned = False
        try:
            with self._cv:
                if self._waiters or not self._available():
                    queued_ahead = len(self._waiters)
                    self._waiters.append(ticket)
                    waiting = True
                    did_wait = True
                    _call_hook(cancel_ref, "begin_admission_wait")

                while waiting:
                    if bool(getattr(cancel_ref, "aborted", False)):
                        with contextlib.suppress(ValueError):
                            self._waiters.remove(ticket)
                        self._cv.notify_all()
                        raise DeadlineCancelledError("cancel_ref aborted during model admission")
                    if self._waiters[0] is ticket and self._available():
                        self._waiters.popleft()
                        waiting = False
                        break
                    waited = time.monotonic() - started
                    if not warned and waited >= _STALL_WARNING_SECONDS:
                        warned = True
                        log.warning(
                            "model.admission_stalled",
                            alias=self.alias,
                            limit=self._limit,
                            in_flight=self._in_flight,
                            queued=len(self._waiters),
                            wait_seconds=round(waited, 3),
                        )
                    self._cv.wait(_CANCEL_POLL_SECONDS)

                self._in_flight += 1
                # When the limit is wider than one, let the new FIFO head claim
                # the next already-free slot without waiting for this holder to
                # release first.
                self._cv.notify_all()
        finally:
            if waiting:
                # An unexpected hook/condition failure must not strand its FIFO
                # ticket.  The normal cancellation path already removed it.
                with self._cv:
                    with contextlib.suppress(ValueError):
                        self._waiters.remove(ticket)
                    self._cv.notify_all()
            if did_wait:
                _call_hook(cancel_ref, "end_admission_wait")

        waited = time.monotonic() - started if did_wait else 0.0
        lease = AdmissionLease(
            self,
            wait_seconds=waited,
            queued_ahead=queued_ahead,
        )
        if bool(getattr(cancel_ref, "aborted", False)):
            lease.release()
            raise DeadlineCancelledError("cancel_ref aborted during model admission")
        if waited > 0:
            log.info(
                "model.admission_wait",
                alias=self.alias,
                limit=self.limit,
                queued_ahead=queued_ahead,
                wait_seconds=round(waited, 3),
            )
        return lease

    def _release(self) -> None:
        with self._cv:
            if self._in_flight <= 0:
                raise RuntimeError("model admission lease released without a holder")
            self._in_flight -= 1
            self._cv.notify_all()
