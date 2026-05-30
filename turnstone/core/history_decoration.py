"""Shared history-replay projection + decoration helpers.

The REST ``make_history_handler`` (``GET /history``) endpoint is the
single surface that builds the history wire payload for both kinds. Its
pipeline composes the helpers in this module: load the audit-trail
indexes, :func:`decorate_history_messages` (attach the persisted intent
verdict from ``intent_verdicts`` + output-guard assessment from
``output_assessments`` to each ``tool_calls`` entry, strip string
``<tool_output>`` envelopes), :func:`extract_reasoning_for_history`
(surface stored reasoning), then :func:`project_history_messages` (the
final structural projection both UIs — interactive ``replayHistory``
and the coordinator dashboard — consume directly).

Centralising the projection here is what keeps the wire shape single-
sourced: the helpers project only the fields the UI renders, dropping
redundant ones (``call_id``/``func_name`` already carried on
``tc.id``/``tc.name``) so the wire payload stays tight.

All functions are pure I/O or pure transforms — safe to call from
either an async caller (via ``asyncio.to_thread``) or a sync hook.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger
from turnstone.core.tool_advisory import (
    _USER_INTERJECTION_BODY_MARKER,
    _USER_INTERJECTION_IMPORTANT_PREAMBLE,
    _USER_INTERJECTION_NOTICE_PREAMBLE,
)
from turnstone.core.watch import WATCH_REMINDER_OPTIONAL_KEYS

log = get_logger(__name__)


def load_verdict_indexes(
    ws_id: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Bulk-load intent verdicts and output assessments for a workstream.

    Returns ``(verdicts_by_call_id, assessments_by_call_id)``.

    ``verdicts_by_call_id`` keeps the newest intent verdict per call_id
    (DESC ordering + first-seen-wins; LLM upgrade beats heuristic).

    ``assessments_by_call_id`` maps call_id → a SLOT
    ``{"heuristic": row|None, "llm": row|None}`` holding the newest row of
    each tier, because the output-guard chip is a MERGE of the two
    detectors (issue #560, "show, annotated").  Failed-judge rows
    (``tier="llm_error"``) are dropped here so a risk="none" failure row
    can never shadow a real heuristic finding on reconnect — they are
    audit-only and surface via the ``/v1/api/admin/output-assessments``
    list, not the inline chip.

    Pure storage I/O — safe to run in ``asyncio.to_thread`` from an
    async caller.  Returns empty dicts when storage is unavailable or
    the lookup raises (best-effort: replay must never block on
    audit-trail decoration).
    """
    verdicts_by_call_id: dict[str, dict[str, Any]] = {}
    assessments_by_call_id: dict[str, dict[str, Any]] = {}
    if not ws_id:
        return verdicts_by_call_id, assessments_by_call_id
    try:
        from turnstone.core.storage._registry import get_storage

        storage = get_storage()
        if storage is None:
            return verdicts_by_call_id, assessments_by_call_id
        for v in storage.list_intent_verdicts(ws_id=ws_id, limit=10000):
            cid = v.get("call_id") or ""
            if cid and cid not in verdicts_by_call_id:
                verdicts_by_call_id[cid] = v
        for a in storage.list_output_assessments(ws_id=ws_id, limit=10000):
            cid = a.get("call_id") or ""
            if not cid:
                continue
            tier = a.get("tier", "heuristic")
            if tier == "llm_error":
                continue  # audit-only failure row — never the acted UI finding
            # KNOWN LIMITATION (historical data only): rows written BEFORE the
            # llm_error split recorded judge FAILURES as tier="llm" risk="none"
            # too, so on replay of pre-split workstreams such a row lands in the
            # "llm" slot and the chip mis-renders it as a successful "none"
            # verdict (spurious "⚖ LLM: none" + the error string as rationale).
            # The heuristic finding still SURVIVES the max-merge — the chip
            # never vanishes — so this is cosmetic.  We can't fingerprint it
            # safely (a legitimate benign verdict is also tier="llm" risk="none"),
            # and new failures self-heal under "llm_error".
            slot = assessments_by_call_id.setdefault(cid, {"heuristic": None, "llm": None})
            key = "llm" if tier == "llm" else "heuristic"
            if slot.get(key) is None:  # first-seen wins (rows arrive newest-first)
                slot[key] = a
    except Exception:
        # Missing storage / migration drift / driver error must not
        # block replay — degrade to an unannotated history.
        log.debug(
            "verdict/assessment lookup failed; replay continues unannotated",
            exc_info=True,
        )
    return verdicts_by_call_id, assessments_by_call_id


def build_verdict_payload(vrow: dict[str, Any]) -> dict[str, Any] | None:
    """Project a stored ``intent_verdicts`` row into the wire shape.

    Returns ``None`` when the verdict is the unflagged baseline
    (``risk_level == "none"``) — the client's ``renderVerdictBadge``
    helper would suppress those anyway, so skipping at the wire layer
    keeps the payload tight on long workstreams.

    Drops ``call_id`` and ``func_name`` from the wire payload — they're
    already carried on the parent ``tc.id`` / ``tc.name`` fields.
    Ships ``reasoning`` for either tier when the row has non-empty
    prose (heuristic rules in this project DO write meaningful
    rationales — e.g. ``policy.py`` emits structured reasoning per
    matched pattern). ``judge_model`` rides through so the batch tier
    badge can render ``⚖ llm:claude-haiku-4`` on history-only batches
    rather than the bare ``⚖ llm`` label.
    """
    if (vrow.get("risk_level") or "none") == "none":
        return None
    payload: dict[str, Any] = {
        "risk_level": vrow.get("risk_level", "medium"),
        "recommendation": vrow.get("recommendation", "review"),
        "confidence": vrow.get("confidence", 0.0),
        "intent_summary": vrow.get("intent_summary", ""),
        "tier": vrow.get("tier", "heuristic"),
    }
    if vrow.get("reasoning"):
        payload["reasoning"] = vrow.get("reasoning", "")
    judge_model = vrow.get("judge_model") or ""
    if judge_model:
        payload["judge_model"] = judge_model
    return payload


def _decode_json_list(raw: Any) -> list[str]:
    """Decode a stored JSON-string list (``flags`` / ``annotations``) into a list.

    Falls back to an empty list on bad JSON rather than raising — the rest
    of the assessment is still useful.
    """
    if raw is None:
        return []
    try:
        decoded = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return []
    return decoded if isinstance(decoded, list) else []


def build_merged_output_assessment_payload(slot: dict[str, Any]) -> dict[str, Any] | None:
    """Project a stored heuristic+LLM assessment pair into the chip payload.

    ``slot`` is ``{"heuristic": row|None, "llm": row|None}`` from
    :func:`load_verdict_indexes` — the ``llm`` slot holds the judge's OWN
    successful verdict; failed-judge rows were dropped upstream so they
    cannot shadow a heuristic finding.

    Delegates the actual merge to
    :func:`output_guard.merge_guard_display_payload` — the SAME projection
    the live ``on_output_warning`` path calls — so the inline finding chip
    renders identically live and on reconnect.  Returns ``None`` (skip on
    clean) when neither detector flagged and nothing was redacted.
    """
    from turnstone.core.output_guard import merge_guard_display_payload

    heuristic = slot.get("heuristic") or {}
    llm = slot.get("llm")
    return merge_guard_display_payload(
        heuristic_risk=heuristic.get("risk_level", "none") or "none",
        heuristic_flags=_decode_json_list(heuristic.get("flags")),
        heuristic_annotations=_decode_json_list(heuristic.get("annotations")),
        redacted=bool(heuristic.get("redacted", 0)),
        llm_succeeded=llm is not None,
        llm_risk=(llm or {}).get("risk_level", "none") or "none",
        llm_flags=_decode_json_list((llm or {}).get("flags")),
        llm_reasoning=(llm or {}).get("reasoning", "") or "",
        llm_confidence=(llm or {}).get("confidence", 0.0) or 0.0,
        llm_model=(llm or {}).get("judge_model", "") or "",
    )


def decorate_tool_call(
    tc: dict[str, Any],
    verdicts_by_call_id: dict[str, dict[str, Any]],
    assessments_by_call_id: dict[str, dict[str, Any]],
) -> None:
    """Mutate ``tc`` in place, attaching ``verdict`` / ``output_assessment``.

    Reads only ``id`` (top-level on every tool_call shape), so it works on
    either the OpenAI-nested ``{id, function: {name, arguments}}`` shape —
    what ``decorate_history_messages`` passes from the REST ``/history``
    pipeline — or a flattened ``{id, name, arguments}`` shape.  No-ops
    cleanly when the call_id has no matching row (unflagged tools stay
    clean).
    """
    call_id = tc.get("id", "") or ""
    if not call_id:
        return
    vrow = verdicts_by_call_id.get(call_id)
    if vrow is not None:
        verdict = build_verdict_payload(vrow)
        if verdict is not None:
            tc["verdict"] = verdict
    slot = assessments_by_call_id.get(call_id)
    if slot is not None:
        assessment = build_merged_output_assessment_payload(slot)
        if assessment is not None:
            tc["output_assessment"] = assessment


def _entity_decode_wrapper_tags(text: str) -> str:
    """Reverse :func:`tool_advisory.escape_wrapper_tags` on extraction.

    The wrap layer escapes the four wrapper-tag forms to HTML entities
    so embedded user / advisory text cannot fabricate or close an
    envelope.  When the replay decorator pulls advisories back out of
    the persisted envelope, the inner text needs to be returned to its
    literal form for UI rendering.

    Decodes ``&amp;`` last so a tool output that contains the literal
    string ``&lt;tool_output&gt;`` round-trips identically to its
    source: encode produces ``&amp;lt;tool_output&amp;gt;`` (no
    collision with wrapper-tag escapes), decode walks the wrapper
    escapes first, then strips the ``&amp;`` sentinel back to ``&``.
    The short-circuit on ``"&" not in text`` covers the common case
    where no escaped entities are present.
    """
    if "&" not in text:
        return text
    return (
        text.replace("&lt;/tool_output&gt;", "</tool_output>")
        .replace("&lt;tool_output&gt;", "<tool_output>")
        .replace("&lt;system-reminder&gt;", "<system-reminder>")
        .replace("&lt;/system-reminder&gt;", "</system-reminder>")
        .replace("&amp;", "&")
    )


def _classify_advisory(render_text: str) -> dict[str, str] | None:
    """Map a ``<system-reminder>`` body back to a wire-shape advisory.

    Returns a dict with ``type`` / ``text`` / optional ``priority`` for
    advisory shapes the UI knows how to render, or ``None`` to suppress
    the advisory entirely (output-guard findings already render via the
    ``output_assessment`` audit-table decoration; doubling them would
    paint two warning bubbles).  Unknown advisory shapes fall through
    to ``None`` rather than rendering an opaque envelope blob.
    """
    if render_text.startswith("Output guard:"):
        return None
    if _USER_INTERJECTION_BODY_MARKER in render_text:
        # UserInterjection is the only producer that uses this marker.
        # The preamble disambiguates priority: "important" gets the
        # MUST-address framing, "notice" gets the incorporate-if-relevant
        # framing.  The body sits after the marker.  Preamble + marker
        # constants are imported from ``tool_advisory`` so the parser
        # and producer can never drift on wording.
        if render_text.startswith(_USER_INTERJECTION_IMPORTANT_PREAMBLE):
            priority = "important"
        elif render_text.startswith(_USER_INTERJECTION_NOTICE_PREAMBLE):
            priority = "notice"
        else:
            # Marker present but preamble drifted — still render as a
            # notice rather than dropping the user's text.
            priority = "notice"
        body = render_text.split(_USER_INTERJECTION_BODY_MARKER, 1)[1]
        # Suppress empty/whitespace-only advisories — ``queue_message``
        # accepts any non-None text including ``""`` / ``"   "``, and a
        # blank body would paint a featureless empty user bubble on
        # replay.  Dropping at the classifier keeps the wire-shape
        # contract uniform (no empty advisories ever ride the wire).
        if not body.strip():
            return None
        return {"type": "user_interjection", "text": body, "priority": priority}
    return None


def extract_advisories_from_tool_envelope(
    content: str,
) -> tuple[str, list[dict[str, str]]] | None:
    """Strip a ``<tool_output>`` envelope and return ``(clean, advisories)``.

    Returns ``None`` when *content* doesn't look like a wrapped tool
    result — caller should leave the message unchanged.  When the
    envelope parses but no advisories survive classification (e.g.
    only an output_guard advisory rode along), returns the cleaned
    output with an empty advisories list — the caller still needs to
    strip the envelope from the rendered content.
    """
    if not content.startswith("<tool_output>\n"):
        return None
    close = content.find("\n</tool_output>")
    if close == -1:
        return None
    inner = content[len("<tool_output>\n") : close]
    rest = content[close + len("\n</tool_output>") :]
    advisories: list[dict[str, str]] = []
    cursor = 0
    while True:
        open_idx = rest.find("<system-reminder>\n", cursor)
        if open_idx == -1:
            break
        close_idx = rest.find("\n</system-reminder>", open_idx)
        if close_idx == -1:
            break
        body = rest[open_idx + len("<system-reminder>\n") : close_idx]
        decoded = _entity_decode_wrapper_tags(body)
        classified = _classify_advisory(decoded)
        if classified is not None:
            advisories.append(classified)
        cursor = close_idx + len("\n</system-reminder>")
    return _entity_decode_wrapper_tags(inner), advisories


if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.providers._protocol import LLMProvider


def _make_provider_factory(module_path: str, class_name: str) -> Callable[[], LLMProvider]:
    """Build a thread-unsafe lazy-init factory for a provider singleton.

    Each block-type entry in ``_BLOCK_TYPE_PROVIDER_FACTORY`` closes
    over its own (module_path, class_name) pair.  Adding a fourth
    provider is a single tuple in the dict, not a new 9-line getter.

    Uses ``nonlocal`` instead of ``functools.lru_cache`` so the cache
    state stays inside this closure (lru_cache would attach state to
    the inner function object, which is correct but adds a per-call
    hash lookup on a bound key for what's effectively a single-slot
    cache).
    """
    instance: LLMProvider | None = None

    def factory() -> LLMProvider:
        nonlocal instance
        if instance is None:
            import importlib

            module = importlib.import_module(module_path)
            instance = getattr(module, class_name)()
        return instance

    return factory


# Block-type → provider factory.  Routing is structural — block shape
# is non-overlapping across providers by API design.  Recognised
# block types today:
#
# * ``"thinking"`` — Anthropic native (Phase 1).  Walks the
#   ``thinking`` field on each block.
# * ``"redacted_thinking"`` — Anthropic native (Phase 1).  Anthropic's
#   safety system rewrites a thinking block into a sealed
#   ``redacted_thinking`` block; the Anthropic docs note these can
#   appear before, after, or interleaved with regular ``thinking``
#   blocks.  Same factory: AnthropicProvider's extractor walks the
#   full block list and filters to ``type == "thinking"``, so the
#   redacted blocks are correctly skipped while the surrounding
#   real thinking text still surfaces.
# * ``"reasoning"`` — OpenAI Responses native (Phase 3).  Walks
#   ``summary[*].text`` (always present) and ``content[*].text``
#   (present when ``include=["reasoning.encrypted_content"]`` is
#   requested AND the response carries raw reasoning text).
# * ``"reasoning_text"`` — synthetic, stamped by
#   ``ChatSession._maybe_synth_reasoning_block`` for Chat Completions
#   paths (vLLM, llama.cpp, Gemini-compat) where reasoning surfaces
#   only as ``reasoning_delta`` chunks with no native block shape.
_anthropic_factory = _make_provider_factory(
    "turnstone.core.providers._anthropic", "AnthropicProvider"
)
_BLOCK_TYPE_PROVIDER_FACTORY: dict[str, Callable[[], LLMProvider]] = {
    "thinking": _anthropic_factory,
    "redacted_thinking": _anthropic_factory,
    "reasoning": _make_provider_factory(
        "turnstone.core.providers._openai_responses", "OpenAIResponsesProvider"
    ),
    "reasoning_text": _make_provider_factory(
        "turnstone.core.providers._openai_chat", "OpenAIChatCompletionsProvider"
    ),
}


def extract_reasoning_text_from_provider_content(provider_content: Any) -> str:
    """Dispatch reasoning extraction by scanning for a recognised block type.

    Walks ``provider_content`` looking for the first block whose
    ``type`` is in :data:`_BLOCK_TYPE_PROVIDER_FACTORY`, then dispatches
    the WHOLE list to that provider's ``extract_reasoning_text``.  Each
    provider's extractor already filters internally by its own block
    type (Anthropic walks ``thinking``, OpenAI Responses walks
    ``reasoning``, OpenAI Chat walks ``reasoning_text``), so passing
    the full list is correct — interleaved foreign blocks are ignored.

    Returns ``""`` for empty / missing / non-list input or when no
    recognised reasoning-bearing block type appears anywhere in the
    list.

    Why scan instead of just inspecting ``provider_content[0]``: the
    OpenAI Responses streaming layer captures EVERY ``output_item.done``
    event into ``provider_blocks`` (``_openai_responses.py:415-420``),
    not just reasoning items.  In practice the order is usually
    ``[reasoning, message, ...]`` but the API doesn't guarantee that —
    a hypothetical ``[message, reasoning]`` ordering would silently
    drop the reasoning under an index-only check.  Same robustness
    point for Anthropic's hypothetical mixed-order outputs.

    Pure transform — safe from any thread.  The REST ``/history``
    reasoning surfacing (:func:`extract_reasoning_for_history`) calls
    this directly.  See ``_BLOCK_TYPE_PROVIDER_FACTORY`` above for the
    recognised block types and the providers that own them.
    """
    if not isinstance(provider_content, list) or not provider_content:
        return ""
    for block in provider_content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if not isinstance(block_type, str):
            continue
        factory = _BLOCK_TYPE_PROVIDER_FACTORY.get(block_type)
        if factory is not None:
            return factory().extract_reasoning_text(provider_content)
    return ""


def extract_reasoning_for_history(
    messages: list[dict[str, Any]],
    surface_persisted_reasoning_flag: bool,
) -> None:
    """Surface stored reasoning text on each assistant message; strip the
    raw provider content from the wire payload.

    For the ``make_history_handler`` REST path where the response
    payload IS the messages list returned from ``storage.load_messages``
    — both extraction source and stamp destination are the same dict.
    Walks *messages* in place: for every assistant message, dispatches
    via :func:`extract_reasoning_text_from_provider_content` and stamps
    ``msg["reasoning"]`` when *surface_persisted_reasoning_flag* is True and the
    dispatcher returned non-empty text.  Strips ``_provider_content``
    unconditionally — the field is internal and never read by either UI.

    Runs before :func:`project_history_messages` in the ``/history``
    pipeline: this helper stamps ``msg["reasoning"]`` (and strips
    ``_provider_content``), then the projection passes that ``reasoning``
    field through to the wire payload — the projection never re-reads
    ``_provider_content`` (it is gone by then).

    Pure transform.  Safe to call from ``asyncio.to_thread``.
    """
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        provider_content = msg.get("_provider_content")
        # Always strip the internal lane before the wire payload leaves
        # the helper, even when surface_persisted_reasoning_flag is False or the
        # field is empty/missing.  The strip is the contract; reasoning
        # surfacing is conditional on top of it.
        if "_provider_content" in msg:
            del msg["_provider_content"]
        if not surface_persisted_reasoning_flag:
            continue
        text = extract_reasoning_text_from_provider_content(provider_content)
        if text:
            msg["reasoning"] = text


def attach_vllm_chat_reasoning_field(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project persisted reasoning onto outgoing assistant messages as a
    non-standard ``reasoning`` field consumed by vLLM's chat template.

    vLLM's ``ChatMessage`` (``vllm/entrypoints/openai/chat_completion/
    protocol.py``) accepts a non-standard ``reasoning`` input field that
    propagates into the template render context as both ``reasoning``
    and ``reasoning_content``.  Templates from reasoning-aware families
    (Qwen3, DeepSeek-R1) inline that text on the next turn; templates
    that don't read the field silently drop it.  Either way the field
    name doesn't conflict with the OpenAI spec — ``sanitize_messages``
    preserves it because it isn't ``_``-prefixed, and the OpenAI Python
    SDK passes unknown message-level fields through to the wire
    (TypedDict input shape, no runtime validation).

    Pure transform: returns a new list with new dict copies for the
    assistant messages that get a ``reasoning`` field attached.  Other
    messages and assistant messages without reasoning text pass through
    by reference.  The original messages are never mutated.

    All three gates (provider isinstance, ``server_type == "vllm"``,
    operator flag ``replay_reasoning_to_model``) MUST be checked by the
    caller — this helper assumes the decision has already been made.
    See ``ChatSession._maybe_attach_vllm_chat_reasoning`` for the
    integration point.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            out.append(msg)
            continue
        provider_content = msg.get("_provider_content")
        if not provider_content:
            out.append(msg)
            continue
        text = extract_reasoning_text_from_provider_content(provider_content)
        if not text:
            out.append(msg)
            continue
        out.append({**msg, "reasoning": text})
    return out


def decorate_history_messages(
    messages: list[dict[str, Any]],
    verdicts_by_call_id: dict[str, dict[str, Any]],
    assessments_by_call_id: dict[str, dict[str, Any]],
) -> None:
    """Mutate a list of OpenAI-format messages, decorating tool_calls.

    Used by the ``/history`` REST endpoint after ``load_messages``
    returns.  For each assistant message with ``tool_calls``, runs
    :func:`decorate_tool_call` on every entry.  For each tool message
    whose ``content`` carries a ``<tool_output>`` envelope (queued
    user message spliced via :class:`UserInterjection` during a tool
    batch), strips the envelope, restores literal wrapper tags inside
    the body, and surfaces the extracted advisories on
    ``msg["advisories"]`` so the wire layer can replay them as user
    bubbles after the tool result.

    Pure transform — no I/O.  Async callers should pre-load the
    indexes via :func:`load_verdict_indexes` (in ``to_thread``) and
    pass them in.
    """
    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            tcs = msg.get("tool_calls")
            if isinstance(tcs, list):
                for tc in tcs:
                    if isinstance(tc, dict):
                        decorate_tool_call(tc, verdicts_by_call_id, assessments_by_call_id)
        elif role == "tool":
            content = msg.get("content")
            if not isinstance(content, str):
                continue
            try:
                extracted = extract_advisories_from_tool_envelope(content)
            except Exception:
                # Defensive — on any unexpected parse failure leave the
                # message untouched rather than crashing the replay.
                log.debug("advisory extraction failed; leaving content intact", exc_info=True)
                continue
            if extracted is None:
                continue
            cleaned, advisories = extracted
            msg["content"] = cleaned
            if advisories:
                msg["advisories"] = advisories


def project_history_messages(
    messages: list[dict[str, Any]], awaiting_approval: bool = False
) -> list[dict[str, Any]]:
    """Project decorated storage messages into the canonical wire shape.

    This is the SINGLE server-side projection that both the interactive
    ``replayHistory`` renderer and the coordinator dashboard's history
    rebuild consume.  It runs LAST in the ``make_history_handler``
    pipeline — after :func:`decorate_history_messages` (verdict /
    output_assessment + STRING ``<tool_output>`` advisory stripping) and
    :func:`extract_reasoning_for_history` (``reasoning`` stamping +
    ``_provider_content`` strip) — and reshapes the provider-native
    ``reconstruct_messages`` storage shape into the flat render shape:

    - multipart user ``content`` → plain string + derived ``attachments``
      (the ``_attachments_meta`` side-channel wins when present);
    - ``_source`` → ``source``; ``_reminders`` → filtered ``reminders``;
    - nested ``tool_calls[].function.{name,arguments}`` → flat
      ``{id, name, arguments}`` carrying the decoration (``verdict`` /
      ``output_assessment``) already placed on the call by
      :func:`decorate_tool_call`;
    - ``reasoning`` passes through (already stamped upstream — this
      projection NEVER reads ``_provider_content``, which is gone by now);
    - tool results: surface ``advisories``, coerce list content to a
      string, derive ``denied`` / ``is_error`` from the content prefix;
    - ``denied`` propagates from a tool result to its parent assistant
      turn; ``pending`` marks the last assistant tool-call turn ONLY when
      the workstream is genuinely awaiting approval for it
      (*awaiting_approval*) — driven by the live ``_pending_approval``
      signal, NOT orphan-detection.  An orphan that is executing or was
      interrupted is not awaiting approval and must still render its tool
      block.

    *awaiting_approval* is the caller's live read of the workstream's
    ``_pending_approval`` (``make_history_handler`` reads it off the
    loaded session; ``False`` for a storage-only / closed ws).  It must
    track the SAME signal that the SSE replay uses to re-emit the
    interactive approve_request prompt (``_interactive_events_replay`` /
    the coord replay): ``pending=True`` tells the renderer to SKIP the
    static tool block precisely because that live prompt will render it
    instead.  Deriving ``pending`` from orphan-detection alone desynced
    the two — a tool call mid-execution (orphan, but not awaiting) was
    marked pending, so it rendered from neither source on a fresh connect
    and only reappeared on reconnect (ring-buffer event replay).

    Two projection responsibilities live HERE and nowhere else:

    * **List-content advisory extraction.**  A queued ``UserInterjection``
      spliced into a LIST-typed tool result (image / structured MCP
      output) rides as an appended ``wrap_tool_result("", advisories)``
      carrier part.  :func:`decorate_history_messages` only handles
      STRING content, so the carrier survives to here; this projection
      extracts the advisories and drops the carrier part.  STRING
      envelopes are already stripped upstream, so their ``advisories``
      pass through untouched — we never double-extract.
    * **List → string coercion** of tool content: the renderers require a
      string (``replayHistory`` calls ``stripAnsi(content).trim()``;
      coord joins text parts), so a LIST tool ``content`` is reduced to
      its joined text parts here.

    Returns a NEW list of NEW entry dicts (strict 1:1 with *messages*) —
    never mutates the input.  Pure transform; safe from any thread.
    """
    # Pre-scan: which tool_call_ids have a result message?  An assistant
    # tool_call with no result is an orphan; only the LAST such turn is
    # marked "pending" (see the reversed single-turn marking below) —
    # a mid-conversation orphan (cancelled / interrupted) must still
    # render its tool block, so it is deliberately left unmarked.
    resulted_call_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "tool":
            cid = msg.get("tool_call_id")
            if cid:
                resulted_call_ids.add(str(cid))

    history: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        attachments_meta: list[dict[str, Any]] = []

        # (1) Collapse multipart user content (text + image_url / document
        #     parts) to a plain string + a derived attachment list.
        if role == "user" and isinstance(content, list):
            text_parts: list[str] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    text_parts.append(str(part.get("text", "")))
                elif ptype == "image_url":
                    attachments_meta.append({"kind": "image", "filename": "", "mime_type": ""})
                elif ptype == "document":
                    d = part.get("document", {})
                    attachments_meta.append(
                        {
                            "kind": "text",
                            "filename": str(d.get("name", "")),
                            "mime_type": str(d.get("media_type", "")),
                        }
                    )
            content = "\n".join(text_parts)

        # (2) The authoritative ``_attachments_meta`` side-channel wins
        #     when present (carries image filenames the image_url part
        #     itself can't express).
        side_meta = msg.get("_attachments_meta")
        if isinstance(side_meta, list) and side_meta:
            attachments_meta = [
                {
                    "kind": str(m.get("kind") or ""),
                    "filename": str(m.get("filename") or ""),
                    "mime_type": str(m.get("mime_type") or ""),
                }
                for m in side_meta
                if isinstance(m, dict)
            ]

        entry: dict[str, Any] = {"role": role, "content": content}
        if attachments_meta:
            entry["attachments"] = attachments_meta

        # (3) ``_source`` side-channel → top-level ``source`` (drives the
        #     ``.msg.user.system-nudge`` marker on replay).
        if msg.get("_source"):
            entry["source"] = str(msg["_source"])

        # (4) ``_reminders`` side-channel → top-level ``reminders``,
        #     filtered + key-projected.  Filter first so an all-malformed
        #     list doesn't set the field to ``[]`` (absent vs empty mean
        #     the same on the wire); project on a known key set to narrow
        #     the blast radius if a producer stuffs extra fields.
        reminders = msg.get("_reminders")
        if isinstance(reminders, list):
            clean_reminders: list[dict[str, Any]] = []
            for r in reminders:
                if not isinstance(r, dict):
                    continue
                rtype = str(r.get("type") or "")
                rtext = str(r.get("text") or "")
                if not rtype and not rtext:
                    continue
                clean: dict[str, Any] = {"type": rtype, "text": rtext}
                for opt_key in WATCH_REMINDER_OPTIONAL_KEYS:
                    if opt_key in r:
                        clean[opt_key] = r[opt_key]
                clean_reminders.append(clean)
            if clean_reminders:
                entry["reminders"] = clean_reminders

        # (5) Reasoning is already stamped by ``extract_reasoning_for_history``
        #     (gated on the active model's surface_persisted_reasoning flag) —
        #     pass it through.  This projection never re-extracts from
        #     ``_provider_content`` (already stripped upstream).
        if msg.get("reasoning"):
            entry["reasoning"] = msg["reasoning"]

        # (6) Flatten OpenAI-nested tool_calls ``{id, function:{name,
        #     arguments}}`` → ``{id, name, arguments}`` that the renderers
        #     read, carrying the decoration (``verdict`` /
        #     ``output_assessment``) ``decorate_tool_call`` placed on the
        #     nested call.
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            tc_entries: list[dict[str, Any]] = []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                arguments = fn.get("arguments")
                if arguments is None:
                    arguments = tc.get("arguments")
                if arguments is None:
                    arguments = ""
                tc_entry: dict[str, Any] = {
                    "id": tc.get("id", "") or "",
                    "name": fn.get("name") or tc.get("name") or "",
                    "arguments": arguments,
                }
                if tc.get("verdict"):
                    tc_entry["verdict"] = tc["verdict"]
                if tc.get("output_assessment"):
                    tc_entry["output_assessment"] = tc["output_assessment"]
                tc_entries.append(tc_entry)
            entry["tool_calls"] = tc_entries

        # (7) Tool results: carry ``tool_call_id`` + ``advisories``, coerce
        #     list content to text (extracting list-envelope advisories
        #     along the way), and derive ``denied`` / ``is_error`` from the
        #     content prefix — the storage shape pre-sets none of these.
        if role == "tool":
            result_call_id = msg.get("tool_call_id")
            if result_call_id:
                entry["tool_call_id"] = str(result_call_id)
            # STRING-content advisories were already surfaced by
            # ``decorate_history_messages`` (on ``msg["advisories"]``);
            # pass them through.  LIST-content envelopes are extracted
            # here (decorate skips non-string content).
            existing_advisories = msg.get("advisories")
            extracted_advisories: list[dict[str, str]] = []
            if isinstance(content, list):
                # The Seam 1 splice rides as an appended
                # ``wrap_tool_result("", advisories)`` carrier part — the
                # inner cleaned content is empty by construction.  Drop the
                # carrier part ONLY when the parser accepted the envelope
                # AND the cleaned inner is empty AND at least one advisory
                # survived; a tool legitimately emitting an envelope with a
                # non-empty body is left in place.
                kept_text: list[str] = []
                for part in content:
                    text = (
                        part.get("text")
                        if isinstance(part, dict) and part.get("type") == "text"
                        else None
                    )
                    if isinstance(text, str) and text.startswith("<tool_output>\n"):
                        try:
                            extracted = extract_advisories_from_tool_envelope(text)
                        except Exception:
                            extracted = None
                        if extracted is not None:
                            cleaned_text, advisories_from_part = extracted
                            if not cleaned_text and advisories_from_part:
                                extracted_advisories.extend(advisories_from_part)
                                continue
                    if isinstance(text, str):
                        kept_text.append(text)
                content = "\n".join(kept_text)
                entry["content"] = content
            if isinstance(content, str):
                if content.startswith("Denied by user") or content.startswith("Blocked"):
                    entry["denied"] = True
                # Persisted flag wins; fall back to the text heuristic for
                # historical data that predates ``is_error``.
                if (
                    msg.get("is_error")
                    or content.startswith("Error")
                    or content.startswith("Command timed out")
                    or content.startswith("Search timed out")
                    or content.startswith("Unknown tool:")
                    or content.startswith("JSON parse error:")
                    or content.startswith("MCP prompt timed out")
                    or content.startswith("MCP prompt error")
                ):
                    entry["is_error"] = True
            # Surface advisories on the wire (string: passed through from
            # decorate; list: extracted just above).  Project on a known
            # key set, mirroring the ``reminders`` filter.
            advisories_src: list[dict[str, Any]] = (
                extracted_advisories
                if extracted_advisories
                else (existing_advisories if isinstance(existing_advisories, list) else [])
            )
            if advisories_src:
                clean_advisories: list[dict[str, Any]] = []
                for a in advisories_src:
                    if not isinstance(a, dict):
                        continue
                    atype = str(a.get("type") or "")
                    atext = str(a.get("text") or "")
                    if not atype or not atext:
                        continue
                    clean_advisories.append(
                        {
                            "type": atype,
                            "text": atext,
                            "priority": str(a.get("priority") or "notice"),
                        }
                    )
                if clean_advisories:
                    entry["advisories"] = clean_advisories

        history.append(entry)

    # (8) Propagate denial from a tool result to its parent assistant turn
    #     so the tool block renders the denied (not approved) badge.
    last_assistant_idx: int | None = None
    for idx, entry in enumerate(history):
        if entry.get("tool_calls"):
            last_assistant_idx = idx
        elif entry.get("role") == "tool" and entry.get("denied") and last_assistant_idx is not None:
            history[last_assistant_idx]["denied"] = True

    # (9) Mark ``pending`` ONLY on the LAST assistant tool-call turn, and
    #     only when the workstream is genuinely awaiting approval for it
    #     (``awaiting_approval`` — the live ``_pending_approval`` read)
    #     AND it still has an orphan (a tool_call with no result in the
    #     loaded window).  ``pending`` tells the renderer to skip the
    #     static tool block because the SSE replay re-emits the live
    #     approve_request prompt instead, so this must track the same
    #     signal as that re-emit.  An orphan that is executing or was
    #     interrupted is NOT awaiting approval, so it stays unmarked and
    #     renders its tool block (the fresh-connect-during-execution gap).
    if awaiting_approval:
        for entry in reversed(history):
            tcs = entry.get("tool_calls")
            if tcs:
                has_orphan = any(
                    tc.get("id") and str(tc["id"]) not in resulted_call_ids for tc in tcs
                )
                if has_orphan:
                    entry["pending"] = True
                break

    return history
