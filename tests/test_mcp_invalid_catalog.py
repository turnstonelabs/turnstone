"""A malformed catalog entry fails its MCP server with a clear error (#1224).

The SDK validates a list result as a whole, so one malformed tool rejects its server's entire
catalog. A real low-level MCP server, served over streamable HTTP in this process, lists one valid
and one malformed tool. The tests connect to it through ``MCPClientManager`` on the static and the
per-user pool paths and check the error operators see, that the breaker records nothing, that the
static health loop retries on its slow cadence, and that a fixed server publishes its catalog.
"""

from __future__ import annotations

import contextlib
import copy
import logging
import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import mcp.types as mcp_types
import pytest
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.applications import Starlette
from starlette.routing import Mount

from tests.conftest import _free_port, _poll_until, _run_on_loop, _wait_tcp_ready, serve_until_exit
from turnstone.core.mcp_client import (
    InvalidCatalogError,
    MCPClientManager,
    _AuthCapture,
    _invalid_page_error,
)
from turnstone.core.oauth.context import OAuthContext

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

GOOD = {"name": "good", "inputSchema": {"type": "object", "properties": {}}}
NO_SCHEMA = {"name": "broken", "description": "lists no input schema"}
FIXED = {**NO_SCHEMA, "inputSchema": {"type": "object"}}
NO_SCHEMA_ERROR = (
    "MCP server 'srv' lists an invalid tool 'broken' at tools[1].inputSchema: Field required"
)


class _Loose(BaseModel):
    """A result the server sends as given, so it can break the protocol's schema."""

    model_config = ConfigDict(extra="allow")


class Upstream:
    """What the server lists, how it pages the list, and what a tool call returns."""

    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = [GOOD, NO_SCHEMA]
        self.page_size = 10
        self.call_result: dict[str, Any] = {"content": [{"type": "text", "text": "ok"}]}
        self.list_requests = 0

    def page(self, cursor: str | None) -> _Loose:
        self.list_requests += 1
        start = int(cursor or 0)
        end = start + self.page_size
        page: dict[str, Any] = {"tools": self.tools[start:end]}
        if end < len(self.tools):
            page["nextCursor"] = str(end)
        return _Loose.model_validate(page)


def _build_server(upstream: Upstream) -> Server[Any, Any]:
    server: Server[Any, Any] = Server("upstream")

    async def _list_tools(request: mcp_types.ListToolsRequest) -> _Loose:
        return upstream.page(request.params.cursor if request.params is not None else None)

    async def _call_tool(request: mcp_types.CallToolRequest) -> _Loose:
        return _Loose.model_validate(upstream.call_result)

    # The server sends whatever a handler returns, so these skip its result types.
    server.request_handlers[mcp_types.ListToolsRequest] = _list_tools  # type: ignore[assignment]
    server.request_handlers[mcp_types.CallToolRequest] = _call_tool  # type: ignore[assignment]
    return server


@pytest.fixture
def upstream_server() -> Iterator[tuple[str, Upstream]]:
    upstream = Upstream()
    sessions = StreamableHTTPSessionManager(app=_build_server(upstream))

    @contextlib.asynccontextmanager
    async def _lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with sessions.run():
            yield

    app = Starlette(routes=[Mount("/", app=sessions.handle_request)], lifespan=_lifespan)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=0,
        )
    )
    thread = threading.Thread(
        target=serve_until_exit, args=(server,), daemon=True, name="invalid-catalog-upstream"
    )
    thread.start()
    try:
        assert _wait_tcp_ready(port, 5.0), "upstream MCP server did not come up"
        yield f"http://127.0.0.1:{port}/mcp", upstream
    finally:
        server.should_exit = True
        server.force_exit = True
        thread.join(timeout=5)


@contextlib.contextmanager
def _started_manager(servers: dict[str, Any], **attrs: float) -> Iterator[MCPClientManager]:
    """A started manager, with *attrs* (timing constants) set before it connects."""
    with patch(
        "turnstone.core.mcp_client.load_config",
        return_value={"static_health_check_seconds": 30},
    ):
        mgr = MCPClientManager(servers)
    for name, value in attrs.items():
        setattr(mgr, name, value)
    mgr.start()
    try:
        yield mgr
    finally:
        mgr.shutdown()


def _static(url: str) -> dict[str, Any]:
    return {"srv": {"type": "http", "url": url}}


def test_static_connect_reports_the_invalid_tool(upstream_server: tuple[str, Upstream]) -> None:
    url, upstream = upstream_server

    with _started_manager(_static(url)) as mgr:
        # Long enough for the health loop's first tick, which would retry at once unprompted.
        time.sleep(0.5)
        status = mgr.get_server_status("srv")
        retry_in = mgr._static_reconnect_next["srv"] - time.monotonic()
        published = mgr.is_mcp_tool("mcp__srv__good")

    assert status["connected"] is False
    assert status["error"] == NO_SCHEMA_ERROR
    assert status["consecutive_failures"] == 0
    assert status["circuit_open"] is False
    assert upstream.list_requests == 1
    assert retry_in > mgr._INVALID_CATALOG_RETRY_S - 10
    assert not published


def test_health_loop_retries_slowly_and_publishes_once_fixed(
    upstream_server: tuple[str, Upstream],
) -> None:
    url, upstream = upstream_server
    # A reconnect backoff this short would retry many times within the half second checked below.
    timing = {
        "_INVALID_CATALOG_RETRY_S": 1.0,
        "_STATIC_RECONNECT_BASE_S": 0.01,
        "_STATIC_RECONNECT_MAX_S": 0.05,
    }

    with _started_manager(_static(url), **timing) as mgr:
        assert _poll_until(lambda: upstream.list_requests == 2, 10), "the health loop never retried"
        time.sleep(0.5)
        assert upstream.list_requests == 2
        failing = mgr.get_server_status("srv")

        upstream.tools = [GOOD, FIXED]
        # Status reads connected while discovery is still running, so wait for the commit.
        assert _poll_until(lambda: mgr.is_mcp_tool("mcp__srv__broken"), 10)
        fixed = mgr.get_server_status("srv")

    assert failing["error"] == NO_SCHEMA_ERROR
    assert failing["consecutive_failures"] == 0
    assert fixed["connected"] is True
    assert fixed["tools"] == 2
    assert fixed["error"] == ""


def test_kept_tools_leave_after_a_reconnect_finds_an_invalid_catalog(
    upstream_server: tuple[str, Upstream],
) -> None:
    """A dispatch to a tool kept from the last good connect reconnects once, not on every call.

    The invalid catalog counts against no breaker, so the breaker cannot cap the reconnects; the
    server's earlier tools leave instead, and the health loop's slow retry brings them back.
    """
    url, upstream = upstream_server
    upstream.tools = [GOOD]

    async def _lose_session() -> None:
        async with mgr._static_connect_lock_for("srv"):
            await mgr._teardown_static_session("srv")

    with _started_manager(_static(url)) as mgr:
        assert mgr._loop is not None
        assert mgr.is_mcp_tool("mcp__srv__good")
        upstream.tools = [GOOD, NO_SCHEMA]
        _run_on_loop(mgr._loop, _lose_session())
        before = upstream.list_requests

        with pytest.raises(RuntimeError, match="reconnect failed: MCP server 'srv' lists"):
            mgr.call_tool_sync("mcp__srv__good", {}, timeout=10)
        with pytest.raises(ValueError, match="Unknown MCP tool"):
            mgr.call_tool_sync("mcp__srv__good", {}, timeout=10)
        reconnects = upstream.list_requests - before
        retry_in = mgr._static_reconnect_next["srv"] - time.monotonic()
        status = mgr.get_server_status("srv")

    assert reconnects == 1
    assert retry_in > mgr._INVALID_CATALOG_RETRY_S - 10
    assert status["error"] == NO_SCHEMA_ERROR
    assert status["consecutive_failures"] == 0


def test_operator_reconnect_schedules_the_slow_retry_and_success_clears_it(
    upstream_server: tuple[str, Upstream],
) -> None:
    url, upstream = upstream_server
    upstream.tools = [GOOD]

    with _started_manager(_static(url)) as mgr:
        upstream.tools = [GOOD, NO_SCHEMA]
        failed = mgr.reconnect_sync("srv")
        retry_in = mgr._static_reconnect_next["srv"] - time.monotonic()
        # As after many slow retries; a session lost right after the fix must not inherit it.
        mgr._static_reconnect_attempt["srv"] = 10
        upstream.tools = [GOOD, FIXED]
        fixed = mgr.reconnect_sync("srv")
        leftover = [
            name
            for name in ("_static_reconnect_next", "_static_reconnect_attempt")
            if "srv" in getattr(mgr, name)
        ]

    assert failed["error"] == NO_SCHEMA_ERROR
    assert retry_in > mgr._INVALID_CATALOG_RETRY_S - 10
    assert fixed["error"] == ""
    assert fixed["tools"] == 2
    assert leftover == []


def test_pool_connect_reports_an_invalid_tool_on_a_later_page(
    upstream_server: tuple[str, Upstream],
) -> None:
    url, upstream = upstream_server
    upstream.page_size = 2
    upstream.tools = [GOOD, {**GOOD, "name": "also_good"}, NO_SCHEMA]
    key = ("user-1", "srv")

    async def _connect() -> None:
        await mgr._connect_one_pool(
            key,
            {"type": "streamable-http", "url": url, "headers": {}},
            "access-token",
            auth_capture=_AuthCapture(),
        )

    with _started_manager({}) as mgr:
        assert mgr._loop is not None
        with pytest.raises(InvalidCatalogError) as caught:
            _run_on_loop(mgr._loop, _connect())
        published = mgr.is_mcp_tool("mcp__srv__good", user_id="user-1")
        failures = mgr._consecutive_failures.get("srv", 0)

    assert str(caught.value) == (
        "MCP server 'srv' lists an invalid tool 'broken' at tools[0].inputSchema: "
        "Field required (page 2)"
    )
    assert upstream.list_requests == 2
    assert not published
    assert failures == 0


def test_pool_prime_shows_the_invalid_tool_in_the_users_status(
    upstream_server: tuple[str, Upstream],
) -> None:
    url, _upstream = upstream_server
    row = {"name": "srv", "transport": "streamable-http", "url": url, "auth_type": "oauth_user"}
    storage = MagicMock()
    storage.get_mcp_server_by_name.return_value = row
    lookup = AsyncMock(return_value=SimpleNamespace(kind="token", token="access-token"))

    with _started_manager({}) as mgr:
        mgr.set_storage(storage)
        mgr.set_app_state(SimpleNamespace(oauth_context=OAuthContext(token_store=MagicMock())))
        mgr._oauth_user_server_names = {"srv"}
        assert mgr._loop is not None
        with patch("turnstone.core.mcp_client.get_user_access_token_classified", new=lookup):
            _run_on_loop(mgr._loop, mgr._prime_user_pools("user-1"))
        status = mgr.get_server_status("srv", user_id="user-1")

    assert status["discovery_error"] == NO_SCHEMA_ERROR
    assert status["tools"] == 0
    assert status["consecutive_failures"] == 0


def test_refresh_keeps_the_last_valid_catalog(upstream_server: tuple[str, Upstream]) -> None:
    url, upstream = upstream_server
    upstream.tools = [GOOD]

    with _started_manager(_static(url)) as mgr:
        assert mgr.get_server_status("srv")["tools"] == 1
        upstream.tools = [GOOD, NO_SCHEMA]
        assert mgr.refresh_sync("srv", timeout=15) == {"srv": None}
        status = mgr.get_server_status("srv")
        # Retried like any failed refresh: the next full pass is what clears the error.
        retry_armed = "srv" in mgr._static_refresh_retry
        result = mgr.call_tool_sync("mcp__srv__good", {}, timeout=10)

    assert status["connected"] is True
    assert status["tools"] == 1
    assert status["error"] == f"Refresh failed: InvalidCatalogError: {NO_SCHEMA_ERROR}"
    assert status["consecutive_failures"] == 0
    assert retry_armed
    assert result == "ok"


def test_invalid_call_result_counts_against_no_breaker(
    upstream_server: tuple[str, Upstream],
) -> None:
    url, upstream = upstream_server
    upstream.tools = [GOOD]
    upstream.call_result = {"content": [{"type": "text"}]}

    with _started_manager(_static(url)) as mgr:
        for _ in range(mgr._CB_FAILURE_THRESHOLD):
            with pytest.raises(ValidationError):
                mgr.call_tool_sync("mcp__srv__good", {}, timeout=10)
        status = mgr.get_server_status("srv")
        upstream.call_result = {"content": [{"type": "text", "text": "ok"}]}
        result = mgr.call_tool_sync("mcp__srv__good", {}, timeout=10)

    assert status["connected"] is True
    assert status["consecutive_failures"] == 0
    assert status["circuit_open"] is False
    assert result == "ok"


def _invalid(result_type: type[BaseModel], payload: dict[str, Any]) -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        result_type.model_validate(payload)
    return caught.value


@pytest.mark.parametrize(
    ("result_type", "kind", "page", "payload", "message"),
    [
        pytest.param(
            mcp_types.ListToolsResult,
            "tools",
            1,
            {"tools": [GOOD, NO_SCHEMA]},
            NO_SCHEMA_ERROR,
            id="missing-field-names-the-entry",
        ),
        pytest.param(
            mcp_types.ListToolsResult,
            "tools",
            1,
            {"tools": [{"inputSchema": {"type": "object"}}]},
            "MCP server 'srv' lists an invalid tool at tools[0].name: Field required",
            id="nameless-entry",
        ),
        pytest.param(
            mcp_types.ListToolsResult,
            "tools",
            3,
            {"tools": [{"name": 5}, {"name": "x", "inputSchema": []}]},
            "MCP server 'srv' lists an invalid tool at tools[0].name: "
            "Input should be a valid string (page 3; 2 more errors)",
            id="wrong-type-on-a-later-page",
        ),
        pytest.param(
            mcp_types.ListToolsResult,
            "tools",
            1,
            {"tools": [{"name": "line\nbreak"}]},
            "MCP server 'srv' lists an invalid tool 'line\\nbreak' at tools[0].inputSchema: "
            "Field required",
            id="name-is-escaped",
        ),
        pytest.param(
            mcp_types.ListToolsResult,
            "tools",
            1,
            {"tools": [{"name": "n" * 100}]},
            f"MCP server 'srv' lists an invalid tool '{'n' * 64}' at tools[0].inputSchema: "
            "Field required",
            id="long-name-is-cut",
        ),
        pytest.param(
            mcp_types.ListResourceTemplatesResult,
            "resource templates",
            1,
            {"resourceTemplates": [{"name": "files"}]},
            "MCP server 'srv' lists an invalid resource template 'files' at "
            "resourceTemplates[0].uriTemplate: Field required",
            id="other-catalog",
        ),
        pytest.param(
            mcp_types.ListToolsResult,
            "tools",
            1,
            {"tools": [GOOD], "nextCursor": 7},
            "MCP server 'srv' sent an invalid tools list at nextCursor: "
            "Input should be a valid string",
            id="page-level-field",
        ),
    ],
)
def test_error_names_the_entry_and_field(
    result_type: type[BaseModel], kind: str, page: int, payload: dict[str, Any], message: str
) -> None:
    error = _invalid_page_error("srv", kind, page, _invalid(result_type, payload))
    assert str(error) == message
    # A copy rebuilds the exception from its message alone, as pickling does.
    assert str(copy.copy(error)) == message
