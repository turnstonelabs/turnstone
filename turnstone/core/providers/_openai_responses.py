"""Responses API provider — for commercial OpenAI models (GPT-5.x, O-series).

Uses the OpenAI Responses API (``/v1/responses``) which natively supports
reasoning, tool use, web search, and tool search without the limitations
of the Chat Completions endpoint.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

import structlog

from turnstone.core.providers._openai_common import (
    OPENAI_COMPAT_DEFAULT,
    REASONING_MODES,
    RETRYABLE_ERROR_NAMES,
    apply_cache_retention,
    apply_temperature,
    apply_tool_search,
    apply_verbosity,
    extract_usage,
    format_citations,
    format_document_wrapper,
    lookup_openai_capabilities,
    resolve_server_side_tools,
    sanitize_messages,
)
from turnstone.core.providers._protocol import (
    ModelCapabilities,
    StreamChunk,
    ToolCallDelta,
    _join_reasoning_with_cap,
    resolve_reasoning_effort,
)
from turnstone.core.trajectory import materialize_attachments

log = structlog.get_logger(__name__)


# response.failed error codes that indicate a transient server-side
# condition — the only ones worth retrying (the API's other codes are
# deterministic request rejections).
_TRANSIENT_FAILURE_CODES = frozenset({"server_error", "rate_limit_exceeded"})


def _extend_message_annotations(item: Any, annotations: list[Any]) -> None:
    """Collect url_citation annotations off a message output item's text
    parts — ONE walk shared by the ``output_item.done`` handler and the
    terminal-payload rebuild so the two cannot drift (``content`` may be
    ``None`` on partial items)."""
    if getattr(item, "type", "") != "message":
        return
    for content_part in getattr(item, "content", None) or []:
        part_anns = getattr(content_part, "annotations", None)
        if part_anns:
            annotations.extend(part_anns)


def _raise_responses_failure(error_code: str, error_msg: str) -> NoReturn:
    """One classification ladder for BOTH in-band failure shapes (`error`
    events and ``response.failed``) — a one-sided edit would make the same
    API failure retryable through one event type and fatal through the
    other."""
    if error_code in _TRANSIENT_FAILURE_CODES:
        raise ResponsesStreamFailedError(f"Responses API error ({error_code}): {error_msg}")
    raise RuntimeError(f"Responses API error ({error_code or 'unknown'}): {error_msg}")


class ResponsesStreamFailedError(RuntimeError):
    """A TRANSIENT in-band ``response.failed`` terminal event.

    Raised only for ``_TRANSIENT_FAILURE_CODES`` (server error, rate
    limit) — and listed in the provider's ``retryable_error_names`` — so
    retry loops treat those like the wire-level errors they stand in
    for.  Deterministic in-band failures (invalid prompt, image fetch,
    policy) raise plain ``RuntimeError`` and stop retry loops on attempt
    zero, exactly as the retired non-streaming lane's HTTP errors did.
    Callers that give up keep their degrade paths (judges fall back to
    the heuristic tier).
    """


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
            if d.get("media_type") == "application/pdf":
                # Native PDF: Responses ``input_file`` with an inline base64
                # data URI (``data`` is already base64 — see
                # storage/_utils.attachment_to_content_part).
                converted.append(
                    {
                        "type": "input_file",
                        "filename": d.get("name") or "document.pdf",
                        "file_data": f"data:application/pdf;base64,{d.get('data', '')}",
                    }
                )
            else:
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
        elif ptype == "input_audio":
            # Audio-input is not wired on the Responses lane.  The capability-gated
            # fallback (STT / perception) runs upstream of this translator, so by
            # here any remaining input_audio is a defensive placeholder rather than
            # an unhandled part leaking to the API.
            converted.append(
                {"type": "input_text", "text": "[audio attachment — not supported by this model]"}
            )
        else:
            converted.append(part)
    return converted


class OpenAIResponsesProvider:
    """Provider for the Responses API — commercial OpenAI, plus the
    ``openai-compatible`` lane pinned to ``api_surface="responses"``.

    Translates between turnstone's internal OpenAI Chat Completions-like
    message format and the Responses API input/output format.

    *compat* mirrors ``AnthropicProvider(compat=True)``: the compat-mode
    instance serves operator-run Responses endpoints, so capability
    lookup skips the commercial table (``OPENAI_COMPAT_DEFAULT`` — the
    model id is an operator-chosen string there, and a prefix collision
    with a cloud model id must not inherit its contract).  The request
    shape is identical in both modes; ``XAIProvider`` subclasses the
    default (non-compat) mode.
    """

    def __init__(self, *, compat: bool = False) -> None:
        self._compat = compat

    @property
    def provider_name(self) -> str:
        return "openai"

    def get_capabilities(self, model: str) -> ModelCapabilities:
        if self._compat:
            return OPENAI_COMPAT_DEFAULT
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

        Default ``False`` differs intentionally from
        ``AnthropicProvider._convert_messages`` (which defaults
        ``True``).  Anthropic's default exists for back-compat with
        pre-Phase-2 callers who never threaded the kwarg; OpenAI
        Responses replay is brand-new in Phase 3 and has no such
        legacy.  Production callers (``_build_kwargs``) always pass
        the resolved flag explicitly, so the default only matters in
        tests.  Conservative-default-False keeps the persist-only
        capture path live without forcing a downstream cost on every
        unaware caller.
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

        # Skip PDF inlining: this lane has a native ``input_file`` block, so the
        # ``application/pdf`` document part must survive to ``convert_content_parts``
        # below.  Without this, ``sanitize_messages`` would replace it with an
        # unsupported-placeholder before the native translator ever runs.
        messages = sanitize_messages(messages, skip_pdf_inline=True)
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

        # Inject native web search — replace-only: it stands in for a client
        # ``web_search`` def that survived the session's visibility filter.
        # ``caps.supports_web_search`` alone must NOT inject, or a toolset
        # whose envelope hides web_search (persona visibility set,
        # coordinator toolset) gains native search on capable models; the
        # capability-only lane for def-less requests is handled (and gated
        # the same way) by the server_side_tools loop in _build_kwargs.
        if has_web_search_func:
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
        temperature: float | None,
        reasoning_effort: str | None,
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

        The AND-gate against ``caps.supports_reasoning_replay`` lives
        upstream in ``model_turn.resolve_replay_reasoning_to_model``
        (single source of truth across providers; the session wrapper
        delegates there).  Production callers always thread the resolved
        flag, so this method trusts the bool it receives.
        """
        caps = capabilities or self.get_capabilities(model)

        instructions, input_items = self._convert_messages(
            messages, replay_reasoning_to_model=replay_reasoning_to_model
        )
        tools = apply_tool_search(caps, tools, deferred_names)
        converted_tools = self._convert_tools(tools, caps)

        # Auto-inject server-side tools declared on the capability row.
        # ``resolve_server_side_tools`` merges the legacy
        # ``supports_web_search`` flag, so search-capable models that
        # haven't been migrated to the explicit tuple still get
        # ``{"type": "web_search"}`` appended.  Subclasses (e.g.
        # ``XAIProvider``) opt their own provider-specific server tools
        # into ``caps.server_side_tools`` and inherit this injection.
        # Replace-only for EVERY server-side tool: inject the native entry only
        # when a same-named client def survived the session's visibility filter.
        # This ties server-side tools into the persona / coordinator envelope —
        # a visibility set that hides (or never allowlisted) the client def also
        # suppresses the native injection, closing the gap where a provider-
        # specific server-side tool would otherwise inject past a restricted
        # persona.  web_search is the only such tool today; a future server-side
        # tool must ship a client def to be injectable (and thus gateable).
        # NOTE: the match is by exact string — the caps ``type`` must equal the
        # client def's ``name`` (true for web_search).  A tool whose native type
        # differs from its client name (e.g. ``web_search_preview`` vs a
        # ``web_search`` def) would need an explicit type→name map added here, or
        # it silently won't inject.
        client_tool_names = {
            t.get("function", {}).get("name") for t in tools or [] if "function" in t
        }
        for tool_type in resolve_server_side_tools(caps):
            if tool_type not in client_tool_names:
                continue
            converted_tools = converted_tools or []
            if not any(t.get("type") == tool_type for t in converted_tools):
                converted_tools.append({"type": tool_type})

        kwargs: dict[str, Any] = {
            "model": model,
            "input": input_items,
            "max_output_tokens": max_tokens,
            "store": False,
        }

        if replay_reasoning_to_model:
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

        # Reasoning params → {"effort": ..., "mode": ...} (Responses format).
        # "mode": "pro" (GPT-5.6) applies more model work before a single
        # final answer; it rides with or without an effort level (effort
        # defaults to medium in pro mode), and effort still rides without a
        # mode.  Both are operator-declared and gated by their static
        # capability, so a value on a model lacking the feature is dropped.
        reasoning: dict[str, Any] = {}
        effort = resolve_reasoning_effort(caps, reasoning_effort)
        if effort:
            reasoning["effort"] = effort
        if caps.supports_pro_mode and caps.reasoning_mode != "":
            if not isinstance(caps.reasoning_mode, str):
                log.warning(
                    "openai.responses: ignoring non-string reasoning mode",
                    value=caps.reasoning_mode,
                    expected=sorted(REASONING_MODES),
                )
            elif caps.reasoning_mode in REASONING_MODES:
                reasoning["mode"] = caps.reasoning_mode
            else:
                log.warning(
                    "openai.responses: ignoring unknown reasoning mode",
                    value=caps.reasoning_mode,
                    expected=sorted(REASONING_MODES),
                )
        if reasoning:
            kwargs["reasoning"] = reasoning

        apply_verbosity(kwargs, caps)
        if not self._compat:
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
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        extra_params: dict[str, Any] | None = None,
        deferred_names: frozenset[str] | None = None,
        cancel_ref: list[Any] | None = None,
        capabilities: ModelCapabilities | None = None,
        # Phase 3 reasoning-persistence kwarg — gates
        # ``include=["reasoning.encrypted_content"]`` on the request
        # AND ``_convert_messages`` round-tripping stored reasoning
        # items as input.  The AND-gate against
        # ``caps.supports_reasoning_replay`` lives in
        # ``model_turn.resolve_replay_reasoning_to_model`` — single
        # source of truth across providers (the session wrapper
        # delegates there).
        replay_reasoning_to_model: bool = True,
        extra_headers: dict[str, str] | None = None,
        resolve_attachments: Callable[[list[str]], dict[str, Any]] | None = None,
    ) -> Iterator[StreamChunk]:
        messages = materialize_attachments(messages, resolve_attachments)
        if extra_params:
            log.debug("openai.responses: extra_params ignored (not supported by Responses API)")
        caps = capabilities or self.get_capabilities(model)
        kwargs = self._build_kwargs(
            model,
            messages,
            tools,
            max_tokens,
            temperature,
            reasoning_effort,
            deferred_names,
            capabilities=caps,
            replay_reasoning_to_model=replay_reasoning_to_model,
        )
        kwargs["stream"] = True
        if extra_headers:
            kwargs["extra_headers"] = extra_headers

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
        return self._iter_stream(stream, finish_reason_optional=caps.finish_reason_optional)

    def _iter_stream(
        self, stream: Any, *, finish_reason_optional: bool = False
    ) -> Iterator[StreamChunk]:
        """Convert Responses API stream events to StreamChunks.

        *finish_reason_optional* is the model capability of the same name:
        a lax Responses-compatible server that ends the stream without any
        terminal event (``response.completed`` / ``response.incomplete``)
        gets the end-of-generator shim below — the retired non-streaming
        path needed no terminal event either.
        """
        first = True
        content_len = 0
        reasoning_len = 0
        tool_call_count = 0
        last_finish: str | None = None
        completion_tokens: int | None = None
        # Track tool call indices by item_id for consistent ToolCallDelta.index.
        # ``next_tool_idx`` mints slots (NOT len(dict): duplicate/empty item
        # ids overwrite their mapping and would collide later slots);
        # ``last_tool_idx`` routes argument deltas whose item_id was never
        # announced — a lax server's deltas belong to the call most recently
        # opened, not hardwired slot 0.
        tool_call_indices: dict[str, int] = {}
        next_tool_idx = 0
        last_tool_idx = 0
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
                    reasoning_len += len(delta_text)
                    if first:
                        sc.is_first = True
                        first = False
                    yield sc
                continue

            # -- refusal parts --
            # Emitted whole on the ``done`` event (not per-delta), matching
            # how the Responses API's non-streaming shape carries refusals
            # (one whole content part) — handling the deltas too would
            # double-emit.
            if event_type == "response.refusal.done":
                refusal_text = getattr(event, "refusal", "")
                sc = StreamChunk(content_delta=f"[Refused: {refusal_text}]")
                content_len += len(sc.content_delta)
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
                    idx = next_tool_idx
                    next_tool_idx += 1
                    last_tool_idx = idx
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
                    idx = tool_call_indices.get(item_id, last_tool_idx)
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
                    _extend_message_annotations(item, annotations)
                continue

            # -- terminal response event --
            # ``response.incomplete`` is the real terminal event for a
            # max-output-tokens truncation (status "incomplete"); handling
            # only ``response.completed`` dropped the truncated run's
            # finish reason, final usage, AND collected provider_blocks.
            if event_type in ("response.completed", "response.incomplete"):
                response = getattr(event, "response", None)
                # A terminal event without a usable status — payload absent
                # entirely (lax compat server) OR a slim payload omitting
                # the field — is still a terminal signal: the event type
                # itself says whether the run completed.  Only usage (and
                # the rebuild below) genuinely needs the payload.
                status = (getattr(response, "status", "") if response is not None else "") or ""
                if not status:
                    status = "completed" if event_type == "response.completed" else "incomplete"
                last_finish = "stop" if status == "completed" else "length"
                usage = extract_usage(getattr(response, "usage", None)) if response else None
                if usage:
                    completion_tokens = usage.completion_tokens
                # Prefer the terminal response's own output items over the
                # incrementally collected ones — but only when the two can
                # DISAGREE: on truncation (an item still being generated
                # never receives its ``output_item.done`` event, and
                # storing a reasoning item without its required following
                # item makes the next turn's replay a 400) or when the
                # collected count differs from the terminal output (a lax
                # server dropped ``.done`` events).  On the happy path the
                # ``.done`` items ARE the terminal items, so the rebuild is
                # skipped — it would re-serialize every output item and
                # double every already-collected annotation once per turn.
                # A rebuild replaces BOTH lanes: blocks from the terminal
                # output, annotations from a fresh walk of it (annotations
                # have no other source than these item walks).
                out_items = (getattr(response, "output", None) or []) if response else []
                if status != "completed" or len(out_items) != len(provider_blocks):
                    final_items = [
                        item.model_dump() for item in out_items if hasattr(item, "model_dump")
                    ]
                    if final_items:
                        provider_blocks = final_items
                        annotations = []
                        for item in out_items:
                            _extend_message_annotations(item, annotations)
                # Under-streaming gateway parity: the retired non-streaming
                # path read content and tool calls off this same terminal
                # payload, so output that exists ONLY in the final blocks —
                # a buffering proxy that never fired output_text.delta /
                # output_item.added events — must be emitted here or the
                # drained result is a clean-looking empty success.  Gated
                # on NOTHING of that kind having streamed: the normal
                # event flow already emitted these, and a partially
                # under-streaming server (some deltas, fuller terminal) is
                # indistinguishable from a complete stream without diffing.
                if content_len == 0:
                    parts_text: list[str] = []
                    for block in provider_blocks:
                        if not (isinstance(block, dict) and block.get("type") == "message"):
                            continue
                        for part in block.get("content") or []:
                            if not isinstance(part, dict):
                                continue
                            if part.get("type") == "output_text" and part.get("text"):
                                parts_text.append(part["text"])
                            elif part.get("type") == "refusal" and part.get("refusal"):
                                parts_text.append(f"[Refused: {part['refusal']}]")
                    harvested = "".join(parts_text)
                    if harvested:
                        content_len = len(harvested)
                        hc = StreamChunk(content_delta=harvested)
                        if first:
                            hc.is_first = True
                            first = False
                        yield hc
                if tool_call_count == 0:
                    for block in provider_blocks:
                        if not (isinstance(block, dict) and block.get("type") == "function_call"):
                            continue
                        idx = next_tool_idx
                        next_tool_idx += 1
                        tool_call_count += 1
                        tc_chunk = StreamChunk(
                            tool_call_deltas=[
                                ToolCallDelta(
                                    index=idx,
                                    id=block.get("call_id", "") or "",
                                    name=block.get("name", "") or "",
                                    arguments_delta=block.get("arguments", "") or "",
                                )
                            ]
                        )
                        if first:
                            tc_chunk.is_first = True
                            first = False
                        yield tc_chunk
                sc = StreamChunk(
                    finish_reason=last_finish,
                    usage=usage,
                )
                if provider_blocks:
                    sc.provider_blocks = provider_blocks
                yield sc
                continue

            # -- in-band error event (ResponseErrorEvent) --
            # The SDK YIELDS `error` SSE events rather than raising, and no
            # response.failed necessarily follows — without this branch the
            # stream exhausts finish-less and the real API message is lost
            # behind a misleading IncompleteStreamError.  AFTER a terminal
            # event the generation is complete and in hand: a trailing
            # error frame is teardown noise, and raising would discard the
            # finished result (the in-band twin of the post-finish
            # transport-blip tolerance ``drain_stream`` grants).
            if event_type == "error":
                if last_finish is not None:
                    log.warning(
                        "openai.responses.post_terminal_error",
                        code=getattr(event, "code", "") or "",
                        message=getattr(event, "message", "") or "",
                    )
                    break
                _raise_responses_failure(
                    getattr(event, "code", "") or "",
                    getattr(event, "message", "Unknown error") or "Unknown error",
                )

            # -- error --
            if event_type == "response.failed":
                response = getattr(event, "response", None)
                error = getattr(response, "error", None) if response else None
                if last_finish is not None:
                    log.warning(
                        "openai.responses.post_terminal_error",
                        code=(getattr(error, "code", "") if error else "") or "",
                        message=(getattr(error, "message", "") if error else "") or "",
                    )
                    break
                _raise_responses_failure(
                    (getattr(error, "code", "") if error else "") or "",
                    (getattr(error, "message", "") if error else "") or "Unknown error",
                )

        # Terminal-event-less lax-server tolerance, armed ONLY by the
        # operator-declared ``finish_reason_optional`` capability: a
        # stream that ended cleanly after delivering output but never sent
        # ``response.completed``/``response.incomplete`` is a completed
        # generation on such a server (the retired non-streaming path
        # needed no terminal event).  The ``.done``-collected blocks ride
        # the shimmed finish chunk, exactly as they would the terminal
        # handler's.  Everywhere else the drain's complete-or-error gate
        # raises — a missing terminal on an event-disciplined server means
        # the generation died mid-response.
        if (
            finish_reason_optional
            and last_finish is None
            and (content_len or reasoning_len or tool_call_count)
        ):
            last_finish = "stop"
            sc = StreamChunk(finish_reason="stop")
            if provider_blocks:
                sc.provider_blocks = provider_blocks
            yield sc

        log.debug(
            "openai.responses.response",
            stream=True,
            finish_reason=last_finish,
            content_length=content_len,
            reasoning_length=reasoning_len,
            tool_call_count=tool_call_count,
            completion_tokens=completion_tokens,
        )

        # Emit accumulated citations as a final info chunk
        if annotations:
            citation_text = format_citations("", annotations).strip()
            if citation_text:
                yield StreamChunk(info_delta=citation_text)

    # -- tool conversion (public interface) ----------------------------------

    def convert_tools(
        self,
        tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return tools  # Conversion happens internally in _build_kwargs

    # -- retryable errors ----------------------------------------------------

    # Computed once at class creation — the retry predicate consults this
    # per error, and a per-access union allocates a fresh frozenset each time.
    _RETRYABLE_WITH_STREAM_FAILURES: frozenset[str] = RETRYABLE_ERROR_NAMES | {
        "ResponsesStreamFailedError"
    }

    @property
    def retryable_error_names(self) -> frozenset[str]:
        return self._RETRYABLE_WITH_STREAM_FAILURES

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
