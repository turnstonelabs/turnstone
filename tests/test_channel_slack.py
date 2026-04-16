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
        slash_command="/network-help",
    )

    storage = MagicMock()
    storage.list_channel_routes_by_type = MagicMock(return_value=[])

    router = MagicMock()
    router.get_or_create_workstream = AsyncMock(return_value=("ws-1", True))
    router.send_message = AsyncMock()
    router.send_approval = AsyncMock()
    router.send_plan_feedback = AsyncMock()
    router.get_node_url = AsyncMock(return_value="http://localhost:8080")
    router.aclose = AsyncMock()

    client = AsyncMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "1234567890.000100"})
    client.chat_update = AsyncMock(return_value={"ok": True})
    client.chat_postEphemeral = AsyncMock(return_value={"ok": True})
    client.conversations_history = AsyncMock(return_value={"ok": True, "messages": []})
    client.views_open = AsyncMock(return_value={"ok": True})

    # Patch httpx.AsyncClient so each test doesn't open a real client that
    # leaks an unclosed-warning at GC time.  The bot's _http_client is only
    # used by the SDK router (which we replace with a MagicMock below), so
    # an AsyncMock standin is enough for every test that uses this factory.
    with (
        patch("turnstone.channels.slack.bot.AsyncApp", MagicMock()),
        patch("turnstone.channels.slack.bot.AsyncWebClient", return_value=client),
        patch("turnstone.channels.slack.bot.httpx.AsyncClient", return_value=AsyncMock()),
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
        assert cfg.slash_command == "/turnstone"
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
            slash_command="/network-help",
        )
        assert cfg.bot_token == "xoxb-123"
        assert cfg.app_token == "xapp-456"
        assert cfg.allowed_channels == ["C1", "C2"]
        assert cfg.max_message_length == 4000
        assert cfg.streaming_edit_interval == 0.5
        assert cfg.model == "gpt-4.1"
        assert cfg.auto_approve is True
        assert cfg.slash_command == "/network-help"


# ---------------------------------------------------------------------------
# SlackRoute
# ---------------------------------------------------------------------------


class TestSlackRoute:
    """Tests for canonical Slack route parsing/formatting."""

    def test_parse_channel_only(self) -> None:
        from turnstone.channels.slack.routes import SlackRoute

        route = SlackRoute.parse("C123")
        assert route.channel == "C123"
        assert route.user_id is None
        assert route.thread_ts is None

    def test_parse_channel_and_user(self) -> None:
        from turnstone.channels.slack.routes import SlackRoute

        route = SlackRoute.parse("C123:U456")
        assert route.channel == "C123"
        assert route.user_id == "U456"
        assert route.thread_ts is None

    def test_parse_full(self) -> None:
        from turnstone.channels.slack.routes import SlackRoute

        route = SlackRoute.parse("C123:U456:111.222")
        assert route.channel == "C123"
        assert route.user_id == "U456"
        assert route.thread_ts == "111.222"

    def test_to_channel_id(self) -> None:
        from turnstone.channels.slack.routes import SlackRoute

        assert SlackRoute(channel="C123").to_channel_id() == "C123"
        assert SlackRoute(channel="C123", user_id="U456").to_channel_id() == "C123:U456"
        assert (
            SlackRoute(channel="C123", user_id="U456", thread_ts="111.222").to_channel_id()
            == "C123:U456:111.222"
        )


# ---------------------------------------------------------------------------
# Preview sanitization
# ---------------------------------------------------------------------------


class TestPreviewSanitization:
    def test_sanitize_slack_preview_escapes_and_truncates(self) -> None:
        from turnstone.channels.slack.bot import _sanitize_slack_preview

        text = "<@U123>`abc`" + ("x" * 2000)
        out = _sanitize_slack_preview(text, max_length=50)

        # Mention markup is escaped so it can't render as a real ping
        assert "&lt;@U123&gt;" in out
        # Single backticks survive — code-quoted snippets stay readable
        assert "`abc`" in out
        assert len(out) <= 50

    def test_sanitize_slack_preview_neutralizes_triple_backtick(self) -> None:
        """Triple backticks would close the surrounding mrkdwn fence — splice
        a zero-width space inside so Slack no longer recognizes it as a
        delimiter."""
        from turnstone.channels.slack.bot import _sanitize_slack_preview

        out = _sanitize_slack_preview("inner ``` text", max_length=200)
        assert "```" not in out
        assert "``\u200b`" in out

    def test_sanitize_slack_preview_keeps_short_input(self) -> None:
        from turnstone.channels.slack.bot import _sanitize_slack_preview

        out = _sanitize_slack_preview("short and clean", max_length=200)
        assert out == "short and clean"


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
# _on_message filtering and routing
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
        bot._channel_sessions[("C01SAPU5414", "U12345")] = ("ws-1", "9999999.000001")  # type: ignore[attr-defined]

        event = _make_slack_event(thread_ts="1234567890.000100")
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

        event = _make_slack_event(channel="D12345", channel_type="im", thread_ts="")
        say = AsyncMock()

        _run(bot._on_message(event, say))  # type: ignore[attr-defined]

        router.send_message.assert_awaited_once()

    def test_notification_reply_routes_to_origin_workstream(self) -> None:
        from turnstone.channels.slack.routes import SlackRoute

        bot, router, _ = _make_bot()
        ws_id = "ws-123"
        thread_ts = "1776321000.563629"
        bot._notify_ws_map[thread_ts] = (  # type: ignore[attr-defined]
            ws_id,
            SlackRoute(channel="C01SAPU5414", user_id="U12345", thread_ts=thread_ts),
        )

        event = _make_slack_event(
            channel="C01SAPU5414",
            channel_type="channel",
            thread_ts=thread_ts,
            user="U12345",
            text="reply text",
            ts="1776321000.999999",
        )
        say = AsyncMock()

        _run(bot._on_message(event, say))  # type: ignore[attr-defined]

        router.send_message.assert_awaited_once_with(ws_id, "reply text")
        assert bot._notify_reply_routes[ws_id] == SlackRoute(  # type: ignore[attr-defined]
            channel="C01SAPU5414",
            user_id="U12345",
            thread_ts=thread_ts,
        )

    def test_notification_reply_dead_ws_clears_tracking(self) -> None:
        from turnstone.channels.slack.routes import SlackRoute
        from turnstone.sdk._types import TurnstoneAPIError

        bot, router, client = _make_bot()
        ws_id = "ws-dead"
        thread_ts = "1776321102.784939"
        bot._notify_ws_map[thread_ts] = (  # type: ignore[attr-defined]
            ws_id,
            SlackRoute(channel="C01SAPU5414", user_id="U12345", thread_ts=thread_ts),
        )
        router.send_message.side_effect = TurnstoneAPIError(404, "Unknown workstream")

        event = _make_slack_event(
            channel="C01SAPU5414",
            channel_type="channel",
            thread_ts=thread_ts,
            user="U12345",
            text="reply text",
            ts="1776321103.000000",
        )
        say = AsyncMock()

        _run(bot._on_message(event, say))  # type: ignore[attr-defined]

        assert ws_id not in bot._notify_reply_routes  # type: ignore[attr-defined]
        assert thread_ts not in bot._notify_ws_map  # type: ignore[attr-defined]
        client.chat_postEphemeral.assert_awaited_once()


# ---------------------------------------------------------------------------
# Per-user session isolation
# ---------------------------------------------------------------------------


class TestPerUserSessionIsolation:
    """User1 session must not be closed when user2 starts a session."""

    def test_user2_session_does_not_archive_user1(self) -> None:
        bot, router, _client = _make_bot()

        bot._channel_sessions[("C01SAPU5414", "U111")] = ("ws-user1", "1000000.000001")  # type: ignore[attr-defined]
        bot._subscribed_ws.add("ws-user1")  # type: ignore[attr-defined]

        body = {"channel_id": "C01SAPU5414", "user_id": "U222"}
        ack = AsyncMock()

        router.get_or_create_workstream = AsyncMock(return_value=("ws-user2", True))

        _run(bot._on_slash_command(ack, body))  # type: ignore[attr-defined]

        assert ("C01SAPU5414", "U111") in bot._channel_sessions  # type: ignore[attr-defined]
        assert bot._channel_sessions[("C01SAPU5414", "U111")] == ("ws-user1", "1000000.000001")  # type: ignore[attr-defined]

    def test_same_user_second_session_archives_first(self) -> None:
        bot, router, client = _make_bot()

        bot._channel_sessions[("C01SAPU5414", "U111")] = ("ws-old", "1000000.000001")  # type: ignore[attr-defined]
        bot._subscribed_ws.add("ws-old")  # type: ignore[attr-defined]

        body = {"channel_id": "C01SAPU5414", "user_id": "U111"}
        ack = AsyncMock()

        router.get_or_create_workstream = AsyncMock(return_value=("ws-new", True))

        _run(bot._on_slash_command(ack, body))  # type: ignore[attr-defined]

        assert bot._channel_sessions.get(("C01SAPU5414", "U111")) is not None  # type: ignore[attr-defined]
        ws_id, _ = bot._channel_sessions[("C01SAPU5414", "U111")]  # type: ignore[attr-defined]
        assert ws_id == "ws-new"

        client.chat_postMessage.assert_awaited()
        calls = [str(c) for c in client.chat_postMessage.call_args_list]
        assert any("archived" in c for c in calls)


# ---------------------------------------------------------------------------
# Approval ownership
# ---------------------------------------------------------------------------


class TestApprovalOwnership:
    def test_non_owner_cannot_approve(self) -> None:
        from turnstone.channels.slack.bot import PendingApproval

        bot, router, client = _make_bot()
        ws_id = "ws-1"
        bot._pending_approval[ws_id] = PendingApproval(  # type: ignore[attr-defined]
            channel="C01SAPU5414",
            message_ts="111.222",
            owner_user_id="U_OWNER",
        )

        body = {
            "actions": [{"value": f"{ws_id}|corr-1"}],
            "user": {"id": "U_OTHER"},
            "container": {"channel_id": "C01SAPU5414", "message_ts": "111.222"},
        }

        _run(bot._on_approve(AsyncMock(), body))  # type: ignore[attr-defined]

        client.chat_postEphemeral.assert_awaited_once()
        router.send_approval.assert_not_awaited()

    def test_non_owner_cannot_deny(self) -> None:
        from turnstone.channels.slack.bot import PendingApproval

        bot, router, client = _make_bot()
        ws_id = "ws-1"
        bot._pending_approval[ws_id] = PendingApproval(  # type: ignore[attr-defined]
            channel="C01SAPU5414",
            message_ts="111.222",
            owner_user_id="U_OWNER",
        )

        body = {
            "actions": [{"value": f"{ws_id}|corr-1"}],
            "user": {"id": "U_OTHER"},
            "container": {"channel_id": "C01SAPU5414", "message_ts": "111.222"},
        }

        _run(bot._on_deny(AsyncMock(), body))  # type: ignore[attr-defined]

        client.chat_postEphemeral.assert_awaited_once()
        router.send_approval.assert_not_awaited()

    def test_owner_can_approve(self) -> None:
        from turnstone.channels.slack.bot import PendingApproval

        bot, router, client = _make_bot()
        ws_id = "ws-1"
        bot._pending_approval[ws_id] = PendingApproval(  # type: ignore[attr-defined]
            channel="C01SAPU5414",
            message_ts="111.222",
            owner_user_id="U_OWNER",
        )

        body = {
            "actions": [{"value": f"{ws_id}|corr-1"}],
            "user": {"id": "U_OWNER"},
            "container": {"channel_id": "C01SAPU5414", "message_ts": "111.222"},
        }

        _run(bot._on_approve(AsyncMock(), body))  # type: ignore[attr-defined]

        router.send_approval.assert_awaited_once_with(ws_id, "corr-1", approved=True)
        client.chat_update.assert_awaited_once()


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
        router.send_plan_feedback = AsyncMock()
        client = AsyncMock()
        client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "123"})
        client.chat_update = AsyncMock(return_value={"ok": True})
        client.conversations_history = AsyncMock(return_value={"ok": True, "messages": []})

        with (
            patch("turnstone.channels.slack.bot.AsyncApp", MagicMock()),
            patch("turnstone.channels.slack.bot.AsyncWebClient", return_value=client),
            patch(
                "turnstone.channels.slack.bot.httpx.AsyncClient",
                return_value=AsyncMock(),
            ),
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
        from turnstone.channels.slack.routes import SlackRoute
        from turnstone.sdk.events import ContentEvent

        bot, _client = self._make_ws_bot()

        event = ContentEvent(ws_id="ws-1", text="Hello")
        route = SlackRoute(channel="C1", user_id="U1", thread_ts="123.456")
        _run(bot._on_ws_event("ws-1", route, event))  # type: ignore[attr-defined]

        assert "ws-1" in bot._streaming  # type: ignore[attr-defined]

    def test_stream_end_finalizes_streaming(self) -> None:
        from turnstone.channels.slack.routes import SlackRoute
        from turnstone.sdk.events import ContentEvent, StreamEndEvent

        bot, _client = self._make_ws_bot()
        route = SlackRoute(channel="C1", user_id="U1", thread_ts="123.456")

        _run(bot._on_ws_event("ws-1", route, ContentEvent(ws_id="ws-1", text="Hi")))  # type: ignore[attr-defined]
        assert "ws-1" in bot._streaming  # type: ignore[attr-defined]

        _run(bot._on_ws_event("ws-1", route, StreamEndEvent(ws_id="ws-1")))  # type: ignore[attr-defined]
        assert "ws-1" not in bot._streaming  # type: ignore[attr-defined]

    def test_stream_end_no_streaming_is_noop(self) -> None:
        from turnstone.channels.slack.routes import SlackRoute
        from turnstone.sdk.events import StreamEndEvent

        bot, _client = self._make_ws_bot()
        route = SlackRoute(channel="C1", user_id="U1", thread_ts="123.456")

        _run(bot._on_ws_event("ws-1", route, StreamEndEvent(ws_id="ws-1")))  # type: ignore[attr-defined]
        assert "ws-1" not in bot._streaming  # type: ignore[attr-defined]

    def test_error_event_posts_message(self) -> None:
        from turnstone.channels.slack.routes import SlackRoute
        from turnstone.sdk.events import ErrorEvent

        bot, client = self._make_ws_bot()
        route = SlackRoute(channel="C1", user_id="U1", thread_ts="123.456")

        event = ErrorEvent(ws_id="ws-1", message="Something went wrong")
        _run(bot._on_ws_event("ws-1", route, event))  # type: ignore[attr-defined]

        client.chat_postMessage.assert_awaited_once()
        text = client.chat_postMessage.call_args[1]["text"]
        assert "Something went wrong" in text

    def test_approve_request_auto_approve(self) -> None:
        from turnstone.channels.slack.bot import TurnstoneSlackBot
        from turnstone.channels.slack.config import SlackConfig
        from turnstone.channels.slack.routes import SlackRoute
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
            patch(
                "turnstone.channels.slack.bot.httpx.AsyncClient",
                return_value=AsyncMock(),
            ),
        ):
            bot = TurnstoneSlackBot(config, server_url="http://localhost:8080", storage=storage)
        bot.router = router  # type: ignore[attr-defined]
        bot._client = client  # type: ignore[attr-defined]
        bot.storage = None  # type: ignore[attr-defined]

        event = ApproveRequestEvent(
            ws_id="ws-1", items=[{"func_name": "bash", "needs_approval": True}]
        )
        route = SlackRoute(channel="C1", user_id="U1", thread_ts="123.456")
        _run(bot._on_ws_event("ws-1", route, event))  # type: ignore[attr-defined]

        router.send_approval.assert_awaited_once_with("ws-1", "", approved=True)

    def test_approve_request_sends_approval_buttons(self) -> None:
        from turnstone.channels.slack.routes import SlackRoute
        from turnstone.sdk.events import ApproveRequestEvent

        bot, client = self._make_ws_bot()

        event = ApproveRequestEvent(
            ws_id="ws-1", items=[{"func_name": "bash", "needs_approval": True}]
        )
        route = SlackRoute(channel="C1", user_id="U12345", thread_ts="123.456")
        _run(bot._on_ws_event("ws-1", route, event))  # type: ignore[attr-defined]

        client.chat_postMessage.assert_awaited_once()
        call_kwargs = client.chat_postMessage.call_args[1]
        assert "blocks" in call_kwargs
        assert "ws-1" in bot._pending_approval  # type: ignore[attr-defined]
        assert bot._pending_approval["ws-1"].owner_user_id == "U12345"  # type: ignore[attr-defined]

    def test_intent_verdict_updates_approval_message(self) -> None:
        from turnstone.channels.slack.bot import PendingApproval
        from turnstone.channels.slack.routes import SlackRoute
        from turnstone.sdk.events import IntentVerdictEvent

        bot, client = self._make_ws_bot()
        client.conversations_history = AsyncMock(
            return_value={"ok": True, "messages": [{"blocks": []}]}
        )

        bot._pending_approval["ws-1"] = PendingApproval(  # type: ignore[attr-defined]
            channel="C1",
            message_ts="999.000",
            owner_user_id="U12345",
        )

        event = IntentVerdictEvent(
            ws_id="ws-1",
            func_name="bash",
            risk_level="high",
            confidence=0.9,
            intent_summary="Dangerous",
        )
        route = SlackRoute(channel="C1", user_id="U12345", thread_ts="123.456")
        _run(bot._on_ws_event("ws-1", route, event))  # type: ignore[attr-defined]

        client.chat_update.assert_awaited_once()

    def test_approval_resolved_clears_pending(self) -> None:
        from turnstone.channels.slack.bot import PendingApproval
        from turnstone.channels.slack.routes import SlackRoute
        from turnstone.sdk.events import ApprovalResolvedEvent

        bot, client = self._make_ws_bot()
        bot._pending_approval["ws-1"] = PendingApproval(  # type: ignore[attr-defined]
            channel="C1",
            message_ts="999.000",
            owner_user_id="U12345",
        )

        event = ApprovalResolvedEvent(ws_id="ws-1", approved=True)
        route = SlackRoute(channel="C1", user_id="U12345", thread_ts="123.456")
        _run(bot._on_ws_event("ws-1", route, event))  # type: ignore[attr-defined]

        assert "ws-1" not in bot._pending_approval  # type: ignore[attr-defined]
        client.chat_update.assert_awaited_once()

    def test_plan_review_event_posts_buttons(self) -> None:
        from turnstone.channels.slack.routes import SlackRoute
        from turnstone.sdk.events import PlanReviewEvent

        bot, client = self._make_ws_bot()
        route = SlackRoute(channel="C1", user_id="U12345", thread_ts="123.456")

        event = PlanReviewEvent(ws_id="ws-1", content="1. do thing\n2. do next thing")
        _run(bot._on_ws_event("ws-1", route, event))  # type: ignore[attr-defined]

        client.chat_postMessage.assert_awaited_once()
        kwargs = client.chat_postMessage.call_args[1]
        assert kwargs["text"] == "Plan review required"
        assert "blocks" in kwargs
        assert "ws-1" in bot._pending_plan_review_ts  # type: ignore[attr-defined]

    def test_plan_approve_sends_feedback_and_updates_message(self) -> None:
        bot, client = self._make_ws_bot()
        body = {
            "actions": [{"value": "ws-1"}],
            "container": {"channel_id": "C1", "message_ts": "111.222"},
        }

        _run(bot._on_plan_approve(AsyncMock(), body))  # type: ignore[attr-defined]

        bot.router.send_plan_feedback.assert_awaited_once_with("ws-1", "", "")  # type: ignore[attr-defined]
        client.chat_update.assert_awaited_once()

    def test_plan_feedback_modal_sends_feedback_and_updates_message(self) -> None:
        bot, client = self._make_ws_bot()
        bot._pending_plan_review_ts["ws-1"] = ("C1", "111.222")  # type: ignore[attr-defined]

        view = {
            "private_metadata": "ws-1",
            "state": {
                "values": {"feedback_block": {"feedback_input": {"value": "please revise step 2"}}}
            },
        }

        _run(bot._on_plan_feedback_modal(AsyncMock(), {}, view))  # type: ignore[attr-defined]

        bot.router.send_plan_feedback.assert_awaited_once_with(  # type: ignore[attr-defined]
            "ws-1",
            "",
            "please revise step 2",
        )
        client.chat_update.assert_awaited_once()


# ---------------------------------------------------------------------------
# Notification tracking
# ---------------------------------------------------------------------------


class TestNotificationTracking:
    """Tests for notification message tracking."""

    def test_track_notification_stores_entry(self) -> None:
        from turnstone.channels.slack.bot import TurnstoneSlackBot
        from turnstone.channels.slack.config import SlackConfig
        from turnstone.channels.slack.routes import SlackRoute

        config = SlackConfig(bot_token="xoxb-test", app_token="xapp-test")
        storage = MagicMock()
        with (
            patch("turnstone.channels.slack.bot.AsyncApp", MagicMock()),
            patch("turnstone.channels.slack.bot.AsyncWebClient"),
            patch(
                "turnstone.channels.slack.bot.httpx.AsyncClient",
                return_value=AsyncMock(),
            ),
        ):
            bot = TurnstoneSlackBot(config, server_url="http://localhost:8080", storage=storage)

        route = SlackRoute(channel="C01SAPU5414", user_id="U12345", thread_ts="ts-123")
        bot._track_notification("ts-123", "ws-1", route)  # type: ignore[attr-defined]
        assert bot._notify_ws_map["ts-123"] == ("ws-1", route)  # type: ignore[attr-defined]

    def test_track_notification_evicts_oldest(self) -> None:
        from turnstone.channels.slack.bot import TurnstoneSlackBot
        from turnstone.channels.slack.config import SlackConfig
        from turnstone.channels.slack.routes import SlackRoute

        config = SlackConfig(bot_token="xoxb-test", app_token="xapp-test")
        storage = MagicMock()
        with (
            patch("turnstone.channels.slack.bot.AsyncApp", MagicMock()),
            patch("turnstone.channels.slack.bot.AsyncWebClient"),
            patch(
                "turnstone.channels.slack.bot.httpx.AsyncClient",
                return_value=AsyncMock(),
            ),
        ):
            bot = TurnstoneSlackBot(config, server_url="http://localhost:8080", storage=storage)

        bot._MAX_NOTIFY_TRACKING = 3  # type: ignore[attr-defined]
        bot._notify_ws_map = {  # type: ignore[attr-defined]
            "ts-1": ("ws-1", SlackRoute(channel="C1", user_id="U1", thread_ts="ts-1")),
            "ts-2": ("ws-2", SlackRoute(channel="C1", user_id="U2", thread_ts="ts-2")),
            "ts-3": ("ws-3", SlackRoute(channel="C1", user_id="U3", thread_ts="ts-3")),
        }

        bot._track_notification(  # type: ignore[attr-defined]
            "ts-4",
            "ws-4",
            SlackRoute(channel="C1", user_id="U4", thread_ts="ts-4"),
        )

        assert "ts-4" in bot._notify_ws_map  # type: ignore[attr-defined]
        assert "ts-1" not in bot._notify_ws_map  # type: ignore[attr-defined]
        assert len(bot._notify_ws_map) <= 3  # type: ignore[attr-defined]

    def test_send_notification_tracks_root_thread_ts(self) -> None:
        from turnstone.channels.slack.routes import SlackRoute

        bot, _router, client = _make_bot()
        client.chat_postMessage = AsyncMock(
            side_effect=[
                {"ok": True, "ts": "111.222"},
                {"ok": True, "ts": "111.333"},
            ]
        )

        msg_id = _run(
            bot.send_notification(  # type: ignore[attr-defined]
                "C01SAPU5414:U12345",
                "x" * 5000,
                "ws-1",
            )
        )

        assert msg_id == "111.222"
        assert "111.222" in bot._notify_ws_map  # type: ignore[attr-defined]
        stored_ws, stored_route = bot._notify_ws_map["111.222"]  # type: ignore[attr-defined]
        assert stored_ws == "ws-1"
        assert stored_route == SlackRoute(
            channel="C01SAPU5414",
            user_id="U12345",
            thread_ts="111.222",
        )


# ---------------------------------------------------------------------------
# DM continuity
# ---------------------------------------------------------------------------


class TestDmContinuity:
    def test_dm_messages_reuse_same_workstream(self) -> None:
        bot, router, _client = _make_bot()
        router.get_or_create_workstream = AsyncMock(side_effect=[("ws-dm", True), ("ws-dm", False)])

        event1 = _make_slack_event(
            channel="D12345",
            channel_type="im",
            thread_ts="",
            ts="1.000",
            text="hello",
        )
        event2 = _make_slack_event(
            channel="D12345",
            channel_type="im",
            thread_ts="1.000",
            ts="1.111",
            text="again",
        )

        _run(bot._handle_dm(event1, AsyncMock()))  # type: ignore[attr-defined]
        _run(bot._handle_dm(event2, AsyncMock()))  # type: ignore[attr-defined]

        assert router.get_or_create_workstream.await_count == 2
        assert router.send_message.await_count == 2


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

    def test_slack_requires_both_tokens(self) -> None:
        import sys

        from turnstone.channels.cli import main

        with (
            patch.object(
                sys,
                "argv",
                [
                    "turnstone-channel",
                    "--slack-token",
                    "xoxb-test",
                    "--server-url",
                    "http://localhost:8080",
                ],
            ),
            patch.dict("os.environ", {}, clear=True),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert "--slack-token and --slack-app-token must be provided together" in str(
            exc_info.value
        )

    def test_slack_only_startup_creates_channel_app(self) -> None:
        import sys

        from turnstone.channels.cli import main

        created_adapters: dict[str, object] = {}

        class FakeSlackBot:
            channel_type = "slack"

            def __init__(self, *args, **kwargs) -> None:
                pass

            async def start(self) -> None:
                return None

        class FakeServer:
            def __init__(self, _config) -> None:
                pass

            async def serve(self) -> None:
                return None

        def _fake_create_channel_app(adapters, storage, *, jwt_secret=""):  # type: ignore[no-untyped-def]
            created_adapters.update(adapters)
            return MagicMock()

        async def _fake_gather(*aws):  # type: ignore[no-untyped-def]
            for aw in aws:
                await aw
            return []

        storage = MagicMock()
        storage.register_service = MagicMock()
        storage.deregister_service = MagicMock()

        with (
            patch.object(
                sys,
                "argv",
                [
                    "turnstone-channel",
                    "--slack-token",
                    "xoxb-test",
                    "--slack-app-token",
                    "xapp-test",
                    "--server-url",
                    "http://localhost:8080",
                ],
            ),
            patch("turnstone.core.storage._registry.init_storage"),
            patch("turnstone.core.storage._registry.get_storage", return_value=storage),
            patch(
                "turnstone.channels._http.create_channel_app", side_effect=_fake_create_channel_app
            ),
            patch("turnstone.channels._http._get_service_id", return_value="channel-test"),
            patch("asyncio.gather", side_effect=_fake_gather),
            patch("uvicorn.Config", return_value=MagicMock()),
            patch("uvicorn.Server", FakeServer),
            patch("turnstone.channels.slack.bot.TurnstoneSlackBot", FakeSlackBot),
        ):
            main()

        assert "slack" in created_adapters
        assert len(created_adapters) == 1

    def test_discord_and_slack_startup_creates_both_adapters(self) -> None:
        import sys

        from turnstone.channels.cli import main

        created_adapters: dict[str, object] = {}

        class FakeSlackBot:
            channel_type = "slack"

            def __init__(self, *args, **kwargs) -> None:
                pass

            async def start(self) -> None:
                return None

        class FakeDiscordBot:
            channel_type = "discord"

            def __init__(self, *args, **kwargs) -> None:
                pass

            async def start(self) -> None:
                return None

        class FakeServer:
            def __init__(self, _config) -> None:
                pass

            async def serve(self) -> None:
                return None

        def _fake_create_channel_app(adapters, storage, *, jwt_secret=""):  # type: ignore[no-untyped-def]
            created_adapters.update(adapters)
            return MagicMock()

        async def _fake_gather(*aws):  # type: ignore[no-untyped-def]
            for aw in aws:
                await aw
            return []

        storage = MagicMock()
        storage.register_service = MagicMock()
        storage.deregister_service = MagicMock()

        with (
            patch.object(
                sys,
                "argv",
                [
                    "turnstone-channel",
                    "--discord-token",
                    "discord-test",
                    "--slack-token",
                    "xoxb-test",
                    "--slack-app-token",
                    "xapp-test",
                    "--server-url",
                    "http://localhost:8080",
                ],
            ),
            patch("turnstone.core.storage._registry.init_storage"),
            patch("turnstone.core.storage._registry.get_storage", return_value=storage),
            patch(
                "turnstone.channels._http.create_channel_app", side_effect=_fake_create_channel_app
            ),
            patch("turnstone.channels._http._get_service_id", return_value="channel-test"),
            patch("asyncio.gather", side_effect=_fake_gather),
            patch("uvicorn.Config", return_value=MagicMock()),
            patch("uvicorn.Server", FakeServer),
            patch("turnstone.channels.slack.bot.TurnstoneSlackBot", FakeSlackBot),
            patch("turnstone.channels.discord.bot.TurnstoneBot", FakeDiscordBot),
        ):
            main()

        assert "discord" in created_adapters
        assert "slack" in created_adapters
        assert len(created_adapters) == 2
