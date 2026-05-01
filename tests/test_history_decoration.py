"""Unit tests for ``turnstone.core.history_decoration``.

The decoration helpers are shared between two surfaces — interactive's
SSE replay (``_build_history``) and the lifted ``/history`` REST
endpoint (``make_history_handler``, used by both interactive and
coord). Pinning the wire shape here lets a future schema/projection
change land in one file rather than spread across the two surfaces.
"""

from __future__ import annotations

from turnstone.core.history_decoration import (
    build_output_assessment_payload,
    build_verdict_payload,
    decorate_history_messages,
    decorate_tool_call,
)


class TestBuildVerdictPayload:
    """The wire-shape projection that's the single source of truth for
    what intent_verdict fields ship to the client."""

    def test_skips_unflagged_baseline(self) -> None:
        """``risk_level`` "none" is the unflagged-tool baseline; the
        client filters those anyway, so projecting None at the wire
        layer keeps the payload tight on long workstreams."""
        row = {"risk_level": "none", "recommendation": "approve", "tier": "heuristic"}
        assert build_verdict_payload(row) is None

    def test_drops_call_id_and_func_name(self) -> None:
        """The client already has these on ``tc.id`` / ``tc.name``;
        re-shipping them per-tool_call would balloon long replays."""
        row = {
            "call_id": "call_abc",
            "func_name": "bash",
            "risk_level": "medium",
            "recommendation": "review",
            "confidence": 0.8,
            "intent_summary": "summary",
            "tier": "heuristic",
        }
        out = build_verdict_payload(row)
        assert out is not None
        assert "call_id" not in out
        assert "func_name" not in out
        # Sanity — the kept fields are the ones renderVerdictBadge reads.
        assert out["risk_level"] == "medium"
        assert out["recommendation"] == "review"
        assert out["confidence"] == 0.8
        assert out["intent_summary"] == "summary"
        assert out["tier"] == "heuristic"

    def test_includes_reasoning_for_either_tier_when_present(self) -> None:
        """Heuristic verdicts in this project emit structured
        rationales (one per matched pattern) — e.g.
        ``policy.py`` writes a reasoning string per heuristic hit.
        Ship the field for either tier when it has content; only
        omit when the row didn't write one."""
        for tier in ("heuristic", "llm"):
            row = {
                "risk_level": "high",
                "tier": tier,
                "reasoning": "The command exfiltrates ~/.ssh/id_rsa over an external connection.",
            }
            out = build_verdict_payload(row)
            assert out is not None
            assert "id_rsa" in out["reasoning"]

    def test_omits_reasoning_when_empty(self) -> None:
        """An absent / empty reasoning string shouldn't ship as
        ``reasoning: ""`` — the rationale ``<details>`` block on the
        client renders an empty disclosure when the field is present
        but empty."""
        row = {"risk_level": "high", "tier": "heuristic", "reasoning": ""}
        out = build_verdict_payload(row)
        assert out is not None
        assert "reasoning" not in out

    def test_includes_judge_model_when_present(self) -> None:
        """``judge_model`` rides through so the batch tier badge can
        render ``⚖ llm:claude-haiku-4`` on history-only replays
        rather than the bare ``⚖ llm`` label."""
        row = {"risk_level": "high", "tier": "llm", "judge_model": "claude-haiku-4"}
        out = build_verdict_payload(row)
        assert out is not None
        assert out["judge_model"] == "claude-haiku-4"

    def test_omits_judge_model_when_empty(self) -> None:
        row = {"risk_level": "medium", "tier": "heuristic", "judge_model": ""}
        out = build_verdict_payload(row)
        assert out is not None
        assert "judge_model" not in out


class TestBuildOutputAssessmentPayload:
    """Output-guard wire shape — flags decoded from JSON string at
    this layer so the client never has to parse twice."""

    def test_skips_unflagged_baseline(self) -> None:
        row = {"risk_level": "none", "flags": "[]"}
        assert build_output_assessment_payload(row) is None

    def test_decodes_flags_from_json(self) -> None:
        row = {"risk_level": "high", "flags": '["api_key","email"]', "redacted": 1}
        out = build_output_assessment_payload(row)
        assert out is not None
        assert out["flags"] == ["api_key", "email"]
        assert out["redacted"] is True
        assert out["risk_level"] == "high"

    def test_handles_malformed_flags_json(self) -> None:
        """Bad JSON in ``flags`` must not block the rest of the
        assessment from rendering — degrade to empty list."""
        row = {"risk_level": "medium", "flags": "not-json", "redacted": 0}
        out = build_output_assessment_payload(row)
        assert out is not None
        assert out["flags"] == []
        assert out["redacted"] is False


class TestDecorateToolCall:
    """In-place mutation of either OpenAI-format or flattened tool_call
    entries — both shapes carry ``id`` at the top level."""

    def test_attaches_verdict_when_present(self) -> None:
        tc: dict[str, object] = {"id": "call_1", "function": {"name": "bash", "arguments": "{}"}}
        verdicts = {
            "call_1": {
                "risk_level": "medium",
                "recommendation": "review",
                "confidence": 0.7,
                "intent_summary": "summary",
                "tier": "heuristic",
            }
        }
        decorate_tool_call(tc, verdicts, {})
        assert "verdict" in tc
        assert tc["verdict"]["risk_level"] == "medium"  # type: ignore[index]

    def test_skips_when_no_call_id_match(self) -> None:
        tc: dict[str, object] = {"id": "call_other", "name": "bash"}
        verdicts = {
            "call_1": {"risk_level": "medium", "tier": "heuristic"},
        }
        decorate_tool_call(tc, verdicts, {})
        assert "verdict" not in tc

    def test_skips_unflagged_verdict(self) -> None:
        """``build_verdict_payload`` returns None for unflagged rows;
        decorate_tool_call must not stamp ``verdict`` in that case."""
        tc: dict[str, object] = {"id": "call_1", "name": "bash"}
        verdicts = {"call_1": {"risk_level": "none", "tier": "heuristic"}}
        decorate_tool_call(tc, verdicts, {})
        assert "verdict" not in tc

    def test_handles_empty_id(self) -> None:
        """A tool_call with no id can't be paired against the lookup
        table — must not raise (or stamp the wrong row's verdict)."""
        tc: dict[str, object] = {"id": "", "name": "bash"}
        verdicts = {"call_1": {"risk_level": "high", "tier": "heuristic"}}
        decorate_tool_call(tc, verdicts, {})
        assert "verdict" not in tc


class TestDecorateHistoryMessages:
    """End-to-end mutation of a /history-shaped message list — covers
    the full transform applied by ``make_history_handler``."""

    def test_decorates_tool_calls_and_marks_truncated(self) -> None:
        verdicts = {
            "call_a": {
                "risk_level": "high",
                "recommendation": "deny",
                "confidence": 0.95,
                "intent_summary": "exfil",
                "tier": "llm",
                "reasoning": "ssh key access",
            }
        }
        assessments = {
            "call_a": {"risk_level": "high", "flags": '["secret"]', "redacted": 1},
        }
        # Tool result content of exactly TOOL_RESULT_STORAGE_CAP chars
        # hits the storage cap (longer is impossible — storage clamps
        # at the cap).  Reference the constant rather than a literal so
        # this test stays correct if the cap moves again.
        from turnstone.core.history_decoration import TOOL_RESULT_STORAGE_CAP

        truncated_content = "x" * TOOL_RESULT_STORAGE_CAP
        messages: list[dict[str, object]] = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "running",
                "tool_calls": [
                    {
                        "id": "call_a",
                        "function": {"name": "bash", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_a", "content": truncated_content},
            {"role": "tool", "tool_call_id": "call_b", "content": "short"},
        ]
        decorate_history_messages(messages, verdicts, assessments)
        # Assistant tool_calls got both decorations.
        tc = messages[1]["tool_calls"][0]  # type: ignore[index]
        assert tc["verdict"]["risk_level"] == "high"
        assert tc["verdict"]["tier"] == "llm"
        assert "reasoning" in tc["verdict"]
        assert tc["output_assessment"]["flags"] == ["secret"]
        assert tc["output_assessment"]["redacted"] is True
        # Truncated tool message got the flag; the short one did not.
        assert messages[2].get("truncated") is True
        assert "truncated" not in messages[3]

    def test_no_op_on_empty_indexes(self) -> None:
        """When neither table has rows for the workstream, the wire
        shape passes through unchanged — replay must degrade
        gracefully when verdict storage is empty / unavailable."""
        messages: list[dict[str, object]] = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_a", "function": {"name": "bash", "arguments": "{}"}}],
            },
        ]
        decorate_history_messages(messages, {}, {})
        tc = messages[0]["tool_calls"][0]  # type: ignore[index]
        assert "verdict" not in tc
        assert "output_assessment" not in tc
