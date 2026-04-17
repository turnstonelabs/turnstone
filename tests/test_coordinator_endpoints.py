"""Tests for the coordinator HTTP endpoints.

Builds a minimal Starlette app wiring only the coordinator routes and
an auth-injector middleware.  Verifies the permission gate, 503
remediation when coord_mgr / model alias is missing, ownership
enforcement, and lazy rehydration on GET /{ws_id}.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Route
from starlette.testclient import TestClient

from turnstone.console.coordinator import CoordinatorManager
from turnstone.console.coordinator_ui import ConsoleCoordinatorUI
from turnstone.console.server import (
    coordinator_approve,
    coordinator_cancel,
    coordinator_close,
    coordinator_create,
    coordinator_detail,
    coordinator_history,
    coordinator_list,
    coordinator_send,
)
from turnstone.core.auth import AuthResult
from turnstone.core.storage._sqlite import SQLiteBackend

# ---------------------------------------------------------------------------
# Auth injection middleware
# ---------------------------------------------------------------------------


class _AuthMiddleware(BaseHTTPMiddleware):
    """Inject a configurable AuthResult from a header-based contract.

    Tests set ``X-Test-Perms`` to a comma-separated permission list, and
    ``X-Test-User`` to the user id.  Empty or missing → no auth.
    """

    async def dispatch(self, request, call_next):
        perms = request.headers.get("X-Test-Perms", "")
        user_id = request.headers.get("X-Test-User", "")
        if perms or user_id:
            request.state.auth_result = AuthResult(
                user_id=user_id,
                scopes=frozenset({"approve"}),
                token_source="test",
                permissions=frozenset(p for p in perms.split(",") if p),
            )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeConfigStore:
    """Minimal ConfigStore stub — returns values from a dict."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)


@pytest.fixture
def storage(tmp_path):
    return SQLiteBackend(str(tmp_path / "coord.db"))


def _build_mgr(storage) -> CoordinatorManager:
    """Build a CoordinatorManager with stub factories."""

    def _sf(ui, model_alias=None, ws_id=None, **kw):
        s = MagicMock()
        s.send.return_value = None
        return s

    return CoordinatorManager(
        session_factory=_sf,
        ui_factory=lambda w, u: ConsoleCoordinatorUI(ws_id=w, user_id=u),
        storage=storage,
        max_active=3,
    )


def _make_client(
    storage,
    *,
    coord_mgr=None,
    alias="my-model",
    registry=None,
) -> TestClient:
    """Build a TestClient exposing just the coordinator routes."""
    app = Starlette(
        routes=[
            Route(
                "/v1/api/coordinator/new",
                coordinator_create,
                methods=["POST"],
            ),
            Route("/v1/api/coordinator", coordinator_list, methods=["GET"]),
            Route(
                "/v1/api/coordinator/{ws_id}/send",
                coordinator_send,
                methods=["POST"],
            ),
            Route(
                "/v1/api/coordinator/{ws_id}/approve",
                coordinator_approve,
                methods=["POST"],
            ),
            Route(
                "/v1/api/coordinator/{ws_id}/cancel",
                coordinator_cancel,
                methods=["POST"],
            ),
            Route(
                "/v1/api/coordinator/{ws_id}/close",
                coordinator_close,
                methods=["POST"],
            ),
            Route(
                "/v1/api/coordinator/{ws_id}/history",
                coordinator_history,
                methods=["GET"],
            ),
            Route(
                "/v1/api/coordinator/{ws_id}",
                coordinator_detail,
                methods=["GET"],
            ),
        ],
        middleware=[Middleware(_AuthMiddleware)],
    )
    app.state.coord_mgr = coord_mgr
    app.state.config_store = _FakeConfigStore({"coordinator.model_alias": alias})
    app.state.coord_registry = registry
    app.state.coord_registry_error = "" if coord_mgr else "registry missing"
    app.state.auth_storage = storage
    app.state.jwt_secret = "x" * 64
    return TestClient(app)


# ---------------------------------------------------------------------------
# Permission gate
# ---------------------------------------------------------------------------


def test_missing_permission_returns_403(storage):
    mgr = _build_mgr(storage)
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        "/v1/api/coordinator/new",
        json={"name": "c1"},
        headers={"X-Test-User": "user-1", "X-Test-Perms": "read"},
    )
    assert resp.status_code == 403


def test_no_auth_returns_401(storage):
    """No AuthResult in request.state → 401 (require_permission semantics)."""
    mgr = _build_mgr(storage)
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post("/v1/api/coordinator/new", json={"name": "c1"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 503 remediation when coordinator subsystem isn't configured
# ---------------------------------------------------------------------------


def test_missing_coord_mgr_returns_503(storage):
    client = _make_client(storage, coord_mgr=None)
    resp = client.post(
        "/v1/api/coordinator/new",
        json={"name": "c1"},
        headers={"X-Test-User": "user-1", "X-Test-Perms": "admin.coordinator"},
    )
    assert resp.status_code == 503
    body = resp.json()
    assert "not initialized" in body["error"]


def test_missing_model_alias_returns_503(storage):
    mgr = _build_mgr(storage)
    client = _make_client(storage, coord_mgr=mgr, alias="", registry=_fake_registry())
    resp = client.post(
        "/v1/api/coordinator/new",
        json={},
        headers={"X-Test-User": "user-1", "X-Test-Perms": "admin.coordinator"},
    )
    assert resp.status_code == 503
    assert "model_alias" in resp.json()["error"]


def test_unresolvable_alias_returns_503(storage):
    mgr = _build_mgr(storage)
    broken_registry = MagicMock()
    broken_registry.resolve.side_effect = KeyError("no-such-alias")
    client = _make_client(storage, coord_mgr=mgr, alias="my-alias", registry=broken_registry)
    resp = client.post(
        "/v1/api/coordinator/new",
        json={},
        headers={"X-Test-User": "user-1", "X-Test-Perms": "admin.coordinator"},
    )
    assert resp.status_code == 503
    assert "does not resolve" in resp.json()["error"]


# ---------------------------------------------------------------------------
# Happy path — create + list + send + close
# ---------------------------------------------------------------------------


def _fake_registry() -> MagicMock:
    """MagicMock that returns success on .resolve() so the 503 gate passes."""
    reg = MagicMock()
    reg.resolve.return_value = (MagicMock(), "gpt-4", MagicMock())
    return reg


_COORD_HEADERS = {"X-Test-User": "user-1", "X-Test-Perms": "admin.coordinator"}


def test_create_returns_ws_id_and_records_audit(storage):
    mgr = _build_mgr(storage)
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        "/v1/api/coordinator/new",
        json={"name": "my-coord"},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["ws_id"]
    assert "my-coord" in body["name"]
    # Audit row recorded on storage.
    from turnstone.core.audit import record_audit  # noqa: F401 (verify import works)

    # Query audit_events via storage.
    events = storage.list_audit_events(user_id="user-1", limit=10)
    actions = [e["action"] for e in events]
    assert "coordinator.create" in actions


def test_list_filters_by_caller(storage):
    mgr = _build_mgr(storage)
    mgr.create(user_id="user-1", name="mine")
    mgr.create(user_id="user-2", name="theirs")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get("/v1/api/coordinator", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    names = {c["name"] for c in body["coordinators"]}
    assert names == {"mine"}


def test_list_admin_sees_all(storage):
    mgr = _build_mgr(storage)
    mgr.create(user_id="user-1", name="mine")
    mgr.create(user_id="user-2", name="theirs")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(
        "/v1/api/coordinator",
        headers={
            "X-Test-User": "admin-1",
            "X-Test-Perms": "admin.coordinator,admin.users",
        },
    )
    assert resp.status_code == 200
    assert len(resp.json()["coordinators"]) == 2


def test_send_to_someone_elses_coord_returns_404(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="owner", name="theirs")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/coordinator/{ws.id}/send",
        json={"message": "hi"},
        headers={"X-Test-User": "stranger", "X-Test-Perms": "admin.coordinator"},
    )
    assert resp.status_code == 404  # not 403 — don't leak existence


def test_send_requires_message(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/coordinator/{ws.id}/send",
        json={},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 400


def test_close_records_audit_and_removes_from_mgr(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(f"/v1/api/coordinator/{ws.id}/close", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    assert mgr.get(ws.id) is None
    events = storage.list_audit_events(user_id="user-1", limit=10)
    actions = [e["action"] for e in events]
    assert "coordinator.close" in actions


def test_approve_resolves_ui_event(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    assert isinstance(ws.ui, ConsoleCoordinatorUI)
    ws.ui._pending_approval = {
        "type": "approve_request",
        "items": [
            {
                "call_id": "c-1",
                "func_name": "spawn_workstream",
                "approval_label": "spawn_workstream",
                "needs_approval": True,
            }
        ],
    }
    ws.ui._approval_event.clear()
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/coordinator/{ws.id}/approve",
        json={"approved": True, "always": True, "call_id": "c-1"},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    assert ws.ui._approval_event.is_set()
    assert ws.ui._approval_result == (True, None)
    assert "spawn_workstream" in ws.ui.auto_approve_tools


# ---------------------------------------------------------------------------
# Lazy rehydration
# ---------------------------------------------------------------------------


def test_detail_triggers_lazy_rehydration(storage):
    """GET /v1/api/coordinator/{ws_id} finds the row and rehydrates it."""
    mgr = _build_mgr(storage)
    # Simulate a coordinator persisted by a previous console process.
    storage.register_workstream(
        "persisted-coord",
        node_id="console",
        user_id="user-1",
        kind="coordinator",
    )
    assert mgr.get("persisted-coord") is None
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get("/v1/api/coordinator/persisted-coord", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    # Now tracked in the manager.
    assert mgr.get("persisted-coord") is not None


def test_detail_404_when_not_owned(storage):
    mgr = _build_mgr(storage)
    storage.register_workstream("coord-x", kind="coordinator", user_id="owner")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(
        "/v1/api/coordinator/coord-x",
        headers={"X-Test-User": "stranger", "X-Test-Perms": "admin.coordinator"},
    )
    assert resp.status_code == 404


def test_detail_404_when_kind_interactive(storage):
    """Non-coordinator rows aren't reachable via the coordinator endpoint."""
    mgr = _build_mgr(storage)
    storage.register_workstream("ws-int", kind="interactive", user_id="user-1")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get("/v1/api/coordinator/ws-int", headers=_COORD_HEADERS)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def test_history_returns_messages(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    # Seed a message in storage.
    storage.save_message(ws.id, "user", "hello")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(f"/v1/api/coordinator/{ws.id}/history", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ws_id"] == ws.id
    assert any(m.get("role") == "user" and m.get("content") == "hello" for m in body["messages"])


def test_history_404_for_stranger(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="owner")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(
        f"/v1/api/coordinator/{ws.id}/history",
        headers={"X-Test-User": "stranger", "X-Test-Perms": "admin.coordinator"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


def test_cancel_resolves_pending_approval(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    assert isinstance(ws.ui, ConsoleCoordinatorUI)
    ws.ui._pending_approval = {"type": "approve_request", "items": []}
    ws.ui._approval_event.clear()
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(f"/v1/api/coordinator/{ws.id}/cancel", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    assert ws.ui._approval_event.is_set()
