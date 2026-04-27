"""Tests for turnstone.sdk.server — server client with mocked HTTP transport."""

from __future__ import annotations

import json

import httpx
import pytest

from turnstone.sdk._types import TurnstoneAPIError
from turnstone.sdk.server import AsyncTurnstoneServer


def _mock_transport(
    responses: dict[str, httpx.Response] | None = None,
) -> httpx.MockTransport:
    """Create a mock transport that routes by method+path."""
    table = responses or {}

    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        if key in table:
            return table[key]
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


def _json_response(data: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=data)


# ---------------------------------------------------------------------------
# Workstream management
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_workstreams():
    transport = _mock_transport(
        {
            "GET /v1/api/workstreams": _json_response(
                {"workstreams": [{"ws_id": "ws1", "name": "test", "state": "idle"}]}
            )
        }
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        resp = await client.list_workstreams()
        assert len(resp.workstreams) == 1
        # Row key renamed id → ws_id in the Stage 2 list-verb lift.
        assert resp.workstreams[0].ws_id == "ws1"


@pytest.mark.anyio
async def test_dashboard():
    transport = _mock_transport(
        {
            "GET /v1/api/dashboard": _json_response(
                {
                    "workstreams": [
                        {
                            "ws_id": "ws1",
                            "name": "demo",
                            "state": "idle",
                            "tokens": 100,
                            "context_ratio": 0.1,
                        }
                    ],
                    "aggregate": {
                        "total_tokens": 100,
                        "total_tool_calls": 5,
                        "active_count": 1,
                        "total_count": 1,
                    },
                }
            )
        }
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        resp = await client.dashboard()
        assert resp.aggregate.total_tokens == 100
        assert len(resp.workstreams) == 1


@pytest.mark.anyio
async def test_create_workstream():
    transport = _mock_transport(
        {"POST /v1/api/workstreams/new": _json_response({"ws_id": "ws_new", "name": "Analysis"})}
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        resp = await client.create_workstream(name="Analysis")
        assert resp.ws_id == "ws_new"
        assert resp.name == "Analysis"


@pytest.mark.anyio
async def test_close_workstream():
    transport = _mock_transport(
        {"POST /v1/api/workstreams/close": _json_response({"status": "ok"})}
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        resp = await client.close_workstream("ws1")
        assert resp.status == "ok"


# ---------------------------------------------------------------------------
# Chat interaction
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_send():
    transport = _mock_transport({"POST /v1/api/send": _json_response({"status": "ok"})})
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        resp = await client.send("Hello", "ws1")
        assert resp.status == "ok"


@pytest.mark.anyio
async def test_approve():
    transport = _mock_transport({"POST /v1/api/approve": _json_response({"status": "ok"})})
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        resp = await client.approve(ws_id="ws1", approved=True, feedback="looks good")
        assert resp.status == "ok"


@pytest.mark.anyio
async def test_plan_feedback():
    transport = _mock_transport({"POST /v1/api/plan": _json_response({"status": "ok"})})
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        resp = await client.plan_feedback(ws_id="ws1", feedback="approved")
        assert resp.status == "ok"


@pytest.mark.anyio
async def test_command():
    transport = _mock_transport({"POST /v1/api/command": _json_response({"status": "ok"})})
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        resp = await client.command(ws_id="ws1", command="/clear")
        assert resp.status == "ok"


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_saved_workstreams():
    transport = _mock_transport(
        {
            "GET /v1/api/workstreams/saved": _json_response(
                {
                    "workstreams": [
                        {
                            "ws_id": "s1",
                            "title": "test",
                            "created": "2024-01-01",
                            "updated": "2024-01-02",
                            "message_count": 5,
                        }
                    ]
                }
            )
        }
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        resp = await client.list_saved_workstreams()
        assert len(resp.workstreams) == 1


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_login():
    transport = _mock_transport(
        {"POST /v1/api/auth/login": _json_response({"status": "ok", "role": "full"})}
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        resp = await client.login("test_token")
        assert resp.role == "full"


@pytest.mark.anyio
async def test_logout():
    transport = _mock_transport({"POST /v1/api/auth/logout": _json_response({"status": "ok"})})
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        resp = await client.logout()
        assert resp.status == "ok"


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
                    "version": "0.3.0",
                    "uptime_seconds": 120.0,
                    "model": "gpt-5",
                    "workstreams": {"total": 1, "idle": 1},
                }
            )
        }
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        resp = await client.health()
        assert resp.status == "ok"
        assert resp.version == "0.3.0"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_api_error_raised():
    transport = _mock_transport(
        {"POST /v1/api/send": httpx.Response(404, json={"error": "Unknown workstream"})}
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        with pytest.raises(TurnstoneAPIError) as exc_info:
            await client.send("hi", "bad_ws")
        assert exc_info.value.status_code == 404
        assert "Unknown workstream" in exc_info.value.message


@pytest.mark.anyio
async def test_auth_header_injected():
    """Verify the Authorization header is set when a token is provided."""
    captured_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(200, json={"workstreams": []})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        # Manually set auth header since we're injecting the client
        hc.headers["Authorization"] = "Bearer tok_test"
        client = AsyncTurnstoneServer(httpx_client=hc)
        await client.list_workstreams()
        assert captured_headers.get("authorization") == "Bearer tok_test"


@pytest.mark.anyio
async def test_request_body_correct():
    """Verify POST requests send the correct JSON body."""
    captured_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        await client.send("Hello world", "ws_123")
        assert captured_body == {"message": "Hello world", "ws_id": "ws_123"}


# ---------------------------------------------------------------------------
# create_workstream extended params
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_workstream_extended_params():
    """New optional params appear in JSON body only when non-empty."""
    captured_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content))
        return httpx.Response(200, json={"ws_id": "ws_ext", "name": "ext"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        await client.create_workstream(
            name="ext",
            initial_message="hi",
            auto_approve_tools="read_file,write_file",
            user_id="u42",
            ws_id="ws_custom",
        )
        assert captured_body["name"] == "ext"
        assert captured_body["initial_message"] == "hi"
        assert captured_body["auto_approve_tools"] == "read_file,write_file"
        assert captured_body["user_id"] == "u42"
        assert captured_body["ws_id"] == "ws_custom"


@pytest.mark.anyio
async def test_create_workstream_omits_empty_params():
    """Empty-string params should NOT appear in the JSON body."""
    captured_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content))
        return httpx.Response(200, json={"ws_id": "ws_min", "name": "min"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        await client.create_workstream(name="min")
        assert captured_body == {"name": "min"}
        assert "initial_message" not in captured_body
        assert "auto_approve_tools" not in captured_body
        assert "user_id" not in captured_body
        assert "ws_id" not in captured_body
