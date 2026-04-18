"""Tests for the Discord channel adapter (bot, cog, views, config, CLI)."""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# discord.utils.escape_markdown passes 'count' as positional to re.sub,
# which is deprecated in Python 3.13+.  This is a discord.py bug (fixed
# in newer releases); suppress here to keep the test output clean.
pytestmark = pytest.mark.filterwarnings(
    "ignore:.*'count' is passed as positional argument:DeprecationWarning"
)

discord = pytest.importorskip("discord")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine in a fresh event loop (no pytest-asyncio needed)."""
    return asyncio.run(coro)


def _bind_ws_event_handlers(bot, cls):
    """Bind ``_on_ws_event`` + every ``_handle_*`` method from *cls* to *bot*.

    ``MagicMock(spec=cls)`` stubs async methods as ``AsyncMock`` no-ops,
    so dispatcher tests that invoke the real ``_on_ws_event`` must also
    bind the per-event handlers it delegates to.
    """
    bot._on_ws_event = cls._on_ws_event.__get__(bot, cls)
    for name in dir(cls):
        if name.startswith("_handle_"):
            attr = getattr(cls, name)
            if callable(attr):
                setattr(bot, name, attr.__get__(bot, cls))


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

        assert sm.accumulated_text == "hello world"

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
        assert sm.message is sent_msg

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

    def test_dm_without_reference_sends_guidance(self):
        cog, ts, _bot = self._make_cog()
        dm_channel = AsyncMock()
        msg = _make_message(guild=False, channel=dm_channel)

        _run(cog._on_message(msg))

        ts.router.send_message.assert_not_awaited()
        dm_channel.send.assert_awaited_once()

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
# /ask command — model selection
# ---------------------------------------------------------------------------


class TestAskModelSelection:
    """Tests for the /ask command's model parameter and channel default."""

    def _make_cog_and_interaction(self):
        from turnstone.channels.discord.cog import MessageCog

        bot = MagicMock()
        bot.user = MagicMock()
        bot.user.id = 99999

        ts = MagicMock()
        ts.router = MagicMock()
        ts.router.resolve_user = AsyncMock(return_value="u_abc")
        ts.router.get_or_create_workstream = AsyncMock(return_value=("ws-1", True))
        ts.router.send_message = AsyncMock()
        ts.router.get_channel_default_alias = AsyncMock(return_value="")
        ts.subscribe_ws = AsyncMock()
        ts.config = MagicMock()
        ts.config.model = "cli-model"
        ts.config.thread_auto_archive = 1440
        bot.turnstone = ts

        cog = MessageCog(bot)

        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = MagicMock()
        interaction.user.id = 67890
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()
        thread = AsyncMock(spec=discord.Thread)
        thread.id = 111
        thread.mention = "<#111>"
        channel = MagicMock(spec=discord.TextChannel)
        channel.create_thread = AsyncMock(return_value=thread)
        interaction.channel = channel

        return cog, ts, interaction

    def test_explicit_model_overrides_all(self):
        cog, ts, interaction = self._make_cog_and_interaction()
        ts.router.get_channel_default_alias = AsyncMock(return_value="channel-default")

        _run(cog._cmd_ask(interaction, "hello", model="explicit-model"))

        _, kwargs = ts.router.get_or_create_workstream.call_args
        assert kwargs["model"] == "explicit-model"

    def test_channel_default_used_when_no_explicit_model(self):
        cog, ts, interaction = self._make_cog_and_interaction()
        ts.router.get_channel_default_alias = AsyncMock(return_value="channel-default")

        _run(cog._cmd_ask(interaction, "hello"))

        _, kwargs = ts.router.get_or_create_workstream.call_args
        assert kwargs["model"] == "channel-default"

    def test_cli_model_fallback(self):
        cog, ts, interaction = self._make_cog_and_interaction()
        # Channel default is empty → fall back to CLI --model.
        ts.router.get_channel_default_alias = AsyncMock(return_value="")

        _run(cog._cmd_ask(interaction, "hello"))

        _, kwargs = ts.router.get_or_create_workstream.call_args
        assert kwargs["model"] == "cli-model"

    def test_empty_model_when_no_defaults(self):
        cog, ts, interaction = self._make_cog_and_interaction()
        ts.router.get_channel_default_alias = AsyncMock(return_value="")
        ts.config.model = ""

        _run(cog._cmd_ask(interaction, "hello"))

        _, kwargs = ts.router.get_or_create_workstream.call_args
        assert kwargs["model"] == ""


# ---------------------------------------------------------------------------
# _parse_footer (views.py)
# ---------------------------------------------------------------------------


class TestParseFooter:
    """Tests for _parse_footer in views.py."""

    def test_valid_footer_with_owner(self):
        from turnstone.channels.discord.views import _parse_footer

        interaction = _make_interaction(footer_text="ws_abc|corr_123|12345")
        result = _parse_footer(interaction)
        assert result == ("ws_abc", "corr_123", "12345")

    def test_footer_without_owner_returns_empty_owner(self):
        from turnstone.channels.discord.views import _parse_footer

        # Legacy footer without an owner field (pre-upgrade posts).
        interaction = _make_interaction(footer_text="ws_abc|corr_123")
        result = _parse_footer(interaction)
        assert result == ("ws_abc", "corr_123", "")

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
        _bind_ws_event_handlers(bot, TurnstoneBot)

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
        _bind_ws_event_handlers(bot, TurnstoneBot)

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
        from turnstone.channels._routing import PolicyVerdict
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
        bot.router = MagicMock()
        bot.router.evaluate_tool_policies = AsyncMock(return_value=PolicyVerdict(kind="none"))
        _bind_ws_event_handlers(bot, TurnstoneBot)
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
        _bind_ws_event_handlers(bot, TurnstoneBot)

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
        _bind_ws_event_handlers(bot, TurnstoneBot)
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

    def _make_dm_bot(self, *, sent_message_id: int):
        """Build a MagicMock bot whose notification target resolves to a DM."""
        from turnstone.channels.discord.bot import TurnstoneBot

        bot = MagicMock(spec=TurnstoneBot)
        bot.config = MagicMock()
        bot.config.max_message_length = 2000

        sent_msg = MagicMock()
        sent_msg.id = sent_message_id

        dm_channel = MagicMock()
        dm_channel.send = AsyncMock(return_value=sent_msg)

        user = MagicMock()
        user.id = 7777
        user.create_dm = AsyncMock(return_value=dm_channel)

        inner_bot = MagicMock()
        inner_bot.get_channel = MagicMock(return_value=None)
        inner_bot.fetch_user = AsyncMock(return_value=user)
        bot._bot = inner_bot

        bot.send_notification = TurnstoneBot.send_notification.__get__(bot, TurnstoneBot)
        bot._track_notification = TurnstoneBot._track_notification.__get__(bot, TurnstoneBot)
        return bot

    def test_send_notification_tracks_dm_with_user_id(self):
        """send_notification for a DM records (ws_id, resolved_user_id)."""
        bot = self._make_dm_bot(sent_message_id=12345)
        bot._notify_ws_map = {}
        bot._MAX_NOTIFY_TRACKING = 100

        _run(bot.send_notification("7777", "Hello", "ws-abc"))

        # Tracked under the resolved Discord user ID, not the raw argument.
        assert 12345 in bot._notify_ws_map
        assert bot._notify_ws_map[12345] == ("ws-abc", "7777")

    def test_send_notification_to_guild_channel_is_not_tracked(self):
        """Notifications delivered to a guild channel must not register reply tracking.

        The reply-channel_id check treats the stored value as a Discord
        user ID, so storing a channel ID would reject every legitimate
        reply.
        """
        from turnstone.channels.discord.bot import TurnstoneBot

        bot = MagicMock(spec=TurnstoneBot)
        bot.config = MagicMock()
        bot.config.max_message_length = 2000
        bot._notify_ws_map = {}
        bot._MAX_NOTIFY_TRACKING = 100

        sent_msg = MagicMock()
        sent_msg.id = 99999

        channel = MagicMock()
        channel.send = AsyncMock(return_value=sent_msg)

        inner_bot = MagicMock()
        inner_bot.get_channel = MagicMock(return_value=channel)
        bot._bot = inner_bot

        bot.send_notification = TurnstoneBot.send_notification.__get__(bot, TurnstoneBot)
        bot._track_notification = TurnstoneBot._track_notification.__get__(bot, TurnstoneBot)

        _run(bot.send_notification("888888", "Hello", "ws-abc"))

        assert bot._notify_ws_map == {}

    def test_send_notification_evicts_old_entries(self):
        """Oldest notification tracking entries are evicted when cap is reached."""
        bot = self._make_dm_bot(sent_message_id=4)
        bot._MAX_NOTIFY_TRACKING = 3
        bot._notify_ws_map = {
            1: ("ws-1", "u1"),
            2: ("ws-2", "u2"),
            3: ("ws-3", "u3"),
        }

        _run(bot.send_notification("7777", "Hello", "ws-4"))

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

    def test_dm_without_reference_sends_guidance(self):
        """DM without a message reference should reply with guidance."""
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
        dm_channel = AsyncMock()
        msg = _make_message(guild=False, channel=dm_channel)  # reference=None

        _run(cog._on_message(msg))

        ts.router.send_message.assert_not_awaited()
        dm_channel.send.assert_awaited_once()
        sent_text = dm_channel.send.call_args[0][0]
        assert "/ask" in sent_text

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
        _bind_ws_event_handlers(bot, TurnstoneBot)
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
        _bind_ws_event_handlers(bot, TurnstoneBot)

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

        result = format_tool_result("hello world")
        assert "```" in result
        assert "hello world" in result

    def test_wraps_in_code_block(self):
        from turnstone.channels._formatter import format_tool_result

        result = format_tool_result("output text")
        assert result.startswith("```\n")
        assert result.endswith("\n```")

    def test_truncates_long_output_by_lines(self):
        from turnstone.channels._formatter import format_tool_result

        output = "\n".join(f"line {i}" for i in range(20))
        result = format_tool_result(output)
        # Should have at most 10 content lines + ellipsis
        inner = result.split("```")[1]
        assert inner.strip().count("\n") <= 11

    def test_truncates_long_output_by_chars(self):
        from turnstone.channels._formatter import format_tool_result

        output = "x" * 600
        result = format_tool_result(output)
        # Code block content should be <= 500 chars (497 + ellipsis)
        inner = result.split("```")[1].strip()
        assert len(inner) <= 501  # 497 + ellipsis char

    def test_escapes_triple_backticks_in_output(self):
        from turnstone.channels._formatter import format_tool_result

        output = "before ``` after"
        result = format_tool_result(output)
        # Only the opening and closing code fences should remain as ```.
        assert result.count("```") == 2


# ---------------------------------------------------------------------------
# Media embed detection and rendering
# ---------------------------------------------------------------------------


class TestTryParseMedia:
    """Tests for try_parse_media in _formatter.py."""

    def test_stream_url_detected(self):
        import json

        from turnstone.channels._formatter import try_parse_media

        data = json.dumps({"stream_url": "http://jf:8096/Videos/abc/stream", "container": "mp4"})
        result = try_parse_media(data)
        assert result is not None
        assert result["stream_url"] == "http://jf:8096/Videos/abc/stream"

    def test_media_details_detected(self):
        import json

        from turnstone.channels._formatter import try_parse_media

        data = json.dumps({"id": "abc", "name": "Test Movie", "type": "Movie", "year": 2024})
        result = try_parse_media(data)
        assert result is not None
        assert result["name"] == "Test Movie"

    def test_search_results_detected(self):
        import json

        from turnstone.channels._formatter import try_parse_media

        data = json.dumps({"results": [{"id": "1", "name": "Hit"}], "total_count": 1})
        result = try_parse_media(data)
        assert result is not None
        assert len(result["results"]) == 1

    def test_sessions_detected(self):
        import json

        from turnstone.channels._formatter import try_parse_media

        data = json.dumps({"sessions": [{"id": "s1", "user_name": "ptrck"}]})
        result = try_parse_media(data)
        assert result is not None

    def test_empty_results_returns_none(self):
        import json

        from turnstone.channels._formatter import try_parse_media

        assert try_parse_media(json.dumps({"results": []})) is None

    def test_plain_text_returns_none(self):
        from turnstone.channels._formatter import try_parse_media

        assert try_parse_media("just a string") is None

    def test_non_dict_json_returns_none(self):
        from turnstone.channels._formatter import try_parse_media

        assert try_parse_media("[1, 2, 3]") is None

    def test_unrelated_dict_returns_none(self):
        import json

        from turnstone.channels._formatter import try_parse_media

        assert try_parse_media(json.dumps({"foo": "bar"})) is None


class TestIsSafeImageUrl:
    """Tests for _is_safe_image_url in _formatter.py."""

    @staticmethod
    def _patch_resolver(monkeypatch, ips):
        """Replace socket.getaddrinfo with a stub returning *ips*."""
        import socket

        def fake(host, port, family=0, *args, **kwargs):  # noqa: ARG001
            return [(family, 0, 0, "", (ip, 0)) for ip in ips]

        monkeypatch.setattr(socket, "getaddrinfo", fake)

    def test_http_url(self, monkeypatch):
        from turnstone.channels._formatter import _is_safe_image_url

        self._patch_resolver(monkeypatch, ["203.0.113.5"])
        assert _run(_is_safe_image_url("http://jellyfin:8096/Items/abc/Images/Primary")) is True

    def test_https_url(self, monkeypatch):
        from turnstone.channels._formatter import _is_safe_image_url

        self._patch_resolver(monkeypatch, ["203.0.113.5"])
        assert (
            _run(_is_safe_image_url("https://jellyfin.example.com/Items/abc/Images/Primary"))
            is True
        )

    def test_ftp_rejected(self):
        from turnstone.channels._formatter import _is_safe_image_url

        assert _run(_is_safe_image_url("ftp://evil.com/image.jpg")) is False

    def test_file_rejected(self):
        from turnstone.channels._formatter import _is_safe_image_url

        assert _run(_is_safe_image_url("file:///etc/passwd")) is False

    def test_userinfo_rejected(self):
        from turnstone.channels._formatter import _is_safe_image_url

        assert _run(_is_safe_image_url("http://user:pass@jellyfin:8096/image")) is False

    def test_empty_rejected(self):
        from turnstone.channels._formatter import _is_safe_image_url

        assert _run(_is_safe_image_url("")) is False

    def test_private_ip_allowed(self):
        from turnstone.channels._formatter import _is_safe_image_url

        assert _run(_is_safe_image_url("http://192.168.0.6:8096/Items/abc/Images/Primary")) is True

    def test_dns_rebinding_rejected(self, monkeypatch):
        """Hostname that resolves to a loopback IP must be rejected."""
        from turnstone.channels._formatter import _is_safe_image_url

        self._patch_resolver(monkeypatch, ["127.0.0.1"])
        assert _run(_is_safe_image_url("http://rebind.example.com/image")) is False

    def test_metadata_endpoint_rejected(self):
        """AWS/GCP metadata IP is link-local → rejected."""
        from turnstone.channels._formatter import _is_safe_image_url

        assert _run(_is_safe_image_url("http://169.254.169.254/latest/meta-data/")) is False

    def test_ipv6_aws_nitro_metadata_rejected(self, monkeypatch):
        """fd00:ec2::254 is IPv6 ULA (is_private) but must be blocked —
        the IPv4 169.254.169.254 check left this analogue open."""
        from turnstone.channels._formatter import _is_safe_image_url

        self._patch_resolver(monkeypatch, ["fd00:ec2::254"])
        assert _run(_is_safe_image_url("http://nitro.example.com/")) is False

    def test_ipv6_ecs_task_metadata_rejected(self, monkeypatch):
        """ECS Task Metadata lives in the same fd00:ec2::/32 prefix."""
        from turnstone.channels._formatter import _is_safe_image_url

        self._patch_resolver(monkeypatch, ["fd00:ec2::23"])
        assert _run(_is_safe_image_url("http://ecs-meta.example.com/")) is False


class TestBuildMediaEmbed:
    """Tests for try_build_media_embed and embed builders."""

    def test_single_item_embed_uses_web_url_not_stream_url(self):
        import json

        from turnstone.channels._formatter import try_parse_media

        data = {
            "name": "Test Movie",
            "type": "Movie",
            "year": 2024,
            "stream_url": "http://jf:8096/Videos/abc/stream?api_key=SECRET",
            "web_url": "http://jf:8096/web/#/details?id=abc",
            "overview": "A test movie.",
        }
        parsed = try_parse_media(json.dumps(data))
        assert parsed is not None

        from turnstone.channels._formatter import _build_single_media_embed

        embed = _build_single_media_embed(parsed, "mcp__mediamcp__get_stream_url")
        # web_url should be the embed URL, never stream_url
        assert embed.url == "http://jf:8096/web/#/details?id=abc"
        assert "SECRET" not in str(embed.to_dict())

    def test_search_results_embed_format(self):
        import json

        from turnstone.channels._formatter import try_parse_media

        data = {
            "results": [
                {"name": "Movie A", "year": 2020, "type": "Movie", "runtime_minutes": 120},
                {"name": "Movie B", "year": 2021, "type": "Movie"},
            ],
            "total_count": 2,
        }
        parsed = try_parse_media(json.dumps(data))

        from turnstone.channels._formatter import _build_search_results_embed

        embed = _build_search_results_embed(parsed)
        assert "Movie A" in embed.description
        assert "Movie B" in embed.description
        assert "2 of 2" in embed.footer.text

    def test_build_media_embed_returns_none_for_plain_text(self):
        from turnstone.channels._formatter import try_build_media_embed

        http = MagicMock()
        result = _run(try_build_media_embed("tool", "plain text", http=http))
        assert result is None

    def test_season_episode_string_values(self):
        """Season/episode numbers as strings should not raise."""

        from turnstone.channels._formatter import _build_search_results_embed

        data = {
            "results": [
                {
                    "name": "Pilot",
                    "type": "Episode",
                    "series_name": "Show",
                    "season_number": "1",
                    "episode_number": "1",
                },
            ],
            "total_count": 1,
        }
        embed = _build_search_results_embed(data)
        assert "S01E01" in embed.description


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
        _bind_ws_event_handlers(bot, TurnstoneBot)
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

    def test_thinking_stop_preserves_message_for_reuse(self):
        from turnstone.sdk.events import ThinkingStopEvent

        bot = self._make_bot()
        thread = AsyncMock()
        thinking_msg = MagicMock()
        thinking_msg.delete = AsyncMock()
        bot._thinking_msgs["ws-1"] = thinking_msg

        event = ThinkingStopEvent(ws_id="ws-1")
        _run(bot._on_ws_event("ws-1", thread, event))

        # Message kept for next event to reuse via edit.
        thinking_msg.delete.assert_not_awaited()
        assert "ws-1" in bot._thinking_msgs

    def test_thinking_stop_without_message_is_noop(self):
        from turnstone.sdk.events import ThinkingStopEvent

        bot = self._make_bot()
        thread = AsyncMock()

        event = ThinkingStopEvent(ws_id="ws-1")
        _run(bot._on_ws_event("ws-1", thread, event))

    def test_content_event_reuses_thinking_message(self):
        from turnstone.sdk.events import ContentEvent

        bot = self._make_bot()
        thread = AsyncMock()
        thinking_msg = MagicMock()
        thinking_msg.edit = AsyncMock()
        bot._thinking_msgs["ws-1"] = thinking_msg

        event = ContentEvent(ws_id="ws-1", text="Hello")
        _run(bot._on_ws_event("ws-1", thread, event))

        # Thinking message becomes the StreamingMessage base — no delete.
        assert "ws-1" not in bot._thinking_msgs
        sm = bot._streaming["ws-1"]
        assert sm.message is thinking_msg

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
        _bind_ws_event_handlers(bot, TurnstoneBot)
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
        assert bot._tool_info_msgs["ws-1"] == [("", "bash", "ls -la", sent_msg)]

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

    def test_shows_all_items_regardless_of_approval(self):
        from turnstone.sdk.events import ToolInfoEvent

        bot = self._make_bot()
        thread = AsyncMock()

        items = [
            {"func_name": "bash", "preview": "rm -rf /", "needs_approval": True},
            {"func_name": "read_file", "preview": "/etc/hosts", "needs_approval": False},
        ]
        event = ToolInfoEvent(ws_id="ws-1", items=items)
        _run(bot._on_ws_event("ws-1", thread, event))

        # Both items shown — running indicator is separate from approval dialog.
        assert thread.send.await_count == 2

    def test_reuses_thinking_message_for_first_tool(self):
        from turnstone.sdk.events import ToolInfoEvent

        bot = self._make_bot()
        thread = AsyncMock()
        thinking_msg = MagicMock()
        thinking_msg.edit = AsyncMock()
        bot._thinking_msgs["ws-1"] = thinking_msg

        items = [{"func_name": "bash", "preview": "ls -la", "needs_approval": False}]
        event = ToolInfoEvent(ws_id="ws-1", items=items)
        _run(bot._on_ws_event("ws-1", thread, event))

        # Thinking message edited into tool embed, no new message sent.
        thinking_msg.edit.assert_awaited_once()
        thread.send.assert_not_awaited()
        assert "ws-1" not in bot._thinking_msgs
        # The reused message is tracked for ToolResultEvent editing.
        assert bot._tool_info_msgs["ws-1"][0][3] is thinking_msg


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
        bot._http_client = MagicMock()
        bot._should_auto_approve = MagicMock(return_value=False)
        _bind_ws_event_handlers(bot, TurnstoneBot)
        return bot

    def test_marks_info_done_and_sends_result(self):
        from turnstone.sdk.events import ToolResultEvent

        bot = self._make_bot()
        thread = AsyncMock()

        # Pre-populate a tool info message (as ToolInfoEvent would).
        info_msg = MagicMock()
        info_msg.edit = AsyncMock()
        bot._tool_info_msgs["ws-1"] = [("", "bash", "ls -la", info_msg)]

        event = ToolResultEvent(ws_id="ws-1", name="bash", output="file1\nfile2")
        _run(bot._on_ws_event("ws-1", thread, event))

        # Info embed edited to "Done" status.
        info_msg.edit.assert_awaited_once()
        status_embed = info_msg.edit.call_args[1]["embed"]
        assert "Done" in status_embed.title
        assert status_embed.description == "ls -la"  # preview preserved
        # Result sent as separate new message.
        thread.send.assert_awaited_once()
        result_embed = thread.send.call_args[1]["embed"]
        assert result_embed.title == "bash"
        assert "file1" in result_embed.description
        # Entry consumed from tracking list.
        assert bot._tool_info_msgs["ws-1"] == []

    def test_result_sent_even_without_info_match(self):
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

    def test_success_result_uses_dark_grey_color(self):
        from turnstone.sdk.events import ToolResultEvent

        bot = self._make_bot()
        thread = AsyncMock()

        event = ToolResultEvent(ws_id="ws-1", name="bash", output="ok")
        _run(bot._on_ws_event("ws-1", thread, event))

        embed = thread.send.call_args[1]["embed"]
        assert embed.color == discord.Color.dark_grey()

    def test_call_id_matching(self):
        from turnstone.sdk.events import ToolResultEvent

        bot = self._make_bot()
        thread = AsyncMock()

        first_msg = MagicMock()
        first_msg.edit = AsyncMock()
        second_msg = MagicMock()
        second_msg.edit = AsyncMock()
        bot._tool_info_msgs["ws-1"] = [
            ("call-1", "bash", "", first_msg),
            ("call-2", "bash", "", second_msg),
        ]

        # Result with call_id matches the correct message regardless of order.
        event = ToolResultEvent(ws_id="ws-1", call_id="call-2", name="bash", output="result")
        _run(bot._on_ws_event("ws-1", thread, event))
        second_msg.edit.assert_awaited_once()
        first_msg.edit.assert_not_awaited()

    def test_fifo_fallback_when_no_call_id(self):
        from turnstone.sdk.events import ToolResultEvent

        bot = self._make_bot()
        thread = AsyncMock()

        first_msg = MagicMock()
        first_msg.edit = AsyncMock()
        second_msg = MagicMock()
        second_msg.edit = AsyncMock()
        bot._tool_info_msgs["ws-1"] = [("", "bash", "", first_msg), ("", "bash", "", second_msg)]

        # No call_id — falls back to FIFO name match.
        event1 = ToolResultEvent(ws_id="ws-1", name="bash", output="result1")
        _run(bot._on_ws_event("ws-1", thread, event1))
        first_msg.edit.assert_awaited_once()
        second_msg.edit.assert_not_awaited()

        event2 = ToolResultEvent(ws_id="ws-1", name="bash", output="result2")
        _run(bot._on_ws_event("ws-1", thread, event2))
        second_msg.edit.assert_awaited_once()

    def test_edit_failure_falls_back_to_send(self):
        from turnstone.sdk.events import ToolResultEvent

        bot = self._make_bot()
        thread = AsyncMock()

        info_msg = MagicMock()
        info_msg.edit = AsyncMock(side_effect=Exception("Discord API error"))
        bot._tool_info_msgs["ws-1"] = [("", "bash", "ls -la", info_msg)]

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
        _bind_ws_event_handlers(bot, TurnstoneBot)
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


# ---------------------------------------------------------------------------
# Approval / plan-review interaction views — owner-check regression tests
# ---------------------------------------------------------------------------


def _make_view_interaction(user_id: int, footer: str | None) -> MagicMock:
    """Build a minimal interaction for ApprovalView / PlanReviewView tests."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    interaction.message = MagicMock()
    if footer is None:
        interaction.message.embeds = []
    else:
        embed = MagicMock()
        embed.footer.text = footer
        interaction.message.embeds = [embed]
    return interaction


def _make_view_bot() -> MagicMock:
    """Build a TurnstoneBot double with just the surface the views read."""
    from turnstone.channels.discord.bot import TurnstoneBot

    bot = MagicMock(spec=TurnstoneBot)
    bot.router = MagicMock()
    bot.router.resolve_user = AsyncMock(return_value="turnstone-user-1")
    bot.router.send_approval = AsyncMock()
    bot.router.send_plan_feedback = AsyncMock()
    bot._pending_approval_msgs = {}
    return bot


class TestApprovalViewOwnerCheck:
    """ApprovalView rejects clicks from anyone other than the session owner."""

    def test_owner_approve_allowed(self, monkeypatch):
        from turnstone.channels.discord.views import ApprovalView

        # Avoid real disable_message_buttons (touches discord.ui internals).
        monkeypatch.setattr(
            "turnstone.channels.discord.views._disable_buttons",
            AsyncMock(),
        )
        view = ApprovalView(_make_view_bot())
        interaction = _make_view_interaction(user_id=42, footer="ws-1|corr-1|42")

        _run(view._handle(interaction, approved=True, always=False))

        view.bot.router.send_approval.assert_awaited_once_with(
            ws_id="ws-1",
            correlation_id="corr-1",
            approved=True,
            always=False,
        )

    def test_non_owner_rejected(self):
        from turnstone.channels.discord.views import ApprovalView

        view = ApprovalView(_make_view_bot())
        interaction = _make_view_interaction(user_id=999, footer="ws-1|corr-1|42")

        _run(view._handle(interaction, approved=True, always=False))

        view.bot.router.send_approval.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()
        msg_kwargs = interaction.response.send_message.call_args
        assert "Only the session owner" in msg_kwargs.args[0]
        assert msg_kwargs.kwargs.get("ephemeral") is True

    def test_legacy_footer_without_owner_rejected(self):
        from turnstone.channels.discord.views import ApprovalView

        view = ApprovalView(_make_view_bot())
        # Pre-upgrade footer with only ws_id|correlation_id — fail closed.
        interaction = _make_view_interaction(user_id=42, footer="ws-1|corr-1")

        _run(view._handle(interaction, approved=True, always=False))

        view.bot.router.send_approval.assert_not_awaited()


class TestPlanReviewViewOwnerCheck:
    """PlanReviewView rejects clicks from anyone other than the session owner."""

    def test_owner_approve_allowed(self, monkeypatch):
        from turnstone.channels.discord.views import PlanReviewView

        monkeypatch.setattr(
            "turnstone.channels.discord.views._disable_buttons",
            AsyncMock(),
        )
        view = PlanReviewView(_make_view_bot())
        interaction = _make_view_interaction(user_id=42, footer="ws-1|corr-1|42")

        _run(view._handle_approve(interaction))

        view.bot.router.send_plan_feedback.assert_awaited_once_with(
            ws_id="ws-1",
            correlation_id="corr-1",
            feedback="",
        )

    def test_non_owner_approve_rejected(self):
        from turnstone.channels.discord.views import PlanReviewView

        view = PlanReviewView(_make_view_bot())
        interaction = _make_view_interaction(user_id=999, footer="ws-1|corr-1|42")

        _run(view._handle_approve(interaction))

        view.bot.router.send_plan_feedback.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()

    def test_non_owner_changes_modal_rejected(self):
        from turnstone.channels.discord.views import PlanReviewView

        view = PlanReviewView(_make_view_bot())
        interaction = _make_view_interaction(user_id=999, footer="ws-1|corr-1|42")

        _run(view._handle_changes(interaction))

        interaction.response.send_modal.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()


class TestDiscordThreadOwnerCheck:
    """Sec-3 gate: only the thread creator can send messages into the workstream."""

    @staticmethod
    def _make_cog_and_ts():
        """Build a MessageCog wired to a minimal TurnstoneBot double."""
        from turnstone.channels.discord.cog import MessageCog

        bot = MagicMock()
        bot.user = MagicMock()
        bot.user.id = 99999
        bot.user.mentioned_in = MagicMock(return_value=False)

        ts = MagicMock()
        ts._is_allowed_channel = MagicMock(return_value=True)
        ts.storage = MagicMock()
        ts.router = MagicMock()
        ts.router.lookup_ws_id = AsyncMock(return_value="ws-1")
        ts.router.resolve_user = AsyncMock(return_value="turnstone-user-1")
        ts.router.send_message = AsyncMock()
        ts.router.get_or_create_workstream = AsyncMock(return_value=("ws-1", False))
        ts.config = MagicMock()
        ts._ws_tasks = {}
        ts._subscribed_ws = {"ws-1"}
        ts._notify_ws_map = {}
        ts._notify_reply_channels = {}
        ts.get_thread_invoker = MagicMock(return_value=None)
        ts.subscribe_ws = AsyncMock()
        bot.turnstone = ts

        return MessageCog(bot), ts

    def test_non_owner_thread_message_dropped(self):
        """A linked user who is NOT the thread creator gets their message
        silently dropped — router.send_message must not fire."""
        cog, ts = self._make_cog_and_ts()

        # Build a thread whose owner_id is different from the message author.
        thread = MagicMock(spec=discord.Thread)
        thread.id = 555
        thread.parent_id = 111
        thread.owner_id = 42  # thread creator
        thread.name = "some-thread"

        msg = _make_message(guild=True, channel=thread)
        msg.author.id = 999  # non-owner trying to inject

        _run(cog._on_message(msg))

        ts.router.send_message.assert_not_awaited()
        ts.router.get_or_create_workstream.assert_not_awaited()

    def test_ask_thread_followup_allowed_when_invoker_registered(self):
        """/ask creates threads with owner_id=bot; follow-ups from the
        registered invoker must still reach the workstream."""
        cog, ts = self._make_cog_and_ts()
        # Simulate what _cmd_ask does after channel.create_thread().
        ts.get_thread_invoker = MagicMock(return_value=111)

        thread = MagicMock(spec=discord.Thread)
        thread.id = 555
        thread.parent_id = 222
        thread.owner_id = 99999  # bot owns the thread after channel.create_thread
        thread.name = "ask-thread"

        msg = _make_message(guild=True, channel=thread)
        msg.author.id = 111  # the human who ran /ask

        _run(cog._on_message(msg))

        ts.router.send_message.assert_awaited_once_with("ws-1", msg.content)

    def test_ask_thread_rejects_other_user_even_when_invoker_registered(self):
        """Registered invoker lock: only that user's follow-ups pass."""
        cog, ts = self._make_cog_and_ts()
        ts.get_thread_invoker = MagicMock(return_value=111)

        thread = MagicMock(spec=discord.Thread)
        thread.id = 555
        thread.parent_id = 222
        thread.owner_id = 99999  # bot-owned
        thread.name = "ask-thread"

        msg = _make_message(guild=True, channel=thread)
        msg.author.id = 222  # someone other than the recorded invoker

        _run(cog._on_message(msg))

        ts.router.send_message.assert_not_awaited()
