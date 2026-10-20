"""HTTP failures must reach callers through the real MCP transport (#1223)."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx2
import mcp.types as mcp_types
import pytest
import uvicorn
from mcp import MCPError
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount

from tests.conftest import (
    _free_port,
    _run_on_loop,
    _seed_static_state,
    _wait_tcp_ready,
    serve_until_exit,
)
from turnstone.core.mcp_client import (
    MCPClientManager,
    _AuthCapture,
    _CapturedHTTPError,
    _is_dead_transport,
    _make_capturing_http_client,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from starlette.requests import Request
    from starlette.responses import Response

_METHODS = {"tool": "tools/call", "resource": "resources/read", "prompt": "prompts/get"}
_KEY = ("user-1", "srv")


class Upstream:
    def __init__(self) -> None:
        self.status = 200
        self.protocol_error = False
        self.bare_error = False  # a failure status with no JSON-RPC error body
        self.fail_discovery = False
        self.epoch = 0
        self.sessions: list[str | None] = []
        self.log_levels: list[str | None] = []  # the log level each tools/call asked for


class FailOperations(BaseHTTPMiddleware):
    def __init__(self, app: Any, upstream: Upstream) -> None:
        super().__init__(app)
        self.upstream = upstream

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        if request.method == "POST":
            body = await request.json()
            if body.get("method") in ("initialize", "server/discover"):
                self.upstream.epoch += 1
            if body.get("method") == "tools/call":
                meta = (body.get("params") or {}).get("_meta") or {}
                self.upstream.log_levels.append(meta.get("io.modelcontextprotocol/logLevel"))
            if body.get("method") in _METHODS.values() or self.upstream.fail_discovery:
                self.upstream.sessions.append(
                    request.headers.get("mcp-session-id") or f"stateless-{self.upstream.epoch}"
                )
                if self.upstream.status != 200 or self.upstream.protocol_error:
                    if self.upstream.bare_error:
                        return PlainTextResponse(
                            "no such session", status_code=self.upstream.status
                        )
                    return JSONResponse(
                        {
                            "jsonrpc": "2.0",
                            "id": body["id"],
                            "error": {"code": -32600, "message": "Session not found"},
                        },
                        status_code=self.upstream.status,
                    )
        return await call_next(request)


class RefuseDiscover(BaseHTTPMiddleware):
    """Answers ``server/discover`` as a 2025-11-25 server does, so the client falls back to
    ``initialize`` and the session runs 2025-11-25."""

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        if request.method == "POST":
            body = await request.json()
            if body.get("method") == "server/discover":
                return JSONResponse({"error": "unknown method"}, status_code=404)
        return await call_next(request)


def _build_server() -> Server[Any]:
    async def tools(_ctx: Any, _params: Any) -> mcp_types.ListToolsResult:
        return mcp_types.ListToolsResult(
            tools=[mcp_types.Tool(name="op", input_schema={"type": "object"})]
        )

    async def call_tool(
        _ctx: Any, params: mcp_types.CallToolRequestParams
    ) -> mcp_types.CallToolResult | mcp_types.InputRequiredResult:
        arguments = params.arguments or {}
        if arguments.get("ask"):
            return mcp_types.InputRequiredResult(request_state="awaiting-input")
        if size := arguments.get("size"):
            return mcp_types.CallToolResult(
                content=[mcp_types.TextContent(type="text", text="x" * int(size))],
                is_error=False,
            )
        failed = bool(arguments.get("fail"))
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="boom" if failed else "ok")],
            is_error=failed,
        )

    async def resources(_ctx: Any, _params: Any) -> mcp_types.ListResourcesResult:
        return mcp_types.ListResourcesResult(
            resources=[mcp_types.Resource(name="op", uri="test://resource")]
        )

    async def templates(_ctx: Any, _params: Any) -> mcp_types.ListResourceTemplatesResult:
        return mcp_types.ListResourceTemplatesResult(resource_templates=[])

    async def prompts(_ctx: Any, _params: Any) -> mcp_types.ListPromptsResult:
        return mcp_types.ListPromptsResult(prompts=[mcp_types.Prompt(name="op")])

    async def read_resource(
        _ctx: Any, params: mcp_types.ReadResourceRequestParams
    ) -> mcp_types.ReadResourceResult:
        return mcp_types.ReadResourceResult(
            contents=[mcp_types.TextResourceContents(uri=str(params.uri), text="ok")]
        )

    async def get_prompt(
        _ctx: Any, _params: mcp_types.GetPromptRequestParams
    ) -> mcp_types.GetPromptResult:
        return mcp_types.GetPromptResult(
            messages=[
                mcp_types.PromptMessage(
                    role="user", content=mcp_types.TextContent(type="text", text="ok")
                )
            ]
        )

    return Server(
        "upstream",
        on_list_tools=tools,
        on_call_tool=call_tool,
        on_list_resources=resources,
        on_list_resource_templates=templates,
        on_read_resource=read_resource,
        on_list_prompts=prompts,
        on_get_prompt=get_prompt,
    )


@pytest.fixture
def upstream_server(request: pytest.FixtureRequest) -> Iterator[tuple[str, Upstream]]:
    """A real upstream; parametrized indirectly with "2025-11-25", it refuses ``server/discover`` so
    that sessions run that revision."""
    upstream = Upstream()
    sessions = StreamableHTTPSessionManager(app=_build_server(), json_response=True)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with sessions.run():
            yield

    app = Starlette(routes=[Mount("/", app=sessions.handle_request)], lifespan=lifespan)
    app.add_middleware(FailOperations, upstream=upstream)
    if getattr(request, "param", None) == "2025-11-25":
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
    thread = threading.Thread(
        target=serve_until_exit, args=(server,), daemon=True, name="mcp-http-failures-upstream"
    )
    thread.start()
    try:
        assert _wait_tcp_ready(port, 5), "upstream server did not start"
        yield f"http://127.0.0.1:{port}/mcp", upstream
    finally:
        server.should_exit = True
        server.force_exit = True
        thread.join(timeout=5)


@pytest.fixture(params=[False, True], ids=["static", "pool"])
def dispatch_env(request, upstream_server, monkeypatch):
    url, upstream = upstream_server
    pooled = request.param
    configs = {} if pooled else {"srv": {"type": "http", "url": url}}
    with patch("turnstone.core.mcp_client.load_config", return_value={}):
        mgr = MCPClientManager(configs)
    # No health-loop reconnect may hide the failed dispatch's eviction or breaker count.
    monkeypatch.setattr(mgr, "_static_health_loop", AsyncMock())
    if pooled:
        row = {"name": "srv", "transport": "streamable-http", "url": url, "auth_type": "oauth_user"}
        storage = MagicMock()
        storage.get_mcp_server_by_name.return_value = row
        mgr.set_storage(storage)
        mgr.set_app_state(SimpleNamespace())
        monkeypatch.setattr(
            mgr,
            "_pool_lookup_checked",
            AsyncMock(return_value=(SimpleNamespace(token="access-token"), None)),
        )
    mgr.start()
    try:
        if pooled:

            async def prime():
                entry = await mgr._ensure_pool_entry(_KEY)
                async with entry.open_lock:
                    await mgr._connect_one_pool(
                        _KEY, {"type": "streamable-http", "url": url}, "access-token"
                    )

            # Discovery can open a connection without explicitly supplying a carrier. A later
            # dispatch must still see failures through the hook attached to that same client.
            _run_on_loop(mgr._loop, prime())
        # Establish a session and its catalog before injecting a failure.
        assert mgr.call_tool_sync("mcp__srv__op", {}, user_id="user-1" if pooled else None) == "ok"
        yield mgr, upstream, pooled
    finally:
        mgr.shutdown()


def _call(mgr: MCPClientManager, kind: str, pooled: bool) -> str:
    kwargs = {"user_id": "user-1" if pooled else None, "timeout": 4}
    if kind == "tool":
        return mgr.call_tool_sync("mcp__srv__op", {}, **kwargs)
    if kind == "resource":
        return mgr.read_resource_sync("test://resource", **kwargs)
    return mgr.get_prompt_sync("mcp__srv__op", {}, **kwargs)[0]["content"]


def _session(mgr: MCPClientManager, pooled: bool) -> Any:
    state = mgr._user_pool_entries[_KEY] if pooled else mgr._static_servers["srv"]
    return state.session


def test_tool_error_preserves_error_prefix_and_session(dispatch_env):
    """An isError result crosses the real transport and reaches the agent as an error."""
    mgr, _upstream, pooled = dispatch_env
    session = _session(mgr, pooled)

    output = mgr.call_tool_sync(
        "mcp__srv__op", {"fail": True}, user_id="user-1" if pooled else None, timeout=4
    )

    assert output == "Error: boom"
    assert mgr._consecutive_failures.get("srv", 0) == 0
    assert _session(mgr, pooled) is session
    assert _call(mgr, "tool", pooled) == "ok"
    assert _session(mgr, pooled) is session


def test_a_configured_log_level_reaches_the_server(upstream_server):
    """A 2026-07-28 session asks its server for log messages at the server's configured level on
    each request, and asks for none when no level is set."""
    url, upstream = upstream_server
    for cfg in ({"type": "http", "url": url, "log_level": "warning"}, {"type": "http", "url": url}):
        with patch("turnstone.core.mcp_client.load_config", return_value={}):
            mgr = MCPClientManager({"srv": cfg})
        mgr.start()
        try:
            assert mgr.call_tool_sync("mcp__srv__op", {}, timeout=4) == "ok"
        finally:
            mgr.shutdown()

    assert upstream.log_levels == ["warning", None]


def test_an_input_request_is_breaker_neutral(dispatch_env):
    """A 2026-07-28 server can answer a call with a request for input, which the SDK refuses with
    a RuntimeError. The server answered, so neither the breaker nor the session pays for it."""
    mgr, _upstream, pooled = dispatch_env
    session = _session(mgr, pooled)
    assert session.protocol_version in MODERN_PROTOCOL_VERSIONS
    kwargs = {"user_id": "user-1" if pooled else None, "timeout": 4}

    for _ in range(MCPClientManager._CB_FAILURE_THRESHOLD):
        with pytest.raises(RuntimeError, match="InputRequiredResult"):
            mgr.call_tool_sync("mcp__srv__op", {"ask": True}, **kwargs)

    assert mgr._consecutive_failures.get("srv", 0) == 0
    assert _session(mgr, pooled) is session
    assert _call(mgr, "tool", pooled) == "ok"


@pytest.mark.parametrize("kind", ["tool", "resource", "prompt"])
@pytest.mark.parametrize("status", [404, 502])
def test_http_failure_is_prompt_counted_and_reconnects(dispatch_env, kind, status):
    mgr, upstream, pooled = dispatch_env
    upstream.status = status
    started = time.monotonic()

    with pytest.raises(Exception) as caught:
        _call(mgr, kind, pooled)

    elapsed = time.monotonic() - started
    assert not isinstance(caught.value, TimeoutError), (
        "the SDK stranded the caller until its deadline"
    )
    assert elapsed < 2, f"HTTP {status} took {elapsed:.2f}s to reach the caller"
    assert f"HTTP {status}" in str(caught.value)
    assert mgr._consecutive_failures.get("srv", 0) == 1
    assert _session(mgr, pooled) is None
    assert upstream.sessions[0] == upstream.sessions[1]

    upstream.status = 200
    assert "ok" in _call(mgr, kind, pooled)
    assert upstream.sessions[2] != upstream.sessions[1]
    assert mgr._consecutive_failures.get("srv", 0) == 0


@pytest.mark.parametrize("upstream_server", ["2025-11-25"], indirect=True)
@pytest.mark.parametrize("kind", ["tool", "resource", "prompt"])
def test_a_bare_404_on_a_held_2025_11_25_session_reconnects(dispatch_env, kind):
    """A restarted server answers the held ``mcp-session-id`` with a 404 and no JSON-RPC body.
    The SDK reports its -32600 'Session terminated' stand-in, so only the recorded status says
    the session is gone: it is evicted, one breaker failure is counted, and the next call opens a
    new session."""
    mgr, upstream, pooled = dispatch_env
    assert _session(mgr, pooled).protocol_version == "2025-11-25"
    upstream.status = 404
    upstream.bare_error = True
    started = time.monotonic()

    with pytest.raises(Exception) as caught:
        _call(mgr, kind, pooled)

    elapsed = time.monotonic() - started
    assert elapsed < 2, f"a bare 404 took {elapsed:.2f}s to reach the caller"
    assert "HTTP 404" in str(caught.value)
    assert mgr._consecutive_failures.get("srv", 0) == 1
    assert _session(mgr, pooled) is None
    assert upstream.sessions[0] == upstream.sessions[1]

    upstream.status = 200
    assert "ok" in _call(mgr, kind, pooled)
    assert upstream.sessions[2] != upstream.sessions[1]
    assert mgr._consecutive_failures.get("srv", 0) == 0


@pytest.mark.parametrize("kind", ["tool", "resource", "prompt"])
@pytest.mark.parametrize("status", [200, 400])
def test_http_protocol_error_keeps_session_and_breaker(dispatch_env, kind, status):
    mgr, upstream, pooled = dispatch_env
    original = _session(mgr, pooled)
    upstream.status = status
    upstream.protocol_error = True

    with pytest.raises(MCPError, match="Session not found"):
        _call(mgr, kind, pooled)

    assert mgr._consecutive_failures.get("srv", 0) == 0
    assert _session(mgr, pooled) is original
    upstream.status = 200
    upstream.protocol_error = False
    assert "ok" in _call(mgr, kind, pooled)
    assert upstream.sessions[2] == upstream.sessions[0]


@pytest.mark.parametrize("dispatch_env", [False], indirect=True, ids=["static"])
@pytest.mark.parametrize("kind", ["tool", "resource", "prompt"])
@pytest.mark.parametrize("status", [401, 403])
def test_static_auth_failure_is_prompt_and_breaker_neutral(dispatch_env, kind, status):
    mgr, upstream, pooled = dispatch_env
    upstream.status = status
    started = time.monotonic()

    with pytest.raises(Exception) as caught:
        _call(mgr, kind, pooled)

    assert not isinstance(caught.value, TimeoutError)
    assert f"HTTP {status}" in str(caught.value)
    assert time.monotonic() - started < 2
    assert mgr._consecutive_failures.get("srv", 0) == 0
    assert _session(mgr, pooled) is None


@pytest.mark.parametrize("dispatch_env", [True], indirect=True, ids=["pool"])
def test_pool_reconnect_classifies_its_own_http_failure(dispatch_env):
    mgr, upstream, _pooled = dispatch_env
    upstream.status = 502
    upstream.fail_discovery = True
    entry = mgr._user_pool_entries[_KEY]

    async def reconnect():
        async with entry.open_lock:
            entry.auth_capture.status = 401
            entry.auth_fired_event.set()
            await mgr._connect_one_pool(
                _KEY,
                {
                    "type": "streamable-http",
                    "url": mgr._storage.get_mcp_server_by_name("srv")["url"],
                },
                "access-token",
            )

    with pytest.raises(Exception) as caught:
        _run_on_loop(mgr._loop, reconnect())
    assert mgr._classify_failure(caught.value, capture=entry.auth_capture) == "transport"
    assert entry.auth_capture.status == 502


# 32600: the 1.x SDK's lost-session stand-in, which a proxy built on that SDK can still forward.
@pytest.mark.parametrize("sdk_code", [-32600, -32603, 32600])
@pytest.mark.parametrize(
    ("status", "classification"),
    [(401, "auth_401"), (403, "auth_403"), (404, "transport"), (502, "transport")],
)
def test_http_status_precedes_sdk_protocol_codes(sdk_code, status, classification):
    mgr = MCPClientManager({})
    error = MCPError(code=sdk_code, message="request failed")
    assert mgr._classify_failure(error, capture=_AuthCapture(status=status)) == classification
    assert mgr._classify_failure(error) == "protocol"


@pytest.mark.parametrize(
    ("error_name", "dead"),
    [
        ("ConnectError", True),
        ("ReadError", True),
        ("WriteError", True),
        ("CloseError", True),
        ("ConnectTimeout", True),
        ("ReadTimeout", True),
        ("WriteTimeout", True),
        ("RemoteProtocolError", True),
        ("PoolTimeout", False),
        ("LocalProtocolError", False),
    ],
)
def test_http_exception_families(error_name, dead):
    error = getattr(httpx2, error_name)("failure")
    assert _is_dead_transport(error) is dead
    mgr = MCPClientManager({})
    assert mgr._classify_failure(error) == ("transport" if dead else "other")


@pytest.mark.parametrize(
    ("status", "held", "classification"),
    [
        (401, False, "auth_401"),
        (403, False, "auth_403"),
        (404, True, "transport"),
        (404, False, "other"),
        (502, False, "transport"),
    ],
)
def test_http_status_exception_fallback(status, held, classification):
    request = httpx2.Request(
        "POST", "https://mcp.example.com/", headers={"mcp-session-id": "held"} if held else {}
    )
    response = httpx2.Response(status, request=request)
    error = httpx2.HTTPStatusError("failure", request=request, response=response)
    assert MCPClientManager({})._classify_failure(error) == classification


@pytest.mark.anyio
async def test_hook_keeps_first_failure_and_ignores_cleanup():
    capture = _AuthCapture()
    fired = asyncio.Event()
    async with _make_capturing_http_client(capture, fired) as client:
        hook = client.event_hooks["response"][0]
        cleanup = httpx2.Request("DELETE", "https://mcp.example.com/")
        await hook(httpx2.Response(502, request=cleanup))
        assert capture.status is None
        assert not fired.is_set()
        request = httpx2.Request("POST", "https://mcp.example.com/")
        await hook(httpx2.Response(401, request=request, headers={"www-authenticate": "Bearer"}))
        await hook(httpx2.Response(502, request=request))
        assert capture.status == 401
        assert capture.www_authenticate == "Bearer"
        assert fired.is_set()


@pytest.mark.anyio
async def test_connection_client_sends_its_headers_and_keeps_the_sdk_timeouts():
    """A connection's client sends the headers it is built with, and waits 30 s to connect, write
    or get a pooled connection and 300 s for a read, since a server may hold a stream open."""
    client = _make_capturing_http_client(_AuthCapture(), headers={"X-Api-Key": "key-1"})
    async with client:
        assert client.headers["x-api-key"] == "key-1"
        assert client.timeout == httpx2.Timeout(30.0, read=300.0)


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["http", "owner", "cancel"])
async def test_dispatch_race_reaps_call_and_leaves_owner_lifecycle_alone(outcome):
    mgr = MCPClientManager({})
    capture = _AuthCapture()
    fired = asyncio.Event()
    owner_exit = asyncio.Event()
    started = asyncio.Event()
    reaped = asyncio.Event()

    async def operation():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            reaped.set()

    owner = asyncio.create_task(owner_exit.wait())
    dispatch = asyncio.create_task(
        mgr._await_session_call(operation(), owner=owner, capture=capture, fired_event=fired)
    )
    try:
        async with asyncio.timeout(2):
            await started.wait()
            if outcome == "http":
                capture.status = 502
                fired.set()
            elif outcome == "owner":
                owner_exit.set()
            else:
                dispatch.cancel()
            error_type = asyncio.CancelledError if outcome == "cancel" else ConnectionError
            with pytest.raises(error_type):
                await dispatch
        assert reaped.is_set()
        assert owner.done() is (outcome == "owner")
        assert not owner.cancelled()
    finally:
        dispatch.cancel()
        owner.cancel()
        await asyncio.gather(dispatch, owner, return_exceptions=True)


@pytest.mark.anyio
async def test_static_overlapping_calls_keep_the_failed_connection_capture():
    mgr = MCPClientManager({})
    session = object()
    state = _seed_static_state(mgr, "srv", session=session)
    state.http_capture = _AuthCapture()
    state.http_fired_event = asyncio.Event()
    both_started = asyncio.Event()
    started = 0

    async def operation():
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await asyncio.Event().wait()

    calls = [asyncio.create_task(mgr._static_session_op("srv", operation())) for _ in range(2)]
    try:
        async with asyncio.timeout(2):
            await both_started.wait()
            assert state.in_flight == 2
            capture = state.http_capture
            capture.status = 502
            state.http_fired_event.set()
            # A reconnect replaces the carrier; neither in-flight operation can read its status.
            state.http_capture = _AuthCapture(status=401)
            state.http_fired_event = asyncio.Event()
            for call in calls:
                with pytest.raises(_CapturedHTTPError, match="HTTP 502") as caught:
                    await call
                assert mgr._classify_failure(caught.value) == "transport"
            assert state.in_flight == 0
    finally:
        for call in calls:
            call.cancel()
        await asyncio.gather(*calls, return_exceptions=True)


def test_static_failure_does_not_evict_a_replacement_session():
    mgr = MCPClientManager({})
    original, replacement = object(), object()
    state = _seed_static_state(mgr, "srv", session=replacement)
    error = _CapturedHTTPError(_AuthCapture(status=502))
    mgr._record_and_evict_on_dead_transport("srv", error, session=original)
    assert state.session is replacement
    assert mgr._consecutive_failures["srv"] == 1


@pytest.mark.anyio
@pytest.mark.parametrize("replacement", [None, object()], ids=["connecting", "connected"])
async def test_stale_static_dispatch_does_not_borrow_replacement_status(replacement):
    mgr = MCPClientManager({})
    original = object()
    state = _seed_static_state(mgr, "srv", session=replacement)
    state.http_capture = _AuthCapture(status=401)
    called = False

    async def operation():
        nonlocal called
        called = True

    with pytest.raises(ConnectionError, match="session changed") as caught:
        await mgr._static_session_op("srv", operation(), session=original)
    assert mgr._classify_failure(caught.value) == "transport"
    assert not called
    assert state.in_flight == 0


@pytest.mark.anyio
@pytest.mark.parametrize("succeeded", [True, False], ids=["result", "sdk-error"])
async def test_completed_call_wins_but_failed_call_uses_http_status(succeeded):
    mgr = MCPClientManager({})
    capture = _AuthCapture()
    fired = asyncio.Event()

    async def operation():
        capture.status = 502
        fired.set()
        if not succeeded:
            raise MCPError(code=-32603, message="Server error response")
        return "completed result"

    call = mgr._await_session_call(operation(), owner=None, capture=capture, fired_event=fired)
    if succeeded:
        assert await call == "completed result"
    else:
        with pytest.raises(_CapturedHTTPError, match="HTTP 502"):
            await call


def test_large_sse_tool_result_exceeding_sdk_default_1mb_limit() -> None:
    """A result over the SDK's 1 MiB default SSE event size arrives, up to the 16 MiB we set
    (#679 S8). Only SSE-framed answers are capped, so the server refuses server/discover: the
    session is then 2025-11-25 and gets its results as SSE events, where a 2026-07-28 session
    would get plain JSON."""
    sessions = StreamableHTTPSessionManager(app=_build_server(), json_response=False)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with sessions.run():
            yield

    app = Starlette(routes=[Mount("/", app=sessions.handle_request)], lifespan=lifespan)
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
    thread = threading.Thread(
        target=serve_until_exit, args=(server,), daemon=True, name="mcp-sse-large-payload"
    )
    thread.start()
    try:
        assert _wait_tcp_ready(port, 5), "SSE upstream server did not start"
        url = f"http://127.0.0.1:{port}/mcp"
        with patch("turnstone.core.mcp_client.load_config", return_value={}):
            mgr = MCPClientManager({"srv": {"type": "http", "url": url}})
        mgr.start()
        try:
            assert mgr._static_servers["srv"].session.protocol_version == "2025-11-25"
            size = 2 * 1024 * 1024 + 1024  # >2 MiB, well over the SDK's 1 MiB default
            output = mgr.call_tool_sync("mcp__srv__op", {"size": size}, timeout=15)
            assert len(output) == size
            assert output == "x" * size
            assert mgr._consecutive_failures.get("srv", 0) == 0
        finally:
            mgr.shutdown()
    finally:
        server.should_exit = True
        server.force_exit = True
        thread.join(timeout=5)
