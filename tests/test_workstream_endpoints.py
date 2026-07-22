"""Tests for workstream management endpoints added in PRs #314-#315."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request
    from starlette.responses import Response

from turnstone.core.auth import AuthResult
from turnstone.core.history_decoration import (
    decorate_history_messages,
    project_history_messages,
)
from turnstone.core.session_routes import (
    SessionEndpointConfig,
    make_detail_handler,
    make_export_handler,
    make_history_handler,
    make_open_handler,
    make_refresh_title_handler,
    make_retry_handler,
    make_rewind_handler,
    make_set_title_handler,
)
from turnstone.core.storage._sqlite import SQLiteBackend
from turnstone.core.workstream import WorkstreamKind
from turnstone.server import (
    _interactive_tenant_check,
    delete_workstream_endpoint,
    list_interface_settings,
    update_interface_setting,
)

# ---------------------------------------------------------------------------
# Auth bypass middleware
# ---------------------------------------------------------------------------


class _InjectAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id="test-user",
            scopes=frozenset({"approve"}),
            token_source="config",
            permissions=frozenset({"read", "write", "approve"}),
        )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path):
    return SQLiteBackend(str(tmp_path / "test.db"))


@pytest.fixture
def _inject_storage(storage):
    """Swap global storage registry for the test backend."""
    import turnstone.core.storage._registry as reg

    old = reg._storage
    reg._storage = storage
    yield storage
    reg._storage = old


@pytest.fixture
def delete_client(_inject_storage):
    """Return ``(TestClient, Starlette app)`` for the delete endpoint.

    ``app.state.auth_storage`` is pre-attached so the endpoint's
    pre-delete snapshot block runs (without it the snapshot is
    skipped and the lifecycle event lands with ``name=""``).  Tests
    that need a wired manager attach ``app.state.workstreams = mgr``
    on the returned app; tests that don't care just ignore the
    second tuple member.
    """
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route(
                        "/api/workstreams/{ws_id}/delete",
                        delete_workstream_endpoint,
                        methods=["POST"],
                    ),
                ],
            ),
        ],
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.auth_storage = _inject_storage
    return TestClient(app), app


@pytest.fixture
def title_client(_inject_storage):
    # Build the lifted refresh/set-title handlers the same way server.py
    # wires the interactive bundle — same SessionEndpointConfig
    # (manager_lookup + _interactive_tenant_check) so the tests exercise
    # the production resolution path (mgr fast-path → storage ownership).
    mock_mgr = MagicMock()
    cfg = SessionEndpointConfig(
        permission_gate=None,
        manager_lookup=lambda _r: (mock_mgr, None),
        tenant_check=_interactive_tenant_check,
        not_found_label="Workstream not found",
        audit_action_prefix="workstream",
    )
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route(
                        "/api/workstreams/{ws_id}/title",
                        make_set_title_handler(cfg),
                        methods=["POST"],
                    ),
                    Route(
                        "/api/workstreams/{ws_id}/refresh-title",
                        make_refresh_title_handler(cfg),
                        methods=["POST"],
                    ),
                ],
            ),
        ],
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.workstreams = mock_mgr
    return TestClient(app), mock_mgr


@pytest.fixture
def open_client(_inject_storage):
    """Build a TestClient with the lifted ``open`` handler wired the
    same way ``server.py`` does — alias resolver, no post-load
    callback (the tests assert HTTP-shape only, not the SSE replay).

    The alias resolver is wrapped in a lazy lookup so each test's
    ``@patch("turnstone.core.memory.resolve_workstream")`` is
    visible at request time. A direct function reference would
    bind to the unpatched original at fixture-construction time.
    """

    def _lazy_alias_resolver(ws_id: str) -> str | None:
        from turnstone.core.memory import resolve_workstream

        return resolve_workstream(ws_id)

    mock_mgr = MagicMock()
    cfg = SessionEndpointConfig(
        permission_gate=None,
        manager_lookup=lambda _r: (mock_mgr, None),
        tenant_check=None,
        not_found_label="Workstream not found",
        audit_action_prefix="workstream",
        open_resolve_alias=_lazy_alias_resolver,
    )
    open_handler = make_open_handler(cfg)
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route(
                        "/api/workstreams/{ws_id}/open",
                        open_handler,
                        methods=["POST"],
                    ),
                ],
            ),
        ],
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.workstreams = mock_mgr
    gq: queue.Queue[dict[str, Any]] = queue.Queue()
    app.state.global_queue = gq
    return TestClient(app), mock_mgr, gq


@pytest.fixture
def settings_client(_inject_storage):
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route("/api/admin/settings", list_interface_settings),
                    Route(
                        "/api/admin/settings/{key:path}",
                        update_interface_setting,
                        methods=["POST", "PUT"],
                    ),
                ],
            ),
        ],
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.config_store = None
    app.state.global_queue = queue.Queue()
    return TestClient(app)


# ===========================================================================
# Rewind / retry (#549 verb lift)
# ===========================================================================


def _rewind_retry_mocks(*, worker_running=False, rewind_return=4, retry_return="hi"):
    """Mocked ``(manager, session, enqueued-events)`` for the lifted
    rewind/retry handlers. ``ws._lock`` is a real lock so the handler's
    busy-gate ``with ws._lock`` works; ``ui._enqueue`` records events."""
    import threading

    mock_session = MagicMock()
    mock_session.rewind.return_value = rewind_return
    mock_session.retry.return_value = retry_return
    enqueued: list[dict[str, Any]] = []
    mock_ui = MagicMock()
    mock_ui._enqueue.side_effect = lambda ev: enqueued.append(ev)
    mock_ws = MagicMock()
    mock_ws.session = mock_session
    mock_ws.ui = mock_ui
    mock_ws._lock = threading.Lock()
    mock_ws._worker_running = worker_running
    mock_mgr = MagicMock()
    mock_mgr.get.return_value = mock_ws
    return mock_mgr, mock_session, enqueued


def _verb_cfg(mock_mgr: Any) -> SessionEndpointConfig:
    return SessionEndpointConfig(
        permission_gate=None,
        manager_lookup=lambda _r: (mock_mgr, None),
        tenant_check=None,
        not_found_label="Workstream not found",
        audit_action_prefix="workstream",
    )


def _verb_client(route_path: str, handler: Any) -> TestClient:
    app = Starlette(
        routes=[Mount("/v1", routes=[Route(route_path, handler, methods=["POST"])])],
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    return TestClient(app)


def test_rewind_returns_removed_and_emits_clear_ui():
    mock_mgr, mock_session, enqueued = _rewind_retry_mocks(rewind_return=4)
    handler = make_rewind_handler(_verb_cfg(mock_mgr))
    client = _verb_client("/api/workstreams/{ws_id}/rewind", handler)
    resp = client.post("/v1/api/workstreams/ws1/rewind", json={"turns": 2})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "removed": 4}
    mock_session.rewind.assert_called_once_with(2)
    assert {"type": "clear_ui"} in enqueued


def test_rewind_rejects_non_positive_or_non_int_turns():
    mock_mgr, mock_session, _ = _rewind_retry_mocks()
    handler = make_rewind_handler(_verb_cfg(mock_mgr))
    client = _verb_client("/api/workstreams/{ws_id}/rewind", handler)
    # ``True`` is an int subclass — must be rejected too.
    for bad in ({}, {"turns": 0}, {"turns": -1}, {"turns": "two"}, {"turns": True}):
        resp = client.post("/v1/api/workstreams/ws1/rewind", json=bad)
        assert resp.status_code == 400, bad
    mock_session.rewind.assert_not_called()


def test_rewind_while_busy_returns_busy_and_skips_mutation():
    mock_mgr, mock_session, enqueued = _rewind_retry_mocks(worker_running=True)
    handler = make_rewind_handler(_verb_cfg(mock_mgr))
    client = _verb_client("/api/workstreams/{ws_id}/rewind", handler)
    resp = client.post("/v1/api/workstreams/ws1/rewind", json={"turns": 1})
    assert resp.status_code == 200
    assert resp.json()["status"] == "busy"
    mock_session.rewind.assert_not_called()
    assert any(e.get("type") == "busy_error" for e in enqueued)


def test_retry_dispatches_and_emits_clear_ui():
    mock_mgr, _session, enqueued = _rewind_retry_mocks(retry_return="hello")
    dispatched: list[str] = []
    handler = make_retry_handler(
        _verb_cfg(mock_mgr), dispatch_retry=lambda _ws, msg: dispatched.append(msg)
    )
    client = _verb_client("/api/workstreams/{ws_id}/retry", handler)
    resp = client.post("/v1/api/workstreams/ws1/retry")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "retried": True}
    assert dispatched == ["hello"]
    assert {"type": "clear_ui"} in enqueued


def test_retry_nothing_to_retry_skips_dispatch():
    mock_mgr, _session, enqueued = _rewind_retry_mocks(retry_return=None)
    dispatched: list[str] = []
    handler = make_retry_handler(
        _verb_cfg(mock_mgr), dispatch_retry=lambda _ws, msg: dispatched.append(msg)
    )
    client = _verb_client("/api/workstreams/{ws_id}/retry", handler)
    resp = client.post("/v1/api/workstreams/ws1/retry")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "retried": False}
    assert dispatched == []
    assert {"type": "clear_ui"} in enqueued


def test_retry_while_busy_returns_busy_and_skips_dispatch():
    mock_mgr, mock_session, enqueued = _rewind_retry_mocks(worker_running=True)
    dispatched: list[str] = []
    handler = make_retry_handler(
        _verb_cfg(mock_mgr), dispatch_retry=lambda _ws, msg: dispatched.append(msg)
    )
    client = _verb_client("/api/workstreams/{ws_id}/retry", handler)
    resp = client.post("/v1/api/workstreams/ws1/retry")
    assert resp.status_code == 200
    assert resp.json()["status"] == "busy"
    mock_session.retry.assert_not_called()
    assert dispatched == []
    assert any(e.get("type") == "busy_error" for e in enqueued)


def test_rewind_invokes_audit_emit_with_turns():
    """The handler calls ``audit_emit(request, ws_id, ws, turns)`` — a
    dropped ``audit_emit=`` wiring or a renamed arg would break this."""
    mock_mgr, _session, _enqueued = _rewind_retry_mocks()
    captured: list[tuple[str, int]] = []
    handler = make_rewind_handler(
        _verb_cfg(mock_mgr),
        audit_emit=lambda _req, ws_id, _ws, turns: captured.append((ws_id, turns)),
    )
    client = _verb_client("/api/workstreams/{ws_id}/rewind", handler)
    resp = client.post("/v1/api/workstreams/ws1/rewind", json={"turns": 3})
    assert resp.status_code == 200
    assert captured == [("ws1", 3)]


def test_retry_invokes_audit_emit():
    mock_mgr, _session, _enqueued = _rewind_retry_mocks(retry_return="hi")
    captured: list[str] = []
    handler = make_retry_handler(
        _verb_cfg(mock_mgr),
        dispatch_retry=lambda _ws, _msg: None,
        audit_emit=lambda _req, ws_id, _ws: captured.append(ws_id),
    )
    client = _verb_client("/api/workstreams/{ws_id}/retry", handler)
    resp = client.post("/v1/api/workstreams/ws1/retry")
    assert resp.status_code == 200
    assert captured == ["ws1"]


def test_rewind_swallows_audit_emit_exception():
    """A raising ``audit_emit`` is demoted to a warning — the handler still
    returns 200 and the rewind still took effect (mirrors close/cancel)."""
    mock_mgr, mock_session, _enqueued = _rewind_retry_mocks(rewind_return=2)

    def _boom(_req, _ws_id, _ws, _turns):
        raise RuntimeError("audit backend down")

    handler = make_rewind_handler(_verb_cfg(mock_mgr), audit_emit=_boom)
    client = _verb_client("/api/workstreams/{ws_id}/rewind", handler)
    resp = client.post("/v1/api/workstreams/ws1/rewind", json={"turns": 1})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "removed": 2}
    mock_session.rewind.assert_called_once_with(1)


# ===========================================================================
# DELETE workstream
# ===========================================================================


class TestDeleteWorkstream:
    def test_delete_success(self, delete_client, storage):
        client, _ = delete_client
        storage.register_workstream("ws-abc", "node-1", name="test")
        r = client.post("/v1/api/workstreams/ws-abc/delete")
        assert r.status_code == 200
        assert r.json()["deleted"] == "ws-abc"

    def test_delete_not_found(self, delete_client):
        client, _ = delete_client
        r = client.post("/v1/api/workstreams/nonexistent/delete")
        assert r.status_code == 404
        assert "not found" in r.json()["error"].lower()

    def test_delete_error_redacted(self, delete_client, storage):
        """500 response should not leak exception internals."""
        client, _ = delete_client
        storage.register_workstream("ws-abc", "node-1", name="test", user_id="test-user")
        with patch(
            "turnstone.core.memory.delete_workstream",
            side_effect=RuntimeError("secret internal detail"),
        ):
            r = client.post("/v1/api/workstreams/ws-abc/delete")
        assert r.status_code == 500
        assert "Delete failed" in r.json()["error"]
        assert "secret" not in r.json()["error"]

    def test_delete_fires_lifecycle_event_with_snapshotted_name(self, delete_client, storage):
        """The endpoint must call ``mgr.delete(ws_id, name=...)`` after a
        successful storage delete so the cluster collector → coord
        adapter chain can re-emit ``child_ws_closed`` and the operator's
        child-tree drops the row.  Without this the deleted child stays
        visible (with its last-known state) until a full reload — a
        coordinator that spawns→completes→deletes children leaves an
        ever-growing tree on the dashboard."""
        client, app = delete_client
        storage.register_workstream("ws-event", "node-1", name="needs-event", user_id="test-user")
        mgr = MagicMock()
        app.state.workstreams = mgr
        r = client.post("/v1/api/workstreams/ws-event/delete")
        assert r.status_code == 200
        # The event-emission call must use the name we snapshotted
        # before the storage row was wiped.
        mgr.delete.assert_called_once_with("ws-event", name="needs-event")

    def test_delete_event_emit_failure_does_not_500(self, delete_client, storage):
        """A best-effort event emit: if the manager's emitter chokes
        (queue full, adapter mid-shutdown, etc.) the storage row is
        already gone and the response must still be 200 — rolling
        back the delete to satisfy a fan-out failure would corrupt
        the operator's view of an already-vanished workstream."""
        client, app = delete_client
        storage.register_workstream("ws-flaky", "node-1", name="flaky", user_id="test-user")
        mgr = MagicMock()
        mgr.delete.side_effect = RuntimeError("queue full or whatever")
        app.state.workstreams = mgr
        r = client.post("/v1/api/workstreams/ws-flaky/delete")
        assert r.status_code == 200
        assert r.json()["deleted"] == "ws-flaky"


# ===========================================================================
# SET title
# ===========================================================================


class TestSetWorkstreamTitle:
    def test_set_title_success(self, title_client, storage):
        client, mock_mgr = title_client
        storage.register_workstream("ws-abc", "node-1", name="test", user_id="test-user")
        # mgr.get returning None makes _require_ws_access fall through to
        # the storage-backed ownership check (caller == "test-user" matches
        # the registered owner).  Tests that need a ws returned from the
        # manager set up mock_ws.user_id explicitly.
        mock_mgr.get.return_value = None
        r = client.post(
            "/v1/api/workstreams/ws-abc/title",
            json={"title": "New Title"},
        )
        assert r.status_code == 200
        assert r.json()["title"] == "New Title"

    def test_set_title_empty(self, title_client, storage):
        client, mock_mgr = title_client
        storage.register_workstream("ws-abc", "node-1", name="test", user_id="test-user")
        mock_mgr.get.return_value = None
        r = client.post(
            "/v1/api/workstreams/ws-abc/title",
            json={"title": ""},
        )
        assert r.status_code == 400
        assert "required" in r.json()["error"].lower()

    def test_set_title_missing_body(self, title_client, storage):
        client, mock_mgr = title_client
        storage.register_workstream("ws-abc", "node-1", name="test", user_id="test-user")
        mock_mgr.get.return_value = None
        r = client.post(
            "/v1/api/workstreams/ws-abc/title",
            json={},
        )
        assert r.status_code == 400

    def test_set_title_truncation(self, title_client, storage):
        client, mock_mgr = title_client
        storage.register_workstream("ws-abc", "node-1", name="test", user_id="test-user")
        mock_mgr.get.return_value = None
        long_title = "x" * 200
        r = client.post(
            "/v1/api/workstreams/ws-abc/title",
            json={"title": long_title},
        )
        assert r.status_code == 200
        assert len(r.json()["title"]) <= 80

    def test_set_title_alias_conflict(self, title_client, storage):
        client, mock_mgr = title_client
        storage.register_workstream("ws-1", "node-1", name="first", user_id="test-user")
        storage.register_workstream("ws-2", "node-1", name="second", user_id="test-user")
        storage.set_workstream_alias("ws-1", "taken-name")
        mock_mgr.get.return_value = None
        r = client.post(
            "/v1/api/workstreams/ws-2/title",
            json={"title": "taken-name"},
        )
        assert r.status_code == 409


# ===========================================================================
# REFRESH title
# ===========================================================================


class TestRefreshWorkstreamTitle:
    def test_refresh_success(self, title_client, storage):
        client, mock_mgr = title_client
        storage.register_workstream("ws-abc", "node-1", name="test", user_id="test-user")
        # The in-memory fast path on _require_ws_access checks ws.user_id
        # before falling back to storage, so the mock returned by
        # mgr.get must carry the expected owner.
        mock_ws = MagicMock()
        mock_ws.user_id = "test-user"
        mock_ws.session = MagicMock()
        mock_mgr.get.return_value = mock_ws
        with patch("turnstone.core.memory.get_workstream_display_name", return_value="Old Title"):
            r = client.post("/v1/api/workstreams/ws-abc/refresh-title")
        assert r.status_code == 200
        mock_ws.session.request_title_refresh.assert_called_once_with("Old Title")

    def test_refresh_not_found(self, title_client):
        client, mock_mgr = title_client
        mock_mgr.get.return_value = None
        r = client.post("/v1/api/workstreams/ws-abc/refresh-title")
        assert r.status_code == 404

    def test_refresh_no_session(self, title_client):
        client, mock_mgr = title_client
        mock_ws = MagicMock()
        mock_ws.session = None
        mock_mgr.get.return_value = mock_ws
        r = client.post("/v1/api/workstreams/ws-abc/refresh-title")
        assert r.status_code == 404


# ===========================================================================
# OPEN workstream
# ===========================================================================


class TestOpenWorkstream:
    @patch("turnstone.core.memory.resolve_workstream")
    def test_open_already_loaded(self, mock_resolve, open_client):
        """The lifted body returns ``ws.name`` directly on the
        already-loaded shortcut path (not a display-alias re-lookup).
        Pre-lift interactive routed through ``get_workstream_display_name``
        here; the lift consolidates on the in-memory ``ws.name`` field
        for parity with coord's pre-lift behaviour. Frontend already
        re-fetches names from the dashboard endpoint so a freshly-
        renamed workstream still surfaces its alias on subsequent
        listings — no observable user-facing regression."""
        client, mock_mgr, gq = open_client
        mock_resolve.return_value = "ws-abc"
        mock_ws = MagicMock()
        mock_ws.id = "ws-abc"
        mock_ws.name = "My WS"
        mock_mgr.get.return_value = mock_ws
        r = client.post("/v1/api/workstreams/ws-abc/open")
        assert r.status_code == 200
        assert r.json()["already_loaded"] is True
        assert r.json()["ws_id"] == "ws-abc"
        assert r.json()["name"] == "My WS"

    @patch("turnstone.core.memory.resolve_workstream")
    def test_open_not_found(self, mock_resolve, open_client):
        client, mock_mgr, gq = open_client
        mock_resolve.return_value = None
        r = client.post("/v1/api/workstreams/nonexistent/open")
        assert r.status_code == 404

    @patch("turnstone.core.memory.resolve_workstream")
    def test_open_no_storage_row(self, mock_resolve, open_client, _inject_storage):
        """``mgr.open`` returns ``None`` for missing storage rows, kind
        mismatches, and tombstoned rows — all surface as 404 with
        ``cfg.not_found_label``. Pre-lift returned a more specific
        ``"Workstream not found in storage"`` from a separate
        pre-mgr.create storage probe; the lift consolidates the
        404 path through ``mgr.open``'s single None-return contract
        (the kind-specific failure mode is internal detail not worth
        a distinct error string)."""
        client, mock_mgr, gq = open_client
        mock_resolve.return_value = "ws-abc"
        mock_mgr.get.return_value = None  # not loaded
        mock_mgr.open.return_value = (
            None  # mgr.open's contract: None for missing/wrong-kind/tombstone
        )
        r = client.post("/v1/api/workstreams/ws-abc/open")
        assert r.status_code == 404
        assert "not found" in r.json()["error"].lower()

    @patch("turnstone.core.memory.resolve_workstream")
    def test_open_calls_mgr_open_not_mgr_create(self, mock_resolve, open_client):
        """Post-P3 reckoning item #3: interactive ``open`` must route
        through ``mgr.open()`` (which fires ``emit_rehydrated``), not
        ``mgr.create(ws_id=...)`` (the pre-lift workaround that
        bypassed ``emit_rehydrated`` entirely, leaving it dead-by-
        routing on interactive). Asserting ``mgr.open`` was called
        + ``mgr.create`` was NOT called pins the load-bearing
        behaviour change."""
        client, mock_mgr, gq = open_client
        mock_resolve.return_value = "ws-resolved"
        mock_mgr.get.return_value = None  # not loaded
        loaded_ws = MagicMock()
        loaded_ws.id = "ws-resolved"
        loaded_ws.name = "resolved"
        mock_mgr.open.return_value = loaded_ws

        r = client.post("/v1/api/workstreams/some-alias/open")
        assert r.status_code == 200
        mock_mgr.open.assert_called_once_with("ws-resolved")
        mock_mgr.create.assert_not_called()

    @patch("turnstone.core.memory.resolve_workstream")
    def test_open_resolves_alias_before_lookup(self, mock_resolve, open_client):
        """``cfg.open_resolve_alias`` runs first — the path-param can
        be a user-friendly alias that resolves to a hex id, and the
        already-loaded shortcut + mgr.open both see the resolved id.
        Pre-lift behaviour preserved verbatim (interactive's friendly-
        alias UX survives the lift)."""
        client, mock_mgr, gq = open_client
        mock_resolve.return_value = "ws-canonical-id"
        mock_mgr.get.return_value = None
        loaded_ws = MagicMock()
        loaded_ws.id = "ws-canonical-id"
        loaded_ws.name = "x"
        mock_mgr.open.return_value = loaded_ws

        r = client.post("/v1/api/workstreams/my-friendly-alias/open")
        assert r.status_code == 200
        mock_resolve.assert_called_once_with("my-friendly-alias")
        mock_mgr.get.assert_called_once_with("ws-canonical-id")
        mock_mgr.open.assert_called_once_with("ws-canonical-id")

    @patch("turnstone.core.memory.resolve_workstream")
    def test_open_post_load_callback_fires_with_request_and_ws(self, mock_resolve, _inject_storage):
        """``cfg.open_post_load`` is the kind-specific hook for
        post-mgr.open work (interactive uses it for UI replay +
        handler-side ws_created enqueue; coord wires None). Verify
        the callback receives ``(request, ws)`` exactly once on a
        successful open and is NOT fired on the already-loaded
        shortcut (the pre-lift handler also returned early in the
        already-loaded branch before any post-load work)."""
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.routing import Mount, Route
        from starlette.testclient import TestClient

        captured: list[tuple[str, Any]] = []

        def _post_load(request: Any, ws_obj: Any) -> None:
            captured.append((ws_obj.id, ws_obj.name))

        def _lazy_alias(ws_id: str) -> str | None:
            from turnstone.core.memory import resolve_workstream

            return resolve_workstream(ws_id)

        mock_mgr = MagicMock()
        cfg = SessionEndpointConfig(
            permission_gate=None,
            manager_lookup=lambda _r: (mock_mgr, None),
            tenant_check=None,
            not_found_label="Workstream not found",
            audit_action_prefix="workstream",
            open_resolve_alias=_lazy_alias,
            open_post_load=_post_load,
        )
        handler = make_open_handler(cfg)
        app = Starlette(
            routes=[
                Mount(
                    "/v1",
                    routes=[
                        Route("/api/workstreams/{ws_id}/open", handler, methods=["POST"]),
                    ],
                ),
            ],
            middleware=[Middleware(_InjectAuthMiddleware)],
        )
        client = TestClient(app)

        # Already-loaded path: post_load must NOT fire (pre-lift
        # parity — the original handler returned early without any
        # post-load work in the already-loaded branch).
        mock_resolve.return_value = "ws-loaded"
        loaded_ws = MagicMock()
        loaded_ws.id = "ws-loaded"
        loaded_ws.name = "loaded-name"
        mock_mgr.get.return_value = loaded_ws
        r = client.post("/v1/api/workstreams/ws-loaded/open")
        assert r.status_code == 200
        assert r.json()["already_loaded"] is True
        assert captured == [], "post_load fired on the already-loaded shortcut"

        # Load-from-storage path: post_load fires with (request, ws).
        mock_resolve.return_value = "ws-fresh"
        mock_mgr.get.return_value = None
        opened_ws = MagicMock()
        opened_ws.id = "ws-fresh"
        opened_ws.name = "fresh-name"
        mock_mgr.open.return_value = opened_ws
        r = client.post("/v1/api/workstreams/ws-fresh/open")
        assert r.status_code == 200
        assert captured == [("ws-fresh", "fresh-name")]

    @patch("turnstone.core.memory.resolve_workstream")
    def test_open_500_message_uses_kind_noun_from_cfg(self, mock_resolve, _inject_storage):
        """``cfg.audit_action_prefix`` ("workstream" interactive,
        "coordinator" coord) is woven into the 500 error string so
        coord callers see ``"failed to open coordinator"`` and
        interactive callers see ``"failed to open workstream"``,
        matching the pre-lift wording on both sides. Pre-fix
        (Copilot review on PR #414) the message was hardcoded
        ``"failed to open workstream"`` for both kinds — coord
        callers got misleading text."""
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.routing import Mount, Route
        from starlette.testclient import TestClient

        def _lazy_alias(ws_id: str) -> str | None:
            from turnstone.core.memory import resolve_workstream

            return resolve_workstream(ws_id)

        mock_mgr = MagicMock()
        cfg = SessionEndpointConfig(
            permission_gate=None,
            manager_lookup=lambda _r: (mock_mgr, None),
            tenant_check=None,
            not_found_label="coordinator not found",
            audit_action_prefix="coordinator",  # coord-shaped cfg
            open_resolve_alias=_lazy_alias,
        )
        handler = make_open_handler(cfg)
        app = Starlette(
            routes=[
                Mount(
                    "/v1",
                    routes=[
                        Route("/api/workstreams/{ws_id}/open", handler, methods=["POST"]),
                    ],
                ),
            ],
            middleware=[Middleware(_InjectAuthMiddleware)],
        )
        client = TestClient(app)

        mock_resolve.return_value = "ws-fresh"
        mock_mgr.get.return_value = None
        mock_mgr.open.side_effect = RuntimeError("session factory blew up")

        r = client.post("/v1/api/workstreams/ws-fresh/open")
        assert r.status_code == 500
        body = r.json()
        # Per-kind noun in the message.
        assert "failed to open coordinator" in body["error"]
        # Correlation id present so support can match a log entry.
        assert "correlation_id=" in body["error"]
        # Exception text is NOT echoed (no internal-detail leak).
        assert "session factory blew up" not in body["error"]

    @patch("turnstone.core.memory.resolve_workstream")
    def test_open_swallows_post_load_exception(self, mock_resolve, _inject_storage):
        """A bug in ``open_post_load`` must NOT block the open from
        returning 200 — the workstream is already loaded by mgr.open
        and the post-load is observational. Mirrors the same swallow
        pattern in ``make_cancel_handler``'s ``cancel_forensics``
        wrapper."""
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.routing import Mount, Route
        from starlette.testclient import TestClient

        def _raises_post_load(_request: Any, _ws: Any) -> None:
            raise RuntimeError("post-load blew up")

        def _lazy_alias(ws_id: str) -> str | None:
            from turnstone.core.memory import resolve_workstream

            return resolve_workstream(ws_id)

        mock_mgr = MagicMock()
        cfg = SessionEndpointConfig(
            permission_gate=None,
            manager_lookup=lambda _r: (mock_mgr, None),
            tenant_check=None,
            not_found_label="Workstream not found",
            audit_action_prefix="workstream",
            open_resolve_alias=_lazy_alias,
            open_post_load=_raises_post_load,
        )
        handler = make_open_handler(cfg)
        app = Starlette(
            routes=[
                Mount(
                    "/v1",
                    routes=[
                        Route("/api/workstreams/{ws_id}/open", handler, methods=["POST"]),
                    ],
                ),
            ],
            middleware=[Middleware(_InjectAuthMiddleware)],
        )
        client = TestClient(app)

        mock_resolve.return_value = "ws-fresh"
        mock_mgr.get.return_value = None
        opened_ws = MagicMock()
        opened_ws.id = "ws-fresh"
        opened_ws.name = "fresh-name"
        mock_mgr.open.return_value = opened_ws

        r = client.post("/v1/api/workstreams/ws-fresh/open")
        assert r.status_code == 200
        assert r.json()["ws_id"] == "ws-fresh"


# ===========================================================================
# LIST interface settings
# ===========================================================================


class TestListInterfaceSettings:
    def test_list_defaults(self, settings_client):
        r = settings_client.get("/v1/api/admin/settings")
        assert r.status_code == 200
        settings = r.json()["settings"]
        keys = [s["key"] for s in settings]
        assert "interface.theme" in keys
        assert "interface.close_tab_action" in keys
        # All should be defaults when no config store
        for s in settings:
            assert s["source"] == "default"

    def test_list_only_interface_keys(self, settings_client):
        r = settings_client.get("/v1/api/admin/settings")
        settings = r.json()["settings"]
        for s in settings:
            assert s["key"].startswith("interface.")


# ===========================================================================
# UPDATE interface setting
# ===========================================================================


class TestUpdateInterfaceSetting:
    def test_update_theme(self, settings_client, _inject_storage):
        r = settings_client.post(
            "/v1/api/admin/settings/interface.theme",
            json={"value": "light"},
        )
        assert r.status_code == 200
        assert r.json()["value"] == "light"

    def test_update_via_put(self, settings_client, _inject_storage):
        r = settings_client.put(
            "/v1/api/admin/settings/interface.theme",
            json={"value": "dark"},
        )
        assert r.status_code == 200
        assert r.json()["value"] == "dark"

    def test_reject_non_interface_key(self, settings_client):
        r = settings_client.post(
            "/v1/api/admin/settings/judge.enabled",
            json={"value": True},
        )
        assert r.status_code == 400
        assert "interface" in r.json()["error"].lower()

    def test_reject_unknown_key(self, settings_client):
        r = settings_client.post(
            "/v1/api/admin/settings/interface.nonexistent",
            json={"value": "x"},
        )
        assert r.status_code == 400
        assert "unknown" in r.json()["error"].lower()

    def test_reject_missing_value(self, settings_client):
        r = settings_client.post(
            "/v1/api/admin/settings/interface.theme",
            json={},
        )
        assert r.status_code == 400
        assert "value" in r.json()["error"].lower()

    def test_reject_invalid_choice(self, settings_client):
        r = settings_client.post(
            "/v1/api/admin/settings/interface.theme",
            json={"value": "neon-pink"},
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# History / detail — interactive parity with the lifted factories
# ---------------------------------------------------------------------------
#
# Stage 2 ``history`` / ``detail`` verb lift adds these endpoints to the
# interactive surface as a feature gain (pre-lift only coord exposed
# them). The lifted factories live in :mod:`turnstone.core.session_routes`;
# coord parity coverage lives in :mod:`tests.test_coordinator_endpoints`.
# These tests pin the interactive wiring against the same factory.


def _interactive_endpoint_cfg(
    mock_mgr: Any,
    tenant_check: Any = None,
) -> SessionEndpointConfig:
    """Interactive-shaped cfg wired the same way ``server.py`` does.

    Shared by both :func:`_build_history_app` and :func:`_build_detail_app`
    — every field both factories actually read is present (the detail
    factory ignores ``list_kind`` since it relies on ``mgr.open()`` for
    cross-kind isolation, but the field is harmless to set).

    The optional ``tenant_check`` lets a regression test wire the same
    cross-tenant gate ``server.py`` uses (``_interactive_tenant_check``)
    so the lifted handlers can be exercised with the production-shape
    auth posture, not just the bypass shape.
    """
    return SessionEndpointConfig(
        permission_gate=None,  # auth middleware covers it
        manager_lookup=lambda _r: (mock_mgr, None),
        tenant_check=tenant_check,
        not_found_label="Workstream not found",
        audit_action_prefix="workstream",
        list_kind=WorkstreamKind.INTERACTIVE,
    )


def _build_history_asgi_app(
    mock_mgr: Any,
    storage: Any,
    tenant_check: Any = None,
) -> Starlette:
    """Raw ASGI app for the lifted history factory.

    Returned un-wrapped (no ``TestClient``) so the coalescing tests can
    drive CONCURRENT requests through ``httpx.ASGITransport`` on a
    private event loop — ``TestClient`` is synchronous and can only
    hold one request in flight at a time.
    """
    cfg = _interactive_endpoint_cfg(mock_mgr, tenant_check=tenant_check)
    handler = make_history_handler(cfg)
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route("/api/workstreams/{ws_id}/history", handler, methods=["GET"]),
                ],
            ),
        ],
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.workstreams = mock_mgr
    app.state.auth_storage = storage
    return app


def _build_history_app(
    mock_mgr: Any,
    storage: Any,
    tenant_check: Any = None,
) -> TestClient:
    return TestClient(_build_history_asgi_app(mock_mgr, storage, tenant_check=tenant_check))


def _build_detail_app(
    mock_mgr: Any,
    tenant_check: Any = None,
) -> TestClient:
    cfg = _interactive_endpoint_cfg(mock_mgr, tenant_check=tenant_check)
    handler = make_detail_handler(cfg)
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[Route("/api/workstreams/{ws_id}", handler, methods=["GET"])],
            ),
        ],
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.workstreams = mock_mgr
    return TestClient(app)


def _build_export_app(
    mock_mgr: Any,
    storage: Any,
    *,
    cfg: SessionEndpointConfig | None = None,
) -> TestClient:
    """Mount the lifted ``export`` factory at ``/{ws_id}/export``.

    Mirrors :func:`_build_history_app` — real factory, real storage on
    ``app.state.auth_storage``, driven via ``TestClient``. The optional
    ``cfg`` override lets the misconfig / cross-kind tests swap in a cfg
    with a deliberately wrong (or ``None``) ``list_kind``.
    """
    if cfg is None:
        cfg = _interactive_endpoint_cfg(mock_mgr)
    handler = make_export_handler(cfg)
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route("/api/workstreams/{ws_id}/export", handler, methods=["GET"]),
                ],
            ),
        ],
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.workstreams = mock_mgr
    app.state.auth_storage = storage
    return TestClient(app)


class TestExportInteractive:
    """Interactive coverage for the lifted, conversation-only
    ``GET /v1/api/workstreams/{ws_id}/export`` (issue #613)."""

    def test_happy_path_returns_json_download(self, _inject_storage):
        ws_id = "ws-export-1"
        _inject_storage.register_workstream(ws_id, kind="interactive", user_id="test-user")
        _inject_storage.save_message(ws_id, "user", "export me")
        _inject_storage.save_message(ws_id, "assistant", "exported")
        mock_ws = MagicMock()
        mock_ws.id = ws_id
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = mock_ws
        client = _build_export_app(mock_mgr, _inject_storage)

        r = client.get(f"/v1/api/workstreams/{ws_id}/export")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        assert r.headers["content-disposition"] == f'attachment; filename="{ws_id}.json"'
        assert r.headers["x-content-type-options"] == "nosniff"
        # Parse the actual bytes — conversation envelope with the seeded turns.
        body = json.loads(r.content)
        role_contents = [(m.get("role"), m.get("content")) for m in body["messages"]]
        assert "messages" in body
        assert ("user", "export me") in role_contents

    def test_serves_storage_only_workstream(self, _inject_storage):
        """A persisted-but-not-loaded interactive exports without
        rehydrating — same storage-fallback ladder history uses."""
        ws_id = "ws-export-cold"
        _inject_storage.register_workstream(ws_id, kind="interactive", user_id="test-user")
        _inject_storage.save_message(ws_id, "assistant", "from cold storage")
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = None  # not loaded
        client = _build_export_app(mock_mgr, _inject_storage)

        r = client.get(f"/v1/api/workstreams/{ws_id}/export")
        assert r.status_code == 200
        body = json.loads(r.content)
        contents = [m.get("content") for m in body["messages"]]
        assert "from cold storage" in contents

    def test_404_on_missing_ws_id(self, _inject_storage):
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = None
        client = _build_export_app(mock_mgr, _inject_storage)

        r = client.get("/v1/api/workstreams/no-such-ws/export")
        assert r.status_code == 404
        assert r.json()["error"] == "Workstream not found"

    def test_404_on_cross_kind_coord_ws_id(self, _inject_storage):
        """Cross-kind isolation on the storage fallback: a coord ws_id in
        shared storage 404s on the interactive export endpoint."""
        ws_id = "ws-export-coord"
        _inject_storage.register_workstream(ws_id, kind="coordinator", user_id="test-user")
        _inject_storage.save_message(ws_id, "user", "coord-only content")
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = None
        client = _build_export_app(mock_mgr, _inject_storage)

        r = client.get(f"/v1/api/workstreams/{ws_id}/export")
        assert r.status_code == 404
        assert "coord-only content" not in r.text

    def test_500_when_list_kind_misconfigured(self, _inject_storage):
        """A cfg mounted without ``list_kind`` fails loud (500) rather
        than leaking cross-kind rows through the storage fallback."""
        ws_id = "ws-export-misconfig"
        _inject_storage.register_workstream(ws_id, kind="interactive", user_id="test-user")
        _inject_storage.save_message(ws_id, "user", "should not leak")
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = None
        bad_cfg = SessionEndpointConfig(
            permission_gate=None,
            manager_lookup=lambda _r: (mock_mgr, None),
            tenant_check=None,
            not_found_label="Workstream not found",
            audit_action_prefix="workstream",
            list_kind=None,  # deliberately unset → fail loud
        )
        client = _build_export_app(mock_mgr, _inject_storage, cfg=bad_cfg)

        r = client.get(f"/v1/api/workstreams/{ws_id}/export")
        assert r.status_code == 500
        assert r.json()["error"] == "export handler misconfigured"
        assert "should not leak" not in r.text


class TestHistoryAgentStepsOverlay:
    """The history handler attaches a live task agent's stashed sub-trajectory to
    its ``task_agent`` tool_call (``agent_steps``) so the client rebuilds the
    card.  A cold ws / evicted entry has none → no overlay (honest flat row)."""

    def _save_task_agent_turn(self, storage, ws_id):
        storage.register_workstream(ws_id, kind="interactive", user_id="test-user")
        storage.save_message(ws_id, "user", "kick off")
        tc_json = json.dumps(
            [
                {
                    "id": "task1",
                    "type": "function",
                    "function": {
                        "name": "task_agent",
                        "arguments": '{"prompt":"find call sites"}',
                    },
                }
            ]
        )
        storage.save_message(ws_id, "assistant", "Working", tool_calls=tc_json)
        storage.save_message(ws_id, "tool", "4 call sites found", tool_call_id="task1")

    def test_attaches_agent_steps_from_live_stash(self, _inject_storage):
        ws_id = "ws-recall-warm"
        self._save_task_agent_turn(_inject_storage, ws_id)
        steps = [
            {
                "id": "task1::c1",
                "name": "search",
                "arguments": "{}",
                "output": "12 matches",
                "is_error": False,
            }
        ]
        mock_ws = MagicMock()
        mock_ws.id = ws_id
        mock_ws.ui._pending_approval = None
        mock_ws.ui.get_agent_trajectory = lambda cid: steps if cid == "task1" else None
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = mock_ws

        r = _build_history_app(mock_mgr, _inject_storage).get(
            f"/v1/api/workstreams/{ws_id}/history"
        )
        assert r.status_code == 200
        assistant = next(m for m in r.json()["messages"] if m.get("role") == "assistant")
        tc = assistant["tool_calls"][0]
        assert tc["id"] == "task1"
        assert tc["agent_steps"] == steps

    def test_no_overlay_when_not_retained(self, _inject_storage):
        # Cold / evicted: get_agent_trajectory returns None → no agent_steps key,
        # so the client renders the flat parent record (never a 0-step card).
        ws_id = "ws-recall-cold"
        self._save_task_agent_turn(_inject_storage, ws_id)
        mock_ws = MagicMock()
        mock_ws.id = ws_id
        mock_ws.ui._pending_approval = None
        mock_ws.ui.get_agent_trajectory = lambda cid: None
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = mock_ws

        r = _build_history_app(mock_mgr, _inject_storage).get(
            f"/v1/api/workstreams/{ws_id}/history"
        )
        assert r.status_code == 200
        assistant = next(m for m in r.json()["messages"] if m.get("role") == "assistant")
        assert "agent_steps" not in assistant["tool_calls"][0]


class TestHistoryInteractive:
    """Interactive parity for the lifted ``GET /v1/api/workstreams/{ws_id}/history``."""

    def test_returns_messages_for_in_memory_workstream(self, _inject_storage):
        ws_id = "ws-int-1"
        _inject_storage.register_workstream(ws_id, kind="interactive", user_id="test-user")
        _inject_storage.save_message(ws_id, "user", "hello interactive")
        mock_ws = MagicMock()
        mock_ws.id = ws_id
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = mock_ws
        client = _build_history_app(mock_mgr, _inject_storage)

        r = client.get(f"/v1/api/workstreams/{ws_id}/history")
        assert r.status_code == 200
        body = r.json()
        assert body["ws_id"] == ws_id
        assert any(
            m.get("role") == "user" and m.get("content") == "hello interactive"
            for m in body["messages"]
        )

    def test_serves_storage_only_workstream(self, _inject_storage):
        """Persisted-but-not-loaded interactives serve history without
        rehydrating — same shape as coord. Pre-lift interactive had no
        history endpoint at all, so this is a feature gain."""
        ws_id = "ws-cold"
        _inject_storage.register_workstream(ws_id, kind="interactive", user_id="test-user")
        _inject_storage.save_message(ws_id, "assistant", "from cold storage")
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = None  # not loaded
        client = _build_history_app(mock_mgr, _inject_storage)

        r = client.get(f"/v1/api/workstreams/{ws_id}/history")
        assert r.status_code == 200
        assert any(m.get("content") == "from cold storage" for m in r.json()["messages"])

    def test_404_on_missing_ws_id(self, _inject_storage):
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = None
        client = _build_history_app(mock_mgr, _inject_storage)

        r = client.get("/v1/api/workstreams/no-such-ws/history")
        assert r.status_code == 404
        assert r.json()["error"] == "Workstream not found"

    def test_404_on_cross_kind_coord_ws_id(self, _inject_storage):
        """Cross-kind isolation on the storage fallback: a coord ws_id
        in shared storage 404s on the interactive history endpoint.
        Mirrors :func:`test_history_404_when_kind_interactive` in
        ``tests.test_coordinator_endpoints``."""
        ws_id = "ws-coord-1"
        _inject_storage.register_workstream(ws_id, kind="coordinator", user_id="test-user")
        _inject_storage.save_message(ws_id, "user", "coord-only content")
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = None
        client = _build_history_app(mock_mgr, _inject_storage)

        r = client.get(f"/v1/api/workstreams/{ws_id}/history")
        assert r.status_code == 404
        assert "coord-only content" not in r.text

    def test_clamps_limit_query_param(self, _inject_storage):
        """Same [1, 500] clamp as coord — pre-lift interactive had no
        history endpoint to enforce a clamp, so this is the
        first-time bound. Out-of-range / unparseable values fall
        back to defaults instead of erroring."""
        ws_id = "ws-clamp"
        _inject_storage.register_workstream(ws_id, kind="interactive", user_id="test-user")
        for i in range(4):
            _inject_storage.save_message(ws_id, "user", f"msg-{i}")
        mock_ws = MagicMock()
        mock_ws.id = ws_id
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = mock_ws
        client = _build_history_app(mock_mgr, _inject_storage)

        base = f"/v1/api/workstreams/{ws_id}/history"
        # No limit → default 100 returns all 4.
        assert len(client.get(base).json()["messages"]) == 4
        # limit=2 → only 2.
        assert len(client.get(base, params={"limit": 2}).json()["messages"]) == 2
        # 0 → clamps to 1.
        assert len(client.get(base, params={"limit": 0}).json()["messages"]) == 1
        # Garbage → falls back to 100.
        assert client.get(base, params={"limit": "garbage"}).status_code == 200
        # Above-cap → clamps to 500 (response is still 200; we have 4 rows).
        assert client.get(base, params={"limit": 999}).status_code == 200

    def test_returns_partial_trailing_turn_during_tool_execution(self, _inject_storage):
        """The ``/history`` REST endpoint is a *display* read and must
        surface partial state.  When the operator refreshes the page
        mid-tool-execution — assistant ``tool_calls`` saved, only some
        results saved — the trailing turn must come back on the wire so
        the UI can render what the operator was watching live.

        Storage's ``load_messages`` defaults to a repair pass that
        strips this exact shape (correct for ``session.resume``, wrong
        for display).  ``make_history_handler`` must opt out via
        ``repair=False``; flipping that flag back on breaks this test.
        """
        import json

        ws_id = "ws-mid-exec"
        _inject_storage.register_workstream(ws_id, kind="interactive", user_id="test-user")
        _inject_storage.save_message(ws_id, "user", "kick off")
        tc_json = json.dumps(
            [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"ls"}'},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"pwd"}'},
                },
            ]
        )
        _inject_storage.save_message(ws_id, "assistant", "Working", tool_calls=tc_json)
        _inject_storage.save_message(ws_id, "tool", "file.txt", tool_call_id="call_1")
        # call_2 result not yet persisted — operator refreshes here.
        mock_ws = MagicMock()
        mock_ws.id = ws_id
        # Mid-EXECUTION, not awaiting approval: any approval already
        # resolved, so ``_pending_approval`` is None.  The trailing orphan
        # tool turn must therefore RENDER (``pending`` absent) — marking it
        # pending is the fresh-connect-during-execution bug (the renderer
        # skips pending turns, so the tool call vanishes until a reconnect
        # replays the buffered events).
        mock_ws.ui._pending_approval = None
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = mock_ws
        client = _build_history_app(mock_mgr, _inject_storage)

        r = client.get(f"/v1/api/workstreams/{ws_id}/history")
        assert r.status_code == 200
        messages = r.json()["messages"]
        roles = [m.get("role") for m in messages]
        # All three rows survive — the trailing assistant + partial
        # tool result are what the operator was watching live.  The
        # default-repair shape would have been just ``["user"]``.
        assert roles == ["user", "assistant", "tool"]
        # And the trailing tool-call turn is NOT pending → renders.
        assistant_turn = next(m for m in messages if m.get("role") == "assistant")
        assert assistant_turn.get("pending") is not True

        # Confirm the default-repair path collapses this to just the
        # user message — locks in the regression contract.
        with_repair = _inject_storage.load_messages(ws_id, repair=True)
        assert [m.get("role") for m in with_repair] == ["user"]

    def test_trailing_tool_turn_pending_when_awaiting_approval(self, _inject_storage):
        """Counterpart to the execution case: when the live session IS
        awaiting approval (``_pending_approval`` set), the trailing orphan
        tool-call turn is marked ``pending`` so the renderer skips the static
        block — the SSE replay re-emits the interactive approve_request prompt
        to render it instead.  Keeps the ``/history`` ``pending`` flag in
        lockstep with the live approval signal (``_interactive_events_replay``).
        """
        import json

        ws_id = "ws-awaiting"
        _inject_storage.register_workstream(ws_id, kind="interactive", user_id="test-user")
        _inject_storage.save_message(ws_id, "user", "kick off")
        tc_json = json.dumps(
            [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"ls"}'},
                }
            ]
        )
        _inject_storage.save_message(ws_id, "assistant", "Working", tool_calls=tc_json)
        # No tool result yet AND the session is parked awaiting approval.
        mock_ws = MagicMock()
        mock_ws.id = ws_id
        mock_ws.ui._pending_approval = {"type": "approve_request", "items": []}
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = mock_ws
        client = _build_history_app(mock_mgr, _inject_storage)

        r = client.get(f"/v1/api/workstreams/{ws_id}/history")
        assert r.status_code == 200
        messages = r.json()["messages"]
        assistant_turn = next(m for m in messages if m.get("role") == "assistant")
        assert assistant_turn.get("pending") is True

    def test_history_returns_cursor_and_trims_inflight_orphan_when_replayable(
        self, _inject_storage
    ):
        """Fresh-connect fast-forward, end to end: an executing in-flight
        orphan (assistant tool_calls saved, no results) whose live ring
        buffer can replay → /history OMITS that turn and returns
        ``cursor`` = the resolved boundary's event_id.  The client opens
        its initial SSE with that cursor so the delta rebuilds the turn.
        """
        ws_id = "ws-cursor"
        _inject_storage.register_workstream(ws_id, kind="interactive", user_id="test-user")
        _inject_storage.save_message(ws_id, "user", "kick off", event_id=10)
        tc_json = json.dumps(
            [{"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]
        )
        _inject_storage.save_message(ws_id, "assistant", "Working", tool_calls=tc_json, event_id=12)
        # call_1 result not yet persisted — executing in-flight orphan.
        mock_ws = MagicMock()
        mock_ws.id = ws_id
        mock_ws.ui._pending_approval = None  # executing, not awaiting
        mock_ws.ui.can_replay_from.return_value = True  # buffer can fast-forward
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = mock_ws
        client = _build_history_app(mock_mgr, _inject_storage)

        r = client.get(f"/v1/api/workstreams/{ws_id}/history")
        assert r.status_code == 200
        body = r.json()
        # Cursor = the resolved boundary (the user row's event_id), NOT the
        # orphan assistant's stamp.
        assert body["cursor"] == 10
        # The executing orphan turn is OMITTED — it fast-forwards via the
        # SSE delta, disjoint from this committed snapshot.
        assert [m.get("role") for m in body["messages"]] == ["user"]
        # The gate was consulted with the resolved-boundary cursor.
        mock_ws.ui.can_replay_from.assert_called_once_with(10)

    def test_history_keeps_orphan_and_nulls_cursor_when_not_replayable(self, _inject_storage):
        """Counterpart: when the live buffer can't fast-forward (reloaded /
        evicted), /history keeps the in-flight turn (the #610 history-
        rendered block) and returns ``cursor: null`` — the client connects
        fresh to the synthetic-snapshot floor, never leaving the turn
        unrenderable."""
        ws_id = "ws-cursor-reload"
        _inject_storage.register_workstream(ws_id, kind="interactive", user_id="test-user")
        _inject_storage.save_message(ws_id, "user", "kick off", event_id=10)
        tc_json = json.dumps(
            [{"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]
        )
        _inject_storage.save_message(ws_id, "assistant", "Working", tool_calls=tc_json, event_id=12)
        mock_ws = MagicMock()
        mock_ws.id = ws_id
        mock_ws.ui._pending_approval = None
        mock_ws.ui.can_replay_from.return_value = False  # empty/evicted buffer
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = mock_ws
        client = _build_history_app(mock_mgr, _inject_storage)

        r = client.get(f"/v1/api/workstreams/{ws_id}/history")
        assert r.status_code == 200
        body = r.json()
        assert body["cursor"] is None
        # Orphan turn stays in /history (renders its #610 block); not pending.
        assert [m.get("role") for m in body["messages"]] == ["user", "assistant"]
        assistant_turn = next(m for m in body["messages"] if m.get("role") == "assistant")
        assert assistant_turn.get("pending") is not True

    def test_history_does_not_synthesize_orphan_results(self, _inject_storage):
        """``repair=False`` via ``/history`` must NOT splice synthetic
        ``"Tool execution was cancelled."`` rows for mid-conversation
        orphaned tool_calls — the operator never saw those rows, and
        showing them would invent UI content that doesn't reflect
        persisted state.
        """
        import json

        ws_id = "ws-orphan-mid"
        _inject_storage.register_workstream(ws_id, kind="interactive", user_id="test-user")
        _inject_storage.save_message(ws_id, "user", "first")
        tc_json = json.dumps(
            [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"ls"}'},
                },
            ]
        )
        _inject_storage.save_message(ws_id, "assistant", "Working", tool_calls=tc_json)
        # Cancel landed before any tool result — next turn happens.
        _inject_storage.save_message(ws_id, "user", "second")
        _inject_storage.save_message(ws_id, "assistant", "ok")
        mock_ws = MagicMock()
        mock_ws.id = ws_id
        mock_ws.ui._pending_approval = None  # cancelled, not awaiting approval
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = mock_ws
        client = _build_history_app(mock_mgr, _inject_storage)

        r = client.get(f"/v1/api/workstreams/{ws_id}/history")
        assert r.status_code == 200
        roles = [m.get("role") for m in r.json()["messages"]]
        # No synthetic tool row spliced after the orphaned tool_calls.
        assert roles == ["user", "assistant", "user", "assistant"]
        assert all(m.get("role") != "tool" for m in r.json()["messages"])


class _GatedStorage:
    """Delegates to the real backend; ``load_messages`` counts entries,
    optionally raises, and blocks on ``gate`` until the test releases it
    (a pre-set gate is a pass-through).  The count-then-block ordering
    is load-bearing for the coalescing tests: a request that did NOT
    join an existing flight bumps ``load_calls`` at entry — before the
    gate can block it — so the counter distinguishes join from
    second-flight without any timing assumptions."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.gate = threading.Event()
        self.load_calls = 0
        self.fail_next = 0
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def load_messages(self, ws_id: str, **kwargs: Any) -> Any:
        with self._lock:
            self.load_calls += 1
            should_fail = self.fail_next > 0
            if should_fail:
                self.fail_next -= 1
        if not self.gate.wait(timeout=10):
            raise TimeoutError("test gate never released")
        if should_fail:
            raise RuntimeError("transient load failure (test)")
        return self._inner.load_messages(ws_id, **kwargs)


async def _until(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.01)


class TestHistoryCoalescing:
    """Single-flight coalescing of concurrent ``/history`` requests (#884).

    The matrix these tests assert: join-vs-miss × gates-per-request ×
    failed-vs-clean shared draw × key isolation × no cross-flight
    caching × owner cancellation.  Driven through ``httpx.ASGITransport``
    on a private loop because ``TestClient`` cannot hold two requests in
    flight at once.

    Determinism scheme: every synchronization point is a positive
    observable edge — the owner's arrival on ``load_calls == 1`` (it
    entered ``load_messages`` and parked on the gate), and the JOIN on
    the handler's ``ws.history.coalesced`` debug record, which is
    emitted synchronously at the map-hit branch before the joiner
    awaits the flight (capture through stdlib handlers is verified:
    turnstone's structlog config routes to stdlib logging, so pytest's
    ``caplog`` sees it).  The join-proof stays counter-based on top:
    a request that did NOT join bumps ``load_calls`` before the gate
    can block it (see ``_GatedStorage``), so ``load_calls == 1`` after
    the join record proves sharing."""

    def _register_ws(self, storage: Any, ws_id: str) -> None:
        storage.register_workstream(ws_id, kind="interactive", user_id="test-user")
        storage.save_message(ws_id, "user", "hello")
        storage.save_message(ws_id, "assistant", "hi there")

    def _live_mgr(self, ws_id: str) -> MagicMock:
        mock_ws = MagicMock()
        mock_ws.id = ws_id
        mock_ws.ui._pending_approval = None
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = mock_ws
        return mock_mgr

    def _counting_tenant(self) -> tuple[Any, dict[str, int]]:
        calls = {"n": 0}

        def check(request: Any, ws_id: str, mgr: Any) -> Any:
            calls["n"] += 1
            if request.headers.get("x-test-deny"):
                return JSONResponse({"error": "Workstream not found"}, status_code=404)
            return None

        return check, calls

    def _scaffold(
        self, storage: Any, ws_id: str
    ) -> tuple[_GatedStorage, dict[str, int], Starlette, str]:
        self._register_ws(storage, ws_id)
        gated = _GatedStorage(storage)
        tenant, tenant_calls = self._counting_tenant()
        app = _build_history_asgi_app(self._live_mgr(ws_id), gated, tenant_check=tenant)
        return gated, tenant_calls, app, f"/v1/api/workstreams/{ws_id}/history"

    async def _drive_two_joiners(
        self,
        app: Starlette,
        url: str,
        gated: _GatedStorage,
        caplog: Any,
        *,
        cancel_owner: bool = False,
    ) -> tuple[Any, httpx.Response]:
        """Drive two concurrent requests through one shared flight.

        Every wait is a positive observable edge.  Owner arrival:
        ``load_calls == 1`` (it entered ``load_messages`` and parked on
        the gate).  JOIN: the handler logs ``ws.history.coalesced``
        synchronously at the map-hit branch, before the joiner awaits
        the flight — waiting on that record (via ``caplog``; capture
        verified, structlog routes to stdlib logging) proves t2 joined
        while the flight is still gated, with no wall-clock beat.  The
        trailing ``"ws="`` in the match keeps it from also matching
        ``ws.history.coalesced_retry``.  The counter join-proof stays
        on top: a request that did NOT join bumps ``load_calls`` at
        ``load_messages`` entry, BEFORE the gate can block it (see
        ``_GatedStorage``).

        ``cancel_owner=True`` swaps the clean tail for the
        owner-disconnect tail (cancel t1 before releasing the gate,
        await the joiner, collect t1's outcome) — on that path the
        first tuple member is the owner's ``CancelledError``, not a
        ``Response``, hence the loose first-slot type.
        """
        caplog.set_level(logging.DEBUG, logger="turnstone.core.session_routes")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            t1 = asyncio.create_task(c.get(url))
            await _until(lambda: gated.load_calls == 1)
            t2 = asyncio.create_task(c.get(url))
            await _until(
                lambda: any("ws.history.coalesced ws=" in r.getMessage() for r in caplog.records)
            )
            assert gated.load_calls == 1
            if cancel_owner:
                t1.cancel()
                gated.gate.set()
                r2 = await t2
                (r1,) = await asyncio.gather(t1, return_exceptions=True)
                return r1, r2
            gated.gate.set()
            r1, r2 = await asyncio.gather(t1, t2)
            return r1, r2

    def test_concurrent_requests_share_one_flight(self, _inject_storage: Any, caplog: Any) -> None:
        gated, _tenant_calls, app, url = self._scaffold(_inject_storage, "ws-flight-share")

        r1, r2 = asyncio.run(self._drive_two_joiners(app, url, gated, caplog))
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json() == r2.json()
        contents = [m.get("content") for m in r1.json()["messages"]]
        assert contents == ["hello", "hi there"]
        assert gated.load_calls == 1

    def test_failed_shared_draw_not_fanned_out_to_joiner(
        self, _inject_storage: Any, caplog: Any
    ) -> None:
        """A transient ``load_messages`` failure in the shared draw
        yields the owner's 200-empty (same as a lone request today) but
        the JOINER retries independently and gets the real rows — one
        storage blip must not wipe every coalesced pane."""
        gated, _tenant_calls, app, url = self._scaffold(_inject_storage, "ws-flight-fail")
        gated.fail_next = 1

        r1, r2 = asyncio.run(self._drive_two_joiners(app, url, gated, caplog))
        assert r1.status_code == 200
        assert r1.json()["messages"] == []
        assert r2.status_code == 200
        assert [m.get("content") for m in r2.json()["messages"]] == ["hello", "hi there"]
        # Owner draw + joiner retry — never a third.
        assert gated.load_calls == 2

    def test_gates_run_per_request_before_join(self, _inject_storage: Any) -> None:
        """A caller failing its own tenant gate gets its 404 while the
        flight is still in the air — it never joins and never triggers
        a reconstruction of its own."""
        gated, _tenant_calls, app, url = self._scaffold(_inject_storage, "ws-flight-deny")

        async def run() -> tuple[httpx.Response, httpx.Response]:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                t1 = asyncio.create_task(c.get(url))
                await _until(lambda: gated.load_calls == 1)
                r2 = await c.get(url, headers={"x-test-deny": "1"})
                assert gated.load_calls == 1
                gated.gate.set()
                r1 = await t1
                return r1, r2

        r1, r2 = asyncio.run(run())
        assert r1.status_code == 200
        assert r2.status_code == 404
        assert gated.load_calls == 1

    def test_flight_key_includes_limit(self, _inject_storage: Any) -> None:
        """A limit=1 caller must not receive the limit-100 payload:
        different limits are different flights."""
        gated, _tenant_calls, app, url = self._scaffold(_inject_storage, "ws-flight-limit")

        async def run() -> tuple[httpx.Response, httpx.Response]:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                t1 = asyncio.create_task(c.get(url))
                await _until(lambda: gated.load_calls == 1)
                t2 = asyncio.create_task(c.get(url, params={"limit": 1}))
                # Positively waits for the SECOND reconstruction to
                # enter load_messages while the first is still gated —
                # the limit=1 request did not join.
                await _until(lambda: gated.load_calls == 2)
                gated.gate.set()
                r1, r2 = await asyncio.gather(t1, t2)
                return r1, r2

        r1, r2 = asyncio.run(run())
        assert len(r1.json()["messages"]) == 2
        assert len(r2.json()["messages"]) == 1
        assert gated.load_calls == 2

    def test_no_result_reuse_across_completed_flights(self, _inject_storage: Any) -> None:
        """The flights map is a single-flight, not a cache: a request
        arriving after a flight completed reconstructs afresh."""
        gated, _tenant_calls, app, url = self._scaffold(_inject_storage, "ws-flight-seq")
        gated.gate.set()  # pass-through

        async def run() -> tuple[httpx.Response, httpx.Response]:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                r1 = await c.get(url)
                r2 = await c.get(url)
                return r1, r2

        r1, r2 = asyncio.run(run())
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json() == r2.json()
        assert gated.load_calls == 2

    def test_decoration_failure_is_shared_not_retried(
        self, _inject_storage: Any, caplog: Any
    ) -> None:
        """The inverse of the load-failure test: a decoration/projection
        failure AFTER a successful load is degraded-but-non-empty, so it
        is shared to joiners as-is — ``load_failed`` stays False and no
        joiner retry fires (``load_calls`` stays 1, vs the load-failure
        test's 2).  Pins the two failure modes apart: conflating them
        (e.g. setting ``load_failed`` in the decoration except) would
        turn every shared degraded draw into a double reconstruction."""
        gated, _tenant_calls, app, url = self._scaffold(_inject_storage, "ws-flight-decor")

        with patch(
            "turnstone.core.history_decoration.project_history_messages",
            side_effect=RuntimeError("projection failure (test)"),
        ) as fake_project:
            r1, r2 = asyncio.run(self._drive_two_joiners(app, url, gated, caplog))
        # The degraded path must actually have run — without this, a
        # fixture whose cursor is None either way would let an
        # ineffective patch pass the assertions below vacuously.
        assert fake_project.called
        assert r1.status_code == 200
        assert r2.status_code == 200
        # Degraded (un-projected, cursor=None) but NON-empty and identical —
        # the joiner shared the draw instead of re-reconstructing.
        assert r1.json() == r2.json()
        assert r1.json()["messages"]
        assert r1.json()["cursor"] is None
        assert gated.load_calls == 1

    def test_owner_disconnect_leaves_joiner_completing(
        self, _inject_storage: Any, caplog: Any
    ) -> None:
        """The flight is a detached task: cancelling the request that
        CREATED it (client disconnect) must not cancel the shared
        reconstruction a joiner is awaiting."""
        gated, _tenant_calls, app, url = self._scaffold(_inject_storage, "ws-flight-cancel")

        r1, r2 = asyncio.run(self._drive_two_joiners(app, url, gated, caplog, cancel_owner=True))
        assert isinstance(r1, asyncio.CancelledError)
        assert r2.status_code == 200
        assert [m.get("content") for m in r2.json()["messages"]] == ["hello", "hi there"]
        assert gated.load_calls == 1


class TestBuildHistorySystemTurnPropagation:
    """``project_history_messages`` surfaces first-class operator-context
    ``system`` rows (``_source`` → ``source``) so a tab reconnecting via
    ``/history`` renders the same operator bubble the originating tab saw
    via the live ``system_turn`` SSE event.  The legacy ``_reminders``
    projection lane is gone.
    """

    def test_system_turn_surfaces_source(self):
        history = project_history_messages(
            [{"role": "system", "_source": "correction", "content": "watch out"}]
        )
        assert history[0]["role"] == "system"
        assert history[0]["content"] == "watch out"
        assert history[0]["source"] == "correction"

    def test_legacy_reminders_column_not_projected(self):
        history = project_history_messages(
            [
                {
                    "role": "user",
                    "content": "ah no",
                    "_reminders": [{"type": "correction", "text": "watch out"}],
                }
            ]
        )
        assert history[0]["content"] == "ah no"
        assert "reminders" not in history[0]

    def test_clean_message_passes_through_unchanged(self):
        history = project_history_messages([{"role": "user", "content": "just a normal message"}])
        assert history[0]["content"] == "just a normal message"
        assert "reminders" not in history[0]

    def test_assistant_content_with_literal_reminder_tag_unchanged(self):
        """Assistant output may legitimately reference the tag (e.g. when
        the model is explaining the reminder system itself).  No
        transformation should ever apply to assistant content."""
        content = "Here is a [start system-reminder] tag in assistant output."
        history = project_history_messages([{"role": "assistant", "content": content}])
        assert history[0]["content"] == content


class TestBuildHistoryToolContentCoercion:
    """The ``/history`` projection coerces list-typed tool content to a
    joined string for the renderers.  Operator context no longer rides the
    tool envelope — it is first-class ``system`` rows — so there is no
    advisory extraction here anymore.
    """

    def test_plain_tool_content_passes_through(self):
        msgs: list[dict] = [{"role": "tool", "tool_call_id": "call_a", "content": "plain output"}]
        decorate_history_messages(msgs, {}, {})
        history = project_history_messages(msgs)
        assert history[0]["content"] == "plain output"
        assert "advisories" not in history[0]

    def test_list_content_joined_to_string(self):
        """List-typed tool output (image / structured MCP results) is reduced
        to its joined text parts; non-text parts (image_url) are dropped —
        the renderers consume a string."""
        list_content = [
            {"type": "text", "text": "the chart shows X"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
        ]
        history = project_history_messages(
            [{"role": "tool", "tool_call_id": "call_a", "content": list_content}]
        )
        assert history[0]["content"] == "the chart shows X"
        assert "advisories" not in history[0]


class TestDetailInteractive:
    """Interactive parity for the lifted ``GET /v1/api/workstreams/{ws_id}``.

    The lifted ``make_detail_handler`` factory never reads storage —
    cross-kind isolation is enforced inside ``mgr.open()`` and the
    response is built from in-memory ``Workstream`` fields. The
    ``mgr.open`` calls are mocked via ``MagicMock`` here, so the
    storage-registry side effect that ``_inject_storage`` would
    otherwise provide is irrelevant; the fixture is intentionally
    omitted from these methods (unlike :class:`TestHistoryInteractive`
    where the storage backend serves the message rows).
    """

    def test_returns_workstream_fields(self):
        ws_id = "ws-detail-1"
        ws_state = MagicMock()
        ws_state.value = "idle"
        loaded_ws = MagicMock()
        loaded_ws.id = ws_id
        loaded_ws.name = "my-interactive"
        loaded_ws.state = ws_state
        loaded_ws.user_id = "test-user"
        loaded_ws.kind = "interactive"
        # No pending approval — leave .ui's MagicMock attrs alone; the
        # handler isinstance-checks ``_pending_approval`` against ``dict``
        # before treating it as live, so MagicMock attribute pollution
        # doesn't trigger the pending path.
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = loaded_ws
        client = _build_detail_app(mock_mgr)

        r = client.get(f"/v1/api/workstreams/{ws_id}")
        assert r.status_code == 200
        body = r.json()
        assert body == {
            "ws_id": ws_id,
            "name": "my-interactive",
            "state": "idle",
            "user_id": "test-user",
            "kind": "interactive",
            "pending_approval": False,
            "pending_approval_details": [],
        }

    def test_pending_approval_fields_propagate_from_ui(self):
        """When the workstream's UI is parked on an approval, the detail
        response surfaces ``pending_approval=True`` + the serialized
        ``pending_approval_detail`` so a freshly-loaded chat tab can
        paint the inline gate without waiting for the SSE
        ``approve_request`` replay (which would otherwise produce a
        brief ``--running`` flash on reload)."""
        ws_id = "ws-pending-1"
        ws_state = MagicMock()
        ws_state.value = "attention"
        loaded_ws = MagicMock()
        loaded_ws.id = ws_id
        loaded_ws.name = "coord-1"
        loaded_ws.state = ws_state
        loaded_ws.user_id = "test-user"
        loaded_ws.kind = "coordinator"
        # Realistic _pending_approval shape (mirrors what
        # SessionUIBase.approve_tools assigns) + a serializer that
        # returns the merged-with-verdicts payload.
        loaded_ws.ui._pending_approval = {
            "type": "approve_request",
            "items": [
                {
                    "call_id": "c-1",
                    "func_name": "spawn_workstream",
                    "needs_approval": True,
                },
            ],
            "judge_pending": True,
        }
        loaded_ws.ui.serialize_pending_approval_details = MagicMock(
            return_value=[
                {
                    "cycle_id": "cyc-1",
                    "call_id": "c-1",
                    "judge_pending": True,
                    "items": [
                        {
                            "call_id": "c-1",
                            "func_name": "spawn_workstream",
                            "needs_approval": True,
                            "heuristic_verdict": {
                                "recommendation": "approve",
                                "risk_level": "low",
                                "confidence": 0.9,
                            },
                        }
                    ],
                }
            ]
        )
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = loaded_ws
        client = _build_detail_app(mock_mgr)

        r = client.get(f"/v1/api/workstreams/{ws_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["pending_approval"] is True
        details = body["pending_approval_details"]
        assert len(details) == 1
        assert details[0]["cycle_id"] == "cyc-1"
        assert details[0]["call_id"] == "c-1"
        assert details[0]["judge_pending"] is True
        items = details[0]["items"]
        assert len(items) == 1
        assert items[0]["func_name"] == "spawn_workstream"
        assert items[0]["needs_approval"] is True

    def test_pending_serializer_failure_falls_back_to_bool_only(self):
        """A malformed verdict that crashes ``serialize_pending_approval_detail``
        must NOT fail the detail response — the boolean still informs
        the UI that an approval is pending; SSE replay carries the
        authoritative payload.  Defensive against a future serializer
        regression silently 500ing every page load."""
        ws_id = "ws-pending-broken"
        ws_state = MagicMock()
        ws_state.value = "attention"
        loaded_ws = MagicMock()
        loaded_ws.id = ws_id
        loaded_ws.name = "coord-broken"
        loaded_ws.state = ws_state
        loaded_ws.user_id = "test-user"
        loaded_ws.kind = "coordinator"
        loaded_ws.ui._pending_approval = {"items": []}
        loaded_ws.ui.serialize_pending_approval_details = MagicMock(
            side_effect=RuntimeError("verdict object is malformed"),
        )
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = loaded_ws
        client = _build_detail_app(mock_mgr)

        r = client.get(f"/v1/api/workstreams/{ws_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["pending_approval"] is True
        assert body["pending_approval_details"] == []

    def test_lazy_rehydrates_on_miss(self):
        """``mgr.get`` miss → ``mgr.open`` rehydrate. Same flow as coord;
        pre-lift interactive had no detail endpoint so this is the
        first time the rehydrate path is exercised on this surface."""
        ws_id = "ws-cold-detail"
        ws_state = MagicMock()
        ws_state.value = "closed"
        rehydrated = MagicMock()
        rehydrated.id = ws_id
        rehydrated.name = "rehydrated"
        rehydrated.state = ws_state
        rehydrated.user_id = "owner"
        rehydrated.kind = "interactive"
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = None
        mock_mgr.open.return_value = rehydrated
        client = _build_detail_app(mock_mgr)

        r = client.get(f"/v1/api/workstreams/{ws_id}")
        assert r.status_code == 200
        assert r.json()["name"] == "rehydrated"
        mock_mgr.open.assert_called_once_with(ws_id)

    def test_404_on_missing_ws_id(self):
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = None
        # ``mgr.open`` returns None for missing rows / kind mismatch /
        # tombstoned rows — all 404 with the per-kind label.
        mock_mgr.open.return_value = None
        client = _build_detail_app(mock_mgr)

        r = client.get("/v1/api/workstreams/no-such-ws")
        assert r.status_code == 404
        assert r.json()["error"] == "Workstream not found"

    def test_503_on_session_factory_misconfig(self):
        """``ValueError`` from ``mgr.open`` (e.g. a model alias that no
        longer resolves) surfaces as 503 with the factory's
        remediation text — not a correlation-id'd 500. Mirrors
        :func:`make_open_handler`."""
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = None
        mock_mgr.open.side_effect = ValueError("alias 'gone' no longer resolves")
        client = _build_detail_app(mock_mgr)

        r = client.get("/v1/api/workstreams/ws-misconfig")
        assert r.status_code == 503
        assert "alias 'gone' no longer resolves" in r.json()["error"]

    def test_correlation_id_on_unexpected_rehydrate_failure(self):
        """Bare ``Exception`` from ``mgr.open`` → 500 + correlation_id +
        per-kind noun in user-facing message. Exception text is NOT
        echoed (no internal-detail leak)."""
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = None
        mock_mgr.open.side_effect = RuntimeError("internal stack frame leak")
        client = _build_detail_app(mock_mgr)

        r = client.get("/v1/api/workstreams/ws-broken")
        assert r.status_code == 500
        body = r.json()
        assert "internal stack frame leak" not in body["error"]
        assert "correlation_id=" in body["error"]
        # Per-kind noun via cfg.audit_action_prefix.
        assert "workstream" in body["error"]


class TestTenantCheckOnReadEndpoints:
    """Regression coverage for the cross-tenant gate on the lifted
    ``GET /workstreams/{ws_id}`` (detail) and ``/history`` endpoints.

    Both handlers used to skip ``cfg.tenant_check`` while every other
    lifted session verb invoked it.  Pre-PR-447 the gap was a minor
    info leak (5 display fields on detail; conversation history); PR
    #447 made it real by adding ``pending_approval_detail`` to detail
    (tool previews + LLM judge reasoning).  These tests pin the gate
    so a future cfg refactor can't silently regress it.
    """

    def test_detail_404s_when_tenant_check_rejects(self):
        """A non-owning interactive caller reading another user's ws_id
        through the detail endpoint must 404 before any data flows."""
        ws_id = "ws-other-user"
        loaded_ws = MagicMock()
        loaded_ws.id = ws_id
        loaded_ws.name = "owned-by-stranger"
        loaded_ws.state = MagicMock()
        loaded_ws.state.value = "idle"
        loaded_ws.user_id = "owner"
        loaded_ws.kind = "interactive"
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = loaded_ws

        # Tenant check returns a 404 just like ``_require_ws_access``
        # does on owner-mismatch.  We can't import the production
        # helper here (it pulls the whole server module into the test
        # graph) so we ape its return shape.
        def deny(_request: Any, _ws_id: str, _mgr: Any) -> JSONResponse:
            return JSONResponse({"error": "Workstream not found"}, status_code=404)

        client = _build_detail_app(mock_mgr, tenant_check=deny)

        r = client.get(f"/v1/api/workstreams/{ws_id}")
        assert r.status_code == 404
        body = r.json()
        # Sensitive fields the PR added must not surface for a
        # non-owning caller.
        assert "name" not in body
        assert "pending_approval_details" not in body
        assert "user_id" not in body
        # And mgr.get was NEVER consulted — the gate fires first.
        mock_mgr.get.assert_not_called()
        mock_mgr.open.assert_not_called()

    def test_detail_succeeds_when_tenant_check_allows(self):
        """A passing tenant_check (returns ``None``) lets the handler
        proceed normally — the ``pending_approval`` defaults still
        appear in the response."""
        ws_id = "ws-mine"
        loaded_ws = MagicMock()
        loaded_ws.id = ws_id
        loaded_ws.name = "owned"
        loaded_ws.state = MagicMock()
        loaded_ws.state.value = "idle"
        loaded_ws.user_id = "test-user"
        loaded_ws.kind = "interactive"
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = loaded_ws

        def allow(_request: Any, _ws_id: str, _mgr: Any) -> None:
            return None

        client = _build_detail_app(mock_mgr, tenant_check=allow)

        r = client.get(f"/v1/api/workstreams/{ws_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["ws_id"] == ws_id
        assert body["pending_approval"] is False
        assert body["pending_approval_details"] == []

    def test_history_404s_when_tenant_check_rejects(self, _inject_storage):
        """A non-owning interactive caller reading another user's ws_id
        through the history endpoint must 404 before any storage
        access — owner messages are sensitive content."""
        ws_id = "ws-other-user-hist"
        _inject_storage.register_workstream(ws_id, kind="interactive", user_id="owner")
        _inject_storage.save_message(ws_id, "user", "private message")
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = None

        def deny(_request: Any, _ws_id: str, _mgr: Any) -> JSONResponse:
            return JSONResponse({"error": "Workstream not found"}, status_code=404)

        client = _build_history_app(mock_mgr, _inject_storage, tenant_check=deny)

        r = client.get(f"/v1/api/workstreams/{ws_id}/history")
        assert r.status_code == 404
        # Owner's content must not have leaked into the response.
        assert "private message" not in r.text

    def test_history_succeeds_when_tenant_check_allows(self, _inject_storage):
        ws_id = "ws-mine-hist"
        _inject_storage.register_workstream(ws_id, kind="interactive", user_id="test-user")
        _inject_storage.save_message(ws_id, "user", "hello")
        mock_ws = MagicMock()
        mock_ws.id = ws_id
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = mock_ws

        def allow(_request: Any, _ws_id: str, _mgr: Any) -> None:
            return None

        client = _build_history_app(mock_mgr, _inject_storage, tenant_check=allow)

        r = client.get(f"/v1/api/workstreams/{ws_id}/history")
        assert r.status_code == 200
        assert any(m.get("content") == "hello" for m in r.json()["messages"])

    def test_history_cold_cache_falls_through_to_storage_via_thread(self, _inject_storage):
        """Regression: ``cfg.tenant_check`` is invoked through
        ``await asyncio.to_thread(...)`` so the synchronous
        ``resolve_workstream_owner`` storage fallback no longer
        blocks the event loop on a cold cache.

        Wires the real :func:`resolve_workstream_owner` as the
        tenant_check (instead of the fake ``allow``/``deny`` of the
        sibling tests above), forces ``mgr.get`` to miss, asserts
        the handler still resolves through the storage row, and
        spies on ``asyncio.to_thread`` to pin the offload — reverting
        the wrap to a sync ``cfg.tenant_check(...)`` call would leave
        the storage fallback working but trip the spy assertion.
        """
        import asyncio

        from turnstone.core.web_helpers import resolve_workstream_owner

        ws_id = "ws-cold-cache-hist"
        _inject_storage.register_workstream(ws_id, kind="interactive", user_id="test-user")
        _inject_storage.save_message(ws_id, "user", "from cold storage")
        mock_mgr = MagicMock()
        # Cold cache: nothing in memory, owner row only in storage.
        mock_mgr.get.return_value = None

        def cold_check(request: Any, ws_id: str, mgr: Any) -> JSONResponse | None:
            _owner, err = resolve_workstream_owner(
                request, ws_id, mgr=mgr, not_found_label="Workstream not found"
            )
            return err

        offloaded: list[Any] = []
        real_to_thread = asyncio.to_thread

        async def spy_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
            offloaded.append(func)
            return await real_to_thread(func, *args, **kwargs)

        client = _build_history_app(mock_mgr, _inject_storage, tenant_check=cold_check)
        with patch("asyncio.to_thread", spy_to_thread):
            r = client.get(f"/v1/api/workstreams/{ws_id}/history")

        assert r.status_code == 200
        assert any(m.get("content") == "from cold storage" for m in r.json()["messages"])
        # Pin the offload — reverting ``await asyncio.to_thread(cfg.tenant_check, ...)``
        # to ``cfg.tenant_check(...)`` leaves the response shape intact
        # but drops ``cold_check`` from the spy's call list.
        assert cold_check in offloaded, (
            f"tenant_check must be invoked through asyncio.to_thread; got {offloaded}"
        )

    def test_detail_cold_cache_falls_through_to_storage_via_thread(self, _inject_storage):
        """Detail counterpart to the cold-cache history test.

        Forces ``mgr.get`` to miss and pins the lazy-rehydrate to a
        mocked ``mgr.open`` so the test covers the path where the
        wrapped ``tenant_check`` resolves through storage *before* the
        handler reaches its rehydrate ladder.  Same ``asyncio.to_thread``
        spy as the history test pins the offload itself.
        """
        import asyncio

        from turnstone.core.web_helpers import resolve_workstream_owner

        ws_id = "ws-cold-cache-detail"
        _inject_storage.register_workstream(ws_id, kind="interactive", user_id="test-user")

        rehydrated = MagicMock()
        rehydrated.id = ws_id
        rehydrated.name = "rehydrated-ws"
        rehydrated.state = MagicMock()
        rehydrated.state.value = "idle"
        rehydrated.user_id = "test-user"
        rehydrated.kind = "interactive"
        rehydrated.ui = None  # bypass pending-approval serializer
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = None
        mock_mgr.open.return_value = rehydrated

        def cold_check(request: Any, ws_id: str, mgr: Any) -> JSONResponse | None:
            _owner, err = resolve_workstream_owner(
                request, ws_id, mgr=mgr, not_found_label="Workstream not found"
            )
            return err

        offloaded: list[Any] = []
        real_to_thread = asyncio.to_thread

        async def spy_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
            offloaded.append(func)
            return await real_to_thread(func, *args, **kwargs)

        client = _build_detail_app(mock_mgr, tenant_check=cold_check)
        with patch("asyncio.to_thread", spy_to_thread):
            r = client.get(f"/v1/api/workstreams/{ws_id}")

        assert r.status_code == 200
        body = r.json()
        assert body["ws_id"] == ws_id
        assert body["name"] == "rehydrated-ws"
        # Lazy rehydrate path engaged — the handler called mgr.open after
        # the cold-cache tenant_check resolved through storage.
        mock_mgr.open.assert_called_once_with(ws_id)
        # Pin the offload — see the history test for the rationale.
        assert cold_check in offloaded, (
            f"tenant_check must be invoked through asyncio.to_thread; got {offloaded}"
        )


class TestHistoryReasoningRehydration:
    """The lifted ``GET /v1/api/workstreams/{ws_id}/history`` surfaces
    stored Anthropic thinking blocks on assistant messages so a page
    refresh re-renders the reasoning bubble. Drives through the real
    ``AnthropicProvider.extract_reasoning_text`` and the storage
    ``reconstruct_messages`` boundary that JSON-decodes
    ``provider_data`` into ``_provider_content``.
    """

    def test_history_handler_surfaces_reasoning_for_anthropic_thinking(self, _inject_storage):
        ws_id = "ws-reason-1"
        _inject_storage.register_workstream(ws_id, kind="interactive", user_id="test-user")
        provider_data = json.dumps(
            [
                {"type": "thinking", "thinking": "let me reason", "signature": "s"},
                {"type": "text", "text": "Final answer."},
            ]
        )
        _inject_storage.save_message(
            ws_id, "assistant", "Final answer.", provider_data=provider_data
        )
        # No live session — exercises the storage-only path which
        # falls back to default surface_persisted_reasoning=True.
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = None
        client = _build_history_app(mock_mgr, _inject_storage)

        r = client.get(f"/v1/api/workstreams/{ws_id}/history")
        assert r.status_code == 200
        msgs = r.json()["messages"]
        assistant = next(m for m in msgs if m.get("role") == "assistant")
        assert assistant["reasoning"] == "let me reason"

    def test_history_handler_strips_provider_content(self, _inject_storage):
        ws_id = "ws-reason-2"
        _inject_storage.register_workstream(ws_id, kind="interactive", user_id="test-user")
        provider_data = json.dumps([{"type": "thinking", "thinking": "x", "signature": "s"}])
        _inject_storage.save_message(ws_id, "assistant", "Answer.", provider_data=provider_data)
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = None
        client = _build_history_app(mock_mgr, _inject_storage)

        r = client.get(f"/v1/api/workstreams/{ws_id}/history")
        assert r.status_code == 200
        for m in r.json()["messages"]:
            assert "_provider_content" not in m

    def test_history_handler_with_persist_flag_false_via_live_session(self, _inject_storage):
        """Operator-flipped ``surface_persisted_reasoning=False`` on the active
        model suppresses the reasoning field even when the data is
        stored. ``_provider_content`` is still stripped from the wire.
        """
        ws_id = "ws-reason-3"
        _inject_storage.register_workstream(ws_id, kind="interactive", user_id="test-user")
        provider_data = json.dumps([{"type": "thinking", "thinking": "hidden", "signature": "s"}])
        _inject_storage.save_message(ws_id, "assistant", "Answer.", provider_data=provider_data)
        live_session = SimpleNamespace(
            id=ws_id,
            _registry=SimpleNamespace(
                get_config=lambda alias: SimpleNamespace(surface_persisted_reasoning=False)
            ),
            _model_alias="claude-opus-4-7",
        )
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = live_session
        client = _build_history_app(mock_mgr, _inject_storage)

        r = client.get(f"/v1/api/workstreams/{ws_id}/history")
        assert r.status_code == 200
        for m in r.json()["messages"]:
            if m.get("role") == "assistant":
                assert "reasoning" not in m
            assert "_provider_content" not in m

    def test_history_handler_cold_workstream_resolves_via_workstream_config(self, _inject_storage):
        """Cold workstream (no live session) — the handler walks
        ``workstream_config.model_alias`` (persisted at first send by
        the SessionManager rehydrate path) and looks up the active
        model's ``surface_persisted_reasoning`` flag through the global registry
        on ``app.state``. Operator flag-flip is honored uniformly
        across live and cold workstreams.
        """
        ws_id = "ws-reason-cold"
        _inject_storage.register_workstream(ws_id, kind="interactive", user_id="test-user")
        # Simulate the model alias persisted by the rehydrate path
        # (session_manager.py:628-629 reads it back via the same key).
        _inject_storage.save_workstream_config(ws_id, {"model_alias": "claude-opus-4-7"})
        provider_data = json.dumps(
            [{"type": "thinking", "thinking": "should not surface", "signature": "s"}]
        )
        _inject_storage.save_message(ws_id, "assistant", "Answer.", provider_data=provider_data)
        # No live session — handler falls back to workstream_config + registry.
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = None

        # Build the app with a global registry that reports persist=False
        # for the saved alias.
        cfg = _interactive_endpoint_cfg(mock_mgr)
        handler = make_history_handler(cfg)
        app = Starlette(
            routes=[
                Mount(
                    "/v1",
                    routes=[
                        Route(
                            "/api/workstreams/{ws_id}/history",
                            handler,
                            methods=["GET"],
                        ),
                    ],
                ),
            ],
            middleware=[Middleware(_InjectAuthMiddleware)],
        )
        app.state.workstreams = mock_mgr
        app.state.auth_storage = _inject_storage
        app.state.registry = SimpleNamespace(
            get_config=lambda alias: SimpleNamespace(
                surface_persisted_reasoning=(alias != "claude-opus-4-7"),
            )
        )
        client = TestClient(app)

        r = client.get(f"/v1/api/workstreams/{ws_id}/history")
        assert r.status_code == 200
        # Flag-flip on the saved alias is honored: reasoning suppressed.
        for m in r.json()["messages"]:
            if m.get("role") == "assistant":
                assert "reasoning" not in m
            assert "_provider_content" not in m

    def test_history_handler_cold_workstream_no_alias_defaults_true(self, _inject_storage):
        """A workstream that pre-dates the rehydrate-time alias persist
        (or one that simply has no workstream_config row) falls through
        to the conservative default ``True``.  Reasoning surfaces.
        """
        ws_id = "ws-reason-cold-no-alias"
        _inject_storage.register_workstream(ws_id, kind="interactive", user_id="test-user")
        provider_data = json.dumps(
            [{"type": "thinking", "thinking": "default-true wins", "signature": "s"}]
        )
        _inject_storage.save_message(ws_id, "assistant", "Answer.", provider_data=provider_data)
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = None
        client = _build_history_app(mock_mgr, _inject_storage)

        r = client.get(f"/v1/api/workstreams/{ws_id}/history")
        assert r.status_code == 200
        assistant = next(m for m in r.json()["messages"] if m.get("role") == "assistant")
        assert assistant["reasoning"] == "default-true wins"
