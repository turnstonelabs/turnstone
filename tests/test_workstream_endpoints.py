"""Tests for workstream management endpoints added in PRs #314-#315."""

from __future__ import annotations

import queue
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

from turnstone.core.auth import AuthResult
from turnstone.core.session_routes import (
    SessionEndpointConfig,
    make_open_handler,
)
from turnstone.core.storage._sqlite import SQLiteBackend
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
    return TestClient(app)


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
        storage.register_workstream("ws-abc", "node-1", name="test")
        r = delete_client.post("/v1/api/workstreams/ws-abc/delete")
        assert r.status_code == 200
        assert r.json()["deleted"] == "ws-abc"

    def test_delete_not_found(self, delete_client):
        r = delete_client.post("/v1/api/workstreams/nonexistent/delete")
        assert r.status_code == 404
        assert "not found" in r.json()["error"].lower()

    def test_delete_error_redacted(self, delete_client, storage):
        """500 response should not leak exception internals."""
        storage.register_workstream("ws-abc", "node-1", name="test", user_id="test-user")
        with patch(
            "turnstone.core.memory.delete_workstream",
            side_effect=RuntimeError("secret internal detail"),
        ):
            r = delete_client.post("/v1/api/workstreams/ws-abc/delete")
        assert r.status_code == 500
        assert "Delete failed" in r.json()["error"]
        assert "secret" not in r.json()["error"]


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
