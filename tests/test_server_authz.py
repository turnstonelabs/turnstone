"""HTTP-boundary authorization tests for turnstone-server.

Covers the ownership gates added in PR #2 (sec-1 through sec-9 +
sec-11) and the kind-validation branches that PR #1 tightened but
never had Starlette-level regression coverage.  Each test crosses
the middleware → handler boundary via ``TestClient`` so the JWT
decoding, scope extraction, and audit-context wiring are all exercised.
"""

from __future__ import annotations

import json
import queue
import threading
from typing import Any

import pytest
from starlette.testclient import TestClient

_TEST_JWT_SECRET = "test-jwt-secret-minimum-32-chars!"


def _make_jwt(user_id: str, *, scopes: frozenset[str] | None = None) -> str:
    from turnstone.core.auth import JWT_AUD_SERVER, create_jwt

    return create_jwt(
        user_id=user_id,
        scopes=scopes or frozenset({"read", "write", "approve"}),
        source="test",
        secret=_TEST_JWT_SECRET,
        audience=JWT_AUD_SERVER,
    )


def _auth(user: str, *, scopes: frozenset[str] | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_jwt(user, scopes=scopes)}"}


# ---------------------------------------------------------------------------
# FakeUI / FakeSession doubles — match the shape the create handler expects
# ---------------------------------------------------------------------------


class _FakeUI:
    def __init__(self, ws_id: str = "", user_id: str = "", **_kw: Any) -> None:
        self.ws_id = ws_id
        self._user_id = user_id
        self.auto_approve = False
        self.auto_approve_tools: set[str] = set()
        self._enqueued: list[dict[str, Any]] = []
        self._listeners: list[queue.Queue[dict[str, Any]]] = []
        self._listeners_lock = threading.Lock()
        self._pending_approval: dict[str, Any] | None = None
        self._pending_plan_review: dict[str, Any] | None = None
        self._approval_event = threading.Event()
        self._plan_event = threading.Event()
        self._fg_event = threading.Event()
        self._ws_lock = threading.Lock()
        # Dashboard handler reads these fields under _ws_lock to build
        # per-ws summary rows; keep them zero/empty for the fake so the
        # handler doesn't need to special-case.
        self._ws_prompt_tokens = 0
        self._ws_completion_tokens = 0
        self._ws_tool_calls: dict[str, int] = {}
        self._ws_context_ratio = 0.0
        self._ws_current_activity = ""
        self._ws_activity_state = ""
        self._ws_messages = 0
        self._ws_turn_tool_calls = 0

    def _register_listener(self) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._listeners_lock:
            self._listeners.append(q)
        return q

    def _enqueue(self, ev: dict[str, Any]) -> None:
        self._enqueued.append(ev)

    def on_stream_end(self) -> None:
        pass

    def on_state_change(self, _state: str) -> None:
        pass

    def on_error(self, _msg: str) -> None:
        pass

    def resolve_approval(self, *_a: Any, **_kw: Any) -> None:
        self._approval_event.set()

    def resolve_plan(self, *_a: Any, **_kw: Any) -> None:
        self._plan_event.set()


class _FakeSession:
    def __init__(self, ws_id: str = "", user_id: str = "") -> None:
        self.ws_id = ws_id
        self.user_id = user_id
        self.model = "test-model"
        self.model_alias = ""
        self.reasoning_effort = ""
        self.context_window = 100000
        self.messages: list[dict[str, Any]] = []
        self._last_usage: dict[str, int] | None = None
        self._pending_retry: str | None = None
        self.sends: list[tuple[str, Any, Any]] = []

    def send(self, text: str, *, attachments: Any = None, send_id: Any = None) -> None:
        self.sends.append((text, attachments, send_id))

    def set_watch_runner(self, *_a: Any, **_kw: Any) -> None:
        pass

    def resume(self, _ws_id: str, *, fork: bool = False) -> bool:
        return False

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        pass

    def handle_command(self, _cmd: str) -> bool:
        return False

    def request_title_refresh(self, _title: str) -> None:
        pass


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """Full turnstone-server app with in-memory workstreams + fake sessions."""
    from turnstone.core.metrics import MetricsCollector
    from turnstone.core.storage import get_storage, init_storage, reset_storage
    from turnstone.core.workstream import WorkstreamManager
    from turnstone.server import create_app

    reset_storage()
    init_storage("sqlite", path=str(tmp_path / "t.db"), run_migrations=False)

    metrics = MetricsCollector()
    metrics.model = "test-model"
    monkeypatch.setattr("turnstone.server._metrics", metrics)
    monkeypatch.setattr("turnstone.server.WebUI", _FakeUI)

    def _factory(ui: Any, _model: Any, ws_id: str, **_kw: Any) -> _FakeSession:
        uid = getattr(ui, "_user_id", "")
        return _FakeSession(ws_id=ws_id, user_id=uid)

    mgr = WorkstreamManager(_factory, max_workstreams=10, node_id="node-test")
    gq: queue.Queue[dict[str, Any]] = queue.Queue()
    app = create_app(
        workstreams=mgr,
        global_queue=gq,
        global_listeners=[],
        global_listeners_lock=threading.Lock(),
        skip_permissions=False,
        jwt_secret=_TEST_JWT_SECRET,
        auth_storage=get_storage(),
    )
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client, mgr
    finally:
        client.close()
        reset_storage()


# ---------------------------------------------------------------------------
# PR #1 HTTP-boundary kind validation (q-4) — previously untested
# ---------------------------------------------------------------------------


class TestKindValidationOnCreate:
    """POST /v1/api/workstreams/new — kind field validation at the HTTP edge."""

    def test_rejects_kind_coordinator(self, app_client):
        client, _mgr = app_client
        resp = client.post(
            "/v1/api/workstreams/new",
            json={"kind": "coordinator", "name": "x"},
            headers=_auth("user-1"),
        )
        assert resp.status_code == 400
        assert "coordinator" in resp.json()["error"].lower()

    def test_rejects_unknown_kind(self, app_client):
        client, _mgr = app_client
        resp = client.post(
            "/v1/api/workstreams/new",
            json={"kind": "interative", "name": "x"},  # typo
            headers=_auth("user-1"),
        )
        assert resp.status_code == 400
        assert "unknown" in resp.json()["error"].lower()

    def test_accepts_default_kind(self, app_client):
        client, _mgr = app_client
        resp = client.post(
            "/v1/api/workstreams/new",
            json={"name": "x"},  # kind omitted
            headers=_auth("user-1"),
        )
        assert resp.status_code == 200

    def test_rejects_cross_tenant_parent_ws_id(self, app_client, tmp_path):
        """parent_ws_id pointing at another user's coordinator → 403."""
        from turnstone.core.storage import get_storage

        client, _mgr = app_client
        storage = get_storage()
        assert storage is not None
        # Victim creates a coordinator directly in storage (console path).
        storage.register_workstream(
            "victim-coord",
            node_id="console",
            name="victim",
            kind="coordinator",
            user_id="victim-user",
        )
        resp = client.post(
            "/v1/api/workstreams/new",
            json={"name": "attacker", "parent_ws_id": "victim-coord"},
            headers=_auth("attacker-user"),
        )
        assert resp.status_code == 403
        assert "coordinator you own" in resp.json()["error"]


class TestOpenKindGate:
    """POST /v1/api/workstreams/{ws_id}/open refuses coordinator rows."""

    def test_refuses_to_open_coordinator(self, app_client):
        from turnstone.core.storage import get_storage

        client, _mgr = app_client
        storage = get_storage()
        assert storage is not None
        storage.register_workstream(
            "coord-1",
            node_id="console",
            name="c",
            kind="coordinator",
            user_id="user-1",
        )
        resp = client.post(
            "/v1/api/workstreams/coord-1/open",
            headers=_auth("user-1"),
        )
        assert resp.status_code == 400
        assert "interactive" in resp.json()["error"].lower()


# ---------------------------------------------------------------------------
# PR #2 authz cluster — cross-tenant gates on interactive-ws mutations
# ---------------------------------------------------------------------------


def _register_ws(storage: Any, ws_id: str, owner: str) -> None:
    storage.register_workstream(ws_id, node_id="node-test", name=ws_id, user_id=owner)


class TestCrossTenantDelete:
    def test_non_owner_cannot_delete(self, app_client):
        from turnstone.core.storage import get_storage

        client, _mgr = app_client
        storage = get_storage()
        assert storage is not None
        _register_ws(storage, "ws-victim", "victim-user")
        resp = client.post(
            "/v1/api/workstreams/ws-victim/delete",
            headers=_auth("attacker-user"),
        )
        assert resp.status_code == 404
        # Victim's workstream still present in storage.
        assert storage.get_workstream("ws-victim") is not None

    def test_owner_delete_records_audit(self, app_client):
        from turnstone.core.storage import get_storage

        client, _mgr = app_client
        storage = get_storage()
        assert storage is not None
        _register_ws(storage, "ws-own", "user-1")
        resp = client.post(
            "/v1/api/workstreams/ws-own/delete",
            headers=_auth("user-1"),
        )
        assert resp.status_code == 200
        events = storage.list_audit_events(action="workstream.deleted")
        assert any(e["resource_id"] == "ws-own" for e in events)


class TestCrossTenantApprove:
    def test_non_owner_cannot_approve(self, app_client):
        from turnstone.core.storage import get_storage

        client, _mgr = app_client
        storage = get_storage()
        assert storage is not None
        _register_ws(storage, "ws-victim", "victim-user")
        resp = client.post(
            "/v1/api/approve",
            json={"ws_id": "ws-victim", "approved": True},
            headers=_auth("attacker-user"),
        )
        assert resp.status_code == 404


class TestCrossTenantClose:
    def test_non_owner_cannot_close(self, app_client):
        from turnstone.core.storage import get_storage

        client, _mgr = app_client
        storage = get_storage()
        assert storage is not None
        _register_ws(storage, "ws-victim", "victim-user")
        resp = client.post(
            "/v1/api/workstreams/close",
            json={"ws_id": "ws-victim"},
            headers=_auth("attacker-user"),
        )
        assert resp.status_code == 404


class TestCrossTenantTitle:
    def test_non_owner_cannot_refresh_title(self, app_client):
        from turnstone.core.storage import get_storage

        client, _mgr = app_client
        storage = get_storage()
        assert storage is not None
        _register_ws(storage, "ws-victim", "victim-user")
        resp = client.post(
            "/v1/api/workstreams/ws-victim/refresh-title",
            headers=_auth("attacker-user"),
        )
        assert resp.status_code == 404

    def test_non_owner_cannot_set_title(self, app_client):
        from turnstone.core.storage import get_storage

        client, _mgr = app_client
        storage = get_storage()
        assert storage is not None
        _register_ws(storage, "ws-victim", "victim-user")
        resp = client.post(
            "/v1/api/workstreams/ws-victim/title",
            json={"title": "phishing title"},
            headers=_auth("attacker-user"),
        )
        assert resp.status_code == 404


class TestCrossTenantOpen:
    def test_non_owner_cannot_open_persisted(self, app_client):
        from turnstone.core.storage import get_storage

        client, _mgr = app_client
        storage = get_storage()
        assert storage is not None
        _register_ws(storage, "ws-victim", "victim-user")
        resp = client.post(
            "/v1/api/workstreams/ws-victim/open",
            headers=_auth("attacker-user"),
        )
        assert resp.status_code == 404


class TestListWorkstreamsTrustedTeamVisibility:
    """Listing endpoints (/workstreams, /dashboard, /workstreams/saved)
    return the cluster-wide set to any authenticated caller.  Mutations
    are gated independently on the per-workstream handlers — see
    TestCrossTenant{Delete,Approve,Close,Title,Open} for those gates."""

    def test_list_returns_all_owners(self, app_client):
        client, _mgr = app_client
        resp_a = client.post(
            "/v1/api/workstreams/new",
            json={"name": "a"},
            headers=_auth("user-a"),
        )
        resp_b = client.post(
            "/v1/api/workstreams/new",
            json={"name": "b"},
            headers=_auth("user-b"),
        )
        assert resp_a.status_code == 200 and resp_b.status_code == 200
        ws_a, ws_b = resp_a.json()["ws_id"], resp_b.json()["ws_id"]

        # user-a now sees both.
        resp = client.get("/v1/api/workstreams", headers=_auth("user-a"))
        assert resp.status_code == 200
        ids = {w["id"] for w in resp.json()["workstreams"]}
        assert {ws_a, ws_b}.issubset(ids), ids


class TestDashboardTrustedTeamVisibility:
    def test_dashboard_aggregate_includes_all_owners(self, app_client):
        client, _mgr = app_client
        client.post("/v1/api/workstreams/new", json={"name": "a"}, headers=_auth("user-a"))
        client.post("/v1/api/workstreams/new", json={"name": "b"}, headers=_auth("user-b"))
        client.post("/v1/api/workstreams/new", json={"name": "b2"}, headers=_auth("user-b"))

        resp = client.get("/v1/api/dashboard", headers=_auth("user-b"))
        assert resp.status_code == 200
        data = resp.json()
        # All three workstreams visible regardless of caller identity.
        assert data["aggregate"]["total_count"] == 3
        owners = {w["user_id"] for w in data["workstreams"]}
        assert {"user-a", "user-b"}.issubset(owners)


class TestSavedWorkstreamsTrustedTeamVisibility:
    """Listing returns the cluster-wide set across all owners.  Resuming
    an owned saved workstream goes through the per-workstream ownership
    gate on /open (see TestCrossTenantOpen); ownerless persisted rows
    are claimable by any authenticated caller via /open, consistent
    with the same trusted-team model."""

    def _seed(self, client):
        """Create two workstreams per user, each with a message so they
        land in list_workstreams_with_history (the SQL gates on an
        EXISTS conversation)."""
        from turnstone.core.storage import get_storage

        storage = get_storage()
        assert storage is not None
        _register_ws(storage, "alice-saved", "alice")
        storage.save_message("alice-saved", "user", "alice's plan")
        _register_ws(storage, "bob-saved", "bob")
        storage.save_message("bob-saved", "user", "bob's plan")
        return storage

    def test_any_caller_sees_all_rows(self, app_client):
        client, _mgr = app_client
        self._seed(client)
        resp = client.get("/v1/api/workstreams/saved", headers=_auth("alice"))
        assert resp.status_code == 200
        ids = {r["ws_id"] for r in resp.json()["workstreams"]}
        assert {"alice-saved", "bob-saved"}.issubset(ids), ids

    def test_service_scope_sees_all_rows(self, app_client):
        """Service-scope still works — same set, different auth path."""
        client, _mgr = app_client
        self._seed(client)
        resp = client.get(
            "/v1/api/workstreams/saved",
            headers=_auth("cluster-collector", scopes=frozenset({"read", "service"})),
        )
        assert resp.status_code == 200
        ids = {r["ws_id"] for r in resp.json()["workstreams"]}
        assert {"alice-saved", "bob-saved"}.issubset(ids)

    def test_orphan_rows_visible(self, app_client):
        """Ownerless rows (empty user_id from migrations / startup
        ``name="default"``) appear in the cluster-wide listing alongside
        owned rows.  /open lets any authenticated caller claim them —
        intentional under the trusted-team model — so the listing isn't
        leaking anything the resume path wouldn't already grant."""
        client, _mgr = app_client
        storage = self._seed(client)
        _register_ws(storage, "orphan-saved", "")
        storage.save_message("orphan-saved", "user", "orphan content")
        resp = client.get(
            "/v1/api/workstreams/saved",
            headers=_auth("alice", scopes=frozenset({"read"})),
        )
        assert resp.status_code == 200
        ids = {r["ws_id"] for r in resp.json()["workstreams"]}
        assert "orphan-saved" in ids

    def test_coordinator_rows_excluded_even_for_service(self, app_client):
        """kind filter is orthogonal to the user_id filter — even a
        service caller (cluster-wide) must not see coordinator rows on
        the interactive 'saved workstreams' endpoint."""
        from turnstone.core.storage import get_storage
        from turnstone.core.workstream import WorkstreamKind

        client, _mgr = app_client
        storage = get_storage()
        assert storage is not None
        storage.register_workstream(
            "coord-row",
            node_id="console",
            user_id="alice",
            name="alice-coord",
            kind=WorkstreamKind.COORDINATOR,
            parent_ws_id=None,
        )
        storage.save_message("coord-row", "user", "planning")
        _register_ws(storage, "alice-interactive", "alice")
        storage.save_message("alice-interactive", "user", "interactive")

        resp = client.get(
            "/v1/api/workstreams/saved",
            headers=_auth("alice", scopes=frozenset({"read", "service"})),
        )
        assert resp.status_code == 200
        ids = {r["ws_id"] for r in resp.json()["workstreams"]}
        assert "alice-interactive" in ids
        assert "coord-row" not in ids


class TestGlobalEventsServiceGate:
    def test_non_service_rejected(self, app_client):
        client, _mgr = app_client
        resp = client.get(
            "/v1/api/events/global",
            headers=_auth("user-a"),  # no service scope
        )
        assert resp.status_code == 403
        assert "service" in resp.json()["error"].lower()

    def test_service_scope_accepted(self, app_client):
        """Regression for the console-collector 403 footgun: the
        collector's ServiceTokenManager is configured in console/server.py
        with scopes ``{"read", "service"}``.  This gate must accept
        exactly that scope set so the collector's SSE subscription
        doesn't silently 403 out (#sev-0).  Any future scope renaming
        that would drop ``"service"`` from the node-side check breaks
        this test before it breaks the dashboard.

        Probe a deliberately-wrong ``expected_node_id`` — the handler
        runs the scope gate first, then the node-identity check.  A
        409 response proves we made it past the scope gate (which is
        what this test is asserting), while also avoiding an
        indefinitely-open SSE stream the TestClient would never close.
        """
        client, _mgr = app_client
        # Exact scope set the collector uses today.
        collector_scopes = frozenset({"read", "service"})
        resp = client.get(
            "/v1/api/events/global?expected_node_id=definitely-wrong-node-id",
            headers=_auth("console-collector", scopes=collector_scopes),
        )
        # 409 = the scope gate passed and we hit the node-identity
        # mismatch branch.  Anything else (403 / 500 / 200 stream)
        # is a failure for this contract.
        assert resp.status_code == 409, (
            f"service-scoped token did not reach node-id check: "
            f"{resp.status_code} {resp.text[:120]}"
        )


class TestPerWsSseGate:
    def test_non_owner_rejected(self, app_client):
        from turnstone.core.storage import get_storage

        client, _mgr = app_client
        storage = get_storage()
        assert storage is not None
        _register_ws(storage, "ws-victim", "victim-user")
        resp = client.get(
            "/v1/api/events?ws_id=ws-victim",
            headers=_auth("attacker-user"),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Audit events on successful mutations (sec-11)
# ---------------------------------------------------------------------------


class TestAuditEventsOnMutations:
    def test_workstream_created_emits_audit(self, app_client):
        from turnstone.core.storage import get_storage

        client, _mgr = app_client
        storage = get_storage()
        assert storage is not None
        resp = client.post(
            "/v1/api/workstreams/new",
            json={"name": "auditme"},
            headers=_auth("user-audit"),
        )
        assert resp.status_code == 200
        ws_id = resp.json()["ws_id"]
        events = storage.list_audit_events(action="workstream.created")
        matching = [e for e in events if e["resource_id"] == ws_id]
        assert matching, "audit row absent for newly created workstream"
        detail = json.loads(matching[0]["detail"])
        assert detail["kind"] == "interactive"
