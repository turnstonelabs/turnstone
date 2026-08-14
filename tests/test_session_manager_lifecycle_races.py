"""Deterministic lifecycle races around worker and session admission."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

import pytest

from tests.test_session_manager import FakeAdapter, FakeSession, FakeStorage, _make_manager
from turnstone.core import session_worker
from turnstone.core.session import ChatSession
from turnstone.core.session_manager import SessionManager
from turnstone.core.state_writer import StateWriter
from turnstone.core.workstream import WorkstreamState

if TYPE_CHECKING:
    from collections.abc import Callable


class _AttemptSignallingLock:
    """A normal lock that exposes when the second caller starts waiting."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count_lock = threading.Lock()
        self._attempts = 0
        self.second_attempted = threading.Event()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        with self._count_lock:
            self._attempts += 1
            if self._attempts == 2:
                self.second_attempted.set()
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> _AttemptSignallingLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class _BlockingErrorStorage(FakeStorage):
    def __init__(self) -> None:
        super().__init__()
        self.error_write_entered = threading.Event()
        self.release_error_write = threading.Event()
        self._blocked_error = False

    def update_workstream_state(self, ws_id: str, state: str) -> None:
        if state == "error" and not self._blocked_error:
            self._blocked_error = True
            self.error_write_entered.set()
            assert self.release_error_write.wait(timeout=10), "test did not release state write"
        super().update_workstream_state(ws_id, state)


class _RecordingStateWriter(StateWriter):
    def __init__(self, storage: FakeStorage) -> None:
        super().__init__(storage, flush_interval=60.0)
        self.lifecycle_order: list[str] = []

    def discard(
        self,
        ws_id: str,
        *,
        flush_lock_timeout: float = 5.0,
        tombstone: bool = False,
        incarnation: int | None = None,
    ) -> bool:
        result = super().discard(
            ws_id,
            flush_lock_timeout=flush_lock_timeout,
            tombstone=tombstone,
            incarnation=incarnation,
        )
        if tombstone:
            self.lifecycle_order.append("tombstone")
        return result


class _RaisingDiscardStateWriter(StateWriter):
    def discard(
        self,
        ws_id: str,
        *,
        flush_lock_timeout: float = 5.0,
        tombstone: bool = False,
        incarnation: int | None = None,
    ) -> bool:
        if tombstone:
            raise RuntimeError("discard forced failure")
        return super().discard(
            ws_id,
            flush_lock_timeout=flush_lock_timeout,
            tombstone=tombstone,
            incarnation=incarnation,
        )


class _DurabilitySession(FakeSession):
    """Fake resource shell using ChatSession's real durability lane."""

    def __init__(self, ws_id: str) -> None:
        super().__init__(ws_id)
        self._generation_lock = threading.RLock()
        self._publication_shutdown = False
        self._cancel_event = threading.Event()
        self._durability_cond = threading.Condition(threading.Lock())
        self._durability_next_ticket = 0
        self._durability_serving_ticket = 0
        self.shutdown_entered = threading.Event()

    def commit_durable(self, persist: Callable[[], None]) -> bool:
        def _admit(durable: list[Callable[[], None]]) -> None:
            durable.append(persist)

        return ChatSession._commit_for_generation(  # type: ignore[arg-type]
            self,
            0,
            _admit,
        )

    def shutdown_publication_and_drain_durability(self) -> None:
        self.shutdown_entered.set()
        ChatSession.shutdown_publication_and_drain_durability(self)  # type: ignore[arg-type]


class _ReplaceAfterSnapshotStorage(FakeStorage):
    """Replace A immediately after returning its first open snapshot."""

    def __init__(self, ws_id: str, replacement_token: str) -> None:
        super().__init__()
        self.ws_id = ws_id
        self.replacement_token = replacement_token
        self.replaced = False

    def ensure_workstream_incarnation_snapshot(self, ws_id: str) -> dict[str, Any] | None:
        snapshot = super().ensure_workstream_incarnation_snapshot(ws_id)
        if snapshot is not None and ws_id == self.ws_id and not self.replaced:
            self.replaced = True
            self.delete_workstream(ws_id)
            self.register_workstream(
                ws_id,
                user_id="owner-b",
                name="row-b",
                kind="interactive",
                fork_reservation_token=self.replacement_token,
            )
            self.ws_config[ws_id] = {"model_alias": "model-b"}
        return snapshot


class _BlockingIncarnationSnapshotStorage(FakeStorage):
    def __init__(self) -> None:
        super().__init__()
        self.block = False
        self.snapshot_entered = threading.Event()
        self.release_snapshot = threading.Event()

    def ensure_workstream_incarnation_snapshot(self, ws_id: str) -> dict[str, Any] | None:
        if self.block:
            self.snapshot_entered.set()
            assert self.release_snapshot.wait(timeout=10), "test did not release snapshot"
        return super().ensure_workstream_incarnation_snapshot(ws_id)


class _BlockingCloseAdapter(FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.close_entered = threading.Event()
        self.release_close = threading.Event()

    def emit_closed(
        self,
        ws_id: str,
        *,
        reason: str = "closed",
        name: str = "",
    ) -> None:
        self.close_entered.set()
        assert self.release_close.wait(timeout=10), "test did not release close publication"
        super().emit_closed(ws_id, reason=reason, name=name)


class _AcquireProbe:
    """Expose the instant a caller tries to enter an underlying lock."""

    def __init__(self, lock: object, attempted: threading.Event) -> None:
        self._lock = lock
        self._attempted = attempted

    def acquire(self) -> bool:
        self._attempted.set()
        return self._lock.acquire()  # type: ignore[attr-defined,no-any-return]

    def release(self) -> None:
        self._lock.release()  # type: ignore[attr-defined]

    def __enter__(self) -> _AcquireProbe:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class _BlockingSoftCloseSession(FakeSession):
    """Expose the close-preparation window and its durability refusal."""

    def __init__(
        self,
        ws_id: str,
        *,
        prepare_result: bool = True,
        unresolved: bool = False,
    ) -> None:
        super().__init__(ws_id)
        self.prepare_result = prepare_result
        self.unresolved = unresolved
        self.prepare_entered = threading.Event()
        self.release_prepare = threading.Event()

    def has_unresolved_conversation_persistence(self) -> bool:
        return self.unresolved

    def prepare_soft_close(self) -> bool:
        self.prepare_entered.set()
        assert self.release_prepare.wait(timeout=10), "test did not release close preparation"
        if self.prepare_result:
            self.unresolved = False
        return self.prepare_result


def test_soft_close_fences_fresh_send_before_session_preparation() -> None:
    """A send crossing the close drain is refused before worker admission."""
    mgr, adapter, storage = _make_manager()
    ws = mgr.create(user_id="u1", name="closing")
    session = _BlockingSoftCloseSession(ws.id)
    ws.session = session  # type: ignore[assignment]
    close_results: list[bool] = []
    worker_ran = threading.Event()

    close_thread = threading.Thread(
        target=lambda: close_results.append(mgr.close(ws.id)),
        daemon=True,
    )
    close_thread.start()
    assert session.prepare_entered.wait(timeout=5), "close never entered session preparation"

    # ``prepare_soft_close`` is deliberately blocked. The dispatch tombstone
    # must already be visible under the worker's own admission lock; otherwise
    # the caller gets a false accepted response for a generation that the
    # session close fence will reject after the thread starts.
    assert session_worker.send(ws, enqueue=lambda: None, run=worker_ran.set) is False
    assert worker_ran.is_set() is False
    assert ws.worker_thread is None

    session.release_prepare.set()
    close_thread.join(timeout=5)

    assert not close_thread.is_alive()
    assert close_results == [True]
    assert mgr.get(ws.id) is None
    assert ws._closed is True
    assert storage.rows[ws.id].state == "closed"
    assert adapter.cleaned_up == [ws.id]


def test_unresolved_soft_close_retries_inside_dispatch_fence() -> None:
    """Recovery is attempted while fresh worker admission stays fenced."""
    mgr, adapter, storage = _make_manager()
    ws = mgr.create(user_id="u1", name="close-retry-unresolved")
    session = _BlockingSoftCloseSession(ws.id, unresolved=True)
    ws.session = session  # type: ignore[assignment]
    close_results: list[bool] = []

    close_thread = threading.Thread(
        target=lambda: close_results.append(mgr.close(ws.id)),
        daemon=True,
    )
    close_thread.start()
    assert session.prepare_entered.wait(timeout=5), "close never retried persistence"
    assert session_worker.send(ws, enqueue=lambda: None, run=lambda: None) is False

    session.release_prepare.set()
    close_thread.join(timeout=5)

    assert not close_thread.is_alive()
    assert close_results == [True]
    assert session.unresolved is False
    assert mgr.get(ws.id) is None
    assert ws._closed is True
    assert storage.rows[ws.id].state == "closed"
    assert adapter.cleaned_up == [ws.id]


def test_refused_soft_close_restores_fresh_dispatch() -> None:
    """A durability refusal rolls back only the workstream dispatch fence."""
    mgr, adapter, storage = _make_manager()
    ws = mgr.create(user_id="u1", name="close-refused")
    session = _BlockingSoftCloseSession(ws.id, prepare_result=False, unresolved=True)
    ws.session = session  # type: ignore[assignment]

    close_results: list[bool] = []
    close_thread = threading.Thread(
        target=lambda: close_results.append(mgr.close(ws.id)),
        daemon=True,
    )
    close_thread.start()
    assert session.prepare_entered.wait(timeout=5), "close never entered session preparation"
    assert session_worker.send(ws, enqueue=lambda: None, run=lambda: None) is False
    session.release_prepare.set()
    close_thread.join(timeout=5)
    assert not close_thread.is_alive()
    assert close_results == [False]

    assert mgr.get(ws.id) is ws
    assert ws._closed is False
    assert ws.id not in adapter.cleaned_up
    assert storage.rows[ws.id].state != "closed"

    worker_ran = threading.Event()
    assert session_worker.send(ws, enqueue=lambda: None, run=worker_ran.set) is True
    assert worker_ran.wait(timeout=5), "fresh worker did not run after close rollback"
    worker = ws.worker_thread
    assert worker is not None
    worker.join(timeout=5)
    assert not worker.is_alive()


def test_close_idle_does_not_retire_an_admitted_worker() -> None:
    """Worker admission makes an otherwise-IDLE workstream ineligible."""
    mgr, adapter, storage = _make_manager()
    ws = mgr.create(user_id="u1", name="worker-admitted")
    ws.last_active = time.monotonic() - 100
    worker_entered = threading.Event()
    release_worker = threading.Event()

    def _run() -> None:
        worker_entered.set()
        assert release_worker.wait(timeout=10), "test did not release worker"

    assert session_worker.send(ws, enqueue=lambda: None, run=_run) is True
    assert worker_entered.wait(timeout=5), "worker never started"
    with ws._lock:
        assert ws._worker_running is True
        assert ws.state is WorkstreamState.IDLE
        worker_thread = ws.worker_thread
    assert worker_thread is not None

    try:
        closed = mgr.close_idle(max_age_seconds=0)
    finally:
        release_worker.set()
    worker_thread.join(timeout=5)

    assert not worker_thread.is_alive()
    assert closed == []
    assert mgr.get(ws.id) is ws
    assert storage.rows[ws.id].state != "closed"
    assert [event.kind for event in adapter.events] == ["created"]
    assert adapter.cleaned_up == []


@pytest.mark.parametrize("admission", ["create", "open"])
@pytest.mark.parametrize("worker_kind", ["turn", "command"])
def test_capacity_admission_does_not_retire_an_idle_worker(
    admission: str,
    worker_kind: str,
) -> None:
    """Create/open capacity pressure loses to an admitted worker slot."""
    mgr, adapter, storage = _make_manager(max_active=1)
    incumbent = mgr.create(user_id="u1", name="incumbent")
    incumbent.last_active = time.monotonic() - 100
    probed_lock = _AttemptSignallingLock()
    incumbent._lock = probed_lock  # type: ignore[assignment]
    target_id = f"capacity-{admission}-{worker_kind}"
    if admission == "open":
        storage.register_workstream(
            target_id,
            user_id="u2",
            name="saved-target",
            kind=incumbent.kind,
        )

    results: list[object] = []
    errors: list[BaseException] = []

    def _admit() -> None:
        try:
            if admission == "create":
                results.append(mgr.create(ws_id=target_id, user_id="u2"))
            else:
                results.append(mgr.open(target_id))
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    # Hold the authoritative worker/lifecycle lock until capacity selection has
    # chosen the stale IDLE hint and is waiting to revalidate it. Installing the
    # worker claim before release deterministically makes eviction lose.
    probed_lock.acquire()
    admission_thread = threading.Thread(target=_admit, daemon=True)
    admission_thread.start()
    assert probed_lock.second_attempted.wait(timeout=5), "capacity path never revalidated victim"
    incumbent._worker_running = True
    incumbent.worker_kind = worker_kind  # type: ignore[assignment]
    probed_lock.release()
    admission_thread.join(timeout=5)

    assert not admission_thread.is_alive()
    assert results == []
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert mgr.get(incumbent.id) is incumbent
    assert incumbent._closed is False
    assert mgr.get(target_id) is None
    assert mgr.eviction_count == 0
    assert adapter.cleaned_up == []
    assert [(event.kind, event.reason) for event in adapter.events] == [("created", None)]


@pytest.mark.parametrize("admission", ["create", "open"])
@pytest.mark.parametrize("barrier_kind", ["pending", "claimed"])
def test_capacity_admission_does_not_retire_an_idle_send_barrier(
    admission: str,
    barrier_kind: str,
) -> None:
    """Acknowledged or drain-claimed sends make an IDLE slot ineligible."""
    mgr, adapter, storage = _make_manager(max_active=1)
    incumbent = mgr.create(user_id="u1", name="incumbent")
    incumbent.last_active = time.monotonic() - 100
    probed_lock = _AttemptSignallingLock()
    incumbent._lock = probed_lock  # type: ignore[assignment]
    target_id = f"capacity-{admission}-{barrier_kind}"
    if admission == "open":
        storage.register_workstream(
            target_id,
            user_id="u2",
            name="saved-target",
            kind=incumbent.kind,
        )

    results: list[object] = []
    errors: list[BaseException] = []
    release_drain = threading.Event()
    drain_thread: threading.Thread | None = None

    def _admit() -> None:
        try:
            if admission == "create":
                results.append(mgr.create(ws_id=target_id, user_id="u2"))
            else:
                results.append(mgr.open(target_id))
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    probed_lock.acquire()
    admission_thread = threading.Thread(target=_admit, daemon=True)
    admission_thread.start()
    assert probed_lock.second_attempted.wait(timeout=5), "capacity path never revalidated victim"
    if barrier_kind == "pending":
        incumbent._pending_sends.append(None)  # type: ignore[arg-type]
    else:
        drain_thread = threading.Thread(
            target=lambda: release_drain.wait(timeout=10),
            daemon=True,
        )
        drain_thread.start()
        incumbent._pending_drain = drain_thread
    probed_lock.release()
    admission_thread.join(timeout=5)

    try:
        assert not admission_thread.is_alive()
        assert results == []
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
        assert mgr.get(incumbent.id) is incumbent
        assert incumbent._closed is False
        assert mgr.get(target_id) is None
        assert mgr.eviction_count == 0
        assert adapter.cleaned_up == []
        assert [(event.kind, event.reason) for event in adapter.events] == [("created", None)]
    finally:
        release_drain.set()
        if drain_thread is not None:
            drain_thread.join(timeout=5)


def test_open_retries_when_durable_incarnation_changes_mid_rehydrate() -> None:
    """A snapshot-A/config-B hybrid is retired before it can be returned."""
    ws_id = "open-incarnation-aba"
    storage = _ReplaceAfterSnapshotStorage(ws_id, "token-b")
    storage.register_workstream(
        ws_id,
        user_id="owner-a",
        name="row-a",
        kind="interactive",
        fork_reservation_token="token-a",
    )
    storage.ws_config[ws_id] = {"model_alias": "model-a"}
    adapter = FakeAdapter()
    mgr = SessionManager(
        adapter,
        storage=storage,
        max_active=3,
        event_emitter=adapter,
    )

    reopened = mgr.open(ws_id)

    assert reopened is not None
    assert reopened.user_id == "owner-b"
    assert reopened.name == "row-b"
    assert reopened._fork_reservation_token == "token-b"
    assert reopened.session is not None
    assert adapter.build_models == ["model-b", "model-b"]
    assert len(adapter.built_sessions) == 2
    assert adapter.built_sessions[0].cancelled is True
    assert adapter.built_sessions[0].closed is True
    assert [(event.kind, event.ws_id) for event in adapter.events] == [("rehydrated", ws_id)]


def test_close_idle_racing_delete_persisted_has_one_deleted_terminal() -> None:
    """The idle sweep cannot soft-close through an admitted hard delete."""
    mgr, adapter, storage = _make_manager()
    ws = mgr.create(user_id="u1", name="delete-wins")
    ws.last_active = time.monotonic() - 100
    delete_entered = threading.Event()
    release_delete = threading.Event()
    idle_start = threading.Barrier(2)
    delete_results: list[bool] = []
    idle_results: list[list[str]] = []

    def _delete_row() -> bool:
        delete_entered.set()
        assert release_delete.wait(timeout=10), "test did not release durable delete"
        storage.delete_workstream(ws.id)
        return True

    def _delete() -> None:
        delete_results.append(mgr.delete_persisted(ws.id, delete_fn=_delete_row))

    def _close_idle() -> None:
        idle_start.wait(timeout=5)
        idle_results.append(mgr.close_idle(max_age_seconds=0))

    delete_thread = threading.Thread(target=_delete, daemon=True)
    idle_thread = threading.Thread(target=_close_idle, daemon=True)
    delete_thread.start()
    assert delete_entered.wait(timeout=5), "delete never acquired lifecycle admission"
    idle_thread.start()
    idle_start.wait(timeout=5)
    # Yield long enough for the idle sweeper either to contend on the exact
    # lifecycle lock or expose the old unlocked pop path.
    time.sleep(0.05)
    release_delete.set()
    delete_thread.join(timeout=5)
    idle_thread.join(timeout=5)

    assert not delete_thread.is_alive()
    assert not idle_thread.is_alive()
    assert delete_results == [True]
    assert idle_results == [[]]
    assert mgr.get(ws.id) is None
    assert ws.id not in storage.rows
    assert (ws.id, "closed") not in storage.state_updates
    assert [(event.kind, event.reason) for event in adapter.events] == [
        ("created", None),
        ("closed", "deleted"),
    ]
    assert adapter.cleaned_up == [ws.id]


def test_stale_delete_snapshot_does_not_tombstone_current_local_successor() -> None:
    """Request A loses to the locally loaded and durably current B."""
    mgr, adapter, storage = _make_manager()
    ws_id = "delete-request-stale"
    storage.register_workstream(
        ws_id,
        user_id="owner-a",
        kind="interactive",
        fork_reservation_token="token-a",
    )
    predecessor = mgr.open(ws_id)
    assert predecessor is not None
    assert mgr.close(ws_id) is True
    assert storage.delete_workstream_if_fork_reserved(ws_id, "token-a") is True
    storage.register_workstream(
        ws_id,
        user_id="owner-b",
        name="successor",
        kind="interactive",
        fork_reservation_token="token-b",
    )
    successor = mgr.open(ws_id)
    assert successor is not None
    delete_called = threading.Event()

    def _delete_a() -> bool:
        delete_called.set()
        return storage.delete_workstream_if_fork_reserved(ws_id, "token-a")

    assert (
        mgr.delete_persisted(
            ws_id,
            delete_fn=_delete_a,
            expected_reservation_token="token-a",
        )
        is False
    )

    assert not delete_called.is_set()
    assert mgr.get(ws_id) is successor
    assert successor._closed is False
    assert storage.rows[ws_id].name == "successor"
    assert adapter.cleaned_up == [ws_id]
    assert [(event.kind, event.reason) for event in adapter.events] == [
        ("rehydrated", None),
        ("closed", "closed"),
        ("rehydrated", None),
    ]


def test_current_delete_snapshot_retires_stale_local_predecessor() -> None:
    """Request B may delete durable B even while the manager still holds A."""
    mgr, adapter, storage = _make_manager()
    ws_id = "delete-local-stale"
    storage.register_workstream(
        ws_id,
        user_id="owner-a",
        name="predecessor",
        kind="interactive",
        fork_reservation_token="token-a",
    )
    predecessor = mgr.open(ws_id)
    assert predecessor is not None
    predecessor_session = predecessor.session
    assert storage.delete_workstream_if_fork_reserved(ws_id, "token-a") is True
    storage.register_workstream(
        ws_id,
        user_id="owner-b",
        name="successor",
        kind="interactive",
        fork_reservation_token="token-b",
    )

    assert (
        mgr.delete_persisted(
            ws_id,
            delete_fn=lambda: storage.delete_workstream_if_fork_reserved(
                ws_id,
                "token-b",
            ),
            expected_reservation_token="token-b",
            name="successor",
        )
        is True
    )

    assert mgr.get(ws_id) is None
    assert ws_id not in storage.rows
    assert isinstance(predecessor_session, FakeSession)
    assert predecessor_session.closed is True
    assert adapter.cleaned_up == [ws_id]
    assert [(event.kind, event.reason, event.name) for event in adapter.events] == [
        ("rehydrated", None, None),
        ("closed", "deleted", "successor"),
    ]


def test_delete_direction_snapshot_does_not_hold_global_manager_lock() -> None:
    """A blocked row lock for A must not convoy unrelated manager reads."""
    storage = _BlockingIncarnationSnapshotStorage()
    adapter = FakeAdapter()
    mgr = SessionManager(
        adapter,
        storage=storage,
        max_active=3,
        event_emitter=adapter,
    )
    ws_id = "delete-direction-blocked"
    storage.register_workstream(
        ws_id,
        user_id="owner-a",
        kind="interactive",
        fork_reservation_token="token-a",
    )
    predecessor = mgr.open(ws_id)
    assert predecessor is not None
    assert storage.delete_workstream_if_fork_reserved(ws_id, "token-a") is True
    storage.register_workstream(
        ws_id,
        user_id="owner-b",
        kind="interactive",
        fork_reservation_token="token-b",
    )
    storage.block = True
    delete_results: list[bool] = []
    delete_thread = threading.Thread(
        target=lambda: delete_results.append(
            mgr.delete_persisted(
                ws_id,
                delete_fn=lambda: storage.delete_workstream_if_fork_reserved(
                    ws_id,
                    "token-b",
                ),
                expected_reservation_token="token-b",
            )
        ),
        daemon=True,
    )
    delete_thread.start()
    assert storage.snapshot_entered.wait(timeout=5), "delete never reached durable snapshot"

    probe_results: list[object] = []
    probe_thread = threading.Thread(
        target=lambda: probe_results.append(mgr.list_all()),
        daemon=True,
    )
    probe_thread.start()
    probe_thread.join(timeout=1)
    try:
        assert not probe_thread.is_alive()
        assert probe_results == [[predecessor]]
    finally:
        storage.release_snapshot.set()
    delete_thread.join(timeout=5)

    assert not delete_thread.is_alive()
    assert delete_results == [True]


def test_delete_exception_retires_exact_object_and_allows_reopen() -> None:
    """A failed durable delete never leaves a poisoned tracked object."""
    mgr, adapter, storage = _make_manager()
    ws = mgr.create(user_id="u1", name="survives")

    def _raise_delete() -> bool:
        raise RuntimeError("delete forced failure")

    with pytest.raises(RuntimeError, match="delete forced failure"):
        mgr.delete_persisted(
            ws.id,
            delete_fn=_raise_delete,
            expected_reservation_token=ws._fork_reservation_token,
        )

    assert mgr.get(ws.id) is None
    assert ws.id in storage.rows
    assert ws._closed is True
    assert adapter.cleaned_up == [ws.id]
    reopened = mgr.open(ws.id)
    assert reopened is not None
    assert reopened is not ws
    assert reopened._closed is False
    assert reopened._fork_reservation_token == ws._fork_reservation_token
    assert [(event.kind, event.reason) for event in adapter.events] == [
        ("created", None),
        ("rehydrated", None),
    ]


def test_state_writer_discard_exception_retires_exact_object_and_allows_reopen() -> None:
    storage = FakeStorage()
    writer = _RaisingDiscardStateWriter(storage, flush_interval=60.0)
    adapter = FakeAdapter()
    mgr = SessionManager(
        adapter,
        storage=storage,
        max_active=3,
        state_writer=writer,
        event_emitter=adapter,
    )
    ws = mgr.create(user_id="u1", name="survives")

    with pytest.raises(RuntimeError, match="discard forced failure"):
        mgr.delete_persisted(
            ws.id,
            delete_fn=lambda: storage.delete_workstream_if_fork_reserved(
                ws.id,
                ws._fork_reservation_token,
            ),
            expected_reservation_token=ws._fork_reservation_token,
        )

    assert mgr.get(ws.id) is None
    assert ws.id in storage.rows
    reopened = mgr.open(ws.id)
    assert reopened is not None
    assert reopened is not ws
    assert reopened._closed is False


@pytest.mark.parametrize("terminal", ["close", "delete"])
def test_terminal_during_session_build_closes_late_session(terminal: str) -> None:
    """A builder that loses lifecycle ownership cannot leak its session."""
    mgr, adapter, storage = _make_manager()
    ws_id = f"build-race-{terminal}"
    build_entered = threading.Event()
    release_build = threading.Event()
    built_sessions: list[FakeSession] = []
    create_results: list[object] = []
    create_errors: list[BaseException] = []

    def _build(ws: object, _model: object | None) -> FakeSession:
        session = FakeSession(ws_id)
        built_sessions.append(session)
        build_entered.set()
        assert release_build.wait(timeout=10), "test did not release session build"
        return session

    def _create() -> None:
        try:
            create_results.append(mgr.create(ws_id=ws_id, user_id="u1", name="building"))
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            create_errors.append(exc)

    adapter.build_session_hook = _build  # type: ignore[assignment]
    create_thread = threading.Thread(target=_create, daemon=True)
    create_thread.start()
    assert build_entered.wait(timeout=5), "create never entered session build"
    try:
        if terminal == "close":
            assert mgr.close(ws_id) is True
        else:

            def _delete_row() -> bool:
                storage.delete_workstream(ws_id)
                return True

            assert mgr.delete_persisted(ws_id, delete_fn=_delete_row) is True
        assert mgr.count == 0
    finally:
        release_build.set()
    create_thread.join(timeout=5)

    assert not create_thread.is_alive()
    assert create_results == []
    assert len(create_errors) == 1
    assert isinstance(create_errors[0], RuntimeError)
    assert mgr.get(ws_id) is None
    assert ws_id not in storage.rows
    assert len(built_sessions) == 1
    assert adapter.built_sessions == built_sessions
    assert built_sessions[0].cancelled is True
    assert built_sessions[0].closed is True
    assert adapter.events == []


def test_delete_drains_and_tombstones_predecessor_state_before_same_id_successor() -> None:
    """An admitted old state write cannot cross hard-delete into its successor."""
    storage = _BlockingErrorStorage()
    writer = _RecordingStateWriter(storage)
    adapter = FakeAdapter()
    mgr = SessionManager(
        adapter,
        storage=storage,
        max_active=3,
        state_writer=writer,
        event_emitter=adapter,
    )
    ws_id = "state-delete-aba"
    predecessor = mgr.create(ws_id=ws_id, user_id="u1", name="predecessor")
    predecessor_incarnation = predecessor._state_incarnation

    state_lane = _AttemptSignallingLock()
    with mgr._lock:
        predecessor._state_tail_lock = state_lane  # type: ignore[assignment]
        mgr._state_tail_locks[ws_id] = state_lane  # type: ignore[assignment]

    state_thread = threading.Thread(
        target=mgr.set_state,
        args=(ws_id, WorkstreamState.ERROR),
        daemon=True,
    )
    state_thread.start()
    assert storage.error_write_entered.wait(timeout=5), "predecessor state write never blocked"

    durable_delete_called = threading.Event()
    delete_results: list[bool] = []

    def _delete_row() -> bool:
        writer.lifecycle_order.append("delete")
        storage.delete_workstream(ws_id)
        durable_delete_called.set()
        return True

    delete_thread = threading.Thread(
        target=lambda: delete_results.append(mgr.delete_persisted(ws_id, delete_fn=_delete_row)),
        daemon=True,
    )
    delete_thread.start()
    assert state_lane.second_attempted.wait(timeout=5), "delete never waited on state tail"
    assert not durable_delete_called.is_set()

    storage.release_error_write.set()
    state_thread.join(timeout=5)
    delete_thread.join(timeout=5)

    assert not state_thread.is_alive()
    assert not delete_thread.is_alive()
    assert delete_results == [True]
    assert writer.lifecycle_order == ["tombstone", "delete"]
    assert ws_id not in storage.rows

    successor = mgr.create(ws_id=ws_id, user_id="u2", name="successor")
    assert successor._state_incarnation != predecessor_incarnation
    storage.state_updates.clear()

    # Model a predecessor closure arriving after the successor's reopen. The
    # writer must reject its old incarnation instead of touching the new row.
    writer.record(
        ws_id,
        WorkstreamState.RUNNING.value,
        flush_now=True,
        incarnation=predecessor_incarnation,
    )

    assert storage.state_updates == []
    assert storage.rows[ws_id].name == "successor"
    assert storage.rows[ws_id].state == "idle"


def test_delete_drains_admitted_conversation_write_and_fences_same_id_successor(
    storage_backend: Any,
) -> None:
    """An accepted save drains before delete; its closed lane cannot hit a successor."""
    backend = storage_backend
    adapter = FakeAdapter()
    mgr = SessionManager(
        adapter,
        storage=backend,
        max_active=3,
        event_emitter=adapter,
    )
    ws_id = "conversation-delete-aba"
    predecessor = mgr.create(ws_id=ws_id, user_id="u1", name="predecessor")
    session = _DurabilitySession(ws_id)
    predecessor.session = session  # type: ignore[assignment]
    persist_entered = threading.Event()
    release_persist = threading.Event()

    def _persist_predecessor() -> None:
        persist_entered.set()
        assert release_persist.wait(timeout=10), "test did not release conversation write"
        backend.save_message(ws_id, "user", "late predecessor")

    commit_results: list[bool] = []
    commit_thread = threading.Thread(
        target=lambda: commit_results.append(session.commit_durable(_persist_predecessor)),
        daemon=True,
    )
    commit_thread.start()
    assert persist_entered.wait(timeout=5), "durability batch never started"

    delete_called = threading.Event()
    delete_results: list[bool] = []

    def _delete_exact() -> bool:
        delete_called.set()
        return backend.delete_workstream_if_fork_reserved(
            ws_id,
            predecessor._fork_reservation_token,
        )

    delete_thread = threading.Thread(
        target=lambda: delete_results.append(
            mgr.delete_persisted(
                ws_id,
                delete_fn=_delete_exact,
                expected_reservation_token=predecessor._fork_reservation_token,
            )
        ),
        daemon=True,
    )
    delete_thread.start()
    assert session.shutdown_entered.wait(timeout=5), "delete never closed durable admission"
    assert not delete_called.is_set()

    release_persist.set()
    commit_thread.join(timeout=5)
    delete_thread.join(timeout=5)

    assert not commit_thread.is_alive()
    assert not delete_thread.is_alive()
    assert commit_results == [True]
    assert delete_results == [True]
    assert delete_called.is_set()

    successor = mgr.create(ws_id=ws_id, user_id="u2", name="successor")
    assert successor.user_id == "u2"
    assert successor.name == "successor"
    assert backend.load_message_turns(ws_id) == []
    assert (
        session.commit_durable(
            lambda: backend.save_message(ws_id, "user", "post-shutdown predecessor")
        )
        is False
    )
    assert backend.load_message_turns(ws_id) == []


def test_same_id_successor_created_waits_for_predecessor_closed_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-ID lifecycle ordering keeps old closed before successor created."""
    storage = FakeStorage()
    adapter = _BlockingCloseAdapter()
    mgr = SessionManager(
        adapter,
        storage=storage,
        max_active=3,
        event_emitter=adapter,
    )
    ws_id = "event-delete-aba"
    predecessor = mgr.create(ws_id=ws_id, user_id="u1", name="predecessor")
    delete_results: list[bool] = []

    def _delete_row() -> bool:
        storage.delete_workstream(ws_id)
        return True

    delete_thread = threading.Thread(
        target=lambda: delete_results.append(mgr.delete_persisted(ws_id, delete_fn=_delete_row)),
        daemon=True,
    )
    delete_thread.start()
    assert adapter.close_entered.wait(timeout=5), "predecessor close never reached emitter"

    successor_lock_attempted = threading.Event()
    acquire_lifecycle = mgr._acquire_open_lock

    def _acquire_with_probe(candidate_id: str) -> _AcquireProbe:
        return _AcquireProbe(acquire_lifecycle(candidate_id), successor_lock_attempted)

    monkeypatch.setattr(mgr, "_acquire_open_lock", _acquire_with_probe)
    successors: list[object] = []
    successor_errors: list[BaseException] = []

    def _create_successor() -> None:
        try:
            successors.append(mgr.create(ws_id=ws_id, user_id="u2", name="successor"))
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            successor_errors.append(exc)

    create_thread = threading.Thread(target=_create_successor, daemon=True)
    create_thread.start()
    try:
        assert successor_lock_attempted.wait(timeout=5), "successor never reached lifecycle lane"

        # The replacement has observed the durable gap but must still wait for
        # the old terminal event; no second ws_created may overtake ws_closed.
        assert successors == []
        assert [(event.kind, event.ws_id) for event in adapter.events] == [
            ("created", predecessor.id),
        ]
    finally:
        adapter.release_close.set()
    delete_thread.join(timeout=5)
    create_thread.join(timeout=5)

    assert not delete_thread.is_alive()
    assert not create_thread.is_alive()
    assert delete_results == [True]
    assert successor_errors == []
    assert len(successors) == 1
    assert [(event.kind, event.ws_id) for event in adapter.events] == [
        ("created", ws_id),
        ("closed", ws_id),
        ("created", ws_id),
    ]


def test_retirement_probe_never_blocks_on_held_session_locks(tmp_db: str) -> None:
    """Round-4 review pin (AB/BA deadlock): the idle-close and eviction scans
    probe persistence while holding ``ws._lock``, and force-cancel's finalizer
    holds the generation lock and then takes ``ws._lock`` — so the retirement
    probe must never BLOCK on the session's generation/handoff locks. A held
    lock reads as busy → not retirable this sweep (True), never a hang.
    """
    from tests._session_helpers import make_session
    from turnstone.core.session_manager import _session_persistence_blocks_retirement

    session = make_session()
    # Free locks: a clean session is retirable...
    assert _session_persistence_blocks_retirement(session) is False
    # ...and a pending journal row blocks retirement.
    with session._history_handoff_lock:
        session._journal_conversation_row_locked(
            commit_key="probe-key",
            message={"role": "system", "content": "accepted overlay"},
            persist=lambda: 0,
            event_id=None,
        )
    assert _session_persistence_blocks_retirement(session) is True

    for lock_name in ("_generation_lock", "_history_handoff_lock"):
        hold = threading.Event()
        release = threading.Event()
        lock = getattr(session, lock_name)

        def _holder(
            lock: Any = lock,
            hold: threading.Event = hold,
            release: threading.Event = release,
        ) -> None:
            with lock:
                hold.set()
                release.wait(5)

        holder = threading.Thread(target=_holder, daemon=True)
        holder.start()
        assert hold.wait(2)
        outcome: list[bool] = []
        prober = threading.Thread(
            target=lambda out=outcome: out.append(_session_persistence_blocks_retirement(session)),
            daemon=True,
        )
        prober.start()
        prober.join(2)
        still_running = prober.is_alive()
        release.set()
        holder.join(2)
        assert not still_running, f"probe blocked on a held {lock_name}"
        assert outcome == [True], lock_name
