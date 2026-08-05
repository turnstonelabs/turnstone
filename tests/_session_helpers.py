"""Shared session-test helpers.

The minimal ``ChatSession`` factory, the ``SessionUIBase`` no-op/recording
subclasses, and — since #832 — the tree's standard streaming provider
fakes (``make_result`` / ``arm_session`` / ``scripted_provider`` /
``ArmedHandle``, at the bottom): every suite that drives the streaming
seam imports them from here so the eager-arming contract lives in one
place.  Hoisting keeps callers from drifting on the defaults — the one
deliberate exception, ``test_model_registry.py``'s ``_make_session``,
takes a different signature (registry / model_alias / reasoning_effort
+ ``_FakeUI``) and is NOT a candidate for sharing this helper.

Module is named with a leading underscore so pytest doesn't try to
collect it as a test file — it's an importable utility, not a test.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from turnstone.core.model_turn import ModelTurnResult
from turnstone.core.providers import ModelCapabilities, StreamChunk, ToolCallDelta, UsageInfo
from turnstone.core.session import ChatSession
from turnstone.core.session_ui_base import SessionUIBase
from turnstone.core.trajectory import ProviderNative, ToolCall, Turn


class NullUI(SessionUIBase):
    """Bare-bones UI satisfying the SessionUIBase contract for tests
    that don't care about UI side effects."""

    def __init__(self) -> None:
        super().__init__()


def make_session(**kwargs: Any) -> ChatSession:
    """Build a ChatSession with minimal defaults; tests override
    individual fields via kwargs."""
    defaults: dict[str, Any] = {
        "client": MagicMock(),
        "model": "test-model",
        "ui": NullUI(),
        "instructions": None,
        "temperature": 0.5,
        "max_tokens": 4096,
        "tool_timeout": 30,
    }
    defaults.update(kwargs)
    return ChatSession(**defaults)


def mock_completion_result(
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """A provider result shaped like ``CompletionResult``.

    Callers that route through ``model_turn`` (judges, task agents, and
    every lane #827 migrates) hit its re-ingest, which iterates
    ``tool_calls``/``provider_blocks`` and joins ``reasoning`` — a bare
    MagicMock attribute would TypeError deep inside the seam, so every
    field the re-ingest reads is pinned to a real value here.  ONE shared
    definition: when the re-ingest starts reading a new CompletionResult
    field, add it here and every suite moves together.
    """
    result = MagicMock()
    result.content = content
    result.tool_calls = tool_calls
    result.finish_reason = "stop"
    result.usage = None
    result.provider_blocks = []
    result.reasoning = ""
    return result


def fake_chat_stream(
    *,
    content: str | None = None,
    tool_calls: list[dict[str, str]] | None = None,
    finish_reason: str = "stop",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    reasoning_content: str | None = None,
    reasoning: str | None = None,
) -> list[Any]:
    """Fake OpenAI Chat Completions SSE chunks for driving the REAL
    ``OpenAIChatCompletionsProvider`` through a fake SDK client::

        client.chat.completions.create = lambda **kw: fake_chat_stream(...)

    Exercises the adapter's ``_iter_stream`` plus ``drain_stream`` end to
    end (the highest-fidelity fake lane), unlike ``as_stream`` which fakes
    at the provider boundary.  ``tool_calls`` entries are
    ``{"id", "name", "arguments"}`` dicts.  ``SimpleNamespace`` (not
    ``MagicMock``) so absent SDK fields read as real ``None`` — an
    auto-created mock attribute would leak into ``len()``/string paths.

    Emits the realistic three-phase shape: data chunk(s), a finish-reason
    chunk, then the ``stream_options.include_usage`` usage-only chunk with
    empty ``choices``.
    """

    def _delta(
        content_val: str | None = None,
        tcs: list[Any] | None = None,
        rc: str | None = None,
        rsn: str | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            content=content_val,
            tool_calls=tcs,
            reasoning=rsn,
            reasoning_content=rc,
            annotations=None,
        )

    chunks: list[Any] = []
    if reasoning_content is not None or reasoning is not None:
        chunks.append(
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason=None, delta=_delta(rc=reasoning_content, rsn=reasoning)
                    )
                ],
                usage=None,
            )
        )
    if content is not None:
        chunks.append(
            SimpleNamespace(
                choices=[SimpleNamespace(finish_reason=None, delta=_delta(content))],
                usage=None,
            )
        )
    if tool_calls:
        tcs = [
            SimpleNamespace(
                index=i,
                id=tc.get("id", ""),
                function=SimpleNamespace(
                    name=tc.get("name", ""), arguments=tc.get("arguments", "")
                ),
            )
            for i, tc in enumerate(tool_calls)
        ]
        chunks.append(
            SimpleNamespace(
                choices=[SimpleNamespace(finish_reason=None, delta=_delta(None, tcs))],
                usage=None,
            )
        )
    chunks.append(
        SimpleNamespace(
            choices=[SimpleNamespace(finish_reason=finish_reason, delta=_delta())],
            usage=None,
        )
    )
    chunks.append(
        SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                prompt_tokens_details=None,
                input_tokens_details=None,
            ),
        )
    )
    return chunks


class _ScriptedClient:
    """Callable client-method fake following a script of stream builders.

    Call N returns the stream described by ``scripts[N]``; the last script
    repeats for any further calls.  Each script is a dict of kwargs for
    the bound stream builder, or a pre-built return value.  Records every
    call's kwargs on ``.calls`` — read ``len(fn.calls)`` where a test
    previously kept its own counter cell, and ``fn.calls[i]["messages"]``
    where it captured request bodies.
    """

    def __init__(self, scripts: tuple[Any, ...], to_stream: Any) -> None:
        self._scripts = scripts
        self._to_stream = to_stream
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        script = self._scripts[min(len(self.calls) - 1, len(self._scripts) - 1)]
        return self._to_stream(**script) if isinstance(script, dict) else script


def scripted_chat_client(*scripts: Any) -> _ScriptedClient:
    """A scripted ``client.chat.completions.create`` — dict scripts are
    :func:`fake_chat_stream` kwargs."""
    return _ScriptedClient(scripts, fake_chat_stream)


def scripted_anthropic_client(*scripts: Any) -> _ScriptedClient:
    """A scripted ``client.messages.stream`` — dict scripts are
    :func:`fake_anthropic_stream` kwargs (``blocks`` plus optional
    ``stop_reason``/``usage``)."""
    return _ScriptedClient(scripts, fake_anthropic_stream)


class FakeAnthropicBlock:
    """A full-content Anthropic content-block fake for
    :func:`fake_anthropic_stream` — plain attributes plus the
    ``model_dump()`` the provider's block capture reads."""

    def __init__(self, **fields: Any) -> None:
        self._fields = fields
        for key, value in fields.items():
            setattr(self, key, value)

    def model_dump(self, **_kw: Any) -> dict[str, Any]:
        return dict(self._fields)


def fake_anthropic_stream(
    blocks: list[Any],
    *,
    stop_reason: str | None = "end_turn",
    usage: Any = None,
) -> Any:
    """Fake Anthropic SDK stream context manager for tests that drive the
    REAL ``AnthropicProvider`` through a fake client::

        client.messages.stream = lambda **kw: fake_anthropic_stream(...)

    Accepts the same full-content block fakes the pre-#831
    ``get_final_message`` fixtures used (objects with ``.type`` + fields
    and ``model_dump()``) and synthesizes the real event grammar the
    streaming iterator consumes: ``content_block_start`` carries the block
    with its text/thinking/signature EMPTIED and ``input`` as ``{}`` (the
    SDK start shape), deltas carry the content, ``content_block_stop``
    finalizes tool input, and the closing ``message_delta`` carries
    ``stop_reason`` (+ optional usage object).  Without the stripping, the
    provider's raw-block accumulator would double every text/thinking
    field (start capture + delta append).

    ``stop_reason=None`` omits the closing ``message_delta`` entirely —
    the terminal-signal-less lax-gateway shape ``finish_reason_optional``
    exists for (content arrives, then the stream just ends).
    """
    events: list[Any] = []
    for idx, block in enumerate(blocks):
        d = dict(block.model_dump()) if hasattr(block, "model_dump") else dict(vars(block))
        btype = d.get("type", "")
        start = dict(d)
        if btype == "text":
            start["text"] = ""
        elif btype == "thinking":
            start["thinking"] = ""
            start["signature"] = ""
        elif btype == "tool_use":
            start["input"] = {}
        events.append(
            SimpleNamespace(
                type="content_block_start", index=idx, content_block=SimpleNamespace(**start)
            )
        )
        if btype == "text" and d.get("text"):
            events.append(
                SimpleNamespace(
                    type="content_block_delta",
                    index=idx,
                    delta=SimpleNamespace(type="text_delta", text=d["text"]),
                )
            )
        elif btype == "thinking":
            if d.get("thinking"):
                events.append(
                    SimpleNamespace(
                        type="content_block_delta",
                        index=idx,
                        delta=SimpleNamespace(type="thinking_delta", thinking=d["thinking"]),
                    )
                )
            if d.get("signature"):
                events.append(
                    SimpleNamespace(
                        type="content_block_delta",
                        index=idx,
                        delta=SimpleNamespace(type="signature_delta", signature=d["signature"]),
                    )
                )
        elif btype == "tool_use":
            events.append(
                SimpleNamespace(
                    type="content_block_delta",
                    index=idx,
                    delta=SimpleNamespace(
                        type="input_json_delta",
                        partial_json=json.dumps(d.get("input", {})),
                    ),
                )
            )
        events.append(SimpleNamespace(type="content_block_stop", index=idx))
    if stop_reason is not None or usage is not None:
        events.append(
            SimpleNamespace(
                type="message_delta", usage=usage, delta=SimpleNamespace(stop_reason=stop_reason)
            )
        )

    mgr = MagicMock()
    mgr.__enter__ = MagicMock(return_value=events)
    mgr.__exit__ = MagicMock(return_value=False)
    return mgr


def as_stream(result: Any) -> list[StreamChunk]:
    """Adapt a ``CompletionResult``-shaped fake to a ``create_streaming``
    return value (single terminal chunk).

    The #831 transport collapse routes every single-shot lane through
    ``drain_stream(provider.create_streaming(...))``, so provider fakes
    return chunk iterables now.  Tests keep building result-shaped fakes
    (``mock_completion_result`` or hand-rolled) and wrap them at
    assignment: ``provider.create_streaming.return_value =
    as_stream(result)``.  A list re-iterates on every call, so one
    ``return_value`` serves repeated-call tests; convert AFTER mutating
    the fake's fields — the chunk snapshots them.

    Multi-chunk accumulation semantics are exercised by the dedicated
    ``drain_stream`` unit tests, not through this helper.
    """
    deltas = [
        ToolCallDelta(
            index=i,
            id=tc.get("id", ""),
            name=tc.get("function", {}).get("name", ""),
            arguments_delta=tc.get("function", {}).get("arguments", ""),
        )
        for i, tc in enumerate(result.tool_calls or [])
    ]
    return [
        StreamChunk(
            content_delta=result.content or "",
            reasoning_delta=getattr(result, "reasoning", "") or "",
            tool_call_deltas=deltas,
            usage=result.usage,
            finish_reason=result.finish_reason or "stop",
            provider_blocks=list(result.provider_blocks or []),
        )
    ]


def think_tag_stream(utterance: str) -> list[StreamChunk]:
    """``create_streaming`` return value simulating a passthrough server
    that emits *utterance* — typically think-tag-bearing — as plain
    streamed content.

    The per-lane fixture for inline-reasoning dialect pins: lane tests
    supply their own utterances (the dialect's SEMANTICS are specified
    once, in ``tests._reasoning_dialect.CASES``, and pinned by the
    one-shot suites — lane pins assert lane behavior, not tag grammar).
    Routes through the real ``drain_stream`` seam exactly like
    ``as_stream``.
    """
    return as_stream(mock_completion_result(content=utterance))


def seam_provider(utterance: str, *, provider_name: str = "openai-compatible") -> MagicMock:
    """Provider fake whose ``create_streaming`` replays *utterance* through
    the REAL drain seam (``think_tag_stream``) — THE lane-suite seam fake.

    One definition so the lane suites cannot drift when the provider
    surface ``model_turn`` probes grows: real ``ModelCapabilities`` for
    the clamp math, ``provider_name`` overridable per suite.

    Assign the RETURNED fake to ``session._provider`` — never mutate the
    provider a session resolved on its own: with a MagicMock client the
    session resolves the process-wide ``create_provider(...)`` singleton,
    and writing that shared instance's ``create_streaming`` poisons every
    later session in the test run (the SSE-recovery e2e servers resolve
    the same instance).
    """
    provider = MagicMock()
    provider.provider_name = provider_name
    provider.get_capabilities.return_value = ModelCapabilities()
    provider.create_streaming = MagicMock(return_value=think_tag_stream(utterance))
    return provider


class RecordingUI:
    """UI adapter recording the ordered event stream ``send()`` emits."""

    def __init__(self):
        self.events = []

    def _rec(self, kind, detail=""):
        self.events.append((kind, detail))

    def on_turn_start(self):
        self._rec("turn_start")

    def on_turn_committed(self):
        self._rec("turn_committed")

    def on_stream_discarded(self):
        self._rec("stream_discarded")

    def on_thinking_start(self):
        self._rec("thinking_start")

    def on_thinking_stop(self):
        self._rec("thinking_stop")

    def on_reasoning_token(self, text):
        self._rec("reasoning", text)

    def on_content_token(self, text):
        self._rec("content", text)

    def on_stream_end(self):
        self._rec("stream_end")

    def approve_tools(self, items):
        return True, None

    def on_tool_result(self, call_id, name, output, **kwargs):
        pass

    def on_tool_output_chunk(self, call_id, chunk):
        pass

    def on_status(self, usage, context_window, effort):
        pass

    def on_info(self, message):
        self._rec("info", message)

    def on_error(self, message):
        self._rec("error", message)

    def on_state_change(self, state):
        self._rec("state", state)

    def on_rename(self, name):
        pass

    def on_output_warning(self, call_id, assessment):
        pass

    def record_output_assessment(
        self,
        call_id,
        assessment,
        *,
        tier="heuristic",
        reasoning="",
        judge_model="",
        latency_ms=0,
        confidence=0.0,
    ):
        pass

    def kinds(self):
        return [k for k, _ in self.events]

    def of(self, kind):
        return [d for k, d in self.events if k == kind]


# ---------------------------------------------------------------------------
# Streaming provider fakes — the #832 seam contract
# ---------------------------------------------------------------------------


def make_result(
    content: str = "",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    usage: UsageInfo | None = None,
    native_blocks: list[dict[str, Any]] | None = None,
    producer: str = "openai-compatible",
    wire_msgs: list[dict[str, Any]] | None = None,
) -> ModelTurnResult:
    """A ``ModelTurnResult`` shaped like the streaming wrapper's return —
    for tests that only need "a turn happened" and patch
    ``_stream_response`` wholesale.  The Turn and the ``tool_calls``
    mirror are built from the same dicts, preserving the #825 pairing
    invariant fakes must not break."""
    calls = list(tool_calls or [])
    tc_tuple = tuple(
        ToolCall(
            id=tc.get("id", ""),
            name=tc.get("function", {}).get("name", ""),
            arguments=tc.get("function", {}).get("arguments", ""),
        )
        for tc in calls
    )
    native = (
        ProviderNative(producer=producer, blocks=tuple(native_blocks)) if native_blocks else None
    )
    return ModelTurnResult(
        turn=Turn.assistant(content, tool_calls=tc_tuple, native=native),
        finish_reason=finish_reason,
        usage=usage,
        tool_calls=calls,
        wire_msgs=wire_msgs,
        producer=producer,
    )


class ArmedHandle:
    """Closeable sentinel standing in for the SDK stream handle."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def arm_session(
    session: Any,
    *streams: Any,
    retryable: frozenset[str] = frozenset({"IncompleteStreamError"}),
    name: str = "openai-compatible",
) -> MagicMock:
    """Install a sequential multi-turn armed provider fake on *session*.

    Each ``create_streaming`` call serves the next element of *streams*:
    an iterable/generator is armed (a closeable sentinel appended to
    ``cancel_ref`` — the eager append every real adapter performs, which
    the fold's creation-vs-midstream classifier keys on) and returned to
    be consumed once; an EXCEPTION instance is raised at create time
    WITHOUT arming — a creation-phase failure the per-lane ladder owns.
    Calls beyond the script fail loudly (the pre-fold lax consumer used
    to absorb an exhausted iterator as a silent empty turn; the strict
    finish gate rejects that now, so an under-scripted test must say so).

    Title generation is latched off — with a provider-LEVEL fake the
    best-effort title lane would otherwise consume the first script
    before the main loop ran.
    """
    session._title_generated = True
    provider = MagicMock()
    provider.provider_name = name
    provider.get_capabilities.return_value = ModelCapabilities()
    provider.retryable_error_names = retryable
    provider._armed_handle = MagicMock()
    remaining = list(streams)

    def _create(**kwargs: Any):
        assert remaining, "arm_session: script exhausted — send looped for more turns than scripted"
        nxt = remaining.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        ref = kwargs.get("cancel_ref")
        if ref is not None:
            ref.append(provider._armed_handle)
        return iter(nxt) if not hasattr(nxt, "__next__") else nxt

    provider.create_streaming = MagicMock(side_effect=_create)
    session._provider = provider
    return provider


def scripted_provider(chunks: list[StreamChunk]) -> MagicMock:
    """Provider fake replaying *chunks*, arming ``cancel_ref`` eagerly.

    Assign to ``session._provider`` (never mutate a resolved provider —
    the create_provider singleton rule above).  Each call returns a FRESH
    iterator over the same script so ladder tests re-drive it; the armed
    handle is appended per call, matching the one-handle-per-create
    behavior of every real adapter.
    """
    provider = MagicMock()
    provider.provider_name = "openai-compatible"
    provider.get_capabilities.return_value = ModelCapabilities()
    provider.retryable_error_names = frozenset({"IncompleteStreamError"})

    def _create(**kwargs: Any):
        ref = kwargs.get("cancel_ref")
        if ref is not None:
            ref.append(ArmedHandle())
        return iter(chunks)

    provider.create_streaming = MagicMock(side_effect=_create)
    return provider
