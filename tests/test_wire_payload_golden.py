"""Wire-payload equivalence harness (canonical-trajectory refactor, precondition P2).

Freezes the *exact* request payload each provider sends, for a representative set of
trajectories, by capturing the ``**kwargs`` handed to the provider's SDK call seam
(Anthropic ``client.messages.stream``, OpenAI ``client.chat.completions.create``,
Responses ``client.responses.create/stream``, Google via the OpenAI-compat client).

Each provider's ``create_streaming`` assembles its kwargs and calls the SDK *eagerly*
before returning the stream iterator, so a fake recording client captures the full
composed payload — fold-output + orphan repair + format translation + param/tool
assembly — without a network round-trip.

The captured payloads are asserted byte-for-byte (as normalized JSON) against goldens
under ``tests/data/wire_payloads/``.  This is the safety net the wire-path refactor is
proven against: regenerate the baseline on the unchanged tree with
``UPDATE_WIRE_GOLDENS=1 pytest tests/test_wire_payload_golden.py``, commit it, then any
behavioural drift in the refactor shows up as a failing assertion here.

Intentional changes (e.g. the P1 truncation-orphan fix) are reviewed by regenerating
the affected golden and inspecting the diff.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from tests._wire_capture import RecordingClient
from turnstone.core.lowering import repair_wire_messages
from turnstone.core.providers._anthropic import AnthropicProvider
from turnstone.core.providers._google import GoogleProvider
from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider
from turnstone.core.providers._openai_responses import OpenAIResponsesProvider
from turnstone.core.providers._protocol import ModelCapabilities

if TYPE_CHECKING:
    from turnstone.core.providers._protocol import LLMProvider

GOLDEN_DIR = Path(__file__).parent / "data" / "wire_payloads"
_UPDATE = os.environ.get("UPDATE_WIRE_GOLDENS") == "1"


def _capture(
    provider: LLMProvider,
    *,
    model: str,
    messages: list[dict[str, Any]],
    caps: ModelCapabilities | None = None,
    **opts: Any,
) -> dict[str, Any]:
    """Drive ``create_streaming`` against a recording client; return the SDK kwargs.

    *caps* overrides the provider's own capability lookup — required for the
    anthropic-compatible lane, which has no static table (an operator-run model
    definition supplies its capabilities).
    """
    client = RecordingClient()
    caps = caps or provider.get_capabilities(model)
    # Mirror the session's wire prep: orphan repair runs once on the canonical
    # Turns (``ChatSession._prepare_wire_messages``), then the result is lowered
    # to the dict projection the translator consumes.  Fixtures arrive
    # folded/empty-dropped; repair is the remaining send-side pass.
    messages = repair_wire_messages(messages)
    gen = provider.create_streaming(
        client=client,
        model=model,
        messages=messages,
        capabilities=caps,
        resolve_attachments=_capture_resolver,
        **opts,
    )
    # kwargs are recorded eagerly during the call above; close the (unconsumed)
    # iterator so any stream-manager cleanup runs against the empty stub.
    close = getattr(gen, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):
            close()
    assert "payload" in client.captured, "provider did not reach its SDK call seam"
    return cast("dict[str, Any]", client.captured["payload"])


def _normalize(obj: Any) -> Any:
    """JSON round-trip with stable ordering; surface any non-serializable value."""
    return json.loads(
        json.dumps(obj, sort_keys=True, default=lambda o: f"<<UNSERIALIZABLE {type(o).__name__}>>")
    )


def _assert_golden(name: str, payload: dict[str, Any]) -> None:
    path = GOLDEN_DIR / f"{name}.json"
    norm = _normalize(payload)
    if _UPDATE:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        # ensure_ascii=False keeps non-ASCII (em dashes in repair
        # messages) literal, matching the existing baselines — a regen
        # must not churn unrelated lines into \uXXXX escapes.
        path.write_text(json.dumps(norm, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
        return
    assert path.exists(), f"missing golden {name!r}; run UPDATE_WIRE_GOLDENS=1 to baseline"
    assert norm == json.loads(path.read_text()), f"wire-payload drift for {name!r}"


# --------------------------------------------------------------------------- #
# Fixtures — trajectories post-fold/empty-drop; _capture applies the remaining
# send-side wire prep (orphan repair) before handing them to the provider.
# --------------------------------------------------------------------------- #
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Look up the weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]


def _tc(call_id: str, name: str = "get_weather", args: str = '{"city": "Paris"}') -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


FIX_TEXT: list[dict[str, Any]] = [
    {"role": "user", "content": "Hi there."},
    {"role": "assistant", "content": "Hello! How can I help?"},
    {"role": "user", "content": "What's the weather in Paris?"},
]

FIX_TOOLCALL_COMPLETE: list[dict[str, Any]] = [
    {"role": "user", "content": "Weather in Paris?"},
    {"role": "assistant", "content": "", "tool_calls": [_tc("call_1")]},
    {"role": "tool", "tool_call_id": "call_1", "content": "18C, clear."},
    {"role": "assistant", "content": "It's 18C and clear in Paris."},
]

# Two tool_calls, only the first answered, then conversation continues → the
# mid-conversation orphan that every provider must repair (synthesize) at send.
FIX_MID_ORPHAN: list[dict[str, Any]] = [
    {"role": "user", "content": "Weather in Paris and London?"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [_tc("call_1"), _tc("call_2", args='{"city": "London"}')],
    },
    {"role": "tool", "tool_call_id": "call_1", "content": "18C, clear."},
    {"role": "user", "content": "Actually, never mind London."},
]

# Trailing assistant(tool_calls) with no results → send-time synthesis.
FIX_TRAILING_ORPHAN: list[dict[str, Any]] = [
    {"role": "user", "content": "Weather in Paris?"},
    {"role": "assistant", "content": "", "tool_calls": [_tc("call_1")]},
]

# Native reasoning lane (Anthropic-shaped); replay on by default.
FIX_NATIVE_REASONING: list[dict[str, Any]] = [
    {"role": "user", "content": "Think about the weather."},
    {
        "role": "assistant",
        "content": "Let me check.",
        "_provider_content": [
            {"type": "thinking", "thinking": "The user wants weather.", "signature": "sig-abc"},
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "Paris"}},
        ],
        "tool_calls": [_tc("call_1")],
    },
    {"role": "tool", "tool_call_id": "call_1", "content": "18C, clear."},
]

# Native reasoning lane with an UNANSWERED tool_use — the verbatim-replay orphan.
# The native ``tool_use`` is mirrored top-level in ``tool_calls`` (the P1 invariant),
# so the send-time repair can synthesize its cancellation result by reading
# ``tool_calls`` alone.  Exercises the Anthropic verbatim-replay branch that the old
# per-provider ``pc_tool_ids`` synthesis covered.
FIX_NATIVE_ORPHAN: list[dict[str, Any]] = [
    {"role": "user", "content": "Think about the weather."},
    {
        "role": "assistant",
        "content": "Let me check.",
        "_provider_content": [
            {"type": "thinking", "thinking": "The user wants weather.", "signature": "sig-abc"},
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "Paris"}},
        ],
        "tool_calls": [_tc("call_1")],
    },
]

# Multipart user content (image attachment as the provider receives it today).
# By-reference image: the trajectory carries a {type:image, attachment_id}
# placeholder; the translator materializes it to the inline part below via the
# resolver _capture passes (mirroring ChatSession._resolve_attachments).
_MULTIPART_IMG_ID = "mp-image-hash"
_MULTIPART_IMAGE_PART: dict[str, Any] = {
    "type": "image_url",
    "image_url": {
        "url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    },
}

FIX_MULTIPART: list[dict[str, Any]] = [
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "What's in this image?"},
            {"type": "image", "attachment_id": _MULTIPART_IMG_ID},
        ],
    },
]


def _capture_resolver(ids: list[str]) -> dict[str, dict[str, Any]]:
    """Stand-in for ``ChatSession._resolve_attachments`` — maps the fixture's
    by-reference image id to its inline content part."""
    return {_MULTIPART_IMG_ID: _MULTIPART_IMAGE_PART} if _MULTIPART_IMG_ID in ids else {}


# Operator-context system turn left inline (the native mid-conversation-system path).
FIX_OPERATOR_SYSTEM: list[dict[str, Any]] = [
    {"role": "user", "content": "Run the deploy."},
    {"role": "assistant", "content": "", "tool_calls": [_tc("call_1", name="deploy", args="{}")]},
    {"role": "tool", "tool_call_id": "call_1", "content": "deployed"},
    {
        "role": "system",
        "_source": "output_guard",
        "content": "Output-guard: deploy output looked clean.",
    },
    {"role": "user", "content": "Great, what's next?"},
]

_FIXTURES: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {
    "text": (FIX_TEXT, {}),
    "toolcall_complete": (FIX_TOOLCALL_COMPLETE, {"tools": _TOOLS}),
    "mid_orphan": (FIX_MID_ORPHAN, {"tools": _TOOLS}),
    "trailing_orphan": (FIX_TRAILING_ORPHAN, {"tools": _TOOLS}),
    "native_reasoning": (
        FIX_NATIVE_REASONING,
        {"tools": _TOOLS, "replay_reasoning_to_model": True},
    ),
    "native_orphan": (
        FIX_NATIVE_ORPHAN,
        {"tools": _TOOLS, "replay_reasoning_to_model": True},
    ),
    "multipart": (FIX_MULTIPART, {}),
    "operator_system": (FIX_OPERATOR_SYSTEM, {"tools": _TOOLS}),
}

# (provider_id, provider factory, model).  Two Anthropic rows exercise both the
# mid-conversation-system converter branch (opus-4-8) and the hoist branch.
_PROVIDERS: list[tuple[str, type[LLMProvider], str]] = [
    ("anthropic_native", AnthropicProvider, "claude-opus-4-8"),
    ("anthropic_hoist", AnthropicProvider, "claude-sonnet-4-6"),
    ("openai_chat", OpenAIChatCompletionsProvider, "gpt-4o-mini"),
    ("openai_responses", OpenAIResponsesProvider, "gpt-5"),
    ("google", GoogleProvider, "gemini-2.5-pro"),
]


@pytest.mark.parametrize("fixture_id", sorted(_FIXTURES))
@pytest.mark.parametrize("provider_id,factory,model", _PROVIDERS, ids=[p[0] for p in _PROVIDERS])
def test_wire_payload(
    provider_id: str, factory: type[LLMProvider], model: str, fixture_id: str
) -> None:
    messages, opts = _FIXTURES[fixture_id]
    provider = factory()
    payload = _capture(provider, model=model, messages=[dict(m) for m in messages], **opts)
    _assert_golden(f"{provider_id}__{fixture_id}", payload)


# The anthropic-compatible lane (vLLM /v1/messages) has no static capability
# table and carries reasoning control in ``extra_body.chat_template_kwargs``
# rather than the native ``thinking`` param — a wire shape the matrix above
# never exercises (both AnthropicProvider rows are the native lane). Freeze it
# with the capabilities a manual-mode model definition supplies and a real
# effort level, so the graded ``reasoning_effort`` key is pinned in the golden
# (never the native ``thinking`` param, and no forced ``temperature=1.0``).
_COMPAT_CAPS = ModelCapabilities(
    context_window=262144,
    max_output_tokens=64000,
    token_param="max_tokens",
    thinking_mode="manual",
    thinking_param="enable_thinking",
    supports_reasoning_replay=True,
)


@pytest.mark.parametrize("fixture_id", sorted(_FIXTURES))
def test_wire_payload_anthropic_compat(fixture_id: str) -> None:
    messages, opts = _FIXTURES[fixture_id]
    provider = AnthropicProvider(compat=True)
    payload = _capture(
        provider,
        model="qwen3.6-27b",
        messages=[dict(m) for m in messages],
        caps=_COMPAT_CAPS,
        reasoning_effort="high",
        **opts,
    )
    assert "thinking" not in payload, "compat lane must never send the native thinking param"
    _assert_golden(f"anthropic_compat__{fixture_id}", payload)
