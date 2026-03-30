"""Discord bot adapter — connects Discord threads to turnstone workstreams.

:class:`TurnstoneBot` extends ``discord.ext.commands.Bot`` and manages the
lifecycle of SSE event subscriptions, streaming message edits, and interactive
approval / plan-review views.

Events are consumed from the server's per-workstream SSE endpoint
(``GET /v1/api/events?ws_id=X``) using httpx-sse.  Inbound messages are
sent directly to server nodes via HTTP (``POST /v1/api/send``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

from turnstone.channels._formatter import chunk_message
from turnstone.channels._routing import ChannelRouter
from turnstone.core.log import get_logger
from turnstone.sdk.events import (
    ApproveRequestEvent,
    ContentEvent,
    ErrorEvent,
    IntentVerdictEvent,
    PlanReviewEvent,
    ServerEvent,
    StreamEndEvent,
)

if TYPE_CHECKING:
    import discord
    from discord.ext import commands

    from turnstone.channels.discord.config import DiscordConfig
    from turnstone.core.storage._protocol import StorageBackend

log = get_logger(__name__)

# SSE reconnection parameters
_SSE_RECONNECT_DELAY: float = 2.0
_SSE_MAX_RECONNECT_DELAY: float = 30.0


# ---------------------------------------------------------------------------
# StreamingMessage helper
# ---------------------------------------------------------------------------


@dataclass
class StreamingMessage:
    """Accumulates streamed content and periodically edits a Discord message.

    Discord rate-limits message edits, so we batch content updates and flush
    at a configurable interval.  On finalize we send any remaining content
    and chunk if the total exceeds the platform limit.
    """

    channel: discord.abc.Messageable
    max_length: int = 2000
    edit_interval: float = 1.5
    _message: discord.Message | None = field(default=None, init=False, repr=False)
    _buffer: list[str] = field(default_factory=list, init=False, repr=False)
    _last_edit: float = field(default=0.0, init=False, repr=False)

    async def append(self, text: str) -> None:
        """Add *text* to the buffer and edit the message if the interval has elapsed."""
        self._buffer.append(text)
        now = time.monotonic()
        if now - self._last_edit >= self.edit_interval:
            await self._flush()

    async def finalize(self) -> None:
        """Flush any remaining buffered content, chunking if necessary."""
        content = "".join(self._buffer)
        if not content:
            return

        if self._message is not None:
            # Final edit — may need chunking if content grew beyond the limit.
            chunks = chunk_message(content, self.max_length)
            try:
                await self._message.edit(content=chunks[0])
            except Exception:
                log.debug("streaming_message.edit_failed_on_finalize")
            # Any overflow chunks are sent as new messages.
            for chunk in chunks[1:]:
                await self.channel.send(chunk)
        else:
            # Never sent an initial message — send all chunks now.
            for chunk in chunk_message(content, self.max_length):
                await self.channel.send(chunk)

    async def _flush(self) -> None:
        """Edit or create the message with the current buffer contents."""
        content = "".join(self._buffer)
        if not content:
            return

        # Truncate to max_length for the in-progress edit (finalize handles overflow).
        display = content[: self.max_length]

        try:
            if self._message is None:
                self._message = await self.channel.send(display)
            else:
                await self._message.edit(content=display)
        except Exception:
            log.debug("streaming_message.flush_failed")
        self._last_edit = time.monotonic()


# ---------------------------------------------------------------------------
# TurnstoneBot
# ---------------------------------------------------------------------------


class TurnstoneBot:
    """Discord bot that bridges Discord threads to turnstone workstreams.

    Parameters
    ----------
    config:
        Discord-specific configuration.
    server_url:
        Base URL of the turnstone server API (e.g. ``http://localhost:8080/v1``).
    storage:
        Storage backend for persistent route / user lookups.
    api_token:
        Optional bearer token for authenticating with the server API.
    """

    channel_type: str = "discord"
    _MAX_NOTIFY_TRACKING: int = 100

    def __init__(
        self,
        config: DiscordConfig,
        server_url: str,
        storage: StorageBackend,
        *,
        api_token: str = "",
        console_url: str = "",
    ) -> None:
        import discord
        from discord.ext import commands

        self.config = config
        self._server_url = server_url.rstrip("/")
        self._console_url = console_url.rstrip("/") if console_url else ""
        self._api_token = api_token
        self.storage = storage
        self.router = ChannelRouter(
            server_url,
            storage,
            auto_approve=config.auto_approve,
            auto_approve_tools=list(config.auto_approve_tools),
            skill=config.skill,
            api_token=api_token,
            console_url=console_url,
        )

        self._subscribed_ws: set[str] = set()
        self._sse_tasks: dict[str, asyncio.Task[None]] = {}
        self._streaming: dict[str, StreamingMessage] = {}
        # Track the Discord message containing the pending approval embed per
        # workstream so that IntentVerdictEvent can update it with LLM judge
        # results.
        self._pending_approval_msgs: dict[str, discord.Message] = {}
        # Notification reply tracking: maps Discord message ID ->
        # (ws_id, target_discord_user_id) so that DM replies can be routed
        # back to the originating workstream.  The target user ID is checked
        # on reply to prevent cross-user message injection.
        self._notify_ws_map: dict[int, tuple[str, str]] = {}
        # Temporary DM forwarding: maps ws_id -> (DM channel, target_user_id)
        # for forwarding the workstream's next response back to the
        # notification reply DM.  The target_user_id is carried so the
        # response message can be re-tracked for multi-turn DM conversations.
        self._notify_reply_channels: dict[str, tuple[discord.abc.Messageable, str]] = {}

        # Shared HTTP client for SSE connections (long-lived, no timeout).
        headers: dict[str, str] = {}
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        self._http_client = httpx.AsyncClient(headers=headers, timeout=None)

        intents = discord.Intents.default()
        intents.message_content = True

        self._bot: commands.Bot = commands.Bot(
            command_prefix="!ts ",
            intents=intents,
            help_command=None,
        )

        # Attach ourselves so cogs can access the TurnstoneBot instance.
        self._bot.turnstone = self  # type: ignore[attr-defined]

        # Register lifecycle hooks.
        self._bot.setup_hook = self._setup_hook  # type: ignore[method-assign]

        @self._bot.event
        async def on_ready() -> None:
            await self._on_ready()

    # -- lifecycle -----------------------------------------------------------

    async def _setup_hook(self) -> None:
        """Called by discord.py after login but before connecting to the gateway."""
        from turnstone.channels.discord.cog import MessageCog
        from turnstone.channels.discord.views import ApprovalView, PlanReviewView

        msg_cog = MessageCog(self._bot)
        await self._bot.add_cog(msg_cog._cog)

        # Register persistent views so button callbacks survive restarts.
        self._bot.add_view(ApprovalView(self)._view)
        self._bot.add_view(PlanReviewView(self)._view)

        log.info("discord.setup_hook_complete")

    async def _on_ready(self) -> None:
        """Sync slash commands and recover existing routes."""
        import discord

        bot = self._bot
        log.info("discord.ready", user=str(bot.user), guild_count=len(bot.guilds))

        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            log.info("discord.commands_synced", guild_id=self.config.guild_id)
        else:
            await bot.tree.sync()
            log.info("discord.commands_synced_global")

        await self._recover_routes()

    async def _recover_routes(self) -> None:
        """Re-subscribe to event channels for existing discord routes.

        Queries the storage backend for all channel routes of type ``discord``
        and opens SSE connections for each workstream.
        """
        routes = await asyncio.to_thread(self.storage.list_channel_routes_by_type, "discord")
        for route in routes:
            ws_id = route["ws_id"]
            channel_id = int(route["channel_id"])
            channel = self._bot.get_channel(channel_id)
            if channel is not None:
                await self.subscribe_ws(ws_id, channel)  # type: ignore[arg-type]
                log.info("discord.route_recovered", ws_id=ws_id, channel_id=channel_id)
            else:
                log.warning(
                    "discord.route_recovery_channel_missing",
                    ws_id=ws_id,
                    channel_id=channel_id,
                )

    # -- subscription management ---------------------------------------------

    async def subscribe_ws(
        self,
        ws_id: str,
        thread: discord.abc.Messageable,
    ) -> None:
        """Subscribe to workstream events via SSE and dispatch them to *thread*."""
        if ws_id in self._subscribed_ws:
            return

        task = asyncio.create_task(
            self._sse_listener(ws_id, thread),
            name=f"sse:{ws_id}",
        )
        self._sse_tasks[ws_id] = task
        self._subscribed_ws.add(ws_id)
        log.info("discord.subscribed", ws_id=ws_id)

    async def unsubscribe_ws(self, ws_id: str) -> None:
        """Cancel the SSE listener for *ws_id* and clean up streaming state."""
        task = self._sse_tasks.pop(ws_id, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._subscribed_ws.discard(ws_id)
        self._streaming.pop(ws_id, None)
        self._pending_approval_msgs.pop(ws_id, None)
        self._notify_reply_channels.pop(ws_id, None)
        # Purge stale notification tracking entries for this workstream.
        stale = [mid for mid, entry in self._notify_ws_map.items() if entry[0] == ws_id]
        for mid in stale:
            del self._notify_ws_map[mid]
        log.info("discord.unsubscribed", ws_id=ws_id)

    # -- SSE listener --------------------------------------------------------

    async def _sse_listener(self, ws_id: str, thread: discord.abc.Messageable) -> None:
        """SSE listener task for a workstream.

        Connects to the server's per-workstream SSE endpoint, parses events
        using :meth:`ServerEvent.from_dict`, and dispatches to
        :meth:`_on_ws_event`.  Reconnects with exponential backoff on
        connection failures.
        """
        import httpx_sse

        # When routing through the console, connect SSE directly to the
        # assigned server node (node_url from the create response).
        node_base = await self.router.get_node_url(ws_id)
        url = f"{node_base}/api/events"
        delay = _SSE_RECONNECT_DELAY

        while True:
            try:
                async with httpx_sse.aconnect_sse(
                    self._http_client, "GET", url, params={"ws_id": ws_id}
                ) as event_source:
                    delay = _SSE_RECONNECT_DELAY  # reset on successful connect
                    async for sse in event_source.aiter_sse():
                        if sse.event == "message" or not sse.event:
                            try:
                                data = json.loads(sse.data)
                            except json.JSONDecodeError:
                                log.debug(
                                    "discord.sse_invalid_json",
                                    ws_id=ws_id,
                                    data=sse.data[:200],
                                )
                                continue
                            event = ServerEvent.from_dict(data)
                            await self._on_ws_event(ws_id, thread, event)
                            if isinstance(event, StreamEndEvent):
                                return  # clean end, do not reconnect
            except httpx.RemoteProtocolError:
                # Server closed connection (normal on stream_end or shutdown).
                log.debug("discord.sse_remote_closed", ws_id=ws_id)
            except asyncio.CancelledError:
                return  # unsubscribe or shutdown
            except Exception:
                log.warning("discord.sse_error", ws_id=ws_id, exc_info=True)

            # Exponential backoff before reconnecting.
            await asyncio.sleep(delay)
            delay = min(delay * 2, _SSE_MAX_RECONNECT_DELAY)

    # -- event dispatch ------------------------------------------------------

    async def _on_ws_event(
        self,
        ws_id: str,
        thread: discord.abc.Messageable,
        event: ServerEvent,
    ) -> None:
        """Handle a typed server event for a subscribed workstream."""
        import discord

        from turnstone.channels._formatter import (
            format_approval_request,
            format_plan_review,
            format_verdict,
        )
        from turnstone.channels.discord.views import ApprovalView, PlanReviewView

        if isinstance(event, ContentEvent):
            sm = self._streaming.get(ws_id)
            if sm is None:
                sm = StreamingMessage(
                    channel=thread,
                    max_length=self.config.max_message_length,
                    edit_interval=self.config.streaming_edit_interval,
                )
                self._streaming[ws_id] = sm
            await sm.append(event.text)

        elif isinstance(event, ApproveRequestEvent):
            # Evaluate admin tool policies before auto-approve.
            _policy_handled = False
            if self.storage is not None:
                try:
                    from turnstone.core.policy import evaluate_tool_policies_batch

                    _tool_names = [
                        it.get("approval_label", "") or it.get("func_name", "")
                        for it in event.items
                        if it.get("needs_approval") and it.get("func_name") and not it.get("error")
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
                            await thread.send(
                                f"*Tool blocked by admin policy: {', '.join(denied)}*"
                            )
                            _policy_handled = True
                        elif all(verdicts.get(n) == "allow" for n in _tool_names):
                            await self.router.send_approval(
                                ws_id,
                                "",
                                approved=True,
                            )
                            await thread.send("*Tool approved by policy.*")
                            _policy_handled = True
                except Exception:
                    log.debug("Tool policy evaluation failed for ws %s", ws_id, exc_info=True)
            if not _policy_handled and (
                self.config.auto_approve or self._should_auto_approve(event)
            ):
                # correlation_id is empty because the server's /api/approve
                # endpoint resolves approvals by ws_id alone (one pending
                # approval per workstream at a time).
                await self.router.send_approval(ws_id, "", approved=True)
                await thread.send("*Tool auto-approved.*")
            elif not _policy_handled:
                text = format_approval_request(event.items)
                embed = discord.Embed(
                    title="Tool Approval Required",
                    description=text,
                    color=discord.Color.orange(),
                )
                # Include heuristic verdicts from approval items.
                for item in event.items:
                    verdict = item.get("verdict")
                    if verdict:
                        name = item.get("func_name") or item.get("approval_label") or "tool"
                        embed.add_field(
                            name=f"Verdict: {name}",
                            value=format_verdict(verdict),
                            inline=False,
                        )
                embed.set_footer(text=f"{ws_id}|")
                msg = await thread.send(embed=embed, view=ApprovalView(self)._view)
                self._pending_approval_msgs[ws_id] = msg

        elif isinstance(event, PlanReviewEvent):
            text = format_plan_review(event.content)
            embed = discord.Embed(
                title="Plan Review",
                description=text,
                color=discord.Color.blue(),
            )
            embed.set_footer(text=f"{ws_id}|")
            await thread.send(embed=embed, view=PlanReviewView(self)._view)

        elif isinstance(event, IntentVerdictEvent):
            # LLM judge verdict arrived — update the pending approval embed.
            approval_msg = self._pending_approval_msgs.get(ws_id)
            if approval_msg and approval_msg.embeds:
                embed = approval_msg.embeds[0]
                verdict_data = {
                    "risk_level": event.risk_level,
                    "recommendation": event.recommendation,
                    "confidence": event.confidence,
                    "intent_summary": event.intent_summary,
                    "tier": event.tier,
                }
                name = event.func_name or "tool"
                # Update embed color based on LLM judge risk level.
                risk = (event.risk_level or "medium").upper()
                color_map = {
                    "LOW": discord.Color.green(),
                    "MEDIUM": discord.Color.orange(),
                    "HIGH": discord.Color.red(),
                    "CRITICAL": discord.Color.dark_red(),
                }
                embed.color = color_map.get(risk, discord.Color.orange())
                embed.add_field(
                    name=f"Judge Verdict: {name}",
                    value=format_verdict(verdict_data),
                    inline=False,
                )
                try:
                    await approval_msg.edit(embed=embed)
                except Exception:
                    log.debug("discord.verdict_embed_edit_failed", ws_id=ws_id)

        elif isinstance(event, StreamEndEvent):
            sm = self._streaming.pop(ws_id, None)
            if sm is not None:
                await sm.finalize()
            # Forward accumulated response to notification reply DM if active.
            dm_entry = self._notify_reply_channels.pop(ws_id, None)
            if dm_entry is not None and sm is not None:
                content = "".join(sm._buffer)
                if content:
                    dm_channel, target_user_id = dm_entry
                    last_msg: discord.Message | None = None
                    for dm_chunk in chunk_message(content, self.config.max_message_length):
                        try:
                            last_msg = await dm_channel.send(dm_chunk)
                        except Exception:
                            log.debug("discord.notify_reply_dm_failed", ws_id=ws_id)
                            break
                    # Track the response message so the user can reply again
                    # for multi-turn DM conversations.
                    if last_msg is not None:
                        self._track_notification(last_msg.id, ws_id, target_user_id)
            # Clean up pending approval message tracking.
            self._pending_approval_msgs.pop(ws_id, None)

        elif isinstance(event, ErrorEvent):
            safe_msg = event.message[:500] if event.message else "An error occurred"
            await thread.send(f"**Error:** {safe_msg}")

    # -- helpers -------------------------------------------------------------

    def _should_auto_approve(self, event: ApproveRequestEvent) -> bool:
        """Return True if all tools in *event.items* are in the auto-approve list."""
        allowed = self.config.auto_approve_tools
        if not allowed or not event.items:
            return False
        for item in event.items:
            # Support both server SSE format (func_name) and OpenAI format (function.name).
            name = (
                item.get("func_name")
                or item.get("approval_label")
                or item.get("function", {}).get("name", "")
            )
            if name not in allowed:
                return False
        return True

    def _track_notification(self, message_id: int, ws_id: str, target_user_id: str) -> None:
        """Record a notification message for reply routing.

        Evicts the oldest entry when the map exceeds
        ``_MAX_NOTIFY_TRACKING``.  Relies on dict insertion order
        (Python 3.7+).
        """
        while len(self._notify_ws_map) >= self._MAX_NOTIFY_TRACKING:
            oldest = next(iter(self._notify_ws_map))
            del self._notify_ws_map[oldest]
        self._notify_ws_map[message_id] = (ws_id, target_user_id)

    def _is_allowed_channel(self, channel_id: int) -> bool:
        """Return True if *channel_id* is in the allowed list (or list is empty)."""
        if not self.config.allowed_channels:
            return True
        return channel_id in self.config.allowed_channels

    def run(self, **kwargs: object) -> None:
        """Start the bot (blocking). Pass-through to ``commands.Bot.run``."""
        self._bot.run(self.config.bot_token, log_handler=None, **kwargs)  # type: ignore[arg-type]

    async def start(self) -> None:
        """Start the bot (async). Use this for multi-adapter ``asyncio.gather``."""
        await self._bot.start(self.config.bot_token, reconnect=True)

    async def send(self, channel_id: str, content: str) -> str:
        """Send a message to a Discord channel or user DM.

        Implements the :class:`ChannelAdapter` protocol.  Tries the ID as a
        channel first; if not found, attempts a user DM.  Long messages are
        chunked via :func:`chunk_message`.
        """
        import discord

        int_id = int(channel_id)
        target: discord.abc.Messageable | None = self._bot.get_channel(int_id)  # type: ignore[assignment]
        if target is None:
            try:
                user = await self._bot.fetch_user(int_id)
                target = await user.create_dm()
            except discord.NotFound as exc:
                raise ValueError(f"Discord channel/user {channel_id} not found") from exc

        content = discord.utils.escape_mentions(content)
        chunks = chunk_message(content, self.config.max_message_length)
        msg: discord.Message | None = None
        for chunk in chunks:
            msg = await target.send(chunk)  # type: ignore[union-attr]

        return str(msg.id) if msg else ""

    async def send_notification(self, channel_id: str, content: str, ws_id: str) -> str:
        """Send a notification DM and track the message for reply routing.

        Like :meth:`send` but records a mapping from the outgoing Discord
        message ID to ``(ws_id, channel_id)`` so that a user reply can be
        routed back to the originating workstream.  The *channel_id* is the
        Discord user ID the notification was sent to — verified on reply to
        prevent cross-user message injection.
        """
        msg_id_str = await self.send(channel_id, content)
        if msg_id_str and ws_id:
            self._track_notification(int(msg_id_str), ws_id, channel_id)
            log.debug(
                "discord.notification_tracked",
                message_id=msg_id_str,
                ws_id=ws_id,
                target_user=channel_id,
            )
        return msg_id_str

    async def stop(self) -> None:
        """Disconnect the bot, cancel SSE tasks, and clean up."""
        for ws_id in list(self._subscribed_ws):
            await self.unsubscribe_ws(ws_id)
        await self.router.aclose()
        await self._http_client.aclose()
        await self._bot.close()
