"""OpenAI-compatible provider — wraps current behavior with zero semantic change.

Handles OpenAI, vLLM, llama.cpp, and any server that speaks the
OpenAI Chat Completions API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

from turnstone.core.providers._protocol import (
    CompletionResult,
    ModelCapabilities,
    StreamChunk,
    ToolCallDelta,
    UsageInfo,
    _lookup_capabilities,
)

# -- model capabilities -------------------------------------------------------

_OPENAI_CAPABILITIES: dict[str, ModelCapabilities] = {
    # GPT-4o family
    "gpt-4o": ModelCapabilities(
        context_window=128000,
        max_output_tokens=16384,
    ),
    "gpt-4o-mini": ModelCapabilities(
        context_window=128000,
        max_output_tokens=16384,
    ),
    # GPT-5 base — NO temperature support
    "gpt-5": ModelCapabilities(
        context_window=400000,
        max_output_tokens=128000,
        supports_temperature=False,
        reasoning_effort_values=("minimal", "low", "medium", "high"),
        default_reasoning_effort="medium",
    ),
    "gpt-5-mini": ModelCapabilities(
        context_window=400000,
        max_output_tokens=128000,
        supports_temperature=False,
        reasoning_effort_values=("minimal", "low", "medium", "high"),
        default_reasoning_effort="medium",
    ),
    "gpt-5-nano": ModelCapabilities(
        context_window=400000,
        max_output_tokens=128000,
        supports_temperature=False,
        reasoning_effort_values=("minimal", "low", "medium", "high"),
        default_reasoning_effort="medium",
    ),
    # GPT-5.1 — temperature OK when reasoning_effort=none (default)
    "gpt-5.1": ModelCapabilities(
        context_window=400000,
        max_output_tokens=128000,
        reasoning_effort_values=("none", "low", "medium", "high"),
        default_reasoning_effort="none",
    ),
    # GPT-5.2 — adds xhigh
    "gpt-5.2": ModelCapabilities(
        context_window=400000,
        max_output_tokens=128000,
        reasoning_effort_values=("none", "low", "medium", "high", "xhigh"),
        default_reasoning_effort="none",
    ),
    # O-series reasoning models
    "o1": ModelCapabilities(
        context_window=200000,
        max_output_tokens=100000,
        supports_temperature=False,
        supports_streaming=False,
    ),
    "o1-mini": ModelCapabilities(
        context_window=128000,
        max_output_tokens=65536,
        supports_temperature=False,
        supports_streaming=False,
    ),
    "o3": ModelCapabilities(
        context_window=200000,
        max_output_tokens=100000,
        supports_temperature=False,
    ),
    "o3-mini": ModelCapabilities(
        context_window=200000,
        max_output_tokens=100000,
        supports_temperature=False,
    ),
    "o3-pro": ModelCapabilities(
        context_window=200000,
        max_output_tokens=100000,
        supports_temperature=False,
        supports_streaming=False,
    ),
    "o4-mini": ModelCapabilities(
        context_window=200000,
        max_output_tokens=100000,
        supports_temperature=False,
    ),
}

# Default for unknown models (local servers: vLLM, llama.cpp, etc.)
_OPENAI_DEFAULT = ModelCapabilities()


class OpenAIProvider:
    """Provider for OpenAI-compatible APIs (OpenAI, vLLM, llama.cpp, etc.)."""

    @property
    def provider_name(self) -> str:
        return "openai"

    def get_capabilities(self, model: str) -> ModelCapabilities:
        return _lookup_capabilities(model, _OPENAI_CAPABILITIES, _OPENAI_DEFAULT)

    # -- shared param logic --------------------------------------------------

    def _apply_model_params(
        self,
        kwargs: dict[str, Any],
        caps: ModelCapabilities,
        temperature: float,
        reasoning_effort: str,
    ) -> None:
        """Conditionally add temperature and reasoning_effort to *kwargs*.

        - Models with ``supports_temperature=False`` (GPT-5 base, O-series)
          never receive temperature.
        - Models that list ``"none"`` in their effort values (GPT-5.1/5.2)
          only receive temperature when reasoning is inactive.
        - ``reasoning_effort`` is forwarded as a first-class API parameter
          only for models that declare supported effort values.
        """
        if caps.supports_temperature:
            # GPT-5.1/5.2: temperature only valid when reasoning_effort is "none"
            if "none" in caps.reasoning_effort_values and reasoning_effort not in (
                "none",
                "",
            ):
                pass  # Skip temperature when reasoning is active
            else:
                kwargs["temperature"] = temperature
        if caps.reasoning_effort_values and reasoning_effort and reasoning_effort != "none":
            kwargs["reasoning_effort"] = reasoning_effort

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
    ) -> Iterator[StreamChunk]:
        caps = self.get_capabilities(model)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            caps.token_param: max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        self._apply_model_params(kwargs, caps, temperature, reasoning_effort)
        if tools:
            kwargs["tools"] = tools
        if extra_params:
            kwargs["extra_body"] = extra_params

        stream = client.chat.completions.create(**kwargs)
        yield from self._iter_stream(stream)

    def _iter_stream(self, stream: Any) -> Iterator[StreamChunk]:
        """Convert OpenAI stream chunks to normalized StreamChunks."""
        first = True
        for chunk in stream:
            sc = StreamChunk()

            # Finish reason
            if chunk.choices and chunk.choices[0].finish_reason:
                sc.finish_reason = chunk.choices[0].finish_reason

            # Usage from final chunk
            if hasattr(chunk, "usage") and chunk.usage is not None:
                u = chunk.usage
                pt = getattr(u, "prompt_tokens", None)
                ct = getattr(u, "completion_tokens", None)
                tt = getattr(u, "total_tokens", None)
                if pt is not None and ct is not None:
                    sc.usage = UsageInfo(
                        prompt_tokens=pt,
                        completion_tokens=ct,
                        total_tokens=tt or (pt + ct),
                    )

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

            has_content = sc.content_delta or sc.reasoning_delta or sc.tool_call_deltas
            if has_content and first:
                sc.is_first = True
                first = False

            if has_content or sc.finish_reason or sc.usage:
                yield sc

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
    ) -> CompletionResult:
        caps = self.get_capabilities(model)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            caps.token_param: max_tokens,
            "stream": False,
        }
        self._apply_model_params(kwargs, caps, temperature, reasoning_effort)
        if tools:
            kwargs["tools"] = tools
        if extra_params:
            kwargs["extra_body"] = extra_params

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

        usage = None
        if hasattr(response, "usage") and response.usage:
            u = response.usage
            usage = UsageInfo(
                prompt_tokens=u.prompt_tokens,
                completion_tokens=u.completion_tokens,
                total_tokens=getattr(u, "total_tokens", None)
                or (u.prompt_tokens + u.completion_tokens),
            )

        return CompletionResult(
            content=msg.content or "",
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
        )

    # -- tool conversion -----------------------------------------------------

    def convert_tools(
        self,
        tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return tools  # Already in OpenAI format

    # -- retryable errors ----------------------------------------------------

    @property
    def retryable_error_names(self) -> frozenset[str]:
        return frozenset(
            {
                "APIError",
                "APIConnectionError",
                "RateLimitError",
                "Timeout",
                "APITimeoutError",
            }
        )
