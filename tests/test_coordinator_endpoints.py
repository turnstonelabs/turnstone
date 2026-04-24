"""Tests for the coordinator HTTP endpoints.

Builds a minimal Starlette app wiring only the coordinator routes and
an auth-injector middleware.  Verifies the permission gate, 503
remediation when coord_mgr / model alias is missing, ownership
enforcement, and lazy rehydration on GET /{ws_id}.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Auth injection middleware
# ---------------------------------------------------------------------------
from tests._coord_test_helpers import (
    _AuthMiddleware,
    _build_mgr,
    _fake_registry,
    _FakeConfigStore,
)
from turnstone.console.coordinator_ui import ConsoleCoordinatorUI
from turnstone.console.server import (
    cluster_ws_detail,
    coordinator_approve,
    coordinator_cancel,
    coordinator_children,
    coordinator_close,
    coordinator_create,
    coordinator_detail,
    coordinator_history,
    coordinator_list,
    coordinator_open,
    coordinator_saved,
    coordinator_send,
    coordinator_tasks,
)
from turnstone.core.auth import AuthResult
from turnstone.core.storage._sqlite import SQLiteBackend

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path):
    return SQLiteBackend(str(tmp_path / "coord.db"))


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
            # Literal path before the /{ws_id} routes below so Starlette
            # matches "saved" as the literal, not as a ws_id.
            Route(
                "/v1/api/coordinator/saved",
                coordinator_saved,
                methods=["GET"],
            ),
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
                "/v1/api/coordinator/{ws_id}/open",
                coordinator_open,
                methods=["POST"],
            ),
            Route(
                "/v1/api/coordinator/{ws_id}/children",
                coordinator_children,
                methods=["GET"],
            ),
            Route(
                "/v1/api/coordinator/{ws_id}/tasks",
                coordinator_tasks,
                methods=["GET"],
            ),
            Route(
                "/v1/api/coordinator/{ws_id}",
                coordinator_detail,
                methods=["GET"],
            ),
            Route(
                "/v1/api/cluster/ws/{ws_id}/detail",
                cluster_ws_detail,
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


def test_missing_model_alias_falls_back_to_registry_default(storage):
    """``coordinator.model_alias`` unset → resolve through the
    registry's default alias rather than 503-ing.  Operators get a
    working coordinator out of the box once any model is configured."""
    mgr = _build_mgr(storage)
    client = _make_client(storage, coord_mgr=mgr, alias="", registry=_fake_registry())
    resp = client.post(
        "/v1/api/coordinator/new",
        json={},
        headers={"X-Test-User": "user-1", "X-Test-Perms": "admin.coordinator"},
    )
    # The fake registry resolves any alias (including None → default)
    # so the create call succeeds past the gate.  We only assert that
    # the 503 remediation stack is NOT fired — any success or further
    # downstream failure is unrelated to this regression.
    assert resp.status_code != 503, resp.json()


def test_missing_alias_and_no_default_returns_503(storage):
    """When neither ``coordinator.model_alias`` nor the registry
    default resolves, 503 with remediation still fires so operators
    know they haven't configured any model at all."""
    mgr = _build_mgr(storage)
    broken_registry = MagicMock()
    broken_registry.resolve.side_effect = KeyError("no-default")
    client = _make_client(storage, coord_mgr=mgr, alias="", registry=broken_registry)
    resp = client.post(
        "/v1/api/coordinator/new",
        json={},
        headers={"X-Test-User": "user-1", "X-Test-Perms": "admin.coordinator"},
    )
    assert resp.status_code == 503
    assert "does not resolve" in resp.json()["error"]


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


def _seed_closed_coord_with_history(
    mgr,
    storage,
    *,
    user_id: str,
    name: str,
) -> str:
    """Create + close a coordinator and seed one conversation row.

    list_workstreams_with_history's WHERE EXISTS guard skips coords with no
    messages, so the saved-list endpoint won't surface a freshly-closed
    coordinator unless we've stamped at least one conversation row.
    """
    ws = mgr.create(user_id=user_id, name=name)
    storage.save_message(ws.id, role="user", content="seed")
    closed = mgr.close(ws.id)
    assert closed
    return ws.id


@pytest.fixture
def saved_storage(tmp_path):
    """Storage fixture for saved-coordinator tests.

    coordinator_saved goes through ``list_workstreams_with_history``
    which calls ``get_storage()`` (the singleton registry), not whatever
    backend the manager holds.  This fixture initialises the registry to
    a fresh SQLite db and yields the same backend so the test can also
    seed conversation rows directly.
    """
    from turnstone.core.storage import init_storage, reset_storage

    db_path = str(tmp_path / "saved.db")
    reset_storage()
    backend = init_storage("sqlite", path=db_path, run_migrations=False)
    try:
        yield backend
    finally:
        reset_storage()


def test_saved_filters_by_caller(saved_storage):
    storage = saved_storage
    mgr = _build_mgr(storage)
    mine_id = _seed_closed_coord_with_history(mgr, storage, user_id="user-1", name="mine")
    _seed_closed_coord_with_history(mgr, storage, user_id="user-2", name="theirs")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get("/v1/api/coordinator/saved", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert {c["ws_id"] for c in body["coordinators"]} == {mine_id}


def test_saved_admin_sees_all(saved_storage):
    storage = saved_storage
    mgr = _build_mgr(storage)
    a = _seed_closed_coord_with_history(mgr, storage, user_id="user-1", name="a")
    b = _seed_closed_coord_with_history(mgr, storage, user_id="user-2", name="b")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(
        "/v1/api/coordinator/saved",
        headers={
            "X-Test-User": "admin-1",
            "X-Test-Perms": "admin.coordinator,admin.users",
        },
    )
    assert resp.status_code == 200
    assert {c["ws_id"] for c in resp.json()["coordinators"]} == {a, b}


def test_saved_blank_uid_returns_empty(saved_storage):
    """Non-admin caller with no sub gets fail-closed empty list.

    Mirrors list_saved_workstreams — empty user_id must not fall through
    to a cluster-wide query (would leak orphan / migration rows).
    """
    storage = saved_storage
    mgr = _build_mgr(storage)
    _seed_closed_coord_with_history(mgr, storage, user_id="someone", name="x")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(
        "/v1/api/coordinator/saved",
        headers={"X-Test-User": "", "X-Test-Perms": "admin.coordinator"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"coordinators": []}


def test_saved_excludes_currently_loaded(saved_storage):
    """A coordinator currently in coord_mgr must NOT appear in saved cards.

    Even if its DB row says state='closed' (e.g. mid-restart race), the
    in-memory presence wins so the same ws_id can't be in both the
    active list and the saved-cards grid simultaneously.
    """
    storage = saved_storage
    mgr = _build_mgr(storage)
    closed_id = _seed_closed_coord_with_history(mgr, storage, user_id="user-1", name="closed")
    # Create another coord, leave it loaded — should never appear in saved.
    loaded_ws = mgr.create(user_id="user-1", name="loaded")
    storage.save_message(loaded_ws.id, role="user", content="seed")
    # Force it to state='closed' on disk without removing from memory, to
    # exercise the defence-in-depth ``loaded`` filter.
    storage.update_workstream_state(loaded_ws.id, "closed")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get("/v1/api/coordinator/saved", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    saved_ids = {c["ws_id"] for c in resp.json()["coordinators"]}
    assert closed_id in saved_ids
    assert loaded_ws.id not in saved_ids


def test_saved_excludes_active_state_rows(saved_storage):
    """Only state='closed' rows surface in the saved list.

    A coordinator that's idle on disk but not currently loaded into
    coord_mgr (e.g. orphaned across a console restart that hasn't
    rehydrated yet) is NOT 'saved' — it's just not loaded yet, and the
    saved grid is for explicit user-closed sessions.
    """
    storage = saved_storage
    mgr = _build_mgr(storage)
    closed_id = _seed_closed_coord_with_history(mgr, storage, user_id="user-1", name="closed")
    # An idle row in storage with no in-memory presence — must not appear.
    orphan = mgr.create(user_id="user-1", name="orphan")
    storage.save_message(orphan.id, role="user", content="seed")
    # Drop from memory without changing state (simulates manager restart).
    mgr._workstreams.pop(orphan.id, None)
    if orphan.id in mgr._order:
        mgr._order.remove(orphan.id)
    assert storage.get_workstream(orphan.id)["state"] == "idle"
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get("/v1/api/coordinator/saved", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    saved_ids = {c["ws_id"] for c in resp.json()["coordinators"]}
    assert saved_ids == {closed_id}


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


# ---------------------------------------------------------------------------
# Open (explicit rehydration)
# ---------------------------------------------------------------------------


def test_open_returns_already_loaded_when_in_memory(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1", name="live")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(f"/v1/api/coordinator/{ws.id}/open", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ws_id"] == ws.id
    assert body.get("already_loaded") is True


def test_open_returns_404_on_ownership_mismatch_in_memory(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="owner", name="theirs")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/coordinator/{ws.id}/open",
        headers={"X-Test-User": "stranger", "X-Test-Perms": "admin.coordinator"},
    )
    assert resp.status_code == 404  # not 403 — don't leak existence


def test_open_rehydrates_when_not_in_memory(storage, monkeypatch):
    mgr = _build_mgr(storage)
    rehydrated = MagicMock()
    rehydrated.id = "coord-rehy"
    rehydrated.name = "rehydrated"
    rehydrated.user_id = "user-1"
    monkeypatch.setattr(mgr, "open", MagicMock(return_value=rehydrated))
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post("/v1/api/coordinator/coord-rehy/open", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ws_id"] == "coord-rehy"
    assert body["name"] == "rehydrated"
    assert "already_loaded" not in body
    mgr.open.assert_called_once_with("coord-rehy", "user-1")


def test_open_admin_uses_open_admin(storage, monkeypatch):
    mgr = _build_mgr(storage)
    rehydrated = MagicMock()
    rehydrated.id = "coord-rehy"
    rehydrated.name = "r"
    rehydrated.user_id = "someone-else"
    monkeypatch.setattr(mgr, "open_admin", MagicMock(return_value=rehydrated))
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        "/v1/api/coordinator/coord-rehy/open",
        headers={
            "X-Test-User": "admin-1",
            "X-Test-Perms": "admin.coordinator,admin.users",
        },
    )
    assert resp.status_code == 200
    mgr.open_admin.assert_called_once_with("coord-rehy")


def test_open_returns_404_when_unknown_ws_id(storage, monkeypatch):
    mgr = _build_mgr(storage)
    monkeypatch.setattr(mgr, "open", MagicMock(return_value=None))
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post("/v1/api/coordinator/nonexistent/open", headers=_COORD_HEADERS)
    assert resp.status_code == 404


def test_open_503_on_coord_mgr_unavailable(storage):
    client = _make_client(storage, coord_mgr=None)
    resp = client.post("/v1/api/coordinator/any-ws/open", headers=_COORD_HEADERS)
    assert resp.status_code == 503


def test_open_correlation_id_on_factory_failure(storage, monkeypatch):
    mgr = _build_mgr(storage)
    monkeypatch.setattr(mgr, "open", MagicMock(side_effect=RuntimeError("boom")))
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post("/v1/api/coordinator/bad-ws/open", headers=_COORD_HEADERS)
    assert resp.status_code == 500
    assert "correlation_id=" in resp.json()["error"]


def test_open_503_when_open_raises_value_error(storage, monkeypatch):
    """ValueError from the factory surfaces as 503 with the remediation text."""
    mgr = _build_mgr(storage)
    monkeypatch.setattr(mgr, "open", MagicMock(side_effect=ValueError("coord registry missing")))
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post("/v1/api/coordinator/bad-ws/open", headers=_COORD_HEADERS)
    assert resp.status_code == 503
    assert "registry missing" in resp.json()["error"]


# ---------------------------------------------------------------------------
# GET /v1/api/coordinator/{ws_id}/children — phase 3 tree view backend
# ---------------------------------------------------------------------------


def _seed_child(storage, parent_ws_id: str, ws_id: str, *, state: str = "idle") -> None:
    storage.register_workstream(
        ws_id,
        node_id="node-a",
        user_id="user-1",
        name=f"child-{ws_id[:4]}",
        kind="interactive",
        parent_ws_id=parent_ws_id,
    )
    if state != "idle":
        storage.update_workstream_state(ws_id, state)


def test_children_empty_for_new_coordinator(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(f"/v1/api/coordinator/{ws.id}/children", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"items": [], "truncated": False}


def test_children_returns_interactive_children(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    _seed_child(storage, ws.id, "c" * 32)
    _seed_child(storage, ws.id, "d" * 32, state="running")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(f"/v1/api/coordinator/{ws.id}/children", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    states = {r["state"] for r in body["items"]}
    assert states == {"idle", "running"}
    kinds = {r["kind"] for r in body["items"]}
    assert kinds == {"interactive"}


def test_children_ownership_404(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="owner")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(
        f"/v1/api/coordinator/{ws.id}/children",
        headers={"X-Test-User": "stranger", "X-Test-Perms": "admin.coordinator"},
    )
    assert resp.status_code == 404


def test_children_admin_bypass(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="owner")
    _seed_child(storage, ws.id, "a" * 32)
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(
        f"/v1/api/coordinator/{ws.id}/children",
        headers={
            "X-Test-User": "admin-1",
            "X-Test-Perms": "admin.coordinator,admin.users",
        },
    )
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1


def test_children_invalid_ws_id_400(storage):
    mgr = _build_mgr(storage)
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get("/v1/api/coordinator/INVALID-WS-SHOUT/children", headers=_COORD_HEADERS)
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /v1/api/coordinator/{ws_id}/tasks — phase 3 task pane backend
# ---------------------------------------------------------------------------


def test_tasks_empty_envelope_for_new_coordinator(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(f"/v1/api/coordinator/{ws.id}/tasks", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"version": 1, "tasks": []}


def test_tasks_round_trips_stored_envelope(storage):
    import json

    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    envelope = {
        "version": 1,
        "tasks": [
            {
                "id": "tsk_abc",
                "title": "Spawn analyzer",
                "status": "in_progress",
                "child_ws_id": "",
                "created": "2026-04-17T00:00:00+00:00",
                "updated": "2026-04-17T00:01:00+00:00",
            }
        ],
    }
    storage.save_workstream_config(ws.id, {"tasks": json.dumps(envelope)})
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(f"/v1/api/coordinator/{ws.id}/tasks", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == envelope


def test_tasks_corrupt_envelope_returns_empty(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    storage.save_workstream_config(ws.id, {"tasks": "NOT-JSON"})
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(f"/v1/api/coordinator/{ws.id}/tasks", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"version": 1, "tasks": []}


def test_tasks_ownership_404(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="owner")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(
        f"/v1/api/coordinator/{ws.id}/tasks",
        headers={"X-Test-User": "stranger", "X-Test-Perms": "admin.coordinator"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /v1/api/cluster/ws/{ws_id}/detail — cluster-wide live inspect
# ---------------------------------------------------------------------------


_CLUSTER_HEADERS = {"X-Test-User": "user-1", "X-Test-Perms": "admin.cluster.inspect"}


def test_cluster_inspect_requires_permission(storage):
    client = _make_client(storage, coord_mgr=_build_mgr(storage), registry=_fake_registry())
    resp = client.get(
        "/v1/api/cluster/ws/" + ("a" * 32) + "/detail",
        headers={"X-Test-User": "u", "X-Test-Perms": "read"},
    )
    assert resp.status_code == 403


def test_cluster_inspect_unknown_ws_id_404(storage):
    client = _make_client(storage, coord_mgr=_build_mgr(storage), registry=_fake_registry())
    resp = client.get("/v1/api/cluster/ws/" + ("a" * 32) + "/detail", headers=_CLUSTER_HEADERS)
    assert resp.status_code == 404


def test_cluster_inspect_invalid_ws_id_400(storage):
    client = _make_client(storage, coord_mgr=_build_mgr(storage), registry=_fake_registry())
    resp = client.get("/v1/api/cluster/ws/NOT-HEX/detail", headers=_CLUSTER_HEADERS)
    assert resp.status_code == 400


def test_cluster_inspect_ownership_404(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="owner")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(
        f"/v1/api/cluster/ws/{ws.id}/detail",
        headers={"X-Test-User": "stranger", "X-Test-Perms": "admin.cluster.inspect"},
    )
    assert resp.status_code == 404


def test_cluster_inspect_coordinator_self_path(storage):
    """A coordinator row returns live from the in-process manager."""
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(f"/v1/api/cluster/ws/{ws.id}/detail", headers=_CLUSTER_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["persisted"]["ws_id"] == ws.id
    assert body["persisted"]["kind"] == "coordinator"
    # Live is populated from the manager snapshot (pending_approval key signals
    # we went through the coordinator branch, not the node-fetch branch).
    assert body["live"] is not None
    assert "pending_approval" in body["live"]
    # Freshly created coordinator has no pending approval — the previous
    # implementation read `not _approval_event.is_set()` which fires True
    # on any unset event, making this flag spuriously True on every new
    # coordinator.  Regression guard.
    assert body["live"]["pending_approval"] is False
    assert body["live"]["activity_state"] == ""
    assert isinstance(body["messages"], list)


def test_cluster_inspect_unloaded_coordinator_live_null(storage):
    """A persisted-but-not-loaded coordinator returns live: null, 200."""
    mgr = _build_mgr(storage)
    # Persist a coordinator row directly without loading into the manager.
    storage.register_workstream(
        "f" * 32,
        node_id="console",
        user_id="user-1",
        name="offline-coord",
        kind="coordinator",
        parent_ws_id=None,
    )
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(f"/v1/api/cluster/ws/{'f' * 32}/detail", headers=_CLUSTER_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["live"] is None
    assert body["persisted"]["kind"] == "coordinator"


def _install_proxy_client(client: TestClient, transport: httpx.MockTransport) -> None:
    """Attach an httpx.AsyncClient backed by a MockTransport to the app state.

    cluster_ws_detail's node-backed branch reads
    ``request.app.state.proxy_client`` + ``request.app.state.collector``
    to fetch a node's dashboard.  Both are normally wired in the lifespan;
    tests short-circuit by injecting a proxy client here and stubbing
    the collector's node lookup with a MagicMock.
    """
    client.app.state.proxy_client = httpx.AsyncClient(transport=transport)


def _install_collector_with_node(client: TestClient, node_id: str, server_url: str) -> None:
    """Stub app.state.collector so _get_server_url returns server_url."""
    collector = MagicMock()
    collector.get_node_detail.return_value = {
        "node_id": node_id,
        "server_url": server_url,
    }
    client.app.state.collector = collector


def _seed_node_workstream(storage, *, ws_id: str, node_id: str, user_id: str = "user-1") -> None:
    storage.register_workstream(
        ws_id,
        node_id=node_id,
        user_id=user_id,
        name=f"child-{ws_id[:4]}",
        kind="interactive",
        parent_ws_id=None,
    )


def test_cluster_inspect_node_backed_success(storage):
    """Node returns a matching workstream entry in /dashboard — cluster_ws_detail
    merges its live fields into the `live` block."""
    mgr = _build_mgr(storage)
    ws_id = "ab" * 16
    _seed_node_workstream(storage, ws_id=ws_id, node_id="node-a")
    payload = {
        "workstreams": [
            {
                "id": ws_id,
                "state": "running",
                "tokens": 512,
                "context_ratio": 0.25,
                "activity": "tool: bash",
                "activity_state": "tool",
                "tool_calls": 3,
                "model": "gpt-5",
                "model_alias": "default",
                "title": "hello",
                "name": "child",
            }
        ]
    }

    def _handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/api/dashboard"
        return httpx.Response(200, json=payload)

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    _install_collector_with_node(client, "node-a", "http://node-a")
    _install_proxy_client(client, httpx.MockTransport(_handler))

    resp = client.get(f"/v1/api/cluster/ws/{ws_id}/detail", headers=_CLUSTER_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    live = body["live"]
    assert live is not None
    assert live["state"] == "running"
    assert live["tokens"] == 512
    assert live["tool_calls"] == 3
    # pending_approval synthesized from activity_state != "approval"
    assert live["pending_approval"] is False


def test_cluster_inspect_node_backed_pending_approval_synthesized(storage):
    """activity_state=='approval' from the node synthesizes pending_approval=True."""
    mgr = _build_mgr(storage)
    ws_id = "cd" * 16
    _seed_node_workstream(storage, ws_id=ws_id, node_id="node-a")
    payload = {
        "workstreams": [
            {
                "id": ws_id,
                "state": "attention",
                "activity_state": "approval",
                "activity": "awaiting approval",
                "tokens": 100,
            }
        ]
    }
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    _install_collector_with_node(client, "node-a", "http://node-a")
    _install_proxy_client(client, httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    resp = client.get(f"/v1/api/cluster/ws/{ws_id}/detail", headers=_CLUSTER_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["live"]["pending_approval"] is True


def test_cluster_inspect_node_unreachable_live_null(storage):
    """httpx connect/timeout error → live: null, status 200."""
    mgr = _build_mgr(storage)
    ws_id = "de" * 16
    _seed_node_workstream(storage, ws_id=ws_id, node_id="node-a")

    def _handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("node down")

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    _install_collector_with_node(client, "node-a", "http://node-a")
    _install_proxy_client(client, httpx.MockTransport(_handler))
    resp = client.get(f"/v1/api/cluster/ws/{ws_id}/detail", headers=_CLUSTER_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["live"] is None


def test_cluster_inspect_node_5xx_live_null(storage):
    """Non-2xx from the node → live: null, status 200."""
    mgr = _build_mgr(storage)
    ws_id = "ef" * 16
    _seed_node_workstream(storage, ws_id=ws_id, node_id="node-a")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    _install_collector_with_node(client, "node-a", "http://node-a")
    _install_proxy_client(client, httpx.MockTransport(lambda r: httpx.Response(503, text="down")))
    resp = client.get(f"/v1/api/cluster/ws/{ws_id}/detail", headers=_CLUSTER_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["live"] is None


def test_cluster_inspect_node_missing_entry_live_null(storage):
    """Node returned 200 but the target ws_id is not in its workstream list."""
    mgr = _build_mgr(storage)
    ws_id = "1a" * 16
    _seed_node_workstream(storage, ws_id=ws_id, node_id="node-a")
    payload = {
        "workstreams": [
            {"id": "different-" + "x" * 24, "state": "idle"},
        ]
    }
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    _install_collector_with_node(client, "node-a", "http://node-a")
    _install_proxy_client(client, httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    resp = client.get(f"/v1/api/cluster/ws/{ws_id}/detail", headers=_CLUSTER_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["live"] is None


def test_cluster_inspect_message_limit_clamped(storage):
    """Seed enough messages that the 200-row clamp must actually
    execute, and assert the tail slice is correct — prior version
    only checked `<= 200` on a fresh coordinator (0 messages), which
    passed even if the clamp were stripped."""
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    # 250 messages — last 200 must come back, in chronological order.
    for i in range(250):
        storage.save_message(ws.id, role="user", content=f"msg-{i:04d}")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(
        f"/v1/api/cluster/ws/{ws.id}/detail?message_limit=9999",
        headers=_CLUSTER_HEADERS,
    )
    assert resp.status_code == 200
    messages = resp.json()["messages"]
    # Clamp took effect: exactly 200 rows back.
    assert len(messages) == 200
    # Chronological order preserved: oldest of the tail-200 first,
    # newest last.  The tail of 250 inserts is messages 50..249.
    contents = [m.get("content") for m in messages]
    assert contents[0] == "msg-0050"
    assert contents[-1] == "msg-0249"


def test_cluster_inspect_zero_message_limit_returns_empty(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    for i in range(10):
        storage.save_message(ws.id, role="user", content=f"msg-{i}")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(
        f"/v1/api/cluster/ws/{ws.id}/detail?message_limit=0",
        headers=_CLUSTER_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["messages"] == []


# ---------------------------------------------------------------------------
# _coordinator_rows tenant filter — regression for the cross-tenant leak
# ultrareview flagged in the cluster dashboard path
# ---------------------------------------------------------------------------


def test_coordinator_rows_filters_by_caller_identity(storage):
    """Non-admin callers get only their own coordinators.  `list_all` must
    never be reached for them — mirrors list_for_user's docstring
    invariant, which the phase-3 dashboard merge originally bypassed."""
    from unittest.mock import MagicMock

    from turnstone.console.server import _coordinator_rows

    mgr = _build_mgr(storage)
    mgr.create(user_id="alice", name="alice-coord")
    mgr.create(user_id="bob", name="bob-coord")

    def _request_for(user_id: str, perms: frozenset[str]) -> MagicMock:
        request = MagicMock()
        request.app.state.coord_mgr = mgr
        request.state.auth_result = AuthResult(
            user_id=user_id,
            scopes=frozenset({"read"}),
            token_source="test",
            permissions=perms,
        )
        return request

    alice_rows = _coordinator_rows(
        _request_for("alice", frozenset({"read"})),
    )
    names = {r["name"] for r in alice_rows}
    assert names == {"alice-coord"}, "non-admin alice must NOT see bob's coordinator"

    bob_rows = _coordinator_rows(_request_for("bob", frozenset({"read"})))
    assert {r["name"] for r in bob_rows} == {"bob-coord"}

    admin_rows = _coordinator_rows(
        _request_for("admin-1", frozenset({"read", "admin.users"})),
    )
    assert {r["name"] for r in admin_rows} == {"alice-coord", "bob-coord"}


def test_coordinator_rows_empty_user_id_returns_empty(storage):
    """Defense-in-depth: a request with no user_id (shouldn't reach this
    path through the auth middleware, but defensive) gets zero rows,
    not a list_all leak."""
    from unittest.mock import MagicMock

    from turnstone.console.server import _coordinator_rows

    mgr = _build_mgr(storage)
    mgr.create(user_id="alice", name="alice-coord")

    request = MagicMock()
    request.app.state.coord_mgr = mgr
    request.state.auth_result = AuthResult(
        user_id="",
        scopes=frozenset({"read"}),
        token_source="test",
        permissions=frozenset({"read"}),
    )
    assert _coordinator_rows(request) == []


def _persisted_rows_request(storage, mgr, user_id: str, perms: frozenset[str]):
    """Build a _coordinator_rows-shaped request with auth_storage wired
    up so the persisted-rows merge path fires."""
    from unittest.mock import MagicMock

    request = MagicMock()
    request.app.state.coord_mgr = mgr
    request.app.state.auth_storage = storage
    request.state.auth_result = AuthResult(
        user_id=user_id,
        scopes=frozenset({"read"}),
        token_source="test",
        permissions=perms,
    )
    return request


def test_coordinator_rows_surfaces_closed_coordinators_from_storage(storage):
    """Closed coordinators get popped from ``self._workstreams`` but
    their persisted row stays in storage with ``state='closed'``.  The
    landing page polls _coordinator_rows via
    /v1/api/cluster/workstreams?node=console — the persisted-rows
    merge path surfaces closed rows so the operator can still see
    them alongside active ones."""
    from turnstone.console.server import _coordinator_rows
    from turnstone.core.workstream import WorkstreamKind

    mgr = _build_mgr(storage)
    # Seed a persisted-but-not-loaded closed coordinator directly —
    # register + soft-close via storage primitives.
    storage.register_workstream(
        "a" * 32,
        node_id="console",
        user_id="alice",
        name="historical-coord",
        state="closed",
        kind=WorkstreamKind.COORDINATOR,
        parent_ws_id=None,
    )
    # Also seed a live coordinator via the manager to prove merge.
    mgr.create(user_id="alice", name="live-coord")

    request = _persisted_rows_request(storage, mgr, "alice", frozenset({"read"}))
    rows = _coordinator_rows(request)
    names = {r["name"] for r in rows}
    assert names == {"live-coord", "historical-coord"}
    # Closed coord carries its persisted state so the UI can render
    # it with the correct state glyph.
    closed = next(r for r in rows if r["name"] == "historical-coord")
    assert closed["state"] == "closed"
    assert closed["kind"] == "coordinator"


def test_coordinator_rows_dedupes_by_ws_id_in_memory_wins(storage):
    """When a coordinator is both in-memory (manager) AND in storage,
    _coordinator_rows must prefer the in-memory row so live session
    state (model / model_alias / current state) stays authoritative.
    The storage row has stale fields after every restart / refresh,
    so merging it twice is strictly worse."""
    from turnstone.console.server import _coordinator_rows

    mgr = _build_mgr(storage)
    live = mgr.create(user_id="alice", name="alice-live")
    # Persist an explicit storage-only shape for the SAME ws_id —
    # mgr.create already did this, but we deliberately corrupt the
    # stored row to prove the in-memory row wins.  Update the state
    # to something the manager would never produce so the dedup check
    # is unambiguous.
    storage.update_workstream_state(live.id, "error")

    request = _persisted_rows_request(storage, mgr, "alice", frozenset({"read"}))
    rows = _coordinator_rows(request)
    assert len(rows) == 1
    assert rows[0]["id"] == live.id
    # In-memory WorkstreamState wins over the persisted "error" tweak
    # — the manager reports "idle" for a freshly-created coordinator.
    assert rows[0]["state"] == "idle"


def test_coordinator_rows_persisted_respects_tenant_filter(storage):
    """Non-admin callers must not see OTHER tenants' persisted
    (closed) coordinators either.  Regression lock — the user_id
    kwarg on list_workstreams is pushed through; the defense-in-depth
    client-side empty-string check catches the tail."""
    from turnstone.console.server import _coordinator_rows
    from turnstone.core.workstream import WorkstreamKind

    mgr = _build_mgr(storage)
    storage.register_workstream(
        "a" * 32,
        node_id="console",
        user_id="alice",
        name="alice-closed",
        state="closed",
        kind=WorkstreamKind.COORDINATOR,
        parent_ws_id=None,
    )
    storage.register_workstream(
        "b" * 32,
        node_id="console",
        user_id="bob",
        name="bob-closed",
        state="closed",
        kind=WorkstreamKind.COORDINATOR,
        parent_ws_id=None,
    )

    alice_req = _persisted_rows_request(storage, mgr, "alice", frozenset({"read"}))
    alice_rows = _coordinator_rows(alice_req)
    assert {r["name"] for r in alice_rows} == {"alice-closed"}

    bob_req = _persisted_rows_request(storage, mgr, "bob", frozenset({"read"}))
    bob_rows = _coordinator_rows(bob_req)
    assert {r["name"] for r in bob_rows} == {"bob-closed"}

    admin_req = _persisted_rows_request(storage, mgr, "admin-1", frozenset({"read", "admin.users"}))
    admin_rows = _coordinator_rows(admin_req)
    assert {r["name"] for r in admin_rows} == {"alice-closed", "bob-closed"}


def test_coordinator_rows_persisted_skips_orphan_rows_for_non_admin(storage):
    """Defense-in-depth empty-string tenancy check — a persisted row
    with empty user_id (migration-artifact / system-owned) must NOT
    be visible to a non-admin caller with empty caller_uid either.
    The SQL user_id filter above already enforces this, but duplicate
    the check client-side so orphan rows never leak to any
    hypothetical empty-sub JWT."""
    from unittest.mock import MagicMock

    from turnstone.console.server import _coordinator_rows
    from turnstone.core.workstream import WorkstreamKind

    mgr = _build_mgr(storage)
    storage.register_workstream(
        "c" * 32,
        node_id="console",
        user_id="",  # orphan / system row
        name="orphan-closed",
        state="closed",
        kind=WorkstreamKind.COORDINATOR,
        parent_ws_id=None,
    )

    # Non-admin with empty caller_uid must not see the orphan.
    request = MagicMock()
    request.app.state.coord_mgr = mgr
    request.app.state.auth_storage = storage
    request.state.auth_result = AuthResult(
        user_id="",
        scopes=frozenset({"read"}),
        token_source="test",
        permissions=frozenset({"read"}),
    )
    assert _coordinator_rows(request) == []
