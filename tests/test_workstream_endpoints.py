"""Tests for workstream management endpoints added in PRs #314-#315."""

from __future__ import annotations

import queue
import threading
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
from turnstone.core.session import ChatSession
from turnstone.core.storage._sqlite import SQLiteBackend
from turnstone.server import (
    create_workstream,
    delete_workstream_endpoint,
    list_interface_settings,
    open_workstream,
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
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route(
                        "/api/workstreams/{ws_id}/open",
                        open_workstream,
                        methods=["POST"],
                    ),
                ],
            ),
        ],
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    mock_mgr = MagicMock()
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

    def test_delete_error_redacted(self, delete_client):
        """500 response should not leak exception internals."""
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
        storage.register_workstream("ws-abc", "node-1", name="test")
        mock_ws = MagicMock()
        mock_mgr.get.return_value = mock_ws
        r = client.post(
            "/v1/api/workstreams/ws-abc/title",
            json={"title": "New Title"},
        )
        assert r.status_code == 200
        assert r.json()["title"] == "New Title"

    def test_set_title_empty(self, title_client):
        client, _ = title_client
        r = client.post(
            "/v1/api/workstreams/ws-abc/title",
            json={"title": ""},
        )
        assert r.status_code == 400
        assert "required" in r.json()["error"].lower()

    def test_set_title_missing_body(self, title_client):
        client, _ = title_client
        r = client.post(
            "/v1/api/workstreams/ws-abc/title",
            json={},
        )
        assert r.status_code == 400

    def test_set_title_truncation(self, title_client, storage):
        client, mock_mgr = title_client
        storage.register_workstream("ws-abc", "node-1", name="test")
        mock_mgr.get.return_value = MagicMock()
        long_title = "x" * 200
        r = client.post(
            "/v1/api/workstreams/ws-abc/title",
            json={"title": long_title},
        )
        assert r.status_code == 200
        assert len(r.json()["title"]) <= 80

    def test_set_title_alias_conflict(self, title_client, storage):
        client, _ = title_client
        storage.register_workstream("ws-1", "node-1", name="first")
        storage.register_workstream("ws-2", "node-1", name="second")
        storage.set_workstream_alias("ws-1", "taken-name")
        r = client.post(
            "/v1/api/workstreams/ws-2/title",
            json={"title": "taken-name"},
        )
        assert r.status_code == 409


# ===========================================================================
# REFRESH title
# ===========================================================================


class TestRefreshWorkstreamTitle:
    def test_refresh_success(self, title_client):
        client, mock_mgr = title_client
        mock_ws = MagicMock()
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
        client, mock_mgr, gq = open_client
        mock_resolve.return_value = "ws-abc"
        mock_ws = MagicMock()
        mock_ws.id = "ws-abc"
        mock_mgr.get.return_value = mock_ws
        with patch("turnstone.core.memory.get_workstream_display_name", return_value="My WS"):
            r = client.post("/v1/api/workstreams/ws-abc/open")
        assert r.status_code == 200
        assert r.json()["already_loaded"] is True
        assert r.json()["ws_id"] == "ws-abc"

    @patch("turnstone.core.memory.resolve_workstream")
    def test_open_not_found(self, mock_resolve, open_client):
        client, mock_mgr, gq = open_client
        mock_resolve.return_value = None
        r = client.post("/v1/api/workstreams/nonexistent/open")
        assert r.status_code == 404

    @patch("turnstone.core.memory.resolve_workstream")
    def test_open_no_storage_row(self, mock_resolve, open_client, _inject_storage):
        client, mock_mgr, gq = open_client
        mock_resolve.return_value = "ws-abc"
        mock_mgr.get.return_value = None  # not loaded
        # Storage has no row for ws-abc
        r = client.post("/v1/api/workstreams/ws-abc/open")
        assert r.status_code == 404
        assert "storage" in r.json()["error"].lower()


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


# ===========================================================================
# FORK naming — forked workstreams must NOT inherit source title
# ===========================================================================


class TestForkWorkstreamNaming:
    """Regression test: forking a workstream without a custom name must keep
    the auto-generated short-ID name (``ws-XXXX``) rather than inheriting the
    source workstream's display name.

    The bug was an ``else`` branch in ``create_workstream`` that called
    ``get_workstream_display_name(target_id)`` and overwrote ``ws.name``
    with the source workstream's title.
    """

    @pytest.fixture()
    def fork_app(self, tmp_path):
        """Minimal Starlette app with the real ``create_workstream`` handler,
        a real ``WorkstreamManager``, and mocked session factory.

        Returns ``(TestClient, WorkstreamManager, queue.Queue)``.
        """
        import turnstone.core.storage._registry as _reg
        from turnstone.core.workstream import WorkstreamManager

        storage = SQLiteBackend(str(tmp_path / "fork_test.db"))

        old_storage = _reg._storage
        _reg._storage = storage

        def _session_factory(
            ui: Any, model_alias: Any = None, ws_id: Any = None, **kwargs: Any
        ) -> ChatSession:
            return ChatSession(
                client=MagicMock(),
                model=model_alias or "test-model",
                ui=ui,
                instructions=None,
                temperature=0.5,
                max_tokens=4096,
                tool_timeout=30,
                ws_id=ws_id,
                skill=kwargs.get("skill"),
            )

        mgr = WorkstreamManager(_session_factory)

        routes = [
            Mount(
                "/v1",
                routes=[
                    Route(
                        "/api/workstreams/new",
                        create_workstream,
                        methods=["POST"],
                    ),
                ],
            ),
        ]
        app = Starlette(
            routes=routes,
            middleware=[Middleware(_InjectAuthMiddleware)],
        )
        app.state.workstreams = mgr
        app.state.skip_permissions = True
        gq: queue.Queue[dict[str, Any]] = queue.Queue()
        app.state.global_queue = gq
        app.state.global_listeners = []
        app.state.global_listeners_lock = threading.Lock()

        client = TestClient(app, raise_server_exceptions=False)
        yield client, mgr, gq
        _reg._storage = old_storage

    @patch("turnstone.core.memory.resolve_workstream", return_value="src-ws-id")
    def test_fork_without_custom_name_keeps_auto_name(self, _mock_resolve, fork_app):
        """When forking without a custom name, the new workstream must keep
        its auto-generated ``ws-XXXX`` name, NOT the source workstream's title.
        """
        client, mgr, gq = fork_app

        # Mock session.resume so the fork path executes
        with patch.object(ChatSession, "resume", return_value=True):
            resp = client.post(
                "/v1/api/workstreams/new",
                json={"resume_ws": "src-ws-id"},
            )
        assert resp.status_code == 200
        data = resp.json()
        ws_id = data["ws_id"]
        assert data["resumed"] is True

        # The name must be the auto-generated short-ID, not any inherited title
        ws = mgr.get(ws_id)
        assert ws is not None
        assert ws.name == f"ws-{ws_id[:4]}"
        assert data["name"] == f"ws-{ws_id[:4]}"

        # The ws_rename event on the global queue should also carry the auto name
        events = []
        while not gq.empty():
            events.append(gq.get_nowait())
        rename_events = [e for e in events if e["type"] == "ws_rename"]
        assert len(rename_events) == 1
        assert rename_events[0]["name"] == f"ws-{ws_id[:4]}"

    @patch("turnstone.core.memory.resolve_workstream", return_value="src-ws-id")
    def test_fork_with_custom_name_uses_provided_name(self, _mock_resolve, fork_app):
        """When forking with a custom name, the new workstream must use that
        name (set as an alias).
        """
        client, mgr, gq = fork_app

        with patch.object(ChatSession, "resume", return_value=True), patch(
            "turnstone.core.memory.set_workstream_alias"
        ) as mock_alias:
            resp = client.post(
                "/v1/api/workstreams/new",
                json={"resume_ws": "src-ws-id", "name": "My Custom Fork"},
            )
        assert resp.status_code == 200
        data = resp.json()
        ws_id = data["ws_id"]
        assert data["resumed"] is True
        assert data["name"] == "My Custom Fork"

        ws = mgr.get(ws_id)
        assert ws is not None
        assert ws.name == "My Custom Fork"
        mock_alias.assert_called_once_with(ws_id, "My Custom Fork")
