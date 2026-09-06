"""Offline pins of SDK boundary behaviors used by stream error handling.

Six facts, each probed against the REAL SDKs over mock/loopback
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
5. OpenAI v3 raises real HTTP errors before returning a stream, while an HTTP
   200 ``application/json`` response becomes an empty iterator unless the
   adapter rejects it before arming the stream.
6. Refusal text is visible without inventing an early terminal signal, and
   neither structured-output judge interprets a filtered response as a verdict.

If an SDK/httpx upgrade changes any of these, the provider boundary and
``transport_guarded`` conversion (including the retry gate consuming it) must
be re-verified — these tests are the tripwire. Wire framing bytes (CRLF, the
SSE double-LF) are built via ``chr`` rather than escape literals so the
payloads are byte-exact regardless of source-encoding handling.
"""

import contextlib
import json
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import anthropic
import httpx
import httpx2
import openai
import pytest

from turnstone.core.providers._openai_common import (
    UpstreamRateLimitError,
    UpstreamResponseError,
    UpstreamTransientError,
)

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


@pytest.mark.parametrize("parts", [["Cannot", " help."], ["", "Cannot", "", " help.", ""]])
@pytest.mark.parametrize(
    "ending", ["finish", "finishless", "pre_finish_death", "post_finish_death"]
)
@pytest.mark.parametrize("optional_finish", [False, True])
def test_chat_refusal_is_visible_without_inventing_a_terminal(parts, ending, optional_finish):
    from turnstone.core.providers import (
        IncompleteStreamError,
        ModelCapabilities,
        create_provider,
        drain_stream,
        transport_guarded,
    )

    chunks = [{"delta": {"refusal": part}, "finish_reason": None} for part in parts]
    if ending in ("finish", "post_finish_death"):
        chunks.append({"delta": {}, "finish_reason": "stop"})
    payload = "".join(
        "data: "
        + json.dumps(
            {
                "id": "refusal",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "test",
                "choices": [{"index": 0, **choice}],
            }
        )
        + "\n\n"
        for choice in chunks
    ).encode()
    body = (
        _Httpx2DyingStream(payload)
        if "death" in ending
        else httpx2.ByteStream(payload + b"data: [DONE]\n\n")
    )
    with openai.OpenAI(
        api_key="test-only",
        max_retries=0,
        http_client=httpx2.Client(
            transport=httpx2.MockTransport(
                lambda request: httpx2.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=body,
                )
            )
        ),
    ) as client:
        stream = create_provider("openai-compatible").create_streaming(
            client=client,
            model="test",
            messages=[{"role": "user", "content": "test"}],
            capabilities=ModelCapabilities(finish_reason_optional=optional_finish),
        )
        if ending == "pre_finish_death" or (ending == "finishless" and not optional_finish):
            with pytest.raises(IncompleteStreamError):
                drain_stream(transport_guarded(stream))
        else:
            result = drain_stream(transport_guarded(stream))
            assert result.content == "[Refused: " + "".join(parts) + "]"
            assert result.finish_reason == "content_filter"


@pytest.mark.parametrize("refusal", [None, ""])
@pytest.mark.parametrize("content", ["Hello world", ""])
@pytest.mark.parametrize("finish", ["stop", "content_filter", None])
def test_empty_chat_refusal_field_does_not_rewrite_content_or_finish(refusal, content, finish):
    from turnstone.core.providers import (
        IncompleteStreamError,
        ModelCapabilities,
        create_provider,
        drain_stream,
        transport_guarded,
    )

    chunks = [({"content": content, "refusal": refusal}, None)]
    if finish is not None:
        chunks.append(({}, finish))
    payload = (
        "".join(
            "data: "
            + json.dumps(
                {
                    "id": "empty-refusal",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "test",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
                }
            )
            + "\n\n"
            for delta, reason in chunks
        )
        + "data: [DONE]\n\n"
    )
    with openai.OpenAI(
        api_key="test-only",
        max_retries=0,
        http_client=httpx2.Client(
            transport=httpx2.MockTransport(
                lambda request: httpx2.Response(
                    200, headers={"content-type": "text/event-stream"}, text=payload
                )
            )
        ),
    ) as client:
        stream = create_provider("openai-compatible").create_streaming(
            client=client,
            model="test",
            messages=[{"role": "user", "content": "test"}],
            capabilities=ModelCapabilities(finish_reason_optional=finish is None),
        )
        if finish is None and not content:
            # A metadata-only stream still has no evidence of completion;
            # the compatibility shim requires an actual content chunk.
            with pytest.raises(IncompleteStreamError):
                drain_stream(transport_guarded(stream))
            return
        result = drain_stream(transport_guarded(stream))
    assert result.content == content
    assert result.finish_reason == (finish or "stop")


def test_chat_refusal_after_unclosed_inline_reasoning_cannot_be_retried():
    from turnstone.core.model_turn import ModelLane, is_empty_completion, model_turn
    from turnstone.core.providers import create_provider
    from turnstone.core.trajectory import Turn

    deltas = [
        ({"content": "<think>unfinished reasoning"}, None),
        ({"refusal": "Cannot help."}, None),
        ({}, "stop"),
    ]
    payload = (
        "".join(
            "data: "
            + json.dumps(
                {
                    "id": "refusal",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "test",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
            )
            + "\n\n"
            for delta, finish in deltas
        )
        + "data: [DONE]\n\n"
    )
    with openai.OpenAI(
        api_key="test-only",
        max_retries=0,
        http_client=httpx2.Client(
            transport=httpx2.MockTransport(
                lambda request: httpx2.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    text=payload,
                )
            )
        ),
    ) as client:
        result = model_turn(
            ModelLane(provider=create_provider("openai-compatible"), client=client, model="test"),
            [Turn.user("Continue.")],
        )
    assert not is_empty_completion(result)
    assert result.finish_reason == "content_filter"


@pytest.mark.parametrize("judge_kind", ["intent", "output"])
@pytest.mark.parametrize("quoted_verdict", [False, True])
@pytest.mark.parametrize("finish", ["stop", "length", "content_filter"])
def test_judges_reject_chat_refusals_before_parsing_or_reprompting(
    judge_kind, quoted_verdict, finish
):
    from turnstone.core.judge import IntentJudge, JudgeConfig
    from turnstone.core.model_turn import ModelLane, ResolvedModelBinding
    from turnstone.core.output_guard_judge import OutputGuardJudge
    from turnstone.core.providers import create_provider

    refusal = "I cannot evaluate this request."
    if quoted_verdict:
        refusal += " I cannot return " + json.dumps(
            {
                "risk_level": "low" if judge_kind == "intent" else "none",
                "recommendation": "approve",
                "confidence": 0.99,
                "reasoning": "Allowed.",
                "flags": [],
            }
        )
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        chunks = [({"refusal": refusal}, None), ({}, finish)]
        payload = "".join(
            "data: "
            + json.dumps(
                {
                    "id": "refusal",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "test",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
            )
            + "\n\n"
            for delta, finish in chunks
        )
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=payload + "data: [DONE]\n\n",
        )

    with openai.OpenAI(
        api_key="test-only",
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(respond)),
    ) as client:
        provider = create_provider("openai-compatible")
        binding = ResolvedModelBinding(
            lane=ModelLane(
                provider=provider,
                client=client,
                model="test",
                capabilities=provider.get_capabilities("test"),
            ),
            config=None,
            registry_generation=0,
        )
        if judge_kind == "intent":
            judge = IntentJudge(
                config=JudgeConfig(enabled=True, read_only_tools=False),
                session_binding=binding,
            )
            verdict = judge._evaluate_single(
                {"func_name": "bash", "func_args": {"command": "ls"}, "call_id": "call"},
                [{"role": "user", "content": "Inspect the directory."}],
                None,
                client,
                lane=binding.lane,
            )
            assert verdict is None
        else:
            guard = OutputGuardJudge(
                config=JudgeConfig(output_guard_llm=True), session_binding=binding
            )
            try:
                with patch.object(guard, "_create_client", return_value=client):
                    output = guard.evaluate("Untrusted tool output.", func_name="web_fetch")
                assert not output.succeeded
                assert output.error == ("length" if finish == "length" else "content_filter")
            finally:
                guard.close()
    assert len(requests) == 1


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


class _BlockingJsonStream(httpx2.SyncByteStream):
    """JSON body that blocks until cross-thread close releases it."""

    def __init__(self) -> None:
        self.read_started = threading.Event()
        self.closed = threading.Event()

    def __iter__(self):
        self.read_started.set()
        if not self.closed.wait(5):
            raise AssertionError("JSON response body was not cancelled")
        raise httpx2.ReadError("JSON response closed by cancellation")
        yield b""  # pragma: no cover - make this a generator

    def close(self) -> None:
        self.closed.set()


class _OversizedJsonStream(httpx2.SyncByteStream):
    """Unbounded-looking body that records how far the guard consumes."""

    def __init__(self) -> None:
        self.read_count = 0
        self.closed = False

    def __iter__(self):
        yield b'{"error":{"message":"'
        for _ in range(100):
            self.read_count += 1
            yield b"x" * 1024
        raise AssertionError("bounded JSON reader consumed the response to EOF")

    def close(self) -> None:
        self.closed = True


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


@pytest.mark.parametrize("surface", ["chat", "responses"])
@pytest.mark.parametrize(
    ("status_code", "message", "error_type"),
    [
        (
            200,
            "request (9870 tokens) exceeds the available context size (4096 tokens)",
            UpstreamResponseError,
        ),
        (400, "request exceeds the available context size", openai.BadRequestError),
        (413, "payload too large", openai.APIStatusError),
        (429, "rate limit reached", openai.RateLimitError),
    ],
)
def test_openai_v3_json_error_precedes_stream_arming(
    surface: str,
    status_code: int,
    message: str,
    error_type: type[Exception],
):
    """JSON failures retain their status and body before stream iteration.

    The SDK owns real HTTP failures. Turnstone handles the compatibility case
    where the endpoint returns the same payload under HTTP 200. Neither may
    reach the finish-reason gate and become ``IncompleteStreamError``.
    """
    from turnstone.core.providers import create_provider

    requests: list = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            status_code,
            headers={"content-type": "application/json"},
            json={"error": {"message": message, "type": "upstream_error"}},
            request=request,
        )

    client = openai.OpenAI(
        api_key="probe",
        base_url="http://probe.invalid/v1",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )
    cancel_ref: list = []
    provider = create_provider("openai-compatible", api_surface=surface)
    try:
        with pytest.raises(error_type) as excinfo:
            provider.create_streaming(
                client=client,
                model="m",
                messages=[{"role": "user", "content": "hi"}],
                cancel_ref=cancel_ref,
            )
    finally:
        client.close()

    assert getattr(excinfo.value, "status_code", None) == status_code
    assert message in str(excinfo.value)
    assert cancel_ref == []
    assert len(requests) == 1


@pytest.mark.parametrize("surface", ["chat", "responses"])
@pytest.mark.parametrize(
    ("code", "error_type", "expected_error"),
    [
        ("rate_limit_exceeded", "rate_limit_error", UpstreamRateLimitError),
        ("server_error", "server_error", UpstreamTransientError),
    ],
)
def test_http_200_json_transient_error_keeps_retry_classification(
    surface: str,
    code: str,
    error_type: str,
    expected_error: type[UpstreamResponseError],
):
    from turnstone.core.providers import create_provider

    message = "maximum number of tokens allowed per minute is temporarily unavailable"

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"content-type": "application/json"},
            json={"error": {"message": message, "type": error_type, "code": code}},
            request=request,
        )

    client = openai.OpenAI(
        api_key="probe",
        base_url="http://probe.invalid/v1",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )
    cancel_ref: list = []
    provider = create_provider("openai-compatible", api_surface=surface)
    try:
        with pytest.raises(expected_error) as excinfo:
            provider.create_streaming(
                client=client,
                model="m",
                messages=[{"role": "user", "content": "hi"}],
                cancel_ref=cancel_ref,
            )
    finally:
        client.close()

    assert excinfo.value.code == code
    assert excinfo.value.error_type == error_type
    assert type(excinfo.value).__name__ in provider.retryable_error_names
    assert cancel_ref == []


@pytest.mark.parametrize(
    "payload",
    [
        {"message": {"role": "assistant", "content": "generated text must stay private"}},
        {
            "error": None,
            "choices": [{"message": {"content": "generated text must stay private"}}],
        },
    ],
)
def test_non_stream_ambiguous_json_omits_generated_content_from_error(payload: dict):
    from turnstone.core.providers._openai_common import reject_non_stream_response

    private_output = "generated text must not enter diagnostics"
    payload_text = json.dumps(payload).replace("generated text must stay private", private_output)
    response = httpx2.Response(
        200,
        headers={"content-type": "application/json"},
        content=payload_text.encode(),
    )
    stream = SimpleNamespace(response=response, close=response.close)

    with pytest.raises(UpstreamResponseError) as excinfo:
        reject_non_stream_response(stream)

    assert excinfo.value.body == "<JSON response did not contain a valid error object>"
    assert private_output not in str(excinfo.value)


def test_non_stream_unparseable_json_fails_closed():
    from turnstone.core.providers._openai_common import reject_non_stream_response

    private_output = "generated text must not enter diagnostics"
    response = httpx2.Response(
        200,
        headers={"content-type": "application/json"},
        content=f"not-json {private_output}".encode(),
    )
    stream = SimpleNamespace(response=response, close=response.close)

    with pytest.raises(UpstreamResponseError) as excinfo:
        reject_non_stream_response(stream)

    assert excinfo.value.body == "<JSON response did not contain a valid error object>"
    assert private_output not in str(excinfo.value)


def test_non_stream_error_excludes_sibling_completion_content():
    from turnstone.core.providers._openai_common import reject_non_stream_response

    private_output = "generated text must not enter diagnostics"
    response = httpx2.Response(
        200,
        headers={"content-type": "application/json"},
        json={
            "error": {"message": "backend rejected the request", "type": "invalid_request"},
            "choices": [{"message": {"content": private_output}}],
        },
    )
    stream = SimpleNamespace(response=response, close=response.close)

    with pytest.raises(UpstreamResponseError) as excinfo:
        reject_non_stream_response(stream)

    assert "backend rejected the request" in excinfo.value.body
    assert private_output not in str(excinfo.value)


def test_non_stream_json_body_read_is_bounded():
    from turnstone.core.providers._openai_common import reject_non_stream_response

    raw_stream = _OversizedJsonStream()
    response = httpx2.Response(
        200,
        headers={"content-type": "application/json"},
        stream=raw_stream,
    )
    stream = SimpleNamespace(response=response, close=response.close)

    with pytest.raises(UpstreamResponseError) as excinfo:
        reject_non_stream_response(stream)

    assert excinfo.value.body == "<JSON response did not contain a valid error object>"
    assert raw_stream.read_count < 100
    assert raw_stream.closed


@pytest.mark.parametrize("surface", ["chat", "responses"])
def test_non_stream_json_body_read_is_cancellable_without_arming(surface: str):
    from turnstone.core.deadline import StreamAbortRef
    from turnstone.core.providers import create_provider
    from turnstone.core.providers._protocol import IncompleteStreamError

    raw_stream = _BlockingJsonStream()

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"content-type": "application/json"},
            stream=raw_stream,
            request=request,
        )

    client = openai.OpenAI(
        api_key="probe",
        base_url="http://probe.invalid/v1",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )
    cancel_ref = StreamAbortRef()
    errors: list[BaseException] = []
    provider = create_provider("openai-compatible", api_surface=surface)

    def run() -> None:
        try:
            provider.create_streaming(
                client=client,
                model="m",
                messages=[{"role": "user", "content": "hi"}],
                cancel_ref=cancel_ref,
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert raw_stream.read_started.wait(2)
        assert cancel_ref == []
        cancel_ref.abort()
        worker.join(2)
    finally:
        cancel_ref.abort()
        worker.join(2)
        client.close()

    assert not worker.is_alive()
    assert raw_stream.closed.is_set()
    assert len(errors) == 1
    assert isinstance(errors[0], IncompleteStreamError)


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
