"""History admission and node actions through real auth and migrated storage."""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from tests.test_server_authz import _FakeSession, _FakeUI
from turnstone.console.collector import ClusterCollector
from turnstone.console.server import create_app as create_console
from turnstone.core.adapters.interactive_adapter import InteractiveAdapter
from turnstone.core.auth import JWT_AUD_CONSOLE, JWT_AUD_SERVER, create_jwt, hash_password
from turnstone.core.config_store import ConfigStore
from turnstone.core.session_manager import SessionManager
from turnstone.core.storage import init_storage, reset_storage
from turnstone.server import create_app as create_node

pytestmark = pytest.mark.anyio
_SECRET = "history-rbac-test-secret-at-least-32-characters"


@pytest.fixture
async def history_apps(tmp_path):
    reset_storage()
    storage = init_storage("sqlite", path=str(tmp_path / "history.db"))
    password = "history-test-password"
    password_hash = hash_password(password)
    for user, role in (("operator", "operator"), ("admin", "admin"), ("viewer", "viewer")):
        storage.create_user(user, user, user, password_hash)
        storage.assign_role(user, "builtin-" + role)
    storage.create_project("shared", "Shared", "admin")
    storage.add_project_member("shared", "operator")
    storage.add_project_member("shared", "viewer")
    storage.create_project("private", "Private", "admin")
    for ws_id, kind, owner, project in (
        ("own", "interactive", "operator", "shared"),
        ("related", "interactive", "admin", "shared"),
        ("hidden", "interactive", "admin", "private"),
        ("coordinator", "coordinator", "admin", "shared"),
        ("legacy", "interactive", "admin", None),
    ):
        storage.register_workstream(
            ws_id, "node-a", name=ws_id, user_id=owner, kind=kind, project_id=project
        )
        storage.update_workstream_state(ws_id, "closed")
        storage.save_message(ws_id, "user", "History for " + ws_id)
    events = queue.Queue()
    adapter = InteractiveAdapter(
        global_queue=events,
        ui_factory=lambda ws: _FakeUI(ws_id=ws.id, user_id=ws.user_id),
        session_factory=lambda ui, _model, ws_id, **kw: _FakeSession(ws_id, ui._user_id),
    )
    manager = SessionManager(adapter, storage=storage, max_active=10, event_emitter=adapter)
    node = create_node(
        workstreams=manager,
        global_queue=events,
        global_listeners=[],
        global_listeners_lock=threading.Lock(),
        skip_permissions=False,
        jwt_secret=_SECRET,
        auth_storage=storage,
    )
    collector = MagicMock(spec=ClusterCollector)
    collector.get_node_detail.return_value = {"server_url": "http://node.test"}
    router = MagicMock()
    router.is_ready.return_value = True
    router.route.return_value = SimpleNamespace(node_id="node-a", url="http://node.test")
    console = create_console(
        collector=collector, jwt_secret=_SECRET, auth_storage=storage, router=router
    )
    config = ConfigStore(storage)
    node.state.config_store = console.state.config_store = config
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(node), base_url="http://node.test") as nc,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(console), base_url="http://console.test"
        ) as cc,
    ):
        console.state.proxy_client = nc
        for client in (nc, cc):
            response = await client.post(
                "/v1/api/auth/login", json={"username": "operator", "password": password}
            )
            assert response.status_code == 200
            assert "admin.coordinator" not in response.json()["permissions"]
        try:
            yield SimpleNamespace(
                node=nc,
                console=cc,
                node_app=node,
                console_app=console,
                storage=storage,
                config=config,
                manager=manager,
                events=events,
            )
        finally:
            for ws in manager.list_all():
                manager.close(ws.id)
    reset_storage()


def _headers(user="operator", scopes=("read", "write"), permissions=(), audience=JWT_AUD_SERVER):
    return {
        "Authorization": "Bearer "
        + create_jwt(
            user_id=user,
            scopes=frozenset(scopes),
            permissions=frozenset(permissions),
            source="test",
            secret=_SECRET,
            audience=audience,
        )
    }


@pytest.mark.parametrize("required", [False, True])
async def test_operator_discovers_own_and_project_history(history_apps, required):
    apps = history_apps
    apps.config.set("server.require_project", required)
    for client in (apps.console, apps.node):
        response = await client.get("/v1/api/workstreams/saved")
        assert response.status_code == 200
        assert {row["ws_id"] for row in response.json()["workstreams"]} == {
            "own",
            "related",
            "legacy",
        }
    proxied = await apps.console.get("/node/node-a/v1/api/workstreams/saved")
    assert proxied.status_code == 200
    assert {row["ws_id"] for row in proxied.json()["workstreams"]} == {"own", "related", "legacy"}
    assert apps.manager.list_all() == []
    assert (await apps.node.get("/v1/api/workstreams/hidden/history")).status_code == 403
    assert (await apps.node.get("/v1/api/workstreams/coordinator/history")).status_code == 404
    assert (await apps.node.get("/v1/api/workstreams/related/history")).status_code == 200


async def test_real_close_preserves_discovery_and_emits_event(history_apps):
    apps = history_apps
    assert (await apps.node.post("/v1/api/workstreams/related/open")).status_code == 200
    response = await apps.node.post("/v1/api/workstreams/related/close", json={})
    assert response.status_code == 200, response.text
    assert apps.manager.get("related") is None
    emitted = []
    while not apps.events.empty():
        emitted.append(apps.events.get_nowait())
    assert any(e["type"] == "ws_closed" and e["ws_id"] == "related" for e in emitted)
    rows = (await apps.console.get("/v1/api/workstreams/saved")).json()["workstreams"]
    assert "related" in {row["ws_id"] for row in rows}
    assert (await apps.node.post("/v1/api/workstreams/related/open")).status_code == 200


@pytest.mark.parametrize("permission,expected", [(False, 403), (True, 200)])
@pytest.mark.parametrize("route", ["node", "node_proxy", "route_proxy"])
async def test_coordinator_delete_requires_its_permission(
    history_apps, permission, expected, route
):
    apps = history_apps
    client = apps.node if route == "node" else apps.console
    path = {
        "node": "/v1/api/workstreams/coordinator/delete",
        "node_proxy": "/node/node-a/v1/api/workstreams/coordinator/delete",
        "route_proxy": "/v1/api/route/workstreams/delete",
    }[route]
    response = await client.post(
        path,
        json={"ws_id": "coordinator"},
        headers=_headers(
            permissions=("admin.coordinator",) if permission else (),
            audience=JWT_AUD_SERVER if route == "node" else JWT_AUD_CONSOLE,
        ),
    )
    assert response.status_code == expected
    assert (apps.storage.get_workstream("coordinator") is None) == permission
    assert apps.manager.list_all() == []
    assert (await apps.node.post("/v1/api/workstreams/related/delete")).status_code == 200


@pytest.mark.parametrize(
    "ws_id,status",
    [
        ("related", 403),
        ("hidden", 403),
        ("coordinator", 404),
        ("missing", 404),
    ],
)
async def test_reader_cannot_rehydrate_through_detail(history_apps, ws_id, status):
    apps = history_apps
    reader = _headers(user="viewer", scopes=("read",))
    assert (
        await apps.node.get(f"/v1/api/workstreams/{ws_id}", headers=reader)
    ).status_code == status
    assert apps.manager.list_all() == []
    for suffix in ("history", "export"):
        response = await apps.node.get(f"/v1/api/workstreams/related/{suffix}", headers=reader)
        assert response.status_code == 200
    assert apps.manager.list_all() == []
    assert (await apps.node.get("/v1/api/workstreams/related")).status_code == 200
    assert (await apps.node.get("/v1/api/workstreams/related", headers=reader)).status_code == 200


async def test_saved_query_failure_is_unavailable(history_apps, monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("private database diagnostic")

    monkeypatch.setattr(history_apps.storage, "list_workstreams_with_history", fail)
    for client in (history_apps.node, history_apps.console):
        response = await client.get("/v1/api/workstreams/saved")
        assert response.status_code == 503
        assert response.json() == {"error": "Saved sessions unavailable"}


async def test_whoami_distinguishes_scopes_from_named_permissions(history_apps):
    response = await history_apps.node.get(
        "/v1/api/auth/whoami",
        headers=_headers(user="admin", scopes=("read",), permissions=("admin.coordinator",)),
    )
    assert response.status_code == 200
    assert response.json()["scopes"] == "read"
    assert response.json()["permissions"] == "admin.coordinator"


async def test_admin_union_and_scope_denials(history_apps):
    apps = history_apps
    response = await apps.console.post(
        "/v1/api/auth/login", json={"username": "admin", "password": "history-test-password"}
    )
    assert response.status_code == 200
    rows = (await apps.console.get("/v1/api/workstreams/saved")).json()["workstreams"]
    assert {row["ws_id"] for row in rows} == {"own", "related", "hidden", "coordinator", "legacy"}
    reader = _headers(user="admin", scopes=("read",), permissions=("admin.coordinator",))
    assert (
        await apps.node.post("/v1/api/workstreams/coordinator/delete", headers=reader)
    ).status_code == 403
    assert (
        await apps.node.get("/v1/api/workstreams/saved", headers=_headers(scopes=()))
    ).status_code == 403
    apps.node.cookies.clear()
    assert (await apps.node.get("/v1/api/workstreams/saved")).status_code == 401
