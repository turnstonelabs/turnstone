"""Shared history-replay decoration helpers.

Both surfaces that build a history wire payload — interactive's SSE
``_build_history`` and the lifted ``make_history_handler`` REST
endpoint — need the same audit-trail data attached to each
``tool_calls`` entry: the persisted intent verdict (``intent_verdicts``
table) and the output-guard assessment (``output_assessments`` table).

Centralising the lookup + decoration here keeps the two surfaces from
drifting on which fields ship to the client and how they're shaped.
The shared helpers also let us project only the fields the UI actually
renders, dropping redundant ones (``call_id``/``func_name`` already
carried on ``tc.id``/``tc.name``) so the wire payload stays tight.

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

log = get_logger(__name__)


def load_verdict_indexes(
    ws_id: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Bulk-load intent verdicts and output assessments for a workstream.

    Returns ``(verdicts_by_call_id, assessments_by_call_id)``.  Both
    tables are indexed by ws_id so the queries are O(rows-for-ws); the
    DESC ordering plus first-seen-wins dedupe leaves the newest
    verdict per call_id (LLM upgrade beats heuristic when both exist).

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
            if cid and cid not in assessments_by_call_id:
                assessments_by_call_id[cid] = a
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


def build_output_assessment_payload(arow: dict[str, Any]) -> dict[str, Any] | None:
    """Project a stored ``output_assessments`` row into the wire shape.

    Returns ``None`` when the assessment is the unflagged baseline
    (``risk_level == "none"``) — same skip-on-clean pattern as
    :func:`build_verdict_payload`.

    Decodes ``flags`` from its JSON string form here so the client
    never has to parse twice.  Falls back to an empty list on bad JSON
    rather than raising — the rest of the assessment is still useful.
    """
    if (arow.get("risk_level") or "none") == "none":
        return None
    flags_raw = arow.get("flags") or "[]"
    try:
        flags = json.loads(flags_raw) if isinstance(flags_raw, str) else flags_raw
    except (ValueError, TypeError):
        flags = []
    return {
        "risk_level": arow.get("risk_level", "none"),
        "flags": flags if isinstance(flags, list) else [],
        "redacted": bool(arow.get("redacted", 0)),
    }


def decorate_tool_call(
    tc: dict[str, Any],
    verdicts_by_call_id: dict[str, dict[str, Any]],
    assessments_by_call_id: dict[str, dict[str, Any]],
) -> None:
    """Mutate ``tc`` in place, attaching ``verdict`` / ``output_assessment``.

    Works on either tool_call shape:
    - OpenAI format (``{id, function: {name, arguments}}``) — used by
      ``/history`` REST.
    - Flattened format (``{id, name, arguments}``) — used by SSE replay.

    Both carry ``id`` at the top level, which is the only field this
    helper reads.  No-ops cleanly when the call_id has no matching
    row (unflagged tools stay clean).
    """
    call_id = tc.get("id", "") or ""
    if not call_id:
        return
    vrow = verdicts_by_call_id.get(call_id)
    if vrow is not None:
        verdict = build_verdict_payload(vrow)
        if verdict is not None:
            tc["verdict"] = verdict
    arow = assessments_by_call_id.get(call_id)
    if arow is not None:
        assessment = build_output_assessment_payload(arow)
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
    """Dispatch reasoning extraction by first-block ``type`` field.

    Returns ``""`` for empty / missing / non-list / unknown-type input.

    Pure transform — safe from any thread.  Both history surfaces
    (interactive ``_build_history`` and lifted ``make_history_handler``)
    call this directly.  See ``_BLOCK_TYPE_PROVIDER_FACTORY`` above
    for the recognised block types and the providers that own them.
    """
    if not isinstance(provider_content, list) or not provider_content:
        return ""
    first_block = provider_content[0]
    if not isinstance(first_block, dict):
        return ""
    block_type = first_block.get("type")
    if not isinstance(block_type, str):
        return ""
    factory = _BLOCK_TYPE_PROVIDER_FACTORY.get(block_type)
    if factory is None:
        return ""
    return factory().extract_reasoning_text(provider_content)


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

    The interactive ``_build_history`` surface DOES NOT call this
    helper; it builds new entry dicts from scratch and calls
    :func:`extract_reasoning_text_from_provider_content` directly per
    assistant message, stamping ``entry["reasoning"]`` inline.  The two
    surfaces converge on the same dispatcher; only the mutation shape
    differs.

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
