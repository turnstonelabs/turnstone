"""Lowering — neutral trajectory → still-neutral-but-wire-valid.

Sits between the session (which owns the canonical, provider-neutral
trajectory) and the per-provider translators (which own format only — the
``C`` layer).  Lowering keeps the messages provider-neutral but makes them
*valid* for an LLM round-trip, so every translator can assume a well-formed
input and stay a pure format mapping.

Today this module owns **repair** (validity): synthesizing cancellation
results for orphaned client tool calls.  The fold (representation) moves here
in a later step; the two together are the "lowering" stage.

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

# The synthetic result body for a tool call that never produced output.  The
# neutral turn carries ``is_error=True``; each translator renders that per its
# format (Anthropic ``tool_result.is_error``; the OpenAI-compatible lanes have
# no such field and drop it).
CANCELLED_TOOL_RESULT = "Tool execution was cancelled."


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


def repair_wire_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return *messages* with cancellation results for any orphaned tool calls.

    The synth-transient-@-send repair policy (see the module docstring).
    Identity-preserving: when nothing is orphaned the input is returned
    unchanged, so the common path is allocation-free.  Never mutates *messages*.
    """
    orphans = _find_orphaned_tool_calls(messages)
    if not orphans:
        return messages
    out = list(messages)
    # The detector yields insert positions in ascending order; splice from the
    # highest down so each splice can't shift an as-yet-unused lower position.
    for insert_at, ids in sorted(orphans, key=lambda pair: pair[0], reverse=True):
        synthetic = [
            {
                "role": "tool",
                "tool_call_id": uid,
                "content": CANCELLED_TOOL_RESULT,
                "is_error": True,
            }
            for uid in ids
        ]
        out[insert_at:insert_at] = synthetic
    return out
