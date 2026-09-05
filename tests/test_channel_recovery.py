"""Restart recovery across platform adapters, HTTP handlers and durable storage.

Platform delivery and model sessions are fake; routing, authorization, history
cloning and the SSE 404 response use the real implementations.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from tests._helpers import wait_until
from tests.test_server_authz import _auth
from tests.test_server_authz import app_client as app_client
from turnstone.channels._routing import ChannelRouter
from turnstone.core.storage import get_storage
from turnstone.sdk.server import AsyncTurnstoneServer

pytestmark = pytest.mark.filterwarnings(
    "ignore:.*'asyncio.iscoroutinefunction' is deprecated:DeprecationWarning"
)


@pytest.mark.anyio
@pytest.mark.parametrize("platform", ["discord", "slack-thread", "slack-dm"])
@pytest.mark.parametrize("restart", ["node", "bot", "both"])
async def test_restart_retains_association_and_recovers_history(app_client, platform, restart):
    client, manager = app_client
    storage = get_storage()
    assert storage is not None
    source = client.post(
        "/v1/api/workstreams/new", json={"name": "channel"}, headers=_auth("owner")
    ).json()["ws_id"]
    storage.save_message(source, "user", "Remember the original conversation")
    if restart != "bot":
        manager.close(source)
        assert manager.get(source) is None

    if platform == "discord":
        discord = pytest.importorskip("discord")
        from tests.test_channel_discord import _make_message
        from turnstone.channels.discord.bot import TurnstoneBot
        from turnstone.channels.discord.cog import MessageCog
        from turnstone.channels.discord.config import DiscordConfig

        storage.create_channel_user("discord", "111", "owner")
        storage.create_channel_route("discord", "555", source, channel_user_id="111")
        bot = TurnstoneBot(DiscordConfig(allowed_channels=[222]), "http://server.example", storage)
        thread = MagicMock(spec=discord.Thread)
        thread.id, thread.parent_id, thread.owner_id, thread.name = 555, 222, 99999, "conversation"
        thread.send = AsyncMock()
        cog = MessageCog(bot._bot)
        message = _make_message(guild=True, channel=thread)
        message.author.id = 111
        message.content = "Continue please"
        channel_type, route_id = "discord", "555"

        async def inbound(author):
            message.author.id = author
            await cog._on_message(message)

        async def receive_unavailable():
            await bot._sse_listener(source, thread)

    else:
        pytest.importorskip("slack_bolt")
        from turnstone.channels.slack.bot import TurnstoneSlackBot
        from turnstone.channels.slack.config import SlackConfig

        channel = "D1" if platform == "slack-dm" else "C1"
        stamp = "" if platform == "slack-dm" else "1000.000001"
        route_id = f"{channel}:111" + (f":{stamp}" if stamp else "")
        storage.create_channel_user("slack", "111", "owner")
        storage.create_channel_route("slack", route_id, source)
        with (
            patch("turnstone.channels.slack.bot.AsyncApp"),
            patch("turnstone.channels.slack.bot.AsyncWebClient", return_value=AsyncMock()),
        ):
            bot = TurnstoneSlackBot(
                SlackConfig(bot_token="test", allowed_channels=[channel]),
                "http://server.example",
                storage,
            )
        channel_type = "slack"
        if stamp:
            bot._channel_sessions[(channel, "111")] = (source, stamp)

        async def inbound(author):
            await bot._on_message(
                {
                    "channel": channel,
                    "user": str(author),
                    "thread_ts": stamp,
                    "text": "Continue please",
                },
                AsyncMock(),
            )

        async def receive_unavailable():
            await bot._sse_listener(source, route_id)

    await bot.router.aclose()
    await bot._http_client.aclose()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.app),
        base_url="http://server.example",
        headers=_auth("owner"),
    ) as http:
        bot._http_client = http
        bot.router._server = AsyncTurnstoneServer(httpx_client=http)
        # Test startup and inbound subscription requests without opening an
        # infinite ASGI stream. The unavailable response below uses real SSE.
        bot.subscribe_ws = AsyncMock()
        try:
            if restart in {"bot", "both"}:
                if platform == "discord":
                    with (
                        patch.object(bot._bot, "get_channel", return_value=None),
                        patch.object(
                            bot._bot, "fetch_channel", new=AsyncMock(return_value=thread)
                        ) as fetch,
                    ):
                        await bot._recover_routes()
                        if restart == "bot":
                            fetch.assert_awaited_once_with(555)
                        else:
                            fetch.assert_not_awaited()
                else:
                    bot._channel_sessions.clear()
                    await bot._recover_routes()
            bot.subscribe_ws.reset_mock()

            if restart != "bot":
                bot._subscribed_ws.add(source)
                await asyncio.wait_for(receive_unavailable(), timeout=2)
                assert source not in bot._subscribed_ws
                assert storage.get_channel_route(channel_type, route_id)["ws_id"] == source

            # Another linked user must not acquire this existing thread.
            # A Slack DM is a separate user route, so exercise isolation on
            # the thread variants, which share a platform conversation.
            if platform != "slack-dm":
                storage.create_channel_user(channel_type, "222", "other-user")
                await inbound(222)
                assert storage.get_channel_route(channel_type, route_id)["ws_id"] == source
                bot.subscribe_ws.assert_not_awaited()

            await inbound(111)
            route = storage.get_channel_route(channel_type, route_id)
            destination = route["ws_id"]
            assert (destination == source) is (restart == "bot")
            assert storage.get_workstream(source) is not None
            assert (
                storage.load_messages(destination)[0]["content"]
                == "Remember the original conversation"
            )
            if platform == "discord":
                assert route["channel_user_id"] == "111"
                bot.subscribe_ws.assert_awaited_once_with(destination, thread)
            else:
                bot.subscribe_ws.assert_awaited_once_with(destination, route_id)
                if platform == "slack-thread":
                    assert bot._channel_sessions[("C1", "111")] == (destination, stamp)
            session = manager.get(destination).session
            await asyncio.to_thread(wait_until, lambda: len(session.sends) == 1)
            assert session.sends[0][0] == "Continue please"
        finally:
            await bot.router.aclose()
            if platform == "discord":
                await bot._bot.close()
            for ws in manager.list_all():
                manager.close(ws.id)


@pytest.mark.anyio
@pytest.mark.parametrize("existing", [False, True])
async def test_competing_gateways_only_dispatch_after_claiming_route(db, existing):
    if existing:
        db.create_channel_route("discord", "555", "source", channel_user_id="111")
    routers = [ChannelRouter("http://server.example", db) for _ in range(2)]
    barrier = asyncio.Barrier(2)

    async def create(ws_id, **kwargs):
        await barrier.wait()
        return MagicMock(ws_id=ws_id)

    for index, router in enumerate(routers):

        async def create_candidate(_index=index, **kwargs):
            return await create(f"candidate-{_index}", **kwargs)

        router._server.create_workstream = create_candidate
        router._server.send = AsyncMock()
        router.close_workstream = AsyncMock()
    try:
        results = await asyncio.gather(
            *[
                router.get_or_create_workstream(
                    "discord", "555", channel_user_id="111", initial_message="hello"
                )
                for router in routers
            ],
            return_exceptions=True,
        )
        winner = db.get_channel_route("discord", "555")
        assert winner["channel_user_id"] == "111"
        assert sum(isinstance(result, RuntimeError) for result in results) == 1
        for index, (router, result) in enumerate(zip(routers, results, strict=True)):
            if isinstance(result, RuntimeError):
                router.close_workstream.assert_awaited_once_with(f"candidate-{index}")
                router._server.send.assert_not_awaited()
            else:
                assert result == (winner["ws_id"], True)
                router.close_workstream.assert_not_awaited()
                if not existing:
                    router._server.send.assert_awaited_once_with("hello", winner["ws_id"])
    finally:
        for router in routers:
            await router.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("platform", ["discord", "slack"])
async def test_old_listener_cannot_clear_a_new_subscription(platform):
    if platform == "discord":
        pytest.importorskip("discord")
        from tests.test_channel_discord import _make_real_bot

        bot = _make_real_bot()
    else:
        pytest.importorskip("slack_bolt")
        from tests.test_channel_slack import _make_bot

        bot, _, _ = _make_bot()
        bot._channel_sessions[("C1", "111")] = ("new-ws", "1000")

    release = asyncio.Event()

    async def old_listener():
        await release.wait()
        await bot._stop_unavailable_stream("ws")

    old = asyncio.create_task(old_listener())
    replacement = asyncio.create_task(asyncio.Event().wait())
    bot._sse_tasks["ws"] = replacement
    bot._subscribed_ws.add("ws")
    try:
        release.set()
        await old
        assert bot._sse_tasks["ws"] is replacement
        assert "ws" in bot._subscribed_ws
        # Cleanup for a different, old workstream cannot delete a newly
        # recovered thread association either.
        await bot._stop_unavailable_stream("old-ws")
        bot.storage.delete_channel_route.assert_not_called()
        if platform == "slack":
            assert bot._channel_sessions[("C1", "111")] == ("new-ws", "1000")
    finally:
        await bot.unsubscribe_ws("ws")
        await bot.router.aclose()
        await bot._http_client.aclose()
        if platform == "discord":
            await bot._bot.close()


@pytest.mark.anyio
async def test_close_during_fork_cannot_resurrect_route(db):
    db.create_channel_route("discord", "555", "source", channel_user_id="111")
    router = ChannelRouter("http://server.example", db)

    async def create(**kwargs):
        db.delete_channel_route("discord", "555")
        return MagicMock(ws_id="candidate")

    router._server.create_workstream = create
    router._server.send = AsyncMock()
    router.close_workstream = AsyncMock()
    try:
        with pytest.raises(RuntimeError, match="route changed"):
            await router.get_or_create_workstream(
                "discord", "555", channel_user_id="111", require_existing=True
            )
        assert db.get_channel_route("discord", "555") is None
        router.close_workstream.assert_awaited_once_with("candidate")
        router._server.send.assert_not_awaited()
        with pytest.raises(RuntimeError, match="was closed"):
            await router.get_or_create_workstream(
                "discord", "555", channel_user_id="111", require_existing=True
            )
    finally:
        await router.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("status", [403, 404, 503])
async def test_uncached_discord_thread_fetch_failure_preserves_route(db, status):
    discord = pytest.importorskip("discord")
    from tests.test_channel_discord import _make_real_bot

    bot = _make_real_bot()
    bot.router.get_live_workstream_ids = AsyncMock(return_value={"ws"})
    bot.storage = db
    db.create_channel_route("discord", "555", "ws", channel_user_id="111")
    bot.subscribe_ws = AsyncMock()
    try:
        with (
            patch.object(bot._bot, "get_channel", return_value=None),
            patch.object(
                bot._bot,
                "fetch_channel",
                new=AsyncMock(
                    side_effect=discord.HTTPException(
                        MagicMock(status=status, reason="unavailable"),
                        "unavailable",
                    )
                ),
            ),
        ):
            await bot._recover_routes()
        bot.subscribe_ws.assert_not_awaited()
        assert db.get_channel_route("discord", "555")["channel_user_id"] == "111"
    finally:
        await bot.router.aclose()
        await bot._http_client.aclose()
        await bot._bot.close()


@pytest.mark.anyio
async def test_discord_close_serializes_with_pending_reply(db):
    discord = pytest.importorskip("discord")
    from tests.test_channel_discord import (
        TestDiscordThreadOwnerCheck,
        _make_interaction,
        _make_message,
    )

    db.create_channel_route("discord", "555", "source", channel_user_id="111")
    db.create_channel_user("discord", "111", "owner")
    router = ChannelRouter("http://server.example", db)
    cog, ts = TestDiscordThreadOwnerCheck._make_cog_and_ts()
    ts.storage, ts.router = db, router
    ts.unsubscribe_ws = AsyncMock()
    router._server.send = AsyncMock()
    router._server.create_workstream = AsyncMock()
    entered, release = asyncio.Event(), asyncio.Event()

    async def close(ws_id):
        assert ws_id == "source"
        entered.set()
        await release.wait()

    router._server.close_workstream = close
    thread = MagicMock(spec=discord.Thread)
    thread.id, thread.parent_id, thread.owner_id = 555, 222, 99999
    thread.edit = AsyncMock()
    interaction = _make_interaction()
    interaction.user.id, interaction.channel = 111, thread
    message = _make_message(channel=thread)
    message.author.id = 111
    tasks = []
    try:
        tasks.append(asyncio.create_task(cog._cmd_close(interaction)))
        await asyncio.wait_for(entered.wait(), 1)
        tasks.append(asyncio.create_task(cog._on_message(message)))
        await asyncio.sleep(0)
        router._server.create_workstream.assert_not_awaited()
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert db.get_channel_route("discord", "555") is None
        router._server.send.assert_not_awaited()
        ts.subscribe_ws.assert_not_awaited()
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once_with("Workstream closed.")
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await router.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("platform", ["discord", "slack"])
@pytest.mark.parametrize("outcome", ["refused", "replaced"])
async def test_close_failure_preserves_current_route(db, platform, outcome):
    from turnstone.sdk._types import TurnstoneAPIError

    route_id = "555" if platform == "discord" else "C1:111:1000"
    db.create_channel_route(platform, route_id, "source", channel_user_id="111")
    db.create_channel_user(platform, "111", "owner")
    router = ChannelRouter("http://server.example", db)

    async def close(ws_id):
        if outcome == "refused":
            raise TurnstoneAPIError(409, "close refused")
        assert db.replace_channel_route(platform, route_id, "source", "replacement")

    router._server.close_workstream = close
    try:
        if platform == "discord":
            discord = pytest.importorskip("discord")
            from tests.test_channel_discord import TestDiscordThreadOwnerCheck, _make_interaction

            cog, ts = TestDiscordThreadOwnerCheck._make_cog_and_ts()
            ts.storage, ts.router = db, router
            ts.unsubscribe_ws = AsyncMock()
            interaction = _make_interaction()
            interaction.user.id = 111
            interaction.channel = MagicMock(spec=discord.Thread)
            interaction.channel.id = 555
            await cog._cmd_close(interaction)
            ts.unsubscribe_ws.assert_not_awaited()
            assert "Workstream closed." not in interaction.followup.send.call_args.args
        else:
            from tests.test_channel_slack import _make_bot

            bot, _, _ = _make_bot()
            bot.router, bot.storage = router, db
            bot._channel_sessions[("C1", "111")] = ("source", "1000")
            with pytest.raises((TurnstoneAPIError, RuntimeError)):
                await bot._archive_session("C1", "111", "source", "1000")
            cached_ws = "source" if outcome == "refused" else "replacement"
            assert bot._channel_sessions[("C1", "111")] == (cached_ws, "1000")
        expected = "source" if outcome == "refused" else "replacement"
        assert db.get_channel_route(platform, route_id)["ws_id"] == expected
    finally:
        await router.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("fetch_fails", [False, True])
async def test_discord_restores_cached_threads_before_fetching_and_skips_live_streams(fetch_fails):
    discord = pytest.importorskip("discord")
    from tests.test_channel_discord import _make_real_bot

    bot = _make_real_bot()
    bot.router.get_live_workstream_ids = AsyncMock(side_effect=lambda ws_ids: set(ws_ids))
    bot.storage.list_channel_routes_by_type.return_value = [
        {"ws_id": "uncached", "channel_id": "111"},
        {"ws_id": "cached", "channel_id": "222"},
    ]
    cached = MagicMock(spec=discord.Thread)
    cached.id, cached.parent_id = 222, 333
    uncached = MagicMock(spec=discord.Thread)
    uncached.id, uncached.parent_id = 111, 333

    async def fetch(channel_id):
        assert "cached" in bot._subscribed_ws
        if fetch_fails:
            raise TimeoutError("platform unavailable")
        return uncached

    async def listen(*args):
        await asyncio.Event().wait()

    bot._sse_listener = listen
    try:
        with (
            patch.object(
                bot._bot,
                "get_channel",
                side_effect=lambda channel_id: cached if channel_id == 222 else None,
            ),
            patch.object(bot._bot, "fetch_channel", new=AsyncMock(side_effect=fetch)) as fetch_mock,
        ):
            await bot._recover_routes()
            assert "cached" in bot._subscribed_ws
            if not fetch_fails:
                await bot._recover_routes()
                fetch_mock.assert_awaited_once_with(111)
        bot.storage.delete_channel_route.assert_not_called()
    finally:
        await bot.stop()


@pytest.mark.anyio
@pytest.mark.parametrize("cancel_scan", [False, True])
async def test_discord_coalesces_recovery_and_allows_later_retry(cancel_scan):
    pytest.importorskip("discord")
    from tests.test_channel_discord import _make_real_bot

    bot = _make_real_bot()
    bot.router.get_live_workstream_ids = AsyncMock(side_effect=lambda ws_ids: set(ws_ids))
    bot.storage.list_channel_routes_by_type.return_value = [
        {"ws_id": "source", "channel_id": "111"}
    ]
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(channel_id):
        entered.set()
        await release.wait()
        raise TimeoutError("platform unavailable")

    task = None
    try:
        with (
            patch.object(bot._bot, "get_channel", return_value=None),
            patch.object(bot._bot, "fetch_channel", new=AsyncMock(side_effect=fetch)) as fetch_mock,
        ):
            task = asyncio.create_task(bot._recover_routes())
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(asyncio.gather(*(bot._recover_routes() for _ in range(3))), 1)
            fetch_mock.assert_awaited_once_with(111)
            if cancel_scan:
                task.cancel()
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await bot._recover_routes()
            assert fetch_mock.await_count == 2
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await bot.stop()


@pytest.mark.anyio
@pytest.mark.parametrize("replacement_exists", [False, True])
async def test_slack_close_conflict_refreshes_cache_so_next_restart_converges(
    db, replacement_exists
):
    from tests.test_channel_slack import _make_bot

    channel, user, stamp = "C01SAPU5414", "U12345", "1000.000001"
    route_id = f"{channel}:{user}:{stamp}"
    db.create_channel_user("slack", user, "owner")
    db.create_channel_route("slack", route_id, "source")
    bot, _, _ = _make_bot()
    router = ChannelRouter("http://server.example", db)
    bot.router, bot.storage = router, db
    bot._channel_sessions[(channel, user)] = ("source", stamp)
    closed = []

    async def close(ws_id):
        closed.append(ws_id)
        if len(closed) == 1:
            if replacement_exists:
                assert db.replace_channel_route("slack", route_id, "source", "replacement")
            else:
                assert db.delete_channel_route("slack", route_id)

    router._server.close_workstream = close
    router._server.create_workstream = AsyncMock(return_value=MagicMock(ws_id="new-session"))
    try:
        for _ in range(2):
            await bot._on_slash_command(
                AsyncMock(), {"channel_id": channel, "user_id": user, "text": ""}
            )
        assert closed == (["source", "replacement"] if replacement_exists else ["source"])
        router._server.create_workstream.assert_awaited_once()
        assert bot._channel_sessions[(channel, user)][0] == "new-session"
    finally:
        await bot.stop()


@pytest.mark.anyio
async def test_slack_restart_wins_over_pending_recovery():
    from tests.test_channel_slack import _make_bot

    bot, router, client = _make_bot()
    key = ("C01SAPU5414", "U12345")
    old_thread = "1000.000001"
    new_thread = "2000.000001"
    bot._channel_sessions[key] = ("source", old_thread)
    entered_cleanup, finish_cleanup = (asyncio.Event(), asyncio.Event())

    async def old_listener():
        try:
            await asyncio.Event().wait()
        finally:
            entered_cleanup.set()
            await finish_cleanup.wait()

    old = asyncio.create_task(old_listener())
    bot._sse_tasks["source"] = old
    bot._subscribed_ws.add("source")
    await asyncio.sleep(0)
    router.get_or_create_workstream.side_effect = [("recovered", True), ("new-session", True)]
    client.chat_postMessage.return_value = {"ok": True, "ts": new_thread}
    inbound = asyncio.create_task(
        bot._on_message(
            {
                "channel": key[0],
                "user": key[1],
                "thread_ts": old_thread,
                "text": "continue old conversation",
            },
            AsyncMock(),
        )
    )
    try:
        await asyncio.wait_for(entered_cleanup.wait(), 1)
        await bot._on_slash_command(
            AsyncMock(), {"channel_id": key[0], "user_id": key[1], "text": ""}
        )
        finish_cleanup.set()
        await asyncio.wait_for(inbound, 1)
        assert bot._channel_sessions[key] == ("new-session", new_thread)
        router.send_message.assert_not_awaited()
    finally:
        finish_cleanup.set()
        await bot.stop()


@pytest.mark.anyio
async def test_slack_concurrent_replies_share_recovery():
    from tests.test_channel_slack import _make_bot

    bot, _, _ = _make_bot()
    storage = MagicMock()
    route = {"ws_id": "source", "channel_user_id": ""}
    storage.get_channel_route.side_effect = lambda *args: dict(route)
    storage.get_workstream.side_effect = lambda ws_id: {"ws_id": ws_id}

    def replace(channel_type, channel_id, expected, ws_id):
        if route["ws_id"] != expected:
            return False
        route["ws_id"] = ws_id
        return True

    storage.replace_channel_route.side_effect = replace
    live = set()
    router = ChannelRouter("http://server.example", storage)

    async def create(**kwargs):
        live.add("recovered")
        return MagicMock(ws_id="recovered")

    router._server.create_workstream = create
    router._server.list_workstreams = AsyncMock(
        side_effect=lambda: MagicMock(
            workstreams=[MagicMock(ws_id=ws_id, state="idle") for ws_id in live]
        )
    )
    router._server.send = AsyncMock()
    bot.router = router
    bot.subscribe_ws = AsyncMock()
    bot._channel_sessions["C01SAPU5414", "U12345"] = ("source", "1000.000001")
    barrier = asyncio.Barrier(2)

    async def linked(*args):
        await barrier.wait()
        return True

    bot._require_linked = linked
    notices = [AsyncMock(), AsyncMock()]
    try:
        await asyncio.gather(
            *[
                bot._on_message(
                    {
                        "channel": "C01SAPU5414",
                        "user": "U12345",
                        "thread_ts": "1000.000001",
                        "text": f"message {index}",
                    },
                    notices[index],
                )
                for index in range(2)
            ]
        )
        assert router._server.send.await_count == 2
        assert all(notice.await_count == 0 for notice in notices)
    finally:
        await bot.stop()
