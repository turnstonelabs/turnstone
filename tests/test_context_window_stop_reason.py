"""Anthropic's ``model_context_window_exceeded`` stop reason is an overflow (#1162).

A response that ends on that reason is an HTTP 200 whose content stops where
the context window ended.  It must never be persisted as a clean completion:
the adapter surfaces it as :class:`ContextWindowExceededError`, the overflow
predicate recognizes the class, and the send loop's compact-and-retry arm
recovers requests known to disable native tools. The cases use the real SDK
over a mock transport and the real adapter under the session loop.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from unittest.mock import patch

import httpx2
import pytest
from anthropic import Anthropic

from tests._session_helpers import (
    FakeAnthropicBlock,
    NullUI,
    make_session,
    replace_session_lane,
    scripted_anthropic_client,
)
from tests._wire_capture import ANTHROPIC_SSE_STREAM, anthropic_body_capture_client
from turnstone.core.completion_recovery import (
    is_context_overflow,
)
from turnstone.core.providers import (
    ContextWindowExceededError,
    IncompleteStreamError,
    create_provider,
    drain_stream,
    transport_guarded,
)
from turnstone.core.providers._anthropic import AnthropicProvider
from turnstone.core.storage import get_storage
from turnstone.core.trajectory import dicts_from_turns

_STOP = "model_context_window_exceeded"
_SSE_WINDOW_FULL = ANTHROPIC_SSE_STREAM.replace(
    '"stop_reason":"end_turn"', f'"stop_reason":"{_STOP}"'
)
assert _SSE_WINDOW_FULL != ANTHROPIC_SSE_STREAM


def _stream(client: Any) -> Any:
    return create_provider("anthropic").create_streaming(
        client=client, model="claude-sonnet-4-6", messages=[{"role": "user", "content": "hi"}]
    )


def test_real_sdk_stop_reason_surfaces_as_the_overflow_error_after_the_partial_text() -> None:
    """The SDK passes the new stop reason through; the adapter yields the
    partial content it streamed, then the terminal event's usage (the failed
    attempt's spend is billed and must reach accounting), and only then
    raises instead of finishing."""
    client = anthropic_body_capture_client({}, sse=_SSE_WINDOW_FULL)
    delivered: list[str] = []
    completion_tokens: list[int] = []
    try:
        with pytest.raises(ContextWindowExceededError, match="context window exceeded"):
            for chunk in _stream(client):
                if chunk.content_delta:
                    delivered.append(chunk.content_delta)
                if chunk.usage is not None:
                    completion_tokens.append(chunk.usage.completion_tokens)
    finally:
        client.close()
    assert delivered == ["hello"]
    assert completion_tokens == [0, 1]  # message_start, then the terminal message_delta


def test_drain_gate_never_blesses_the_cut_off_turn() -> None:
    """``transport_guarded`` converts only transport deaths, so the overflow
    reaches the drain as itself — not as the retryable IncompleteStreamError,
    which would re-issue the same over-long prompt."""
    client = anthropic_body_capture_client({}, sse=_SSE_WINDOW_FULL)
    try:
        with pytest.raises(ContextWindowExceededError) as excinfo:
            drain_stream(transport_guarded(_stream(client)))
    finally:
        client.close()
    assert not isinstance(excinfo.value, IncompleteStreamError)
    assert type(excinfo.value).__name__ not in AnthropicProvider().retryable_error_names


def test_overflow_predicate_recognizes_the_class_without_the_phrase_match() -> None:
    assert is_context_overflow(ContextWindowExceededError("model stopped")) is True
    assert is_context_overflow(RuntimeError("model stopped")) is False


@pytest.mark.parametrize("reject_first", [False, True])
def test_summary_shrinks_accepted_native_overflow(tmp_db, reject_first) -> None:
    """An overflow on a lane that runs native tools still shrinks: subdividing
    or shortening the batch is a different request, not a replay of the cut-off
    one, whether the overflow was a 400 rejection or an accepted stop reason."""
    requests: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if len(requests) > 1:
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=ANTHROPIC_SSE_STREAM,
                request=request,
            )
        if reject_first:
            return httpx2.Response(
                400,
                json={"error": {"type": "invalid_request_error", "message": "prompt is too long"}},
                request=request,
            )
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=_SSE_WINDOW_FULL,
            request=request,
        )

    with Anthropic(
        api_key="offline-test-only",
        base_url="https://provider.example.com",
        http_client=httpx2.Client(transport=httpx2.MockTransport(respond)),
        max_retries=0,
    ) as client:
        session = make_session(client=client)
        lane = replace_session_lane(
            session, client=client, provider=AnthropicProvider(compat=True), model="test-model"
        )
        lane = replace(
            lane,
            extra_params={
                "mcp_servers": [
                    {"type": "url", "name": "tools", "url": "https://tools.example.com/mcp"}
                ]
            },
        )
        runtime = session._build_summary_runtime(lane, continuation_overhead_tokens=0)
        # A lone block shortens its head+tail; two blocks split in half.
        batch = ["one block " * 2000] if reject_first else ["left " * 1000, "right " * 1000]
        result = session._compaction_engine.summarize_batch("Summarize.", batch, 0, runtime)
        assert "hello" in result.text
        assert len(requests) >= 2
        assert len(requests[1].content) < len(requests[0].content)


class _BufferUI(NullUI):
    def on_state_change(self, state: str) -> None:
        pass


@pytest.mark.parametrize("native_tools", [False, True])
def test_send_loop_compacts_accepted_overflow_at_any_tool_posture(tmp_db, native_tools) -> None:
    """Through the REAL adapter: the first response ends on the stop reason,
    compaction runs once, the retried response is what gets persisted, and the
    dead partial text never reaches the turn buffer. A request that could run
    native tools compacts too: shrinking the next request is not a replay."""
    ui = _BufferUI()
    session = make_session(ui=ui)
    get_storage().register_workstream(session.ws_id, user_id=session._user_id, kind=session._kind)
    session._RETRY_BASE_DELAY = 0
    session._title_generated = True
    provider = AnthropicProvider()
    client = replace_session_lane(
        session,
        provider=provider,
        capabilities=replace(
            provider.get_capabilities("test-model"), supports_web_search=native_tools
        ),
    ).client
    client_fn = scripted_anthropic_client(
        {"blocks": [FakeAnthropicBlock(type="text", text="dead partial")], "stop_reason": _STOP},
        {"blocks": [FakeAnthropicBlock(type="text", text="recovered")]},
    )
    client.messages.stream = client_fn

    with patch.object(session, "_compact_messages") as compact:
        session.send("test")

    compact.assert_called_once()
    assert len(client_fn.calls) == 2
    assistant = [m for m in dicts_from_turns(session.messages) if m["role"] == "assistant"]
    assert assistant[-1]["content"] == "recovered"
    assert "".join(ui._ws_turn_content) == "recovered"
