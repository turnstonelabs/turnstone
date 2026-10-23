"""Image results cross the real SDK transport on both dispatch paths and protocol revisions."""

from __future__ import annotations

import contextlib
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import mcp.types as mcp_types
import pytest
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Mount

from tests.conftest import _free_port, _wait_tcp_ready, serve_until_exit
from tests.test_mcp_http_failures import RefuseDiscover
from tests.test_mcp_image_results import URI, _result
from turnstone.core import mcp_client


@pytest.fixture(params=["2026-07-28", "2025-11-25"])
def image_server(request):
    async def list_tools(ctx, params):
        return mcp_types.ListToolsResult(
            tools=[mcp_types.Tool(name="camera", input_schema={"type": "object"})]
        )

    async def call_tool(ctx, params):
        arguments = params.arguments or {}
        if arguments.get("structured"):
            return mcp_types.CallToolResult(content=[], structured_content={"answer": 42})
        return _result(error=bool(arguments.get("fail")), image_only=bool(arguments.get("only")))

    sessions = StreamableHTTPSessionManager(
        app=Server("camera", on_list_tools=list_tools, on_call_tool=call_tool),
        json_response=False,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with sessions.run():
            yield

    app = Starlette(routes=[Mount("/", app=sessions.handle_request)], lifespan=lifespan)
    if request.param == "2025-11-25":
        app.add_middleware(RefuseDiscover)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="critical",
            access_log=False,
            timeout_graceful_shutdown=0,
        )
    )
    thread = threading.Thread(target=serve_until_exit, args=(server,), daemon=True)
    thread.start()
    try:
        assert _wait_tcp_ready(port, 5)
        yield f"http://127.0.0.1:{port}/mcp", request.param
    finally:
        server.should_exit = True
        server.force_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("pooled", [False, True], ids=["static", "pool"])
def test_images_and_errors_survive_sdk_and_normalize_off_loop(image_server, pooled, monkeypatch):
    url, revision = image_server
    monkeypatch.setattr(mcp_client, "load_config", lambda *_args: {})
    manager = mcp_client.MCPClientManager({} if pooled else {"srv": {"type": "http", "url": url}})
    monkeypatch.setattr(manager, "_static_health_loop", AsyncMock())
    if pooled:
        storage = MagicMock()
        storage.get_mcp_server_by_name.return_value = {
            "name": "srv",
            "transport": "streamable-http",
            "url": url,
            "auth_type": "oauth_user",
        }
        manager.set_storage(storage)
        manager.set_app_state(SimpleNamespace())
        monkeypatch.setattr(
            manager,
            "_pool_lookup_checked",
            AsyncMock(
                return_value=(SimpleNamespace(token="test-token"), None),
            ),
        )
    normalized_on = []
    normalize = mcp_client.stored_upload_content

    def record_thread(kind, data):
        normalized_on.append(threading.get_ident())
        return normalize(kind, data)

    monkeypatch.setattr(mcp_client, "stored_upload_content", record_thread)
    manager.start()
    try:
        options = {"user_id": "user-1" if pooled else None, "timeout": 5}
        for arguments in ({}, {"only": True}, {"fail": True}):
            result = manager.call_tool_sync("mcp__srv__camera", arguments, **options)
            assert result.is_error is bool(arguments.get("fail"))
            output = mcp_client._decode_tool_result(result)
            assert isinstance(output, list)
            assert output[-1]["image_url"]["url"] == URI
            if arguments.get("fail"):
                assert output[0]["text"].startswith("Error:")
        assert (
            mcp_client._decode_tool_result(
                manager.call_tool_sync("mcp__srv__camera", {"structured": True}, **options)
            )
            == '{"answer": 42}'
        )
        state = (
            manager._user_pool_entries[("user-1", "srv")]
            if pooled
            else manager._static_servers["srv"]
        )
        assert state.session.protocol_version == revision
        assert normalized_on == [threading.get_ident()] * 3
    finally:
        manager.shutdown()
