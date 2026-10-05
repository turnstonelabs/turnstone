"""Workstream owner leases: one live writer per durable workstream incarnation.

Each :class:`~turnstone.core.session_manager.SessionManager` instance is one lease
holder. Its holder id is boot-scoped (``{node_id}/{random}``) rather than the
stable node id, so two processes configured with the same ``TURNSTONE_NODE_ID``
still conflict. Storage keeps the holder, the fencing epoch and the expiry on the
``workstreams`` row and judges expiry against the database clock. This module
owns the process-local side:

* :class:`WorkstreamLease` is the handle for one acquisition (one epoch). The
  manager and the workstream's ``ChatSession`` share it. Each durable write
  snapshots :attr:`WorkstreamLease.fence` when it is admitted, the same way it
  snapshots the workstream id, and storage rejects any fence whose holder, epoch
  and incarnation no longer match the row. Every acquisition advances the epoch
  and a release clears the holder, so a late closure from a retired object, or
  from a holder that was paused while another process took the workstream over,
  can never match again. The handle's local state only drives renewal, release
  and retirement; storage is the sole judge of a write.
* :class:`LeaseKeeper` renews every tracked lease of one holder with a single
  storage call per tick and reports each lost lease at least once.
"""

from __future__ import annotations

import queue
import secrets
import threading
from typing import TYPE_CHECKING, Literal

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.metrics import LeaseEvent
    from turnstone.core.storage._protocol import LeaseFence, StorageBackend

log = get_logger(__name__)

#: Lease lifetime granted by each acquisition or renewal, in database time.
LEASE_TTL_SECONDS = 30.0
#: Renewal cadence. Each tick starts a full interval after the previous one
#: returned, so a single missed renewal still leaves the lease live: a short
#: stall or database blip does not hand the workstream to another process,
#: while a holder that stays unreachable past the TTL can be taken over.
LEASE_RENEW_INTERVAL_SECONDS = 10.0

LeaseState = Literal["held", "lost", "released"]


def new_lease_holder(node_id: str | None) -> str:
    """Return a holder id unique to this manager instance and process boot."""
    return f"{node_id or 'local'}/{secrets.token_hex(8)}"


def record_lease_event(event: LeaseEvent, count: int = 1) -> None:
    """Count one lease lifecycle event; metrics must never break the lease path."""
    if count <= 0:
        return
    try:
        from turnstone.core.metrics import metrics

        metrics.record_lease_event(event, count)
    except Exception:
        log.debug("workstream_lease.metrics_failed event=%s", event, exc_info=True)


class WorkstreamLease:
    """Process-local handle for one acquisition of a workstream owner lease."""

    __slots__ = ("_fence", "_lock", "_state")

    def __init__(self, fence: LeaseFence) -> None:
        self._fence = fence
        self._lock = threading.Lock()
        self._state: LeaseState = "held"

    def __repr__(self) -> str:
        return (
            f"WorkstreamLease(ws_id={self.ws_id[:8]!r}, holder={self.holder!r}, "
            f"epoch={self.epoch}, state={self._state!r})"
        )

    @property
    def ws_id(self) -> str:
        return self._fence.ws_id

    @property
    def holder(self) -> str:
        return self._fence.holder

    @property
    def epoch(self) -> int:
        return self._fence.epoch

    @property
    def state(self) -> LeaseState:
        return self._state

    @property
    def held(self) -> bool:
        return self._state == "held"

    @property
    def fence(self) -> LeaseFence:
        """The immutable storage fence for this acquisition."""
        return self._fence

    def mark_lost(self) -> bool:
        """Record that this acquisition no longer owns the row; ``True`` the first time."""
        with self._lock:
            if self._state != "held":
                return False
            self._state = "lost"
            return True

    def mark_released(self) -> bool:
        """Record an orderly release; ``True`` when the handle was still held."""
        with self._lock:
            if self._state != "held":
                return False
            self._state = "released"
            return True


class LeaseKeeper:
    """Renew one holder's leases and report every lost lease at least once.

    The keeper never decides what a loss means; ``on_lost`` (the manager)
    retires the workstreams, one call per batch of losses, and must tolerate a
    second report of the same handle. A storage error during renewal is not a
    loss: the leases stay tracked and renewal retries on the next tick,
    because the database may simply be unreachable while no other process can
    take over. A tracked handle a session already marked lost (storage refused
    its write) is reported on the next tick without a storage call.

    Hosts start the threads: the renewal loop hands each tick's losses to a
    separate retirement thread, because retiring a workstream waits for its
    lifecycle lane and teardown, and that wait must never delay renewing the
    others. The retirement thread merges every batch waiting for it into one
    call. Tests drive :meth:`renew_once` directly, which retires synchronously.
    """

    def __init__(
        self,
        storage: StorageBackend,
        holder: str,
        *,
        on_lost: Callable[[list[WorkstreamLease]], None],
        ttl_seconds: float = LEASE_TTL_SECONDS,
        renew_interval_seconds: float = LEASE_RENEW_INTERVAL_SECONDS,
        label: str = "",
    ) -> None:
        if renew_interval_seconds <= 0 or ttl_seconds <= renew_interval_seconds:
            raise ValueError("lease TTL must exceed a positive renewal interval")
        self._storage = storage
        self._holder = holder
        self._on_lost = on_lost
        self._ttl_seconds = ttl_seconds
        self._renew_interval_seconds = renew_interval_seconds
        self._label = label
        self._lock = threading.Lock()
        self._leases: dict[str, WorkstreamLease] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lost_queue: queue.SimpleQueue[list[WorkstreamLease] | None] = queue.SimpleQueue()
        self._retire_thread: threading.Thread | None = None
        self._renew_failing = False

    @property
    def ttl_seconds(self) -> float:
        return self._ttl_seconds

    def track(self, lease: WorkstreamLease) -> None:
        """Renew ``lease`` from now on: the one tracked handle for its workstream."""
        with self._lock:
            self._leases[lease.ws_id] = lease

    def untrack(self, lease: WorkstreamLease) -> None:
        with self._lock:
            if self._leases.get(lease.ws_id) is lease:
                self._leases.pop(lease.ws_id, None)

    def tracked(self) -> list[WorkstreamLease]:
        """Every tracked handle, including ones already marked lost."""
        with self._lock:
            return list(self._leases.values())

    def get(self, ws_id: str) -> WorkstreamLease | None:
        with self._lock:
            return self._leases.get(ws_id)

    def start(self) -> None:
        """Start the renewal and retirement threads. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        label = self._label or "workstreams"
        lost_queue: queue.SimpleQueue[list[WorkstreamLease] | None] = queue.SimpleQueue()
        self._lost_queue = lost_queue
        self._retire_thread = threading.Thread(
            target=self._retire_loop,
            args=(lost_queue,),
            name=f"lease-retire-{label}",
            daemon=True,
        )
        self._retire_thread.start()
        self._thread = threading.Thread(
            target=self._loop,
            name=f"lease-keeper-{label}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Stop both threads. Idempotent; leases stay tracked."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            self._thread = None
        retire_thread = self._retire_thread
        if retire_thread is not None:
            self._lost_queue.put(None)
            retire_thread.join(timeout=timeout)
            self._retire_thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self._renew_interval_seconds):
            try:
                lost = self.renew_once(notify=False)
                if lost:
                    self._lost_queue.put(lost)
            except Exception:
                log.warning("workstream_lease.keeper_tick_failed", exc_info=True)

    def _retire_loop(self, lost_queue: queue.SimpleQueue[list[WorkstreamLease] | None]) -> None:
        while (batch := lost_queue.get()) is not None:
            stopping = False
            while not stopping:
                try:
                    more = lost_queue.get_nowait()
                except queue.Empty:
                    break
                if more is None:
                    stopping = True
                else:
                    batch.extend(more)
            self._notify_lost(batch)
            if stopping:
                return

    def _notify_lost(self, leases: list[WorkstreamLease]) -> None:
        try:
            self._on_lost(leases)
        except Exception:
            log.warning(
                "workstream_lease.retire_failed ws=%s",
                ",".join(lease.ws_id[:8] for lease in leases),
                exc_info=True,
            )

    def renew_once(self, *, notify: bool = True) -> list[WorkstreamLease]:
        """Renew every held lease once; return the leases newly found lost.

        With ``notify`` (the default) the losses are retired before returning;
        the renewal thread passes ``False`` and queues them instead.
        """
        snapshot = self.tracked()
        lost = [lease for lease in snapshot if lease.state == "lost"]
        live = [lease for lease in snapshot if lease.held]
        if live:
            try:
                renewed = self._storage.renew_workstream_leases(
                    self._holder,
                    [lease.fence for lease in live],
                    ttl_seconds=self._ttl_seconds,
                )
            except Exception:
                if not self._renew_failing:
                    self._renew_failing = True
                    log.warning(
                        "workstream_lease.renew_failing holder=%s leases=%d",
                        self._holder,
                        len(live),
                        exc_info=True,
                    )
                record_lease_event("renew_failed")
            else:
                if self._renew_failing:
                    self._renew_failing = False
                    log.info("workstream_lease.renew_restored holder=%s", self._holder)
                record_lease_event("renewed", len(renewed))
                for lease in live:
                    if lease.ws_id not in renewed and lease.mark_lost():
                        lost.append(lease)
        for lease in lost:
            self.untrack(lease)
            log.warning(
                "workstream_lease.lost ws=%s holder=%s epoch=%d",
                lease.ws_id[:8],
                lease.holder,
                lease.epoch,
            )
            record_lease_event("lost")
        if notify and lost:
            self._notify_lost(lost)
        return lost
