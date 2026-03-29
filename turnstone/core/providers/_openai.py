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
            # Validate against supported values; fall back to model default
            if reasoning_effort in caps.reasoning_effort_values:
                kwargs["reasoning_effort"] = reasoning_effort
            elif caps.default_reasoning_effort and caps.default_reasoning_effort != "none":
                kwargs["reasoning_effort"] = caps.default_reasoning_effort

    # -- web search ----------------------------------------------------------

    def _apply_web_search(
        self,
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
        # Remove web_search function tool — model has built-in search
        if tools:
            tools = [t for t in tools if t.get("function", {}).get("name") != "web_search"]
            if not tools:
                tools = None
        kwargs["web_search_options"] = {}
        return tools

    # -- prompt cache retention -----------------------------------------------

    @staticmethod
    def _apply_cache_retention(kwargs: dict[str, Any], model: str) -> None:
        """Enable 24-hour extended prompt cache retention for GPT-5.x models.

        OpenAI caching is automatic (no code changes for basic caching), but
        the default TTL is only 5-10 minutes.  Extended retention keeps cached
        KV tensors for up to 24 hours at no additional cost, which is valuable
        for workstreams with bursty activity patterns.
        """
        # GPT-5, GPT-5.1, GPT-5.2, GPT-5.3, GPT-5.4 and variants
        if model.startswith("gpt-5"):
            kwargs["prompt_cache_retention"] = "24h"

    # -- tool search ---------------------------------------------------------

    def _apply_tool_search(
        self,
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

    # -- message sanitisation ------------------------------------------------

    @staticmethod
    def _sanitize_messages(
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
        messages = self._sanitize_messages(messages)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            caps.token_param: max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        self._apply_model_params(kwargs, caps, temperature, reasoning_effort)
        self._apply_cache_retention(kwargs, model)
        tools = self._apply_web_search(kwargs, caps, tools)
        tools = self._apply_tool_search(caps, tools, deferred_names)
        if tools:
            kwargs["tools"] = tools
        if extra_params:
            kwargs["extra_body"] = extra_params

        stream = client.chat.completions.create(**kwargs)
        if cancel_ref is not None:
            cancel_ref.append(stream)
        return self._iter_stream(stream)

    def _iter_stream(self, stream: Any) -> Iterator[StreamChunk]:
        """Convert OpenAI stream chunks to normalized StreamChunks."""
        first = True
        annotations: list[Any] = []
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
                    # Extract cached_tokens from prompt_tokens_details.
                    # OpenAI caching is automatic with no write premium, so
                    # cache_creation_tokens is always 0 (only Anthropic reports it).
                    ptd = getattr(u, "prompt_tokens_details", None)
                    cached = getattr(ptd, "cached_tokens", 0) if ptd else 0
                    sc.usage = UsageInfo(
                        prompt_tokens=pt,
                        completion_tokens=ct,
                        total_tokens=tt or (pt + ct),
                        cache_read_tokens=cached or 0,
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

        # Emit accumulated citations as a final info chunk
        if annotations:
            citation_text = self._format_citations("", annotations).strip()
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
        messages = self._sanitize_messages(messages)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            caps.token_param: max_tokens,
            "stream": False,
        }
        self._apply_model_params(kwargs, caps, temperature, reasoning_effort)
        self._apply_cache_retention(kwargs, model)
        tools = self._apply_web_search(kwargs, caps, tools)
        tools = self._apply_tool_search(caps, tools, deferred_names)
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

        # Extract url_citation annotations from web search models
        content = msg.content or ""
        annotations = getattr(msg, "annotations", None)
        if annotations:
            content = self._format_citations(content, annotations)

        usage = None
        if hasattr(response, "usage") and response.usage:
            u = response.usage
            ptd = getattr(u, "prompt_tokens_details", None)
            cached = getattr(ptd, "cached_tokens", 0) if ptd else 0
            usage = UsageInfo(
                prompt_tokens=u.prompt_tokens,
                completion_tokens=u.completion_tokens,
                total_tokens=getattr(u, "total_tokens", None)
                or (u.prompt_tokens + u.completion_tokens),
                cache_read_tokens=cached or 0,
            )

        return CompletionResult(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
        )

    @staticmethod
    def _format_citations(content: str, annotations: list[Any]) -> str:
        """Append url_citation sources as footnotes at the end of the content."""
        seen_urls: set[str] = set()
        sources: list[str] = []
        for ann in annotations:
            ann_type = getattr(ann, "type", None)
            if ann_type == "url_citation":
                citation = getattr(ann, "url_citation", None)
                if citation:
                    title = getattr(citation, "title", "")
                    url = getattr(citation, "url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        sources.append(f"[{title}]({url})" if title else url)
        if sources:
            content += "\n\nSources:\n" + "\n".join(f"- {s}" for s in sources)
        return content

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
