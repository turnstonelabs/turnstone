"""Channel router -- maps external channels/threads to turnstone workstreams.

:class:`ChannelRouter` uses the turnstone SDK clients to communicate with
the server (single-node) or console (multi-node) API, and the storage
backend for persistent channel-to-workstream mappings.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from turnstone.core.log import get_logger
from turnstone.sdk._types import TurnstoneAPIError
from turnstone.sdk.console import AsyncTurnstoneConsole
from turnstone.sdk.server import AsyncTurnstoneServer

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.storage import StorageBackend

log = get_logger(__name__)

_WS_CREATE_TIMEOUT = 30.0  # seconds


class ChannelRouter:
    """Manage channel-to-workstream routing via SDK clients.

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
        console_url: str = "",
        console_token_factory: Callable[[], str] | None = None,
        server_token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._server_url = server_url.rstrip("/")
        self._console_url = console_url.rstrip("/") if console_url else ""
        self._storage = storage
        self._auto_approve = auto_approve
        self._auto_approve_tools: list[str] = auto_approve_tools or []
        self._skill = skill
        self._create_locks: dict[str, asyncio.Lock] = {}
        # Per-workstream node URLs from console routing responses.
        # Populated when console_url is set and the create response
        # includes node_url.
        self._node_urls: dict[str, str] = {}

        # SDK clients: use console for multi-node, server for single-node.
        self._console: AsyncTurnstoneConsole | None = None
        self._server: AsyncTurnstoneServer | None = None
        if self._console_url:
            self._console = AsyncTurnstoneConsole(
                base_url=self._console_url,
                token=api_token,
                token_factory=console_token_factory,
                timeout=_WS_CREATE_TIMEOUT,
            )
        else:
            self._server = AsyncTurnstoneServer(
                base_url=self._server_url,
                token=api_token,
                token_factory=server_token_factory,
                timeout=_WS_CREATE_TIMEOUT,
            )

    # -- lifecycle -----------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying SDK clients."""
        if self._server:
            await self._server.aclose()
        if self._console:
            await self._console.aclose()
        log.info("channel_router.closed")

    # -- internal helpers ----------------------------------------------------

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
        client_type: str = "",
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

            # 2. Create via SDK client with atomic resume.
            resume_ws = old_ws_id or ""
            _tools_csv = ",".join(self._auto_approve_tools) if self._auto_approve_tools else ""
            log.info(
                "channel_router.creating_workstream",
                channel_type=channel_type,
                channel_id=channel_id,
                resume_ws=resume_ws or None,
            )

            if self._console:
                data = await self._console.route_create_workstream(
                    name=name,
                    model=model,
                    resume_ws=resume_ws,
                    skill=self._skill,
                    auto_approve=self._auto_approve,
                    auto_approve_tools=_tools_csv,
                    client_type=client_type,
                )
                ws_id = data.get("ws_id", "")
            else:
                assert self._server is not None
                resp = await self._server.create_workstream(
                    name=name,
                    model=model,
                    resume_ws=resume_ws,
                    skill=self._skill,
                    auto_approve=self._auto_approve,
                    auto_approve_tools=_tools_csv,
                    client_type=client_type,
                )
                ws_id = resp.ws_id
                data = {"ws_id": resp.ws_id, "name": resp.name}

            if not ws_id:
                msg_err = "workstream creation returned empty ws_id"
                raise RuntimeError(msg_err)

            # When routed through the console, capture the node URL for
            # direct SSE connections.
            node_url = data.get("node_url", "")
            if node_url:
                self._node_urls[ws_id] = node_url.rstrip("/")

            # 3. Send the initial message if this is a brand-new workstream.
            if initial_message and not resume_ws:
                if self._console:
                    await self._console.route_send(initial_message, ws_id)
                else:
                    assert self._server is not None
                    await self._server.send(initial_message, ws_id)

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

    async def get_node_url(self, ws_id: str) -> str:
        """Return the direct server URL for SSE connections to *ws_id*.

        When routing through a console, this is the ``node_url`` from the
        create response.  If the ws_id is not cached (e.g. after a bot
        restart), queries the console's route lookup endpoint before
        falling back to the configured server_url.
        """
        url = self._node_urls.get(ws_id)
        if url:
            return url
        if self._console:
            try:
                data = await self._console.route_lookup(ws_id)
                node_url = data.get("node_url", "")
                if node_url:
                    self._node_urls[ws_id] = node_url.rstrip("/")
                    return self._node_urls[ws_id]
            except Exception:
                pass
        return self._server_url

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
        if self._console:
            await self._console.route_send(message, ws_id)
        else:
            assert self._server is not None
            await self._server.send(message, ws_id)
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
        if self._console:
            await self._console.route_approve(
                ws_id=ws_id, approved=approved, feedback=feedback, always=always
            )
        else:
            assert self._server is not None
            await self._server.approve(
                ws_id=ws_id, approved=approved, feedback=feedback or None, always=always
            )
        log.debug(
            "channel_router.send_approval",
            ws_id=ws_id,
            correlation_id=correlation_id,
            approved=approved,
        )

    async def send_plan_feedback(self, ws_id: str, correlation_id: str, feedback: str) -> None:
        """Respond to a plan review via the server API."""
        if self._console:
            await self._console.route_plan_feedback(ws_id=ws_id, feedback=feedback)
        else:
            assert self._server is not None
            await self._server.plan_feedback(ws_id=ws_id, feedback=feedback)
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
        self._node_urls.pop(ws_id, None)
        try:
            if self._console:
                await self._console.route_close(ws_id)
            else:
                assert self._server is not None
                await self._server.close_workstream(ws_id)
            log.info("channel_router.close_workstream", ws_id=ws_id)
        except TurnstoneAPIError as exc:
            log.warning(
                "channel_router.close_workstream_failed",
                ws_id=ws_id,
                status=exc.status_code,
            )
