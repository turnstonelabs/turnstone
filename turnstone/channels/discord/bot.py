"""Discord bot adapter — connects Discord threads to turnstone workstreams.

:class:`TurnstoneBot` extends ``discord.ext.commands.Bot`` and manages the
lifecycle of event subscriptions, streaming message edits, and interactive
approval / plan-review views.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from turnstone.channels._formatter import chunk_message
from turnstone.channels._routing import ChannelRouter
from turnstone.core.log import get_logger
from turnstone.mq.protocol import (
    ApprovalRequestEvent,
    ContentEvent,
    ErrorEvent,
    IntentVerdictEvent,
    OutboundEvent,
    PlanReviewEvent,
    TurnCompleteEvent,
    WorkstreamResumedEvent,
)

if TYPE_CHECKING:
    import discord
    from discord.ext import commands

    from turnstone.channels.discord.config import DiscordConfig
    from turnstone.core.storage._protocol import StorageBackend
    from turnstone.mq.async_broker import AsyncRedisBroker

log = get_logger(__name__)


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
    broker:
        Async Redis broker for MQ communication.
    storage:
        Storage backend for persistent route / user lookups.
    """

    channel_type: str = "discord"
    _MAX_NOTIFY_TRACKING: int = 100

    def __init__(
        self,
        config: DiscordConfig,
        broker: AsyncRedisBroker,
        storage: StorageBackend,
    ) -> None:
        import discord
        from discord.ext import commands

        self.config = config
        self.broker = broker
        self.storage = storage
        self.router = ChannelRouter(
            broker,
            storage,
            auto_approve=config.auto_approve,
            auto_approve_tools=list(config.auto_approve_tools),
            template=config.template,
        )

        self._subscribed_ws: set[str] = set()
        self._streaming: dict[str, StreamingMessage] = {}
        # Track the Discord message containing the pending approval embed per
        # workstream so that IntentVerdictEvent can update it with LLM judge
        # results.
        self._pending_approval_msgs: dict[str, discord.Message] = {}
        # Notification reply tracking: maps Discord message ID →
        # (ws_id, target_discord_user_id) so that DM replies can be routed
        # back to the originating workstream.  The target user ID is checked
        # on reply to prevent cross-user message injection.
        self._notify_ws_map: dict[int, tuple[str, str]] = {}
        # Temporary DM forwarding: maps ws_id → (DM channel, target_user_id)
        # for forwarding the workstream's next response back to the
        # notification reply DM.  The target_user_id is carried so the
        # response message can be re-tracked for multi-turn DM conversations.
        self._notify_reply_channels: dict[str, tuple[discord.abc.Messageable, str]] = {}

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

        await self.broker.connect()
        await self.router.start()

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
        and subscribes to each workstream's event channel.
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
        """Subscribe to workstream events and dispatch them to *thread*."""
        if ws_id in self._subscribed_ws:
            return

        channel = f"{self.broker._prefix}:events:{ws_id}"

        async def _callback(raw: str) -> None:
            await self._on_ws_event(ws_id, thread, raw)

        await self.broker.subscribe(channel, _callback)
        self._subscribed_ws.add(ws_id)
        log.info("discord.subscribed", ws_id=ws_id)

    async def unsubscribe_ws(self, ws_id: str) -> None:
        """Cancel the subscription for *ws_id* and clean up streaming state."""
        channel = f"{self.broker._prefix}:events:{ws_id}"
        await self.broker.unsubscribe(channel)
        self._subscribed_ws.discard(ws_id)
        self._streaming.pop(ws_id, None)
        self._pending_approval_msgs.pop(ws_id, None)
        self._notify_reply_channels.pop(ws_id, None)
        # Purge stale notification tracking entries for this workstream.
        stale = [mid for mid, entry in self._notify_ws_map.items() if entry[0] == ws_id]
        for mid in stale:
            del self._notify_ws_map[mid]
        log.info("discord.unsubscribed", ws_id=ws_id)

    # -- event dispatch ------------------------------------------------------

    async def _on_ws_event(
        self,
        ws_id: str,
        thread: discord.abc.Messageable,
        raw: str,
    ) -> None:
        """Handle an outbound event for a subscribed workstream."""
        import discord

        from turnstone.channels._formatter import (
            format_approval_request,
            format_plan_review,
            format_verdict,
        )
        from turnstone.channels.discord.views import ApprovalView, PlanReviewView

        event = OutboundEvent.from_json(raw)

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

        elif isinstance(event, ApprovalRequestEvent):
            if self.config.auto_approve or self._should_auto_approve(event):
                await self.router.send_approval(ws_id, event.correlation_id, approved=True)
                await thread.send("*Tool auto-approved.*")
            else:
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
                embed.set_footer(text=f"{ws_id}|{event.correlation_id}")
                msg = await thread.send(embed=embed, view=ApprovalView(self)._view)
                self._pending_approval_msgs[ws_id] = msg

        elif isinstance(event, PlanReviewEvent):
            text = format_plan_review(event.content)
            embed = discord.Embed(
                title="Plan Review",
                description=text,
                color=discord.Color.blue(),
            )
            embed.set_footer(text=f"{ws_id}|{event.correlation_id}")
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

        elif isinstance(event, TurnCompleteEvent):
            sm = self._streaming.pop(ws_id, None)
            if sm is not None:
                await sm.finalize()
            elif event.content:
                # Catch-up: content events were missed (race between global
                # SSE and per-ws SSE) — send the full response directly.
                for chunk in chunk_message(event.content, self.config.max_message_length):
                    await thread.send(chunk)
            # Forward response to notification reply DM if active.
            dm_entry = self._notify_reply_channels.pop(ws_id, None)
            if dm_entry is not None and event.content:
                dm_channel, target_user_id = dm_entry
                last_msg: discord.Message | None = None
                for chunk in chunk_message(event.content, self.config.max_message_length):
                    try:
                        last_msg = await dm_channel.send(chunk)
                    except Exception:
                        log.debug("discord.notify_reply_dm_failed", ws_id=ws_id)
                        break
                # Track the response message so the user can reply again
                # for multi-turn DM conversations.
                if last_msg is not None:
                    self._track_notification(last_msg.id, ws_id, target_user_id)
            # Clean up pending approval message tracking.
            self._pending_approval_msgs.pop(ws_id, None)

        elif isinstance(event, WorkstreamResumedEvent):
            name = event.name or "previous workstream"
            count = event.message_count
            await thread.send(f"*Resumed: {name} ({count} messages restored)*")

        elif isinstance(event, ErrorEvent):
            safe_msg = event.message[:500] if event.message else "An error occurred"
            await thread.send(f"**Error:** {safe_msg}")

    # -- helpers -------------------------------------------------------------

    def _should_auto_approve(self, event: ApprovalRequestEvent) -> bool:
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
        """Disconnect the bot and clean up subscriptions."""
        for ws_id in list(self._subscribed_ws):
            await self.unsubscribe_ws(ws_id)
        await self.router.stop()
        await self.broker.close()
        await self._bot.close()
