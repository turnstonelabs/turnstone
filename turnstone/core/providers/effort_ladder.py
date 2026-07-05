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
local-lane template toggle.

Two documented approximations:

* The ladder describes what Turnstone SENDS — a server-side template
  may alias further (e.g. DeepSeek-V4 maps ``low``/``medium`` to its
  default ``high`` tier).
* Anthropic manual-mode budgets are shown unclamped.  The request path
  additionally clamps ``budget_tokens`` below the per-request
  ``max_tokens``, so a very small ``max_tokens`` can collapse tiers (or
  disable thinking) at request time — not knowable from capabilities.
"""

from __future__ import annotations

import dataclasses
from typing import Any

# _map_reasoning_to_effort is Anthropic-lane semantics shared with the
# request path deliberately — projecting through the same function is
# what keeps the ladder honest.
from turnstone.core.providers._anthropic import (
    DEFAULT_THINKING_BUDGET,
    EFFORT_BUDGET_MAP,
    _map_reasoning_to_effort,
)
from turnstone.core.providers._protocol import (
    EFFORT_TEMPLATE_FALLBACK_PARAM,
    ModelCapabilities,
    flat_effort_suppressed,
    reasoning_template_kwargs,
    resolve_reasoning_effort,
)

# The full session-knob domain, in ladder order (mirrors the console
# selects; "" rides as the caller's alias for its default and is not a
# ladder row).
KNOB_VALUES: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# Providers that serve requests through OpenAIChatCompletionsProvider (or
# a subclass) and therefore inherit BOTH channels: chat_template_kwargs
# injection via _finalize_extra_body AND the flat reasoning_effort param
# via apply_temperature_and_effort.  google belongs here because
# GoogleProvider subclasses the chat provider — an operator override that
# sets thinking_mode/effort_param on such a model changes real requests,
# and the ladder must mirror that.  xai does NOT: XAIProvider subclasses
# OpenAIResponsesProvider, whose surface ignores extra_body entirely, so
# template overrides can never change an xai request and the ladder
# projects xai through the flat channel only (fallthrough in _effective).
_CHAT_LANES = frozenset({"openai-compatible", "google"})


def effort_ladder(
    provider_name: str,
    caps: ModelCapabilities,
    api_surface: str = "",
) -> list[dict[str, str]]:
    """Project every knob position to its effective wire behavior.

    Returns ``[{"value": <knob>, "effective": <token>}, ...]`` in knob
    order.  Equal ``effective`` tokens ⇒ identical requests.

    *api_surface* mirrors ``server_compat["api_surface"]``: the
    ``"responses"`` surface handles reasoning natively and ignores
    ``extra_body``, so chat-lane providers pinned to it project through
    the flat param only — the same divergence ``create_provider``
    applies at request time.
    """
    return [
        {"value": knob, "effective": _effective(provider_name, caps, knob, api_surface)}
        for knob in KNOB_VALUES
    ]


def effort_ladder_for_model(
    provider_name: str,
    model: str,
    capability_overrides: dict[str, Any] | None,
    api_surface: str = "",
) -> list[dict[str, str]]:
    """Ladder for a stored model row: provider defaults + operator overrides.

    Mirrors ``ChatSession._resolve_capabilities`` — overrides are
    field-filtered and applied over the provider's per-model defaults.
    Callers holding a raw DB row must parse the ``capabilities`` JSON
    string first (``model_registry`` does the same) — this function
    takes a dict.
    """
    from turnstone.core.providers import create_provider

    caps = create_provider(provider_name, api_surface=api_surface or None).get_capabilities(model)
    if capability_overrides:
        fields = {f.name for f in dataclasses.fields(type(caps))}
        overrides = {k: v for k, v in capability_overrides.items() if k in fields}
        if overrides:
            caps = dataclasses.replace(caps, **overrides)
    return effort_ladder(provider_name, caps, api_surface)


def _effective(
    provider_name: str,
    caps: ModelCapabilities,
    knob: str,
    api_surface: str = "",
) -> str:
    if provider_name == "anthropic":
        return _effective_anthropic(caps, knob)
    if provider_name == "anthropic-compatible":
        return _effective_local(provider_name, caps, knob)
    if provider_name in _CHAT_LANES:
        if api_surface == "responses":
            # Responses surface: native reasoning, extra_body ignored.
            return resolve_reasoning_effort(caps, knob) or "default"
        return _effective_local(provider_name, caps, knob)
    # openai / xai — Responses-surface providers: reasoning rides the
    # native ``reasoning={"effort": ...}`` param and extra_body is
    # ignored, so the flat channel is the whole story.
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
        budget = EFFORT_BUDGET_MAP.get(knob, DEFAULT_THINKING_BUDGET)
        return f"{effort}·budget:{budget}" if effort else f"budget:{budget}"
    # thinking_mode "none": output_config effort still applies — the
    # request path gates it on supports_effort alone, not thinking_mode.
    return effort or "default"


def _effective_local(provider_name: str, caps: ModelCapabilities, knob: str) -> str:
    """Chat lanes — mirrors ``merge_reasoning_template_kwargs`` (+ flat param)."""
    # The anthropic-compatible request path passes the fallback effort
    # key (its only effort channel is chat_template_kwargs); the chat
    # lanes carry effort on the flat param instead.
    fallback = EFFORT_TEMPLATE_FALLBACK_PARAM if provider_name == "anthropic-compatible" else ""
    updates = reasoning_template_kwargs(caps, knob, fallback_effort_param=fallback)
    effort_param = caps.effort_param or fallback
    parts: list[str] = []
    if caps.thinking_param in updates:
        parts.append("on" if updates[caps.thinking_param] else "off")
    if effort_param and effort_param in updates:
        parts.append(str(updates[effort_param]))
    # Chat-completions providers also send the flat param unless a
    # declared effort_param claims the template channel — the same
    # ``flat_effort_suppressed`` predicate ``apply_temperature_and_effort``
    # applies at request time.  The anthropic-compatible lane has no
    # flat param.
    if provider_name != "anthropic-compatible" and not flat_effort_suppressed(caps):
        flat = resolve_reasoning_effort(caps, knob)
        if flat:
            parts.append(flat)
    return "+".join(parts) if parts else "default"
