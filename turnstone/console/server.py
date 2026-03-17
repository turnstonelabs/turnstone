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
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from turnstone.api.console_spec import build_console_spec
from turnstone.api.docs import make_docs_handler, make_openapi_handler
from turnstone.console.collector import ClusterCollector
from turnstone.core.auth import JWT_AUD_CONSOLE, AuthMiddleware

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from starlette.requests import Request

    from turnstone.mq.broker import RedisBroker

log = logging.getLogger("turnstone.console.server")

# ---------------------------------------------------------------------------
# Static assets — loaded once at startup
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent / "static"
_SHARED_DIR = Path(__file__).parent.parent / "shared_static"
_HTML = ""


def _load_static() -> None:
    global _HTML
    _HTML = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")


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


# ---------------------------------------------------------------------------
# Proxy helpers
# ---------------------------------------------------------------------------

# JS shim injected into proxied HTML when served through the console.
# Overrides fetch() and EventSource() so root-relative URLs (/v1/api/send etc.)
# route through the console proxy at /node/{node_id}/v1/api/... instead.
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
    '<a href="/" style="color:#8a93ad;text-decoration:none;font-weight:500;'
    'padding:2px 0" '
    "onmouseover=\"this.style.color='#e5a042'\" "
    "onmouseout=\"this.style.color='#8a93ad'\">"
    "&larr; Console</a>"
    '<span style="color:#3b4463">\u2502</span>'
    '<span style="color:#8a93ad;font-size:11px">NODE_ID_PLACEHOLDER</span>'
    "</div>"
)

# Injected <style> offsets fixed-position overlays below the console banner.
_CONSOLE_PROXY_STYLE = "<style>.dashboard-overlay{top:32px!important}</style>"


_VALID_NODE_ID = re.compile(r"^[a-zA-Z0-9._-]+$")


def _proxy_auth_headers(request: Request) -> dict[str, str]:
    """Build auth headers for proxied requests to upstream servers.

    Uses the service proxy token (``JWT_AUD_SERVER``) so the upstream node
    accepts the request.  The user's console-audience JWT is *not* forwarded
    — it would be rejected by the server's audience validation.
    """
    # Prefer the auto-rotating ServiceTokenManager when available
    mgr = getattr(request.app.state, "proxy_token_mgr", None)
    if mgr is not None:
        return dict(mgr.bearer_header)

    # Fall back to static proxy_auth_token (e.g. from --auth-token)
    static_token = getattr(request.app.state, "proxy_auth_token", "")
    if static_token:
        return {"Authorization": f"Bearer {static_token}"}

    return {}


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


async def cluster_snapshot(request: Request) -> JSONResponse:
    collector: ClusterCollector = request.app.state.collector
    return JSONResponse(collector.get_snapshot())


async def cluster_events_sse(request: Request) -> Response:
    collector: ClusterCollector = request.app.state.collector
    client_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=500)

    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        loop = asyncio.get_running_loop()
        try:
            # Atomic snapshot+register — no event gap possible.
            snap = await loop.run_in_executor(
                None, collector.get_snapshot_and_register, client_queue
            )
            snap["type"] = "snapshot"
            yield {"data": json.dumps(snap)}

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
            "version_drift": overview.get("version_drift", False),
            "versions": overview.get("versions", []),
        }
    )


async def auth_login(request: Request) -> Response:
    """Authenticate via username:password or legacy token, return JWT."""
    from turnstone.core.auth import handle_auth_login

    return await handle_auth_login(request, JWT_AUD_CONSOLE)


async def auth_logout(request: Request) -> Response:
    """POST /v1/api/auth/logout — clear auth cookie."""
    from turnstone.core.auth import handle_auth_logout

    return await handle_auth_logout(request)


async def auth_status(request: Request) -> Response:
    """GET /v1/api/auth/status — public endpoint for login UI state detection."""
    from turnstone.core.auth import handle_auth_status

    return await handle_auth_status(request)


async def auth_setup(request: Request) -> Response:
    """POST /v1/api/auth/setup — create first admin user (public, one-time only)."""
    from turnstone.core.auth import handle_auth_setup

    return await handle_auth_setup(request, JWT_AUD_CONSOLE)


async def auth_whoami(request: Request) -> Response:
    """GET /v1/api/auth/whoami — return authenticated user info."""
    from turnstone.core.auth import handle_auth_whoami

    return await handle_auth_whoami(request)


async def oidc_authorize(request: Request) -> Response:
    """GET /v1/api/auth/oidc/authorize — redirect to OIDC provider."""
    from turnstone.core.auth import handle_oidc_authorize

    return await handle_oidc_authorize(request, JWT_AUD_CONSOLE)


async def oidc_callback(request: Request) -> Response:
    """GET /v1/api/auth/oidc/callback — OIDC callback, exchange code for JWT."""
    from turnstone.core.auth import handle_oidc_callback

    return await handle_oidc_callback(request, JWT_AUD_CONSOLE)


# ---------------------------------------------------------------------------
# Route handlers — workstream creation
# ---------------------------------------------------------------------------


async def create_workstream(request: Request) -> JSONResponse:
    """POST /v1/api/cluster/workstreams/new — create a workstream via MQ.

    Three targeting modes:
    - ``node_id`` set to a specific node ID → directed to that node's queue
    - ``node_id`` omitted or ``"auto"`` → console picks the node with most headroom
    - ``node_id`` set to ``"pool"`` → pushed to the shared queue for any bridge
    """
    from turnstone.core.web_helpers import read_json_or_400

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    broker: RedisBroker = request.app.state.broker
    collector: ClusterCollector = request.app.state.collector

    raw_node_id = body.get("node_id", "")
    raw_name = body.get("name", "")
    raw_model = body.get("model", "")
    raw_initial_message = body.get("initial_message", "")
    raw_skill = body.get("skill", "")
    if not isinstance(raw_node_id, str):
        raw_node_id = "" if raw_node_id is None else None
    if not isinstance(raw_name, str):
        raw_name = "" if raw_name is None else None
    if not isinstance(raw_model, str):
        raw_model = "" if raw_model is None else None
    if not isinstance(raw_initial_message, str):
        raw_initial_message = "" if raw_initial_message is None else None
    if not isinstance(raw_skill, str):
        raw_skill = "" if raw_skill is None else None
    if (
        raw_node_id is None
        or raw_name is None
        or raw_model is None
        or raw_initial_message is None
        or raw_skill is None
    ):
        return JSONResponse(
            {"error": "node_id, name, model, initial_message, and skill must be strings"},
            status_code=400,
        )
    node_id = raw_node_id
    name = raw_name[:256]
    model = raw_model[:128]
    initial_message = raw_initial_message[:4096]
    skill = raw_skill[:256]

    from turnstone.mq.protocol import CreateWorkstreamMessage

    # General pool — push to shared queue, any bridge picks it up
    if node_id == "pool":
        msg = CreateWorkstreamMessage(
            name=name,
            model=model,
            initial_message=initial_message,
            skill=skill,
        )
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
        initial_message=initial_message,
        skill=skill,
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
        resp = await client.get(f"{server_url}/", headers=_proxy_auth_headers(request))
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
        page = page.replace('href="/shared/', f'href="{prefix}/shared/')
        page = page.replace('src="/shared/', f'src="{prefix}/shared/')
        # Inject console-return banner + proxy shim after <body>
        banner = _CONSOLE_BANNER_TEMPLATE.replace("NODE_ID_PLACEHOLDER", html.escape(node_id))
        shim = (
            "<script>"
            + _JS_PROXY_SHIM.replace('"PREFIX_PLACEHOLDER"', json.dumps(prefix))
            + "</script>"
        )
        page = page.replace("<body>", "<body>" + banner + _CONSOLE_PROXY_STYLE + shim, 1)
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
    try:
        resp = await client.get(
            f"{server_url}/static/{path}",
            headers=_proxy_auth_headers(request),
        )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/octet-stream"),
        )
    except httpx.HTTPError as exc:
        log.debug("Proxy static error for %s/%s: %s", node_id, path, exc)
        return JSONResponse({"error": "Node unreachable"}, status_code=502)


async def proxy_shared_static(request: Request) -> Response:
    """GET /node/{node_id}/shared/{path} — proxy shared static files."""
    node_id = request.path_params["node_id"]
    path = request.path_params["path"]
    server_url = _get_server_url(request, node_id)
    if not server_url:
        return JSONResponse({"error": "Node not found"}, status_code=404)

    client: httpx.AsyncClient = request.app.state.proxy_client
    try:
        resp = await client.get(
            f"{server_url}/shared/{path}",
            headers=_proxy_auth_headers(request),
        )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/octet-stream"),
        )
    except httpx.HTTPError as exc:
        log.debug("Proxy shared static error for %s/%s: %s", node_id, path, exc)
        return JSONResponse({"error": "Node unreachable"}, status_code=502)


async def proxy_api(request: Request) -> Response:
    """Proxy API requests to target node. Detects SSE vs regular."""
    node_id = request.path_params["node_id"]
    path = request.path_params["path"]
    server_url = _get_server_url(request, node_id)
    if not server_url:
        return JSONResponse({"error": "Node not found"}, status_code=404)

    # Detect if this came through the /v1/ proxy route
    api_prefix = "api"
    safe_node = urllib.parse.quote(node_id, safe="")
    if request.url.path.startswith(f"/node/{safe_node}/v1/api/"):
        api_prefix = "v1/api"

    # SSE detection: GET requests to events endpoints
    if request.method == "GET" and path in ("events", "events/global"):
        return await _proxy_sse(request, server_url, path, api_prefix=api_prefix)

    if request.method == "POST":
        return await _proxy_post(request, server_url, path, api_prefix=api_prefix)

    return await _proxy_get(request, server_url, f"{api_prefix}/{path}")


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
        resp = await client.get(target, headers=_proxy_auth_headers(request))
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )
    except httpx.HTTPError as exc:
        log.debug("Proxy GET error for %s: %s", target, exc)
        return JSONResponse({"error": "Node unreachable"}, status_code=502)


async def _proxy_post(
    request: Request, server_url: str, path: str, *, api_prefix: str = "api"
) -> Response:
    """Forward a POST request to the target server."""
    client: httpx.AsyncClient = request.app.state.proxy_client
    body = await request.body()
    content_type = request.headers.get("content-type", "application/json")
    target = f"{server_url}/{api_prefix}/{path}"
    if request.url.query:
        target += f"?{request.url.query}"
    try:
        post_headers = {"Content-Type": content_type}
        post_headers.update(_proxy_auth_headers(request))
        resp = await client.post(target, content=body, headers=post_headers)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )
    except httpx.HTTPError as exc:
        log.debug("Proxy POST error for %s/%s: %s", api_prefix, path, exc)
        return JSONResponse({"error": "Node unreachable"}, status_code=502)


async def _proxy_sse(
    request: Request, server_url: str, path: str, *, api_prefix: str = "api"
) -> Response:
    """Proxy an SSE stream from the target server to the browser.

    Relays raw bytes verbatim so server-side ping comments, event framing,
    and keepalives all pass through unchanged.
    """
    target = f"{server_url}/{api_prefix}/{path}"
    if request.url.query:
        target += f"?{request.url.query}"

    sse_client: httpx.AsyncClient = request.app.state.proxy_sse_client
    sse_auth = _proxy_auth_headers(request)

    async def raw_stream() -> AsyncGenerator[bytes, None]:
        try:
            async with sse_client.stream(
                "GET",
                target,
                headers={**sse_auth, "Accept": "text/event-stream", "Cache-Control": "no-store"},
                timeout=httpx.Timeout(connect=10, read=None, write=5, pool=None),
            ) as response:
                if response.status_code != 200:
                    log.debug(
                        "SSE proxy received status %s from %s",
                        response.status_code,
                        target,
                    )
                    yield f"event: error\ndata: Upstream returned status {response.status_code}\n\n".encode()
                    return
                async for chunk in response.aiter_bytes():
                    if await request.is_disconnected():
                        return
                    yield chunk
        except httpx.HTTPError:
            log.debug("SSE proxy stream ended for %s", target)

    return StreamingResponse(
        raw_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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
    # Separate client for SSE streams — longer read timeout, shared connection pool
    app.state.proxy_sse_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5, read=30, write=5, pool=5),
        limits=httpx.Limits(keepalive_expiry=30),
        headers=headers,
    )
    # Start scheduler if configured
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.start()
    # OIDC discovery (if configured)
    oidc_config = app.state.oidc_config
    if oidc_config.enabled:
        from turnstone.core.oidc import discover_oidc

        try:
            oidc_config = await discover_oidc(oidc_config)
            app.state.oidc_config = oidc_config
        except Exception:
            log.warning("OIDC discovery failed — OIDC login disabled", exc_info=True)
        if oidc_config.enabled and oidc_config.jwks_uri:
            try:
                from turnstone.core.oidc import fetch_jwks

                app.state.jwks_data = await fetch_jwks(oidc_config.jwks_uri)
                log.info(
                    "OIDC enabled: %s (%s)",
                    oidc_config.provider_name,
                    oidc_config.issuer,
                )
            except Exception:
                log.warning(
                    "OIDC JWKS prefetch failed — will retry on first login",
                    exc_info=True,
                )
    yield
    # Shutdown
    if scheduler is not None:
        scheduler.stop()
    await app.state.proxy_sse_client.aclose()
    await app.state.proxy_client.aclose()
    app.state.collector.stop()
    app.state.broker.close()


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
# Admin API endpoints — user + token management
# ---------------------------------------------------------------------------


async def admin_list_users(request: Request) -> JSONResponse:
    """GET /v1/api/admin/users — list all users."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err
    return JSONResponse({"users": storage.list_users()})


async def admin_create_user(request: Request) -> JSONResponse:
    """POST /v1/api/admin/users — create a new user."""
    import uuid

    from turnstone.core.auth import hash_password, require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err

    from turnstone.core.web_helpers import read_json_or_400

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    username = body.get("username", "").strip()
    display_name = body.get("display_name", "").strip()
    password = body.get("password", "")

    from turnstone.core.auth import is_valid_username

    if not is_valid_username(username):
        return JSONResponse(
            {"error": "Invalid username (1-64 chars: letters, digits, . _ -)"},
            status_code=400,
        )
    if not display_name:
        return JSONResponse({"error": "display_name is required"}, status_code=400)
    if not password or len(password) < 8:
        return JSONResponse({"error": "Password must be at least 8 characters"}, status_code=400)

    # Check username uniqueness
    if storage.get_user_by_username(username) is not None:
        return JSONResponse({"error": "Username already taken"}, status_code=409)

    user_id = uuid.uuid4().hex
    pw_hash = hash_password(password)
    storage.create_user(user_id, username, display_name, pw_hash)

    from turnstone.core.audit import record_audit

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "user.create",
        "user",
        user_id,
        {"username": username},
        ip,
    )

    # Read back to get the storage-canonical created timestamp
    user = storage.get_user(user_id)
    return JSONResponse(
        {
            "user_id": user["user_id"],
            "username": user["username"],
            "display_name": user["display_name"],
            "created": user["created"],
        }
    )


async def admin_delete_user(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/users/{user_id} — delete user + cascade tokens."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err
    user_id = request.path_params["user_id"]
    # Prevent self-deletion
    auth_result = getattr(request.state, "auth_result", None)
    if auth_result and auth_result.user_id == user_id:
        return JSONResponse({"error": "Cannot delete your own account"}, status_code=400)
    # Look up username for the audit trail before deleting
    target_user = storage.get_user(user_id)
    if storage.delete_user(user_id):
        from turnstone.core.audit import record_audit

        audit_uid, ip = _audit_context(request)
        record_audit(
            storage,
            audit_uid,
            "user.delete",
            "user",
            user_id,
            {"username": target_user.get("username", "") if target_user else ""},
            ip,
        )
        return JSONResponse({"status": "ok"})
    return JSONResponse({"error": "User not found"}, status_code=404)


async def admin_list_tokens(request: Request) -> JSONResponse:
    """GET /v1/api/admin/users/{user_id}/tokens — list tokens for a user."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err
    user_id = request.path_params["user_id"]
    return JSONResponse({"tokens": storage.list_api_tokens(user_id)})


async def admin_create_token(request: Request) -> JSONResponse:
    """POST /v1/api/admin/users/{user_id}/tokens — create API token."""
    import uuid

    from turnstone.core.auth import generate_token, hash_token, require_permission, token_prefix
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err
    user_id = request.path_params["user_id"]

    # Verify user exists
    if storage.get_user(user_id) is None:
        return JSONResponse({"error": "User not found"}, status_code=404)

    try:
        body: dict[str, Any] = await request.json()
    except (ValueError, json.JSONDecodeError):
        body = {}

    name = body.get("name", "")
    scopes = body.get("scopes", "read,write,approve")
    expires_days = body.get("expires_days")

    # Validate scopes
    from turnstone.core.auth import VALID_SCOPES

    requested = {s.strip() for s in scopes.split(",") if s.strip()}
    if not requested or not requested.issubset(VALID_SCOPES):
        return JSONResponse(
            {"error": "Invalid scopes (allowed: read, write, approve)"}, status_code=400
        )

    expires: str | None = None
    if expires_days is not None:
        from datetime import UTC, datetime, timedelta

        expires = (datetime.now(UTC) + timedelta(days=int(expires_days))).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )

    raw = generate_token()
    tid = uuid.uuid4().hex
    storage.create_api_token(
        token_id=tid,
        token_hash=hash_token(raw),
        token_prefix=token_prefix(raw),
        user_id=user_id,
        name=name,
        scopes=scopes,
        expires=expires,
    )

    from turnstone.core.audit import record_audit

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "token.create",
        "token",
        tid,
        {"name": name},
        ip,
    )

    return JSONResponse(
        {
            "token": raw,
            "token_id": tid,
            "token_prefix": token_prefix(raw),
            "scopes": scopes,
        }
    )


async def admin_revoke_token(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/tokens/{token_id} — revoke an API token."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err
    token_id = request.path_params["token_id"]
    if storage.delete_api_token(token_id):
        from turnstone.core.audit import record_audit

        audit_uid, ip = _audit_context(request)
        record_audit(
            storage,
            audit_uid,
            "token.revoke",
            "token",
            token_id,
            {},
            ip,
        )
        return JSONResponse({"status": "ok"})
    return JSONResponse({"error": "Token not found"}, status_code=404)


# ---------------------------------------------------------------------------
# Admin: Channel user mapping
# ---------------------------------------------------------------------------


async def admin_list_channels(request: Request) -> JSONResponse:
    """GET /v1/api/admin/users/{user_id}/channels — list channel links for a user."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err
    user_id = request.path_params["user_id"]
    channels = storage.list_channel_users_by_user(user_id)
    return JSONResponse({"channels": channels})


async def admin_create_channel(request: Request) -> JSONResponse:
    """POST /v1/api/admin/users/{user_id}/channels — link a channel account."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err
    user_id = request.path_params["user_id"]

    from turnstone.core.web_helpers import read_json_or_400

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    channel_type = body.get("channel_type", "").strip().lower()
    channel_user_id = body.get("channel_user_id", "").strip()

    if not channel_type:
        return JSONResponse({"error": "channel_type is required"}, status_code=400)
    if not channel_user_id:
        return JSONResponse({"error": "channel_user_id is required"}, status_code=400)
    if len(channel_type) > 64 or len(channel_user_id) > 256:
        return JSONResponse({"error": "Value too long"}, status_code=400)

    # Verify user exists
    if storage.get_user(user_id) is None:
        return JSONResponse({"error": "User not found"}, status_code=404)

    # Check for existing mapping
    existing = storage.get_channel_user(channel_type, channel_user_id)
    if existing is not None:
        return JSONResponse(
            {"error": f"Channel user already linked to user {existing['user_id']}"},
            status_code=409,
        )

    storage.create_channel_user(channel_type, channel_user_id, user_id)
    result = storage.get_channel_user(channel_type, channel_user_id)
    if result is None:
        return JSONResponse({"error": "Failed to create channel mapping"}, status_code=500)
    # Guard against race: another request may have claimed this channel_user_id.
    if result.get("user_id") != user_id:
        return JSONResponse(
            {"error": f"Channel user already linked to user {result['user_id']}"},
            status_code=409,
        )

    from turnstone.core.audit import record_audit

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "channel.link",
        "channel",
        channel_user_id,
        {"channel_type": channel_type, "user_id": user_id},
        ip,
    )

    return JSONResponse(result)


async def admin_delete_channel(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/channels/{channel_type}/{channel_user_id} — unlink."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err
    channel_type = request.path_params["channel_type"]
    channel_user_id = request.path_params["channel_user_id"]
    if storage.delete_channel_user(channel_type, channel_user_id):
        from turnstone.core.audit import record_audit

        audit_uid, ip = _audit_context(request)
        record_audit(
            storage,
            audit_uid,
            "channel.unlink",
            "channel",
            channel_user_id,
            {"channel_type": channel_type},
            ip,
        )
        return JSONResponse({"status": "ok"})
    return JSONResponse({"error": "Channel link not found"}, status_code=404)


# ---------------------------------------------------------------------------
# Admin API endpoints — OIDC identities
# ---------------------------------------------------------------------------


async def admin_list_oidc_identities(request: Request) -> JSONResponse:
    """GET /v1/api/admin/users/{user_id}/oidc-identities — list OIDC links for a user."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err

    user_id = request.path_params["user_id"]
    identities = storage.list_oidc_identities_for_user(user_id)
    return JSONResponse({"oidc_identities": identities})


async def admin_delete_oidc_identity(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/oidc-identities?issuer=...&subject=... — unlink OIDC identity."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err

    issuer = request.query_params.get("issuer", "")
    subject = request.query_params.get("subject", "")
    if not issuer or not subject:
        return JSONResponse({"error": "issuer and subject required"}, status_code=400)

    # Look up before delete so audit captures which user was affected
    identity = storage.get_oidc_identity(issuer, subject)
    if not identity:
        return JSONResponse({"error": "Identity not found"}, status_code=404)

    storage.delete_oidc_identity(issuer, subject)

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "oidc_identity.delete",
        "oidc_identity",
        f"{issuer}:{subject}",
        {"user_id": identity["user_id"]},
        ip,
    )

    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Admin API endpoints — scheduled tasks
# ---------------------------------------------------------------------------


def _normalize_task_dict(task: dict[str, Any]) -> dict[str, Any]:
    """Convert DB row ints/csv to JSON-friendly bools/lists."""
    tools_str = task.get("auto_approve_tools", "")
    task["auto_approve_tools"] = [s.strip() for s in tools_str.split(",") if s.strip()]
    task["auto_approve"] = bool(task.get("auto_approve", 0))
    task["enabled"] = bool(task.get("enabled", 1))
    return task


def _compute_next_run(schedule_type: str, cron_expr: str, at_time: str) -> str:
    """Compute the next run time for a schedule. Empty string if invalid."""
    if schedule_type == "at":
        return at_time
    if schedule_type == "cron" and cron_expr:
        from datetime import UTC, datetime

        from croniter import croniter

        cron = croniter(cron_expr, datetime.now(UTC))
        next_dt = cron.get_next(datetime)
        return str(next_dt.strftime("%Y-%m-%dT%H:%M:%S"))
    return ""


def _validate_schedule_fields(schedule_type: str, cron_expr: str, at_time: str) -> str | None:
    """Validate schedule type/expression. Returns error string or None."""
    if schedule_type not in ("cron", "at"):
        return "schedule_type must be 'cron' or 'at'"
    if schedule_type == "cron":
        if not cron_expr:
            return "cron_expr is required when schedule_type is 'cron'"
        from croniter import croniter

        if not croniter.is_valid(cron_expr):
            return f"Invalid cron expression: {cron_expr}"
    if schedule_type == "at":
        if not at_time:
            return "at_time is required when schedule_type is 'at'"
        from datetime import UTC, datetime

        try:
            dt = datetime.fromisoformat(at_time)
            if dt.tzinfo is None:
                return (
                    "at_time must include a timezone offset (e.g. 2024-01-01T12:00:00Z or +00:00)"
                )
            if dt <= datetime.now(UTC):
                return "at_time must be in the future"
        except ValueError:
            return "at_time must be a valid ISO8601 timestamp with timezone"
    return None


async def admin_list_schedules(request: Request) -> JSONResponse:
    """GET /v1/api/admin/schedules — list all scheduled tasks."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.schedules")
    if err:
        return err
    tasks = storage.list_scheduled_tasks()
    for t in tasks:
        _normalize_task_dict(t)
    return JSONResponse({"schedules": tasks})


async def admin_create_schedule(request: Request) -> JSONResponse:
    """POST /v1/api/admin/schedules — create a scheduled task."""
    import uuid

    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.schedules")
    if err:
        return err

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    name = str(body.get("name", "")).strip()[:256]
    description = str(body.get("description", "")).strip()[:1024]
    schedule_type = str(body.get("schedule_type", "")).strip()
    cron_expr = str(body.get("cron_expr", "")).strip()[:256]
    at_time = str(body.get("at_time", "")).strip()[:64]
    target_mode = str(body.get("target_mode", "auto")).strip()[:256]
    model = str(body.get("model", "")).strip()[:128]
    initial_message = str(body.get("initial_message", "")).strip()[:4096]
    auto_approve = bool(body.get("auto_approve", False))
    raw_tools = body.get("auto_approve_tools", [])
    auto_approve_tools = raw_tools if isinstance(raw_tools, list) else []
    skill_name = str(body.get("skill", "")).strip()[:256]
    enabled = bool(body.get("enabled", True))

    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    if not initial_message:
        return JSONResponse({"error": "initial_message is required"}, status_code=400)
    if skill_name and not storage.get_prompt_template_by_name(skill_name):
        return JSONResponse({"error": f"Skill not found: {skill_name}"}, status_code=400)

    validation_err = _validate_schedule_fields(schedule_type, cron_expr, at_time)
    if validation_err:
        return JSONResponse({"error": validation_err}, status_code=400)

    if not target_mode:
        return JSONResponse({"error": "target_mode is required"}, status_code=400)

    # Cap total schedule count to prevent unbounded growth
    max_schedules = 200
    existing = storage.list_scheduled_tasks()
    if len(existing) >= max_schedules:
        return JSONResponse(
            {"error": f"Maximum of {max_schedules} schedules reached"}, status_code=409
        )

    next_run = _compute_next_run(schedule_type, cron_expr, at_time)
    task_id = uuid.uuid4().hex
    created_by = getattr(getattr(request, "state", None), "user_id", "")

    storage.create_scheduled_task(
        task_id=task_id,
        name=name,
        description=description,
        schedule_type=schedule_type,
        cron_expr=cron_expr,
        at_time=at_time,
        target_mode=target_mode,
        model=model,
        initial_message=initial_message,
        auto_approve=auto_approve,
        auto_approve_tools=auto_approve_tools,
        created_by=created_by,
        next_run=next_run if enabled else "",
        skill=skill_name,
    )

    if not enabled:
        # Storage backends default enabled=1 on create; persist user's choice
        storage.update_scheduled_task(task_id, enabled=False)

    task = storage.get_scheduled_task(task_id)
    if task:
        _normalize_task_dict(task)
    return JSONResponse(task)


async def admin_get_schedule(request: Request) -> JSONResponse:
    """GET /v1/api/admin/schedules/{task_id} — get single task."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.schedules")
    if err:
        return err
    task_id = request.path_params["task_id"]
    task = storage.get_scheduled_task(task_id)
    if task is None:
        return JSONResponse({"error": "Schedule not found"}, status_code=404)
    _normalize_task_dict(task)
    return JSONResponse(task)


async def admin_update_schedule(request: Request) -> JSONResponse:
    """PUT /v1/api/admin/schedules/{task_id} — partial update."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.schedules")
    if err:
        return err
    task_id = request.path_params["task_id"]

    existing = storage.get_scheduled_task(task_id)
    if existing is None:
        return JSONResponse({"error": "Schedule not found"}, status_code=404)

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    updates: dict[str, Any] = {}
    if "name" in body:
        updates["name"] = str(body["name"]).strip()[:256]
    if "description" in body:
        updates["description"] = str(body["description"]).strip()[:1024]
    if "schedule_type" in body:
        updates["schedule_type"] = str(body["schedule_type"]).strip()
    if "cron_expr" in body:
        updates["cron_expr"] = str(body["cron_expr"]).strip()[:256]
    if "at_time" in body:
        updates["at_time"] = str(body["at_time"]).strip()[:64]
    if "target_mode" in body:
        updates["target_mode"] = str(body["target_mode"]).strip()[:256]
    if "model" in body:
        updates["model"] = str(body["model"]).strip()[:128]
    if "initial_message" in body:
        updates["initial_message"] = str(body["initial_message"]).strip()[:4096]
    if "auto_approve" in body:
        updates["auto_approve"] = bool(body["auto_approve"])
    if "auto_approve_tools" in body:
        raw = body["auto_approve_tools"]
        updates["auto_approve_tools"] = raw if isinstance(raw, list) else []
    if "skill" in body:
        skill_val = str(body["skill"]).strip()[:256]
        if skill_val and not storage.get_prompt_template_by_name(skill_val):
            return JSONResponse({"error": f"Skill not found: {skill_val}"}, status_code=400)
        updates["skill"] = skill_val
    if "enabled" in body:
        updates["enabled"] = bool(body["enabled"])

    # Validate schedule fields if changed
    stype = updates.get("schedule_type", existing["schedule_type"])
    cexpr = updates.get("cron_expr", existing["cron_expr"])
    atime = updates.get("at_time", existing["at_time"])
    schedule_fields_changed = (
        "schedule_type" in updates or "cron_expr" in updates or "at_time" in updates
    )
    if schedule_fields_changed:
        validation_err = _validate_schedule_fields(stype, cexpr, atime)
        if validation_err:
            return JSONResponse({"error": validation_err}, status_code=400)

    # Recompute next_run if schedule changed or enabled toggled
    if schedule_fields_changed or "enabled" in updates:
        enabled = updates.get("enabled", bool(existing.get("enabled", 1)))
        if enabled:
            # Re-validate at_time when re-enabling a one-shot task
            if stype == "at" and not schedule_fields_changed:
                validation_err = _validate_schedule_fields(stype, cexpr, atime)
                if validation_err:
                    return JSONResponse({"error": validation_err}, status_code=400)
            updates["next_run"] = _compute_next_run(stype, cexpr, atime)
        else:
            updates["next_run"] = ""

    storage.update_scheduled_task(task_id, **updates)
    task = storage.get_scheduled_task(task_id)
    if task:
        _normalize_task_dict(task)
    return JSONResponse(task)


async def admin_delete_schedule(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/schedules/{task_id} — delete task + runs."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.schedules")
    if err:
        return err
    task_id = request.path_params["task_id"]
    if storage.delete_scheduled_task(task_id):
        return JSONResponse({"status": "ok"})
    return JSONResponse({"error": "Schedule not found"}, status_code=404)


async def admin_list_schedule_runs(request: Request) -> JSONResponse:
    """GET /v1/api/admin/schedules/{task_id}/runs — run history."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.schedules")
    if err:
        return err
    task_id = request.path_params["task_id"]

    # Verify task exists
    if storage.get_scheduled_task(task_id) is None:
        return JSONResponse({"error": "Schedule not found"}, status_code=404)

    try:
        limit = max(1, min(int(request.query_params.get("limit", "50")), 200))
    except (ValueError, TypeError):
        limit = 50
    runs = storage.list_task_runs(task_id, limit=limit)
    return JSONResponse({"runs": runs})


# ---------------------------------------------------------------------------
# Admin API endpoints — watches (aggregated from nodes)
# ---------------------------------------------------------------------------


async def admin_list_watches(request: Request) -> JSONResponse:
    """GET /v1/api/admin/watches — aggregate watches from all nodes."""
    from turnstone.core.auth import require_permission

    err = require_permission(request, "admin.watches")
    if err:
        return err
    collector: ClusterCollector = request.app.state.collector
    nodes, _ = collector.get_nodes(limit=500)
    client: httpx.AsyncClient = request.app.state.proxy_client
    headers = _proxy_auth_headers(request)
    sem = asyncio.Semaphore(_NODE_FAN_OUT_LIMIT)

    async def _fetch_node(node: dict[str, Any]) -> list[dict[str, Any]]:
        server_url = (node.get("server_url") or "").rstrip("/")
        if not server_url:
            return []
        async with sem:
            try:
                resp = await client.get(f"{server_url}/v1/api/watches", headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    watches: list[dict[str, Any]] = data.get("watches", [])
                    # Tag each watch with node_id in case the server omits it
                    for w in watches:
                        if not w.get("node_id"):
                            w["node_id"] = node["node_id"]
                    return watches
            except Exception:
                log.debug(
                    "Failed to fetch watches from node %s",
                    node.get("node_id"),
                    exc_info=True,
                )
        return []

    tasks = [_fetch_node(n) for n in nodes]
    results = await asyncio.gather(*tasks)
    all_watches: list[dict[str, Any]] = []
    for batch in results:
        all_watches.extend(batch)
    # Sort: active first, then by created descending (stable sort trick)
    all_watches.sort(key=lambda w: w.get("created", ""), reverse=True)
    all_watches.sort(key=lambda w: not w.get("active", False))
    return JSONResponse({"watches": all_watches})


_VALID_WATCH_ID = re.compile(r"^[a-fA-F0-9]+$")

# Max concurrent outbound requests when fanning out to cluster nodes.
# Sized below the default httpx pool limit (100) to leave headroom for
# other proxy traffic (UI proxying, SSE streams, etc.).
_NODE_FAN_OUT_LIMIT = 50


async def admin_cancel_watch(request: Request) -> Response:
    """POST /v1/api/admin/watches/{watch_id}/cancel — proxy cancel to the owning node."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400

    err = require_permission(request, "admin.watches")
    if err:
        return err
    watch_id = request.path_params["watch_id"]
    if not watch_id or not _VALID_WATCH_ID.match(watch_id) or len(watch_id) > 128:
        return JSONResponse({"error": "Invalid watch_id"}, status_code=400)

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    node_id = str(body.get("node_id", "") or request.query_params.get("node_id", "")).strip()
    if not node_id:
        return JSONResponse({"error": "node_id is required"}, status_code=400)

    server_url = _get_server_url(request, node_id)
    if not server_url:
        return JSONResponse({"error": "Node not found"}, status_code=404)

    client: httpx.AsyncClient = request.app.state.proxy_client
    headers = {"Content-Type": "application/json"}
    headers.update(_proxy_auth_headers(request))
    try:
        resp = await client.post(
            f"{server_url}/v1/api/watches/{watch_id}/cancel",
            content=b"{}",
            headers=headers,
        )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )
    except httpx.HTTPError:
        return JSONResponse({"error": "Node unreachable"}, status_code=502)


# ---------------------------------------------------------------------------
# Admin API endpoints — governance (roles, orgs, policies, templates, usage, audit)
# ---------------------------------------------------------------------------


def _audit_context(request: Request) -> tuple[str, str]:
    """Extract (user_id, ip_address) from request for audit logging.

    Honors ``X-Forwarded-For`` only when the request appears to come
    through a trusted proxy (``X-Forwarded-Proto`` is set), matching the
    existing ``is_secure_request()`` trust model.  Falls back to
    ``request.client.host`` otherwise.
    """
    from turnstone.core.auth import is_secure_request

    auth_result = getattr(request.state, "auth_result", None)
    user_id = auth_result.user_id if auth_result else ""
    ip = ""
    # Only trust X-Forwarded-For when behind a proxy that sets X-Forwarded-Proto
    if is_secure_request(dict(request.headers), request.url.scheme):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            ip = forwarded.split(",")[0].strip()
    if not ip:
        ip = request.client.host if request.client else ""
    return user_id, ip


_VALID_PERMISSIONS = frozenset(
    {
        "read",
        "write",
        "approve",
        "admin.users",
        "admin.roles",
        "admin.orgs",
        "admin.policies",
        "admin.skills",
        "admin.audit",
        "admin.usage",
        "admin.schedules",
        "admin.watches",
        "admin.judge",
        "admin.memories",
        "admin.settings",
        "admin.mcp",
        "tools.approve",
        "workstreams.create",
        "workstreams.close",
    }
)


async def admin_list_roles(request: Request) -> JSONResponse:
    """GET /v1/api/admin/roles — list all roles."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.roles")
    if err:
        return err
    return JSONResponse({"roles": storage.list_roles()})


async def admin_create_role(request: Request) -> JSONResponse:
    """POST /v1/api/admin/roles — create a new role."""
    import uuid

    from turnstone.core.audit import record_audit
    from turnstone.core.auth import is_valid_username, require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.roles")
    if err:
        return err

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    name = str(body.get("name", "")).strip()[:128]
    display_name = str(body.get("display_name", "")).strip()[:256]
    permissions = str(body.get("permissions", "")).strip()

    if not is_valid_username(name):
        return JSONResponse(
            {"error": "Invalid name (1-64 chars: letters, digits, . _ -)"},
            status_code=400,
        )
    if not display_name:
        display_name = name

    # Validate permissions against the allowed set
    if permissions:
        perm_list = [p.strip() for p in permissions.split(",") if p.strip()]
        invalid = [p for p in perm_list if p not in _VALID_PERMISSIONS]
        if invalid:
            return JSONResponse(
                {"error": f"Invalid permissions: {', '.join(invalid)}"},
                status_code=400,
            )

    # Check for duplicate name
    if storage.get_role_by_name(name) is not None:
        return JSONResponse({"error": f"Role '{name}' already exists"}, status_code=409)

    role_id = uuid.uuid4().hex
    storage.create_role(
        role_id=role_id,
        name=name,
        display_name=display_name,
        permissions=permissions,
        builtin=False,
        org_id="",
    )

    audit_uid, ip = _audit_context(request)
    record_audit(storage, audit_uid, "role.create", "role", role_id, {"name": name}, ip)

    role = storage.get_role(role_id)
    if role is None:
        return JSONResponse({"error": "Role creation failed"}, status_code=500)
    return JSONResponse(role)


async def admin_update_role(request: Request) -> JSONResponse:
    """PUT /v1/api/admin/roles/{role_id} — update a custom role."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.roles")
    if err:
        return err

    role_id = request.path_params["role_id"]
    existing = storage.get_role(role_id)
    if existing is None:
        return JSONResponse({"error": "Role not found"}, status_code=404)
    if existing.get("builtin"):
        return JSONResponse({"error": "Cannot modify builtin role"}, status_code=400)

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    updates: dict[str, Any] = {}
    if "display_name" in body:
        updates["display_name"] = str(body["display_name"]).strip()[:256]
    if "permissions" in body:
        raw_perms = str(body["permissions"]).strip()
        if raw_perms:
            perm_list = [p.strip() for p in raw_perms.split(",") if p.strip()]
            invalid = [p for p in perm_list if p not in _VALID_PERMISSIONS]
            if invalid:
                return JSONResponse(
                    {"error": f"Invalid permissions: {', '.join(invalid)}"},
                    status_code=400,
                )
        updates["permissions"] = raw_perms

    storage.update_role(role_id, **updates)

    audit_uid, ip = _audit_context(request)
    record_audit(storage, audit_uid, "role.update", "role", role_id, updates, ip)

    role = storage.get_role(role_id)
    return JSONResponse(role)


async def admin_delete_role(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/roles/{role_id} — delete a custom role."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.roles")
    if err:
        return err

    role_id = request.path_params["role_id"]
    existing = storage.get_role(role_id)
    if existing is None:
        return JSONResponse({"error": "Role not found"}, status_code=404)
    if existing.get("builtin"):
        return JSONResponse({"error": "Cannot delete builtin role"}, status_code=400)

    storage.delete_role(role_id)

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "role.delete",
        "role",
        role_id,
        {"name": existing.get("name", "")},
        ip,
    )

    return JSONResponse({"status": "ok"})


async def admin_list_user_roles(request: Request) -> JSONResponse:
    """GET /v1/api/admin/users/{user_id}/roles — list roles assigned to a user."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err

    user_id = request.path_params["user_id"]
    return JSONResponse({"roles": storage.list_user_roles(user_id)})


async def admin_assign_role(request: Request) -> JSONResponse:
    """POST /v1/api/admin/users/{user_id}/roles — assign a role to a user."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err

    user_id = request.path_params["user_id"]

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    role_id = str(body.get("role_id", "")).strip()
    if not role_id:
        return JSONResponse({"error": "role_id is required"}, status_code=400)

    audit_uid, ip = _audit_context(request)

    # Validate that user exists
    if storage.get_user(user_id) is None:
        return JSONResponse({"error": "User not found"}, status_code=404)

    # Validate that role exists
    target_role = storage.get_role(role_id)
    if target_role is None:
        return JSONResponse({"error": "Role not found"}, status_code=404)

    # Prevent self-assignment
    auth_result = getattr(request.state, "auth_result", None)
    if auth_result and auth_result.user_id == user_id:
        return JSONResponse({"error": "Cannot modify own role assignments"}, status_code=403)

    # Ensure caller holds all permissions present in the target role
    target_perms = set(
        p.strip() for p in target_role.get("permissions", "").split(",") if p.strip()
    )
    if (
        auth_result
        and auth_result.permissions
        and not target_perms.issubset(auth_result.permissions)
    ):
        return JSONResponse(
            {"error": "Cannot assign role with permissions you do not hold"},
            status_code=403,
        )

    storage.assign_role(user_id, role_id, assigned_by=audit_uid)
    record_audit(
        storage,
        audit_uid,
        "role.assign",
        "user",
        user_id,
        {"role_id": role_id},
        ip,
    )

    return JSONResponse({"status": "ok"})


async def admin_unassign_role(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/users/{user_id}/roles/{role_id} — unassign a role."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err

    user_id = request.path_params["user_id"]
    role_id = request.path_params["role_id"]

    audit_uid, ip = _audit_context(request)

    # Prevent self-modification
    if audit_uid and audit_uid == user_id:
        return JSONResponse({"error": "Cannot modify own role assignments"}, status_code=403)

    if storage.unassign_role(user_id, role_id):
        record_audit(
            storage,
            audit_uid,
            "role.unassign",
            "user",
            user_id,
            {"role_id": role_id},
            ip,
        )
        return JSONResponse({"status": "ok"})
    return JSONResponse({"error": "Role assignment not found"}, status_code=404)


async def admin_list_orgs(request: Request) -> JSONResponse:
    """GET /v1/api/admin/orgs — list all organizations."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.orgs")
    if err:
        return err
    return JSONResponse({"orgs": storage.list_orgs()})


async def admin_get_org(request: Request) -> JSONResponse:
    """GET /v1/api/admin/orgs/{org_id} — get a single organization."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.orgs")
    if err:
        return err

    org_id = request.path_params["org_id"]
    org = storage.get_org(org_id)
    if org is None:
        return JSONResponse({"error": "Organization not found"}, status_code=404)
    return JSONResponse(org)


async def admin_update_org(request: Request) -> JSONResponse:
    """PUT /v1/api/admin/orgs/{org_id} — update an organization."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.orgs")
    if err:
        return err

    org_id = request.path_params["org_id"]
    existing = storage.get_org(org_id)
    if existing is None:
        return JSONResponse({"error": "Organization not found"}, status_code=404)

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    updates: dict[str, Any] = {}
    if "display_name" in body:
        updates["display_name"] = str(body["display_name"]).strip()[:256]
    if "settings" in body:
        settings_str = str(body["settings"]).strip()
        try:
            json.loads(settings_str)
        except (json.JSONDecodeError, TypeError):
            return JSONResponse({"error": "settings must be valid JSON"}, status_code=400)
        updates["settings"] = settings_str

    storage.update_org(org_id, **updates)

    audit_uid, ip = _audit_context(request)
    record_audit(storage, audit_uid, "org.update", "org", org_id, updates, ip)

    org = storage.get_org(org_id)
    return JSONResponse(org)


async def admin_list_policies(request: Request) -> JSONResponse:
    """GET /v1/api/admin/policies — list all tool policies."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.policies")
    if err:
        return err
    return JSONResponse({"policies": storage.list_tool_policies()})


async def admin_create_policy(request: Request) -> JSONResponse:
    """POST /v1/api/admin/policies — create a tool policy."""
    import uuid

    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.policies")
    if err:
        return err

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    name = str(body.get("name", "")).strip()[:256]
    tool_pattern = str(body.get("tool_pattern", "")).strip()[:256]
    action = str(body.get("action", "")).strip().lower()
    priority = int(body.get("priority", 0)) if isinstance(body.get("priority"), (int, float)) else 0
    org_id = str(body.get("org_id", "")).strip()[:64]
    enabled = bool(body.get("enabled", True))

    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    if not tool_pattern:
        return JSONResponse({"error": "tool_pattern is required"}, status_code=400)
    if action not in ("allow", "deny", "ask"):
        return JSONResponse(
            {"error": "action must be one of: allow, deny, ask"},
            status_code=400,
        )

    audit_uid, ip = _audit_context(request)

    policy_id = uuid.uuid4().hex
    storage.create_tool_policy(
        policy_id=policy_id,
        name=name,
        tool_pattern=tool_pattern,
        action=action,
        priority=priority,
        org_id=org_id,
        enabled=enabled,
        created_by=audit_uid,
    )

    record_audit(
        storage,
        audit_uid,
        "policy.create",
        "policy",
        policy_id,
        {"name": name},
        ip,
    )

    policy = storage.get_tool_policy(policy_id)
    return JSONResponse(policy)


async def admin_update_policy(request: Request) -> JSONResponse:
    """PUT /v1/api/admin/policies/{policy_id} — update a tool policy."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.policies")
    if err:
        return err

    policy_id = request.path_params["policy_id"]
    existing = storage.get_tool_policy(policy_id)
    if existing is None:
        return JSONResponse({"error": "Policy not found"}, status_code=404)

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    updates: dict[str, Any] = {}
    if "name" in body:
        updates["name"] = str(body["name"]).strip()[:256]
    if "tool_pattern" in body:
        updates["tool_pattern"] = str(body["tool_pattern"]).strip()[:256]
    if "action" in body:
        act = str(body["action"]).strip().lower()
        if act not in ("allow", "deny", "ask"):
            return JSONResponse(
                {"error": "action must be one of: allow, deny, ask"},
                status_code=400,
            )
        updates["action"] = act
    if "priority" in body:
        updates["priority"] = (
            int(body["priority"]) if isinstance(body["priority"], (int, float)) else 0
        )
    if "enabled" in body:
        updates["enabled"] = bool(body["enabled"])

    storage.update_tool_policy(policy_id, **updates)

    audit_uid, ip = _audit_context(request)
    record_audit(storage, audit_uid, "policy.update", "policy", policy_id, updates, ip)

    policy = storage.get_tool_policy(policy_id)
    return JSONResponse(policy)


async def admin_delete_policy(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/policies/{policy_id} — delete a tool policy."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.policies")
    if err:
        return err

    policy_id = request.path_params["policy_id"]
    existing = storage.get_tool_policy(policy_id)
    if existing is None:
        return JSONResponse({"error": "Policy not found"}, status_code=404)

    storage.delete_tool_policy(policy_id)

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "policy.delete",
        "policy",
        policy_id,
        {"name": existing.get("name", "")},
        ip,
    )

    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Admin: Skills (thin layer over prompt templates with extended fields)
# ---------------------------------------------------------------------------

_VALID_ACTIVATIONS = {"named", "default", "search"}


def _parse_skill_session_config(body: dict[str, Any]) -> tuple[dict[str, Any], JSONResponse | None]:
    """Parse and validate session config fields from a skill request body.

    Returns (fields_dict, error_response). error_response is None on success.
    Only includes fields that are present in the body (for partial updates).
    """
    import json as _json

    fields: dict[str, Any] = {}

    if "model" in body:
        fields["model"] = str(body["model"] or "").strip()

    if "temperature" in body:
        temp = body["temperature"]
        if temp is not None and temp != "":
            try:
                temp = float(temp)
                if not (0.0 <= temp <= 2.0):
                    return {}, JSONResponse(
                        {"error": "temperature must be between 0 and 2"}, status_code=400
                    )
                fields["temperature"] = temp
            except (ValueError, TypeError):
                fields["temperature"] = None
        else:
            fields["temperature"] = None

    if "token_budget" in body:
        try:
            tb = int(body.get("token_budget", 0) or 0)
        except (ValueError, TypeError):
            return {}, JSONResponse({"error": "token_budget must be an integer"}, status_code=400)
        if tb < 0:
            return {}, JSONResponse({"error": "token_budget must be non-negative"}, status_code=400)
        fields["token_budget"] = tb

    if "max_tokens" in body:
        mt = body["max_tokens"]
        if mt is not None and mt != "":
            try:
                mt = int(mt)
            except (ValueError, TypeError):
                return {}, JSONResponse({"error": "max_tokens must be an integer"}, status_code=400)
            if mt < 1:
                return {}, JSONResponse({"error": "max_tokens must be positive"}, status_code=400)
            fields["max_tokens"] = mt
        else:
            fields["max_tokens"] = None

    if "agent_max_turns" in body:
        amt = body["agent_max_turns"]
        if amt is not None and amt != "":
            try:
                amt = int(amt)
            except (ValueError, TypeError):
                return {}, JSONResponse(
                    {"error": "agent_max_turns must be an integer"}, status_code=400
                )
            if amt < 1:
                return {}, JSONResponse(
                    {"error": "agent_max_turns must be positive"}, status_code=400
                )
            fields["agent_max_turns"] = amt
        else:
            fields["agent_max_turns"] = None

    if "reasoning_effort" in body:
        fields["reasoning_effort"] = str(body["reasoning_effort"] or "").strip()

    if "auto_approve" in body:
        fields["auto_approve"] = bool(body.get("auto_approve", False))

    if "enabled" in body:
        fields["enabled"] = bool(body.get("enabled", True))

    if "activation" in body:
        activation = str(body["activation"] or "named").strip()
        if activation not in _VALID_ACTIVATIONS:
            return {}, JSONResponse(
                {"error": f"activation must be one of: {', '.join(sorted(_VALID_ACTIVATIONS))}"},
                status_code=400,
            )
        fields["activation"] = activation

    if "notify_on_complete" in body:
        nc = str(body.get("notify_on_complete", "{}")).strip()
        if nc and nc != "{}":
            try:
                _json.loads(nc)
            except (_json.JSONDecodeError, TypeError):
                return {}, JSONResponse(
                    {"error": "notify_on_complete must be valid JSON"}, status_code=400
                )
        fields["notify_on_complete"] = nc

    if "allowed_tools" in body:
        at_raw = body.get("allowed_tools", "[]")
        if isinstance(at_raw, list):
            fields["allowed_tools"] = _json.dumps(at_raw)
        else:
            at_str = str(at_raw).strip()
            if at_str and not at_str.startswith("["):
                at_str = _json.dumps([t.strip() for t in at_str.split(",") if t.strip()])
            try:
                _json.loads(at_str or "[]")
            except (ValueError, TypeError):
                at_str = "[]"
            fields["allowed_tools"] = at_str or "[]"

    return fields, None


def _skill_to_response(r: dict[str, Any], resource_count: int = 0) -> dict[str, Any]:
    """Convert a storage skill dict to a JSON-safe response dict."""
    import contextlib
    import json as _json

    tags: list[str] = []
    with contextlib.suppress(ValueError, TypeError):
        tags = _json.loads(r.get("tags", "[]"))
    return {
        "template_id": r.get("template_id", ""),
        "name": r.get("name", ""),
        "category": r.get("category", ""),
        "description": r.get("description", ""),
        "content": r.get("content", ""),
        "tags": tags,
        "is_default": r.get("is_default", False),
        "activation": r.get("activation", "named"),
        "origin": r.get("origin", "manual"),
        "mcp_server": r.get("mcp_server", ""),
        "readonly": r.get("readonly", False),
        "author": r.get("author", ""),
        "version": r.get("version", "1.0.0"),
        "variables": r.get("variables", "[]"),
        "token_estimate": r.get("token_estimate", 0),
        "source_url": r.get("source_url", ""),
        "org_id": r.get("org_id", ""),
        "created_by": r.get("created_by", ""),
        # Session config fields
        "model": r.get("model", ""),
        "auto_approve": r.get("auto_approve", False),
        "temperature": r.get("temperature"),
        "reasoning_effort": r.get("reasoning_effort", ""),
        "max_tokens": r.get("max_tokens"),
        "token_budget": r.get("token_budget", 0),
        "agent_max_turns": r.get("agent_max_turns"),
        "notify_on_complete": r.get("notify_on_complete", "{}"),
        "enabled": r.get("enabled", True),
        "allowed_tools": r.get("allowed_tools", "[]"),
        "scan_status": r.get("scan_status", ""),
        "scan_report": r.get("scan_report", "{}"),
        "scan_version": r.get("scan_version", ""),
        "resource_count": resource_count,
        "created": r.get("created", ""),
        "updated": r.get("updated", ""),
    }


async def admin_list_skills(request: Request) -> JSONResponse:
    """GET /v1/api/admin/skills — list all skills."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err
    params = dict(request.query_params)
    limit = _parse_int(params, "limit", 0, minimum=0, maximum=10000)
    offset = _parse_int(params, "offset", 0, minimum=0, maximum=100000)
    rows = storage.list_prompt_templates(limit=limit, offset=offset)
    total = storage.count_prompt_templates()
    skill_ids = [r["template_id"] for r in rows]
    rc_map = storage.count_skill_resources_bulk(skill_ids) if skill_ids else {}
    skills = [_skill_to_response(r, resource_count=rc_map.get(r["template_id"], 0)) for r in rows]
    return JSONResponse({"skills": skills, "total": total})


async def admin_get_skill(request: Request) -> JSONResponse:
    """GET /v1/api/admin/skills/{skill_id} — get a single skill."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    skill_id = request.path_params["skill_id"]
    skill = storage.get_prompt_template(skill_id)
    if skill is None:
        return JSONResponse({"error": "Skill not found"}, status_code=404)
    rc = len(storage.list_skill_resources(skill_id))
    return JSONResponse(_skill_to_response(skill, resource_count=rc))


async def admin_create_skill(request: Request) -> JSONResponse:
    """POST /v1/api/admin/skills — create a skill."""
    import json as _json
    import uuid

    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    name = str(body.get("name", "")).strip()[:256]
    content = str(body.get("content", "")).strip()[:32768]
    category = str(body.get("category", "general")).strip()[:64]
    description = str(body.get("description", "")).strip()[:1024]
    variables = str(body.get("variables", "[]")).strip()
    try:
        _json.loads(variables)
    except (_json.JSONDecodeError, TypeError):
        return JSONResponse({"error": "variables must be a valid JSON array"}, status_code=400)
    is_default = bool(body.get("is_default", False))
    org_id = str(body.get("org_id", "")).strip()[:64]
    author = str(body.get("author", "")).strip()[:256]
    version = str(body.get("version", "1.0.0")).strip()[:64]

    raw_tags = body.get("tags", [])
    if isinstance(raw_tags, list):
        tags_str = _json.dumps(raw_tags)
    else:
        tags_str = str(raw_tags).strip()
        try:
            _json.loads(tags_str)
        except (ValueError, TypeError):
            tags_str = "[]"

    token_estimate = len(content) // 4 if content else 0

    # Session config fields via shared helper
    session_fields, session_err = _parse_skill_session_config(body)
    if session_err:
        return session_err

    # Resolve activation / is_default sync
    activation = session_fields.pop("activation", "")
    if not activation:
        activation = "default" if is_default else "named"
    if activation == "default":
        is_default = True

    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    if not content:
        return JSONResponse({"error": "content is required"}, status_code=400)
    if storage.get_prompt_template_by_name(name):
        return JSONResponse({"error": "Skill name already exists"}, status_code=409)

    audit_uid, ip = _audit_context(request)

    skill_id = uuid.uuid4().hex
    storage.create_prompt_template(
        template_id=skill_id,
        name=name,
        category=category,
        content=content,
        variables=variables,
        is_default=is_default,
        org_id=org_id,
        created_by=audit_uid,
        description=description,
        tags=tags_str,
        version=version,
        author=author,
        activation=activation,
        token_estimate=token_estimate,
        **session_fields,
    )

    record_audit(
        storage,
        audit_uid,
        "skill.create",
        "skill",
        skill_id,
        {"name": name},
        ip,
    )

    skill = storage.get_prompt_template(skill_id)
    return JSONResponse(_skill_to_response(skill))


async def admin_update_skill(request: Request) -> JSONResponse:
    """PUT /v1/api/admin/skills/{skill_id} — update a skill."""
    import json as _json

    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    skill_id = request.path_params["skill_id"]
    existing = storage.get_prompt_template(skill_id)
    if existing is None:
        return JSONResponse({"error": "Skill not found"}, status_code=404)
    if existing.get("readonly"):
        return JSONResponse({"error": "MCP-sourced skills are read-only"}, status_code=403)

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    # Session config fields via shared helper
    session_fields, session_err = _parse_skill_session_config(body)
    if session_err:
        return session_err

    updates: dict[str, Any] = dict(session_fields)
    if "name" in body:
        updates["name"] = str(body["name"]).strip()[:256]
        existing_by_name = storage.get_prompt_template_by_name(updates["name"])
        if existing_by_name and existing_by_name["template_id"] != skill_id:
            return JSONResponse({"error": "Skill name already exists"}, status_code=409)
    if "content" in body:
        content = str(body["content"]).strip()[:32768]
        updates["content"] = content
        updates["token_estimate"] = len(content) // 4 if content else 0
    if "category" in body:
        updates["category"] = str(body["category"]).strip()[:64]
    if "description" in body:
        updates["description"] = str(body["description"]).strip()[:1024]
    if "variables" in body:
        var_str = str(body["variables"]).strip()
        try:
            _json.loads(var_str)
        except (_json.JSONDecodeError, TypeError):
            return JSONResponse({"error": "variables must be a valid JSON array"}, status_code=400)
        updates["variables"] = var_str
    if "is_default" in body:
        updates["is_default"] = bool(body["is_default"])
    if "activation" in updates and updates["activation"] == "default":
        updates["is_default"] = True
    if "author" in body:
        updates["author"] = str(body["author"]).strip()[:256]
    if "version" in body:
        updates["version"] = str(body["version"]).strip()[:64]
    if "tags" in body:
        raw_tags = body["tags"]
        if isinstance(raw_tags, list):
            updates["tags"] = _json.dumps(raw_tags)
        else:
            tag_str = str(raw_tags).strip()
            try:
                _json.loads(tag_str)
            except (ValueError, TypeError):
                tag_str = "[]"
            updates["tags"] = tag_str

    # Snapshot current state for version history before applying update
    existing_versions = storage.list_skill_versions(skill_id)
    version_int = len(existing_versions) + 1
    audit_uid_pre, _ = _audit_context(request)
    storage.create_skill_version(
        skill_id=skill_id,
        version=version_int,
        snapshot=_json.dumps(existing, default=str),
        changed_by=audit_uid_pre,
    )

    storage.update_prompt_template(skill_id, **updates)

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "skill.update",
        "skill",
        skill_id,
        updates,
        ip,
    )

    updated_skill = storage.get_prompt_template(skill_id)
    return JSONResponse(_skill_to_response(updated_skill))


async def admin_delete_skill(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/skills/{skill_id} — delete a skill."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    skill_id = request.path_params["skill_id"]
    existing = storage.get_prompt_template(skill_id)
    if existing is None:
        return JSONResponse({"error": "Skill not found"}, status_code=404)
    if existing.get("readonly"):
        return JSONResponse({"error": "MCP-sourced skills are read-only"}, status_code=403)

    storage.delete_skill_resources(skill_id)
    storage.delete_skill_versions(skill_id)
    storage.delete_prompt_template(skill_id)

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "skill.delete",
        "skill",
        skill_id,
        {"name": existing.get("name", "")},
        ip,
    )

    return JSONResponse({"status": "ok"})


async def admin_list_skill_versions(request: Request) -> JSONResponse:
    """GET /v1/api/admin/skills/{skill_id}/versions — version history."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    skill_id = request.path_params["skill_id"]
    versions = storage.list_skill_versions(skill_id)
    return JSONResponse({"versions": versions})


async def list_skills_summary(request: Request) -> JSONResponse:
    """GET /v1/api/skills — list available skills (summary)."""
    import contextlib
    import json as _json

    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    rows = storage.list_prompt_templates()
    skills = []
    for r in rows:
        if not r.get("enabled", True):
            continue
        tags: list[str] = []
        with contextlib.suppress(ValueError, TypeError):
            tags = _json.loads(r.get("tags", "[]"))
        skills.append(
            {
                "name": r["name"],
                "category": r.get("category", ""),
                "description": r.get("description", ""),
                "tags": tags,
                "is_default": r.get("is_default", False),
                "activation": r.get("activation", "named"),
                "origin": r.get("origin", "manual"),
                "author": r.get("author", ""),
                "version": r.get("version", "1.0.0"),
            }
        )
    return JSONResponse({"skills": skills})


async def admin_usage(request: Request) -> JSONResponse:
    """GET /v1/api/admin/usage — query usage data."""
    from datetime import UTC, datetime, timedelta

    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.usage")
    if err:
        return err

    params = dict(request.query_params)
    since = params.get("since", "")
    until = params.get("until", "")
    user_id = params.get("user_id", "")
    model = params.get("model", "")
    group_by = params.get("group_by", "day")

    if group_by not in ("day", "hour", "model", "user"):
        return JSONResponse(
            {"error": "group_by must be one of: day, hour, model, user"},
            status_code=400,
        )

    if not since:
        since = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")

    summary = storage.query_usage(since=since, until=until, user_id=user_id, model=model)
    breakdown = storage.query_usage(
        since=since,
        until=until,
        user_id=user_id,
        model=model,
        group_by=group_by,
    )

    return JSONResponse({"summary": summary, "breakdown": breakdown})


async def admin_audit(request: Request) -> JSONResponse:
    """GET /v1/api/admin/audit — query audit events."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.audit")
    if err:
        return err

    params = dict(request.query_params)
    action = params.get("action", "")
    user_id = params.get("user_id", "")
    since = params.get("since", "")
    until = params.get("until", "")
    try:
        limit = max(1, min(int(params.get("limit", "50")), 200))
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = max(int(params.get("offset", "0")), 0)
    except (ValueError, TypeError):
        offset = 0

    events = storage.list_audit_events(
        action=action,
        user_id=user_id,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )
    total = storage.count_audit_events(
        action=action,
        user_id=user_id,
        since=since,
        until=until,
    )

    return JSONResponse({"events": events, "total": total})


async def admin_list_verdicts(request: Request) -> JSONResponse:
    """GET /v1/api/admin/verdicts — list intent verdicts."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.judge")
    if err:
        return err

    params = dict(request.query_params)
    ws_id = params.get("ws_id", "")
    since = params.get("since", "")
    until = params.get("until", "")
    risk_level = params.get("risk_level", "")
    try:
        limit = max(1, min(int(params.get("limit", "100")), 500))
    except (ValueError, TypeError):
        limit = 100
    try:
        offset = max(int(params.get("offset", "0")), 0)
    except (ValueError, TypeError):
        offset = 0

    verdicts = storage.list_intent_verdicts(
        ws_id=ws_id,
        since=since,
        until=until,
        risk_level=risk_level,
        limit=limit,
        offset=offset,
    )

    total = storage.count_intent_verdicts(
        ws_id=ws_id,
        since=since,
        until=until,
        risk_level=risk_level,
    )
    return JSONResponse({"verdicts": verdicts, "total": total})


async def admin_list_output_assessments(request: Request) -> JSONResponse:
    """GET /v1/api/admin/output-assessments — list output guard assessments."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.judge")
    if err:
        return err

    params = dict(request.query_params)
    ws_id = params.get("ws_id", "")
    risk_level = params.get("risk_level", "")
    since = params.get("since", "")
    until = params.get("until", "")
    try:
        limit = max(1, min(int(params.get("limit", "100")), 500))
    except (ValueError, TypeError):
        limit = 100
    try:
        offset = max(int(params.get("offset", "0")), 0)
    except (ValueError, TypeError):
        offset = 0

    assessments = storage.list_output_assessments(
        ws_id=ws_id,
        risk_level=risk_level,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )
    total = storage.count_output_assessments(
        ws_id=ws_id, risk_level=risk_level, since=since, until=until
    )
    return JSONResponse({"assessments": assessments, "total": total})


async def admin_rescan_skill(request: Request) -> JSONResponse:
    """POST /v1/api/admin/skills/{skill_id}/rescan — re-scan skill security."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    skill_id = request.path_params["skill_id"]
    skill = storage.get_prompt_template(skill_id)
    if not skill:
        return JSONResponse({"error": "Skill not found"}, status_code=404)

    from turnstone.core.storage._utils import scan_skill_content

    content = skill.get("content", "")
    allowed_tools = skill.get("allowed_tools", "[]")
    scan_status, scan_report, scan_version = scan_skill_content(content, allowed_tools)
    storage.update_prompt_template(
        skill_id,
        scan_status=scan_status,
        scan_report=scan_report,
        scan_version=scan_version,
    )
    return JSONResponse(
        {
            "scan_status": scan_status,
            "scan_report": scan_report,
            "scan_version": scan_version,
        }
    )


# ---------------------------------------------------------------------------
# Admin: Skill Resources
# ---------------------------------------------------------------------------

_ALLOWED_RESOURCE_DIRS = ("scripts/", "references/", "assets/")
_MAX_RESOURCE_SIZE = 100 * 1024  # 100KB
_MAX_RESOURCES_PER_SKILL = 10


async def admin_list_skill_resources(request: Request) -> JSONResponse:
    """GET /v1/api/admin/skills/{skill_id}/resources — list resources."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    skill_id = request.path_params["skill_id"]
    skill = storage.get_prompt_template(skill_id)
    if skill is None:
        return JSONResponse({"error": "Skill not found"}, status_code=404)

    rows = storage.list_skill_resources(skill_id)
    resources = [
        {
            "resource_id": r.get("resource_id", ""),
            "skill_id": r.get("skill_id", ""),
            "path": r.get("path", ""),
            "content_type": r.get("content_type", "text/plain"),
            "size": len(r.get("content", "")),
            "created": r.get("created", ""),
        }
        for r in rows
    ]
    return JSONResponse({"resources": resources})


async def admin_get_skill_resource(request: Request) -> JSONResponse:
    """GET /v1/api/admin/skills/{skill_id}/resources/{path:path} — get one resource."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    skill_id = request.path_params["skill_id"]
    path = request.path_params["path"]
    resource = storage.get_skill_resource(skill_id, path)
    if resource is None:
        return JSONResponse({"error": "Resource not found"}, status_code=404)
    return JSONResponse(
        {
            "resource_id": resource.get("resource_id", ""),
            "skill_id": resource.get("skill_id", ""),
            "path": resource.get("path", ""),
            "content": resource.get("content", ""),
            "content_type": resource.get("content_type", "text/plain"),
            "size": len(resource.get("content", "")),
            "created": resource.get("created", ""),
        }
    )


async def admin_create_skill_resource(request: Request) -> JSONResponse:
    """POST /v1/api/admin/skills/{skill_id}/resources — upload resource."""
    import uuid

    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    skill_id = request.path_params["skill_id"]
    skill = storage.get_prompt_template(skill_id)
    if skill is None:
        return JSONResponse({"error": "Skill not found"}, status_code=404)
    if skill.get("readonly"):
        return JSONResponse({"error": "Installed skills are read-only"}, status_code=403)

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    path = str(body.get("path", "")).strip()
    content = str(body.get("content", ""))
    content_type = str(body.get("content_type", "text/plain")).strip()[:64]

    if not path:
        return JSONResponse({"error": "path is required"}, status_code=400)
    # Normalize and reject path traversal
    import posixpath

    path = posixpath.normpath(path)
    if ".." in path.split("/") or "\x00" in path:
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    if not any(path.startswith(d.rstrip("/")) for d in _ALLOWED_RESOURCE_DIRS):
        return JSONResponse(
            {"error": "path must start with scripts/, references/, or assets/"},
            status_code=400,
        )
    if len(content) > _MAX_RESOURCE_SIZE:
        return JSONResponse(
            {"error": f"Resource exceeds {_MAX_RESOURCE_SIZE // 1024}KB limit"},
            status_code=400,
        )

    existing = storage.list_skill_resources(skill_id)
    if len(existing) >= _MAX_RESOURCES_PER_SKILL:
        return JSONResponse(
            {"error": f"Maximum {_MAX_RESOURCES_PER_SKILL} resources per skill"},
            status_code=400,
        )
    if storage.get_skill_resource(skill_id, path) is not None:
        return JSONResponse({"error": "Resource path already exists"}, status_code=409)

    resource_id = uuid.uuid4().hex
    storage.create_skill_resource(
        resource_id=resource_id,
        skill_id=skill_id,
        path=path,
        content=content,
        content_type=content_type,
    )

    audit_uid, ip = _audit_context(request)
    record_audit(storage, audit_uid, "skill_resource.create", "skill", skill_id, {"path": path}, ip)

    created = storage.get_skill_resource(skill_id, path)
    return JSONResponse(
        {
            "resource_id": resource_id,
            "skill_id": skill_id,
            "path": path,
            "content_type": content_type,
            "size": len(content),
            "created": (created or {}).get("created", ""),
        },
        status_code=201,
    )


async def admin_delete_skill_resource(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/skills/{skill_id}/resources/{path:path} — delete resource."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    skill_id = request.path_params["skill_id"]
    skill = storage.get_prompt_template(skill_id)
    if skill is None:
        return JSONResponse({"error": "Skill not found"}, status_code=404)
    if skill.get("readonly"):
        return JSONResponse({"error": "Installed skills are read-only"}, status_code=403)

    path = request.path_params["path"]
    deleted = storage.delete_skill_resource_by_path(skill_id, path)
    if not deleted:
        return JSONResponse({"error": "Resource not found"}, status_code=404)

    audit_uid, ip = _audit_context(request)
    record_audit(storage, audit_uid, "skill_resource.delete", "skill", skill_id, {"path": path}, ip)

    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Admin: Skill Discovery
# ---------------------------------------------------------------------------


def _get_discovery_url(request: Request) -> str:
    """Get skills discovery URL from DB settings, config.toml, or default."""
    from turnstone.core.config import load_config
    from turnstone.core.skill_sources import DEFAULT_DISCOVERY_URL

    storage = getattr(request.app.state, "auth_storage", None)
    if storage:
        try:
            row = storage.get_system_setting("skills.discovery_url")
            if row:
                val = json.loads(row["value"])
                if val:
                    return str(val)
        except (KeyError, json.JSONDecodeError, TypeError, AttributeError):
            pass
    skills_cfg = load_config("skills")
    url = skills_cfg.get("discovery_url", "")
    if url:
        return str(url)
    return DEFAULT_DISCOVERY_URL


async def admin_skill_discover(request: Request) -> JSONResponse:
    """GET /v1/api/admin/skills/discover — search external skill registries."""
    from turnstone.core.auth import require_permission
    from turnstone.core.skill_sources import SkillSourceError, SkillsShClient
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    q = str(request.query_params.get("q", "")).strip()
    if not q:
        return JSONResponse({"error": "Search query is required"}, status_code=400)
    try:
        limit = max(1, min(int(request.query_params.get("limit", "20")), 100))
    except (ValueError, TypeError):
        limit = 20

    discovery_url = _get_discovery_url(request)
    client = SkillsShClient(base_url=discovery_url)
    try:
        listings = await client.search(query=q, limit=limit)
    except SkillSourceError as exc:
        return JSONResponse({"error": f"Discovery error: {exc}"}, status_code=502)

    # Mark which skills are already installed (by source_url match)
    installed_map: dict[str, dict[str, str]] = {}
    for row in storage.list_installed_skill_urls():
        installed_map[row["source_url"]] = {
            "scan_status": row.get("scan_status", ""),
            "template_id": row.get("template_id", ""),
        }

    skills_out = []
    for listing in listings:
        is_installed = listing.source_url in installed_map if listing.source_url else False
        entry: dict[str, Any] = {
            "id": listing.id,
            "name": listing.name,
            "description": listing.description,
            "author": listing.author,
            "source": listing.source,
            "source_url": listing.source_url,
            "install_count": listing.install_count,
            "tags": listing.tags,
            "installed": is_installed,
        }
        if is_installed and listing.source_url:
            info = installed_map[listing.source_url]
            entry["scan_status"] = info["scan_status"]
            entry["template_id"] = info["template_id"]
        skills_out.append(entry)

    return JSONResponse({"skills": skills_out})


async def admin_skill_install(request: Request) -> JSONResponse:
    """POST /v1/api/admin/skills/install — install a skill from external source."""
    import uuid

    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.skill_sources import (
        SkillNotFoundError,
        SkillSourceError,
        SkillsShClient,
        fetch_skill_from_github,
    )
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    source = str(body.get("source", "")).strip()
    if source not in ("skills.sh", "github"):
        return JSONResponse({"error": "source must be 'skills.sh' or 'github'"}, status_code=400)

    try:
        if source == "skills.sh":
            skill_id_param = str(body.get("skill_id", "")).strip()
            if not skill_id_param:
                return JSONResponse({"error": "skill_id is required"}, status_code=400)

            discovery_url = _get_discovery_url(request)
            client = SkillsShClient(base_url=discovery_url)
            github_url = await client.resolve_github_url(skill_id_param)
            package = await fetch_skill_from_github(github_url)
        else:
            url = str(body.get("url", "")).strip()
            if not url:
                return JSONResponse({"error": "url is required"}, status_code=400)
            package = await fetch_skill_from_github(url)
    except SkillNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except SkillSourceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    # Check for duplicate by source_url
    source_url = package.listing.source_url
    if source_url:
        existing = storage.get_skill_by_source_url(source_url)
        if existing:
            return JSONResponse(
                {"error": f"Skill from '{source_url}' is already installed"},
                status_code=409,
            )

    # Check for duplicate by name
    if storage.get_prompt_template_by_name(package.parsed.name):
        return JSONResponse(
            {"error": f"Skill name '{package.parsed.name}' already exists"},
            status_code=409,
        )

    import json as _json

    audit_uid, ip = _audit_context(request)
    skill_id = uuid.uuid4().hex
    parsed = package.parsed
    tags_str = _json.dumps(parsed.tags)
    allowed_tools_str = _json.dumps(parsed.allowed_tools)
    content = parsed.content[:32768]
    token_estimate = len(content) // 4 if content else 0

    storage.create_prompt_template(
        template_id=skill_id,
        name=parsed.name,
        category="general",
        content=content,
        variables="[]",
        is_default=False,
        org_id="",
        created_by=audit_uid,
        origin="source",
        readonly=True,
        description=parsed.description,
        tags=tags_str,
        source_url=source_url,
        version=parsed.version,
        author=parsed.author,
        activation="named",
        token_estimate=token_estimate,
        allowed_tools=allowed_tools_str,
    )

    # Store bundled resources
    for path, content in package.resources.items():
        storage.create_skill_resource(
            resource_id=uuid.uuid4().hex,
            skill_id=skill_id,
            path=path,
            content=content,
        )

    record_audit(
        storage,
        audit_uid,
        "skill.install",
        "skill",
        skill_id,
        {"name": parsed.name, "source": source, "source_url": source_url},
        ip,
    )

    skill = storage.get_prompt_template(skill_id)
    return JSONResponse(_skill_to_response(skill))


# ---------------------------------------------------------------------------
# Admin: Memories
# ---------------------------------------------------------------------------


def _validate_memory_scope_filter(scope: str, scope_id: str) -> JSONResponse | None:
    """Validate scope/scope_id consistency for memory queries."""
    scope = scope.strip()
    scope_id = scope_id.strip()
    if scope == "global" and scope_id:
        return JSONResponse({"error": "scope_id is not allowed with global scope"}, status_code=400)
    if scope_id and not scope:
        return JSONResponse(
            {"error": "scope is required when scope_id is provided"}, status_code=400
        )
    return None


async def admin_list_memories(request: Request) -> JSONResponse:
    """GET /v1/api/admin/memories — list structured memories with filters."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.memories")
    if err:
        return err

    mem_type = request.query_params.get("type", "")
    scope = request.query_params.get("scope", "")
    scope_id = request.query_params.get("scope_id", "")
    err = _validate_memory_scope_filter(scope, scope_id)
    if err:
        return err
    try:
        limit = max(1, min(int(request.query_params.get("limit", "100")), 200))
    except (ValueError, TypeError):
        return JSONResponse({"error": "limit must be an integer"}, status_code=400)

    rows = storage.list_structured_memories(
        mem_type=mem_type, scope=scope, scope_id=scope_id, limit=limit
    )
    total = storage.count_structured_memories(mem_type=mem_type, scope=scope, scope_id=scope_id)
    return JSONResponse({"memories": rows, "total": total})


async def admin_search_memories(request: Request) -> JSONResponse:
    """GET /v1/api/admin/memories/search — search memories by query."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.memories")
    if err:
        return err

    query = request.query_params.get("q", "").strip()
    if not query:
        return JSONResponse({"error": "q is required"}, status_code=400)
    mem_type = request.query_params.get("type", "")
    scope = request.query_params.get("scope", "")
    scope_id = request.query_params.get("scope_id", "")
    err = _validate_memory_scope_filter(scope, scope_id)
    if err:
        return err
    try:
        limit = max(1, min(int(request.query_params.get("limit", "20")), 50))
    except (ValueError, TypeError):
        return JSONResponse({"error": "limit must be an integer"}, status_code=400)

    rows = storage.search_structured_memories(
        query, mem_type=mem_type, scope=scope, scope_id=scope_id, limit=limit
    )
    return JSONResponse({"memories": rows, "total": len(rows)})


async def admin_get_memory(request: Request) -> JSONResponse:
    """GET /v1/api/admin/memories/{memory_id} — get a single memory."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.memories")
    if err:
        return err

    memory_id = request.path_params["memory_id"]
    mem = storage.get_structured_memory(memory_id)
    if not mem:
        return JSONResponse({"error": "Memory not found"}, status_code=404)
    return JSONResponse(mem)


async def admin_delete_memory(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/memories/{memory_id} — delete a memory by ID."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.memories")
    if err:
        return err

    memory_id = request.path_params["memory_id"]
    existing = storage.get_structured_memory(memory_id)
    if not existing:
        return JSONResponse({"error": "Memory not found"}, status_code=404)

    storage.delete_structured_memory_by_id(memory_id)

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "memory.delete",
        "memory",
        memory_id,
        {"name": existing.get("name", ""), "scope": existing.get("scope", "")},
        ip,
    )

    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Admin: System Settings
# ---------------------------------------------------------------------------


def _publish_config_change(request: Request, *, key: str, node_id: str, action: str) -> None:
    """Fan out config-reload to all known server nodes (best-effort).

    Uses the collector's node registry and the existing proxy auth
    mechanism — no MQ dependency.
    """
    import contextlib

    import httpx

    collector = getattr(request.app.state, "collector", None)
    if not collector:
        return
    headers = _proxy_auth_headers(request)
    with contextlib.suppress(Exception):
        nodes = collector.get_nodes()
        for node in nodes.get("nodes", []):
            url = node.get("url", "")
            if url:
                with contextlib.suppress(Exception):
                    httpx.post(
                        f"{url}/v1/api/_internal/config-reload",
                        headers=headers,
                        timeout=5.0,
                    )


async def admin_list_settings(request: Request) -> JSONResponse:
    """GET /v1/api/admin/settings — list all settings with effective values."""
    from turnstone.core.auth import require_permission
    from turnstone.core.settings_registry import SETTINGS, deserialize_value
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.settings")
    if err:
        return err

    reveal = request.query_params.get("reveal") == "true"
    stored = {r["key"]: r for r in storage.list_system_settings() if r.get("node_id", "") == ""}

    settings: list[dict[str, Any]] = []
    for key, defn in sorted(SETTINGS.items()):
        row = stored.get(key)
        if row:
            try:
                val = deserialize_value(key, row["value"])
            except (ValueError, KeyError):
                val = row["value"]
            info = {
                "key": key,
                "value": "***" if defn.is_secret and not reveal else val,
                "source": "storage",
                "type": defn.type,
                "description": defn.description,
                "section": defn.section,
                "is_secret": defn.is_secret,
                "node_id": row.get("node_id", ""),
                "changed_by": row.get("changed_by", ""),
                "updated": row.get("updated", ""),
                "restart_required": defn.restart_required,
            }
        else:
            info = {
                "key": key,
                "value": "(managed via config file / env)" if defn.is_secret else defn.default,
                "source": "default",
                "type": defn.type,
                "description": defn.description,
                "section": defn.section,
                "is_secret": defn.is_secret,
                "node_id": "",
                "changed_by": "",
                "updated": "",
                "restart_required": defn.restart_required,
            }
        settings.append(info)

    return JSONResponse({"settings": settings})


async def admin_settings_schema(request: Request) -> JSONResponse:
    """GET /v1/api/admin/settings/schema — return the full settings registry."""
    from turnstone.core.auth import require_permission
    from turnstone.core.settings_registry import SETTINGS

    err = require_permission(request, "admin.settings")
    if err:
        return err

    schema: list[dict[str, Any]] = []
    for key, defn in sorted(SETTINGS.items()):
        schema.append(
            {
                "key": key,
                "type": defn.type,
                "default": defn.default,
                "description": defn.description,
                "section": defn.section,
                "is_secret": defn.is_secret,
                "min_value": defn.min_value,
                "max_value": defn.max_value,
                "choices": defn.choices,
                "restart_required": defn.restart_required,
                "help": defn.help,
                "reference_url": defn.reference_url,
            }
        )

    return JSONResponse({"schema": schema})


async def admin_update_setting(request: Request) -> JSONResponse:
    """PUT /v1/api/admin/settings/{key} — set a setting value."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.settings_registry import (
        serialize_value,
        validate_key,
        validate_value,
    )
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.settings")
    if err:
        return err

    key = request.path_params["key"]
    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    try:
        defn = validate_key(key)
    except ValueError:
        return JSONResponse({"error": f"Unknown setting: {key}"}, status_code=400)

    if defn.is_secret:
        return JSONResponse(
            {
                "error": "Secret settings cannot be modified via API — use config.toml or environment variables"
            },
            status_code=403,
        )

    if "value" not in body:
        return JSONResponse({"error": "value is required"}, status_code=400)

    raw_value = body.get("value")
    try:
        typed_value = validate_value(key, raw_value)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    node_id = str(body.get("node_id", ""))
    audit_uid, ip = _audit_context(request)

    storage.upsert_system_setting(
        key=key,
        value=serialize_value(typed_value),
        node_id=node_id,
        is_secret=defn.is_secret,
        changed_by=audit_uid,
    )

    record_audit(
        storage,
        audit_uid,
        "setting.update",
        "setting",
        key,
        {"value": "***" if defn.is_secret else typed_value, "node_id": node_id},
        ip,
    )

    _publish_config_change(request, key=key, node_id=node_id, action="set")

    return JSONResponse(
        {
            "key": key,
            "value": "***" if defn.is_secret else typed_value,
            "source": "storage",
            "type": defn.type,
            "description": defn.description,
            "section": defn.section,
            "is_secret": defn.is_secret,
            "node_id": node_id,
            "changed_by": audit_uid,
            "updated": "",
            "restart_required": defn.restart_required,
        }
    )


async def admin_delete_setting(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/settings/{key} — reset a setting to default."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.settings_registry import validate_key
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.settings")
    if err:
        return err

    key = request.path_params["key"]
    try:
        validate_key(key)
    except ValueError:
        return JSONResponse({"error": f"Unknown setting: {key}"}, status_code=400)

    node_id = request.query_params.get("node_id", "")
    deleted = storage.delete_system_setting(key, node_id=node_id)
    if not deleted:
        return JSONResponse({"error": f"Setting '{key}' not found in storage"}, status_code=404)

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "setting.delete",
        "setting",
        key,
        {"node_id": node_id},
        ip,
    )

    _publish_config_change(request, key=key, node_id=node_id, action="delete")

    return JSONResponse({"status": "ok", "key": key})


# ---------------------------------------------------------------------------
# Admin: MCP Registry
# ---------------------------------------------------------------------------


def _get_registry_url(request: Request) -> str:
    """Get the MCP Registry URL from DB settings, config.toml, or default."""
    from turnstone.core.config import load_config
    from turnstone.core.mcp_registry import DEFAULT_REGISTRY_URL

    # Check database settings first
    storage = getattr(request.app.state, "auth_storage", None)
    if storage:
        try:
            row = storage.get_system_setting("mcp.registry_url")
            if row:
                val = json.loads(row["value"])
                if val:
                    return str(val)
        except (KeyError, json.JSONDecodeError, TypeError, AttributeError):
            pass

    # Fall back to config.toml [mcp] section
    mcp_cfg = load_config("mcp")
    url = mcp_cfg.get("registry_url", "")
    if url:
        return str(url)

    return DEFAULT_REGISTRY_URL


async def admin_registry_search(request: Request) -> JSONResponse:
    """GET /v1/api/admin/mcp-registry/search — search the MCP Registry."""
    from turnstone.core.auth import require_permission
    from turnstone.core.mcp_registry import (
        MCPRegistryClient,
        MCPRegistryError,
        RegistryServer,
        registry_server_to_dict,
    )
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.mcp")
    if err:
        return err

    q = str(request.query_params.get("search", "")).strip()
    try:
        limit = max(1, min(int(request.query_params.get("limit", "20")), 100))
    except (ValueError, TypeError):
        limit = 20
    cursor = request.query_params.get("cursor") or None

    registry_url = _get_registry_url(request)

    async with MCPRegistryClient(base_url=registry_url) as client:
        try:
            result = await client.search(q=q, limit=limit, cursor=cursor)
        except MCPRegistryError as exc:
            return JSONResponse({"error": f"Registry error: {exc}"}, status_code=502)

    # Deduplicate: keep only isLatest entries, first occurrence per name wins.
    # Skip servers with no install source (no remotes and no packages).
    seen: dict[str, RegistryServer] = {}
    for srv in result.servers:
        if srv.meta and not srv.meta.is_latest:
            continue
        if not srv.remotes and not srv.packages:
            continue
        if srv.name not in seen:
            seen[srv.name] = srv
    deduped = list(seen.values())

    # Mark which servers are already installed
    installed: dict[str, dict[str, Any]] = {}
    for s in storage.list_mcp_servers():
        rn = s.get("registry_name")
        if rn:
            installed[rn] = s

    servers_out = []
    for srv in deduped:
        d = registry_server_to_dict(srv)
        existing = installed.get(srv.name)
        if existing:
            d["installed"] = True
            d["installed_server_id"] = existing["server_id"]
            d["installed_version"] = existing.get("registry_version", "")
            d["update_available"] = existing.get("registry_version", "") != srv.version
        else:
            d["installed"] = False
            d["installed_server_id"] = ""
            d["installed_version"] = ""
            d["update_available"] = False
        servers_out.append(d)

    return JSONResponse(
        {
            "servers": servers_out,
            "total": result.total_count,
            "next_cursor": result.next_cursor,
        }
    )


async def admin_registry_install(request: Request) -> JSONResponse:
    """POST /v1/api/admin/mcp-registry/install — install a server from the registry."""
    import uuid

    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.mcp_registry import (
        MCPRegistryClient,
        MCPRegistryError,
        resolve_install_config,
        sanitize_registry_name,
    )
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.mcp")
    if err:
        return err

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    registry_name = str(body.get("registry_name", "")).strip()
    if not registry_name:
        return JSONResponse({"error": "registry_name is required"}, status_code=400)

    source = str(body.get("source", "")).strip()
    if source not in ("remote", "package"):
        return JSONResponse({"error": "source must be 'remote' or 'package'"}, status_code=400)

    try:
        index = int(body.get("index", 0))
    except (ValueError, TypeError):
        return JSONResponse({"error": "index must be an integer"}, status_code=400)
    variables = body.get("variables") or {}
    env_values = body.get("env") or {}
    header_values = body.get("headers") or {}
    if not isinstance(variables, dict):
        return JSONResponse({"error": "variables must be an object"}, status_code=400)
    if not isinstance(env_values, dict):
        return JSONResponse({"error": "env must be an object"}, status_code=400)
    if not isinstance(header_values, dict):
        return JSONResponse({"error": "headers must be an object"}, status_code=400)
    custom_name = str(body.get("name", "")).strip()

    # Check for duplicates
    existing = storage.get_mcp_server_by_registry_name(registry_name)
    if existing:
        return JSONResponse(
            {
                "error": (
                    f"Registry server '{registry_name}' is already installed "
                    f"as '{existing['name']}'"
                )
            },
            status_code=409,
        )

    # Check max servers
    current = storage.list_mcp_servers()
    if len(current) >= _MCP_MAX_SERVERS:
        return JSONResponse({"error": f"Maximum {_MCP_MAX_SERVERS} servers"}, status_code=400)

    # Fetch the specific server from the registry
    registry_url = _get_registry_url(request)
    async with MCPRegistryClient(base_url=registry_url) as client:
        try:
            result = await client.search(q=registry_name, limit=100)
        except MCPRegistryError as exc:
            return JSONResponse({"error": f"Registry error: {exc}"}, status_code=502)

    # Find the exact server by name
    server = None
    for s in result.servers:
        if s.name == registry_name:
            server = s
            break
    if server is None:
        return JSONResponse(
            {"error": f"Server '{registry_name}' not found in registry"},
            status_code=404,
        )

    # Resolve install configuration
    try:
        config = resolve_install_config(server, source, index, variables)
    except MCPRegistryError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except (IndexError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    # Determine server name
    try:
        name = custom_name or sanitize_registry_name(registry_name)
    except MCPRegistryError as exc:
        return JSONResponse({"error": f"{exc}; provide a custom 'name'"}, status_code=400)
    if not name or not _MCP_NAME_RE.match(name) or "__" in name:
        return JSONResponse(
            {"error": f"Name '{name}' is invalid; provide a custom 'name'"},
            status_code=400,
        )
    if storage.get_mcp_server_by_name(name):
        return JSONResponse(
            {"error": f"Server name '{name}' already exists; provide a custom 'name'"},
            status_code=409,
        )

    # Merge user-provided env and header values
    merged_env = config.get("env", {})
    if isinstance(env_values, dict):
        merged_env.update(env_values)
    merged_headers = config.get("headers", {})
    if isinstance(header_values, dict):
        merged_headers.update(header_values)

    server_id = uuid.uuid4().hex
    audit_uid, ip = _audit_context(request)

    storage.create_mcp_server(
        server_id=server_id,
        name=name,
        transport=config["transport"],
        command=config.get("command", ""),
        args=json.dumps(config.get("args", [])),
        url=config.get("url", ""),
        headers=json.dumps(merged_headers),
        env=json.dumps(merged_env),
        auto_approve=False,
        enabled=True,
        created_by=audit_uid,
        registry_name=registry_name,
        registry_version=config["registry_version"],
        registry_meta=json.dumps(config["registry_meta"]),
    )

    record_audit(
        storage,
        audit_uid,
        "mcp_server.registry_install",
        "mcp_server",
        server_id,
        {"name": name, "registry_name": registry_name, "source": source},
        ip,
    )

    # Auto-reload nodes for one-click UX
    await _notify_nodes_mcp_reload(request)

    server_row = storage.get_mcp_server(server_id)
    return JSONResponse(_mcp_server_to_detail(_mask_mcp_secrets(server_row or {})))


# ---------------------------------------------------------------------------
# Admin: MCP Servers
# ---------------------------------------------------------------------------

_MCP_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
_MCP_MAX_SERVERS = 50


def _mask_mcp_secrets(server: dict[str, Any], reveal: bool = False) -> dict[str, Any]:
    """Replace env/headers values with '***' unless reveal is True."""
    if reveal:
        return server
    s = dict(server)
    if s.get("env") and s["env"] != "{}":
        try:
            env_dict = json.loads(s["env"]) if isinstance(s["env"], str) else s["env"]
            s["env"] = json.dumps({k: "***" for k in env_dict})
        except (json.JSONDecodeError, TypeError):
            s["env"] = "{}"
    if s.get("headers") and s["headers"] != "{}":
        try:
            hdr_dict = json.loads(s["headers"]) if isinstance(s["headers"], str) else s["headers"]
            s["headers"] = json.dumps({k: "***" for k in hdr_dict})
        except (json.JSONDecodeError, TypeError):
            s["headers"] = "{}"
    return s


def _mcp_server_to_detail(
    server: dict[str, Any],
    node_statuses: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Convert a storage dict to a McpServerDetail-shaped dict."""
    d = dict(server)
    d["status"] = node_statuses or {}
    return d


async def _collect_mcp_status(
    request: Request,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Query all nodes for MCP status. Returns {node_id: {server_name: status}}."""
    collector: ClusterCollector = request.app.state.collector
    nodes, _ = collector.get_nodes(sort_by="activity", limit=1000, offset=0)
    client: httpx.AsyncClient = request.app.state.proxy_client
    headers = _proxy_auth_headers(request)
    sem = asyncio.Semaphore(_NODE_FAN_OUT_LIMIT)

    async def _fetch(node: dict[str, Any]) -> tuple[str, dict[str, dict[str, Any]] | None]:
        node_id = node.get("node_id", "")
        url = node.get("server_url", "")
        if not url:
            return node_id, None
        async with sem:
            try:
                resp = await client.get(
                    f"{url.rstrip('/')}/v1/api/_internal/mcp-status",
                    headers=headers,
                    timeout=10,
                )
                if resp.status_code == 200:
                    return node_id, resp.json().get("servers", {})
            except Exception:
                log.debug("Failed to fetch MCP status from node %s", node_id, exc_info=True)
        return node_id, None

    results = await asyncio.gather(*[_fetch(n) for n in nodes])
    return {nid: servers for nid, servers in results if servers is not None}


async def admin_list_mcp_servers(request: Request) -> JSONResponse:
    """GET /v1/api/admin/mcp-servers — list all MCP server definitions."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.mcp")
    if err:
        return err

    reveal = str(request.query_params.get("reveal", "")).lower() in ("true", "1")
    servers = storage.list_mcp_servers()

    # Collect live status from all nodes
    node_statuses = await _collect_mcp_status(request)

    db_names: set[str] = set()
    result = []
    for s in servers:
        db_names.add(s["name"])
        # Build per-node status for this server
        per_node: dict[str, dict[str, Any]] = {}
        for node_id, node_servers in node_statuses.items():
            status = node_servers.get(s["name"])
            if status:
                per_node[node_id] = status
        s = _mask_mcp_secrets(s, reveal)
        result.append(_mcp_server_to_detail(s, per_node))

    # Merge config-sourced servers visible on nodes but not in DB
    config_names: set[str] = set()
    for node_servers in node_statuses.values():
        for name in node_servers:
            if name not in db_names:
                config_names.add(name)
    for name in sorted(config_names):
        # Build a synthetic read-only entry from node-reported data
        per_node = {}
        transport = "stdio"
        command = ""
        url = ""
        for node_id, node_servers in node_statuses.items():
            ns = node_servers.get(name)
            if ns:
                per_node[node_id] = ns
                transport = ns.get("transport", "stdio")
                command = ns.get("command", "")
                url = ns.get("url", "")
        result.append(
            {
                "server_id": "",
                "name": name,
                "transport": transport,
                "command": command,
                "args": "[]",
                "url": url,
                "headers": "{}",
                "env": "{}",
                "auto_approve": False,
                "enabled": True,
                "created_by": "",
                "created": "",
                "updated": "",
                "source": "config",
                "status": per_node,
            }
        )

    return JSONResponse({"servers": result})


async def admin_create_mcp_server(request: Request) -> JSONResponse:
    """POST /v1/api/admin/mcp-servers — create an MCP server definition."""
    import uuid

    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.mcp")
    if err:
        return err

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    name = str(body.get("name", "")).strip()[:64]
    transport = str(body.get("transport", "")).strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    if not _MCP_NAME_RE.match(name):
        return JSONResponse(
            {"error": "name must match [a-zA-Z0-9._-]+"},
            status_code=400,
        )
    if "__" in name:
        return JSONResponse(
            {"error": "name must not contain '__' (reserved delimiter)"},
            status_code=400,
        )
    if transport not in ("stdio", "streamable-http"):
        return JSONResponse(
            {"error": "transport must be 'stdio' or 'streamable-http'"},
            status_code=400,
        )
    if transport == "stdio" and not str(body.get("command", "")).strip():
        return JSONResponse({"error": "command is required for stdio transport"}, status_code=400)
    if transport == "streamable-http" and not str(body.get("url", "")).strip():
        return JSONResponse(
            {"error": "url is required for streamable-http transport"}, status_code=400
        )

    # Check max servers
    existing = storage.list_mcp_servers()
    if len(existing) >= _MCP_MAX_SERVERS:
        return JSONResponse(
            {"error": f"Maximum {_MCP_MAX_SERVERS} servers"},
            status_code=400,
        )

    # Check name uniqueness
    if storage.get_mcp_server_by_name(name):
        return JSONResponse(
            {"error": f"Server '{name}' already exists"},
            status_code=409,
        )

    server_id = uuid.uuid4().hex
    audit_uid, ip = _audit_context(request)

    args_list = body.get("args", [])
    headers_dict = body.get("headers", {})
    env_dict = body.get("env", {})

    storage.create_mcp_server(
        server_id=server_id,
        name=name,
        transport=transport,
        command=str(body.get("command", "")).strip(),
        args=json.dumps(args_list) if isinstance(args_list, list) else "[]",
        url=str(body.get("url", "")).strip(),
        headers=json.dumps(headers_dict) if isinstance(headers_dict, dict) else "{}",
        env=json.dumps(env_dict) if isinstance(env_dict, dict) else "{}",
        auto_approve=bool(body.get("auto_approve", False)),
        enabled=bool(body.get("enabled", True)),
        created_by=audit_uid,
    )

    record_audit(
        storage,
        audit_uid,
        "mcp_server.create",
        "mcp_server",
        server_id,
        {"name": name},
        ip,
    )

    server = storage.get_mcp_server(server_id)
    return JSONResponse(_mcp_server_to_detail(_mask_mcp_secrets(server or {})))


async def admin_get_mcp_server(request: Request) -> JSONResponse:
    """GET /v1/api/admin/mcp-servers/{server_id} — get single MCP server."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.mcp")
    if err:
        return err

    server_id = request.path_params["server_id"]
    server = storage.get_mcp_server(server_id)
    if server is None:
        return JSONResponse({"error": "MCP server not found"}, status_code=404)

    node_statuses = await _collect_mcp_status(request)
    per_node: dict[str, dict[str, Any]] = {}
    for node_id, node_servers in node_statuses.items():
        status = node_servers.get(server["name"])
        if status:
            per_node[node_id] = status

    reveal = str(request.query_params.get("reveal", "")).lower() in ("true", "1")
    server = _mask_mcp_secrets(server, reveal)
    return JSONResponse(_mcp_server_to_detail(server, per_node))


async def admin_update_mcp_server(request: Request) -> JSONResponse:
    """PUT /v1/api/admin/mcp-servers/{server_id} — update an MCP server."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.mcp")
    if err:
        return err

    server_id = request.path_params["server_id"]
    existing = storage.get_mcp_server(server_id)
    if existing is None:
        return JSONResponse({"error": "MCP server not found"}, status_code=404)

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    updates: dict[str, Any] = {}
    if "name" in body:
        name = str(body["name"]).strip()[:64]
        if not name:
            return JSONResponse({"error": "name cannot be empty"}, status_code=400)
        if not _MCP_NAME_RE.match(name):
            return JSONResponse(
                {"error": "name must match [a-zA-Z0-9._-]+"},
                status_code=400,
            )
        if "__" in name:
            return JSONResponse(
                {"error": "name must not contain '__'"},
                status_code=400,
            )
        if name != existing["name"] and storage.get_mcp_server_by_name(name):
            return JSONResponse(
                {"error": f"Server '{name}' already exists"},
                status_code=409,
            )
        updates["name"] = name
    if "transport" in body:
        transport = str(body["transport"]).strip()
        if transport not in ("stdio", "streamable-http"):
            return JSONResponse(
                {"error": "transport must be 'stdio' or 'streamable-http'"},
                status_code=400,
            )
        updates["transport"] = transport
    if "command" in body:
        updates["command"] = str(body["command"]).strip()
    if "args" in body:
        updates["args"] = json.dumps(body["args"]) if isinstance(body["args"], list) else "[]"
    if "url" in body:
        updates["url"] = str(body["url"]).strip()
    if "headers" in body:
        updates["headers"] = (
            json.dumps(body["headers"]) if isinstance(body["headers"], dict) else "{}"
        )
    if "env" in body:
        updates["env"] = json.dumps(body["env"]) if isinstance(body["env"], dict) else "{}"
    if "auto_approve" in body:
        updates["auto_approve"] = bool(body["auto_approve"])
    if "enabled" in body:
        updates["enabled"] = bool(body["enabled"])

    if updates:
        storage.update_mcp_server(server_id, **updates)

    audit_uid, ip = _audit_context(request)
    audit_detail = dict(updates)
    for _secret_key in ("env", "headers"):
        if _secret_key in audit_detail:
            audit_detail[_secret_key] = "(updated)"
    record_audit(
        storage,
        audit_uid,
        "mcp_server.update",
        "mcp_server",
        server_id,
        audit_detail,
        ip,
    )

    server = storage.get_mcp_server(server_id)
    return JSONResponse(_mcp_server_to_detail(_mask_mcp_secrets(server or {})))


async def admin_delete_mcp_server(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/mcp-servers/{server_id}."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.mcp")
    if err:
        return err

    server_id = request.path_params["server_id"]
    existing = storage.get_mcp_server(server_id)
    if existing is None:
        return JSONResponse({"error": "MCP server not found"}, status_code=404)

    storage.delete_mcp_server(server_id)

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "mcp_server.delete",
        "mcp_server",
        server_id,
        {"name": existing.get("name", "")},
        ip,
    )

    return JSONResponse({"status": "ok"})


async def _notify_nodes_mcp_reload(request: Request) -> dict[str, Any]:
    """Tell all nodes to re-read the mcp_servers DB table and reconcile."""
    collector: ClusterCollector = request.app.state.collector
    nodes, _ = collector.get_nodes(sort_by="activity", limit=1000, offset=0)
    client: httpx.AsyncClient = request.app.state.proxy_client
    headers = _proxy_auth_headers(request)
    sem = asyncio.Semaphore(_NODE_FAN_OUT_LIMIT)

    async def _notify(node: dict[str, Any]) -> tuple[str, Any]:
        node_id = node.get("node_id", "")
        url = node.get("server_url", "")
        if not url:
            return node_id, None
        async with sem:
            try:
                resp = await client.post(
                    f"{url.rstrip('/')}/v1/api/_internal/mcp-reload",
                    headers=headers,
                    timeout=30,
                )
                return node_id, resp.json()
            except Exception as exc:
                log.debug("Failed to notify node %s for MCP reload", node_id, exc_info=True)
                return node_id, {"error": str(exc)}

    results = await asyncio.gather(*[_notify(n) for n in nodes])
    return {nid: data for nid, data in results if data is not None}


async def admin_mcp_reload(request: Request) -> JSONResponse:
    """POST /v1/api/admin/mcp-servers/reload — tell nodes to re-read DB."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.mcp")
    if err:
        return err

    results = await _notify_nodes_mcp_reload(request)
    return JSONResponse({"status": "ok", "results": results})


async def admin_import_mcp_config(request: Request) -> JSONResponse:
    """POST /v1/api/admin/mcp-servers/import — import from pasted JSON config."""
    import uuid

    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.mcp")
    if err:
        return err

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    data = body.get("config")
    if not isinstance(data, dict):
        return JSONResponse(
            {"error": "config is required (JSON object with mcpServers key)"}, status_code=400
        )

    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict) or not servers:
        return JSONResponse(
            {"error": "No mcpServers found in config"},
            status_code=400,
        )

    imported: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    audit_uid, ip = _audit_context(request)
    current_count = len(storage.list_mcp_servers())

    for srv_name, cfg in servers.items():
        srv_name = str(srv_name).strip()[:64]
        if not srv_name or not _MCP_NAME_RE.match(srv_name) or "__" in srv_name:
            errors.append(f"{srv_name}: invalid server name")
            continue
        if storage.get_mcp_server_by_name(srv_name):
            skipped.append(srv_name)
            continue
        if current_count >= _MCP_MAX_SERVERS:
            errors.append(f"{srv_name}: max servers reached")
            break

        transport = "stdio"
        if "url" in cfg or cfg.get("type") in ("http", "streamable-http"):
            transport = "streamable-http"

        # Coerce fields to expected types
        raw_args = cfg.get("args", [])
        raw_headers = cfg.get("headers", {})
        raw_env = cfg.get("env", {})
        if not isinstance(raw_args, list):
            errors.append(f"{srv_name}: args must be a list")
            continue
        if not isinstance(raw_headers, dict):
            errors.append(f"{srv_name}: headers must be an object")
            continue
        if not isinstance(raw_env, dict):
            errors.append(f"{srv_name}: env must be an object")
            continue

        server_id = uuid.uuid4().hex
        try:
            storage.create_mcp_server(
                server_id=server_id,
                name=srv_name,
                transport=transport,
                command=str(cfg.get("command", "")),
                args=json.dumps(raw_args),
                url=str(cfg.get("url", "")),
                headers=json.dumps(raw_headers),
                env=json.dumps(raw_env),
                auto_approve=False,
                enabled=True,
                created_by=audit_uid,
            )
            imported.append(srv_name)
            current_count += 1
        except Exception as exc:
            errors.append(f"{srv_name}: {exc}")

    if imported:
        record_audit(
            storage,
            audit_uid,
            "mcp_server.import",
            "mcp_server",
            "",
            {"imported": imported, "skipped": skipped},
            ip,
        )

    return JSONResponse({"imported": imported, "skipped": skipped, "errors": errors})


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    *,
    collector: ClusterCollector,
    broker: RedisBroker,
    auth_config: Any,
    jwt_secret: str = "",
    auth_storage: Any = None,
    proxy_auth_token: str = "",
    proxy_token_mgr: Any = None,
    cors_origins: list[str] | None = None,
) -> Starlette:
    """Build the Starlette ASGI application for the console dashboard."""
    _spec = build_console_spec()
    _openapi_handler = make_openapi_handler(_spec)
    _docs_handler = make_docs_handler()

    app = Starlette(
        routes=[
            Route("/", index),
            Mount(
                "/v1",
                routes=[
                    Route("/api/cluster/overview", cluster_overview),
                    Route("/api/cluster/nodes", cluster_nodes),
                    Route("/api/cluster/workstreams", cluster_workstreams),
                    Route("/api/cluster/workstreams/new", create_workstream, methods=["POST"]),
                    Route("/api/cluster/node/{node_id}", cluster_node_detail),
                    Route("/api/cluster/snapshot", cluster_snapshot),
                    Route("/api/cluster/events", cluster_events_sse),
                    Route("/api/skills", list_skills_summary),
                    Route("/api/auth/login", auth_login, methods=["POST"]),
                    Route("/api/auth/logout", auth_logout, methods=["POST"]),
                    Route("/api/auth/status", auth_status),
                    Route("/api/auth/setup", auth_setup, methods=["POST"]),
                    Route("/api/auth/whoami", auth_whoami),
                    Route("/api/auth/oidc/authorize", oidc_authorize),
                    Route("/api/auth/oidc/callback", oidc_callback),
                    Route("/api/admin/users", admin_list_users),
                    Route("/api/admin/users", admin_create_user, methods=["POST"]),
                    Route("/api/admin/users/{user_id}", admin_delete_user, methods=["DELETE"]),
                    Route("/api/admin/users/{user_id}/tokens", admin_list_tokens),
                    Route(
                        "/api/admin/users/{user_id}/tokens", admin_create_token, methods=["POST"]
                    ),
                    Route("/api/admin/tokens/{token_id}", admin_revoke_token, methods=["DELETE"]),
                    Route(
                        "/api/admin/users/{user_id}/channels",
                        admin_list_channels,
                    ),
                    Route(
                        "/api/admin/users/{user_id}/channels",
                        admin_create_channel,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/admin/channels/{channel_type}/{channel_user_id}",
                        admin_delete_channel,
                        methods=["DELETE"],
                    ),
                    Route(
                        "/api/admin/users/{user_id}/oidc-identities",
                        admin_list_oidc_identities,
                    ),
                    Route(
                        "/api/admin/oidc-identities",
                        admin_delete_oidc_identity,
                        methods=["DELETE"],
                    ),
                    Route("/api/admin/schedules", admin_list_schedules),
                    Route("/api/admin/schedules", admin_create_schedule, methods=["POST"]),
                    Route("/api/admin/schedules/{task_id}", admin_get_schedule),
                    Route("/api/admin/schedules/{task_id}", admin_update_schedule, methods=["PUT"]),
                    Route(
                        "/api/admin/schedules/{task_id}",
                        admin_delete_schedule,
                        methods=["DELETE"],
                    ),
                    Route("/api/admin/schedules/{task_id}/runs", admin_list_schedule_runs),
                    Route("/api/admin/watches", admin_list_watches),
                    Route(
                        "/api/admin/watches/{watch_id}/cancel",
                        admin_cancel_watch,
                        methods=["POST"],
                    ),
                    # Governance: Roles
                    Route("/api/admin/roles", admin_list_roles),
                    Route("/api/admin/roles", admin_create_role, methods=["POST"]),
                    Route("/api/admin/roles/{role_id}", admin_update_role, methods=["PUT"]),
                    Route("/api/admin/roles/{role_id}", admin_delete_role, methods=["DELETE"]),
                    Route("/api/admin/users/{user_id}/roles", admin_list_user_roles),
                    Route(
                        "/api/admin/users/{user_id}/roles",
                        admin_assign_role,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/admin/users/{user_id}/roles/{role_id}",
                        admin_unassign_role,
                        methods=["DELETE"],
                    ),
                    # Governance: Orgs
                    Route("/api/admin/orgs", admin_list_orgs),
                    Route("/api/admin/orgs/{org_id}", admin_get_org),
                    Route("/api/admin/orgs/{org_id}", admin_update_org, methods=["PUT"]),
                    # Governance: Tool policies
                    Route("/api/admin/policies", admin_list_policies),
                    Route("/api/admin/policies", admin_create_policy, methods=["POST"]),
                    Route(
                        "/api/admin/policies/{policy_id}",
                        admin_update_policy,
                        methods=["PUT"],
                    ),
                    Route(
                        "/api/admin/policies/{policy_id}",
                        admin_delete_policy,
                        methods=["DELETE"],
                    ),
                    # Governance: Skill Discovery
                    Route("/api/admin/skills/discover", admin_skill_discover),
                    Route(
                        "/api/admin/skills/install",
                        admin_skill_install,
                        methods=["POST"],
                    ),
                    # Governance: Skills
                    Route("/api/admin/skills", admin_list_skills),
                    Route("/api/admin/skills", admin_create_skill, methods=["POST"]),
                    Route("/api/admin/skills/{skill_id}", admin_get_skill),
                    Route(
                        "/api/admin/skills/{skill_id}",
                        admin_update_skill,
                        methods=["PUT"],
                    ),
                    Route(
                        "/api/admin/skills/{skill_id}",
                        admin_delete_skill,
                        methods=["DELETE"],
                    ),
                    Route(
                        "/api/admin/skills/{skill_id}/versions",
                        admin_list_skill_versions,
                    ),
                    # Governance: Skill Resources
                    Route(
                        "/api/admin/skills/{skill_id}/resources",
                        admin_list_skill_resources,
                    ),
                    Route(
                        "/api/admin/skills/{skill_id}/resources",
                        admin_create_skill_resource,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/admin/skills/{skill_id}/resources/{path:path}",
                        admin_get_skill_resource,
                    ),
                    Route(
                        "/api/admin/skills/{skill_id}/resources/{path:path}",
                        admin_delete_skill_resource,
                        methods=["DELETE"],
                    ),
                    # Governance: Memories
                    Route("/api/admin/memories", admin_list_memories),
                    Route("/api/admin/memories/search", admin_search_memories),
                    Route("/api/admin/memories/{memory_id}", admin_get_memory),
                    Route(
                        "/api/admin/memories/{memory_id}",
                        admin_delete_memory,
                        methods=["DELETE"],
                    ),
                    # System: Settings
                    Route("/api/admin/settings", admin_list_settings),
                    Route("/api/admin/settings/schema", admin_settings_schema),
                    Route(
                        "/api/admin/settings/{key:path}",
                        admin_update_setting,
                        methods=["PUT"],
                    ),
                    Route(
                        "/api/admin/settings/{key:path}",
                        admin_delete_setting,
                        methods=["DELETE"],
                    ),
                    # System: MCP Registry
                    Route("/api/admin/mcp-registry/search", admin_registry_search),
                    Route(
                        "/api/admin/mcp-registry/install",
                        admin_registry_install,
                        methods=["POST"],
                    ),
                    # System: MCP Servers
                    Route("/api/admin/mcp-servers", admin_list_mcp_servers),
                    Route(
                        "/api/admin/mcp-servers",
                        admin_create_mcp_server,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/admin/mcp-servers/import",
                        admin_import_mcp_config,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/admin/mcp-servers/reload",
                        admin_mcp_reload,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/admin/mcp-servers/{server_id}",
                        admin_get_mcp_server,
                    ),
                    Route(
                        "/api/admin/mcp-servers/{server_id}",
                        admin_update_mcp_server,
                        methods=["PUT"],
                    ),
                    Route(
                        "/api/admin/mcp-servers/{server_id}",
                        admin_delete_mcp_server,
                        methods=["DELETE"],
                    ),
                    # Governance: Usage & Audit
                    Route("/api/admin/usage", admin_usage),
                    Route("/api/admin/audit", admin_audit),
                    # Governance: Intent Verdicts
                    Route("/api/admin/verdicts", admin_list_verdicts),
                    Route("/api/admin/output-assessments", admin_list_output_assessments),
                    Route(
                        "/api/admin/skills/{skill_id}/rescan",
                        admin_rescan_skill,
                        methods=["POST"],
                    ),
                ],
            ),
            Route("/health", health),
            Route("/openapi.json", _openapi_handler),
            Route("/docs", _docs_handler),
            Mount("/static", app=StaticFiles(directory=str(_STATIC_DIR)), name="static"),
            Mount("/shared", app=StaticFiles(directory=str(_SHARED_DIR)), name="shared"),
            # Proxy routes — serve server UI through console port
            Route("/node/{node_id}/", proxy_index),
            Route("/node/{node_id}/static/{path:path}", proxy_static),
            Route("/node/{node_id}/shared/{path:path}", proxy_shared_static),
            Route("/node/{node_id}/v1/api/{path:path}", proxy_api, methods=["GET", "POST"]),
            Route("/node/{node_id}/api/{path:path}", proxy_api, methods=["GET", "POST"]),
            Route("/node/{node_id}/{path:path}", proxy_non_api),
        ],
        middleware=_build_console_middleware(cors_origins),
        lifespan=_lifespan,
    )
    app.state.collector = collector
    app.state.broker = broker
    app.state.auth_config = auth_config
    app.state.jwt_secret = jwt_secret
    app.state.auth_storage = auth_storage
    app.state.proxy_auth_token = proxy_auth_token
    app.state.proxy_token_mgr = proxy_token_mgr

    from turnstone.core.auth import LoginRateLimiter

    app.state.login_limiter = LoginRateLimiter()

    # OIDC configuration (opt-in via env vars)
    from turnstone.core.oidc import load_oidc_config

    oidc_config = load_oidc_config()
    app.state.oidc_config = oidc_config
    app.state.jwks_data = None  # populated after async discovery

    # Scheduler — start background thread if storage is available
    if auth_storage is not None:
        from turnstone.console.scheduler import TaskScheduler

        scheduler = TaskScheduler(
            broker=broker,
            collector=collector,
            storage=auth_storage,
        )
        app.state.scheduler = scheduler
    else:
        app.state.scheduler = None

    return app


def _build_console_middleware(cors_origins: list[str] | None = None) -> list[Middleware]:
    """Build the middleware stack with optional CORS."""
    stack: list[Middleware] = []
    if cors_origins:
        from turnstone.core.web_helpers import cors_middleware

        stack.append(cors_middleware(cors_origins))
    stack.append(Middleware(AuthMiddleware, jwt_audience=JWT_AUD_CONSOLE))
    return stack


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
    from turnstone.mq.broker import add_redis_args

    add_redis_args(parser)
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=10.0,
        help="Node polling interval in seconds (default: 10)",
    )
    from turnstone.core.log import add_log_args

    add_log_args(parser)
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("TURNSTONE_AUTH_TOKEN", ""),
        help="Bearer token for polling turnstone-server nodes (default: $TURNSTONE_AUTH_TOKEN)",
    )

    from turnstone.core.config import apply_config

    apply_config(parser, ["console", "redis", "auth"])
    args = parser.parse_args()

    from turnstone.core.log import configure_logging_from_args

    configure_logging_from_args(args, "console")

    from turnstone.mq.broker import broker_from_args

    broker = broker_from_args(args)

    # If no explicit auth token is provided, use a ServiceTokenManager
    # so collector JWTs auto-rotate.  A shared JWT secret is required for
    # multi-service deployments — ephemeral secrets differ per process.
    collector_token = args.auth_token
    collector_token_mgr = None
    if not collector_token:
        _jwt_secret = os.environ.get("TURNSTONE_JWT_SECRET", "")
        if not _jwt_secret:
            log.error(
                "TURNSTONE_JWT_SECRET is not set and no --auth-token provided. "
                "The console cannot authenticate to server nodes. Set TURNSTONE_JWT_SECRET "
                "to a shared secret (at least 32 characters) or pass --auth-token."
            )
            raise SystemExit(1)
        from turnstone.core.auth import JWT_AUD_SERVER, ServiceTokenManager

        collector_token_mgr = ServiceTokenManager(
            user_id="console-collector",
            scopes=frozenset({"read"}),
            source="console",
            secret=_jwt_secret,
            audience=JWT_AUD_SERVER,
            expiry_hours=1,
        )
        collector_token = collector_token_mgr.token
        log.info("console.collector_jwt_minted")

    collector = ClusterCollector(
        broker=broker,
        poll_interval=args.poll_interval,
        auth_token=collector_token,
    )
    collector.start()

    _load_static()

    from turnstone.core.auth import load_auth_config, load_jwt_secret

    auth_config = load_auth_config()
    jwt_secret = load_jwt_secret() if auth_config.enabled else ""

    # Initialize storage for user/token management (optional — requires DB config)
    auth_storage = None
    try:
        from turnstone.core.storage import init_storage

        db_backend = os.environ.get("TURNSTONE_DB_BACKEND", "sqlite")
        db_url = os.environ.get("TURNSTONE_DB_URL", "")
        db_path = os.environ.get("TURNSTONE_DB_PATH", "")
        auth_storage = init_storage(db_backend, path=db_path, url=db_url)
    except Exception:
        log.info("Console storage not available — admin API disabled, JWT-only auth")

    # If no explicit auth token is provided, use a ServiceTokenManager
    # so proxy JWTs auto-rotate.
    proxy_token = args.auth_token
    proxy_token_mgr = None
    if not proxy_token and jwt_secret:
        from turnstone.core.auth import JWT_AUD_SERVER, ServiceTokenManager

        proxy_token_mgr = ServiceTokenManager(
            user_id="console-proxy",
            scopes=frozenset({"read", "write", "approve"}),
            source="console",
            secret=jwt_secret,
            audience=JWT_AUD_SERVER,
            expiry_hours=1,
        )
        proxy_token = proxy_token_mgr.token
        log.info("console.proxy_jwt_minted")

    from turnstone.core.web_helpers import parse_cors_origins

    cors_origins = parse_cors_origins()

    app = create_app(
        collector=collector,
        broker=broker,
        auth_config=auth_config,
        jwt_secret=jwt_secret,
        auth_storage=auth_storage,
        proxy_auth_token=proxy_token,
        proxy_token_mgr=proxy_token_mgr,
        cors_origins=cors_origins,
    )

    log.info("Console starting on http://%s:%s", args.host, args.port)
    if auth_config.enabled:
        log.info("Auth: enabled (%d config token(s))", len(auth_config.tokens))
    print("Press Ctrl+C to stop.")

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
