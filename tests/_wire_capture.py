"""Recording fake SDK client — captures the kwargs at each provider's seam.

Every provider's ``create_streaming`` assembles its kwargs and calls the
SDK *eagerly* before returning the stream iterator (Anthropic
``client.messages.stream``, OpenAI ``client.chat.completions.create``,
Responses ``client.responses.create/stream``), so driving a provider
against a :class:`RecordingClient` captures the full composed request
payload without a network round-trip.

Shared by the wire-payload golden harness (``test_wire_payload_golden``)
and the effort-ladder parity harness (``test_effort_ladder_wire_parity``)
so both assert against the same capture seam.

:func:`anthropic_body_capture_client` is the real-SDK counterpart for the
Anthropic lane: a genuine ``anthropic.Anthropic`` over an HTTPX2 mock
transport that records the JSON body and the headers the SDK actually put
on the wire — for pins whose subject is the SDK's own merge (``extra_body``,
``extra_headers``), which the fake above never performs.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator


class _EmptyStream:
    """Stand-in for an SDK stream / stream-manager: empty iterable AND no-op CM."""

    def __iter__(self) -> Iterator[Any]:
        return iter(())

    def __enter__(self) -> _EmptyStream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _Seam:
    """Records the kwargs of a single SDK call, returns an empty stream stub."""

    def __init__(self, sink: dict[str, Any]) -> None:
        self._sink = sink

    def __call__(self, **kwargs: Any) -> _EmptyStream:
        # Last write wins; only one seam is exercised per provider call.
        self._sink["payload"] = kwargs
        return _EmptyStream()


class _Completions:
    def __init__(self, sink: dict[str, Any]) -> None:
        self.create = _Seam(sink)


class _Chat:
    def __init__(self, sink: dict[str, Any]) -> None:
        self.completions = _Completions(sink)


class _Messages:
    def __init__(self, sink: dict[str, Any]) -> None:
        self.stream = _Seam(sink)


class _Responses:
    def __init__(self, sink: dict[str, Any]) -> None:
        self.create = _Seam(sink)
        self.stream = _Seam(sink)


class RecordingClient:
    """Fake SDK client exposing every provider's call seam, recording kwargs."""

    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}
        self.messages = _Messages(self.captured)
        self.chat = _Chat(self.captured)
        self.responses = _Responses(self.captured)


# -- real Anthropic SDK over an HTTPX2 mock transport ------------------------

_LF = chr(10)

# One complete Messages SSE stream: a message_start with usage, one text block
# saying "hello", and the terminal message_delta / message_stop pair.  Framing
# bytes are built via ``chr`` so the payload is byte-exact regardless of
# source-encoding handling.
ANTHROPIC_SSE_STREAM = (
    "event: message_start"
    + _LF
    + 'data: {"type":"message_start","message":{"id":"msg_01X","type":"message",'
    '"role":"assistant","model":"m","content":[],"stop_reason":null,'
    '"stop_sequence":null,"usage":{"input_tokens":10,"output_tokens":1}}}'
    + _LF
    + _LF
    + "event: content_block_start"
    + _LF
    + 'data: {"type":"content_block_start","index":0,'
    '"content_block":{"type":"text","text":""}}'
    + _LF
    + _LF
    + "event: content_block_delta"
    + _LF
    + 'data: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"text_delta","text":"hello"}}'
    + _LF
    + _LF
    + "event: message_delta"
    + _LF
    + 'data: {"type":"message_delta","delta":{"stop_reason":"end_turn",'
    '"stop_sequence":null},"usage":{"output_tokens":1}}'
    + _LF
    + _LF
    + "event: message_stop"
    + _LF
    + 'data: {"type":"message_stop"}'
    + _LF
    + _LF
)


def anthropic_body_capture_client(
    captured: dict[str, Any], *, sse: str = ANTHROPIC_SSE_STREAM
) -> Any:
    """Real Anthropic v1 client over an HTTPX2 mock transport.

    Every request is answered with *sse*; its decoded JSON body lands in
    ``captured["body"]`` and its headers (an ``httpx2.Headers``) in
    ``captured["headers"]``.  ``max_retries=0`` so a test never re-requests.
    Close the client after use.
    """
    import anthropic
    import httpx2

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse.encode(),
            request=request,
        )

    return anthropic.Anthropic(
        api_key="probe",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )
