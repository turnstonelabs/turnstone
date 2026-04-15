"""Responses API provider — for commercial OpenAI models (GPT-5.x, O-series).

Uses the OpenAI Responses API (``/v1/responses``) which natively supports
reasoning, tool use, web search, and tool search without the limitations
of the Chat Completions endpoint.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

import structlog

from turnstone.core.providers._openai_common import (
    RETRYABLE_ERROR_NAMES,
    apply_cache_retention,
    apply_temperature,
    apply_tool_search,
    extract_usage,
    format_citations,
    format_document_wrapper,
    lookup_openai_capabilities,
    resolve_reasoning_effort,
    sanitize_messages,
)
from turnstone.core.providers._protocol import (
    CompletionResult,
    ModelCapabilities,
    StreamChunk,
    ToolCallDelta,
)

log = structlog.get_logger(__name__)


def convert_content_parts(parts: list[Any]) -> list[dict[str, Any]]:
    """Convert Chat Completions content parts to Responses API format.

    Handles text, image_url, and internal ``document`` parts.  The
    Responses API uses ``input_image`` instead of ``image_url``; there
    is no native document block, so documents are inlined as
    ``input_text`` with a ``<document>`` wrapper.
    """
    converted: list[dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type", "")
        if ptype == "text":
            converted.append({"type": "input_text", "text": part.get("text", "")})
        elif ptype == "image_url":
            url_data = part.get("image_url", {})
            url = url_data.get("url", "") if isinstance(url_data, dict) else ""
            converted.append({"type": "input_image", "image_url": url})
        elif ptype == "document":
            d = part.get("document", {})
            converted.append(
                {
                    "type": "input_text",
                    "text": format_document_wrapper(
                        d.get("name", ""),
                        d.get("media_type", "text/plain"),
                        d.get("data", ""),
                    ),
                }
            )
        else:
            converted.append(part)
    return converted


class OpenAIResponsesProvider:
    """Provider for commercial OpenAI models via the Responses API.

    Translates between turnstone's internal OpenAI Chat Completions-like
    message format and the Responses API input/output format.
    """

    @property
    def provider_name(self) -> str:
        return "openai"

    def get_capabilities(self, model: str) -> ModelCapabilities:
        return lookup_openai_capabilities(model)

    # -- message conversion --------------------------------------------------

    @staticmethod
    def _convert_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert Chat Completions messages to Responses API input items.

        Returns ``(instructions, input_items)`` where *instructions* is the
        concatenated system/developer messages (or ``None``) and *input_items*
        is the Responses API ``input`` array.
        """
        messages = sanitize_messages(messages)
        instructions_parts: list[str] = []
        items: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content")

            if role in ("system", "developer"):
                if isinstance(content, str) and content:
                    instructions_parts.append(content)
                elif isinstance(content, list):
                    # Content parts — extract text
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            instructions_parts.append(part["text"])
                continue

            if role == "user":
                item: dict[str, Any] = {"type": "message", "role": "user"}
                if isinstance(content, str):
                    item["content"] = content
                elif isinstance(content, list):
                    # Vision: content parts (text + image_url)
                    item["content"] = convert_content_parts(content)
                else:
                    item["content"] = content or ""
                items.append(item)

            elif role == "assistant":
                # With store=False, provider_blocks cannot be replayed as input
                # (output format != input format, and IDs aren't persisted).
                # Rebuild from the normalized content/tool_calls instead.

                # Text content → assistant message (plain string for input)
                if content:
                    items.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": content,
                        }
                    )

                # Tool calls → function_call items
                for tc in msg.get("tool_calls") or []:
                    func = tc.get("function", {})
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": tc.get("id", ""),
                            "name": func.get("name", ""),
                            "arguments": func.get("arguments", ""),
                        }
                    )

            elif role == "tool":
                # Tool result → function_call_output
                output = content
                if isinstance(content, list):
                    # Structured content (e.g. vision) — serialize to string
                    output = json.dumps(content)
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": msg.get("tool_call_id", ""),
                        "output": output or "",
                    }
                )

        instructions = "\n\n".join(instructions_parts) if instructions_parts else None
        return instructions, items

    # -- tool conversion -----------------------------------------------------

    @staticmethod
    def _convert_tools(
        tools: list[dict[str, Any]] | None,
        caps: ModelCapabilities,
    ) -> list[dict[str, Any]] | None:
        """Convert Chat Completions tool format to Responses API format.

        Chat Completions: ``{"type": "function", "function": {"name", "description", "parameters"}}``
        Responses API:    ``{"type": "function", "name", "description", "parameters", "strict": false}``

        Also handles web_search injection for models that support it.
        """
        if not tools:
            return None

        converted: list[dict[str, Any]] = []
        has_web_search_func = False

        for tool in tools:
            func = tool.get("function")
            if not func:
                converted.append(tool)
                continue

            name = func.get("name", "")

            # web_search function tool → native web_search_tool
            if name == "web_search" and caps.supports_web_search:
                has_web_search_func = True
                continue

            item: dict[str, Any] = {
                "type": "function",
                "name": name,
                "description": func.get("description", ""),
                "parameters": func.get("parameters", {}),
                "strict": False,
            }
            # Preserve defer_loading for tool search
            if tool.get("defer_loading"):
                item["defer_loading"] = True
            converted.append(item)

        # Inject native web search tool
        if has_web_search_func or caps.supports_web_search:
            converted.append({"type": "web_search"})

        # Responses API requires a tool_search tool when defer_loading is used
        if any(t.get("defer_loading") for t in converted):
            converted.append({"type": "tool_search"})

        return converted if converted else None

    # -- parameter building --------------------------------------------------

    def _build_kwargs(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str,
        deferred_names: frozenset[str] | None,
        capabilities: ModelCapabilities | None = None,
    ) -> dict[str, Any]:
        """Build the kwargs dict for ``client.responses.create/stream``."""
        caps = capabilities or self.get_capabilities(model)

        instructions, input_items = self._convert_messages(messages)
        tools = apply_tool_search(caps, tools, deferred_names)
        converted_tools = self._convert_tools(tools, caps)

        # Ensure web search is always injected for search-capable models,
        # even when no function tools are registered (e.g. creative mode).
        if caps.supports_web_search:
            converted_tools = converted_tools or []
            if not any(t.get("type") == "web_search" for t in converted_tools):
                converted_tools.append({"type": "web_search"})

        kwargs: dict[str, Any] = {
            "model": model,
            "input": input_items,
            "max_output_tokens": max_tokens,
            "store": False,
        }

        if instructions:
            kwargs["instructions"] = instructions

        if converted_tools:
            kwargs["tools"] = converted_tools

        apply_temperature(kwargs, caps, temperature, reasoning_effort)

        # Reasoning effort → {"effort": value} dict (Responses API format)
        effort = resolve_reasoning_effort(caps, reasoning_effort)
        if effort:
            kwargs["reasoning"] = {"effort": effort}

        apply_cache_retention(kwargs, model)
        return kwargs

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
        capabilities: ModelCapabilities | None = None,
    ) -> Iterator[StreamChunk]:
        if extra_params:
            log.debug("openai.responses: extra_params ignored (not supported by Responses API)")
        kwargs = self._build_kwargs(
            model,
            messages,
            tools,
            max_tokens,
            temperature,
            reasoning_effort,
            deferred_names,
            capabilities=capabilities,
        )
        kwargs["stream"] = True

        log.debug(
            "openai.responses.request",
            model=model,
            stream=True,
            max_tokens=max_tokens,
            input_items=len(kwargs.get("input", [])),
            tool_count=len(kwargs.get("tools", [])),
        )

        stream = client.responses.create(**kwargs)
        if cancel_ref is not None:
            cancel_ref.append(stream)
        return self._iter_stream(stream)

    def _iter_stream(self, stream: Any) -> Iterator[StreamChunk]:
        """Convert Responses API stream events to StreamChunks."""
        first = True
        content_len = 0
        tool_call_count = 0
        last_finish: str | None = None
        completion_tokens: int | None = None
        # Track tool call indices by call_id for consistent ToolCallDelta.index
        tool_call_indices: dict[str, int] = {}
        # Collect output items for provider_blocks
        provider_blocks: list[dict[str, Any]] = []
        # Collect annotations across text parts
        annotations: list[Any] = []

        for event in stream:
            event_type = getattr(event, "type", "")

            # -- text content deltas --
            if event_type == "response.output_text.delta":
                delta_text = getattr(event, "delta", "")
                if delta_text:
                    sc = StreamChunk(content_delta=delta_text)
                    content_len += len(delta_text)
                    if first:
                        sc.is_first = True
                        first = False
                    yield sc
                continue

            # -- reasoning deltas --
            if event_type in (
                "response.reasoning_text.delta",
                "response.reasoning_summary_text.delta",
            ):
                delta_text = getattr(event, "delta", "")
                if delta_text:
                    sc = StreamChunk(reasoning_delta=delta_text)
                    if first:
                        sc.is_first = True
                        first = False
                    yield sc
                continue

            # -- new tool call (function_call output item added) --
            if event_type == "response.output_item.added":
                item = getattr(event, "item", None)
                if item and getattr(item, "type", "") == "function_call":
                    call_id = getattr(item, "call_id", "")
                    item_id = getattr(item, "id", "")
                    name = getattr(item, "name", "")
                    idx = len(tool_call_indices)
                    # Index by item_id — argument deltas reference this, not call_id
                    tool_call_indices[item_id] = idx
                    sc = StreamChunk(
                        tool_call_deltas=[ToolCallDelta(index=idx, id=call_id, name=name)]
                    )
                    tool_call_count += 1
                    if first:
                        sc.is_first = True
                        first = False
                    yield sc
                continue

            # -- tool call argument deltas --
            if event_type == "response.function_call_arguments.delta":
                item_id = getattr(event, "item_id", "")
                delta_args = getattr(event, "delta", "")
                if delta_args:
                    idx = tool_call_indices.get(item_id, 0)
                    yield StreamChunk(
                        tool_call_deltas=[ToolCallDelta(index=idx, arguments_delta=delta_args)]
                    )
                continue

            # -- web search status --
            if event_type == "response.web_search_call.searching":
                yield StreamChunk(info_delta="[Searching…]")
                continue
            if event_type == "response.web_search_call.completed":
                yield StreamChunk(info_delta="[Search complete]")
                continue

            # -- output item done (capture for provider_blocks) --
            if event_type == "response.output_item.done":
                item = getattr(event, "item", None)
                if item:
                    item_dict = item.model_dump() if hasattr(item, "model_dump") else {}
                    if item_dict:
                        provider_blocks.append(item_dict)
                    # Collect annotations from completed text parts
                    if getattr(item, "type", "") == "message":
                        for content_part in getattr(item, "content", []):
                            part_anns = getattr(content_part, "annotations", None)
                            if part_anns:
                                annotations.extend(part_anns)
                continue

            # -- response completed --
            if event_type == "response.completed":
                response = getattr(event, "response", None)
                if response:
                    status = getattr(response, "status", "completed")
                    last_finish = "stop" if status == "completed" else "length"
                    usage = extract_usage(getattr(response, "usage", None))
                    if usage:
                        completion_tokens = usage.completion_tokens
                    sc = StreamChunk(
                        finish_reason=last_finish,
                        usage=usage,
                    )
                    if provider_blocks:
                        sc.provider_blocks = provider_blocks
                    yield sc
                continue

            # -- error --
            if event_type == "response.failed":
                response = getattr(event, "response", None)
                error = getattr(response, "error", None) if response else None
                error_msg = getattr(error, "message", "Unknown error") if error else "Unknown error"
                raise RuntimeError(f"Responses API error: {error_msg}")

        log.debug(
            "openai.responses.response",
            stream=True,
            finish_reason=last_finish,
            content_length=content_len,
            tool_call_count=tool_call_count,
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
        capabilities: ModelCapabilities | None = None,
    ) -> CompletionResult:
        if extra_params:
            log.debug("openai.responses: extra_params ignored (not supported by Responses API)")
        kwargs = self._build_kwargs(
            model,
            messages,
            tools,
            max_tokens,
            temperature,
            reasoning_effort,
            deferred_names,
            capabilities=capabilities,
        )

        log.debug(
            "openai.responses.request",
            model=model,
            stream=False,
            max_tokens=max_tokens,
            input_items=len(kwargs.get("input", [])),
            tool_count=len(kwargs.get("tools", [])),
        )

        response = client.responses.create(**kwargs)
        return self._parse_response(response)

    def _parse_response(self, response: Any) -> CompletionResult:
        """Convert a Responses API ``Response`` object to ``CompletionResult``."""
        content_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        provider_blocks: list[dict[str, Any]] = []
        all_annotations: list[Any] = []

        for item in getattr(response, "output", []):
            item_type = getattr(item, "type", "")

            if item_type == "message":
                for content_part in getattr(item, "content", []):
                    part_type = getattr(content_part, "type", "")
                    if part_type == "output_text":
                        content_parts.append(getattr(content_part, "text", ""))
                        anns = getattr(content_part, "annotations", None)
                        if anns:
                            all_annotations.extend(anns)
                    elif part_type == "refusal":
                        content_parts.append(f"[Refused: {getattr(content_part, 'refusal', '')}]")

            elif item_type == "function_call":
                tool_calls.append(
                    {
                        "id": getattr(item, "call_id", ""),
                        "type": "function",
                        "function": {
                            "name": getattr(item, "name", ""),
                            "arguments": getattr(item, "arguments", ""),
                        },
                    }
                )

            # Capture all output items for provider_blocks (multi-turn)
            item_dict = item.model_dump() if hasattr(item, "model_dump") else {}
            if item_dict:
                provider_blocks.append(item_dict)

        content = "".join(content_parts)
        if all_annotations:
            content = format_citations(content, all_annotations)

        status = getattr(response, "status", "completed")
        finish_reason = "stop" if status == "completed" else "length"
        usage = extract_usage(getattr(response, "usage", None))

        result = CompletionResult(
            content=content,
            tool_calls=tool_calls if tool_calls else None,
            finish_reason=finish_reason,
            usage=usage,
            provider_blocks=provider_blocks,
        )
        log.debug(
            "openai.responses.response",
            stream=False,
            finish_reason=finish_reason,
            content_length=len(content),
            tool_call_count=len(tool_calls),
            completion_tokens=usage.completion_tokens if usage else None,
        )
        return result

    # -- tool conversion (public interface) ----------------------------------

    def convert_tools(
        self,
        tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return tools  # Conversion happens internally in _build_kwargs

    # -- retryable errors ----------------------------------------------------

    @property
    def retryable_error_names(self) -> frozenset[str]:
        return RETRYABLE_ERROR_NAMES
