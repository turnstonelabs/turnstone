"""Buffered workstream-state persistence.

``SessionManager.set_state`` previously held ``ws._lock`` across a
synchronous Postgres ``UPDATE`` for every ``thinking → running → idle
→ attention`` transition — multiple writes per turn, with
per-workstream observers serialising behind each round-trip. This
module replaces that with a write-behind buffer:

* Non-terminal transitions buffer in a per-ws_id dict (last state wins
  per ws_id — coalesced).
* A daemon flusher drains the buffer to ``storage.update_workstream_state``
  every ``flush_interval`` seconds (default 1.0s; loop wakes early on
  ``record``).
* Terminal transitions (``ERROR``) and ``close()`` bypass the buffer
  via ``record(..., flush_now=True)`` / ``discard(ws_id)`` — those
  paths must be durable before observers see the transition.
* Bounded buffer (``max_buffer``): when full, the oldest ws_id's
  pending state is evicted on insertion of a new ws_id. All entries
  are non-terminal (terminals bypass), so eviction is safe.

**Close-vs-buffered-transient invariant**. A closed ws row must never
be resurrected by a late-flushing buffered transient writing 'running'
AFTER ``close()``'s sync 'closed' write. The flow that preserves it:

1. ``close()`` briefly acquires ``ws._lock`` and sets ``ws._closed = True``.
2. On the manager's per-id state-tail lane, ``close()`` calls
   :meth:`StateWriter.discard` to drop any pending buffered transition for
   the exact incarnation AND wait for any in-progress flush to complete.
3. ``close()`` writes ``state='closed'`` synchronously to storage.
4. Any later ``set_state`` for this ws_id sees ``ws._closed=True``
   under ``ws._lock`` and short-circuits — never reaches
   :meth:`StateWriter.record`.

**Terminal-bypass invariant**. The same hazard exists for the
``flush_now=True`` path: an earlier buffered 'running' for the same
ws_id could flush AFTER the sync 'error' write and clobber it. Same
fix — ``record(flush_now=True)`` discards any pending entry and waits
on the flush_lock before the sync write.
"""

from __future__ import annotations

import contextlib
import threading
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

log = get_logger(__name__)


class StateWriter:
    """Buffered ``update_workstream_state`` writer.

    Construct once per process; pass to :class:`SessionManager`.
    Lifecycle managed by the host's ASGI lifespan: call
    :meth:`start` on startup, :meth:`shutdown` on teardown.
    """

    def __init__(
        self,
        storage: Any,
        *,
        flush_interval: float = 1.0,
        max_buffer: int = 10_000,
        on_flush_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self._storage = storage
        self._flush_interval = flush_interval
        self._max_buffer = max_buffer
        self._on_flush_error = on_flush_error
        # ws_id → (manager incarnation, state.value). Python dict preserves
        # insertion order, so iterating the buffer yields oldest-first for
        # FIFO eviction.  Incarnations prevent an old deferred tail from
        # writing after the same logical id has been reopened.
        self._buffer: dict[str, tuple[int | None, str]] = {}
        self._incarnations: dict[str, int] = {}
        # Workstream ids whose CURRENT incarnation is closed.  ``reopen``
        # clears the id but installs a fresh token; explicit old-token records
        # remain rejected even after that clear.
        self._closed_ids: set[str] = set()
        self._lock = threading.Lock()
        # Held by the flusher while it's iterating + writing the
        # snapshotted batch. ``discard`` waits on it so close() can
        # ensure no stray write follows its sync ``state='closed'``.
        self._flush_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(
        self,
        ws_id: str,
        state: str,
        *,
        flush_now: bool = False,
        incarnation: int | None = None,
    ) -> None:
        """Buffer (or sync-write) a state transition.

        ``flush_now=True`` writes synchronously and bypasses the
        buffer — used for ERROR transitions where durability matters
        before any observer sees the state. Errors are logged and
        swallowed to match the prior ``set_state`` behaviour (which
        wrapped its DB call in a try/except for the same reason).

        The terminal-bypass invariant requires the same
        drop-pending + wait-for-in-flight-flush dance that
        :meth:`discard` performs: an earlier buffered 'running' for
        the same ws_id must not flush AFTER the sync write and
        clobber the terminal state. We pop under ``self._lock`` and
        wait on ``self._flush_lock`` before the sync UPDATE so the
        terminal state is the final write for this ws_id.
        """
        if flush_now:
            # Every buffer snapshot and terminal write takes flush_lock first.
            # Whichever wins has a total order: a prior transient lands before
            # ERROR, or ERROR removes it before the flusher can snapshot it.
            try:
                with self._flush_lock:
                    with self._lock:
                        if not self._accepts_locked(ws_id, incarnation):
                            return
                        pending = self._buffer.get(ws_id)
                        if pending is not None and (
                            incarnation is None or pending[0] == incarnation
                        ):
                            self._buffer.pop(ws_id, None)
                    self._storage.update_workstream_state(ws_id, state)
            except Exception as exc:
                log.debug(
                    "state_writer.flush_now_failed ws=%s",
                    ws_id[:8],
                    exc_info=True,
                )
                self._notify_error(exc)
            return
        with self._lock:
            if not self._accepts_locked(ws_id, incarnation):
                return
            # Bounded buffer. If a new ws_id arrives at capacity, drop
            # the oldest pending entry. Updates to an existing key
            # don't grow the buffer.
            if ws_id not in self._buffer and len(self._buffer) >= self._max_buffer:
                evict_id = next(iter(self._buffer))
                self._buffer.pop(evict_id)
                log.warning(
                    "state_writer.buffer_full evicted=%s — DB unreachable?",
                    evict_id[:8],
                )
            effective_incarnation = (
                incarnation if incarnation is not None else self._incarnations.get(ws_id)
            )
            self._buffer[ws_id] = (effective_incarnation, state)
        # Wake the flusher so a single transition gets persisted within
        # ~one round-trip rather than waiting up to flush_interval.
        # Coalescing across bursts still happens because the flusher
        # snapshots the buffer atomically.
        self._wake.set()

    def reopen(self, ws_id: str, *, incarnation: int | None = None) -> int:
        """Install the token for the new live owner of ``ws_id``.

        Legacy callers may omit the token; a fresh local token is allocated.
        Production managers always pass their exact workstream incarnation so
        delayed closures can be rejected after an ABA close/reopen.
        """
        with self._lock:
            if incarnation is None:
                incarnation = self._incarnations.get(ws_id, 0) + 1
            self._incarnations[ws_id] = incarnation
            self._closed_ids.discard(ws_id)
            # A reopen is a new lifetime.  Any unflushed predecessor entry is
            # stale even if close's discard raced and has not run yet.
            self._buffer.pop(ws_id, None)
            return incarnation

    def discard(
        self,
        ws_id: str,
        *,
        flush_lock_timeout: float = 5.0,
        tombstone: bool = False,
        incarnation: int | None = None,
    ) -> bool:
        """Drop any pending buffered state for ``ws_id`` and wait for any
        in-progress flush to complete.

        Called by ``SessionManager.close`` (and ``close_idle``) on the
        per-id state-tail lane, after the brief ``ws._closed=True`` mutation
        and before the synchronous ``state='closed'`` write. No lifecycle or
        workstream lock is held while this method waits.

        ``flush_lock_timeout`` bounds the wait on the in-flight flush.
        Without a timeout a stuck Postgres connection (network
        partition, table-lock contention) would block the discard
        forever and stall terminal cleanup on a failed storage connection.
        Defaults to 5s. On timeout we proceed and log; the worst
        outcome is "buffered transient flushes shortly after the
        sync 'closed' write" — eventual consistency degrades but the
        process keeps moving.
        """
        with self._lock:
            self._discard_locked(
                ws_id,
                tombstone=tombstone,
                incarnation=incarnation,
            )
        # If a flusher is currently writing, wait for it to finish.
        # The flusher snapshots the buffer under self._lock then writes
        # under self._flush_lock, so any write of ``ws_id`` already
        # in-flight will complete before this returns.
        if not self._flush_lock.acquire(timeout=flush_lock_timeout):
            log.warning(
                "state_writer.discard_flush_lock_timeout ws=%s — proceeding without wait",
                ws_id[:8],
            )
            return False
        try:
            # Repeat after the wait.  A record/reopen may have landed while
            # this caller waited; exact-token matching prevents an old close
            # from deleting or tombstoning the replacement's buffered state.
            with self._lock:
                self._discard_locked(
                    ws_id,
                    tombstone=tombstone,
                    incarnation=incarnation,
                )
        finally:
            self._flush_lock.release()
        return True

    def start(self) -> None:
        """Start the background flusher thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="state-writer-flush",
            daemon=True,
        )
        self._thread.start()

    def shutdown(self, *, timeout: float = 5.0) -> None:
        """Stop the flusher and drain any pending writes synchronously.

        Idempotent — safe to call multiple times. Best-effort drain
        even if the flusher thread doesn't exit cleanly.
        """
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        # Final synchronous drain. The flusher may have exited mid-loop
        # without picking up the last record(s); make sure they land.
        self.flush()

    def flush(self) -> None:
        """Synchronously drain one batch of buffered state writes.

        Public wrapper over the internal flush step so callers and
        tests don't reach into ``_flush_once``. The flusher loop and
        :meth:`shutdown` use it; tests use it as a deterministic
        drain instead of waiting on ``flush_interval``.
        """
        self._flush_once()

    # ------------------------------------------------------------------
    # Flusher internals
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=self._flush_interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            self._flush_once()

    def _flush_once(self) -> None:
        with self._flush_lock:
            with self._lock:
                if not self._buffer:
                    return
                pending = self._buffer
                self._buffer = {}
            for ws_id, (incarnation, state) in pending.items():
                with self._lock:
                    if not self._accepts_locked(ws_id, incarnation):
                        continue
                try:
                    self._storage.update_workstream_state(ws_id, state)
                except Exception as exc:
                    log.debug(
                        "state_writer.flush_failed ws=%s",
                        ws_id[:8],
                        exc_info=True,
                    )
                    self._notify_error(exc)

    def _accepts_locked(self, ws_id: str, incarnation: int | None) -> bool:
        """Whether one write still belongs to the current live lifetime."""
        if ws_id in self._closed_ids:
            return False
        if incarnation is None:
            return True
        return self._incarnations.get(ws_id) == incarnation

    def _discard_locked(
        self,
        ws_id: str,
        *,
        tombstone: bool,
        incarnation: int | None,
    ) -> None:
        current = self._incarnations.get(ws_id)
        targets_current = incarnation is None or current == incarnation
        if tombstone and targets_current:
            self._closed_ids.add(ws_id)
        pending = self._buffer.get(ws_id)
        if pending is not None and (incarnation is None or pending[0] == incarnation):
            self._buffer.pop(ws_id, None)

    def _notify_error(self, exc: Exception) -> None:
        if self._on_flush_error is None:
            return
        with contextlib.suppress(Exception):
            self._on_flush_error(exc)
