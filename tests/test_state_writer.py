"""Unit tests for ``turnstone.core.state_writer``.

Tests cover the contract callers depend on:

* Buffered transitions coalesce per ws_id (last state wins).
* ``flush_now=True`` bypasses the buffer (used for terminal ERROR
  transitions and any other write that must be durable on return).
* ``discard`` drops pending and waits for any in-flight flush to
  complete (the bug-3 invariant — close()'s sync ``closed`` write must
  not be overtaken by a buffered transient).
* Bounded buffer evicts oldest under capacity pressure.
* DB error during flush doesn't poison the loop; subsequent flushes
  still run.
* Shutdown drains any pending entries synchronously.
"""

from __future__ import annotations

import threading
import time

from turnstone.core.state_writer import StateWriter


class _FakeStorage:
    """Records update_workstream_state calls. Optionally raises or pauses."""

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.raises = raises
        self._call_lock = threading.Lock()
        # Optional gate to pin a write inside update_workstream_state
        # so the test can race ``discard`` against an in-flight flush.
        self.write_gate: threading.Event | None = None
        # Set by the writer thread once it enters update_workstream_state.
        self.write_started = threading.Event()

    def update_workstream_state(self, ws_id: str, state: str) -> None:
        if self.write_gate is not None:
            self.write_started.set()
            self.write_gate.wait(timeout=2.0)
        with self._call_lock:
            self.calls.append((ws_id, state))
        if self.raises is not None:
            raise self.raises


def _drain(writer: StateWriter) -> None:
    """Trigger a single flush synchronously."""
    writer.flush()


# ---------------------------------------------------------------------------
# Coalescing + flush
# ---------------------------------------------------------------------------


def test_buffered_transitions_coalesce_per_ws_id() -> None:
    storage = _FakeStorage()
    writer = StateWriter(storage)

    writer.record("ws-1", "thinking")
    writer.record("ws-1", "running")
    writer.record("ws-1", "idle")
    writer.record("ws-2", "thinking")

    _drain(writer)
    # Only the latest state per ws_id should land.
    assert sorted(storage.calls) == sorted([("ws-1", "idle"), ("ws-2", "thinking")])


def test_flush_now_bypasses_buffer_and_writes_sync() -> None:
    storage = _FakeStorage()
    writer = StateWriter(storage)

    # Pre-buffer something for a different ws_id to prove the sync
    # path doesn't drain the whole buffer.
    writer.record("ws-other", "running")

    writer.record("ws-err", "error", flush_now=True)
    # ws-err landed sync, ws-other still buffered.
    assert ("ws-err", "error") in storage.calls
    assert ("ws-other", "running") not in storage.calls

    _drain(writer)
    assert ("ws-other", "running") in storage.calls


def test_flush_now_swallows_storage_error() -> None:
    storage = _FakeStorage(raises=RuntimeError("db down"))
    writer = StateWriter(storage)
    # Should not raise — set_state path can't recover from a storage
    # write failure mid-transition.
    writer.record("ws-1", "error", flush_now=True)


def test_flush_now_drops_pending_buffered_state_for_same_ws_id() -> None:
    """Terminal-bypass invariant: a buffered transient for the same
    ws_id must NOT flush AFTER the sync ``flush_now`` write and
    clobber the terminal state. (This was a real correctness gap
    flagged by /review.)"""
    storage = _FakeStorage()
    writer = StateWriter(storage)

    # Buffer a transient transition first.
    writer.record("ws-A", "running")

    # Sync ERROR write must drop the buffered 'running' AND wait on
    # the flush_lock so any in-flight flush can't sneak through after.
    writer.record("ws-A", "error", flush_now=True)

    # Run the flusher; nothing pending for ws-A any more.
    writer.flush()

    ws_writes = [s for w, s in storage.calls if w == "ws-A"]
    # The sync 'error' must be in storage, and 'running' must NOT have
    # been flushed AFTER it.
    assert "error" in ws_writes, ws_writes
    assert ws_writes[-1] == "error", f"buffered 'running' clobbered terminal 'error': {ws_writes}"
    # Stronger: the 'running' should never have landed at all.
    assert "running" not in ws_writes, ws_writes


def test_flush_now_waits_for_in_flight_flush_to_complete() -> None:
    """Same shape as the discard wait: if a flusher is mid-write on
    the same ws_id, ``flush_now`` must NOT issue its sync write
    until the flusher finishes — otherwise the order on the wire is
    flush_now → flusher's late write → final state is the transient,
    not the terminal."""
    storage = _FakeStorage()
    storage.write_gate = threading.Event()
    writer = StateWriter(storage)

    writer.record("ws-A", "running")

    flush_done = threading.Event()

    def _flush_in_bg() -> None:
        writer.flush()
        flush_done.set()

    flusher = threading.Thread(target=_flush_in_bg, daemon=True)
    flusher.start()
    assert storage.write_started.wait(timeout=1.0)

    flush_now_done = threading.Event()

    def _flush_now_in_bg() -> None:
        writer.record("ws-A", "error", flush_now=True)
        flush_now_done.set()

    fn_thread = threading.Thread(target=_flush_now_in_bg, daemon=True)
    fn_thread.start()
    time.sleep(0.05)
    assert flush_now_done.is_set() is False, (
        "flush_now returned before in-flight flush released flush_lock"
    )

    storage.write_gate.set()
    flusher.join(timeout=2.0)
    fn_thread.join(timeout=2.0)
    assert flush_done.is_set() and flush_now_done.is_set()
    # The flusher's 'running' lands first, then flush_now's 'error'.
    ws_writes = [s for w, s in storage.calls if w == "ws-A"]
    assert ws_writes == ["running", "error"], ws_writes


# ---------------------------------------------------------------------------
# Bounded buffer
# ---------------------------------------------------------------------------


def test_bounded_buffer_evicts_oldest_on_capacity() -> None:
    storage = _FakeStorage()
    writer = StateWriter(storage, max_buffer=3)

    writer.record("ws-1", "running")
    writer.record("ws-2", "running")
    writer.record("ws-3", "running")
    # ws-4 forces eviction of ws-1 (oldest).
    writer.record("ws-4", "running")

    _drain(writer)
    landed = {ws_id for ws_id, _ in storage.calls}
    assert "ws-1" not in landed
    assert {"ws-2", "ws-3", "ws-4"} <= landed


def test_bounded_buffer_update_existing_does_not_evict() -> None:
    storage = _FakeStorage()
    writer = StateWriter(storage, max_buffer=2)

    writer.record("ws-1", "running")
    writer.record("ws-2", "running")
    # Update existing — must not evict.
    writer.record("ws-1", "idle")

    _drain(writer)
    landed = dict(storage.calls)
    assert landed["ws-1"] == "idle"
    assert landed["ws-2"] == "running"


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------


def test_storage_error_does_not_poison_subsequent_flushes() -> None:
    storage = _FakeStorage(raises=RuntimeError("db blip"))
    errors: list[Exception] = []
    writer = StateWriter(storage, on_flush_error=errors.append)

    writer.record("ws-1", "running")
    _drain(writer)
    # Error was surfaced via callback.
    assert len(errors) == 1

    # Storage recovers; next flush succeeds.
    storage.raises = None
    writer.record("ws-2", "idle")
    _drain(writer)
    assert ("ws-2", "idle") in storage.calls


# ---------------------------------------------------------------------------
# discard / close-race
# ---------------------------------------------------------------------------


def test_discard_drops_pending_buffered_state() -> None:
    storage = _FakeStorage()
    writer = StateWriter(storage)

    writer.record("ws-close", "running")
    writer.discard("ws-close")
    _drain(writer)
    assert storage.calls == []


def test_discard_waits_for_in_flight_flush_to_complete() -> None:
    """The bug-3 invariant: ``close()`` calls ``discard`` BEFORE its
    sync ``state='closed'`` write. If a flusher was mid-write for the
    same ws_id, the flusher's write must complete BEFORE
    ``discard`` returns — so ``close()``'s sync write strictly
    follows the flusher's transient write, leaving 'closed' as the
    final state. (If discard returned early, close's 'closed' write
    could be overwritten by the flusher's late 'running' write.)
    """
    storage = _FakeStorage()
    storage.write_gate = threading.Event()
    writer = StateWriter(storage)

    writer.record("ws-A", "running")

    # Kick off a flush in a background thread; it will block inside
    # update_workstream_state on storage.write_gate.
    flush_done = threading.Event()

    def _flush_in_bg() -> None:
        writer.flush()
        flush_done.set()

    flusher = threading.Thread(target=_flush_in_bg, daemon=True)
    flusher.start()
    assert storage.write_started.wait(timeout=1.0)
    assert flush_done.is_set() is False  # writer is pinned

    # Call discard concurrently — it must NOT return until the flush
    # completes.
    discard_done = threading.Event()

    def _discard_in_bg() -> None:
        writer.discard("ws-A")
        discard_done.set()

    discarder = threading.Thread(target=_discard_in_bg, daemon=True)
    discarder.start()
    # discard should be blocked on flush_lock.
    time.sleep(0.05)
    assert discard_done.is_set() is False, "discard returned before flusher released the write"

    # Release the writer; both threads should complete now.
    storage.write_gate.set()
    flusher.join(timeout=2.0)
    discarder.join(timeout=2.0)
    assert flush_done.is_set()
    assert discard_done.is_set()
    # The flusher's write went through.
    assert ("ws-A", "running") in storage.calls


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_start_starts_flusher_and_buffered_writes_land() -> None:
    storage = _FakeStorage()
    writer = StateWriter(storage, flush_interval=0.05)
    writer.start()
    try:
        writer.record("ws-1", "running")
        # Wait up to 1s for the flusher to drain.
        for _ in range(20):
            if storage.calls:
                break
            time.sleep(0.05)
        assert ("ws-1", "running") in storage.calls
    finally:
        writer.shutdown(timeout=2.0)


def test_shutdown_drains_pending_synchronously() -> None:
    storage = _FakeStorage()
    # Long flush interval so no automatic drain happens.
    writer = StateWriter(storage, flush_interval=60.0)
    writer.start()
    try:
        writer.record("ws-1", "running")
        writer.record("ws-2", "thinking")
    finally:
        writer.shutdown(timeout=2.0)
    landed = {ws_id for ws_id, _ in storage.calls}
    assert {"ws-1", "ws-2"} <= landed


def test_start_is_idempotent() -> None:
    storage = _FakeStorage()
    writer = StateWriter(storage, flush_interval=0.05)
    writer.start()
    first_thread = writer._thread
    writer.start()
    assert writer._thread is first_thread
    writer.shutdown(timeout=2.0)


def test_discard_times_out_when_flush_hangs() -> None:
    """If the flusher is wedged on a stuck Postgres connection, discard
    must NOT block forever — callers hold ws._lock across this call,
    so an unbounded wait would deadlock all close paths system-wide."""
    storage = _FakeStorage()
    storage.write_gate = threading.Event()  # never released
    writer = StateWriter(storage)

    writer.record("ws-A", "running")

    # Pin the flusher inside update_workstream_state.
    flusher = threading.Thread(target=writer.flush, daemon=True)
    flusher.start()
    assert storage.write_started.wait(timeout=1.0)

    # discard must return within ~timeout, NOT hang forever.
    start = time.monotonic()
    writer.discard("ws-A", flush_lock_timeout=0.1)
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, f"discard hung: {elapsed:.2f}s"

    # Cleanup.
    storage.write_gate.set()
    flusher.join(timeout=2.0)


def test_shutdown_is_idempotent() -> None:
    storage = _FakeStorage()
    writer = StateWriter(storage, flush_interval=0.05)
    writer.start()
    writer.shutdown(timeout=2.0)
    # Second shutdown is a no-op, must not raise.
    writer.shutdown(timeout=2.0)


# ---------------------------------------------------------------------------
# Wake-on-record
# ---------------------------------------------------------------------------


def test_record_wakes_flusher_immediately() -> None:
    """Single transitions get persisted within ~one round-trip rather
    than waiting up to flush_interval seconds."""
    storage = _FakeStorage()
    # Long interval — only the wake event should drive the flush.
    writer = StateWriter(storage, flush_interval=10.0)
    writer.start()
    try:
        writer.record("ws-1", "running")
        for _ in range(30):
            if storage.calls:
                break
            time.sleep(0.02)
        assert ("ws-1", "running") in storage.calls
    finally:
        writer.shutdown(timeout=2.0)
