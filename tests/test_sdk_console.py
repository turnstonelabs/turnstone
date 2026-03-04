"""Tests for turnstone.sdk.console — console client with mocked HTTP transport."""

from __future__ import annotations

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
