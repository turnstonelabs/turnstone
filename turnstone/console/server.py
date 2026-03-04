"""Cluster dashboard HTTP server for turnstone.

Serves the cluster-level dashboard UI and provides REST/SSE APIs
backed by the ClusterCollector.  Uses Starlette/ASGI with uvicorn.

Also provides:
- Workstream creation via MQ dispatch to target nodes
- Reverse proxy for server UIs so users only need console port access
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import html
import json
import logging
import math
import os
import queue
import re
import textwrap
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from sse_starlette import EventSourceResponse
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from turnstone.console.collector import ClusterCollector
from turnstone.mq.broker import RedisBroker

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("turnstone.console.server")

# ---------------------------------------------------------------------------
# Static assets — loaded once at startup
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent / "static"
_HTML = ""
_CSS = ""
_JS = ""


def _load_static() -> None:
    global _HTML, _CSS, _JS
    _HTML = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    _CSS = (_STATIC_DIR / "style.css").read_text(encoding="utf-8")
    _JS = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Query parameter helpers
# ---------------------------------------------------------------------------


def _parse_int(
    params: dict[str, str],
    name: str,
    default: int,
    minimum: int = 0,
    maximum: int = 10000,
) -> int:
    try:
        val = int(params.get(name, str(default)))
    except (ValueError, IndexError):
        val = default
    return max(minimum, min(val, maximum))


# ---------------------------------------------------------------------------
# Pure ASGI middleware
# ---------------------------------------------------------------------------


class AuthMiddleware:
    """ASGI middleware that enforces bearer-token / cookie authentication."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if request.method == "OPTIONS":
            await self.app(scope, receive, send)
            return
        from turnstone.core.auth import check_request

        auth_config = request.app.state.auth_config
        method = request.method
        path = request.url.path
        auth_header = request.headers.get("Authorization")
        cookie_header = request.headers.get("Cookie")
        allowed, status, msg = check_request(auth_config, method, path, auth_header, cookie_header)
        if not allowed:
            response = JSONResponse({"error": msg}, status_code=status)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Proxy helpers
# ---------------------------------------------------------------------------

# JS shim prepended to the server's app.js when proxied through the console.
# Overrides fetch() and EventSource() so root-relative URLs (/api/send etc.)
# route through the console proxy at /node/{node_id}/api/... instead.
_JS_PROXY_SHIM = """\
(function(){
  var _pfx="PREFIX_PLACEHOLDER";
  var _oF=window.fetch;
  window.fetch=function(u,o){
    if(typeof u==="string"&&u.startsWith("/"))u=_pfx+u;
    return _oF.call(this,u,o);
  };
  var _oE=window.EventSource;
  window.EventSource=function(u,o){
    if(typeof u==="string"&&u.startsWith("/"))u=_pfx+u;
    return new _oE(u,o);
  };
  window.EventSource.prototype=_oE.prototype;
  window.EventSource.CONNECTING=_oE.CONNECTING;
  window.EventSource.OPEN=_oE.OPEN;
  window.EventSource.CLOSED=_oE.CLOSED;
})();
"""

_CONSOLE_BANNER_TEMPLATE = (
    '<div style="background:#111827;border-bottom:1px solid rgba(229,160,66,0.3);'
    "padding:6px 20px;font-family:'IBM Plex Mono',monospace;font-size:12px;"
    'display:flex;align-items:center;gap:12px;position:relative;z-index:9999">'
    '<a href="/" style="color:#e5a042;text-decoration:none;font-weight:600;'
    'padding:2px 0" '
    "onmouseover=\"this.style.textDecoration='underline'\" "
    "onmouseout=\"this.style.textDecoration='none'\">"
    "&larr; Console</a>"
    '<span style="color:#8a93ad;font-size:11px">NODE_ID_PLACEHOLDER</span>'
    "</div>"
)


_VALID_NODE_ID = re.compile(r"^[a-zA-Z0-9._-]+$")


def _get_server_url(request: Request, node_id: str) -> str | None:
    """Resolve node_id to its server_url via the collector."""
    if not node_id or not _VALID_NODE_ID.match(node_id) or len(node_id) > 256:
        return None
    collector: ClusterCollector = request.app.state.collector
    detail = collector.get_node_detail(node_id)
    if detail and detail.get("server_url"):
        url: str = detail["server_url"]
        return url.rstrip("/")
    return None


def _pick_best_node(collector: ClusterCollector) -> str:
    """Select the reachable node with the most available capacity."""
    nodes, _ = collector.get_nodes(sort_by="activity", limit=1000, offset=0)
    best_id = ""
    best_headroom = -1
    for n in nodes:
        if not n.get("reachable", False):
            continue
        headroom = n.get("max_ws", 10) - n.get("ws_total", 0)
        if headroom > best_headroom:
            best_headroom = headroom
            best_id = n["node_id"]
    return best_id


# ---------------------------------------------------------------------------
# Route handlers — dashboard
# ---------------------------------------------------------------------------


async def index(request: Request) -> HTMLResponse:
    return HTMLResponse(_HTML)


async def cluster_overview(request: Request) -> JSONResponse:
    collector: ClusterCollector = request.app.state.collector
    return JSONResponse(collector.get_overview())


async def cluster_nodes(request: Request) -> JSONResponse:
    collector: ClusterCollector = request.app.state.collector
    params = dict(request.query_params)
    sort_by = params.get("sort", "activity")
    limit = _parse_int(params, "limit", 100, minimum=1, maximum=1000)
    offset = _parse_int(params, "offset", 0)
    nodes, total = collector.get_nodes(sort_by=sort_by, limit=limit, offset=offset)
    return JSONResponse({"nodes": nodes, "total": total})


async def cluster_workstreams(request: Request) -> JSONResponse:
    collector: ClusterCollector = request.app.state.collector
    params = dict(request.query_params)
    state = params.get("state")
    node = params.get("node")
    search = params.get("search")
    sort_by = params.get("sort", "state")
    page = _parse_int(params, "page", 1, minimum=1)
    per_page = _parse_int(params, "per_page", 50, minimum=1, maximum=200)
    ws_list, total = collector.get_workstreams(
        state=state,
        node=node,
        search=search,
        sort_by=sort_by,
        page=page,
        per_page=per_page,
    )
    pages = math.ceil(total / per_page) if per_page > 0 else 0
    return JSONResponse(
        {
            "workstreams": ws_list,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
        }
    )


async def cluster_node_detail(request: Request) -> JSONResponse:
    collector: ClusterCollector = request.app.state.collector
    node_id = request.path_params["node_id"]
    if not node_id or "/" in node_id or len(node_id) > 256:
        return JSONResponse({"error": "Invalid node ID"}, status_code=400)
    detail = collector.get_node_detail(node_id)
    if detail:
        return JSONResponse(detail)
    return JSONResponse({"error": "Node not found"}, status_code=404)


async def cluster_events_sse(request: Request) -> Response:
    collector: ClusterCollector = request.app.state.collector
    client_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=500)
    collector.register_listener(client_queue)

    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        loop = asyncio.get_running_loop()
        try:
            while True:
                try:
                    event = await loop.run_in_executor(
                        None, functools.partial(client_queue.get, timeout=5)
                    )
                    yield {"data": json.dumps(event)}
                except queue.Empty:
                    pass
                if await request.is_disconnected():
                    break
        finally:
            collector.unregister_listener(client_queue)

    return EventSourceResponse(event_generator(), ping=5)


async def health(request: Request) -> JSONResponse:
    collector: ClusterCollector = request.app.state.collector
    overview = collector.get_overview()
    return JSONResponse(
        {
            "status": "ok",
            "service": "turnstone-console",
            "nodes": overview["nodes"],
            "workstreams": overview["workstreams"],
        }
    )


async def auth_login(request: Request) -> Response:
    from turnstone.core.auth import make_set_cookie

    try:
        body: dict[str, Any] = await request.json()
    except (ValueError, json.JSONDecodeError):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    token = body.get("token", "")
    auth_config = request.app.state.auth_config
    role = auth_config.check(token)
    if role:
        response = JSONResponse({"status": "ok", "role": role})
        response.headers["Set-Cookie"] = make_set_cookie(token)
        return response
    return JSONResponse({"error": "Invalid token"}, status_code=401)


async def auth_logout(request: Request) -> Response:
    from turnstone.core.auth import make_clear_cookie

    response = JSONResponse({"status": "ok"})
    response.headers["Set-Cookie"] = make_clear_cookie()
    return response


# ---------------------------------------------------------------------------
# Route handlers — workstream creation
# ---------------------------------------------------------------------------


async def create_workstream(request: Request) -> JSONResponse:
    """POST /api/cluster/workstreams/new — create a workstream via MQ.

    Three targeting modes:
    - ``node_id`` set to a specific node ID → directed to that node's queue
    - ``node_id`` omitted or ``"auto"`` → console picks the node with most headroom
    - ``node_id`` set to ``"pool"`` → pushed to the shared queue for any bridge
    """
    try:
        body: dict[str, Any] = await request.json()
    except (ValueError, json.JSONDecodeError):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    broker: RedisBroker = request.app.state.broker
    collector: ClusterCollector = request.app.state.collector

    raw_node_id = body.get("node_id", "")
    raw_name = body.get("name", "")
    raw_model = body.get("model", "")
    if not isinstance(raw_node_id, str):
        raw_node_id = "" if raw_node_id is None else None
    if not isinstance(raw_name, str):
        raw_name = "" if raw_name is None else None
    if not isinstance(raw_model, str):
        raw_model = "" if raw_model is None else None
    if raw_node_id is None or raw_name is None or raw_model is None:
        return JSONResponse({"error": "node_id, name, and model must be strings"}, status_code=400)
    node_id = raw_node_id
    name = raw_name[:256]
    model = raw_model[:128]

    from turnstone.mq.protocol import CreateWorkstreamMessage

    # General pool — push to shared queue, any bridge picks it up
    if node_id == "pool":
        msg = CreateWorkstreamMessage(name=name, model=model)
        broker.push_inbound(msg.to_json())
        log.debug("Pool dispatch: correlation_id=%s name=%r", msg.correlation_id, name)
        return JSONResponse(
            {
                "status": "ok",
                "correlation_id": msg.correlation_id,
                "target_node": "pool",
            }
        )

    # Auto-select node by most available capacity
    if not node_id or node_id == "auto":
        node_id = _pick_best_node(collector)
        if not node_id:
            return JSONResponse({"error": "No reachable nodes available"}, status_code=503)

    # Validate node exists
    detail = collector.get_node_detail(node_id)
    if not detail:
        return JSONResponse({"error": "Node not found"}, status_code=404)

    msg = CreateWorkstreamMessage(
        name=name,
        model=model,
        target_node=node_id,
    )
    broker.push_inbound(msg.to_json(), node_id=node_id)

    return JSONResponse(
        {
            "status": "ok",
            "correlation_id": msg.correlation_id,
            "target_node": node_id,
        }
    )


# ---------------------------------------------------------------------------
# Route handlers — reverse proxy
# ---------------------------------------------------------------------------


async def proxy_index(request: Request) -> Response:
    """GET /node/{node_id}/ — serve proxied server UI with URL rewriting."""
    node_id = request.path_params["node_id"]
    server_url = _get_server_url(request, node_id)
    if not server_url:
        return JSONResponse({"error": "Node not found"}, status_code=404)

    client: httpx.AsyncClient = request.app.state.proxy_client
    safe_node = urllib.parse.quote(node_id, safe="")
    prefix = f"/node/{safe_node}"
    try:
        resp = await client.get(f"{server_url}/")
        if resp.status_code < 200 or resp.status_code >= 300:
            log.debug("Upstream %s returned status %s", node_id, resp.status_code)
            return JSONResponse(
                {"error": "Upstream server error", "status_code": resp.status_code},
                status_code=resp.status_code,
            )
        page = resp.text
        # Rewrite static asset paths
        page = page.replace('href="/static/', f'href="{prefix}/static/')
        page = page.replace('src="/static/', f'src="{prefix}/static/')
        # Inject console-return banner after <body>
        banner = _CONSOLE_BANNER_TEMPLATE.replace("NODE_ID_PLACEHOLDER", html.escape(node_id))
        page = page.replace("<body>", "<body>" + banner, 1)
        return HTMLResponse(page)
    except httpx.HTTPError as exc:
        log.debug("Proxy index error for %s: %s", node_id, exc)
        return JSONResponse({"error": "Node unreachable"}, status_code=502)


async def proxy_static(request: Request) -> Response:
    """GET /node/{node_id}/static/{path} — proxy static files."""
    node_id = request.path_params["node_id"]
    path = request.path_params["path"]
    server_url = _get_server_url(request, node_id)
    if not server_url:
        return JSONResponse({"error": "Node not found"}, status_code=404)

    client: httpx.AsyncClient = request.app.state.proxy_client
    safe_node = urllib.parse.quote(node_id, safe="")
    prefix = f"/node/{safe_node}"
    try:
        resp = await client.get(f"{server_url}/static/{path}")
        content_type = resp.headers.get("content-type", "application/octet-stream")
        body = resp.content
        # Inject proxy shim into app.js
        if path == "app.js":
            shim = _JS_PROXY_SHIM.replace('"PREFIX_PLACEHOLDER"', json.dumps(prefix))
            body = shim.encode("utf-8") + body
            content_type = "application/javascript; charset=utf-8"
        return Response(content=body, status_code=resp.status_code, media_type=content_type)
    except httpx.HTTPError as exc:
        log.debug("Proxy static error for %s/%s: %s", node_id, path, exc)
        return JSONResponse({"error": "Node unreachable"}, status_code=502)


async def proxy_api(request: Request) -> Response:
    """Proxy API requests to target node. Detects SSE vs regular."""
    node_id = request.path_params["node_id"]
    path = request.path_params["path"]
    server_url = _get_server_url(request, node_id)
    if not server_url:
        return JSONResponse({"error": "Node not found"}, status_code=404)

    # SSE detection: GET requests to events endpoints
    if request.method == "GET" and path in ("events", "events/global"):
        return await _proxy_sse(request, server_url, path)

    if request.method == "POST":
        return await _proxy_post(request, server_url, path)

    return await _proxy_get(request, server_url, f"api/{path}")


async def proxy_non_api(request: Request) -> Response:
    """Proxy non-API GET endpoints (health, metrics) to target node."""
    node_id = request.path_params["node_id"]
    path = request.path_params["path"]
    server_url = _get_server_url(request, node_id)
    if not server_url:
        return JSONResponse({"error": "Node not found"}, status_code=404)
    return await _proxy_get(request, server_url, path)


async def _proxy_get(request: Request, server_url: str, path: str) -> Response:
    """Forward a GET request to the target server."""
    client: httpx.AsyncClient = request.app.state.proxy_client
    target = f"{server_url}/{path}"
    if request.url.query:
        target += f"?{request.url.query}"
    try:
        resp = await client.get(target)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )
    except httpx.HTTPError as exc:
        log.debug("Proxy GET error for %s: %s", target, exc)
        return JSONResponse({"error": "Node unreachable"}, status_code=502)


async def _proxy_post(request: Request, server_url: str, path: str) -> Response:
    """Forward a POST request to the target server."""
    client: httpx.AsyncClient = request.app.state.proxy_client
    body = await request.body()
    content_type = request.headers.get("content-type", "application/json")
    target = f"{server_url}/api/{path}"
    if request.url.query:
        target += f"?{request.url.query}"
    try:
        resp = await client.post(
            target,
            content=body,
            headers={"Content-Type": content_type},
        )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )
    except httpx.HTTPError as exc:
        log.debug("Proxy POST error for api/%s: %s", path, exc)
        return JSONResponse({"error": "Node unreachable"}, status_code=502)


async def _proxy_sse(request: Request, server_url: str, path: str) -> Response:
    """Proxy an SSE stream from the target server to the browser."""
    target = f"{server_url}/api/{path}"
    if request.url.query:
        target += f"?{request.url.query}"

    proxy_token: str = request.app.state.proxy_auth_token

    async def sse_generator() -> AsyncGenerator[dict[str, str], None]:
        headers: dict[str, str] = {}
        if proxy_token:
            headers["Authorization"] = f"Bearer {proxy_token}"
        async with httpx.AsyncClient(timeout=None, headers=headers) as sse_client:
            try:
                async with sse_client.stream("GET", target) as resp:
                    if resp.status_code != 200:
                        log.debug(
                            "SSE proxy received status %s from %s",
                            resp.status_code,
                            target,
                        )
                        yield {
                            "event": "error",
                            "data": f"Upstream returned status {resp.status_code}",
                        }
                        return
                    buf = ""
                    async for chunk in resp.aiter_text():
                        if await request.is_disconnected():
                            return
                        buf += chunk
                        while "\n\n" in buf:
                            event_text, buf = buf.split("\n\n", 1)
                            data_lines = []
                            for line in event_text.split("\n"):
                                if line.startswith("data:"):
                                    # SSE spec: strip exactly one leading space
                                    value = line[5:]
                                    if value.startswith(" "):
                                        value = value[1:]
                                    data_lines.append(value)
                            if data_lines:
                                yield {"data": "\n".join(data_lines)}
            except httpx.HTTPError:
                log.debug("SSE proxy stream ended for %s", target)

    return EventSourceResponse(sse_generator(), ping=5)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncGenerator[None, None]:
    # Create async HTTP client for proxy routes
    headers: dict[str, str] = {}
    token = app.state.proxy_auth_token
    if token:
        headers["Authorization"] = f"Bearer {token}"
    app.state.proxy_client = httpx.AsyncClient(timeout=30, headers=headers)
    yield
    # Shutdown
    await app.state.proxy_client.aclose()
    app.state.collector.stop()
    app.state.broker.close()


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    *,
    collector: ClusterCollector,
    broker: RedisBroker,
    auth_config: Any,
    proxy_auth_token: str = "",
) -> Starlette:
    """Build the Starlette ASGI application for the console dashboard."""
    app = Starlette(
        routes=[
            Route("/", index),
            Route("/api/cluster/overview", cluster_overview),
            Route("/api/cluster/nodes", cluster_nodes),
            Route("/api/cluster/workstreams", cluster_workstreams),
            Route("/api/cluster/workstreams/new", create_workstream, methods=["POST"]),
            Route("/api/cluster/node/{node_id}", cluster_node_detail),
            Route("/api/cluster/events", cluster_events_sse),
            Route("/health", health),
            Route("/api/auth/login", auth_login, methods=["POST"]),
            Route("/api/auth/logout", auth_logout, methods=["POST"]),
            Mount("/static", app=StaticFiles(directory=str(_STATIC_DIR)), name="static"),
            # Proxy routes — serve server UI through console port
            Route("/node/{node_id}/", proxy_index),
            Route("/node/{node_id}/static/{path:path}", proxy_static),
            Route("/node/{node_id}/api/{path:path}", proxy_api, methods=["GET", "POST"]),
            Route("/node/{node_id}/{path:path}", proxy_non_api),
        ],
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["Content-Type", "Authorization"],
            ),
            Middleware(AuthMiddleware),
        ],
        lifespan=_lifespan,
    )
    app.state.collector = collector
    app.state.broker = broker
    app.state.auth_config = auth_config
    app.state.proxy_auth_token = proxy_auth_token
    return app


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="turnstone console — cluster dashboard service.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              turnstone-console                              # default Redis on localhost
              turnstone-console --port 9090                  # custom port
              turnstone-console --redis-host redis.internal   # remote Redis
        """),
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind to (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8090,
        help="Port to listen on (default: 8090)",
    )
    parser.add_argument(
        "--redis-host",
        default="localhost",
        help="Redis host (default: localhost)",
    )
    parser.add_argument(
        "--redis-port",
        type=int,
        default=6379,
        help="Redis port (default: 6379)",
    )
    parser.add_argument(
        "--redis-password",
        default=os.environ.get("REDIS_PASSWORD"),
        help="Redis password (default: $REDIS_PASSWORD)",
    )
    parser.add_argument(
        "--redis-db",
        type=int,
        default=0,
        help="Redis DB number (default: 0)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=10.0,
        help="Node polling interval in seconds (default: 10)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("TURNSTONE_AUTH_TOKEN", ""),
        help="Bearer token for polling turnstone-server nodes (default: $TURNSTONE_AUTH_TOKEN)",
    )

    from turnstone.core.config import apply_config

    apply_config(parser, ["console", "redis", "auth"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    broker = RedisBroker(
        host=args.redis_host,
        port=args.redis_port,
        db=args.redis_db,
        password=args.redis_password,
    )

    collector = ClusterCollector(
        broker=broker,
        poll_interval=args.poll_interval,
        auth_token=args.auth_token,
    )
    collector.start()

    _load_static()

    from turnstone.core.auth import load_auth_config

    auth_config = load_auth_config()

    app = create_app(
        collector=collector,
        broker=broker,
        auth_config=auth_config,
        proxy_auth_token=args.auth_token,
    )

    print(f"turnstone console running on http://{args.host}:{args.port}")
    if auth_config.enabled:
        print(f"Auth: enabled ({len(auth_config.tokens)} token(s) configured)")
    print("Press Ctrl+C to stop.")

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
