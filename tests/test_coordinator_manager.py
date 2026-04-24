"""Tests for :class:`turnstone.console.coordinator.CoordinatorManager`.

Covers the lifecycle semantics without standing up a full ModelRegistry
or ChatSession: a stub session factory returns a MagicMock-backed
session so tests stay fast.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from turnstone.console.coordinator import CoordinatorManager
from turnstone.console.coordinator_ui import ConsoleCoordinatorUI
from turnstone.core.storage._sqlite import SQLiteBackend
from turnstone.core.workstream import WorkstreamState


@pytest.fixture
def storage(tmp_path):
    return SQLiteBackend(str(tmp_path / "coord.db"))


@pytest.fixture
def built_mgr(storage):
    """Build a CoordinatorManager with a stub session factory.

    The factory records its calls and returns a MagicMock-backed
    session so ``_spawn_worker`` can run without hitting real LLM
    infrastructure.
    """
    call_log: list[dict] = []

    def _session_factory(ui, model_alias=None, ws_id=None, **kwargs):
        call_log.append(
            {
                "ui": ui,
                "model_alias": model_alias,
                "ws_id": ws_id,
                **kwargs,
            }
        )
        mock_session = MagicMock()
        mock_session.ws_id = ws_id
        # send() is the worker thread target; make it a fast no-op.
        mock_session.send.return_value = None
        return mock_session

    def _ui_factory(ws_id, user_id):
        return ConsoleCoordinatorUI(ws_id=ws_id, user_id=user_id)

    mgr = CoordinatorManager(
        session_factory=_session_factory,
        ui_factory=_ui_factory,
        storage=storage,
        max_active=3,
    )
    return mgr, call_log, storage


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_registers_row_with_coordinator_kind(built_mgr):
    mgr, _calls, storage = built_mgr
    ws = mgr.create(user_id="user-1", name="c1")
    row = storage.get_workstream(ws.id)
    assert row is not None
    assert row["kind"] == "coordinator"
    assert row["user_id"] == "user-1"
    assert row["node_id"] == "console"
    assert row["parent_ws_id"] is None


def test_create_passes_kind_to_factory(built_mgr):
    mgr, calls, _s = built_mgr
    mgr.create(user_id="user-1")
    assert calls[-1]["kind"] == "coordinator"
    assert calls[-1]["parent_ws_id"] is None


def test_create_dispatches_initial_message(built_mgr):
    import time

    mgr, _calls, _s = built_mgr
    ws = mgr.create(user_id="user-1", initial_message="hello")
    # Give the worker a brief window to run send() on the mock.
    for _ in range(20):
        if ws.session.send.called:
            break
        time.sleep(0.01)
    ws.session.send.assert_called_once_with("hello")


def test_create_no_initial_message_skips_worker(built_mgr):
    mgr, _calls, _s = built_mgr
    ws = mgr.create(user_id="user-1")
    assert ws.session.send.call_count == 0


# ---------------------------------------------------------------------------
# max_active + eviction
# ---------------------------------------------------------------------------


def test_max_active_enforced_evicts_idle(built_mgr):
    mgr, _calls, _s = built_mgr
    ws_a = mgr.create(user_id="u1")
    ws_b = mgr.create(user_id="u2")
    ws_c = mgr.create(user_id="u3")
    # All three at capacity.  The next create should evict the oldest
    # IDLE — ws_a has the oldest last_active.
    ws_d = mgr.create(user_id="u4")
    # ws_a got evicted from the dict; b/c/d are still present.
    assert mgr.get(ws_a.id) is None
    for w in (ws_b, ws_c, ws_d):
        assert mgr.get(w.id) is not None


def test_max_active_raises_when_all_non_idle(built_mgr):
    mgr, _calls, _s = built_mgr
    ws_a = mgr.create(user_id="u1")
    ws_b = mgr.create(user_id="u2")
    ws_c = mgr.create(user_id="u3")
    # Force all into a non-idle state so no eviction candidate exists.
    for w in (ws_a, ws_b, ws_c):
        w.state = WorkstreamState.RUNNING
    with pytest.raises(RuntimeError) as exc_info:
        mgr.create(user_id="u4")
    assert "slots are active" in str(exc_info.value)


def test_rollback_on_factory_failure(storage):
    """If the session factory raises, the slot + persisted row are rolled back."""

    def _factory_explodes(*args, **kwargs):
        raise RuntimeError("session construction failed")

    mgr = CoordinatorManager(
        session_factory=_factory_explodes,
        ui_factory=lambda w, u: ConsoleCoordinatorUI(ws_id=w, user_id=u),
        storage=storage,
        max_active=3,
    )
    with pytest.raises(RuntimeError):
        mgr.create(user_id="u1")
    # No leaked in-memory workstream.
    assert mgr.list_all() == []


# ---------------------------------------------------------------------------
# send / cancel / close
# ---------------------------------------------------------------------------


def test_send_returns_false_when_not_loaded(built_mgr):
    mgr, _calls, _s = built_mgr
    assert mgr.send("nonexistent", "hello") is False


def test_send_returns_false_on_queue_full_without_spawning_duplicate(storage):
    """If queue_message raises queue.Full, _spawn_worker must NOT fall
    through and start a second concurrent worker on the same ChatSession
    — that would corrupt history / cursors / approvals.  Instead, send()
    returns False so the endpoint can surface 429."""
    import queue
    import threading

    entered = threading.Event()
    block = threading.Event()

    def _slow_send(msg):
        entered.set()
        block.wait(timeout=5.0)

    def _session_factory(ui, model_alias=None, ws_id=None, **kwargs):
        sess = MagicMock()
        sess.send.side_effect = _slow_send
        sess.queue_message.side_effect = queue.Full()
        return sess

    mgr = CoordinatorManager(
        session_factory=_session_factory,
        ui_factory=lambda w, u: ConsoleCoordinatorUI(ws_id=w, user_id=u),
        storage=storage,
        max_active=3,
    )
    ws = mgr.create(user_id="u1", initial_message="first")
    try:
        assert entered.wait(timeout=2.0), "worker didn't start"
        original_thread = ws.worker_thread
        assert mgr.send(ws.id, "second") is False
        # Must NOT have replaced worker_thread with a fresh second worker.
        assert ws.worker_thread is original_thread
    finally:
        block.set()
        if ws.worker_thread:
            ws.worker_thread.join(timeout=2.0)


def test_send_enqueues_on_live_worker(storage):
    """When a worker thread is already processing, send() routes through
    queue_message instead of spawning a duplicate worker."""
    import threading
    import time

    entered = threading.Event()
    block = threading.Event()

    def _slow_send(msg):
        entered.set()
        block.wait(timeout=5.0)

    def _session_factory(ui, model_alias=None, ws_id=None, **kwargs):
        sess = MagicMock()
        sess.send.side_effect = _slow_send
        return sess

    mgr = CoordinatorManager(
        session_factory=_session_factory,
        ui_factory=lambda w, u: ConsoleCoordinatorUI(ws_id=w, user_id=u),
        storage=storage,
        max_active=3,
    )
    ws = mgr.create(user_id="u1", initial_message="first")
    try:
        # Wait until the worker is actually inside session.send.
        assert entered.wait(timeout=2.0), "worker didn't start"
        # Now the worker is alive — mgr.send should route through queue_message.
        for _ in range(20):
            if ws.worker_thread and ws.worker_thread.is_alive():
                break
            time.sleep(0.01)
        sent = mgr.send(ws.id, "second")
        assert sent
        ws.session.queue_message.assert_called_with("second")
    finally:
        block.set()
        if ws.worker_thread:
            ws.worker_thread.join(timeout=2.0)


def test_cancel_resolves_pending_approval(built_mgr):
    mgr, _calls, _s = built_mgr
    ws = mgr.create(user_id="u1")
    assert ws.ui is not None
    assert isinstance(ws.ui, ConsoleCoordinatorUI)
    # Put ui into a pending-approval state.
    ws.ui._pending_approval = {"type": "approve_request", "items": []}
    ws.ui._approval_event.clear()
    assert mgr.cancel(ws.id) is True
    # resolve_approval should have been called with approved=False.
    assert ws.ui._approval_event.is_set()
    assert ws.ui._approval_result == (False, "cancelled")


def test_cancel_unblocks_worker_blocked_on_approval(built_mgr):
    """Cancel fires while a worker thread is blocked inside
    ui.approve_tools() waiting on _approval_event.  The worker must
    unblock with approved=False and return."""
    import threading
    import time

    mgr, _calls, _s = built_mgr
    ws = mgr.create(user_id="u1")
    ui = ws.ui
    assert isinstance(ui, ConsoleCoordinatorUI)

    # Simulate the session worker entering approve_tools.  We call it
    # directly on its own thread so the test can observe the unblock.
    result_holder: list[tuple[bool, str | None]] = []

    def _worker() -> None:
        outcome = ui.approve_tools(
            [
                {
                    "call_id": "c1",
                    "func_name": "spawn_workstream",
                    "approval_label": "spawn_workstream",
                    "needs_approval": True,
                }
            ]
        )
        result_holder.append(outcome)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    # Give the worker time to enter the approval wait.
    for _ in range(50):
        if ui._pending_approval is not None:
            break
        time.sleep(0.01)
    assert ui._pending_approval is not None, "worker didn't reach approve_tools"

    # Cancel fires — worker should unblock with approved=False.
    assert mgr.cancel(ws.id) is True
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert result_holder == [(False, "cancelled")]


def test_close_removes_and_updates_state(built_mgr):
    mgr, _calls, storage = built_mgr
    ws = mgr.create(user_id="u1")
    # Extract side-effectful call from the assert expression so
    # python -O (which strips asserts) can't drop the close().
    closed = mgr.close(ws.id)
    assert closed is True
    assert mgr.get(ws.id) is None
    row = storage.get_workstream(ws.id)
    assert row["state"] == "closed"


# ---------------------------------------------------------------------------
# list_for_user + list_all
# ---------------------------------------------------------------------------


def test_list_for_user_filters_by_owner(built_mgr):
    mgr, _calls, _s = built_mgr
    a = mgr.create(user_id="user-1")
    b = mgr.create(user_id="user-1")
    mgr.create(user_id="user-2")  # non-owner — existence matters, value doesn't
    user1_rows = mgr.list_for_user("user-1")
    ids = {r.id for r in user1_rows}
    assert ids == {a.id, b.id}


def test_list_all_returns_every_loaded(built_mgr):
    mgr, _calls, _s = built_mgr
    mgr.create(user_id="u1")
    mgr.create(user_id="u2")
    assert len(mgr.list_all()) == 2


# ---------------------------------------------------------------------------
# Lazy rehydration
# ---------------------------------------------------------------------------


def test_open_rehydrates_from_storage(built_mgr):
    mgr, _calls, storage = built_mgr
    # Simulate a coordinator persisted from a previous console process.
    storage.register_workstream(
        "coord-persisted",
        node_id="console",
        user_id="user-1",
        kind="coordinator",
    )
    # Initially not loaded in memory.
    assert mgr.get("coord-persisted") is None
    ws = mgr.open("coord-persisted", "user-1")
    assert ws is not None
    assert ws.kind == "coordinator"
    assert ws.user_id == "user-1"
    # Now tracked.
    assert mgr.get("coord-persisted") is not None


def test_open_rejects_non_coordinator_kind(built_mgr):
    mgr, _calls, storage = built_mgr
    storage.register_workstream("interactive-ws", kind="interactive", user_id="user-1")
    # open() has side effects (factory call, slot reservation); keep it
    # out of the assert expression so python -O can't strip it.
    opened = mgr.open("interactive-ws", "user-1")
    assert opened is None


def test_open_enforces_ownership(built_mgr):
    mgr, _calls, storage = built_mgr
    storage.register_workstream("coord-x", kind="coordinator", user_id="owner")
    # Non-owner gets None.
    stranger_ws = mgr.open("coord-x", "stranger")
    assert stranger_ws is None
    # Owner gets the row.
    owner_ws = mgr.open("coord-x", "owner")
    assert owner_ws is not None


def test_open_admin_ignores_ownership(built_mgr):
    mgr, _calls, storage = built_mgr
    storage.register_workstream("coord-x", kind="coordinator", user_id="owner")
    ws = mgr.open_admin("coord-x")
    assert ws is not None


def test_open_resurrects_closed_coordinator(built_mgr):
    """A coordinator that was closed (state='closed' in storage) IS now
    resurrectable via open().  Restore is an explicit user action via
    the Saved Coordinators landing UI; ``_reserve_and_install_locked``
    still enforces ``max_active`` (evicts an idle peer or 429s).  The
    old "URL revisit silently undoes Close" safety lives in the slot
    accounting now, not in a flat refusal at the open path."""
    mgr, _calls, storage = built_mgr
    ws = mgr.create(user_id="u1")
    mgr.close(ws.id)
    assert storage.get_workstream(ws.id)["state"] == "closed"

    reopened = mgr.open(ws.id, "u1")
    assert reopened is not None
    assert reopened.id == ws.id
    # Re-loaded into memory.
    assert mgr.get(ws.id) is reopened

    # Admin path also resurrects.
    mgr.close(ws.id)
    assert mgr.open_admin(ws.id) is not None


def test_open_refuses_deleted_coordinator(built_mgr):
    """A coordinator marked state='deleted' is a tombstone — open() must
    refuse to resurrect even though closed-state is now resurrectable."""
    mgr, _calls, storage = built_mgr
    ws = mgr.create(user_id="u1")
    mgr.close(ws.id)
    storage.update_workstream_state(ws.id, "deleted")
    user_open = mgr.open(ws.id, "u1")
    assert user_open is None
    admin_open = mgr.open_admin(ws.id)
    assert admin_open is None


def test_open_refuses_empty_owner_for_non_admin(built_mgr):
    """Empty-owner rows (orphan / pre-002 migrated) must not be
    rehydrated by non-admin callers — would consume a max_active slot
    and let any user evict another tenant's IDLE coordinator."""
    mgr, _calls, storage = built_mgr
    storage.register_workstream("coord-orphan", kind="coordinator", user_id=None)
    # Non-admin caller — empty owner must NOT short-circuit the gate.
    assert mgr.open("coord-orphan", "any-user") is None
    # Admin path can still rehydrate (e.g. cleanup tooling).
    assert mgr.open_admin("coord-orphan") is not None


def test_open_returns_existing_when_loaded(built_mgr):
    mgr, _calls, _s = built_mgr
    ws1 = mgr.create(user_id="u1")
    ws2 = mgr.open(ws1.id, "u1")
    assert ws2 is ws1


# ---------------------------------------------------------------------------
# Concurrency regressions — blockers 1 & 2 from review
# ---------------------------------------------------------------------------


def test_concurrent_open_for_same_ws_id_constructs_one_session(storage):
    """Two threads calling open() for the same persisted-but-unloaded
    ws_id must not each spin up a session.  Per-ws_id serialization
    ensures the second thread picks up the first thread's session."""
    import threading
    import time

    construct_count = {"n": 0}
    construct_lock = threading.Lock()
    first_in = threading.Event()
    release_first = threading.Event()

    def _slow_factory(ui, model_alias=None, ws_id=None, **kwargs):
        with construct_lock:
            construct_count["n"] += 1
            my_idx = construct_count["n"]
        if my_idx == 1:
            first_in.set()
            # Block so the second thread can race past the storage read.
            release_first.wait(timeout=5.0)
        sess = MagicMock()
        sess.ws_id = ws_id
        sess.send.return_value = None
        return sess

    mgr = CoordinatorManager(
        session_factory=_slow_factory,
        ui_factory=lambda w, u: ConsoleCoordinatorUI(ws_id=w, user_id=u),
        storage=storage,
        max_active=5,
    )
    storage.register_workstream(
        "coord-shared",
        node_id="console",
        user_id="user-1",
        kind="coordinator",
    )

    results: list[Any] = [None, None]

    def _open_one(idx: int) -> None:
        results[idx] = mgr.open("coord-shared", "user-1")

    t1 = threading.Thread(target=_open_one, args=(0,))
    t2 = threading.Thread(target=_open_one, args=(1,))
    t1.start()
    assert first_in.wait(timeout=2.0), "first thread didn't enter factory"
    t2.start()
    # Give t2 a chance to reach the per-ws lock and block.
    time.sleep(0.1)
    release_first.set()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert construct_count["n"] == 1, (
        f"expected exactly 1 session construction, got {construct_count['n']}"
    )
    assert results[0] is not None
    assert results[1] is not None
    # Both threads must see the same installed Workstream instance.
    assert results[0] is results[1]
    # Manager tracks exactly one entry.
    assert len(mgr.list_all()) == 1


def test_concurrent_create_respects_max_active(storage):
    """max_active + 2 concurrent creates → exactly max_active succeed
    and the overflow raises RuntimeError.  Regression for the
    check-then-install gap that previously let all creates pass the gate."""
    import threading

    slow_entered = threading.Event()
    release = threading.Event()

    def _slow_factory(ui, model_alias=None, ws_id=None, **kwargs):
        # Block after construction to widen the race window between
        # slot reservation and final install.  Only the first N reach
        # here — the rest must trip on the capacity gate earlier.
        slow_entered.set()
        release.wait(timeout=5.0)
        sess = MagicMock()
        sess.send.return_value = None
        return sess

    max_active = 3
    mgr = CoordinatorManager(
        session_factory=_slow_factory,
        ui_factory=lambda w, u: ConsoleCoordinatorUI(ws_id=w, user_id=u),
        storage=storage,
        max_active=max_active,
    )

    successes: list[bool] = []
    failures: list[Exception] = []
    successes_lock = threading.Lock()

    def _create_one(user_suffix: int) -> None:
        try:
            mgr.create(user_id=f"u{user_suffix}")
            with successes_lock:
                successes.append(True)
        except RuntimeError as exc:
            with successes_lock:
                failures.append(exc)

    threads = [threading.Thread(target=_create_one, args=(i,)) for i in range(max_active + 2)]
    for t in threads:
        t.start()
    # Wait until at least one creation is blocked inside the factory.
    assert slow_entered.wait(timeout=2.0)
    release.set()
    for t in threads:
        t.join(timeout=5.0)

    assert len(successes) == max_active, f"expected {max_active} successes, got {len(successes)}"
    assert len(failures) == 2
    for exc in failures:
        assert "slots are active" in str(exc)
    assert len(mgr.list_all()) == max_active


# ---------------------------------------------------------------------------
# Cross-tenant leak — blocker 3 from review
# ---------------------------------------------------------------------------


def test_list_for_user_excludes_empty_owner_rows(built_mgr):
    """A coordinator whose user_id is empty (system-created, migration
    artifact, or lazily rehydrated from a NULL owner) must NOT appear
    in list_for_user() output for other callers — doing so would leak
    ws_id + name + state across tenants."""
    mgr, _calls, storage = built_mgr
    # Real user's coordinator.
    owned = mgr.create(user_id="alice")
    # Simulate a rogue empty-owner session by creating one with
    # user_id="" directly.  Matches what a rehydrate of a NULL-owner
    # row would produce, or a system-created coordinator.
    empty_owner = mgr.create(user_id="")
    rows = mgr.list_for_user("alice")
    ids = {ws.id for ws in rows}
    assert owned.id in ids
    assert empty_owner.id not in ids, (
        "list_for_user must not expose empty-owner coordinators to other callers"
    )


# ---------------------------------------------------------------------------
# Phase 3 — child-event fan-out
# ---------------------------------------------------------------------------


def _seed_child_row(storage, *, parent_ws_id: str, ws_id: str, state: str = "idle") -> None:
    storage.register_workstream(
        ws_id,
        node_id="node-a",
        user_id="user-1",
        name=f"c-{ws_id[:4]}",
        kind="interactive",
        parent_ws_id=parent_ws_id,
    )
    if state != "idle":
        storage.update_workstream_state(ws_id, state)


def _drain(listener, *, wait: float = 0.5):
    """Drain a ConsoleCoordinatorUI listener queue with a short timeout."""
    import queue as _q

    items = []
    try:
        while True:
            items.append(listener.get(timeout=wait))
    except _q.Empty:
        return items


def test_children_registry_bootstrapped_from_storage_on_create(built_mgr):
    mgr, _calls, storage = built_mgr
    ws = mgr.create(user_id="user-1")
    # The registry starts empty — no children yet.
    assert mgr._children.get(ws.id, set()) == set()


def test_children_registry_bootstrapped_from_storage_on_open(built_mgr):
    mgr, _calls, storage = built_mgr
    # Seed a persisted coordinator row + two children directly in storage
    # so open() rehydrates them without create() being called.
    coord_id = "a" * 32
    storage.register_workstream(
        coord_id,
        node_id="console",
        user_id="user-1",
        name="persisted",
        kind="coordinator",
        parent_ws_id=None,
    )
    _seed_child_row(storage, parent_ws_id=coord_id, ws_id="b" * 32)
    _seed_child_row(storage, parent_ws_id=coord_id, ws_id="c" * 32)
    ws = mgr.open(coord_id, "user-1")
    assert ws is not None
    assert mgr._children[coord_id] == {"b" * 32, "c" * 32}


def test_dispatch_ws_created_fans_out_to_parent(built_mgr):
    mgr, _calls, _storage = built_mgr
    ws = mgr.create(user_id="user-1")
    listener = ws.ui._register_listener()
    mgr._dispatch_child_event(
        {
            "type": "ws_created",
            "ws_id": "d" * 32,
            "parent_ws_id": ws.id,
            "node_id": "node-a",
            "name": "new-child",
            "title": "",
            "user_id": "user-1",
        }
    )
    events = _drain(listener)
    child_created = [e for e in events if e.get("type") == "child_ws_created"]
    assert len(child_created) == 1
    assert child_created[0]["child_ws_id"] == "d" * 32
    assert child_created[0]["parent_ws_id"] == ws.id
    assert "d" * 32 in mgr._children[ws.id]


def test_dispatch_ws_created_ignores_unrelated_parent(built_mgr):
    mgr, _calls, _storage = built_mgr
    ws = mgr.create(user_id="user-1")
    listener = ws.ui._register_listener()
    # A ws_created for a parent this coordinator doesn't own.
    mgr._dispatch_child_event(
        {
            "type": "ws_created",
            "ws_id": "e" * 32,
            "parent_ws_id": "f" * 32,
            "node_id": "node-a",
            "name": "stranger-child",
            "title": "",
            "user_id": "user-1",
        }
    )
    events = _drain(listener, wait=0.1)
    assert not any(e.get("type") == "child_ws_created" for e in events)


def test_dispatch_ws_created_cross_tenant_dropped(built_mgr):
    """A ws_created event whose user_id does not match the coordinator's
    owner must NOT reach the coordinator's SSE stream — prevents the
    cross-tenant info-leak via spoofed parent_ws_id (sec-1)."""
    mgr, _calls, _storage = built_mgr
    ws = mgr.create(user_id="alice")
    listener = ws.ui._register_listener()
    # A mallory-owned workstream claiming alice's coordinator as parent.
    mgr._dispatch_child_event(
        {
            "type": "ws_created",
            "ws_id": "d" * 32,
            "parent_ws_id": ws.id,
            "node_id": "node-a",
            "name": "spoofed-child",
            "title": "",
            "user_id": "mallory",
        }
    )
    events = _drain(listener, wait=0.1)
    assert not any(e.get("type") == "child_ws_created" for e in events)
    # Registry must not have gained mallory's ws_id either.
    assert "d" * 32 not in mgr._children.get(ws.id, set())


def test_dispatch_ws_created_empty_user_id_dropped(built_mgr):
    """An event with empty/missing user_id fails closed — we can't
    prove tenancy, so we refuse to route it."""
    mgr, _calls, _storage = built_mgr
    ws = mgr.create(user_id="alice")
    listener = ws.ui._register_listener()
    mgr._dispatch_child_event(
        {
            "type": "ws_created",
            "ws_id": "d" * 32,
            "parent_ws_id": ws.id,
            "node_id": "node-a",
            "name": "no-owner-child",
            "title": "",
            # user_id intentionally absent
        }
    )
    events = _drain(listener, wait=0.1)
    assert not any(e.get("type") == "child_ws_created" for e in events)
    assert "d" * 32 not in mgr._children.get(ws.id, set())


def test_dispatch_cluster_state_fans_out_when_child_tracked(built_mgr):
    mgr, _calls, _storage = built_mgr
    ws = mgr.create(user_id="user-1")
    child_id = "a" * 32
    mgr._add_child(ws.id, child_id)
    listener = ws.ui._register_listener()
    mgr._dispatch_child_event(
        {
            "type": "cluster_state",
            "ws_id": child_id,
            "state": "running",
            "tokens": 42,
            "node_id": "node-a",
        }
    )
    events = _drain(listener)
    state_events = [e for e in events if e.get("type") == "child_ws_state"]
    assert len(state_events) == 1
    assert state_events[0]["child_ws_id"] == child_id
    assert state_events[0]["state"] == "running"
    assert state_events[0]["tokens"] == 42


def test_dispatch_ws_closed_fans_out(built_mgr):
    mgr, _calls, _storage = built_mgr
    ws = mgr.create(user_id="user-1")
    child_id = "a" * 32
    mgr._add_child(ws.id, child_id)
    listener = ws.ui._register_listener()
    mgr._dispatch_child_event({"type": "ws_closed", "ws_id": child_id, "reason": "closed"})
    events = _drain(listener)
    close_events = [e for e in events if e.get("type") == "child_ws_closed"]
    assert len(close_events) == 1
    assert close_events[0]["child_ws_id"] == child_id
    assert close_events[0]["reason"] == "closed"


def test_dispatch_unrelated_state_ignored(built_mgr):
    mgr, _calls, _storage = built_mgr
    ws = mgr.create(user_id="user-1")
    listener = ws.ui._register_listener()
    # No _add_child called — ws_id is not in anyone's registry.
    mgr._dispatch_child_event({"type": "cluster_state", "ws_id": "a" * 32, "state": "running"})
    events = _drain(listener, wait=0.1)
    assert not any(e.get("type", "").startswith("child_ws_") for e in events)


def test_shutdown_is_idempotent(built_mgr):
    mgr, _calls, _storage = built_mgr
    # No fanout started — shutdown must not raise.
    mgr.shutdown()
    mgr.shutdown()


# ---------------------------------------------------------------------------
# Phase 3 — review-pass-2 regression tests
# ---------------------------------------------------------------------------


def test_rebuild_registry_unions_with_concurrent_adds(built_mgr):
    """A ws_created event that arrives during open() must survive the
    subsequent _rebuild_children_registry call — the rebuild must UNION
    its storage read with whatever the fan-out thread already added."""
    mgr, _calls, storage = built_mgr
    coord_id = "a" * 32
    # Seed a persisted coordinator row — open() will rehydrate it.
    storage.register_workstream(
        coord_id,
        node_id="console",
        user_id="user-1",
        name="persisted",
        kind="coordinator",
        parent_ws_id=None,
    )
    # Persist one child (will show up in rebuild's storage query).
    _seed_child_row(storage, parent_ws_id=coord_id, ws_id="b" * 32)
    # Simulate the fan-out thread pre-adding a different child_ws_id
    # between the placeholder install and the rebuild call.  Calling
    # open() in this test runs synchronously, so we emulate the race
    # by pre-populating the registry for the coord before open.
    mgr._add_child(coord_id, "c" * 32)
    ws = mgr.open(coord_id, "user-1")
    assert ws is not None
    # Both the persisted child (from rebuild) AND the pre-added one
    # (from the simulated fan-out race) should be present.
    assert "b" * 32 in mgr._children[coord_id]
    assert "c" * 32 in mgr._children[coord_id]


def test_dispatch_ws_created_atomic_against_close(built_mgr):
    """Concurrent close() during a ws_created dispatch must not leave
    the evicted coordinator's registry entry behind.

    Regression for a race where the dispatch reads _active_coords
    lock-free, close() runs (pops _children[parent]) between the
    snapshot read and the _children_lock acquisition, then setdefault
    resurrects the entry — leaking the registry key forever."""
    mgr, _calls, _storage = built_mgr
    ws = mgr.create(user_id="user-1")
    # Close the coordinator — _children[ws.id] gets popped and
    # _active_coords loses the entry.
    closed = mgr.close(ws.id)
    assert closed
    # A ws_created event still arriving for the now-closed parent
    # must NOT resurrect the registry entry via setdefault.
    mgr._dispatch_child_event(
        {
            "type": "ws_created",
            "ws_id": "d" * 32,
            "parent_ws_id": ws.id,
            "node_id": "node-a",
            "user_id": "user-1",
        }
    )
    assert ws.id not in mgr._children
    assert ws.id not in mgr._active_coords


def test_open_impl_eviction_clears_children_registry(built_mgr):
    """When _open_impl evicts an idle coordinator to make room, the
    evicted coordinator's _children entry must be popped — matching
    the create() eviction path."""
    mgr, _calls, storage = built_mgr
    # Fill the manager to capacity (max_active=3) with owned coords,
    # then pre-seed a 4th as persisted-only so open() triggers eviction.
    for i in range(3):
        mgr.create(user_id=f"u{i}")
    # Record which coord is idlest (oldest create) — it's the eviction
    # candidate.
    victim_id = mgr._order[0]
    # Pre-seed the victim's _children to prove the pop works.
    mgr._add_child(victim_id, "z" * 32)
    assert victim_id in mgr._children
    # Persist a 4th coord row so open() will rehydrate + evict.
    fourth_id = "f" * 32
    storage.register_workstream(
        fourth_id,
        node_id="console",
        user_id="u3",
        name="fourth",
        kind="coordinator",
        parent_ws_id=None,
    )
    # Force open() — it must evict the idle victim and clear its
    # registry entry in the process.
    result = mgr.open_admin(fourth_id)
    assert result is not None
    assert victim_id not in mgr._workstreams, "victim should have been evicted to make room"
    assert victim_id not in mgr._children, (
        "_open_impl must pop the evicted coordinator's _children entry "
        "(mirrors create() eviction path)"
    )


def test_child_to_coord_reverse_index_maintained(built_mgr):
    """_coord_for_child uses the reverse index for O(1) lookup.  The
    index must stay in sync with the forward set across add/close
    paths — this test pokes each maintenance point."""
    mgr, _calls, _storage = built_mgr
    ws = mgr.create(user_id="user-1")
    # _add_child path — populates both sides.
    assert mgr._add_child(ws.id, "child-1")
    assert mgr._coord_for_child("child-1") == ws.id
    assert mgr._child_to_coord["child-1"] == ws.id

    # close() path — pops both sides.
    mgr.close(ws.id)
    assert mgr._coord_for_child("child-1") is None
    assert "child-1" not in mgr._child_to_coord


def test_prime_children_from_snapshot(built_mgr):
    """start_child_event_fanout uses the collector snapshot to prime
    the child registry so a just-opened coordinator sees already-live
    children without waiting for the next ws_state event.  Simulate
    by calling the helper directly."""
    mgr, _calls, _storage = built_mgr
    ws = mgr.create(user_id="user-1")
    snapshot = {
        "nodes": [
            {
                "node_id": "node-a",
                "workstreams": [
                    {"id": "child-1", "parent_ws_id": ws.id, "state": "running"},
                    {"id": "child-2", "parent_ws_id": ws.id, "state": "idle"},
                    # Unrelated — parent isn't a tracked coordinator.
                    {
                        "id": "foreign-1",
                        "parent_ws_id": "some-other-coord",
                        "state": "idle",
                    },
                ],
            }
        ]
    }
    mgr._prime_children_from_snapshot(snapshot)
    assert mgr._children[ws.id] == {"child-1", "child-2"}
    assert mgr._coord_for_child("child-1") == ws.id
    assert mgr._coord_for_child("child-2") == ws.id
    # Foreign children with parents we don't track stay out of the
    # registry — we only care about live coordinators.
    assert mgr._coord_for_child("foreign-1") is None


def test_prime_children_from_empty_snapshot_noop(built_mgr):
    """No nodes → no state changes.  Defensive: snapshot shape can
    legitimately be missing the ``nodes`` key right after startup."""
    mgr, _calls, _storage = built_mgr
    ws = mgr.create(user_id="user-1")
    mgr._prime_children_from_snapshot({})
    mgr._prime_children_from_snapshot({"nodes": []})
    assert mgr._children[ws.id] == set()
