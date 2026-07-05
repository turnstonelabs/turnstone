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

import json
from typing import Any

from turnstone.core.lowering import (
    CANCELLED_TOOL_RESULT,
    _find_orphaned_tool_calls,
    repair_wire_messages,
    sanitize_tool_call_arguments,
    tool_args_preview,
    wire_valid_arguments,
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
        # The unobserved synth carries the typed disposition (wire-invisible
        # side channel, stripped by the translator before the provider wire).
        "_effect_status": "unknown",
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


# --------------------------------------------------------------------------- #
# wire_valid_arguments — the shared "is this renderable" predicate
# --------------------------------------------------------------------------- #
def test_wire_valid_arguments_accepts_json_objects() -> None:
    assert wire_valid_arguments("{}") is True
    assert wire_valid_arguments('{"command": "ls -la"}') is True
    assert wire_valid_arguments('  { "a": 1 }\n') is True  # surrounding whitespace ok


def test_wire_valid_arguments_rejects_unrenderable() -> None:
    assert wire_valid_arguments('{"command": "cat /va') is False  # unterminated (the incident)
    assert wire_valid_arguments("") is False  # empty (no-arg call) — json.loads raises
    assert wire_valid_arguments("[]") is False  # array, not object
    assert wire_valid_arguments("5") is False  # bare scalar
    assert wire_valid_arguments('"hi"') is False  # bare string
    assert wire_valid_arguments(None) is False  # missing
    assert wire_valid_arguments({"a": 1}) is False  # raw dict — not a string on the wire


def test_wire_valid_arguments_totals_on_deeply_nested_json() -> None:
    # Deeply-nested JSON makes json.loads raise RecursionError (not a ValueError);
    # the predicate must return False, not propagate and crash the send.
    deep = "[" * 5000 + "]" * 5000
    assert wire_valid_arguments(deep) is False


def test_tool_args_preview_stringifies_and_caps() -> None:
    assert tool_args_preview("x" * 500) == "x" * 120
    assert tool_args_preview(None) == "None"
    assert tool_args_preview({"a": 1}) == "{'a': 1}"


# --------------------------------------------------------------------------- #
# sanitize_tool_call_arguments — the legalize pass
# --------------------------------------------------------------------------- #
def _call(call_id: str, arguments: Any, name: str = "bash") -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _assistant_calls(*calls: dict[str, Any]) -> dict[str, Any]:
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


def test_sanitize_identity_when_all_valid() -> None:
    msgs = [_assistant_calls(_call("c1", "{}"), _call("c2", '{"a": 1}')), _tool("c1"), _tool("c2")]
    # Every arguments already a JSON object → same object returned (allocation-free).
    assert sanitize_tool_call_arguments(msgs) is msgs


def test_sanitize_identity_when_no_tool_calls() -> None:
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    assert sanitize_tool_call_arguments(msgs) is msgs


def test_sanitize_legalizes_unterminated_arguments() -> None:
    # The production incident: deepseek-v4-flash emitted an unterminated args string
    # with a non-``length`` finish reason, so it was committed and replayed verbatim.
    msgs = [_assistant_calls(_call("c1", '{"command": "cat /va')), _tool("c1", "retry")]
    out = sanitize_tool_call_arguments(msgs)
    assert out is not msgs  # copied on repair
    assert out[0]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert json.loads(out[0]["tool_calls"][0]["function"]["arguments"]) == {}


def test_sanitize_legalizes_empty_arguments() -> None:
    # A no-arg tool call sends ``""``; json.loads("") raises, so deepseek_v4 would 400.
    out = sanitize_tool_call_arguments([_assistant_calls(_call("c1", ""))])
    assert out[0]["tool_calls"][0]["function"]["arguments"] == "{}"


def test_sanitize_legalizes_non_object_json() -> None:
    out = sanitize_tool_call_arguments([_assistant_calls(_call("c1", "[]"), _call("c2", "5"))])
    assert [tc["function"]["arguments"] for tc in out[0]["tool_calls"]] == ["{}", "{}"]


def test_sanitize_serializes_raw_dict_arguments() -> None:
    out = sanitize_tool_call_arguments([_assistant_calls(_call("c1", {"command": "ls"}))])
    got = out[0]["tool_calls"][0]["function"]["arguments"]
    assert isinstance(got, str) and json.loads(got) == {"command": "ls"}


def test_sanitize_falls_back_when_dict_not_serializable() -> None:
    # Defensive branch: a dict arguments carrying a non-JSON-encodable value
    # (a set) makes json.dumps raise TypeError — it collapses to "{}", not a crash.
    out = sanitize_tool_call_arguments([_assistant_calls(_call("c1", {"x": {1, 2, 3}}))])
    assert out[0]["tool_calls"][0]["function"]["arguments"] == "{}"


def test_sanitize_touches_only_the_offending_call() -> None:
    good = _call("c1", '{"a": 1}')
    bad = _call("c2", "{oops")
    out = sanitize_tool_call_arguments([_assistant_calls(good, bad)])
    # Valid sibling preserved by identity; only the bad call is rebuilt.
    assert out[0]["tool_calls"][0] is good
    assert out[0]["tool_calls"][1]["function"]["arguments"] == "{}"


def test_sanitize_does_not_mutate_input() -> None:
    raw = '{"command": "cat /va'
    bad = _call("c1", raw)
    msgs = [_assistant_calls(bad)]
    sanitize_tool_call_arguments(msgs)
    assert bad["function"]["arguments"] == raw  # caller's dict untouched
    assert msgs[0]["tool_calls"][0] is bad


# --------------------------------------------------------------------------- #
# legalize ∘ repair — the two send-time validity passes compose
# --------------------------------------------------------------------------- #
def test_legalize_then_repair_answered_call() -> None:
    # Malformed-but-answered (the poison-pill shape): args legalized, no orphan added.
    msgs = [_assistant_calls(_call("c1", "{bad")), _tool("c1", "retry with valid JSON")]
    out = repair_wire_messages(sanitize_tool_call_arguments(msgs))
    assert [m["role"] for m in out] == ["assistant", "tool"]
    assert json.loads(out[0]["tool_calls"][0]["function"]["arguments"]) == {}


def test_legalize_then_repair_orphaned_call() -> None:
    # Malformed AND unanswered: legalized args + a synthesized cancellation result.
    msgs = [_assistant_calls(_call("c1", "{bad"))]
    out = repair_wire_messages(sanitize_tool_call_arguments(msgs))
    assert [m["role"] for m in out] == ["assistant", "tool"]
    assert json.loads(out[0]["tool_calls"][0]["function"]["arguments"]) == {}
    assert out[1]["content"] == CANCELLED_TOOL_RESULT


def test_pipeline_every_emitted_arguments_is_a_json_object() -> None:
    # The end-state invariant a strict renderer relies on.
    msgs = [
        _assistant_calls(_call("c1", ""), _call("c2", "{oops"), _call("c3", '{"ok": true}')),
        _tool("c1"),
        _tool("c2"),
        _tool("c3"),
    ]
    out = repair_wire_messages(sanitize_tool_call_arguments(msgs))
    for m in out:
        for tc in m.get("tool_calls", []):
            assert isinstance(json.loads(tc["function"]["arguments"]), dict)
