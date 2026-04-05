"""Shared helpers for OpenAI-family providers (Chat Completions & Responses).

Capability table, temperature/reasoning gating, cache retention, citation
formatting, and message sanitisation live here so both
``OpenAIChatCompletionsProvider`` and ``OpenAIResponsesProvider`` stay DRY.
"""

from __future__ import annotations

from typing import Any

from turnstone.core.providers._protocol import (
    ModelCapabilities,
    UsageInfo,
    _lookup_capabilities,
)

# ---------------------------------------------------------------------------
# Model capability table
# ---------------------------------------------------------------------------

OPENAI_CAPABILITIES: dict[str, ModelCapabilities] = {
    # GPT-5 base — NO temperature support
    "gpt-5": ModelCapabilities(
        context_window=400000,
        max_output_tokens=128000,
        supports_temperature=False,
        reasoning_effort_values=("minimal", "low", "medium", "high"),
        default_reasoning_effort="medium",
        supports_vision=True,
    ),
    "gpt-5-mini": ModelCapabilities(
        context_window=400000,
        max_output_tokens=128000,
        supports_temperature=False,
        reasoning_effort_values=("minimal", "low", "medium", "high"),
        default_reasoning_effort="medium",
        supports_vision=True,
    ),
    "gpt-5-nano": ModelCapabilities(
        context_window=400000,
        max_output_tokens=128000,
        supports_temperature=False,
        reasoning_effort_values=("minimal", "low", "medium", "high"),
        default_reasoning_effort="medium",
        supports_vision=True,
    ),
    # GPT-5 pro — high reasoning only, extended output
    "gpt-5-pro": ModelCapabilities(
        context_window=400000,
        max_output_tokens=272000,
        supports_temperature=False,
        reasoning_effort_values=("high",),
        default_reasoning_effort="high",
        supports_vision=True,
    ),
    # GPT-5.1 — temperature OK when reasoning_effort=none (default)
    "gpt-5.1": ModelCapabilities(
        context_window=400000,
        max_output_tokens=128000,
        reasoning_effort_values=("none", "low", "medium", "high"),
        default_reasoning_effort="none",
        supports_vision=True,
    ),
    # GPT-5.2 — adds xhigh
    "gpt-5.2": ModelCapabilities(
        context_window=400000,
        max_output_tokens=128000,
        reasoning_effort_values=("none", "low", "medium", "high", "xhigh"),
        default_reasoning_effort="none",
        supports_vision=True,
    ),
    # GPT-5.2 pro — always-reasoning variant
    "gpt-5.2-pro": ModelCapabilities(
        context_window=400000,
        max_output_tokens=128000,
        supports_temperature=False,
        reasoning_effort_values=("medium", "high", "xhigh"),
        default_reasoning_effort="medium",
        supports_vision=True,
    ),
    # GPT-5.3 — same capabilities as 5.2 (matches gpt-5.3-chat-latest, codex)
    "gpt-5.3": ModelCapabilities(
        context_window=400000,
        max_output_tokens=128000,
        reasoning_effort_values=("none", "low", "medium", "high", "xhigh"),
        default_reasoning_effort="none",
        supports_vision=True,
    ),
    # GPT-5.4 — 1M context window, native tool search
    "gpt-5.4": ModelCapabilities(
        context_window=1050000,
        max_output_tokens=128000,
        reasoning_effort_values=("none", "low", "medium", "high", "xhigh"),
        default_reasoning_effort="none",
        supports_tool_search=True,
        supports_vision=True,
    ),
    # GPT-5.4 pro — always-reasoning, 1M context, native tool search
    "gpt-5.4-pro": ModelCapabilities(
        context_window=1050000,
        max_output_tokens=128000,
        supports_temperature=False,
        reasoning_effort_values=("medium", "high", "xhigh"),
        default_reasoning_effort="medium",
        supports_tool_search=True,
        supports_vision=True,
    ),
    # O-series reasoning models
    "o1": ModelCapabilities(
        context_window=200000,
        max_output_tokens=100000,
        supports_temperature=False,
        supports_streaming=False,
        supports_vision=True,
    ),
    "o1-mini": ModelCapabilities(
        context_window=128000,
        max_output_tokens=65536,
        supports_temperature=False,
        supports_streaming=False,
        supports_vision=True,
    ),
    "o3": ModelCapabilities(
        context_window=200000,
        max_output_tokens=100000,
        supports_temperature=False,
        supports_vision=True,
    ),
    "o3-mini": ModelCapabilities(
        context_window=200000,
        max_output_tokens=100000,
        supports_temperature=False,
        supports_vision=True,
    ),
    "o3-pro": ModelCapabilities(
        context_window=200000,
        max_output_tokens=100000,
        supports_temperature=False,
        supports_streaming=False,
        supports_vision=True,
    ),
    "o4-mini": ModelCapabilities(
        context_window=200000,
        max_output_tokens=100000,
        supports_temperature=False,
        supports_vision=True,
    ),
    # Search models — always search on every request, no reasoning_effort
    "gpt-5-search-api": ModelCapabilities(
        context_window=400000,
        max_output_tokens=128000,
        supports_temperature=False,
        supports_web_search=True,
        reasoning_effort_values=(),
        supports_vision=True,
    ),
}

# Default for unknown models (local servers: vLLM, llama.cpp, etc.)
OPENAI_DEFAULT = ModelCapabilities()


def lookup_openai_capabilities(model: str) -> ModelCapabilities:
    """Find capabilities for *model* by longest prefix match."""
    return _lookup_capabilities(model, OPENAI_CAPABILITIES, OPENAI_DEFAULT)


# ---------------------------------------------------------------------------
# Temperature and reasoning effort gating
# ---------------------------------------------------------------------------


def apply_temperature(
    kwargs: dict[str, Any],
    caps: ModelCapabilities,
    temperature: float,
    reasoning_effort: str,
) -> None:
    """Conditionally add temperature to *kwargs*.

    - Models with ``supports_temperature=False`` (GPT-5 base, O-series)
      never receive temperature.
    - Models that list ``"none"`` in their effort values (GPT-5.1/5.2)
      only receive temperature when reasoning is inactive.
    """
    if not caps.supports_temperature:
        return
    if "none" in caps.reasoning_effort_values and reasoning_effort not in ("none", ""):
        return  # Skip temperature when reasoning is active
    kwargs["temperature"] = temperature


def resolve_reasoning_effort(caps: ModelCapabilities, reasoning_effort: str) -> str | None:
    """Return the validated reasoning effort value, or ``None`` to omit.

    Validates against supported values and falls back to model default.
    """
    if not caps.reasoning_effort_values or not reasoning_effort or reasoning_effort == "none":
        return None
    if reasoning_effort in caps.reasoning_effort_values:
        return reasoning_effort
    if caps.default_reasoning_effort and caps.default_reasoning_effort != "none":
        return caps.default_reasoning_effort
    return None


def apply_temperature_and_effort(
    kwargs: dict[str, Any],
    caps: ModelCapabilities,
    temperature: float,
    reasoning_effort: str,
) -> None:
    """Conditionally add temperature and reasoning_effort to *kwargs*.

    Chat Completions API version — reasoning effort is a flat parameter.
    """
    apply_temperature(kwargs, caps, temperature, reasoning_effort)
    effort = resolve_reasoning_effort(caps, reasoning_effort)
    if effort:
        kwargs["reasoning_effort"] = effort


# ---------------------------------------------------------------------------
# Cache retention
# ---------------------------------------------------------------------------


def apply_cache_retention(kwargs: dict[str, Any], model: str) -> None:
    """Enable 24-hour extended prompt cache retention for GPT-5.x models.

    OpenAI caching is automatic (no code changes for basic caching), but
    the default TTL is only 5-10 minutes.  Extended retention keeps cached
    KV tensors for up to 24 hours at no additional cost, which is valuable
    for workstreams with bursty activity patterns.
    """
    if model.startswith("gpt-5"):
        kwargs["prompt_cache_retention"] = "24h"


# ---------------------------------------------------------------------------
# Tool search (native deferred loading)
# ---------------------------------------------------------------------------


def apply_tool_search(
    caps: ModelCapabilities,
    tools: list[dict[str, Any]] | None,
    deferred_names: frozenset[str] | None = None,
) -> list[dict[str, Any]] | None:
    """Mark deferred tools with ``defer_loading: true`` for native search.

    For GPT-5.4+ models that support tool search, OpenAI's API handles
    discovery automatically — no explicit search tool is needed.
    """
    if not caps.supports_tool_search or not deferred_names or not tools:
        return tools
    result = []
    for tool in tools:
        name = tool.get("function", {}).get("name", "")
        if name in deferred_names:
            result.append({**tool, "defer_loading": True})
        else:
            result.append(tool)
    return result


# ---------------------------------------------------------------------------
# Citation formatting
# ---------------------------------------------------------------------------


def format_citations(content: str, annotations: list[Any]) -> str:
    """Append url_citation sources as footnotes at the end of the content."""
    seen_urls: set[str] = set()
    sources: list[str] = []
    for ann in annotations:
        ann_type = getattr(ann, "type", None)
        if ann_type == "url_citation":
            title: str = ""
            url: str = ""
            citation = getattr(ann, "url_citation", None)
            if citation is not None:
                # Chat Completions API: nested url_citation object
                title = getattr(citation, "title", "") or ""
                url = getattr(citation, "url", "") or ""
            elif hasattr(ann, "url") and isinstance(getattr(ann, "url", None), str):
                # Responses API: attributes directly on the annotation
                title = getattr(ann, "title", "") or ""
                url = getattr(ann, "url", "") or ""
            if url and url not in seen_urls:
                seen_urls.add(url)
                sources.append(f"[{title}]({url})" if title else url)
    if sources:
        content += "\n\nSources:\n" + "\n".join(f"- {s}" for s in sources)
    return content


# ---------------------------------------------------------------------------
# Message sanitisation (Chat Completions specific but shared for compat)
# ---------------------------------------------------------------------------


def sanitize_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Ensure assistant messages always have ``content`` or ``tool_calls``.

    OpenAI-compatible APIs reject assistant messages that have neither.
    This is a defensive catch-all; the upstream layers should already
    guarantee well-formed messages.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        if (
            msg.get("role") == "assistant"
            and msg.get("content") is None
            and not msg.get("tool_calls")
        ):
            msg = {**msg, "content": ""}
        out.append(msg)
    return out


# ---------------------------------------------------------------------------
# Usage extraction
# ---------------------------------------------------------------------------


def extract_usage(usage_obj: Any) -> UsageInfo | None:
    """Normalize usage from either Chat Completions or Responses API.

    Chat Completions uses ``prompt_tokens`` / ``completion_tokens``.
    Responses API uses ``input_tokens`` / ``output_tokens``.
    We check for each in order, preferring the real SDK attribute names.
    """
    if usage_obj is None:
        return None

    # Token counts — prefer Chat Completions names, fall back to Responses API
    pt = getattr(usage_obj, "prompt_tokens", None)
    if not isinstance(pt, int):
        pt = getattr(usage_obj, "input_tokens", None)
    ct = getattr(usage_obj, "completion_tokens", None)
    if not isinstance(ct, int):
        ct = getattr(usage_obj, "output_tokens", None)
    tt = getattr(usage_obj, "total_tokens", None)
    if not isinstance(pt, int) or not isinstance(ct, int):
        return None

    # Cache tokens — Chat Completions: prompt_tokens_details.cached_tokens,
    # Responses API: input_tokens_details.cached_tokens
    ptd = getattr(usage_obj, "prompt_tokens_details", None)
    if ptd is None:
        ptd = getattr(usage_obj, "input_tokens_details", None)
    cached = getattr(ptd, "cached_tokens", 0) if ptd is not None else 0

    return UsageInfo(
        prompt_tokens=pt,
        completion_tokens=ct,
        total_tokens=tt if isinstance(tt, int) else (pt + ct),
        cache_read_tokens=cached if isinstance(cached, int) else 0,
    )


# ---------------------------------------------------------------------------
# Retryable error names (shared across both OpenAI providers)
# ---------------------------------------------------------------------------

RETRYABLE_ERROR_NAMES: frozenset[str] = frozenset(
    {
        "APIError",
        "APIConnectionError",
        "RateLimitError",
        "Timeout",
        "APITimeoutError",
    }
)
