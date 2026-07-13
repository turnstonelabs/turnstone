"""Unit tests for ``turnstone.core.history_decoration``.

The decoration helpers compose the single ``/history`` REST projection
pipeline (``make_history_handler``, used by both interactive and coord):
``decorate_history_messages`` + ``extract_reasoning_for_history`` +
``project_history_messages``. Pinning the wire shape here lets a future
schema/projection change land in one file.
"""

from __future__ import annotations

import json

from turnstone.core.history_decoration import (
    build_merged_output_assessment_payload,
    build_verdict_payload,
    decorate_history_messages,
    decorate_tool_call,
    load_verdict_indexes,
)


class TestBuildVerdictPayload:
    """The wire-shape projection that's the single source of truth for
    what intent_verdict fields ship to the client."""

    def test_ships_unflagged_baseline(self) -> None:
        """``risk_level`` "none" rows ship like any other — the live
        path paints a badge for every delivered verdict (the client
        has no risk filter), so replay must carry the same set or
        benign verdicts vanish on rehydrate (live/replay parity)."""
        row = {"risk_level": "none", "recommendation": "approve", "tier": "llm"}
        out = build_verdict_payload(row)
        assert out["risk_level"] == "none"
        assert out["recommendation"] == "approve"
        assert out["tier"] == "llm"

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


class TestBuildMergedOutputAssessmentPayload:
    """Replay-side merge of the heuristic + LLM rows into one chip payload.

    Delegates to ``output_guard.merge_guard_display_payload`` — the same
    projection the live ``on_output_warning`` path calls — so the inline
    finding chip renders identically live and on reconnect.  ``slot`` is
    ``{"heuristic": row|None, "llm": row|None}`` from ``load_verdict_indexes``.
    """

    def test_skips_unflagged_baseline(self) -> None:
        slot = {"heuristic": {"risk_level": "none", "flags": "[]"}, "llm": None}
        assert build_merged_output_assessment_payload(slot) is None

    def test_decodes_heuristic_flags_from_json(self) -> None:
        slot = {
            "heuristic": {"risk_level": "high", "flags": '["api_key","email"]', "redacted": 1},
            "llm": None,
        }
        out = build_merged_output_assessment_payload(slot)
        assert out is not None
        assert out["flags"] == ["api_key", "email"]
        assert out["redacted"] is True
        assert out["risk_level"] == "high"
        assert out["tier"] == "heuristic"

    def test_handles_malformed_flags_json(self) -> None:
        """Bad JSON in ``flags`` must not block the rest of the
        assessment from rendering — degrade to empty list."""
        slot = {
            "heuristic": {"risk_level": "medium", "flags": "not-json", "redacted": 0},
            "llm": None,
        }
        out = build_merged_output_assessment_payload(slot)
        assert out is not None
        assert out["flags"] == []
        assert out["redacted"] is False

    def test_llm_escalates_over_clean_heuristic(self) -> None:
        """LLM positive on a clean heuristic surfaces under tier='llm' with
        the judge's own risk/confidence/reasoning/model as annotation."""
        slot = {
            "heuristic": {"risk_level": "none", "flags": "[]", "redacted": 0},
            "llm": {
                "risk_level": "medium",
                "flags": '["camouflaged_injection"]',
                "reasoning": "Authority-framed directive embedded in the doc.",
                "confidence": 0.82,
                "judge_model": "gpt-5-mini",
            },
        }
        out = build_merged_output_assessment_payload(slot)
        assert out is not None
        assert out["risk_level"] == "medium"
        assert out["flags"] == ["camouflaged_injection"]
        assert out["tier"] == "llm"
        assert out["judge_risk"] == "medium"
        assert out["confidence"] == 0.82
        assert out["reasoning"] == "Authority-framed directive embedded in the doc."
        assert out["judge_model"] == "gpt-5-mini"

    def test_llm_none_does_not_lower_heuristic_positive(self) -> None:
        """Core merge rule + the vanishing-chip fix: a successful LLM "none"
        never lowers a heuristic positive — it surfaces, annotated with the
        judge's dissent (judge_risk="none" differs from the displayed risk)."""
        slot = {
            "heuristic": {
                "risk_level": "medium",
                "flags": '["camouflaged_injection"]',
                "redacted": 0,
            },
            "llm": {
                "risk_level": "none",
                "flags": "[]",
                "reasoning": "Benign analyst commentary.",
                "confidence": 0.9,
                "judge_model": "gpt-5-mini",
            },
        }
        out = build_merged_output_assessment_payload(slot)
        assert out is not None
        assert out["risk_level"] == "medium"  # heuristic survives
        assert out["flags"] == ["camouflaged_injection"]
        assert out["tier"] == "llm"
        assert out["judge_risk"] == "none"  # judge's dissent, drives the badge
        assert out["reasoning"] == "Benign analyst commentary."

    def test_flags_are_unioned_and_deduped(self) -> None:
        slot = {
            "heuristic": {
                "risk_level": "high",
                "flags": '["prompt_injection","credential_leak"]',
                "redacted": 0,
            },
            "llm": {
                "risk_level": "high",
                "flags": '["prompt_injection","data_exfiltration"]',
                "reasoning": "x",
                "confidence": 0.9,
                "judge_model": "m",
            },
        }
        out = build_merged_output_assessment_payload(slot)
        assert out is not None
        assert out["flags"] == ["prompt_injection", "credential_leak", "data_exfiltration"]

    def test_heuristic_only_has_no_llm_badge(self) -> None:
        """A regex-only finding (no LLM slot) carries no LLM attribution."""
        slot = {
            "heuristic": {"risk_level": "high", "flags": '["credential_leak"]', "redacted": 1},
            "llm": None,
        }
        out = build_merged_output_assessment_payload(slot)
        assert out is not None
        assert out["tier"] == "heuristic"
        assert "judge_risk" not in out
        assert "confidence" not in out
        assert "reasoning" not in out
        assert "judge_model" not in out


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

    def test_stamps_unflagged_verdict(self) -> None:
        """A ``risk_level="none"`` row still stamps ``verdict`` — the
        operator saw the badge live, so it must survive rehydrate."""
        tc: dict[str, object] = {"id": "call_1", "name": "bash"}
        verdicts = {"call_1": {"risk_level": "none", "tier": "llm"}}
        decorate_tool_call(tc, verdicts, {})
        assert "verdict" in tc
        assert tc["verdict"]["risk_level"] == "none"  # type: ignore[index]

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
            "call_a": {
                "heuristic": {"risk_level": "high", "flags": '["secret"]', "redacted": 1},
                "llm": None,
            },
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

    def test_parallel_batch_keeps_every_judged_verdict(self) -> None:
        """Regression: a parallel batch where the judge cleared most
        calls (``risk_level="none"``) must rehydrate with a verdict on
        EVERY judged call, not just the flagged minority.  The old
        wire-layer ``none`` filter made benign verdicts vanish after a
        restart while the live stream had shown all of them."""
        calls = [f"call_{i}" for i in range(8)]
        verdicts = {
            cid: {
                "risk_level": "low" if i < 2 else "none",
                "recommendation": "approve",
                "confidence": 0.9,
                "intent_summary": f"benign op {i}",
                "tier": "llm",
            }
            for i, cid in enumerate(calls)
        }
        messages: list[dict[str, object]] = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": cid, "function": {"name": "read_file", "arguments": "{}"}}
                    for cid in calls
                ],
            },
        ]
        decorate_history_messages(messages, verdicts, {})
        tool_calls = messages[0]["tool_calls"]  # type: ignore[index]
        decorated = [tc["verdict"]["risk_level"] for tc in tool_calls]
        assert decorated == ["low", "low"] + ["none"] * 6

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


class TestExtractReasoningForHistory:
    """``extract_reasoning_for_history`` — Phase 1 surfaces stored
    Anthropic thinking blocks on assistant messages and strips
    ``_provider_content`` from the wire payload.

    Drives through the real ``AnthropicProvider.extract_reasoning_text``
    (no mock-of-extractor) — the helper test and the provider unit
    test (``tests/test_provider_anthropic_reasoning.py``) together
    catch a regression at either layer distinctly.
    """

    def _anthropic_thinking_msg(self, text: str = "let me think") -> dict[str, object]:
        return {
            "role": "assistant",
            "content": "Final answer.",
            "_provider_content": [
                {"type": "thinking", "thinking": text, "signature": "sig"},
                {"type": "text", "text": "Final answer."},
            ],
        }

    def test_extract_thinking_surfaces_reasoning_field(self) -> None:
        from turnstone.core.history_decoration import extract_reasoning_for_history

        messages = [self._anthropic_thinking_msg("let me think")]
        extract_reasoning_for_history(messages, surface_persisted_reasoning_flag=True)
        assert messages[0]["reasoning"] == "let me think"

    def test_strips_provider_content_after_extraction(self) -> None:
        from turnstone.core.history_decoration import extract_reasoning_for_history

        messages = [self._anthropic_thinking_msg("anything")]
        extract_reasoning_for_history(messages, surface_persisted_reasoning_flag=True)
        assert "_provider_content" not in messages[0]

    def test_strips_provider_content_when_flag_false(self) -> None:
        from turnstone.core.history_decoration import extract_reasoning_for_history

        messages = [self._anthropic_thinking_msg("anything")]
        extract_reasoning_for_history(messages, surface_persisted_reasoning_flag=False)
        # Strip is unconditional; reasoning is the conditional bit.
        assert "_provider_content" not in messages[0]
        assert "reasoning" not in messages[0]

    def test_first_block_thinking_dispatches_to_anthropic(self) -> None:
        # Even when text and tool_use blocks follow, the first-block-type
        # discriminator routes thinking-prefixed payloads correctly.
        from turnstone.core.history_decoration import extract_reasoning_for_history

        messages = [
            {
                "role": "assistant",
                "content": "x",
                "_provider_content": [
                    {"type": "thinking", "thinking": "first", "signature": "s"},
                    {"type": "text", "text": "spoken"},
                    {"type": "tool_use", "id": "t1", "name": "f", "input": {}},
                ],
            }
        ]
        extract_reasoning_for_history(messages, surface_persisted_reasoning_flag=True)
        assert messages[0]["reasoning"] == "first"

    def test_first_block_reasoning_dispatches_to_openai_responses(self) -> None:
        # Phase 3: dispatcher routes type=="reasoning" to the
        # OpenAI Responses extractor, which now returns the
        # summary[*].text concatenation.  Pre-Phase-3 this asserted
        # "" (the stub); the assertion was tightened once the wire
        # path landed.
        from turnstone.core.history_decoration import extract_reasoning_for_history

        messages = [
            {
                "role": "assistant",
                "content": "x",
                "_provider_content": [
                    {"type": "reasoning", "summary": [{"type": "summary_text", "text": "s"}]}
                ],
            }
        ]
        extract_reasoning_for_history(messages, surface_persisted_reasoning_flag=True)
        assert messages[0]["reasoning"] == "s"
        assert "_provider_content" not in messages[0]

    def test_unknown_first_block_type_no_op(self) -> None:
        from turnstone.core.history_decoration import extract_reasoning_for_history

        messages = [
            {
                "role": "assistant",
                "content": "x",
                "_provider_content": [{"type": "text", "text": "no reasoning here"}],
            }
        ]
        extract_reasoning_for_history(messages, surface_persisted_reasoning_flag=True)
        assert "reasoning" not in messages[0]
        assert "_provider_content" not in messages[0]

    def test_skips_messages_without_provider_content(self) -> None:
        from turnstone.core.history_decoration import extract_reasoning_for_history

        messages = [{"role": "assistant", "content": "plain"}]
        extract_reasoning_for_history(messages, surface_persisted_reasoning_flag=True)
        assert "reasoning" not in messages[0]
        assert messages[0]["content"] == "plain"

    def test_user_and_tool_messages_untouched(self) -> None:
        from turnstone.core.history_decoration import extract_reasoning_for_history

        messages: list[dict[str, object]] = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "c1", "content": "out"},
            self._anthropic_thinking_msg("only this one"),
        ]
        extract_reasoning_for_history(messages, surface_persisted_reasoning_flag=True)
        assert "reasoning" not in messages[0]
        assert "reasoning" not in messages[1]
        assert messages[2]["reasoning"] == "only this one"

    def test_empty_provider_content_no_extraction(self) -> None:
        from turnstone.core.history_decoration import extract_reasoning_for_history

        messages = [{"role": "assistant", "content": "x", "_provider_content": []}]
        extract_reasoning_for_history(messages, surface_persisted_reasoning_flag=True)
        assert "reasoning" not in messages[0]
        # Empty-list provider_content is still stripped from the wire.
        assert "_provider_content" not in messages[0]

    def test_first_block_not_a_dict_skipped(self) -> None:
        from turnstone.core.history_decoration import extract_reasoning_for_history

        messages: list[dict[str, object]] = [
            {
                "role": "assistant",
                "content": "x",
                "_provider_content": ["bogus"],
            }
        ]
        extract_reasoning_for_history(messages, surface_persisted_reasoning_flag=True)
        assert "reasoning" not in messages[0]
        assert "_provider_content" not in messages[0]

    def test_first_block_reasoning_text_dispatches_to_openai_chat(self) -> None:
        # Phase 3 path 3: synthetic ``reasoning_text`` blocks (stamped
        # by model_turn.synth_reasoning_block for vLLM / llama.cpp /
        # Gemini-compat conversations) dispatch to
        # OpenAIChatCompletionsProvider.extract_reasoning_text.
        from turnstone.core.history_decoration import extract_reasoning_for_history

        messages = [
            {
                "role": "assistant",
                "content": "answer",
                "_provider_content": [
                    {"type": "reasoning_text", "text": "synth thought", "source": "vllm"},
                ],
            }
        ]
        extract_reasoning_for_history(messages, surface_persisted_reasoning_flag=True)
        assert messages[0]["reasoning"] == "synth thought"
        assert "_provider_content" not in messages[0]

    def test_dispatcher_scans_past_unrecognized_first_blocks(self) -> None:
        # Regression for Copilot finding: dispatcher used to inspect
        # only provider_content[0]['type'].  OpenAI Responses captures
        # EVERY output_item.done event into provider_blocks (not just
        # reasoning), so a hypothetical [message, reasoning, ...]
        # ordering would have silently dropped the reasoning.  Now
        # walks the list for the first recognised reasoning-bearing
        # type and dispatches the whole list to that provider.
        from turnstone.core.history_decoration import extract_reasoning_for_history

        messages = [
            {
                "role": "assistant",
                "content": "answer",
                "_provider_content": [
                    # First block is a non-reasoning OpenAI Responses item.
                    {"type": "message", "role": "assistant", "content": "answer"},
                    # Reasoning sits later in the list.
                    {
                        "type": "reasoning",
                        "id": "r_1",
                        "summary": [{"type": "summary_text", "text": "deferred"}],
                    },
                ],
            }
        ]
        extract_reasoning_for_history(messages, surface_persisted_reasoning_flag=True)
        assert messages[0]["reasoning"] == "deferred"
        assert "_provider_content" not in messages[0]

    def test_first_block_redacted_thinking_dispatches_to_anthropic(self) -> None:
        # Anthropic's extended-thinking API documents that
        # ``redacted_thinking`` blocks (sealed by the safety system)
        # can appear before, after, or interleaved with regular
        # ``thinking`` blocks.  When the redacted block lands first,
        # the dispatcher must still route to AnthropicProvider so the
        # surrounding real thinking text surfaces — without this the
        # reasoning bubble silently disappears on history rehydration.
        # Pinned by registering "redacted_thinking" as a second key
        # in _BLOCK_TYPE_PROVIDER_FACTORY pointing at the Anthropic
        # factory; Anthropic's extractor's type=="thinking" filter
        # already correctly skips the redacted block.
        from turnstone.core.history_decoration import extract_reasoning_for_history

        messages = [
            {
                "role": "assistant",
                "content": "answer",
                "_provider_content": [
                    {"type": "redacted_thinking", "data": "sealed-blob"},
                    {"type": "thinking", "thinking": "real thought", "signature": "s"},
                    {"type": "text", "text": "answer"},
                ],
            }
        ]
        extract_reasoning_for_history(messages, surface_persisted_reasoning_flag=True)
        assert messages[0]["reasoning"] == "real thought"
        assert "_provider_content" not in messages[0]


class TestAttachVllmChatReasoningField:
    """``attach_vllm_chat_reasoning_field`` — Phase 5 surfaces persisted
    reasoning as the vLLM-specific ``reasoning`` field on outgoing
    assistant messages so vLLM-served reasoning models can thread CoT
    across turns.

    Drives through the real ``extract_reasoning_text_from_provider_content``
    dispatcher — no extractor mocks — so a regression in either layer
    surfaces distinctly.  All 3 caller-side gates (provider isinstance,
    server_type, operator flag) are exercised by
    ``test_session_chat_reasoning_replay.py``; this class pins the
    helper's projection contract in isolation.
    """

    def _assistant_with(self, provider_content: list[dict[str, object]]) -> dict[str, object]:
        return {
            "role": "assistant",
            "content": "Final answer.",
            "_provider_content": provider_content,
        }

    def test_synthetic_reasoning_text_attaches_field(self) -> None:
        # Path 3 capture (vLLM --reasoning-parser, llama.cpp
        # reasoning_format, Gemini-compat) lands in _provider_content as
        # a synthetic reasoning_text block; helper must round-trip it
        # back onto the same model on the next turn.
        from turnstone.core.history_decoration import attach_vllm_chat_reasoning_field

        msgs = [self._assistant_with([{"type": "reasoning_text", "text": "synth thought"}])]
        out = attach_vllm_chat_reasoning_field(msgs)
        assert out[0]["reasoning"] == "synth thought"

    def test_anthropic_thinking_attaches_field(self) -> None:
        # Cross-provider switch: workstream started with Anthropic,
        # operator flipped model to a vLLM-served reasoning model.
        # Helper extracts the thinking text and discards the signature
        # (vLLM doesn't validate signatures).
        from turnstone.core.history_decoration import attach_vllm_chat_reasoning_field

        msgs = [
            self._assistant_with(
                [
                    {"type": "thinking", "thinking": "claude was here", "signature": "sig"},
                    {"type": "text", "text": "answer"},
                ]
            )
        ]
        out = attach_vllm_chat_reasoning_field(msgs)
        assert out[0]["reasoning"] == "claude was here"
        # Signature is dropped at extraction; ``reasoning`` field carries
        # plain text only.
        assert "sig" not in out[0]["reasoning"]

    def test_openai_responses_reasoning_attaches_field(self) -> None:
        # Cross-provider switch: workstream started on gpt-5, operator
        # flipped to a vLLM-served model.  Helper extracts the
        # summary[*].text concatenation.
        from turnstone.core.history_decoration import attach_vllm_chat_reasoning_field

        msgs = [
            self._assistant_with(
                [
                    {
                        "type": "reasoning",
                        "id": "r_1",
                        "summary": [{"type": "summary_text", "text": "responses thought"}],
                    }
                ]
            )
        ]
        out = attach_vllm_chat_reasoning_field(msgs)
        assert out[0]["reasoning"] == "responses thought"

    def test_no_provider_content_returns_unchanged(self) -> None:
        from turnstone.core.history_decoration import attach_vllm_chat_reasoning_field

        msgs: list[dict[str, object]] = [{"role": "assistant", "content": "plain"}]
        out = attach_vllm_chat_reasoning_field(msgs)
        assert "reasoning" not in out[0]
        # No copy made when there's nothing to attach — same object.
        assert out[0] is msgs[0]

    def test_empty_provider_content_returns_unchanged(self) -> None:
        from turnstone.core.history_decoration import attach_vllm_chat_reasoning_field

        msgs: list[dict[str, object]] = [
            {"role": "assistant", "content": "x", "_provider_content": []}
        ]
        out = attach_vllm_chat_reasoning_field(msgs)
        assert "reasoning" not in out[0]
        assert out[0] is msgs[0]

    def test_unknown_block_type_returns_unchanged(self) -> None:
        # _provider_content has blocks but none are reasoning-bearing.
        from turnstone.core.history_decoration import attach_vllm_chat_reasoning_field

        msgs: list[dict[str, object]] = [
            self._assistant_with([{"type": "text", "text": "no reasoning here"}])
        ]
        out = attach_vllm_chat_reasoning_field(msgs)
        assert "reasoning" not in out[0]
        assert out[0] is msgs[0]

    def test_does_not_touch_user_tool_system_messages(self) -> None:
        # Only assistant messages get the reasoning field.  User / tool /
        # system messages pass through by reference.
        from turnstone.core.history_decoration import attach_vllm_chat_reasoning_field

        msgs: list[dict[str, object]] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "c1", "content": "out"},
            # Even an assistant-shaped non-assistant role (defensive — shouldn't happen)
            # must not have provider_content read.
        ]
        out = attach_vllm_chat_reasoning_field(msgs)
        assert "reasoning" not in out[0]
        assert "reasoning" not in out[1]
        assert "reasoning" not in out[2]
        # All three return by reference (no allocation when no attach).
        for original, returned in zip(msgs, out, strict=True):
            assert original is returned

    def test_preserves_provider_content_for_downstream_sanitize(self) -> None:
        # Helper attaches ``reasoning`` but leaves ``_provider_content``
        # in place.  Downstream ``sanitize_messages`` (in the provider's
        # _prepare_messages) strips the ``_``-prefixed sibling key
        # before the wire payload leaves.  Helper isn't responsible for
        # that strip — composition with sanitize is the contract.
        from turnstone.core.history_decoration import attach_vllm_chat_reasoning_field

        original_content = [{"type": "reasoning_text", "text": "kept"}]
        msgs = [self._assistant_with(original_content)]
        out = attach_vllm_chat_reasoning_field(msgs)
        assert out[0]["reasoning"] == "kept"
        # Provider content survives on the helper's output dict.
        assert out[0]["_provider_content"] == original_content

    def test_does_not_mutate_input_messages(self) -> None:
        # Pure transform: input list and input dicts are untouched.
        # Callers can keep iterating the original list without surprise.
        from turnstone.core.history_decoration import attach_vllm_chat_reasoning_field

        original = self._assistant_with([{"type": "reasoning_text", "text": "x"}])
        msgs = [original]
        attach_vllm_chat_reasoning_field(msgs)
        assert "reasoning" not in original
        # Original dict untouched even though the function returned a
        # modified copy.

    def test_mixed_messages_only_attaches_to_assistants_with_reasoning(self) -> None:
        # Realistic shape: a workstream with user, assistant-with-reasoning,
        # tool, assistant-plain, user.  Only the first assistant gets the
        # reasoning field; everything else passes through by reference.
        from turnstone.core.history_decoration import attach_vllm_chat_reasoning_field

        with_reasoning = self._assistant_with([{"type": "reasoning_text", "text": "thinking"}])
        plain_assistant: dict[str, object] = {"role": "assistant", "content": "second"}
        msgs: list[dict[str, object]] = [
            {"role": "user", "content": "q1"},
            with_reasoning,
            {"role": "tool", "tool_call_id": "c1", "content": "result"},
            plain_assistant,
            {"role": "user", "content": "q2"},
        ]
        out = attach_vllm_chat_reasoning_field(msgs)
        assert out[0] is msgs[0]
        assert out[1]["reasoning"] == "thinking"
        assert out[1] is not with_reasoning  # new dict for the attached one
        assert out[2] is msgs[2]
        assert out[3] is plain_assistant
        assert "reasoning" not in out[3]
        assert out[4] is msgs[4]


class TestLoadVerdictIndexesMerge:
    """load_verdict_indexes + the merge, against real storage — pins the
    vanishing-chip fix (failed-judge row must not hide a heuristic finding)
    and the de-escalation annotate behavior end to end."""

    def _record(self, storage, **kw) -> None:
        base = {
            "func_name": "read_file",
            "flags": "[]",
            "annotations": "[]",
            "output_length": 900,
            "redacted": False,
        }
        base.update(kw)
        storage.record_output_assessment(**base)

    def test_llm_error_row_does_not_shadow_heuristic(self, storage_backend) -> None:
        """A failed-judge row (tier='llm_error', risk='none') is audit-only and
        must NOT win the replay merge over a real heuristic finding — the bug
        behind the chip that showed live but vanished on reconnect."""
        ws_id, call_id = "ws-merge-err", "call-env"
        self._record(
            storage_backend,
            assessment_id="a-h",
            ws_id=ws_id,
            call_id=call_id,
            flags=json.dumps(["credential_leak", "env_file_leak"]),
            risk_level="high",
            redacted=True,
            tier="heuristic",
        )
        self._record(
            storage_backend,
            assessment_id="a-e",
            ws_id=ws_id,
            call_id=call_id,
            risk_level="none",
            tier="llm_error",
            reasoning="timeout",
        )
        _verdicts, assessments = load_verdict_indexes(ws_id)
        # The llm_error row is dropped at load — slot has only the heuristic.
        assert assessments[call_id]["llm"] is None
        out = build_merged_output_assessment_payload(assessments[call_id])
        assert out is not None
        assert out["risk_level"] == "high"
        assert "credential_leak" in out["flags"]
        assert out["tier"] == "heuristic"  # failed judge → no LLM badge

    def test_llm_clear_annotates_heuristic_on_replay(self, storage_backend) -> None:
        """A successful LLM "none" on a heuristic positive surfaces the
        heuristic finding on reconnect, annotated with the judge's dissent."""
        ws_id, call_id = "ws-merge-clear", "call-doc"
        self._record(
            storage_backend,
            assessment_id="b-h",
            ws_id=ws_id,
            call_id=call_id,
            func_name="web_fetch",
            flags=json.dumps(["camouflaged_injection"]),
            risk_level="medium",
            tier="heuristic",
        )
        self._record(
            storage_backend,
            assessment_id="b-l",
            ws_id=ws_id,
            call_id=call_id,
            func_name="web_fetch",
            risk_level="none",
            tier="llm",
            reasoning="Benign analyst commentary.",
            confidence=0.9,
            judge_model="gpt-5-mini",
        )
        _verdicts, assessments = load_verdict_indexes(ws_id)
        out = build_merged_output_assessment_payload(assessments[call_id])
        assert out is not None
        assert out["risk_level"] == "medium"  # heuristic survives
        assert out["tier"] == "llm"
        assert out["judge_risk"] == "none"
        assert out["reasoning"] == "Benign analyst commentary."
