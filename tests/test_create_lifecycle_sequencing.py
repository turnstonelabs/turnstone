"""Race regressions for the deferred-create publication boundary."""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

from tests.test_server_authz import (
    _auth,
    _FakeSession,
)
from tests.test_server_authz import app_client as app_client
from tests.test_session_manager import FakeAdapter, _make_manager
from turnstone.core.session_routes import (
    SessionEndpointConfig,
    make_create_handler,
)


class _BlockingCreateEmitter(FakeAdapter):
    """Pause inside ``emit_created`` after commit admission."""

    def __init__(self) -> None:
        super().__init__()
        self.create_emit_entered = threading.Event()
        self.release_create_emit = threading.Event()

    def emit_created(self, ws: Any) -> None:
        self.create_emit_entered.set()
        assert self.release_create_emit.wait(timeout=10), "test did not release create emit"
        super().emit_created(ws)


@pytest.mark.parametrize("terminal", ["close", "delete"])
def test_terminal_after_commit_admission_observes_created_first(terminal: str) -> None:
    """A close/delete admitted during create fan-out cannot overtake it."""
    adapter = _BlockingCreateEmitter()
    mgr, _, _ = _make_manager(adapter=adapter)
    ws = mgr.create(user_id="u1", name="ordered", defer_emit_created=True)
    commit_result: list[bool] = []
    terminal_result: list[bool] = []
    terminal_started = threading.Event()
    terminal_done = threading.Event()

    def _commit() -> None:
        commit_result.append(mgr.commit_create(ws))

    def _retire() -> None:
        terminal_started.set()
        if terminal == "close":
            terminal_result.append(mgr.close(ws.id))
        else:
            terminal_result.append(mgr.delete(ws.id))
        terminal_done.set()

    commit_thread = threading.Thread(target=_commit, daemon=True)
    terminal_thread = threading.Thread(target=_retire, daemon=True)
    commit_thread.start()
    assert adapter.create_emit_entered.wait(timeout=5), "commit never entered emit_created"
    terminal_thread.start()
    assert terminal_started.wait(timeout=5)
    try:
        assert not terminal_done.wait(timeout=0.1), "terminal transition overtook create emit"
    finally:
        adapter.release_create_emit.set()
    commit_thread.join(timeout=5)
    terminal_thread.join(timeout=5)

    assert not commit_thread.is_alive()
    assert not terminal_thread.is_alive()
    assert commit_result == [True]
    assert terminal_result == [True]
    assert [(event.kind, event.reason) for event in adapter.events] == [
        ("created", None),
        ("closed", "closed" if terminal == "close" else "deleted"),
    ]


def test_pending_idle_deferred_create_is_not_capacity_evicted() -> None:
    """A not-yet-published IDLE reservation remains an in-flight transaction."""
    mgr, adapter, _ = _make_manager(max_active=1)
    pending = mgr.create(
        user_id="u1",
        name="pending",
        defer_emit_created=True,
    )

    with pytest.raises(RuntimeError, match="All 1 slots are active"):
        mgr.create(user_id="u2", name="challenger")

    # Pending reservations are deliberately hidden from public lookup/list
    # surfaces. Prove the exact object survived capacity pressure by committing
    # it successfully, after which it becomes visible as the sole occupant.
    assert mgr.count == 1
    assert adapter.events == []
    assert mgr.commit_create(pending) is True
    assert mgr.get(pending.id) is pending
    assert mgr.list_all() == [pending]
    assert [event.kind for event in adapter.events] == ["created"]
    assert adapter.cleaned_up == []
    assert mgr.eviction_count == 0


def _drain_global_events(app_client: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    global_queue = app_client.app.state.global_queue
    while True:
        try:
            events.append(global_queue.get_nowait())
        except queue.Empty:
            return events


def _created_audits(storage: Any, ws_id: str) -> list[dict[str, Any]]:
    return [
        event
        for event in storage.list_audit_events(action="workstream.created")
        if event["resource_id"] == ws_id
    ]


async def _wait_for_thread_event(event: threading.Event, timeout: float) -> bool:
    """Poll a thread seam without occupying the loop's default executor."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not event.is_set():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.01)
    return True


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_cancellation_during_session_build_removes_exact_hidden_create(
    app_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: str,
) -> None:
    """A cancelled request drains the admitted build before exact rollback."""
    from turnstone.core.attachment_buffer import get_attachment_buffer

    assert anyio_backend == "asyncio"
    sync_client, mgr = app_client
    storage = sync_client.app.state.auth_storage
    assert storage is not None
    ws_id = "1" * 32
    build_entered = threading.Event()
    release_build = threading.Event()
    build_finished = threading.Event()
    original_build = mgr._adapter.build_session
    buffer = get_attachment_buffer()
    buffer.clear()

    def _blocked_build(ws: Any, **kwargs: Any) -> Any:
        build_entered.set()
        assert release_build.wait(timeout=10), "test did not release session build"
        try:
            return original_build(ws, **kwargs)
        finally:
            build_finished.set()

    monkeypatch.setattr(mgr._adapter, "build_session", _blocked_build)
    transport = httpx.ASGITransport(app=sync_client.app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            request_task = asyncio.create_task(
                client.post(
                    "/v1/api/workstreams/new",
                    json={"ws_id": ws_id, "name": "cancel-during-build"},
                    headers=_auth("user-1"),
                )
            )
            assert await _wait_for_thread_event(build_entered, 5), "create never entered build"

            # A caller-known id can receive a concurrent staged upload while
            # its hidden durable reservation is still being constructed.
            buffer.stage(
                ws_id=ws_id,
                user_id="user-1",
                filename="pending.md",
                mime_type="text/markdown",
                kind="text",
                content=b"pending create upload",
            )
            with mgr._lock:
                pending = mgr._workstreams.get(ws_id)
                assert pending is not None
                assert mgr._pending_creates.get(ws_id) is pending
            assert storage.get_workstream(ws_id) is not None

            request_task.cancel()
            await asyncio.sleep(0.05)
            assert not request_task.done()
            release_build.set()
            with pytest.raises(asyncio.CancelledError):
                await request_task
    finally:
        release_build.set()

    assert build_finished.is_set()
    with mgr._lock:
        assert ws_id not in mgr._workstreams
        assert ws_id not in mgr._pending_creates
    assert storage.get_workstream(ws_id) is None
    assert buffer.list_for(ws_id=ws_id, user_id="user-1") == []
    assert _created_audits(storage, ws_id) == []
    assert not [event for event in _drain_global_events(sync_client) if event.get("ws_id") == ws_id]
    buffer.clear()


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_second_cancellation_cannot_interrupt_create_rollback(
    app_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: str,
) -> None:
    """Repeated cancellation is deferred until discard and delete settle."""
    from turnstone.core.attachment_buffer import get_attachment_buffer

    assert anyio_backend == "asyncio"
    sync_client, mgr = app_client
    storage = sync_client.app.state.auth_storage
    assert storage is not None
    ws_id = "2" * 32
    build_entered = threading.Event()
    release_build = threading.Event()
    rollback_entered = threading.Event()
    release_rollback = threading.Event()
    original_build = mgr._adapter.build_session
    original_discard = mgr.discard
    buffer = get_attachment_buffer()
    buffer.clear()

    def _blocked_build(ws: Any, **kwargs: Any) -> Any:
        build_entered.set()
        assert release_build.wait(timeout=10), "test did not release session build"
        return original_build(ws, **kwargs)

    def _blocked_discard(*args: Any, **kwargs: Any) -> bool:
        rollback_entered.set()
        assert release_rollback.wait(timeout=10), "test did not release rollback"
        return original_discard(*args, **kwargs)

    monkeypatch.setattr(mgr._adapter, "build_session", _blocked_build)
    monkeypatch.setattr(mgr, "discard", _blocked_discard)
    transport = httpx.ASGITransport(app=sync_client.app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            request_task = asyncio.create_task(
                client.post(
                    "/v1/api/workstreams/new",
                    json={"ws_id": ws_id, "name": "cancel-rollback-twice"},
                    headers=_auth("user-1"),
                )
            )
            assert await _wait_for_thread_event(build_entered, 5), "create never entered build"
            buffer.stage(
                ws_id=ws_id,
                user_id="user-1",
                filename="pending.md",
                mime_type="text/markdown",
                kind="text",
                content=b"survives until rollback",
            )
            request_task.cancel()
            release_build.set()
            assert await _wait_for_thread_event(rollback_entered, 5), "rollback never started"

            # The first cancellation has already transferred ownership to the
            # cleanup bracket. A second one must not strand either half of the
            # in-memory/durable rollback transaction.
            assert request_task.cancel() is True
            await asyncio.sleep(0.05)
            assert not request_task.done()
            assert storage.get_workstream(ws_id) is not None
            assert buffer.list_for(ws_id=ws_id, user_id="user-1")
            release_rollback.set()
            with pytest.raises(asyncio.CancelledError):
                await request_task
    finally:
        release_build.set()
        release_rollback.set()

    with mgr._lock:
        assert ws_id not in mgr._workstreams
        assert ws_id not in mgr._pending_creates
    assert storage.get_workstream(ws_id) is None
    assert buffer.list_for(ws_id=ws_id, user_id="user-1") == []
    assert _created_audits(storage, ws_id) == []
    assert not [event for event in _drain_global_events(sync_client) if event.get("ws_id") == ws_id]
    buffer.clear()


def test_partial_multipart_failure_drops_staged_refs_before_same_id_successor(
    app_client: Any,
) -> None:
    """A partially staged failed request cannot lend uploads to its successor."""
    from turnstone.core.attachment_buffer import get_attachment_buffer

    client, mgr = app_client
    storage = client.app.state.auth_storage
    assert storage is not None
    ws_id = "3" * 32
    buffer = get_attachment_buffer()
    buffer.clear()
    try:
        failed = client.post(
            "/v1/api/workstreams/new",
            data={"meta": json.dumps({"ws_id": ws_id, "name": "partial"})},
            files=[
                ("file", ("valid.md", b"first file stages", "text/markdown")),
                ("file", ("invalid.bin", b"\x00\x01\x02", "application/octet-stream")),
            ],
            headers=_auth("user-1"),
        )

        assert failed.status_code == 400, failed.text
        assert storage.get_workstream(ws_id) is None
        assert buffer.list_for(ws_id=ws_id, user_id="user-1") == []
        with mgr._lock:
            assert ws_id not in mgr._workstreams
            assert ws_id not in mgr._pending_creates
        assert _created_audits(storage, ws_id) == []
        assert not [event for event in _drain_global_events(client) if event.get("ws_id") == ws_id]

        successor = client.post(
            "/v1/api/workstreams/new",
            json={"ws_id": ws_id, "name": "successor"},
            headers=_auth("user-1"),
        )
        assert successor.status_code == 200, successor.text
        assert successor.json()["attachment_ids"] == []
        assert storage.get_workstream(ws_id) is not None
        assert buffer.list_for(ws_id=ws_id, user_id="user-1") == []
    finally:
        buffer.clear()


def test_close_idle_zero_never_retires_pending_create() -> None:
    """The idle sweeper treats a hidden reservation as an in-flight create."""
    mgr, adapter, storage = _make_manager()
    pending = mgr.create(
        user_id="u1",
        name="pending-idle",
        defer_emit_created=True,
    )
    pending.last_active = time.monotonic() - 100

    assert mgr.close_idle(max_age_seconds=0) == []
    assert adapter.cleaned_up == []
    assert adapter.events == []
    assert storage.rows[pending.id].state == "creating"
    with mgr._lock:
        assert mgr._workstreams.get(pending.id) is pending
        assert mgr._pending_creates.get(pending.id) is pending

    assert mgr.commit_create(pending) is True
    assert mgr.get(pending.id) is pending
    assert [event.kind for event in adapter.events] == ["created"]


def test_delete_endpoint_waits_for_admitted_create_publication(
    app_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Durable deletion linearizes after a create that already owns admission."""
    client, mgr = app_client
    storage = client.app.state.auth_storage
    assert storage is not None
    ws_id = "4" * 32
    pending = mgr.create(
        ws_id=ws_id,
        user_id="user-1",
        name="commit-before-delete",
        defer_emit_created=True,
    )
    emit_entered = threading.Event()
    release_emit = threading.Event()
    delete_admission_entered = threading.Event()
    durable_delete_entered = threading.Event()
    delete_started = threading.Event()
    original_emit = mgr._event_emitter.emit_created
    original_delete = storage.delete_workstream_if_fork_reserved
    original_delete_persisted = mgr.delete_persisted
    commit_results: list[bool] = []
    delete_responses: list[Any] = []

    def _blocked_emit(ws: Any) -> None:
        emit_entered.set()
        assert release_emit.wait(timeout=10), "test did not release create publication"
        original_emit(ws)

    def _tracked_delete(candidate_id: str, reservation_token: str) -> bool:
        durable_delete_entered.set()
        return original_delete(candidate_id, reservation_token)

    def _tracked_delete_persisted(*args: Any, **kwargs: Any) -> bool:
        delete_admission_entered.set()
        return original_delete_persisted(*args, **kwargs)

    def _commit() -> None:
        commit_results.append(mgr.commit_create(pending))

    def _delete() -> None:
        delete_started.set()
        delete_responses.append(
            client.post(
                f"/v1/api/workstreams/{ws_id}/delete",
                headers=_auth("user-1"),
            )
        )

    monkeypatch.setattr(mgr._event_emitter, "emit_created", _blocked_emit)
    monkeypatch.setattr(storage, "delete_workstream_if_fork_reserved", _tracked_delete)
    monkeypatch.setattr(mgr, "delete_persisted", _tracked_delete_persisted)
    commit_thread = threading.Thread(target=_commit, daemon=True)
    delete_thread = threading.Thread(target=_delete, daemon=True)
    commit_thread.start()
    assert emit_entered.wait(timeout=5), "commit never entered publication"
    delete_thread.start()
    assert delete_started.wait(timeout=5)
    try:
        assert delete_admission_entered.wait(timeout=5), "delete never reached manager admission"
        assert delete_thread.is_alive(), "delete overtook admitted create publication"
        assert not durable_delete_entered.is_set()
        assert storage.get_workstream(ws_id) is not None
    finally:
        release_emit.set()
    commit_thread.join(timeout=10)
    delete_thread.join(timeout=10)

    assert not commit_thread.is_alive()
    assert not delete_thread.is_alive()
    assert commit_results == [True]
    assert len(delete_responses) == 1
    assert delete_responses[0].status_code == 200, delete_responses[0].text
    assert durable_delete_entered.is_set()
    assert storage.get_workstream(ws_id) is None
    assert mgr.get(ws_id) is None
    lifecycle = [
        event["type"]
        for event in _drain_global_events(client)
        if event.get("ws_id") == ws_id and event.get("type") in {"ws_created", "ws_closed"}
    ]
    assert lifecycle == ["ws_created", "ws_closed"]


@pytest.mark.parametrize("terminal", ["close", "delete"])
def test_interactive_post_install_has_no_late_publication_after_terminal(
    app_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    terminal: str,
) -> None:
    """The post-commit tail cannot publish or install onto a retired object."""
    from turnstone.core.audit import record_audit as original_record_audit
    from turnstone.core.storage import get_storage

    client, mgr = app_client
    storage = get_storage()
    assert storage is not None
    source_id = "a" * 32
    destination_id = "b" * 32
    storage.register_workstream(
        source_id,
        node_id="node-test",
        name="source",
        user_id="user-1",
    )
    audit_entered = threading.Event()
    release_audit = threading.Event()
    watch_registrations: list[str] = []

    def _record_audit(*args: Any, **kwargs: Any) -> Any:
        action = args[2] if len(args) > 2 else kwargs.get("action")
        if action == "workstream.created":
            audit_entered.set()
            assert release_audit.wait(timeout=10), "test did not release create audit"
        return original_record_audit(*args, **kwargs)

    def _set_watch_runner(session: _FakeSession, *_args: Any, **_kwargs: Any) -> None:
        watch_registrations.append(session.ws_id)

    monkeypatch.setattr("turnstone.core.audit.record_audit", _record_audit)
    monkeypatch.setattr(_FakeSession, "set_watch_runner", _set_watch_runner)
    client.app.state.watch_runner = object()
    responses: list[Any] = []
    request_errors: list[BaseException] = []

    def _create() -> None:
        try:
            responses.append(
                client.post(
                    "/v1/api/workstreams/new",
                    json={
                        "ws_id": destination_id,
                        "name": "named fork",
                        "resume_ws": source_id,
                    },
                    headers=_auth("user-1"),
                )
            )
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            request_errors.append(exc)

    request_thread = threading.Thread(target=_create, daemon=True)
    request_thread.start()
    assert audit_entered.wait(timeout=5), "create never reached the post-commit audit"
    destination = mgr.get(destination_id)
    assert destination is not None
    try:
        if terminal == "close":
            assert mgr.close(destination_id) is True
        else:
            assert storage.delete_workstream(destination_id) is True
            assert mgr.delete(destination_id) is True

        events_before_release = _drain_global_events(client)
        lifecycle_before_release = [
            event["type"]
            for event in events_before_release
            if event.get("type") in {"ws_created", "ws_rename", "ws_closed"}
        ]
        assert lifecycle_before_release == ["ws_created", "ws_rename", "ws_closed"]
        assert watch_registrations == [destination_id]
    finally:
        release_audit.set()
    request_thread.join(timeout=10)

    assert not request_thread.is_alive()
    assert request_errors == []
    assert len(responses) == 1
    assert responses[0].status_code == 200, responses[0].text
    assert mgr.get(destination_id) is None
    events_after_release = _drain_global_events(client)
    assert not [
        event for event in events_after_release if event.get("type") in {"ws_created", "ws_rename"}
    ]
    assert watch_registrations == [destination_id]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_cancellation_after_commit_waits_post_install_and_keeps_one_create(
    anyio_backend: str,
) -> None:
    """Cancellation preserves the admitted create and drains its shielded tail."""
    assert anyio_backend == "asyncio"
    mgr, adapter, _ = _make_manager()
    post_install_entered = asyncio.Event()
    release_post_install = asyncio.Event()
    post_install_completed = False

    def _manager_lookup(_request: Any) -> tuple[Any, None]:
        return mgr, None

    def _build_kwargs(
        _request: Any,
        body: dict[str, Any],
        uid: str,
        _skill_data: dict[str, Any] | None,
        _skill_id: str,
        _skill_version: int,
    ) -> dict[str, Any]:
        return {"user_id": uid or "u1", "name": str(body.get("name") or "")}

    async def _post_install(
        _request: Any,
        _ws: Any,
        _body: dict[str, Any],
        _uid: str,
        _skill_data: dict[str, Any] | None,
        _skill_version: int,
        _attachment_ids: list[str],
    ) -> dict[str, Any]:
        nonlocal post_install_completed
        post_install_entered.set()
        await release_post_install.wait()
        post_install_completed = True
        return {}

    cfg = SessionEndpointConfig(
        permission_gate=None,
        manager_lookup=_manager_lookup,
        tenant_check=None,
        not_found_label="Workstream not found",
        audit_action_prefix="workstream",
        create_build_kwargs=_build_kwargs,
        create_post_install=_post_install,
    )
    app = Starlette(routes=[Route("/new", make_create_handler(cfg), methods=["POST"])])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        request_task = asyncio.create_task(client.post("/new", json={"name": "cancelled"}))
        await asyncio.wait_for(post_install_entered.wait(), timeout=5)
        request_task.cancel()
        await asyncio.sleep(0.05)
        assert not request_task.done()
        assert post_install_completed is False
        release_post_install.set()
        with pytest.raises(asyncio.CancelledError):
            await request_task

    assert post_install_completed is True
    assert mgr.count == 1
    created = adapter.events_of("created")
    assert len(created) == 1
    assert created[0].ws_id == mgr.list_all()[0].id
    assert adapter.events_of("closed") == []
