"""Shared helpers for OpenAI-family providers (Chat Completions & Responses).

Capability table, temperature/reasoning gating, cache retention, citation
formatting, and message sanitisation live here so both
``OpenAIChatCompletionsProvider`` and ``OpenAIResponsesProvider`` stay DRY.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from turnstone.core.attachments import safe_attachment_label
from turnstone.core.lowering import CANCELLED_TOOL_RESULT
from turnstone.core.providers._protocol import (
    ModelCapabilities,
    UsageInfo,
    _lookup_capabilities,
    flat_effort_suppressed,
    resolve_reasoning_effort,
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
        supports_pdf=True,
        supports_reasoning_replay=True,
    ),
    "gpt-5-mini": ModelCapabilities(
        context_window=400000,
        max_output_tokens=128000,
        supports_temperature=False,
        reasoning_effort_values=("minimal", "low", "medium", "high"),
        default_reasoning_effort="medium",
        supports_vision=True,
        supports_pdf=True,
        supports_reasoning_replay=True,
    ),
    "gpt-5-nano": ModelCapabilities(
        context_window=400000,
        max_output_tokens=128000,
        supports_temperature=False,
        reasoning_effort_values=("minimal", "low", "medium", "high"),
        default_reasoning_effort="medium",
        supports_vision=True,
        supports_pdf=True,
        supports_reasoning_replay=True,
    ),
    # GPT-5 pro — high reasoning only, extended output
    "gpt-5-pro": ModelCapabilities(
        context_window=400000,
        max_output_tokens=272000,
        supports_temperature=False,
        reasoning_effort_values=("high",),
        default_reasoning_effort="high",
        supports_vision=True,
        supports_pdf=True,
        supports_reasoning_replay=True,
    ),
    # GPT-5.1 — temperature OK when reasoning_effort=none (default)
    "gpt-5.1": ModelCapabilities(
        context_window=400000,
        max_output_tokens=128000,
        reasoning_effort_values=("none", "low", "medium", "high"),
        default_reasoning_effort="none",
        supports_vision=True,
        supports_pdf=True,
        supports_reasoning_replay=True,
    ),
    # GPT-5.1 codex-max — the model xhigh was introduced on; without this
    # row it would prefix-match "gpt-5.1" (no xhigh) and the knob's xhigh
    # would wrongly cap at high.  Responses-only, supports explicit none.
    "gpt-5.1-codex-max": ModelCapabilities(
        context_window=400000,
        max_output_tokens=128000,
        reasoning_effort_values=("none", "low", "medium", "high", "xhigh"),
        default_reasoning_effort="medium",
        supports_vision=True,
        supports_pdf=True,
        supports_reasoning_replay=True,
    ),
    # GPT-5.2 — adds xhigh
    "gpt-5.2": ModelCapabilities(
        context_window=400000,
        max_output_tokens=128000,
        reasoning_effort_values=("none", "low", "medium", "high", "xhigh"),
        default_reasoning_effort="none",
        supports_vision=True,
        supports_pdf=True,
        supports_reasoning_replay=True,
    ),
    # GPT-5.2 pro — always-reasoning variant
    "gpt-5.2-pro": ModelCapabilities(
        context_window=400000,
        max_output_tokens=128000,
        supports_temperature=False,
        reasoning_effort_values=("medium", "high", "xhigh"),
        default_reasoning_effort="medium",
        supports_vision=True,
        supports_pdf=True,
        supports_reasoning_replay=True,
    ),
    # GPT-5.3 — same capabilities as 5.2 (matches gpt-5.3-chat-latest, codex)
    "gpt-5.3": ModelCapabilities(
        context_window=400000,
        max_output_tokens=128000,
        reasoning_effort_values=("none", "low", "medium", "high", "xhigh"),
        default_reasoning_effort="none",
        supports_vision=True,
        supports_pdf=True,
        supports_reasoning_replay=True,
    ),
    # GPT-5.4 — 1M context window, native tool search
    "gpt-5.4": ModelCapabilities(
        context_window=1050000,
        max_output_tokens=128000,
        reasoning_effort_values=("none", "low", "medium", "high", "xhigh"),
        default_reasoning_effort="none",
        supports_tool_search=True,
        supports_vision=True,
        supports_pdf=True,
        supports_reasoning_replay=True,
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
        supports_pdf=True,
        supports_reasoning_replay=True,
    ),
    # GPT-5.5 — 1M context, native tool search, stronger agentic/tool use.
    # Unlike 5.1-5.4 (default none), 5.5 defaults to MEDIUM reasoning per
    # developers.openai.com/api/docs/guides/latest-model (2026-07 check).
    "gpt-5.5": ModelCapabilities(
        context_window=1050000,
        max_output_tokens=128000,
        reasoning_effort_values=("none", "low", "medium", "high", "xhigh"),
        default_reasoning_effort="medium",
        supports_tool_search=True,
        supports_vision=True,
        supports_pdf=True,
        supports_reasoning_replay=True,
    ),
    # GPT-5.5 pro — always-reasoning, 1M context, native tool search
    "gpt-5.5-pro": ModelCapabilities(
        context_window=1050000,
        max_output_tokens=128000,
        supports_temperature=False,
        reasoning_effort_values=("medium", "high", "xhigh"),
        default_reasoning_effort="medium",
        supports_tool_search=True,
        supports_vision=True,
        supports_pdf=True,
        supports_reasoning_replay=True,
    ),
    # GPT-5.6 (Sol / Terra / Luna) — released 2026-07-09.  The bare
    # "gpt-5.6" alias routes to Sol, so this catch-all row also covers the
    # explicit "gpt-5.6-sol" id by longest-prefix match.  Every tier supports
    # the family reasoning ladder through "max", output verbosity, and
    # reasoning.mode="pro"; there is no separate gpt-5.6-pro model.  Default
    # effort is "medium" and temperature is accepted only when effort="none".
    "gpt-5.6": ModelCapabilities(
        context_window=1050000,
        max_output_tokens=128000,
        reasoning_effort_values=("none", "low", "medium", "high", "xhigh", "max"),
        default_reasoning_effort="medium",
        supports_tool_search=True,
        supports_vision=True,
        supports_pdf=True,
        supports_reasoning_replay=True,
        supports_verbosity=True,
        supports_pro_mode=True,
    ),
    # GPT-5.6 Terra — balanced intelligence/cost tier.
    "gpt-5.6-terra": ModelCapabilities(
        context_window=1050000,
        max_output_tokens=128000,
        reasoning_effort_values=("none", "low", "medium", "high", "xhigh", "max"),
        default_reasoning_effort="medium",
        supports_tool_search=True,
        supports_vision=True,
        supports_pdf=True,
        supports_reasoning_replay=True,
        supports_verbosity=True,
        supports_pro_mode=True,
    ),
    # GPT-5.6 Luna — cost-sensitive, high-volume tier.
    "gpt-5.6-luna": ModelCapabilities(
        context_window=1050000,
        max_output_tokens=128000,
        reasoning_effort_values=("none", "low", "medium", "high", "xhigh", "max"),
        default_reasoning_effort="medium",
        supports_tool_search=True,
        supports_vision=True,
        supports_pdf=True,
        supports_reasoning_replay=True,
        supports_verbosity=True,
        supports_pro_mode=True,
    ),
    # O-series reasoning models
    "o1": ModelCapabilities(
        context_window=200000,
        max_output_tokens=100000,
        supports_temperature=False,
        supports_streaming=False,
        reasoning_effort_values=("low", "medium", "high"),
        default_reasoning_effort="medium",
        supports_vision=True,
        supports_pdf=True,
        supports_reasoning_replay=True,
    ),
    "o1-mini": ModelCapabilities(
        context_window=128000,
        max_output_tokens=65536,
        supports_temperature=False,
        supports_streaming=False,
        supports_vision=True,
        supports_pdf=True,
        supports_reasoning_replay=True,
    ),
    # low/medium/high on every o-series model EXCEPT o1-mini (Azure
    # reasoning guide, 2026-06 revision) — without declared values the
    # session knob was silently dropped for these models.
    "o3": ModelCapabilities(
        context_window=200000,
        max_output_tokens=100000,
        supports_temperature=False,
        reasoning_effort_values=("low", "medium", "high"),
        default_reasoning_effort="medium",
        supports_vision=True,
        supports_pdf=True,
        supports_reasoning_replay=True,
    ),
    "o3-mini": ModelCapabilities(
        context_window=200000,
        max_output_tokens=100000,
        supports_temperature=False,
        reasoning_effort_values=("low", "medium", "high"),
        default_reasoning_effort="medium",
        supports_vision=True,
        supports_pdf=True,
        supports_reasoning_replay=True,
    ),
    "o3-pro": ModelCapabilities(
        context_window=200000,
        max_output_tokens=100000,
        supports_temperature=False,
        supports_streaming=False,
        reasoning_effort_values=("low", "medium", "high"),
        default_reasoning_effort="medium",
        supports_vision=True,
        supports_pdf=True,
        supports_reasoning_replay=True,
    ),
    "o4-mini": ModelCapabilities(
        context_window=200000,
        max_output_tokens=100000,
        supports_temperature=False,
        reasoning_effort_values=("low", "medium", "high"),
        default_reasoning_effort="medium",
        supports_vision=True,
        supports_pdf=True,
        supports_reasoning_replay=True,
    ),
    # Search models — always search on every request, no reasoning_effort
    "gpt-5-search-api": ModelCapabilities(
        context_window=400000,
        max_output_tokens=128000,
        supports_temperature=False,
        supports_web_search=True,
        reasoning_effort_values=(),
        supports_vision=True,
        supports_pdf=True,
    ),
    # Audio models — not chat/session models; used only as STT/TTS roles via
    # the /v1/audio/transcriptions and /v1/audio/speech endpoints. Prefixes
    # cover variants: "gpt-4o-transcribe" → "-diarize", "tts-1" → "tts-1-hd".
    "whisper-1": ModelCapabilities(
        supports_temperature=False,
        supports_streaming=False,
        supports_tools=False,
        supports_transcription=True,
    ),
    "gpt-4o-transcribe": ModelCapabilities(
        supports_temperature=False,
        supports_streaming=False,
        supports_tools=False,
        supports_transcription=True,
    ),
    "gpt-4o-mini-transcribe": ModelCapabilities(
        supports_temperature=False,
        supports_streaming=False,
        supports_tools=False,
        supports_transcription=True,
    ),
    "tts-1": ModelCapabilities(
        supports_temperature=False,
        supports_streaming=False,
        supports_tools=False,
        supports_speech_synthesis=True,
    ),
    "gpt-4o-mini-tts": ModelCapabilities(
        supports_temperature=False,
        supports_streaming=False,
        supports_tools=False,
        supports_speech_synthesis=True,
    ),
}

# Default for unknown models on the commercial lane.
OPENAI_DEFAULT = ModelCapabilities()

# The ``openai-compatible`` lane (either API surface) never consults the
# commercial table above: a local server serves whatever the operator
# named it (vLLM ``--served-model-name`` is a free string), so a prefix
# collision with a cloud model id ("gpt-5.5-my-finetune", "o3-distill")
# would inherit that model's sampling and effort contract, none of which
# holds for the box actually serving the name.  Every local model gets
# the plain dataclass defaults plus ``effort_passthrough`` — the session
# effort knob is forwarded verbatim on the flat ``reasoning_effort``
# param (the documented compat field; tolerant servers ignore it, vLLM
# maps it into the template where supported) unless the operator
# declares ``reasoning_effort_values`` (then the ordinal snap applies)
# or an ``effort_param`` (then the template channel claims the value
# and the flat param is suppressed).  Everything else is declared on
# the model definition (capabilities JSON + server_compat), mirroring
# ``_ANTHROPIC_COMPAT_DEFAULT`` and ``lookup_model_capabilities``'s
# "no static table for local models" contract.
OPENAI_COMPAT_DEFAULT = ModelCapabilities(effort_passthrough=True)


def lookup_openai_capabilities(model: str) -> ModelCapabilities:
    """Find capabilities for *model* by longest prefix match."""
    return _lookup_capabilities(model, OPENAI_CAPABILITIES, OPENAI_DEFAULT)


# ---------------------------------------------------------------------------
# Temperature and reasoning effort gating
# ---------------------------------------------------------------------------


def apply_temperature(
    kwargs: dict[str, Any],
    caps: ModelCapabilities,
    temperature: float | None,
    reasoning_effort: str | None,
) -> None:
    """Conditionally add temperature to *kwargs*.

    - ``None`` temperature is never written: the request omits the field
      so the SERVER default applies.  House rule: code never pins a
      temperature — a Python-level constant here would silently re-pin
      every lane that deliberately left it unresolved.
    - Models with ``supports_temperature=False`` (GPT-5 base, O-series)
      never receive temperature.
    - Models that list ``"none"`` in their effort values (GPT-5.1/5.2)
      only receive temperature when reasoning is EXPLICITLY off.  An
      unset effort (``None``/empty) leaves the server default in charge,
      which may have reasoning active (gpt-5.5/5.6 default medium) — so
      unset skips temperature too.
    """
    if temperature is None:
        return
    if not caps.supports_temperature:
        return
    if "none" in caps.reasoning_effort_values and reasoning_effort != "none":
        return  # Skip temperature unless reasoning is explicitly off
    kwargs["temperature"] = temperature


def apply_temperature_and_effort(
    kwargs: dict[str, Any],
    caps: ModelCapabilities,
    temperature: float | None,
    reasoning_effort: str | None,
) -> None:
    """Conditionally add temperature and reasoning_effort to *kwargs*.

    Chat Completions API version — reasoning effort is a flat parameter,
    suppressed when a declared ``caps.effort_param`` claims the
    chat-template channel instead (see ``flat_effort_suppressed`` for the
    rationale; the effort-ladder projection shares the same predicate).
    """
    apply_temperature(kwargs, caps, temperature, reasoning_effort)
    if flat_effort_suppressed(caps):
        return
    effort = resolve_reasoning_effort(caps, reasoning_effort)
    if effort:
        kwargs["reasoning_effort"] = effort


# ---------------------------------------------------------------------------
# Cache retention
# ---------------------------------------------------------------------------


def apply_cache_retention(kwargs: dict[str, Any], model: str) -> None:
    """Configure the prompt-cache lifetime supported by each GPT-5 generation.

    GPT-5.6 replaces the deprecated ``prompt_cache_retention`` field with
    ``prompt_cache_options.ttl``; 30 minutes is currently its only accepted
    minimum lifetime.  Earlier GPT-5 models retain the 24-hour policy.
    """
    if model.startswith("gpt-5.6"):
        kwargs["prompt_cache_options"] = {"ttl": "30m"}
    elif model.startswith("gpt-5"):
        kwargs["prompt_cache_retention"] = "24h"


# ---------------------------------------------------------------------------
# Output verbosity + reasoning mode (Responses API)
# ---------------------------------------------------------------------------

# Known-good enum values for the operator-declared ``verbosity`` /
# ``reasoning_mode`` capability fields.  These arrive from the
# model-definition capabilities JSON via
# ``ChatSession._resolve_capabilities`` — a field-name-filtered
# ``dataclasses.replace`` that does NOT validate values — so an operator
# typo would otherwise ride straight to the wire and 400 every request.
# The emission sites drop unknown values with a warning instead, mirroring
# how ``model_registry`` clamps out-of-range temperature / max_tokens.
VERBOSITY_LEVELS: frozenset[str] = frozenset({"low", "medium", "high"})
REASONING_MODES: frozenset[str] = frozenset({"standard", "pro"})


def apply_verbosity(kwargs: dict[str, Any], caps: ModelCapabilities) -> None:
    """Set Responses-API output verbosity when the operator declared one.

    ``verbosity`` (``"low"``/``"medium"``/``"high"``) is the GPT-5 family's
    output-length lever, distinct from reasoning effort (you can ask for a
    terse answer at high reasoning).  It is Responses-API-specific and nests
    under ``text.verbosity`` — a top-level ``verbosity`` field 400s there —
    so Turnstone emits it on this lane only.  ``supports_verbosity`` is the
    static capability; ``caps.verbosity`` is the operator-declared value
    (model-definition capabilities JSON), ``""`` = omit.  An unset value, or
    one on a model that doesn't support it, is silently omitted (matching
    ``apply_temperature``); a value outside ``VERBOSITY_LEVELS`` is dropped
    with a warning (an operator typo must not 400 every request).
    """
    if not caps.supports_verbosity or caps.verbosity == "":
        return
    if not isinstance(caps.verbosity, str):
        log.warning(
            "openai.responses: ignoring non-string verbosity",
            value=caps.verbosity,
            expected=sorted(VERBOSITY_LEVELS),
        )
        return
    if caps.verbosity not in VERBOSITY_LEVELS:
        log.warning(
            "openai.responses: ignoring unknown verbosity",
            value=caps.verbosity,
            expected=sorted(VERBOSITY_LEVELS),
        )
        return
    kwargs.setdefault("text", {})["verbosity"] = caps.verbosity


# ---------------------------------------------------------------------------
# Tool search (native deferred loading)
# ---------------------------------------------------------------------------


def resolve_server_side_tools(caps: ModelCapabilities) -> list[str]:
    """Return the effective server-side tool list for *caps*.

    Merges the explicit ``server_side_tools`` tuple with the legacy
    ``supports_web_search`` boolean so existing capability rows that
    only set the flag continue to inject ``{"type": "web_search"}``
    on the Responses surface without an explicit tuple entry.

    Returns a fresh list; callers are free to mutate it.
    """
    effective: list[str] = list(caps.server_side_tools)
    if caps.supports_web_search and "web_search" not in effective:
        effective.append("web_search")
    return effective


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


def inline_document_parts(parts: list[Any], *, skip_pdf_inline: bool = False) -> list[Any]:
    """Rewrite internal ``document`` content parts as text parts.

    OpenAI Chat Completions and the Google OpenAI-compat endpoint do not
    accept a native ``document`` block type, so we wrap the text payload
    in an escaped delimiter and emit it as a plain text part.  Other
    part types pass through unchanged.

    ``skip_pdf_inline`` leaves ``application/pdf`` document parts untouched so a
    downstream translator that *does* have a native PDF block (the Responses
    lane's :func:`~turnstone.core.providers._openai_responses.convert_content_parts`
    ``input_file``) can emit it.  Without it the PDF would be replaced by an
    unsupported-placeholder here, before the native translator ever runs —
    silently killing the native path.
    """
    out: list[Any] = []
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "document":
            d = part.get("document", {})
            if d.get("media_type") == "application/pdf":
                if skip_pdf_inline:
                    # A downstream lane emits the native PDF block; pass through.
                    out.append(part)
                    continue
                # This lane (OpenAI Chat / Google compat / local servers) has no
                # native PDF block and ``data`` is base64 — text-wrapping it would
                # emit garbage.  Surface a placeholder; the capability-gated
                # fallback (rasterize / text-extract) replaces it upstream.
                out.append(
                    {
                        "type": "text",
                        "text": (
                            "[PDF attachment "
                            f"'{safe_attachment_label(d.get('name'), default='document.pdf')}' "
                            "— not supported by this model]"
                        ),
                    }
                )
            else:
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


def _inline_documents_in_message(
    msg: dict[str, Any], *, skip_pdf_inline: bool = False
) -> dict[str, Any]:
    """Return ``msg`` with any list-type content's ``document`` parts inlined."""
    content = msg.get("content")
    if isinstance(content, list):
        return {
            **msg,
            "content": inline_document_parts(content, skip_pdf_inline=skip_pdf_inline),
        }
    return msg


def sanitize_messages(
    messages: list[dict[str, Any]],
    *,
    skip_pdf_inline: bool = False,
) -> list[dict[str, Any]]:
    """Sanitize messages for OpenAI-compatible APIs.

    Mostly format translation (the ``C`` layer): orphaned *real-id* tool_calls
    are synthesized upstream by
    :func:`turnstone.core.lowering.repair_wire_messages`, so by here every
    real-id tool_call already has a result.  This pass:

    1. Drops internal sibling keys (``_*``) and the neutral ``is_error`` flag
       (no equivalent on the OpenAI-compatible tool message).
    2. Ensures assistant messages always have ``content`` or ``tool_calls``
       (APIs reject messages with neither).
    3. Fills empty tool_call IDs with synthetic ``call_{uuid}`` values,
       positionally remaps the matching empty-ID results, and synthesizes
       cancellation results for back-filled calls left unanswered (local
       servers sometimes omit IDs entirely — these calls are id-less when the
       upstream repair runs, so this lane owns their orphan synthesis).
    4. Drops tool messages whose ``tool_call_id`` has no matching tool_call in
       the preceding assistant message (stale / orphaned results).

    Returns a new list; the original messages are not mutated.
    """

    # Drop internal sibling keys (``_provider_content``,
    # ``_attachments_meta``, etc.) that the OpenAI / Google-compat APIs
    # don't understand before they reach the wire.  ``is_error`` is the
    # neutral tool-result error flag — Anthropic renders it natively, but the
    # OpenAI-compatible tool message has no such field, so it is dropped here
    # (this is the ``C``-layer translation of the flag) rather than riding to
    # the wire as an unknown key.
    def _clean(m: dict[str, Any]) -> dict[str, Any]:
        cleaned = {k: v for k, v in m.items() if not (isinstance(k, str) and k.startswith("_"))}
        if cleaned.get("role") == "tool":
            cleaned.pop("is_error", None)
        return cleaned

    messages = [_clean(m) for m in messages]
    # Inline any internal ``document`` content parts — OpenAI Chat
    # Completions does not accept a native document block type.  ``skip_pdf_inline``
    # (set by the Responses lane) keeps ``application/pdf`` parts intact so its
    # native ``input_file`` translator downstream can emit them.
    messages = [_inline_documents_in_message(m, skip_pdf_inline=skip_pdf_inline) for m in messages]
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

        # (3) Assistant with tool_calls: fix empty IDs, copy through results
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
            # Orphans with a real id are synthesized upstream by
            # ``lowering.repair_wire_messages``; the back-filled empty-id calls
            # below are invisible to it (it can't pair an id-less call), so this
            # pass owns their orphan synthesis.
            answered_backfilled: set[str] = set()
            empty_result_idx = 0
            while i < len(messages) and messages[i].get("role") == "tool":
                tool_msg = messages[i]
                result_tc_id = tool_msg.get("tool_call_id", "")
                if not result_tc_id and empty_result_idx in id_remap:
                    # Positional remap: empty result → matching new ID
                    new_id = id_remap[empty_result_idx]
                    tool_msg = {**tool_msg, "tool_call_id": new_id}
                    answered_backfilled.add(new_id)
                    empty_result_idx += 1
                    out.append(tool_msg)
                elif not result_tc_id:
                    # Empty ID with no remap available — drop it
                    log.debug("sanitize_messages: dropping tool result with empty ID")
                    empty_result_idx += 1
                elif result_tc_id in tc_id_set:
                    out.append(tool_msg)
                else:
                    log.debug(
                        "sanitize_messages: dropping stale tool result: %s",
                        result_tc_id,
                    )
                i += 1

            # Synthesize cancellation results for back-filled (empty-id)
            # tool_calls left unanswered — the upstream repair could not see
            # them before sanitize assigned their ids.  Real-id orphans are
            # already handled upstream, so this is scoped to ``id_remap`` only.
            for uid in id_remap.values():
                if uid not in answered_backfilled:
                    out.append(
                        {"role": "tool", "tool_call_id": uid, "content": CANCELLED_TOOL_RESULT}
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
    cache_written = getattr(ptd, "cache_write_tokens", 0) if ptd is not None else 0

    return UsageInfo(
        prompt_tokens=pt,
        completion_tokens=ct,
        total_tokens=tt if isinstance(tt, int) else (pt + ct),
        cache_creation_tokens=cache_written if isinstance(cache_written, int) else 0,
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
