"""Collector reachability-transition semantics.

Regression cover for the observability gap where the collector logged TLS /
connection failures at DEBUG, so a persistent mTLS-verify failure was invisible
at the default log level. ``_mark_unreachable`` now reports the first
(reachable→unreachable) transition so the SSE loop can log it at WARNING and
stay quiet on subsequent retries.

The node SSE loop tests drive ``_node_sse_task`` through an ``httpx2.MockTransport``: a failed
connection marks the node unreachable and the loop reconnects, and the loop's event-size cap is
``MAX_SSE_EVENT_SIZE``, not httpx2's 1 MiB default.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import httpx2
import pytest

from turnstone.console import collector as collector_module
from turnstone.console.collector import ClusterCollector, NodeSnapshot

if TYPE_CHECKING:
    from collections.abc import Callable

_NODE_URL = "http://node-1.example"
_EVENTS_URL = f"{_NODE_URL}/v1/api/events/global?expected_node_id=node-1"


def _collector() -> ClusterCollector:
    return ClusterCollector(storage=MagicMock())


def test_first_failure_is_a_transition_then_quiet():
    c = _collector()
    c._nodes["node-1"] = NodeSnapshot(node_id="node-1", reachable=True)

    # First failure flips reachable→unreachable → True (log at WARNING).
    assert c._mark_unreachable("node-1", reason="SSLCertVerificationError") is True
    assert c._nodes["node-1"].reachable is False
    assert c._nodes["node-1"].reachable_reason == "SSLCertVerificationError"

    # Still-down retries are not transitions → False (stay at DEBUG).
    assert c._mark_unreachable("node-1", reason="SSLCertVerificationError") is False


def test_recovery_then_failure_is_a_new_transition():
    c = _collector()
    c._nodes["node-1"] = NodeSnapshot(node_id="node-1", reachable=True)
    c._mark_unreachable("node-1", reason="ConnectError")

    # Node comes back (as _apply_snapshot does), then fails again → new transition.
    c._nodes["node-1"].reachable = True
    assert c._mark_unreachable("node-1", reason="ConnectError") is True


def test_unknown_node_is_not_a_transition():
    c = _collector()
    assert c._mark_unreachable("ghost", reason="ConnectError") is False


# ---------------------------------------------------------------------------
# The node SSE loop
# ---------------------------------------------------------------------------


@pytest.fixture
def backoffs(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the SSE loop's backoff sleeps instead of waiting them out."""
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def record(delay: float) -> None:
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(collector_module.asyncio, "sleep", record)
    return delays


def _node_collector() -> ClusterCollector:
    c = _collector()
    c._nodes["node-1"] = NodeSnapshot(node_id="node-1", server_url=_NODE_URL)
    return c


def _snapshot(*workstreams: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "node_snapshot",
        "node_id": "node-1",
        "health": {},
        "aggregate": {},
        "workstreams": list(workstreams),
    }


def _event_stream(*events: dict[str, Any]) -> httpx2.Response:
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, content=body.encode()
    )


def _run_node_stream(
    c: ClusterCollector, replies: list[Callable[[httpx2.Request], httpx2.Response]]
) -> list[httpx2.Request]:
    """Run node-1's SSE loop with one reply per connection, then stop it.

    The connection after the last reply sets the stop event and gets an empty stream, so the loop
    ends on its own. Returns every request the loop sent.
    """
    sent: list[httpx2.Request] = []

    async def run() -> None:
        stop = asyncio.Event()

        def handler(request: httpx2.Request) -> httpx2.Response:
            sent.append(request)
            if len(sent) > len(replies):
                stop.set()
                return _event_stream()
            return replies[len(sent) - 1](request)

        client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        c._sse_async_client = client
        c._running = True
        try:
            await asyncio.wait_for(c._node_sse_task("node-1", stop), timeout=10)
        finally:
            c._running = False
            c._sse_async_client = None
            await client.aclose()

    asyncio.run(run())
    return sent


def test_a_transport_error_marks_the_node_down_and_the_loop_reconnects(backoffs):
    c = _node_collector()
    seen_on_reconnect: list[tuple[bool, str]] = []

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("Connection refused", request=request)

    def snapshot(request: httpx2.Request) -> httpx2.Response:
        node = c._nodes["node-1"]
        seen_on_reconnect.append((node.reachable, node.reachable_reason))
        return _event_stream(_snapshot({"id": "ws-1", "state": "idle", "title": "one"}))

    sent = _run_node_stream(c, [refuse, snapshot])

    assert [str(request.url) for request in sent] == [_EVENTS_URL] * 3
    assert sent[0].headers["accept"] == "text/event-stream"
    assert seen_on_reconnect == [(False, "ConnectError")]
    assert len(backoffs) == 1
    node = c._nodes["node-1"]
    assert (node.reachable, node.reachable_reason) == (True, "")
    assert set(node.workstreams) == {"ws-1"}


def test_a_node_4xx_body_is_logged_without_control_characters(backoffs, caplog):
    # A compromised node could otherwise forge a log line, or send terminal escapes, through the
    # preview of its error reply.
    c = _node_collector()
    body = ("denied" + chr(13) + chr(10) + "forged line" + chr(27) + "[2K").encode()

    def denied(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(403, content=body)

    with caplog.at_level(logging.WARNING, logger="turnstone.console.collector"):
        _run_node_stream(c, [denied])

    [message] = [r.getMessage() for r in caplog.records if "SSE 403" in r.getMessage()]
    assert message.endswith("denied  forged line [2K")


def test_a_snapshot_naming_another_node_is_logged_escaped(backoffs, caplog):
    c = _node_collector()
    forged = "node-1" + chr(13) + chr(10) + "WARNING forged" + chr(27) + "[2K"

    def other_node(_request: httpx2.Request) -> httpx2.Response:
        return _event_stream({**_snapshot(), "node_id": forged})

    with caplog.at_level(logging.WARNING, logger="turnstone.console.collector"):
        _run_node_stream(c, [other_node])

    [message] = [r.getMessage() for r in caplog.records if "mismatch" in r.getMessage()]
    assert "forged" in message
    assert all(ch.isprintable() for ch in message)


def test_a_node_event_over_one_mebibyte_applies(backoffs):
    # httpx2's EventSource refuses an event over 1 MiB unless told otherwise.
    c = _node_collector()
    title = "x" * (2 * 1024 * 1024)

    def large_snapshot(_request: httpx2.Request) -> httpx2.Response:
        return _event_stream(_snapshot({"id": "ws-1", "state": "idle", "title": title}))

    _run_node_stream(c, [large_snapshot])

    node = c._nodes["node-1"]
    assert (node.reachable, node.reachable_reason) == (True, "")
    assert node.workstreams["ws-1"]["title"] == title
    assert backoffs == []


def test_a_node_event_over_the_cap_fails_the_connection(backoffs, monkeypatch):
    monkeypatch.setattr(collector_module, "MAX_SSE_EVENT_SIZE", 1024)
    c = _node_collector()

    def large_snapshot(_request: httpx2.Request) -> httpx2.Response:
        return _event_stream(_snapshot({"id": "ws-1", "state": "idle", "title": "x" * 2048}))

    _run_node_stream(c, [large_snapshot])

    node = c._nodes["node-1"]
    assert (node.reachable, node.reachable_reason) == (False, "SSEError")
    assert node.workstreams == {}
    assert len(backoffs) == 1
