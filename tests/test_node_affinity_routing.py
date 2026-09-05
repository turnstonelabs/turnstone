"""Real console -> node requests preserve placement intent and channel identity."""

from __future__ import annotations

import json

import httpx
import pytest

from tests.test_console_routing_proxy import (
    _TEST_AUTH_HEADERS,
    _TEST_JWT_SECRET,
    _make_app,
    _make_mock_collector,
)
from tests.test_server_authz import _DEFAULT_TEST_PERMS, _auth
from tests.test_server_authz import app_client as app_client
from turnstone.console.router import ConsoleRouter
from turnstone.core.storage import get_storage


def _console(storage):
    storage.register_service("server", "host-1", "http://host.example:8080")
    router = ConsoleRouter(storage)
    router.refresh_cache()
    collector = _make_mock_collector()
    collector.get_node_detail.side_effect = lambda node_id: (
        {"server_url": "http://host.example:8080"} if node_id == "host-1" else None
    )
    collector.get_all_nodes.return_value = [
        dict(node_id="host-1", reachable=True, max_ws=10, ws_total=0)
    ]
    app = _make_app(collector=collector, router=router, auth_storage=storage)
    app.state.proxy_client = object()  # read-only/denied requests must never dispatch
    return app, router


@pytest.mark.anyio
@pytest.mark.parametrize(
    "endpoint,body,multipart,required",
    [
        ("route", {}, False, None),
        ("route", {"ws_id": "a" * 32}, False, None),
        ("route", {"target_node": "host-1"}, False, "host-1"),
        ("route", {"required_node_id": "host-1"}, False, "host-1"),
        ("route", {"ws_id": "a" * 32, "target_node": "host-1"}, False, "host-1"),
        ("route", {"ws_id": "a" * 32}, True, None),
        ("route", {"ws_id": "a" * 32, "target_node": "host-1"}, True, "host-1"),
        ("cluster", {}, False, None),
        ("cluster", {"node_id": "pool"}, False, None),
        ("cluster", {"node_id": "host-1"}, False, "host-1"),
        ("cluster", {"node_id": "host-1"}, True, "host-1"),
    ],
)
async def test_create_intent_crosses_console_and_node(
    app_client, endpoint, body, multipart, required
):
    client, manager = app_client
    client.app.state.node_id = manager._node_id = "host-1"
    storage = get_storage()
    console, router = _console(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.app), base_url="http://host.example:8080"
    ) as node:
        console.state.proxy_client = node
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=console), base_url="http://console"
        ) as proxy:
            path = f"/v1/api/{endpoint}/workstreams/new"
            if multipart and endpoint == "route":
                path += "?ws_id=" + body["ws_id"]
            kwargs = (
                {"files": {"meta": (None, json.dumps(body), "application/json")}}
                if multipart
                else {"json": body}
            )
            response = await proxy.post(path, headers=_TEST_AUTH_HEADERS, **kwargs)
            assert response.status_code == 200, response.text
            ws_id = response.json().get("ws_id") or response.json()["correlation_id"]
            assert storage.get_workstream(ws_id)["required_node_id"] == required
            manager.close(ws_id)
            router.refresh_cache()
            assert storage.get_workstream(ws_id)["required_node_id"] == required
            if required:
                storage.deregister_service("server", "host-1")
                storage.register_service("server", "node-1", "http://other:8080")
                router.refresh_cache()
                response = await proxy.get(
                    "/v1/api/route", params={"ws_id": ws_id}, headers=_TEST_AUTH_HEADERS
                )
                assert response.status_code == 503, response.text
                assert response.json()["required_node_id"] == "host-1"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "endpoint,multipart", [("route", False), ("cluster", False), ("route", True)]
)
async def test_explicit_continuation_is_new_identity_and_inheritance_is_enforced(
    app_client, endpoint, multipart
):
    client, manager = app_client
    client.app.state.node_id = manager._node_id = "host-1"
    source = client.post(
        "/v1/api/workstreams/new",
        json={"required_node_id": "host-1"},
        headers=_auth("test-routing"),
    ).json()["ws_id"]
    storage = get_storage()
    storage.save_message(source, "user", "saved history")
    manager.close(source)
    console, router = _console(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.app), base_url="http://host.example:8080"
    ) as node:
        console.state.proxy_client = node
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=console), base_url="http://console"
        ) as proxy:
            path = f"/v1/api/{endpoint}/workstreams/new"
            if multipart:
                storage.register_service("server", "node-1", "http://other:8080")
                router.refresh_cache()
                from turnstone.core.rendezvous import NodeRef, select

                candidates = [
                    NodeRef("host-1", "http://host.example:8080"),
                    NodeRef("node-1", "http://other:8080"),
                ]
                destination_id = next(
                    f"{i:032x}"
                    for i in range(1000)
                    if select(f"{i:032x}", candidates).node_id == "node-1"
                )
                observed = []

                async def capture(request):
                    observed.append(request)

                node.event_hooks["request"].append(capture)
                response = await proxy.post(
                    path + "?ws_id=" + destination_id,
                    files={
                        "meta": (
                            None,
                            json.dumps(
                                {
                                    "ws_id": destination_id,
                                    "resume_ws": source,
                                    "resume_ws_exact": True,
                                }
                            ),
                            "application/json",
                        )
                    },
                    headers=_TEST_AUTH_HEADERS,
                )
                assert observed[-1].url.host == "host.example"
                assert json.loads(observed[-1].content)["resume_ws_exact"] is True
            else:
                response = await proxy.post(
                    path, json={"resume_ws": source}, headers=_TEST_AUTH_HEADERS
                )
            assert response.status_code == 200, response.text
            inherited = response.json().get("ws_id") or response.json()["correlation_id"]
            assert inherited != source
            assert storage.get_workstream(inherited)["required_node_id"] == "host-1"
            manager.close(inherited)
            storage.deregister_service("server", "host-1")
            storage.register_service("server", "node-1", "http://other:8080")
            router.refresh_cache()
            console.state.collector.get_node_detail.side_effect = lambda nid: (
                {"server_url": "http://other:8080"} if nid == "node-1" else None
            )
            client.app.state.node_id = manager._node_id = "node-1"
            response = await proxy.post(
                path, json={"resume_ws": source}, headers=_TEST_AUTH_HEADERS
            )
            assert response.status_code == 503, response.text
            assert manager.list_all() == []
            target_field = "target_node" if endpoint == "route" else "node_id"
            response = await proxy.post(
                path, json={"resume_ws": source, target_field: "node-1"}, headers=_TEST_AUTH_HEADERS
            )
            assert response.status_code == 200, response.text
            destination = response.json().get("ws_id") or response.json()["correlation_id"]
            assert destination not in {source, inherited}
            assert storage.get_workstream(destination)["required_node_id"] == "node-1"
            assert storage.get_workstream(source)["required_node_id"] == "host-1"
            assert [row["content"] for row in storage.load_messages(destination)] == [
                "saved history"
            ]


@pytest.mark.anyio
async def test_private_requirement_is_hidden_from_route_and_fork(app_client):
    from turnstone.core.auth import JWT_AUD_CONSOLE, create_jwt

    client, manager = app_client
    client.app.state.node_id = manager._node_id = "host-1"
    source = client.post(
        "/v1/api/workstreams/new", json={"required_node_id": "host-1"}, headers=_auth("owner")
    ).json()["ws_id"]
    manager.close(source)
    storage = get_storage()
    storage.create_project("private", "Private", "owner", visibility="private")
    import sqlalchemy as sa

    from turnstone.core.storage._schema import workstreams

    with storage._conn() as conn:
        conn.execute(
            sa.update(workstreams).where(workstreams.c.ws_id == source).values(project_id="private")
        )
        conn.commit()
    console, router = _console(storage)
    storage.deregister_service("server", "host-1")
    storage.register_service("server", "node-1", "http://other:8080")
    router.refresh_cache()
    token = create_jwt(
        user_id="stranger",
        scopes=frozenset({"read", "write"}),
        permissions=_DEFAULT_TEST_PERMS,
        source="test",
        secret=_TEST_JWT_SECRET,
        audience=JWT_AUD_CONSOLE,
    )
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=console), base_url="http://console"
    ) as proxy:
        for ws_id in (source, "b" * 32):
            response = await proxy.get("/v1/api/route", params={"ws_id": ws_id}, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["node_id"] == "node-1"
            for endpoint in ("route", "cluster"):
                response = await proxy.post(
                    f"/v1/api/{endpoint}/workstreams/new",
                    json={"resume_ws": ws_id, "resume_ws_exact": True},
                    headers=headers,
                )
                assert response.status_code == 404, response.text
                assert response.json() == {"error": "Workstream not found"}


@pytest.mark.anyio
@pytest.mark.parametrize("console_mode", [False, True])
async def test_channel_preserves_pin_when_required_node_is_absent(app_client, console_mode):
    from turnstone.channels._routing import ChannelRouter
    from turnstone.sdk._types import TurnstoneAPIError
    from turnstone.sdk.console import AsyncTurnstoneConsole
    from turnstone.sdk.server import AsyncTurnstoneServer

    client, manager = app_client
    client.app.state.node_id = manager._node_id = "host-1"
    source = client.post(
        "/v1/api/workstreams/new",
        json={"required_node_id": "host-1"},
        headers=_auth("test-routing"),
    ).json()["ws_id"]
    manager.close(source)
    storage = get_storage()
    storage.create_channel_route("discord", "123", source, channel_user_id="owner")
    channels = ChannelRouter("http://other:8080", storage)
    await channels.aclose()
    if console_mode:
        app, router = _console(storage)
        storage.deregister_service("server", "host-1")
        storage.register_service("server", "node-1", "http://other:8080")
        router.refresh_cache()
        headers = _TEST_AUTH_HEADERS
    else:
        app = client.app
        headers = _auth("test-routing")
    client.app.state.node_id = manager._node_id = "node-1"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://other:8080", headers=headers
    ) as http:
        if console_mode:
            channels._server = None
            channels._console = AsyncTurnstoneConsole(httpx_client=http)
        else:
            channels._server = AsyncTurnstoneServer(httpx_client=http)
        with pytest.raises(TurnstoneAPIError) as error:
            await channels.get_or_create_workstream("discord", "123", channel_user_id="owner")
        assert error.value.status_code == (503 if console_mode else 409)
        assert storage.get_channel_route("discord", "123")["ws_id"] == source
        assert manager.list_all() == []
    await channels.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "endpoint,body,status",
    [
        ("route", {"target_node": "missing"}, 503),
        ("cluster", {"node_id": "missing"}, 503),
        ("route", {"required_node_id": "host-1", "target_node": "node-1"}, 400),
        ("cluster", {"required_node_id": "host-1", "node_id": "node-1"}, 400),
        ("route", {"required_node_id": ""}, 400),
        ("cluster", {"required_node_id": False}, 400),
    ],
)
async def test_invalid_or_missing_required_node_never_dispatches(
    app_client, endpoint, body, status
):
    console, _ = _console(get_storage())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=console), base_url="http://console"
    ) as proxy:
        response = await proxy.post(
            f"/v1/api/{endpoint}/workstreams/new", json=body, headers=_TEST_AUTH_HEADERS
        )
    assert response.status_code == status, response.text
    assert app_client[1].list_all() == []
