"""Message handling cog for the Discord channel adapter.

Handles ``on_message`` events and slash commands (``/link``, ``/unlink``,
``/ask``, ``/status``, ``/close``).
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    import discord
    from discord import app_commands
    from discord.ext import commands

    from turnstone.channels.discord.bot import TurnstoneBot

log = get_logger(__name__)

_THREAD_NAME_MAX = 100
_DM_REPLY_MAX_LENGTH = 4096  # Discord's own message limit
_LEGACY_THREAD_MESSAGE = (
    "I can't verify who started this older thread. Use `/ask` in the parent channel "
    "to start a new conversation."
)

# /link is the only flow that reads Turnstone API tokens out of user
# input; throttle aggressively so an attacker with throw-away Discord
# accounts can't online-enumerate valid tokens.
_LINK_RATE_WINDOW_S: float = 3600.0
_LINK_RATE_LIMIT: int = 5
_LINK_RATE_CAP: int = 2048


class MessageCog:
    """Cog that processes messages and registers slash commands.

    Accessed via ``bot.turnstone`` to reach the :class:`TurnstoneBot` wrapper.
    """

    def __init__(self, bot: commands.Bot) -> None:
        import discord
        from discord import app_commands
        from discord.ext import commands as _commands

        self.bot = bot
        self.ts: TurnstoneBot = bot.turnstone  # type: ignore[attr-defined]

        # Per-Discord-user sliding-window rate limit on /link, to block
        # online enumeration of Turnstone API tokens.
        self._link_buckets: OrderedDict[str, deque[float]] = OrderedDict()
        self._thread_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()

        # -- Cog wiring (manual since we can't use decorators with guarded imports) --

        # We build the cog dynamically so discord.py's import is fully deferred.
        cog_self = self

        class _Cog(_commands.Cog, name="Turnstone"):  # type: ignore[call-arg]
            """Turnstone Discord integration."""

            @_commands.Cog.listener()
            async def on_message(self_cog: _Cog, message: discord.Message) -> None:  # noqa: N805
                await cog_self._on_message(message)

            @app_commands.command(name="link", description="Link your Discord account to Turnstone")
            async def link(self_cog: _Cog, interaction: discord.Interaction) -> None:  # noqa: N805
                await interaction.response.send_modal(cog_self._link_modal_cls(cog_self))

            @app_commands.command(
                name="unlink", description="Unlink your Discord account from Turnstone"
            )
            async def unlink(self_cog: _Cog, interaction: discord.Interaction) -> None:  # noqa: N805
                await cog_self._cmd_unlink(interaction)

            @app_commands.command(name="ask", description="Start a new Turnstone workstream")
            @app_commands.describe(
                message="Your message to the assistant",
                model="Model alias (leave blank for default)",
            )
            async def ask(
                self_cog: _Cog,  # noqa: N805
                interaction: discord.Interaction,
                message: str,
                model: str = "",
            ) -> None:
                await cog_self._cmd_ask(interaction, message, model=model)

            @ask.autocomplete("model")
            async def _model_autocomplete(
                self_cog: _Cog,  # noqa: N805
                interaction: discord.Interaction,
                current: str,
            ) -> list[app_commands.Choice[str]]:
                return await cog_self._autocomplete_model(interaction, current)

            @app_commands.command(name="status", description="Show workstream status")
            async def status(self_cog: _Cog, interaction: discord.Interaction) -> None:  # noqa: N805
                await cog_self._cmd_status(interaction)

            @app_commands.command(name="close", description="Close the current workstream")
            async def close(self_cog: _Cog, interaction: discord.Interaction) -> None:  # noqa: N805
                await cog_self._cmd_close(interaction)

        self._cog = _Cog()

        # -- Modal for /link (avoids token appearing in slash command audit logs) --
        class _LinkTokenModal(discord.ui.Modal, title="Link Account"):  # type: ignore[call-arg]
            token: discord.ui.TextInput[_LinkTokenModal] = discord.ui.TextInput(
                label="API Token",
                style=discord.TextStyle.short,
                placeholder="Paste your ts_... API token",
                required=True,
            )

            def __init__(modal_self, cog: MessageCog) -> None:  # noqa: N805
                super().__init__()
                modal_self._cog = cog

            async def on_submit(modal_self, interaction: discord.Interaction) -> None:  # noqa: N805
                await modal_self._cog._cmd_link(interaction, str(modal_self.token))

        self._link_modal_cls = _LinkTokenModal

    # -- on_message ----------------------------------------------------------

    async def _on_message(self, message: discord.Message) -> None:
        """Route incoming messages to existing workstream threads."""
        import discord

        # Ignore self and other bots.
        if message.author == self.bot.user or message.author.bot:
            return

        # DM handling — route replies to tracked notifications.
        if message.guild is None:
            await self._handle_dm(message)
            return

        channel = message.channel

        # --- Message in a Discord Thread ---
        if isinstance(channel, discord.Thread):
            lock = self._thread_lock(channel.id)
            async with lock:
                await self._handle_thread_message(message, channel)
            return

        # --- @mention in a non-thread channel ---
        if self.bot.user is not None and self.bot.user.mentioned_in(message):
            if not self.ts._is_allowed_channel(channel.id):
                return

            user_id = await self.ts.router.resolve_user("discord", str(message.author.id))
            if user_id is None:
                return

            # Strip the mention from the message text.
            content = message.content
            if self.bot.user is not None:
                content = content.replace(f"<@{self.bot.user.id}>", "").strip()
                content = content.replace(f"<@!{self.bot.user.id}>", "").strip()

            if not content:
                content = "Hello"

            # Create a thread from the message.
            thread_name = content[:_THREAD_NAME_MAX] if len(content) > _THREAD_NAME_MAX else content
            thread = await message.create_thread(
                name=thread_name,
                auto_archive_duration=self.ts.config.thread_auto_archive,  # type: ignore[arg-type]
            )
            # Create workstream WITHOUT initial_message — subscribe to events
            # first, then send the message.  With SSE the event stream is
            # reliable once connected, but we still subscribe first for
            # consistency.
            mention_model = await self.ts.router.get_channel_default_alias()
            if not mention_model:
                mention_model = self.ts.config.model
            ws_id, _is_new = await self.ts.router.get_or_create_workstream(
                channel_type="discord",
                channel_id=str(thread.id),
                name=thread_name,
                model=mention_model,
                initial_message="",
                client_type="chat",
                channel_user_id=str(message.author.id),
            )

            await self.ts.subscribe_ws(ws_id, thread)
            await self.ts.router.send_message(ws_id, content)
            log.info(
                "discord.workstream_created",
                ws_id=ws_id,
                thread_id=thread.id,
                author=str(message.author),
            )

    def _thread_lock(self, thread_id: int) -> asyncio.Lock:
        """Serialize local reply/close operations; idle locks are not retained."""
        lock = self._thread_locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._thread_locks[thread_id] = lock
        return lock

    async def _handle_thread_message(
        self, message: discord.Message, channel: discord.Thread
    ) -> None:
        parent_id = channel.parent_id or 0
        if not self.ts._is_allowed_channel(parent_id):
            return

        # Read the association and its durable owner together. A bot-owned
        # /ask thread cannot prove the human invoker through owner_id alone.
        route = await asyncio.to_thread(
            self.ts.storage.get_channel_route, "discord", str(channel.id)
        )
        if route is None:
            # Not our thread — ignore.
            return

        # Owner check: only the thread creator (who initiated the
        # workstream) can inject messages. Without this gate, any
        # linked user in a public / multi-member thread could
        # redirect someone else's assistant and bill their quota,
        # because the gateway forwards with its service-scoped JWT
        # and the server bypasses ownership on service scope.
        #
        effective_owner_id = route.get("channel_user_id", "")
        if not effective_owner_id:
            if await self.ts.router.resolve_user("discord", str(message.author.id)):
                await channel.send(_LEGACY_THREAD_MESSAGE)
            return
        if str(message.author.id) != effective_owner_id:
            log.debug(
                "discord.thread_message_rejected_non_owner",
                thread_id=channel.id,
                author_id=message.author.id,
                owner_id=effective_owner_id,
            )
            return

        # Resolve user.
        user_id = await self.ts.router.resolve_user("discord", str(message.author.id))
        if user_id is None:
            return

        # Use get_or_create_workstream so stale routes (evicted ws) are
        # auto-refreshed with a new workstream.
        try:
            ws_id, is_new = await self.ts.router.get_or_create_workstream(
                "discord",
                str(channel.id),
                name=channel.name or "",
                initial_message="",
                client_type="chat",
                channel_user_id=str(message.author.id),
                require_existing=True,
            )
        except Exception:
            log.warning("discord.ws_reactivation_failed", thread_id=channel.id, exc_info=True)
            await channel.send(
                "I couldn't recover this conversation. Please try your message again."
            )
            return

        if is_new:
            await channel.send("*Workstream reactivated.*")

        # Ensure subscription is active (handles bot restart recovery).
        if ws_id != route["ws_id"]:
            await self.ts.unsubscribe_ws(route["ws_id"])
        await self.ts.subscribe_ws(ws_id, channel)

        await self.ts.router.send_message(ws_id, message.content)
        log.debug(
            "discord.message_routed",
            ws_id=ws_id,
            thread_id=channel.id,
            author=str(message.author),
        )
        return

    # -- DM reply handling ---------------------------------------------------

    async def _handle_dm(self, message: discord.Message) -> None:
        """Route DM replies to tracked notification workstreams."""
        # Only handle explicit replies to a tracked notification message.
        ref = message.reference
        if ref is None or ref.message_id is None:
            await message.channel.send(
                "*Direct messages aren't supported. "
                "Use `/ask` in a server channel or @mention me to start a conversation.*"
            )
            return

        # Atomic pop prevents TOCTOU race across await points.
        entry = self.ts._notify_ws_map.pop(ref.message_id, None)
        if entry is None:
            # NOTE: This also fires for replies to non-notification bot
            # messages in DMs (false positive).  Acceptable because DM
            # interactions are almost exclusively notification-driven.
            await message.channel.send("*This notification is no longer active.*")
            return

        ws_id, target_user_id = entry

        # Defence in depth: verify the replying user is the notification
        # recipient.  Discord enforces this (DMs are private), but a
        # server-side check prevents cross-user injection via compromised
        # accounts or API-level forgery.
        if str(message.author.id) != target_user_id:
            # Re-insert so the legitimate user can still reply.
            self.ts._notify_ws_map[ref.message_id] = entry
            log.warning(
                "discord.notification_reply_user_mismatch",
                expected=target_user_id,
                actual=str(message.author.id),
            )
            return

        # Resolve user identity — unlinked users are silently ignored.
        # Re-insert the tracking entry so the user can retry after linking.
        user_id = await self.ts.router.resolve_user("discord", str(message.author.id))
        if user_id is None:
            self.ts._notify_ws_map[ref.message_id] = entry
            return

        # Route the reply to the originating workstream.
        content = message.content[:_DM_REPLY_MAX_LENGTH]
        await self.ts.router.send_message(ws_id, content)

        # Register the DM channel for response forwarding.  The bot's
        # _on_ws_event handler will send the next turn's response here,
        # track the response for further replies, and clean up on
        # StreamEndEvent.
        self.ts._notify_reply_channels[ws_id] = (message.channel, target_user_id)

        log.info(
            "discord.notification_reply_routed",
            ws_id=ws_id,
            author=str(message.author),
        )

    # -- slash commands ------------------------------------------------------

    def _allow_link_attempt(self, discord_user_id: str) -> bool:
        """Return True when this Discord user is under the /link rate limit.

        Sliding window: up to ``_LINK_RATE_LIMIT`` attempts per
        ``_LINK_RATE_WINDOW_S`` seconds.  Each attempt — success or
        failure — consumes a slot.  The bucket map is LRU-bounded.
        """
        now = time.monotonic()
        window_start = now - _LINK_RATE_WINDOW_S
        bucket = self._link_buckets.get(discord_user_id)
        if bucket is None:
            bucket = deque()
            self._link_buckets[discord_user_id] = bucket
            while len(self._link_buckets) > _LINK_RATE_CAP:
                self._link_buckets.popitem(last=False)
        else:
            self._link_buckets.move_to_end(discord_user_id)
        while bucket and bucket[0] < window_start:
            bucket.popleft()
        if len(bucket) >= _LINK_RATE_LIMIT:
            return False
        bucket.append(now)
        return True

    async def _cmd_link(self, interaction: discord.Interaction, token: str) -> None:
        """Link a Discord user to a turnstone account via API token."""
        from turnstone.core.auth import hash_token

        if not self._allow_link_attempt(str(interaction.user.id)):
            log.warning(
                "discord.link_rate_limited",
                discord_user=str(interaction.user),
            )
            await interaction.response.send_message(
                (
                    f"Too many /link attempts.  Try again later — limit is "
                    f"{_LINK_RATE_LIMIT} per hour."
                ),
                ephemeral=True,
            )
            return

        # Check if already linked.
        existing = await asyncio.to_thread(
            self.ts.storage.get_channel_user, "discord", str(interaction.user.id)
        )
        if existing:
            await interaction.response.send_message(
                "Your Discord account is already linked. Use `/unlink` first.",
                ephemeral=True,
            )
            return

        token_hash = hash_token(token)
        token_record = await asyncio.to_thread(self.ts.storage.get_api_token_by_hash, token_hash)

        if token_record is None:
            await interaction.response.send_message(
                "Invalid token. Please provide a valid Turnstone API token.",
                ephemeral=True,
            )
            return

        user_id = token_record.get("user_id", "")
        if not user_id:
            await interaction.response.send_message(
                "Token has no associated user.",
                ephemeral=True,
            )
            return

        await asyncio.to_thread(
            self.ts.storage.create_channel_user,
            "discord",
            str(interaction.user.id),
            user_id,
        )

        await interaction.response.send_message("Account linked!", ephemeral=True)
        log.info(
            "discord.user_linked",
            discord_user=str(interaction.user),
            user_id=user_id,
        )

    async def _cmd_unlink(self, interaction: discord.Interaction) -> None:
        """Remove the channel user mapping for the calling Discord user."""
        deleted = await asyncio.to_thread(
            self.ts.storage.delete_channel_user,
            "discord",
            str(interaction.user.id),
        )
        if deleted:
            await interaction.response.send_message("Account unlinked.", ephemeral=True)
            log.info("discord.user_unlinked", discord_user=str(interaction.user))
        else:
            await interaction.response.send_message(
                "No linked account found.",
                ephemeral=True,
            )

    async def _cmd_ask(
        self, interaction: discord.Interaction, message: str, *, model: str = ""
    ) -> None:
        """Create a new thread and workstream with an initial message."""
        import discord

        user_id = await self.ts.router.resolve_user("discord", str(interaction.user.id))
        if user_id is None:
            await interaction.response.send_message(
                "Your Discord account is not linked. Use `/link` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        channel = interaction.channel
        if channel is None:
            await interaction.followup.send("Cannot determine channel.", ephemeral=True)
            return
        if not self.ts._is_allowed_channel(channel.id):
            await interaction.followup.send(
                "This channel is not enabled for Turnstone.", ephemeral=True
            )
            return

        thread_name = message[:_THREAD_NAME_MAX] if len(message) > _THREAD_NAME_MAX else message

        # Create a standalone thread in the current channel.
        if isinstance(channel, discord.TextChannel):
            thread = await channel.create_thread(
                name=thread_name,
                auto_archive_duration=self.ts.config.thread_auto_archive,  # type: ignore[arg-type]
                type=discord.ChannelType.public_thread,
            )
        else:
            await interaction.followup.send(
                "Cannot create a thread in this channel type.",
                ephemeral=True,
            )
            return

        # Resolve model: explicit > channel default > CLI --model > server default.
        effective_model = model
        if not effective_model:
            effective_model = await self.ts.router.get_channel_default_alias()
        if not effective_model:
            effective_model = self.ts.config.model

        ws_id, _is_new = await self.ts.router.get_or_create_workstream(
            channel_type="discord",
            channel_id=str(thread.id),
            name=thread_name,
            model=effective_model,
            initial_message="",
            client_type="chat",
            channel_user_id=str(interaction.user.id),
        )

        await self.ts.subscribe_ws(ws_id, thread)
        await self.ts.router.send_message(ws_id, message)

        await interaction.followup.send(
            f"Workstream started in {thread.mention}",
            ephemeral=True,
        )
        log.info(
            "discord.ask_workstream_created",
            ws_id=ws_id,
            thread_id=thread.id,
            author=str(interaction.user),
        )

    async def _autocomplete_model(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Return model alias suggestions for the /ask autocomplete."""
        from discord import app_commands

        try:
            data = await self.ts.router.list_models(cached=True)
        except Exception:
            return []
        choices: list[app_commands.Choice[str]] = []
        for m in data.get("models", []):
            alias = m.get("alias", "")
            if not alias:
                continue
            if current and current.lower() not in alias.lower():
                continue
            choices.append(app_commands.Choice(name=alias, value=alias))
            if len(choices) >= 25:
                break
        return choices

    async def _cmd_status(self, interaction: discord.Interaction) -> None:
        """Show workstream status for the current thread."""
        import discord

        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            await interaction.response.send_message(
                "This command can only be used inside a thread.",
                ephemeral=True,
            )
            return

        route = await asyncio.to_thread(
            self.ts.storage.get_channel_route, "discord", str(channel.id)
        )
        if route is None:
            await interaction.response.send_message(
                "No workstream is associated with this thread.",
                ephemeral=True,
            )
            return

        ws_id = route["ws_id"]
        node_id = route.get("node_id", "")
        created = route.get("created", "")

        embed = discord.Embed(
            title="Workstream Status",
            color=discord.Color.green(),
        )
        embed.add_field(name="Workstream ID", value=ws_id, inline=False)
        if node_id:
            embed.add_field(name="Node", value=node_id, inline=True)
        if created:
            embed.add_field(name="Created", value=created, inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _cmd_close(self, interaction: discord.Interaction) -> None:
        """Close the workstream and archive the thread."""
        import discord

        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            await interaction.response.send_message(
                "This command can only be used inside a thread.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        lock = self._thread_lock(channel.id)
        async with lock:
            try:
                await self._close_thread(interaction, channel)
            except Exception:
                log.warning("discord.workstream_close_failed", thread_id=channel.id, exc_info=True)
                await interaction.followup.send(
                    "I couldn't close this conversation. Please try again.", ephemeral=True
                )

    async def _close_thread(
        self, interaction: discord.Interaction, channel: discord.Thread
    ) -> None:
        import discord

        route = await asyncio.to_thread(
            self.ts.storage.get_channel_route, "discord", str(channel.id)
        )
        if route is None:
            await interaction.followup.send(
                "No workstream is associated with this thread.",
                ephemeral=True,
            )
            return

        owner_id = route.get("channel_user_id", "")
        if not owner_id:
            await interaction.followup.send(_LEGACY_THREAD_MESSAGE, ephemeral=True)
            return
        if str(interaction.user.id) != owner_id or not await self.ts.router.resolve_user(
            "discord", str(interaction.user.id)
        ):
            await interaction.followup.send(
                "Only the linked conversation owner can close this workstream.", ephemeral=True
            )
            return

        ws_id = route["ws_id"]

        # Close via server API.
        await self.ts.router.close_workstream(ws_id)

        # Delete route and unsubscribe.
        if not await self.ts.router.delete_route("discord", str(channel.id), expected_ws_id=ws_id):
            await interaction.followup.send(
                "The conversation changed while closing. Please try `/close` again.", ephemeral=True
            )
            return
        await self.ts.unsubscribe_ws(ws_id)

        await interaction.followup.send("Workstream closed.")
        log.info("discord.workstream_closed", ws_id=ws_id, thread_id=channel.id)

        # Archive the thread.
        try:
            await channel.edit(archived=True)
        except discord.Forbidden:
            log.warning("discord.archive_forbidden", thread_id=channel.id)
