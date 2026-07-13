"""Tests for the compaction crossing discipline: what crosses the summary
boundary VERBATIM (not only as summarizer paraphrase) and how the synthetic
summary turns are recognized.

- **Provenance tags** — ``_compact_messages`` and
  ``reconstruct_turns_checkpointed`` mark both synthetic summary turns
  ``source="compaction"``; ``_find_turn_boundaries`` and ``_generate_title``
  test the tag, not the ``[Conversation summary]`` content string.  A user
  who literally types the label therefore stays a REAL turn (previously it
  was silently treated as synthetic — provenance by spelling).
- **Carry budget** — ``_carry_budget_chars`` scales the verbatim-carry
  allowance to ~25% of the window (clamped by the summary output reserve,
  floored at ``_MIN_CARRY_BUDGET_CHARS``), replacing the fixed 400-char
  continuation-hint clip; oversize content keeps head + tail around an
  honest marker.
- **Wind-down spill** — with ``carry_spill=True`` (the end-of-turn site
  passes the ``stopped_to_compact`` latch) the final summarized assistant
  turn's text is copied onto the summary under ``## Wind-down (verbatim)``
  — shell concatenation, so the model's own plan statement survives the
  collapse even when the summarizer paraphrases it.
- The overflow-backstop compact-and-retry passes ``my_generation`` so a
  stale send cannot compact-and-swap a newer generation's history.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests._session_helpers import make_session
from turnstone.core.session import COMPACTION_SOURCE, COMPACTION_SUMMARY_LABEL
from turnstone.core.trajectory import turns_from_dicts


@pytest.fixture
def session(tmp_db, mock_openai_client):
    """Small-window session: context_window=10_000, compact_max_tokens=100 so
    the summary output reserve is tiny and the carry budget is easy to compute
    (reserve=100, margin=500, spare=9_400, budget=min(2_500, 9_400)=2_500
    tokens → 10_000 chars at the uncalibrated 4.0 chars/token)."""
    return make_session(
        client=mock_openai_client,
        context_window=10_000,
        compact_max_tokens=100,
        max_tokens=1_000,
        tool_timeout=10,
    )


def _stub_summary(text: str = "DENSE"):
    return SimpleNamespace(content=text, finish_reason="stop")


# ---------------------------------------------------------------------------
# Provenance tags on the synthetic summary turns
# ---------------------------------------------------------------------------


class TestSummaryTurnProvenance:
    def test_compact_tags_both_summary_turns(self, session):
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "do the thing"},
                {"role": "assistant", "content": "did the thing"},
            ]
        )
        session._msg_tokens = [1, 1]
        with patch.object(session, "_utility_completion", return_value=_stub_summary()):
            assert session._compact_messages(auto=True) is True

        label, summary = session.messages[0], session.messages[1]
        assert label.text == COMPACTION_SUMMARY_LABEL
        assert label.source == COMPACTION_SOURCE
        assert summary.source == COMPACTION_SOURCE

    def test_boundaries_exclude_tagged_label_only(self, session):
        session.messages = turns_from_dicts(
            [
                {
                    "role": "user",
                    "content": COMPACTION_SUMMARY_LABEL,
                    "_source": COMPACTION_SOURCE,
                },
                {"role": "assistant", "content": "summary"},
                {"role": "user", "content": "real follow-up"},
            ]
        )
        assert session._find_turn_boundaries() == [2]

    def test_literal_label_from_user_is_a_real_boundary(self, session):
        """A user who literally types '[Conversation summary]' is not a
        compaction artifact — provenance rides the tag, not the spelling."""
        session.messages = turns_from_dicts([{"role": "user", "content": COMPACTION_SUMMARY_LABEL}])
        assert session._find_turn_boundaries() == [0]

    def test_title_gen_titles_from_literal_label_user(self, session):
        """The tag distinction reaches _generate_title: a synthetic label is
        skipped (pinned in test_cooperative_compaction), but a REAL user
        message that happens to equal the label is titled from normally."""
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": COMPACTION_SUMMARY_LABEL},
                {"role": "assistant", "content": "an answer"},
            ]
        )
        with (
            patch.object(
                session, "_utility_completion", return_value=_stub_summary("A Title")
            ) as uc,
            patch.object(session, "ui", new=MagicMock()),
        ):
            session._generate_title()

        uc.assert_called_once()
        prompt = uc.call_args[0][0][-1].text
        assert COMPACTION_SUMMARY_LABEL in prompt  # titled FROM the real message


class TestCheckpointReconstructionProvenance:
    def test_resume_turns_carry_compaction_source(self, storage_backend):
        """A reopened session must see the same provenance the live session
        held: reconstruct_turns_checkpointed tags the synthetic label AND the
        marker-backed summary turn, while real tail rows stay untagged."""
        st = storage_backend
        st.register_workstream("ws1", user_id="u1", title="t", kind="interactive")
        st.save_message("ws1", "user", "old question")
        st.save_message("ws1", "assistant", "old answer")
        watermark = st.get_compaction_watermark("ws1", 0)
        st.save_message(
            "ws1",
            "assistant",
            "THE SUMMARY",
            source=COMPACTION_SOURCE,
            meta=json.dumps({"watermark": watermark}),
        )
        st.save_message("ws1", "user", "new question")

        turns = st.load_message_turns("ws1")
        assert [t.text for t in turns] == [
            COMPACTION_SUMMARY_LABEL,
            "THE SUMMARY",
            "new question",
        ]
        assert turns[0].source == COMPACTION_SOURCE
        assert turns[1].source == COMPACTION_SOURCE
        assert turns[2].source is None


# ---------------------------------------------------------------------------
# Carry budget — the verbatim-crossing allowance
# ---------------------------------------------------------------------------


def _isolate_overhead(s, system_tokens: int = 0) -> None:
    """Pin the fixed prompt overhead (system + tool defs) for exact budget
    arithmetic — the real values vary with the composed prompt and registered
    tools (same isolation pattern as TestRemainingTokenBudget)."""
    s._system_tokens = system_tokens
    s._tools = []


class TestCarryBudget:
    def test_scales_to_quarter_window(self, session):
        # overhead=0, reserve=100 (compact_max_tokens), margin=500,
        # spare=9_400; min(10_000 // 4, 9_400) = 2_500 tokens * 4.0 chars/token.
        _isolate_overhead(session)
        assert session._carry_budget_chars() == 10_000

    def test_floors_on_tiny_window(self, tmp_db, mock_openai_client):
        tiny = make_session(client=mock_openai_client, context_window=1_000, tool_timeout=10)
        _isolate_overhead(tiny)
        assert tiny._carry_budget_chars() == tiny._MIN_CARRY_BUDGET_CHARS

    @pytest.mark.parametrize("carries", [1, 2])
    def test_overhead_reserve_and_carries_fit_window_at_shipped_defaults(
        self, tmp_db, mock_openai_client, carries
    ):
        """The invariant that prevents a carry-induced overflow, pinned at the
        SHIPPED defaults (budget bugs hide behind test-sized configs), for
        BOTH carry counts, and INCLUDING the fixed prompt overhead: the
        post-compaction prompt is system + tools + summary + carries, so a
        budget that ignores the overhead (or sizes carries independently)
        stacks past the window and the backstop re-compacts the carries
        away."""
        s = make_session(client=mock_openai_client, tool_timeout=10)
        _isolate_overhead(s, system_tokens=4_000)  # a chunky composed prompt
        reserve = s._summary_output_tokens()
        per_carry_tokens = s._carry_budget_chars(carries) / s._chars_per_token
        margin = int(s.context_window * s._SUMMARY_SAFETY_MARGIN)
        assert 4_000 + reserve + carries * per_carry_tokens + margin <= s.context_window

    def test_budget_shrinks_with_prompt_overhead(self, tmp_db, mock_openai_client):
        """Monotonicity pin: the overhead term is genuinely in the formula —
        a bigger system prompt leaves less to carry."""
        s = make_session(client=mock_openai_client, tool_timeout=10)
        _isolate_overhead(s, system_tokens=0)
        roomy = s._carry_budget_chars(2)
        _isolate_overhead(s, system_tokens=8_000)
        assert s._carry_budget_chars(2) < roomy

    def test_double_carry_splits_the_spare(self, tmp_db, mock_openai_client):
        """At shipped defaults the spare (window − overhead − reserve −
        margin) binds two carries: each gets spare // 2, strictly less than
        the solo quarter-window allowance."""
        s = make_session(client=mock_openai_client, tool_timeout=10)
        _isolate_overhead(s, system_tokens=2_000)
        reserve = s._summary_output_tokens()
        margin = int(s.context_window * s._SUMMARY_SAFETY_MARGIN)
        spare = s.context_window - reserve - margin - 2_000
        assert s._carry_budget_chars(2) == int((spare // 2) * s._chars_per_token)
        assert s._carry_budget_chars(2) < s._carry_budget_chars(1)


class TestContinuationHintCarry:
    def test_long_ask_crosses_verbatim(self, session):
        """A 3_000-char user message is within the 10_000-char carry budget and
        must cross whole — the old fixed clip kept 400 chars of it."""
        ask = "spec line\n" * 300  # 3_000 chars
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": ask},
                {"role": "assistant", "content": "working on it"},
            ]
        )
        session._msg_tokens = [1, 1]
        with patch.object(session, "_utility_completion", return_value=_stub_summary()):
            assert session._compact_messages(auto=True) is True

        summary_text = session.messages[1].text or ""
        assert ask.strip() in summary_text  # verbatim, not clipped
        assert "## Continue" in summary_text

    def test_oversize_ask_keeps_head_and_tail_with_marker(self, session):
        head_sentinel = "HEAD-OF-SPEC"
        tail_sentinel = "TAIL-OF-SPEC"
        ask = head_sentinel + ("x" * 20_000) + tail_sentinel  # over the 10_000 budget
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": ask},
                {"role": "assistant", "content": "working on it"},
            ]
        )
        session._msg_tokens = [1, 1]
        with patch.object(session, "_utility_completion", return_value=_stub_summary()):
            assert session._compact_messages(auto=True) is True

        summary_text = session.messages[1].text or ""
        assert head_sentinel in summary_text
        assert tail_sentinel in summary_text
        # The marker reports the ORIGINAL size, and the summary tells the
        # model the full text is retrievable — a truncated carry is a cache
        # miss with a pointer, not a silent loss.
        assert f"…[truncated — {len(ask):,} chars total]…" in summary_text
        assert "the recall tool can retrieve it" in summary_text
        assert ask not in summary_text  # genuinely truncated

    def test_untruncated_carry_gets_no_recall_pointer(self, session):
        """The retrievability note appears ONLY when something was cut."""
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "short ask"},
                {"role": "assistant", "content": "working on it"},
            ]
        )
        session._msg_tokens = [1, 1]
        with patch.object(session, "_utility_completion", return_value=_stub_summary()):
            assert session._compact_messages(auto=True) is True
        assert "recall tool" not in (session.messages[1].text or "")


# ---------------------------------------------------------------------------
# Wind-down spill — the model's plan statement crosses verbatim
# ---------------------------------------------------------------------------


class TestWindDownSpill:
    SPILL = (
        "Goal: finish the migration.\n"
        "Remaining: backfill rows 300-900, rerun the verifier.\n"
        "Next step: resume at scripts/backfill.py --from 300."
    )

    def _compacted_summary(self, session, *, carry_spill: bool) -> str:
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "please migrate the database"},
                {"role": "assistant", "content": self.SPILL},
            ]
        )
        session._msg_tokens = [1, 1]
        with patch.object(session, "_utility_completion", return_value=_stub_summary()):
            assert session._compact_messages(auto=True, carry_spill=carry_spill) is True
        return session.messages[1].text or ""

    def test_spill_copied_verbatim_under_heading(self, session):
        summary_text = self._compacted_summary(session, carry_spill=True)
        assert "## Wind-down (verbatim)" in summary_text
        assert self.SPILL in summary_text  # copied, not paraphrased
        # Ordering: recorded plan first, then how to resume.
        assert summary_text.index("## Wind-down (verbatim)") < summary_text.index("## Continue")

    def test_no_spill_without_flag(self, session):
        summary_text = self._compacted_summary(session, carry_spill=False)
        assert "## Wind-down (verbatim)" not in summary_text

    def test_no_spill_when_last_summarized_turn_is_not_assistant(self, session):
        session.messages = turns_from_dicts(
            [
                {"role": "assistant", "content": "answer"},
                {"role": "user", "content": "next task"},
            ]
        )
        session._msg_tokens = [1, 1]
        with patch.object(session, "_utility_completion", return_value=_stub_summary()):
            assert session._compact_messages(auto=True, carry_spill=True) is True
        assert "## Wind-down (verbatim)" not in (session.messages[1].text or "")

    def test_empty_spill_adds_no_heading(self, session):
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "task"},
                {"role": "assistant", "content": "   "},
            ]
        )
        session._msg_tokens = [1, 1]
        with patch.object(session, "_utility_completion", return_value=_stub_summary()):
            assert session._compact_messages(auto=True, carry_spill=True) is True
        assert "## Wind-down (verbatim)" not in (session.messages[1].text or "")

    def test_oversize_spill_truncated_by_carry_budget(self, session):
        big_spill = "PLAN-HEAD " + ("y" * 20_000) + " PLAN-TAIL"
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "task"},
                {"role": "assistant", "content": big_spill},
            ]
        )
        session._msg_tokens = [1, 1]
        with patch.object(session, "_utility_completion", return_value=_stub_summary()):
            assert session._compact_messages(auto=True, carry_spill=True) is True
        summary_text = session.messages[1].text or ""
        assert "PLAN-HEAD" in summary_text and "PLAN-TAIL" in summary_text
        assert "…[truncated —" in summary_text
        assert "the recall tool can retrieve it" in summary_text

    def test_double_carry_shares_the_budget(self, tmp_db, mock_openai_client):
        """Spill + hint on ONE compaction — the end-of-turn shape — must fit
        the window together.  At the shipped window defaults each carry gets
        spare // 2, so two oversize carries land truncated to the shared
        budget instead of stacking two solo quarter-window allowances on top
        of the half-window summary reserve."""
        s = make_session(client=mock_openai_client, tool_timeout=10)
        per_carry = s._carry_budget_chars(2)
        ask = "ASK-HEAD " + "a" * (per_carry * 2) + " ASK-TAIL"
        spill = "PLAN-HEAD " + "b" * (per_carry * 2) + " PLAN-TAIL"
        s.messages = turns_from_dicts(
            [
                {"role": "user", "content": ask},
                {"role": "assistant", "content": spill},
            ]
        )
        s._msg_tokens = [1, 1]
        with patch.object(s, "_utility_completion", return_value=_stub_summary()):
            assert s._compact_messages(auto=True, carry_spill=True) is True

        text = s.messages[1].text or ""
        assert "## Wind-down (verbatim)" in text and "## Continue" in text
        for sentinel in ("ASK-HEAD", "ASK-TAIL", "PLAN-HEAD", "PLAN-TAIL"):
            assert sentinel in text
        assert text.count("…[truncated —") == 2  # both carries hit the shared cap
        framing = 700  # headings, hint wording, stub summary, recall pointer
        assert len(text) <= 2 * per_carry + framing

    def test_do_auto_compact_forwards_carry_spill(self, session):
        """The end-of-turn site passes carry_spill=stopped_to_compact through
        _do_auto_compact — pin the forwarding."""
        with patch.object(session, "_compact_messages", return_value=True) as cm:
            session._do_auto_compact(my_generation=3, carry_spill=True)
        assert cm.call_args.kwargs["carry_spill"] is True
        assert cm.call_args.kwargs["my_generation"] == 3
