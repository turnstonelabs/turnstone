"""Ladder↔wire parity harness — the effort ladder must tell the truth.

``effort_ladder`` *projects* the session effort knob through the same
mapping functions the providers use at request time.  This suite proves
that projection against the REAL request path: for every provider lane
and capability shape, each knob position is driven through the actual
provider ``create_streaming`` against a recording fake client (the same
SDK-seam capture the wire-payload goldens use), the effort-relevant
subset of the captured kwargs is extracted, and it must equal what the
ladder token decodes to.  Two invariants per shape:

1. **Semantics** — each ladder token decodes to an expected wire subset
   (``on``/``off`` ⇒ the chat-template toggle, ``budget:N`` ⇒ Anthropic
   thinking budget, a bare level ⇒ the lane's flat/effort channel) and
   the observed wire subset must match it exactly.
2. **Grouping** — the ladder's core promise: two knob positions carry
   equal ``effective`` tokens if and only if they produce identical
   effort-relevant wire payloads.

A failure here means the UI annotates behavior the wire does not have —
the bug class that shipped xai in the ladder's chat-lane set even though
``XAIProvider`` rides the Responses surface, which drops ``extra_body``.

The harness goes through ``create_provider`` (not direct classes) so the
provider ROUTING the ladder assumes — e.g. ``api_surface="responses"``
selecting the Responses adapter — is itself under test.
"""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
from typing import Any

import pytest

from tests._wire_capture import RecordingClient
from turnstone.core.providers import create_provider
from turnstone.core.providers._protocol import ModelCapabilities
from turnstone.core.providers.effort_ladder import KNOB_VALUES, effort_ladder

# Big enough that Anthropic manual-mode budgets are never clamped by
# max_tokens (the ladder documents budgets unclamped).
_MAX_TOKENS = 32_000


@dataclasses.dataclass(frozen=True)
class Shape:
    """One (provider lane, capability shape) point of the parity matrix."""

    id: str
    provider: str
    caps: ModelCapabilities
    api_surface: str = ""
    model: str = "m"


# Real registry rows for the two lanes whose defaults carry effort values —
# parity should cover what ships, not only synthetic shapes.
_GEMINI_CAPS = create_provider("google").get_capabilities("gemini-3-flash")
_GROK_CAPS = create_provider("xai").get_capabilities("grok-4.3")

SHAPES: tuple[Shape, ...] = (
    # -- anthropic-compatible (vLLM /v1/messages): template channel only --
    Shape(
        "compat-toggle-manual",
        "anthropic-compatible",
        ModelCapabilities(thinking_mode="manual", thinking_param="enable_thinking"),
    ),
    Shape(
        "compat-toggle-adaptive",
        "anthropic-compatible",
        ModelCapabilities(thinking_mode="adaptive", thinking_param="enable_thinking"),
    ),
    Shape(
        "compat-freeform-effort",
        "anthropic-compatible",
        ModelCapabilities(
            thinking_mode="manual",
            thinking_param="thinking",
            effort_param="reasoning_effort",
        ),
    ),
    Shape(
        # DeepSeek-V4 official contract: toggle + effort in {high, max}.
        "compat-validated-effort",
        "anthropic-compatible",
        ModelCapabilities(
            thinking_mode="manual",
            thinking_param="thinking",
            effort_param="reasoning_effort",
            reasoning_effort_values=("high", "max"),
            default_reasoning_effort="high",
        ),
    ),
    Shape(
        "compat-inert",
        "anthropic-compatible",
        ModelCapabilities(thinking_mode="none"),
    ),
    # -- openai-compatible on the Chat Completions surface: both channels --
    Shape(
        "oc-toggle-only",
        "openai-compatible",
        ModelCapabilities(thinking_mode="manual", thinking_param="enable_thinking"),
    ),
    Shape(
        "oc-toggle-plus-flat",
        "openai-compatible",
        ModelCapabilities(
            thinking_mode="manual",
            thinking_param="enable_thinking",
            reasoning_effort_values=("low", "medium", "high"),
            default_reasoning_effort="medium",
        ),
    ),
    Shape(
        "oc-effort-param-suppresses-flat",
        "openai-compatible",
        ModelCapabilities(
            thinking_mode="manual",
            thinking_param="enable_thinking",
            effort_param="reasoning_effort",
            reasoning_effort_values=("low", "medium", "high"),
            default_reasoning_effort="medium",
        ),
    ),
    Shape(
        "oc-flat-only",
        "openai-compatible",
        ModelCapabilities(
            reasoning_effort_values=("low", "medium", "high"),
            default_reasoning_effort="medium",
        ),
    ),
    Shape(
        "oc-adaptive",
        "openai-compatible",
        ModelCapabilities(thinking_mode="adaptive", thinking_param="enable_thinking"),
    ),
    # -- openai-compatible pinned to the Responses surface: template caps
    #    become inert and only the native flat channel remains --
    Shape(
        "oc-responses-surface",
        "openai-compatible",
        ModelCapabilities(
            thinking_mode="manual",
            thinking_param="enable_thinking",
            effort_param="reasoning_effort",
            reasoning_effort_values=("low", "medium", "high"),
            default_reasoning_effort="medium",
        ),
        api_surface="responses",
    ),
    # -- commercial flat lanes --
    Shape(
        "openai-flat",
        "openai",
        ModelCapabilities(
            reasoning_effort_values=("low", "medium", "high"),
            default_reasoning_effort="medium",
        ),
    ),
    Shape("google-default", "google", _GEMINI_CAPS, model="gemini-3-flash"),
    Shape(
        # GoogleProvider subclasses the chat provider, so a template
        # override DOES change real requests — hybrid toggle + flat.
        "google-manual-override",
        "google",
        dataclasses.replace(_GEMINI_CAPS, thinking_mode="manual", thinking_param="enable_thinking"),
        model="gemini-3-flash",
    ),
    Shape("xai-default", "xai", _GROK_CAPS, model="grok-4.3"),
    Shape(
        # XAIProvider rides the Responses surface: template overrides are
        # inert on the wire, and the ladder must not pretend otherwise.
        "xai-template-override-inert",
        "xai",
        dataclasses.replace(
            _GROK_CAPS,
            thinking_mode="manual",
            thinking_param="enable_thinking",
            effort_param="reasoning_effort",
        ),
        model="grok-4.3",
    ),
    # -- native Anthropic --
    Shape(
        "anthropic-adaptive-effort",
        "anthropic",
        ModelCapabilities(
            thinking_mode="adaptive",
            supports_effort=True,
            effort_levels=("low", "medium", "high", "xhigh", "max"),
        ),
        model="claude-fable-5",
    ),
    Shape(
        "anthropic-adaptive-plain",
        "anthropic",
        ModelCapabilities(thinking_mode="adaptive"),
        model="claude-fable-5",
    ),
    Shape(
        "anthropic-manual-budgets",
        "anthropic",
        ModelCapabilities(thinking_mode="manual"),
        model="claude-3-7-sonnet-latest",
    ),
    Shape(
        "anthropic-manual-plus-effort",
        "anthropic",
        ModelCapabilities(
            thinking_mode="manual",
            supports_effort=True,
            effort_levels=("low", "medium", "high"),
        ),
        model="claude-3-7-sonnet-latest",
    ),
    Shape(
        "anthropic-none-effort",
        "anthropic",
        ModelCapabilities(
            thinking_mode="none",
            supports_effort=True,
            effort_levels=("low", "medium", "high"),
        ),
        model="claude-3-5-haiku-latest",
    ),
    Shape(
        "anthropic-inert",
        "anthropic",
        ModelCapabilities(thinking_mode="none"),
        model="claude-3-5-haiku-latest",
    ),
)


# --------------------------------------------------------------------------- #
# Wire capture + effort-subset extraction
# --------------------------------------------------------------------------- #


def _wire_payload(shape: Shape, knob: str) -> dict[str, Any]:
    """Drive the real provider request path; return the captured SDK kwargs."""
    provider = create_provider(shape.provider, api_surface=shape.api_surface or None)
    client = RecordingClient()
    gen = provider.create_streaming(
        client=client,
        model=shape.model,
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=_MAX_TOKENS,
        reasoning_effort=knob,
        capabilities=shape.caps,
    )
    # kwargs are recorded eagerly during the call above; close the
    # unconsumed iterator so stream-manager cleanup runs on the stub.
    close = getattr(gen, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):
            close()
    assert "payload" in client.captured, f"{shape.id}: provider made no SDK call"
    return dict(client.captured["payload"])


def _effort_wire_subset(payload: dict[str, Any], caps: ModelCapabilities) -> dict[str, Any]:
    """Every effort-related lever in *payload*, normalized across lanes.

    Keys: ``thinking`` (native Anthropic param), ``output_effort``
    (Anthropic ``output_config.effort``), ``flat`` (Chat Completions
    ``reasoning_effort`` / Responses ``reasoning.effort``), ``toggle``
    and ``template_effort`` (``extra_body.chat_template_kwargs``).
    """
    subset: dict[str, Any] = {}
    if "thinking" in payload:
        subset["thinking"] = payload["thinking"]
    output_config = payload.get("output_config")
    if isinstance(output_config, dict) and "effort" in output_config:
        subset["output_effort"] = output_config["effort"]
    if "reasoning_effort" in payload:
        subset["flat"] = payload["reasoning_effort"]
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict) and "effort" in reasoning:
        subset["flat"] = reasoning["effort"]
    extra_body = payload.get("extra_body")
    ctk = extra_body.get("chat_template_kwargs") if isinstance(extra_body, dict) else None
    if isinstance(ctk, dict):
        known = {caps.thinking_param, caps.effort_param} - {""}
        unexpected = set(ctk) - known
        assert not unexpected, f"unexpected chat_template_kwargs keys: {unexpected}"
        if caps.thinking_param in ctk:
            subset["toggle"] = ctk[caps.thinking_param]
        if caps.effort_param and caps.effort_param in ctk:
            subset["template_effort"] = ctk[caps.effort_param]
    return subset


# --------------------------------------------------------------------------- #
# Ladder-token decoding — the token grammar, made executable
# --------------------------------------------------------------------------- #


def _decode_token(shape: Shape, token: str) -> dict[str, Any]:
    """Expected effort wire subset for a ladder ``effective`` token."""
    caps = shape.caps
    if shape.provider == "anthropic":
        return _decode_native(caps, token)
    if shape.provider in ("openai", "xai") or shape.api_surface == "responses":
        return {} if token == "default" else {"flat": token}
    return _decode_template(shape.provider, caps, token)


def _decode_native(caps: ModelCapabilities, token: str) -> dict[str, Any]:
    if caps.thinking_mode == "adaptive":
        # Thinking is unconditionally adaptive; a non-"adaptive" token is
        # the output_config effort level riding on top.
        expected: dict[str, Any] = {"thinking": {"type": "adaptive"}}
        if token != "adaptive":
            expected["output_effort"] = token
        return expected
    if token in ("default", "off"):
        return {}
    effort, sep, budget = token.partition("·budget:")
    if sep:
        return {
            "output_effort": effort,
            "thinking": {"type": "enabled", "budget_tokens": int(budget)},
        }
    if token.startswith("budget:"):
        budget_tokens = int(token.removeprefix("budget:"))
        return {"thinking": {"type": "enabled", "budget_tokens": budget_tokens}}
    return {"output_effort": token}


def _decode_template(provider: str, caps: ModelCapabilities, token: str) -> dict[str, Any]:
    if token == "default":
        return {}
    parts = token.split("+")
    expected: dict[str, Any] = {}
    if parts[0] in ("on", "off"):
        expected["toggle"] = parts[0] == "on"
        parts = parts[1:]
    if parts:
        assert len(parts) == 1, f"unparseable ladder token: {token!r}"
        if caps.effort_param:
            expected["template_effort"] = parts[0]
        else:
            # The anthropic-compatible lane has no flat channel, so a
            # bare effort part there could only be the template key.
            assert provider != "anthropic-compatible", token
            expected["flat"] = parts[0]
    return expected


# --------------------------------------------------------------------------- #
# The parity tests
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: s.id)
def test_ladder_tokens_match_wire(shape: Shape) -> None:
    """Invariant 1: each token's decoded meaning equals the captured wire."""
    ladder = effort_ladder(shape.provider, shape.caps, shape.api_surface)
    assert [row["value"] for row in ladder] == list(KNOB_VALUES)
    for row in ladder:
        knob, token = row["value"], row["effective"]
        observed = _effort_wire_subset(_wire_payload(shape, knob), shape.caps)
        expected = _decode_token(shape, token)
        assert observed == expected, (
            f"{shape.id}/knob={knob}: ladder says {token!r} which decodes to "
            f"{expected}, but the wire carries {observed}"
        )


@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: s.id)
def test_equal_tokens_iff_equal_wire(shape: Shape) -> None:
    """Invariant 2: token equality ⇔ effort-wire equality, per shape."""
    tokens = {
        row["value"]: row["effective"]
        for row in effort_ladder(shape.provider, shape.caps, shape.api_surface)
    }
    subsets = {
        knob: _effort_wire_subset(_wire_payload(shape, knob), shape.caps) for knob in KNOB_VALUES
    }
    for a, b in itertools.combinations(KNOB_VALUES, 2):
        same_token = tokens[a] == tokens[b]
        same_wire = subsets[a] == subsets[b]
        assert same_token == same_wire, (
            f"{shape.id}: knobs {a!r}/{b!r} have "
            f"{'equal' if same_token else 'distinct'} tokens "
            f"({tokens[a]!r} vs {tokens[b]!r}) but "
            f"{'identical' if same_wire else 'different'} wire subsets "
            f"({subsets[a]} vs {subsets[b]})"
        )
