"""Shared session-test helpers.

Two reasoning-test modules (``test_session_replay_reasoning.py`` and
``test_session_synth_reasoning_block.py``) need the same minimal
``ChatSession`` factory + a ``SessionUIBase`` no-op subclass.  Hoisting
keeps a future third caller from drifting on the defaults — the third
existing ``_make_session`` (``test_model_registry.py``) deliberately
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

from turnstone.core.providers import StreamChunk, ToolCallDelta
from turnstone.core.session import ChatSession
from turnstone.core.session_ui_base import SessionUIBase


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
