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

from turnstone.api.console_spec import build_console_spec
from turnstone.api.docs import make_docs_handler, make_openapi_handler
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
_SHARED_DIR = Path(__file__).parent.parent / "shared_static"
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
        jwt_secret = getattr(request.app.state, "jwt_secret", "")
        storage = getattr(request.app.state, "auth_storage", None)
        method = request.method
        path = request.url.path
        auth_header = request.headers.get("Authorization")
        cookie_header = request.headers.get("Cookie")
        allowed, status, msg, auth_result = check_request(
            auth_config,
            method,
            path,
            auth_header,
            cookie_header,
            jwt_secret=jwt_secret,
            storage=storage,
        )
        if not allowed:
            response = JSONResponse({"error": msg}, status_code=status)
            await response(scope, receive, send)
            return

        if auth_result and auth_result.user_id:
            from turnstone.core.log import ctx_user_id

            ctx_user_id.set(auth_result.user_id)
        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["auth_result"] = auth_result
        await self.app(scope, receive, send)


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

    Forwards the user's JWT (from cookie or Bearer header) so that
    upstream servers with auth enabled accept the proxied request.
    Falls back to the static proxy_auth_token if configured.
    """
    # Prefer the incoming Authorization header (e.g. Bearer JWT)
    auth_header = request.headers.get("Authorization", "")
    if auth_header:
        return {"Authorization": auth_header}

    # Extract JWT from cookie
    from turnstone.core.auth import AUTH_COOKIE, _extract_cookie

    cookie_header = request.headers.get("Cookie", "")
    cookie_token = _extract_cookie(cookie_header, AUTH_COOKIE)
    if cookie_token:
        return {"Authorization": f"Bearer {cookie_token}"}

    # Fall back to static proxy_auth_token
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
            "version_drift": overview.get("version_drift", False),
            "versions": overview.get("versions", []),
        }
    )


async def auth_login(request: Request) -> Response:
    """Authenticate via username:password or legacy token, return JWT."""
    from turnstone.core.auth import (
        AuthResult,
        _authenticate_token,
        create_jwt,
        make_set_cookie,
        verify_password,
    )

    try:
        body: dict[str, Any] = await request.json()
    except (ValueError, json.JSONDecodeError):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    auth_config = request.app.state.auth_config
    jwt_secret = getattr(request.app.state, "jwt_secret", "")
    storage = getattr(request.app.state, "auth_storage", None)

    result: AuthResult | None = None
    username = body.get("username", "")
    password = body.get("password", "")

    if username and password and storage is not None:
        user = storage.get_user_by_username(username)
        if user and verify_password(password, user["password_hash"]):
            result = AuthResult(
                user_id=user["user_id"],
                scopes=frozenset({"read", "write", "approve"}),
                token_source="password",
            )
    elif body.get("token"):
        result = _authenticate_token(
            body["token"],
            auth_config,
            jwt_secret=jwt_secret,
            storage=storage,
        )

    if result is None:
        return JSONResponse({"error": "Invalid credentials"}, status_code=401)

    jwt_token = ""
    if jwt_secret:
        jwt_token = create_jwt(
            user_id=result.user_id,
            scopes=result.scopes,
            source=result.token_source,
            secret=jwt_secret,
        )

    role = "full" if result.has_scope("write") else "read"
    scopes_str = ",".join(sorted(result.scopes))
    resp_body: dict[str, str] = {"status": "ok", "role": role, "scopes": scopes_str}
    if jwt_token:
        resp_body["jwt"] = jwt_token
    if result.user_id:
        resp_body["user_id"] = result.user_id

    response = JSONResponse(resp_body)
    cookie_value = jwt_token if jwt_token else body.get("token", "")
    if cookie_value:
        response.headers["Set-Cookie"] = make_set_cookie(cookie_value)
    return response


async def auth_logout(request: Request) -> Response:
    from turnstone.core.auth import make_clear_cookie

    response = JSONResponse({"status": "ok"})
    response.headers["Set-Cookie"] = make_clear_cookie()
    return response


async def auth_status(request: Request) -> JSONResponse:
    """GET /v1/api/auth/status — public endpoint for login UI state detection."""
    auth_config = request.app.state.auth_config
    storage = getattr(request.app.state, "auth_storage", None)

    has_users = False
    if storage is not None:
        try:
            users = storage.list_users()
            has_users = len(users) > 0
        except Exception:
            pass

    return JSONResponse(
        {
            "auth_enabled": auth_config.enabled,
            "has_users": has_users,
            "setup_required": auth_config.enabled and not has_users,
        }
    )


async def auth_setup(request: Request) -> JSONResponse:
    """POST /v1/api/auth/setup — create first admin user (public, one-time only).

    Only works when auth is enabled and zero users exist. Returns JWT on success.
    """
    import uuid

    from turnstone.core.auth import create_jwt, hash_password, make_set_cookie

    storage = getattr(request.app.state, "auth_storage", None)
    jwt_secret = getattr(request.app.state, "jwt_secret", "")

    if storage is None:
        return JSONResponse({"error": "Storage not available"}, status_code=503)

    try:
        body: dict[str, Any] = await request.json()
    except (ValueError, json.JSONDecodeError):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

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
    if len(password) < 8:
        return JSONResponse({"error": "Password must be at least 8 characters"}, status_code=400)

    user_id = uuid.uuid4().hex
    pw_hash = hash_password(password)

    # Atomic: insert only if no users exist (prevents TOCTOU race)
    try:
        created = storage.create_first_user(user_id, username, display_name, pw_hash)
    except Exception:
        return JSONResponse({"error": "Storage error"}, status_code=503)
    if not created:
        return JSONResponse({"error": "Setup already completed"}, status_code=409)

    # Issue JWT automatically
    scopes = frozenset({"read", "write", "approve"})
    jwt_token = ""
    if jwt_secret:
        jwt_token = create_jwt(
            user_id=user_id,
            scopes=scopes,
            source="password",
            secret=jwt_secret,
        )

    resp_body: dict[str, str] = {
        "status": "ok",
        "user_id": user_id,
        "username": username,
        "role": "full",
        "scopes": ",".join(sorted(scopes)),
    }
    if jwt_token:
        resp_body["jwt"] = jwt_token

    response = JSONResponse(resp_body)
    if jwt_token:
        response.headers["Set-Cookie"] = make_set_cookie(jwt_token)
    return response


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
    try:
        body: dict[str, Any] = await request.json()
    except (ValueError, json.JSONDecodeError):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    broker: RedisBroker = request.app.state.broker
    collector: ClusterCollector = request.app.state.collector

    raw_node_id = body.get("node_id", "")
    raw_name = body.get("name", "")
    raw_model = body.get("model", "")
    raw_initial_message = body.get("initial_message", "")
    if not isinstance(raw_node_id, str):
        raw_node_id = "" if raw_node_id is None else None
    if not isinstance(raw_name, str):
        raw_name = "" if raw_name is None else None
    if not isinstance(raw_model, str):
        raw_model = "" if raw_model is None else None
    if not isinstance(raw_initial_message, str):
        raw_initial_message = "" if raw_initial_message is None else None
    if raw_node_id is None or raw_name is None or raw_model is None or raw_initial_message is None:
        return JSONResponse(
            {"error": "node_id, name, model, and initial_message must be strings"}, status_code=400
        )
    node_id = raw_node_id
    name = raw_name[:256]
    model = raw_model[:128]
    initial_message = raw_initial_message[:4096]

    from turnstone.mq.protocol import CreateWorkstreamMessage

    # General pool — push to shared queue, any bridge picks it up
    if node_id == "pool":
        msg = CreateWorkstreamMessage(name=name, model=model, initial_message=initial_message)
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
    """Proxy an SSE stream from the target server to the browser."""
    target = f"{server_url}/{api_prefix}/{path}"
    if request.url.query:
        target += f"?{request.url.query}"

    sse_client: httpx.AsyncClient = request.app.state.proxy_sse_client
    sse_auth = _proxy_auth_headers(request)

    async def sse_generator() -> AsyncGenerator[dict[str, str], None]:
        from httpx_sse import aconnect_sse

        try:
            async with aconnect_sse(sse_client, "GET", target, headers=sse_auth) as source:
                if source.response.status_code != 200:
                    log.debug(
                        "SSE proxy received status %s from %s",
                        source.response.status_code,
                        target,
                    )
                    yield {
                        "event": "error",
                        "data": f"Upstream returned status {source.response.status_code}",
                    }
                    return
                async for sse in source.aiter_sse():
                    if await request.is_disconnected():
                        return
                    yield {"event": sse.event, "data": sse.data}
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
    # Separate client for SSE streams — longer read timeout, shared connection pool
    app.state.proxy_sse_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5, read=30, write=5, pool=5),
        headers=headers,
    )
    yield
    # Shutdown
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
    storage = getattr(request.app.state, "auth_storage", None)
    if storage is None:
        return JSONResponse({"error": "Storage not available"}, status_code=503)
    return JSONResponse({"users": storage.list_users()})


async def admin_create_user(request: Request) -> JSONResponse:
    """POST /v1/api/admin/users — create a new user."""
    import uuid

    from turnstone.core.auth import hash_password

    storage = getattr(request.app.state, "auth_storage", None)
    if storage is None:
        return JSONResponse({"error": "Storage not available"}, status_code=503)
    try:
        body: dict[str, Any] = await request.json()
    except (ValueError, json.JSONDecodeError):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

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
    storage = getattr(request.app.state, "auth_storage", None)
    if storage is None:
        return JSONResponse({"error": "Storage not available"}, status_code=503)
    user_id = request.path_params["user_id"]
    if storage.delete_user(user_id):
        return JSONResponse({"status": "ok"})
    return JSONResponse({"error": "User not found"}, status_code=404)


async def admin_list_tokens(request: Request) -> JSONResponse:
    """GET /v1/api/admin/users/{user_id}/tokens — list tokens for a user."""
    storage = getattr(request.app.state, "auth_storage", None)
    if storage is None:
        return JSONResponse({"error": "Storage not available"}, status_code=503)
    user_id = request.path_params["user_id"]
    return JSONResponse({"tokens": storage.list_api_tokens(user_id)})


async def admin_create_token(request: Request) -> JSONResponse:
    """POST /v1/api/admin/users/{user_id}/tokens — create API token."""
    import uuid

    from turnstone.core.auth import generate_token, hash_token, token_prefix

    storage = getattr(request.app.state, "auth_storage", None)
    if storage is None:
        return JSONResponse({"error": "Storage not available"}, status_code=503)
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
    storage = getattr(request.app.state, "auth_storage", None)
    if storage is None:
        return JSONResponse({"error": "Storage not available"}, status_code=503)
    token_id = request.path_params["token_id"]
    if storage.delete_api_token(token_id):
        return JSONResponse({"status": "ok"})
    return JSONResponse({"error": "Token not found"}, status_code=404)


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
                    Route("/api/cluster/events", cluster_events_sse),
                    Route("/api/auth/login", auth_login, methods=["POST"]),
                    Route("/api/auth/logout", auth_logout, methods=["POST"]),
                    Route("/api/auth/status", auth_status),
                    Route("/api/auth/setup", auth_setup, methods=["POST"]),
                    Route("/api/admin/users", admin_list_users),
                    Route("/api/admin/users", admin_create_user, methods=["POST"]),
                    Route("/api/admin/users/{user_id}", admin_delete_user, methods=["DELETE"]),
                    Route("/api/admin/users/{user_id}/tokens", admin_list_tokens),
                    Route(
                        "/api/admin/users/{user_id}/tokens", admin_create_token, methods=["POST"]
                    ),
                    Route("/api/admin/tokens/{token_id}", admin_revoke_token, methods=["DELETE"]),
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
    app.state.jwt_secret = jwt_secret
    app.state.auth_storage = auth_storage
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
        "--log-format",
        default="auto",
        choices=["auto", "json", "text"],
        help="Log output format (default: auto — JSON when stderr is not a TTY)",
    )
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("TURNSTONE_AUTH_TOKEN", ""),
        help="Bearer token for polling turnstone-server nodes (default: $TURNSTONE_AUTH_TOKEN)",
    )

    from turnstone.core.config import apply_config

    apply_config(parser, ["console", "redis", "auth"])
    args = parser.parse_args()

    from turnstone.core.log import configure_logging

    configure_logging(
        level=args.log_level,
        json_output={"json": True, "text": False}.get(args.log_format),
        service="console",
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

    app = create_app(
        collector=collector,
        broker=broker,
        auth_config=auth_config,
        jwt_secret=jwt_secret,
        auth_storage=auth_storage,
        proxy_auth_token=args.auth_token,
    )

    log.info("Console starting on http://%s:%s", args.host, args.port)
    if auth_config.enabled:
        log.info("Auth: enabled (%d config token(s))", len(auth_config.tokens))
    print("Press Ctrl+C to stop.")

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
