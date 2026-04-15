"""Tests for the Slack channel adapter (bot, config, CLI)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

slack_bolt = pytest.importorskip("slack_bolt")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):  # type: ignore[no-untyped-def]
    """Run an async coroutine in a fresh event loop (no pytest-asyncio needed)."""
    return asyncio.run(coro)


def _make_slack_event(
    *,
    bot_id: str | None = None,
    subtype: str | None = None,
    channel: str = "C01SAPU5414",
    channel_type: str = "channel",
    thread_ts: str = "1234567890.000100",
    user: str = "U12345",
    text: str = "hello",
    ts: str = "1234567890.000200",
) -> dict[str, object]:
    event: dict[str, object] = {
        "channel": channel,
        "channel_type": channel_type,
        "user": user,
        "text": text,
        "ts": ts,
        "thread_ts": thread_ts,
    }
    if bot_id is not None:
        event["bot_id"] = bot_id
    if subtype is not None:
        event["subtype"] = subtype
    return event


def _make_bot() -> tuple[object, MagicMock, MagicMock]:
    """Build a TurnstoneSlackBot with fully mocked dependencies."""
    from turnstone.channels.slack.bot import TurnstoneSlackBot
    from turnstone.channels.slack.config import SlackConfig

    config = SlackConfig(
        bot_token="xoxb-test",
        app_token="xapp-test",
        allowed_channels=["C01SAPU5414"],
        auto_approve=False,
    )

    storage = MagicMock()
    storage.list_channel_routes_by_type = MagicMock(return_value=[])

    router = MagicMock()
    router.get_or_create_workstream = AsyncMock(return_value=("ws-1", True))
    router.send_message = AsyncMock()
    router.send_approval = AsyncMock()
    router.get_node_url = AsyncMock(return_value="http://localhost:8080")
    router.aclose = AsyncMock()

    client = AsyncMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "1234567890.000100"})
    client.chat_update = AsyncMock(return_value={"ok": True})
    client.chat_postEphemeral = AsyncMock(return_value={"ok": True})
    client.conversations_history = AsyncMock(return_value={"ok": True, "messages": []})

    with (
        patch("turnstone.channels.slack.bot.AsyncApp", MagicMock()),
        patch("turnstone.channels.slack.bot.AsyncWebClient", return_value=client),
    ):
        bot = TurnstoneSlackBot(
            config,
            server_url="http://localhost:8080",
            storage=storage,
        )

    bot.router = router  # type: ignore[attr-defined]
    bot._client = client  # type: ignore[attr-defined]

    return bot, router, client


# ---------------------------------------------------------------------------
# SlackConfig
# ---------------------------------------------------------------------------


class TestSlackConfig:
    """Tests for SlackConfig default and custom values."""

    def test_defaults(self) -> None:
        from turnstone.channels.slack.config import SlackConfig

        cfg = SlackConfig()
        assert cfg.bot_token == ""
        assert cfg.app_token == ""
        assert cfg.allowed_channels == []
        assert cfg.max_message_length == 3000
        assert cfg.streaming_edit_interval == 1.5
        # Inherited from ChannelConfig
        assert cfg.model == ""
        assert cfg.auto_approve is False

    def test_custom_values(self) -> None:
        from turnstone.channels.slack.config import SlackConfig

        cfg = SlackConfig(
            bot_token="xoxb-123",
            app_token="xapp-456",
            allowed_channels=["C1", "C2"],
            max_message_length=4000,
            streaming_edit_interval=0.5,
            model="gpt-4.1",
            auto_approve=True,
        )
        assert cfg.bot_token == "xoxb-123"
        assert cfg.app_token == "xapp-456"
        assert cfg.allowed_channels == ["C1", "C2"]
        assert cfg.max_message_length == 4000
        assert cfg.streaming_edit_interval == 0.5
        assert cfg.model == "gpt-4.1"
        assert cfg.auto_approve is True


# ---------------------------------------------------------------------------
# StreamingMessage
# ---------------------------------------------------------------------------


class TestStreamingMessage:
    """Tests for the StreamingMessage helper."""

    def test_append_accumulates(self) -> None:
        from turnstone.channels.slack.bot import StreamingMessage

        client = AsyncMock()
        client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "123"})
        client.chat_update = AsyncMock(return_value={"ok": True})
        
        sm = StreamingMessage(client=client, channel="C1", edit_interval=999.0)

        _run(sm.append("hello "))
        _run(sm.append("world"))

        assert "".join(sm._buffer) == "hello world"

    def test_finalize_sends_when_no_prior_message(self) -> None:
        from turnstone.channels.slack.bot import StreamingMessage

        client = AsyncMock()
        client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "123"})
        sm = StreamingMessage(client=client, channel="C1", edit_interval=999.0)

        _run(sm.append("hello"))
        _run(sm.finalize())

        client.chat_postMessage.assert_awaited_once()

    def test_finalize_edits_existing_message(self) -> None:
        from turnstone.channels.slack.bot import StreamingMessage

        client = AsyncMock()
        client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "123"})
        client.chat_update = AsyncMock(return_value={"ok": True})
        sm = StreamingMessage(client=client, channel="C1", edit_interval=0.0)

        _run(sm.append("hi"))
        assert sm._ts == "123"

        _run(sm.finalize())
        client.chat_update.assert_awaited()

    def test_finalize_chunks_long_content(self) -> None:
        from turnstone.channels.slack.bot import StreamingMessage

        client = AsyncMock()
        client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "123"})
        sm = StreamingMessage(client=client, channel="C1", max_length=10, edit_interval=999.0)

        _run(sm.append("a" * 25))
        _run(sm.finalize())

        assert client.chat_postMessage.await_count >= 2

    def test_finalize_empty_is_noop(self) -> None:
        from turnstone.channels.slack.bot import StreamingMessage

        client = AsyncMock()
        client.chat_postMessage = AsyncMock()
        sm = StreamingMessage(client=client, channel="C1")

        _run(sm.finalize())
        client.chat_postMessage.assert_not_awaited()


# ---------------------------------------------------------------------------
# _on_message filtering
# ---------------------------------------------------------------------------


class TestOnMessage:
    """Tests for the _on_message handler filtering logic."""

    def test_ignores_bot_messages(self) -> None:
        bot, router, _ = _make_bot()
        event = _make_slack_event(bot_id="B12345")
        say = AsyncMock()

        _run(bot._on_message(event, say))  # type: ignore[attr-defined]

        router.send_message.assert_not_awaited()

    def test_ignores_subtype_messages(self) -> None:
        bot, router, _ = _make_bot()
        event = _make_slack_event(subtype="message_changed")
        say = AsyncMock()

        _run(bot._on_message(event, say))  # type: ignore[attr-defined]

        router.send_message.assert_not_awaited()

    def test_ignores_non_allowed_channel(self) -> None:
        bot, router, _ = _make_bot()
        event = _make_slack_event(channel="C_NOT_ALLOWED")
        say = AsyncMock()

        _run(bot._on_message(event, say))  # type: ignore[attr-defined]

        router.send_message.assert_not_awaited()

    def test_ignores_message_outside_session_thread(self) -> None:
        bot, router, _ = _make_bot()
        # Set up a session with a different thread_ts
        bot._channel_sessions[("C01SAPU5414", "U12345")] = ("ws-1", "9999999.000001")  # type: ignore[attr-defined]

        event = _make_slack_event(thread_ts="1234567890.000100")  # different thread
        say = AsyncMock()

        _run(bot._on_message(event, say))  # type: ignore[attr-defined]

        router.send_message.assert_not_awaited()

    def test_routes_message_in_active_session_thread(self) -> None:
        bot, router, _ = _make_bot()
        bot._channel_sessions[("C01SAPU5414", "U12345")] = ("ws-1", "1234567890.000100")  # type: ignore[attr-defined]

        event = _make_slack_event(thread_ts="1234567890.000100")
        say = AsyncMock()

        _run(bot._on_message(event, say))  # type: ignore[attr-defined]

        router.send_message.assert_awaited_once_with("ws-1", "hello")

    def test_dm_routes_freely(self) -> None:
        bot, router, _ = _make_bot()
        router.get_or_create_workstream = AsyncMock(return_value=("ws-dm", True))

        event = _make_slack_event(channel_type="im", thread_ts="")
        say = AsyncMock()

        _run(bot._on_message(event, say))  # type: ignore[attr-defined]

        router.send_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# Per-user session isolation
# ---------------------------------------------------------------------------


class TestPerUserSessionIsolation:
    """User1 session must not be closed when user2 starts a session."""

    def test_user2_session_does_not_archive_user1(self) -> None:
        bot, router, client = _make_bot()

        # User1 has an existing session
        bot._channel_sessions[("C01SAPU5414", "U111")] = ("ws-user1", "1000000.000001")  # type: ignore[attr-defined]
        bot._subscribed_ws.add("ws-user1")  # type: ignore[attr-defined]

        # User2 starts a new session
        body = {"channel_id": "C01SAPU5414", "user_id": "U222"}
        ack = AsyncMock()

        router.get_or_create_workstream = AsyncMock(return_value=("ws-user2", True))

        _run(bot._on_slash_command(ack, body))  # type: ignore[attr-defined]

        # User1 session should still exist
        assert ("C01SAPU5414", "U111") in bot._channel_sessions  # type: ignore[attr-defined]
        assert bot._channel_sessions[("C01SAPU5414", "U111")] == ("ws-user1", "1000000.000001")  # type: ignore[attr-defined]

    def test_same_user_second_session_archives_first(self) -> None:
        bot, router, client = _make_bot()

        # User1 has an existing session
        bot._channel_sessions[("C01SAPU5414", "U111")] = ("ws-old", "1000000.000001")  # type: ignore[attr-defined]
        bot._subscribed_ws.add("ws-old")  # type: ignore[attr-defined]

        # Same user starts a new session
        body = {"channel_id": "C01SAPU5414", "user_id": "U111"}
        ack = AsyncMock()

        router.get_or_create_workstream = AsyncMock(return_value=("ws-new", True))

        _run(bot._on_slash_command(ack, body))  # type: ignore[attr-defined]

        # Old session should be gone, new one in its place
        assert bot._channel_sessions.get(("C01SAPU5414", "U111")) is not None  # type: ignore[attr-defined]
        ws_id, _ = bot._channel_sessions[("C01SAPU5414", "U111")]  # type: ignore[attr-defined]
        assert ws_id == "ws-new"

        # Archive message should have been sent
        client.chat_postMessage.assert_awaited()
        calls = [str(c) for c in client.chat_postMessage.call_args_list]
        assert any("archived" in c for c in calls)


# ---------------------------------------------------------------------------
# _on_ws_event dispatch
# ---------------------------------------------------------------------------


class TestWsEventDispatch:
    """Tests for SSE event handling in the Slack bot."""

    def _make_ws_bot(self) -> tuple[object, MagicMock]:
        from turnstone.channels.slack.bot import TurnstoneSlackBot
        from turnstone.channels.slack.config import SlackConfig

        config = SlackConfig(
            bot_token="xoxb-test",
            app_token="xapp-test",
            auto_approve=False,
        )
        storage = MagicMock()
        router = MagicMock()
        router.send_approval = AsyncMock()
        client = AsyncMock()
        client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "123"})
        client.chat_update = AsyncMock(return_value={"ok": True})
        client.conversations_history = AsyncMock(return_value={"ok": True, "messages": []})

        with (
            patch("turnstone.channels.slack.bot.AsyncApp", MagicMock()),
            patch("turnstone.channels.slack.bot.AsyncWebClient", return_value=client),
        ):
            bot = TurnstoneSlackBot(
                config,
                server_url="http://localhost:8080",
                storage=storage,
            )
        bot.router = router  # type: ignore[attr-defined]
        bot._client = client  # type: ignore[attr-defined]
        bot.storage = None  # type: ignore[attr-defined]
        return bot, client

    def test_content_event_creates_streaming_message(self) -> None:
        from turnstone.sdk.events import ContentEvent

        bot, client = self._make_ws_bot()

        event = ContentEvent(ws_id="ws-1", text="Hello")
        _run(bot._on_ws_event("ws-1", "C1", "123.456", event))  # type: ignore[attr-defined]

        assert "ws-1" in bot._streaming  # type: ignore[attr-defined]

    def test_stream_end_finalizes_streaming(self) -> None:
        from turnstone.sdk.events import ContentEvent, StreamEndEvent

        bot, client = self._make_ws_bot()

        _run(bot._on_ws_event("ws-1", "C1", "123.456", ContentEvent(ws_id="ws-1", text="Hi")))  # type: ignore[attr-defined]
        assert "ws-1" in bot._streaming  # type: ignore[attr-defined]

        _run(bot._on_ws_event("ws-1", "C1", "123.456", StreamEndEvent(ws_id="ws-1")))  # type: ignore[attr-defined]
        assert "ws-1" not in bot._streaming  # type: ignore[attr-defined]

    def test_stream_end_no_streaming_is_noop(self) -> None:
        from turnstone.sdk.events import StreamEndEvent

        bot, client = self._make_ws_bot()

        _run(bot._on_ws_event("ws-1", "C1", "123.456", StreamEndEvent(ws_id="ws-1")))  # type: ignore[attr-defined]
        assert "ws-1" not in bot._streaming  # type: ignore[attr-defined]

    def test_error_event_posts_message(self) -> None:
        from turnstone.sdk.events import ErrorEvent

        bot, client = self._make_ws_bot()

        event = ErrorEvent(ws_id="ws-1", message="Something went wrong")
        _run(bot._on_ws_event("ws-1", "C1", "123.456", event))  # type: ignore[attr-defined]

        client.chat_postMessage.assert_awaited_once()
        text = client.chat_postMessage.call_args[1]["text"]
        assert "Something went wrong" in text

    def test_approve_request_auto_approve(self) -> None:
        from turnstone.channels.slack.bot import TurnstoneSlackBot
        from turnstone.channels.slack.config import SlackConfig
        from turnstone.sdk.events import ApproveRequestEvent

        config = SlackConfig(bot_token="xoxb-test", app_token="xapp-test", auto_approve=True)
        storage = MagicMock()
        router = MagicMock()
        router.send_approval = AsyncMock()
        client = AsyncMock()
        client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "123"})

        with (
            patch("turnstone.channels.slack.bot.AsyncApp", MagicMock()),
            patch("turnstone.channels.slack.bot.AsyncWebClient", return_value=client),
        ):
            bot = TurnstoneSlackBot(config, server_url="http://localhost:8080", storage=storage)
        bot.router = router  # type: ignore[attr-defined]
        bot._client = client  # type: ignore[attr-defined]
        bot.storage = None  # type: ignore[attr-defined]

        event = ApproveRequestEvent(ws_id="ws-1", items=[{"func_name": "bash", "needs_approval": True}])
        _run(bot._on_ws_event("ws-1", "C1", "123.456", event))  # type: ignore[attr-defined]

        router.send_approval.assert_awaited_once_with("ws-1", "", approved=True)

    def test_approve_request_sends_approval_buttons(self) -> None:
        from turnstone.sdk.events import ApproveRequestEvent

        bot, client = self._make_ws_bot()

        event = ApproveRequestEvent(ws_id="ws-1", items=[{"func_name": "bash", "needs_approval": True}])
        _run(bot._on_ws_event("ws-1", "C1", "123.456", event))  # type: ignore[attr-defined]

        client.chat_postMessage.assert_awaited_once()
        call_kwargs = client.chat_postMessage.call_args[1]
        assert "blocks" in call_kwargs
        assert "ws-1" in bot._pending_approval_ts  # type: ignore[attr-defined]

    def test_intent_verdict_updates_approval_message(self) -> None:
        from turnstone.sdk.events import IntentVerdictEvent

        bot, client = self._make_ws_bot()
        client.conversations_history = AsyncMock(return_value={
            "ok": True,
            "messages": [{"blocks": []}],
        })

        bot._pending_approval_ts["ws-1"] = ("C1", "999.000")  # type: ignore[attr-defined]

        event = IntentVerdictEvent(
            ws_id="ws-1",
            func_name="bash",
            risk_level="high",
            confidence=0.9,
            intent_summary="Dangerous",
        )
        _run(bot._on_ws_event("ws-1", "C1", "123.456", event))  # type: ignore[attr-defined]

        client.chat_update.assert_awaited_once()

    def test_approval_resolved_clears_pending(self) -> None:
        from turnstone.sdk.events import ApprovalResolvedEvent

        bot, client = self._make_ws_bot()
        bot._pending_approval_ts["ws-1"] = ("C1", "999.000")  # type: ignore[attr-defined]

        event = ApprovalResolvedEvent(ws_id="ws-1", approved=True)
        _run(bot._on_ws_event("ws-1", "C1", "123.456", event))  # type: ignore[attr-defined]

        assert "ws-1" not in bot._pending_approval_ts  # type: ignore[attr-defined]
        client.chat_update.assert_awaited_once()


# ---------------------------------------------------------------------------
# Notification tracking
# ---------------------------------------------------------------------------


class TestNotificationTracking:
    """Tests for notification message tracking."""

    def test_track_notification_stores_entry(self) -> None:
        from turnstone.channels.slack.bot import TurnstoneSlackBot
        from turnstone.channels.slack.config import SlackConfig

        config = SlackConfig(bot_token="xoxb-test", app_token="xapp-test")
        storage = MagicMock()
        with (
            patch("turnstone.channels.slack.bot.AsyncApp", MagicMock()),
            patch("turnstone.channels.slack.bot.AsyncWebClient"),
        ):
            bot = TurnstoneSlackBot(config, server_url="http://localhost:8080", storage=storage)

        bot._track_notification("ts-123", "ws-1", "C01SAPU5414", "U12345")
        assert bot._notify_ws_map["ts-123"] == ("ws-1", "C01SAPU5414", "U12345")

    def test_track_notification_evicts_oldest(self) -> None:
        from turnstone.channels.slack.bot import TurnstoneSlackBot
        from turnstone.channels.slack.config import SlackConfig

        config = SlackConfig(bot_token="xoxb-test", app_token="xapp-test")
        storage = MagicMock()
        with (
            patch("turnstone.channels.slack.bot.AsyncApp", MagicMock()),
            patch("turnstone.channels.slack.bot.AsyncWebClient"),
        ):
            bot = TurnstoneSlackBot(config, server_url="http://localhost:8080", storage=storage)

        bot._MAX_NOTIFY_TRACKING = 3  # type: ignore[attr-defined]
        bot._notify_ws_map = {
            "ts-1": ("ws-1", "C1", "U1"),
            "ts-2": ("ws-2", "C1", "U2"),
            "ts-3": ("ws-3", "C1", "U3"),
        }

        bot._track_notification("ts-4", "ws-4", "C1", "U4")  # type: ignore[attr-defined]

        assert "ts-4" in bot._notify_ws_map  # type: ignore[attr-defined]
        assert "ts-1" not in bot._notify_ws_map  # type: ignore[attr-defined]
        assert len(bot._notify_ws_map) <= 3  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestChannelCLI:
    """Tests for the channel CLI entry point with Slack args."""

    def test_exits_without_adapter_token(self) -> None:
        import sys

        from turnstone.channels.cli import main

        with (
            patch.object(sys, "argv", ["turnstone-channel"]),
            patch.dict("os.environ", {}, clear=True),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1