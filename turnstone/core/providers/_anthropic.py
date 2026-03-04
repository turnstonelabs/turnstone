"""Anthropic provider — native streaming, message translation.

Implements the ``LLMProvider`` protocol for Anthropic's Messages API.
The ``anthropic`` SDK is imported lazily so it remains an optional dependency.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from turnstone.core.providers._protocol import (
    CompletionResult,
    ModelCapabilities,
    StreamChunk,
    ToolCallDelta,
    UsageInfo,
    _lookup_capabilities,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


def _ensure_anthropic() -> Any:
    """Lazy import anthropic SDK, raising helpful error if not installed."""
    try:
        import anthropic  # noqa: PLC0415

        return anthropic
    except ImportError:
        raise ImportError(
            "The 'anthropic' package is required for Anthropic provider. "
            "Install it with: pip install 'turnstone[anthropic]'"
        ) from None


# -- message format helpers --------------------------------------------------


def _to_blocks(content: Any) -> list[dict[str, Any]]:
    """Normalize content to a list of Anthropic content blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return list(content)
    return [{"type": "text", "text": str(content)}]


def _merge_consecutive(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge consecutive messages with the same role (Anthropic requirement)."""
    if not messages:
        return []
    merged: list[dict[str, Any]] = [dict(messages[0])]
    for msg in messages[1:]:
        if msg["role"] == merged[-1]["role"]:
            prev = merged[-1]
            prev["content"] = _to_blocks(prev["content"]) + _to_blocks(msg["content"])
        else:
            merged.append(msg)
    return merged


# Tool version for Anthropic's server-side web search (update when new version ships)
_WEB_SEARCH_TOOL_TYPE = "web_search_20250305"

# -- model capabilities -------------------------------------------------------

_ANTHROPIC_DEFAULT = ModelCapabilities(
    context_window=200000,
    max_output_tokens=64000,
    token_param="max_tokens",
    thinking_mode="manual",
    supports_web_search=True,
)

_ANTHROPIC_CAPABILITIES: dict[str, ModelCapabilities] = {
    "claude-opus-4-6": ModelCapabilities(
        context_window=200000,
        max_output_tokens=128000,
        token_param="max_tokens",
        thinking_mode="adaptive",
        supports_effort=True,
        effort_levels=("low", "medium", "high", "max"),
        supports_web_search=True,
    ),
    "claude-sonnet-4-6": ModelCapabilities(
        context_window=200000,
        max_output_tokens=64000,
        token_param="max_tokens",
        thinking_mode="adaptive",
        supports_effort=True,
        effort_levels=("low", "medium", "high"),
        supports_web_search=True,
    ),
    "claude-haiku-4-5": ModelCapabilities(
        context_window=200000,
        max_output_tokens=64000,
        token_param="max_tokens",
        thinking_mode="manual",
        supports_web_search=True,
    ),
    "claude-sonnet-4-5": ModelCapabilities(
        context_window=200000,
        max_output_tokens=64000,
        token_param="max_tokens",
        thinking_mode="manual",
        supports_web_search=True,
    ),
    "claude-opus-4-5": ModelCapabilities(
        context_window=200000,
        max_output_tokens=64000,
        token_param="max_tokens",
        thinking_mode="manual",
        supports_effort=True,
        effort_levels=("low", "medium", "high"),
        supports_web_search=True,
    ),
    "claude-opus-4": ModelCapabilities(
        context_window=200000,
        max_output_tokens=32000,
        token_param="max_tokens",
        thinking_mode="manual",
        supports_web_search=True,
    ),
    "claude-sonnet-4": ModelCapabilities(
        context_window=200000,
        max_output_tokens=64000,
        token_param="max_tokens",
        thinking_mode="manual",
        supports_web_search=True,
    ),
}


def _map_reasoning_to_effort(
    reasoning_effort: str,
    valid_levels: tuple[str, ...],
) -> str | None:
    """Map turnstone reasoning_effort to Anthropic effort parameter."""
    mapping = {"low": "low", "medium": "medium", "high": "high", "max": "max"}
    effort = mapping.get(reasoning_effort)
    if effort and effort in valid_levels:
        return effort
    return None


# -- provider ----------------------------------------------------------------


class AnthropicProvider:
    """Provider for Anthropic's Messages API with native streaming."""

    @property
    def provider_name(self) -> str:
        return "anthropic"

    def get_capabilities(self, model: str) -> ModelCapabilities:
        return _lookup_capabilities(model, _ANTHROPIC_CAPABILITIES, _ANTHROPIC_DEFAULT)

    # -- web search tool injection -------------------------------------------

    def _inject_web_search(
        self,
        tools: list[dict[str, Any]],
        caps: ModelCapabilities,
    ) -> list[dict[str, Any]]:
        """Replace ``web_search`` function tool with native server-side tool.

        If the tools list contains a ``web_search`` function tool and the model
        supports native web search, replace it with Anthropic's
        ``web_search_20250305`` server-side tool.  The server executes the search
        autonomously — no client-side tool execution loop needed.
        """
        if not caps.supports_web_search:
            return tools
        has_web_search = any(t.get("name") == "web_search" for t in tools)
        if not has_web_search:
            return tools
        # Remove the function-based web_search and add the native tool
        filtered = [t for t in tools if t.get("name") != "web_search"]
        filtered.append({"type": _WEB_SEARCH_TOOL_TYPE, "name": "web_search"})
        return filtered

    # -- shared param logic --------------------------------------------------

    def _build_thinking_and_kwargs(
        self,
        caps: ModelCapabilities,
        reasoning_effort: str,
        extra_params: dict[str, Any] | None,
        max_tokens: int,
        temperature: float,
        converted_msgs: list[dict[str, Any]],
        system_prompt: str,
        model: str,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Build the full kwargs dict with thinking mode and effort params."""
        thinking_params: dict[str, Any] = {}
        if caps.thinking_mode == "adaptive":
            thinking_params = {"thinking": {"type": "adaptive"}}
            temperature = 1.0  # Required with thinking
        elif caps.thinking_mode == "manual":
            thinking_params = self._reasoning_params(reasoning_effort, extra_params, max_tokens)
            if thinking_params:
                temperature = 1.0

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": converted_msgs,
            caps.token_param: max_tokens,
            "temperature": temperature,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if tools:
            anthropic_tools = self.convert_tools(tools)
            anthropic_tools = self._inject_web_search(anthropic_tools, caps)
            kwargs["tools"] = anthropic_tools
        kwargs.update(thinking_params)

        # Effort param for models that support it (Opus 4.6, Sonnet 4.6, Opus 4.5)
        if caps.supports_effort and reasoning_effort:
            effort = _map_reasoning_to_effort(reasoning_effort, caps.effort_levels)
            if effort:
                kwargs["output_config"] = {"effort": effort}

        return kwargs

    # -- message conversion --------------------------------------------------

    def _convert_messages(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        """Convert internal (OpenAI-like) messages to Anthropic format.

        Returns ``(system_prompt, converted_messages)``.
        """
        system_parts: list[str] = []
        converted: list[dict[str, Any]] = []

        i = 0
        while i < len(messages):
            msg = messages[i]
            role = msg["role"]

            if role in ("system", "developer"):
                if msg.get("content"):
                    system_parts.append(msg["content"])
                i += 1
                continue

            if role == "assistant":
                # If raw provider content was preserved, pass it through verbatim
                # so encrypted_content/encrypted_index from web search are retained
                provider_content = msg.get("_provider_content")
                if provider_content:
                    converted.append({"role": "assistant", "content": provider_content})
                    i += 1
                    continue

                content_blocks: list[dict[str, Any]] = []
                text = msg.get("content")
                if text:
                    content_blocks.append({"type": "text", "text": text})
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    args_str = fn.get("arguments", "{}")
                    try:
                        args_obj = json.loads(args_str)
                    except (json.JSONDecodeError, TypeError):
                        args_obj = {}
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": fn.get("name", ""),
                            "input": args_obj,
                        }
                    )
                if content_blocks:
                    converted.append({"role": "assistant", "content": content_blocks})
                i += 1
                continue

            if role == "tool":
                # Anthropic: tool results are content blocks in a user message
                tool_results: list[dict[str, Any]] = []
                while i < len(messages) and messages[i]["role"] == "tool":
                    tool_msg = messages[i]
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_msg.get("tool_call_id", ""),
                            "content": tool_msg.get("content", ""),
                        }
                    )
                    i += 1
                converted.append({"role": "user", "content": tool_results})
                continue

            if role == "user":
                converted.append({"role": "user", "content": msg.get("content", "")})
                i += 1
                continue

            # Unknown role — pass through as user
            converted.append({"role": "user", "content": str(msg.get("content", ""))})
            i += 1

        return "\n\n".join(system_parts), _merge_consecutive(converted)

    # -- tool conversion -----------------------------------------------------

    def convert_tools(
        self,
        tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Convert OpenAI function-calling schema to Anthropic tool format."""
        result = []
        for tool in tools:
            fn = tool.get("function", {})
            result.append(
                {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object"}),
                }
            )
        return result

    # -- reasoning params ----------------------------------------------------

    def _reasoning_params(
        self,
        reasoning_effort: str,
        extra_params: dict[str, Any] | None,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        """Map reasoning_effort to Anthropic thinking parameters.

        Ensures ``budget_tokens < max_tokens`` so there's room for the
        actual response.  Anthropic requires ``temperature=1`` when
        thinking is enabled — callers must enforce this.
        """
        if not reasoning_effort or reasoning_effort in ("none", ""):
            return {}
        budget_map = {"low": 1024, "medium": 4096, "high": 16384}
        budget = budget_map.get(reasoning_effort, 4096)
        if extra_params and "thinking_budget_tokens" in extra_params:
            budget = extra_params["thinking_budget_tokens"]
        if budget > 0:
            # Budget must leave room for the response
            if budget >= max_tokens:
                budget = max(1024, max_tokens - 1024)
            return {"thinking": {"type": "enabled", "budget_tokens": budget}}
        return {}

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
        _ensure_anthropic()
        caps = self.get_capabilities(model)
        system_prompt, converted_msgs = self._convert_messages(messages)
        kwargs = self._build_thinking_and_kwargs(
            caps,
            reasoning_effort,
            extra_params,
            max_tokens,
            temperature,
            converted_msgs,
            system_prompt,
            model,
            tools,
        )

        with client.messages.stream(**kwargs) as stream:
            yield from self._iter_anthropic_stream(stream)

    def _iter_anthropic_stream(self, stream: Any) -> Iterator[StreamChunk]:
        """Convert Anthropic streaming events to normalized StreamChunks."""
        first = True
        # Map content block index → tool call index for our accumulator
        tool_block_to_index: dict[int, int] = {}
        next_tool_index = 0
        # Track server-side tool blocks (web search) — accumulate query input
        server_tool_blocks: dict[int, dict[str, str]] = {}
        # Capture raw content blocks for multi-turn preservation
        raw_blocks: dict[int, dict[str, Any]] = {}

        for event in stream:
            sc = StreamChunk()
            event_type = event.type

            if event_type == "content_block_start":
                block = event.content_block
                raw_blocks[event.index] = _block_to_dict(block)
                if block.type == "tool_use":
                    idx = next_tool_index
                    tool_block_to_index[event.index] = idx
                    next_tool_index += 1
                    sc.tool_call_deltas.append(
                        ToolCallDelta(index=idx, id=block.id, name=block.name)
                    )
                elif block.type == "server_tool_use":
                    # Server-side tool (web search) — track for query accumulation
                    server_tool_blocks[event.index] = {
                        "name": getattr(block, "name", ""),
                        "input_json": "",
                    }
                elif block.type == "web_search_tool_result":
                    # Search results arrived — count results for info display
                    content = getattr(block, "content", None)
                    if isinstance(content, list):
                        n = sum(
                            1 for r in content if getattr(r, "type", None) == "web_search_result"
                        )
                        sc.info_delta = f"[Found {n} result{'s' if n != 1 else ''}]"
                    elif (
                        content is not None
                        and getattr(content, "type", None) == "web_search_tool_result_error"
                    ):
                        code = getattr(content, "error_code", "unknown")
                        sc.info_delta = f"[Web search error: {code}]"

            elif event_type == "content_block_delta":
                delta = event.delta
                if delta.type == "text_delta":
                    sc.content_delta = delta.text
                    # Accumulate text into raw block for preservation
                    if event.index in raw_blocks:
                        raw_blocks[event.index]["text"] = (
                            raw_blocks[event.index].get("text", "") + delta.text
                        )
                elif delta.type == "thinking_delta":
                    sc.reasoning_delta = delta.thinking
                    # Accumulate thinking text into raw block for round-trip
                    if event.index in raw_blocks:
                        raw_blocks[event.index]["thinking"] = (
                            raw_blocks[event.index].get("thinking", "") + delta.thinking
                        )
                elif delta.type == "input_json_delta":
                    if event.index in server_tool_blocks:
                        # Accumulate server tool input (search query)
                        server_tool_blocks[event.index]["input_json"] += delta.partial_json
                    else:
                        tool_idx = tool_block_to_index.get(event.index, event.index)
                        sc.tool_call_deltas.append(
                            ToolCallDelta(
                                index=tool_idx,
                                arguments_delta=delta.partial_json,
                            )
                        )
                    # Accumulate input JSON into raw block for tool_use/server_tool_use
                    if event.index in raw_blocks:
                        raw_blocks[event.index]["_input_json"] = (
                            raw_blocks[event.index].get("_input_json", "") + delta.partial_json
                        )

            elif event_type == "content_block_stop":
                # Finalize accumulated input JSON into parsed input
                if event.index in raw_blocks and "_input_json" in raw_blocks[event.index]:
                    rb = raw_blocks[event.index]
                    try:
                        rb["input"] = json.loads(rb.pop("_input_json"))
                    except (json.JSONDecodeError, TypeError):
                        rb.pop("_input_json", None)

                # When a server tool block completes, emit search query info
                if event.index in server_tool_blocks:
                    info = server_tool_blocks.pop(event.index)
                    query = ""
                    try:
                        parsed = json.loads(info["input_json"])
                        query = parsed.get("query", "")
                    except (json.JSONDecodeError, TypeError):
                        pass
                    sc.info_delta = f"[Searching: {query}]" if query else "[Searching...]"

            elif event_type == "message_delta":
                if hasattr(event, "usage") and event.usage:
                    u = event.usage
                    sc.usage = UsageInfo(
                        prompt_tokens=getattr(u, "input_tokens", 0),
                        completion_tokens=getattr(u, "output_tokens", 0),
                        total_tokens=(
                            getattr(u, "input_tokens", 0) + getattr(u, "output_tokens", 0)
                        ),
                    )
                if hasattr(event.delta, "stop_reason") and event.delta.stop_reason:
                    sc.finish_reason = _normalize_finish_reason(event.delta.stop_reason)
                    # Emit all raw content blocks for multi-turn preservation
                    if raw_blocks:
                        sc.provider_blocks = [raw_blocks[i] for i in sorted(raw_blocks)]

            elif event_type == "message_start":
                if hasattr(event.message, "usage") and event.message.usage:
                    u = event.message.usage
                    sc.usage = UsageInfo(
                        prompt_tokens=getattr(u, "input_tokens", 0),
                        completion_tokens=0,
                        total_tokens=getattr(u, "input_tokens", 0),
                    )

            has_content = sc.content_delta or sc.reasoning_delta or sc.tool_call_deltas
            if has_content and first:
                sc.is_first = True
                first = False

            if has_content or sc.finish_reason or sc.usage or sc.info_delta:
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
        _ensure_anthropic()
        caps = self.get_capabilities(model)
        system_prompt, converted_msgs = self._convert_messages(messages)
        kwargs = self._build_thinking_and_kwargs(
            caps,
            reasoning_effort,
            extra_params,
            max_tokens,
            temperature,
            converted_msgs,
            system_prompt,
            model,
            tools,
        )

        response = client.messages.create(**kwargs)

        # Extract content and tool_calls from content blocks.
        # Skip server-side blocks (server_tool_use, web_search_tool_result)
        # which are handled server-side and don't require client execution.
        content_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        provider_blocks: list[dict[str, Any]] = []
        for block in response.content:
            provider_blocks.append(_block_to_dict(block))
            if block.type == "text":
                content_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    {
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": json.dumps(block.input),
                        },
                    }
                )
            # server_tool_use, web_search_tool_result — captured in provider_blocks

        finish_reason = _normalize_finish_reason(response.stop_reason or "end_turn")

        usage = None
        if hasattr(response, "usage") and response.usage:
            u = response.usage
            usage = UsageInfo(
                prompt_tokens=u.input_tokens,
                completion_tokens=u.output_tokens,
                total_tokens=u.input_tokens + u.output_tokens,
            )

        return CompletionResult(
            content="\n".join(content_parts),
            tool_calls=tool_calls if tool_calls else None,
            finish_reason=finish_reason,
            usage=usage,
            provider_blocks=provider_blocks,
        )

    # -- retryable errors ----------------------------------------------------

    @property
    def retryable_error_names(self) -> frozenset[str]:
        return frozenset(
            {
                "RateLimitError",
                "APITimeoutError",
                "APIConnectionError",
                "InternalServerError",
                "APIError",
                "OverloadedError",
            }
        )


def _normalize_finish_reason(reason: str) -> str:
    """Normalize Anthropic stop reasons to OpenAI-compatible strings."""
    if reason == "end_turn":
        return "stop"
    if reason == "tool_use":
        return "tool_calls"
    if reason == "max_tokens":
        return "length"
    if reason == "pause_turn":
        # Server-side tool (web search) paused a long turn; treat as stop
        return "stop"
    return reason


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Convert an Anthropic SDK content block to a plain dict.

    Preserves all fields including ``encrypted_content`` and ``encrypted_index``
    on ``web_search_tool_result`` blocks, which must be passed back verbatim
    on subsequent turns for citation resolution.
    """
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True)  # type: ignore[no-any-return]
    # Fallback: extract known attributes
    d: dict[str, Any] = {"type": getattr(block, "type", "")}
    for attr in (
        "id",
        "name",
        "input",
        "text",
        "thinking",
        "signature",
        "content",
        "encrypted_content",
        "encrypted_index",
    ):
        val = getattr(block, attr, None)
        if val is not None:
            if hasattr(val, "model_dump"):
                d[attr] = val.model_dump()
            elif isinstance(val, list):
                d[attr] = [
                    item.model_dump() if hasattr(item, "model_dump") else item for item in val
                ]
            else:
                d[attr] = val
    return d
