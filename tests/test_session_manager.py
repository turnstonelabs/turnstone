"""Smoke tests for the unified SessionManager.

Cover the concurrency-sensitive paths that historically diverged
between ``WorkstreamManager`` and ``CoordinatorManager`` — slot
reservation, eviction, per-ws lock serialization on lazy rehydrate,
state flips, close-unblocks-UI. All exercised through a
``FakeAdapter`` that records ``emit_*`` / ``cleanup_ui`` calls so
tests can assert the transport contract without spinning up real
WebUI / ClusterCollector pipelines. The adapter is wired as both the
manager's ``adapter`` (for ``cleanup_ui`` / ``build_*``) and its
``event_emitter`` (for the ``emit_*`` lifecycle calls); production
adapters do the same — interactive on ``server.py``, coordinator on
``console/server.py``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from turnstone.core.session_manager import SessionKindAdapter, SessionManager
from turnstone.core.workstream import Workstream, WorkstreamKind, WorkstreamState

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@dataclass
class _Event:
    kind: str  # "created" | "rehydrated" | "state" | "closed"
    ws_id: str
    state: WorkstreamState | None = None
    reason: str | None = None
    name: str | None = None


class FakeUI:
    """Minimal UI stand-in with the events close() needs to unblock."""

    def __init__(self) -> None:
        self.approval_unblocked = False
        self.plan_unblocked = False
        self.fg_unblocked = False
        self.closed_broadcast = False

    def _unblock(self) -> None:
        self.approval_unblocked = True
        self.plan_unblocked = True
        self.fg_unblocked = True

    def broadcast_ws_closed(self) -> None:
        self.closed_broadcast = True


class FakeSession:
    """Minimal ChatSession stand-in; exposes cancel / close / resume."""

    def __init__(self, ws_id: str) -> None:
        self.ws_id = ws_id
        self.cancelled = False
        self.closed = False
        self.resumed = False

    def cancel(self) -> None:
        self.cancelled = True

    def close(self) -> None:
        self.closed = True

    def resume(self, ws_id: str) -> None:
        self.resumed = True


class FakeAdapter:
    """Records events + builds FakeUI / FakeSession for tests."""

    def __init__(
        self,
        kind: WorkstreamKind = WorkstreamKind.INTERACTIVE,
        *,
        build_session_raises: bool = False,
    ) -> None:
        self.kind = kind
        self.events: list[_Event] = []
        self._events_lock = threading.Lock()
        self.cleaned_up: list[str] = []
        self.build_session_calls = 0
        self.build_session_raises = build_session_raises
        # Slow down session build so concurrent tests can race.
        self.build_session_delay = 0.0

    def emit_created(self, ws: Workstream) -> None:
        with self._events_lock:
            self.events.append(_Event("created", ws.id))

    def emit_rehydrated(self, ws: Workstream) -> None:
        # Distinct from "created" so a regression where the manager
        # fires emit_created on the rehydrate path (or emit_rehydrated
        # on the create path) actually fails a test. The
        # SessionEventEmitter Protocol carves these out as semantically
        # different — coord uses emit_rehydrated for the storage-seeded
        # subtree rebuild that emit_created skips.
        with self._events_lock:
            self.events.append(_Event("rehydrated", ws.id))

    def emit_state(self, ws: Workstream, state: WorkstreamState) -> None:
        with self._events_lock:
            self.events.append(_Event("state", ws.id, state=state))

    def emit_closed(
        self,
        ws_id: str,
        *,
        reason: str = "closed",
        name: str = "",
    ) -> None:
        with self._events_lock:
            self.events.append(_Event("closed", ws_id, reason=reason, name=name))

    def cleanup_ui(self, ws: Workstream) -> None:
        self.cleaned_up.append(ws.id)
        if ws.session is not None:
            ws.session.cancel()
            ws.session.close()
        if ws.ui is not None:
            ws.ui._unblock()
            ws.ui.broadcast_ws_closed()

    def build_ui(self, ws: Workstream) -> Any:
        return FakeUI()

    def build_session(self, ws: Workstream, **_: object) -> Any:
        self.build_session_calls += 1
        if self.build_session_delay:
            time.sleep(self.build_session_delay)
        if self.build_session_raises:
            raise RuntimeError("build_session forced failure")
        return FakeSession(ws.id)

    def events_of(self, kind: str) -> list[_Event]:
        with self._events_lock:
            return [e for e in self.events if e.kind == kind]


@dataclass
class _Row:
    ws_id: str
    user_id: str
    name: str
    kind: str
    state: str = "idle"
    parent_ws_id: str | None = None


class FakeStorage:
    """In-memory storage that mirrors the StorageBackend surface the manager uses."""

    def __init__(self) -> None:
        self.rows: dict[str, _Row] = {}
        self.state_updates: list[tuple[str, str]] = []
        self.register_raises = False
        self.lock = threading.Lock()

    def register_workstream(
        self,
        ws_id: str,
        *,
        node_id: str | None = None,
        user_id: str | None = None,
        name: str = "",
        kind: WorkstreamKind | str = WorkstreamKind.INTERACTIVE,
        parent_ws_id: str | None = None,
        skill_id: str = "",
        skill_version: int = 0,
    ) -> None:
        if self.register_raises:
            raise RuntimeError("register forced failure")
        kind_str = kind.value if isinstance(kind, WorkstreamKind) else str(kind)
        with self.lock:
            self.rows[ws_id] = _Row(
                ws_id=ws_id,
                user_id=user_id or "",
                name=name,
                kind=kind_str,
                parent_ws_id=parent_ws_id,
            )

    def update_workstream_state(self, ws_id: str, state: str) -> None:
        with self.lock:
            self.state_updates.append((ws_id, state))
            if ws_id in self.rows:
                self.rows[ws_id].state = state

    def get_workstream(self, ws_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.rows.get(ws_id)
            if row is None:
                return None
            return {
                "ws_id": row.ws_id,
                "user_id": row.user_id,
                "name": row.name,
                "kind": row.kind,
                "state": row.state,
                "parent_ws_id": row.parent_ws_id,
            }

    def delete_workstream(self, ws_id: str) -> None:
        with self.lock:
            self.rows.pop(ws_id, None)

    def count_skill_versions(self, template_id: str) -> int:
        return 0


def _make_manager(
    adapter: FakeAdapter | None = None,
    *,
    max_active: int = 5,
    storage: FakeStorage | None = None,
) -> tuple[SessionManager, FakeAdapter, FakeStorage]:
    adapter = adapter or FakeAdapter()
    storage = storage or FakeStorage()
    # FakeAdapter implements both Protocols (the production adapters
    # do too — wire as both so the emit_* assertions in this file
    # still see the events the manager fires).
    mgr = SessionManager(
        adapter,
        storage=storage,
        max_active=max_active,
        event_emitter=adapter,
    )
    return mgr, adapter, storage


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_session_manager_constructs_with_adapter() -> None:
    mgr, adapter, _ = _make_manager()
    assert mgr.max_active == 5
    assert mgr.kind == adapter.kind


def test_session_manager_rejects_invalid_max_active() -> None:
    with pytest.raises(ValueError, match="max_active must be >= 1"):
        SessionManager(FakeAdapter(), storage=FakeStorage(), max_active=0)


def test_noop_adapter_satisfies_protocol() -> None:
    adapter: SessionKindAdapter = FakeAdapter()
    assert adapter.kind == WorkstreamKind.INTERACTIVE


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_persists_and_emits_created() -> None:
    mgr, adapter, storage = _make_manager()
    ws = mgr.create(user_id="u1", name="hello")
    assert ws.user_id == "u1"
    assert ws.name == "hello"
    assert ws.session is not None
    assert ws.ui is not None
    assert ws.id in storage.rows
    assert storage.rows[ws.id].kind == WorkstreamKind.INTERACTIVE.value
    assert [e.ws_id for e in adapter.events_of("created")] == [ws.id]


def test_create_evicts_oldest_idle_at_capacity() -> None:
    mgr, adapter, _ = _make_manager(max_active=2)
    first = mgr.create(user_id="u1")
    # Nudge the timestamp so 'first' is the clear eviction candidate.
    first.last_active = time.monotonic() - 100
    mgr.create(user_id="u1")
    # Third create triggers eviction of the oldest IDLE (= first).
    third = mgr.create(user_id="u1")
    assert mgr.get(first.id) is None
    assert mgr.get(third.id) is not None
    # Adapter transport saw the eviction and the new create.
    assert first.id in [e.ws_id for e in adapter.events_of("closed")]
    assert third.id in [e.ws_id for e in adapter.events_of("created")]
    assert first.id in adapter.cleaned_up


def test_create_raises_when_all_active_and_no_idle() -> None:
    mgr, _, _ = _make_manager(max_active=1)
    ws = mgr.create(user_id="u1")
    ws.state = WorkstreamState.RUNNING  # block eviction
    with pytest.raises(RuntimeError, match="slots are active"):
        mgr.create(user_id="u1")


def test_create_rolls_back_slot_on_session_failure() -> None:
    adapter = FakeAdapter(build_session_raises=True)
    mgr, _, storage = _make_manager(adapter=adapter)
    with pytest.raises(RuntimeError, match="build_session forced failure"):
        mgr.create(user_id="u1")
    # Slot freed — no dangling capacity consumption.  The storage row
    # survives construction failure on purpose: the next ``open(ws_id)``
    # retries build_session rather than forcing the user to create a
    # brand-new workstream.
    assert mgr.count == 0
    assert len(storage.rows) == 1


def test_create_rolls_back_slot_on_persist_failure() -> None:
    storage = FakeStorage()
    storage.register_raises = True
    mgr, _, _ = _make_manager(storage=storage)
    with pytest.raises(RuntimeError, match="register forced failure"):
        mgr.create(user_id="u1")
    assert mgr.count == 0


def test_concurrent_create_does_not_exceed_max_active() -> None:
    mgr, _, _ = _make_manager(max_active=3)
    adapter = mgr._adapter  # type: ignore[attr-defined]
    assert isinstance(adapter, FakeAdapter)
    adapter.build_session_delay = 0.02  # widen the race window

    results: list[Workstream | Exception] = []
    lock = threading.Lock()

    def _create() -> None:
        try:
            ws = mgr.create(user_id="u1")
            with lock:
                results.append(ws)
        except Exception as e:
            with lock:
                results.append(e)

    threads = [threading.Thread(target=_create) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Manager holds at most max_active; overflow creates must raise
    # (5 overflows raise, 3 succeed — no silent exceed).
    assert mgr.count <= 3
    successes = [r for r in results if isinstance(r, Workstream)]
    assert len(successes) <= 3


# ---------------------------------------------------------------------------
# open — lazy rehydrate
# ---------------------------------------------------------------------------


def test_open_returns_none_for_missing_row() -> None:
    mgr, _, _ = _make_manager()
    opened = mgr.open("missing")
    assert opened is None


def test_open_blocks_deleted_state() -> None:
    mgr, _, storage = _make_manager()
    ws = mgr.create(user_id="u1")
    mgr.close(ws.id)
    # Flip the row to the tombstone state — open must refuse it.
    storage.rows[ws.id].state = "deleted"
    opened = mgr.open(ws.id)
    assert opened is None


def test_open_resurrects_closed_state() -> None:
    mgr, adapter, _ = _make_manager()
    ws = mgr.create(user_id="u1")
    ws_id = ws.id
    mgr.close(ws_id)
    assert mgr.get(ws_id) is None

    reopened = mgr.open(ws_id)
    assert reopened is not None
    assert reopened.id == ws_id
    assert reopened.session is not None
    assert reopened.session.resumed is True  # type: ignore[attr-defined]
    # Open fires emit_rehydrated (NOT emit_created) so observers can
    # gate any extra resurrect-only setup on it (e.g. coord's
    # storage-seeded children rebuild).
    assert ws_id in [e.ws_id for e in adapter.events_of("rehydrated")]


def test_open_ignores_owner_mismatch() -> None:
    # Turnstone is a trusted-team tool; row-level ownership is
    # metadata, not an access boundary.  ``open`` no longer cares
    # who the caller is — any authenticated caller can rehydrate
    # any persisted workstream.  Scope-level auth at the HTTP
    # layer is the only gate.
    mgr, _, _ = _make_manager()
    ws = mgr.create(user_id="u1")
    mgr.close(ws.id)
    reopened = mgr.open(ws.id)
    assert reopened is not None
    assert reopened.user_id == "u1"  # metadata preserved


def test_open_rejects_wrong_kind() -> None:
    mgr, _, storage = _make_manager()
    ws = mgr.create(user_id="u1")
    # Storage row claims a different kind than our adapter's.
    storage.rows[ws.id].kind = WorkstreamKind.COORDINATOR.value
    mgr.close(ws.id)
    opened = mgr.open(ws.id)
    assert opened is None


def test_concurrent_open_for_same_ws_id_returns_same_session() -> None:
    mgr, adapter, _ = _make_manager()
    ws = mgr.create(user_id="u1")
    ws_id = ws.id
    mgr.close(ws_id)
    adapter.build_session_calls = 0
    adapter.build_session_delay = 0.02

    results: list[Workstream | None] = []
    lock = threading.Lock()

    def _open() -> None:
        r = mgr.open(ws_id)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=_open) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    loaded = [r for r in results if r is not None]
    assert len(loaded) == 6
    # All threads got the same Workstream instance — no duplicate session.
    sessions = {id(r.session) for r in loaded}
    assert len(sessions) == 1
    # build_session ran exactly once — the per-ws lock serialized the rest.
    assert adapter.build_session_calls == 1


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


def test_close_unblocks_ui_and_emits_closed() -> None:
    mgr, adapter, storage = _make_manager()
    ws = mgr.create(user_id="u1")
    ws_id = ws.id

    closed = mgr.close(ws_id)
    assert closed is True
    assert mgr.get(ws_id) is None
    assert ws_id in adapter.cleaned_up
    assert ws_id in [e.ws_id for e in adapter.events_of("closed")]
    # Storage reflects the close.
    assert (ws_id, "closed") in storage.state_updates
    # UI events unblocked.
    assert ws.ui.approval_unblocked is True  # type: ignore[attr-defined]
    assert ws.ui.plan_unblocked is True  # type: ignore[attr-defined]
    assert ws.ui.closed_broadcast is True  # type: ignore[attr-defined]
    # Session cancelled + closed.
    assert ws.session.cancelled is True  # type: ignore[attr-defined]
    assert ws.session.closed is True  # type: ignore[attr-defined]


def test_close_last_workstream_succeeds() -> None:
    """The WSM 'refuse to close last workstream' guard is gone (#400 follow-up).

    The default startup workstream relic is deleted; the dashboard
    handles the 0-workstream state and callers can close freely.
    """
    mgr, _, _ = _make_manager()
    ws = mgr.create(user_id="u1")
    closed = mgr.close(ws.id)
    assert closed is True
    assert mgr.count == 0


def test_close_unknown_returns_false() -> None:
    mgr, _, _ = _make_manager()
    closed = mgr.close("not-there")
    assert closed is False


# ---------------------------------------------------------------------------
# set_state
# ---------------------------------------------------------------------------


def test_set_state_updates_storage_and_fires_observer() -> None:
    mgr, adapter, storage = _make_manager()
    ws = mgr.create(user_id="u1")
    mgr.set_state(ws.id, WorkstreamState.RUNNING)
    assert ws.state == WorkstreamState.RUNNING
    assert (ws.id, WorkstreamState.RUNNING.value) in storage.state_updates
    state_events = adapter.events_of("state")
    assert any(e.ws_id == ws.id and e.state == WorkstreamState.RUNNING for e in state_events)


def test_set_state_unknown_ws_is_noop() -> None:
    mgr, adapter, _ = _make_manager()
    mgr.set_state("ghost", WorkstreamState.RUNNING)
    assert adapter.events_of("state") == []


# ---------------------------------------------------------------------------
# close_idle / list_all / get / count
# ---------------------------------------------------------------------------


def test_close_idle_closes_old_idle_and_keeps_active() -> None:
    mgr, _, _ = _make_manager()
    old = mgr.create(user_id="u1")
    fresh = mgr.create(user_id="u1")
    running = mgr.create(user_id="u1")
    old.last_active = time.monotonic() - 100
    running.state = WorkstreamState.RUNNING
    running.last_active = time.monotonic() - 100

    closed = mgr.close_idle(max_age_seconds=10.0)
    assert old.id in closed
    assert mgr.get(old.id) is None
    # Fresh stays (not old enough).
    assert mgr.get(fresh.id) is not None
    # Running stays (wrong state).
    assert mgr.get(running.id) is not None


def test_close_idle_on_empty_manager_returns_empty_list() -> None:
    mgr, _, _ = _make_manager()
    assert mgr.close_idle(max_age_seconds=1.0) == []


def test_list_all_returns_creation_order() -> None:
    mgr, _, _ = _make_manager()
    a = mgr.create(user_id="u1")
    b = mgr.create(user_id="u1")
    c = mgr.create(user_id="u1")
    assert [ws.id for ws in mgr.list_all()] == [a.id, b.id, c.id]


def test_count_reflects_live_workstreams() -> None:
    mgr, _, _ = _make_manager()
    assert mgr.count == 0
    a = mgr.create(user_id="u1")
    assert mgr.count == 1
    mgr.create(user_id="u1")
    assert mgr.count == 2
    mgr.close(a.id)
    assert mgr.count == 1


# ---------------------------------------------------------------------------
# Eviction transport — adapter contract
# ---------------------------------------------------------------------------


def test_eviction_fires_emit_closed_to_adapter_transport() -> None:
    mgr, adapter, _ = _make_manager(max_active=2)
    a = mgr.create(user_id="u1")
    a.last_active = time.monotonic() - 100
    mgr.create(user_id="u1")
    mgr.create(user_id="u1")  # triggers eviction of 'a'
    closed_events = adapter.events_of("closed")
    evicted = [e for e in closed_events if e.ws_id == a.id]
    assert evicted and evicted[0].reason == "evicted"
    assert a.id in adapter.cleaned_up


def test_manual_close_uses_closed_reason() -> None:
    mgr, adapter, _ = _make_manager()
    ws = mgr.create(user_id="u1")
    mgr.close(ws.id)
    closed_events = [e for e in adapter.events_of("closed") if e.ws_id == ws.id]
    assert closed_events and closed_events[0].reason == "closed"


# ---------------------------------------------------------------------------
# CLI focus state
# ---------------------------------------------------------------------------


def test_active_id_seeded_on_first_create() -> None:
    mgr, _, _ = _make_manager()
    assert mgr.active_id is None
    ws = mgr.create(user_id="u1")
    assert mgr.active_id == ws.id
    assert mgr.get_active() is ws


def test_active_id_unchanged_on_subsequent_creates() -> None:
    mgr, _, _ = _make_manager()
    first = mgr.create(user_id="u1")
    mgr.create(user_id="u1")
    # Creating a second workstream doesn't change focus.
    assert mgr.active_id == first.id


def test_switch_moves_active_id() -> None:
    mgr, _, _ = _make_manager()
    a = mgr.create(user_id="u1")
    b = mgr.create(user_id="u1")
    assert mgr.active_id == a.id
    result = mgr.switch(b.id)
    assert result is b
    assert mgr.active_id == b.id
    # Unknown id → no change.
    assert mgr.switch("ghost") is None
    assert mgr.active_id == b.id


def test_switch_by_index_uses_1_based_ordering() -> None:
    mgr, _, _ = _make_manager()
    a = mgr.create(user_id="u1")
    b = mgr.create(user_id="u1")
    c = mgr.create(user_id="u1")
    assert mgr.switch_by_index(2) is b
    assert mgr.active_id == b.id
    assert mgr.switch_by_index(3) is c
    assert mgr.switch_by_index(0) is None
    assert mgr.switch_by_index(99) is None
    # Still on c after the invalid switches.
    assert mgr.active_id == c.id
    assert mgr.index_of(a.id) == 1
    assert mgr.index_of("ghost") == 0


def test_active_id_moves_on_eviction() -> None:
    mgr, _, _ = _make_manager(max_active=2)
    a = mgr.create(user_id="u1")
    a.last_active = time.monotonic() - 100
    mgr.create(user_id="u1")
    # First create seeded active to a; eviction of a must re-home active.
    assert mgr.active_id == a.id
    c = mgr.create(user_id="u1")  # evicts a
    assert mgr.active_id != a.id
    assert mgr.active_id in (mgr._order[0], c.id)  # type: ignore[attr-defined]


def test_active_id_moves_on_close() -> None:
    mgr, _, _ = _make_manager()
    a = mgr.create(user_id="u1")
    b = mgr.create(user_id="u1")
    mgr.switch(a.id)
    mgr.close(a.id)
    assert mgr.active_id == b.id


def test_eviction_count_tracks_evictions() -> None:
    mgr, _, _ = _make_manager(max_active=2)
    assert mgr.eviction_count == 0
    a = mgr.create(user_id="u1")
    a.last_active = time.monotonic() - 100
    mgr.create(user_id="u1")
    mgr.create(user_id="u1")  # evicts a
    assert mgr.eviction_count == 1


# ---------------------------------------------------------------------------
# Storage access patterns with mocks (defensive coverage)
# ---------------------------------------------------------------------------


def test_create_uses_configured_node_id() -> None:
    storage = MagicMock()
    adapter = FakeAdapter()
    mgr = SessionManager(
        adapter, storage=storage, max_active=3, node_id="node-xyz", event_emitter=adapter
    )
    mgr.create(user_id="u1")
    assert storage.register_workstream.call_args.kwargs["node_id"] == "node-xyz"


# ---------------------------------------------------------------------------
# StateWriter integration — bug-3 invariant under write-behind
# ---------------------------------------------------------------------------


class TestSessionManagerWithStateWriter:
    """When a buffered StateWriter is wired in, ``set_state`` no longer
    blocks ``ws._lock`` on a sync DB write — but ``close`` must still
    leave 'closed' as the durable final state for the row, never
    a buffered transient that flushes after close."""

    def _make_with_writer(
        self, *, flush_interval: float = 0.05
    ) -> tuple[SessionManager, FakeStorage, Any]:
        from turnstone.core.state_writer import StateWriter

        storage = FakeStorage()
        writer = StateWriter(storage, flush_interval=flush_interval)
        adapter = FakeAdapter()
        mgr = SessionManager(
            adapter,
            storage=storage,
            max_active=3,
            state_writer=writer,
            event_emitter=adapter,
        )
        return mgr, storage, writer

    def test_set_state_buffers_through_writer(self) -> None:
        mgr, storage, writer = self._make_with_writer(flush_interval=60.0)
        ws = mgr.create(user_id="u1")
        # Initial register write may have landed; clear and inspect afterwards.
        storage.state_updates.clear()

        mgr.set_state(ws.id, WorkstreamState.RUNNING)
        # Long flush_interval → buffered, no sync write yet.
        assert storage.state_updates == []
        # Drain the buffer manually (test stand-in for the periodic flush).
        writer.flush()
        assert (ws.id, "running") in storage.state_updates

    def test_set_state_error_flushes_sync(self) -> None:
        """Terminal ERROR transitions must be durable on return — error
        surfacing paths (audit, dashboard) need the row to reflect
        the failure before any observer sees it."""
        mgr, storage, _writer = self._make_with_writer(flush_interval=60.0)
        ws = mgr.create(user_id="u1")
        storage.state_updates.clear()

        mgr.set_state(ws.id, WorkstreamState.ERROR, error_msg="boom")
        # No buffer-drain needed — error path bypasses.
        assert (ws.id, "error") in storage.state_updates

    def test_close_after_buffered_set_state_writes_closed_not_transient(self) -> None:
        """The bug-3 invariant under write-behind. close() must call
        state_writer.discard BEFORE its sync 'closed' write, so any
        buffered 'running' state can't be flushed AFTER 'closed'.
        """
        mgr, storage, writer = self._make_with_writer(flush_interval=60.0)
        ws = mgr.create(user_id="u1")
        storage.state_updates.clear()

        # Buffer a transient transition.
        mgr.set_state(ws.id, WorkstreamState.RUNNING)
        # Now close — must drain/discard the buffer + write 'closed' sync.
        ok = mgr.close(ws.id)
        assert ok is True
        # Force a flush; the buffered 'running' must already be gone.
        writer.flush()

        # The final write for ws.id must be 'closed', and 'running' must
        # NOT have landed in storage at all.
        ws_writes = [s for w, s in storage.state_updates if w == ws.id]
        assert "running" not in ws_writes, f"buffered running flushed after close: {ws_writes}"
        assert ws_writes[-1] == "closed"

    def test_close_idle_after_buffered_set_state_writes_closed(self) -> None:
        """Same invariant via close_idle (the idle-cleanup batch path)."""
        mgr, storage, writer = self._make_with_writer(flush_interval=60.0)
        ws = mgr.create(user_id="u1")
        storage.state_updates.clear()

        mgr.set_state(ws.id, WorkstreamState.RUNNING)
        # Force the workstream into IDLE state before close_idle considers
        # it (close_idle gates on ws.state, not the buffered state).
        with mgr._lock:
            mgr._workstreams[ws.id].state = WorkstreamState.IDLE
            mgr._workstreams[ws.id].last_active = 0.0  # ancient → stale

        closed = mgr.close_idle(max_age_seconds=0)
        assert ws.id in closed
        writer.flush()
        ws_writes = [s for w, s in storage.state_updates if w == ws.id]
        assert "running" not in ws_writes, f"buffered running flushed after close_idle: {ws_writes}"
        assert ws_writes[-1] == "closed"

    def test_set_state_after_close_short_circuits(self) -> None:
        """The existing tombstone (ws._closed) check must still fire
        before reaching state_writer.record — set_state after close
        must NOT enqueue 'running' to the buffer (which would then
        get flushed and resurrect the closed row)."""
        mgr, storage, writer = self._make_with_writer(flush_interval=60.0)
        ws = mgr.create(user_id="u1")
        ws_id = ws.id

        # Close first — sets ws._closed=True synchronously.
        mgr.close(ws_id)
        storage.state_updates.clear()

        # A late set_state call (e.g. from a worker still cleaning up).
        # Should NOT buffer 'running' for this ws.
        mgr.set_state(ws_id, WorkstreamState.RUNNING)
        writer.flush()
        ws_writes = [s for w, s in storage.state_updates if w == ws_id]
        assert "running" not in ws_writes, (
            f"set_state after close enqueued through buffer: {ws_writes}"
        )
