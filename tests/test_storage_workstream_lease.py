"""Workstream owner lease at the storage layer, on both dialects.

Every test runs against the backend ``--storage-backend`` selects. The two
dialects differ in exactly one rule: PostgreSQL refuses to acquire a live lease
another holder owns, while SQLite (one process by contract) takes it over.
Every write rule is identical on both.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from typing import Any

import pytest
import sqlalchemy as sa

from tests._storage_fakes import acquire_lease as _acquire
from tests._storage_fakes import expire_lease as _expire
from tests._storage_fakes import make_attachment
from turnstone.core.storage import (
    ConversationCommitWorkstreamGoneError,
    LeaseFence,
    WorkstreamLeaseHeldError,
    WorkstreamLeaseLostError,
    _lease,
)
from turnstone.core.storage._schema import workstreams
from turnstone.core.storage._sqlite import SQLiteBackend


def _register(backend: Any, ws_id: str, *, state: str = "idle", token: str = "") -> str:
    token = token or f"tok-{ws_id}"
    assert (
        backend.register_workstream(
            ws_id, state=state, kind="interactive", fork_reservation_token=token
        )
        is True
    )
    return token


def _lease_state(backend: Any, ws_id: str) -> Any:
    with backend._engine.connect() as conn:
        return conn.execute(
            sa.select(
                workstreams.c.lease_holder,
                workstreams.c.lease_node_id,
                workstreams.c.lease_epoch,
                workstreams.c.lease_expires_ms,
                workstreams.c.updated,
            ).where(workstreams.c.ws_id == ws_id)
        ).one()


def _force_updated(backend: Any, ws_id: str, updated: str) -> None:
    with backend._engine.connect() as conn:
        conn.execute(
            sa.update(workstreams).where(workstreams.c.ws_id == ws_id).values(updated=updated)
        )
        conn.commit()


def _is_sqlite(backend: Any) -> bool:
    return isinstance(backend, SQLiteBackend)


def _key() -> str:
    return f"commit-{uuid.uuid4().hex}"


# -- Acquisition -------------------------------------------------------------


def test_acquire_grants_a_new_epoch_per_acquisition(storage_backend: Any) -> None:
    backend = storage_backend
    _register(backend, "ws-acquire")

    first = _acquire(backend, "ws-acquire")
    again = backend.acquire_workstream_lease(
        "ws-acquire",
        incarnation_token="tok-ws-acquire",
        holder="node-a/1",
        node_id="node-a",
        ttl_seconds=30.0,
    )

    assert first == LeaseFence("ws-acquire", "node-a/1", 1, "tok-ws-acquire")
    assert again is not None
    assert again.fence.epoch == 2
    # Re-acquiring as the same holder supersedes, it does not take over.
    assert (again.previous_holder, again.previous_node_id, again.took_over_live) == ("", "", False)
    holder, node, epoch, expires, _updated = _lease_state(backend, "ws-acquire")
    assert (holder, node, epoch) == ("node-a/1", "node-a", 2)
    assert expires > 0
    assert backend.get_workstream("ws-acquire")["lease_node_id"] == "node-a"


def test_acquire_is_bound_to_one_published_incarnation(storage_backend: Any) -> None:
    backend = storage_backend
    _register(backend, "ws-bound")
    _register(backend, "ws-creating", state="creating")
    _register(backend, "ws-deleted")
    backend.update_workstream_state("ws-deleted", "deleted")

    def attempt(ws_id: str, token: str, *, allow_creating: bool = False) -> Any:
        return backend.acquire_workstream_lease(
            ws_id,
            incarnation_token=token,
            holder="node-a/1",
            node_id="node-a",
            ttl_seconds=30.0,
            allow_creating=allow_creating,
        )

    assert attempt("ws-missing", "tok-ws-missing") is None
    assert attempt("ws-bound", "another-incarnation") is None
    assert attempt("ws-bound", "") is None
    assert attempt("ws-deleted", "tok-ws-deleted") is None
    assert attempt("ws-creating", "tok-ws-creating") is None
    assert attempt("ws-creating", "tok-ws-creating", allow_creating=True) is not None
    assert _lease_state(backend, "ws-bound").lease_holder is None


def test_live_lease_of_another_holder_is_refused_on_postgresql_taken_over_on_sqlite(
    storage_backend: Any,
) -> None:
    backend = storage_backend
    _register(backend, "ws-contended")
    first = _acquire(backend, "ws-contended", holder="node-a/1")

    if _is_sqlite(backend):
        second = backend.acquire_workstream_lease(
            "ws-contended",
            incarnation_token="tok-ws-contended",
            holder="node-b/2",
            node_id="node-b",
            ttl_seconds=30.0,
        )
        assert second is not None
        assert second.took_over_live is True
        assert (second.previous_holder, second.previous_node_id) == ("node-a/1", "node-a")
        with pytest.raises(WorkstreamLeaseLostError):
            backend.update_workstream_title("ws-contended", "stale", lease=first)
        return

    with pytest.raises(WorkstreamLeaseHeldError) as excinfo:
        _acquire(backend, "ws-contended", holder="node-b/2")
    err = excinfo.value
    assert err.holder_node_id == "node-a"
    assert 0 < err.retry_after_ms <= 30_000
    assert err.as_dict() == {
        "error": "This workstream is open on node 'node-a'.",
        "code": "workstream_lease_held",
        "holder_node_id": "node-a",
        "retry_after_ms": err.retry_after_ms,
    }
    assert _lease_state(backend, "ws-contended").lease_holder == "node-a/1"
    backend.update_workstream_title("ws-contended", "still owned", lease=first)


def test_expired_lease_is_taken_over_and_fences_out_the_former_holder(
    storage_backend: Any,
) -> None:
    backend = storage_backend
    _register(backend, "ws-expired")
    former = _acquire(backend, "ws-expired", holder="node-a/1")
    _expire(backend, "ws-expired")

    grant = backend.acquire_workstream_lease(
        "ws-expired",
        incarnation_token="tok-ws-expired",
        holder="node-b/2",
        node_id="node-b",
        ttl_seconds=30.0,
    )

    assert grant is not None
    assert grant.fence.epoch == former.epoch + 1
    assert (grant.previous_holder, grant.previous_node_id) == ("node-a/1", "node-a")
    assert grant.took_over_live is False
    with pytest.raises(WorkstreamLeaseLostError):
        backend.save_message("ws-expired", "assistant", "paused", commit_key=_key(), lease=former)
    assert backend.count_messages("ws-expired") == 0


def test_postgresql_concurrent_acquisitions_grant_exactly_one(storage_backend: Any) -> None:
    backend = storage_backend
    if _is_sqlite(backend):
        pytest.skip("SQLite takes a live lease over by contract")
    _register(backend, "ws-race")
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def contend(holder: str) -> None:
        barrier.wait(timeout=10)
        try:
            _acquire(backend, "ws-race", holder=holder)
        except WorkstreamLeaseHeldError:
            outcome = "held"
        else:
            outcome = "granted"
        with lock:
            outcomes.append(outcome)

    threads = [
        threading.Thread(target=contend, args=(holder,), name=f"lease-contender-{holder}")
        for holder in ("node-a/1", "node-b/2")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["granted", "held"]
    assert _lease_state(backend, "ws-race").lease_epoch == 1


# -- Renewal and release -----------------------------------------------------


def test_row_reads_name_the_holder_only_while_its_lease_is_live(storage_backend: Any) -> None:
    """Routing follows a live holder; a crashed holder's expired lease attracts nothing."""
    ws_id = "ws-live-hint"
    _register(storage_backend, ws_id)
    _acquire(storage_backend, ws_id, holder="node-a/1")
    assert storage_backend.get_workstream(ws_id)["lease_node_id"] == "node-a"

    _expire(storage_backend, ws_id)

    assert storage_backend.get_workstream(ws_id)["lease_node_id"] is None
    assert storage_backend.get_workstreams_batch([ws_id])[ws_id]["lease_node_id"] is None
    # The stored lease itself is untouched until someone takes it over.
    assert _lease_state(storage_backend, ws_id).lease_holder == "node-a/1"


def test_renew_extends_only_leases_the_holder_still_owns(storage_backend: Any) -> None:
    backend = storage_backend
    for ws_id in ("ws-kept", "ws-lapsed", "ws-lost"):
        _register(backend, ws_id)
    kept = _acquire(backend, "ws-kept")
    lapsed = _acquire(backend, "ws-lapsed")
    lost = _acquire(backend, "ws-lost")
    _force_updated(backend, "ws-kept", "2020-01-01T00:00:00")
    # An expired lease nobody took over is still exclusive and revivable.
    _expire(backend, "ws-lapsed")
    _expire(backend, "ws-lost")
    _acquire(backend, "ws-lost", holder="node-b/2")

    renewed = backend.renew_workstream_leases("node-a/1", [kept, lapsed, lost], ttl_seconds=30.0)

    assert renewed == {"ws-kept", "ws-lapsed"}
    # Live again by the database clock (row reads report a live holder only).
    assert backend.get_workstream("ws-lapsed")["lease_node_id"] == "node-a"
    assert _lease_state(backend, "ws-kept").updated == "2020-01-01T00:00:00"
    assert backend.renew_workstream_leases("node-b/2", [kept, lapsed], ttl_seconds=30.0) == set()
    assert backend.renew_workstream_leases("node-a/1", [], ttl_seconds=30.0) == set()


def test_postgresql_renewal_skips_a_locked_row_without_losing_it(storage_backend: Any) -> None:
    """Renewal must not wait behind a long transaction while holding other rows."""
    backend = storage_backend
    if _is_sqlite(backend):
        pytest.skip("SQLite has no row locks; BEGIN IMMEDIATE serializes writers")
    _register(backend, "ws-busy")
    _register(backend, "ws-free")
    busy = _acquire(backend, "ws-busy")
    free = _acquire(backend, "ws-free")
    before = {
        ws_id: _lease_state(backend, ws_id).lease_expires_ms for ws_id in (busy.ws_id, free.ws_id)
    }
    results: list[set[str]] = []

    def renew() -> None:
        results.append(backend.renew_workstream_leases("node-a/1", [busy, free], ttl_seconds=600.0))

    with backend._engine.connect() as blocker:
        # A long transaction (a fork clone, a large delete) holds the row.
        blocker.execute(
            sa.select(workstreams.c.ws_id)
            .where(workstreams.c.ws_id == busy.ws_id)
            .with_for_update()
        )
        worker = threading.Thread(target=renew, name="lease-renewal-probe")
        worker.start()
        worker.join(timeout=10)
        finished_while_locked = not worker.is_alive()
        blocker.rollback()
    worker.join(timeout=10)

    assert finished_while_locked, "renewal waited on the locked row"
    # The busy lease is still owned (not reported lost), just not extended.
    assert results == [{busy.ws_id, free.ws_id}]
    assert _lease_state(backend, free.ws_id).lease_expires_ms > before[free.ws_id]
    assert _lease_state(backend, busy.ws_id).lease_expires_ms == before[busy.ws_id]


def test_renew_batches_large_holder_sets(
    storage_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    backend = storage_backend
    monkeypatch.setattr(_lease, "RENEW_BATCH_SIZE", 2)
    fences = []
    for index in range(5):
        ws_id = f"ws-batch-{index}"
        _register(backend, ws_id)
        fences.append(_acquire(backend, ws_id))
    backend_module = sys.modules[type(backend).__module__]
    renew_batch = backend_module.renew_lease_batch_on_connection
    batch_sizes: list[int] = []

    def counting(conn: Any, *, holder: str, batch: Any, ttl_ms: int) -> Any:
        batch_sizes.append(len(batch))
        return renew_batch(conn, holder=holder, batch=batch, ttl_ms=ttl_ms)

    monkeypatch.setattr(backend_module, "renew_lease_batch_on_connection", counting)

    assert backend.renew_workstream_leases("node-a/1", fences, ttl_seconds=30.0) == {
        fence.ws_id for fence in fences
    }
    assert batch_sizes == [2, 2, 1]


def test_release_is_exact_and_keeps_the_epoch(storage_backend: Any) -> None:
    backend = storage_backend
    _register(backend, "ws-release")
    first = _acquire(backend, "ws-release")

    assert backend.release_workstream_lease(first) is True
    holder, node, epoch, expires, _updated = _lease_state(backend, "ws-release")
    assert (holder, node, epoch, expires) == (None, None, 1, None)
    assert backend.release_workstream_lease(first) is False
    # A released fence never matches again, even for the same holder.
    with pytest.raises(WorkstreamLeaseLostError):
        backend.update_workstream_title("ws-release", "late", lease=first)

    second = _acquire(backend, "ws-release", holder="node-b/2")
    assert second.epoch == 2
    assert backend.release_workstream_lease(first) is False
    assert _lease_state(backend, "ws-release").lease_holder == "node-b/2"


def test_a_fence_never_matches_a_later_incarnation_of_the_same_id(storage_backend: Any) -> None:
    """A re-registered row restarts at epoch 0, so the token must travel too."""
    backend = storage_backend
    _register(backend, "ws-reborn", token="first-incarnation")
    old = _acquire(backend, "ws-reborn", token="first-incarnation")
    assert backend.delete_workstream("ws-reborn", lease=old) is True
    _register(backend, "ws-reborn", token="second-incarnation")
    new = _acquire(backend, "ws-reborn", token="second-incarnation")
    assert (new.holder, new.epoch) == (old.holder, old.epoch)

    with pytest.raises(WorkstreamLeaseLostError):
        backend.save_message("ws-reborn", "assistant", "old", commit_key=_key(), lease=old)
    assert backend.release_workstream_lease(old) is False
    backend.save_message("ws-reborn", "assistant", "new", commit_key=_key(), lease=new)
    assert backend.count_messages("ws-reborn") == 1


# -- Write admission ---------------------------------------------------------

Write = Callable[[Any, str, "LeaseFence | None"], Any]

_ATTACHMENT_ID = "a" * 64

SESSION_WRITES: dict[str, Write] = {
    "save_message_keyed": lambda b, ws, lease: b.save_message(
        ws, "assistant", "reply", commit_key=_key(), lease=lease
    ),
    "save_message_unkeyed": lambda b, ws, lease: b.save_message(
        ws, "assistant", "reply", lease=lease
    ),
    "save_user_message_with_attachments": lambda b, ws, lease: b.save_user_message_with_attachments(
        ws,
        "look",
        [make_attachment(_ATTACHMENT_ID, b"user bytes")],
        commit_key=_key(),
        lease=lease,
    ),
    "save_tool_message_with_attachments": lambda b, ws, lease: b.save_tool_message_with_attachments(
        ws,
        "result",
        "read_file",
        "call-1",
        [make_attachment("b" * 64, b"tool bytes")],
        commit_key=_key(),
        lease=lease,
    ),
    "truncate_messages_tail": lambda b, ws, lease: b.truncate_messages_tail(ws, 0, lease=lease),
    "delete_messages_after": lambda b, ws, lease: b.delete_messages_after(ws, 100, lease=lease),
    "set_message_attachments": lambda b, ws, lease: b.set_message_attachments(
        ws, 1, [_ATTACHMENT_ID], lease=lease
    ),
    "save_workstream_config": lambda b, ws, lease: b.save_workstream_config(
        ws, {"model_alias": "m"}, lease=lease
    ),
    "update_workstream_state": lambda b, ws, lease: b.update_workstream_state(
        ws, "running", lease=lease
    ),
    "update_workstream_title": lambda b, ws, lease: b.update_workstream_title(
        ws, "title", lease=lease
    ),
    "set_workstream_alias": lambda b, ws, lease: b.set_workstream_alias(
        ws, f"alias-{ws}", lease=lease
    ),
    "update_workstream_name": lambda b, ws, lease: b.update_workstream_name(
        ws, "name", lease=lease
    ),
    "end_foreign_node_watches": lambda b, ws, lease: b.end_foreign_node_watches(
        ws, "node-new", lease=lease
    ),
}


@pytest.mark.parametrize("write_name", sorted(SESSION_WRITES))
def test_session_owned_write_admission(storage_backend: Any, write_name: str) -> None:
    backend = storage_backend
    write = SESSION_WRITES[write_name]
    ws_id = f"ws-{write_name.replace('_', '-')}"
    _register(backend, ws_id)
    first = _acquire(backend, ws_id)

    # The holder's current fence writes.
    write(backend, ws_id, first)

    # Any newer acquisition (here the same holder's) retires the old fence.
    current = _acquire(backend, ws_id)
    with pytest.raises(WorkstreamLeaseLostError):
        write(backend, ws_id, first)
    write(backend, ws_id, current)

    # An unfenced (offline) write is refused while the lease is live.
    with pytest.raises(WorkstreamLeaseHeldError) as excinfo:
        write(backend, ws_id, None)
    assert excinfo.value.holder_node_id == "node-a"

    # After expiry it proceeds, and the same transaction fences the holder out.
    _expire(backend, ws_id)
    write(backend, ws_id, None)
    holder, _node, epoch, _expires, _updated = _lease_state(backend, ws_id)
    assert (holder, epoch) == (None, current.epoch + 1)
    with pytest.raises(WorkstreamLeaseLostError):
        write(backend, ws_id, current)


def test_a_failed_admission_writes_nothing(storage_backend: Any) -> None:
    backend = storage_backend
    _register(backend, "ws-refused")
    stale = _acquire(backend, "ws-refused")
    _acquire(backend, "ws-refused")

    with pytest.raises(WorkstreamLeaseLostError):
        backend.save_message("ws-refused", "assistant", "late", commit_key=_key(), lease=stale)
    with pytest.raises(WorkstreamLeaseHeldError):
        backend.save_workstream_config("ws-refused", {"model_alias": "m"})
    with pytest.raises(WorkstreamLeaseLostError):
        backend.update_workstream_state("ws-refused", "running", lease=stale)

    assert backend.count_messages("ws-refused") == 0
    assert backend.load_workstream_config("ws-refused") == {}
    assert backend.get_workstream("ws-refused")["state"] == "idle"


# What each session-owned write does, fenced, once its workstream row is gone.
# Keyed conversation saves raise Gone (the journal's terminal "deleted" arm);
# unkeyed saves, configuration and the tail delete refuse with LeaseLost (the
# fence names a row that can no longer honor it); column updates of the
# missing row, attachment lists included, stay no-ops (the alias update
# reports True, as it does unfenced), and ending foreign watches ends nothing;
# truncation keeps its plain RuntimeError.
MISSING_PARENT_OUTCOMES: dict[str, type[Exception] | None] = {
    "delete_messages_after": WorkstreamLeaseLostError,
    "end_foreign_node_watches": None,
    "save_message_keyed": ConversationCommitWorkstreamGoneError,
    "save_message_unkeyed": WorkstreamLeaseLostError,
    "save_tool_message_with_attachments": ConversationCommitWorkstreamGoneError,
    "save_user_message_with_attachments": ConversationCommitWorkstreamGoneError,
    "save_workstream_config": WorkstreamLeaseLostError,
    "set_message_attachments": None,
    "set_workstream_alias": None,
    "truncate_messages_tail": RuntimeError,
    "update_workstream_name": None,
    "update_workstream_state": None,
    "update_workstream_title": None,
}


def test_every_session_write_declares_its_missing_parent_outcome() -> None:
    assert MISSING_PARENT_OUTCOMES.keys() == SESSION_WRITES.keys()


@pytest.mark.parametrize("write_name", sorted(SESSION_WRITES))
def test_a_fence_cannot_write_under_a_missing_parent(storage_backend: Any, write_name: str) -> None:
    backend = storage_backend
    ws_id = f"ws-gone-{write_name.replace('_', '-')}"
    _register(backend, ws_id)
    fence = _acquire(backend, ws_id)
    assert backend.delete_workstream(ws_id, lease=fence) is True
    expected = MISSING_PARENT_OUTCOMES[write_name]

    if expected is None:
        SESSION_WRITES[write_name](backend, ws_id, fence)
    else:
        with pytest.raises(Exception) as excinfo:
            SESSION_WRITES[write_name](backend, ws_id, fence)
        assert type(excinfo.value) is expected

    assert backend.get_workstream(ws_id) is None
    assert backend.count_messages(ws_id) == 0
    assert backend.load_workstream_config(ws_id) == {}
    assert backend.list_orphan_conversations() == []


def _watch(backend: Any, ws_id: str, watch_id: str, node_id: str) -> None:
    backend.create_watch(
        watch_id=watch_id,
        ws_id=ws_id,
        node_id=node_id,
        name=f"watch on {node_id or 'no node'}",
        command="echo hi",
        interval_secs=60,
        stop_on=None,
        max_polls=10,
        created_by="model",
        next_poll="2099-01-01T00:00:00",
    )


def test_ending_foreign_watches_ends_only_other_nodes_watches(storage_backend: Any) -> None:
    backend = storage_backend
    _register(backend, "ws-moved")
    _watch(backend, "ws-moved", "w-old", "node-old")
    _watch(backend, "ws-moved", "w-new", "node-new")
    _watch(backend, "ws-moved", "w-none", "")
    stale = _acquire(backend, "ws-moved")
    fence = _acquire(backend, "ws-moved")

    with pytest.raises(WorkstreamLeaseLostError):
        backend.end_foreign_node_watches("ws-moved", "node-new", lease=stale)
    assert backend.get_watch("w-old")["active"]

    ended = backend.end_foreign_node_watches("ws-moved", "node-new", lease=fence)

    assert [(w["watch_id"], w["node_id"], w["command"]) for w in ended] == [
        ("w-old", "node-old", "echo hi")
    ]
    assert not backend.get_watch("w-old")["active"]
    assert backend.get_watch("w-old")["next_poll"] == ""
    assert backend.get_watch("w-new")["active"]
    assert backend.get_watch("w-none")["active"]
    assert backend.end_foreign_node_watches("ws-moved", "node-new", lease=fence) == []


def test_a_fence_for_another_workstream_is_a_caller_bug(storage_backend: Any) -> None:
    backend = storage_backend
    _register(backend, "ws-one")
    _register(backend, "ws-two")
    fence = _acquire(backend, "ws-one")

    with pytest.raises(ValueError, match="different workstream"):
        backend.update_workstream_title("ws-two", "crossed", lease=fence)


def test_bulk_import_refuses_a_batch_touching_a_live_lease(storage_backend: Any) -> None:
    backend = storage_backend
    _register(backend, "ws-bulk-free")
    _register(backend, "ws-bulk-leased")
    _acquire(backend, "ws-bulk-leased")
    rows = [
        {"ws_id": "ws-bulk-free", "role": "user", "content": "one"},
        {"ws_id": "ws-bulk-leased", "role": "user", "content": "two"},
    ]

    with pytest.raises(WorkstreamLeaseHeldError):
        backend.save_messages_bulk(rows)
    assert backend.count_messages("ws-bulk-free") == 0

    _expire(backend, "ws-bulk-leased")
    backend.save_messages_bulk(rows)
    assert backend.count_messages("ws-bulk-free") == 1
    assert backend.count_messages("ws-bulk-leased") == 1
    assert _lease_state(backend, "ws-bulk-leased").lease_holder is None


# -- Lifecycle ---------------------------------------------------------------


@pytest.mark.parametrize(
    "method",
    ["delete_workstream_if_fork_reserved", "finalize_deferred_create", "publish_deferred_create"],
)
def test_token_guarded_lifecycle_checks_the_incarnation_before_the_lease(
    storage_backend: Any, method: str
) -> None:
    """A stale incarnation keeps its documented False, not the owner's 409."""
    backend = storage_backend
    state = "idle" if method == "delete_workstream_if_fork_reserved" else "creating"
    ws_id = f"ws-replaced-{method}"
    _register(backend, ws_id, state=state)
    _acquire(backend, ws_id, holder="node-b/2", allow_creating=True)

    assert getattr(backend, method)(ws_id, "old-incarnation") is False
    row = backend.get_workstream(ws_id)
    assert row is not None and row["state"] == state


def test_creator_fence_carries_a_create_through_finalize_and_publish(
    storage_backend: Any,
) -> None:
    backend = storage_backend
    token = _register(backend, "ws-create", state="creating")
    fence = _acquire(backend, "ws-create", allow_creating=True)

    with pytest.raises(WorkstreamLeaseHeldError):
        backend.finalize_deferred_create("ws-create", token, config={"model_alias": "m"})
    assert backend.finalize_deferred_create(
        "ws-create", token, config={"model_alias": "m"}, lease=fence
    )
    with pytest.raises(WorkstreamLeaseHeldError):
        backend.publish_deferred_create("ws-create", token)
    assert backend.publish_deferred_create("ws-create", "old-incarnation", lease=fence) is False
    assert backend.publish_deferred_create("ws-create", token, lease=fence) is True

    assert backend.get_workstream("ws-create")["state"] == "idle"
    assert backend.renew_workstream_leases("node-a/1", [fence], ttl_seconds=30.0) == {"ws-create"}


def test_prune_never_removes_a_workstream_with_a_live_lease(storage_backend: Any) -> None:
    backend = storage_backend
    for ws_id in ("ws-prune-live", "ws-prune-expired", "ws-prune-free"):
        _register(backend, ws_id)
        _force_updated(backend, ws_id, "2020-01-01T00:00:00")
    _acquire(backend, "ws-prune-live")
    _acquire(backend, "ws-prune-expired")
    _expire(backend, "ws-prune-expired")

    orphans, stale = backend.prune_workstreams(retention_days=30)

    assert (orphans, stale) == (2, 0)
    assert backend.get_workstream("ws-prune-live") is not None
    assert backend.get_workstream("ws-prune-expired") is None
    assert backend.get_workstream("ws-prune-free") is None


def test_a_lease_lasts_its_ttl_in_database_milliseconds(storage_backend: Any) -> None:
    """Pins the clock's unit and epoch and the TTL's: a seconds-scale clock or a
    unit slip would leave relative assertions green while leases last hours."""
    import time

    ws_id = "ws-ttl-scale"
    _register(storage_backend, ws_id)
    _acquire(storage_backend, ws_id)
    with storage_backend._engine.connect() as conn:
        row = conn.execute(
            sa.select(
                workstreams.c.lease_expires_ms,
                _lease.db_now_ms(conn.dialect.name).label("now_ms"),
            ).where(workstreams.c.ws_id == ws_id)
        ).one()

    assert 20_000 <= int(row.lease_expires_ms) - int(row.now_ms) <= 30_000
    assert abs(int(row.now_ms) - time.time() * 1000) < 120_000


LifecycleWrite = Callable[[Any, str, str, "LeaseFence | None"], Any]

# Token-guarded lifecycle writes: the state a row starts in, whether the write
# deletes the row, and the write.
LIFECYCLE_WRITES: dict[str, tuple[str, bool, LifecycleWrite]] = {
    "delete_workstream": (
        "idle",
        True,
        lambda b, ws, _token, lease: b.delete_workstream(ws, lease=lease),
    ),
    "delete_workstream_if_fork_reserved": (
        "idle",
        True,
        lambda b, ws, token, lease: b.delete_workstream_if_fork_reserved(ws, token, lease=lease),
    ),
    "finalize_deferred_create": (
        "creating",
        False,
        lambda b, ws, token, lease: b.finalize_deferred_create(
            ws, token, config={"model_alias": "m"}, lease=lease
        ),
    ),
    "publish_deferred_create": (
        "creating",
        False,
        lambda b, ws, token, lease: b.publish_deferred_create(ws, token, lease=lease),
    ),
}


@pytest.mark.parametrize("method", sorted(LIFECYCLE_WRITES))
def test_lifecycle_write_admission(storage_backend: Any, method: str) -> None:
    backend = storage_backend
    state, deletes_row, write = LIFECYCLE_WRITES[method]
    ws_id = f"ws-life-{method.replace('_', '-')}"
    token = _register(backend, ws_id, state=state)
    stale = _acquire(backend, ws_id, allow_creating=True)
    current = _acquire(backend, ws_id, allow_creating=True)

    with pytest.raises(WorkstreamLeaseLostError):
        write(backend, ws_id, token, stale)
    with pytest.raises(WorkstreamLeaseHeldError):
        write(backend, ws_id, token, None)
    assert backend.get_workstream(ws_id)["state"] == state
    assert write(backend, ws_id, token, current) is True
    if deletes_row:
        assert backend.get_workstream(ws_id) is None

    # On an expired lease an unfenced write proceeds and fences the holder out.
    expired_id = f"{ws_id}-expired"
    expired_token = _register(backend, expired_id, state=state)
    expired = _acquire(backend, expired_id, allow_creating=True)
    _expire(backend, expired_id)
    assert write(backend, expired_id, expired_token, None) is True
    if deletes_row:
        assert backend.get_workstream(expired_id) is None
    else:
        holder, _node, epoch, _expires, _updated = _lease_state(backend, expired_id)
        assert (holder, epoch) == (None, expired.epoch + 1)


@pytest.mark.parametrize("method", sorted(LIFECYCLE_WRITES))
def test_a_fenced_lifecycle_write_on_a_missing_row_returns_false(
    storage_backend: Any, method: str
) -> None:
    backend = storage_backend
    state, _deletes_row, write = LIFECYCLE_WRITES[method]
    ws_id = f"ws-gone-{method.replace('_', '-')}"
    token = _register(backend, ws_id, state=state)
    fence = _acquire(backend, ws_id, allow_creating=True)
    assert backend.delete_workstream(ws_id, lease=fence) is True

    assert write(backend, ws_id, token, fence) is False
    assert backend.get_workstream(ws_id) is None


def test_every_protocol_method_taking_a_lease_is_tabled() -> None:
    """A new fenced write cannot skip the admission and missing-parent tables."""
    import inspect

    from turnstone.core.storage._protocol import StorageBackend

    taking = {
        name
        for name, member in inspect.getmembers(StorageBackend, inspect.isfunction)
        if "lease" in inspect.signature(member).parameters
    }
    tabled = {
        key.removesuffix("_keyed").removesuffix("_unkeyed") for key in SESSION_WRITES
    } | LIFECYCLE_WRITES.keys()
    assert taking == tabled
