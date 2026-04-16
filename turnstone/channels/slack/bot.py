"""Slack bot adapter — connects Slack channels/threads to turnstone workstreams.

:class:`TurnstoneSlackBot` uses Slack Bolt with Socket Mode so no public URL
or API Gateway is required. Mirrors the Discord adapter pattern.

Interaction model
-----------------
* **Channels**: invoke with the configured Slack slash command. The bot replies
  in a thread and waits for messages there. Messages outside a bot-created
  thread are ignored.
* Running the slash command again in the same channel (by the same user)
  archives the old workstream and starts a fresh thread.
* **DMs**: every message is routed freely, no slash command needed.

Events are consumed from the server's per-workstream SSE endpoint
(``GET /v1/api/events?ws_id=X``) using httpx-sse. Inbound messages are
sent directly to server nodes via HTTP (``POST /v1/api/send``).

Install dependencies:
    pip install slack-bolt httpx httpx-sse
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import httpx
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from turnstone.channels._formatter import chunk_message
from turnstone.channels._routing import ChannelRouter
from turnstone.channels.slack.routes import SlackRoute
from turnstone.core.log import get_logger
from turnstone.sdk._types import TurnstoneAPIError
from turnstone.sdk.events import (
    ApprovalResolvedEvent,
    ApproveRequestEvent,
    ContentEvent,
    ErrorEvent,
    IntentVerdictEvent,
    PlanReviewEvent,
    ServerEvent,
    StreamEndEvent,
    ThinkingStartEvent,
    ThinkingStopEvent,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    from turnstone.channels.slack.config import SlackConfig
    from turnstone.core.storage._protocol import StorageBackend

log = get_logger(__name__)

_GREETING = "Hey! Let me know what I can help with."

_SSE_RECONNECT_DELAY: float = 2.0
_SSE_MAX_RECONNECT_DELAY: float = 30.0


@dataclass(frozen=True)
class PendingApproval:
    channel: str
    message_ts: str
    owner_user_id: str | None = None


@dataclass
class StreamingMessage:
    """Accumulates streamed content and periodically edits a Slack message."""

    client: Any
    channel: str
    thread_ts: str = ""
    max_length: int = 3000
    edit_interval: float = 1.5

    _ts: str = field(default="", init=False, repr=False)
    _buffer: list[str] = field(default_factory=list, init=False, repr=False)
    _last_edit: float = field(default=0.0, init=False, repr=False)

    async def append(self, text: str) -> None:
        self._buffer.append(text)
        if time.monotonic() - self._last_edit >= self.edit_interval:
            await self._flush()

    async def finalize(self) -> None:
        content = "".join(self._buffer)
        if not content:
            return

        chunks = chunk_message(content, self.max_length)
        if self._ts:
            try:
                await self.client.chat_update(
                    channel=self.channel,
                    ts=self._ts,
                    text=chunks[0],
                )
            except Exception:
                log.debug("slack.streaming_message.finalize_edit_failed")

            for chunk in chunks[1:]:
                await self.client.chat_postMessage(
                    channel=self.channel,
                    thread_ts=self.thread_ts or self._ts,
                    text=chunk,
                )
        else:
            for chunk in chunks:
                resp = await self.client.chat_postMessage(
                    channel=self.channel,
                    thread_ts=self.thread_ts or None,
                    text=chunk,
                )
                if not self._ts and resp.get("ok"):
                    self._ts = resp["ts"]

    async def _flush(self) -> None:
        content = "".join(self._buffer)
        if not content:
            return

        display = content[: self.max_length]
        try:
            if not self._ts:
                resp = await self.client.chat_postMessage(
                    channel=self.channel,
                    thread_ts=self.thread_ts or None,
                    text=display,
                )
                if resp.get("ok"):
                    self._ts = resp["ts"]
            else:
                await self.client.chat_update(
                    channel=self.channel,
                    ts=self._ts,
                    text=display,
                )
        except Exception:
            log.debug("slack.streaming_message.flush_failed")

        self._last_edit = time.monotonic()


class TurnstoneSlackBot:
    """Slack bot bridging Slack channels/threads to turnstone workstreams."""

    channel_type: str = "slack"
    _MAX_NOTIFY_TRACKING: int = 100

    def __init__(
        self,
        config: SlackConfig,
        server_url: str,
        storage: StorageBackend,
        *,
        api_token: str = "",
        console_url: str = "",
        console_token_factory: Callable[[], str] | None = None,
        server_token_factory: Callable[[], str] | None = None,
    ) -> None:
        self.config = config
        self._server_url = server_url.rstrip("/")
        self._console_url = console_url.rstrip("/") if console_url else ""
        self._api_token = api_token
        self._token_factory = server_token_factory
        self.storage = storage

        self.router = ChannelRouter(
            server_url,
            storage,
            auto_approve=config.auto_approve,
            auto_approve_tools=list(config.auto_approve_tools),
            skill=config.skill,
            api_token=api_token,
            console_url=console_url,
            console_token_factory=console_token_factory,
            server_token_factory=server_token_factory,
        )

        self._subscribed_ws: set[str] = set()
        self._sse_tasks: dict[str, asyncio.Task[None]] = {}
        self._streaming: dict[str, StreamingMessage] = {}

        self._pending_approval: dict[str, PendingApproval] = {}
        self._pending_plan_review_ts: dict[str, tuple[str, str]] = {}
        self._notify_ws_map: dict[str, tuple[str, SlackRoute]] = {}
        # Per-workstream override used to route the next streamed assistant
        # response back into a Slack notification reply thread instead of the
        # default session thread.
        self._notify_reply_routes: dict[str, SlackRoute] = {}
        self._channel_sessions: dict[tuple[str, str], tuple[str, str]] = {}

        headers: dict[str, str] = {}
        if api_token and not server_token_factory:
            headers["Authorization"] = f"Bearer {api_token}"

        self._http_client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(connect=10.0, read=90.0, write=10.0, pool=10.0),
        )

        self._app = AsyncApp(token=config.bot_token)
        self._client = AsyncWebClient(token=config.bot_token)
        self._app_token = config.app_token
        self._handler: AsyncSocketModeHandler | None = None

        self._app.command(config.slash_command)(self._on_slash_command)
        self._app.event("message")(self._on_message)
        self._app.action("ts_approve")(self._on_approve)
        self._app.action("ts_deny")(self._on_deny)
        self._app.action("ts_plan_approve")(self._on_plan_approve)
        self._app.action("ts_plan_request_changes")(self._on_plan_request_changes)
        self._app.view("ts_plan_feedback_modal")(self._on_plan_feedback_modal)

    async def start(self) -> None:
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        await self._recover_routes()
        log.info("slack.starting_socket_mode")
        await self._handler.start_async()

    async def stop(self) -> None:
        for ws_id in list(self._subscribed_ws):
            await self.unsubscribe_ws(ws_id)
        await self.router.aclose()
        await self._http_client.aclose()
        if self._handler is not None:
            await self._handler.close_async()
        log.info("slack.stopped")

    async def _recover_routes(self) -> None:
        """Re-subscribe to SSE streams for existing slack routes."""
        routes = await asyncio.to_thread(
            self.storage.list_channel_routes_by_type,
            "slack",
        )

        latest_sessions: dict[tuple[str, str], tuple[str, str]] = {}

        for route in routes:
            ws_id = route["ws_id"]
            channel_id = route["channel_id"]

            await self.subscribe_ws(ws_id, channel_id)

            slack_route = SlackRoute.parse(channel_id)
            if slack_route.channel and slack_route.user_id and slack_route.thread_ts:
                key = (slack_route.channel, slack_route.user_id)
                existing = latest_sessions.get(key)

                if existing is None or float(slack_route.thread_ts) > float(existing[1]):
                    latest_sessions[key] = (ws_id, slack_route.thread_ts)

            log.info("slack.route_recovered", ws_id=ws_id, channel_id=channel_id)

        self._channel_sessions = latest_sessions

        for (slack_channel, user_id), (ws_id, thread_ts) in self._channel_sessions.items():
            log.info(
                "slack.session_recovered",
                slack_channel=slack_channel,
                user_id=user_id,
                ws_id=ws_id,
                thread_ts=thread_ts,
            )

    async def _on_slash_command(self, ack: Any, body: dict[str, Any]) -> None:
        """Start or restart a per-user session in this channel."""
        await ack()

        slack_channel = body["channel_id"]
        user_id = body["user_id"]

        if self.config.allowed_channels and slack_channel not in self.config.allowed_channels:
            await self._client.chat_postEphemeral(
                channel=slack_channel,
                user=user_id,
                text="Sorry, turnstone isn't enabled in this channel.",
            )
            return

        existing = self._channel_sessions.get((slack_channel, user_id))
        if existing is not None:
            old_ws_id, old_thread_ts = existing
            await self._archive_session(
                slack_channel,
                user_id,
                old_ws_id,
                old_thread_ts,
            )

        opener = await self._client.chat_postMessage(
            channel=slack_channel,
            text=f"<@{user_id}> started a turnstone session.",
        )
        if not opener.get("ok"):
            log.error("slack.slash_command.opener_failed", channel=slack_channel)
            return

        opener_ts = opener["ts"]

        await self._client.chat_postMessage(
            channel=slack_channel,
            thread_ts=opener_ts,
            text=_GREETING,
        )

        route = SlackRoute(
            channel=slack_channel,
            user_id=user_id,
            thread_ts=opener_ts,
        )

        ws_id, _ = await self.router.get_or_create_workstream(
            channel_type="slack",
            channel_id=route.to_channel_id(),
            name=f"slack-{slack_channel[:8]}",
        )
        await self.subscribe_ws(ws_id, route.to_channel_id())
        self._channel_sessions[(slack_channel, user_id)] = (ws_id, opener_ts)

        log.info(
            "slack.session_started",
            ws_id=ws_id,
            channel=slack_channel,
            user=user_id,
            thread_ts=opener_ts,
        )

    async def _archive_session(
        self,
        slack_channel: str,
        user_id: str,
        ws_id: str,
        thread_ts: str,
    ) -> None:
        """Mark the old session as archived and unsubscribe."""
        try:
            await self._client.chat_postMessage(
                channel=slack_channel,
                thread_ts=thread_ts,
                text="_This session has been archived. A new one has started._",
            )
        except Exception:
            log.debug("slack.archive_session.notify_failed", ws_id=ws_id)

        route = SlackRoute(
            channel=slack_channel,
            user_id=user_id,
            thread_ts=thread_ts,
        )

        try:
            await self.router.delete_route("slack", route.to_channel_id())
            log.info(
                "slack.archived_route_deleted",
                ws_id=ws_id,
                channel_id=route.to_channel_id(),
            )
        except Exception:
            log.exception(
                "slack.archived_route_delete_failed",
                ws_id=ws_id,
                channel_id=route.to_channel_id(),
            )

        await self.unsubscribe_ws(ws_id)
        self._channel_sessions.pop((slack_channel, user_id), None)
        log.info(
            "slack.session_archived",
            ws_id=ws_id,
            channel=slack_channel,
            user=user_id,
        )

    async def _on_message(self, event: dict[str, Any], say: Any) -> None:
        """Route messages — thread-only in channels, free in DMs."""
        if event.get("bot_id") or event.get("subtype"):
            return

        channel_id = event.get("channel", "")
        channel_type = event.get("channel_type", "")
        thread_ts = event.get("thread_ts", "")
        user_id = event.get("user", "")
        text = event.get("text", "").strip()

        if not text or not channel_id or not user_id:
            return

        if thread_ts and thread_ts in self._notify_ws_map:
            origin_ws_id, notify_route = self._notify_ws_map[thread_ts]
            if channel_id == notify_route.channel and user_id == (notify_route.user_id or ""):
                try:
                    reply_route = SlackRoute(
                        channel=channel_id,
                        user_id=user_id or None,
                        thread_ts=thread_ts or event.get("ts") or None,
                    )
                    self._notify_reply_routes[origin_ws_id] = reply_route
                    log.info(
                        "slack.notification_reply_attempt",
                        thread_ts=thread_ts,
                        ws_id=origin_ws_id,
                        channel=channel_id,
                        user=user_id,
                    )
                    await self.router.send_message(origin_ws_id, text)
                    log.info("slack.notification_reply_routed", ws_id=origin_ws_id)
                except TurnstoneAPIError as exc:
                    self._notify_reply_routes.pop(origin_ws_id, None)
                    if exc.status_code == 404:
                        self._clear_notification_tracking_for_ws(origin_ws_id)
                        log.warning(
                            "slack.notification_reply_dead_ws",
                            ws_id=origin_ws_id,
                            thread_ts=thread_ts,
                        )
                        try:
                            slash_cmd = self.config.slash_command or "/turnstone"
                            await self._client.chat_postEphemeral(
                                channel=channel_id,
                                user=user_id,
                                text=(
                                    "This notification is no longer linked to an active session. "
                                    f"Please start a new one with `{slash_cmd}`."
                                ),
                            )
                        except Exception:
                            log.debug("slack.notification_reply_dead_ws_notice_failed")
                    else:
                        log.exception("slack.notification_reply_failed")
                except Exception:
                    self._notify_reply_routes.pop(origin_ws_id, None)
                    log.exception("slack.notification_reply_failed")
                return

        if channel_id.startswith("D") or channel_type == "im":
            await self._handle_dm(event, say)
            return

        if self.config.allowed_channels and channel_id not in self.config.allowed_channels:
            return

        session = self._channel_sessions.get((channel_id, user_id))
        if session is None:
            return

        ws_id, session_thread_ts = session
        if thread_ts != session_thread_ts:
            return

        try:
            await self.router.send_message(ws_id, text)
            log.info("slack.message_dispatched", ws_id=ws_id, channel=channel_id)
        except Exception:
            log.exception("slack.message_dispatch_failed", channel=channel_id)
            await say(
                text="Sorry, something went wrong routing your message.",
                thread_ts=thread_ts,
            )

    async def _handle_dm(self, event: dict[str, Any], say: Any) -> None:
        """Handle a direct message — no slash command required."""
        channel_id = event.get("channel", "")
        thread_ts = event.get("thread_ts", "")
        user_id = event.get("user", "")
        text = event.get("text", "").strip()
        msg_ts = event.get("ts", "")
        thread_key = thread_ts or msg_ts
        route = SlackRoute(
            channel=channel_id,
            user_id=user_id or None,
            thread_ts=thread_key or None,
        )

        try:
            ws_id, is_new = await self.router.get_or_create_workstream(
                channel_type="slack",
                channel_id=route.to_channel_id(),
                name=f"slack-dm-{user_id[:8]}",
            )
            if is_new:
                await self.subscribe_ws(ws_id, route.to_channel_id())

            await self.router.send_message(ws_id, text)
            log.info("slack.dm_dispatched", ws_id=ws_id, user=user_id)
        except Exception:
            log.exception("slack.dm_dispatch_failed", user=user_id)
            await say(text="Sorry, something went wrong routing your message.")

    async def _on_approve(self, ack: Any, body: dict[str, Any]) -> None:
        await ack()
        value = body["actions"][0].get("value", "")
        parts = value.split("|", 1)
        if len(parts) != 2:
            return

        ws_id, correlation_id = parts
        entry = self._pending_approval.get(ws_id)
        actor_user_id = body.get("user", {}).get("id", "")
        channel = body["container"]["channel_id"]

        log.info(
            "slack.approval_actor_check",
            ws_id=ws_id,
            actor_user_id=actor_user_id,
            owner_user_id=entry.owner_user_id if entry else None,
            has_entry=entry is not None,
        )

        if entry is None or not entry.owner_user_id:
            log.warning(
                "slack.approval_missing_owner",
                ws_id=ws_id,
                actor_user_id=actor_user_id,
            )
            await self._client.chat_postEphemeral(
                channel=channel,
                user=actor_user_id,
                text="This approval can no longer be verified. Please retry from the active session.",
            )
            return

        if actor_user_id != entry.owner_user_id:
            await self._client.chat_postEphemeral(
                channel=channel,
                user=actor_user_id,
                text="Only the session owner can approve this tool call.",
            )
            return

        await self.router.send_approval(ws_id, correlation_id, approved=True)
        ts = body["container"]["message_ts"]

        try:
            await self._client.chat_update(
                channel=channel,
                ts=ts,
                text="Tool approved",
                blocks=[],
            )
        except Exception:
            log.debug("slack.approve_message_update_failed")

    async def _on_deny(self, ack: Any, body: dict[str, Any]) -> None:
        await ack()
        value = body["actions"][0].get("value", "")
        parts = value.split("|", 1)
        if len(parts) != 2:
            return

        ws_id, correlation_id = parts
        entry = self._pending_approval.get(ws_id)
        actor_user_id = body.get("user", {}).get("id", "")
        channel = body["container"]["channel_id"]

        log.info(
            "slack.approval_actor_check",
            ws_id=ws_id,
            actor_user_id=actor_user_id,
            owner_user_id=entry.owner_user_id if entry else None,
            has_entry=entry is not None,
        )

        if entry is None or not entry.owner_user_id:
            log.warning(
                "slack.approval_missing_owner",
                ws_id=ws_id,
                actor_user_id=actor_user_id,
            )
            await self._client.chat_postEphemeral(
                channel=channel,
                user=actor_user_id,
                text="This approval can no longer be verified. Please retry from the active session.",
            )
            return

        if actor_user_id != entry.owner_user_id:
            await self._client.chat_postEphemeral(
                channel=channel,
                user=actor_user_id,
                text="Only the session owner can deny this tool call.",
            )
            return

        await self.router.send_approval(ws_id, correlation_id, approved=False)
        ts = body["container"]["message_ts"]

        try:
            await self._client.chat_update(
                channel=channel,
                ts=ts,
                text="Tool denied",
                blocks=[],
            )
        except Exception:
            log.debug("slack.deny_message_update_failed")

    async def _on_plan_approve(self, ack: Any, body: dict[str, Any]) -> None:
        await ack()
        ws_id = body["actions"][0].get("value", "")
        log.info("slack.plan_approve_clicked", ws_id=ws_id)
        if not ws_id:
            return

        self._streaming.pop(ws_id, None)
        await self.router.send_plan_feedback(ws_id, "", "")
        log.info("slack.plan_feedback_sent", ws_id=ws_id, feedback="")

        channel = body["container"]["channel_id"]
        ts = body["container"]["message_ts"]

        self._pending_plan_review_ts.pop(ws_id, None)

        try:
            await self._client.chat_update(
                channel=channel,
                ts=ts,
                text="Plan approved",
                blocks=[],
            )
        except Exception:
            log.debug("slack.plan_review_approve_update_failed")

    async def _on_plan_request_changes(self, ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        ws_id = body["actions"][0].get("value", "")
        if not ws_id:
            return

        trigger_id = body.get("trigger_id", "")
        if not trigger_id:
            return

        await client.views_open(
            trigger_id=trigger_id,
            view={
                "type": "modal",
                "callback_id": "ts_plan_feedback_modal",
                "private_metadata": ws_id,
                "title": {"type": "plain_text", "text": "Plan feedback"},
                "submit": {"type": "plain_text", "text": "Send"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "feedback_block",
                        "label": {"type": "plain_text", "text": "Requested changes"},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "feedback_input",
                            "multiline": True,
                        },
                    }
                ],
            },
        )

    async def _on_plan_feedback_modal(self, ack: Any, body: dict[str, Any], view: dict[str, Any]) -> None:
        await ack()

        ws_id = view.get("private_metadata", "")
        if not ws_id:
            return

        feedback = (
            view.get("state", {})
            .get("values", {})
            .get("feedback_block", {})
            .get("feedback_input", {})
            .get("value", "")
            .strip()
        )

        if not feedback:
            feedback = "Please revise the plan."

        log.info("slack.plan_feedback_modal_submitted", ws_id=ws_id, feedback=feedback)

        self._streaming.pop(ws_id, None)
        await self.router.send_plan_feedback(ws_id, "", feedback)
        log.info("slack.plan_feedback_sent", ws_id=ws_id, feedback=feedback)

        entry = self._pending_plan_review_ts.pop(ws_id, None)
        if entry is not None:
            channel, ts = entry
            try:
                await self._client.chat_update(
                    channel=channel,
                    ts=ts,
                    text="Plan changes requested",
                    blocks=[],
                )
            except Exception:
                log.debug("slack.plan_review_modal_update_failed")

    async def subscribe_ws(self, ws_id: str, channel_id: str) -> None:
        if ws_id in self._subscribed_ws:
            return

        task = asyncio.create_task(
            self._sse_listener(ws_id, channel_id),
            name=f"sse:{ws_id}",
        )
        self._sse_tasks[ws_id] = task
        self._subscribed_ws.add(ws_id)
        log.info("slack.subscribed", ws_id=ws_id, channel_id=channel_id)

    async def unsubscribe_ws(self, ws_id: str) -> None:
        task = self._sse_tasks.pop(ws_id, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        self._subscribed_ws.discard(ws_id)
        self._streaming.pop(ws_id, None)
        self._pending_approval.pop(ws_id, None)
        self._pending_plan_review_ts.pop(ws_id, None)
        self._clear_notification_tracking_for_ws(ws_id)

        log.info("slack.unsubscribed", ws_id=ws_id)

    async def _sse_listener(self, ws_id: str, channel_id: str) -> None:
        """Connect to the server SSE endpoint and dispatch events."""
        import httpx_sse

        delay = _SSE_RECONNECT_DELAY
        url = ""

        while True:
            try:
                node_base = await self.router.get_node_url(ws_id)
                url = f"{node_base}/v1/api/events"

                sse_headers: dict[str, str] | None = None
                if self._token_factory is not None:
                    sse_headers = {"Authorization": f"Bearer {self._token_factory()}"}

                async with httpx_sse.aconnect_sse(
                    self._http_client,
                    "GET",
                    url,
                    params={"ws_id": ws_id},
                    headers=sse_headers,
                ) as event_source:
                    status = event_source.response.status_code
                    if status == 404:
                        log.info("slack.sse_ws_gone", ws_id=ws_id)
                        await self._cleanup_stale_route(ws_id, channel_id)
                        return

                    if status >= 400:
                        log.warning(
                            "slack.sse_upstream_error",
                            ws_id=ws_id,
                            status=status,
                        )
                        raise httpx.HTTPStatusError(
                            f"SSE upstream {status}",
                            request=event_source.response.request,
                            response=event_source.response,
                        )

                    delay = _SSE_RECONNECT_DELAY
                    async for sse in event_source.aiter_sse():
                        if sse.event == "message" or not sse.event:
                            try:
                                data = json.loads(sse.data)
                            except json.JSONDecodeError:
                                log.debug("slack.sse_invalid_json", ws_id=ws_id)
                                continue

                            event = ServerEvent.from_dict(data)
                            try:
                                effective_route = self._notify_reply_routes.get(
                                    ws_id,
                                    SlackRoute.parse(channel_id),
                                )
                                await self._on_ws_event(ws_id, effective_route, event)
                            except Exception:
                                log.warning(
                                    "slack.event_dispatch_failed",
                                    ws_id=ws_id,
                                    exc_info=True,
                                )

            except httpx.HTTPStatusError:
                pass
            except httpx.RemoteProtocolError:
                log.debug("slack.sse_remote_closed", ws_id=ws_id)
            except asyncio.CancelledError:
                return
            except httpx.ReadTimeout:
                log.info("slack.sse_read_timeout", ws_id=ws_id)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                log.warning(
                    "slack.sse_connect_failed",
                    ws_id=ws_id,
                    url=url,
                    error=str(exc),
                )
            except Exception:
                log.warning("slack.sse_error", ws_id=ws_id, exc_info=True)

            await asyncio.sleep(delay)
            delay = min(delay * 2, _SSE_MAX_RECONNECT_DELAY)

    async def _cleanup_stale_route(self, ws_id: str, channel_id: str) -> None:
        """Remove a channel route whose workstream no longer exists."""
        route = SlackRoute.parse(channel_id)
        if route.channel and route.user_id and route.thread_ts:
            self._channel_sessions.pop((route.channel, route.user_id), None)

        await self.router.delete_route("slack", channel_id)
        self._subscribed_ws.discard(ws_id)
        self._sse_tasks.pop(ws_id, None)
        self._streaming.pop(ws_id, None)
        self._pending_approval.pop(ws_id, None)
        self._pending_plan_review_ts.pop(ws_id, None)
        self._clear_notification_tracking_for_ws(ws_id)

        log.info("slack.stale_route_removed", ws_id=ws_id)

    async def _on_ws_event(
        self,
        ws_id: str,
        route: SlackRoute,
        event: ServerEvent,
    ) -> None:
        """Handle a typed server event for a subscribed workstream."""
        slack_channel = route.channel
        thread_ts = route.thread_ts or ""
        owner_user_id = route.user_id

        if isinstance(event, (ThinkingStartEvent, ThinkingStopEvent)):
            pass

        elif isinstance(event, ContentEvent):
            sm = self._streaming.get(ws_id)
            if sm is None or sm.channel != slack_channel or sm.thread_ts != thread_ts:
                sm = StreamingMessage(
                    client=self._client,
                    channel=slack_channel,
                    thread_ts=thread_ts,
                    max_length=self.config.max_message_length,
                    edit_interval=self.config.streaming_edit_interval,
                )
                self._streaming[ws_id] = sm

            await sm.append(event.text)

        elif isinstance(event, ApproveRequestEvent):
            _policy_handled = False
            if self.storage is not None:
                try:
                    from turnstone.core.policy import evaluate_tool_policies_batch

                    _tool_names = [
                        it.get("approval_label", "") or it.get("func_name", "")
                        for it in event.items
                        if it.get("needs_approval")
                        and it.get("func_name")
                        and not it.get("error")
                    ]
                    _tool_names = [n for n in _tool_names if n]
                    if _tool_names:
                        verdicts = await asyncio.to_thread(
                            evaluate_tool_policies_batch,
                            self.storage,
                            _tool_names,
                        )
                        if any(v == "deny" for v in verdicts.values()):
                            denied = [n for n, v in verdicts.items() if v == "deny"]
                            await self.router.send_approval(
                                ws_id,
                                "",
                                approved=False,
                                feedback=f"Blocked by tool policy: {', '.join(denied)}",
                            )
                            await self._client.chat_postMessage(
                                channel=slack_channel,
                                thread_ts=thread_ts or None,
                                text=f"_Tool blocked by admin policy: {', '.join(denied)}_",
                            )
                            _policy_handled = True
                        elif all(verdicts.get(n) == "allow" for n in _tool_names):
                            await self.router.send_approval(ws_id, "", approved=True)
                            await self._client.chat_postMessage(
                                channel=slack_channel,
                                thread_ts=thread_ts or None,
                                text="_Tool approved by policy._",
                            )
                            _policy_handled = True
                except Exception:
                    log.debug(
                        "Tool policy evaluation failed for ws %s",
                        ws_id,
                        exc_info=True,
                    )

            if not _policy_handled and (
                self.config.auto_approve or self._should_auto_approve(event)
            ):
                await self.router.send_approval(ws_id, "", approved=True)
                await self._client.chat_postMessage(
                    channel=slack_channel,
                    thread_ts=thread_ts or None,
                    text="_Tool auto-approved._",
                )
            elif not _policy_handled:
                await self._send_approval_request(
                    ws_id,
                    "",
                    event.items,
                    slack_channel,
                    thread_ts,
                    owner_user_id,
                )

        elif isinstance(event, IntentVerdictEvent):
            entry = self._pending_approval.get(ws_id)
            if entry is not None:
                pending_channel = entry.channel
                pending_ts = entry.message_ts
                risk = (event.risk_level or "medium").upper()
                verdict_text = (
                    f"*Judge Verdict: {event.func_name or 'tool'}*\n"
                    f"Risk: {risk} | Confidence: {event.confidence or 'N/A'}\n"
                    f"_{event.intent_summary or ''}_"
                )
                try:
                    result = await self._client.conversations_history(
                        channel=pending_channel,
                        latest=pending_ts,
                        limit=1,
                        inclusive=True,
                    )
                    existing_blocks = []
                    if result.get("ok") and result.get("messages"):
                        existing_blocks = result["messages"][0].get("blocks", [])
                    existing_blocks.append(
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": verdict_text,
                            },
                        }
                    )
                    await self._client.chat_update(
                        channel=pending_channel,
                        ts=pending_ts,
                        blocks=existing_blocks,
                        text="Tool approval required",
                    )
                except Exception:
                    log.debug("slack.verdict_message_update_failed", ws_id=ws_id)

        elif isinstance(event, PlanReviewEvent):
            log.info("slack.plan_review_received", ws_id=ws_id)
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Plan Review*\n```{event.content[:2000]}```",
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Approve"},
                            "style": "primary",
                            "action_id": "ts_plan_approve",
                            "value": ws_id,
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Request changes"},
                            "style": "danger",
                            "action_id": "ts_plan_request_changes",
                            "value": ws_id,
                        },
                    ],
                },
            ]

            resp = await self._client.chat_postMessage(
                channel=slack_channel,
                thread_ts=thread_ts or None,
                text="Plan review required",
                blocks=cast("list[dict[str, Any]]", blocks),
            )
            if resp.get("ok"):
                self._pending_plan_review_ts[ws_id] = (slack_channel, resp["ts"])

        elif isinstance(event, ApprovalResolvedEvent):
            entry = self._pending_approval.pop(ws_id, None)
            if entry is not None:
                pending_channel = entry.channel
                pending_ts = entry.message_ts
                label = "Approved" if event.approved else "Denied"
                try:
                    await self._client.chat_update(
                        channel=pending_channel,
                        ts=pending_ts,
                        text=label,
                        blocks=cast("list[dict[str, Any]]", []),
                    )
                except Exception:
                    log.debug("slack.approval_resolved_edit_failed", ws_id=ws_id)

        elif isinstance(event, StreamEndEvent):
            sm = self._streaming.pop(ws_id, None)

            if sm is not None:
                await sm.finalize()

            reply_route = self._notify_reply_routes.get(ws_id)
            if reply_route is not None and sm is not None and sm._ts:
                if reply_route.channel and reply_route.user_id:
                    self._track_notification(sm._ts, ws_id, reply_route)

            self._pending_approval.pop(ws_id, None)

        elif isinstance(event, ErrorEvent):
            safe_msg = event.message[:500] if event.message else "An error occurred"
            await self._client.chat_postMessage(
                channel=slack_channel,
                thread_ts=thread_ts or None,
                text=f"*Error:* {safe_msg}",
            )

    async def _send_approval_request(
        self,
        ws_id: str,
        correlation_id: str,
        items: list[dict[str, Any]],
        channel: str,
        thread_ts: str,
        owner_user_id: str | None,
    ) -> None:
        tool_lines = []
        for item in items:
            name = item.get("approval_label") or item.get("func_name") or "tool"
            preview = item.get("preview", "")
            tool_lines.append(
                f"• *{name}*\n```{preview}```" if preview else f"• *{name}*"
            )

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Tool Approval Required*\n" + "\n".join(tool_lines),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "style": "primary",
                        "action_id": "ts_approve",
                        "value": f"{ws_id}|{correlation_id}",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Deny"},
                        "style": "danger",
                        "action_id": "ts_deny",
                        "value": f"{ws_id}|{correlation_id}",
                    },
                ],
            },
        ]

        resp = await self._client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts or None,
            text="Tool approval required",
            blocks=cast("list[dict[str, Any]]", blocks),
        )
        if resp.get("ok"):
            self._pending_approval[ws_id] = PendingApproval(
                channel=channel,
                message_ts=resp["ts"],
                owner_user_id=owner_user_id,
            )
            log.info(
                "slack.pending_approval_stored",
                ws_id=ws_id,
                channel=channel,
                thread_ts=thread_ts,
                owner_user_id=owner_user_id,
            )

    def _should_auto_approve(self, event: ApproveRequestEvent) -> bool:
        allowed = self.config.auto_approve_tools
        if not allowed or not event.items:
            return False

        for item in event.items:
            name = (
                item.get("func_name")
                or item.get("approval_label")
                or item.get("function", {}).get("name", "")
            )
            if name not in allowed:
                return False

        return True

    def _track_notification(
        self,
        msg_ts: str,
        ws_id: str,
        route: SlackRoute,
    ) -> None:
        while len(self._notify_ws_map) >= self._MAX_NOTIFY_TRACKING:
            del self._notify_ws_map[next(iter(self._notify_ws_map))]
        self._notify_ws_map[msg_ts] = (ws_id, route)
        log.info(
            "slack.notification_tracked",
            message_ts=msg_ts,
            ws_id=ws_id,
            channel=route.channel,
            user=route.user_id,
        )

    async def send(self, channel_id: str, content: str) -> str:
        route = SlackRoute.parse(channel_id)

        root_ts = route.thread_ts or ""
        first_post_ts = ""

        for i, chunk in enumerate(chunk_message(content, self.config.max_message_length)):
            resp = await self._client.chat_postMessage(
                channel=route.channel,
                thread_ts=root_ts or None,
                text=chunk,
            )
            if resp.get("ok"):
                ts = resp["ts"]
                if i == 0:
                    first_post_ts = ts
                    if not root_ts:
                        root_ts = ts

        return root_ts or first_post_ts

    async def send_notification(self, channel_id: str, content: str, ws_id: str) -> str:
        route = SlackRoute.parse(channel_id)
        thread_root_ts = await self.send(channel_id, content)
        if thread_root_ts and ws_id and route.channel and route.user_id:
            self._track_notification(
                thread_root_ts,
                ws_id,
                SlackRoute(
                    channel=route.channel,
                    user_id=route.user_id,
                    thread_ts=thread_root_ts,
                ),
            )
        return thread_root_ts

    def _clear_notification_tracking_for_ws(self, ws_id: str) -> None:
        self._notify_reply_routes.pop(ws_id, None)
        stale = [ts for ts, entry in self._notify_ws_map.items() if entry[0] == ws_id]
        for ts in stale:
            del self._notify_ws_map[ts]