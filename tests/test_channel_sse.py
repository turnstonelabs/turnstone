"""Tests for the shared SSE reconnect helper in turnstone.channels._sse.

Each test drives the loop through a real ``httpx2.AsyncClient`` whose
``MockTransport`` answers every connection attempt with the next scripted reply.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import socket
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import httpx2
import pytest

from turnstone.channels import _sse
from turnstone.channels._routing import ChannelRouter
from turnstone.core.sse_limits import MAX_SSE_EVENT_SIZE

if TYPE_CHECKING:
    from collections.abc import Callable


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _content_event(text: str = "hello") -> str:
    """A payload ``ServerEvent.from_dict`` will accept (a ContentEvent)."""
    return json.dumps({"type": "content", "ws_id": "ws-1", "text": text})


def _status(code: int) -> Callable[[httpx2.Request], httpx2.Response]:
    """A reply with *code* and no body."""
    return lambda request: httpx2.Response(code)


def _stream(*payloads: str) -> Callable[[httpx2.Request], httpx2.Response]:
    """A 200 event stream with one ``data`` event per payload, then the end of the stream."""
    body = "".join(f"data: {payload}\n\n" for payload in payloads).encode()
    return lambda request: httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, content=body
    )


def _raise(
    exc_type: type[httpx2.TransportError],
) -> Callable[[httpx2.Request], httpx2.Response]:
    """A reply that fails the connection attempt with *exc_type*."""

    def reply(request: httpx2.Request) -> httpx2.Response:
        raise exc_type("boom", request=request)

    return reply


class _Node:
    """A node that answers each connection attempt with the next scripted reply.

    Once the script runs out it raises ``asyncio.CancelledError``, as cancelling
    the listener's task would, so the loop returns and the test ends.
    """

    def __init__(self, *replies: Callable[[httpx2.Request], httpx2.Response]) -> None:
        self._replies = list(replies)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if not self._replies:
            raise asyncio.CancelledError
        return self._replies.pop(0)(request)


async def _listen(
    node: _Node,
    *,
    on_event: AsyncMock | None = None,
    on_unavailable: AsyncMock | None = None,
    token_factory: Callable[[], str] | None = None,
) -> None:
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(node)) as client:
        await _sse.run_sse_stream(
            http_client=client,
            log_prefix="test",
            ws_id="ws-1",
            node_url_fn=AsyncMock(return_value="http://node.example"),
            token_factory=token_factory,
            on_event=on_event or AsyncMock(),
            on_unavailable=on_unavailable or AsyncMock(),
        )


@pytest.fixture
def _fast_sleep(monkeypatch):
    """Patch asyncio.sleep so backoff doesn't actually wait; record calls."""
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("turnstone.channels._sse.asyncio.sleep", fake_sleep)
    return sleeps


# ---------------------------------------------------------------------------
# 404 → on_unavailable + exit
# ---------------------------------------------------------------------------


class TestUnavailableStream:
    def test_404_calls_on_unavailable_and_returns(self, _fast_sleep):
        node = _Node(_status(404))
        on_unavailable = AsyncMock()
        on_event = AsyncMock()

        _run(_listen(node, on_event=on_event, on_unavailable=on_unavailable))

        on_unavailable.assert_awaited_once()
        on_event.assert_not_awaited()
        # No reconnect after 404.
        assert len(node.requests) == 1
        assert str(node.requests[0].url) == (
            "http://node.example/v1/api/workstreams/ws-1/events?user_turn=1"
        )
        assert _fast_sleep == []

    def test_on_unavailable_exception_still_exits(self, _fast_sleep):
        """If on_unavailable raises, the loop must not reconnect."""
        node = _Node(_status(404))
        on_unavailable = AsyncMock(side_effect=RuntimeError("storage down"))

        _run(_listen(node, on_unavailable=on_unavailable))

        on_unavailable.assert_awaited_once()
        # Still a single connect — no livelock.
        assert len(node.requests) == 1


# ---------------------------------------------------------------------------
# 500+ → exponential backoff
# ---------------------------------------------------------------------------


class TestBackoff:
    def test_500_triggers_backoff_and_retries(self, _fast_sleep):
        node = _Node(_status(503), _status(503), _status(503))

        _run(_listen(node))

        assert len(node.requests) == 4
        # Starts at SSE_RECONNECT_DELAY and doubles each time, capped at
        # SSE_MAX_RECONNECT_DELAY.
        assert _fast_sleep == [_sse.SSE_RECONNECT_DELAY * n for n in (1, 2, 4)]

    def test_backoff_resets_on_a_successful_connect(self, _fast_sleep):
        """A 200 connect resets the delay, so after that stream ends the loop waits the base
        delay again, and the next error doubles from there."""
        node = _Node(_status(503), _stream(_content_event()), _status(503))
        on_event = AsyncMock()

        _run(_listen(node, on_event=on_event))

        on_event.assert_awaited_once()
        # The base delay after the first 503, the base delay again once the
        # stream ends (the connect reset it), then double after the next 503.
        assert _fast_sleep == [_sse.SSE_RECONNECT_DELAY * n for n in (1, 1, 2)]


# ---------------------------------------------------------------------------
# Event dispatch
# ---------------------------------------------------------------------------


class TestEventDispatch:
    def test_invalid_json_is_skipped(self, _fast_sleep):
        node = _Node(_stream("{not json", _content_event()))
        on_event = AsyncMock()

        _run(_listen(node, on_event=on_event))

        # Good event delivered, bad one silently dropped.
        assert on_event.await_count == 1

    def test_on_event_exception_does_not_kill_stream(self, _fast_sleep):
        node = _Node(_stream(_content_event(), _content_event()))
        on_event = AsyncMock(side_effect=[RuntimeError("boom"), None])

        _run(_listen(node, on_event=on_event))

        # Both events attempted — first raised but second still delivered.
        assert on_event.await_count == 2

    def test_an_event_over_one_mebibyte_is_delivered(self, _fast_sleep):
        """httpx2 refuses an event over 1 MiB unless told otherwise, so this
        pins that the loop passes MAX_SSE_EVENT_SIZE."""
        text = "x" * (2 * 1024 * 1024)
        node = _Node(_stream(_content_event(text)))
        on_event = AsyncMock()

        _run(_listen(node, on_event=on_event))

        on_event.assert_awaited_once()
        assert on_event.await_args.args[0].text == text

    def test_an_event_over_the_cap_ends_the_connection(self, _fast_sleep, caplog):
        # A valid event, so only the cap can keep it from on_event.
        node = _Node(_stream(_content_event("x" * MAX_SSE_EVENT_SIZE)))
        on_event = AsyncMock()

        with caplog.at_level(logging.DEBUG, logger="turnstone.channels._sse"):
            _run(_listen(node, on_event=on_event))

        on_event.assert_not_awaited()
        errors = [r.getMessage() for r in caplog.records if "test.sse_error" in r.getMessage()]
        # The rendered traceback carries httpx2's SSEError for the cap.
        assert len(errors) == 1
        assert f"exceeded the {MAX_SSE_EVENT_SIZE} byte limit" in errors[0]
        # The loop backs off and reconnects.
        assert len(node.requests) == 2
        assert _fast_sleep == [_sse.SSE_RECONNECT_DELAY]


# ---------------------------------------------------------------------------
# Token factory
# ---------------------------------------------------------------------------


class TestTokenFactory:
    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("token_factory", "authorization"),
        [(None, None), (lambda: "a-token", "Bearer a-token")],
        ids=["no-token-factory", "token-factory"],
    )
    async def test_the_token_comes_from_the_factory_alone(self, token_factory, authorization):
        """On a client with no default headers, as the bots build theirs, a connection carries the
        factory's token or none, beside the event-stream headers."""
        from turnstone.channels._sse import run_sse_stream

        requests = []

        def respond(request):
            requests.append(request)
            return httpx2.Response(404)

        on_unavailable = AsyncMock()
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
            await asyncio.wait_for(
                run_sse_stream(
                    http_client=client,
                    log_prefix="test",
                    ws_id="ws-1",
                    node_url_fn=AsyncMock(return_value="http://node.example"),
                    token_factory=token_factory,
                    on_event=AsyncMock(),
                    on_unavailable=on_unavailable,
                ),
                timeout=2,
            )
        assert len(requests) == 1
        assert requests[0].headers.get("Authorization") == authorization
        assert requests[0].headers["Accept"] == "text/event-stream"
        assert requests[0].headers["Cache-Control"] == "no-store"
        on_unavailable.assert_awaited_once()

    def test_header_refreshed_per_connection(self, _fast_sleep):
        """token_factory is called once per reconnect so rotating service
        JWTs stay fresh."""
        counter = itertools.count()
        # Two reconnects, then the script runs out and the loop exits.
        node = _Node(_status(503), _status(503))

        _run(_listen(node, token_factory=lambda: f"tok-{next(counter)}"))

        assert [request.headers["Authorization"] for request in node.requests] == [
            "Bearer tok-0",
            "Bearer tok-1",
            "Bearer tok-2",
        ]


# ---------------------------------------------------------------------------
# Transport and protocol errors
# ---------------------------------------------------------------------------


def _not_an_event_stream(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, headers={"content-type": "application/json"}, content=b"{}")


class TestTransportErrors:
    @pytest.mark.parametrize(
        ("reply", "logged"),
        [
            (_raise(httpx2.ConnectError), "test.sse_connect_failed"),
            (_raise(httpx2.ConnectTimeout), "test.sse_connect_failed"),
            (_raise(httpx2.ReadTimeout), "test.sse_read_timeout"),
            (_raise(httpx2.RemoteProtocolError), "test.sse_remote_closed"),
            (_status(503), "test.sse_http_status_error"),
            # httpx2.SSEError, which no clause names.
            (_not_an_event_stream, "test.sse_error"),
        ],
        ids=[
            "connect-error",
            "connect-timeout",
            "read-timeout",
            "remote-protocol-error",
            "status-error",
            "not-an-event-stream",
        ],
    )
    def test_failure_backs_off_and_reconnects(self, _fast_sleep, caplog, reply, logged):
        """Each failure reaches the clause meant for it, then the loop backs off
        and reconnects."""
        node = _Node(reply)

        with caplog.at_level(logging.DEBUG, logger="turnstone.channels._sse"):
            _run(_listen(node))

        assert any(logged in r.getMessage() for r in caplog.records)
        assert len(node.requests) == 2
        assert _fast_sleep == [_sse.SSE_RECONNECT_DELAY]

    def test_a_console_lookup_that_cannot_connect_is_a_connect_failure(
        self, _fast_sleep, caplog, monkeypatch
    ):
        """The router resolves the node through the console SDK on every attempt, so the
        errors the SDK raises must be the ones this loop names."""
        # A loopback port nothing listens on: the SDK's real transport refuses the lookup.
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            monkeypatch.delenv(name, raising=False)
            monkeypatch.delenv(name.lower(), raising=False)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        lookups = itertools.count()

        async def listen() -> None:
            router = ChannelRouter(
                "http://node.example", MagicMock(), console_url=f"http://127.0.0.1:{port}"
            )

            async def node_url(ws_id: str) -> str:
                if next(lookups) >= 2:
                    raise asyncio.CancelledError
                return await router.get_node_url(ws_id)

            try:
                async with httpx2.AsyncClient(transport=httpx2.MockTransport(_Node())) as client:
                    await _sse.run_sse_stream(
                        http_client=client,
                        log_prefix="test",
                        ws_id="ws-1",
                        node_url_fn=node_url,
                        token_factory=None,
                        on_event=AsyncMock(),
                        on_unavailable=AsyncMock(),
                    )
            finally:
                await router.aclose()

        with caplog.at_level(logging.DEBUG, logger="turnstone.channels._sse"):
            _run(listen())

        messages = [r.getMessage() for r in caplog.records]
        assert sum("test.sse_connect_failed" in m for m in messages) == 2
        assert not any("test.sse_error" in m for m in messages)
