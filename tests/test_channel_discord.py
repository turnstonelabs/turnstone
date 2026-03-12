"""Tests for the Discord channel adapter (bot, cog, views, config, CLI)."""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

discord = pytest.importorskip("discord")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine in a fresh event loop (no pytest-asyncio needed)."""
    return asyncio.run(coro)


def _make_message(*, bot=False, guild=True, content="hello", channel=None):
    """Build a mock ``discord.Message``."""
    msg = MagicMock(spec=discord.Message)
    msg.author = MagicMock()
    msg.author.bot = bot
    msg.author.id = 12345
    msg.content = content
    msg.guild = MagicMock() if guild else None
    msg.channel = channel or MagicMock()
    msg.mentions = []
    return msg


def _make_interaction(*, footer_text=None, has_embeds=True):
    """Build a mock ``discord.Interaction``."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = 67890
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()

    if has_embeds and footer_text is not None:
        embed = MagicMock()
        embed.footer.text = footer_text
        interaction.message = MagicMock()
        interaction.message.embeds = [embed]
    elif not has_embeds:
        interaction.message = MagicMock()
        interaction.message.embeds = []
    else:
        interaction.message = None

    return interaction


# ---------------------------------------------------------------------------
# DiscordConfig
# ---------------------------------------------------------------------------


class TestDiscordConfig:
    """Tests for DiscordConfig default and custom values."""

    def test_defaults(self):
        from turnstone.channels.discord.config import DiscordConfig

        cfg = DiscordConfig()
        assert cfg.bot_token == ""
        assert cfg.guild_id == 0
        assert cfg.allowed_channels == []
        assert cfg.thread_auto_archive == 1440
        assert cfg.max_message_length == 2000
        assert cfg.streaming_edit_interval == 1.5
        # Inherited from ChannelConfig
        assert cfg.redis_host == "localhost"
        assert cfg.redis_port == 6379
        assert cfg.model == ""
        assert cfg.auto_approve is False

    def test_custom_values(self):
        from turnstone.channels.discord.config import DiscordConfig

        cfg = DiscordConfig(
            bot_token="tok_123",
            guild_id=999,
            allowed_channels=[1, 2, 3],
            thread_auto_archive=60,
            max_message_length=4000,
            streaming_edit_interval=0.5,
            model="gpt-5",
            auto_approve=True,
        )
        assert cfg.bot_token == "tok_123"
        assert cfg.guild_id == 999
        assert cfg.allowed_channels == [1, 2, 3]
        assert cfg.thread_auto_archive == 60
        assert cfg.max_message_length == 4000
        assert cfg.streaming_edit_interval == 0.5
        assert cfg.model == "gpt-5"
        assert cfg.auto_approve is True


# ---------------------------------------------------------------------------
# StreamingMessage
# ---------------------------------------------------------------------------


class TestStreamingMessage:
    """Tests for the StreamingMessage helper in bot.py."""

    def test_append_accumulates(self):
        from turnstone.channels.discord.bot import StreamingMessage

        channel = MagicMock()
        channel.send = AsyncMock()
        sm = StreamingMessage(channel=channel, edit_interval=999.0)

        _run(sm.append("hello "))
        _run(sm.append("world"))

        assert "".join(sm._buffer) == "hello world"

    def test_finalize_sends_when_no_prior_message(self):
        from turnstone.channels.discord.bot import StreamingMessage

        channel = MagicMock()
        channel.send = AsyncMock()
        sm = StreamingMessage(channel=channel, edit_interval=999.0)

        _run(sm.append("hello"))
        _run(sm.finalize())

        channel.send.assert_awaited_once_with("hello")

    def test_finalize_edits_existing_message(self):
        from turnstone.channels.discord.bot import StreamingMessage

        channel = MagicMock()
        sent_msg = MagicMock()
        sent_msg.edit = AsyncMock()
        channel.send = AsyncMock(return_value=sent_msg)
        sm = StreamingMessage(channel=channel, edit_interval=0.0)

        # First append triggers flush (interval=0) which creates the message.
        _run(sm.append("hi"))
        assert sm._message is sent_msg

        _run(sm.append(" there"))
        _run(sm.finalize())

        # finalize edits the existing message with full content.
        sent_msg.edit.assert_awaited_with(content="hi there")

    def test_finalize_chunks_long_content(self):
        from turnstone.channels.discord.bot import StreamingMessage

        channel = MagicMock()
        channel.send = AsyncMock()
        sm = StreamingMessage(channel=channel, max_length=10, edit_interval=999.0)

        # Content longer than max_length should be chunked on finalize.
        _run(sm.append("a" * 25))
        _run(sm.finalize())

        # Should have sent multiple chunks via channel.send.
        assert channel.send.await_count >= 2

    def test_finalize_empty_is_noop(self):
        from turnstone.channels.discord.bot import StreamingMessage

        channel = MagicMock()
        channel.send = AsyncMock()
        sm = StreamingMessage(channel=channel)

        _run(sm.finalize())
        channel.send.assert_not_awaited()


# ---------------------------------------------------------------------------
# MessageCog._on_message
# ---------------------------------------------------------------------------


class TestMessageCog:
    """Tests for the MessageCog on_message filtering logic."""

    def _make_cog(self):
        """Build a MessageCog with a fully mocked bot and TurnstoneBot."""
        from turnstone.channels.discord.cog import MessageCog

        bot = MagicMock()
        bot.user = MagicMock()
        bot.user.id = 99999
        bot.user.mentioned_in = MagicMock(return_value=False)

        ts = MagicMock()
        ts._is_allowed_channel = MagicMock(return_value=True)
        ts.storage = MagicMock()
        ts.router = MagicMock()
        ts.router.resolve_user = AsyncMock(return_value="u_abc")
        ts.router.send_message = AsyncMock()
        ts.config = MagicMock()
        ts._ws_tasks = {}
        bot.turnstone = ts

        cog = MessageCog(bot)
        return cog, ts, bot

    def test_ignores_bot_messages(self):
        cog, ts, _bot = self._make_cog()
        msg = _make_message(bot=True)

        _run(cog._on_message(msg))

        # No router interaction means the message was ignored.
        ts.router.send_message.assert_not_awaited()

    def test_ignores_own_messages(self):
        cog, ts, bot = self._make_cog()
        msg = _make_message(bot=False)
        msg.author = bot.user  # message from ourselves

        _run(cog._on_message(msg))

        ts.router.send_message.assert_not_awaited()

    def test_ignores_dms(self):
        cog, ts, _bot = self._make_cog()
        msg = _make_message(guild=False)

        _run(cog._on_message(msg))

        ts.router.send_message.assert_not_awaited()

    def test_ignores_non_allowed_channels(self):
        cog, ts, _bot = self._make_cog()
        ts._is_allowed_channel = MagicMock(return_value=False)

        thread = MagicMock(spec=discord.Thread)
        thread.id = 111
        thread.parent_id = 222
        msg = _make_message(channel=thread)

        _run(cog._on_message(msg))

        ts.router.send_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# _parse_footer (views.py)
# ---------------------------------------------------------------------------


class TestParseFooter:
    """Tests for _parse_footer in views.py."""

    def test_valid_footer(self):
        from turnstone.channels.discord.views import _parse_footer

        interaction = _make_interaction(footer_text="ws_abc|corr_123")
        result = _parse_footer(interaction)
        assert result == ("ws_abc", "corr_123")

    def test_footer_with_pipe_in_correlation(self):
        from turnstone.channels.discord.views import _parse_footer

        interaction = _make_interaction(footer_text="ws_abc|corr|extra")
        result = _parse_footer(interaction)
        # split("|", 1) means the second part includes everything after first pipe.
        assert result == ("ws_abc", "corr|extra")

    def test_no_message_returns_none(self):
        from turnstone.channels.discord.views import _parse_footer

        interaction = MagicMock()
        interaction.message = None
        assert _parse_footer(interaction) is None

    def test_no_embeds_returns_none(self):
        from turnstone.channels.discord.views import _parse_footer

        interaction = _make_interaction(has_embeds=False)
        assert _parse_footer(interaction) is None

    def test_empty_footer_returns_none(self):
        from turnstone.channels.discord.views import _parse_footer

        # Build an interaction whose embed has footer.text = None.
        interaction = MagicMock(spec=discord.Interaction)
        embed = MagicMock()
        embed.footer.text = None
        interaction.message = MagicMock()
        interaction.message.embeds = [embed]
        assert _parse_footer(interaction) is None

    def test_footer_without_pipe_returns_none(self):
        from turnstone.channels.discord.views import _parse_footer

        interaction = _make_interaction(footer_text="no_pipe_here")
        # footer text has no "|" separator
        embed = MagicMock()
        embed.footer.text = "no_pipe_here"
        interaction.message.embeds = [embed]
        assert _parse_footer(interaction) is None


# ---------------------------------------------------------------------------
# CLI main() — no adapter configured
# ---------------------------------------------------------------------------


class TestWsEventFinalization:
    """TurnCompleteEvent should finalize streaming messages in the Discord bot."""

    def test_turn_complete_finalizes_streaming(self):
        """ContentEvent + TurnCompleteEvent(correlation_id='') finalizes the message."""
        from turnstone.channels.discord.bot import TurnstoneBot
        from turnstone.mq.protocol import ContentEvent, TurnCompleteEvent

        bot = MagicMock(spec=TurnstoneBot)
        bot.config = MagicMock()
        bot.config.max_message_length = 2000
        bot.config.streaming_edit_interval = 1.5
        bot.config.auto_approve = False
        bot.config.auto_approve_tools = []
        bot._streaming = {}

        # Use the real _on_ws_event method
        bot._on_ws_event = TurnstoneBot._on_ws_event.__get__(bot, TurnstoneBot)

        thread = AsyncMock()

        # Feed content event
        content_raw = ContentEvent(ws_id="ws-1", text="Hello world").to_json()
        _run(bot._on_ws_event("ws-1", thread, content_raw))

        # StreamingMessage should exist
        assert "ws-1" in bot._streaming

        # Feed turn complete with empty correlation_id (server-UI-initiated)
        complete_raw = TurnCompleteEvent(ws_id="ws-1", correlation_id="").to_json()
        _run(bot._on_ws_event("ws-1", thread, complete_raw))

        # StreamingMessage should be removed and finalized
        assert "ws-1" not in bot._streaming

    def test_turn_complete_no_streaming_is_noop(self):
        """TurnCompleteEvent without prior content should not error."""
        from turnstone.channels.discord.bot import TurnstoneBot
        from turnstone.mq.protocol import TurnCompleteEvent

        bot = MagicMock(spec=TurnstoneBot)
        bot._streaming = {}
        bot._on_ws_event = TurnstoneBot._on_ws_event.__get__(bot, TurnstoneBot)

        thread = AsyncMock()

        complete_raw = TurnCompleteEvent(ws_id="ws-1", correlation_id="").to_json()
        _run(bot._on_ws_event("ws-1", thread, complete_raw))

        # No error, no streaming message
        assert "ws-1" not in bot._streaming


class TestChannelCLI:
    """Tests for the channel CLI entry point."""

    def test_exits_without_adapter_token(self):
        from turnstone.channels.cli import main

        with (
            patch.object(sys, "argv", ["turnstone-channel"]),
            patch.dict("os.environ", {}, clear=True),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
