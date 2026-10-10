"""Connection tests and connect-failure wording (#1328), through the real MCP transport."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import ipaddress
import json
import logging
import socket
import threading
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import httpx
import mcp.types as mcp_types
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from mcp import McpError
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tests.conftest import _free_port, _poll_until, serve_asgi
from turnstone.console.server import admin_test_mcp_server
from turnstone.core.auth import AuthResult
from turnstone.core.mcp_client import (
    ConnectFailure,
    MCPClientManager,
    MCPShutdownError,
    ServerUnreachableError,
    _AuthCapture,
    _ErrorAnswerError,
    _make_capturing_http_factory,
    _NotMcpResponseError,
    _sanitize_error_text,
    classify_connect_failure,
    probe_http_server,
    probe_http_server_max_s,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from starlette.requests import Request

GOOD = {"name": "good", "inputSchema": {"type": "object"}}
NO_SCHEMA = {"name": "broken"}
TOKEN = "t0ken-for-the-test"


class Upstream:
    def __init__(self) -> None:
        self.bearer: str | None = None  # the Authorization bearer required, when set
        self.seen_auth: list[str | None] = []
        self.drop_sessions = False  # answer a catalog list on a held session with a 404
        self.fail_method: tuple[str, int] | None = None  # answer this method with this status
        self.stall_method: str | None = None  # hold this method's answer until released
        self.stalled = threading.Event()
        self.release = threading.Event()
        self.trickle_end = False  # answer the request that ends a session a byte at a time


async def _trickle(release: threading.Event) -> AsyncIterator[bytes]:
    for _ in range(400):  # 20 s at most
        if release.is_set():
            return
        yield b" "
        await asyncio.sleep(0.05)


class _Gate(BaseHTTPMiddleware):
    """Misbehaves by path: a same-origin redirect, an HTML page, a REST API's JSON, a JSON-RPC
    error with a null id, a blank answer, an outage, a malformed catalog. And by state: a bearer
    check, a dropped session, a method answered with an error status, a method that never answers,
    a session end answered a byte at a time."""

    def __init__(self, app: Any, upstream: Upstream) -> None:
        super().__init__(app)
        self.upstream = upstream

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        path = request.url.path
        if path.startswith("/r/"):
            return Response(status_code=307, headers={"location": path[2:]})
        if path == "/docs":
            return HTMLResponse("<html><body>Read the docs</body></html>")
        if path == "/api":
            return JSONResponse({"name": "a REST API", "version": "1"})
        if path == "/rpc-error":
            error = {"code": -32600, "message": "Invalid Request: bad protocol version"}
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": error})
        if request.method == "DELETE" and path == "/mcp" and self.upstream.trickle_end:
            return StreamingResponse(_trickle(self.upstream.release))
        if path == "/unavailable":
            return Response("down for maintenance", status_code=503)
        if path == "/blank":
            return Response(b"")  # 200 with no content type
        if request.method == "POST" and path in ("/mcp", "/badtool"):
            auth = request.headers.get("authorization")
            self.upstream.seen_auth.append(auth)
            if self.upstream.bearer is not None and auth != f"Bearer {self.upstream.bearer}":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            body = await request.json()
            method = body.get("method")
            held = request.headers.get("mcp-session-id")
            if self.upstream.drop_sessions and held and method == "tools/list":
                error = {"code": -32600, "message": "Session not found"}
                return JSONResponse({"jsonrpc": "2.0", "id": body["id"], "error": error}, 404)
            if self.upstream.fail_method is not None and method == self.upstream.fail_method[0]:
                return Response("refused", status_code=self.upstream.fail_method[1])
            if method is not None and method == self.upstream.stall_method:
                self.upstream.stalled.set()
                for _ in range(600):
                    if self.upstream.release.is_set():
                        break
                    await asyncio.sleep(0.05)
            if path == "/badtool" and method == "tools/list":
                result = {"tools": [GOOD, NO_SCHEMA]}
                return JSONResponse({"jsonrpc": "2.0", "id": body["id"], "result": result})
        return await call_next(request)


class _McpApp:
    def __init__(self, sessions: StreamableHTTPSessionManager) -> None:
        self.sessions = sessions

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self.sessions.handle_request(scope, receive, send)


def _build_server() -> Server[Any, Any]:
    server: Server[Any, Any] = Server("upstream")

    @server.list_tools()
    async def tools() -> list[mcp_types.Tool]:
        return [
            mcp_types.Tool(name="alpha", inputSchema={"type": "object"}),
            mcp_types.Tool(name="beta", inputSchema={"type": "object"}),
        ]

    @server.list_resources()
    async def resources() -> list[mcp_types.Resource]:
        return [mcp_types.Resource(name="doc", uri="test://doc")]

    @server.list_resource_templates()
    async def templates() -> list[mcp_types.ResourceTemplate]:
        return [mcp_types.ResourceTemplate(name="page", uriTemplate="test://page/{n}")]

    @server.list_prompts()
    async def prompts() -> list[mcp_types.Prompt]:
        return [mcp_types.Prompt(name="greet")]

    return server


@contextlib.contextmanager
def _serve(upstream: Upstream, **ssl: str) -> Iterator[int]:
    sessions = StreamableHTTPSessionManager(app=_build_server(), json_response=True)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> Any:
        async with sessions.run():
            yield

    mcp_app = _McpApp(sessions)
    methods = ["GET", "POST", "DELETE"]
    routes = [Route(path, endpoint=mcp_app, methods=methods) for path in ("/mcp", "/badtool")]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(_Gate, upstream=upstream)
    with serve_asgi(app, **ssl) as port:
        yield port


@pytest.fixture
def upstream() -> Iterator[tuple[str, Upstream]]:
    state = Upstream()
    with _serve(state) as port:
        try:
            yield f"http://127.0.0.1:{port}", state
        finally:
            state.release.set()


def _self_signed(tmp_path: Path) -> dict[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return {"ssl_certfile": str(cert_path), "ssl_keyfile": str(key_path)}


@contextlib.contextmanager
def _silent_server() -> Iterator[int]:
    """Accepts TCP connections and never answers on them."""
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.05)  # closing the socket does not wake a blocked accept()
    accepted: list[socket.socket] = []
    stop = threading.Event()

    def _accept() -> None:
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                conn, _ = listener.accept()
                accepted.append(conn)

    thread = threading.Thread(target=_accept, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stop.set()
        thread.join(timeout=5)
        listener.close()
        for conn in accepted:
            conn.close()


# ---------------------------------------------------------------------------
# probe_http_server, through the real transport
# ---------------------------------------------------------------------------


def test_lists_the_catalog_with_the_given_headers(upstream: tuple[str, Upstream]) -> None:
    base, state = upstream
    state.bearer = TOKEN

    probe = probe_http_server(f"{base}/mcp", {"Authorization": f"Bearer {TOKEN}"}, name="srv")

    assert probe.failure is None
    assert probe.tools == ["alpha", "beta"]
    assert probe.resources == 2  # the resource and the template, as the status counts them
    assert probe.prompts == 1
    assert set(state.seen_auth) == {f"Bearer {TOKEN}"}


def test_rejected_credentials_report_the_status_and_never_the_header(
    upstream: tuple[str, Upstream],
) -> None:
    base, state = upstream
    state.bearer = TOKEN

    probe = probe_http_server(f"{base}/mcp", {"Authorization": "Bearer wr0ng"}, name="srv")

    assert probe.failure == ConnectFailure(
        "http", "HTTP 401 Unauthorized: the server requires authorization", 401
    )
    assert probe.tools == []


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/nope", ConnectFailure("http", "HTTP 404 Not Found: no MCP endpoint at this URL", 404)),
        ("/unavailable", ConnectFailure("http", "HTTP 503 Service Unavailable", 503)),
        (
            "/badtool",
            ConnectFailure(
                "invalid_catalog",
                "MCP server 'srv' lists an invalid tool 'broken' at tools[1].inputSchema: "
                "Field required",
            ),
        ),
    ],
    ids=["wrong-path", "server-error", "invalid-tool"],
)
def test_server_answers_are_classified(
    upstream: tuple[str, Upstream], path: str, expected: ConnectFailure
) -> None:
    base, _ = upstream
    assert probe_http_server(f"{base}{path}", name="srv").failure == expected


def test_a_web_page_fails_at_once(upstream: tuple[str, Upstream]) -> None:
    """The SDK alone leaves the request waiting out the connect timeout."""
    base, _ = upstream
    started = time.monotonic()

    probe = probe_http_server(f"{base}/docs", name="srv")

    assert probe.failure == ConnectFailure(
        "not_mcp", "Not an MCP endpoint: the server answered with text/html"
    )
    assert time.monotonic() - started < MCPClientManager._CONNECT_TIMEOUT / 3


def test_a_blank_answer_names_the_missing_content_type(upstream: tuple[str, Upstream]) -> None:
    base, _ = upstream
    assert probe_http_server(f"{base}/blank", name="srv").failure == ConnectFailure(
        "not_mcp", "Not an MCP endpoint: the server answered with no content type"
    )


def test_a_rest_api_root_fails_at_once(upstream: tuple[str, Upstream]) -> None:
    """JSON that is not a JSON-RPC message: the SDK alone logs a parse error and waits."""
    base, _ = upstream
    started = time.monotonic()

    probe = probe_http_server(f"{base}/api", name="srv")

    assert probe.failure == ConnectFailure(
        "not_mcp",
        "Not an MCP endpoint: the server answered with application/json that is not a JSON-RPC "
        "message",
    )
    assert time.monotonic() - started < MCPClientManager._CONNECT_TIMEOUT / 3


def test_an_error_the_sdk_cannot_read_is_reported_at_once(upstream: tuple[str, Upstream]) -> None:
    """JSON-RPC 2.0 answers a request it cannot read with an error whose id is null, which the
    SDK's schema refuses: the SDK alone logs a parse error and waits. The server's error is what
    the operator needs."""
    base, _ = upstream
    started = time.monotonic()

    probe = probe_http_server(f"{base}/rpc-error", name="srv")

    assert probe.failure == ConnectFailure(
        "protocol",
        "The server answered with MCP error -32600: Invalid Request: bad protocol version",
    )
    assert time.monotonic() - started < MCPClientManager._CONNECT_TIMEOUT / 3


@pytest.mark.parametrize("status", [401, 503])
def test_an_error_status_on_a_notification_is_reported(
    upstream: tuple[str, Upstream], status: int
) -> None:
    """The SDK ignores only a notification's 404; any other status ends the transport, and the
    status is what the operator sees, not the closed stream it leaves behind."""
    base, state = upstream
    state.fail_method = ("notifications/initialized", status)

    failure = probe_http_server(f"{base}/mcp", name="srv").failure

    assert failure is not None
    assert (failure.kind, failure.status) == ("http", status)
    assert failure.message.startswith(f"HTTP {status} ")


DROPPED = ConnectFailure(
    "http",
    "HTTP 404 Not Found: the server dropped the session it had just opened (a restart, or "
    "several replicas without session affinity)",
    404,
)


def test_a_session_dropped_after_initialize_is_not_a_wrong_url(
    upstream: tuple[str, Upstream],
) -> None:
    """A 404 on a request carrying the session's id, which replicas behind a load balancer
    without session affinity answer: the URL is right."""
    base, state = upstream
    state.drop_sessions = True
    assert probe_http_server(f"{base}/mcp", name="srv").failure == DROPPED


@pytest.mark.parametrize(
    ("path", "drop_sessions", "expected"),
    [
        (
            "/r/docs",
            False,
            ConnectFailure("not_mcp", "Not an MCP endpoint: the server answered with text/html"),
        ),
        ("/r/mcp", True, DROPPED),
    ],
    ids=["web-page", "dropped-session"],
)
def test_a_same_origin_redirect_keeps_the_answer_checks(
    upstream: tuple[str, Upstream], path: str, drop_sessions: bool, expected: ConnectFailure
) -> None:
    """httpx sends a redirected request with its body unread; the hooks still read it."""
    base, state = upstream
    state.drop_sessions = drop_sessions
    started = time.monotonic()

    assert probe_http_server(f"{base}{path}", name="srv").failure == expected
    assert time.monotonic() - started < MCPClientManager._CONNECT_TIMEOUT / 3


def test_an_untrusted_certificate_reports_tls(tmp_path: Path) -> None:
    with _serve(Upstream(), **_self_signed(tmp_path)) as port:
        probe = probe_http_server(f"https://127.0.0.1:{port}/mcp", name="srv")

    assert probe.failure == ConnectFailure(
        "tls", "TLS handshake failed: certificate verify failed: self-signed certificate"
    )


def test_a_closed_port_is_unreachable() -> None:
    port = _free_port()

    probe = probe_http_server(f"http://127.0.0.1:{port}/mcp")

    assert probe.failure is not None
    assert probe.failure.kind == "unreachable"
    # With no name, the URL's host labels the server.
    assert probe.failure.message.startswith(
        f"MCP server '127.0.0.1' unreachable at 127.0.0.1:{port}"
    )


def test_a_silent_server_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MCPClientManager, "_CONNECT_TIMEOUT", 1)
    with _silent_server() as port:
        probe = probe_http_server(f"http://127.0.0.1:{port}/mcp", name="srv")

    assert probe.failure == ConnectFailure("timeout", "The server did not answer in time")


def test_a_slow_session_end_cannot_hold_the_test_past_its_bound(
    upstream: tuple[str, Upstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK bounds each read of the request that ends the session, not the request: a server
    that trickles its answer would otherwise keep the test, and its console slot, for as long as
    it keeps sending."""
    base, state = upstream
    state.trickle_end = True
    monkeypatch.setattr(MCPClientManager, "_CONNECT_TIMEOUT", 3)
    monkeypatch.setattr(MCPClientManager, "_OWNER_CANCEL_GRACE_S", 0.5)
    monkeypatch.setattr("turnstone.core.mcp_client._SESSION_END_TIMEOUT_S", 1.0)
    monkeypatch.setattr("turnstone.core.mcp_client._PROBE_TEARDOWN_S", 0.5)
    started = time.monotonic()

    probe = probe_http_server(f"{base}/mcp", name="srv")

    assert probe.failure == ConnectFailure("timeout", "The server did not answer in time")
    assert time.monotonic() - started < probe_http_server_max_s() + 1


def test_a_host_that_cannot_be_looked_up_is_named() -> None:
    """A host with an empty label fails its encoding for the lookup: the host is the problem,
    and the test and a saved server's status say so."""
    url = "http://mcp..example.com/mcp"
    probe = probe_http_server(url, name="srv")
    mgr = _manager({}, health_check_s=0)
    mgr.start()
    try:
        result = mgr.add_server_sync("srv", {"type": "streamable-http", "url": url})
    finally:
        mgr.shutdown()

    assert probe.failure is not None
    assert probe.failure.kind == "unreachable"
    assert probe.failure.message.startswith("MCP server 'srv' unreachable at mcp..example.com:80")
    assert result["error"] == probe.failure.message


# ---------------------------------------------------------------------------
# classify_connect_failure
# ---------------------------------------------------------------------------


def _mcp_error(code: int, message: str) -> McpError:
    return McpError(mcp_types.ErrorData(code=code, message=message))


def _status_error(status: int, reason: str, text: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://mcp.example/mcp")
    response = httpx.Response(
        status, request=request, extensions={"reason_phrase": reason.encode()}
    )
    return httpx.HTTPStatusError(text or f"{status} {reason}", request=request, response=response)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        pytest.param(
            BaseExceptionGroup(
                "transport",
                [
                    BaseExceptionGroup("inner", [asyncio.CancelledError()]),
                    BaseExceptionGroup(
                        "inner", [asyncio.CancelledError(), _mcp_error(-32602, "bad version")]
                    ),
                ],
            ),
            ConnectFailure("protocol", "The server answered with MCP error -32602: bad version"),
            id="failure-beside-cancellations",
        ),
        pytest.param(
            BaseExceptionGroup("wedged transport", [asyncio.CancelledError()]),
            ConnectFailure("error", "BaseExceptionGroup: wedged transport (1 sub-exception)"),
            id="cancellations-only",
        ),
        pytest.param(
            _mcp_error(32600, "Session terminated"),
            ConnectFailure("http", "HTTP 404 Not Found: no MCP endpoint at this URL", 404),
            id="sdk-synthesized-404",
        ),
        pytest.param(
            _mcp_error(mcp_types.CONNECTION_CLOSED, "Connection closed"),
            ConnectFailure("error", "The connection closed before the server answered"),
            id="connection-closed",
        ),
        pytest.param(
            BaseExceptionGroup(
                "transport",
                [
                    _mcp_error(mcp_types.CONNECTION_CLOSED, "Connection closed"),
                    httpx.RemoteProtocolError("Server disconnected without sending a response."),
                ],
            ),
            ConnectFailure(
                "error", "Connection failed: Server disconnected without sending a response."
            ),
            id="a-closed-connection-yields-to-its-cause",
        ),
        pytest.param(
            _status_error(
                302,
                "Found",
                "Redirect response '302 Found' for url 'http://u:pw@mcp.example/mcp?token=s3cret'",
            ),
            ConnectFailure("http", "HTTP 302 Found", 302),
            id="redirect-with-no-location",  # httpx's text, naming the whole URL
        ),
        pytest.param(
            TimeoutError(),
            ConnectFailure("timeout", "The server did not answer in time"),
            id="bare-timeout",
        ),
        pytest.param(
            TimeoutError("MCP server 'srv' registration timed out"),
            ConnectFailure("timeout", "MCP server 'srv' registration timed out"),
            id="worded-timeout",
        ),
        pytest.param(
            httpx.ReadTimeout(""),
            ConnectFailure("timeout", "The server did not answer in time"),
            id="client-timeout",
        ),
        pytest.param(
            httpx.ConnectError("All connection attempts failed"),
            ConnectFailure("unreachable", "Connection failed: All connection attempts failed"),
            id="connect-error",
        ),
        pytest.param(
            httpx.RemoteProtocolError("Server disconnected without sending a response."),
            ConnectFailure(
                "error", "Connection failed: Server disconnected without sending a response."
            ),
            id="dropped-after-connecting",
        ),
        pytest.param(
            MCPShutdownError(),
            ConnectFailure("error", "MCP client is shutting down"),
            id="shutdown",
        ),
        pytest.param(
            ConnectionError("refused"),
            ConnectFailure("error", "ConnectionError: refused"),
            id="unrecognized-keeps-its-type",
        ),
        pytest.param(
            BaseExceptionGroup(
                "transport", [anyio.ClosedResourceError(), _status_error(401, "Unauthorized")]
            ),
            ConnectFailure("http", "HTTP 401 Unauthorized: the server requires authorization", 401),
            id="recognized-beats-a-teardown-side-effect",
        ),
        pytest.param(
            BaseExceptionGroup(
                "transport",
                [
                    anyio.ClosedResourceError(),
                    httpx.RemoteProtocolError("Server disconnected without sending a response."),
                ],
            ),
            ConnectFailure(
                "error", "Connection failed: Server disconnected without sending a response."
            ),
            id="a-plain-error-also-beats-a-side-effect",
        ),
    ],
)
def test_classifier(exc: BaseException, expected: ConnectFailure) -> None:
    assert classify_connect_failure(exc) == expected


@pytest.mark.parametrize(
    "exc",
    [
        httpx.LocalProtocolError("Illegal header value b'sk-live-SECRET123 '"),
        UnicodeEncodeError("ascii", "sk-live-SECRET" + chr(0xE9), 14, 15, "not in range"),
    ],
    ids=["whitespace", "outside-ascii"],
)
def test_a_refused_header_is_never_quoted(exc: BaseException) -> None:
    failure = classify_connect_failure(BaseExceptionGroup("transport", [exc]))
    assert failure.kind == "error"
    assert failure.message.startswith("A header is not valid HTTP")
    assert "SECRET" not in failure.message


def test_only_a_refused_header_is_worded_as_one() -> None:
    """h11 refuses a framing header it cannot honour with text of its own, and an encoding error
    from elsewhere (a stdio argument) is no header at all."""
    framing = httpx.LocalProtocolError("Only Transfer-Encoding: chunked is supported")
    assert classify_connect_failure(framing) == ConnectFailure(
        "error", "Connection failed: Only Transfer-Encoding: chunked is supported"
    )
    surrogate = UnicodeEncodeError("utf-8", "arg" + chr(0xD800), 3, 4, "surrogates not allowed")
    failure = classify_connect_failure(surrogate)
    assert failure.message.startswith("UnicodeEncodeError: 'utf-8' codec")
    assert not failure.recognized


def test_only_an_unknown_exception_is_unrecognized() -> None:
    """The flag a caller reads to log a traceback; a deliberate plain error is recognized."""
    assert not classify_connect_failure(ConnectionError("refused")).recognized
    assert classify_connect_failure(MCPShutdownError()).recognized
    assert classify_connect_failure(httpx.RemoteProtocolError("closed")).recognized
    assert classify_connect_failure(_mcp_error(mcp_types.CONNECTION_CLOSED, "closed")).recognized


@pytest.mark.parametrize(
    ("exc", "first_error", "expected"),
    [
        pytest.param(
            _mcp_error(-32603, "Internal error"),
            (502, False),
            ConnectFailure("http", "HTTP 502 Bad Gateway", 502),
            id="generic-mcp-error",
        ),
        pytest.param(
            _mcp_error(-32600, "Session not found"),
            (404, False),
            ConnectFailure("http", "HTTP 404 Not Found: no MCP endpoint at this URL", 404),
            id="404-on-initialize",
        ),
        pytest.param(
            _mcp_error(32600, "Session terminated"),
            (404, True),
            DROPPED,
            id="404-on-a-held-session",
        ),
        pytest.param(
            _status_error(403, "Forbidden"),
            (403, False),
            ConnectFailure("http", "HTTP 403 Forbidden: the server refused access", 403),
            id="status-error",
        ),
        pytest.param(
            ServerUnreachableError("MCP server 'srv' unreachable at h:80: refused"),
            (500, False),
            ConnectFailure("unreachable", "MCP server 'srv' unreachable at h:80: refused"),
            id="older-status-cannot-explain-another-failure",
        ),
        pytest.param(
            ConnectionError("MCP transport owner died during discovery"),
            (401, True),
            ConnectFailure("http", "HTTP 401 Unauthorized: the server requires authorization", 401),
            id="transport-died-during-discovery",
        ),
        pytest.param(
            BaseExceptionGroup("transport", [anyio.BrokenResourceError()]),
            (503, True),
            ConnectFailure("http", "HTTP 503 Service Unavailable", 503),
            id="closed-stream",
        ),
        pytest.param(
            _mcp_error(mcp_types.CONNECTION_CLOSED, "Connection closed"),
            (500, True),
            ConnectFailure("http", "HTTP 500 Internal Server Error", 500),
            id="connection-closed",
        ),
    ],
)
def test_classifier_words_http_failures_from_the_recorded_status(
    exc: BaseException, first_error: tuple[int, bool], expected: ConnectFailure
) -> None:
    """The status off the wire, whatever exception the SDK version raises for it (a status
    error, a synthesized 32600, or the generic -32603 an SDK may report instead)."""
    capture = _AuthCapture(first_error=first_error)
    assert classify_connect_failure(exc, capture) == expected


@pytest.mark.parametrize("closed_first", [True, False], ids=["closed-first", "closed-last"])
def test_a_closed_connection_never_outranks_another_cause(closed_first: bool) -> None:
    """A closed connection is what a teardown leaves behind; the recorded status explains it only
    when nothing else does, in whichever order the task group holds them."""
    leaves: list[BaseException] = [
        _mcp_error(mcp_types.CONNECTION_CLOSED, "Connection closed"),
        TimeoutError(),
    ]
    if not closed_first:
        leaves.reverse()
    capture = _AuthCapture(first_error=(503, True))

    failure = classify_connect_failure(BaseExceptionGroup("transport", leaves), capture)

    assert failure == ConnectFailure("timeout", "The server did not answer in time")


NOTIFICATION = {"jsonrpc": "2.0", "method": "notifications/initialized"}
ANSWER = {"jsonrpc": "2.0", "id": 7, "result": {}}  # the client's answer to a server's ping
REQUEST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


@pytest.mark.parametrize(
    ("answered", "first_error"),
    [
        ([(404, NOTIFICATION), (404, ANSWER)], None),
        ([(404, NOTIFICATION), (500, REQUEST)], (500, True)),
        ([(503, NOTIFICATION)], (503, True)),
        ([(401, ANSWER)], (401, True)),
        ([(404, REQUEST)], (404, True)),
    ],
    ids=["ignored-404s", "first-failure", "notification", "answer", "request-404"],
)
@pytest.mark.anyio
async def test_the_carrier_records_what_ends_the_transport(
    answered: list[tuple[int, dict[str, Any]]], first_error: tuple[int, bool] | None
) -> None:
    """As the SDK does: a 404 on a notification or on the client's own answer is ignored, and
    any other error status ends the transport, whatever the message."""
    capture = _AuthCapture()
    client = _make_capturing_http_factory(capture)()
    try:
        record = client.event_hooks["response"][0]
        held = {"mcp-session-id": "s1"}
        for status, body in answered:
            request = httpx.Request("POST", "http://mcp.example/mcp", json=body, headers=held)
            await record(httpx.Response(status, request=request))
    finally:
        await client.aclose()

    assert capture.first_error == first_error


@pytest.mark.anyio
async def test_the_initialize_check_leaves_later_answers_alone() -> None:
    """Only initialize fails fast: a live session's later answers keep the SDK's handling."""
    client = _make_capturing_http_factory(_AuthCapture())()
    reject = client.event_hooks["response"][1]

    def answered(method: str, content_type: str, body: bytes = b"<html></html>") -> httpx.Response:
        sent = {"jsonrpc": "2.0", "id": 1, "method": method}
        request = httpx.Request("POST", "http://mcp.example/mcp", json=sent)
        return httpx.Response(
            200, request=request, headers={"content-type": content_type}, content=body
        )

    error = b'{"jsonrpc": "2.0", "id": null, "error": {"code": -32600, "message": "Invalid"}}'
    # JSON-RPC's error code is an integer: an envelope with any other is no JSON-RPC message.
    worded = b'{"jsonrpc": "2.0", "id": null, "error": {"code": "E1", "message": "Invalid"}}'
    try:
        await reject(answered("tools/list", "text/html"))
        await reject(answered("tools/list", "application/json", b'{"status": "ok"}'))
        valid = b'{"jsonrpc": "2.0", "id": 1, "result": {}}'
        await reject(answered("initialize", "application/json", valid))
        with pytest.raises(_NotMcpResponseError) as page:
            await reject(answered("initialize", "text/html; charset=utf-8"))
        with pytest.raises(_NotMcpResponseError) as rest:
            await reject(answered("initialize", "application/json", b'{"status": "ok"}'))
        with pytest.raises(_ErrorAnswerError) as unread:
            await reject(answered("initialize", "application/json", error))
        with pytest.raises(_NotMcpResponseError):
            await reject(answered("initialize", "application/json", worded))
    finally:
        await client.aclose()

    assert page.value.answer == "text/html"
    assert rest.value.answer == "application/json that is not a JSON-RPC message"
    assert (unread.value.code, unread.value.message) == (-32600, "Invalid")


def test_an_error_text_reads_on_one_line_with_no_control_characters() -> None:
    """A server's error text could rewrite the terminal that shows the log, or forge a line."""
    esc, nel, line_sep = chr(0x1B), chr(0x85), chr(0x2028)
    text = f"MCP error 1: {esc}[2Jfake{nel}line{line_sep}two\r\nthree"

    assert _sanitize_error_text(text) == "MCP error 1:  [2Jfake line two three"


def test_recorded_wording_is_one_line() -> None:
    redirect = _status_error(
        307, "Temporary Redirect", "Redirect to http://mcp.example/mcp\nnot followed"
    )
    text = MCPClientManager({})._connect_failure_text("srv", redirect)
    assert text == ("HTTP 307 Temporary Redirect: Redirect to http://mcp.example/mcp not followed")


# ---------------------------------------------------------------------------
# What a connect records and logs
# ---------------------------------------------------------------------------


def _settled(mgr: MCPClientManager, name: str) -> bool:
    status = mgr.get_server_status(name)
    return bool(status["connected"]) and status["error"] == ""


def _manager(configs: dict[str, Any], *, health_check_s: float) -> MCPClientManager:
    with patch("turnstone.core.mcp_client.load_config", return_value={}):
        mgr = MCPClientManager(configs)
    mgr._static_health_check_s = health_check_s
    return mgr


def test_failed_add_stays_registered_and_the_health_loop_connects_it(
    upstream: tuple[str, Upstream],
) -> None:
    base, state = upstream
    state.bearer = TOKEN
    mgr = _manager({}, health_check_s=0.2)
    mgr.start()
    try:
        cfg = {"type": "streamable-http", "url": f"{base}/mcp", "headers": {}}
        result = mgr.add_server_sync("srv", cfg)

        error = "HTTP 401 Unauthorized: the server requires authorization"
        assert result["connected"] is False
        assert result["error"] == error
        assert mgr._server_configs["srv"] is cfg
        assert mgr.get_all_server_status()["srv"]["error"] == error

        state.bearer = None  # the server starts accepting the request
        # A session is installed before discovery publishes and clears the old error: wait for
        # the whole connect.
        assert _poll_until(lambda: _settled(mgr, "srv"), 15)
        assert mgr.get_server_status("srv")["tools"] == 2
    finally:
        mgr.shutdown()


def test_a_saved_web_page_fails_at_once(upstream: tuple[str, Upstream]) -> None:
    """The initialize check in every connect's client, not only the test's."""
    base, _ = upstream
    mgr = _manager({}, health_check_s=0)
    mgr.start()
    try:
        started = time.monotonic()
        result = mgr.add_server_sync("srv", {"type": "streamable-http", "url": f"{base}/docs"})
        elapsed = time.monotonic() - started
    finally:
        mgr.shutdown()

    assert result["error"] == "Not an MCP endpoint: the server answered with text/html"
    assert elapsed < MCPClientManager._CONNECT_TIMEOUT / 3


def test_a_saved_server_that_drops_its_session_says_so(upstream: tuple[str, Upstream]) -> None:
    base, state = upstream
    state.drop_sessions = True
    mgr = _manager({}, health_check_s=0)
    mgr.start()
    try:
        result = mgr.add_server_sync("srv", {"type": "streamable-http", "url": f"{base}/mcp"})
        status = mgr.get_server_status("srv")
    finally:
        mgr.shutdown()

    assert result["error"] == DROPPED.message
    assert status["error"] == DROPPED.message


def test_a_saved_server_words_a_failed_listing_from_its_status(
    upstream: tuple[str, Upstream],
) -> None:
    """A 401 on tools/list ends the transport under the listing, which sees only that; the status
    recorded off the wire says what happened."""
    base, state = upstream
    state.fail_method = ("tools/list", 401)
    mgr = _manager({}, health_check_s=0)
    mgr.start()
    try:
        result = mgr.add_server_sync("srv", {"type": "streamable-http", "url": f"{base}/mcp"})
        status = mgr.get_server_status("srv")
    finally:
        mgr.shutdown()

    error = "HTTP 401 Unauthorized: the server requires authorization"
    assert result["error"] == status["error"] == error


def test_a_refused_header_value_never_reaches_status_or_our_logs(
    upstream: tuple[str, Upstream], caplog: pytest.LogCaptureFixture
) -> None:
    """httpx quotes a header it refuses to send, value and all. (The SDK's own error line still
    carries it: the save and test checks keep such a header from being configured.)"""
    base, _ = upstream
    secret = "sk-live-SECRET123"
    headers = {"X-Api-Key": f"{secret} "}  # a stray space
    caplog.set_level(logging.DEBUG)
    probe = probe_http_server(f"{base}/mcp", headers, name="srv")
    mgr = _manager({}, health_check_s=0)
    mgr.start()
    try:
        cfg = {"type": "streamable-http", "url": f"{base}/mcp", "headers": headers}
        result = mgr.add_server_sync("srv", cfg)
        status = mgr.get_server_status("srv")
    finally:
        mgr.shutdown()

    assert probe.failure is not None
    assert probe.failure.kind == "error"
    assert result["error"] == status["error"] == probe.failure.message
    assert secret not in probe.failure.message
    ours = [r for r in caplog.records if r.name.startswith("turnstone")]
    assert ours
    assert not [r for r in ours if secret in caplog.handler.format(r)]


def test_removing_a_server_cancels_a_reloads_retry(upstream: tuple[str, Upstream]) -> None:
    """With the health check off, a reload's retry holds the server's connect lock for its whole
    attempt; a delete must not wait that out (it waits only the reload's timeout)."""
    base, state = upstream
    state.bearer = TOKEN
    row = {"name": "srv", "transport": "streamable-http", "url": f"{base}/mcp", "headers": "{}"}
    storage = MagicMock()
    storage.list_mcp_servers.return_value = [row]
    mgr = _manager({}, health_check_s=0)
    mgr.start()
    try:
        mgr.reconcile_sync(storage)  # the add fails, and the server stays registered
        state.bearer = None
        state.stall_method = "tools/list"
        mgr.reconcile_sync(storage)  # starts the retry, which connects and waits on the listing
        assert state.stalled.wait(10)
        mgr.reconcile_sync(storage)  # leaves the running retry where the removal finds it

        storage.list_mcp_servers.return_value = []
        started = time.monotonic()
        removed = mgr.reconcile_sync(storage, timeout=10)["removed"]
        elapsed = time.monotonic() - started
    finally:
        state.release.set()
        mgr.shutdown()

    assert removed == ["srv"]
    assert elapsed < 8


def test_a_reload_retries_a_down_server(upstream: tuple[str, Upstream]) -> None:
    """With the health check off, a reload is what retries a server that failed to connect."""
    base, state = upstream
    state.bearer = TOKEN
    row = {"name": "srv", "transport": "streamable-http", "url": f"{base}/mcp", "headers": "{}"}
    storage = MagicMock()
    storage.list_mcp_servers.return_value = [row]
    mgr = _manager({}, health_check_s=0)
    mgr.start()
    try:
        mgr.reconcile_sync(storage)
        failed = mgr.get_server_status("srv")

        state.bearer = None
        mgr.reconcile_sync(storage)  # starts the retry and returns
        assert _poll_until(lambda: _settled(mgr, "srv"), 15)
        retried = mgr.get_server_status("srv")
    finally:
        mgr.shutdown()

    assert failed["connected"] is False
    assert failed["error"] == "HTTP 401 Unauthorized: the server requires authorization"
    assert retried["tools"] == 2


def test_a_reload_leaves_retries_to_a_running_health_check(upstream: tuple[str, Upstream]) -> None:
    """Its backoff paces retries; a reload does not add one per save."""
    base, state = upstream
    state.bearer = TOKEN
    row = {"name": "srv", "transport": "streamable-http", "url": f"{base}/mcp", "headers": "{}"}
    storage = MagicMock()
    storage.list_mcp_servers.return_value = [row]
    mgr = _manager({}, health_check_s=3600)  # running, next tick an hour out
    mgr.start()
    try:
        mgr.reconcile_sync(storage)
        state.bearer = None
        tries = len(state.seen_auth)
        mgr.reconcile_sync(storage)
        time.sleep(0.5)
        status = mgr.get_server_status("srv")
    finally:
        mgr.shutdown()

    assert len(state.seen_auth) == tries
    assert status["connected"] is False
    assert status["error"] == "HTTP 401 Unauthorized: the server requires authorization"


def test_reconcile_keeps_owning_a_failed_add() -> None:
    url = f"http://127.0.0.1:{_free_port()}/mcp"
    row = {"name": "srv", "transport": "streamable-http", "url": url, "headers": "{}"}
    storage = MagicMock()
    storage.list_mcp_servers.return_value = [row]
    mgr = _manager({}, health_check_s=0)
    mgr.start()
    try:
        assert mgr.reconcile_sync(storage) == {"added": [], "removed": [], "updated": []}
        assert "srv" in mgr._db_managed
        assert "unreachable at 127.0.0.1" in mgr.get_server_status("srv")["error"]

        moved = {**row, "url": f"http://127.0.0.1:{_free_port()}/mcp"}
        storage.list_mcp_servers.return_value = [moved]
        mgr.reconcile_sync(storage)
        assert mgr._server_configs["srv"]["url"] == moved["url"]
        assert "srv" in mgr._db_managed

        storage.list_mcp_servers.return_value = []
        assert mgr.reconcile_sync(storage)["removed"] == ["srv"]
        assert "srv" not in mgr._server_configs
        assert "srv" not in mgr.get_all_server_status()
    finally:
        mgr.shutdown()


def test_startup_failure_is_logged_in_words_without_the_exception_chain() -> None:
    port = _free_port()
    mgr = _manager(
        {"srv": {"type": "streamable-http", "url": f"http://127.0.0.1:{port}/mcp"}},
        health_check_s=0,
    )
    with patch("turnstone.core.mcp_client.log") as log:
        mgr.start()
        mgr.shutdown()

    calls = [c for c in log.warning.call_args_list if c.args[0].startswith("Failed to connect")]
    assert len(calls) == 1
    assert calls[0].args[1] == "srv"
    assert calls[0].args[2].startswith(f"MCP server 'srv' unreachable at 127.0.0.1:{port}")
    assert calls[0].kwargs == {}


@pytest.mark.parametrize(("failed_before", "level"), [(0, "info"), (2, "info"), (3, "debug")])
def test_reconnect_line_quiets_once_failures_repeat(failed_before: int, level: str) -> None:
    mgr = _manager(
        {"srv": {"type": "streamable-http", "url": "http://srv.example/mcp"}}, health_check_s=0
    )
    mgr._static_reconnect_attempt["srv"] = failed_before
    failing = AsyncMock(side_effect=ConnectionError("refused"))
    with (
        patch.object(mgr, "_ensure_static_connected", failing),
        patch("turnstone.core.mcp_client.log") as log,
    ):
        asyncio.run(mgr._static_reconnect_one("srv"))

    line = ("MCP static health: reconnecting '%s'", "srv")
    info = [c.args for c in log.info.call_args_list if c.args == line]
    debug = [c.args for c in log.debug.call_args_list if c.args == line]
    assert (info, debug) == (([line], []) if level == "info" else ([], [line]))


# ---------------------------------------------------------------------------
# The admin endpoint, end to end
# ---------------------------------------------------------------------------


class _AdminAuth(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id="admin",
            scopes=frozenset({"approve"}),
            token_source="config",
            permissions=frozenset({"read", "write", "approve", "admin.mcp"}),
        )
        return await call_next(request)


def test_endpoint_tests_a_real_server(upstream: tuple[str, Upstream], storage: Any) -> None:
    base, state = upstream
    state.bearer = TOKEN
    app = Starlette(
        routes=[Route("/v1/api/admin/mcp-servers/test", admin_test_mcp_server, methods=["POST"])],
        middleware=[Middleware(_AdminAuth)],
    )
    app.state.auth_storage = storage
    client = TestClient(app)
    body = {
        "name": "srv",
        "url": f"{base}/mcp",
        "auth_type": "static",
        "headers": {"Authorization": f"Bearer {TOKEN}"},
    }

    ok = client.post("/v1/api/admin/mcp-servers/test", json=body)
    rejected = client.post(
        "/v1/api/admin/mcp-servers/test",
        json={**body, "headers": {"Authorization": "Bearer wr0ng"}},
    )

    assert ok.status_code == 200
    assert ok.json() == {
        "ok": True,
        "tools": ["alpha", "beta"],
        "resources": 2,
        "prompts": 1,
        "error": None,
        "kind": None,
        "status": None,
    }
    assert rejected.status_code == 200
    assert rejected.json() == {
        "ok": False,
        "tools": [],
        "resources": 0,
        "prompts": 0,
        "error": "HTTP 401 Unauthorized: the server requires authorization",
        "kind": "http",
        "status": 401,
    }
    events = storage.list_audit_events(action="mcp_server.test")
    details = sorted(
        (
            (
                e["resource_id"],
                json.loads(e["detail"]) if isinstance(e["detail"], str) else e["detail"],
            )
            for e in events
        ),
        key=lambda event: event[1]["outcome"],
    )
    where = {"scheme": "http", "host": "127.0.0.1", "port": int(base.rsplit(":", 1)[1])}
    assert details == [
        ("srv", {**where, "outcome": "http", "status": 401}),
        ("srv", {**where, "outcome": "ok"}),
    ]
    assert "Bearer" not in str(events)
