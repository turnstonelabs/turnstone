"""SessionManager owner-lease lifecycle against the in-memory FakeStorage.

FakeStorage models PostgreSQL by default: a live lease another holder owns
refuses acquisition. The storage-level rules themselves are covered on both
real dialects in ``test_storage_workstream_lease.py``; these tests pin where
the manager acquires, presents, renews, releases and retires leases.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from tests.test_session_manager import FakeAdapter, FakeSession, FakeStorage, _make_manager
from turnstone.core.metrics import metrics
from turnstone.core.session_manager import CloseOutcome
from turnstone.core.storage import (
    LeaseFence,
    WorkstreamLeaseHeldError,
    WorkstreamLeaseLostError,
)
from turnstone.core.workstream import Workstream, WorkstreamState
from turnstone.core.workstream_lease import LeaseKeeper, WorkstreamLease


class LeaseAwareSession(FakeSession):
    """FakeSession exposing the lease seams a ChatSession provides."""

    def __init__(self, ws_id: str, lease: WorkstreamLease | None) -> None:
        super().__init__(ws_id)
        self.workstream_lease = lease
        self.lease_lost = False
        self.lease_lost_calls = 0

    def handle_workstream_lease_lost(self, lease: WorkstreamLease | None = None) -> None:
        if self.lease_lost:
            return
        self.lease_lost = True
        self.lease_lost_calls += 1
        lost = lease if lease is not None else self.workstream_lease
        if lost is not None:
            lost.mark_lost()


def _own(ws: Workstream) -> WorkstreamLease | None:
    """The slot's lease handle."""
    with ws._lock:
        return ws._lease


def _tracked_ids(mgr: Any) -> set[str]:
    return {lease.ws_id for lease in mgr._lease_keeper.tracked()}


class LeaseAwareAdapter(FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.build_kwargs: list[dict[str, object]] = []

    def build_session(self, ws: Workstream, **kwargs: object) -> Any:
        self.build_session_calls += 1
        self.build_kwargs.append(kwargs)
        if self.build_session_raises:
            raise RuntimeError("build_session forced failure")
        lease = kwargs.get("workstream_lease")
        session = LeaseAwareSession(ws.id, lease if isinstance(lease, WorkstreamLease) else None)
        self.built_sessions.append(session)
        return session


def _manager(
    storage: FakeStorage,
    *,
    node_id: str = "node-a",
    max_active: int = 5,
) -> tuple[Any, LeaseAwareAdapter]:
    adapter = LeaseAwareAdapter()
    mgr, _, _ = _make_manager(adapter, storage=storage, node_id=node_id, max_active=max_active)
    return mgr, adapter


def _expire(storage: FakeStorage, ws_id: str) -> None:
    storage.rows[ws_id].lease_expires_at = time.time() - 1


def _lease_events() -> dict[str, int]:
    return dict(metrics._lease_events)


# -- acquisition -------------------------------------------------------------


def test_create_leases_the_reservation_before_the_session_is_built() -> None:
    storage = FakeStorage()
    mgr, adapter = _manager(storage)

    ws = mgr.create(user_id="u1")

    assert _own(ws) is not None and _own(ws).held
    row = storage.rows[ws.id]
    assert (row.lease_holder, row.lease_node_id, row.lease_epoch) == (
        mgr._lease_holder,
        "node-a",
        1,
    )
    assert _own(ws).fence == LeaseFence(
        ws.id, mgr._lease_holder, 1, storage.fork_reservations[ws.id]
    )
    assert adapter.build_kwargs[-1]["workstream_lease"] is _own(ws)
    assert mgr.lease_fence(ws.id) == _own(ws).fence
    assert mgr._lease_holder.startswith("node-a/")


@pytest.mark.parametrize("exit_path", ["close", "idle_close", "eviction"])
def test_unloading_a_copy_another_process_took_announces_no_close(exit_path: str) -> None:
    """The workstream did not close: it lives on at the new owner, so no ws_closed here."""
    storage = FakeStorage()
    mgr, adapter = _manager(storage, node_id="node-a", max_active=1)
    ws = mgr.create(user_id="u1")
    other, _ = _manager(storage, node_id="node-b")
    _expire(storage, ws.id)
    assert other.open(ws.id) is not None  # before this node's keeper noticed

    if exit_path == "close":
        assert mgr.close(ws.id)
    elif exit_path == "idle_close":
        ws.last_active = time.monotonic() - 3600
        assert mgr.close_idle(60) == [ws.id]
    else:
        mgr.create(user_id="u1")  # capacity 1 evicts the idle copy

    assert mgr.get(ws.id) is None
    assert all(event.ws_id != ws.id for event in adapter.events_of("closed"))
    assert ws.id in adapter.lease_retired
    assert storage.rows[ws.id].lease_holder == other._lease_holder


def test_closing_a_copy_another_process_took_reports_it_owned_elsewhere() -> None:
    """The close route answers it like NOT_FOUND, so the console re-routes the close."""
    storage = FakeStorage()
    mgr, _ = _manager(storage, node_id="node-a")
    ws = mgr.create(user_id="u1")
    other, _ = _manager(storage, node_id="node-b")
    _expire(storage, ws.id)
    assert other.open(ws.id) is not None

    assert mgr.close_with_outcome(ws.id) is CloseOutcome.OWNED_ELSEWHERE
    assert mgr.get(ws.id) is None
    assert storage.rows[ws.id].state != "closed"


def test_a_delete_refused_because_the_lease_moved_announces_the_unload() -> None:
    storage = FakeStorage()
    mgr, adapter = _manager(storage, node_id="node-a")
    ws = mgr.create(user_id="u1")
    other, _ = _manager(storage, node_id="node-b")
    _expire(storage, ws.id)
    assert other.open(ws.id) is not None
    token = storage.fork_reservations[ws.id]

    with pytest.raises(WorkstreamLeaseLostError):
        mgr.delete_persisted(
            ws.id,
            delete_fn=lambda *, lease: storage.delete_workstream_if_fork_reserved(
                ws.id, token, lease=lease
            ),
            expected_reservation_token=token,
        )

    assert mgr.get(ws.id) is None and ws.id in storage.rows
    assert adapter.lease_retired == [ws.id]
    assert all(event.ws_id != ws.id for event in adapter.events_of("closed"))


def test_a_lease_retirement_is_announced_inside_the_id_lane() -> None:
    """A reopen of the id waits for the old copy's announcement, as it does for a close."""
    storage = FakeStorage()
    mgr, _ = _manager(storage, node_id="node-a")
    ws = mgr.create(user_id="u1")
    other, _ = _manager(storage, node_id="node-b")
    _expire(storage, ws.id)
    assert other.open(ws.id) is not None
    lane_held: list[bool] = []
    announce = mgr._announce_unload

    def recording(target: Workstream, ours: bool, **kwargs: Any) -> None:
        # The id lane (the per-id open lock), which a reopen takes first.
        entry = mgr._open_locks.get(target.id)
        lane_held.append(entry is not None and entry[0]._is_owned())
        announce(target, ours, **kwargs)

    mgr._announce_unload = recording  # type: ignore[method-assign]

    assert [lease.ws_id for lease in mgr.renew_leases_once()] == [ws.id]
    assert lane_held == [True]


def _raising_discard(mgr: Any, storage: FakeStorage) -> None:
    from tests.test_session_manager_lifecycle_races import _RaisingDiscardStateWriter

    mgr._state_writer = _RaisingDiscardStateWriter(storage, flush_interval=60.0)


def test_a_raising_state_discard_never_strands_a_retired_slot() -> None:
    storage = FakeStorage()
    mgr, _ = _manager(storage, node_id="node-a", max_active=1)
    _raising_discard(mgr, storage)
    ws = mgr.create(user_id="u1")
    other, _ = _manager(storage, node_id="node-b")
    _expire(storage, ws.id)
    assert other.open(ws.id) is not None

    mgr.renew_leases_once()

    assert mgr.get(ws.id) is None and _tracked_ids(mgr) == set()
    assert mgr.create(user_id="u1") is not None  # the slot is free again


@pytest.mark.parametrize("path", ["close", "idle_close"])
def test_a_raising_closed_write_still_releases_the_lease(path: str) -> None:
    storage = FakeStorage()
    mgr, _ = _manager(storage, node_id="node-a")
    _raising_discard(mgr, storage)
    ws = mgr.create(user_id="u1")

    with pytest.raises(RuntimeError, match="discard forced failure"):
        if path == "close":
            mgr.close(ws.id)
        else:
            ws.last_active = time.monotonic() - 3600
            mgr.close_idle(60)

    assert _tracked_ids(mgr) == set()
    assert storage.rows[ws.id].lease_holder is None


def test_a_stopped_copy_is_not_served_as_already_loaded() -> None:
    """Its session found its write refused; the next open retires it and names the holder."""
    storage = FakeStorage()
    mgr, _ = _manager(storage, node_id="node-a")
    ws = mgr.create(user_id="u1")
    other, _ = _manager(storage, node_id="node-b")
    _expire(storage, ws.id)
    assert other.open(ws.id) is not None
    own = _own(ws)
    assert own is not None
    own.mark_lost()  # the journal's refused write, before any renewal tick

    assert mgr.loaded(ws.id) is None
    with pytest.raises(WorkstreamLeaseHeldError) as excinfo:
        mgr.open_with_outcome(ws.id)

    assert excinfo.value.holder_node_id == "node-b"
    assert mgr.get(ws.id) is None
    assert ws.session.lease_lost_calls == 1


def test_create_unwinds_when_its_lease_cannot_be_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = FakeStorage()
    mgr, adapter = _manager(storage)

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("lease store unavailable")

    monkeypatch.setattr(storage, "acquire_workstream_lease", unavailable)

    with pytest.raises(RuntimeError, match="lease store unavailable"):
        mgr.create(user_id="u1")

    assert mgr.list_all() == []
    assert adapter.build_session_calls == 0
    assert storage.rows == {}
    assert _tracked_ids(mgr) == set()


@pytest.mark.parametrize("path", ["create", "open"])
def test_an_interrupted_load_leaves_no_slot_and_no_lease(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Ctrl-C while the session is built, or while it loads its history, unwinds like a failure."""
    storage = FakeStorage()
    mgr, adapter = _manager(storage)
    ws_id = ""
    if path == "open":
        ws = mgr.create(user_id="u1")
        assert mgr.close(ws.id)
        ws_id = ws.id

    def interrupted(*_args: Any, **_kwargs: Any) -> Any:
        raise KeyboardInterrupt

    if path == "create":
        monkeypatch.setattr(adapter, "build_session", interrupted)
        with pytest.raises(KeyboardInterrupt):
            mgr.create(user_id="u1")
        assert storage.rows == {}
    else:
        monkeypatch.setattr(LeaseAwareSession, "rehydrate", interrupted)
        with pytest.raises(KeyboardInterrupt):
            mgr.open(ws_id)
        assert storage.rows[ws_id].lease_holder is None

    assert mgr.list_all() == []
    assert _tracked_ids(mgr) == set()


@pytest.mark.parametrize("path", ["create", "open"])
def test_a_lease_lost_before_its_slot_holds_it_fails_the_load(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Nothing would report a lease lost before any slot held it."""
    storage = FakeStorage()
    mgr, adapter = _manager(storage)
    ws_id = ""
    if path == "open":
        ws = mgr.create(user_id="u1")
        assert mgr.close(ws.id)
        ws_id = ws.id
    acquire = mgr._acquire_lease

    def acquire_then_lose(*args: Any, **kwargs: Any) -> Any:
        lease = acquire(*args, **kwargs)
        lease.mark_lost()
        return lease

    monkeypatch.setattr(mgr, "_acquire_lease", acquire_then_lose)
    builds = adapter.build_session_calls

    with pytest.raises(WorkstreamLeaseLostError):
        if path == "create":
            mgr.create(user_id="u1")
        else:
            mgr.open(ws_id)

    assert mgr.list_all() == []
    assert adapter.build_session_calls == builds
    assert _tracked_ids(mgr) == set()


def test_retirement_tells_a_session_built_during_the_open_it_was_lost() -> None:
    """The retirement's own notice reaches a session the first notice missed."""
    storage = FakeStorage()
    mgr, _ = _manager(storage)
    ws = mgr.create(user_id="u1")
    session = ws.session
    lease = _own(ws)
    assert lease is not None
    # The loss is reported before the slot's session could hear it (as when it
    # is still being built), so only the retirement itself can stop it.
    lease.mark_lost()
    mgr._retire_lost_lease(ws, lease)

    assert session.lease_lost_calls == 1
    assert mgr.get(ws.id) is None


def test_two_managers_cannot_both_open_one_workstream() -> None:
    storage = FakeStorage()
    owner, _ = _manager(storage, node_id="node-a")
    other, other_adapter = _manager(storage, node_id="node-b")
    ws = owner.create(user_id="u1")
    before = _lease_events().get("conflict", 0)

    with pytest.raises(WorkstreamLeaseHeldError) as excinfo:
        other.open(ws.id)

    assert excinfo.value.holder_node_id == "node-a"
    assert other.count == 0
    assert other_adapter.build_session_calls == 0
    assert storage.rows[ws.id].lease_holder == owner._lease_holder
    assert _lease_events().get("conflict", 0) == before + 1


def test_a_refused_open_evicts_nobody() -> None:
    storage = FakeStorage()
    owner, _ = _manager(storage, node_id="node-a")
    other, _ = _manager(storage, node_id="node-b", max_active=1)
    held = owner.create(user_id="u1")
    idle_peer = other.create(user_id="u1")
    idle_peer.last_active = time.monotonic() - 1000

    with pytest.raises(WorkstreamLeaseHeldError):
        other.open(held.id)

    assert other.get(idle_peer.id) is idle_peer


def test_closed_workstream_reopens_elsewhere() -> None:
    storage = FakeStorage()
    owner, _ = _manager(storage, node_id="node-a")
    other, _ = _manager(storage, node_id="node-b")
    ws = owner.create(user_id="u1")
    fence = _own(ws).fence

    assert owner.close(ws.id) is True
    assert storage.rows[ws.id].state == "closed"
    assert storage.rows[ws.id].lease_holder is None
    assert fence in storage.released

    reopened = other.open(ws.id)
    assert reopened is not None
    assert storage.rows[ws.id].lease_holder == other._lease_holder
    assert storage.rows[ws.id].lease_epoch == fence.epoch + 1


# -- release points ----------------------------------------------------------


def test_eviction_releases_the_victims_lease() -> None:
    storage = FakeStorage()
    mgr, _ = _manager(storage, max_active=1)
    victim = mgr.create(user_id="u1")
    victim.last_active = time.monotonic() - 1000

    mgr.create(user_id="u1")

    assert storage.rows[victim.id].lease_holder is None
    assert _own(victim) is not None and _own(victim).state == "released"


def test_discard_releases_before_the_unfenced_row_delete() -> None:
    storage = FakeStorage()
    mgr, _ = _manager(storage)
    ws = mgr.create(user_id="u1", defer_emit_created=True)
    holder_seen: list[str | None] = []

    def _delete_row() -> None:
        holder_seen.append(storage.rows[ws.id].lease_holder)
        assert storage.delete_workstream_if_fork_reserved(ws.id, ws._fork_reservation_token)

    assert mgr.discard(ws.id, expected=ws, after_release=_delete_row)
    assert holder_seen == [None]
    assert ws.id not in storage.rows


def test_delete_persisted_presents_the_holders_fence() -> None:
    storage = FakeStorage()
    mgr, _ = _manager(storage)
    ws = mgr.create(user_id="u1")
    seen: list[LeaseFence | None] = []

    def _delete(*, lease: LeaseFence | None) -> bool:
        seen.append(lease)
        return storage.delete_workstream_if_fork_reserved(
            ws.id, ws._fork_reservation_token, lease=lease
        )

    assert mgr.delete_persisted(ws.id, delete_fn=_delete) is True
    assert seen == [_own(ws).fence]
    assert ws.id not in storage.rows
    assert mgr.lease_fence(ws.id) is None


def test_non_holder_delete_is_refused_while_the_owner_is_live() -> None:
    storage = FakeStorage()
    owner, _ = _manager(storage, node_id="node-a")
    other, _ = _manager(storage, node_id="node-b")
    ws = owner.create(user_id="u1")
    token = storage.fork_reservations[ws.id]

    with pytest.raises(WorkstreamLeaseHeldError):
        other.delete_persisted(
            ws.id,
            delete_fn=lambda *, lease: storage.delete_workstream_if_fork_reserved(
                ws.id, token, lease=lease
            ),
        )
    assert ws.id in storage.rows
    assert owner.get(ws.id) is ws


def test_release_leases_clears_every_held_row() -> None:
    storage = FakeStorage()
    mgr, _ = _manager(storage)
    first = mgr.create(user_id="u1")
    second = mgr.create(user_id="u1")

    mgr.release_leases()

    assert storage.rows[first.id].lease_holder is None
    assert storage.rows[second.id].lease_holder is None


def test_release_leases_drains_writes_first_and_keeps_an_undrained_sessions_lease() -> None:
    """Shutdown never releases a lease while a write it fences may still land."""
    from unittest.mock import MagicMock

    storage = FakeStorage()
    mgr, _ = _manager(storage)
    drained = mgr.create(user_id="u1")
    stuck = mgr.create(user_id="u1")
    order: list[str] = []
    timeouts: list[float | None] = []

    def drain_hook(name: str, result: bool) -> Any:
        def drain(timeout: float | None = None) -> bool:
            order.append(f"drain:{name}")
            timeouts.append(timeout)
            return result

        return drain

    drained.session.shutdown_publication_and_drain_durability = drain_hook("drained", True)
    stuck.session.shutdown_publication_and_drain_durability = drain_hook("stuck", False)
    drained.session.close_publication = lambda: order.append("latch:drained")
    stuck.session.close_publication = lambda: order.append("latch:stuck")
    state_writer = MagicMock()
    state_writer.flush.side_effect = lambda: order.append("flush")
    mgr._state_writer = state_writer
    release = storage.release_workstream_lease

    def recording_release(fence: LeaseFence) -> bool:
        order.append(f"release:{fence.ws_id}")
        return release(fence)

    storage.release_workstream_lease = recording_release  # type: ignore[method-assign]

    mgr.release_leases()

    # Every session stops publishing before any drain starts.
    assert order == [
        "latch:drained",
        "latch:stuck",
        "drain:drained",
        "drain:stuck",
        "flush",
        f"release:{drained.id}",
    ]
    assert all(t is not None and 0 <= t <= mgr._SHUTDOWN_DRAIN_SECONDS for t in timeouts)
    assert storage.rows[drained.id].lease_holder is None
    # The undrained session's lease expires on its own instead.
    assert storage.rows[stuck.id].lease_holder == mgr._lease_holder


def test_release_leases_stops_at_the_first_storage_failure() -> None:
    storage = FakeStorage()
    mgr, _ = _manager(storage)
    mgr.create(user_id="u1")
    mgr.create(user_id="u1")
    attempts: list[str] = []

    def unreachable(fence: LeaseFence) -> bool:
        attempts.append(fence.ws_id)
        raise ConnectionError("database unreachable")

    storage.release_workstream_lease = unreachable  # type: ignore[method-assign]

    mgr.release_leases()

    assert len(attempts) == 1


def _exit_close(mgr: Any, adapter: Any, storage: FakeStorage) -> str:
    ws = mgr.create(user_id="u1")
    assert mgr.close(ws.id)
    return ws.id


def _exit_close_idle(mgr: Any, adapter: Any, storage: FakeStorage) -> str:
    ws = mgr.create(user_id="u1")
    ws.last_active = time.monotonic() - 1000
    assert ws.id in mgr.close_idle(60.0)
    return ws.id


def _exit_eviction(mgr: Any, adapter: Any, storage: FakeStorage) -> str:
    victim = mgr.create(user_id="u1")
    victim.last_active = time.monotonic() - 1000
    for _ in range(mgr._max_active):
        mgr.create(user_id="u1")
    assert mgr.get(victim.id) is None
    return victim.id


def _exit_delete(mgr: Any, adapter: Any, storage: FakeStorage) -> str:
    ws = mgr.create(user_id="u1")
    assert mgr.delete(ws.id)
    return ws.id


def _exit_create_fails(mgr: Any, adapter: Any, storage: FakeStorage) -> str:
    adapter.build_session_raises = True
    with pytest.raises(RuntimeError):
        mgr.create(user_id="u1", ws_id="ws-create-fails")
    return "ws-create-fails"


def _exit_ambiguous_delete(mgr: Any, adapter: Any, storage: FakeStorage) -> str:
    ws = mgr.create(user_id="u1")
    assert mgr.delete_persisted(ws.id, delete_fn=lambda *, lease: False) is False
    assert mgr.get(ws.id) is None
    return ws.id


def _exit_open_fails(mgr: Any, adapter: Any, storage: FakeStorage) -> str:
    ws = mgr.create(user_id="u1")
    assert mgr.close(ws.id)
    adapter.build_session_raises = True
    with pytest.raises(RuntimeError):
        mgr.open(ws.id)
    return ws.id


def _exit_open_at_capacity(mgr: Any, adapter: Any, storage: FakeStorage) -> str:
    target = mgr.create(user_id="u1")
    assert mgr.close(target.id)
    for _ in range(mgr._max_active):
        busy = mgr.create(user_id="u1")
        mgr.set_state(busy.id, WorkstreamState.RUNNING)
    with pytest.raises(RuntimeError, match="slots are active"):
        mgr.open(target.id)
    return target.id


def _exit_close_pending_create(mgr: Any, adapter: Any, storage: FakeStorage) -> str:
    ws = mgr.create(user_id="u1", defer_emit_created=True)
    mgr.close(ws.id)
    return ws.id


def _exit_rollback_create(mgr: Any, adapter: Any, storage: FakeStorage) -> str:
    ws = mgr.create(user_id="u1", defer_emit_created=True)
    mgr.rollback_create(ws)
    return ws.id


def _exit_lease_lost(mgr: Any, adapter: Any, storage: FakeStorage) -> str:
    ws = mgr.create(user_id="u1")
    _expire(storage, ws.id)
    other, _ = _manager(storage, node_id="node-b")
    assert other.open(ws.id) is not None
    assert [lease.ws_id for lease in mgr.renew_leases_once()] == [ws.id]
    # The row is the other node's now: release it there so the shared
    # assertion below checks this manager's bookkeeping only.
    assert other.close(ws.id)
    return ws.id


@pytest.mark.parametrize(
    "leave",
    [
        _exit_close,
        _exit_close_pending_create,
        _exit_rollback_create,
        _exit_lease_lost,
        _exit_close_idle,
        _exit_eviction,
        _exit_delete,
        _exit_ambiguous_delete,
        _exit_create_fails,
        _exit_open_fails,
        _exit_open_at_capacity,
    ],
    ids=lambda leave: leave.__name__.removeprefix("_exit_"),
)
def test_every_slot_exit_releases_its_lease(leave: Any) -> None:
    """A missed release is renewed until shutdown, so every exit must release."""
    storage = FakeStorage()
    mgr, adapter = _manager(storage, max_active=2)

    ws_id = leave(mgr, adapter, storage)

    assert mgr.lease_fence(ws_id) is None
    assert all(lease.ws_id != ws_id for lease in mgr._lease_keeper.tracked())
    row = storage.rows.get(ws_id)
    assert row is None or row.lease_holder is None


# -- state writes ------------------------------------------------------------


def test_state_writes_present_the_slot_fence() -> None:
    storage = FakeStorage()
    mgr, _ = _manager(storage)
    ws = mgr.create(user_id="u1")

    mgr.set_state(ws.id, WorkstreamState.RUNNING)
    assert storage.rows[ws.id].state == "running"

    # A newer acquisition elsewhere (expired lease, then takeover) retires
    # this slot's fence: the next state write is refused, not applied.
    _expire(storage, ws.id)
    other, _ = _manager(storage, node_id="node-b")
    assert other.open(ws.id) is not None
    mgr.set_state(ws.id, WorkstreamState.IDLE)
    assert storage.rows[ws.id].state == "running"


# -- renewal, loss, supersede ------------------------------------------------


def test_renewal_keeps_leases_live_and_a_storage_error_is_not_a_loss() -> None:
    storage = FakeStorage()
    mgr, _ = _manager(storage)
    ws = mgr.create(user_id="u1")
    expires = storage.rows[ws.id].lease_expires_at

    time.sleep(0.01)
    assert mgr.renew_leases_once() == []
    assert storage.rows[ws.id].lease_expires_at > expires

    storage.renew_raises = True
    assert mgr.renew_leases_once() == []
    assert mgr.get(ws.id) is ws
    assert mgr.lease_fence(ws.id) == _own(ws).fence


def test_lost_lease_retires_the_workstream_silently() -> None:
    storage = FakeStorage()
    mgr, adapter = _manager(storage, node_id="node-a")
    ws = mgr.create(user_id="u1")
    session = ws.session
    _expire(storage, ws.id)
    other, _ = _manager(storage, node_id="node-b")
    assert other.open(ws.id) is not None
    state_writes = list(storage.state_updates)

    lost = mgr.renew_leases_once()

    assert [lease.ws_id for lease in lost] == [ws.id]
    assert mgr.get(ws.id) is None
    assert session.lease_lost_calls == 1
    assert session.closed and session.cancelled
    # The new owner's row is untouched and no cluster close is announced.
    assert storage.state_updates == state_writes
    assert storage.rows[ws.id].lease_holder == other._lease_holder
    assert all(event.ws_id != ws.id for event in adapter.events_of("closed"))
    # Kind-specific bookkeeping still runs (the coordinator children registry).
    assert adapter.lease_retired == [ws.id]
    assert mgr.renew_leases_once() == []


# -- keeper ------------------------------------------------------------------


def test_keeper_threads_retire_a_lost_lease_off_the_renewal_thread() -> None:
    storage = FakeStorage()
    retired = threading.Event()
    keeper = LeaseKeeper(
        storage,
        "node-a/1",
        on_lost=lambda leases: retired.set(),
        ttl_seconds=0.2,
        renew_interval_seconds=0.02,
        label="test",
    )
    storage.register_workstream("ws-k", fork_reservation_token="tok")
    grant = storage.acquire_workstream_lease(
        "ws-k", incarnation_token="tok", holder="node-a/1", node_id="node-a", ttl_seconds=0.2
    )
    assert grant is not None
    keeper.track(WorkstreamLease(grant.fence))
    keeper.start()
    try:
        deadline = time.monotonic() + 5
        while storage.renew_calls < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert storage.renew_calls >= 2
        storage.rows["ws-k"].lease_holder = "node-b/2"
        assert retired.wait(5)
    finally:
        keeper.stop()
    assert keeper.tracked() == []


def test_a_slow_retirement_never_delays_renewal() -> None:
    """Retirement runs on its own thread, so renewals continue while it blocks."""
    storage = FakeStorage()
    entered = threading.Event()
    release = threading.Event()
    retire_threads: list[str] = []

    def slow_retire(_leases: list[WorkstreamLease]) -> None:
        retire_threads.append(threading.current_thread().name)
        entered.set()
        release.wait(5)

    keeper = LeaseKeeper(
        storage,
        "node-a/1",
        on_lost=slow_retire,
        ttl_seconds=0.2,
        renew_interval_seconds=0.02,
        label="test",
    )
    for ws_id in ("ws-lost", "ws-kept"):
        storage.register_workstream(ws_id, fork_reservation_token=f"tok-{ws_id}")
        grant = storage.acquire_workstream_lease(
            ws_id,
            incarnation_token=f"tok-{ws_id}",
            holder="node-a/1",
            node_id="node-a",
            ttl_seconds=0.2,
        )
        assert grant is not None
        keeper.track(WorkstreamLease(grant.fence))
    storage.rows["ws-lost"].lease_holder = "node-b/2"
    keeper.start()
    try:
        assert entered.wait(5)
        calls_at_block = storage.renew_calls
        deadline = time.monotonic() + 5
        while storage.renew_calls < calls_at_block + 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert storage.renew_calls >= calls_at_block + 2
    finally:
        release.set()
        keeper.stop()
    assert retire_threads == ["lease-retire-test"]
    assert [lease.ws_id for lease in keeper.tracked()] == ["ws-kept"]


def test_a_handle_the_session_marked_lost_is_retired_without_a_storage_call() -> None:
    storage = FakeStorage()
    mgr, _ = _manager(storage)
    ws = mgr.create(user_id="u1")
    calls = storage.renew_calls

    _own(ws).mark_lost()
    lost = mgr.renew_leases_once()

    assert lost == [_own(ws)]
    assert mgr.get(ws.id) is None
    assert storage.renew_calls == calls


def test_a_burst_of_losses_stops_every_copy_before_retiring_any(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retirement waits for its id lane and teardown; no stop notice waits behind one."""
    storage = FakeStorage()
    mgr, _ = _manager(storage)
    workstreams = [mgr.create(user_id="u1") for _ in range(3)]
    order: list[tuple[str, str]] = []
    stop = mgr._stop_session_for_lost_lease
    retire = mgr._retire_lost_lease

    def record_stop(ws: Any, lease: WorkstreamLease) -> None:
        order.append(("stop", ws.id))
        stop(ws, lease)

    def record_retire(ws: Any, lease: WorkstreamLease) -> None:
        order.append(("retire", ws.id))
        retire(ws, lease)

    monkeypatch.setattr(mgr, "_stop_session_for_lost_lease", record_stop)
    monkeypatch.setattr(mgr, "_retire_lost_lease", record_retire)
    for ws in workstreams:
        storage.rows[ws.id].lease_holder = "node-b/2"

    lost = mgr.renew_leases_once()

    assert len(lost) == 3
    first_retire = [kind for kind, _ in order].index("retire")
    assert {ws_id for kind, ws_id in order[:first_retire] if kind == "stop"} == {
        ws.id for ws in workstreams
    }
    assert sorted(ws_id for kind, ws_id in order if kind == "retire") == sorted(
        ws.id for ws in workstreams
    )
    assert all(mgr.get(ws.id) is None for ws in workstreams)


def test_the_retire_loop_merges_waiting_batches_and_still_retires_before_stopping() -> None:
    import queue

    batches: list[list[str]] = []
    keeper = LeaseKeeper(
        FakeStorage(), "h", on_lost=lambda leases: batches.append([x.ws_id for x in leases])
    )

    def handle(ws_id: str) -> WorkstreamLease:
        return WorkstreamLease(LeaseFence(ws_id=ws_id, holder="h", epoch=1, incarnation_token="t"))

    waiting: queue.SimpleQueue[list[WorkstreamLease] | None] = queue.SimpleQueue()
    waiting.put([handle("ws-a")])
    waiting.put([handle("ws-b"), handle("ws-c")])
    waiting.put(None)  # stop() arrives with losses still waiting
    keeper._retire_loop(waiting)

    assert batches == [["ws-a", "ws-b", "ws-c"]]


def test_one_failing_retirement_never_strands_the_rest_of_its_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakeStorage()
    mgr, _ = _manager(storage)
    first, second = mgr.create(user_id="u1"), mgr.create(user_id="u1")
    retire = mgr._retire_lost_lease

    def failing_first(ws: Any, lease: WorkstreamLease) -> None:
        if ws is first:
            raise RuntimeError("teardown failed")
        retire(ws, lease)

    monkeypatch.setattr(mgr, "_retire_lost_lease", failing_first)
    for ws in (first, second):
        storage.rows[ws.id].lease_holder = "node-b/2"

    assert len(mgr.renew_leases_once()) == 2

    assert mgr.get(second.id) is None
    # Restore it so teardown can retire the copy the failure left behind.
    monkeypatch.setattr(mgr, "_retire_lost_lease", retire)
    retire(first, first._lease)


def test_keeper_rejects_a_ttl_that_renewal_cannot_keep_alive() -> None:
    with pytest.raises(ValueError):
        LeaseKeeper(
            FakeStorage(), "h", on_lost=lambda leases: None, ttl_seconds=5, renew_interval_seconds=5
        )


def test_lease_events_are_exported() -> None:
    text = metrics.generate_text({}, 0)

    for event in ("acquired", "conflict", "lost", "released", "renewed"):
        assert f'turnstone_workstream_lease_events_total{{event="{event}"}}' in text
