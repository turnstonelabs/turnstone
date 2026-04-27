"""Slack bot adapter — connects Slack channels/threads to turnstone workstreams.

:class:`TurnstoneSlackBot` uses Slack Bolt with Socket Mode so no public URL
or API Gateway is required. Mirrors the Discord adapter pattern.

Interaction model
-----------------
* **Channels**: invoke with the configured Slack slash command. The bot replies
  in a thread and waits for messages there. Messages outside a bot-created
  thread are ignored.
* Running the slash command again in the same channel (by the same user)
  archives the old workstream and starts a fresh thread.
* **DMs**: every message is routed freely, no slash command needed.

Events are consumed from the server's per-workstream SSE endpoint
(``GET /v1/api/workstreams/{ws_id}/events``) using httpx-sse. Inbound
messages are sent directly to server nodes via HTTP
(``POST /v1/api/workstreams/{ws_id}/send``).

Install dependencies:
    pip install slack-bolt httpx httpx-sse
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import httpx
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from turnstone.channels._config import MAX_NOTIFY_TRACKING
from turnstone.channels._formatter import chunk_message
from turnstone.channels._routing import ChannelRouter
from turnstone.channels._sse import run_sse_stream
from turnstone.channels.slack.routes import SlackRoute
from turnstone.core.log import get_logger
from turnstone.sdk._types import TurnstoneAPIError
from turnstone.sdk.events import (
    ApprovalResolvedEvent,
    ApproveRequestEvent,
    ContentEvent,
    ErrorEvent,
    IntentVerdictEvent,
    PlanReviewEvent,
    ServerEvent,
    StreamEndEvent,
    ThinkingStartEvent,
    ThinkingStopEvent,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    from turnstone.channels.slack.config import SlackConfig
    from turnstone.core.storage._protocol import StorageBackend

log = get_logger(__name__)

_GREETING = "Hey! Let me know what I can help with."

# Slack section.text caps at 3000 chars.  Reserve headroom for the heading
# and the "+N more" suffix; cap each preview individually so a single huge
# tool can't crowd out the rest of a multi-tool batch.
_APPROVAL_TEXT_BUDGET: int = 2700
_APPROVAL_PER_ITEM_PREVIEW: int = 600

# Inbound-message limits — one user can't exhaust the LLM budget on
# their own.
_PER_USER_RATE_WINDOW_S: float = 60.0
_PER_USER_RATE_LIMIT: int = 10
_PER_USER_RATE_CAP: int = 4096  # LRU bound on the per-user deque map
_MAX_INBOUND_MESSAGE_LEN: int = 8192  # chars; roughly 8 KiB

# /turnstone link has a much tighter throttle than regular messages so
# throw-away Slack accounts can't online-enumerate Turnstone API tokens
# (mirrors Discord's /link cap in discord/cog.py).
_LINK_RATE_WINDOW_S: float = 3600.0
_LINK_RATE_LIMIT: int = 5
_LINK_RATE_CAP: int = 2048


def _sanitize_slack_preview(text: str, max_length: int = 1200) -> str:
    """Escape Slack mrkdwn-sensitive content for safe fenced display.

    Targets the *closing-fence* injection vector: any literal ```\
    inside the content would terminate the surrounding mrkdwn fence and
    let the rest of the preview render as live markup (mentions, links,
    etc.).  We splice a zero-width space inside the triple sequence so
    Slack no longer recognizes it as a fence delimiter, and escape
    ``&<>`` for good measure.  Single backticks are kept intact so code
    snippets in plans / tool previews remain readable.
    """
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("```", "``\u200b`")
    if len(text) > max_length:
        return text[: max_length - 3] + "..."
    return text


def _parse_ts(ts: str) -> tuple[int, int]:
    """Parse a Slack ``"seconds.microseconds"`` timestamp into ordered parts.

    Pads (or truncates) the fractional field to 6 digits before converting
    so that e.g. ``"1.2"`` and ``"1.000002"`` compare correctly — ``int``
    strips leading zeros and otherwise both collapse to ``(1, 2)``.
    """
    seconds_str, _sep, frac_str = ts.partition(".")
    seconds = int(seconds_str)
    micros = int(frac_str.ljust(6, "0")[:6]) if frac_str else 0
    return (seconds, micros)


@dataclass(frozen=True)
class PendingApproval:
    channel: str
    message_ts: str
    owner_user_id: str | None = None
    # Block Kit payload posted to Slack so IntentVerdictEvent can edit
    # the message without an extra conversations_history fetch.  A
    # mutable list so repeat IntentVerdictEvents stack on the same
    # approval message (matches the pre-refactor "fetch live blocks
    # and append" behavior).
    blocks: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class StreamingMessage:
    """Accumulates streamed content and periodically edits a Slack message."""

    client: Any
    channel: str
    thread_ts: str = ""
    max_length: int = 3000
    edit_interval: float = 1.5

    _ts: str = field(default="", init=False, repr=False)
    _buffer: list[str] = field(default_factory=list, init=False, repr=False)
    # Rolling truncated in-progress display string; stops growing at
    # max_length so per-flush cost is O(max_length) instead of
    # O(total_streamed_chars).
    _display: str = field(default="", init=False, repr=False)
    _last_edit: float = field(default=0.0, init=False, repr=False)
    _finalized_text: str | None = field(default=None, init=False, repr=False)

    @property
    def message_ts(self) -> str:
        """Slack ``ts`` of the initial posted message, or ``""`` if not yet sent."""
        return self._ts

    @property
    def accumulated_text(self) -> str:
        """The joined text of everything appended so far.

        Cached after ``finalize()`` so downstream callers that read the
        full message content don't re-join the buffer twice.
        """
        if self._finalized_text is not None:
            return self._finalized_text
        return "".join(self._buffer)

    async def append(self, text: str) -> None:
        self._buffer.append(text)
        if len(self._display) < self.max_length:
            self._display = (self._display + text)[: self.max_length]
        if time.monotonic() - self._last_edit >= self.edit_interval:
            await self._flush()

    async def finalize(self) -> None:
        content = "".join(self._buffer)
        self._finalized_text = content
        if not content:
            return

        chunks = chunk_message(content, self.max_length)
        if self._ts:
            try:
                await self.client.chat_update(
                    channel=self.channel,
                    ts=self._ts,
                    text=chunks[0],
                )
            except Exception:
                log.debug("slack.streaming_message.finalize_edit_failed", exc_info=True)

            for chunk in chunks[1:]:
                await self.client.chat_postMessage(
                    channel=self.channel,
                    thread_ts=self.thread_ts or self._ts,
                    text=chunk,
                )
        else:
            for chunk in chunks:
                # After the first chunk posts successfully, reuse its ts as
                # the thread parent so the remaining chunks group into one
                # thread instead of fragmenting as independent top-level
                # messages (Slack DMs otherwise inherit thread_ts="").
                resp = await self.client.chat_postMessage(
                    channel=self.channel,
                    thread_ts=self.thread_ts or self._ts or None,
                    text=chunk,
                )
                if not self._ts and resp.get("ok"):
                    self._ts = resp["ts"]

    async def _flush(self) -> None:
        display = self._display
        if not display:
            return

        try:
            if not self._ts:
                resp = await self.client.chat_postMessage(
                    channel=self.channel,
                    thread_ts=self.thread_ts or None,
                    text=display,
                )
                if resp.get("ok"):
                    self._ts = resp["ts"]
            else:
                await self.client.chat_update(
                    channel=self.channel,
                    ts=self._ts,
                    text=display,
                )
        except Exception:
            log.debug("slack.streaming_message.flush_failed", exc_info=True)

        self._last_edit = time.monotonic()


class TurnstoneSlackBot:
    """Slack bot bridging Slack channels/threads to turnstone workstreams."""

    channel_type: str = "slack"
    _MAX_NOTIFY_TRACKING: int = MAX_NOTIFY_TRACKING

    def __init__(
        self,
        config: SlackConfig,
        server_url: str,
        storage: StorageBackend,
        *,
        api_token: str = "",
        console_url: str = "",
        console_token_factory: Callable[[], str] | None = None,
        server_token_factory: Callable[[], str] | None = None,
    ) -> None:
        self.config = config
        self._server_url = server_url.rstrip("/")
        self._console_url = console_url.rstrip("/") if console_url else ""
        self._api_token = api_token
        self._token_factory = server_token_factory
        self.storage = storage

        self.router = ChannelRouter(
            server_url,
            storage,
            auto_approve=config.auto_approve,
            auto_approve_tools=list(config.auto_approve_tools),
            skill=config.skill,
            api_token=api_token,
            console_url=console_url,
            console_token_factory=console_token_factory,
            server_token_factory=server_token_factory,
        )

        self._subscribed_ws: set[str] = set()
        self._sse_tasks: dict[str, asyncio.Task[None]] = {}
        self._streaming: dict[str, StreamingMessage] = {}

        self._pending_approval: dict[str, PendingApproval] = {}
        # (channel, message_ts, owner_user_id) per pending plan review, so
        # _on_plan_* handlers can reject clicks from non-owners.
        self._pending_plan_review_ts: dict[str, tuple[str, str, str]] = {}
        self._notify_ws_map: dict[str, tuple[str, SlackRoute]] = {}
        # Per-workstream override used to route the next streamed assistant
        # response back into a Slack notification reply thread instead of the
        # default session thread.
        self._notify_reply_routes: dict[str, SlackRoute] = {}
        self._channel_sessions: dict[tuple[str, str], tuple[str, str]] = {}
        # Per-user sliding-window rate limit; maps slack_user_id → deque
        # of recent allow timestamps within _PER_USER_RATE_WINDOW_S.
        self._rate_buckets: OrderedDict[str, deque[float]] = OrderedDict()
        # Separate sliding-window throttle for /turnstone link so an
        # abusive user can't enumerate tokens while still being allowed
        # to chat at the baseline message rate.
        self._link_buckets: OrderedDict[str, deque[float]] = OrderedDict()

        headers: dict[str, str] = {}
        if api_token and not server_token_factory:
            headers["Authorization"] = f"Bearer {api_token}"

        self._http_client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(connect=10.0, read=90.0, write=10.0, pool=10.0),
        )

        self._app = AsyncApp(token=config.bot_token)
        self._client = AsyncWebClient(token=config.bot_token)
        self._app_token = config.app_token
        self._handler: AsyncSocketModeHandler | None = None

        self._app.command(config.slash_command)(self._on_slash_command)
        self._app.event("message")(self._on_message)

        async def _approve_cb(ack: Any, body: dict[str, Any]) -> None:
            await self._resolve_approval(ack, body, approved=True)

        async def _deny_cb(ack: Any, body: dict[str, Any]) -> None:
            await self._resolve_approval(ack, body, approved=False)

        self._app.action("ts_approve")(_approve_cb)
        self._app.action("ts_deny")(_deny_cb)
        self._app.action("ts_plan_approve")(self._on_plan_approve)
        self._app.action("ts_plan_request_changes")(self._on_plan_request_changes)
        self._app.view("ts_plan_feedback_modal")(self._on_plan_feedback_modal)

    async def start(self) -> None:
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        await self._recover_routes()
        log.info("slack.starting_socket_mode")
        await self._handler.start_async()  # type: ignore[no-untyped-call]

    async def stop(self) -> None:
        for ws_id in list(self._subscribed_ws):
            await self.unsubscribe_ws(ws_id)
        await self.router.aclose()
        await self._http_client.aclose()
        if self._handler is not None:
            await self._handler.close_async()  # type: ignore[no-untyped-call]
        log.info("slack.stopped")

    async def _recover_routes(self) -> None:
        """Re-subscribe to SSE streams for existing slack routes."""
        routes = await asyncio.to_thread(
            self.storage.list_channel_routes_by_type,
            "slack",
        )

        latest_sessions: dict[tuple[str, str], tuple[str, str]] = {}

        for route in routes:
            ws_id = route["ws_id"]
            channel_id = route["channel_id"]

            await self.subscribe_ws(ws_id, channel_id)

            slack_route = SlackRoute.parse(channel_id)
            if slack_route.channel and slack_route.user_id and slack_route.thread_ts:
                key = (slack_route.channel, slack_route.user_id)
                existing = latest_sessions.get(key)

                if existing is None or _parse_ts(slack_route.thread_ts) > _parse_ts(existing[1]):
                    latest_sessions[key] = (ws_id, slack_route.thread_ts)

            log.info("slack.route_recovered", ws_id=ws_id, channel_id=channel_id)

        self._channel_sessions = latest_sessions

        for (slack_channel, user_id), (ws_id, thread_ts) in self._channel_sessions.items():
            log.info(
                "slack.session_recovered",
                slack_channel=slack_channel,
                user_id=user_id,
                ws_id=ws_id,
                thread_ts=thread_ts,
            )

    async def _on_slash_command(self, ack: Any, body: dict[str, Any]) -> None:
        """Start or restart a per-user session in this channel.

        Subcommands (parsed from the slash-command ``text`` field):

        - ``link <api_token>``: link the caller's Slack user ID to the
          Turnstone account that owns *api_token*.
        - ``unlink``: remove the link.
        - (default): open or restart a Turnstone session in this channel.
        """
        await ack()

        slack_channel = body["channel_id"]
        user_id = body["user_id"]
        raw_text = (body.get("text") or "").strip()

        # Subcommand routing — always available regardless of channel
        # allowlist because linking is the prerequisite for everything
        # else.  Match on the first whitespace-delimited word so an
        # innocuous prompt like `linking up the docs` doesn't hijack
        # into the link handler with `"ing up the docs"` as the token.
        parts = raw_text.split(None, 1)
        subcmd = parts[0] if parts else ""
        if subcmd == "link":
            token = parts[1].strip() if len(parts) > 1 else ""
            await self._handle_link(slack_channel, user_id, token)
            return
        if subcmd == "unlink":
            await self._handle_unlink(slack_channel, user_id)
            return

        if self.config.allowed_channels and slack_channel not in self.config.allowed_channels:
            await self._client.chat_postEphemeral(
                channel=slack_channel,
                user=user_id,
                text="Sorry, turnstone isn't enabled in this channel.",
            )
            return

        # Only linked Slack users can start sessions — otherwise every
        # Slack workspace member would be creating workstreams under
        # the shared gateway identity with no attribution.
        if not await self._require_linked(slack_channel, user_id):
            return

        existing = self._channel_sessions.get((slack_channel, user_id))
        if existing is not None:
            old_ws_id, old_thread_ts = existing
            await self._archive_session(
                slack_channel,
                user_id,
                old_ws_id,
                old_thread_ts,
            )

        opener = await self._client.chat_postMessage(
            channel=slack_channel,
            text=f"<@{user_id}> started a turnstone session.",
        )
        if not opener.get("ok"):
            log.error("slack.slash_command.opener_failed", channel=slack_channel)
            return

        opener_ts = opener["ts"]

        await self._client.chat_postMessage(
            channel=slack_channel,
            thread_ts=opener_ts,
            text=_GREETING,
        )

        route = SlackRoute(
            channel=slack_channel,
            user_id=user_id,
            thread_ts=opener_ts,
        )

        ws_id, _ = await self.router.get_or_create_workstream(
            channel_type="slack",
            channel_id=route.to_channel_id(),
            name=f"slack-{slack_channel[:8]}",
            client_type="chat",
        )
        await self.subscribe_ws(ws_id, route.to_channel_id())
        self._channel_sessions[(slack_channel, user_id)] = (ws_id, opener_ts)

        log.info(
            "slack.session_started",
            ws_id=ws_id,
            channel=slack_channel,
            user=user_id,
            thread_ts=opener_ts,
        )

    def _allow_user_send(self, slack_user_id: str) -> bool:
        """Return True when *slack_user_id* is within the send rate limit.

        Sliding-window counter: up to ``_PER_USER_RATE_LIMIT`` allowed
        sends per ``_PER_USER_RATE_WINDOW_S``.  Stale entries are evicted
        lazily, and the bucket map is LRU-bounded so a workspace with
        churning user IDs can't grow the map unboundedly.
        """
        now = time.monotonic()
        window_start = now - _PER_USER_RATE_WINDOW_S
        bucket = self._rate_buckets.get(slack_user_id)
        if bucket is None:
            bucket = deque()
            self._rate_buckets[slack_user_id] = bucket
            while len(self._rate_buckets) > _PER_USER_RATE_CAP:
                self._rate_buckets.popitem(last=False)
        else:
            self._rate_buckets.move_to_end(slack_user_id)
        while bucket and bucket[0] < window_start:
            bucket.popleft()
        if len(bucket) >= _PER_USER_RATE_LIMIT:
            return False
        bucket.append(now)
        return True

    async def _check_inbound(
        self,
        slack_channel: str,
        slack_user_id: str,
        text: str,
    ) -> bool:
        """Validate an inbound message against size + rate limits.

        Returns True when the caller should proceed.  Posts an ephemeral
        message on rejection so the user understands why their message
        was dropped.
        """
        if len(text) > _MAX_INBOUND_MESSAGE_LEN:
            await self._client.chat_postEphemeral(
                channel=slack_channel,
                user=slack_user_id,
                text=(
                    f"Message too long ({len(text)} chars).  Please keep "
                    f"messages under {_MAX_INBOUND_MESSAGE_LEN} characters."
                ),
            )
            log.info(
                "slack.inbound_oversize",
                user_id=slack_user_id,
                length=len(text),
            )
            return False
        if not self._allow_user_send(slack_user_id):
            await self._client.chat_postEphemeral(
                channel=slack_channel,
                user=slack_user_id,
                text=(
                    f"You're sending messages too fast.  Limit is "
                    f"{_PER_USER_RATE_LIMIT} per "
                    f"{int(_PER_USER_RATE_WINDOW_S)} seconds."
                ),
            )
            log.warning("slack.inbound_rate_limited", user_id=slack_user_id)
            return False
        return True

    async def _require_linked(self, slack_channel: str, slack_user_id: str) -> bool:
        """Return True if *slack_user_id* is mapped to a Turnstone account.

        Posts an ephemeral "please /link first" message on False so the
        caller can early-return.
        """
        mapped = await self.router.resolve_user("slack", slack_user_id)
        if mapped:
            return True
        await self._client.chat_postEphemeral(
            channel=slack_channel,
            user=slack_user_id,
            text=(
                "You need to link your Slack account to a Turnstone user before "
                "using this bot.  Run `/turnstone link <your_api_token>` — tokens "
                "are managed in the Turnstone console."
            ),
        )
        return False

    def _allow_link_attempt(self, slack_user_id: str) -> bool:
        """Return True when *slack_user_id* is under the /link rate limit.

        Mirrors the Discord helper at `discord/cog.py::_allow_link_attempt`
        — 5 attempts per hour per Slack user, LRU-bounded.
        """
        now = time.monotonic()
        window_start = now - _LINK_RATE_WINDOW_S
        bucket = self._link_buckets.get(slack_user_id)
        if bucket is None:
            bucket = deque()
            self._link_buckets[slack_user_id] = bucket
            while len(self._link_buckets) > _LINK_RATE_CAP:
                self._link_buckets.popitem(last=False)
        else:
            self._link_buckets.move_to_end(slack_user_id)
        while bucket and bucket[0] < window_start:
            bucket.popleft()
        if len(bucket) >= _LINK_RATE_LIMIT:
            return False
        bucket.append(now)
        return True

    async def _handle_link(self, slack_channel: str, slack_user_id: str, token: str) -> None:
        """Link the caller's Slack ID to the Turnstone user that owns *token*."""
        from turnstone.core.auth import hash_token

        if not self._allow_link_attempt(slack_user_id):
            log.warning("slack.link_rate_limited", slack_user_id=slack_user_id)
            await self._client.chat_postEphemeral(
                channel=slack_channel,
                user=slack_user_id,
                text=(
                    f"Too many `/turnstone link` attempts.  Try again "
                    f"later — limit is {_LINK_RATE_LIMIT} per hour."
                ),
            )
            return

        if not token:
            await self._client.chat_postEphemeral(
                channel=slack_channel,
                user=slack_user_id,
                text="Usage: `/turnstone link <your_api_token>`",
            )
            return

        existing = await asyncio.to_thread(self.storage.get_channel_user, "slack", slack_user_id)
        if existing:
            await self._client.chat_postEphemeral(
                channel=slack_channel,
                user=slack_user_id,
                text="Your Slack account is already linked.  Use `/turnstone unlink` first.",
            )
            return

        record = await asyncio.to_thread(self.storage.get_api_token_by_hash, hash_token(token))
        if record is None or not record.get("user_id"):
            # Generic failure message — don't leak whether the token
            # existed but was e.g. revoked.
            await self._client.chat_postEphemeral(
                channel=slack_channel,
                user=slack_user_id,
                text="Invalid token.  Please provide a valid Turnstone API token.",
            )
            log.info("slack.link_invalid_token", slack_user_id=slack_user_id)
            return

        await asyncio.to_thread(
            self.storage.create_channel_user,
            "slack",
            slack_user_id,
            record["user_id"],
        )
        await self._client.chat_postEphemeral(
            channel=slack_channel,
            user=slack_user_id,
            text="Linked.  You can now use Turnstone from Slack.",
        )
        log.info(
            "slack.link_succeeded",
            slack_user_id=slack_user_id,
            turnstone_user_id=record["user_id"],
        )

    async def _handle_unlink(self, slack_channel: str, slack_user_id: str) -> None:
        """Drop the Slack → Turnstone mapping for the caller."""
        existing = await asyncio.to_thread(self.storage.get_channel_user, "slack", slack_user_id)
        if not existing:
            await self._client.chat_postEphemeral(
                channel=slack_channel,
                user=slack_user_id,
                text="Your Slack account was not linked.",
            )
            return
        await asyncio.to_thread(self.storage.delete_channel_user, "slack", slack_user_id)
        await self._client.chat_postEphemeral(
            channel=slack_channel,
            user=slack_user_id,
            text="Unlinked.",
        )
        log.info("slack.unlink_succeeded", slack_user_id=slack_user_id)

    async def _archive_session(
        self,
        slack_channel: str,
        user_id: str,
        ws_id: str,
        thread_ts: str,
    ) -> None:
        """Mark the old session as archived and unsubscribe."""
        try:
            await self._client.chat_postMessage(
                channel=slack_channel,
                thread_ts=thread_ts,
                text="_This session has been archived. A new one has started._",
            )
        except Exception:
            log.debug("slack.archive_session.notify_failed", ws_id=ws_id, exc_info=True)

        route = SlackRoute(
            channel=slack_channel,
            user_id=user_id,
            thread_ts=thread_ts,
        )

        try:
            await self.router.delete_route("slack", route.to_channel_id())
            log.info(
                "slack.archived_route_deleted",
                ws_id=ws_id,
                channel_id=route.to_channel_id(),
            )
        except Exception:
            log.exception(
                "slack.archived_route_delete_failed",
                ws_id=ws_id,
                channel_id=route.to_channel_id(),
            )

        await self.unsubscribe_ws(ws_id)
        # close_workstream is the only path that drops the ws_id from
        # ChannelRouter._node_urls; skipping it leaks one cache entry per
        # archived session over long bot uptime.
        await self.router.close_workstream(ws_id)
        self._channel_sessions.pop((slack_channel, user_id), None)
        log.info(
            "slack.session_archived",
            ws_id=ws_id,
            channel=slack_channel,
            user=user_id,
        )

    async def _on_message(self, event: dict[str, Any], say: Any) -> None:
        """Route messages — thread-only in channels, free in DMs."""
        if event.get("bot_id") or event.get("subtype"):
            return

        channel_id = event.get("channel", "")
        channel_type = event.get("channel_type", "")
        thread_ts = event.get("thread_ts", "")
        user_id = event.get("user", "")
        text = event.get("text", "").strip()

        if not text or not channel_id or not user_id:
            return

        if thread_ts and thread_ts in self._notify_ws_map:
            origin_ws_id, notify_route = self._notify_ws_map[thread_ts]
            if channel_id == notify_route.channel and user_id == (notify_route.user_id or ""):
                # Same identity + rate/size gates the other two routing
                # paths use — the author-match above narrows the surface
                # to the originally notified user, but that user may have
                # unlinked or their token may have been revoked since the
                # notification was recorded.
                if not await self._require_linked(channel_id, user_id):
                    return
                if not await self._check_inbound(channel_id, user_id, text):
                    return
                try:
                    reply_route = SlackRoute(
                        channel=channel_id,
                        user_id=user_id or None,
                        thread_ts=thread_ts or event.get("ts") or None,
                    )
                    self._notify_reply_routes[origin_ws_id] = reply_route
                    log.info(
                        "slack.notification_reply_attempt",
                        thread_ts=thread_ts,
                        ws_id=origin_ws_id,
                        channel=channel_id,
                        user=user_id,
                    )
                    await self.router.send_message(origin_ws_id, text)
                    log.info("slack.notification_reply_routed", ws_id=origin_ws_id)
                except TurnstoneAPIError as exc:
                    self._notify_reply_routes.pop(origin_ws_id, None)
                    if exc.status_code == 404:
                        self._clear_notification_tracking_for_ws(origin_ws_id)
                        log.warning(
                            "slack.notification_reply_dead_ws",
                            ws_id=origin_ws_id,
                            thread_ts=thread_ts,
                        )
                        try:
                            slash_cmd = self.config.slash_command or "/turnstone"
                            await self._client.chat_postEphemeral(
                                channel=channel_id,
                                user=user_id,
                                text=(
                                    "This notification is no longer linked to an active session. "
                                    f"Please start a new one with `{slash_cmd}`."
                                ),
                            )
                        except Exception:
                            log.debug(
                                "slack.notification_reply_dead_ws_notice_failed",
                                exc_info=True,
                            )
                    else:
                        log.exception("slack.notification_reply_failed")
                except Exception:
                    self._notify_reply_routes.pop(origin_ws_id, None)
                    log.exception("slack.notification_reply_failed")
                return

        if channel_id.startswith("D") or channel_type == "im":
            await self._handle_dm(event, say)
            return

        if self.config.allowed_channels and channel_id not in self.config.allowed_channels:
            return

        session = self._channel_sessions.get((channel_id, user_id))
        if session is None:
            return

        ws_id, session_thread_ts = session
        if thread_ts != session_thread_ts:
            return

        # Gate message routing on a linked Slack user so all LLM usage
        # can be attributed to a Turnstone account.  Unlinked users
        # see an ephemeral prompt to run `/turnstone link`.
        if not await self._require_linked(channel_id, user_id):
            return

        if not await self._check_inbound(channel_id, user_id, text):
            return

        try:
            await self.router.send_message(ws_id, text)
            log.info("slack.message_dispatched", ws_id=ws_id, channel=channel_id)
        except Exception:
            log.exception("slack.message_dispatch_failed", channel=channel_id)
            await say(
                text="Sorry, something went wrong routing your message.",
                thread_ts=thread_ts,
            )

    async def _handle_dm(self, event: dict[str, Any], say: Any) -> None:
        """Handle a direct message — no slash command required."""
        channel_id = event.get("channel", "")
        thread_ts = event.get("thread_ts", "")
        user_id = event.get("user", "")
        text = event.get("text", "").strip()

        # Same identity gate as `/turnstone` and channel messages: a
        # Slack user must be linked to a Turnstone account before they
        # can drive LLM calls via DM.
        if not await self._require_linked(channel_id, user_id):
            return

        if not await self._check_inbound(channel_id, user_id, text):
            return
        # Slack DMs don't auto-thread, so when no explicit thread_ts is
        # present we route by (channel, user) alone.  Falling back to the
        # per-message ``ts`` would make every top-level DM spawn a new
        # workstream because the key would be unique per message.
        route = SlackRoute(
            channel=channel_id,
            user_id=user_id or None,
            thread_ts=thread_ts or None,
        )

        try:
            ws_id, is_new = await self.router.get_or_create_workstream(
                channel_type="slack",
                channel_id=route.to_channel_id(),
                name=f"slack-dm-{user_id[:8]}",
                client_type="chat",
            )
            if is_new:
                await self.subscribe_ws(ws_id, route.to_channel_id())

            await self.router.send_message(ws_id, text)
            log.info("slack.dm_dispatched", ws_id=ws_id, user=user_id)
        except Exception:
            log.exception("slack.dm_dispatch_failed", user=user_id)
            await say(text="Sorry, something went wrong routing your message.")

    async def _resolve_approval(
        self,
        ack: Any,
        body: dict[str, Any],
        *,
        approved: bool,
    ) -> None:
        """Handle an approve or deny button click from an approval prompt."""
        await ack()
        value = body["actions"][0].get("value", "")
        parts = value.split("|", 1)
        if len(parts) != 2:
            return

        ws_id, correlation_id = parts
        entry = self._pending_approval.get(ws_id)
        actor_user_id = body.get("user", {}).get("id", "")
        channel = body["container"]["channel_id"]
        verb = "approve" if approved else "deny"

        log.info(
            "slack.approval_actor_check",
            ws_id=ws_id,
            actor_user_id=actor_user_id,
            owner_user_id=entry.owner_user_id if entry else None,
            has_entry=entry is not None,
        )

        if entry is None or not entry.owner_user_id:
            log.warning(
                "slack.approval_missing_owner",
                ws_id=ws_id,
                actor_user_id=actor_user_id,
            )
            await self._client.chat_postEphemeral(
                channel=channel,
                user=actor_user_id,
                text="This approval can no longer be verified. Please retry from the active session.",
            )
            return

        if actor_user_id != entry.owner_user_id:
            await self._client.chat_postEphemeral(
                channel=channel,
                user=actor_user_id,
                text=f"Only the session owner can {verb} this tool call.",
            )
            return

        await self.router.send_approval(ws_id, correlation_id, approved=approved)
        # Drop the pending entry now that we've handled it locally. Otherwise
        # the subsequent ApprovalResolvedEvent will rewrite the message a
        # second time ("Tool approved" → "Approved") — wasted chat_update and
        # a visible edit flicker.
        self._pending_approval.pop(ws_id, None)
        ts = body["container"]["message_ts"]
        update_text = "Tool approved" if approved else "Tool denied"
        event_key = "approve" if approved else "deny"

        try:
            await self._client.chat_update(
                channel=channel,
                ts=ts,
                text=update_text,
                blocks=[],
            )
        except Exception:
            log.debug(f"slack.{event_key}_message_update_failed", exc_info=True)

    async def _ensure_plan_review_owner(
        self,
        entry: tuple[str, str, str] | None,
        actor_user_id: str,
        channel: str,
        verb: str,
    ) -> bool:
        """Return True when *actor_user_id* owns the pending plan review.

        The gateway forwards ``/plan`` calls with its service-scoped JWT,
        and the server bypasses ownership checks on service scope — so
        every plan-review interaction needs an adapter-side owner gate.
        """
        if entry is None:
            log.warning("slack.plan_review_missing_entry", actor_user_id=actor_user_id)
            await self._client.chat_postEphemeral(
                channel=channel,
                user=actor_user_id,
                text="This plan review can no longer be verified. Please retry from the active session.",
            )
            return False
        _channel, _ts, owner_user_id = entry
        if not owner_user_id or actor_user_id != owner_user_id:
            await self._client.chat_postEphemeral(
                channel=channel,
                user=actor_user_id,
                text=f"Only the session owner can {verb} this plan.",
            )
            return False
        return True

    async def _on_plan_approve(self, ack: Any, body: dict[str, Any]) -> None:
        await ack()
        ws_id = body["actions"][0].get("value", "")
        log.info("slack.plan_approve_clicked", ws_id=ws_id)
        if not ws_id:
            return

        actor_user_id = body.get("user", {}).get("id", "")
        channel = body["container"]["channel_id"]
        entry = self._pending_plan_review_ts.get(ws_id)
        if not await self._ensure_plan_review_owner(entry, actor_user_id, channel, "approve"):
            return

        self._streaming.pop(ws_id, None)
        await self.router.send_plan_feedback(ws_id, "", "")
        log.info("slack.plan_feedback_sent", ws_id=ws_id, feedback="")

        ts = body["container"]["message_ts"]

        self._pending_plan_review_ts.pop(ws_id, None)

        try:
            await self._client.chat_update(
                channel=channel,
                ts=ts,
                text="Plan approved",
                blocks=[],
            )
        except Exception:
            log.debug("slack.plan_review_approve_update_failed", exc_info=True)

    async def _on_plan_request_changes(self, ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        ws_id = body["actions"][0].get("value", "")
        if not ws_id:
            return

        actor_user_id = body.get("user", {}).get("id", "")
        channel = body["container"]["channel_id"]
        entry = self._pending_plan_review_ts.get(ws_id)
        if not await self._ensure_plan_review_owner(
            entry, actor_user_id, channel, "request changes on"
        ):
            return

        trigger_id = body.get("trigger_id", "")
        if not trigger_id:
            return

        await client.views_open(
            trigger_id=trigger_id,
            view={
                "type": "modal",
                "callback_id": "ts_plan_feedback_modal",
                "private_metadata": ws_id,
                "title": {"type": "plain_text", "text": "Plan feedback"},
                "submit": {"type": "plain_text", "text": "Send"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "feedback_block",
                        "label": {"type": "plain_text", "text": "Requested changes"},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "feedback_input",
                            "multiline": True,
                        },
                    }
                ],
            },
        )

    async def _on_plan_feedback_modal(
        self, ack: Any, body: dict[str, Any], view: dict[str, Any]
    ) -> None:
        await ack()

        ws_id = view.get("private_metadata", "")
        if not ws_id:
            return

        actor_user_id = body.get("user", {}).get("id", "")
        entry = self._pending_plan_review_ts.get(ws_id)
        if entry is None:
            # No pending review (stale modal) — silently drop.
            return
        _ignored_channel, _ignored_ts, owner_user_id = entry
        if not owner_user_id or actor_user_id != owner_user_id:
            # Modal submit can't emit ephemeral; just log and drop.
            log.warning(
                "slack.plan_feedback_non_owner",
                ws_id=ws_id,
                actor_user_id=actor_user_id,
                owner_user_id=owner_user_id,
            )
            return

        feedback = (
            view.get("state", {})
            .get("values", {})
            .get("feedback_block", {})
            .get("feedback_input", {})
            .get("value", "")
            .strip()
        )

        if not feedback:
            feedback = "Please revise the plan."

        log.info("slack.plan_feedback_modal_submitted", ws_id=ws_id, feedback=feedback)

        self._streaming.pop(ws_id, None)
        await self.router.send_plan_feedback(ws_id, "", feedback)
        log.info("slack.plan_feedback_sent", ws_id=ws_id, feedback=feedback)

        entry = self._pending_plan_review_ts.pop(ws_id, None)
        if entry is not None:
            channel, ts, _owner = entry
            try:
                await self._client.chat_update(
                    channel=channel,
                    ts=ts,
                    text="Plan changes requested",
                    blocks=[],
                )
            except Exception:
                log.debug("slack.plan_review_modal_update_failed", exc_info=True)

    async def subscribe_ws(self, ws_id: str, channel_id: str) -> None:
        # Recover from a dead SSE task before the early-return check.  If the
        # prior listener died with an unhandled exception, its ws_id is still
        # in _subscribed_ws — without this purge subscribe_ws would silently
        # no-op forever.
        prior = self._sse_tasks.get(ws_id)
        if prior is not None and prior.done():
            log.info("slack.sse_task_recovered", ws_id=ws_id)
            self._sse_tasks.pop(ws_id, None)
            self._subscribed_ws.discard(ws_id)

        if ws_id in self._subscribed_ws:
            return

        task = asyncio.create_task(
            self._sse_listener(ws_id, channel_id),
            name=f"sse:{ws_id}",
        )
        self._sse_tasks[ws_id] = task
        self._subscribed_ws.add(ws_id)
        log.info("slack.subscribed", ws_id=ws_id, channel_id=channel_id)

    def _clear_ws_state(self, ws_id: str) -> None:
        """Drop all in-memory state keyed by *ws_id*.

        Does not cancel or await the SSE task — callers handle task
        lifecycle differently (``unsubscribe_ws`` cancels and awaits;
        ``_cleanup_stale_route`` is itself invoked from inside the task).
        """
        self._subscribed_ws.discard(ws_id)
        self._streaming.pop(ws_id, None)
        self._pending_approval.pop(ws_id, None)
        self._pending_plan_review_ts.pop(ws_id, None)
        self._clear_notification_tracking_for_ws(ws_id)

    async def unsubscribe_ws(self, ws_id: str) -> None:
        task = self._sse_tasks.pop(ws_id, None)
        if task is not None:
            task.cancel()
            # Await the cancelled task so CancelledError propagates out of
            # the SSE loop before we clear the per-ws state below.
            # CancelledError is expected; other exceptions from the SSE
            # loop are already logged there and must not block shutdown.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        self._clear_ws_state(ws_id)

        log.info("slack.unsubscribed", ws_id=ws_id)

    async def _sse_listener(self, ws_id: str, channel_id: str) -> None:
        """Connect to the server SSE endpoint and dispatch events.

        Delegates the reconnect/backoff loop to :func:`run_sse_stream`.
        """

        async def _on_event(event: ServerEvent) -> None:
            effective_route = self._notify_reply_routes.get(
                ws_id,
                SlackRoute.parse(channel_id),
            )
            await self._on_ws_event(ws_id, effective_route, event)

        async def _on_stale() -> None:
            await self._cleanup_stale_route(ws_id, channel_id)

        await run_sse_stream(
            http_client=self._http_client,
            log_prefix="slack",
            ws_id=ws_id,
            node_url_fn=self.router.get_node_url,
            token_factory=self._token_factory,
            on_event=_on_event,
            on_stale=_on_stale,
        )

    async def _cleanup_stale_route(self, ws_id: str, channel_id: str) -> None:
        """Remove a channel route whose workstream no longer exists."""
        route = SlackRoute.parse(channel_id)
        if route.channel and route.user_id and route.thread_ts:
            self._channel_sessions.pop((route.channel, route.user_id), None)

        await self.router.delete_route("slack", channel_id)
        # Called from inside the SSE task itself — don't await the task here.
        self._sse_tasks.pop(ws_id, None)
        self._clear_ws_state(ws_id)

        log.info("slack.stale_route_removed", ws_id=ws_id)

    async def _on_ws_event(
        self,
        ws_id: str,
        route: SlackRoute,
        event: ServerEvent,
    ) -> None:
        """Dispatch a typed server event to its per-event handler."""
        if isinstance(event, (ThinkingStartEvent, ThinkingStopEvent)):
            return

        if isinstance(event, ContentEvent):
            await self._handle_content(ws_id, route, event)
        elif isinstance(event, ApproveRequestEvent):
            await self._handle_approve_request(ws_id, route, event)
        elif isinstance(event, IntentVerdictEvent):
            await self._handle_intent_verdict(ws_id, event)
        elif isinstance(event, PlanReviewEvent):
            await self._handle_plan_review(ws_id, route, event)
        elif isinstance(event, ApprovalResolvedEvent):
            await self._handle_approval_resolved(ws_id, event)
        elif isinstance(event, StreamEndEvent):
            await self._handle_stream_end(ws_id)
        elif isinstance(event, ErrorEvent):
            await self._handle_error(route, event)

    async def _handle_content(self, ws_id: str, route: SlackRoute, event: ContentEvent) -> None:
        slack_channel = route.channel
        thread_ts = route.thread_ts or ""
        sm = self._streaming.get(ws_id)
        if sm is None or sm.channel != slack_channel or sm.thread_ts != thread_ts:
            # Finalize the outgoing StreamingMessage before swapping so any
            # already-buffered tokens still land on the old thread instead
            # of being dropped (happens when the effective route flips
            # mid-stream — e.g. a user replies to a notification and pins
            # the conversation to a different thread).
            if sm is not None:
                await sm.finalize()
            sm = StreamingMessage(
                client=self._client,
                channel=slack_channel,
                thread_ts=thread_ts,
                max_length=self.config.max_message_length,
                edit_interval=self.config.streaming_edit_interval,
            )
            self._streaming[ws_id] = sm
        await sm.append(event.text)

    async def _handle_approve_request(
        self,
        ws_id: str,
        route: SlackRoute,
        event: ApproveRequestEvent,
    ) -> None:
        slack_channel = route.channel
        thread_ts = route.thread_ts or ""
        owner_user_id = route.user_id

        verdict = await self.router.evaluate_tool_policies(event.items)
        policy_handled = False
        if verdict.kind == "deny":
            denied = ", ".join(verdict.denied_tools)
            await self.router.send_approval(
                ws_id,
                "",
                approved=False,
                feedback=f"Blocked by tool policy: {denied}",
            )
            await self._client.chat_postMessage(
                channel=slack_channel,
                thread_ts=thread_ts or None,
                text=f"_Tool blocked by admin policy: {denied}_",
            )
            policy_handled = True
        elif verdict.kind == "allow":
            await self.router.send_approval(ws_id, "", approved=True)
            await self._client.chat_postMessage(
                channel=slack_channel,
                thread_ts=thread_ts or None,
                text="_Tool approved by policy._",
            )
            policy_handled = True

        if not policy_handled and (self.config.auto_approve or self._should_auto_approve(event)):
            await self.router.send_approval(ws_id, "", approved=True)
            await self._client.chat_postMessage(
                channel=slack_channel,
                thread_ts=thread_ts or None,
                text="_Tool auto-approved._",
            )
        elif not policy_handled:
            await self._send_approval_request(
                ws_id,
                "",
                event.items,
                slack_channel,
                thread_ts,
                owner_user_id,
            )

    async def _handle_intent_verdict(self, ws_id: str, event: IntentVerdictEvent) -> None:
        entry = self._pending_approval.get(ws_id)
        if entry is None:
            return

        pending_channel = entry.channel
        pending_ts = entry.message_ts
        risk = (event.risk_level or "medium").upper()
        verdict_text = (
            f"*Judge Verdict: {event.func_name or 'tool'}*\n"
            f"Risk: {risk} | Confidence: {event.confidence or 'N/A'}\n"
            f"_{event.intent_summary or ''}_"
        )
        # Append the verdict section in-place on the cached blocks so
        # repeat IntentVerdictEvents stack on the same approval message
        # (matches the pre-refactor "fetch live blocks and append"
        # behavior, but without the extra conversations_history call).
        entry.blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": verdict_text},
            }
        )
        try:
            await self._client.chat_update(
                channel=pending_channel,
                ts=pending_ts,
                blocks=entry.blocks,
                text="Tool approval required",
            )
        except Exception:
            log.debug("slack.verdict_message_update_failed", ws_id=ws_id, exc_info=True)

    async def _handle_plan_review(
        self, ws_id: str, route: SlackRoute, event: PlanReviewEvent
    ) -> None:
        slack_channel = route.channel
        thread_ts = route.thread_ts or ""
        owner_user_id = route.user_id or ""

        log.info("slack.plan_review_received", ws_id=ws_id)
        plan_preview = _sanitize_slack_preview(event.content, max_length=2000)
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Plan Review*\n```{plan_preview}```",
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "style": "primary",
                        "action_id": "ts_plan_approve",
                        "value": ws_id,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Request changes"},
                        "style": "danger",
                        "action_id": "ts_plan_request_changes",
                        "value": ws_id,
                    },
                ],
            },
        ]

        resp = await self._client.chat_postMessage(
            channel=slack_channel,
            thread_ts=thread_ts or None,
            text="Plan review required",
            blocks=cast("list[dict[str, Any]]", blocks),
        )
        if resp.get("ok"):
            self._pending_plan_review_ts[ws_id] = (slack_channel, resp["ts"], owner_user_id)

    async def _handle_approval_resolved(self, ws_id: str, event: ApprovalResolvedEvent) -> None:
        entry = self._pending_approval.pop(ws_id, None)
        if entry is None:
            return

        pending_channel = entry.channel
        pending_ts = entry.message_ts
        label = "Approved" if event.approved else "Denied"
        try:
            await self._client.chat_update(
                channel=pending_channel,
                ts=pending_ts,
                text=label,
                blocks=cast("list[dict[str, Any]]", []),
            )
        except Exception:
            log.debug("slack.approval_resolved_edit_failed", ws_id=ws_id, exc_info=True)

    async def _handle_stream_end(self, ws_id: str) -> None:
        sm = self._streaming.pop(ws_id, None)
        if sm is not None:
            await sm.finalize()

        # Pop the notification-reply override so subsequent turns on this ws
        # default back to the session route.  Without the pop, one notification
        # reply pins all future responses to the notification thread until the
        # bot restarts.
        reply_route = self._notify_reply_routes.pop(ws_id, None)
        if (
            reply_route is not None
            and sm is not None
            and sm.message_ts
            and reply_route.channel
            and reply_route.user_id
        ):
            self._track_notification(sm.message_ts, ws_id, reply_route)

        self._pending_approval.pop(ws_id, None)

    async def _handle_error(self, route: SlackRoute, event: ErrorEvent) -> None:
        safe_msg = event.message[:500] if event.message else "An error occurred"
        await self._client.chat_postMessage(
            channel=route.channel,
            thread_ts=route.thread_ts or None,
            text=f"*Error:* {safe_msg}",
        )

    async def _send_approval_request(
        self,
        ws_id: str,
        correlation_id: str,
        items: list[dict[str, Any]],
        channel: str,
        thread_ts: str,
        owner_user_id: str | None,
    ) -> None:
        tool_lines: list[str] = []
        body_len = 0
        truncated_items = 0
        for item in items:
            raw_name = item.get("approval_label") or item.get("func_name") or "tool"
            name = raw_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

            raw_preview = item.get("preview", "")
            preview = (
                _sanitize_slack_preview(raw_preview, max_length=_APPROVAL_PER_ITEM_PREVIEW)
                if raw_preview
                else ""
            )

            line = f"• *{name}*\n```{preview}```" if preview else f"• *{name}*"
            # +1 for the join newline
            if body_len + len(line) + 1 > _APPROVAL_TEXT_BUDGET:
                truncated_items = len(items) - len(tool_lines)
                break
            tool_lines.append(line)
            body_len += len(line) + 1

        if truncated_items > 0:
            tool_lines.append(f"_…and {truncated_items} more (preview truncated)_")

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Tool Approval Required*\n" + "\n".join(tool_lines),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "style": "primary",
                        "action_id": "ts_approve",
                        "value": f"{ws_id}|{correlation_id}",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Deny"},
                        "style": "danger",
                        "action_id": "ts_deny",
                        "value": f"{ws_id}|{correlation_id}",
                    },
                ],
            },
        ]

        resp = await self._client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts or None,
            text="Tool approval required",
            blocks=cast("list[dict[str, Any]]", blocks),
        )
        if resp.get("ok"):
            self._pending_approval[ws_id] = PendingApproval(
                channel=channel,
                message_ts=resp["ts"],
                owner_user_id=owner_user_id,
                blocks=list(cast("list[dict[str, Any]]", blocks)),
            )
            log.info(
                "slack.pending_approval_stored",
                ws_id=ws_id,
                channel=channel,
                thread_ts=thread_ts,
                owner_user_id=owner_user_id,
            )

    def _should_auto_approve(self, event: ApproveRequestEvent) -> bool:
        allowed = self.config.auto_approve_tools
        if not allowed or not event.items:
            return False

        for item in event.items:
            name = (
                item.get("func_name")
                or item.get("approval_label")
                or item.get("function", {}).get("name", "")
            )
            if name not in allowed:
                return False

        return True

    def _track_notification(
        self,
        msg_ts: str,
        ws_id: str,
        route: SlackRoute,
    ) -> None:
        while len(self._notify_ws_map) >= self._MAX_NOTIFY_TRACKING:
            del self._notify_ws_map[next(iter(self._notify_ws_map))]
        self._notify_ws_map[msg_ts] = (ws_id, route)
        log.info(
            "slack.notification_tracked",
            message_ts=msg_ts,
            ws_id=ws_id,
            channel=route.channel,
            user=route.user_id,
        )

    async def send(self, channel_id: str, content: str) -> str:
        route = SlackRoute.parse(channel_id)

        root_ts = route.thread_ts or ""
        first_post_ts = ""

        for i, chunk in enumerate(chunk_message(content, self.config.max_message_length)):
            resp = await self._client.chat_postMessage(
                channel=route.channel,
                thread_ts=root_ts or None,
                text=chunk,
            )
            if resp.get("ok"):
                ts = resp["ts"]
                if i == 0:
                    first_post_ts = ts
                    if not root_ts:
                        root_ts = ts

        return root_ts or first_post_ts

    async def send_notification(self, channel_id: str, content: str, ws_id: str) -> str:
        route = SlackRoute.parse(channel_id)
        thread_root_ts = await self.send(channel_id, content)
        if thread_root_ts and ws_id and route.channel and route.user_id:
            self._track_notification(
                thread_root_ts,
                ws_id,
                SlackRoute(
                    channel=route.channel,
                    user_id=route.user_id,
                    thread_ts=thread_root_ts,
                ),
            )
        return thread_root_ts

    def _clear_notification_tracking_for_ws(self, ws_id: str) -> None:
        self._notify_reply_routes.pop(ws_id, None)
        stale = [ts for ts, entry in self._notify_ws_map.items() if entry[0] == ws_id]
        for ts in stale:
            del self._notify_ws_map[ts]
