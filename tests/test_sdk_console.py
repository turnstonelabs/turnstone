"""Tests for turnstone.sdk.console — console client with mocked HTTP transport."""

from __future__ import annotations

import json

import httpx
import pytest

from turnstone.sdk._types import TurnstoneAPIError
from turnstone.sdk.console import AsyncTurnstoneConsole


def _json_response(data: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=data)


def _mock_transport(
    responses: dict[str, httpx.Response] | None = None,
) -> httpx.MockTransport:
    table = responses or {}

    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        if key in table:
            return table[key]
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Cluster overview
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_overview():
    transport = _mock_transport(
        {
            "GET /v1/api/cluster/overview": _json_response(
                {
                    "nodes": 2,
                    "workstreams": 5,
                    "states": {"running": 1, "idle": 4},
                    "aggregate": {"total_tokens": 1000, "total_tool_calls": 20},
                    "version_drift": False,
                    "versions": ["0.3.0"],
                }
            )
        }
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        resp = await client.overview()
        assert resp.nodes == 2
        assert resp.workstreams == 5


@pytest.mark.anyio
async def test_nodes():
    transport = _mock_transport(
        {
            "GET /v1/api/cluster/nodes": _json_response(
                {
                    "nodes": [
                        {
                            "node_id": "n1",
                            "server_url": "http://localhost:8080",
                            "ws_total": 3,
                            "ws_running": 1,
                            "ws_thinking": 0,
                            "ws_attention": 0,
                            "ws_idle": 2,
                            "ws_error": 0,
                            "total_tokens": 500,
                            "started": 1700000000.0,
                            "reachable": True,
                            "health": {"status": "ok"},
                            "version": "0.3.0",
                        }
                    ],
                    "total": 1,
                }
            )
        }
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        resp = await client.nodes(sort="tokens", limit=50)
        assert resp.total == 1
        assert resp.nodes[0].node_id == "n1"


@pytest.mark.anyio
async def test_workstreams():
    transport = _mock_transport(
        {
            "GET /v1/api/cluster/workstreams": _json_response(
                {
                    "workstreams": [
                        {
                            "id": "ws1",
                            "name": "test",
                            "state": "running",
                            "node": "n1",
                        }
                    ],
                    "total": 1,
                    "page": 1,
                    "per_page": 50,
                    "pages": 1,
                }
            )
        }
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        resp = await client.workstreams(state="running", page=1)
        assert resp.total == 1


@pytest.mark.anyio
async def test_node_detail():
    transport = _mock_transport(
        {
            "GET /v1/api/cluster/node/n1": _json_response(
                {
                    "node_id": "n1",
                    "server_url": "http://localhost:8080",
                    "health": {"status": "ok"},
                    "workstreams": [],
                    "aggregate": {"total_tokens": 0, "total_tool_calls": 0},
                }
            )
        }
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        resp = await client.node_detail("n1")
        assert resp.node_id == "n1"


@pytest.mark.anyio
async def test_create_workstream():
    transport = _mock_transport(
        {
            "POST /v1/api/cluster/workstreams/new": _json_response(
                {"status": "dispatched", "correlation_id": "abc123", "target_node": "n1"}
            )
        }
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        resp = await client.create_workstream(node_id="n1", name="test")
        assert resp.correlation_id == "abc123"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_login():
    transport = _mock_transport(
        {"POST /v1/api/auth/login": _json_response({"status": "ok", "role": "read"})}
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        resp = await client.login("tok_test")
        assert resp.role == "read"


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_health():
    transport = _mock_transport(
        {
            "GET /health": _json_response(
                {
                    "status": "ok",
                    "service": "turnstone-console",
                    "nodes": 2,
                    "workstreams": 5,
                    "version_drift": False,
                    "versions": ["0.3.0"],
                }
            )
        }
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        resp = await client.health()
        assert resp.status == "ok"
        assert resp.nodes == 2


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_node_not_found():
    transport = _mock_transport(
        {"GET /v1/api/cluster/node/bad": httpx.Response(404, json={"error": "Node not found"})}
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        with pytest.raises(TurnstoneAPIError) as exc_info:
            await client.node_detail("bad")
        assert exc_info.value.status_code == 404


@pytest.mark.anyio
async def test_query_params_passed():
    """Verify query params are sent correctly for paginated endpoints."""
    captured_url: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_url.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "workstreams": [],
                "total": 0,
                "page": 2,
                "per_page": 25,
                "pages": 0,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        await client.workstreams(state="running", page=2, per_page=25)
        assert "state=running" in captured_url[0]
        assert "page=2" in captured_url[0]
        assert "per_page=25" in captured_url[0]


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------

_SCHEDULE_FIXTURE = {
    "task_id": "t1",
    "name": "nightly",
    "description": "",
    "schedule_type": "cron",
    "cron_expr": "0 2 * * *",
    "at_time": "",
    "target_mode": "auto",
    "model": "",
    "initial_message": "Run nightly checks",
    "auto_approve": False,
    "auto_approve_tools": [],
    "enabled": True,
    "created_by": "u1",
    "last_run": None,
    "next_run": "2026-03-06T02:00:00Z",
    "created": "2026-03-05T12:00:00Z",
    "updated": "2026-03-05T12:00:00Z",
}


@pytest.mark.anyio
async def test_list_schedules():
    transport = _mock_transport(
        {"GET /v1/api/admin/schedules": _json_response({"schedules": [_SCHEDULE_FIXTURE]})}
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        resp = await client.list_schedules()
        assert len(resp.schedules) == 1
        assert resp.schedules[0].task_id == "t1"
        assert resp.schedules[0].name == "nightly"


@pytest.mark.anyio
async def test_create_schedule():
    captured_body: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.append(json.loads(request.content))
        return _json_response(_SCHEDULE_FIXTURE)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        resp = await client.create_schedule(
            name="nightly",
            schedule_type="cron",
            initial_message="Run nightly checks",
            cron_expr="0 2 * * *",
        )
        assert resp.task_id == "t1"
        body = captured_body[0]
        assert body["name"] == "nightly"
        assert body["schedule_type"] == "cron"
        assert body["cron_expr"] == "0 2 * * *"
        assert body["initial_message"] == "Run nightly checks"
        # Optional fields with defaults should not appear when not set
        assert "description" not in body
        assert "model" not in body


@pytest.mark.anyio
async def test_get_schedule():
    transport = _mock_transport(
        {"GET /v1/api/admin/schedules/t1": _json_response(_SCHEDULE_FIXTURE)}
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        resp = await client.get_schedule("t1")
        assert resp.task_id == "t1"
        assert resp.schedule_type == "cron"


@pytest.mark.anyio
async def test_update_schedule_partial():
    """Only explicitly-passed fields should appear in the request body."""
    captured_body: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.append(json.loads(request.content))
        return _json_response({**_SCHEDULE_FIXTURE, "enabled": False})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        resp = await client.update_schedule("t1", enabled=False)
        assert resp.enabled is False
        body = captured_body[0]
        assert body == {"enabled": False}


@pytest.mark.anyio
async def test_delete_schedule():
    transport = _mock_transport(
        {"DELETE /v1/api/admin/schedules/t1": _json_response({"status": "ok"})}
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        resp = await client.delete_schedule("t1")
        assert resp.status == "ok"


@pytest.mark.anyio
async def test_list_schedule_runs():
    transport = _mock_transport(
        {
            "GET /v1/api/admin/schedules/t1/runs": _json_response(
                {
                    "runs": [
                        {
                            "run_id": "r1",
                            "task_id": "t1",
                            "node_id": "n1",
                            "ws_id": "ws1",
                            "correlation_id": "c1",
                            "started": "2026-03-05T02:00:00Z",
                            "status": "dispatched",
                            "error": "",
                        }
                    ]
                }
            )
        }
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        resp = await client.list_schedule_runs("t1", limit=10)
        assert len(resp.runs) == 1
        assert resp.runs[0].run_id == "r1"
        assert resp.runs[0].status == "dispatched"


# ---------------------------------------------------------------------------
# create_workstream extended params
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_workstream_extended_params():
    """New optional params appear in JSON body only when non-empty."""
    captured_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content))
        return _json_response(
            {"status": "dispatched", "correlation_id": "abc", "target_node": "n1"}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        await client.create_workstream(
            node_id="n1",
            name="ext",
            auto_approve=True,
            auto_approve_tools="read_file",
            user_id="u42",
        )
        assert captured_body["node_id"] == "n1"
        assert captured_body["name"] == "ext"
        assert captured_body["auto_approve"] is True
        assert captured_body["auto_approve_tools"] == "read_file"
        assert captured_body["user_id"] == "u42"


@pytest.mark.anyio
async def test_create_workstream_omits_empty_new_params():
    """Default-valued new params should not appear in JSON body."""
    captured_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content))
        return _json_response(
            {"status": "dispatched", "correlation_id": "abc", "target_node": "n1"}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        await client.create_workstream(name="min")
        assert captured_body == {"name": "min"}
        assert "auto_approve" not in captured_body
        assert "auto_approve_tools" not in captured_body
        assert "user_id" not in captured_body


# ---------------------------------------------------------------------------
# Route methods
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_route_create_workstream():
    captured_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content))
        return _json_response({"ws_id": "ws1", "node_url": "http://n1:8080", "node_id": "n1"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        resp = await client.route_create_workstream(
            name="routed",
            model="gpt-5",
            auto_approve=True,
            target_node="n1",
            user_id="u1",
        )
        assert resp["ws_id"] == "ws1"
        assert resp["node_url"] == "http://n1:8080"
        assert captured_body["name"] == "routed"
        assert captured_body["model"] == "gpt-5"
        assert captured_body["auto_approve"] is True
        assert captured_body["target_node"] == "n1"
        assert captured_body["user_id"] == "u1"


@pytest.mark.anyio
async def test_route_create_workstream_rejects_attachments_with_target_node():
    """Regression: target_node has no effect on multipart route_create
    (which routes by ?ws_id=) — refuse the combination at the SDK boundary
    instead of silently routing to the wrong node.
    """
    from turnstone.sdk._types import AttachmentUpload

    transport = httpx.MockTransport(
        lambda req: _json_response({"error": "should not be called"}, status=500)
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        with pytest.raises(ValueError, match="target_node"):
            await client.route_create_workstream(
                name="x",
                target_node="n1",
                attachments=[AttachmentUpload(filename="a.txt", data=b"hi")],
            )


@pytest.mark.anyio
async def test_route_create_workstream_omits_defaults():
    captured_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content))
        return _json_response({"ws_id": "ws1", "node_url": "http://n1:8080"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        await client.route_create_workstream(name="bare")
        assert captured_body == {"name": "bare"}


@pytest.mark.anyio
async def test_route_send():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return _json_response({"status": "ok"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        resp = await client.route_send("Hello", "ws1")
        assert resp["status"] == "ok"
        assert captured["path"] == "/v1/api/route/workstreams/ws1/send"
        assert captured["body"] == {"message": "Hello"}


@pytest.mark.anyio
async def test_route_approve():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return _json_response({"status": "ok"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        await client.route_approve(ws_id="ws1", approved=False, feedback="no", always=True)
        assert captured["path"] == "/v1/api/route/workstreams/ws1/approve"
        assert captured["body"] == {
            "approved": False,
            "feedback": "no",
            "always": True,
        }


@pytest.mark.anyio
async def test_route_approve_omits_defaults():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return _json_response({"status": "ok"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        await client.route_approve(ws_id="ws1", approved=True)
        assert captured["path"] == "/v1/api/route/workstreams/ws1/approve"
        assert captured["body"] == {"approved": True}


@pytest.mark.anyio
async def test_route_plan_feedback():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return _json_response({"status": "ok"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        await client.route_plan_feedback(ws_id="ws1", feedback="approved")
        assert captured["path"] == "/v1/api/route/plan"
        assert captured["body"] == {"ws_id": "ws1", "feedback": "approved"}


@pytest.mark.anyio
async def test_route_close():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return _json_response({"status": "ok"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        resp = await client.route_close("ws1")
        assert resp["status"] == "ok"
        assert captured["path"] == "/v1/api/route/workstreams/ws1/close"
        assert captured["body"] == {}


@pytest.mark.anyio
async def test_route_cancel():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return _json_response({"status": "ok"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        await client.route_cancel("ws1", force=True)
        assert captured["path"] == "/v1/api/route/workstreams/ws1/cancel"
        assert captured["body"] == {"force": True}


@pytest.mark.anyio
async def test_route_cancel_omits_force_when_false():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return _json_response({"status": "ok"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        await client.route_cancel("ws1")
        assert captured["path"] == "/v1/api/route/workstreams/ws1/cancel"
        assert captured["body"] == {}


@pytest.mark.anyio
async def test_route_command():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return _json_response({"status": "ok"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        await client.route_command(ws_id="ws1", command="/clear")
        assert captured["path"] == "/v1/api/route/command"
        assert captured["body"] == {"ws_id": "ws1", "command": "/clear"}


@pytest.mark.anyio
async def test_route_lookup():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["url"] = str(request.url)
        return _json_response({"node_url": "http://n1:8080", "node_id": "n1"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneConsole(httpx_client=hc)
        resp = await client.route_lookup("ws1")
        assert resp["node_url"] == "http://n1:8080"
        assert resp["node_id"] == "n1"
        assert captured["path"] == "/v1/api/route"
        assert "ws_id=ws1" in captured["url"]
