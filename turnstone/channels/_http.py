"""Lightweight HTTP server for the channel gateway.

Runs alongside the channel adapters (Discord, etc.) to receive notification
requests from the bridge.  Exposes ``POST /v1/api/notify`` and ``GET /health``.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import uuid
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from starlette.requests import Request

    from turnstone.channels._protocol import ChannelAdapter
    from turnstone.core.storage._protocol import StorageBackend

log = get_logger(__name__)

# ws_id is a hex string (8–32 chars depending on entry point).
_WS_ID_RE = re.compile(r"^[0-9a-f]{8,32}$")


async def _handle_health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "channel"})


def _check_auth(request: Request) -> JSONResponse | None:
    """Validate the request's Authorization header.  Returns an error response or None."""
    auth_token: str = getattr(request.app.state, "auth_token", "")
    jwt_secret: str = getattr(request.app.state, "jwt_secret", "")

    if not auth_token and not jwt_secret:
        log.warning("notify.auth_not_configured")
        return JSONResponse({"error": "authentication not configured"}, status_code=401)

    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    token = header[7:]

    # Static token check
    if auth_token:
        import hmac

        if hmac.compare_digest(token, auth_token):
            return None

    # JWT check
    if jwt_secret and "." in token:
        from turnstone.core.auth import JWT_AUD_CHANNEL, validate_jwt

        result = validate_jwt(token, jwt_secret, audience=JWT_AUD_CHANNEL)
        if result is not None:
            return None

    return JSONResponse({"error": "Unauthorized"}, status_code=401)


async def _handle_notify(request: Request) -> JSONResponse:
    """Deliver a notification to one or more channel adapters."""
    auth_err = _check_auth(request)
    if auth_err is not None:
        return auth_err

    adapters: dict[str, ChannelAdapter] = request.app.state.adapters
    storage: StorageBackend = request.app.state.storage

    try:
        body: dict[str, Any] = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    target = body.get("target")
    message = body.get("message", "").strip() if isinstance(body.get("message"), str) else ""
    title = body.get("title", "").strip() if isinstance(body.get("title"), str) else ""
    ws_id = body.get("ws_id", "").strip() if isinstance(body.get("ws_id"), str) else ""
    if ws_id and not _WS_ID_RE.match(ws_id):
        return JSONResponse({"error": "invalid ws_id format"}, status_code=400)

    if not target or not message:
        return JSONResponse({"error": "target and message are required"}, status_code=400)

    content = f"**{title}**\n{message}" if title else message

    # Resolve targets
    targets: list[tuple[str, str]] = []
    if "username" in target:
        user = await asyncio.to_thread(storage.get_user_by_username, target["username"])
        if user is None:
            log.warning("notify.user_not_found", username=target["username"])
            return JSONResponse(
                {"error": "target not found or has no linked channels"},
                status_code=404,
            )
        links = await asyncio.to_thread(storage.list_channel_users_by_user, user["user_id"])
        for link in links:
            targets.append((link["channel_type"], link["channel_user_id"]))
        if not targets:
            log.warning("notify.user_no_linked_channels", username=target["username"])
            return JSONResponse(
                {"error": "target not found or has no linked channels"},
                status_code=404,
            )
    elif "channel_type" in target and "channel_id" in target:
        targets.append((target["channel_type"], target["channel_id"]))
    else:
        return JSONResponse(
            {"error": "target must have username or channel_type+channel_id"},
            status_code=400,
        )

    results: list[dict[str, str]] = []
    for channel_type, channel_id in targets:
        adapter = adapters.get(channel_type)
        if adapter is None:
            results.append(
                {
                    "channel_type": channel_type,
                    "channel_id": channel_id,
                    "status": "no_adapter",
                }
            )
            log.warning(
                "notify.no_adapter",
                channel_type=channel_type,
                channel_id=channel_id,
            )
            continue
        try:
            if ws_id:
                msg_id = await adapter.send_notification(channel_id, content, ws_id)
            else:
                msg_id = await adapter.send(channel_id, content)
            results.append(
                {
                    "channel_type": channel_type,
                    "channel_id": channel_id,
                    "status": "sent",
                    "message_id": msg_id,
                }
            )
            log.info(
                "notify.delivered",
                channel_type=channel_type,
                channel_id=channel_id,
                message_id=msg_id,
            )
        except Exception:
            log.exception(
                "notify.delivery_failed",
                channel_type=channel_type,
                channel_id=channel_id,
            )
            results.append(
                {
                    "channel_type": channel_type,
                    "channel_id": channel_id,
                    "status": "failed",
                }
            )

    return JSONResponse({"results": results})


def create_channel_app(
    adapters: dict[str, ChannelAdapter],
    storage: StorageBackend,
    *,
    auth_token: str = "",
    jwt_secret: str = "",
) -> Starlette:
    """Create the channel gateway HTTP application."""
    app = Starlette(
        routes=[
            Route("/health", _handle_health),
            Mount(
                "/v1",
                routes=[
                    Route("/api/notify", _handle_notify, methods=["POST"]),
                ],
            ),
        ],
    )
    app.state.adapters = adapters
    app.state.storage = storage
    app.state.auth_token = auth_token
    app.state.jwt_secret = jwt_secret
    return app


def _get_service_id() -> str:
    """Generate a unique service ID from hostname + random suffix."""
    return f"channel-{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
