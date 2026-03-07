"""Channel router -- maps external channels/threads to turnstone workstreams.

:class:`ChannelRouter` uses the async Redis broker for MQ communication and
the storage backend for persistent channel-to-workstream mappings.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from turnstone.core.log import get_logger
from turnstone.mq.protocol import (
    ApproveMessage,
    CreateWorkstreamMessage,
    OutboundEvent,
    PlanFeedbackMessage,
    SendMessage,
    WorkstreamClosedEvent,
    WorkstreamCreatedEvent,
)

if TYPE_CHECKING:
    from turnstone.core.storage import StorageBackend
    from turnstone.mq.async_broker import AsyncRedisBroker

log = get_logger(__name__)

_WS_CREATE_TIMEOUT = 30.0  # seconds


class ChannelRouter:
    """Manage channel-to-workstream routing and MQ message dispatch.

    Parameters
    ----------
    broker:
        An :class:`AsyncRedisBroker` used for pub/sub and queue operations.
    storage:
        A :class:`StorageBackend` instance for persistent route lookups.
        All storage calls are synchronous and will be wrapped in
        :func:`asyncio.to_thread`.
    """

    def __init__(
        self,
        broker: AsyncRedisBroker,
        storage: StorageBackend,
        *,
        auto_approve: bool = False,
        auto_approve_tools: list[str] | None = None,
    ) -> None:
        self._broker = broker
        self._storage = storage
        self._auto_approve = auto_approve
        self._auto_approve_tools: list[str] = auto_approve_tools or []
        self._pending: dict[str, asyncio.Event] = {}
        self._pending_results: dict[str, str] = {}
        self._global_task: asyncio.Task[None] | None = None
        self._create_locks: dict[str, asyncio.Lock] = {}

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Subscribe to global events for workstream lifecycle."""
        channel = f"{self._broker._prefix}:events:global"
        await self._broker.subscribe(channel, self._on_global_event)
        log.info("channel_router.started", channel=channel)

    async def stop(self) -> None:
        """Unsubscribe and clean up pending state."""
        channel = f"{self._broker._prefix}:events:global"
        await self._broker.unsubscribe(channel)
        # Wake any waiters so they don't hang forever.
        for evt in self._pending.values():
            evt.set()
        self._pending.clear()
        self._pending_results.clear()
        log.info("channel_router.stopped")

    # -- event handler -------------------------------------------------------

    async def _on_global_event(self, raw: str) -> None:
        """Handle events on the global pub/sub channel.

        Exceptions are caught so the broker listener task stays alive.
        """
        try:
            event = OutboundEvent.from_json(raw)

            if isinstance(event, WorkstreamCreatedEvent):
                cid = event.correlation_id
                if cid in self._pending:
                    self._pending_results[cid] = event.ws_id
                    self._pending[cid].set()
                    log.debug(
                        "channel_router.ws_created",
                        ws_id=event.ws_id,
                        correlation_id=cid,
                    )

            elif isinstance(event, WorkstreamClosedEvent):
                ws_id = event.ws_id
                route = await asyncio.to_thread(self._storage.get_channel_route_by_ws, ws_id)
                if route:
                    # Don't delete the route — the workstream may have been evicted
                    # and the thread can reactivate it.  Route cleanup only happens
                    # via explicit /close or delete_route().
                    log.info(
                        "channel_router.ws_closed_route_kept",
                        ws_id=ws_id,
                        channel_type=route["channel_type"],
                        channel_id=route["channel_id"],
                    )
        except Exception:
            log.exception("channel_router.global_event_error")

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
                # Verify the workstream is still alive (owned by a bridge node).
                owner = await self._broker.get_ws_owner(route["ws_id"])
                if owner:
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

            # 2. Create via MQ with atomic resume (reuse old ws_id directly).
            resume_ws = old_ws_id or ""
            msg = CreateWorkstreamMessage(
                name=name,
                model=model,
                initial_message="" if resume_ws else initial_message,
                resume_ws=resume_ws,
                auto_approve=self._auto_approve,
                auto_approve_tools=list(self._auto_approve_tools),
            )
            cid = msg.correlation_id
            waiter = asyncio.Event()
            self._pending[cid] = waiter

            await self._broker.push_inbound(msg.to_json())
            log.info(
                "channel_router.creating_workstream",
                correlation_id=cid,
                channel_type=channel_type,
                channel_id=channel_id,
                resume_ws=resume_ws or None,
            )

            try:
                await asyncio.wait_for(waiter.wait(), timeout=_WS_CREATE_TIMEOUT)
            except TimeoutError:
                self._pending.pop(cid, None)
                self._pending_results.pop(cid, None)
                raise

            ws_id = self._pending_results.pop(cid, "")
            self._pending.pop(cid, None)

            if not ws_id:
                msg_err = "workstream creation returned empty ws_id"
                raise RuntimeError(msg_err)

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

    async def send_message(self, ws_id: str, message: str) -> str:
        """Push a :class:`SendMessage` to the broker.

        Returns the ``correlation_id`` of the submitted message.
        """
        msg = SendMessage(
            ws_id=ws_id,
            message=message,
            auto_approve=self._auto_approve,
            auto_approve_tools=list(self._auto_approve_tools),
        )
        await self._broker.push_inbound(msg.to_json())
        log.debug("channel_router.send_message", ws_id=ws_id, correlation_id=msg.correlation_id)
        return msg.correlation_id

    async def send_approval(
        self,
        ws_id: str,
        correlation_id: str,
        approved: bool,
        feedback: str = "",
        always: bool = False,
    ) -> None:
        """Push an :class:`ApproveMessage` to the broker response queue."""
        msg = ApproveMessage(
            ws_id=ws_id,
            request_id=correlation_id,
            approved=approved,
            feedback=feedback or None,
            always=always,
        )
        await self._broker.push_response(correlation_id, msg.to_json())
        log.debug(
            "channel_router.send_approval",
            ws_id=ws_id,
            correlation_id=correlation_id,
            approved=approved,
        )

    async def send_plan_feedback(self, ws_id: str, correlation_id: str, feedback: str) -> None:
        """Push a :class:`PlanFeedbackMessage` to the broker response queue."""
        msg = PlanFeedbackMessage(
            ws_id=ws_id,
            request_id=correlation_id,
            feedback=feedback,
        )
        await self._broker.push_response(correlation_id, msg.to_json())
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
