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


def _make_message(*, bot=False, guild=True, content="hello", channel=None, reference=None):
    """Build a mock ``discord.Message``."""
    msg = MagicMock(spec=discord.Message)
    msg.author = MagicMock()
    msg.author.bot = bot
    msg.author.id = 12345
    msg.content = content
    msg.guild = MagicMock() if guild else None
    msg.channel = channel or MagicMock()
    msg.mentions = []
    msg.reference = reference
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
        assert cfg.server_url == "http://localhost:8080"
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
        ts._notify_ws_map = {}
        ts._notify_reply_channels = {}
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
    """StreamEndEvent should finalize streaming messages in the Discord bot."""

    def test_stream_end_finalizes_streaming(self):
        """ContentEvent + StreamEndEvent finalizes the message."""
        from turnstone.channels.discord.bot import TurnstoneBot
        from turnstone.sdk.events import ContentEvent, StreamEndEvent

        bot = MagicMock(spec=TurnstoneBot)
        bot.config = MagicMock()
        bot.config.max_message_length = 2000
        bot.config.streaming_edit_interval = 1.5
        bot.config.auto_approve = False
        bot.config.auto_approve_tools = []
        bot._streaming = {}
        bot._thinking_msgs = {}
        bot._tool_info_msgs = {}
        bot._pending_approval_msgs = {}
        bot._notify_reply_channels = {}

        # Use the real _on_ws_event method
        bot._on_ws_event = TurnstoneBot._on_ws_event.__get__(bot, TurnstoneBot)

        thread = AsyncMock()

        # Feed content event
        content_event = ContentEvent(ws_id="ws-1", text="Hello world")
        _run(bot._on_ws_event("ws-1", thread, content_event))

        # StreamingMessage should exist
        assert "ws-1" in bot._streaming

        # Feed stream end
        end_event = StreamEndEvent(ws_id="ws-1")
        _run(bot._on_ws_event("ws-1", thread, end_event))

        # StreamingMessage should be removed and finalized
        assert "ws-1" not in bot._streaming

    def test_stream_end_no_streaming_is_noop(self):
        """StreamEndEvent without prior content should not error."""
        from turnstone.channels.discord.bot import TurnstoneBot
        from turnstone.sdk.events import StreamEndEvent

        bot = MagicMock(spec=TurnstoneBot)
        bot._streaming = {}
        bot._thinking_msgs = {}
        bot._tool_info_msgs = {}
        bot._pending_approval_msgs = {}
        bot._notify_reply_channels = {}
        bot._on_ws_event = TurnstoneBot._on_ws_event.__get__(bot, TurnstoneBot)

        thread = AsyncMock()

        end_event = StreamEndEvent(ws_id="ws-1")
        _run(bot._on_ws_event("ws-1", thread, end_event))

        # No error, no streaming message
        assert "ws-1" not in bot._streaming


# ---------------------------------------------------------------------------
# Verdict display in approval embeds
# ---------------------------------------------------------------------------


class TestApprovalVerdictDisplay:
    """Approval requests should include verdict fields in the Discord embed."""

    def _make_bot(self):
        """Build a mock TurnstoneBot with _on_ws_event bound."""
        from turnstone.channels.discord.bot import TurnstoneBot

        bot = MagicMock(spec=TurnstoneBot)
        bot.config = MagicMock()
        bot.config.max_message_length = 2000
        bot.config.streaming_edit_interval = 1.5
        bot.config.auto_approve = False
        bot.config.auto_approve_tools = []
        bot.storage = None
        bot._streaming = {}
        bot._thinking_msgs = {}
        bot._tool_info_msgs = {}
        bot._pending_approval_msgs = {}
        bot._notify_reply_channels = {}
        bot._should_auto_approve = MagicMock(return_value=False)
        bot._on_ws_event = TurnstoneBot._on_ws_event.__get__(bot, TurnstoneBot)
        return bot

    def test_approval_with_heuristic_verdict(self):
        """ApproveRequestEvent items with verdict dicts add embed fields."""
        from turnstone.sdk.events import ApproveRequestEvent

        bot = self._make_bot()
        thread = AsyncMock()
        sent_msg = MagicMock()
        thread.send = AsyncMock(return_value=sent_msg)

        items = [
            {
                "func_name": "bash",
                "preview": "rm -rf /tmp",
                "needs_approval": True,
                "verdict": {
                    "risk_level": "high",
                    "recommendation": "deny",
                    "confidence": 0.85,
                    "intent_summary": "Deleting temp files",
                    "tier": "heuristic",
                },
            }
        ]
        event = ApproveRequestEvent(ws_id="ws-1", items=items)
        _run(bot._on_ws_event("ws-1", thread, event))

        # thread.send was called with an embed containing a verdict field
        thread.send.assert_awaited_once()
        call_kwargs = thread.send.call_args[1]
        embed = call_kwargs["embed"]
        # discord.Embed.fields is a list of EmbedProxy objects
        assert len(embed.fields) == 1
        field = embed.fields[0]
        assert field.name == "Verdict: bash"
        assert "HIGH" in field.value
        assert "85%" in field.value

        # Pending approval message tracked
        assert "ws-1" in bot._pending_approval_msgs

    def test_approval_without_verdict(self):
        """ApproveRequestEvent items without verdict still work normally."""
        from turnstone.sdk.events import ApproveRequestEvent

        bot = self._make_bot()
        thread = AsyncMock()
        sent_msg = MagicMock()
        thread.send = AsyncMock(return_value=sent_msg)

        items = [{"func_name": "read_file", "preview": "/etc/hosts", "needs_approval": True}]
        event = ApproveRequestEvent(ws_id="ws-1", items=items)
        _run(bot._on_ws_event("ws-1", thread, event))

        thread.send.assert_awaited_once()
        call_kwargs = thread.send.call_args[1]
        embed = call_kwargs["embed"]
        # No verdict field added
        assert len(embed.fields) == 0

    def test_intent_verdict_event_updates_embed(self):
        """IntentVerdictEvent should update the pending approval embed."""
        from turnstone.sdk.events import IntentVerdictEvent

        bot = self._make_bot()
        thread = AsyncMock()

        # Set up a pending approval message with a mock embed
        msg = MagicMock()
        embed = MagicMock()
        msg.embeds = [embed]
        msg.edit = AsyncMock()
        bot._pending_approval_msgs["ws-1"] = msg

        event = IntentVerdictEvent(
            ws_id="ws-1",
            func_name="bash",
            risk_level="high",
            recommendation="deny",
            confidence=0.9,
            intent_summary="Dangerous operation",
            tier="llm",
        )
        _run(bot._on_ws_event("ws-1", thread, event))

        # Embed should be updated with the judge verdict field
        embed.add_field.assert_called_once()
        field_kwargs = embed.add_field.call_args[1]
        assert field_kwargs["name"] == "Judge Verdict: bash"
        assert "HIGH" in field_kwargs["value"]
        assert "90%" in field_kwargs["value"]

        # Message should be edited
        msg.edit.assert_awaited_once()

    def test_intent_verdict_without_pending_approval_is_noop(self):
        """IntentVerdictEvent without a pending approval message should not error."""
        from turnstone.sdk.events import IntentVerdictEvent

        bot = self._make_bot()
        thread = AsyncMock()

        event = IntentVerdictEvent(ws_id="ws-1", func_name="bash", risk_level="low")
        # Should not raise
        _run(bot._on_ws_event("ws-1", thread, event))

    def test_stream_end_clears_pending_approval(self):
        """StreamEndEvent should clean up the pending approval message tracking."""
        from turnstone.channels.discord.bot import TurnstoneBot
        from turnstone.sdk.events import StreamEndEvent

        bot = MagicMock(spec=TurnstoneBot)
        bot._streaming = {}
        bot._thinking_msgs = {}
        bot._tool_info_msgs = {}
        bot._pending_approval_msgs = {"ws-1": MagicMock()}
        bot._notify_reply_channels = {}
        bot._on_ws_event = TurnstoneBot._on_ws_event.__get__(bot, TurnstoneBot)

        thread = AsyncMock()
        event = StreamEndEvent(ws_id="ws-1")
        _run(bot._on_ws_event("ws-1", thread, event))

        assert "ws-1" not in bot._pending_approval_msgs


class TestStreamEndBehavior:
    """StreamEndEvent finalizes streaming and cleans up state."""

    def _make_bot(self):
        from turnstone.channels.discord.bot import TurnstoneBot

        bot = MagicMock(spec=TurnstoneBot)
        bot.config = MagicMock()
        bot.config.max_message_length = 2000
        bot.config.streaming_edit_interval = 1.5
        bot.config.auto_approve = False
        bot.config.auto_approve_tools = []
        bot._streaming = {}
        bot._thinking_msgs = {}
        bot._tool_info_msgs = {}
        bot._pending_approval_msgs = {}
        bot._notify_reply_channels = {}
        bot._on_ws_event = TurnstoneBot._on_ws_event.__get__(bot, TurnstoneBot)
        return bot

    def test_stream_end_no_streaming_no_send(self):
        """StreamEndEvent without prior content should not send anything."""
        from turnstone.sdk.events import StreamEndEvent

        bot = self._make_bot()
        thread = AsyncMock()

        event = StreamEndEvent(ws_id="ws-1")
        _run(bot._on_ws_event("ws-1", thread, event))

        thread.send.assert_not_awaited()

    def test_stream_end_finalizes_existing_streaming(self):
        """StreamEndEvent with an existing StreamingMessage should finalize it."""
        from turnstone.sdk.events import ContentEvent, StreamEndEvent

        bot = self._make_bot()
        thread = AsyncMock()

        # Feed content event to create SM
        content_event = ContentEvent(ws_id="ws-1", text="Streamed")
        _run(bot._on_ws_event("ws-1", thread, content_event))
        assert "ws-1" in bot._streaming

        # Now StreamEndEvent — SM should be finalized
        end_event = StreamEndEvent(ws_id="ws-1")
        _run(bot._on_ws_event("ws-1", thread, end_event))
        assert "ws-1" not in bot._streaming


class TestNotificationTracking:
    """Tests for notification message tracking and DM reply routing."""

    def test_send_notification_tracks_message(self):
        """send_notification should store message_id -> (ws_id, target_user) mapping."""
        from turnstone.channels.discord.bot import TurnstoneBot

        bot = MagicMock(spec=TurnstoneBot)
        bot._notify_ws_map = {}
        bot._MAX_NOTIFY_TRACKING = 100
        bot.send = AsyncMock(return_value="12345")
        bot.send_notification = TurnstoneBot.send_notification.__get__(bot, TurnstoneBot)
        bot._track_notification = TurnstoneBot._track_notification.__get__(bot, TurnstoneBot)

        _run(bot.send_notification("chan-1", "Hello", "ws-abc"))

        assert 12345 in bot._notify_ws_map
        assert bot._notify_ws_map[12345] == ("ws-abc", "chan-1")

    def test_send_notification_evicts_old_entries(self):
        """Oldest notification tracking entries are evicted when cap is reached."""
        from turnstone.channels.discord.bot import TurnstoneBot

        bot = MagicMock(spec=TurnstoneBot)
        bot._MAX_NOTIFY_TRACKING = 3
        bot._notify_ws_map = {
            1: ("ws-1", "u1"),
            2: ("ws-2", "u2"),
            3: ("ws-3", "u3"),
        }
        bot.send = AsyncMock(return_value="4")
        bot.send_notification = TurnstoneBot.send_notification.__get__(bot, TurnstoneBot)
        bot._track_notification = TurnstoneBot._track_notification.__get__(bot, TurnstoneBot)

        _run(bot.send_notification("chan-1", "Hello", "ws-4"))

        assert 4 in bot._notify_ws_map
        assert 1 not in bot._notify_ws_map  # oldest evicted
        assert len(bot._notify_ws_map) <= 3

    def test_dm_reply_routes_to_workstream(self):
        """DM reply to a tracked notification routes the message to the workstream."""
        from turnstone.channels.discord.cog import MessageCog

        bot = MagicMock()
        bot.user = MagicMock()
        bot.user.id = 99999

        ts = MagicMock()
        ts._is_allowed_channel = MagicMock(return_value=True)
        ts.storage = MagicMock()
        ts.router = MagicMock()
        ts.router.resolve_user = AsyncMock(return_value="u_abc")
        ts.router.send_message = AsyncMock()
        ts.config = MagicMock()
        # Maps message_id -> (ws_id, target_discord_user_id)
        ts._notify_ws_map = {77777: ("ws-target", "12345")}
        ts._notify_reply_channels = {}
        bot.turnstone = ts

        cog = MessageCog(bot)

        # Build a DM reply to the tracked notification message
        ref = MagicMock()
        ref.message_id = 77777
        msg = _make_message(guild=False, content="additional context", reference=ref)
        # msg.author.id defaults to 12345 from _make_message

        _run(cog._on_message(msg))

        ts.router.send_message.assert_awaited_once_with("ws-target", "additional context")
        assert "ws-target" in ts._notify_reply_channels
        dm_chan, target_uid = ts._notify_reply_channels["ws-target"]
        assert target_uid == "12345"
        assert 77777 not in ts._notify_ws_map  # cleaned up

    def test_dm_reply_user_mismatch_rejected_and_preserved(self):
        """DM reply from wrong user is rejected; entry re-inserted for legitimate user."""
        from turnstone.channels.discord.cog import MessageCog

        bot = MagicMock()
        bot.user = MagicMock()
        bot.user.id = 99999

        ts = MagicMock()
        ts.router = MagicMock()
        ts.router.resolve_user = AsyncMock(return_value="u_abc")
        ts.router.send_message = AsyncMock()
        # Target user is "99999" but replying user has author.id = 12345
        ts._notify_ws_map = {77777: ("ws-target", "99999")}
        ts._notify_reply_channels = {}
        bot.turnstone = ts

        cog = MessageCog(bot)
        ref = MagicMock()
        ref.message_id = 77777
        msg = _make_message(guild=False, content="impostor", reference=ref)

        _run(cog._on_message(msg))

        ts.router.send_message.assert_not_awaited()
        # Entry should be re-inserted so the legitimate user can still reply.
        assert 77777 in ts._notify_ws_map
        assert ts._notify_ws_map[77777] == ("ws-target", "99999")

    def test_dm_reply_stale_notification_feedback(self):
        """DM reply to an expired/unknown notification should inform the user."""
        from turnstone.channels.discord.cog import MessageCog

        bot = MagicMock()
        bot.user = MagicMock()
        bot.user.id = 99999

        ts = MagicMock()
        ts.router = MagicMock()
        ts.router.send_message = AsyncMock()
        ts._notify_ws_map = {}  # empty — no tracked notifications
        ts._notify_reply_channels = {}
        bot.turnstone = ts

        cog = MessageCog(bot)

        ref = MagicMock()
        ref.message_id = 99999  # not in map
        dm_channel = AsyncMock()
        msg = _make_message(guild=False, content="reply", reference=ref, channel=dm_channel)

        _run(cog._on_message(msg))

        # Should NOT route to any workstream
        ts.router.send_message.assert_not_awaited()
        # Should send feedback to the DM channel
        dm_channel.send.assert_awaited_once_with("*This notification is no longer active.*")

    def test_dm_without_reference_ignored(self):
        """DM without a message reference should be ignored."""
        from turnstone.channels.discord.cog import MessageCog

        bot = MagicMock()
        bot.user = MagicMock()
        bot.user.id = 99999

        ts = MagicMock()
        ts.router = MagicMock()
        ts.router.send_message = AsyncMock()
        ts._notify_ws_map = {77777: ("ws-target", "12345")}
        ts._notify_reply_channels = {}
        bot.turnstone = ts

        cog = MessageCog(bot)
        msg = _make_message(guild=False)  # reference=None

        _run(cog._on_message(msg))

        ts.router.send_message.assert_not_awaited()

    def test_dm_reply_unlinked_user_ignored(self):
        """DM reply from an unlinked user should be ignored."""
        from turnstone.channels.discord.cog import MessageCog

        bot = MagicMock()
        bot.user = MagicMock()
        bot.user.id = 99999

        ts = MagicMock()
        ts.router = MagicMock()
        ts.router.resolve_user = AsyncMock(return_value=None)
        ts.router.send_message = AsyncMock()
        ts._notify_ws_map = {77777: ("ws-target", "12345")}
        ts._notify_reply_channels = {}
        bot.turnstone = ts

        cog = MessageCog(bot)

        ref = MagicMock()
        ref.message_id = 77777
        msg = _make_message(guild=False, content="reply", reference=ref)

        _run(cog._on_message(msg))

        ts.router.send_message.assert_not_awaited()

    def test_stream_end_forwards_accumulated_content_to_dm(self):
        """StreamEndEvent should forward accumulated content to notification reply DM."""
        from turnstone.channels.discord.bot import TurnstoneBot
        from turnstone.sdk.events import ContentEvent, StreamEndEvent

        bot = MagicMock(spec=TurnstoneBot)
        bot.config = MagicMock()
        bot.config.max_message_length = 2000
        bot.config.streaming_edit_interval = 1.5
        bot._streaming = {}
        bot._thinking_msgs = {}
        bot._tool_info_msgs = {}
        bot._pending_approval_msgs = {}
        bot._notify_ws_map = {}
        bot._MAX_NOTIFY_TRACKING = 100

        dm_channel = AsyncMock()
        sent_msg = MagicMock()
        sent_msg.id = 88888
        dm_channel.send = AsyncMock(return_value=sent_msg)
        bot._notify_reply_channels = {"ws-1": (dm_channel, "u123")}
        bot._on_ws_event = TurnstoneBot._on_ws_event.__get__(bot, TurnstoneBot)
        bot._track_notification = TurnstoneBot._track_notification.__get__(bot, TurnstoneBot)

        thread = AsyncMock()

        # Feed content events to accumulate buffer
        content_event = ContentEvent(ws_id="ws-1", text="Here's the response")
        _run(bot._on_ws_event("ws-1", thread, content_event))

        # Feed stream end — should finalize and forward to DM
        end_event = StreamEndEvent(ws_id="ws-1")
        _run(bot._on_ws_event("ws-1", thread, end_event))

        # Should send to DM channel
        dm_channel.send.assert_awaited_once_with("Here's the response")
        # Should clean up forwarding
        assert "ws-1" not in bot._notify_reply_channels
        # Response message should be tracked for multi-turn replies
        assert 88888 in bot._notify_ws_map
        assert bot._notify_ws_map[88888] == ("ws-1", "u123")

    def test_stream_end_cleans_up_dm_even_without_content(self):
        """StreamEndEvent without prior content should still clean up DM tracking."""
        from turnstone.channels.discord.bot import TurnstoneBot
        from turnstone.sdk.events import StreamEndEvent

        bot = MagicMock(spec=TurnstoneBot)
        bot._streaming = {}
        bot._thinking_msgs = {}
        bot._tool_info_msgs = {}
        bot._pending_approval_msgs = {}
        bot._notify_ws_map = {}

        dm_channel = AsyncMock()
        bot._notify_reply_channels = {"ws-1": (dm_channel, "u123")}
        bot._on_ws_event = TurnstoneBot._on_ws_event.__get__(bot, TurnstoneBot)

        thread = AsyncMock()

        end_event = StreamEndEvent(ws_id="ws-1")
        _run(bot._on_ws_event("ws-1", thread, end_event))

        # DM should not be sent to (no content)
        dm_channel.send.assert_not_awaited()
        # But should still be cleaned up
        assert "ws-1" not in bot._notify_reply_channels
        # No response tracked (nothing was sent)
        assert len(bot._notify_ws_map) == 0


# ---------------------------------------------------------------------------
# Formatter: format_tool_result
# ---------------------------------------------------------------------------


class TestFormatToolResult:
    """Tests for format_tool_result in _formatter.py."""

    def test_basic_output(self):
        from turnstone.channels._formatter import format_tool_result

        result = format_tool_result("bash", "hello world")
        assert "**bash**" in result
        assert "```" in result
        assert "hello world" in result

    def test_error_prefix(self):
        from turnstone.channels._formatter import format_tool_result

        result = format_tool_result("bash", "command not found", is_error=True)
        assert "**ERROR**" in result

    def test_truncates_long_output_by_lines(self):
        from turnstone.channels._formatter import format_tool_result

        output = "\n".join(f"line {i}" for i in range(20))
        result = format_tool_result("bash", output)
        # Should have at most 10 content lines + ellipsis
        inner = result.split("```")[1]
        assert inner.strip().count("\n") <= 11

    def test_truncates_long_output_by_chars(self):
        from turnstone.channels._formatter import format_tool_result

        output = "x" * 600
        result = format_tool_result("bash", output)
        # Code block content should be <= 500 chars (497 + ellipsis)
        inner = result.split("```")[1].strip()
        assert len(inner) <= 501  # 497 + ellipsis char

    def test_escapes_triple_backticks_in_output(self):
        from turnstone.channels._formatter import format_tool_result

        output = "before ``` after"
        result = format_tool_result("bash", output)
        # Only the opening and closing code fences should remain as ```.
        assert result.count("```") == 2


# ---------------------------------------------------------------------------
# Thinking indicator lifecycle
# ---------------------------------------------------------------------------


class TestThinkingIndicator:
    """Tests for ThinkingStart/Stop event handling in the Discord bot."""

    def _make_bot(self):
        from turnstone.channels.discord.bot import TurnstoneBot

        bot = MagicMock(spec=TurnstoneBot)
        bot.config = MagicMock()
        bot.config.max_message_length = 2000
        bot.config.streaming_edit_interval = 1.5
        bot.config.auto_approve = False
        bot.config.auto_approve_tools = []
        bot.storage = None
        bot._streaming = {}
        bot._thinking_msgs = {}
        bot._tool_info_msgs = {}
        bot._pending_approval_msgs = {}
        bot._notify_reply_channels = {}
        bot._should_auto_approve = MagicMock(return_value=False)
        bot._on_ws_event = TurnstoneBot._on_ws_event.__get__(bot, TurnstoneBot)
        return bot

    def test_thinking_start_sends_message(self):
        from turnstone.sdk.events import ThinkingStartEvent

        bot = self._make_bot()
        thread = AsyncMock()
        sent_msg = MagicMock()
        thread.send = AsyncMock(return_value=sent_msg)

        event = ThinkingStartEvent(ws_id="ws-1")
        _run(bot._on_ws_event("ws-1", thread, event))

        thread.send.assert_awaited_once_with("*Thinking...*")
        assert bot._thinking_msgs["ws-1"] is sent_msg

    def test_thinking_stop_deletes_message(self):
        from turnstone.sdk.events import ThinkingStopEvent

        bot = self._make_bot()
        thread = AsyncMock()
        thinking_msg = MagicMock()
        thinking_msg.delete = AsyncMock()
        bot._thinking_msgs["ws-1"] = thinking_msg

        event = ThinkingStopEvent(ws_id="ws-1")
        _run(bot._on_ws_event("ws-1", thread, event))

        thinking_msg.delete.assert_awaited_once()
        assert "ws-1" not in bot._thinking_msgs

    def test_thinking_stop_without_message_is_noop(self):
        from turnstone.sdk.events import ThinkingStopEvent

        bot = self._make_bot()
        thread = AsyncMock()

        event = ThinkingStopEvent(ws_id="ws-1")
        _run(bot._on_ws_event("ws-1", thread, event))
        # No error, no state change

    def test_content_event_clears_thinking_message(self):
        from turnstone.sdk.events import ContentEvent

        bot = self._make_bot()
        thread = AsyncMock()
        thinking_msg = MagicMock()
        thinking_msg.delete = AsyncMock()
        bot._thinking_msgs["ws-1"] = thinking_msg

        event = ContentEvent(ws_id="ws-1", text="Hello")
        _run(bot._on_ws_event("ws-1", thread, event))

        thinking_msg.delete.assert_awaited_once()
        assert "ws-1" not in bot._thinking_msgs

    def test_stream_end_clears_thinking_message(self):
        from turnstone.sdk.events import StreamEndEvent

        bot = self._make_bot()
        thread = AsyncMock()
        thinking_msg = MagicMock()
        thinking_msg.delete = AsyncMock()
        bot._thinking_msgs["ws-1"] = thinking_msg
        bot._notify_reply_channels = {}

        event = StreamEndEvent(ws_id="ws-1")
        _run(bot._on_ws_event("ws-1", thread, event))

        thinking_msg.delete.assert_awaited_once()
        assert "ws-1" not in bot._thinking_msgs


# ---------------------------------------------------------------------------
# Tool info / result embeds
# ---------------------------------------------------------------------------


class TestToolInfoEvent:
    """Tests for ToolInfoEvent handling in the Discord bot."""

    def _make_bot(self):
        from turnstone.channels.discord.bot import TurnstoneBot

        bot = MagicMock(spec=TurnstoneBot)
        bot.config = MagicMock()
        bot.config.max_message_length = 2000
        bot.config.streaming_edit_interval = 1.5
        bot.config.auto_approve = False
        bot.config.auto_approve_tools = []
        bot.storage = None
        bot._streaming = {}
        bot._thinking_msgs = {}
        bot._tool_info_msgs = {}
        bot._pending_approval_msgs = {}
        bot._notify_reply_channels = {}
        bot._should_auto_approve = MagicMock(return_value=False)
        bot._on_ws_event = TurnstoneBot._on_ws_event.__get__(bot, TurnstoneBot)
        return bot

    def test_sends_per_item_embed(self):
        from turnstone.sdk.events import ToolInfoEvent

        bot = self._make_bot()
        thread = AsyncMock()
        sent_msg = MagicMock()
        thread.send = AsyncMock(return_value=sent_msg)

        items = [{"func_name": "bash", "preview": "ls -la", "needs_approval": False}]
        event = ToolInfoEvent(ws_id="ws-1", items=items)
        _run(bot._on_ws_event("ws-1", thread, event))

        thread.send.assert_awaited_once()
        embed = thread.send.call_args[1]["embed"]
        assert embed.title == "bash"
        assert embed.description == "ls -la"
        # Message tracked for later editing by ToolResultEvent.
        assert bot._tool_info_msgs["ws-1"] == [("bash", sent_msg)]

    def test_multiple_tools_send_multiple_embeds(self):
        from turnstone.sdk.events import ToolInfoEvent

        bot = self._make_bot()
        thread = AsyncMock()

        items = [
            {"func_name": "bash", "preview": "ls", "needs_approval": False},
            {"func_name": "read_file", "preview": "/etc", "needs_approval": False},
        ]
        event = ToolInfoEvent(ws_id="ws-1", items=items)
        _run(bot._on_ws_event("ws-1", thread, event))

        assert thread.send.await_count == 2
        assert len(bot._tool_info_msgs["ws-1"]) == 2

    def test_needs_approval_without_auto_approve_sends_nothing(self):
        from turnstone.sdk.events import ToolInfoEvent

        bot = self._make_bot()
        thread = AsyncMock()

        items = [{"func_name": "bash", "preview": "rm -rf /", "needs_approval": True}]
        event = ToolInfoEvent(ws_id="ws-1", items=items)
        _run(bot._on_ws_event("ws-1", thread, event))

        thread.send.assert_not_awaited()

    def test_needs_approval_with_auto_approve_sends_embed(self):
        from turnstone.sdk.events import ToolInfoEvent

        bot = self._make_bot()
        bot.config.auto_approve = True
        thread = AsyncMock()

        items = [{"func_name": "bash", "preview": "rm -rf /", "needs_approval": True}]
        event = ToolInfoEvent(ws_id="ws-1", items=items)
        _run(bot._on_ws_event("ws-1", thread, event))

        thread.send.assert_awaited_once()
        assert thread.send.call_args[1]["embed"].title == "bash"

    def test_needs_approval_with_auto_approve_tools_filters(self):
        from turnstone.sdk.events import ToolInfoEvent

        bot = self._make_bot()
        bot.config.auto_approve_tools = ["bash"]
        thread = AsyncMock()

        items = [
            {"func_name": "bash", "preview": "ls", "needs_approval": True},
            {"func_name": "write_file", "preview": "/etc/passwd", "needs_approval": True},
        ]
        event = ToolInfoEvent(ws_id="ws-1", items=items)
        _run(bot._on_ws_event("ws-1", thread, event))

        # Only bash is auto-approved, so only one embed sent.
        thread.send.assert_awaited_once()
        assert thread.send.call_args[1]["embed"].title == "bash"


class TestToolResultEvent:
    """Tests for ToolResultEvent handling in the Discord bot."""

    def _make_bot(self):
        from turnstone.channels.discord.bot import TurnstoneBot

        bot = MagicMock(spec=TurnstoneBot)
        bot.config = MagicMock()
        bot.config.max_message_length = 2000
        bot.config.streaming_edit_interval = 1.5
        bot.config.auto_approve = False
        bot.config.auto_approve_tools = []
        bot.storage = None
        bot._streaming = {}
        bot._thinking_msgs = {}
        bot._tool_info_msgs = {}
        bot._pending_approval_msgs = {}
        bot._notify_reply_channels = {}
        bot._should_auto_approve = MagicMock(return_value=False)
        bot._on_ws_event = TurnstoneBot._on_ws_event.__get__(bot, TurnstoneBot)
        return bot

    def test_edits_matching_info_message(self):
        from turnstone.sdk.events import ToolResultEvent

        bot = self._make_bot()
        thread = AsyncMock()

        # Pre-populate a tool info message (as ToolInfoEvent would).
        info_msg = MagicMock()
        info_msg.edit = AsyncMock()
        bot._tool_info_msgs["ws-1"] = [("bash", info_msg)]

        event = ToolResultEvent(ws_id="ws-1", name="bash", output="file1\nfile2")
        _run(bot._on_ws_event("ws-1", thread, event))

        # Should edit existing message, not send a new one.
        info_msg.edit.assert_awaited_once()
        embed = info_msg.edit.call_args[1]["embed"]
        assert embed.title == "bash"
        assert "file1" in embed.description
        thread.send.assert_not_awaited()
        # Entry consumed from tracking list.
        assert bot._tool_info_msgs["ws-1"] == []

    def test_sends_new_embed_when_no_match(self):
        from turnstone.sdk.events import ToolResultEvent

        bot = self._make_bot()
        thread = AsyncMock()

        event = ToolResultEvent(ws_id="ws-1", name="bash", output="file1\nfile2")
        _run(bot._on_ws_event("ws-1", thread, event))

        thread.send.assert_awaited_once()
        embed = thread.send.call_args[1]["embed"]
        assert embed.title == "bash"
        assert "file1" in embed.description

    def test_error_result_uses_red_color(self):
        from turnstone.sdk.events import ToolResultEvent

        bot = self._make_bot()
        thread = AsyncMock()

        event = ToolResultEvent(
            ws_id="ws-1", name="bash", output="command not found", is_error=True
        )
        _run(bot._on_ws_event("ws-1", thread, event))

        embed = thread.send.call_args[1]["embed"]
        assert embed.color == discord.Color.red()
        assert "**ERROR**" in embed.description

    def test_success_result_uses_dark_grey_color(self):
        from turnstone.sdk.events import ToolResultEvent

        bot = self._make_bot()
        thread = AsyncMock()

        event = ToolResultEvent(ws_id="ws-1", name="bash", output="ok")
        _run(bot._on_ws_event("ws-1", thread, event))

        embed = thread.send.call_args[1]["embed"]
        assert embed.color == discord.Color.dark_grey()

    def test_fifo_matching_same_name_tools(self):
        from turnstone.sdk.events import ToolResultEvent

        bot = self._make_bot()
        thread = AsyncMock()

        first_msg = MagicMock()
        first_msg.edit = AsyncMock()
        second_msg = MagicMock()
        second_msg.edit = AsyncMock()
        bot._tool_info_msgs["ws-1"] = [("bash", first_msg), ("bash", second_msg)]

        # First result matches first info message.
        event1 = ToolResultEvent(ws_id="ws-1", name="bash", output="result1")
        _run(bot._on_ws_event("ws-1", thread, event1))
        first_msg.edit.assert_awaited_once()
        second_msg.edit.assert_not_awaited()

        # Second result matches second info message.
        event2 = ToolResultEvent(ws_id="ws-1", name="bash", output="result2")
        _run(bot._on_ws_event("ws-1", thread, event2))
        second_msg.edit.assert_awaited_once()

    def test_edit_failure_falls_back_to_send(self):
        from turnstone.sdk.events import ToolResultEvent

        bot = self._make_bot()
        thread = AsyncMock()

        info_msg = MagicMock()
        info_msg.edit = AsyncMock(side_effect=Exception("Discord API error"))
        bot._tool_info_msgs["ws-1"] = [("bash", info_msg)]

        event = ToolResultEvent(ws_id="ws-1", name="bash", output="ok")
        _run(bot._on_ws_event("ws-1", thread, event))

        # Edit failed, should fall back to send.
        info_msg.edit.assert_awaited_once()
        thread.send.assert_awaited_once()


# ---------------------------------------------------------------------------
# Approval resolved (timeout / external resolution)
# ---------------------------------------------------------------------------


class TestApprovalResolved:
    """ApprovalResolvedEvent should disable buttons on the pending approval embed."""

    def _make_bot(self):
        from turnstone.channels.discord.bot import TurnstoneBot

        bot = MagicMock(spec=TurnstoneBot)
        bot.config = MagicMock()
        bot.config.max_message_length = 2000
        bot.config.streaming_edit_interval = 1.5
        bot.config.auto_approve = False
        bot.config.auto_approve_tools = []
        bot.storage = None
        bot._streaming = {}
        bot._thinking_msgs = {}
        bot._tool_info_msgs = {}
        bot._pending_approval_msgs = {}
        bot._notify_reply_channels = {}
        bot._should_auto_approve = MagicMock(return_value=False)
        bot._on_ws_event = TurnstoneBot._on_ws_event.__get__(bot, TurnstoneBot)
        return bot

    def test_disables_buttons_on_timeout(self):
        from turnstone.sdk.events import ApprovalResolvedEvent

        bot = self._make_bot()
        thread = AsyncMock()

        # Set up a pending approval message with components.
        approval_msg = MagicMock()
        approval_msg.embeds = [MagicMock()]
        approval_msg.components = []
        approval_msg.edit = AsyncMock()
        bot._pending_approval_msgs["ws-1"] = approval_msg

        event = ApprovalResolvedEvent(ws_id="ws-1", approved=False, feedback="timeout")
        _run(bot._on_ws_event("ws-1", thread, event))

        approval_msg.edit.assert_awaited_once()
        # Pending approval message should be removed.
        assert "ws-1" not in bot._pending_approval_msgs

    def test_disables_buttons_on_approved(self):
        from turnstone.sdk.events import ApprovalResolvedEvent

        bot = self._make_bot()
        thread = AsyncMock()

        approval_msg = MagicMock()
        approval_msg.embeds = [MagicMock()]
        approval_msg.components = []
        approval_msg.edit = AsyncMock()
        bot._pending_approval_msgs["ws-1"] = approval_msg

        event = ApprovalResolvedEvent(ws_id="ws-1", approved=True)
        _run(bot._on_ws_event("ws-1", thread, event))

        approval_msg.edit.assert_awaited_once()
        # Check the embed title was updated with "Approved".
        edited_embed = approval_msg.edit.call_args[1]["embed"]
        assert "Approved" in edited_embed.title

    def test_no_pending_approval_is_noop(self):
        from turnstone.sdk.events import ApprovalResolvedEvent

        bot = self._make_bot()
        thread = AsyncMock()

        event = ApprovalResolvedEvent(ws_id="ws-1", approved=False)
        _run(bot._on_ws_event("ws-1", thread, event))
        # No error, no state change.


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
