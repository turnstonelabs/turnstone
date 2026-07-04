"""Effective effort-ladder projection — what each knob position actually does.

The session reasoning-effort knob has seven positions; how many are
DISTINCT depends on the provider lane and the model's capabilities
(qwen3.6: two — off/on; a freeform ``effort_param`` model: one per
forwarded value; Claude with effort levels: one per level plus the
adaptive fallback).  This module projects the knob domain through the
same mapping functions the providers use at request time, so UIs can
annotate knob positions that alias to identical wire behavior instead
of presenting seven positions as seven behaviors.

Pure computation — no network, no provider clients.  Labels are short
comparable tokens: two knob positions with the same ``effective`` string
produce the same request.  ``"default"`` means nothing effort-related is
sent (the server/model default applies); ``"off"``/``"on"`` describe the
local-lane template toggle.  The ladder describes what Turnstone SENDS —
a server-side template may alias further (e.g. DeepSeek-V4 maps
``low``/``medium`` to its default ``high`` tier).
"""

from __future__ import annotations

import dataclasses
from typing import Any

from turnstone.core.providers._anthropic import (
    _DEFAULT_THINKING_BUDGET,
    _EFFORT_BUDGET_MAP,
    _map_reasoning_to_effort,
)
from turnstone.core.providers._protocol import (
    ModelCapabilities,
    reasoning_template_kwargs,
    resolve_reasoning_effort,
)

# The full session-knob domain, in ladder order (mirrors the console
# selects; "" rides as the caller's alias for its default and is not a
# ladder row).
KNOB_VALUES: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# Lanes whose reasoning control rides chat_template_kwargs
# (merge_reasoning_template_kwargs) rather than native params.
_LOCAL_LANES = frozenset({"openai-compatible", "anthropic-compatible"})


def effort_ladder(provider_name: str, caps: ModelCapabilities) -> list[dict[str, str]]:
    """Project every knob position to its effective wire behavior.

    Returns ``[{"value": <knob>, "effective": <token>}, ...]`` in knob
    order.  Equal ``effective`` tokens ⇒ identical requests.
    """
    return [
        {"value": knob, "effective": _effective(provider_name, caps, knob)} for knob in KNOB_VALUES
    ]


def effort_ladder_for_model(
    provider_name: str,
    model: str,
    capability_overrides: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """Ladder for a stored model row: provider defaults + operator overrides.

    Mirrors ``ChatSession._resolve_capabilities`` — overrides are
    field-filtered and applied over the provider's per-model defaults.
    """
    from turnstone.core.providers import create_provider

    caps = create_provider(provider_name).get_capabilities(model)
    if capability_overrides:
        fields = {f.name for f in dataclasses.fields(type(caps))}
        overrides = {k: v for k, v in capability_overrides.items() if k in fields}
        if overrides:
            caps = dataclasses.replace(caps, **overrides)
    return effort_ladder(provider_name, caps)


def _effective(provider_name: str, caps: ModelCapabilities, knob: str) -> str:
    if provider_name == "anthropic":
        return _effective_anthropic(caps, knob)
    if provider_name in _LOCAL_LANES:
        return _effective_local(provider_name, caps, knob)
    # Flat-param lanes: openai (chat + responses), google, xai.
    return resolve_reasoning_effort(caps, knob) or "default"


def _effective_anthropic(caps: ModelCapabilities, knob: str) -> str:
    """Native lane — mirrors ``_build_thinking_and_kwargs``."""
    effort = _map_reasoning_to_effort(knob, caps.effort_levels) if caps.supports_effort else None
    if caps.thinking_mode == "adaptive":
        # Thinking always on, model self-regulates; effort rides
        # output_config when the knob maps onto a supported level.
        return effort or "adaptive"
    if caps.thinking_mode == "manual":
        if not knob or knob == "none":
            return "off"
        budget = _EFFORT_BUDGET_MAP.get(knob, _DEFAULT_THINKING_BUDGET)
        return f"{effort}·budget:{budget}" if effort else f"budget:{budget}"
    return "default"


def _effective_local(provider_name: str, caps: ModelCapabilities, knob: str) -> str:
    """Local lanes — mirrors ``merge_reasoning_template_kwargs`` (+ flat param)."""
    updates = reasoning_template_kwargs(caps, knob)
    parts: list[str] = []
    if caps.thinking_param in updates:
        parts.append("on" if updates[caps.thinking_param] else "off")
    if caps.effort_param and caps.effort_param in updates:
        parts.append(str(updates[caps.effort_param]))
    # openai-compatible additionally sends the flat param when no
    # effort_param declares the template channel (suppression rule in
    # apply_temperature_and_effort).
    if provider_name == "openai-compatible" and not caps.effort_param:
        flat = resolve_reasoning_effort(caps, knob)
        if flat:
            parts.append(flat)
    return "+".join(parts) if parts else "default"
