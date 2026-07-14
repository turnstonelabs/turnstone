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
    finish_shim_due,
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


class _ArgsScanner:
    """Incremental JSON-completeness state over streamed argument fragments.

    Tracks bracket depth and string state character-by-character so the
    slotter can ask, at any fragment boundary, whether the arguments
    accumulated so far form a syntactically CLOSED value.  Arguments are
    JSON objects per the tool-call spec, so depth-tracking suffices; a
    server streaming non-JSON arguments never reads complete, which
    degrades the slotter to plain per-index accumulation (the pre-slotter
    behavior) for that slot.
    """

    __slots__ = ("depth", "escaped", "in_string", "saw_token")

    def __init__(self) -> None:
        self.depth = 0
        self.in_string = False
        self.escaped = False
        self.saw_token = False

    def feed(self, fragment: str) -> None:
        for ch in fragment:
            if self.in_string:
                if self.escaped:
                    self.escaped = False
                elif ch == "\\":
                    self.escaped = True
                elif ch == '"':
                    self.in_string = False
                continue
            if ch in " \t\r\n":
                continue
            self.saw_token = True
            if ch == '"':
                self.in_string = True
            elif ch in "{[":
                self.depth += 1
            elif ch in "}]":
                self.depth -= 1

    @property
    def complete(self) -> bool:
        return self.saw_token and self.depth <= 0 and not self.in_string


class ToolCallSlotter:
    """Wire-index → logical-slot assignment for streamed tool-call deltas.

    One slotter (the base iterator's) drives both the normalized
    ``tool_calls`` mirror and ``GoogleProvider``'s raw fidelity capture
    (via the ``on_tool_call_delta`` hook), so the two lanes assign call
    identity identically by construction.  Index-degenerate compat
    servers (historical vLLM/llama.cpp builds) emit every parallel call
    at index 0; ids, names, and argument JSON completeness decide where
    one call ends and the next begins:

    - A delta whose id CONTRADICTS the slot's id is a new call; a delta
      whose id MATCHES is the same call, however many times the server
      repeats the name header per fragment.  Ids are authoritative both
      ways — and a slot whose id is KNOWN never splits on an id-less
      delta at all: on an id-disciplined server new calls arrive with
      ids, so an id-less fragment (the call's first name announcement,
      an argument fragment, a redundant header) is always a
      continuation.  All heuristics below apply only to fully id-less
      slots.
    - A delta with NO name is always an arguments continuation.
    - A name arriving for a slot that has NO name yet is the call's
      FIRST announcement (e.g. the slot was opened by an args-only
      delta), never a new call.
    - A delta announcing a DIFFERENT name than the slot's is a new call
      (two functions cannot be one call).
    - A re-announcement of the SAME name splits only when the slot's
      accumulated arguments are syntactically complete JSON AND the
      delta itself carries arguments (the next parallel call arriving
      with its payload).  Mid-JSON, or as a bare trailing delta after
      complete arguments, the name is a redundant per-fragment header /
      footer and the delta merges.  With NO arguments accumulated yet:
      a re-announce that carries arguments merges (name-first emission,
      the arguments are starting now); a bare re-announce splits (the
      id-less whole-delta shape — two zero-argument parallel calls).

    Residual ambiguity, resolved toward the shapes observed in the wild:
    a same-name re-announce on an empty slot that carries arguments is
    read as one call (not a zero-arg call followed by an arg-ful twin),
    and a bare same-name re-announce after complete arguments is read
    as a redundant footer (not a second zero-arg call of the same
    function).  Only ids disambiguate those; servers that omit them
    choose the bet.
    """

    def __init__(self) -> None:
        self._slot_for_index: dict[int, int] = {}
        self._slot_ids: dict[int, str] = {}
        self._slot_names: dict[int, str] = {}
        self._slot_args: dict[int, _ArgsScanner] = {}
        self._next_slot = 0

    def slot_for(self, wire_index: int, tc_id: str, name: str, args: str) -> int:
        slot = self._slot_for_index.get(wire_index)
        if slot is None or self._is_new_call(slot, tc_id, name, args):
            slot = self._next_slot
            self._next_slot += 1
            self._slot_for_index[wire_index] = slot
        if tc_id:
            self._slot_ids[slot] = tc_id
        if name:
            self._slot_names[slot] = name
        # JSON-completeness state is consulted only for fully id-less
        # slots (``_is_new_call`` short-circuits on a known id), so id'd
        # slots — the dominant case — skip the per-character scan.
        if args and slot not in self._slot_ids:
            self._slot_args.setdefault(slot, _ArgsScanner()).feed(args)
        return slot

    def _is_new_call(self, slot: int, tc_id: str, name: str, args: str) -> bool:
        slot_id = self._slot_ids.get(slot, "")
        if tc_id:
            return bool(slot_id) and tc_id != slot_id
        if slot_id:
            # Id-disciplined slot: new calls on this server arrive with
            # ids (the id-conflict rule above), so an id-less delta —
            # the call's first name fragment, an argument fragment, a
            # redundant header — is always a continuation.
            return False
        if not name:
            return False
        slot_name = self._slot_names.get(slot, "")
        if not slot_name:
            # First name announcement for a slot opened by an args-only
            # delta — naming the call, not starting a new one.
            return False
        if name != slot_name:
            return True
        scanner = self._slot_args.get(slot)
        if scanner is not None:
            # Complete arguments end the call — but only a re-announce
            # that itself CARRIES arguments starts the next one; a bare
            # same-name delta after complete arguments is a redundant
            # footer.
            return scanner.complete and bool(args)
        return not args


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
        return self._iter_stream(stream, finish_reason_optional=caps.finish_reason_optional)

    def _iter_stream(
        self,
        stream: Any,
        *,
        finish_reason_optional: bool = False,
        on_tool_call_delta: Callable[[ToolCallDelta, Any], None] | None = None,
    ) -> Iterator[StreamChunk]:
        """Convert OpenAI Chat Completions stream chunks to StreamChunks.

        *finish_reason_optional* is the model capability of the same name:
        it arms the lax-server finish shim at the end of this generator.

        *on_tool_call_delta* is called with ``(normalized, raw_sdk_delta)``
        for every tool-call delta — the normalized ``ToolCallDelta``
        carries the slot the base's slotter assigned AND the base's
        id/name/arguments extraction, so a subclass capturing provider
        extras (``GoogleProvider``'s ``thought_signature``) accumulates
        the exact bytes the ``tool_calls`` mirror sees and cannot desync
        from it on either axis.
        """
        first = True
        annotations: list[Any] = []
        content_len = 0
        reasoning_len = 0
        tool_call_count = 0
        last_finish_reason: str | None = None
        completion_tokens: int | None = None
        # Remap wire indexes onto logical slots (see ToolCallSlotter) so
        # downstream accumulators (drain_stream, the chat loop) can key by
        # index safely even on index-degenerate compat servers.
        slotter = ToolCallSlotter()
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
                reasoning_len += len(rc)

            # Content
            if delta.content:
                sc.content_delta = delta.content
                content_len += len(delta.content)

            # Tool calls
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    tc_id = tc_delta.id or ""
                    fn = tc_delta.function
                    name = (fn.name if fn else None) or ""
                    args = (fn.arguments if fn else None) or ""
                    slot = slotter.slot_for(tc_delta.index, tc_id, name, args)
                    tcd = ToolCallDelta(index=slot)
                    if tc_id:
                        tcd.id = tc_id
                    if name:
                        tcd.name = name
                    if args:
                        tcd.arguments_delta = args
                    if on_tool_call_delta is not None:
                        on_tool_call_delta(tcd, tc_delta)
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

        # Finish-reason-less lax-server tolerance (the deleted non-streaming
        # path's `finish_reason or "stop"` default), armed ONLY by the
        # operator-declared ``finish_reason_optional`` capability: on a
        # server that never sends finish reasons, a stream that ended
        # CLEANLY — the SDK ends iteration on [DONE]; an abrupt connection
        # death raises httpx.TransportError out of this generator — after
        # delivering output is a completed generation.  Everywhere else a
        # clean finish-less end is indistinguishable from a generation
        # that died behind a clean-closing proxy/ASGI layer, so the shim
        # stays DISARMED and the drain's complete-or-error gate raises
        # (retryable) instead of blessing possibly-truncated text.
        # Reasoning counts as delivered output — a thinking model that
        # spent its budget before emitting content is still a completed
        # generation (the retired non-streaming path returned it with
        # empty content).  Emit the finish BEFORE the citation footer so
        # the drain folds the footer as trailing info.  A clean-exhaustion
        # stream with NO output still yields nothing, so dead/empty
        # streams keep failing the gate even when the shim is armed.
        if finish_shim_due(
            finish_reason_optional=finish_reason_optional,
            finish_seen=last_finish_reason is not None,
            delivered_output=bool(content_len or reasoning_len or tool_call_count),
        ):
            last_finish_reason = "stop"
            yield StreamChunk(finish_reason="stop")

        log.debug(
            "openai.chat.response",
            stream=True,
            finish_reason=last_finish_reason,
            content_length=content_len,
            reasoning_length=reasoning_len,
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
