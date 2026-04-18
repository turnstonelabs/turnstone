"""Tests for the shared SSE reconnect helper in turnstone.channels._sse."""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


class _FakeSSEEvent:
    """A fake ``httpx_sse.ServerSentEvent`` with the subset we read."""

    def __init__(self, event: str, data: str) -> None:
        self.event = event
        self.data = data


class _FakeEventSource:
    """Context manager returned by our fake ``aconnect_sse``.

    Captures the (status_code, events) the test wants to deliver.
    ``aiter_sse`` yields the events then returns; the caller then hits
    the outer ``while True`` loop again, which will pick up the next
    queued response via the shared iterator state on _FakeConnect.
    """

    def __init__(self, *, status_code: int, events: list[_FakeSSEEvent]) -> None:
        self.response = SimpleNamespace(
            status_code=status_code,
            request=MagicMock(),
        )
        self._events = events

    async def __aenter__(self) -> _FakeEventSource:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None

    async def aiter_sse(self):  # type: ignore[no-untyped-def]
        for event in self._events:
            yield event


class _FakeConnect:
    """Drop-in replacement for ``httpx_sse.aconnect_sse``.

    On each call, pops the next ``_FakeEventSource`` from *queue*.  When
    the queue is empty, raises ``asyncio.CancelledError`` so the loop
    terminates cleanly in tests.
    """

    def __init__(self, queue: list[_FakeEventSource]) -> None:
        self._queue = queue
        self.call_count = 0

    def __call__(self, *args, **kwargs):  # noqa: ANN001, ANN204
        self.call_count += 1
        if not self._queue:
            raise asyncio.CancelledError
        return self._queue.pop(0)


@pytest.fixture
def _fast_sleep(monkeypatch):
    """Patch asyncio.sleep so backoff doesn't actually wait; record calls."""
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("turnstone.channels._sse.asyncio.sleep", fake_sleep)
    return sleeps


def _valid_event_data(ws_id: str = "ws-1") -> str:
    """A payload ``ServerEvent.from_dict`` will accept (a ContentEvent)."""
    return json.dumps(
        {
            "type": "content",
            "ws_id": ws_id,
            "text": "hello",
        }
    )


# ---------------------------------------------------------------------------
# 404 → on_stale + exit
# ---------------------------------------------------------------------------


class TestStaleRoute:
    def test_404_calls_on_stale_and_returns(self, monkeypatch, _fast_sleep):
        from turnstone.channels import _sse

        queue = [_FakeEventSource(status_code=404, events=[])]
        fake_connect = _FakeConnect(queue)
        monkeypatch.setattr(_sse.httpx_sse, "aconnect_sse", fake_connect)

        on_stale = AsyncMock()
        on_event = AsyncMock()

        async def node_url_fn(ws_id: str) -> str:
            return "http://node"

        _run(
            _sse.run_sse_stream(
                http_client=MagicMock(),
                log_prefix="test",
                ws_id="ws-1",
                node_url_fn=node_url_fn,
                token_factory=None,
                on_event=on_event,
                on_stale=on_stale,
            )
        )

        on_stale.assert_awaited_once()
        on_event.assert_not_awaited()
        # No reconnect after 404.
        assert fake_connect.call_count == 1
        assert _fast_sleep == []

    def test_on_stale_exception_still_exits(self, monkeypatch, _fast_sleep):
        """If on_stale raises, the loop must not reconnect."""
        from turnstone.channels import _sse

        queue = [_FakeEventSource(status_code=404, events=[])]
        fake_connect = _FakeConnect(queue)
        monkeypatch.setattr(_sse.httpx_sse, "aconnect_sse", fake_connect)

        on_stale = AsyncMock(side_effect=RuntimeError("storage down"))

        async def node_url_fn(ws_id: str) -> str:
            return "http://node"

        _run(
            _sse.run_sse_stream(
                http_client=MagicMock(),
                log_prefix="test",
                ws_id="ws-1",
                node_url_fn=node_url_fn,
                token_factory=None,
                on_event=AsyncMock(),
                on_stale=on_stale,
            )
        )

        on_stale.assert_awaited_once()
        # Still a single connect — no livelock.
        assert fake_connect.call_count == 1


# ---------------------------------------------------------------------------
# 500+ → exponential backoff
# ---------------------------------------------------------------------------


class TestBackoff:
    def test_500_triggers_backoff_and_retries(self, monkeypatch, _fast_sleep):
        from turnstone.channels import _sse

        queue = [
            _FakeEventSource(status_code=503, events=[]),
            _FakeEventSource(status_code=503, events=[]),
            _FakeEventSource(status_code=503, events=[]),
        ]
        fake_connect = _FakeConnect(queue)
        monkeypatch.setattr(_sse.httpx_sse, "aconnect_sse", fake_connect)

        async def node_url_fn(ws_id: str) -> str:
            return "http://node"

        with contextlib.suppress(asyncio.CancelledError):
            _run(
                _sse.run_sse_stream(
                    http_client=MagicMock(),
                    log_prefix="test",
                    ws_id="ws-1",
                    node_url_fn=node_url_fn,
                    token_factory=None,
                    on_event=AsyncMock(),
                    on_stale=AsyncMock(),
                )
            )

        assert fake_connect.call_count >= 3
        # First three recorded sleeps are 2s, 4s, 8s (starts at
        # SSE_RECONNECT_DELAY, doubles each time, capped at
        # SSE_MAX_RECONNECT_DELAY).
        assert _fast_sleep[0] == _sse.SSE_RECONNECT_DELAY
        assert _fast_sleep[1] == _sse.SSE_RECONNECT_DELAY * 2
        assert _fast_sleep[2] == _sse.SSE_RECONNECT_DELAY * 4

    def test_backoff_resets_after_successful_dispatch(self, monkeypatch, _fast_sleep):
        """After a 200 + successful event dispatch, the next error
        restarts backoff at the initial delay."""
        from turnstone.channels import _sse

        good_event = _FakeSSEEvent(event="message", data=_valid_event_data())
        queue = [
            _FakeEventSource(status_code=503, events=[]),
            _FakeEventSource(status_code=200, events=[good_event]),
            _FakeEventSource(status_code=503, events=[]),
        ]
        fake_connect = _FakeConnect(queue)
        monkeypatch.setattr(_sse.httpx_sse, "aconnect_sse", fake_connect)

        on_event = AsyncMock()

        async def node_url_fn(ws_id: str) -> str:
            return "http://node"

        with contextlib.suppress(asyncio.CancelledError):
            _run(
                _sse.run_sse_stream(
                    http_client=MagicMock(),
                    log_prefix="test",
                    ws_id="ws-1",
                    node_url_fn=node_url_fn,
                    token_factory=None,
                    on_event=on_event,
                    on_stale=AsyncMock(),
                )
            )

        on_event.assert_awaited()
        # Sleep sequence: 2 (after first 503), 2 (reset after 200/event),
        # then CancelledError exits.  First two sleeps are both the base
        # delay — the reset did its job.
        assert len(_fast_sleep) >= 2
        assert _fast_sleep[0] == _sse.SSE_RECONNECT_DELAY
        assert _fast_sleep[1] == _sse.SSE_RECONNECT_DELAY


# ---------------------------------------------------------------------------
# Event dispatch
# ---------------------------------------------------------------------------


class TestEventDispatch:
    def test_invalid_json_is_skipped(self, monkeypatch, _fast_sleep):
        from turnstone.channels import _sse

        bad = _FakeSSEEvent(event="message", data="{not json")
        good = _FakeSSEEvent(event="message", data=_valid_event_data())
        queue = [_FakeEventSource(status_code=200, events=[bad, good])]
        fake_connect = _FakeConnect(queue)
        monkeypatch.setattr(_sse.httpx_sse, "aconnect_sse", fake_connect)

        on_event = AsyncMock()

        async def node_url_fn(ws_id: str) -> str:
            return "http://node"

        with contextlib.suppress(asyncio.CancelledError):
            _run(
                _sse.run_sse_stream(
                    http_client=MagicMock(),
                    log_prefix="test",
                    ws_id="ws-1",
                    node_url_fn=node_url_fn,
                    token_factory=None,
                    on_event=on_event,
                    on_stale=AsyncMock(),
                )
            )

        # Good event delivered, bad one silently dropped.
        assert on_event.await_count == 1

    def test_on_event_exception_does_not_kill_stream(self, monkeypatch, _fast_sleep):
        from turnstone.channels import _sse

        e1 = _FakeSSEEvent(event="message", data=_valid_event_data())
        e2 = _FakeSSEEvent(event="message", data=_valid_event_data())
        queue = [_FakeEventSource(status_code=200, events=[e1, e2])]
        fake_connect = _FakeConnect(queue)
        monkeypatch.setattr(_sse.httpx_sse, "aconnect_sse", fake_connect)

        on_event = AsyncMock(side_effect=[RuntimeError("boom"), None])

        async def node_url_fn(ws_id: str) -> str:
            return "http://node"

        with contextlib.suppress(asyncio.CancelledError):
            _run(
                _sse.run_sse_stream(
                    http_client=MagicMock(),
                    log_prefix="test",
                    ws_id="ws-1",
                    node_url_fn=node_url_fn,
                    token_factory=None,
                    on_event=on_event,
                    on_stale=AsyncMock(),
                )
            )

        # Both events attempted — first raised but second still delivered.
        assert on_event.await_count == 2


# ---------------------------------------------------------------------------
# Token factory
# ---------------------------------------------------------------------------


class TestTokenFactory:
    def test_header_refreshed_per_connection(self, monkeypatch, _fast_sleep):
        """token_factory is called once per reconnect so rotating service
        JWTs stay fresh."""
        from turnstone.channels import _sse

        # Two reconnects followed by CancelledError to exit.
        queue = [
            _FakeEventSource(status_code=503, events=[]),
            _FakeEventSource(status_code=503, events=[]),
        ]
        fake_connect = _FakeConnect(queue)
        monkeypatch.setattr(_sse.httpx_sse, "aconnect_sse", fake_connect)

        tokens: list[str] = []

        def factory() -> str:
            tok = f"tok-{len(tokens)}"
            tokens.append(tok)
            return tok

        async def node_url_fn(ws_id: str) -> str:
            return "http://node"

        with contextlib.suppress(asyncio.CancelledError):
            _run(
                _sse.run_sse_stream(
                    http_client=MagicMock(),
                    log_prefix="test",
                    ws_id="ws-1",
                    node_url_fn=node_url_fn,
                    token_factory=factory,
                    on_event=AsyncMock(),
                    on_stale=AsyncMock(),
                )
            )

        assert len(tokens) >= 2
        assert tokens[0] != tokens[1]


# ---------------------------------------------------------------------------
# httpx errors
# ---------------------------------------------------------------------------


class TestTransportErrors:
    def test_connect_error_falls_through_to_backoff(self, monkeypatch, _fast_sleep):
        """ConnectError is caught and treated as retryable."""
        from turnstone.channels import _sse

        call_order = {"n": 0}

        def fake_connect(*args, **kwargs):  # noqa: ANN001, ANN003
            call_order["n"] += 1
            if call_order["n"] == 1:
                raise httpx.ConnectError("boom")
            # Second attempt: signal the loop to exit.
            raise asyncio.CancelledError

        monkeypatch.setattr(_sse.httpx_sse, "aconnect_sse", fake_connect)

        async def node_url_fn(ws_id: str) -> str:
            return "http://node"

        with contextlib.suppress(asyncio.CancelledError):
            _run(
                _sse.run_sse_stream(
                    http_client=MagicMock(),
                    log_prefix="test",
                    ws_id="ws-1",
                    node_url_fn=node_url_fn,
                    token_factory=None,
                    on_event=AsyncMock(),
                    on_stale=AsyncMock(),
                )
            )

        assert call_order["n"] == 2
        # Backoff ran once after the ConnectError.
        assert _fast_sleep == [_sse.SSE_RECONNECT_DELAY]
