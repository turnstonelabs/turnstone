"""Shared helpers for OpenAI-family providers (Chat Completions & Responses).

Capability table, temperature/reasoning gating, cache retention, citation
formatting, and message sanitisation live here so both
``OpenAIChatCompletionsProvider`` and ``OpenAIResponsesProvider`` stay DRY.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from turnstone.core.providers._protocol import (
    ModelCapabilities,
    UsageInfo,
    _lookup_capabilities,
)

log = structlog.get_logger(__name__)

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
OPENAI_DEFAULT = ModelCapabilities(supports_tool_advisories=False)


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


def _escape_attr(value: str) -> str:
    """Minimal XML-attribute escape — prevents quote-break injection."""
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def format_document_wrapper(name: str, mime: str, data: str) -> str:
    """Produce the ``<document>...</document>`` wrapper used by non-Anthropic
    providers that lack a native document block.

    Attribute values are escaped.  A literal ``</document>`` appearing in
    ``data`` is neutralized so the model can't be tricked into ending the
    document region early via attacker-controlled payloads.
    """
    safe_name = _escape_attr(name or "")
    safe_mime = _escape_attr(mime or "text/plain")
    safe_data = (data or "").replace("</document>", "<\\/document>")
    return f'<document name="{safe_name}" media_type="{safe_mime}">\n{safe_data}\n</document>'


def inline_document_parts(parts: list[Any]) -> list[Any]:
    """Rewrite internal ``document`` content parts as text parts.

    OpenAI Chat Completions and the Google OpenAI-compat endpoint do not
    accept a native ``document`` block type, so we wrap the text payload
    in an escaped delimiter and emit it as a plain text part.  Other
    part types pass through unchanged.
    """
    out: list[Any] = []
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "document":
            d = part.get("document", {})
            out.append(
                {
                    "type": "text",
                    "text": format_document_wrapper(
                        d.get("name", ""),
                        d.get("media_type", "text/plain"),
                        d.get("data", ""),
                    ),
                }
            )
        else:
            out.append(part)
    return out


def _inline_documents_in_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Return ``msg`` with any list-type content's ``document`` parts inlined."""
    content = msg.get("content")
    if isinstance(content, list):
        return {**msg, "content": inline_document_parts(content)}
    return msg


def sanitize_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Sanitize messages for OpenAI-compatible APIs.

    Performs three repairs:

    1. Ensures assistant messages always have ``content`` or ``tool_calls``
       (APIs reject messages with neither).
    2. Fills empty tool_call IDs with synthetic ``call_{uuid}`` values
       (local servers sometimes omit them).
    3. Detects and repairs orphaned tool_call / tool_result pairs:

       - Synthesizes error tool messages for tool_calls with no matching
         tool result.
       - Drops tool messages whose ``tool_call_id`` has no matching
         tool_call in the preceding assistant message.

    Returns a new list; the original messages are not mutated.
    """
    # Drop internal sibling keys (``_provider_content``,
    # ``_attachments_meta``, etc.) that the OpenAI / Google-compat APIs
    # don't understand before they reach the wire.
    messages = [
        {k: v for k, v in m.items() if not (isinstance(k, str) and k.startswith("_"))}
        for m in messages
    ]
    # Inline any internal ``document`` content parts — OpenAI Chat
    # Completions does not accept a native document block type.
    messages = [_inline_documents_in_message(m) for m in messages]
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "")

        # (1) Fix empty-content assistant messages
        if role == "assistant" and msg.get("content") is None and not msg.get("tool_calls"):
            msg = {**msg, "content": ""}
            out.append(msg)
            i += 1
            continue

        # (2+3) Assistant with tool_calls: fix IDs and detect orphans
        if role == "assistant" and msg.get("tool_calls"):
            tool_calls = msg["tool_calls"]

            # Back-fill empty IDs and build positional remap for tool results.
            # Local servers (vLLM, llama.cpp) sometimes omit IDs entirely;
            # positional pairing is the best heuristic in that case.
            needs_id_fix = any(not tc.get("id") for tc in tool_calls)
            id_remap: dict[int, str] = {}  # positional index → new ID
            if needs_id_fix:
                new_tcs = []
                empty_idx = 0
                for tc in tool_calls:
                    if not tc.get("id"):
                        new_id = f"call_{uuid.uuid4().hex}"
                        id_remap[empty_idx] = new_id
                        empty_idx += 1
                        new_tcs.append({**tc, "id": new_id})
                    else:
                        new_tcs.append(tc)
                msg = {**msg, "tool_calls": new_tcs}
                tool_calls = msg["tool_calls"]

            # Collect IDs from this assistant message
            tc_ids = [tc["id"] for tc in tool_calls if tc.get("id")]
            tc_id_set = set(tc_ids)

            out.append(msg)
            i += 1

            # Copy through existing tool messages, applying ID remap and
            # filtering out stale results that don't match any tool_call.
            local_answered: set[str] = set()
            empty_result_idx = 0
            while i < len(messages) and messages[i].get("role") == "tool":
                tool_msg = messages[i]
                result_tc_id = tool_msg.get("tool_call_id", "")
                if not result_tc_id and empty_result_idx in id_remap:
                    # Positional remap: empty result → matching new ID
                    new_id = id_remap[empty_result_idx]
                    tool_msg = {**tool_msg, "tool_call_id": new_id}
                    local_answered.add(new_id)
                    empty_result_idx += 1
                    out.append(tool_msg)
                elif not result_tc_id:
                    # Empty ID with no remap available — drop it
                    log.debug("sanitize_messages: dropping tool result with empty ID")
                    empty_result_idx += 1
                elif result_tc_id in tc_id_set:
                    local_answered.add(result_tc_id)
                    out.append(tool_msg)
                else:
                    log.debug(
                        "sanitize_messages: dropping stale tool result: %s",
                        result_tc_id,
                    )
                i += 1

            # Synthesize error results for tool_calls not answered in
            # THIS turn (not all of `out`, to avoid false matches from
            # reused IDs across turns).
            still_orphaned = [uid for uid in tc_ids if uid not in local_answered]
            if still_orphaned:
                log.debug(
                    "sanitize_messages: synthesizing %d tool result(s) for orphaned tool_calls",
                    len(still_orphaned),
                )
                for uid in still_orphaned:
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": uid,
                            "content": "Tool execution was cancelled.",
                        }
                    )
            continue

        # (3d) Drop orphaned tool results
        if role == "tool":
            tc_id = msg.get("tool_call_id", "")
            # Find the preceding assistant message's tool_call IDs
            prev_tc_ids: set[str] = set()
            for k in range(len(out) - 1, -1, -1):
                if out[k].get("role") == "assistant" and out[k].get("tool_calls"):
                    prev_tc_ids = {tc.get("id", "") for tc in out[k]["tool_calls"] if tc.get("id")}
                    break
            if prev_tc_ids and tc_id and tc_id not in prev_tc_ids:
                log.debug(
                    "sanitize_messages: dropping orphaned tool result (no matching tool_call): %s",
                    tc_id,
                )
                i += 1
                continue

        out.append(msg)
        i += 1
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
