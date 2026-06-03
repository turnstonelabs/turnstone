"""Unit tests for ``lowering.repair_wire_messages`` — the send-time orphan repair.

The single send-side orphan-repair policy: an assistant turn whose client
``tool_calls`` lack matching ``tool`` results gets a synthetic, ``is_error``
cancellation result spliced in before the wire.  This is the one place that
synthesis happens for the wire — the per-provider translators carry none, so
this pins the behaviour the old ``_anthropic`` ``pc_tool_ids`` /
``sanitize_messages`` synthesis used to own.  See
``test_wire_payload_golden.py`` for the byte-level per-provider proof.
"""

from __future__ import annotations

from typing import Any

from turnstone.core.lowering import (
    CANCELLED_TOOL_RESULT,
    _find_orphaned_tool_calls,
    repair_wire_messages,
)


def _tc(call_id: str, name: str = "f") -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


def _assistant(*call_ids: str, content: str = "") -> dict[str, Any]:
    return {"role": "assistant", "content": content, "tool_calls": [_tc(c) for c in call_ids]}


def _tool(call_id: str, content: str = "ok") -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _synth_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tool turns repair added that carry the cancellation body."""
    return [
        m for m in messages if m.get("role") == "tool" and m.get("content") == CANCELLED_TOOL_RESULT
    ]


# --------------------------------------------------------------------------- #
# _find_orphaned_tool_calls — the detector
# --------------------------------------------------------------------------- #
def test_detector_empty() -> None:
    assert _find_orphaned_tool_calls([]) == []


def test_detector_no_tool_calls() -> None:
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    assert _find_orphaned_tool_calls(msgs) == []


def test_detector_complete_no_orphan() -> None:
    msgs = [_assistant("c1"), _tool("c1"), {"role": "user", "content": "thanks"}]
    assert _find_orphaned_tool_calls(msgs) == []


def test_detector_single_orphan_trailing() -> None:
    msgs = [{"role": "user", "content": "go"}, _assistant("c1")]
    # insert_at is just after the assistant (index 2); c1 unanswered.
    assert _find_orphaned_tool_calls(msgs) == [(2, ["c1"])]


def test_detector_partial_results() -> None:
    msgs = [_assistant("c1", "c2"), _tool("c1"), {"role": "user", "content": "stop"}]
    # c1 answered, c2 orphaned; insert just after the real result (index 2).
    assert _find_orphaned_tool_calls(msgs) == [(2, ["c2"])]


def test_detector_multiple_orphans_order_preserved() -> None:
    msgs = [_assistant("c1", "c2", "c3"), {"role": "user", "content": "skip"}]
    assert _find_orphaned_tool_calls(msgs) == [(1, ["c1", "c2", "c3"])]


def test_detector_looks_through_interspersed_system() -> None:
    # Native path: an operator system turn rides between the assistant and its
    # results; it must not be read as the end of the tool block.
    msgs = [
        _assistant("c1", "c2"),
        _tool("c1"),
        {"role": "system", "_source": "output_guard", "content": "note"},
        {"role": "user", "content": "next"},
    ]
    # insert_at stays right after the real result (index 2), before the system turn.
    assert _find_orphaned_tool_calls(msgs) == [(2, ["c2"])]


def test_detector_repeated_ids_are_per_assistant() -> None:
    # The same id reused across turns: turn 1 answered, turn 2 orphaned.
    msgs = [
        {"role": "user", "content": "A"},
        _assistant("c1"),
        _tool("c1"),
        {"role": "user", "content": "B"},
        _assistant("c1"),
    ]
    assert _find_orphaned_tool_calls(msgs) == [(5, ["c1"])]


def test_detector_ignores_empty_ids() -> None:
    msgs = [{"role": "assistant", "content": "", "tool_calls": [_tc("")]}]
    assert _find_orphaned_tool_calls(msgs) == []


# --------------------------------------------------------------------------- #
# repair_wire_messages — the synth policy
# --------------------------------------------------------------------------- #
def test_repair_identity_when_complete() -> None:
    msgs = [_assistant("c1"), _tool("c1")]
    # No orphan → same object returned (allocation-free common path).
    assert repair_wire_messages(msgs) is msgs


def test_repair_no_tool_calls_identity() -> None:
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    assert repair_wire_messages(msgs) is msgs


def test_repair_synthesizes_trailing_orphan() -> None:
    msgs = [{"role": "user", "content": "go"}, _assistant("c1")]
    out = repair_wire_messages(msgs)
    assert len(out) == 3
    assert out[2] == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": CANCELLED_TOOL_RESULT,
        "is_error": True,
    }


def test_repair_synthesizes_only_missing() -> None:
    msgs = [_assistant("c1", "c2"), _tool("c1"), {"role": "user", "content": "stop"}]
    out = repair_wire_messages(msgs)
    # Real c1 result stays first; synthetic c2 spliced right after, before the user.
    assert [m["role"] for m in out] == ["assistant", "tool", "tool", "user"]
    assert out[1]["tool_call_id"] == "c1" and out[1]["content"] == "ok"
    assert out[2]["tool_call_id"] == "c2" and out[2]["is_error"] is True


def test_repair_multiple_orphans_in_declaration_order() -> None:
    msgs = [_assistant("c1", "c2", "c3"), {"role": "user", "content": "skip"}]
    out = repair_wire_messages(msgs)
    synth_ids = [m["tool_call_id"] for m in _synth_results(out)]
    assert synth_ids == ["c1", "c2", "c3"]


def test_repair_two_assistant_turns() -> None:
    msgs = [
        _assistant("c1"),
        {"role": "user", "content": "and"},
        _assistant("c2"),
    ]
    out = repair_wire_messages(msgs)
    # Each orphaned turn gets its own synthetic result, positioned after it.
    assert [m["role"] for m in out] == ["assistant", "tool", "user", "assistant", "tool"]
    assert out[1]["tool_call_id"] == "c1"
    assert out[4]["tool_call_id"] == "c2"


def test_repair_synth_inserts_before_interspersed_system() -> None:
    msgs = [
        _assistant("c1", "c2"),
        _tool("c1"),
        {"role": "system", "_source": "output_guard", "content": "note"},
        {"role": "user", "content": "next"},
    ]
    out = repair_wire_messages(msgs)
    # Synthetic c2 stays contiguous with the real result, before the system turn.
    assert [m["role"] for m in out] == ["assistant", "tool", "tool", "system", "user"]
    assert out[2]["tool_call_id"] == "c2" and out[2]["is_error"] is True


def test_repair_does_not_mutate_input() -> None:
    msgs = [_assistant("c1")]
    original_len = len(msgs)
    repair_wire_messages(msgs)
    assert len(msgs) == original_len  # caller's list untouched
    assert "tool_calls" in msgs[0]
