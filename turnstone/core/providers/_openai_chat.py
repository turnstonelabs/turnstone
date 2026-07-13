"""Chat Completions provider — for local model servers (vLLM, llama.cpp, SGLang).

Wraps the OpenAI Chat Completions API (``/v1/chat/completions``).
Commercial OpenAI models should use ``OpenAIResponsesProvider`` instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

import structlog

from turnstone.core.providers._openai_common import (
    OPENAI_COMPAT_DEFAULT,
    RETRYABLE_ERROR_NAMES,
    apply_temperature_and_effort,
    apply_tool_search,
    extract_usage,
    format_citations,
    sanitize_messages,
)
from turnstone.core.providers._protocol import (
    ModelCapabilities,
    StreamChunk,
    ToolCallDelta,
    _join_reasoning_with_cap,
    merge_reasoning_template_kwargs,
)
from turnstone.core.trajectory import materialize_attachments


def _reasoning_text(obj: Any) -> str:
    """The non-canonical reasoning text off a Chat-Completions message or
    streaming delta — ``reasoning`` (vLLM) preferred over
    ``reasoning_content`` (llama.cpp, other parsers), first non-empty
    STRING wins.

    The type guard matters twice over: a server that puts a structured
    object in ``reasoning`` must not shadow valid text sitting in
    ``reasoning_content``, and a non-``str`` must never leak into the
    session's reasoning accumulator (``"".join(...)`` downstream).  One
    helper for both the streaming and non-streaming paths so the two
    lanes cannot drift on precedence or guarding.
    """
    for attr in ("reasoning", "reasoning_content"):
        value = getattr(obj, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""


log = structlog.get_logger(__name__)


class OpenAIChatCompletionsProvider:
    """Provider for local OpenAI-compatible servers (vLLM, llama.cpp, SGLang).

    Uses the Chat Completions API (``/v1/chat/completions``).
    """

    @property
    def provider_name(self) -> str:
        return "openai-compatible"

    def get_capabilities(self, model: str) -> ModelCapabilities:
        # Operator-owned lane — never the commercial table (see
        # ``OPENAI_COMPAT_DEFAULT``).  GoogleProvider subclasses this
        # class and overrides with its own registry.
        return OPENAI_COMPAT_DEFAULT

    # -- message preparation --------------------------------------------------

    def _prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Prepare messages for the API request.

        Subclasses (e.g. GoogleProvider) override this to reconstruct
        provider-specific content from ``_provider_content`` before
        sending.  The base implementation just calls ``sanitize_messages``.
        """
        return sanitize_messages(messages)

    # -- web search ----------------------------------------------------------

    @staticmethod
    def _apply_web_search(
        kwargs: dict[str, Any],
        caps: ModelCapabilities,
        tools: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]] | None:
        """Inject ``web_search_options`` for search models.

        For models with ``supports_web_search``, the web search function tool
        is removed (the model searches automatically) and ``web_search_options``
        is added to the request kwargs.

        Returns the (possibly filtered) tools list.
        """
        if not caps.supports_web_search:
            return tools
        # Replace-only: native search stands in for the client ``web_search``
        # def. When the request never advertised one (persona visibility set,
        # coordinator toolset), injecting the option would hand the model a
        # capability its envelope hides.
        if not tools or not any(t.get("function", {}).get("name") == "web_search" for t in tools):
            return tools
        tools = [t for t in tools if t.get("function", {}).get("name") != "web_search"]
        if not tools:
            tools = None
        kwargs["web_search_options"] = {}
        return tools

    # -- thinking mode -------------------------------------------------------

    def _finalize_extra_body(
        self,
        extra_params: dict[str, Any] | None,
        caps: ModelCapabilities,
        reasoning_effort: str | None,
    ) -> dict[str, Any] | None:
        """Build the final ``extra_body``, injecting reasoning params if needed.

        ``merge_reasoning_template_kwargs`` maps the session effort knob onto
        the template's thinking toggle (``caps.thinking_param``, active when
        ``thinking_mode`` is manual/adaptive — effort ``"none"`` sends
        ``false``) and the graded effort key (``caps.effort_param``) inside
        ``chat_template_kwargs``.  Keys the operator already pinned via
        ``server_compat`` win; the caller's dict is never mutated.

        Returns ``None`` when the result would be empty (no extra_body
        needed).
        """
        return merge_reasoning_template_kwargs(caps, reasoning_effort, extra_params) or None

    # -- streaming -----------------------------------------------------------

    # Phase 2 of the reasoning-persistence feature plumbs an optional
    # ``replay_reasoning_to_model`` kwarg through every provider's
    # ``create_streaming``.  OpenAI Chat (and
    # the local-model server flavours that route through this adapter)
    # have no first-class reasoning shape on the wire, so the kwarg is
    # accepted for Protocol conformance and ignored here.
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
        messages = materialize_attachments(messages, resolve_attachments)
        caps = capabilities or self.get_capabilities(model)
        messages = self._prepare_messages(messages)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            caps.token_param: max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        apply_temperature_and_effort(kwargs, caps, temperature, reasoning_effort)
        tools = self._apply_web_search(kwargs, caps, tools)
        tools = apply_tool_search(caps, tools, deferred_names)
        if tools:
            kwargs["tools"] = tools
        extra_body = self._finalize_extra_body(extra_params, caps, reasoning_effort)
        if extra_body:
            kwargs["extra_body"] = extra_body
        if extra_headers:
            kwargs["extra_headers"] = extra_headers

        log.debug(
            "openai.chat.request",
            model=model,
            stream=True,
            max_tokens=max_tokens,
            message_count=len(messages),
            tool_count=len(tools) if tools else 0,
        )
        stream = client.chat.completions.create(**kwargs)
        if cancel_ref is not None:
            cancel_ref.append(stream)
        return self._iter_stream(stream)

    def _iter_stream(self, stream: Any) -> Iterator[StreamChunk]:
        """Convert OpenAI Chat Completions stream chunks to StreamChunks."""
        first = True
        annotations: list[Any] = []
        content_len = 0
        tool_call_count = 0
        last_finish_reason: str | None = None
        completion_tokens: int | None = None
        # Remap wire indexes onto logical slots: index-degenerate compat
        # servers (historical vLLM/llama.cpp builds) emit every parallel
        # tool call at index 0 — a delta whose id contradicts its index's
        # current call opens a new slot, mirroring the Anthropic iterator's
        # per-block index assignment.  Id-less argument fragments keep
        # following their index's current slot, so downstream accumulators
        # (drain_stream, the chat loop) can key by index safely.
        slot_for_index: dict[int, int] = {}
        slot_ids: dict[int, str] = {}
        next_slot = 0
        for chunk in stream:
            sc = StreamChunk()

            # Finish reason
            if chunk.choices and chunk.choices[0].finish_reason:
                sc.finish_reason = chunk.choices[0].finish_reason
                last_finish_reason = sc.finish_reason

            # Usage from final chunk
            if hasattr(chunk, "usage") and chunk.usage is not None:
                sc.usage = extract_usage(chunk.usage)
                if sc.usage:
                    completion_tokens = sc.usage.completion_tokens

            if not chunk.choices:
                if sc.usage:
                    yield sc
                continue

            delta = chunk.choices[0].delta

            # Reasoning field (vLLM --reasoning-parser, llama.cpp)
            rc = _reasoning_text(delta)
            if rc:
                sc.reasoning_delta = rc

            # Content
            if delta.content:
                sc.content_delta = delta.content
                content_len += len(delta.content)

            # Tool calls
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    wire_index = tc_delta.index
                    tc_id = tc_delta.id or ""
                    slot = slot_for_index.get(wire_index)
                    if slot is None or (
                        tc_id and slot_ids.get(slot, "") and slot_ids[slot] != tc_id
                    ):
                        slot = next_slot
                        next_slot += 1
                        slot_for_index[wire_index] = slot
                    if tc_id:
                        slot_ids[slot] = tc_id
                    tcd = ToolCallDelta(index=slot)
                    if tc_id:
                        tcd.id = tc_id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tcd.name = tc_delta.function.name
                        if tc_delta.function.arguments:
                            tcd.arguments_delta = tc_delta.function.arguments
                    sc.tool_call_deltas.append(tcd)
                    tool_call_count += 1

            # Accumulate url_citation annotations from search models
            delta_anns = getattr(delta, "annotations", None)
            if delta_anns:
                annotations.extend(delta_anns)

            has_content = sc.content_delta or sc.reasoning_delta or sc.tool_call_deltas
            if has_content and first:
                sc.is_first = True
                first = False

            if has_content or sc.finish_reason or sc.usage:
                yield sc

        log.debug(
            "openai.chat.response",
            stream=True,
            finish_reason=last_finish_reason,
            content_length=content_len,
            tool_call_deltas=tool_call_count,
            completion_tokens=completion_tokens,
        )

        # Emit accumulated citations as a final info chunk
        if annotations:
            citation_text = format_citations("", annotations).strip()
            if citation_text:
                yield StreamChunk(info_delta=citation_text)

    # -- tool conversion -----------------------------------------------------

    def convert_tools(
        self,
        tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return tools  # Already in OpenAI Chat Completions format

    # -- retryable errors ----------------------------------------------------

    @property
    def retryable_error_names(self) -> frozenset[str]:
        return RETRYABLE_ERROR_NAMES

    # -- reasoning extraction ------------------------------------------------

    def extract_reasoning_text(
        self,
        provider_blocks: list[dict[str, Any]] | None,
    ) -> str:
        """Walk synthetic ``reasoning_text`` blocks (Phase 3 path-3
        capture) and return the concatenated reasoning text.

        OpenAI Chat Completions has no native reasoning shape on the
        wire — vLLM ``--reasoning-parser``, llama.cpp
        ``reasoning_format``, and Gemini's OpenAI-compat endpoint all
        surface reasoning as non-canonical ``delta.reasoning_content``
        Pydantic extras.  ``model_turn.synth_reasoning_block``
        captures these into a single ``{type: "reasoning_text", text,
        source?}`` block when no native ``provider_blocks`` were
        emitted.  This extractor unwraps those for UI rehydration.
        """
        if not isinstance(provider_blocks, list):
            return ""
        parts: list[str] = []
        for block in provider_blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "reasoning_text":
                continue
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return _join_reasoning_with_cap(parts)
