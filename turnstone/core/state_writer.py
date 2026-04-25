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

The bug-3 invariant the close path must keep holding (a closed ws
row can't be resurrected by a buffered transient state writing
"running" after close's sync "closed" write):

1. ``close()`` acquires ``ws._lock`` and sets ``ws._closed = True``.
2. ``close()`` calls :meth:`StateWriter.discard` to drop any pending
   buffered transition for the ws_id AND wait for any in-progress
   flush to complete (so a flusher mid-write can't sneak through
   AFTER ``close()``'s sync write).
3. ``close()`` writes ``state='closed'`` synchronously to storage.
4. Any later ``set_state`` for this ws_id sees ``ws._closed=True``
   under ``ws._lock`` and short-circuits — never reaches
   :meth:`StateWriter.record`.
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
        # ws_id → state.value. Python dict preserves insertion order, so
        # iterating the buffer yields oldest-first for FIFO eviction.
        self._buffer: dict[str, str] = {}
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

    def record(self, ws_id: str, state: str, *, flush_now: bool = False) -> None:
        """Buffer (or sync-write) a state transition.

        ``flush_now=True`` writes synchronously and bypasses the
        buffer — used for ERROR transitions where durability matters
        before any observer sees the state. Errors are logged and
        swallowed to match the prior ``set_state`` behaviour (which
        wrapped its DB call in a try/except for the same reason).
        """
        if flush_now:
            try:
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
            self._buffer[ws_id] = state
        # Wake the flusher so a single transition gets persisted within
        # ~one round-trip rather than waiting up to flush_interval.
        # Coalescing across bursts still happens because the flusher
        # snapshots the buffer atomically.
        self._wake.set()

    def discard(self, ws_id: str) -> None:
        """Drop any pending buffered state for ``ws_id`` and wait for any
        in-progress flush to complete.

        Called by ``SessionManager.close`` (and ``close_idle``) under
        ``ws._lock`` after ``ws._closed=True`` and BEFORE the sync
        ``state='closed'`` write. After this returns, no buffered or
        in-flight write for ``ws_id`` can land in storage AFTER the
        caller's sync ``closed`` write.
        """
        with self._lock:
            self._buffer.pop(ws_id, None)
        # If a flusher is currently writing, wait for it to finish.
        # The flusher snapshots the buffer under self._lock then writes
        # under self._flush_lock, so any write of ``ws_id`` already
        # in-flight will complete before this returns.
        with self._flush_lock:
            pass

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
        with self._lock:
            if not self._buffer:
                return
            pending = self._buffer
            self._buffer = {}
        with self._flush_lock:
            for ws_id, state in pending.items():
                try:
                    self._storage.update_workstream_state(ws_id, state)
                except Exception as exc:
                    log.debug(
                        "state_writer.flush_failed ws=%s",
                        ws_id[:8],
                        exc_info=True,
                    )
                    self._notify_error(exc)

    def _notify_error(self, exc: Exception) -> None:
        if self._on_flush_error is None:
            return
        with contextlib.suppress(Exception):
            self._on_flush_error(exc)
