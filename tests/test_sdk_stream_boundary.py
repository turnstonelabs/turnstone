"""Offline pins of SDK boundary behaviors used by stream error handling.

Eight facts, each probed against the REAL SDKs over mock/loopback
transports (no network, no live backend):

1. OpenAI's ``max_retries`` covers request time only — a mid-BODY death
   produces no re-request. The supported SDK (>=3.14.1) wraps ``httpx2``
   transport errors in ``APIConnectionError`` or its ``APITimeoutError``
   subclass, retaining the transport cause.
2. OpenAI v3's runtime-only legacy-client path preserves the old ``httpx``
   exception family as the SDK error's cause when an application explicitly
   injects that client.
3. Anthropic v1's ``messages.stream()`` helper propagates the ``httpx2``
   shape unwrapped with no SDK re-request, and the client rejects an injected
   legacy ``httpx.Client`` at construction — the tests inject HTTPX2 objects.
4. Closing an OpenAI v3 or Anthropic v1 default client from another thread
   while a read is blocked (the ``ModelRegistry.reload()`` shape) completes
   safely; a later wire release surfaces as a raw or SDK-wrapped
   ``httpx2.TransportError`` on the blocked ``next()``. The production
   ``transport_guarded`` seam must normalize it before the retry gate.
5. OpenAI v3 raises real HTTP errors before returning a stream, while an HTTP
   200 ``application/json`` response becomes an empty iterator unless the
   adapter rejects it before arming the stream.
6. Refusal text is visible without inventing an early terminal signal, and
   neither structured-output judge interprets a filtered response as a verdict.
7. Anthropic v1 removed ``temperature`` / ``top_p`` / ``top_k`` from the
   Messages signatures (a typed kwarg is a ``TypeError``): the adapter's
   capability-gated temperature reaches the request body through
   ``extra_body`` with unchanged send/omit semantics, and an operator
   ``server_compat`` pin of the same key wins.
8. Both SDKs merge caller ``extra_headers`` over their own credential header
   case-insensitively, even over ``with_options(api_key=...)``, so every
   adapter refuses credential names in ``extra_headers`` before the call.

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
from typing import Any
from unittest.mock import patch

import anthropic
import httpx
import httpx2
import openai
import pytest

from tests._wire_capture import RecordingClient, anthropic_body_capture_client
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

    def __init__(self, payload: bytes, error: httpx.TransportError | None = None) -> None:
        self._payload = payload
        self._error = error or httpx.ReadError("[SSL] record layer failure (_ssl.c:2590)")

    def __iter__(self):
        yield self._payload
        raise self._error


class _Httpx2DyingStream(httpx2.SyncByteStream):
    """HTTPX2 response body: one SSE payload, then a wire death."""

    def __init__(self, payload: bytes, error: httpx2.TransportError | None = None) -> None:
        self._payload = payload
        self._error = error or httpx2.ReadError("[SSL] record layer failure (_ssl.c:2590)")

    def __iter__(self):
        yield self._payload
        raise self._error


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


def test_openai_v3_chat_midbody_death_preserves_transport_cause_and_no_rerequest():
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
    with pytest.raises(openai.APIConnectionError) as excinfo:
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                texts.append(chunk.choices[0].delta.content)
    assert type(excinfo.value) is openai.APIConnectionError
    assert type(excinfo.value.__cause__) is httpx2.ReadError
    assert texts == ["hello"]  # the request succeeded; the BODY died
    assert len(requests) == 1  # max_retries never re-requested mid-body


def test_openai_v3_responses_midbody_death_preserves_transport_cause_and_no_rerequest():
    requests: list = []
    client = openai.OpenAI(
        api_key="probe",
        base_url="http://probe.invalid/v1",
        http_client=httpx2.Client(transport=_httpx2_dying_transport(RESPONSES_EVENT, requests)),
        max_retries=2,
    )
    stream = client.responses.create(model="m", input="hi", stream=True)
    texts = []
    with pytest.raises(openai.APIConnectionError) as excinfo:
        for event in stream:
            if event.type == "response.output_text.delta":
                texts.append(event.delta)
    assert type(excinfo.value) is openai.APIConnectionError
    assert type(excinfo.value.__cause__) is httpx2.ReadError
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
    with pytest.raises(openai.APIConnectionError) as excinfo:
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                texts.append(chunk.choices[0].delta.content)
    assert type(excinfo.value) is openai.APIConnectionError
    assert type(excinfo.value.__cause__) is httpx.ReadError
    assert texts == ["hello"]
    assert len(requests) == 1


@pytest.mark.parametrize("surface", ["chat", "responses"])
@pytest.mark.parametrize("http_module", [httpx, httpx2], ids=["httpx", "httpx2"])
@pytest.mark.parametrize(
    ("error_name", "sdk_error_cls"),
    [("ReadError", openai.APIConnectionError), ("ReadTimeout", openai.APITimeoutError)],
)
@pytest.mark.parametrize("finished", [False, True])
def test_openai_stream_transport_error_preserves_finish_semantics(
    surface, http_module, error_name, sdk_error_cls, finished, caplog
):
    """Exercise SDK wrapping, adapter iteration, and the shared drain together."""
    from turnstone.core.providers import (
        IncompleteStreamError,
        create_provider,
        drain_stream,
    )

    requests: list = []
    error = getattr(http_module, error_name)("wire died")
    body_cls = _DyingStream if http_module is httpx else _Httpx2DyingStream
    payload = CHAT_CHUNK if surface == "chat" else RESPONSES_EVENT
    if finished:
        terminal = (
            {
                "id": "x",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "m",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            if surface == "chat"
            else {
                "type": "response.completed",
                "sequence_number": 1,
                "response": {"id": "x", "status": "completed", "output": []},
            }
        )
        payload += "data: " + json.dumps(terminal) + LF + LF

    def handler(request):
        requests.append(request)
        return http_module.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=body_cls(payload.encode(), error),
        )

    with openai.OpenAI(
        api_key="probe",
        base_url="http://probe.invalid/v1",
        http_client=http_module.Client(transport=http_module.MockTransport(handler)),
        max_retries=2,
    ) as client:
        provider = create_provider("openai-compatible", api_surface=surface)
        cancel_ref: list = []
        chunks = provider.create_streaming(
            client=client,
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            cancel_ref=cancel_ref,
        )
        assert len(cancel_ref) == 1
        if finished:
            result = drain_stream(chunks)
            assert result.content == "hello"
            assert result.finish_reason == "stop"
            assert result.usage is None
            blips = [r.message for r in caplog.records if "stream.post_finish_blip" in r.message]
            assert len(blips) == 1
            assert error_name in blips[0]
            assert "usage_captured" in blips[0] and "False" in blips[0]
        else:
            with pytest.raises(IncompleteStreamError, match=error_name) as excinfo:
                drain_stream(chunks)
            assert excinfo.value.__cause__ is error
            sdk_error = excinfo.value.__context__
            assert type(sdk_error) is sdk_error_cls
            assert sdk_error.__cause__ is error
            assert type(excinfo.value).__name__ in provider.retryable_error_names
    assert len(requests) == 1


def test_anthropic_midbody_death_is_unwrapped_httpx2_error_and_no_rerequest():
    requests: list = []
    client = anthropic.Anthropic(
        api_key="probe",
        base_url="http://probe.invalid",
        http_client=httpx2.Client(transport=_httpx2_dying_transport(ANTHROPIC_EVENTS, requests)),
        max_retries=2,
    )
    texts = []
    # The provider adapter consumes the stream() helper's iterator — same
    # shape as _anthropic.py's create_streaming.
    with (
        client.messages.stream(
            model="m", max_tokens=64, messages=[{"role": "user", "content": "hi"}]
        ) as stream,
        pytest.raises(httpx2.ReadError) as excinfo,
    ):
        for event in stream:
            if getattr(event, "type", "") == "content_block_delta":
                delta = getattr(event, "delta", None)
                if getattr(delta, "type", "") == "text_delta":
                    texts.append(delta.text)
    assert type(excinfo.value).__name__ == "ReadError"
    assert isinstance(excinfo.value, httpx2.TransportError)
    assert texts == ["hello"]
    assert len(requests) == 1


def test_anthropic_v1_rejects_legacy_httpx_client_at_construction():
    """Why every Anthropic boundary test injects HTTPX2 objects: the SDK
    refuses the old family up front, so a legacy client cannot slip a
    different exception family past ``transport_guarded`` unnoticed."""
    legacy = httpx.Client()
    try:
        with pytest.raises(TypeError, match="httpx2"):
            anthropic.Anthropic(api_key="probe", http_client=legacy)  # type: ignore[arg-type]
    finally:
        legacy.close()


@pytest.mark.parametrize("surface", ["chat", "responses"])
def test_closed_v3_default_client_creation_stays_unarmed(surface: str):
    """A re-create on the client closed by reload is still a creation error.

    OpenAI >=3.14.1 propagates the HTTPX2 client's ``RuntimeError`` without
    retrying or wrapping it. It stays outside body iteration; the session's
    re-create failure path preserves the original normalized stream death.
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

    with pytest.raises(RuntimeError, match="client has been closed") as excinfo:
        provider.create_streaming(
            client=client,
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            cancel_ref=cancel_ref,
        )

    assert cancel_ref == []
    assert type(excinfo.value) is RuntimeError
    assert type(excinfo.value).__name__ not in provider.retryable_error_names


def _loopback_lane(lane: str, port: int) -> tuple[Any, Any]:
    """Real SDK client + provider for one streaming lane on a loopback server.

    The Anthropic SDK appends ``/v1/messages`` to ``base_url`` itself; the
    OpenAI SDK expects the ``/v1`` root.
    """
    from turnstone.core.providers import create_provider

    if lane == "anthropic":
        client = anthropic.Anthropic(
            api_key="probe", base_url=f"http://127.0.0.1:{port}", max_retries=0, timeout=5.0
        )
        return client, create_provider("anthropic")
    client = openai.OpenAI(
        api_key="probe", base_url=f"http://127.0.0.1:{port}/v1", max_retries=0, timeout=5.0
    )
    return client, create_provider("openai-compatible", api_surface=lane)


@pytest.mark.parametrize(
    ("lane", "payload"),
    [("chat", CHAT_CHUNK), ("responses", RESPONSES_EVENT), ("anthropic", ANTHROPIC_EVENTS)],
)
def test_cross_thread_default_client_close_then_wire_release_is_normalized(lane: str, payload: str):
    """The ``ModelRegistry.reload()`` shape stays safe at the retry seam.

    Neither OpenAI v3's nor Anthropic v1's synchronous HTTPX2 client promises
    that cross-thread ``close()`` itself interrupts a blocked body read. Pin
    the behavior Turnstone needs instead: closing from the admin thread
    completes safely while a worker is in ``next()``, and the subsequent wire
    release reaches ``transport_guarded`` as a provider-retryable
    ``IncompleteStreamError`` for both OpenAI streaming adapters and the
    Anthropic adapter.
    """
    from turnstone.core.providers import transport_guarded
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

    client, provider = _loopback_lane(lane, port)

    def read_stream() -> None:
        try:
            chunks = provider.create_streaming(
                client=client,
                model="m",
                messages=[{"role": "user", "content": "hi"}],
            )
            it = transport_guarded(chunks)
            # The Anthropic adapter yields a usage-only chunk for
            # ``message_start`` ahead of the first text delta.
            for sc in it:
                if sc.content_delta:
                    first_content.append(sc.content_delta)
                    break
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
            http_client=httpx2.Client(
                transport=_httpx2_dying_transport(ANTHROPIC_EVENTS, requests)
            ),
        )
        self._armed_at_return(AnthropicProvider(), client)
        assert len(requests) == 1


# ---------------------------------------------------------------------------
# 7. Anthropic v1 sampling: extra_body carries capability-gated temperature
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kwarg", ["temperature", "top_p", "top_k"])
def test_anthropic_v1_rejects_typed_sampling_kwargs(kwarg: str):
    """The SDK half of fact 7: the sampling kwargs are gone from the Messages
    signatures, so an adapter that regressed to a typed kwarg would fail
    every real-SDK test before issuing a request.  Binding fails before any
    transport is touched, so no mock is needed."""
    client = anthropic.Anthropic(api_key="probe")
    try:
        with pytest.raises(TypeError, match=kwarg):
            client.messages.stream(
                model="m",
                max_tokens=16,
                messages=[{"role": "user", "content": "hi"}],
                **{kwarg: 0.5},
            )
    finally:
        client.close()


_COMPAT_TEMPLATE = {"enable_thinking": False}
# A row expects this when the key must not be in the body at all: it is the
# ``.get`` default, so a literal ``null`` on the wire cannot pass as absence.
_ABSENT = "<absent>"


@pytest.mark.parametrize(
    (
        "provider_name",
        "model",
        "call_temperature",
        "extra_params",
        "wire_temperature",
        "wire_template",
    ),
    [
        # Adaptive thinking on a sampling-capable row forces the API-required 1.0.
        ("anthropic", "claude-sonnet-4-6", 0.5, None, 1.0, _ABSENT),
        # Thinking off (no effort knob) on a manual-mode row: the resolved value verbatim.
        ("anthropic", "claude-haiku-4-5", 0.5, None, 0.5, _ABSENT),
        # No operator value resolved: None is never written (house rule: no code pins).
        ("anthropic", "claude-haiku-4-5", None, None, _ABSENT, _ABSENT),
        # supports_temperature=False: the field never reaches the body.
        ("anthropic", "claude-opus-4-8", 0.5, None, _ABSENT, _ABSENT),
        # Compat lane: the knob rides beside the operator's chat_template_kwargs.
        (
            "anthropic-compatible",
            "qwen3.6-27b",
            0.5,
            {"chat_template_kwargs": _COMPAT_TEMPLATE},
            0.5,
            _COMPAT_TEMPLATE,
        ),
        # An operator server_compat pin of the same key wins over the knob.
        (
            "anthropic-compatible",
            "qwen3.6-27b",
            0.5,
            {"temperature": 0.3, "chat_template_kwargs": _COMPAT_TEMPLATE},
            0.3,
            _COMPAT_TEMPLATE,
        ),
    ],
)
def test_anthropic_v1_temperature_rides_extra_body_to_the_wire(
    provider_name: str,
    model: str,
    call_temperature: float | None,
    extra_params: dict[str, Any] | None,
    wire_temperature: Any,
    wire_template: Any,
):
    """The adapter half of fact 7: the same send/omit semantics as the typed
    kwarg had, now through ``extra_body``, with an operator ``server_compat``
    pin keeping precedence over the resolved knob.  Driven through the real
    SDK so the merge into the JSON body is what is pinned, not the kwargs
    dict; absence is pinned as absence (see ``_ABSENT``)."""
    from turnstone.core.providers import create_provider

    captured: dict[str, Any] = {}
    client = anthropic_body_capture_client(captured)
    try:
        chunks = list(
            create_provider(provider_name).create_streaming(
                client=client,
                model=model,
                messages=[{"role": "user", "content": "hi"}],
                temperature=call_temperature,
                extra_params=extra_params,
            )
        )
    finally:
        client.close()
    body = captured["body"]
    assert body.get("temperature", _ABSENT) == wire_temperature
    assert body.get("chat_template_kwargs", _ABSENT) == wire_template
    finishes = [sc.finish_reason for sc in chunks if sc.finish_reason]
    assert finishes == ["stop"]


# ---------------------------------------------------------------------------
# 8. Credential headers: the SDKs let a caller header replace the credential
# ---------------------------------------------------------------------------


def test_anthropic_extra_headers_replace_the_credential_header():
    """The SDK half of fact 8 (Anthropic): a caller ``X-Api-Key`` replaces the
    key the SDK emits, even the one minted via ``with_options`` — the reason
    the adapters refuse credential names before the call."""
    captured: dict[str, Any] = {}
    client = anthropic_body_capture_client(captured)
    try:
        with client.with_options(api_key="minted").messages.stream(
            model="m",
            max_tokens=8,
            messages=[{"role": "user", "content": "hi"}],
            extra_headers={"X-Api-Key": "injected"},
        ) as stream:
            for _ in stream:
                pass
    finally:
        client.close()
    assert captured["headers"].get("x-api-key") == "injected"


def test_openai_extra_headers_replace_the_credential_header():
    """The SDK half of fact 8 (OpenAI v3): a caller ``Authorization`` replaces
    the bearer the SDK emits for a ``with_options``-minted key."""
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["authorization"] = request.headers.get("authorization")
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=("data: [DONE]" + LF + LF).encode(),
            request=request,
        )

    client = openai.OpenAI(
        api_key="static",
        base_url="http://probe.invalid/v1",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )
    try:
        stream = client.with_options(api_key="minted").chat.completions.create(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            extra_headers={"Authorization": "Bearer injected"},
        )
        list(stream)
    finally:
        client.close()
    assert seen["authorization"] == "Bearer injected"


@pytest.mark.parametrize("header", ["x-api-key", "X-Api-Key", "Authorization"])
@pytest.mark.parametrize(
    ("provider_name", "api_surface"),
    [("anthropic", None), ("openai-compatible", "chat"), ("openai-compatible", "responses")],
)
def test_adapters_refuse_credential_headers_before_the_sdk_call(
    provider_name: str, api_surface: str | None, header: str
):
    """The Turnstone half of fact 8: request assembly raises before any SDK
    call, so ``with_options(api_key=...)`` stays the only credential path."""
    from turnstone.core.providers import create_provider

    client = RecordingClient()
    provider = create_provider(provider_name, api_surface=api_surface)
    with pytest.raises(ValueError, match="with_options"):
        provider.create_streaming(
            client=client,
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            extra_headers={header: "injected"},
        )
    assert "payload" not in client.captured
