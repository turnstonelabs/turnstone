"""LLM provider protocol — the contract every backend adapter must implement.

Defines normalized data types for streaming chunks, completion results, and
token usage so that ``ChatSession`` can work with any LLM backend without
knowing provider-specific details.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class ModelCapabilities:
    """Describes what a specific model supports — used by providers to
    adjust API parameters (temperature, token param name, thinking mode, etc.).
    """

    context_window: int = 200000
    max_output_tokens: int = 64000
    supports_temperature: bool = True
    supports_streaming: bool = True
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
    # snap to ``default_reasoning_effort``); with no declared values the
    # knob is forwarded as-is and the template is the authority on
    # validity.  See ``reasoning_template_kwargs``.
    effort_param: str = ""
    supports_effort: bool = False
    effort_levels: tuple[str, ...] = ()
    reasoning_effort_values: tuple[str, ...] = ()
    default_reasoning_effort: str = "medium"
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


def reasoning_template_kwargs(
    caps: ModelCapabilities,
    reasoning_effort: str,
) -> dict[str, Any]:
    """``chat_template_kwargs`` entries carrying reasoning control.

    On local model servers the reasoning levers live in the chat template:
    a boolean toggle (``caps.thinking_param``) and an optional graded
    effort key (``caps.effort_param``, gpt-oss-style templates).  The
    session effort knob drives both, mirroring the native Anthropic
    contracts: ``"manual"`` maps ``"none"``/empty to an explicit
    ``false`` (``_reasoning_params`` parity — the knob is the switch),
    while ``"adaptive"`` always sends ``true`` (the model self-regulates;
    the native adaptive branch never lets the knob force-disable
    thinking).  The effort value is validated via
    ``resolve_reasoning_effort`` when the model declares
    ``reasoning_effort_values`` (off-list knob values snap to
    ``default_reasoning_effort``); with no declared values the knob is
    forwarded as-is and the template is the authority on validity.
    """
    updates: dict[str, Any] = {}
    effort_on = bool(reasoning_effort) and reasoning_effort != "none"
    if caps.thinking_mode == "manual":
        updates[caps.thinking_param] = effort_on
    elif caps.thinking_mode == "adaptive":
        updates[caps.thinking_param] = True
    if caps.effort_param and effort_on:
        effort = (
            resolve_reasoning_effort(caps, reasoning_effort)
            if caps.reasoning_effort_values
            else reasoning_effort
        )
        if effort:
            updates[caps.effort_param] = effort
    return updates


def merge_reasoning_template_kwargs(
    caps: ModelCapabilities,
    reasoning_effort: str,
    extra_params: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Merge ``reasoning_template_kwargs`` into a copy of the extra params.

    Returns a new top-level dict whenever *extra_params* is non-empty or
    an injection applies (``None`` stays ``None`` when there is nothing
    to inject) — callers may attach the result to a request without
    aliasing the session's dict.  Existing keys win — an operator
    ``server_compat`` pin beats the knob mapping.  The caller's dict
    (and its ``chat_template_kwargs`` sub-dict) is never mutated.
    """
    updates = reasoning_template_kwargs(caps, reasoning_effort)
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
        temperature: float = 0.5,
        reasoning_effort: str = "medium",
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

        ``replay_reasoning_to_model`` defaults to ``True`` here (and on
        every concrete provider's ``create_streaming`` /
        ``create_completion``) for back-compat with direct callers that
        haven't been updated to thread the resolver — eval scripts,
        ad-hoc tests, third-party harnesses.  This is INTENTIONALLY
        the opposite of the operator-side default
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

    def create_completion(
        self,
        *,
        client: Any,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.5,
        reasoning_effort: str = "medium",
        extra_params: dict[str, Any] | None = None,
        deferred_names: frozenset[str] | None = None,
        capabilities: ModelCapabilities | None = None,
        replay_reasoning_to_model: bool = True,
        extra_headers: dict[str, str] | None = None,
        resolve_attachments: Callable[[list[str]], dict[str, Any]] | None = None,
    ) -> CompletionResult:
        """Create a non-streaming request, returning a normalized result.

        ``replay_reasoning_to_model`` mirrors the per-model
        ``model_definitions`` operator flag.  Anthropic uses it to
        gate the verbatim ``_provider_content`` replay (Phase 2);
        other providers accept the kwarg for Protocol conformance and
        ignore it (chat-template ``<think>`` content isn't part of
        their wire-side replay path).
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
          ``ChatSession._maybe_synth_reasoning_block`` for vLLM /
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
