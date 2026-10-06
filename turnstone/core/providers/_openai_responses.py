"""Responses API provider — for commercial OpenAI models.

Uses the OpenAI Responses API (``/v1/responses``) which natively supports
reasoning, tool use, web search, and tool search without the limitations
of the Chat Completions endpoint.
"""

from __future__ import annotations

import functools
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NamedTuple, NoReturn

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
    format_refusal,
    iter_with_cleanup,
    lookup_openai_capabilities,
    reject_non_stream_response,
    resolve_server_side_tools,
    sanitize_messages,
)
from turnstone.core.providers._protocol import (
    TRAILING_INFO_SEPARATOR,
    ModelCapabilities,
    ProviderRequestMetrics,
    StreamChunk,
    ToolCallDelta,
    _join_reasoning_with_cap,
    finish_shim_due,
    folds_trailing_info,
    refuse_aborted_request,
    refuse_credential_headers,
    request_uses_native_tools,
    resolve_reasoning_effort,
    serialized_tool_chars,
)
from turnstone.core.trajectory import materialize_attachments

log = structlog.get_logger(__name__)


# response.failed error codes that indicate a transient server-side
# condition — the only ones worth retrying (the API's other codes are
# deterministic request rejections).
_TRANSIENT_FAILURE_CODES = frozenset({"server_error", "rate_limit_exceeded"})

# Output item types that mean the server ran its own tool loop inside the
# response, sampling more than once, so the reported ``input_tokens`` is a sum
# across passes rather than the next request's context.  Hosted web search is
# the measured member (2.04x on a three-search turn) and the only server tool
# the session injects; the other hosted tools' usage shapes are #1194.
_SERVER_EXECUTED_ITEM_TYPES = frozenset({"web_search_call"})

# Adapter-owned replay metadata on the first native output item. The generic
# trajectory and storage preserve it opaquely; input projection never sends it.
_REASONING_CONFIG_KEY = "_reasoning_config"


class _HostedItem(NamedTuple):
    """How a stored hosted tool item replays as input: its SDK input shape."""

    tool: str  # the hosted tool that adds the item
    required: tuple[str, ...]  # fields the API rejects the item without (live 400)
    optional: tuple[str, ...]


# Hosted tool output items replayed as input, by item type. OpenAI documents replaying every
# output item of a stateless request, and accepted these in each shape probed live
# (2026-10-05, #1281): with and without reasoning replay, in any status, with the tool absent
# from the request, and with either half of a tool search alone. Ids stay off the wire: they
# are optional on input, and the API rejects an id it did not mint, which a row produced by
# another Responses endpoint but stored under the same producer can carry. A replayed search
# shows the model its query or opened page, not what it found: the API accepts ``results``
# and ``action.sources`` on input but leaves them out of the model's context, so neither is
# requested or replayed.
_HOSTED_ITEMS: dict[str, _HostedItem] = {
    "web_search_call": _HostedItem("web_search", (), ("status", "action")),
    "tool_search_call": _HostedItem(
        "tool_search", ("arguments",), ("status", "call_id", "execution")
    ),
    "tool_search_output": _HostedItem(
        "tool_search", ("tools",), ("status", "call_id", "execution")
    ),
}

# The input fields of each web search action type (the SDK input shape).
_WEB_SEARCH_ACTION_FIELDS: dict[str, tuple[str, ...]] = {
    "search": ("query", "queries"),
    "open_page": ("url",),
    "find_in_page": ("pattern", "url"),
}


@functools.lru_cache(maxsize=4096)
def _log_hosted_omission_once(item_type: str, item_id: str, omitted: str) -> None:
    """Log what a replayed hosted item leaves out, once per process.

    History keeps the stored item, so the same omission recurs on every request.
    """
    log.info(
        "openai.responses.hosted_item_omission",
        item_type=item_type,
        item_id=item_id,
        omitted=omitted,
    )


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


def _replay_reasoning_config(
    messages: list[dict[str, Any]],
    input_items: list[dict[str, Any]],
    assistant_item_ends: list[int],
    *,
    producer: str,
    model: str,
    effort: str | None,
    effort_values: tuple[str, ...],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Rebuild updates at response boundaries from the accepted effort history.

    Unrecorded, foreign and corrupt assistant turns start a new baseline; a
    compaction summary is naturally such a boundary. An omitted effort also
    resets the baseline: the API cannot express an unknown server default as
    an update. Current omission therefore uses ordinary request-level reasoning.
    """
    if effort is None:
        return effort, input_items
    assistants = [msg for msg in messages if msg.get("role") == "assistant"]
    if len(assistants) != len(assistant_item_ends):
        return effort, input_items
    history: list[tuple[int, str | None]] = []
    for ordinal, msg in enumerate(assistants):
        blocks = msg.get("_provider_content")
        config = (
            blocks[0].get(_REASONING_CONFIG_KEY)
            if isinstance(blocks, list) and blocks and isinstance(blocks[0], dict)
            else None
        )
        if (
            msg.get("_producer") != producer
            or not isinstance(config, dict)
            or config.get("model") != model
            or "effort" not in config
            or (config["effort"] is not None and config["effort"] not in effort_values)
        ):
            history.clear()
        else:
            if config["effort"] is None:
                history.clear()
            history.append((ordinal, config["effort"]))
    if not history:
        return effort, input_items

    initial_effort = previous_effort = history[0][1]
    updates: dict[int, str] = {}
    for ordinal, recorded_effort in history[1:]:
        if recorded_effort != previous_effort and recorded_effort is not None:
            updates[assistant_item_ends[ordinal - 1]] = recorded_effort
        previous_effort = recorded_effort
    if effort != previous_effort:
        updates[assistant_item_ends[-1]] = effort
    if not updates:
        return initial_effort, input_items

    # An assistant with only unreplayed reasoning can emit no input items.
    # Coalesce updates at the same item boundary so they can never be adjacent.
    replayed: list[dict[str, Any]] = []
    for index in range(len(input_items) + 1):
        if index in updates:
            replayed.append(
                {"type": "configuration_update", "reasoning": {"effort": updates[index]}}
            )
        if index < len(input_items):
            replayed.append(input_items[index])
    return initial_effort, replayed


def _record_reasoning_config(
    blocks: list[dict[str, Any]], config: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Keep request state with real output, so it cannot make an empty reply usable."""
    if not blocks or config is None:
        return blocks
    return [{**blocks[0], _REASONING_CONFIG_KEY: config}, *blocks[1:]]


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
        supports_mid_conversation_system: bool = False,
        native_producer: str = "openai",
        assistant_item_ends: list[int] | None = None,
        hosted_tools: frozenset[str] = frozenset(),
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert Chat Completions messages to Responses API input items.

        Returns ``(instructions, input_items)`` where *instructions* is the
        concatenated system/developer messages (or ``None``) and *input_items*
        is the Responses API ``input`` array. With native mid-conversation
        system support, only leading instructions are hoisted; later system
        and developer messages retain their role and position in ``input``.

        Native assistant message boundaries and ``phase`` survive when they
        still match canonical text and call order. Reasoning items are replayed
        only when *replay_reasoning_to_model* is True; phase is independent of
        that toggle. Explicitly foreign producer metadata is ignored, while
        untagged legacy native blocks retain shape-based replay.

        Output items of the hosted tools in *hosted_tools* (``web_search``,
        ``tool_search``) are replayed in their native positions, whatever the
        reasoning toggle. With tool search, each call to a tool that an earlier
        search loaded carries the tool's namespace (:func:`_namespace_loaded_calls`).

        ``assistant_item_ends`` records the converted end of each response so effort
        updates can be inserted before that response's subsequent user or tool input.
        """
        # Save native blocks before sanitization strips private fields. Key by
        # assistant ordinal: sanitizer repair inserts/drops tool results, so raw
        # message indices can shift, but assistants are never dropped/duplicated.
        native_by_assistant_ordinal: dict[int, list[dict[str, Any]]] = {}
        ord_pre = 0
        for raw_msg in messages:
            if raw_msg.get("role") != "assistant":
                continue
            pc = raw_msg.get("_provider_content")
            producer = raw_msg.get("_producer")
            if isinstance(pc, list) and (not producer or producer == native_producer):
                native_by_assistant_ordinal[ord_pre] = [b for b in pc if isinstance(b, dict)]
            ord_pre += 1

        # Skip PDF inlining: this lane has a native ``input_file`` block, so the
        # ``application/pdf`` document part must survive to ``convert_content_parts``
        # below.  Without this, ``sanitize_messages`` would replace it with an
        # unsupported-placeholder before the native translator ever runs.
        messages = sanitize_messages(messages, skip_pdf_inline=True)
        instructions_parts: list[str] = []
        items: list[dict[str, Any]] = []
        assistant_ordinal_post = 0

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content")

            if role in ("system", "developer"):
                instruction_text: list[str] = []
                if isinstance(content, str) and content:
                    instruction_text.append(content)
                elif isinstance(content, list):
                    # Content parts — extract text
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            instruction_text.append(part["text"])
                if instruction_text:
                    if supports_mid_conversation_system and items:
                        items.append(
                            {
                                "type": "message",
                                "role": role,
                                "content": "\n\n".join(instruction_text),
                            }
                        )
                    else:
                        instructions_parts.extend(instruction_text)
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
                items.extend(
                    _assistant_items_for_input(
                        msg,
                        native_by_assistant_ordinal.get(assistant_ordinal_post, []),
                        replay_reasoning_to_model=replay_reasoning_to_model,
                        hosted_tools=hosted_tools,
                    )
                )
                assistant_ordinal_post += 1
                if assistant_item_ends is not None:
                    assistant_item_ends.append(len(items))

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

        if "tool_search" in hosted_tools:
            _namespace_loaded_calls(items)
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

        assistant_item_ends: list[int] = []
        instructions, input_items = self._convert_messages(
            messages,
            replay_reasoning_to_model=replay_reasoning_to_model,
            supports_mid_conversation_system=caps.supports_mid_conversation_system,
            native_producer=self.provider_name,
            assistant_item_ends=assistant_item_ends,
            hosted_tools=self._replayed_hosted_tools(caps),
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
        if self._uses_reasoning_config_updates(caps, kwargs, reasoning):
            initial_effort, kwargs["input"] = _replay_reasoning_config(
                messages,
                input_items,
                assistant_item_ends,
                producer=self.provider_name,
                model=model,
                effort=effort,
                effort_values=caps.reasoning_effort_values,
            )
            if initial_effort is None:
                reasoning.pop("effort", None)
            else:
                reasoning["effort"] = initial_effort
        if reasoning:
            kwargs["reasoning"] = reasoning

        apply_verbosity(kwargs, caps)
        if not self._compat:
            apply_cache_retention(kwargs, model)
        return kwargs

    def _replayed_hosted_tools(self, caps: ModelCapabilities) -> frozenset[str]:
        """The hosted tools whose output items are replayed: each one the model can run.

        The model's capability, not this request's tool list, decides, so a history
        replays the same way when a request leaves a tool out. The replay is verified
        against OpenAI's own API only, so a compatible endpoint, which reports the same
        provider name, and a subclass serving another API replay none.
        """
        if self._compat or self.provider_name != "openai":
            return frozenset()
        tools = set(resolve_server_side_tools(caps))
        if caps.supports_tool_search:
            tools.add("tool_search")
        return frozenset(tools)

    def _uses_reasoning_config_updates(
        self, caps: ModelCapabilities, kwargs: dict[str, Any], reasoning: dict[str, Any]
    ) -> bool:
        """Only standard Responses requests use the configuration-update contract.

        This adapter never sends native multi-agent, automatic compaction or truncation
        parameters. Compatible endpoints keep their existing wire shape. Sampling
        parameters keep request-level effort so their acceptance still follows the
        request's explicit no-reasoning setting.
        """
        return (
            not self._compat
            and caps.supports_reasoning_config_updates
            and reasoning.get("mode") != "pro"
            and "temperature" not in kwargs
        )

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
        request_metrics_ref: list[ProviderRequestMetrics] | None = None,
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
            refuse_credential_headers(extra_headers)
            kwargs["extra_headers"] = extra_headers

        refuse_aborted_request(cancel_ref)
        if request_metrics_ref is not None:
            request_metrics_ref.append(
                ProviderRequestMetrics(
                    serialized_tool_chars=serialized_tool_chars(kwargs.get("tools")),
                    native_tools_enabled=request_uses_native_tools(kwargs),
                )
            )

        log.debug(
            "openai.responses.request",
            model=model,
            stream=True,
            max_tokens=max_tokens,
            input_items=len(kwargs.get("input", [])),
            tool_count=len(kwargs.get("tools", [])),
        )

        reasoning_config = (
            {
                "model": model,
                "effort": next(
                    (
                        item["reasoning"]["effort"]
                        for item in reversed(kwargs["input"])
                        if item.get("type") == "configuration_update"
                    ),
                    kwargs.get("reasoning", {}).get("effort"),
                ),
            }
            if self._uses_reasoning_config_updates(caps, kwargs, kwargs.get("reasoning", {}))
            else None
        )
        refuse_aborted_request(cancel_ref)
        stream = client.responses.create(**kwargs)
        reject_non_stream_response(stream, cancel_ref=cancel_ref)
        if cancel_ref is not None:
            cancel_ref.append(stream)
        return iter_with_cleanup(
            self._iter_stream(
                stream,
                finish_reason_optional=caps.finish_reason_optional,
                reasoning_config=reasoning_config,
            ),
            stream,
        )

    def _iter_stream(
        self,
        stream: Any,
        *,
        finish_reason_optional: bool = False,
        reasoning_config: dict[str, Any] | None = None,
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
        orphan_args_seen = False
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
                sc = StreamChunk(content_delta=format_refusal(refusal_text))
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
                    if item_id not in tool_call_indices:
                        # Orphan deltas ARE a streamed tool-call signal:
                        # without this flag the terminal harvest (gated on
                        # "no tool calls streamed") re-emits the same call
                        # onto the same slot and the arguments JSON
                        # duplicates.
                        orphan_args_seen = True
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
                                parts_text.append(format_refusal(part["refusal"]))
                    harvested = "".join(parts_text)
                    if harvested:
                        content_len = len(harvested)
                        hc = StreamChunk(content_delta=harvested)
                        if first:
                            hc.is_first = True
                            first = False
                        yield hc
                if tool_call_count == 0 and not orphan_args_seen:
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
                if usage is not None and any(
                    isinstance(block, dict) and block.get("type") in _SERVER_EXECUTED_ITEM_TYPES
                    for block in provider_blocks
                ):
                    # The hosted tool loop sampled more than once inside this
                    # response and ``input_tokens`` sums every pass (measured
                    # at 2.04x the next request's real input on a three-search
                    # turn), while ``cached_tokens`` counts one read and the
                    # search results are never replayed.  Only the completed
                    # event carries usage, so there is no opening pass to
                    # derive from: flag the counters cumulative and let
                    # ``resolve_context_usage`` fall back to the consumer's own
                    # estimate of what it sent.
                    usage = replace(usage, prompt_tokens_cumulative=True)
                sc = StreamChunk(
                    finish_reason=last_finish,
                    usage=usage,
                )
                if provider_blocks:
                    sc.provider_blocks = _record_reasoning_config(provider_blocks, reasoning_config)
                yield sc
                continue

            # -- in-band failure events --
            # ``error`` (ResponseErrorEvent): the SDK YIELDS these rather
            # than raising, and no response.failed necessarily follows —
            # without this branch the stream exhausts finish-less and the
            # real API message is lost behind a misleading
            # IncompleteStreamError.  ``response.failed`` carries the same
            # failure nested in its response payload.  ONE tail for both
            # shapes (only the code/message extraction differs), so the
            # same server failure can never become retryable through one
            # event type and fatal through the other: BEFORE a terminal
            # event the failure raises; AFTER one the generation is
            # complete and in hand — a trailing failure frame is teardown
            # noise (the in-band twin of the post-finish transport-blip
            # tolerance ``drain_stream`` grants), logged and dropped.
            if event_type in ("error", "response.failed"):
                if event_type == "error":
                    code = getattr(event, "code", "") or ""
                    message = getattr(event, "message", "") or ""
                else:
                    response = getattr(event, "response", None)
                    error = getattr(response, "error", None) if response else None
                    code = (getattr(error, "code", "") if error else "") or ""
                    message = (getattr(error, "message", "") if error else "") or ""
                if last_finish is not None:
                    log.warning("openai.responses.post_terminal_error", code=code, message=message)
                    break
                _raise_responses_failure(code, message or "Unknown error")

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
        # Orphan argument deltas count as delivered output exactly as they
        # count as a streamed tool-call signal for the terminal harvest —
        # a lax server that never announces items must not read as an
        # empty (failed) stream when its tool output actually arrived.
        if finish_shim_due(
            finish_reason_optional=finish_reason_optional,
            finish_seen=last_finish is not None,
            delivered_output=bool(
                content_len or reasoning_len or tool_call_count or orphan_args_seen
            ),
        ):
            last_finish = "stop"
            sc = StreamChunk(finish_reason="stop")
            if provider_blocks:
                sc.provider_blocks = _record_reasoning_config(provider_blocks, reasoning_config)
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


def _assistant_items_for_input(
    message: dict[str, Any],
    native: list[dict[str, Any]],
    *,
    replay_reasoning_to_model: bool,
    hosted_tools: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Recover native message phases/order only while canonical history agrees.

    A Turn joins all output messages into one text field. Native blocks retain
    their boundaries, but may predate edits, fence neutralization, or call repair.
    Match the text and call IDs before using that layout (a turn without text needs
    only its call IDs to match), and always construct calls from the lowered
    canonical fields. An ambiguous layout falls back to canonical text/calls with no
    guessed phase, plus the existing reasoning replay.

    Hosted tool items of *hosted_tools* replay alongside the reasoning: in native
    order, and ahead of the text and calls when the layout falls back. With tool
    search, a call also carries the namespace of the native call with its ID. Other
    output items stay omitted.
    """
    content = message.get("content")
    namespaces: dict[str, str] = {}
    if "tool_search" in hosted_tools:
        for block in native:
            call_id, namespace = block.get("call_id"), block.get("namespace")
            if (
                block.get("type") == "function_call"
                and isinstance(call_id, str)
                and isinstance(namespace, str)
                and call_id
                and namespace
            ):
                namespaces[call_id] = namespace
    calls: list[dict[str, Any]] = []
    for tc in message.get("tool_calls") or []:
        call = {
            "type": "function_call",
            "call_id": tc.get("id", ""),
            "name": tc.get("function", {}).get("name", ""),
            "arguments": tc.get("function", {}).get("arguments", ""),
        }
        if call["call_id"] in namespaces:
            call["namespace"] = namespaces[call["call_id"]]
        calls.append(call)
    # Reasoning and hosted tool items, in native order: the fallback layout keeps them
    # ahead of its text and calls.
    prelude: list[dict[str, Any]] = []
    ordered: list[dict[str, Any]] = []
    text_items: list[dict[str, Any]] = []
    native_call_ids: list[str] = []
    annotations: list[Any] = []
    valid_layout = isinstance(content, str)
    for block in native:
        kind = block.get("type")
        if kind == "reasoning" and replay_reasoning_to_model:
            item = _reasoning_item_for_input(block)
            if item is not None:
                prelude.append(item)
                ordered.append(item)
        elif (hosted := _hosted_item_for_input(block, hosted_tools)) is not None:
            prelude.append(hosted)
            ordered.append(hosted)
        elif kind == "function_call":
            if len(native_call_ids) < len(calls):
                ordered.append(calls[len(native_call_ids)])
            native_call_ids.append(block.get("call_id", ""))
        elif kind == "message":
            parts = block.get("content")
            if block.get("role") != "assistant" or not isinstance(parts, list):
                valid_layout = False
                continue
            texts: list[str] = []
            for part in parts:
                if not isinstance(part, dict):
                    valid_layout = False
                elif part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    texts.append(part["text"])
                    annotations.extend(
                        SimpleNamespace(**ann)
                        for ann in part.get("annotations") or []
                        if isinstance(ann, dict)
                    )
                elif part.get("type") == "refusal" and isinstance(part.get("refusal"), str):
                    texts.append(format_refusal(part["refusal"]))
                else:
                    valid_layout = False
            text = "".join(texts)
            if text:
                projected = {"type": "message", "role": "assistant", "content": text}
                if block.get("phase") in ("commentary", "final_answer"):
                    projected["phase"] = block["phase"]
                text_items.append(projected)
                ordered.append(projected)

    if (
        valid_layout
        and (text_items or not content)
        and native_call_ids == [call["call_id"] for call in calls]
    ):
        native_text = "".join(item["content"] for item in text_items)
        if content == native_text:
            return ordered
        # Streaming appends a deduplicated citation footer to the canonical text.
        # Recognize that exact transform; an arbitrary appended edit is ambiguous.
        footer = format_citations("", annotations).strip()
        if (
            footer
            and folds_trailing_info(native_text)
            and content == native_text + TRAILING_INFO_SEPARATOR + footer
        ):
            text_items[-1]["content"] += TRAILING_INFO_SEPARATOR + footer
            return ordered

    if content:
        prelude.append({"type": "message", "role": "assistant", "content": content})
    return prelude + calls


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


def _hosted_item_for_input(
    stored: dict[str, Any], hosted_tools: frozenset[str]
) -> dict[str, Any] | None:
    """Project a stored hosted tool item into its input shape, or None when it is not replayed.

    The stored item is the SDK's ``model_dump``: absent fields are None, and the first
    output item can carry this adapter's ``_reasoning_config``. Only the item's input
    fields go back, without their Nones. An item missing a field the API requires is
    omitted, and a search action of a type the API does not know is left off its item;
    each omission is logged, so the replayed record never shrinks silently.
    """
    kind = stored.get("type")
    spec = _HOSTED_ITEMS.get(kind) if isinstance(kind, str) else None
    if spec is None or spec.tool not in hosted_tools:
        return None
    item_id = stored.get("id") if isinstance(stored.get("id"), str) else ""
    for field in spec.required:
        if stored.get(field) is None:
            _log_hosted_omission_once(kind, item_id, f"the item, which has no {field}")
            return None
    item: dict[str, Any] = {"type": kind}
    for field in (*spec.required, *spec.optional):
        if stored.get(field) is not None:
            item[field] = stored[field]
    if kind == "web_search_call" and "action" in item:
        action = _web_search_action_for_input(item["action"])
        if action is None:
            del item["action"]
            _log_hosted_omission_once(kind, item_id, "its action, of an unknown type")
        else:
            item["action"] = action
    elif kind == "tool_search_output":
        if not isinstance(item["tools"], list):
            _log_hosted_omission_once(kind, item_id, "the item, whose tools are not a list")
            return None
        item["tools"] = [_tool_for_input(tool) for tool in item["tools"]]
    return item


def _web_search_action_for_input(action: Any) -> dict[str, Any] | None:
    """Project a web search action into its input shape, or None for a type the API lacks."""
    action_type = action.get("type") if isinstance(action, dict) else None
    fields = _WEB_SEARCH_ACTION_FIELDS.get(action_type) if isinstance(action_type, str) else None
    if fields is None:
        return None
    projected = {"type": action_type}
    for field in fields:
        if action.get(field) is not None:
            projected[field] = action[field]
    return projected


def _tool_for_input(tool: Any) -> Any:
    """Project a tool definition a tool search loaded into its input shape.

    ``model_dump`` spells the ``async`` field ``async_`` and writes absent fields as None.
    A namespace's own tools are projected the same way.
    """
    if not isinstance(tool, dict):
        return tool
    out = {
        ("async" if key == "async_" else key): value
        for key, value in tool.items()
        if value is not None
    }
    if isinstance(out.get("tools"), list):
        out["tools"] = [_tool_for_input(member) for member in out["tools"]]
    return out


def _namespace_loaded_calls(items: list[dict[str, Any]]) -> None:
    """Give each call to a tool that an earlier tool search loaded the tool's namespace.

    Once a ``tool_search_output`` has loaded a deferred tool, the API rejects a later call
    to it that has no namespace, unless the tool is also offered undeferred (live probe,
    2026-10-05). A native call keeps its own namespace. A call without one, from another
    producer's turn or made while the tool was offered undeferred, takes the namespace the
    load gave it: a function loaded on its own is its own namespace, and a namespace's
    functions take the namespace's name. Calls before the load are left as they are.

    ``items`` is rewritten in place, one item for one, so the response boundaries that
    ``assistant_item_ends`` recorded still hold.
    """
    loaded: dict[str, str] = {}
    for index, item in enumerate(items):
        if item.get("type") == "tool_search_output":
            for tool in item.get("tools") or []:
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                    continue
                if tool.get("type") == "function":
                    loaded[tool["name"]] = tool["name"]
                elif tool.get("type") == "namespace":
                    for member in tool.get("tools") or []:
                        if isinstance(member, dict) and isinstance(member.get("name"), str):
                            loaded[member["name"]] = tool["name"]
        elif item.get("type") == "function_call" and "namespace" not in item:
            namespace = loaded.get(item.get("name", ""))
            if namespace:
                items[index] = {**item, "namespace": namespace}
