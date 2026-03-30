"""Channel router -- maps external channels/threads to turnstone workstreams.

:class:`ChannelRouter` uses direct HTTP calls to the turnstone server API
and the storage backend for persistent channel-to-workstream mappings.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from turnstone.core.storage import StorageBackend

log = get_logger(__name__)

_WS_CREATE_TIMEOUT = 30.0  # seconds


class ChannelRouter:
    """Manage channel-to-workstream routing via the server REST API.

    Parameters
    ----------
    server_url:
        Base URL of the turnstone server (e.g. ``http://localhost:8080/v1``).
    storage:
        A :class:`StorageBackend` instance for persistent route lookups.
        All storage calls are synchronous and will be wrapped in
        :func:`asyncio.to_thread`.
    api_token:
        Optional bearer token for authenticating with the server API.
    """

    def __init__(
        self,
        server_url: str,
        storage: StorageBackend,
        *,
        auto_approve: bool = False,
        auto_approve_tools: list[str] | None = None,
        skill: str = "",
        api_token: str = "",
    ) -> None:
        self._server_url = server_url.rstrip("/")
        self._storage = storage
        self._auto_approve = auto_approve
        self._auto_approve_tools: list[str] = auto_approve_tools or []
        self._skill = skill
        self._create_locks: dict[str, asyncio.Lock] = {}
        headers: dict[str, str] = {}
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        self._client = httpx.AsyncClient(
            base_url=self._server_url,
            headers=headers,
            timeout=_WS_CREATE_TIMEOUT,
        )

    # -- lifecycle -----------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
        log.info("channel_router.closed")

    # -- internal helpers ----------------------------------------------------

    async def _post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        """POST JSON to the server and return the response."""
        resp = await self._client.post(path, json=body)
        resp.raise_for_status()
        return resp

    async def _is_ws_alive(self, ws_id: str) -> bool:
        """Check whether *ws_id* is a known workstream.

        Uses an O(1) storage lookup (primary-key query) instead of
        fetching the full workstream list from the server.  If the
        workstream exists in the database it is considered alive.  A
        false positive (exists in DB but not loaded on any server node)
        is harmless -- the subsequent ``send_message`` call will receive
        a 404 and the adapter will handle reconnection.
        """
        try:
            resolved = await asyncio.to_thread(self._storage.resolve_workstream, ws_id)
            return resolved is not None
        except Exception:
            return False

    # -- workstream management -----------------------------------------------

    async def get_or_create_workstream(
        self,
        channel_type: str,
        channel_id: str,
        name: str = "",
        model: str = "",
        initial_message: str = "",
    ) -> tuple[str, bool]:
        """Look up or create a workstream for a channel.

        Returns ``(ws_id, is_new)`` where *is_new* is ``True`` when a new
        workstream was created.

        A per-channel lock prevents duplicate workstreams when concurrent
        messages arrive for the same channel before the first creation
        completes.
        """
        key = f"{channel_type}:{channel_id}"
        lock = self._create_locks.setdefault(key, asyncio.Lock())

        old_ws_id: str | None = None

        async with lock:
            # 1. Check for existing route.
            old_ws_id = ""
            route = await asyncio.to_thread(
                self._storage.get_channel_route, channel_type, channel_id
            )
            if route:
                # Verify the workstream is still alive on the server.
                if await self._is_ws_alive(route["ws_id"]):
                    return route["ws_id"], False
                # Workstream was evicted/closed — capture old ws_id for
                # resume, then remove the stale route.
                old_ws_id = route["ws_id"]
                await asyncio.to_thread(
                    self._storage.delete_channel_route, channel_type, channel_id
                )
                log.info(
                    "channel_router.stale_route_cleared",
                    ws_id=old_ws_id,
                    channel_type=channel_type,
                    channel_id=channel_id,
                )

            # 2. Create via HTTP API with atomic resume.
            # Note: auto_approve_tools is not passed here because the server's
            # create endpoint does not accept it.  Per-tool auto-approve is
            # handled channel-side in the adapter's _should_auto_approve().
            resume_ws = old_ws_id or ""
            body: dict[str, Any] = {
                "name": name,
                "model": model,
                "resume_ws": resume_ws,
                "skill": self._skill,
                "auto_approve": self._auto_approve,
            }
            log.info(
                "channel_router.creating_workstream",
                channel_type=channel_type,
                channel_id=channel_id,
                resume_ws=resume_ws or None,
            )

            resp = await self._post("/api/workstreams/new", body)
            data = resp.json()
            ws_id: str = data.get("ws_id", "")

            if not ws_id:
                msg_err = "workstream creation returned empty ws_id"
                raise RuntimeError(msg_err)

            # 3. Send the initial message if this is a brand-new workstream.
            if initial_message and not resume_ws:
                await self._post("/api/send", {"ws_id": ws_id, "message": initial_message})

            # 4. Persist the route.
            await asyncio.to_thread(
                self._storage.create_channel_route, channel_type, channel_id, ws_id
            )
            log.info(
                "channel_router.route_created",
                ws_id=ws_id,
                channel_type=channel_type,
                channel_id=channel_id,
            )

            return ws_id, True

    # -- user resolution -----------------------------------------------------

    async def resolve_user(self, channel_type: str, channel_user_id: str) -> str | None:
        """Resolve an external platform user to a turnstone ``user_id``.

        Returns ``None`` if no mapping exists.
        """
        result = await asyncio.to_thread(
            self._storage.get_channel_user, channel_type, channel_user_id
        )
        if result is None:
            return None
        return result.get("user_id")

    # -- message dispatch ----------------------------------------------------

    async def send_message(self, ws_id: str, message: str) -> None:
        """Send a user message to a workstream via the server API."""
        await self._post("/api/send", {"ws_id": ws_id, "message": message})
        log.debug("channel_router.send_message", ws_id=ws_id)

    async def send_approval(
        self,
        ws_id: str,
        correlation_id: str,
        approved: bool,
        feedback: str = "",
        always: bool = False,
    ) -> None:
        """Approve or deny a pending tool call via the server API."""
        body: dict[str, Any] = {
            "ws_id": ws_id,
            "approved": approved,
            "always": always,
        }
        if feedback:
            body["feedback"] = feedback
        await self._post("/api/approve", body)
        log.debug(
            "channel_router.send_approval",
            ws_id=ws_id,
            correlation_id=correlation_id,
            approved=approved,
        )

    async def send_plan_feedback(self, ws_id: str, correlation_id: str, feedback: str) -> None:
        """Respond to a plan review via the server API."""
        await self._post("/api/plan", {"ws_id": ws_id, "feedback": feedback})
        log.debug(
            "channel_router.send_plan_feedback",
            ws_id=ws_id,
            correlation_id=correlation_id,
        )

    # -- route management ----------------------------------------------------

    async def delete_route(self, channel_type: str, channel_id: str) -> None:
        """Remove a channel-to-workstream mapping."""
        deleted = await asyncio.to_thread(
            self._storage.delete_channel_route, channel_type, channel_id
        )
        log.info(
            "channel_router.delete_route",
            channel_type=channel_type,
            channel_id=channel_id,
            deleted=deleted,
        )

    async def close_workstream(self, ws_id: str) -> None:
        """Close a workstream via the server API."""
        try:
            await self._post("/api/workstreams/close", {"ws_id": ws_id})
            log.info("channel_router.close_workstream", ws_id=ws_id)
        except httpx.HTTPStatusError as exc:
            log.warning(
                "channel_router.close_workstream_failed",
                ws_id=ws_id,
                status=exc.response.status_code,
            )
