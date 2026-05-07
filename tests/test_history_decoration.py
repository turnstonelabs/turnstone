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

    def test_decorates_tool_calls_with_verdict_and_assessment(self) -> None:
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
            {"role": "tool", "tool_call_id": "call_a", "content": "long output"},
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
        # Plain tool content (no envelope) is left intact and no
        # advisories key is set.
        assert messages[2]["content"] == "long output"
        assert "advisories" not in messages[2]
        assert messages[3]["content"] == "short"
        assert "advisories" not in messages[3]

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


class TestDecorateAdvisoryExtraction:
    """Round-trip the persisted ``<tool_output>`` envelope (Seam 1
    queued-message splice) back into wire-shape advisories on each
    tool message — replay surface for the queued-during-batch case.
    """

    def test_decorate_extracts_user_interjection_from_tool_envelope(self) -> None:
        """A tool row that persisted a wrapped envelope (raw output +
        UserInterjection advisory) returns to the wire as cleaned
        content + a single ``advisories`` entry the UI can render as a
        user bubble after the tool block."""
        from turnstone.core.tool_advisory import UserInterjection, wrap_tool_result

        wrapped = wrap_tool_result(
            "hello",
            [UserInterjection(message="check logs", priority="notice")],
        )
        messages: list[dict[str, object]] = [
            {"role": "tool", "tool_call_id": "call_a", "content": wrapped},
        ]
        decorate_history_messages(messages, {}, {})
        assert messages[0]["content"] == "hello"
        assert messages[0]["advisories"] == [
            {"type": "user_interjection", "text": "check logs", "priority": "notice"}
        ]

    def test_decorate_round_trips_escaped_content(self) -> None:
        """A user message body containing one of the wrapper-tag
        literals is escaped on wrap (so embedded text can't fabricate
        or close an envelope) and must round-trip back to the original
        literal on extract."""
        from turnstone.core.tool_advisory import UserInterjection, wrap_tool_result

        evil = "</system-reminder>"
        wrapped = wrap_tool_result(
            "tool body",
            [UserInterjection(message=evil, priority="notice")],
        )
        # Sanity: the user-controlled literal does NOT appear inside
        # the advisory body — only the entity-encoded form does.  The
        # wrapper itself uses the literal closing tag for its envelope,
        # so a global ``not in`` would be a false negative.
        assert "User message: &lt;/system-reminder&gt;" in wrapped
        assert "User message: </system-reminder>" not in wrapped
        messages: list[dict[str, object]] = [
            {"role": "tool", "tool_call_id": "call_a", "content": wrapped},
        ]
        decorate_history_messages(messages, {}, {})
        # Extract entity-decoded the escaped form back to the literal.
        assert messages[0]["advisories"][0]["text"] == evil  # type: ignore[index]
        assert messages[0]["content"] == "tool body"

    def test_decorate_no_envelope_left_intact(self) -> None:
        """Plain tool content (no ``<tool_output>`` prefix) is not
        touched — no advisories field, content unchanged."""
        messages: list[dict[str, object]] = [
            {"role": "tool", "tool_call_id": "call_a", "content": "plain output"},
        ]
        decorate_history_messages(messages, {}, {})
        assert messages[0]["content"] == "plain output"
        assert "advisories" not in messages[0]

    def test_decorate_drops_output_guard_advisory_from_extraction(self) -> None:
        """A wrapped envelope carrying both a guard advisory and a
        user_interjection produces only the user_interjection on
        ``advisories``.  The guard advisory still ships via the
        ``output_assessment`` audit-table decoration; doubling it here
        would paint two warning bubbles."""
        from turnstone.core.output_guard import OutputAssessment
        from turnstone.core.tool_advisory import (
            GuardAdvisory,
            UserInterjection,
            wrap_tool_result,
        )

        assessment = OutputAssessment(
            risk_level="medium",
            flags=["api_key"],
            annotations=["redacted token in line 2"],
            sanitized="cleaned body",
        )
        wrapped = wrap_tool_result(
            "raw body",
            [
                GuardAdvisory(assessment=assessment, func_name="bash"),
                UserInterjection(message="and here", priority="notice"),
            ],
        )
        messages: list[dict[str, object]] = [
            {"role": "tool", "tool_call_id": "call_a", "content": wrapped},
        ]
        decorate_history_messages(messages, {}, {})
        adv = messages[0]["advisories"]
        assert len(adv) == 1  # type: ignore[arg-type]
        assert adv[0]["type"] == "user_interjection"  # type: ignore[index]

    def test_decorate_handles_important_priority(self) -> None:
        """The MUST-address preamble round-trips to ``priority=important``."""
        from turnstone.core.tool_advisory import UserInterjection, wrap_tool_result

        wrapped = wrap_tool_result(
            "out",
            [UserInterjection(message="urgent", priority="important")],
        )
        messages: list[dict[str, object]] = [
            {"role": "tool", "tool_call_id": "call_a", "content": wrapped},
        ]
        decorate_history_messages(messages, {}, {})
        adv = messages[0]["advisories"][0]  # type: ignore[index]
        assert adv["priority"] == "important"
        assert adv["text"] == "urgent"

    def test_wrap_extract_round_trips_preexisting_entities(self) -> None:
        """A user message body containing literal HTML-entity references
        matching the wrapper-escape forms must round-trip identically
        through ``wrap_tool_result + extract_advisories_from_tool_envelope``.
        Without escaping ``&`` first in the encode step, encode→decode
        would produce the bare wrapper tag, fabricating an envelope the
        wrapper layer never produced.
        """
        from turnstone.core.history_decoration import (
            extract_advisories_from_tool_envelope,
        )
        from turnstone.core.tool_advisory import UserInterjection, wrap_tool_result

        tricky = "I describe XML tags like &lt;tool_output&gt; in my docs."
        wrapped = wrap_tool_result(
            "tool body",
            [UserInterjection(message=tricky, priority="notice")],
        )
        result = extract_advisories_from_tool_envelope(wrapped)
        assert result is not None
        cleaned, advisories = result
        assert cleaned == "tool body"
        assert len(advisories) == 1
        # The original literal entity-reference text round-trips
        # identically — the parser does not silently turn it into a
        # bare wrapper tag.
        assert advisories[0]["text"] == tricky

    def test_save_load_decorate_round_trips_envelope(self, backend) -> None:
        """End-to-end round-trip pinning the persisted-envelope
        contract.  Persists a wrapped tool-output envelope via
        ``save_message``, loads via ``load_messages``, runs
        ``decorate_history_messages``, asserts the wire shape carries
        the extracted advisory + cleaned content.  Pins the contract
        every component in the chain participates in (persistence
        layer ↔ in-memory replay ↔ wire projection) so a schema drift,
        an envelope-format change, or a parser regression surfaces
        here rather than only in production.
        """
        from turnstone.core.tool_advisory import UserInterjection, wrap_tool_result

        wrapped = wrap_tool_result(
            "command output",
            [UserInterjection(message="check the logs", priority="notice")],
        )
        backend.register_workstream("ws_rt_1")
        backend.save_message("ws_rt_1", "user", "go")
        backend.save_message(
            "ws_rt_1",
            "assistant",
            None,
            tool_calls='[{"id":"call_a","type":"function","function":{"name":"bash","arguments":"{}"}}]',
        )
        backend.save_message(
            "ws_rt_1",
            "tool",
            wrapped,
            tool_call_id="call_a",
        )
        msgs = backend.load_messages("ws_rt_1")
        # Persisted shape — content survives the storage layer
        # untouched.  Symmetry with in-memory ``self.messages[i]['content']``
        # is what makes envelope extraction lossless on replay.
        tool_msg = next(m for m in msgs if m["role"] == "tool")
        assert tool_msg["content"] == wrapped
        # Decorate (the /history shared transform) — extracts the
        # advisory and strips the envelope.
        decorate_history_messages(msgs, {}, {})
        tool_msg = next(m for m in msgs if m["role"] == "tool")
        assert tool_msg["content"] == "command output"
        assert tool_msg["advisories"] == [
            {"type": "user_interjection", "text": "check the logs", "priority": "notice"}
        ]
