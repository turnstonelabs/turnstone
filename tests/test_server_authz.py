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
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

_TEST_JWT_SECRET = "test-jwt-secret-minimum-32-chars!"


# Default permission set for test JWTs.  Mirrors what builtin-operator
# carries: enough perms to exercise create/close/approve gates without
# turning every existing test into a re-authorization round.  Tests
# negating these gates pass ``permissions=frozenset()`` explicitly.
_DEFAULT_TEST_PERMS = frozenset(
    {"workstreams.create", "workstreams.close", "tools.approve", "conversation.modify"}
)


def _make_jwt(
    user_id: str,
    *,
    scopes: frozenset[str] | None = None,
    permissions: frozenset[str] | None = None,
) -> str:
    from turnstone.core.auth import JWT_AUD_SERVER, create_jwt

    return create_jwt(
        user_id=user_id,
        scopes=scopes or frozenset({"read", "write", "approve"}),
        source="test",
        secret=_TEST_JWT_SECRET,
        audience=JWT_AUD_SERVER,
        permissions=_DEFAULT_TEST_PERMS if permissions is None else permissions,
    )


def _auth(
    user: str,
    *,
    scopes: frozenset[str] | None = None,
    permissions: frozenset[str] | None = None,
) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_jwt(user, scopes=scopes, permissions=permissions)}"}


class TestAssignableScopes:
    """``service`` scope is a cross-tenant bypass and must never be
    GRANTED via a user-facing token mint (admin API or CLI) — otherwise an
    ``admin.users`` holder could self-mint it and see every private
    project's workstreams.  Both mint paths route through
    :func:`reject_unassignable_scopes`."""

    def test_service_scope_rejected(self) -> None:
        from turnstone.core.auth import reject_unassignable_scopes

        assert reject_unassignable_scopes("service") is not None
        assert reject_unassignable_scopes("read,service") is not None
        assert reject_unassignable_scopes("read,write,approve,service") is not None

    def test_service_not_in_assignable_set(self) -> None:
        from turnstone.core.auth import ASSIGNABLE_SCOPES, VALID_SCOPES

        assert "service" in VALID_SCOPES  # still a valid runtime scope
        assert "service" not in ASSIGNABLE_SCOPES  # but not user-assignable

    def test_ordinary_scopes_accepted(self) -> None:
        from turnstone.core.auth import reject_unassignable_scopes

        assert reject_unassignable_scopes("read") is None
        assert reject_unassignable_scopes("read,write,approve") is None

    def test_empty_and_unknown_rejected(self) -> None:
        from turnstone.core.auth import reject_unassignable_scopes

        assert reject_unassignable_scopes("") is not None
        assert reject_unassignable_scopes("bogus") is not None


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
        self.states: list[str] = []
        self.infos: list[str] = []
        self.errors: list[str] = []
        self._listeners: list[queue.Queue[dict[str, Any]]] = []
        self._listeners_lock = threading.Lock()
        self._pending_approval: dict[str, Any] | None = None
        self._approval_event = threading.Event()
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
        self._llm_verdicts: dict[str, dict[str, Any]] = {}

    def serialize_pending_approval_details(self) -> list[dict[str, Any]]:
        # Mirrors SessionUIBase.serialize_pending_approval_details —
        # the fake is monkeypatched in for ``WebUI`` and the dashboard
        # handler reads this method during projection. Real subclasses
        # iterate their approval-cycle registry (one entry per live
        # cycle); the fake models a single slot, so the list carries
        # zero or one entries.
        pending = self._pending_approval
        if pending is None:
            return []
        items = pending.get("items") or []
        if not items:
            return []
        call_ids = [item.get("call_id", "") for item in items]
        # Match the real impl's pattern (session_ui_base.py): snapshot
        # references under the lock, copy after release. Writers only
        # assign — never mutate — so the reference snapshot is stable
        # outside the lock window.
        with self._ws_lock:
            verdict_refs = {
                cid: self._llm_verdicts[cid]
                for cid in call_ids
                if cid and cid in self._llm_verdicts
            }
        verdicts = {cid: dict(v) for cid, v in verdict_refs.items()}
        serialized: list[dict[str, Any]] = []
        for item in items:
            cid = item.get("call_id", "")
            serialized.append(
                {
                    "call_id": cid,
                    "header": item.get("header", ""),
                    "preview": item.get("preview", ""),
                    "func_name": item.get("func_name", ""),
                    "approval_label": item.get("approval_label", ""),
                    "needs_approval": item.get("needs_approval", False),
                    "error": item.get("error"),
                    "heuristic_verdict": item.get("verdict"),
                    "judge_verdict": verdicts.get(cid),
                }
            )
        # Primary call_id must mirror the real serializer: first
        # *non-empty* in list order, not just first. Aligning the
        # fake here keeps test-vs-prod behavioural drift from
        # masking a real-shape regression.
        primary = next((cid for cid in call_ids if cid), "")
        return [
            {
                "cycle_id": pending.get("cycle_id", ""),
                "call_id": primary,
                "judge_pending": bool(pending.get("judge_pending", False)),
                "items": serialized,
            }
        ]

    def serialize_recent_auto_approvals(self) -> list[dict[str, Any]]:
        # Empty buffer for tests that don't exercise the auto-approve
        # visibility path.  /dashboard handler reads this method
        # unconditionally now (paired with serialize_pending_approval_detail);
        # returning [] keeps the row payload compatible without
        # modeling the full ring buffer in the fake.
        return []

    def _register_listener(self) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._listeners_lock:
            self._listeners.append(q)
        return q

    def _enqueue(self, ev: dict[str, Any]) -> None:
        self._enqueued.append(ev)

    def on_stream_end(self) -> None:
        pass

    def on_state_change(self, state: str) -> None:
        self.states.append(state)

    def on_info(self, msg: str) -> None:
        self.infos.append(msg)

    def on_error(self, msg: str) -> None:
        self.errors.append(msg)

    def resolve_approval(self, *_a: Any, **_kw: Any) -> None:
        self._approval_event.set()


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
        # Real sessions always carry one; the /send route's cancel-drain
        # poll reads it whenever a worker is live at send time.
        self._cancel_event = threading.Event()
        self.sends: list[tuple[str, Any, Any]] = []
        self.commands: list[str] = []
        self.compacts = 0
        self.compact_raises: BaseException | None = None
        self.send_raises: BaseException | None = None
        self.exit_commands: set[str] = set()
        self.queued_flushes = 0
        self.queued_text = ""
        # Gates let a test hold the worker slot open mid-command so a
        # concurrent /send provably lands in the PARK path.
        self.compact_gate: threading.Event | None = None
        self.command_gate: threading.Event | None = None
        self.command_raises: BaseException | None = None
        # Every queue_message call — the park invariant is that command
        # windows NEVER reach the interjection queue.
        self.queue_calls: list[str] = []

    def send(self, text: str, *, attachments: Any = None, send_id: Any = None) -> None:
        self.sends.append((text, attachments, send_id))
        if self.send_raises is not None:
            raise self.send_raises

    def queue_message(
        self,
        text: str,
        attachment_ids: Any = None,
        queue_msg_id: str | None = None,
        interjector_user_id: str = "",
    ) -> tuple[str, str, str]:
        self.queue_calls.append(text)
        cleaned = text[:2000] + "..." if len(text) > 2000 else text
        return cleaned, "notice", queue_msg_id or "m1"

    def set_watch_runner(self, *_a: Any, **_kw: Any) -> None:
        pass

    def resume(self, _ws_id: str, *, fork: bool = False) -> bool:
        return False

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        pass

    def handle_command(self, cmd: str) -> bool:
        self.commands.append(cmd)
        if self.command_gate is not None:
            self.command_gate.wait(timeout=10)
        if self.command_raises is not None:
            raise self.command_raises
        return cmd in self.exit_commands

    def compact_now(self) -> bool:
        self.compacts += 1
        if self.compact_gate is not None:
            self.compact_gate.wait(timeout=10)
        if self.compact_raises is not None:
            raise self.compact_raises
        return True

    def flush_queued_messages(self) -> bool:
        self.queued_flushes += 1
        had, self.queued_text = bool(self.queued_text), ""
        return had

    def request_title_refresh(self, _title: str) -> None:
        pass


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """Full turnstone-server app with in-memory workstreams + fake sessions."""
    from turnstone.core.adapters.interactive_adapter import InteractiveAdapter
    from turnstone.core.metrics import MetricsCollector
    from turnstone.core.session_manager import SessionManager
    from turnstone.core.storage import get_storage, init_storage, reset_storage
    from turnstone.server import WebUI, create_app

    reset_storage()
    init_storage("sqlite", path=str(tmp_path / "t.db"), run_migrations=False)

    metrics = MetricsCollector()
    metrics.model = "test-model"
    monkeypatch.setattr("turnstone.server._metrics", metrics)
    monkeypatch.setattr("turnstone.server.WebUI", _FakeUI)

    def _factory(ui: Any, _model: Any, ws_id: str, **_kw: Any) -> _FakeSession:
        uid = getattr(ui, "_user_id", "")
        return _FakeSession(ws_id=ws_id, user_id=uid)

    gq: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1000)
    WebUI._global_queue = gq
    adapter = InteractiveAdapter(
        global_queue=gq,
        ui_factory=lambda ws: _FakeUI(
            ws_id=ws.id,
            user_id=ws.user_id,
        ),
        session_factory=_factory,
    )
    mgr = SessionManager(
        adapter, storage=get_storage(), max_active=10, node_id="node-test", event_emitter=adapter
    )
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
    """POST /v1/api/workstreams/{ws_id}/open refuses coordinator rows.

    Post-lift behavior change: the lifted ``open`` body delegates the
    kind check to ``SessionManager.open()`` (which returns ``None``
    for kind mismatch / missing row / tombstone — all the
    "manager has no such ws_id" cases). The pre-lift handler had a
    separate pre-mgr storage probe that returned a kind-specific
    400 ("Workstream is not an interactive kind"); the lift
    consolidates on a single 404 ("Workstream not found"). Security
    boundary unchanged — caller still can't open a coord row from
    the interactive node — but the error code + message converge
    with the rest of the not-found paths.
    """

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
        assert resp.status_code == 404
        assert "not found" in resp.json()["error"].lower()


# ---------------------------------------------------------------------------
# PR #2 authz cluster — cross-tenant gates on interactive-ws mutations
# ---------------------------------------------------------------------------


def _register_ws(storage: Any, ws_id: str, owner: str) -> None:
    storage.register_workstream(ws_id, node_id="node-test", name=ws_id, user_id=owner)


class TestCrossTenantDelete:
    def test_any_caller_can_delete(self, app_client):
        # Trusted-team model: scope auth gates the endpoint, not
        # row-level ownership.  ``user_id`` stays on audit + storage
        # metadata.
        from turnstone.core.storage import get_storage

        client, _mgr = app_client
        storage = get_storage()
        assert storage is not None
        _register_ws(storage, "ws-victim", "victim-user")
        resp = client.post(
            "/v1/api/workstreams/ws-victim/delete",
            headers=_auth("attacker-user"),
        )
        assert resp.status_code == 200

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
            "/v1/api/workstreams/ws-victim/approve",
            json={"approved": True},
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
            "/v1/api/workstreams/ws-victim/close",
            json={},
            headers=_auth("attacker-user"),
        )
        assert resp.status_code == 404


class TestPermissionGatesOnLifecycle:
    """Gates that previously didn't exist — ``workstreams.create``,
    ``workstreams.close``, ``tools.approve`` were declared, seeded into
    builtin-operator, surfaced in the admin Roles UI, and never wired
    to a single ``require_permission`` site. PR added the gates; these
    tests confirm a JWT without each perm gets 403."""

    def test_create_without_perm_returns_403(self, app_client):
        client, _mgr = app_client
        resp = client.post(
            "/v1/api/workstreams/new",
            json={"name": "no-perm"},
            headers=_auth("user-1", permissions=frozenset()),
        )
        assert resp.status_code == 403
        assert "workstreams.create" in resp.json()["error"]

    def test_close_without_perm_returns_403(self, app_client):
        from turnstone.core.storage import get_storage

        client, _mgr = app_client
        storage = get_storage()
        assert storage is not None
        _register_ws(storage, "ws-1", "user-1")
        resp = client.post(
            "/v1/api/workstreams/ws-1/close",
            json={},
            headers=_auth("user-1", permissions=frozenset()),
        )
        assert resp.status_code == 403
        assert "workstreams.close" in resp.json()["error"]

    def test_approve_without_perm_returns_403(self, app_client):
        from turnstone.core.storage import get_storage

        client, _mgr = app_client
        storage = get_storage()
        assert storage is not None
        _register_ws(storage, "ws-1", "user-1")
        resp = client.post(
            "/v1/api/workstreams/ws-1/approve",
            json={"approved": True},
            headers=_auth("user-1", permissions=frozenset()),
        )
        assert resp.status_code == 403
        assert "tools.approve" in resp.json()["error"]

    def test_create_with_perm_passes_gate(self, app_client):
        # Sanity: same call WITH the perm reaches the post-gate logic
        # (whatever its outcome — a successful create or a non-403
        # validation/state error is fine; only the gate behaviour is
        # under test here).
        client, _mgr = app_client
        resp = client.post(
            "/v1/api/workstreams/new",
            json={"name": "with-perm"},
            headers=_auth("user-1", permissions=frozenset({"workstreams.create"})),
        )
        assert resp.status_code != 403, resp.json()

    # Positive coverage for the admin.coordinator OR-fallback on each
    # of the three lifted verbs.  Without these, a future refactor
    # that dropped admin.coordinator from the accepted_permissions
    # tuple would regress coord-session children silently — the proxy
    # tests only exercise the route_proxy verb dict, not the lift.

    def test_create_with_admin_coordinator_passes_gate(self, app_client):
        client, _mgr = app_client
        resp = client.post(
            "/v1/api/workstreams/new",
            json={"name": "coord-child"},
            headers=_auth("user-1", permissions=frozenset({"admin.coordinator"})),
        )
        assert resp.status_code != 403, resp.json()

    def test_close_with_admin_coordinator_passes_gate(self, app_client):
        from turnstone.core.storage import get_storage

        client, _mgr = app_client
        storage = get_storage()
        assert storage is not None
        _register_ws(storage, "ws-1", "user-1")
        resp = client.post(
            "/v1/api/workstreams/ws-1/close",
            json={},
            headers=_auth("user-1", permissions=frozenset({"admin.coordinator"})),
        )
        assert resp.status_code != 403, resp.json()

    def test_approve_with_admin_coordinator_passes_gate(self, app_client):
        from turnstone.core.storage import get_storage

        client, _mgr = app_client
        storage = get_storage()
        assert storage is not None
        _register_ws(storage, "ws-1", "user-1")
        resp = client.post(
            "/v1/api/workstreams/ws-1/approve",
            json={"approved": True},
            headers=_auth("user-1", permissions=frozenset({"admin.coordinator"})),
        )
        assert resp.status_code != 403, resp.json()


class TestCrossTenantTitle:
    def test_refresh_title_requires_live_session(self, app_client):
        # Trusted-team model: scope-level auth is the gate; any caller
        # can hit the endpoint.  A not-currently-active workstream
        # still 404s because the refresh needs the live session, not
        # because of tenant mismatch.
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
        assert "not active" in resp.json().get("error", "") or "not found" in resp.json().get(
            "error", ""
        )

    def test_any_caller_can_set_title(self, app_client):
        # Trusted-team model: title is editable by any authenticated
        # caller; ``user_id`` remains metadata.
        from turnstone.core.storage import get_storage

        client, _mgr = app_client
        storage = get_storage()
        assert storage is not None
        _register_ws(storage, "ws-victim", "victim-user")
        resp = client.post(
            "/v1/api/workstreams/ws-victim/title",
            json={"title": "updated title"},
            headers=_auth("attacker-user"),
        )
        assert resp.status_code == 200


class TestCrossTenantOpen:
    def test_any_caller_can_open_persisted(self, app_client):
        # Trusted-team model: open is gated on scope auth, not on row
        # ownership.  The persisted ``user_id`` stays as metadata.
        from turnstone.core.storage import get_storage

        client, _mgr = app_client
        storage = get_storage()
        assert storage is not None
        _register_ws(storage, "ws-victim", "victim-user")
        resp = client.post(
            "/v1/api/workstreams/ws-victim/open",
            headers=_auth("attacker-user"),
        )
        assert resp.status_code == 200


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
        # Row key renamed id → ws_id in the Stage 2 list-verb lift.
        ids = {w["ws_id"] for w in resp.json()["workstreams"]}
        assert {ws_a, ws_b}.issubset(ids), ids

    def test_active_list_row_shape_includes_unified_fields(self, app_client):
        """Stage 2 list-verb-lift parity regression — interactive
        active-list row carries the always-include fields (ws_id,
        name, state, kind, parent_ws_id, user_id) that the lifted
        ``make_list_handler`` produces on every kind. Mirrors the
        coord-side ``test_active_list_row_shape_includes_unified_fields``
        in ``test_coordinator_endpoints.py`` so a future regression
        that drops a field on either branch is caught."""
        client, _mgr = app_client
        create_resp = client.post(
            "/v1/api/workstreams/new",
            json={"name": "shape-check"},
            headers=_auth("user-shape"),
        )
        assert create_resp.status_code == 200
        ws_id = create_resp.json()["ws_id"]

        resp = client.get("/v1/api/workstreams", headers=_auth("user-shape"))
        assert resp.status_code == 200
        body = resp.json()
        assert "workstreams" in body
        rows = [w for w in body["workstreams"] if w["ws_id"] == ws_id]
        assert len(rows) == 1
        row = rows[0]
        # Always-include row shape — interactive populates kind=
        # INTERACTIVE; user_id is post-lift parity (was coord-only).
        assert set(row.keys()) == {
            "ws_id",
            "name",
            "state",
            "kind",
            "parent_ws_id",
            "user_id",
            "project_id",
            "persona",
        }
        assert row["kind"] == "interactive"
        assert row["user_id"] == "user-shape"
        # parent_ws_id is None for top-level interactive workstreams
        # (only coord-spawned children carry it).
        assert row["parent_ws_id"] is None


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

    def test_dashboard_pending_approval_details_default_empty(self, app_client):
        """No pending approval → the list field is explicitly empty on
        the wire so consumers can distinguish "nothing pending" from
        "absent key".  Replaces 1.6's singular ``pending_approval_detail``
        null (breaking, 1.7)."""
        client, _mgr = app_client
        client.post("/v1/api/workstreams/new", json={"name": "a"}, headers=_auth("user-a"))
        resp = client.get("/v1/api/dashboard", headers=_auth("user-a"))
        assert resp.status_code == 200
        rows = resp.json()["workstreams"]
        assert len(rows) == 1
        assert "pending_approval_details" in rows[0]
        assert rows[0]["pending_approval_details"] == []
        # The 1.6 singular field is GONE, not null — a consumer still
        # reading it should break loudly, not read None forever.
        assert "pending_approval_detail" not in rows[0]

    def test_dashboard_pending_approval_details_merge_judge_verdict(self, app_client):
        """When _pending_approval is set on a ws's UI, /dashboard
        embeds one detail entry per live cycle with merged items +
        judge_verdict so coord live-bulk callers can render inline
        approve/deny buttons."""
        client, mgr = app_client
        client.post("/v1/api/workstreams/new", json={"name": "a"}, headers=_auth("user-a"))
        ws_id = next(iter(mgr.list_all())).id
        ui = mgr.get(ws_id).ui
        ui._pending_approval = {
            "type": "approve_request",
            "cycle_id": "cyc-1",
            "items": [
                {
                    "call_id": "c-1",
                    "header": "bash",
                    "preview": "$ ls",
                    "func_name": "bash",
                    "approval_label": "bash",
                    "needs_approval": True,
                }
            ],
            "judge_pending": False,
        }
        ui._llm_verdicts["c-1"] = {
            "recommendation": "deny",
            "risk_level": "crit",
            "confidence": 0.93,
            "tier": "llm",
        }
        resp = client.get("/v1/api/dashboard", headers=_auth("user-a"))
        assert resp.status_code == 200
        row = next(w for w in resp.json()["workstreams"] if w["ws_id"] == ws_id)
        details = row["pending_approval_details"]
        assert len(details) == 1
        detail = details[0]
        assert detail["cycle_id"] == "cyc-1"
        assert detail["call_id"] == "c-1"
        assert detail["judge_pending"] is False
        item = detail["items"][0]
        assert item["func_name"] == "bash"
        assert item["judge_verdict"]["recommendation"] == "deny"
        assert item["judge_verdict"]["risk_level"] == "crit"


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

    def test_enriched_fields_in_response(self, app_client):
        """Saved-list rows carry the enrichment fields, incl. the
        Python-computed context_ratio (latest usage prompt_tokens / model
        context window)."""
        from turnstone.core.storage import get_storage

        client, _mgr = app_client
        storage = get_storage()
        assert storage is not None
        _register_ws(storage, "rich-ws", "alice")
        storage.save_message("rich-ws", "user", "do a thing")
        storage.save_workstream_config("rich-ws", {"model_alias": "m1", "skill": "news"})
        storage.record_usage_event("ev-rich", ws_id="rich-ws", prompt_tokens=500)
        storage.create_model_definition(
            "def-rich", alias="m1", model="m1-model", context_window=1000
        )

        resp = client.get("/v1/api/workstreams/saved", headers=_auth("alice"))
        assert resp.status_code == 200
        row = next(r for r in resp.json()["workstreams"] if r["ws_id"] == "rich-ws")
        assert row["model_alias"] == "m1"
        assert row["launch_skill"] == "news"
        assert row["context_tokens"] == 500
        assert row["context_ratio"] == 0.5  # 500 / 1000
        assert row["child_count"] == 0

    def test_context_ratio_zero_when_window_unknown(self, app_client):
        """A model_alias absent from model_definitions (e.g. config.toml-only)
        leaves context_window NULL → context_ratio degrades to 0.0 instead of
        erroring on the division."""
        from turnstone.core.storage import get_storage

        client, _mgr = app_client
        storage = get_storage()
        assert storage is not None
        _register_ws(storage, "no-window-ws", "alice")
        storage.save_message("no-window-ws", "user", "hi")
        storage.save_workstream_config("no-window-ws", {"model_alias": "toml-only"})
        storage.record_usage_event("ev-now", ws_id="no-window-ws", prompt_tokens=500)

        resp = client.get("/v1/api/workstreams/saved", headers=_auth("alice"))
        assert resp.status_code == 200
        row = next(r for r in resp.json()["workstreams"] if r["ws_id"] == "no-window-ws")
        assert row["context_tokens"] == 500
        assert row["context_ratio"] == 0.0
        assert row["model_alias"] == "toml-only"

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
            "/v1/api/workstreams/ws-victim/events",
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


class TestInteractiveCancelLifted:
    """HTTP-level coverage for the post-lift interactive ``cancel``
    handler at ``POST /v1/api/workstreams/{ws_id}/cancel``. The lifted
    ``make_cancel_handler`` body is shared with coord. Pre-lift
    ``cancel_generation`` was untested at the HTTP layer; coord
    exercised the lifted body via ``test_coordinator_endpoints.py``.
    This class adds the missing interactive-side parity."""

    def _create_ws(self, client) -> str:
        resp = client.post(
            "/v1/api/workstreams/new",
            json={"name": "cancel-target"},
            headers=_auth("user-1"),
        )
        assert resp.status_code == 200
        return resp.json()["ws_id"]

    def test_cancel_returns_dropped_shape(self, app_client):
        """Always-include shape: response carries ``dropped`` (the
        forensic snapshot) regardless of whether anything was running."""
        client, _mgr = app_client
        ws_id = self._create_ws(client)
        resp = client.post(
            f"/v1/api/workstreams/{ws_id}/cancel",
            json={},
            headers=_auth("user-1"),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "dropped" in body
        assert body["dropped"]["was_running"] is False

    def test_cancel_force_clears_worker_thread_and_running_flag(self, app_client):
        """Force-cancel parity with coord: clears ``worker_thread`` AND
        ``_worker_running`` so a follow-up send doesn't route through
        ``enqueue()`` to the abandoned worker's queue (bug-2 from the
        cancel-lift /review). Mirrors
        ``test_cancel_force_flag_abandons_worker_thread_and_emits_stream_end``
        on the coord side."""
        client, mgr = app_client
        ws_id = self._create_ws(client)
        ws = mgr.get(ws_id)
        assert ws is not None
        # Simulate an in-flight worker the lifted cancel needs to
        # abandon. The fake session's cancel() is a no-op, so the
        # cancel flag side-effect doesn't matter — what matters is
        # the (worker_thread, _worker_running) pair after force-cancel.
        ws._worker_running = True
        ws.worker_thread = threading.Thread(target=lambda: None, daemon=True)

        resp = client.post(
            f"/v1/api/workstreams/{ws_id}/cancel",
            json={"force": True},
            headers=_auth("user-1"),
        )
        assert resp.status_code == 200
        # Both fields cleared together — invariant from session_worker
        # ("readers gating on either flag see a coherent
        # (worker_thread, _worker_running) pair").
        assert ws.worker_thread is None
        assert ws._worker_running is False

    def test_cancel_returns_400_when_session_missing(self, app_client):
        """Parity with coord: a placeholder workstream (session=None)
        gets a 400 ``"No session"`` rather than a silent no-op 200.
        Pre-lift interactive already returned 400 here; the lift
        preserves the behaviour and propagates it to coord."""
        client, mgr = app_client
        ws_id = self._create_ws(client)
        ws = mgr.get(ws_id)
        assert ws is not None
        ws.session = None  # force the build-failed shape

        resp = client.post(
            f"/v1/api/workstreams/{ws_id}/cancel",
            json={},
            headers=_auth("user-1"),
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "No session"


from tests._replay_helpers import make_replay_mocks as _make_interactive_replay_mocks  # noqa: E402


class TestInteractiveEventsLifted:
    """Unit + HTTP coverage for the lifted ``events`` SSE handler.

    Substantive coverage targets the ``_interactive_events_replay``
    callback (the kind-specific initial-replay generator the lifted
    body iterates before the live loop) and the legacy URL shim.
    The live SSE loop itself (``ws_closed`` exit + ``is_disconnected``
    check) is hard to assert against ``TestClient`` because each
    event arrives as a separate ``data:`` line and the stream runs
    forever; the loop is the same shape used by every other lifted
    SSE-shaped path (cancel / close / open / send), so a regression
    in the loop body would surface across many test files. Live-loop
    smoke coverage is a deferred follow-up tracked in
    ``1.5.0-stable-handoff.md``'s "Risk flags for the next session"
    section.
    """

    def test_events_replay_yields_connected_first(self):
        """Pre-lift ``events_sse`` yielded a ``connected`` event
        first (model + skip_permissions). The lifted callback
        preserves the order so client SSE handlers that key on
        the connected event for state setup keep working."""
        from turnstone.server import _interactive_events_replay

        ws, ui, request = _make_interactive_replay_mocks()
        out = list(_interactive_events_replay(ws, ui, request))
        assert out[0]["type"] == "connected"
        assert out[0]["model"] == "gpt-5"
        assert out[0]["model_alias"] == "default"
        assert out[0]["skip_permissions"] is False

    def test_events_replay_includes_status_only_when_last_usage_present(self):
        """The ``status`` event populates the per-tab token-usage
        bar on resume. Skipped when ``session._last_usage`` is None
        (a freshly-created workstream that hasn't completed a turn)."""
        from turnstone.server import _interactive_events_replay

        ws, ui, request = _make_interactive_replay_mocks()
        out = list(_interactive_events_replay(ws, ui, request))
        assert "status" not in {ev["type"] for ev in out}

    def test_events_replay_yields_pending_approval_then_verdicts(self):
        """When an approval is pending, the order is approval +
        cached verdicts (so the client renders the prompt and then
        the LLM-judge intent verdicts that fired during it). Pre-lift
        ordering preserved."""
        from turnstone.server import _interactive_events_replay

        ws, ui, request = _make_interactive_replay_mocks(
            _pending_approval={"type": "approve_request", "items": []},
            _llm_verdicts={"v1": {"verdict_id": "v1", "tier": "judge"}},
        )

        out = list(_interactive_events_replay(ws, ui, request))
        types = [ev["type"] for ev in out]
        # The approve_request, then the intent_verdict.
        approve_idx = types.index("approve_request")
        verdict_idx = types.index("intent_verdict")
        assert approve_idx < verdict_idx

    def test_events_replay_skips_when_session_missing(self):
        """Defensive: a placeholder workstream whose session is
        ``None`` (close-then-reopen race) yields an empty replay
        rather than NPE'ing on ``session.model``. The lifted body
        already 409s for missing UI; this guards the rare case
        where UI exists but session was detached."""
        from turnstone.server import _interactive_events_replay

        ws = MagicMock()
        ws.session = None
        ui = MagicMock()
        request = MagicMock()
        out = list(_interactive_events_replay(ws, ui, request))
        assert out == []

    def test_events_replay_omits_conversation_history(self):
        """PR A: conversation history is no longer replayed over SSE.
        The frontend fetches it via ``GET /history`` (REST) on page
        load and re-fetches on ``clear_ui``; the replay must not yield a
        ``history`` event (which previously shipped a multi-MB message
        list on every (re)connect)."""
        from turnstone.server import _interactive_events_replay

        ws, ui, request = _make_interactive_replay_mocks(
            _pending_approval={"type": "approve_request", "items": []},
        )
        out = list(_interactive_events_replay(ws, ui, request))
        assert "history" not in {ev["type"] for ev in out}

    def test_events_path_keyed_url_resolves_to_404_for_unknown_ws(self, app_client):
        """``GET /v1/api/workstreams/{ws_id}/events`` returns 404 for an
        unknown ws_id. Pre-1.5 the same intent was tested against
        ``GET /api/events?ws_id=...`` via the legacy query-keyed
        adapter; that URL family was removed in 1.5 along with the
        adapter."""
        client, _mgr = app_client
        resp = client.get(
            "/v1/api/workstreams/does-not-exist/events",
            headers=_auth("user-1"),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /v1/api/command — /compact worker dispatch (progress-events fix)
# ---------------------------------------------------------------------------


class TestCompactCommandDispatch:
    """Manual /compact runs on the workstream's worker slot: the event loop
    stays free to stream the compaction progress events, and a concurrent
    send takes the queue path instead of racing the history swap."""

    def _create_ws(self, client) -> str:
        resp = client.post(
            "/v1/api/workstreams/new",
            json={"name": "compact-me"},
            headers=_auth("user-1"),
        )
        assert resp.status_code == 200
        return resp.json()["ws_id"]

    def test_compact_dispatches_to_worker(self, app_client):
        client, mgr = app_client
        ws_id = self._create_ws(client)
        resp = client.post(
            "/v1/api/command",
            json={"command": "/compact", "ws_id": ws_id},
            headers=_auth("user-1"),
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

        ws = mgr.get(ws_id)
        assert ws is not None
        # The compaction runs on the spawned worker — join it (the runner
        # clears _worker_running before the thread exits, so the join
        # subsumes the flag).
        ws.worker_thread.join(timeout=5)
        assert not ws.worker_thread.is_alive()
        assert ws.session.compacts == 1
        assert ws._worker_running is False
        # The slot was classified as a command window (what the /send
        # route's park keys on; stale after exit is harmless — every
        # reader conjoins _worker_running).
        assert ws.worker_kind == "command"
        # Exit seam: stranded-text backstop flush only — no drain, no
        # answering send (sends during the window park in the /send route
        # and dispatch as their own workers afterwards).
        assert ws.session.queued_flushes == 1
        assert ws.session.sends == []
        # The worker wrapped the run in busy/idle state transitions.
        assert ws.ui.states == ["thinking", "idle"]

    def test_send_during_compact_window_parks_then_dispatches_fully(self, app_client):
        """A /send during a manual /compact PARKS and then runs as an
        ordinary full-fidelity send — it must never enter the interjection
        queue, whose 2000-char cap silently truncated pasted logs/code and
        whose cross-user guard locked second participants out for the
        whole compaction."""
        client, mgr = app_client
        ws_id = self._create_ws(client)
        ws = mgr.get(ws_id)
        assert ws is not None
        gate = threading.Event()
        ws.session.compact_gate = gate
        resp = client.post(
            "/v1/api/command",
            json={"command": "/compact", "ws_id": ws_id},
            headers=_auth("user-1"),
        )
        assert resp.json() == {"status": "ok"}
        big = "x" * 5000  # over the interjection cap — must survive intact
        send_result: dict = {}

        def _send() -> None:
            r = client.post(
                f"/v1/api/workstreams/{ws_id}/send",
                json={"message": big},
                headers=_auth("user-1"),
            )
            send_result["status"] = r.status_code
            send_result["body"] = r.json()

        sender = threading.Thread(target=_send, daemon=True)
        sender.start()
        # The send is parked: give it time to have taken the park path,
        # then prove nothing was dispatched or queued yet.
        for _ in range(50):
            if ws.session.queue_calls or ws.session.sends:
                break
            time.sleep(0.02)
        assert ws.session.sends == []
        assert ws.session.queue_calls == []  # the queue is unreachable
        gate.set()  # compaction finishes; the park releases
        sender.join(timeout=10)
        assert not sender.is_alive()
        assert send_result["status"] == 200
        assert send_result["body"]["status"] == "ok"
        # Full fidelity: the exact 5000-char text, via a normal send.
        assert [s[0] for s in ws.session.sends] == [big]
        assert ws.session.queue_calls == []

    def test_cancelled_compact_flushes_queue_without_answering(self, app_client):
        """A user-stopped compaction must not auto-run a turn they may no
        longer want — queued text lands in the transcript via the flush
        drain instead (the cancel-seam precedent)."""
        from turnstone.core.session import GenerationCancelled

        client, mgr = app_client
        ws_id = self._create_ws(client)
        ws = mgr.get(ws_id)
        assert ws is not None
        ws.session.compact_raises = GenerationCancelled()
        ws.session.queued_text = "queued mid-compact"
        resp = client.post(
            "/v1/api/command",
            json={"command": "/compact", "ws_id": ws_id},
            headers=_auth("user-1"),
        )
        assert resp.status_code == 200
        ws.worker_thread.join(timeout=5)
        assert ws.session.compacts == 1
        assert ws.session.queued_flushes == 1
        assert ws.session.sends == []
        assert ws.ui.states == ["thinking", "idle"]

    def test_compact_refused_while_worker_running(self, app_client):
        client, mgr = app_client
        ws_id = self._create_ws(client)
        ws = mgr.get(ws_id)
        assert ws is not None
        with ws._lock:
            ws._worker_running = True  # a turn is in flight
        try:
            resp = client.post(
                "/v1/api/command",
                json={"command": "/compact", "ws_id": ws_id},
                headers=_auth("user-1"),
            )
        finally:
            with ws._lock:
                ws._worker_running = False
        assert resp.status_code == 409  # loud refusal for status-code-only callers
        body = resp.json()
        assert body["status"] == "busy"
        assert "busy" in body["error"].lower()
        # Never ran inline, never queued a phantom compaction.
        assert ws.session.compacts == 0
        assert ws.session.commands == []

    def test_non_compact_commands_complete_before_response(self, app_client):
        """Quick commands dispatch through the same worker slot (mutual
        exclusion vs sends / a running compaction / each other) but the
        endpoint awaits completion, preserving the synchronous contract:
        handle_command has finished by the time the response returns."""
        client, mgr = app_client
        ws_id = self._create_ws(client)
        resp = client.post(
            "/v1/api/command",
            json={"command": "/skill", "ws_id": ws_id},
            headers=_auth("user-1"),
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        ws = mgr.get(ws_id)
        # handle_command completed before the response (the done-Event
        # gates it); join before asserting the slot state (the runner
        # clears _worker_running before the thread exits, so the join
        # subsumes the flag; without it this assert races the worker's
        # last steps).
        assert ws.session.commands == ["/skill"]
        ws.worker_thread.join(timeout=5)
        assert not ws.worker_thread.is_alive()
        assert ws._worker_running is False
        # No exit seam work at all for quick commands: no drain, no flush,
        # no follow-up send, and no state chatter (they never left idle).
        assert ws.session.queued_flushes == 0
        assert ws.session.sends == []
        assert ws.ui.states == []

    def test_parked_send_delivers_attachments_after_release(self, app_client, monkeypatch):
        """Attachments are peek-resolved BEFORE the park (the bytes ride the
        request closure), so a send parked through a long compaction still
        delivers them on dispatch — the pre-park refusal
        (attachments_busy) must never apply to a command window."""
        client, mgr = app_client
        ws_id = self._create_ws(client)
        ws = mgr.get(ws_id)
        assert ws is not None

        def fake_resolve(requested_ids, _ws_id, _user_id):
            assert list(requested_ids) == ["a1"]
            return (["fake-attachment-bytes"], ["a1"], [])

        monkeypatch.setattr("turnstone.core.attachments.resolve_staged_attachments", fake_resolve)
        gate = threading.Event()
        ws.session.compact_gate = gate
        client.post(
            "/v1/api/command",
            json={"command": "/compact", "ws_id": ws_id},
            headers=_auth("user-1"),
        )
        send_result: dict = {}

        def _send() -> None:
            r = client.post(
                f"/v1/api/workstreams/{ws_id}/send",
                json={"message": "with attachment", "attachment_ids": ["a1"]},
                headers=_auth("user-1"),
            )
            send_result["body"] = r.json()

        sender = threading.Thread(target=_send, daemon=True)
        sender.start()
        time.sleep(0.3)
        assert ws.session.sends == []  # still parked
        gate.set()
        sender.join(timeout=10)
        assert not sender.is_alive()
        assert send_result["body"]["status"] == "ok"
        assert send_result["body"]["attached_ids"] == ["a1"]
        text, attachments, _sid = ws.session.sends[0]
        assert text == "with attachment"
        assert attachments == ["fake-attachment-bytes"]
        assert ws.session.queue_calls == []

    def test_send_during_quick_command_window_parks(self, app_client):
        """The park applies to EVERY command window, not just /compact — a
        send racing a quick command dispatches after the window with full
        fidelity instead of entering the interjection queue."""
        client, mgr = app_client
        ws_id = self._create_ws(client)
        ws = mgr.get(ws_id)
        assert ws is not None
        gate = threading.Event()
        ws.session.command_gate = gate
        cmd_result: dict = {}

        def _cmd() -> None:
            r = client.post(
                "/v1/api/command",
                json={"command": "/skill", "ws_id": ws_id},
                headers=_auth("user-1"),
            )
            cmd_result["body"] = r.json()

        runner = threading.Thread(target=_cmd, daemon=True)
        runner.start()
        # Wait until the command worker actually holds the slot.
        for _ in range(100):
            if ws._worker_running:
                break
            time.sleep(0.02)
        assert ws._worker_running
        assert ws.worker_kind == "command"
        send_result: dict = {}

        def _send() -> None:
            r = client.post(
                f"/v1/api/workstreams/{ws_id}/send",
                json={"message": "mid-command send"},
                headers=_auth("user-1"),
            )
            send_result["body"] = r.json()

        sender = threading.Thread(target=_send, daemon=True)
        sender.start()
        time.sleep(0.3)  # long enough for a queue-path regression to show
        assert ws.session.queue_calls == []
        assert ws.session.sends == []
        gate.set()
        runner.join(timeout=10)
        sender.join(timeout=10)
        assert not sender.is_alive()
        assert cmd_result["body"] == {"status": "ok"}
        assert send_result["body"]["status"] == "ok"
        assert [s[0] for s in ws.session.sends] == ["mid-command send"]
        assert ws.session.queue_calls == []

    def test_clear_ui_rides_the_worker_not_the_endpoint(self, app_client):
        """The clear_ui follow-up runs on the worker after handle_command —
        parked after the endpoint's 60s done-wait it was silently skipped
        for any command that outlived the backstop, leaving every pane
        rendering a transcript the server no longer holds."""
        client, mgr = app_client
        ws_id = self._create_ws(client)
        resp = client.post(
            "/v1/api/command",
            json={"command": "/clear", "ws_id": ws_id},
            headers=_auth("user-1"),
        )
        assert resp.status_code == 200
        ws = mgr.get(ws_id)
        ws.worker_thread.join(timeout=5)
        assert {"type": "clear_ui"} in ws.ui._enqueued

    def _run_abandoned_command(self, client, mgr, ws_id, command="/clear", raises=None):
        """Dispatch a gated command, force-abandon its worker mid-run, then
        release the gate so the abandoned thread finishes late.  Returns
        (endpoint response body, the abandoned worker thread)."""
        ws = mgr.get(ws_id)
        gate = threading.Event()
        ws.session.command_gate = gate
        ws.session.command_raises = raises
        result: dict = {}

        def _cmd() -> None:
            r = client.post(
                "/v1/api/command",
                json={"command": command, "ws_id": ws_id},
                headers=_auth("user-1"),
            )
            result["body"] = r.json()

        runner = threading.Thread(target=_cmd, daemon=True)
        runner.start()
        for _ in range(100):
            if ws._worker_running:
                break
            time.sleep(0.02)
        assert ws._worker_running
        worker = ws.worker_thread
        # The cancel handler's force path shape: abandon the worker.
        with ws._lock:
            ws.worker_thread = None
            ws._worker_running = False
        gate.set()  # the wedged command unwedges LATE
        worker.join(timeout=5)
        runner.join(timeout=10)
        assert not worker.is_alive() and not runner.is_alive()
        return result["body"], worker

    def test_abandoned_command_worker_fires_no_followups(self, app_client):
        """A force-cancelled wedged command that unwedges minutes later
        must not fire clear_ui (every pane would wipe its transcript
        mid-successor-turn) — the owner guard every sibling worker closure
        applies.  done still fires, so the endpoint never hangs."""
        client, mgr = app_client
        ws_id = self._create_ws(client)
        ws = mgr.get(ws_id)
        body, _ = self._run_abandoned_command(client, mgr, ws_id, command="/clear")
        assert not any(e.get("type") == "clear_ui" for e in ws.ui._enqueued)
        # handle_command genuinely completed, so the (unhung) endpoint's
        # answer reflects that.
        assert body["status"] == "ok"

    def test_abandoned_command_worker_swallows_late_error(self, app_client):
        """The except arm carries the same guard: a stray late 'Command
        error:' from an abandoned worker must not land mid-successor-turn."""
        client, mgr = app_client
        ws_id = self._create_ws(client)
        ws = mgr.get(ws_id)
        self._run_abandoned_command(
            client, mgr, ws_id, command="/skill", raises=RuntimeError("late boom")
        )
        assert ws.ui.errors == []

    def test_exit_command_emits_ended_info_and_never_answers(self, app_client):
        """should_exit commands shut the session down — the worker emits
        the ended notice and never launches an answering turn (sends
        during the window park in the /send route and belong to whatever
        follows the shutdown, not to this worker)."""
        client, mgr = app_client
        ws_id = self._create_ws(client)
        ws = mgr.get(ws_id)
        assert ws is not None
        ws.session.exit_commands = {"/exit"}
        resp = client.post(
            "/v1/api/command",
            json={"command": "/exit", "ws_id": ws_id},
            headers=_auth("user-1"),
        )
        assert resp.status_code == 200
        ws.worker_thread.join(timeout=5)
        assert ws.session.sends == []
        assert any("Session ended" in m for m in ws.ui.infos)

    def test_non_compact_command_refused_while_worker_running(self, app_client):
        """The old inline path was serialized by the event loop itself; the
        worker-slot dispatch restores that mutual exclusion with an explicit
        busy answer — /clear can no longer interleave with a live turn or a
        running compaction."""
        client, mgr = app_client
        ws_id = self._create_ws(client)
        ws = mgr.get(ws_id)
        assert ws is not None
        with ws._lock:
            ws._worker_running = True  # a turn / compaction is in flight
        try:
            resp = client.post(
                "/v1/api/command",
                json={"command": "/clear", "ws_id": ws_id},
                headers=_auth("user-1"),
            )
        finally:
            with ws._lock:
                ws._worker_running = False
        assert resp.status_code == 409
        assert resp.json()["status"] == "busy"
        assert ws.session.commands == []
