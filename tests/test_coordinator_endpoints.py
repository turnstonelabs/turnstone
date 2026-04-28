"""Tests for the coordinator HTTP endpoints.

Builds a minimal Starlette app wiring only the coordinator routes and
an auth-injector middleware.  Verifies the permission gate, 503
remediation when coord_mgr / model alias is missing, ownership
enforcement, and lazy rehydration on GET /{ws_id}. Also exercises
the lifted ``approve`` and ``close`` handlers from
``turnstone.core.session_routes`` wired through the coord
``SessionEndpointConfig`` — same code path the live console uses.
"""

from __future__ import annotations

from typing import cast
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
    _build_mgr_with_factory,
    _fake_registry,
    _FakeConfigStore,
)
from turnstone.console.coordinator_ui import ConsoleCoordinatorUI
from turnstone.console.server import (
    _audit_cancel_coordinator,
    _audit_close_coordinator,
    _audit_coordinator_create,
    _coord_create_build_kwargs,
    _coord_create_post_install,
    _coord_create_validate_request,
    _coord_saved_loaded_lookup,
    _require_admin_coordinator,
    _require_coord_mgr,
    cluster_ws_detail,
    coordinator_children,
    coordinator_tasks,
)
from turnstone.core.attachments import (
    classify_text_attachment as _coord_test_classify_text,
)
from turnstone.core.attachments import (
    sniff_image_mime as _coord_test_sniff_image,
)
from turnstone.core.attachments import (
    upload_lock as _coord_test_upload_lock,
)
from turnstone.core.auth import AuthResult
from turnstone.core.session_routes import (
    AttachmentUploadHelpers,
    SessionEndpointConfig,
    make_approve_handler,
    make_attachment_handlers,
    make_cancel_handler,
    make_close_handler,
    make_create_handler,
    make_dequeue_handler,
    make_detail_handler,
    make_history_handler,
    make_list_handler,
    make_open_handler,
    make_saved_handler,
    make_send_handler,
)
from turnstone.core.storage._sqlite import SQLiteBackend
from turnstone.core.workstream import WorkstreamKind

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _coord_attach_owner(request, ws_id, mgr):
    """Coord attachment owner resolver mirroring production wiring.

    Kind-strict — coord attachments can only be accessed for
    workstreams currently held by ``coord_mgr``; no storage fallback
    so cross-kind ws_ids 404 instead of leaking through storage.
    """
    from starlette.responses import JSONResponse

    from turnstone.core.web_helpers import auth_user_id

    ws = mgr.get(ws_id)
    if ws is None:
        return "", JSONResponse({"error": "coordinator not found"}, status_code=404)
    return ws.user_id or auth_user_id(request), None


# Per-kind config the lifted handler factories capture by closure.
# Mirrors the production console wiring so tests exercise the same
# code path as the live server.
_coord_endpoint_config = SessionEndpointConfig(
    permission_gate=_require_admin_coordinator,
    manager_lookup=_require_coord_mgr,
    tenant_check=None,
    not_found_label="coordinator not found",
    audit_action_prefix="coordinator",
    supports_attachments=True,
    attachment_owner_resolver=_coord_attach_owner,
    attachment_helpers=AttachmentUploadHelpers(
        sniff_image_mime=_coord_test_sniff_image,
        classify_text_attachment=_coord_test_classify_text,
        upload_lock=_coord_test_upload_lock,
    ),
    spawn_metrics=None,
    emit_message_queued=True,
    create_supports_attachments=True,
    create_supports_user_id_override=False,
    create_validate_request=_coord_create_validate_request,
    create_build_kwargs=_coord_create_build_kwargs,
    create_post_install=_coord_create_post_install,
    list_resolve_titles=None,
    list_kind=WorkstreamKind.COORDINATOR,
    saved_state_filter="closed",
    saved_loaded_lookup=_coord_saved_loaded_lookup,
)


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
    coord_attachments = make_attachment_handlers(_coord_endpoint_config)
    coord_create_handler = make_create_handler(
        _coord_endpoint_config, audit_emit=_audit_coordinator_create
    )
    app = Starlette(
        routes=[
            Route(
                "/v1/api/workstreams/new",
                coord_create_handler,
                methods=["POST"],
            ),
            Route(
                "/v1/api/workstreams",
                make_list_handler(_coord_endpoint_config),
                methods=["GET"],
            ),
            # Literal path before the /{ws_id} routes below so Starlette
            # matches "saved" as the literal, not as a ws_id.
            Route(
                "/v1/api/workstreams/saved",
                make_saved_handler(_coord_endpoint_config),
                methods=["GET"],
            ),
            Route(
                "/v1/api/workstreams/{ws_id}/send",
                make_send_handler(_coord_endpoint_config),
                methods=["POST"],
            ),
            Route(
                "/v1/api/workstreams/{ws_id}/send",
                make_dequeue_handler(_coord_endpoint_config),
                methods=["DELETE"],
            ),
            Route(
                "/v1/api/workstreams/{ws_id}/approve",
                make_approve_handler(_coord_endpoint_config),
                methods=["POST"],
            ),
            Route(
                "/v1/api/workstreams/{ws_id}/cancel",
                make_cancel_handler(
                    _coord_endpoint_config,
                    audit_emit=_audit_cancel_coordinator,
                ),
                methods=["POST"],
            ),
            Route(
                "/v1/api/workstreams/{ws_id}/close",
                make_close_handler(
                    _coord_endpoint_config,
                    audit_emit=_audit_close_coordinator,
                    supports_close_reason=False,
                ),
                methods=["POST"],
            ),
            Route(
                "/v1/api/workstreams/{ws_id}/history",
                make_history_handler(_coord_endpoint_config),
                methods=["GET"],
            ),
            Route(
                "/v1/api/workstreams/{ws_id}/open",
                make_open_handler(_coord_endpoint_config),
                methods=["POST"],
            ),
            Route(
                "/v1/api/workstreams/{ws_id}/children",
                coordinator_children,
                methods=["GET"],
            ),
            Route(
                "/v1/api/workstreams/{ws_id}/tasks",
                coordinator_tasks,
                methods=["GET"],
            ),
            Route(
                "/v1/api/workstreams/{ws_id}/attachments",
                coord_attachments.upload,
                methods=["POST"],
            ),
            Route(
                "/v1/api/workstreams/{ws_id}/attachments",
                coord_attachments.list,
                methods=["GET"],
            ),
            Route(
                "/v1/api/workstreams/{ws_id}/attachments/{attachment_id}/content",
                coord_attachments.get_content,
                methods=["GET"],
            ),
            Route(
                "/v1/api/workstreams/{ws_id}/attachments/{attachment_id}",
                coord_attachments.delete,
                methods=["DELETE"],
            ),
            Route(
                "/v1/api/workstreams/{ws_id}",
                make_detail_handler(_coord_endpoint_config),
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
    app.state.coord_adapter = coord_mgr._adapter if coord_mgr is not None else None
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
        "/v1/api/workstreams/new",
        json={"name": "c1"},
        headers={"X-Test-User": "user-1", "X-Test-Perms": "read"},
    )
    assert resp.status_code == 403


def test_no_auth_returns_401(storage):
    """No AuthResult in request.state → 401 (require_permission semantics)."""
    mgr = _build_mgr(storage)
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post("/v1/api/workstreams/new", json={"name": "c1"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 503 remediation when coordinator subsystem isn't configured
# ---------------------------------------------------------------------------


def test_missing_coord_mgr_returns_503(storage):
    client = _make_client(storage, coord_mgr=None)
    resp = client.post(
        "/v1/api/workstreams/new",
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
        "/v1/api/workstreams/new",
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
        "/v1/api/workstreams/new",
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
        "/v1/api/workstreams/new",
        json={},
        headers={"X-Test-User": "user-1", "X-Test-Perms": "admin.coordinator"},
    )
    assert resp.status_code == 503
    assert "does not resolve" in resp.json()["error"]


# ---------------------------------------------------------------------------
# Happy path — create + list + send + close
# ---------------------------------------------------------------------------


_COORD_HEADERS = {"X-Test-User": "user-1", "X-Test-Perms": "admin.coordinator"}


def test_active_list_row_shape_includes_unified_fields(storage):
    """Stage 2 list-verb-lift parity regression — coord active-list row
    carries the always-include fields (ws_id, name, state, kind,
    parent_ws_id, user_id) that the lifted ``make_list_handler``
    produces on every kind. SDK consumers don't have to branch on
    kind to read any of these. Pre-lift coord returned a smaller
    row ({ws_id, name, state, user_id}) under a different top-level
    key (``coordinators`` vs ``workstreams``)."""
    mgr = _build_mgr(storage)
    mgr.create(user_id="u1", name="lifted-coord")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get("/v1/api/workstreams", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    # Top-level key converged on `workstreams`.
    assert "workstreams" in body
    assert "coordinators" not in body
    rows = body["workstreams"]
    assert len(rows) == 1
    row = rows[0]
    # Always-include row shape — coord populates kind=COORDINATOR
    # and parent_ws_id=None (coordinators have no parent today).
    assert set(row.keys()) == {
        "ws_id",
        "name",
        "state",
        "kind",
        "parent_ws_id",
        "user_id",
    }
    assert row["name"] == "lifted-coord"
    assert row["kind"] == "coordinator"
    assert row["parent_ws_id"] is None
    assert row["user_id"] == "u1"


def test_create_returns_ws_id_and_records_audit(storage):
    mgr = _build_mgr(storage)
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        "/v1/api/workstreams/new",
        json={"name": "my-coord"},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ws_id"]
    assert "my-coord" in body["name"]
    # Always-include parity fields land on coord post-`create` lift —
    # SDK consumers don't have to branch on kind to read them.
    assert body["resumed"] is False
    assert body["message_count"] == 0
    assert body["attachment_ids"] == []
    # Audit row recorded on storage.
    events = storage.list_audit_events(user_id="user-1", limit=10)
    actions = [e["action"] for e in events]
    assert "coordinator.create" in actions


def _capture_factory_pair():
    """Return ``(factory, captured)`` — factory records model_alias +
    judge_model into the captured dict on every call so tests can assert
    the per-call override threading."""
    captured: dict = {}

    def _factory(ui, model_alias=None, ws_id=None, **kw):  # type: ignore[no-untyped-def]
        captured["model_alias"] = model_alias
        captured["judge_model"] = kw.get("judge_model")
        return MagicMock()

    return _factory, captured


def test_create_forwards_model_and_judge_model_overrides(storage):
    """Per-call ``model`` + ``judge_model`` body fields land on the
    coord session factory (mirrors interactive's create surface)."""
    factory, captured = _capture_factory_pair()
    mgr = _build_mgr_with_factory(storage, factory)
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        "/v1/api/workstreams/new",
        json={
            "name": "tuned-coord",
            "model": "gpt-5",
            "judge_model": "gpt-5-mini",
        },
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert captured == {"model_alias": "gpt-5", "judge_model": "gpt-5-mini"}


def test_create_empty_model_fields_collapse_to_none(storage):
    """Empty-string ``model`` / ``judge_model`` body fields don't override
    the ConfigStore default — they collapse to ``None`` so the factory
    falls back to ``coordinator.model_alias`` / ``judge.model``."""
    factory, captured = _capture_factory_pair()
    mgr = _build_mgr_with_factory(storage, factory)
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        "/v1/api/workstreams/new",
        json={"name": "default-coord", "model": "  ", "judge_model": ""},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert captured == {"model_alias": None, "judge_model": None}


def test_create_503_factory_misconfig_message_is_sanitised(storage):
    """503 response from a factory ``ValueError`` strips ASCII control
    chars and caps the echoed alias text — defence-in-depth for the
    user-controlled ``body["model"]`` reflection surface. Operators
    keep the actionable message in the log; clients see a clean
    bounded string."""

    def _factory_raises(ui, model_alias=None, ws_id=None, **kw):  # type: ignore[no-untyped-def]
        # Simulate the registry's actual exception shape, plus a
        # control char + a long-tail attacker payload.
        raise ValueError("Unknown model alias: \x00\x07attack\x1b[31m" + ("A" * 1000))

    mgr = _build_mgr_with_factory(storage, _factory_raises)
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        "/v1/api/workstreams/new",
        json={"name": "c"},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 503
    err = resp.json()["error"]
    # Cap enforced (hard-capped at _FACTORY_MISCONFIG_MAX_LEN total —
    # the truncation reserves one codepoint for the ellipsis).
    assert len(err) <= 200
    assert "\x00" not in err
    assert "\x1b" not in err
    assert "Unknown model alias" in err
    assert err.endswith("…")


def test_create_non_string_model_fields_collapse_to_none(storage):
    """Non-string ``model`` / ``judge_model`` body fields (e.g. a hostile
    dict / list / int) collapse to ``None`` rather than reaching
    ``.strip()`` and crashing into the lifted handler's generic 500
    path. Defense-in-depth — the auth gate already requires
    ``admin.coordinator``."""
    factory, captured = _capture_factory_pair()
    mgr = _build_mgr_with_factory(storage, factory)
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        "/v1/api/workstreams/new",
        json={
            "name": "hostile-body",
            "model": {"url": "http://evil"},
            "judge_model": [1, 2, 3],
        },
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert captured == {"model_alias": None, "judge_model": None}


_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xfc\xcf"
    b"\xc0\xc0\xc0\x00\x00\x00\x05\x00\x01\xa5\xf6E@\x00\x00\x00\x00IEND"
    b"\xaeB`\x82"
)


def test_create_with_multipart_attachments_saves_pending_rows(storage):
    """§ Post-P3 reckoning item #1 regression — coord gains create-time
    attachments. Multipart create with a magic-byte-valid PNG saves
    a pending attachment row scoped to the new coord ws_id.

    No ``initial_message`` here, so attachments stay pending and a
    subsequent ``/send`` picks them up via the standard
    send-with-attachments path."""
    from turnstone.core.memory import list_pending_attachments

    mgr = _build_mgr(storage)
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())

    # Inject the test storage backend as the global singleton so
    # ``save_attachment`` / ``list_pending_attachments`` (which both
    # go through ``turnstone.core.memory`` → ``get_storage()``)
    # resolve onto our SQLiteBackend instead of the real one.
    import turnstone.core.storage._registry as _reg

    _old_storage = _reg._storage
    _reg._storage = storage
    try:
        resp = client.post(
            "/v1/api/workstreams/new",
            data={"meta": '{"name": "with-image"}'},
            files={"file": ("img.png", _PNG_1X1, "image/png")},
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        ws_id = body["ws_id"]
        assert ws_id
        assert len(body["attachment_ids"]) == 1
        pending = list_pending_attachments(ws_id, "user-1")
        assert len(pending) == 1
        assert pending[0]["kind"] == "image"
    finally:
        _reg._storage = _old_storage


def test_create_with_multipart_attachments_and_initial_message_reserves(storage):
    """Coord initial-message + create-time-attachments coordination —
    when ``initial_message`` is provided alongside multipart uploads,
    the attachments are reserved onto the dispatched first turn (via
    :meth:`CoordinatorAdapter.send` with ``send_id``), so they're
    not still pending after the create returns. Closes the parity
    gap with interactive's create-with-attachments+initial_message
    worker thread."""
    from turnstone.core.memory import get_attachments, list_pending_attachments

    mgr = _build_mgr(storage)
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())

    import turnstone.core.storage._registry as _reg

    _old_storage = _reg._storage
    _reg._storage = storage
    try:
        resp = client.post(
            "/v1/api/workstreams/new",
            data={"meta": '{"name": "with-init", "initial_message": "look at this image"}'},
            files={"file": ("img.png", _PNG_1X1, "image/png")},
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        ws_id = body["ws_id"]
        attachment_ids = body["attachment_ids"]
        assert len(attachment_ids) == 1
        # Reserved (not pending): the row's ``reserved_for_msg_id``
        # carries the send_id token that ``CoordinatorAdapter.send``
        # generated; the worker's first ``ChatSession.send(...,
        # send_id=...)`` call will consume it on dequeue.
        pending = list_pending_attachments(ws_id, "user-1")
        assert pending == [], "attachments should be reserved, not pending"
        rows = get_attachments(attachment_ids)
        assert len(rows) == 1
        assert rows[0]["reserved_for_msg_id"], "attachment must carry a send_id reservation token"
    finally:
        _reg._storage = _old_storage


def test_create_attachment_failure_no_phantom_create_close_pair(storage):
    """Regression for the ``defer_emit_created`` follow-up — when a
    multipart create rolls back on attachment validation failure,
    the cluster collector sees zero events: no phantom
    ``ws_created``, no orphan ``ws_closed``. Pre-fix, coord's
    ``mgr.create`` fired ``emit_created`` synchronously and the
    rollback path called ``mgr.close`` which fired ``emit_closed``,
    surfacing a quick create→close pair on the cluster events
    stream."""
    mgr = _build_mgr(storage)
    # Pull the collector MagicMock out of the adapter so we can
    # introspect call counts after the failed create.
    coord_collector = mgr._adapter._collector  # type: ignore[attr-defined]
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())

    import turnstone.core.storage._registry as _reg

    _old_storage = _reg._storage
    _reg._storage = storage
    try:
        # Empty file body → ``validate_and_save_uploaded_files``
        # returns the 400 "Empty file" path which the lifted body
        # surfaces directly. The rollback path runs after.
        resp = client.post(
            "/v1/api/workstreams/new",
            data={"meta": '{"name": "rollback-target"}'},
            files={"file": ("blank.bin", b"", "application/octet-stream")},
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 400, resp.text
        # Manager is empty — slot was discarded.
        assert mgr.count == 0
        # The collector saw NEITHER create nor close events for any
        # ws_id. ``MagicMock`` records every method call, so an empty
        # list of calls to these specific methods is the regression
        # check.
        assert coord_collector.emit_console_ws_created.call_count == 0
        assert coord_collector.emit_console_ws_closed.call_count == 0
    finally:
        _reg._storage = _old_storage


def test_create_rejects_disabled_skill(storage):
    """Coord parity gain — disabled skills now rejected at the lift,
    matching interactive's pre-lift behaviour. Pre-lift coord silently
    let disabled skills through."""
    import turnstone.core.storage._registry as _reg

    _old_storage = _reg._storage
    _reg._storage = storage
    try:
        # Save a disabled skill row so ``get_skill_by_name`` resolves
        # but the lifted body's enabled check rejects it. Mirrors the
        # ``_create_template`` helper in ``tests/test_skills.py``;
        # inlined here so this regression test stays self-contained.
        storage.create_prompt_template(
            template_id="sk-disabled",
            name="dormant-skill",
            category="general",
            content="dormant",
            variables="[]",
            is_default=False,
            org_id="",
            created_by="test",
            origin="manual",
            mcp_server="",
            readonly=False,
            description="",
            tags="[]",
            source_url="",
            version="1.0.0",
            author="",
            activation="named",
            token_estimate=0,
            model="",
            auto_approve=False,
            temperature=None,
            reasoning_effort="",
            max_tokens=None,
            token_budget=0,
            agent_max_turns=None,
            notify_on_complete="{}",
            enabled=False,  # the gate under test
            allowed_tools="[]",
            priority=0,
        )
        mgr = _build_mgr(storage)
        client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
        resp = client.post(
            "/v1/api/workstreams/new",
            json={"name": "x", "skill": "dormant-skill"},
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 400
        assert "dormant-skill" in resp.json()["error"]
    finally:
        _reg._storage = _old_storage


def test_list_returns_cluster_wide(storage):
    # Trusted-team visibility: any caller with admin.coordinator sees
    # every active coordinator regardless of owner.  ``user_id`` stays
    # on the response as metadata.
    mgr = _build_mgr(storage)
    mgr.create(user_id="user-1", name="mine")
    mgr.create(user_id="user-2", name="theirs")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get("/v1/api/workstreams", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    names = {c["name"] for c in body["workstreams"]}
    assert names == {"mine", "theirs"}


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


def test_saved_returns_cluster_wide(saved_storage):
    # Trusted-team visibility: every ``admin.coordinator`` caller sees
    # every closed coordinator.
    storage = saved_storage
    mgr = _build_mgr(storage)
    a = _seed_closed_coord_with_history(mgr, storage, user_id="user-1", name="a")
    b = _seed_closed_coord_with_history(mgr, storage, user_id="user-2", name="b")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get("/v1/api/workstreams/saved", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    assert {c["ws_id"] for c in resp.json()["workstreams"]} == {a, b}


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
    resp = client.get("/v1/api/workstreams/saved", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    saved_ids = {c["ws_id"] for c in resp.json()["workstreams"]}
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
    resp = client.get("/v1/api/workstreams/saved", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    saved_ids = {c["ws_id"] for c in resp.json()["workstreams"]}
    assert saved_ids == {closed_id}


def test_send_any_admin_coordinator_caller_can_send(storage):
    # Trusted-team model: send is gated on admin.coordinator scope,
    # not on per-row ownership.  Any caller with the scope can post
    # to any coordinator.
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="owner", name="theirs")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{ws.id}/send",
        json={"message": "hi"},
        headers={"X-Test-User": "stranger", "X-Test-Perms": "admin.coordinator"},
    )
    assert resp.status_code == 200


def test_send_requires_message(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{ws.id}/send",
        json={},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Dequeue (DELETE /send) — lifted handler wired onto the coord endpoint
# config. Pins the URL/scope contract so a regression in the route table
# (wrong endpoint_config, wrong method, missing gate) trips a test.
# ---------------------------------------------------------------------------


def test_dequeue_removes_queued_coord_message(storage):
    """POST /send while busy → queued; DELETE /send with msg_id → removed."""
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    # _build_mgr's session_factory returns a MagicMock posing as a
    # ChatSession so the test can stub queue_message / dequeue_message.
    session = cast("MagicMock", ws.session)

    # Force the queue path by marking the worker as already running.
    # The lifted send handler hands off to ``session_worker.send`` which
    # picks ``enqueue`` over ``run`` when ``ws._worker_running`` is True.
    ws._worker_running = True
    session.queue_message.return_value = ("hi", "important", "msg-abc")
    session.dequeue_message.return_value = True

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    send_resp = client.post(
        f"/v1/api/workstreams/{ws.id}/send",
        json={"message": "hi"},
        headers=_COORD_HEADERS,
    )
    assert send_resp.status_code == 200, send_resp.text
    body = send_resp.json()
    assert body["status"] == "queued"
    assert body["msg_id"] == "msg-abc"

    dequeue_resp = client.request(
        "DELETE",
        f"/v1/api/workstreams/{ws.id}/send",
        json={"msg_id": "msg-abc"},
        headers=_COORD_HEADERS,
    )
    assert dequeue_resp.status_code == 200, dequeue_resp.text
    assert dequeue_resp.json() == {"status": "removed"}
    session.dequeue_message.assert_called_with("msg-abc")


def test_dequeue_unknown_msg_id_returns_not_found(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    cast("MagicMock", ws.session).dequeue_message.return_value = False
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.request(
        "DELETE",
        f"/v1/api/workstreams/{ws.id}/send",
        json={"msg_id": "missing"},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "not_found"}


def test_dequeue_requires_msg_id(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.request(
        "DELETE",
        f"/v1/api/workstreams/{ws.id}/send",
        json={},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 400


def test_dequeue_unknown_ws_returns_404(storage):
    mgr = _build_mgr(storage)
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.request(
        "DELETE",
        "/v1/api/workstreams/0000000000000000000000000000000000000000000000000000000000000000/send",
        json={"msg_id": "anything"},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 404
    assert "coordinator not found" in resp.json()["error"]


def test_dequeue_requires_admin_coordinator_scope(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.request(
        "DELETE",
        f"/v1/api/workstreams/{ws.id}/send",
        json={"msg_id": "msg-abc"},
        headers={"X-Test-User": "user-1", "X-Test-Perms": "read"},
    )
    assert resp.status_code == 403


def test_close_records_audit_and_removes_from_mgr(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(f"/v1/api/workstreams/{ws.id}/close", headers=_COORD_HEADERS)
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
        f"/v1/api/workstreams/{ws.id}/approve",
        json={"approved": True, "always": True, "call_id": "c-1"},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    assert ws.ui._approval_event.is_set()
    assert ws.ui._approval_result == (True, None)
    assert "spawn_workstream" in ws.ui.auto_approve_tools


def _seed_pending(ws, *call_ids: str) -> None:
    ws.ui._pending_approval = {
        "type": "approve_request",
        "items": [
            {
                "call_id": cid,
                "func_name": "spawn_workstream",
                "approval_label": "spawn_workstream",
                "needs_approval": True,
            }
            for cid in call_ids
        ],
    }
    ws.ui._approval_event.clear()


def test_approve_409_on_stale_call_id(storage):
    """Body call_id doesn't match any pending item → 409 with the
    current primary call_id so the UI can re-render against the
    new round."""
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    _seed_pending(ws, "c-current")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{ws.id}/approve",
        json={"approved": True, "call_id": "c-stale"},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "stale call_id"
    assert body["current_call_id"] == "c-current"
    # Approval event must NOT be set — no resolve_approval ran.
    assert not ws.ui._approval_event.is_set()


def test_approve_409_when_no_pending_and_call_id_sent(storage):
    """Body sends a call_id but the UI has no pending approval —
    409 with current_call_id=None so the UI knows to clear the row."""
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    # No _pending_approval seeded → ui._pending_approval is None.
    ws.ui._approval_event.clear()
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{ws.id}/approve",
        json={"approved": True, "call_id": "c-anything"},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "no pending approval"
    assert body["current_call_id"] is None
    assert not ws.ui._approval_event.is_set()


def test_approve_no_call_id_preserves_backward_compat(storage):
    """Existing clients (CLI, channel adapters) that omit call_id
    must still resolve approvals — the guard only kicks in when
    call_id is present in the body."""
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    _seed_pending(ws, "c-1")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{ws.id}/approve",
        json={"approved": True},  # no call_id
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    assert ws.ui._approval_event.is_set()


def test_approve_no_call_id_no_pending_falls_through(storage):
    """Legacy clients (no call_id) calling approve when pending is
    None hit the existing resolve_approval no-op path — the new
    guard must not change that behavior. Regression guard for the
    legacy code path that the call_id check intentionally bypasses."""
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    ws.ui._approval_event.clear()
    # No _pending_approval seeded.
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{ws.id}/approve",
        json={"approved": True},  # no call_id
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    assert ws.ui._approval_event.is_set()


def test_approve_call_id_matches_any_item_in_multi_envelope(storage):
    """N>1 envelope: any one of the items' call_ids in the body
    is sufficient — server resolves the whole envelope per the
    one-boolean semantics of resolve_approval."""
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    _seed_pending(ws, "c-1", "c-2", "c-3")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{ws.id}/approve",
        json={"approved": True, "call_id": "c-2"},  # middle item
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    assert ws.ui._approval_event.is_set()


# ---------------------------------------------------------------------------
# Lazy rehydration
# ---------------------------------------------------------------------------


def test_detail_triggers_lazy_rehydration(storage):
    """GET /v1/api/workstreams/{ws_id} finds the row and rehydrates it."""
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
    resp = client.get("/v1/api/workstreams/persisted-coord", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    # Now tracked in the manager.
    assert mgr.get("persisted-coord") is not None


def test_detail_any_admin_coordinator_caller_can_open(storage):
    # Trusted-team model: any ``admin.coordinator`` caller can rehydrate
    # any persisted coordinator regardless of owner.  ``user_id`` stays
    # on the response as metadata.
    mgr = _build_mgr(storage)
    storage.register_workstream("coord-x", kind="coordinator", user_id="owner")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(
        "/v1/api/workstreams/coord-x",
        headers={"X-Test-User": "stranger", "X-Test-Perms": "admin.coordinator"},
    )
    assert resp.status_code == 200
    assert resp.json()["user_id"] == "owner"


def test_detail_404_when_kind_interactive(storage):
    """Non-coordinator rows aren't reachable via the coordinator endpoint."""
    mgr = _build_mgr(storage)
    storage.register_workstream("ws-int", kind="interactive", user_id="user-1")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get("/v1/api/workstreams/ws-int", headers=_COORD_HEADERS)
    assert resp.status_code == 404


def test_detail_503_on_session_factory_misconfig(storage):
    """``ValueError`` from ``mgr.open`` (e.g. a model alias that no longer
    resolves) surfaces as 503 with the factory's remediation text — not
    a correlation-id'd 500. Mirrors the open verb lift's contract so
    operators can fix the misconfig without grepping logs."""
    from unittest.mock import patch

    mgr = _build_mgr(storage)
    storage.register_workstream("misconfig-coord", kind="coordinator", user_id="user-1")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    with patch.object(mgr, "open", side_effect=ValueError("no such model alias")):
        resp = client.get("/v1/api/workstreams/misconfig-coord", headers=_COORD_HEADERS)
    assert resp.status_code == 503
    assert "no such model alias" in resp.json()["error"]


def test_detail_correlation_id_on_unexpected_rehydrate_failure(storage):
    """Bare ``Exception`` from ``mgr.open`` (build_session / resume failure
    with no documented spec) surfaces as a correlation-id'd 500 with the
    per-kind noun in the user-facing message — not the raw exception
    text. Mirrors :func:`make_open_handler`'s leak-prevention contract."""
    from unittest.mock import patch

    mgr = _build_mgr(storage)
    storage.register_workstream("broken-coord", kind="coordinator", user_id="user-1")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    with patch.object(mgr, "open", side_effect=RuntimeError("internal stack frame leak")):
        resp = client.get("/v1/api/workstreams/broken-coord", headers=_COORD_HEADERS)
    assert resp.status_code == 500
    body = resp.json()
    assert "internal stack frame leak" not in body["error"]
    assert "correlation_id=" in body["error"]
    # Per-kind noun via cfg.audit_action_prefix — coord wires "coordinator".
    assert "coordinator" in body["error"]


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def test_history_returns_messages(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    # Seed a message in storage.
    storage.save_message(ws.id, "user", "hello")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(f"/v1/api/workstreams/{ws.id}/history", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ws_id"] == ws.id
    assert any(m.get("role") == "user" and m.get("content") == "hello" for m in body["messages"])


def test_history_any_admin_coordinator_caller_can_read(storage):
    # Trusted-team visibility: history is readable by any
    # ``admin.coordinator`` caller.
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="owner")
    storage.save_message(ws.id, "user", "hello")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(
        f"/v1/api/workstreams/{ws.id}/history",
        headers={"X-Test-User": "stranger", "X-Test-Perms": "admin.coordinator"},
    )
    assert resp.status_code == 200
    assert resp.json()["ws_id"] == ws.id


def test_history_serves_storage_only_workstream(storage):
    """Persisted-but-not-loaded coordinators (closed / evicted) are still
    readable via /history without rehydrating. Mirrors the pre-lift
    ``_resolve_coordinator_or_404`` ladder: storage-row + kind check
    is sufficient when ``mgr.get`` returns None."""
    mgr = _build_mgr(storage)
    storage.register_workstream("storage-only-coord", kind="coordinator", user_id="user-1")
    storage.save_message("storage-only-coord", "user", "from cold storage")
    # Confirm precondition: row is in storage, NOT in the manager's pool.
    assert mgr.get("storage-only-coord") is None

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(
        "/v1/api/workstreams/storage-only-coord/history",
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    assert any(m.get("content") == "from cold storage" for m in resp.json()["messages"])
    # History does NOT rehydrate (unlike detail) — pool stays cold.
    assert mgr.get("storage-only-coord") is None


def test_history_404_when_kind_interactive(storage):
    """Cross-kind isolation on the storage fallback path: an interactive
    ws_id that exists in storage 404s on the coord history endpoint
    (mirrors :func:`test_detail_404_when_kind_interactive`). The lifted
    factory uses ``cfg.list_kind`` for the kind check; pre-lift coord
    used :func:`_resolve_coordinator_or_404`'s explicit kind compare."""
    mgr = _build_mgr(storage)
    storage.register_workstream("ws-int", kind="interactive", user_id="user-1")
    storage.save_message("ws-int", "user", "interactive content")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get("/v1/api/workstreams/ws-int/history", headers=_COORD_HEADERS)
    assert resp.status_code == 404
    assert "interactive content" not in resp.text


def test_history_swallows_load_messages_exception_returns_empty(storage):
    """``storage.load_messages`` raising mid-call (transient DB outage,
    corrupted row, etc.) must not 5xx the page-load handshake — coord
    pre-lift logged at debug and returned 200 with ``messages == []``.
    The lifted body preserves that contract on both kinds; pin it
    explicitly so a future reader doesn't remove the bare-except as
    dead code."""
    from unittest.mock import patch

    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())

    with patch.object(storage, "load_messages", side_effect=RuntimeError("db gone")):
        resp = client.get(f"/v1/api/workstreams/{ws.id}/history", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ws_id"] == ws.id
    assert body["messages"] == []


def test_history_clamps_limit_query_param(storage):
    """Pre-lift coord clamped ``?limit=`` to [1, 500]. The lifted factory
    preserves the same bounds. Out-of-range / unparseable values
    fall back to defaults instead of erroring — coord's page-load
    handshake should never 4xx on a malformed limit param."""
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    # Seed enough messages to exercise the upper bound. SQLite's INSERT
    # is fast enough that 6 inserts in a tight loop is fine.
    for i in range(6):
        storage.save_message(ws.id, "user", f"msg-{i}")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    base = f"/v1/api/workstreams/{ws.id}/history"

    # No limit → default 100 (returns all 6).
    resp = client.get(base, headers=_COORD_HEADERS)
    assert resp.status_code == 200
    assert len(resp.json()["messages"]) == 6

    # limit=2 → only 2 messages returned (storage owns the row
    # ordering contract; the factory just threads ``limit`` through).
    resp = client.get(base, params={"limit": 2}, headers=_COORD_HEADERS)
    assert resp.status_code == 200
    assert len(resp.json()["messages"]) == 2

    # Negative / zero → clamp to 1 (factory: ``max(1, min(limit, 500))``).
    resp = client.get(base, params={"limit": 0}, headers=_COORD_HEADERS)
    assert resp.status_code == 200
    assert len(resp.json()["messages"]) == 1

    # Garbage → falls back to default 100 (still 200, returns all 6).
    resp = client.get(base, params={"limit": "garbage"}, headers=_COORD_HEADERS)
    assert resp.status_code == 200
    assert len(resp.json()["messages"]) == 6

    # Above-cap → clamps to 500 (page-load handshake never 4xx's).
    resp = client.get(base, params={"limit": 999}, headers=_COORD_HEADERS)
    assert resp.status_code == 200
    # We only have 6 messages but the response is still 200 — the cap
    # is enforced on the SQL LIMIT, not on the row count.
    assert len(resp.json()["messages"]) == 6


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
    resp = client.post(f"/v1/api/workstreams/{ws.id}/cancel", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    assert ws.ui._approval_event.is_set()


def test_cancel_response_always_includes_dropped_key(storage):
    """Always-include shape parity: post-P3 verb lift, coord cancel
    returns ``{"status": "ok", "dropped": {}}`` regardless of whether
    a forensics callable is wired (coord wires ``None``). SDK
    consumers don't have to branch on kind to read ``dropped``."""
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(f"/v1/api/workstreams/{ws.id}/cancel", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["dropped"] == {}


def test_cancel_force_flag_abandons_worker_thread_and_emits_stream_end(storage):
    """Parity gain from the verb lift: coord now honours the ``force``
    flag the same way interactive does (pre-lift coord ignored it).
    Stuck-worker recovery: the abandoned thread is cleared, an
    ``idle`` state-change is dispatched via the UI, and a
    ``stream_end`` event lands on the listener queue so the dashboard
    recovers without waiting for the daemon thread to exit."""
    import threading

    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    assert isinstance(ws.ui, ConsoleCoordinatorUI)
    # Simulate an in-flight worker the UI is still waiting on.
    ws._worker_running = True
    ws.worker_thread = threading.Thread(target=lambda: None, daemon=True)
    listener = ws.ui._register_listener()

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{ws.id}/cancel",
        headers=_COORD_HEADERS,
        json={"force": True},
    )
    assert resp.status_code == 200
    # Worker thread reference cleared so a follow-up send doesn't
    # think a generation is still in flight.
    assert ws.worker_thread is None
    # ``stream_end`` lands on the listener so SDK consumers bail out
    # of the SSE loop instead of hanging on the daemon thread.
    seen = []
    while not listener.empty():
        seen.append(listener.get_nowait().get("type"))
    assert "stream_end" in seen


def test_cancel_returns_400_when_session_missing(storage):
    """Pre-lift coord called ``coord_mgr.cancel`` which silently
    no-op'd on a placeholder workstream (session=None). The lifted
    body 400s for parity with interactive's existing
    ``"No session"`` branch — surfaces the build-failure state to
    the operator instead of swallowing it."""
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    # Force the placeholder/build-failed shape.
    ws.session = None
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(f"/v1/api/workstreams/{ws.id}/cancel", headers=_COORD_HEADERS)
    assert resp.status_code == 400


def test_cancel_swallows_forensics_exception(storage):
    """``cancel_forensics`` is observational — a bug in the snapshot
    callable must NOT block the actual cancel. The lifted body wraps
    the call in try/except + log.debug and falls through with an
    empty ``dropped`` dict so the response shape stays consistent."""
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from turnstone.core.session_routes import (
        SessionEndpointConfig,
        make_cancel_handler,
    )

    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")

    def _raises_forensics(session, ui, *, was_running):  # noqa: ARG001
        raise RuntimeError("forensics blew up")

    cfg = SessionEndpointConfig(
        permission_gate=_require_admin_coordinator,
        manager_lookup=lambda r: (mgr, None),
        tenant_check=None,
        not_found_label="coordinator not found",
        audit_action_prefix="coordinator",
        cancel_forensics=_raises_forensics,
    )
    handler = make_cancel_handler(cfg)
    app = Starlette(routes=[Route("/v1/api/workstreams/{ws_id}/cancel", handler, methods=["POST"])])
    app.add_middleware(_AuthMiddleware)
    client = TestClient(app)
    resp = client.post(f"/v1/api/workstreams/{ws.id}/cancel", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["dropped"] == {}


def test_cancel_swallows_audit_emit_exception(storage):
    """``audit_emit`` failures are demoted to ``log.warning`` and the
    cancel returns 200. Mirrors the same pattern in ``make_close_handler``
    — telemetry bugs must not block recovery verbs."""
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from turnstone.core.session_routes import (
        SessionEndpointConfig,
        make_cancel_handler,
    )

    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")

    def _raises_audit(request, ws_id, ws_obj, force):  # noqa: ARG001
        raise RuntimeError("audit blew up")

    cfg = SessionEndpointConfig(
        permission_gate=_require_admin_coordinator,
        manager_lookup=lambda r: (mgr, None),
        tenant_check=None,
        not_found_label="coordinator not found",
        audit_action_prefix="coordinator",
    )
    handler = make_cancel_handler(cfg, audit_emit=_raises_audit)
    app = Starlette(routes=[Route("/v1/api/workstreams/{ws_id}/cancel", handler, methods=["POST"])])
    app.add_middleware(_AuthMiddleware)
    client = TestClient(app)
    resp = client.post(f"/v1/api/workstreams/{ws.id}/cancel", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_cancel_idle_workstream_does_not_broadcast_approval_resolved(storage):
    """Bug-1 from /review: the unconditional resolve_approval lift
    leaked a stale ``approval_resolved`` SSE event on every idle
    cancel. The fix gates the call on ``_pending_approval is not None``
    so listeners don't see a phantom resolution. Asserts the
    ``approval_resolved`` event does NOT land on the listener queue
    when no approval is pending."""
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    assert isinstance(ws.ui, ConsoleCoordinatorUI)
    assert ws.ui._pending_approval is None  # idle baseline
    listener = ws.ui._register_listener()

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(f"/v1/api/workstreams/{ws.id}/cancel", headers=_COORD_HEADERS)
    assert resp.status_code == 200

    seen_types = []
    while not listener.empty():
        seen_types.append(listener.get_nowait().get("type"))
    assert "approval_resolved" not in seen_types


# ---------------------------------------------------------------------------
# Events (SSE replay shape)
# ---------------------------------------------------------------------------


from tests._replay_helpers import make_replay_mocks as _make_coord_replay_mocks  # noqa: E402


def test_coord_events_replay_yields_connected_first():
    """Pre-status-bar coord replay only re-injected pending_approval +
    pending_plan_review. Post-status-bar parity with interactive
    yields ``connected`` first so the dashboard's status bar populates
    the model cell before any history arrives — mirrors the
    interactive replay (turnstone/server.py:_interactive_events_replay)."""
    from turnstone.console.server import _coord_events_replay

    ws, ui, request = _make_coord_replay_mocks()
    out = list(_coord_events_replay(ws, ui, request))
    assert out[0]["type"] == "connected"
    assert out[0]["model"] == "gpt-5"
    assert out[0]["model_alias"] == "default"
    assert out[0]["skip_permissions"] is False


def test_coord_events_replay_includes_status_only_when_last_usage_present():
    """The ``status`` event populates the per-tab token-usage bar on
    resume. Skipped when ``session._last_usage`` is None (a freshly-
    created coordinator that hasn't completed a turn) — matches
    interactive behaviour."""
    from turnstone.console.server import _coord_events_replay

    ws, ui, request = _make_coord_replay_mocks()
    out = list(_coord_events_replay(ws, ui, request))
    assert "status" not in {ev["type"] for ev in out}


def test_coord_events_replay_status_payload_shape():
    """When ``last_usage`` exists, the replayed ``status`` event carries
    every field the dashboard's updateStatusBar() reads — same shape
    SessionUI.on_status emits live."""
    from turnstone.console.server import _coord_events_replay

    ws, ui, request = _make_coord_replay_mocks(
        last_usage={
            "prompt_tokens": 40000,
            "completion_tokens": 6310,
            "cache_creation_tokens": 100,
            "cache_read_tokens": 50,
        },
        _ws_turn_tool_calls=3,
        _ws_messages=7,
    )
    out = list(_coord_events_replay(ws, ui, request))
    status = next(ev for ev in out if ev["type"] == "status")
    assert status["prompt_tokens"] == 40000
    assert status["completion_tokens"] == 6310
    assert status["total_tokens"] == 46310
    assert status["context_window"] == 100000
    assert status["pct"] == round(46310 / 100000 * 100, 1)
    assert status["effort"] == "medium"
    assert status["tool_calls_this_turn"] == 3
    assert status["turn_count"] == 7
    assert status["cache_creation_tokens"] == 100
    assert status["cache_read_tokens"] == 50


def test_coord_events_replay_skips_session_block_when_no_session():
    """Detached session (close-then-reopen race) — replay skips the
    connected/status preamble and falls through to the pending-prompt
    branches.  Mirrors interactive's defensive guard at
    turnstone/server.py:665."""
    from turnstone.console.server import _coord_events_replay

    ws, ui, _request = _make_coord_replay_mocks()
    ws.session = None
    out = list(_coord_events_replay(ws, ui, MagicMock()))
    assert out == []


def test_coord_events_replay_yields_pending_approval_then_pending_plan():
    """The lifted coord ``events_replay`` callback yields, after the
    connected preamble: pending approval (if any) + pending plan
    review (if any). Pre-lift coord pushed both onto the listener
    queue via ``put_nowait``; the lift restructures as a generator
    the lifted body iterates and yields as ``data:`` lines, but the
    payload identity is preserved. Pure-read — never mutates ``ui``."""
    from turnstone.console.server import _coord_events_replay

    ws, ui, request = _make_coord_replay_mocks(
        _pending_approval={"type": "approve_request", "items": []},
        _pending_plan_review={"type": "plan_review", "content": "..."},
    )

    out = list(_coord_events_replay(ws, ui, request))
    types = [ev["type"] for ev in out]
    # Status preamble is yielded first (no last_usage → no status); the
    # pending-approval / plan ordering then matches the pre-lift body.
    assert types[0] == "connected"
    approve_idx = types.index("approve_request")
    plan_idx = types.index("plan_review")
    assert approve_idx < plan_idx


def test_coord_events_replay_yields_cached_verdicts_after_pending_approval():
    """When the SSE stream reconnects mid-approval, the verdict-cache
    replay must follow the pending_approval re-injection. Without this,
    a refreshing tab sees the approve_request prompt but no judge chip
    until the operator re-invokes the action — intent_verdict is a
    one-shot SSE event with no late-subscriber push. Mirrors the
    interactive replay path."""
    from turnstone.console.server import _coord_events_replay

    ws, ui, request = _make_coord_replay_mocks(
        _pending_approval={
            "type": "approve_request",
            "items": [{"call_id": "c-1"}],
        },
        _llm_verdicts={
            "c-1": {
                "verdict_id": "v-1",
                "call_id": "c-1",
                "recommendation": "deny",
                "risk_level": "high",
            }
        },
    )

    out = list(_coord_events_replay(ws, ui, request))
    types = [ev["type"] for ev in out]
    approve_idx = types.index("approve_request")
    verdict_idx = types.index("intent_verdict")
    assert approve_idx < verdict_idx
    verdict = out[verdict_idx]
    assert verdict["verdict_id"] == "v-1"
    assert verdict["recommendation"] == "deny"


def test_coord_events_replay_skips_verdict_replay_without_pending_approval():
    """Verdict replay rides on top of pending_approval — no prompt,
    no chip. Stale verdicts from a previously-resolved round must
    not surface on a fresh connect."""
    from turnstone.console.server import _coord_events_replay

    ws, ui, request = _make_coord_replay_mocks(
        _llm_verdicts={"old": {"verdict_id": "stale"}},
    )

    out = list(_coord_events_replay(ws, ui, request))
    types = [ev["type"] for ev in out]
    assert "intent_verdict" not in types
    assert "approve_request" not in types
    assert "plan_review" not in types


def test_coord_events_replay_yields_only_connected_when_no_pending():
    """A workstream with a session but no pending approval / plan
    review and no last_usage yields just the ``connected`` preamble.
    The lifted body falls through to the live loop immediately after."""
    from turnstone.console.server import _coord_events_replay

    ws, ui, request = _make_coord_replay_mocks()
    out = list(_coord_events_replay(ws, ui, request))
    assert [ev["type"] for ev in out] == ["connected"]


def test_coord_events_returns_404_on_missing_ws(storage):
    """Cross-kind / missing ws_id surfaces as 404 with
    ``cfg.not_found_label`` ("coordinator not found") via the lifted
    body's ``mgr.get`` None-return contract."""
    mgr = _build_mgr(storage)
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get("/v1/api/workstreams/nonexistent-id/events", headers=_COORD_HEADERS)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Open (explicit rehydration)
# ---------------------------------------------------------------------------


def test_open_returns_already_loaded_when_in_memory(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1", name="live")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(f"/v1/api/workstreams/{ws.id}/open", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ws_id"] == ws.id
    assert body.get("already_loaded") is True


def test_open_any_admin_coordinator_caller_succeeds_in_memory(storage):
    # Trusted-team model: open is gated by admin.coordinator scope
    # only; any authenticated caller can open any in-memory coordinator.
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="owner", name="theirs")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{ws.id}/open",
        headers={"X-Test-User": "stranger", "X-Test-Perms": "admin.coordinator"},
    )
    assert resp.status_code == 200
    assert resp.json().get("already_loaded") is True


def test_open_rehydrates_when_not_in_memory(storage, monkeypatch):
    mgr = _build_mgr(storage)
    rehydrated = MagicMock()
    rehydrated.id = "coord-rehy"
    rehydrated.name = "rehydrated"
    rehydrated.user_id = "user-1"
    monkeypatch.setattr(mgr, "open", MagicMock(return_value=rehydrated))
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post("/v1/api/workstreams/coord-rehy/open", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ws_id"] == "coord-rehy"
    assert body["name"] == "rehydrated"
    assert "already_loaded" not in body
    # SessionManager.open takes a single positional arg now — no
    # per-caller ownership / admin plumbing.
    mgr.open.assert_called_once_with("coord-rehy")


def test_open_returns_404_when_unknown_ws_id(storage, monkeypatch):
    mgr = _build_mgr(storage)
    monkeypatch.setattr(mgr, "open", MagicMock(return_value=None))
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post("/v1/api/workstreams/nonexistent/open", headers=_COORD_HEADERS)
    assert resp.status_code == 404


def test_open_503_on_coord_mgr_unavailable(storage):
    client = _make_client(storage, coord_mgr=None)
    resp = client.post("/v1/api/workstreams/any-ws/open", headers=_COORD_HEADERS)
    assert resp.status_code == 503


def test_open_correlation_id_on_factory_failure(storage, monkeypatch):
    mgr = _build_mgr(storage)
    monkeypatch.setattr(mgr, "open", MagicMock(side_effect=RuntimeError("boom")))
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post("/v1/api/workstreams/bad-ws/open", headers=_COORD_HEADERS)
    assert resp.status_code == 500
    assert "correlation_id=" in resp.json()["error"]


def test_open_503_when_open_raises_value_error(storage, monkeypatch):
    """ValueError from the factory surfaces as 503 with the remediation text."""
    mgr = _build_mgr(storage)
    monkeypatch.setattr(mgr, "open", MagicMock(side_effect=ValueError("coord registry missing")))
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post("/v1/api/workstreams/bad-ws/open", headers=_COORD_HEADERS)
    assert resp.status_code == 503
    assert "registry missing" in resp.json()["error"]


# ---------------------------------------------------------------------------
# GET /v1/api/workstreams/{ws_id}/children — phase 3 tree view backend
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
    resp = client.get(f"/v1/api/workstreams/{ws.id}/children", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"items": [], "truncated": False}


def test_children_returns_interactive_children(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    _seed_child(storage, ws.id, "c" * 32)
    _seed_child(storage, ws.id, "d" * 32, state="running")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(f"/v1/api/workstreams/{ws.id}/children", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    states = {r["state"] for r in body["items"]}
    assert states == {"idle", "running"}
    kinds = {r["kind"] for r in body["items"]}
    assert kinds == {"interactive"}


def test_children_any_admin_coordinator_caller_sees_subtree(storage):
    # Trusted-team visibility: the children subtree is readable by any
    # ``admin.coordinator`` caller.
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="owner")
    _seed_child(storage, ws.id, "a" * 32)
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(
        f"/v1/api/workstreams/{ws.id}/children",
        headers={"X-Test-User": "stranger", "X-Test-Perms": "admin.coordinator"},
    )
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1


def test_children_invalid_ws_id_400(storage):
    mgr = _build_mgr(storage)
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get("/v1/api/workstreams/INVALID-WS-SHOUT/children", headers=_COORD_HEADERS)
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /v1/api/workstreams/{ws_id}/tasks — phase 3 task pane backend
# ---------------------------------------------------------------------------


def test_tasks_empty_envelope_for_new_coordinator(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(f"/v1/api/workstreams/{ws.id}/tasks", headers=_COORD_HEADERS)
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
    resp = client.get(f"/v1/api/workstreams/{ws.id}/tasks", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == envelope


def test_tasks_corrupt_envelope_returns_empty(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    storage.save_workstream_config(ws.id, {"tasks": "NOT-JSON"})
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(f"/v1/api/workstreams/{ws.id}/tasks", headers=_COORD_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"version": 1, "tasks": []}


def test_tasks_any_admin_coordinator_caller_can_read(storage):
    # Trusted-team visibility: any ``admin.coordinator`` caller can
    # read the tasks envelope.
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="owner")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(
        f"/v1/api/workstreams/{ws.id}/tasks",
        headers={"X-Test-User": "stranger", "X-Test-Perms": "admin.coordinator"},
    )
    assert resp.status_code == 200


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


def test_cluster_inspect_any_inspect_caller_sees_detail(storage):
    # Trusted-team visibility: admin.cluster.inspect sees every row.
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="owner")
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.get(
        f"/v1/api/cluster/ws/{ws.id}/detail",
        headers={"X-Test-User": "stranger", "X-Test-Perms": "admin.cluster.inspect"},
    )
    assert resp.status_code == 200
    assert resp.json()["persisted"]["ws_id"] == ws.id


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
                "ws_id": ws_id,
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


def test_cluster_inspect_node_backed_pending_approval_detail_passes_through(storage):
    """Node /dashboard returns pending_approval_detail → live block carries
    it through verbatim. Regression guard for the projection allowlist
    `_CLUSTER_WS_LIVE_KEYS`: dropping the key from the tuple silently
    breaks inline approve/deny buttons on remote-node child rows even
    though coord rows still work via the in-process synthesis branch."""
    mgr = _build_mgr(storage)
    ws_id = "f0" * 16
    _seed_node_workstream(storage, ws_id=ws_id, node_id="node-a")
    detail = {
        "call_id": "c-bash",
        "judge_pending": False,
        "items": [
            {
                "call_id": "c-bash",
                "header": "bash",
                "preview": "$ rm -rf /tmp/x",
                "func_name": "bash",
                "approval_label": "bash",
                "needs_approval": True,
                "error": None,
                "heuristic_verdict": None,
                "judge_verdict": {
                    "recommendation": "deny",
                    "risk_level": "crit",
                    "tier": "llm",
                },
            }
        ],
    }
    payload = {
        "workstreams": [
            {
                "ws_id": ws_id,
                "state": "attention",
                "activity_state": "approval",
                "activity": "awaiting approval",
                "tokens": 100,
                "pending_approval_detail": detail,
            }
        ]
    }
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    _install_collector_with_node(client, "node-a", "http://node-a")
    _install_proxy_client(client, httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    resp = client.get(f"/v1/api/cluster/ws/{ws_id}/detail", headers=_CLUSTER_HEADERS)
    assert resp.status_code == 200
    live = resp.json()["live"]
    assert live["pending_approval"] is True  # derived bool, existing behavior
    assert live["pending_approval_detail"] == detail  # full payload, new behavior


def test_cluster_inspect_node_backed_pending_approval_synthesized(storage):
    """activity_state=='approval' from the node synthesizes pending_approval=True."""
    mgr = _build_mgr(storage)
    ws_id = "cd" * 16
    _seed_node_workstream(storage, ws_id=ws_id, node_id="node-a")
    payload = {
        "workstreams": [
            {
                "ws_id": ws_id,
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
            {"ws_id": "different-" + "x" * 24, "state": "idle"},
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

    # Trusted-team visibility: every caller sees every coordinator.
    for caller in ("alice", "bob", "admin-1"):
        rows = _coordinator_rows(_request_for(caller, frozenset({"read"})))
        assert {r["name"] for r in rows} == {"alice-coord", "bob-coord"}


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


def test_coordinator_rows_persisted_cluster_wide(storage):
    # Trusted-team visibility: every caller sees every persisted row,
    # including rows from other identities and orphan (empty-user_id)
    # rows.  ``user_id`` stays on the response as metadata.
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
    storage.register_workstream(
        "c" * 32,
        node_id="console",
        user_id="",  # orphan / system row
        name="orphan-closed",
        state="closed",
        kind=WorkstreamKind.COORDINATOR,
        parent_ws_id=None,
    )

    for caller, perms in (
        ("alice", frozenset({"read"})),
        ("bob", frozenset({"read"})),
        ("admin-1", frozenset({"read", "admin.users"})),
    ):
        request = _persisted_rows_request(storage, mgr, caller, perms)
        rows = _coordinator_rows(request)
        assert {r["name"] for r in rows} == {"alice-closed", "bob-closed", "orphan-closed"}


# ---------------------------------------------------------------------------
# Stage 2 P1.5 — coord attachment surface parity with interactive
# ---------------------------------------------------------------------------


class TestCoordinatorAttachments:
    """The lifted ``make_attachment_handlers`` factory exposes
    upload / list / get_content / delete on coord workstreams using
    the same kind-agnostic storage layer interactive uses. These
    tests exercise the surface end-to-end via TestClient."""

    def _upload(self, client, ws_id, *, name="hello.md", body=b"hi", mime="text/markdown"):
        files = {"file": (name, body, mime)}
        resp = client.post(
            f"/v1/api/workstreams/{ws_id}/attachments",
            files=files,
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_upload_round_trip_lists_pending(self, storage):
        mgr = _build_mgr(storage)
        ws = mgr.create(user_id="user-1", name="c1")
        client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())

        info = self._upload(client, ws.id, name="note.md", body=b"hello world")
        assert info["filename"] == "note.md"
        assert info["kind"] == "text"
        assert info["size_bytes"] == len(b"hello world")

        listing = client.get(f"/v1/api/workstreams/{ws.id}/attachments", headers=_COORD_HEADERS)
        assert listing.status_code == 200
        ids = [a["attachment_id"] for a in listing.json()["attachments"]]
        assert info["attachment_id"] in ids

    def test_get_content_returns_raw_bytes(self, storage):
        mgr = _build_mgr(storage)
        ws = mgr.create(user_id="user-1", name="c1")
        client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())

        info = self._upload(
            client, ws.id, name="data.json", body=b'{"k":1}', mime="application/json"
        )
        resp = client.get(
            f"/v1/api/workstreams/{ws.id}/attachments/{info['attachment_id']}/content",
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 200
        # Text kinds force text/plain to avoid same-origin HTML/SVG rendering.
        assert resp.headers["content-type"].startswith("text/plain")
        assert resp.content == b'{"k":1}'

    def test_delete_removes_pending(self, storage):
        mgr = _build_mgr(storage)
        ws = mgr.create(user_id="user-1", name="c1")
        client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())

        info = self._upload(client, ws.id)
        resp = client.delete(
            f"/v1/api/workstreams/{ws.id}/attachments/{info['attachment_id']}",
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "deleted"}

        listing = client.get(f"/v1/api/workstreams/{ws.id}/attachments", headers=_COORD_HEADERS)
        ids = [a["attachment_id"] for a in listing.json()["attachments"]]
        assert info["attachment_id"] not in ids

    def test_send_with_attachment_ids_consumes_pending(self, storage):
        """End-to-end: upload an attachment, then ``coord_send`` it. The
        reservation flips ``reserved_for_msg_id`` to the send_id, so the
        attachment is no longer in the pending listing."""
        mgr = _build_mgr(storage)
        ws = mgr.create(user_id="user-1", name="c1")
        client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())

        info = self._upload(client, ws.id)
        resp = client.post(
            f"/v1/api/workstreams/{ws.id}/send",
            json={"message": "hi", "attachment_ids": [info["attachment_id"]]},
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        # Surfacing parity with interactive: response carries the
        # attached / dropped lists even when no drops occurred.
        assert body["attached_ids"] == [info["attachment_id"]]
        assert body["dropped_attachment_ids"] == []

    def test_send_response_includes_attached_ids_field_when_no_attachments(self, storage):
        """The unified response shape always carries ``attached_ids`` /
        ``dropped_attachment_ids`` so SDK consumers don't have to
        branch on whether attachments were involved."""
        mgr = _build_mgr(storage)
        ws = mgr.create(user_id="user-1", name="c1")
        client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())

        resp = client.post(
            f"/v1/api/workstreams/{ws.id}/send",
            json={"message": "no attachments here"},
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["attached_ids"] == []
        assert body["dropped_attachment_ids"] == []

    def test_coord_attachment_endpoints_404_on_interactive_ws_id(self, storage):
        """Security regression: an ``admin.coordinator``-scoped caller
        must NOT be able to read or mutate attachments on
        **interactive** workstreams via the coord attachment surface.
        The kind-strict resolver returns 404 (no storage fallback) so
        a cross-kind ws_id never resolves to its owner."""
        from turnstone.core.workstream import WorkstreamKind

        # Persist an interactive workstream row directly — never loaded
        # into the coord_mgr.
        interactive_ws_id = "i" * 32
        storage.register_workstream(
            interactive_ws_id,
            node_id="some-node",
            user_id="alice",
            name="alice-interactive",
            kind=WorkstreamKind.INTERACTIVE,
            parent_ws_id=None,
        )
        mgr = _build_mgr(storage)
        client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())

        # Upload attempt — must 404, not 200.
        files = {"file": ("note.md", b"sneaky", "text/markdown")}
        resp = client.post(
            f"/v1/api/workstreams/{interactive_ws_id}/attachments",
            files=files,
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 404, resp.text

        # List, get-content, delete — same kind-strict 404 behaviour.
        resp = client.get(
            f"/v1/api/workstreams/{interactive_ws_id}/attachments",
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 404
        resp = client.get(
            f"/v1/api/workstreams/{interactive_ws_id}/attachments/anything/content",
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 404
        resp = client.delete(
            f"/v1/api/workstreams/{interactive_ws_id}/attachments/anything",
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 404
