"""Offline pins of the SDK boundary behaviors the #937 retry design rests on.

Four facts, each probed against the REAL SDKs over mock/loopback
transports (no network, no live backend):

1. OpenAI v3's ``max_retries`` covers request time only — a mid-BODY death
   produces no re-request, and the raw ``httpx2.ReadError`` escapes both Chat
   Completions and Responses chunk iterators unwrapped.
2. OpenAI v3's runtime-only legacy-client path preserves the old ``httpx``
   exception family when an application explicitly injects that client.
3. The Anthropic ``messages.stream()`` helper propagates the ``httpx`` shape.
4. Closing an OpenAI v3 default client from another thread while a read is
   blocked (the ``ModelRegistry.reload()`` shape) completes safely; a later
   wire release surfaces as an ``httpx2.TransportError`` on the blocked
   ``next()``. The production ``transport_guarded`` seam must normalize it
   before the retry gate.

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
import httpx2
import openai
import pytest

LF = chr(10)
CRLF = chr(13) + chr(10)

CHAT_CHUNK = (
    'data: {"id":"x","object":"chat.completion.chunk","created":0,'
    '"model":"m","choices":[{"index":0,"delta":{"content":"hello"},'
    '"finish_reason":null}]}' + LF + LF
)

RESPONSES_EVENT = (
    'data: {"type":"response.output_text.delta","sequence_number":0,'
    '"item_id":"item_1","output_index":0,"content_index":0,'
    '"delta":"hello","logprobs":[]}' + LF + LF
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


class _Httpx2DyingStream(httpx2.SyncByteStream):
    """HTTPX2 response body: one SSE payload, then a wire death."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __iter__(self):
        yield self._payload
        raise httpx2.ReadError("[SSL] record layer failure (_ssl.c:2590)")


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


def _httpx2_dying_transport(payload: str, requests: list) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_Httpx2DyingStream(payload.encode()),
            request=request,
        )

    return httpx2.MockTransport(handler)


def test_openai_v3_chat_midbody_death_is_unwrapped_httpx2_error_and_no_rerequest():
    requests: list = []
    client = openai.OpenAI(
        api_key="probe",
        base_url="http://probe.invalid/v1",
        http_client=httpx2.Client(transport=_httpx2_dying_transport(CHAT_CHUNK, requests)),
        max_retries=2,
    )
    stream = client.chat.completions.create(
        model="m", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    texts = []
    with pytest.raises(httpx2.ReadError) as excinfo:
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                texts.append(chunk.choices[0].delta.content)
    # The retry gate matches on the class NAME; pin the exact identity the
    # SDK lets escape, and that it is the HTTPX2 transport family.
    assert type(excinfo.value).__name__ == "ReadError"
    assert isinstance(excinfo.value, httpx2.TransportError)
    assert texts == ["hello"]  # the request succeeded; the BODY died
    assert len(requests) == 1  # max_retries never re-requested mid-body


def test_openai_v3_responses_midbody_death_is_unwrapped_httpx2_error_and_no_rerequest():
    requests: list = []
    client = openai.OpenAI(
        api_key="probe",
        base_url="http://probe.invalid/v1",
        http_client=httpx2.Client(transport=_httpx2_dying_transport(RESPONSES_EVENT, requests)),
        max_retries=2,
    )
    stream = client.responses.create(model="m", input="hi", stream=True)
    texts = []
    with pytest.raises(httpx2.ReadError) as excinfo:
        for event in stream:
            if event.type == "response.output_text.delta":
                texts.append(event.delta)
    assert type(excinfo.value).__name__ == "ReadError"
    assert isinstance(excinfo.value, httpx2.TransportError)
    assert texts == ["hello"]
    assert len(requests) == 1


def test_openai_v3_legacy_httpx_midbody_death_keeps_legacy_error_family():
    requests: list = []
    legacy_http_client = httpx.Client(transport=_dying_transport(CHAT_CHUNK, requests))
    client = openai.OpenAI(
        api_key="probe",
        base_url="http://probe.invalid/v1",
        http_client=legacy_http_client,  # type: ignore[arg-type]
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
    assert type(excinfo.value).__name__ == "ReadError"
    assert isinstance(excinfo.value, httpx.TransportError)
    assert texts == ["hello"]
    assert len(requests) == 1


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


@pytest.mark.parametrize("surface", ["chat", "responses"])
def test_closed_v3_default_client_creation_stays_sdk_wrapped_and_unarmed(surface: str):
    """A re-create on the client closed by reload is still a creation error.

    OpenAI v3 must wrap the default HTTPX2 client's ``RuntimeError`` as its
    retryable ``APIConnectionError`` before either adapter can arm the stream.
    This preserves the creation-vs-mid-stream classifier while the original
    normalized stream death remains available to the outer re-issue ladder.
    """
    from turnstone.core.providers import create_provider

    client = openai.OpenAI(
        api_key="probe",
        base_url="http://probe.invalid/v1",
        max_retries=0,
    )
    client.close()
    cancel_ref: list = []
    provider = create_provider("openai-compatible", api_surface=surface)

    with pytest.raises(openai.APIConnectionError) as excinfo:
        provider.create_streaming(
            client=client,
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            cancel_ref=cancel_ref,
        )

    assert cancel_ref == []
    assert type(excinfo.value).__name__ in provider.retryable_error_names
    assert type(excinfo.value.__cause__) is RuntimeError


@pytest.mark.parametrize(
    ("surface", "payload"),
    [("chat", CHAT_CHUNK), ("responses", RESPONSES_EVENT)],
)
def test_cross_thread_v3_default_client_close_then_wire_release_is_normalized(
    surface: str, payload: str
):
    """The ``ModelRegistry.reload()`` shape stays safe at the retry seam.

    OpenAI v3's synchronous HTTPX2 client does not promise that cross-thread
    ``close()`` itself interrupts a blocked body read. Pin the behavior
    Turnstone needs instead: closing from the admin thread completes safely
    while a worker is in ``next()``, and the subsequent wire release reaches
    ``transport_guarded`` as a provider-retryable ``IncompleteStreamError``
    for both OpenAI streaming adapters.
    """
    from turnstone.core.providers import create_provider, transport_guarded
    from turnstone.core.providers._protocol import IncompleteStreamError

    body = f"{len(payload):x}" + CRLF + payload + CRLF
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
    listener.settimeout(10.0)
    port = listener.getsockname()[1]
    release_peer = threading.Event()
    reader_blocked = threading.Event()
    close_done = threading.Event()
    first_content: list[str] = []
    reader_errors: list[BaseException] = []
    closer_errors: list[BaseException] = []
    server_errors: list[BaseException] = []

    def serve() -> None:
        try:
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(10.0)
                conn.recv(65536)
                conn.sendall((response_head + body).encode())
                # Keep the peer open through close_done: any reader error
                # before release_peer is therefore caused by close(), not EOF.
                if not release_peer.wait(timeout=15.0):
                    raise AssertionError("peer release was never signalled")
        except BaseException as exc:
            server_errors.append(exc)

    client = openai.OpenAI(
        api_key="probe",
        base_url=f"http://127.0.0.1:{port}/v1",
        max_retries=0,
        timeout=5.0,
    )

    def read_stream() -> None:
        try:
            provider = create_provider("openai-compatible", api_surface=surface)
            chunks = provider.create_streaming(
                client=client,
                model="m",
                messages=[{"role": "user", "content": "hi"}],
            )
            it = transport_guarded(chunks)
            first_content.append(next(it).content_delta)
            reader_blocked.set()
            next(it)
        except BaseException as exc:
            reader_errors.append(exc)

    def closer() -> None:
        try:
            if not reader_blocked.wait(timeout=10.0):
                raise AssertionError("reader never reached the blocked body read")
            time.sleep(0.5)  # let the reader enter the blocking socket read
            client.close()
        except BaseException as exc:
            closer_errors.append(exc)
        finally:
            close_done.set()

    server_thread = threading.Thread(target=serve)
    reader_thread = threading.Thread(target=read_stream)
    closer_thread = threading.Thread(target=closer)
    server_thread.start()
    reader_thread.start()
    closer_thread.start()
    try:
        assert reader_blocked.wait(timeout=10.0)
        assert close_done.wait(timeout=10.0)
        assert closer_errors == []
        # The server has not closed its peer yet, proving close() completed
        # safely rather than merely returning after an EOF unblocked it.
        assert server_errors == []
        release_peer.set()
        reader_thread.join(timeout=10.0)
    finally:
        # Unblock and join every thread on every exit path so nothing
        # outlives the test (leaked-thread guard).
        reader_blocked.set()
        release_peer.set()
        closer_thread.join(timeout=10.0)
        reader_thread.join(timeout=10.0)
        server_thread.join(timeout=10.0)
        listener.close()
        with contextlib.suppress(Exception):
            client.close()
    assert first_content == ["hello"]
    assert len(reader_errors) == 1
    exc = reader_errors[0]
    assert isinstance(exc, IncompleteStreamError)
    assert any(name in str(exc) for name in ("ReadError", "RemoteProtocolError"))
    cause = exc.__cause__
    assert isinstance(cause, httpx2.TransportError)
    assert type(cause).__name__ in {"ReadError", "RemoteProtocolError"}
    assert server_errors == []
    assert not closer_thread.is_alive()
    assert not reader_thread.is_alive()
    assert not server_thread.is_alive()


class TestEagerAppendContract:
    """Every adapter arms ``cancel_ref`` INSIDE ``create_streaming``'s body —
    at HTTP-response time, before the iterator is returned (the Protocol
    contract, strengthened on #832): the interactive wrapper's
    creation-vs-midstream classifier and its health recording key on that
    instant, and a lazily-issued generator adapter would silently move the
    arming to first ``next()``, misclassifying every pre-first-chunk death
    as a creation failure.  Real SDK clients over mock transports; the
    assertion deliberately runs BEFORE any iteration.
    """

    def _armed_at_return(self, provider, client, **extra):
        ref: list = []
        stream = provider.create_streaming(
            client=client,
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            cancel_ref=ref,
            **extra,
        )
        assert len(ref) == 1, "cancel_ref not armed before create_streaming returned"
        assert hasattr(ref[0], "close")
        with contextlib.suppress(Exception):
            stream.close()

    def test_openai_chat_arms_eagerly(self):
        from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider

        requests: list = []
        client = openai.OpenAI(
            api_key="probe",
            http_client=httpx2.Client(transport=_httpx2_dying_transport(CHAT_CHUNK, requests)),
        )
        self._armed_at_return(OpenAIChatCompletionsProvider(), client)
        assert len(requests) == 1  # the HTTP call happened inside create

    def test_openai_responses_arms_eagerly(self):
        from turnstone.core.providers._openai_responses import OpenAIResponsesProvider

        requests: list = []
        client = openai.OpenAI(
            api_key="probe",
            http_client=httpx2.Client(transport=_httpx2_dying_transport(RESPONSES_EVENT, requests)),
        )
        self._armed_at_return(OpenAIResponsesProvider(), client)
        assert len(requests) == 1

    def test_anthropic_arms_eagerly(self):
        from turnstone.core.providers._anthropic import AnthropicProvider

        requests: list = []
        client = anthropic.Anthropic(
            api_key="probe",
            http_client=httpx.Client(transport=_dying_transport(ANTHROPIC_EVENTS, requests)),
        )
        self._armed_at_return(AnthropicProvider(), client)
        assert len(requests) == 1
