"""LLM provider protocol — the contract every backend adapter must implement.

Defines normalized data types for streaming chunks, completion results, and
token usage so that ``ChatSession`` can work with any LLM backend without
knowing provider-specific details.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


@dataclass
class ToolCallDelta:
    """Incremental tool call update within a streaming chunk."""

    index: int
    id: str = ""
    name: str = ""
    arguments_delta: str = ""


@dataclass
class UsageInfo:
    """Normalized token usage."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    # Prompt caching metrics (provider-specific; 0 when not available)
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass
class StreamChunk:
    """Normalized streaming chunk, provider-agnostic."""

    content_delta: str = ""
    reasoning_delta: str = ""
    tool_call_deltas: list[ToolCallDelta] = field(default_factory=list)
    usage: UsageInfo | None = None
    finish_reason: str | None = None
    is_first: bool = False
    info_delta: str = ""
    provider_blocks: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CompletionResult:
    """Normalized non-streaming completion result."""

    content: str
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str = "stop"
    usage: UsageInfo | None = None
    provider_blocks: list[dict[str, Any]] = field(default_factory=list)
    # Non-canonical reasoning text surfaced by Chat-Completions-lane servers
    # (vLLM ``--reasoning-parser``, llama.cpp ``reasoning_format``) — the
    # non-streaming twin of ``StreamChunk.reasoning_delta``.  Lanes whose
    # reasoning rides ``provider_blocks`` natively (Anthropic ``thinking``,
    # OpenAI Responses ``reasoning`` items) leave it empty.
    reasoning: str = ""


class IncompleteStreamError(RuntimeError):
    """The stream ended without any terminal/finish signal.

    Every adapter emits a finish reason on a healthy stream (Chat
    Completions' final choice chunk, Anthropic's ``message_delta`` stop
    reason, Responses' terminal event); a stream that exhausts without
    one is a generation that died mid-response behind a proxy/ASGI layer
    that closed the body cleanly.  Typed and listed in every provider's
    ``retryable_error_names`` so callers re-run it like the wire errors
    it stands in for — restoring the retired non-streaming transport's
    complete-or-error contract for single-shot lanes (the interactive
    loop keeps showing partial output live; this gate is drain-only).
    """


def merge_usage(acc: UsageInfo | None, new: UsageInfo) -> UsageInfo:
    """Merge one stream-chunk usage report into an accumulator, per-field max.

    Anthropic splits a request's usage across events (``message_start``
    carries prompt tokens with completion 0; ``message_delta`` carries
    completion tokens and may omit prompt tokens), so neither first-wins
    nor last-wins sees both — the max-merge does.  ``total_tokens`` is
    recomputed from the merged parts.  Returns a fresh ``UsageInfo`` and
    never mutates ``new`` (the provider's object).

    ``ChatSession``'s inline chunk consumer implements the same rule over
    its dict-shaped accumulator; it adopts this helper when the main loop
    moves onto ``model_turn`` (#832).
    """
    if acc is None:
        return replace(new)
    prompt = max(acc.prompt_tokens, new.prompt_tokens)
    completion = max(acc.completion_tokens, new.completion_tokens)
    return UsageInfo(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cache_creation_tokens=max(acc.cache_creation_tokens, new.cache_creation_tokens),
        cache_read_tokens=max(acc.cache_read_tokens, new.cache_read_tokens),
    )


def accumulate_tool_call_delta(
    acc: dict[int, dict[str, Any]], tcd: ToolCallDelta
) -> dict[str, Any]:
    """Fold one ``ToolCallDelta`` into a per-index tool-call accumulator.

    THE tool-call merge rule, in one place: ``id``/``name`` are whole
    values (last truthy wins — servers may re-send them on every
    fragment), ``arguments_delta`` concatenates.  Returns the (possibly
    fresh) accumulator entry so callers can hang provider extras off it.

    Serves :func:`drain_stream`, ``GoogleProvider``'s raw-fidelity
    capture, and ``ChatSession``'s inline chunk consumer — every
    accumulator in the tree, so the chat loop and the drained lanes
    cannot assemble different calls from the same wire stream.
    """
    tc = acc.setdefault(
        tcd.index,
        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
    )
    if tcd.id:
        tc["id"] = tcd.id
    if tcd.name:
        tc["function"]["name"] = tcd.name
    if tcd.arguments_delta:
        tc["function"]["arguments"] += tcd.arguments_delta
    return tc


def drain_stream(chunks: Iterator[StreamChunk]) -> CompletionResult:
    """Drain a ``create_streaming`` iterator into a ``CompletionResult``.

    The ONE non-streaming transport: single-shot callers (``model_turn``)
    sample through the provider's streaming entry and accumulate here, so
    the streaming and non-streaming lanes cannot drift apart per adapter.
    Accumulation mirrors the main loop's chunk consumer (``ChatSession``),
    plus the complete-or-error gate the interactive loop doesn't need:

    - A stream that exhausts with NO finish reason raises
      :class:`IncompleteStreamError` (retryable) — every adapter emits one
      on a healthy stream (a server that genuinely never sends a terminal
      signal needs ``finish_reason_optional`` declared in its model
      capabilities; the adapter then shims ``"stop"`` once output
      arrived), so its absence means the generation died mid-response.  Partial text must
      never be handed to a caller that stores it as a complete result (a
      compaction summary, a title).  A transport blip AFTER the finish
      reason keeps the completed result and forfeits only trailing
      metadata.
    - ``usage`` merges via :func:`merge_usage` — Anthropic splits prompt
      and completion tokens across separate events.
    - Tool calls accumulate by ``ToolCallDelta.index`` via
      :func:`accumulate_tool_call_delta`.  Adapters own index sanity —
      the chat iterator remaps index-degenerate wire deltas onto
      distinct slots before they reach any accumulator (this one or the
      chat loop's).
    - ``provider_blocks`` replaces on each non-empty emission — every
      adapter attaches its full block list exactly once, on or after the
      terminal chunk.
    - ``info_delta`` before the finish reason is transient status (server-
      side search pings) that the non-streaming lane never surfaced —
      dropped.  ``info_delta`` after the finish reason is the citations
      footer (``format_citations("", annotations).strip()``); folding it
      back as ``content + "\\n\\n" + info`` byte-matches the non-streaming
      lane's ``format_citations(content, annotations)`` append.

    Raises whatever the underlying stream raises — retry/deadline/fallback
    policy stays with the caller, exactly as with the old non-streaming
    transport — EXCEPT httpx transport failures: streaming moves the body
    read out of the SDK's ``APIConnectionError``-wrapped request into raw
    iteration, so a mid-body connection drop or read timeout surfaces as a
    bare ``httpx.TransportError`` no retry predicate recognizes.  Those
    are re-raised (chained) as :class:`IncompleteStreamError`, restoring
    the wire-blip retryability the non-streaming transport had.
    """
    import httpx  # noqa: PLC0415 — heavyweight; deferred off the type-module import path

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    trailing_info_parts: list[str] = []
    tool_calls_acc: dict[int, dict[str, Any]] = {}
    usage: UsageInfo | None = None
    finish_reason: str | None = None
    provider_blocks: list[dict[str, Any]] = []

    iterator = iter(chunks)
    while True:
        try:
            sc = next(iterator)
        except StopIteration:
            break
        except httpx.TransportError as exc:
            if finish_reason is not None:
                # The generation already completed (finish reason in hand);
                # the blip only cost trailing metadata — a usage-only chunk
                # or the citation footer.  Keep the complete result rather
                # than discarding it for a retry that re-pays the tokens —
                # but say so: on the chat lane the usage chunk trails the
                # finish reason, so this result may report usage=None and
                # the call's spend goes missing from usage accounting.
                import structlog  # noqa: PLC0415 — deferred with httpx off the type-module path

                structlog.get_logger(__name__).warning(
                    "drain_stream.post_finish_blip",
                    error_type=type(exc).__name__,
                    usage_captured=usage is not None,
                )
                break
            raise IncompleteStreamError(
                f"stream transport failed mid-response ({type(exc).__name__}: {exc})"
            ) from exc
        if sc.content_delta:
            content_parts.append(sc.content_delta)
        if sc.reasoning_delta:
            reasoning_parts.append(sc.reasoning_delta)
        for tcd in sc.tool_call_deltas:
            accumulate_tool_call_delta(tool_calls_acc, tcd)
        if sc.usage is not None:
            usage = merge_usage(usage, sc.usage)
        if sc.finish_reason:
            finish_reason = sc.finish_reason
        if sc.provider_blocks:
            provider_blocks = sc.provider_blocks
        # Pre-finish info is transient status — intentionally dropped;
        # only the trailing (post-finish) citations footer folds back.
        if sc.info_delta and finish_reason is not None:
            trailing_info_parts.append(sc.info_delta)

    if finish_reason is None:
        raise IncompleteStreamError(
            "stream ended without a finish reason — generation died mid-response "
            "(a server that never sends finish reasons needs finish_reason_optional "
            "declared in its model capabilities)"
        )

    content = "".join(content_parts)
    for info in trailing_info_parts:
        content += "\n\n" + info

    tool_calls = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
    return CompletionResult(
        content=content,
        tool_calls=tool_calls or None,
        finish_reason=finish_reason,
        usage=usage,
        provider_blocks=provider_blocks,
        reasoning="".join(reasoning_parts),
    )


@dataclass(frozen=True)
class ModelCapabilities:
    """Describes what a specific model supports — used by providers to
    adjust API parameters (temperature, token param name, thinking mode, etc.).
    """

    context_window: int = 200000
    max_output_tokens: int = 64000
    supports_temperature: bool = True
    supports_tools: bool = True
    token_param: str = "max_completion_tokens"
    thinking_mode: str = "none"  # "none" | "manual" | "adaptive"
    # For local-server lanes (openai-compatible, anthropic-compatible):
    # the chat_template_kwargs key that toggles thinking (e.g.
    # "enable_thinking" for Gemma/Qwen, "thinking" for Granite/DeepSeek).
    # Ignored when thinking_mode is "none" or by providers that handle
    # thinking natively (real Anthropic).
    thinking_param: str = "enable_thinking"
    # For local-server lanes (openai-compatible, anthropic-compatible):
    # the chat_template_kwargs key that carries a graded reasoning-effort
    # value, for templates that have one (e.g. "reasoning_effort" for
    # gpt-oss-style templates).  Empty = the template has no effort lever,
    # send nothing.  The session knob value is validated against
    # ``reasoning_effort_values`` when that is non-empty (off-list values
    # round UP onto the declared list, capped at its ceiling — see
    # ``snap_reasoning_effort``); with no declared values the knob is
    # forwarded as-is and the template is the authority on validity.
    # See ``reasoning_template_kwargs``.
    effort_param: str = ""
    supports_effort: bool = False
    effort_levels: tuple[str, ...] = ()
    reasoning_effort_values: tuple[str, ...] = ()
    # The model definition's own default effort — the in-code rung of the
    # assignment scheme (alias > stored config > this > omit).  Empty =
    # the definition declares no default: the effort param is omitted and
    # the serving side's own default rules.  Commercial rows declare
    # their documented defaults explicitly; local lanes stay silent so an
    # unconfigured box keeps its template/server behavior.
    default_reasoning_effort: str = ""
    # Local-lane contract: with NO declared ``reasoning_effort_values``
    # the session knob is forwarded VERBATIM instead of omitted — the
    # user's effort setting always reaches the wire, and the serving
    # box is the authority on what it means.  Commercial rows leave
    # this False: there, an empty values list means the model has no
    # effort control at all (legacy o1-mini-era models) and the param must be omitted.
    effort_passthrough: bool = False
    supports_web_search: bool = False
    supports_tool_search: bool = False
    supports_vision: bool = False
    # Chat-input modalities carried as user-turn attachments — distinct from the
    # STT/TTS *roles* below.  ``supports_pdf``: native PDF document ingest;
    # ``supports_audio_input``: native audio ingest (OpenAI ``input_audio`` /
    # vLLM omni).  When False the wire-build path falls back client-side (PDF →
    # rasterize/extract, audio → STT transcription) — see core/attachments.py.
    # ``supports_audio_input`` is orthogonal to ``supports_transcription``: an
    # omni model has the former and lacks the latter (it has no /audio endpoint).
    supports_pdf: bool = False
    supports_audio_input: bool = False
    # Audio I/O roles (STT / TTS) — not chat behavior; consumed by the audio
    # endpoints and the Models -> Roles capability gate (turnstone/core/audio.py).
    supports_transcription: bool = False
    supports_speech_synthesis: bool = False
    # Server-side tool types to auto-inject into Responses-API ``tools[]``
    # for this model (e.g. ``("web_search",)`` for OpenAI search models,
    # ``("web_search", "x_search")`` for Grok variants).  The
    # OpenAI-facing flag ``supports_web_search`` is implicitly merged in
    # by ``resolve_server_side_tools`` so legacy capability rows
    # continue to work without an explicit entry here.
    server_side_tools: tuple[str, ...] = ()
    thinking_display: str = ""  # "summarized" for models that omit thinking by default
    # Phase 3 reasoning-persistence: gate the per-model
    # ``replay_reasoning_to_model`` flag.  When False, the wire-build
    # path skips replay regardless of the operator flag (defends
    # against operators flipping the flag on a model whose API has
    # no reasoning-replay shape — e.g. OpenAI Chat Completions, where
    # reasoning is purely server-side and never round-trips).  Set
    # True for: Anthropic models with ``thinking_mode != "none"``,
    # OpenAI Responses o-series + GPT-5+ (``include=
    # ["reasoning.encrypted_content"]`` round-trip).  Path-3 capture
    # (Chat Completions / vLLM / llama.cpp / Gemini-compat) is
    # persist-only and doesn't gate on this flag.
    supports_reasoning_replay: bool = False
    # Mid-conversation system messages: append ``{"role": "system"}`` to the
    # ``messages`` array (rather than editing the top-level ``system`` field) to
    # add operator-level instructions partway through a session without
    # invalidating the cached prefix.  When False, ``system``-role messages must
    # be hoisted into the top-level ``system`` param (the universal fallback).
    # Available on the Claude API only (NOT Bedrock / Vertex / Foundry), on
    # claude-opus-4-8 (validated header-less) and claude-fable-5 (same
    # documented wire surface); no beta header required.
    supports_mid_conversation_system: bool = False
    # Phase 3 reranker calibration — populated by calibrate-on-detect; read by
    # ChatSession._bm25_rerank_threshold. A non-empty rerank_scale is the
    # "has been calibrated" marker.
    rerank_threshold: float = 0.0
    rerank_scale: str = ""
    rerank_separated: bool = False
    # Responses-API output-length control (GPT-5 family): "low"/"medium"/
    # "high", separate from reasoning effort.  Appended rather than inserted
    # above to preserve the public dataclass constructor's positional order.
    # ``supports_verbosity`` is the static capability; ``verbosity`` is the
    # operator-declared value (model-definition capabilities JSON, merged via
    # ``ChatSession._resolve_capabilities``), "" = omit.  Nests under
    # ``text.verbosity`` on the Responses wire.
    supports_verbosity: bool = False
    verbosity: str = ""
    # Responses-API ``reasoning.mode`` for GPT-5.6.  ``supports_pro_mode`` is
    # the static capability; ``reasoning_mode`` is the operator-declared value,
    # "" = omit (standard reasoning).  There is no gpt-5.6-pro model.
    supports_pro_mode: bool = False
    reasoning_mode: str = ""
    # Lax-server tolerance (operator-declared, model-definition
    # capabilities JSON): this server never sends a terminal signal, so a
    # stream that ends CLEANLY after delivering output (content, reasoning,
    # or tool calls) is a completed generation — the adapter shims a
    # ``"stop"`` finish and :func:`drain_stream`'s complete-or-error gate
    # passes.  Leave False (the default) for every server that reliably
    # terminates its streams: there, a clean signal-less end IS a
    # died-mid-generation stream (worker crashed behind a clean-closing
    # proxy/ASGI layer) and blessing it would store partial text as a
    # complete result.  SSE has no body framing, so the two cases are one
    # wire shape — this flag is the operator asserting which server class
    # they run.  Honored on every drained lane: Chat Completions (no
    # ``finish_reason`` ever arrived), Anthropic (no ``message_delta``
    # stop_reason AND no ``message_stop``), Responses (no terminal
    # ``response.completed``/``response.incomplete`` event).
    finish_reason_optional: bool = False


# The session effort knob is ORDINAL — snapping must respect this order.
# "none" is the disable position and is never a snap target (snapping a
# high knob onto "none" would invert the request into "don't think").
KNOB_EFFORT_ORDER: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_KNOB_RANK: dict[str, int] = {value: rank for rank, value in enumerate(KNOB_EFFORT_ORDER)}


def snap_reasoning_effort(reasoning_effort: str, declared: tuple[str, ...]) -> str | None:
    """Round an off-list knob value up onto the declared effort levels.

    Returns the smallest declared level ranking >= the knob; when the
    knob is above every declared level, the highest declared level (the
    ceiling — asking for more effort than exists must not fall to a
    lower tier).  Declared values outside the knob vocabulary cannot be
    ranked and are reachable only by exact match in the caller; "none"
    is never a snap target.  Returns ``None`` when the knob itself is
    unrankable or nothing declared is rankable.
    """
    rank = _KNOB_RANK.get(reasoning_effort)
    if rank is None or reasoning_effort == "none":
        return None
    rankable = [(r, v) for v in declared if (r := _KNOB_RANK.get(v)) is not None and v != "none"]
    if not rankable:
        return None
    at_or_above = [(r, v) for r, v in rankable if r >= rank]
    return min(at_or_above)[1] if at_or_above else max(rankable)[1]


def resolve_reasoning_effort(caps: ModelCapabilities, reasoning_effort: str | None) -> str | None:
    """Return the validated reasoning effort value, or ``None`` to omit.

    ``None``/empty input means no rung of the assignment scheme resolved
    a value: the param is omitted and the serving side's default rules.

    Declared values match verbatim; off-list knob values round UP onto
    the declared list, capped at its ceiling (``snap_reasoning_effort``).
    ``default_reasoning_effort`` is the last resort for values the
    ordinal snap cannot rank (custom strings on either side).

    The knob's ``"none"`` position means "no reasoning": it is forwarded
    verbatim ONLY when the model declares an explicit ``"none"`` level
    (gpt-5.1+, grok-4.3) — omitting the param there would leave the
    server default (possibly reasoning ON, e.g. gpt-5.5's ``medium``) in
    charge of a knob that promises off.  Models without a declared
    ``"none"`` get the param omitted, and ``"none"`` is never a snap
    target for other knob positions.

    With no declared values, ``caps.effort_passthrough`` (local lanes)
    forwards the knob verbatim — the user's effort setting always
    reaches the wire and the serving box decides what it means; without
    the flag an empty list means "no effort control" and the param is
    omitted (commercial rows).
    """
    if not reasoning_effort:
        return None
    if not caps.reasoning_effort_values:
        if caps.effort_passthrough and reasoning_effort != "none":
            return reasoning_effort
        return None
    if reasoning_effort == "none":
        return "none" if "none" in caps.reasoning_effort_values else None
    if reasoning_effort in caps.reasoning_effort_values:
        return reasoning_effort
    snapped = snap_reasoning_effort(reasoning_effort, caps.reasoning_effort_values)
    if snapped:
        return snapped
    if caps.default_reasoning_effort and caps.default_reasoning_effort != "none":
        return caps.default_reasoning_effort
    return None


def flat_effort_suppressed(caps: ModelCapabilities) -> bool:
    """True when the flat ``reasoning_effort`` param must NOT be sent.

    A set ``caps.effort_param`` declares the chat-template channel
    (``chat_template_kwargs`` via ``merge_reasoning_template_kwargs``) as
    the one this server consumes for graded effort, so the flat param is
    suppressed: sending both would 400 on servers that reject unknown
    top-level fields and can disagree with an operator ``server_compat``
    pin on tolerant ones.  Single source of the rule — the request path
    (``apply_temperature_and_effort``) and the effort-ladder projection
    must apply the same predicate or the UI annotates behavior the wire
    doesn't have.
    """
    return bool(caps.effort_param)


# Default chat-template key for the graded effort value on lanes whose
# ONLY effort channel is ``chat_template_kwargs`` (anthropic-compatible —
# vLLM's /v1/messages schema has no flat ``reasoning_effort`` param).
# Injected when the operator engaged template reasoning control
# (thinking_mode manual/adaptive) without naming an effort key: the
# user's effort setting must reach the wire regardless, a template that
# doesn't reference the kwarg ignores it, and ``caps.effort_param``
# overrides the name for templates that grade under something else
# (e.g. "reasoning").
EFFORT_TEMPLATE_FALLBACK_PARAM = "reasoning_effort"


def reasoning_template_kwargs(
    caps: ModelCapabilities,
    reasoning_effort: str | None,
    *,
    fallback_effort_param: str = "",
) -> dict[str, Any]:
    """``chat_template_kwargs`` entries carrying reasoning control.

    On local model servers the reasoning levers live in the chat template:
    a boolean toggle (``caps.thinking_param``) and a graded effort key
    (``caps.effort_param``, else *fallback_effort_param* on lanes with no
    flat effort channel).  The effort knob drives both, mirroring the
    native Anthropic contracts: ``"manual"`` maps a concrete level to an
    explicit ``true`` and the explicit ``"none"`` knob to an explicit
    ``false`` (``_reasoning_params`` parity — the knob is the switch),
    while an UNSET knob (``None``/empty: no rung of the assignment scheme
    resolved a value) injects nothing — the template's own default rules.
    ``"adaptive"`` always sends ``true`` (the model self-regulates; the
    native adaptive branch never lets the knob force-disable thinking).
    The effort value is validated via ``resolve_reasoning_effort`` when
    the model declares ``reasoning_effort_values`` (off-list knob values
    round up onto the declared list, capped at its ceiling); with no
    declared values the knob is forwarded as-is and the template is the
    authority on validity.
    """
    updates: dict[str, Any] = {}
    explicit_off = reasoning_effort == "none"
    effort_on = bool(reasoning_effort) and not explicit_off
    if caps.thinking_mode == "manual":
        if effort_on or explicit_off:
            updates[caps.thinking_param] = effort_on
    elif caps.thinking_mode == "adaptive":
        updates[caps.thinking_param] = True
    # A declared effort_param is an operator opt-in at any thinking_mode
    # (gpt-oss-style boxes grade without a toggle).  The FALLBACK key
    # rides only when template reasoning control is engaged — a
    # thinking_mode="none" box keeps its inject-nothing contract.
    effort_param = caps.effort_param or (
        fallback_effort_param if caps.thinking_mode in ("manual", "adaptive") else ""
    )
    if effort_param and effort_on:
        effort = (
            resolve_reasoning_effort(caps, reasoning_effort)
            if caps.reasoning_effort_values
            else reasoning_effort
        )
        if effort:
            updates[effort_param] = effort
    return updates


def merge_reasoning_template_kwargs(
    caps: ModelCapabilities,
    reasoning_effort: str | None,
    extra_params: dict[str, Any] | None,
    *,
    fallback_effort_param: str = "",
) -> dict[str, Any] | None:
    """Merge ``reasoning_template_kwargs`` into a copy of the extra params.

    Returns a new top-level dict whenever *extra_params* is non-empty or
    an injection applies (``None`` stays ``None`` when there is nothing
    to inject) — callers may attach the result to a request without
    aliasing the session's dict.  Existing keys win — an operator
    ``server_compat`` pin beats the knob mapping.  The caller's dict
    (and its ``chat_template_kwargs`` sub-dict) is never mutated.
    """
    updates = reasoning_template_kwargs(
        caps, reasoning_effort, fallback_effort_param=fallback_effort_param
    )
    if not updates:
        return dict(extra_params) if extra_params else extra_params
    merged = dict(extra_params) if extra_params else {}
    raw_ctk = merged.get("chat_template_kwargs")
    ctk = dict(raw_ctk) if isinstance(raw_ctk, dict) else {}
    for key, value in updates.items():
        ctk.setdefault(key, value)
    merged["chat_template_kwargs"] = ctk
    return merged


# Operator-friendly UI cap on reasoning text returned from
# ``LLMProvider.extract_reasoning_text``.  Single source of truth so a
# tuning change propagates to every provider's display path uniformly.
# Larger reasoning bodies are still stored verbatim in
# ``provider_data``; only the rehydrated UI display payload is
# truncated.
#
# Named ``_CHARS`` (not ``_BYTES``) because the cap is enforced via
# Python ``str`` slicing, which counts code points.  Reasoning text
# that happens to contain 4-byte UTF-8 glyphs (CJK, emoji) will
# serialise to a larger UTF-8 payload than the constant suggests —
# fine for the UI display path (browsers handle the encoded length),
# but worth knowing if this is ever wired to a byte-quota system.
MAX_REASONING_DISPLAY_CHARS = 64 * 1024


def _join_reasoning_with_cap(parts: list[str]) -> str:
    """Join collected reasoning text parts with newline; truncate at the
    operator-friendly UI cap.

    Shared tail of every provider's ``extract_reasoning_text`` —
    Anthropic walks ``thinking`` blocks, OpenAI Responses walks
    ``reasoning`` items' ``summary`` + ``content``, OpenAI Chat walks
    synthetic ``reasoning_text`` blocks.  All three converge on the
    same emit pattern: collect strings, drop empties, join with
    newline, cap at :data:`MAX_REASONING_DISPLAY_CHARS`.
    """
    if not parts:
        return ""
    joined = "\n".join(parts)
    if len(joined) > MAX_REASONING_DISPLAY_CHARS:
        return joined[:MAX_REASONING_DISPLAY_CHARS]
    return joined


def _lookup_capabilities(
    model: str,
    table: dict[str, ModelCapabilities],
    default: ModelCapabilities,
) -> ModelCapabilities:
    """Find capabilities by longest prefix match."""
    best_match = ""
    for prefix in table:
        if (model == prefix or model.startswith(prefix + "-")) and len(prefix) > len(best_match):
            best_match = prefix
    return table[best_match] if best_match else default


@runtime_checkable
class LLMProvider(Protocol):
    """Protocol that every LLM backend adapter must implement.

    Translates between turnstone's internal OpenAI-like message format
    and the provider's native API.
    """

    @property
    def provider_name(self) -> str:
        """Return provider identifier (``"openai"``, ``"anthropic"``, etc.)."""
        ...

    def get_capabilities(self, model: str) -> ModelCapabilities:
        """Return capabilities for the given model ID."""
        ...

    def create_streaming(
        self,
        *,
        client: Any,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        extra_params: dict[str, Any] | None = None,
        deferred_names: frozenset[str] | None = None,
        cancel_ref: list[Any] | None = None,
        capabilities: ModelCapabilities | None = None,
        replay_reasoning_to_model: bool = True,
        extra_headers: dict[str, str] | None = None,
        resolve_attachments: Callable[[list[str]], dict[str, Any]] | None = None,
    ) -> Iterator[StreamChunk]:
        """Create a streaming request, yielding normalized StreamChunks.

        If *capabilities* is provided the provider uses it instead of
        calling ``get_capabilities(model)`` internally.  This lets the
        session pass config-merged capabilities so that overrides from
        the model registry (e.g. ``thinking_mode``, ``token_param``)
        are respected.

        If *cancel_ref* is provided the provider appends the underlying SDK
        stream object (which has a ``.close()`` method) before yielding the
        first chunk.  The caller can then close it from another thread to
        abort a blocked HTTP read immediately.

        This is the ONLY transport — single-shot callers drain it through
        :func:`drain_stream` instead of a separate non-streaming entry
        (retired on #831), so per-adapter request shaping cannot drift
        between the two consumption styles.

        ``temperature=None`` (the default) means the field is OMITTED from
        the wire request and the server's own default applies — it must
        never be replaced by a Python-level constant (house rule: code
        never pins a temperature; ``model_turn`` resolves the operator's
        ladder and passes ``None`` when nothing is configured).

        ``replay_reasoning_to_model`` defaults to ``True`` here (and on
        every concrete provider's ``create_streaming``) for back-compat
        with direct callers that haven't been updated to thread the
        resolver — eval scripts, ad-hoc tests, third-party harnesses.
        This is INTENTIONALLY the opposite of the operator-side default
        (``ModelConfig.replay_reasoning_to_model = False``,
        ``model_definitions`` server_default ``0``); the resolver in
        ``ChatSession`` reads the operator value and passes it
        explicitly, so production call sites never rely on the
        kwarg-omitted path.  Provider-internal helpers (e.g.
        ``OpenAIResponsesProvider._convert_messages``) default ``False``
        because they're called BY the public entry points — once the
        resolver-driven value lands, it's already explicit.
        """
        ...

    def convert_tools(
        self,
        tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Convert internal tool schemas (OpenAI format) to provider format."""
        ...

    @property
    def retryable_error_names(self) -> frozenset[str]:
        """Exception class names that should trigger retry."""
        ...

    def extract_reasoning_text(
        self,
        provider_blocks: list[dict[str, Any]] | None,
    ) -> str:
        """Return concatenated reasoning text from stored ``provider_blocks``.

        Each provider walks the block types it owns:

        * ``AnthropicProvider`` — ``thinking`` blocks (concatenated
          ``thinking`` text).
        * ``OpenAIResponsesProvider`` — ``reasoning`` items
          (concatenated ``summary`` + ``content`` text).
        * ``OpenAIChatCompletionsProvider`` — synthetic
          ``reasoning_text`` blocks stamped by
          ``model_turn.synth_reasoning_block`` for vLLM /
          llama.cpp / Gemini-OpenAI-compat reasoning capture.
        * ``GoogleProvider`` — inherits the OpenAI Chat extractor
          (Gemini's ``/v1beta/openai/`` reasoning surfaces as
          synthetic ``reasoning_text`` blocks too).

        All providers return the joined text capped at
        :data:`MAX_REASONING_DISPLAY_CHARS` for UI rendering; full
        bytes remain in ``provider_data`` for replay.  Returns ``""``
        when the input list contains no recognised reasoning-bearing
        blocks for the implementing provider.
        """
        ...
