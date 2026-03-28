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
        bot._pending_approval_msgs = {}
        bot._notify_reply_channels = {}

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
        bot._pending_approval_msgs = {}
        bot._notify_reply_channels = {}
        bot._on_ws_event = TurnstoneBot._on_ws_event.__get__(bot, TurnstoneBot)

        thread = AsyncMock()

        complete_raw = TurnCompleteEvent(ws_id="ws-1", correlation_id="").to_json()
        _run(bot._on_ws_event("ws-1", thread, complete_raw))

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
        bot._pending_approval_msgs = {}
        bot._notify_reply_channels = {}
        bot._should_auto_approve = MagicMock(return_value=False)
        bot._on_ws_event = TurnstoneBot._on_ws_event.__get__(bot, TurnstoneBot)
        return bot

    def test_approval_with_heuristic_verdict(self):
        """ApprovalRequestEvent items with verdict dicts add embed fields."""
        from turnstone.mq.protocol import ApprovalRequestEvent

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
        raw = ApprovalRequestEvent(ws_id="ws-1", correlation_id="corr-1", items=items).to_json()
        _run(bot._on_ws_event("ws-1", thread, raw))

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
        """ApprovalRequestEvent items without verdict still work normally."""
        from turnstone.mq.protocol import ApprovalRequestEvent

        bot = self._make_bot()
        thread = AsyncMock()
        sent_msg = MagicMock()
        thread.send = AsyncMock(return_value=sent_msg)

        items = [{"func_name": "read_file", "preview": "/etc/hosts", "needs_approval": True}]
        raw = ApprovalRequestEvent(ws_id="ws-1", correlation_id="corr-1", items=items).to_json()
        _run(bot._on_ws_event("ws-1", thread, raw))

        thread.send.assert_awaited_once()
        call_kwargs = thread.send.call_args[1]
        embed = call_kwargs["embed"]
        # No verdict field added
        assert len(embed.fields) == 0

    def test_intent_verdict_event_updates_embed(self):
        """IntentVerdictEvent should update the pending approval embed."""
        from turnstone.mq.protocol import IntentVerdictEvent

        bot = self._make_bot()
        thread = AsyncMock()

        # Set up a pending approval message with a mock embed
        msg = MagicMock()
        embed = MagicMock()
        msg.embeds = [embed]
        msg.edit = AsyncMock()
        bot._pending_approval_msgs["ws-1"] = msg

        raw = IntentVerdictEvent(
            ws_id="ws-1",
            func_name="bash",
            risk_level="high",
            recommendation="deny",
            confidence=0.9,
            intent_summary="Dangerous operation",
            tier="llm",
        ).to_json()
        _run(bot._on_ws_event("ws-1", thread, raw))

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
        from turnstone.mq.protocol import IntentVerdictEvent

        bot = self._make_bot()
        thread = AsyncMock()

        raw = IntentVerdictEvent(ws_id="ws-1", func_name="bash", risk_level="low").to_json()
        # Should not raise
        _run(bot._on_ws_event("ws-1", thread, raw))

    def test_turn_complete_clears_pending_approval(self):
        """TurnCompleteEvent should clean up the pending approval message tracking."""
        from turnstone.channels.discord.bot import TurnstoneBot
        from turnstone.mq.protocol import TurnCompleteEvent

        bot = MagicMock(spec=TurnstoneBot)
        bot._streaming = {}
        bot._pending_approval_msgs = {"ws-1": MagicMock()}
        bot._notify_reply_channels = {}
        bot._on_ws_event = TurnstoneBot._on_ws_event.__get__(bot, TurnstoneBot)

        thread = AsyncMock()
        raw = TurnCompleteEvent(ws_id="ws-1", correlation_id="").to_json()
        _run(bot._on_ws_event("ws-1", thread, raw))

        assert "ws-1" not in bot._pending_approval_msgs


class TestContentCatchup:
    """TurnCompleteEvent with content field provides catch-up for missed ContentEvents."""

    def _make_bot(self):
        from turnstone.channels.discord.bot import TurnstoneBot

        bot = MagicMock(spec=TurnstoneBot)
        bot.config = MagicMock()
        bot.config.max_message_length = 2000
        bot.config.streaming_edit_interval = 1.5
        bot.config.auto_approve = False
        bot.config.auto_approve_tools = []
        bot._streaming = {}
        bot._pending_approval_msgs = {}
        bot._notify_reply_channels = {}
        bot._on_ws_event = TurnstoneBot._on_ws_event.__get__(bot, TurnstoneBot)
        return bot

    def test_catchup_sends_content_when_no_streaming(self):
        """TurnCompleteEvent with content but no SM sends catch-up message."""
        from turnstone.mq.protocol import TurnCompleteEvent

        bot = self._make_bot()
        thread = AsyncMock()

        raw = TurnCompleteEvent(
            ws_id="ws-1", correlation_id="", content="Caught up response"
        ).to_json()
        _run(bot._on_ws_event("ws-1", thread, raw))

        thread.send.assert_awaited_once_with("Caught up response")

    def test_catchup_skipped_when_streaming_exists(self):
        """TurnCompleteEvent with content and existing SM uses SM finalize, not catch-up."""
        from turnstone.mq.protocol import ContentEvent, TurnCompleteEvent

        bot = self._make_bot()
        thread = AsyncMock()

        # Feed content event to create SM
        content_raw = ContentEvent(ws_id="ws-1", text="Streamed").to_json()
        _run(bot._on_ws_event("ws-1", thread, content_raw))
        assert "ws-1" in bot._streaming

        # Now TurnCompleteEvent with content — SM should be finalized, not catch-up
        complete_raw = TurnCompleteEvent(
            ws_id="ws-1", correlation_id="", content="Streamed"
        ).to_json()
        _run(bot._on_ws_event("ws-1", thread, complete_raw))
        assert "ws-1" not in bot._streaming

    def test_catchup_empty_content_no_message(self):
        """TurnCompleteEvent with empty content and no SM sends nothing."""
        from turnstone.mq.protocol import TurnCompleteEvent

        bot = self._make_bot()
        thread = AsyncMock()

        raw = TurnCompleteEvent(ws_id="ws-1", correlation_id="", content="").to_json()
        _run(bot._on_ws_event("ws-1", thread, raw))

        thread.send.assert_not_awaited()


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

    def test_turn_complete_forwards_to_dm(self):
        """TurnCompleteEvent should forward content to notification reply DM."""
        from turnstone.channels.discord.bot import TurnstoneBot
        from turnstone.mq.protocol import TurnCompleteEvent

        bot = MagicMock(spec=TurnstoneBot)
        bot.config = MagicMock()
        bot.config.max_message_length = 2000
        bot._streaming = {}
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

        raw = TurnCompleteEvent(
            ws_id="ws-1", correlation_id="", content="Here's the response"
        ).to_json()
        _run(bot._on_ws_event("ws-1", thread, raw))

        # Should send to DM channel
        dm_channel.send.assert_awaited_once_with("Here's the response")
        # Should clean up forwarding
        assert "ws-1" not in bot._notify_reply_channels
        # Response message should be tracked for multi-turn replies
        assert 88888 in bot._notify_ws_map
        assert bot._notify_ws_map[88888] == ("ws-1", "u123")

    def test_turn_complete_cleans_up_dm_even_without_content(self):
        """TurnCompleteEvent without content should still clean up DM tracking."""
        from turnstone.channels.discord.bot import TurnstoneBot
        from turnstone.mq.protocol import TurnCompleteEvent

        bot = MagicMock(spec=TurnstoneBot)
        bot._streaming = {}
        bot._pending_approval_msgs = {}
        bot._notify_ws_map = {}

        dm_channel = AsyncMock()
        bot._notify_reply_channels = {"ws-1": (dm_channel, "u123")}
        bot._on_ws_event = TurnstoneBot._on_ws_event.__get__(bot, TurnstoneBot)

        thread = AsyncMock()

        raw = TurnCompleteEvent(ws_id="ws-1", correlation_id="", content="").to_json()
        _run(bot._on_ws_event("ws-1", thread, raw))

        # DM should not be sent to (no content)
        dm_channel.send.assert_not_awaited()
        # But should still be cleaned up
        assert "ws-1" not in bot._notify_reply_channels
        # No response tracked (nothing was sent)
        assert len(bot._notify_ws_map) == 0


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
