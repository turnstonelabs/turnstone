"""Lowering — neutral trajectory → still-neutral-but-wire-valid.

Sits between the session (which owns the canonical, provider-neutral
trajectory) and the per-provider translators (which own format only — the
``C`` layer).  Lowering keeps the messages provider-neutral but makes them
*valid* for an LLM round-trip, so every translator can assume a well-formed
input and stay a pure format mapping.

This module owns the two provider-neutral lowering passes:

* **fold** (representation) — operator-context ``system`` turns are folded into
  the preceding turn as nonce-fenced ``[start system-reminder]`` blocks for models
  without native mid-conversation system support (native models keep them
  inline).  See :func:`fold_system_turns`.
* **repair** (validity) — synthesizing cancellation results for orphaned client
  tool calls.  See :func:`repair_wire_messages`.

The nonce the fold borrows stays **session-minted and session-owned**
(``ChatSession._envelope_nonce``): it binds three consumers — the fold here, the
cached-prefix trust declaration in the system prompt, and the output-guard
forgery check — which must agree on the exact marker, so lowering takes it as a
parameter and never mints its own.

Orphan repair has **one detector** (:func:`_find_orphaned_tool_calls`) feeding
**three policies**, only one of which lives here:

* strip-and-discard **@ load** — drop a trailing incomplete tool-call turn on
  resume (``storage/_utils.py``); recovery, not wire prep.
* synth-and-persist **@ runtime cancel** — write ``is_error`` rows when a
  cancel interrupts tool execution (``ChatSession._synthesize_cancelled_results``).
* **synth-transient @ send** — :func:`repair_wire_messages`, here.  Belt-and-
  suspenders before the wire: any orphan that reaches a ``send`` (a cancel
  that skipped persistence, a resume that left a mid-conversation orphan) gets
  a transient cancellation result so the provider never sees an unanswered
  tool call.  Never persisted.

The detector reads ``tool_calls`` **only**.  That is sound because the
native/``tool_calls`` mirror is enforced at the save boundary
(``storage/_utils.normalize_native_for_save``): whenever an assistant turn
carries a client ``tool_use`` in its provider-native lane, the same call is
mirrored in top-level ``tool_calls``.  Reading ``tool_calls`` therefore catches
verbatim-replay orphans too, so the translators carry no orphan synthesis of
their own.
"""

from __future__ import annotations

from typing import Any

from turnstone.core import fence
from turnstone.core.trajectory import EffectStatus, Turn, dicts_from_turns

# The "you cannot tell whether it ran" clause, shared by every cancel
# disposition surface (this wire-repair fallback AND the session-layer
# synthesis in ChatSession) so they can't drift apart.  Public so session.py
# can import it rather than re-type the sentence — the new tests assert only
# the ``UNKNOWN`` token, so silent wording drift would otherwise be invisible.
UNOBSERVED_OUTCOME_CLAUSE = (
    "Outcome UNKNOWN — this call may have begun executing before the generation "
    "was stopped; do not assume it did not run, and reconcile before re-issuing it."
)

# The timeout twin of UNOBSERVED_OUTCOME_CLAUSE.  A tool stopped at its deadline
# was killed (bash is SIGKILL'd) or abandoned (an MCP action the server may still
# be running) mid-flight, so its side effects are as unobserved as a cancelled
# call's — the same "unknown, never none" discipline, a different cause.  Only
# side-effecting tools use it: read-only timeouts (search, MCP resource/prompt
# reads) stay a plain "timed out", because an idempotent read has nothing to
# reconcile and "reconcile before re-issuing" would be misleading there.  Shared
# (so bash and MCP can't drift) and asserted by the UNKNOWN token in tests.
TIMEOUT_OUTCOME_CLAUSE = (
    "Outcome UNKNOWN — the call was stopped at its deadline; it may have run "
    "partially or had side effects, so do not assume it did not run, and "
    "reconcile before re-issuing it."
)

# The synthetic result body for a tool call that never produced output (the
# last-resort wire-repair for an orphan the session layer didn't synthesize —
# e.g. a force-abandoned worker).  The neutral turn carries ``is_error=True``;
# each translator renders that per its format (Anthropic ``tool_result.is_error``;
# the OpenAI-compatible lanes have no such field and drop it).  The body reads
# outcome-UNKNOWN, matching the cooperative-cancel disposition: an unobserved
# call must not read as "did not run" (unknown, never none).
CANCELLED_TOOL_RESULT = f"Tool execution was cancelled. {UNOBSERVED_OUTCOME_CLAUSE}"


def _find_orphaned_tool_calls(
    messages: list[dict[str, Any]],
) -> list[tuple[int, list[str]]]:
    """Locate assistant tool calls with no matching tool result.

    Returns ``(insert_at, orphan_ids)`` pairs — ``insert_at`` is the slot just
    after the last real tool result in the call's block (so synthesized results
    stay contiguous with any real ones, which Anthropic requires), and
    ``orphan_ids`` are the unanswered ``tool_calls`` ids in declaration order.

    Reads ``tool_calls`` only (see the module docstring).  Operator-context
    ``system`` / ``developer`` turns interleaved in a tool block are looked
    *through* — on the native path they ride between an assistant turn and its
    results, and must not be mistaken for the end of the result run.
    """
    found: list[tuple[int, list[str]]] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            expected_ids = [tc.get("id", "") for tc in msg["tool_calls"] if tc.get("id")]
            j = i + 1
            result_ids: set[str] = set()
            insert_at = i + 1
            while j < n and messages[j].get("role") in ("tool", "system", "developer"):
                if messages[j].get("role") == "tool":
                    tc_id = messages[j].get("tool_call_id", "")
                    if tc_id:
                        result_ids.add(tc_id)
                    insert_at = j + 1
                j += 1
            orphaned = [uid for uid in expected_ids if uid not in result_ids]
            if orphaned:
                found.append((insert_at, orphaned))
            # Advance past the whole block (existing results + the slot we'd
            # splice into); a bare assistant with no following block advances one.
            i = j if j > i + 1 else i + 1
        else:
            i += 1
    return found


def repair_wire_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return *messages* with cancellation results for any orphaned tool calls.

    The synth-transient-@-send repair policy (see the module docstring).
    Identity-preserving: when nothing is orphaned the input is returned
    unchanged, so the common path is allocation-free.  Never mutates *messages*.

    Operates on the wire-dict projection directly (the ``tool_calls`` /
    ``tool_call_id`` shape the detector keys on); the synthesized cancellations
    are built as canonical ``Turn``s and projected to the same dict shape before
    splicing, so the result is a uniform ``list[dict]`` ready for the translator.
    """
    orphans = _find_orphaned_tool_calls(messages)
    if not orphans:
        return messages
    out = list(messages)
    # The detector yields insert positions in ascending order; splice from the
    # highest down so each splice can't shift an as-yet-unused lower position.
    for insert_at, ids in sorted(orphans, key=lambda pair: pair[0], reverse=True):
        synthetic = dicts_from_turns(
            [
                Turn.tool(
                    uid, CANCELLED_TOOL_RESULT, is_error=True, effect_status=EffectStatus.UNKNOWN
                )
                for uid in ids
            ]
        )
        out[insert_at:insert_at] = synthetic
    return out


# --------------------------------------------------------------------------- #
# Fold — operator-context representation (A); runs BEFORE repair on the wire.
# --------------------------------------------------------------------------- #
def fold_system_turns(
    messages: list[dict[str, Any]],
    *,
    supports_mid_conversation_system: bool,
    nonce: str,
) -> list[dict[str, Any]]:
    """Fold first-class operator-context system turns into the preceding turn.

    First-class ``{"role": "system", "_source": ...}`` turns carry operator
    context (advisories / nudges / interjections — see
    ``tool_advisory.make_system_turn``).  Models WITHOUT native mid-conversation
    system support can't take a ``system`` message mid-array, so each such turn
    is wrapped in a nonce-delimited ``[start system-reminder_{nonce}]`` fence
    (:func:`turnstone.core.fence.wrap`) — the system prompt declares the exact
    *nonce* as the sole trusted marker via
    ``build_operator_instruction_declaration`` — and appended to the preceding
    wire turn's content, then dropped from the list.

    Forgery defence is two-layer: ``fence.wrap`` neutralises the operator body's
    closing marker (break-out), and before the first fold onto a host we
    neutralise that (untrusted) host turn's ``[start system-reminder]`` markers via
    :func:`_neutralize_host` (forge-in).  The host pass runs once per host —
    re-running it would defang the real fences we append afterwards — so a leaked
    or guessed nonce still cannot fabricate a trusted block.

    Native models (*supports_mid_conversation_system*) keep the turns inline —
    the Anthropic converter emits them as real ``system`` messages.  Base-prompt
    system messages (no ``_source``) pass through.  Consecutive operator turns
    fold onto the shared predecessor in order, so the wire never carries two
    adjacent ``system`` messages.  An operator turn with no predecessor (should
    not occur — they follow the turn they relate to) is kept standalone so
    nothing is silently dropped.

    Returns a transient copy as wire dicts; the input is untouched.  The fold's
    content-merge / host-escape logic keys directly on the wire content shape.
    """
    if supports_mid_conversation_system:
        return messages
    out: list[dict[str, Any]] = []
    host_escaped = False  # has out[-1] had its untrusted markers defanged?
    for msg in messages:
        if msg.get("role") == "system" and msg.get("_source"):
            raw = msg.get("content")
            text = raw if isinstance(raw, str) else str(raw or "")
            wrapped = fence.wrap(text, nonce, fence.SYSTEM_REMINDER_TAG)
            if out:
                if not host_escaped:
                    out[-1] = _neutralize_host(out[-1])
                    host_escaped = True
                out[-1] = _append_text_block(out[-1], wrapped)
            else:
                out.append(msg)
            continue
        out.append(msg)
        host_escaped = False
    return out


def _neutralize_host(msg: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *msg* with operator-fence markers defanged in its text.

    Defence-in-depth for the fold path: before a real ``[start system-reminder_{nonce}]``
    block is appended to this (untrusted) host turn, any literal
    ``[start system-reminder]`` marker already in its content is neutralised via
    :func:`turnstone.core.fence.neutralize` (opening + closing) so a leaked or
    guessed nonce cannot be used to forge a trusted block here.  Never mutates
    *msg* — the fold holds the read-only contract.
    """
    copy = dict(msg)
    content = copy.get("content")
    if isinstance(content, str):
        copy["content"] = fence.neutralize(content, fence.SYSTEM_REMINDER_TAG, opening=True)
    elif isinstance(content, list):
        copy["content"] = [
            (
                {
                    **p,
                    "text": fence.neutralize(p["text"], fence.SYSTEM_REMINDER_TAG, opening=True),
                }
                if isinstance(p, dict)
                and p.get("type") == "text"
                and isinstance(p.get("text"), str)
                else p
            )
            for p in content
        ]
    return copy


def _append_text_block(msg: dict[str, Any], block: str) -> dict[str, Any]:
    """Return a copy of *msg* with *block* appended to its content as text.

    String content gets a tail block; list content appends to the trailing text
    part (or a new text part).  Never mutates *msg* — the fold holds the
    read-only contract on the entries it threads through.
    """
    copy = dict(msg)
    content = copy.get("content")
    if isinstance(content, str):
        copy["content"] = f"{content}\n\n{block}" if content else block
    elif isinstance(content, list):
        new_parts = [
            dict(p) if isinstance(p, dict) and p.get("type") == "text" else p for p in content
        ]
        text_parts = [p for p in new_parts if isinstance(p, dict) and p.get("type") == "text"]
        if text_parts:
            text_parts[-1]["text"] = f"{text_parts[-1]['text']}\n\n{block}"
        else:
            new_parts.append({"type": "text", "text": block})
        copy["content"] = new_parts
    else:
        copy["content"] = block
    return copy


def _is_empty_wire_content(content: Any) -> bool:
    """True when *content* carries nothing the wire can send.

    ``None`` / blank string / empty list count as empty; a list with any parts
    (text, image, document) does not.
    """
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        return len(content) == 0
    return False


def drop_empty_user_turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop user turns with empty content.

    Runs AFTER the fold so a fold-path wake turn (which a nudge folds into and
    thereby fills) is kept, while a still-empty synthetic user turn — invalid on
    every provider wire — is removed.  Identity-preserving: returns *messages*
    unchanged when no user turn is empty, so the common path is allocation-free.

    The empty check keys on the wire content shape directly.
    """

    def _drop(m: dict[str, Any]) -> bool:
        return m.get("role") == "user" and _is_empty_wire_content(m.get("content"))

    drop_idx = {i for i, m in enumerate(messages) if _drop(m)}
    if not drop_idx:
        return messages
    return [m for i, m in enumerate(messages) if i not in drop_idx]
