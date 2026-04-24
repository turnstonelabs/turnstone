"""Tests for the coordinator governance endpoints and session hooks.

Covers the three console endpoints that let an operator steer a live
coordinator session mid-flight (``/trust``, ``/restrict``,
``/stop_cascade``), the two ``ChatSession`` methods the endpoints
toggle (``set_trust_send`` / ``revoke_tools``), the audit rows the
handlers emit, and the ``_prepare_tool`` revocation gate.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Route
from starlette.testclient import TestClient

from tests._coord_test_helpers import (
    _AuthMiddleware,
    _build_mgr,
    _fake_registry,
    _FakeConfigStore,
    _seed_children,
)
from turnstone.console.server import (
    coordinator_restrict,
    coordinator_stop_cascade,
    coordinator_trust,
)
from turnstone.core.auth import AuthResult
from turnstone.core.storage._sqlite import SQLiteBackend


@pytest.fixture
def storage(tmp_path):
    return SQLiteBackend(str(tmp_path / "coord.db"))


def _make_client(storage, *, coord_mgr, alias="my-model", registry=None) -> TestClient:
    """Starlette app exposing only the three governance endpoints."""
    app = Starlette(
        routes=[
            Route(
                "/v1/api/workstreams/{ws_id}/trust",
                coordinator_trust,
                methods=["POST"],
            ),
            Route(
                "/v1/api/workstreams/{ws_id}/restrict",
                coordinator_restrict,
                methods=["POST"],
            ),
            Route(
                "/v1/api/workstreams/{ws_id}/stop_cascade",
                coordinator_stop_cascade,
                methods=["POST"],
            ),
        ],
        middleware=[Middleware(_AuthMiddleware)],
    )
    app.state.coord_mgr = coord_mgr
    app.state.coord_adapter = coord_mgr._adapter if coord_mgr is not None else None
    app.state.config_store = _FakeConfigStore({"coordinator.model_alias": alias})
    app.state.coord_registry = registry
    app.state.coord_registry_error = "" if coord_mgr else "registry missing"
    app.state.auth_storage = storage
    app.state.jwt_secret = "x" * 64
    return TestClient(app)


def _make_session_mock(*, trust_send: bool = False, revoked: frozenset[str] = frozenset()):
    """Build a MagicMock ``session`` that honours the new ChatSession
    governance surface (``set_trust_send`` / ``get_trust_send`` /
    ``revoke_tools`` / ``get_revoked_tools``) so handler tests exercise
    the real method calls rather than reaching into attributes."""

    state: dict[str, Any] = {"trust_send": trust_send, "revoked": revoked}

    def _set_trust_send(value: bool) -> None:
        state["trust_send"] = bool(value)

    def _get_trust_send() -> bool:
        return bool(state["trust_send"])

    def _revoke_tools(names):
        state["revoked"] = state["revoked"] | frozenset(names)
        return state["revoked"]

    def _get_revoked_tools():
        return state["revoked"]

    session = MagicMock()
    session.set_trust_send.side_effect = _set_trust_send
    session.get_trust_send.side_effect = _get_trust_send
    session.revoke_tools.side_effect = _revoke_tools
    session.get_revoked_tools.side_effect = _get_revoked_tools
    return session, state


_COORD_HEADERS = {"X-Test-User": "user-1", "X-Test-Perms": "admin.coordinator"}
_TRUST_HEADERS = {
    "X-Test-User": "user-1",
    "X-Test-Perms": "admin.coordinator,coordinator.trust.send",
}


# ---------------------------------------------------------------------------
# /trust endpoint — trusted-session mode (item 1)
# ---------------------------------------------------------------------------


def test_trust_toggle_requires_trust_send_permission(storage):
    """Double-gated: admin.coordinator alone is insufficient — the
    trust-send perm is an explicit opt-in."""
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/trust",
        json={"send": True},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 403


def test_trust_toggle_flips_session_flag_and_audits(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    session, state = _make_session_mock()
    coord.session = session
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())

    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/trust",
        json={"send": True},
        headers=_TRUST_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "trust_send": True}
    assert state["trust_send"] is True

    events = [e for e in storage.list_audit_events() if e["action"] == "coordinator.trust.toggled"]
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert detail["send_before"] is False
    assert detail["send_after"] is True


def _service_token_client(
    storage,
    coord_mgr,
    *,
    user_id: str,
    permissions: frozenset[str],
) -> TestClient:
    """Build a TestClient whose middleware injects a service-scoped token.

    Used to verify that the capability-escalating endpoints (``/trust``,
    ``/restrict``, ``/stop_cascade``) do NOT honor the normal
    ``require_permission`` service-scope bypass when the caller lacks
    the specific grant they need.
    """
    app = Starlette(
        routes=[
            Route(
                "/v1/api/workstreams/{ws_id}/trust",
                coordinator_trust,
                methods=["POST"],
            ),
            Route(
                "/v1/api/workstreams/{ws_id}/restrict",
                coordinator_restrict,
                methods=["POST"],
            ),
            Route(
                "/v1/api/workstreams/{ws_id}/stop_cascade",
                coordinator_stop_cascade,
                methods=["POST"],
            ),
        ],
    )
    app.state.coord_mgr = coord_mgr
    app.state.coord_adapter = coord_mgr._adapter if coord_mgr is not None else None
    app.state.config_store = _FakeConfigStore({"coordinator.model_alias": "my-model"})
    app.state.coord_registry = _fake_registry()
    app.state.coord_registry_error = ""
    app.state.auth_storage = storage
    app.state.jwt_secret = "x" * 64

    captured_perms = permissions

    class _ServiceAuth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.auth_result = AuthResult(
                user_id=user_id,
                scopes=frozenset({"read", "write", "approve", "service"}),
                token_source="test",
                permissions=captured_perms,
            )
            return await call_next(request)

    app.user_middleware = [Middleware(_ServiceAuth)]
    app.middleware_stack = app.build_middleware_stack()
    return TestClient(app)


def test_trust_toggle_service_token_cannot_bypass_permission(storage):
    """Service token without coordinator.trust.send is 403'd even when
    its user_id matches the coord owner."""
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="svc-user", name="coord-a")
    coord.session, _ = _make_session_mock()

    client = _service_token_client(
        storage,
        mgr,
        user_id="svc-user",
        permissions=frozenset({"admin.coordinator"}),
    )
    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/trust",
        json={"send": True},
    )
    assert resp.status_code == 403
    assert "coordinator.trust.send" in resp.json()["error"]


def test_trust_toggle_service_token_with_permission_succeeds(storage):
    """Service token WITH the explicit coordinator.trust.send grant IS
    allowed through — locks the intended invariant: bypass is off, but
    an explicit perm still works."""
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="svc-user", name="coord-a")
    session, state = _make_session_mock()
    coord.session = session

    client = _service_token_client(
        storage,
        mgr,
        user_id="svc-user",
        permissions=frozenset({"admin.coordinator", "coordinator.trust.send"}),
    )
    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/trust",
        json={"send": True},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "trust_send": True}
    assert state["trust_send"] is True


def test_restrict_service_token_cannot_bypass_admin_coordinator(storage):
    """/restrict is destructive — a service token WITHOUT explicit
    admin.coordinator grant must be 403'd rather than letting the
    service-scope bypass open the endpoint up."""
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="svc-user", name="coord-a")
    coord.session, _ = _make_session_mock()

    client = _service_token_client(
        storage,
        mgr,
        user_id="svc-user",
        permissions=frozenset(),  # no admin.coordinator
    )
    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/restrict",
        json={"revoke": ["bash"]},
    )
    assert resp.status_code == 403


def test_stop_cascade_service_token_cannot_bypass_admin_coordinator(storage):
    """/stop_cascade mirrors /restrict — same destructive treatment."""
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="svc-user", name="coord-a")
    coord.session, _ = _make_session_mock()

    client = _service_token_client(
        storage,
        mgr,
        user_id="svc-user",
        permissions=frozenset(),
    )
    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/stop_cascade",
        json={},
    )
    assert resp.status_code == 403


def test_trust_toggle_rejects_non_bool(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    coord.session, _ = _make_session_mock()
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/trust",
        json={"send": "yes"},
        headers=_TRUST_HEADERS,
    )
    assert resp.status_code == 400


def test_trust_toggle_rejects_non_object_body(storage):
    """A valid-JSON-but-non-object body (null / list / scalar) must
    400 cleanly rather than AttributeError → 500."""
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    coord.session, _ = _make_session_mock()
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    # Non-dict JSON values — all must 400.  Different bodies may hit
    # `read_json_or_400`'s own parse error ("Invalid JSON body") or the
    # downstream dict-shape guard ("body must be a JSON object"); we
    # only care that none 500.
    for body in ([], 42, "string"):
        resp = client.post(
            f"/v1/api/workstreams/{coord.id}/trust",
            json=body,
            headers=_TRUST_HEADERS,
        )
        assert resp.status_code == 400, body
        assert "JSON object" in resp.json()["error"], resp.json()


def test_restrict_rejects_non_object_body(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    coord.session, _ = _make_session_mock()
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/restrict",
        json=[],
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 400


def test_trust_toggle_cluster_wide_access(storage):
    # Trusted-team model: the trust toggle is gated on the scope
    # permission, not on row-level ownership.  A caller holding
    # ``coordinator.trust.send`` may toggle any coord's trust state.
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-owner", name="coord-a")
    coord.session, _ = _make_session_mock()
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/trust",
        json={"send": True},
        headers={
            "X-Test-User": "user-other",
            "X-Test-Perms": "admin.coordinator,coordinator.trust.send",
        },
    )
    assert resp.status_code == 200


def test_trust_toggle_404_when_session_not_loaded(storage):
    """Persisted-but-not-loaded coordinator: runtime session state can't
    be mutated, so the endpoint 404s.  Matches the tenant-miss shape
    so non-admins can't probe for closed rows via this endpoint."""
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    coord.session = None  # simulate a closed / lazy-rehydrate coord
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/trust",
        json={"send": True},
        headers=_TRUST_HEADERS,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# _prepare_send_to_workstream — trust gate (item 1, unit-level)
# ---------------------------------------------------------------------------


def test_prepare_send_to_workstream_trust_skips_approval_for_own_child():
    from turnstone.core.session import ChatSession

    session = ChatSession.__new__(ChatSession)
    session._coord_client = MagicMock()
    session._trust_send = True
    session._coord_client._is_own_subtree.return_value = True

    item = session._prepare_send_to_workstream(call_id="c1", args={"ws_id": "abc", "message": "hi"})
    assert item["needs_approval"] is False
    assert item["trust_auto_approved"] is True


def test_prepare_send_to_workstream_trust_holds_for_foreign_ws():
    from turnstone.core.session import ChatSession

    session = ChatSession.__new__(ChatSession)
    session._coord_client = MagicMock()
    session._trust_send = True
    session._coord_client._is_own_subtree.return_value = False

    item = session._prepare_send_to_workstream(
        call_id="c2", args={"ws_id": "foreign-ws", "message": "hi"}
    )
    assert item["needs_approval"] is True
    assert item["trust_auto_approved"] is False


def test_prepare_send_to_workstream_without_trust_always_requires_approval():
    from turnstone.core.session import ChatSession

    session = ChatSession.__new__(ChatSession)
    session._coord_client = MagicMock()
    session._trust_send = False
    session._coord_client._is_own_subtree.return_value = True

    item = session._prepare_send_to_workstream(call_id="c3", args={"ws_id": "abc", "message": "hi"})
    assert item["needs_approval"] is True
    assert item["trust_auto_approved"] is False


def test_exec_send_to_workstream_records_trust_audit(storage):
    """The audit row fires before the HTTP send so a downstream failure
    can't suppress the trail."""
    from turnstone.console.coordinator_client import CoordinatorClient
    from turnstone.core.session import ChatSession

    client = CoordinatorClient.__new__(CoordinatorClient)
    client._storage = storage
    client._user_id = "user-1"
    client._coord_ws_id = "coord-1"

    session = ChatSession.__new__(ChatSession)
    session._coord_client = client
    session.ui = MagicMock()
    send_mock = MagicMock(return_value={"status": "ok"})
    client.send = send_mock  # type: ignore[method-assign]

    session._exec_send_to_workstream(
        {
            "call_id": "c1",
            "ws_id": "child-ws-1",
            "message": "please summarise",
            "trust_auto_approved": True,
        }
    )

    events = [
        e for e in storage.list_audit_events() if e["action"] == "coordinator.send.auto_approved"
    ]
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert detail["src"] == "coordinator"
    assert detail["trust"] is True
    assert detail["ws_id"] == "child-ws-1"
    assert "please summarise" in detail["message_preview"]


# ---------------------------------------------------------------------------
# /restrict endpoint + _prepare_tool revocation gate (item 5a)
# ---------------------------------------------------------------------------


def test_restrict_adds_to_revoked_tools_and_audits(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    session, state = _make_session_mock()
    coord.session = session
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())

    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/restrict",
        json={"revoke": ["spawn_workstream", "delete_workstream"]},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["revoked_tools"]) == {"spawn_workstream", "delete_workstream"}
    assert state["revoked"] == frozenset({"spawn_workstream", "delete_workstream"})

    events = [e for e in storage.list_audit_events() if e["action"] == "coordinator.restricted"]
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert set(detail["revoked"]) == {"spawn_workstream", "delete_workstream"}


def test_restrict_is_additive_across_calls(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    coord.session, _ = _make_session_mock()
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())

    client.post(
        f"/v1/api/workstreams/{coord.id}/restrict",
        json={"revoke": ["spawn_workstream"]},
        headers=_COORD_HEADERS,
    )
    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/restrict",
        json={"revoke": ["delete_workstream"]},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    assert set(resp.json()["revoked_tools"]) == {
        "spawn_workstream",
        "delete_workstream",
    }


def test_restrict_empty_revoke_is_noop_but_audits(storage):
    """Empty list is accepted as a no-op write — still emits the audit
    row so operators can see 'operator poked the restrict endpoint but
    didn't actually revoke anything' events."""
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    coord.session, _state = _make_session_mock(revoked=frozenset({"spawn_workstream"}))
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/restrict",
        json={"revoke": []},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    # Pre-existing revocations are preserved; no new entries were added.
    assert set(resp.json()["revoked_tools"]) == {"spawn_workstream"}
    events = [e for e in storage.list_audit_events() if e["action"] == "coordinator.restricted"]
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert detail["revoked"] == []


def test_restrict_rejects_non_list_body(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    coord.session, _ = _make_session_mock()
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/restrict",
        json={"revoke": "spawn_workstream"},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 400


def test_restrict_rejects_oversize_list(storage):
    """Defense-in-depth cap — an admin-sized list can't blow up the
    session frozenset or the audit row's detail column."""
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    coord.session, _ = _make_session_mock()
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/restrict",
        json={"revoke": [f"tool_{i}" for i in range(500)]},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 400


def test_restrict_rejects_oversize_name(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    coord.session, _ = _make_session_mock()
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/restrict",
        json={"revoke": ["x" * 1000]},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 400


def test_restrict_404_when_session_not_loaded(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    coord.session = None
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/restrict",
        json={"revoke": ["bash"]},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 404


def test_prepare_tool_blocks_revoked_tool():
    """Revocation short-circuits BEFORE the preparer dispatch so the
    model sees a clear 'revoked' error rather than a preparer-level
    validation message."""
    from turnstone.core.session import ChatSession

    session = ChatSession.__new__(ChatSession)
    session._revoked_tools = frozenset({"spawn_workstream"})
    session._mcp_client = None
    session.ui = MagicMock()

    tc = {
        "id": "call-1",
        "function": {
            "name": "spawn_workstream",
            "arguments": '{"initial_message": "x"}',
        },
    }
    item = session._prepare_tool(tc)
    assert item["needs_approval"] is False
    assert "revoked" in item["header"].lower()
    assert "revoked" in item["error"].lower()


def test_prepare_tool_allows_non_revoked_tool():
    """The revocation gate must not fire on a tool name that isn't in
    the revoked set.  We pick a name that's also not in the preparers
    dict so we can assert the 'unknown tool' result shape without
    exercising a real preparer."""
    from turnstone.core.session import ChatSession

    session = ChatSession.__new__(ChatSession)
    session._revoked_tools = frozenset({"spawn_workstream"})
    session._mcp_client = None
    session.ui = MagicMock()

    tc = {
        "id": "call-2",
        "function": {"name": "this_tool_is_not_registered", "arguments": "{}"},
    }
    item = session._prepare_tool(tc)
    # Unknown tool path — not the revocation error path.
    err = str(item.get("error") or "")
    assert "revoked" not in err.lower()


# ---------------------------------------------------------------------------
# /stop_cascade endpoint (item 5b)
# ---------------------------------------------------------------------------


def test_stop_cascade_cancels_coord_and_each_child(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    _seed_children(mgr._adapter, coord.id, ["child-1", "child-2", "child-3"])

    def _cancel(wid: str) -> dict:
        if wid == "child-2":
            return {"error": "gateway_timeout", "status": 502}
        return {"status": "ok"}

    coord_client = MagicMock()
    coord_client.cancel.side_effect = _cancel
    coord.session = MagicMock()
    coord.session._coord_client = coord_client

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/stop_cascade",
        json={},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["cancelled"] + body["failed"] + body["skipped"]) == {
        "child-1",
        "child-2",
        "child-3",
    }
    assert body["failed"] == ["child-2"]
    assert set(body["cancelled"]) == {"child-1", "child-3"}
    assert body["skipped"] == []
    assert coord_client.cancel.call_count == 3

    events = [
        e for e in storage.list_audit_events() if e["action"] == "coordinator.stopped_cascade"
    ]
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert set(detail["cancelled"] + detail["failed"] + detail["skipped"]) == {
        "child-1",
        "child-2",
        "child-3",
    }


def test_stop_cascade_routes_404_to_skipped_bucket(storage):
    """A stale registry entry (child row already deleted from storage)
    or an upstream-404 on cancel is semantically 'already gone', not a
    dispatch failure.  Report it in ``skipped`` so operators can tell
    them apart."""
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    _seed_children(mgr._adapter, coord.id, ["stale-child"])

    coord_client = MagicMock()
    coord_client.cancel.return_value = {
        "error": "workstream not in coordinator subtree: stale-child",
        "status": 404,
    }
    coord.session = MagicMock()
    coord.session._coord_client = coord_client

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/stop_cascade",
        json={},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cancelled"] == []
    assert body["failed"] == []
    assert body["skipped"] == ["stale-child"]


def test_stop_cascade_empty_children_still_audits(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    coord.session = MagicMock()
    coord.session._coord_client = MagicMock()

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/stop_cascade",
        json={},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "ok", "cancelled": [], "failed": [], "skipped": []}
    assert [e for e in storage.list_audit_events() if e["action"] == "coordinator.stopped_cascade"]


def test_stop_cascade_without_coord_client_marks_all_failed(storage):
    """If the coord session has no attached coord_client (unexpected
    state for a loaded session), every child routes to ``failed`` so
    the operator can investigate."""
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    _seed_children(mgr._adapter, coord.id, ["child-a", "child-b"])
    coord.session = MagicMock()
    coord.session._coord_client = None

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/stop_cascade",
        json={},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cancelled"] == []
    assert body["skipped"] == []
    assert set(body["failed"]) == {"child-a", "child-b"}


def test_stop_cascade_404_when_session_not_loaded(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    coord.session = None
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/stop_cascade",
        json={},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 404


def test_children_snapshot_returns_copy_not_live_set(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    _seed_children(mgr._adapter, coord.id, ["a", "b", "c"])
    snap = mgr._adapter.children_snapshot(coord.id)
    assert set(snap) == {"a", "b", "c"}
    _seed_children(mgr._adapter, coord.id, ["d"])
    assert set(snap) == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# ChatSession governance methods (q-14) — unit-level
# ---------------------------------------------------------------------------


def test_set_and_get_trust_send_round_trip():
    from turnstone.core.session import ChatSession

    session = ChatSession.__new__(ChatSession)
    import threading as _t

    session._trust_send = False
    session._governance_lock = _t.Lock()

    assert session.get_trust_send() is False
    session.set_trust_send(True)
    assert session.get_trust_send() is True
    session.set_trust_send(False)
    assert session.get_trust_send() is False


def test_revoke_tools_is_additive_and_returns_post_state():
    from turnstone.core.session import ChatSession

    session = ChatSession.__new__(ChatSession)
    import threading as _t

    session._revoked_tools = frozenset()
    session._governance_lock = _t.Lock()

    after = session.revoke_tools(["bash", "read_file"])
    assert after == frozenset({"bash", "read_file"})
    after2 = session.revoke_tools(["write_file"])
    assert after2 == frozenset({"bash", "read_file", "write_file"})
    # Re-revoking is a no-op (idempotent).
    after3 = session.revoke_tools(["bash"])
    assert after3 == after2
    assert session.get_revoked_tools() == after3
