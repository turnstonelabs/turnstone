"""Offline pins of the SDK boundary behaviors the #937 retry design rests on.

Three facts, each probed against the REAL SDKs over mock/loopback
transports (no network, no live backend):

1. The OpenAI SDK's ``max_retries`` covers request time only — a
   mid-BODY death produces no re-request, and the raw ``httpx.ReadError``
   escapes the chunk iterator unwrapped.
2. The Anthropic ``messages.stream()`` helper propagates the same shape.
3. Closing an httpx-backed SDK client from another thread while a read is
   blocked (the ``ModelRegistry.reload()`` shape) surfaces as an
   ``httpx.TransportError`` on the blocked ``next()`` — which is why the
   resilient ``_stream_response`` re-resolves the registry binding before
   re-creating.

If an SDK/httpx upgrade changes any of these, the ``transport_guarded``
conversion (and the retry gate consuming it) must be re-verified — these
tests are the tripwire.  Wire framing bytes (CRLF, the SSE double-LF) are
built via ``chr`` rather than escape literals so the payloads are
byte-exact regardless of source-encoding handling.
"""

import contextlib
import socket
import threading
import time

import anthropic
import httpx
import openai
import pytest

LF = chr(10)
CRLF = chr(13) + chr(10)

CHAT_CHUNK = (
    'data: {"id":"x","object":"chat.completion.chunk","created":0,'
    '"model":"m","choices":[{"index":0,"delta":{"content":"hello"},'
    '"finish_reason":null}]}' + LF + LF
)

ANTHROPIC_EVENTS = (
    "event: message_start"
    + LF
    + 'data: {"type":"message_start","message":{"id":"msg_01X","type":"message",'
    '"role":"assistant","model":"m","content":[],"stop_reason":null,'
    '"stop_sequence":null,"usage":{"input_tokens":10,"output_tokens":1}}}'
    + LF
    + LF
    + "event: content_block_start"
    + LF
    + 'data: {"type":"content_block_start","index":0,'
    '"content_block":{"type":"text","text":""}}'
    + LF
    + LF
    + "event: content_block_delta"
    + LF
    + 'data: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"text_delta","text":"hello"}}' + LF + LF
)


class _DyingStream(httpx.SyncByteStream):
    """Response body: one valid SSE payload, then a mid-read wire death."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __iter__(self):
        yield self._payload
        raise httpx.ReadError("[SSL] record layer failure (_ssl.c:2590)")


def _dying_transport(payload: str, requests: list) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_DyingStream(payload.encode()),
            request=request,
        )

    return httpx.MockTransport(handler)


def test_openai_midbody_death_is_unwrapped_readerror_and_no_rerequest():
    requests: list = []
    client = openai.OpenAI(
        api_key="probe",
        base_url="http://probe.invalid/v1",
        http_client=httpx.Client(transport=_dying_transport(CHAT_CHUNK, requests)),
        max_retries=2,
    )
    stream = client.chat.completions.create(
        model="m", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    texts = []
    with pytest.raises(httpx.ReadError) as excinfo:
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                texts.append(chunk.choices[0].delta.content)
    # The retry gate matches on the class NAME; pin the exact identity the
    # SDK lets escape, and that it is the httpx transport family.
    assert type(excinfo.value).__name__ == "ReadError"
    assert isinstance(excinfo.value, httpx.TransportError)
    assert texts == ["hello"]  # the request succeeded; the BODY died
    assert len(requests) == 1  # max_retries never re-requested mid-body


def test_anthropic_midbody_death_is_unwrapped_readerror_and_no_rerequest():
    requests: list = []
    client = anthropic.Anthropic(
        api_key="probe",
        base_url="http://probe.invalid",
        http_client=httpx.Client(transport=_dying_transport(ANTHROPIC_EVENTS, requests)),
        max_retries=2,
    )
    texts = []
    # The provider adapter consumes the stream() helper's iterator — same
    # shape as _anthropic.py's create_streaming.
    with (
        client.messages.stream(
            model="m", max_tokens=64, messages=[{"role": "user", "content": "hi"}]
        ) as stream,
        pytest.raises(httpx.ReadError) as excinfo,
    ):
        for event in stream:
            if getattr(event, "type", "") == "content_block_delta":
                delta = getattr(event, "delta", None)
                if getattr(delta, "type", "") == "text_delta":
                    texts.append(delta.text)
    assert type(excinfo.value).__name__ == "ReadError"
    assert isinstance(excinfo.value, httpx.TransportError)
    assert texts == ["hello"]
    assert len(requests) == 1


def test_cross_thread_client_close_surfaces_transport_error_to_blocked_read():
    """The ``ModelRegistry.reload()`` shape: an admin-thread ``client.close()``
    under a worker blocked in ``next()`` must surface as an
    ``httpx.TransportError`` (so ``transport_guarded`` converts it and the
    retry gate passes) — not as a plain ``RuntimeError`` the gate would
    treat as fatal."""
    body = f"{len(CHAT_CHUNK):x}" + CRLF + CHAT_CHUNK + CRLF
    response_head = (
        "HTTP/1.1 200 OK"
        + CRLF
        + "Content-Type: text/event-stream"
        + CRLF
        + "Transfer-Encoding: chunked"
        + CRLF
        + CRLF
    )
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    client_closed = threading.Event()
    reader_blocked = threading.Event()

    def serve() -> None:
        conn, _ = listener.accept()
        conn.recv(65536)
        conn.sendall((response_head + body).encode())
        # Hold the connection (no second chunk) so the reader blocks, and
        # release only once the client has been closed under it.
        client_closed.wait(timeout=10.0)
        conn.close()

    client = openai.OpenAI(api_key="probe", base_url=f"http://127.0.0.1:{port}/v1", max_retries=0)

    def closer() -> None:
        reader_blocked.wait(timeout=10.0)
        time.sleep(0.5)  # let the reader enter the blocking socket read
        client.close()
        client_closed.set()

    server_thread = threading.Thread(target=serve)
    closer_thread = threading.Thread(target=closer)
    server_thread.start()
    closer_thread.start()
    try:
        stream = client.chat.completions.create(
            model="m", messages=[{"role": "user", "content": "hi"}], stream=True
        )
        it = iter(stream)
        first = next(it)  # the one sent chunk arrives; the wire then idles
        assert first.choices[0].delta.content == "hello"
        reader_blocked.set()
        with pytest.raises(httpx.TransportError) as excinfo:
            next(it)  # blocked read, killed by the cross-thread close()
        assert type(excinfo.value).__name__ == "ReadError"
    finally:
        # Unblock and join both threads on every exit path so nothing
        # outlives the test (leaked-thread guard).
        reader_blocked.set()
        client_closed.set()
        closer_thread.join(timeout=10.0)
        server_thread.join(timeout=10.0)
        listener.close()
        with contextlib.suppress(Exception):
            client.close()
    assert not closer_thread.is_alive()
    assert not server_thread.is_alive()
