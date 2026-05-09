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
    _join_reasoning_with_cap,
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
        *,
        replay_reasoning_to_model: bool = False,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert Chat Completions messages to Responses API input items.

        Returns ``(instructions, input_items)`` where *instructions* is the
        concatenated system/developer messages (or ``None``) and *input_items*
        is the Responses API ``input`` array.

        When *replay_reasoning_to_model* is True, stored ``_provider_content``
        reasoning items (``type=="reasoning"``, captured via
        ``include=["reasoning.encrypted_content"]`` on a prior turn)
        are emitted as ``ResponseReasoningItemParam`` input items
        immediately before the assistant message they belong to.  The
        SDK explicitly documents this round-trip pattern at
        ``response_reasoning_item_param.py:33-37``: "Be sure to include
        these items in your ``input`` to the Responses API for
        subsequent turns of a conversation if you are manually managing
        context".  Even with ``store=False``, ``encrypted_content``
        round-trips correctly per ``response_create_params.py:70-74``.

        When *replay_reasoning_to_model* is False, reasoning items are silently
        dropped (they were stripped from the wire by ``sanitize_messages``
        anyway, but we also skip the input-item emission step).
        """
        # Capture ``_provider_content`` reasoning items per ASSISTANT
        # ORDINAL (not raw message index) BEFORE sanitization strips
        # the underscore-prefixed key.  Position-by-index would be
        # unsafe: ``sanitize_messages`` drops orphan tool results
        # (``_openai_common.py:489-498`` / ``:521-535``) and inserts
        # synthesized error tool messages for orphaned tool_calls
        # (``:510-517``).  Either operation shifts subsequent message
        # indices, so a pre-vs-post-sanitize index match would
        # silently miss reasoning attachments after any tool-message
        # repair.  Assistant messages themselves are never dropped or
        # duplicated by sanitize_messages — only tool messages — so
        # the n-th assistant in the original list is invariably the
        # n-th assistant in the sanitized list.  Ordinal-keyed lookup
        # survives any tool-message length change.
        reasoning_by_assistant_ordinal: dict[int, list[dict[str, Any]]] = {}
        if replay_reasoning_to_model:
            ord_pre = 0
            for raw_msg in messages:
                if raw_msg.get("role") != "assistant":
                    continue
                pc = raw_msg.get("_provider_content")
                if isinstance(pc, list):
                    items_to_replay = [
                        b for b in pc if isinstance(b, dict) and b.get("type") == "reasoning"
                    ]
                    if items_to_replay:
                        reasoning_by_assistant_ordinal[ord_pre] = items_to_replay
                ord_pre += 1

        messages = sanitize_messages(messages)
        instructions_parts: list[str] = []
        items: list[dict[str, Any]] = []
        # Track assistant ordinal in the SANITIZED list so the lookup
        # into reasoning_by_assistant_ordinal stays aligned with the
        # original-list ordinal.  See the long comment above for why
        # ordinal is invariant under sanitization.
        assistant_ordinal_post = 0

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
                # Phase 3 reasoning replay: emit stored reasoning items
                # BEFORE the assistant message they belong to.  The SDK
                # expects reasoning items to appear in input order
                # alongside the assistant turn that produced them.
                for r_item in reasoning_by_assistant_ordinal.get(assistant_ordinal_post, []):
                    item_for_input = _reasoning_item_for_input(r_item)
                    if item_for_input is not None:
                        items.append(item_for_input)
                assistant_ordinal_post += 1

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
        replay_reasoning_to_model: bool = True,
    ) -> dict[str, Any]:
        """Build the kwargs dict for ``client.responses.create/stream``.

        ``replay_reasoning_to_model`` (Phase 3 of the reasoning-
        persistence feature) gates two things together:
        1. ``include=["reasoning.encrypted_content"]`` on the request
           (so the API surfaces ``encrypted_content`` on reasoning
           items in ``provider_blocks``).
        2. ``_convert_messages`` round-tripping stored reasoning items
           from ``_provider_content`` as ``input`` items on subsequent
           turns (the SDK's ``ResponseReasoningItemParam`` shape).

        Both are also gated by ``caps.supports_reasoning_replay`` —
        models without a reasoning lane (gpt-4o, etc.) silently skip
        replay even if the operator flag is set.
        """
        caps = capabilities or self.get_capabilities(model)

        # The two replay gates collapse to a single boolean: replay is
        # active only when both the operator flag AND the model's
        # capability allow it.  Threaded into ``_convert_messages`` so
        # stored reasoning items become input items on the next call.
        replay_active = bool(replay_reasoning_to_model and caps.supports_reasoning_replay)
        instructions, input_items = self._convert_messages(
            messages, replay_reasoning_to_model=replay_active
        )
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

        if replay_active:
            # SDK doc (response_create_params.py:70-74): with
            # ``include=["reasoning.encrypted_content"]`` the API
            # surfaces opaque ``encrypted_content`` on reasoning
            # items, enabling stateless replay even with ``store=False``.
            kwargs["include"] = ["reasoning.encrypted_content"]

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
        # Phase 3 reasoning-persistence kwarg — gates
        # ``include=["reasoning.encrypted_content"]`` on the request
        # AND ``_convert_messages`` round-tripping stored reasoning
        # items as input.  Both are also gated by
        # ``caps.supports_reasoning_replay`` inside ``_build_kwargs``.
        replay_reasoning_to_model: bool = True,
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
            replay_reasoning_to_model=replay_reasoning_to_model,
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
        # See create_streaming above for the Phase 3 reasoning-persistence rationale.
        replay_reasoning_to_model: bool = True,
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
            replay_reasoning_to_model=replay_reasoning_to_model,
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

    # -- reasoning extraction ------------------------------------------------

    def extract_reasoning_text(
        self,
        provider_blocks: list[dict[str, Any]] | None,
    ) -> str:
        if not isinstance(provider_blocks, list):
            return ""
        parts: list[str] = []
        for block in provider_blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "reasoning":
                continue
            # Per ``ResponseReasoningItem`` (response_reasoning_item.py:31-62):
            # ``summary`` is the human-readable summary list (always
            # present), ``content`` is the raw reasoning text list
            # (optional). We surface both — summary is what the model
            # produces by default; content is only present on certain
            # configurations.
            for s in block.get("summary") or []:
                if isinstance(s, dict) and s.get("type") == "summary_text":
                    text = s.get("text")
                    if isinstance(text, str) and text:
                        parts.append(text)
            for c in block.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "reasoning_text":
                    text = c.get("text")
                    if isinstance(text, str) and text:
                        parts.append(text)
        return _join_reasoning_with_cap(parts)


def _reasoning_item_for_input(stored: dict[str, Any]) -> dict[str, Any] | None:
    """Project a stored reasoning item into ``ResponseReasoningItemParam`` shape.

    The output of a Responses API call carries reasoning items shaped
    like ``ResponseReasoningItem`` (response_reasoning_item.py:31-62);
    we stored those verbatim into ``provider_blocks`` via
    ``item.model_dump()`` (``_iter_stream`` line 415-420 captures all
    output items).  To replay them as input on the next turn, the
    Responses API expects ``ResponseReasoningItemParam``
    (response_reasoning_item_param.py:31-62) which has the same shape
    minus ``status`` (a server-only field).

    The ``id``, ``summary``, ``content``, ``encrypted_content``, and
    ``type`` fields all round-trip directly.  We project explicitly
    rather than ``del stored["status"]; return stored`` so callers
    aren't surprised by mutation of the source dict.

    Returns ``None`` when ``id`` is missing or non-string — per the
    SDK schema (``response_reasoning_item_param.py:39``) ``id`` is
    ``Required[str]``; sending an empty string would emit a malformed
    input item that the API may either reject (4xx) or silently
    misroute.  Caller skips appending when None is returned.  Items
    captured via the streaming layer always have ``id`` populated, so
    this guard is defensive against manually-constructed or migrated
    storage rows.
    """
    item_id = stored.get("id")
    if not isinstance(item_id, str) or not item_id:
        return None
    out: dict[str, Any] = {
        "type": "reasoning",
        "id": item_id,
        "summary": stored.get("summary") or [],
    }
    content = stored.get("content")
    if content:
        out["content"] = content
    encrypted = stored.get("encrypted_content")
    if encrypted:
        out["encrypted_content"] = encrypted
    return out
