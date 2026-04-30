"""Tests for workstream management endpoints added in PRs #314-#315."""

from __future__ import annotations

import queue
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

from turnstone.core.auth import AuthResult
from turnstone.core.session_routes import (
    SessionEndpointConfig,
    make_detail_handler,
    make_history_handler,
    make_open_handler,
)
from turnstone.core.storage._sqlite import SQLiteBackend
from turnstone.core.workstream import WorkstreamKind
from turnstone.server import (
    delete_workstream_endpoint,
    list_interface_settings,
    refresh_workstream_title,
    set_workstream_title,
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
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route(
                        "/api/workstreams/{ws_id}/title",
                        set_workstream_title,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/workstreams/{ws_id}/refresh-title",
                        refresh_workstream_title,
                        methods=["POST"],
                    ),
                ],
            ),
        ],
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    mock_mgr = MagicMock()
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


def _build_history_app(
    mock_mgr: Any,
    storage: Any,
    tenant_check: Any = None,
) -> TestClient:
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
    return TestClient(app)


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


class TestBuildHistoryReminderPropagation:
    """``_build_history`` must surface the ``_reminders`` side-channel on
    each entry so a tab reconnecting via ``/history`` renders the same
    metacognitive nudge bubble the originating tab saw via the live
    ``user_reminder`` SSE event.
    """

    def _session_with_messages(self, messages: list[dict]) -> MagicMock:
        session = MagicMock()
        session.messages = messages
        return session

    def test_reminders_sidechannel_surfaces_on_entry(self):
        from turnstone.server import _build_history

        session = self._session_with_messages(
            [
                {
                    "role": "user",
                    "content": "ah no",
                    "_reminders": [{"type": "correction", "text": "watch out"}],
                }
            ]
        )
        history = _build_history(session)
        assert history[0]["content"] == "ah no"
        assert history[0]["reminders"] == [{"type": "correction", "text": "watch out"}]

    def test_no_reminders_key_when_sidechannel_absent(self):
        from turnstone.server import _build_history

        session = self._session_with_messages([{"role": "user", "content": "just a message"}])
        history = _build_history(session)
        assert "reminders" not in history[0]

    def test_no_reminders_key_when_sidechannel_empty(self):
        from turnstone.server import _build_history

        session = self._session_with_messages([{"role": "user", "content": "hi", "_reminders": []}])
        history = _build_history(session)
        assert "reminders" not in history[0]

    def test_multiple_reminders_preserved_in_order(self):
        from turnstone.server import _build_history

        session = self._session_with_messages(
            [
                {
                    "role": "user",
                    "content": "x",
                    "_reminders": [
                        {"type": "denial", "text": "FIRST"},
                        {"type": "correction", "text": "SECOND"},
                    ],
                }
            ]
        )
        history = _build_history(session)
        assert history[0]["reminders"] == [
            {"type": "denial", "text": "FIRST"},
            {"type": "correction", "text": "SECOND"},
        ]

    def test_reminders_coexist_with_attachments(self):
        from turnstone.server import _build_history

        session = self._session_with_messages(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look"},
                        {"type": "image_url", "image_url": {"url": "data:..."}},
                    ],
                    "_reminders": [{"type": "correction", "text": "watch"}],
                }
            ]
        )
        history = _build_history(session)
        assert history[0]["content"] == "look"
        assert history[0]["attachments"] == [{"kind": "image", "filename": "", "mime_type": ""}]
        assert history[0]["reminders"] == [{"type": "correction", "text": "watch"}]

    def test_malformed_reminders_filtered_out(self):
        """Defensive: a non-dict element in the list (corruption / bug)
        is dropped rather than crashing the history serialisation."""
        from turnstone.server import _build_history

        session = self._session_with_messages(
            [
                {
                    "role": "user",
                    "content": "x",
                    "_reminders": [
                        {"type": "correction", "text": "ok"},
                        "not-a-dict",
                        {"type": "denial"},  # missing text
                    ],
                }
            ]
        )
        history = _build_history(session)
        # Non-dicts dropped; missing-text fills with empty string.
        assert history[0]["reminders"] == [
            {"type": "correction", "text": "ok"},
            {"type": "denial", "text": ""},
        ]

    def test_clean_message_passes_through_unchanged(self):
        """No reminders, plain content — _build_history is a no-op for the
        reminder field and ``content`` rides through verbatim."""
        from turnstone.server import _build_history

        session = self._session_with_messages(
            [{"role": "user", "content": "just a normal message"}]
        )
        history = _build_history(session)
        assert history[0]["content"] == "just a normal message"
        assert "reminders" not in history[0]

    def test_assistant_content_with_literal_reminder_tag_unchanged(self):
        """Assistant output may legitimately reference the tag (e.g. when
        the model is explaining the reminder system itself).  No
        transformation should ever apply to assistant content."""
        from turnstone.server import _build_history

        content = "Here is a <system-reminder> tag in assistant output."
        session = self._session_with_messages([{"role": "assistant", "content": content}])
        history = _build_history(session)
        assert history[0]["content"] == content


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
            "pending_approval_detail": None,
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
        loaded_ws.ui.serialize_pending_approval_detail = MagicMock(
            return_value={
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
        )
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = loaded_ws
        client = _build_detail_app(mock_mgr)

        r = client.get(f"/v1/api/workstreams/{ws_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["pending_approval"] is True
        assert body["pending_approval_detail"]["call_id"] == "c-1"
        assert body["pending_approval_detail"]["judge_pending"] is True
        items = body["pending_approval_detail"]["items"]
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
        loaded_ws.ui.serialize_pending_approval_detail = MagicMock(
            side_effect=RuntimeError("verdict object is malformed"),
        )
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = loaded_ws
        client = _build_detail_app(mock_mgr)

        r = client.get(f"/v1/api/workstreams/{ws_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["pending_approval"] is True
        assert body["pending_approval_detail"] is None

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
        assert "pending_approval_detail" not in body
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
        assert body["pending_approval_detail"] is None

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
