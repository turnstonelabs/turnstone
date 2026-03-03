"""Cluster dashboard HTTP server for turnstone.

Serves the cluster-level dashboard UI and provides REST/SSE APIs
backed by the ClusterCollector.  Uses Starlette/ASGI with uvicorn.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import math
import os
import queue
import textwrap
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
# Route handlers
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
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncGenerator[None, None]:
    yield
    # Shutdown
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
) -> Starlette:
    """Build the Starlette ASGI application for the console dashboard."""
    app = Starlette(
        routes=[
            Route("/", index),
            Route("/api/cluster/overview", cluster_overview),
            Route("/api/cluster/nodes", cluster_nodes),
            Route("/api/cluster/workstreams", cluster_workstreams),
            Route("/api/cluster/node/{node_id}", cluster_node_detail),
            Route("/api/cluster/events", cluster_events_sse),
            Route("/health", health),
            Route("/api/auth/login", auth_login, methods=["POST"]),
            Route("/api/auth/logout", auth_logout, methods=["POST"]),
            Mount("/static", app=StaticFiles(directory=str(_STATIC_DIR)), name="static"),
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

    app = create_app(collector=collector, broker=broker, auth_config=auth_config)

    print(f"turnstone console running on http://{args.host}:{args.port}")
    if auth_config.enabled:
        print(f"Auth: enabled ({len(auth_config.tokens)} token(s) configured)")
    print("Press Ctrl+C to stop.")

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
