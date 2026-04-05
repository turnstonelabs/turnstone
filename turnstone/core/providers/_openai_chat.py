"""Chat Completions provider — for local model servers (vLLM, llama.cpp, SGLang).

Wraps the OpenAI Chat Completions API (``/v1/chat/completions``).
Commercial OpenAI models should use ``OpenAIResponsesProvider`` instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

import structlog

from turnstone.core.providers._openai_common import (
    RETRYABLE_ERROR_NAMES,
    apply_cache_retention,
    apply_temperature_and_effort,
    apply_tool_search,
    extract_usage,
    format_citations,
    lookup_openai_capabilities,
    sanitize_messages,
)
from turnstone.core.providers._protocol import (
    CompletionResult,
    ModelCapabilities,
    StreamChunk,
    ToolCallDelta,
)

log = structlog.get_logger(__name__)


class OpenAIChatCompletionsProvider:
    """Provider for local OpenAI-compatible servers (vLLM, llama.cpp, SGLang).

    Uses the Chat Completions API (``/v1/chat/completions``).
    """

    @property
    def provider_name(self) -> str:
        return "openai-compatible"

    def get_capabilities(self, model: str) -> ModelCapabilities:
        return lookup_openai_capabilities(model)

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
        if tools:
            tools = [t for t in tools if t.get("function", {}).get("name") != "web_search"]
            if not tools:
                tools = None
        kwargs["web_search_options"] = {}
        return tools

    # -- streaming -----------------------------------------------------------

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
    ) -> Iterator[StreamChunk]:
        caps = self.get_capabilities(model)
        messages = sanitize_messages(messages)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            caps.token_param: max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        apply_temperature_and_effort(kwargs, caps, temperature, reasoning_effort)
        apply_cache_retention(kwargs, model)
        tools = self._apply_web_search(kwargs, caps, tools)
        tools = apply_tool_search(caps, tools, deferred_names)
        if tools:
            kwargs["tools"] = tools
        if extra_params:
            kwargs["extra_body"] = extra_params

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
            rc = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)
            if rc:
                sc.reasoning_delta = rc

            # Content
            if delta.content:
                sc.content_delta = delta.content
                content_len += len(delta.content)

            # Tool calls
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    tcd = ToolCallDelta(index=tc_delta.index)
                    if tc_delta.id:
                        tcd.id = tc_delta.id
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

    # -- non-streaming -------------------------------------------------------

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
    ) -> CompletionResult:
        caps = self.get_capabilities(model)
        messages = sanitize_messages(messages)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            caps.token_param: max_tokens,
            "stream": False,
        }
        apply_temperature_and_effort(kwargs, caps, temperature, reasoning_effort)
        apply_cache_retention(kwargs, model)
        tools = self._apply_web_search(kwargs, caps, tools)
        tools = apply_tool_search(caps, tools, deferred_names)
        if tools:
            kwargs["tools"] = tools
        if extra_params:
            kwargs["extra_body"] = extra_params

        log.debug(
            "openai.chat.request",
            model=model,
            stream=False,
            max_tokens=max_tokens,
            message_count=len(messages),
            tool_count=len(tools) if tools else 0,
        )
        response = client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        msg = choice.message

        tool_calls = None
        if msg.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]

        # Extract url_citation annotations from web search models
        content = msg.content or ""
        annotations = getattr(msg, "annotations", None)
        if annotations:
            content = format_citations(content, annotations)

        usage = extract_usage(getattr(response, "usage", None))

        result = CompletionResult(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
        )
        log.debug(
            "openai.chat.response",
            stream=False,
            finish_reason=result.finish_reason,
            content_length=len(content),
            tool_call_count=len(tool_calls) if tool_calls else 0,
            completion_tokens=usage.completion_tokens if usage else None,
        )
        return result

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
