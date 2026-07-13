"""Tests for provider-anchored context fullness and cooperative compaction.

Covers the pieces that make compaction agree with tool-output truncation about
how full the context is, and that let the model reach a stopping point before
the harness collapses the transcript:

- ``_estimated_prompt_tokens`` — the single fullness measure (provider
  ``prompt_tokens`` + post-calibration delta, with a local fallback).
- ``_maybe_compact_midturn`` / ``_do_auto_compact`` — the soft-advise /
  hard-compact escalation and the shared compaction action.
- the ``_compaction_advised`` latch lifecycle and the ``compaction_pending``
  advisory plumbing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests._session_helpers import make_session
from turnstone.core.session import (
    COMPACTION_SOURCE,
    COMPACTION_SUMMARY_LABEL,
    GenerationCancelled,
    _CompactionIrreducibleError,
    _is_ctx_overflow,
)
from turnstone.core.trajectory import dicts_from_turns, turns_from_dicts


@pytest.fixture
def session(tmp_db, mock_openai_client):
    """ChatSession with a small window so thresholds are easy to reason about.

    context_window=10_000, auto_compact_pct default 0.8 → soft=8000,
    hard=min(0.95, 0.9)*10_000=9000.  Built via the shared ``make_session``
    factory so the session shape stays in lockstep with the sibling
    truncation/compaction suites that read the same fullness measure.
    """
    return make_session(
        client=mock_openai_client,
        context_window=10_000,
        max_tokens=1_000,
        tool_timeout=10,
    )


# ---------------------------------------------------------------------------
# _estimated_prompt_tokens — the shared fullness measure
# ---------------------------------------------------------------------------


class TestEstimatedPromptTokens:
    def test_falls_back_to_local_without_usage(self, session):
        """Before the first API call there is no provider anchor.

        Tools cleared so the fallback is the pure system + message sum; the
        tool-def augmentation of the same fallback is pinned separately by
        ``TestProactiveToolDefFallback``.
        """
        session._tools = []
        session._last_usage = None
        session._system_tokens = 500
        session._msg_tokens = [100, 200]
        assert session._estimated_prompt_tokens() == 800

    def test_anchors_to_provider_usage_plus_delta(self, session):
        """Provider prompt_tokens is ground truth; only post-calibration
        messages are estimated on top (system + tool-def + cached prefix are
        all already inside prompt_tokens)."""
        session._last_usage = {"prompt_tokens": 8000}
        session._calibrated_msg_count = 2
        session._msg_tokens = [1, 1, 300, 50]  # delta = msgs after index 2
        assert session._estimated_prompt_tokens() == 8000 + 350

    def test_clamps_stale_calibrated_count(self, session):
        """A stale calibration index (post-compaction / mutation) must not
        over-slice into a negative/garbage delta."""
        session._last_usage = {"prompt_tokens": 5000}
        session._calibrated_msg_count = 99  # > len(_msg_tokens)
        session._msg_tokens = [10, 20]
        assert session._estimated_prompt_tokens() == 5000


# ---------------------------------------------------------------------------
# _maybe_compact_midturn — soft-advise / hard-compact escalation
# ---------------------------------------------------------------------------


class TestMidturnCompactionPolicy:
    def test_below_soft_threshold_is_noop(self, session):
        with (
            patch.object(session, "_estimated_prompt_tokens", return_value=7_000),
            patch.object(session, "_do_auto_compact") as compact,
            patch.object(session, "_append_system_turn") as advise,
        ):
            session._maybe_compact_midturn()
        compact.assert_not_called()
        advise.assert_not_called()
        assert session._compaction_advised is False

    def test_first_crossing_advises_not_compacts(self, session):
        """Regression for the dead-zone bug: the provider reported ~85% while
        the old naive estimate (system + msgs) was far under the 80% soft
        threshold, so mid-turn compaction never fired and the model flailed on
        truncated output.  Now the provider-anchored estimate crosses soft and
        the model is advised to wrap up first."""
        # Naive estimate is ~10% of the window...
        session._system_tokens = 1_000
        session._msg_tokens = [1, 1]
        session._calibrated_msg_count = 2
        assert session._system_tokens + sum(session._msg_tokens) < 8_000
        # ...but the provider counted 8_500 (tool defs + history) = 85%.
        session._last_usage = {"prompt_tokens": 8_500}
        session._compaction_advised = False

        with (
            patch.object(session, "_do_auto_compact") as compact,
            patch.object(session, "_append_system_turn") as advise,
        ):
            session._maybe_compact_midturn()

        compact.assert_not_called()
        advise.assert_called_once()
        assert advise.call_args.args[0] == "compaction_pending"
        assert session._compaction_advised is True

    def test_continue_after_advisory_compacts(self, session):
        """Already advised + still over soft → the model kept working, compact."""
        session._compaction_advised = True
        with (
            patch.object(session, "_estimated_prompt_tokens", return_value=8_500),
            patch.object(session, "_do_auto_compact") as compact,
            patch.object(session, "_append_system_turn") as advise,
        ):
            session._maybe_compact_midturn(my_generation=7)
        # my_generation threads through so the compaction swap stays generation-guarded.
        compact.assert_called_once_with("mid-turn", my_generation=7)
        advise.assert_not_called()

    def test_hard_ceiling_compacts_without_advisory(self, session):
        """Over the hard ceiling → no turn to spare, compact even if never
        advised."""
        session._compaction_advised = False
        with (
            patch.object(session, "_estimated_prompt_tokens", return_value=9_500),
            patch.object(session, "_do_auto_compact") as compact,
            patch.object(session, "_append_system_turn") as advise,
        ):
            session._maybe_compact_midturn(my_generation=7)
        compact.assert_called_once_with("mid-turn", my_generation=7)
        advise.assert_not_called()

    def test_do_auto_compact_rounds_percentage(self, session):
        """The notice uses round(), not int() — 0.58 must render '58%', not the
        float-truncated '57%'."""
        session.auto_compact_pct = 0.58
        with (
            patch.object(session, "_compact_messages") as compact,
            patch.object(session, "_print_status_line"),
            patch.object(session.ui, "on_info") as on_info,
        ):
            session._do_auto_compact("mid-turn")
        compact.assert_called_once_with(
            auto=True, preserve_tail=0, my_generation=0, carry_spill=False
        )
        msg = on_info.call_args.args[0]
        assert "58%" in msg
        assert "mid-turn" in msg


# ---------------------------------------------------------------------------
# Latch lifecycle + advisory plumbing
# ---------------------------------------------------------------------------


class TestCompactionLatch:
    def test_stale_latch_cleared_on_send(self, session):
        """A latch left True by a prior abnormal exit (cancel / error /
        superseded / resume) must not survive into the next send and trigger an
        advisory-skipping compaction.  send() entry clears it."""
        session.messages = turns_from_dicts([{"role": "user", "content": "hi"}])
        session._msg_tokens = [1]
        session._title_generated = True  # don't spawn the auto-title daemon
        session._compaction_advised = True  # stale latch from a prior turn

        with (
            patch.object(session, "_create_stream_with_retry", return_value=iter([])),
            patch.object(
                session, "_stream_response", return_value={"role": "assistant", "content": "done"}
            ),
            patch.object(session, "_full_messages", return_value=[]),
            patch.object(session, "_update_token_table"),
            patch.object(session, "_print_status_line"),
            patch.object(session, "_emit_state"),
            patch.object(session, "_compact_messages"),
            patch("turnstone.core.session.save_message"),
        ):
            session.send("hello")

        assert session._compaction_advised is False

    def test_compact_messages_clears_latch_even_when_it_bails(self, session):
        """_compact_messages must clear the latch on every attempt — including
        its early-return guards — so a bailed forced compaction falls back to
        the advisory grace state instead of retry-storming."""
        session._compaction_advised = True
        # One message → hits the "Not enough messages to compact" early return.
        session.messages = turns_from_dicts([{"role": "user", "content": "hi"}])
        session._compact_messages(auto=True)
        assert session._compaction_advised is False


class TestEndOfTurnAutoResume:
    """End-of-turn: a cooperative stop (model wound down so we could compact)
    resumes after compaction; a natural finish goes idle."""

    def test_advised_stop_resumes_after_compaction(self, session):
        """Latch True at the stop → after compaction a user turn re-prompts the
        model to continue (the loop does not break to idle)."""
        session.messages = turns_from_dicts([{"role": "user", "content": "task"}])
        session._msg_tokens = [1]
        session._title_generated = True

        # send() entry resets the latch, so simulate the mid-turn advisory
        # firing *during* the first stream (latch True), then the model stops.
        calls = {"n": 0}

        def stream(*_a, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                session._compaction_advised = True  # advisory fired this turn
                return {"role": "assistant", "content": "paused; plan recorded"}
            return {"role": "assistant", "content": "done"}

        with (
            patch.object(session, "_create_stream_with_retry", return_value=iter([])),
            patch.object(session, "_stream_response", side_effect=stream),
            patch.object(session, "_full_messages", return_value=[]),
            patch.object(session, "_update_token_table"),
            patch.object(session, "_print_status_line"),
            patch.object(session, "_emit_state"),
            patch.object(session, "_estimated_prompt_tokens", return_value=9_999),
            patch.object(session, "_do_auto_compact"),
            patch.object(session, "_append_user_turn") as resume,
            patch("turnstone.core.session.save_message"),
        ):
            session.send("go")

        # send() also appends the user input ("go") via _append_user_turn, so
        # filter for the resume turn specifically (tagged source=compaction_resume).
        resume_calls = [
            c for c in resume.call_args_list if c.kwargs.get("source") == "compaction_resume"
        ]
        assert len(resume_calls) == 1

    def test_natural_finish_idles_without_resume(self, session):
        """Latch False at the stop (task genuinely done) → compact, then idle;
        no auto-resume."""
        session.messages = turns_from_dicts([{"role": "user", "content": "task"}])
        session._msg_tokens = [1]
        session._title_generated = True
        session._compaction_advised = False

        with (
            patch.object(session, "_create_stream_with_retry", return_value=iter([])),
            patch.object(
                session, "_stream_response", return_value={"role": "assistant", "content": "done"}
            ),
            patch.object(session, "_full_messages", return_value=[]),
            patch.object(session, "_update_token_table"),
            patch.object(session, "_print_status_line"),
            patch.object(session, "_emit_state") as emit_state,
            # Over soft (8000) but UNDER hard (9000): isolates the end-of-turn
            # trigger this test targets.  A value over hard would ALSO trip the
            # proactive pre-send compaction (covered by TestProactivePreSend),
            # double-counting the mocked compactor.
            patch.object(session, "_estimated_prompt_tokens", return_value=8_500),
            patch.object(session, "_do_auto_compact") as compact,
            patch.object(session, "_append_user_turn") as resume,
            patch("turnstone.core.session.save_message"),
        ):
            session.send("go")

        compact.assert_called_once()
        # No resume turn (only the user input "go" was appended).
        assert not [
            c for c in resume.call_args_list if c.kwargs.get("source") == "compaction_resume"
        ]
        emit_state.assert_any_call("idle")

    def test_no_resume_when_compaction_bails(self, session):
        """q-1 regression: if compaction bails (returns False — summary error /
        too-large / too-few), the resume must NOT fire — there's no summary to
        continue from."""
        session.messages = turns_from_dicts([{"role": "user", "content": "task"}])
        session._msg_tokens = [1]
        session._title_generated = True
        calls = {"n": 0}

        def stream(*_a, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                session._compaction_advised = True  # advised stop
                return {"role": "assistant", "content": "paused"}
            return {"role": "assistant", "content": "done"}

        with (
            patch.object(session, "_create_stream_with_retry", return_value=iter([])),
            patch.object(session, "_stream_response", side_effect=stream),
            patch.object(session, "_full_messages", return_value=[]),
            patch.object(session, "_update_token_table"),
            patch.object(session, "_print_status_line"),
            patch.object(session, "_emit_state"),
            patch.object(session, "_estimated_prompt_tokens", return_value=9_999),
            patch.object(session, "_do_auto_compact", return_value=False),  # bailed
            patch.object(session, "_append_user_turn") as resume,
            patch("turnstone.core.session.save_message"),
        ):
            session.send("go")

        assert not [
            c for c in resume.call_args_list if c.kwargs.get("source") == "compaction_resume"
        ]

    def test_resume_preserves_alternation(self, session):
        """The auto-resume must not produce two consecutive user turns — some
        providers require strict user/assistant alternation.  Compaction leaves
        a trailing assistant (summary) turn, and the resume user turn follows
        it.  Drives the real compaction + resume end to end."""
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "do the task"},
                {"role": "assistant", "content": "on it"},
            ]
        )
        session._msg_tokens = [5, 5]
        session._title_generated = True
        session.compact_max_tokens = 100  # positive summary budget at ctx=10k
        session._system_tokens = 0

        summary = SimpleNamespace(content="## Open tasks\nfinish it", finish_reason="stop")
        n = {"i": 0}

        def stream(*_a, **_k):
            n["i"] += 1
            if n["i"] == 1:
                session._compaction_advised = True  # advisory fired this turn
                return {"role": "assistant", "content": "pausing to compact"}
            return {"role": "assistant", "content": "all done"}

        def est(*_a, **_k):
            return 9_999 if n["i"] <= 1 else 10  # over threshold only on the stop turn

        with (
            patch.object(session, "_create_stream_with_retry", return_value=iter([])),
            patch.object(session, "_stream_response", side_effect=stream),
            patch.object(session, "_full_messages", return_value=[]),
            patch.object(session, "_update_token_table"),
            patch.object(session, "_print_status_line"),
            patch.object(session, "_emit_state"),
            patch.object(session, "_estimated_prompt_tokens", side_effect=est),
            patch.object(session, "_utility_completion", return_value=summary),
            patch("turnstone.core.session.save_message"),
        ):
            session.send("go")

        roles = [m["role"] for m in dicts_from_turns(session.messages)]
        assert not any(roles[i] == roles[i + 1] == "user" for i in range(len(roles) - 1)), (
            f"consecutive user turns: {roles}"
        )
        # The resume genuinely happened: a user turn sits after the summary.
        assert "assistant" in roles and roles[-1] != "user"


class TestCompactBeforeTruncate:
    """#2: tail-preserving compaction keeps the in-flight tool-call turn so the
    fresh tool results aren't orphaned, gated by the shared _compaction_owed."""

    def test_preserve_tail_keeps_in_flight_tool_call(self, session):
        """compact(preserve_tail=1) summarizes the older history but keeps the
        last (assistant tool-call) turn verbatim — so a tool result appended
        after it still has its matching tool_use."""
        session.compact_max_tokens = 100  # positive summary budget at ctx=10k
        session._system_tokens = 0
        tc = {"id": "call_1", "type": "function", "function": {"name": "x", "arguments": "{}"}}
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "do it"},
                {"role": "assistant", "content": "older reply"},
                {"role": "user", "content": "more"},
                {"role": "assistant", "content": "", "tool_calls": [tc]},  # in-flight
            ]
        )
        session._msg_tokens = [5, 5, 5, 5]
        summary = SimpleNamespace(content="dense summary", finish_reason="stop")

        with patch.object(session, "_utility_completion", return_value=summary):
            session._compact_messages(auto=True, preserve_tail=1)

        wire = dicts_from_turns(session.messages)
        # [summary_user, summary_asst, preserved assistant-tool-call]
        assert wire[0]["role"] == "user" and "[Conversation summary]" in wire[0]["content"]
        assert wire[1]["role"] == "assistant"
        assert wire[-1]["role"] == "assistant" and wire[-1].get("tool_calls")
        # The tool_call survived, so a tool result for call_1 won't orphan.
        ids = [t["id"] for m in wire if m.get("tool_calls") for t in m["tool_calls"]]
        assert "call_1" in ids

    def test_compaction_owed_predicate(self, session):
        # over hard ceiling (>9000) → owed regardless of the latch
        with patch.object(session, "_estimated_prompt_tokens", return_value=9_500):
            session._compaction_advised = False
            assert session._compaction_owed() is True
        # over soft (>8000) → owed only when advised
        with patch.object(session, "_estimated_prompt_tokens", return_value=8_500):
            session._compaction_advised = True
            assert session._compaction_owed() is True
            session._compaction_advised = False
            assert session._compaction_owed() is False
        # under soft → never owed
        with patch.object(session, "_estimated_prompt_tokens", return_value=7_000):
            session._compaction_advised = True
            assert session._compaction_owed() is False

    def test_owed_compaction_runs_before_truncation_in_tool_path(self, session):
        """Wiring: in the tool path, an owed compaction fires with preserve_tail=1
        before the truncation budget is sized."""
        session.messages = turns_from_dicts([{"role": "user", "content": "task"}])
        session._msg_tokens = [1]
        session._title_generated = True
        tc = {"id": "call_1", "type": "function", "function": {"name": "x", "arguments": "{}"}}
        n = {"i": 0}

        def stream(*_a, **_k):
            n["i"] += 1
            if n["i"] == 1:
                return {"role": "assistant", "content": "", "tool_calls": [tc]}
            return {"role": "assistant", "content": "done"}

        with (
            patch.object(session, "_create_stream_with_retry", return_value=iter([])),
            patch.object(session, "_stream_response", side_effect=stream),
            patch.object(session, "_execute_tools", return_value=([("call_1", "out")], "")),
            patch.object(session, "_full_messages", return_value=[]),
            patch.object(session, "_update_token_table"),
            patch.object(session, "_print_status_line"),
            patch.object(session, "_emit_state"),
            # Owed on the tool turn (pre-truncation); _estimated_prompt_tokens stays
            # small so the end-of-turn path doesn't also compact.
            patch.object(session, "_compaction_owed", side_effect=lambda: n["i"] == 1),
            patch.object(session, "_maybe_compact_midturn"),  # isolate the pre-truncation call
            patch.object(session, "_do_auto_compact") as compact,
            patch("turnstone.core.session.save_message"),
        ):
            session.send("go")

        assert any(
            c.args == ("mid-turn",) and c.kwargs.get("preserve_tail") == 1
            for c in compact.call_args_list
        ), compact.call_args_list

    def test_pre_attempt_suppresses_second_midturn_compaction(self, session):
        """q-2: when an owed compaction already fired pre-truncation
        (``pre_attempted_compact=True``), the post-truncation
        ``_maybe_compact_midturn`` is skipped — re-running would double the
        summary work (and could retry-storm a failed summary)."""
        session.messages = turns_from_dicts([{"role": "user", "content": "task"}])
        session._msg_tokens = [1]
        session._title_generated = True
        tc = {"id": "call_1", "type": "function", "function": {"name": "x", "arguments": "{}"}}
        n = {"i": 0}

        def stream(*_a, **_k):
            n["i"] += 1
            if n["i"] == 1:
                return {"role": "assistant", "content": "", "tool_calls": [tc]}
            return {"role": "assistant", "content": "done"}

        with (
            patch.object(session, "_create_stream_with_retry", return_value=iter([])),
            patch.object(session, "_stream_response", side_effect=stream),
            patch.object(session, "_execute_tools", return_value=([("call_1", "out")], "")),
            patch.object(session, "_full_messages", return_value=[]),
            patch.object(session, "_update_token_table"),
            patch.object(session, "_print_status_line"),
            patch.object(session, "_emit_state"),
            # Owed on the tool turn → the pre-truncation compaction fires.
            # _compaction_owed takes an optional ``used`` arg, so accept *a/**k.
            patch.object(session, "_compaction_owed", side_effect=lambda *a, **k: n["i"] == 1),
            patch.object(session, "_do_auto_compact") as compact,
            patch.object(session, "_maybe_compact_midturn") as midturn,
            patch("turnstone.core.session.save_message"),
        ):
            session.send("go")

        # The pre-truncation compaction actually fired (preserve_tail=1)...
        assert any(
            c.args == ("mid-turn",) and c.kwargs.get("preserve_tail") == 1
            for c in compact.call_args_list
        ), compact.call_args_list
        # ...so the post-truncation mid-turn compaction is suppressed.
        midturn.assert_not_called()

    def test_no_pre_attempt_runs_midturn_compaction(self, session):
        """q-2 sibling: with nothing owed pre-truncation
        (``pre_attempted_compact=False``), the post-truncation
        ``_maybe_compact_midturn`` runs once for the tool turn."""
        session.messages = turns_from_dicts([{"role": "user", "content": "task"}])
        session._msg_tokens = [1]
        session._title_generated = True
        tc = {"id": "call_1", "type": "function", "function": {"name": "x", "arguments": "{}"}}
        n = {"i": 0}

        def stream(*_a, **_k):
            n["i"] += 1
            if n["i"] == 1:
                return {"role": "assistant", "content": "", "tool_calls": [tc]}
            return {"role": "assistant", "content": "done"}

        with (
            patch.object(session, "_create_stream_with_retry", return_value=iter([])),
            patch.object(session, "_stream_response", side_effect=stream),
            patch.object(session, "_execute_tools", return_value=([("call_1", "out")], "")),
            patch.object(session, "_full_messages", return_value=[]),
            patch.object(session, "_update_token_table"),
            patch.object(session, "_print_status_line"),
            patch.object(session, "_emit_state"),
            # Never owed → no pre-truncation compaction this iteration.
            patch.object(session, "_compaction_owed", side_effect=lambda *a, **k: False),
            patch.object(session, "_do_auto_compact") as compact,
            patch.object(session, "_maybe_compact_midturn") as midturn,
            patch("turnstone.core.session.save_message"),
        ):
            session.send("go")

        # No pre-truncation (preserve_tail) compaction happened...
        assert not any(c.kwargs.get("preserve_tail") == 1 for c in compact.call_args_list), (
            compact.call_args_list
        )
        # ...so the post-truncation mid-turn compaction runs once for the tool turn.
        midturn.assert_called_once()


# ---------------------------------------------------------------------------
# Chunked / hierarchical summary compaction
# ---------------------------------------------------------------------------


class TestPackBlocks:
    """``_pack_blocks`` greedily packs formatted blocks into batches that each
    fit the budget, in order, with no drops and no reordering."""

    def test_preserves_all_blocks_and_order(self, session):
        blocks = [f"block-{i}-{'x' * 50}" for i in range(10)]
        batches = session._pack_blocks(blocks, budget_chars=200)
        flat = [b for batch in batches for b in batch]
        assert flat == blocks  # every block present, order preserved
        assert all(batch for batch in batches)  # never an empty batch

    def test_each_batch_within_budget(self, session):
        budget = 200
        blocks = ["a" * 80 for _ in range(12)]
        batches = session._pack_blocks(blocks, budget_chars=budget)
        for batch in batches:
            assert len("\n\n".join(batch)) <= budget

    def test_boundary_block_exactly_at_budget(self, session):
        budget = 100
        exact = "y" * budget  # len == budget: fits a batch, not oversized
        blocks = ["short", exact, "tail"]
        batches = session._pack_blocks(blocks, budget_chars=budget)
        flat = [b for batch in batches for b in batch]
        assert flat == blocks  # order + presence
        assert exact in flat  # untouched, not truncated
        for batch in batches:
            assert len("\n\n".join(batch)) <= budget

    def test_oversized_lone_block_truncated_in_own_batch(self, session):
        budget = 100
        huge = "z" * 500  # > budget → its own truncated batch
        blocks = ["before", huge, "after"]
        batches = session._pack_blocks(blocks, budget_chars=budget)
        flat = [b for batch in batches for b in batch]
        assert flat[0] == "before" and flat[-1] == "after"  # neighbours survive
        truncated = [b for b in flat if "[truncated" in b]
        assert len(truncated) == 1
        assert len(truncated[0]) <= budget
        assert truncated[0].startswith("z")  # head preserved
        for batch in batches:
            assert len("\n\n".join(batch)) <= budget


class TestSummaryInputBudget:
    """``_summary_input_budget_chars`` derives the per-call input budget from the
    context window, reserving the output and prompt overhead."""

    def test_scales_with_context_window(self, session):
        session.compact_max_tokens = 100
        session.context_window = 20_000
        smaller = session._summary_input_budget_chars()
        session.context_window = 40_000
        larger = session._summary_input_budget_chars()
        assert larger > smaller

    def test_subtracts_output_reserve(self, session):
        session.context_window = 50_000
        session.compact_max_tokens = 100
        small_reserve = session._summary_input_budget_chars()
        session.compact_max_tokens = 20_000  # larger output reserve
        large_reserve = session._summary_input_budget_chars()
        assert large_reserve < small_reserve  # less room left for input

    def test_budget_never_exceeds_true_input_capacity(self, session):
        """Review (Copilot): the _MIN_SUMMARY_BUDGET_CHARS floor must not push the
        budget above what actually fits — output reserve + budgeted input + prompt
        must stay within context_window, or the summary call overflows on a tiny
        window instead of bailing.  Without the cap the floor (2000 chars) exceeds
        capacity here and the call would overflow."""
        session.context_window = 1200
        session.compact_max_tokens = 1200
        session._system_tokens = 0
        budget_chars = session._summary_input_budget_chars()
        prompt_tokens = int(
            (len(session._COMPACTOR_SYSTEM_PROMPT) + len(session._COMPACT_USER_PREFIX))
            / session._chars_per_token
        )
        # The full summary call (output reserve + budgeted input + prompt) fits.
        total = (
            session._summary_output_tokens()
            + budget_chars / session._chars_per_token
            + prompt_tokens
        )
        assert total <= session.context_window


class TestChunkedCompaction:
    """The chunked driver: one call when it all fits, recursion when it doesn't,
    and atomicity on a mid-chunk failure."""

    def test_single_batch_is_exactly_one_completion_call(self, session):
        """When everything fits one batch, compaction is a single model call —
        preserving today's behavior (and the existing tests that assume it)."""
        session.compact_max_tokens = 100
        session._system_tokens = 0
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "do the thing"},
                {"role": "assistant", "content": "did the thing"},
            ]
        )
        session._msg_tokens = [5, 5]
        summary = SimpleNamespace(content="## Decisions\ndense", finish_reason="stop")

        with patch.object(session, "_utility_completion", return_value=summary) as uc:
            assert session._compact_messages(auto=True) is True

        assert uc.call_count == 1
        assert len(session.messages) == 2  # summary_user + summary_asst

    def test_multi_batch_recurses_terminates_and_stays_within_budget(self, session):
        """Core regression: many messages with a tiny per-message token estimate
        (the OLD prefix budget would pass them all in one shot) but a formatted
        size that needs several batches.  Every summary call's body must fit
        ``_summary_input_budget_chars`` — the overflow the chunking fixes — and
        the recursion must terminate with a real, shrunk summary."""
        session.context_window = 5_000
        session.compact_max_tokens = 4_000  # squeezes the input budget to the floor
        session._system_tokens = 0
        budget = session._summary_input_budget_chars()

        session.messages = turns_from_dicts(
            [
                {
                    "role": "user" if i % 2 == 0 else "assistant",
                    "content": f"msg-{i:02d} " + "c" * 200,
                }
                for i in range(30)
            ]
        )
        session._msg_tokens = [1] * 30  # tiny token estimate; OLD code selects all

        recorded: list[int] = []

        def fake_uc(messages, **_kwargs):
            body = messages[1].text
            prefix = session._COMPACT_USER_PREFIX
            if body.startswith(prefix):
                body = body[len(prefix) :]
            recorded.append(len(body))
            return SimpleNamespace(content="PARTIAL", finish_reason="stop")

        with patch.object(session, "_utility_completion", side_effect=fake_uc):
            result = session._compact_messages(auto=True)

        assert result is True
        assert len(session.messages) < 30  # genuinely shrank
        assert len(recorded) > 1  # multi-batch: recursion happened
        assert all(n <= budget for n in recorded)  # never overflow the summary call

    def test_recursion_depth_ceiling_bails_to_false(self, session):
        """q-3: the ``depth >= _MAX_SUMMARY_DEPTH`` recursion backstop bails to
        False (the "too large" path) without fabricating a summary.

        depth 0 packs into several batches and recurses; depth 1 still has >1
        batch, and ``depth >= 1`` fires the bail.  That the depth-0 calls ran
        first is proven by ``_utility_completion`` being called (≥1) despite the
        False return.
        """
        session.context_window = 5_000
        session.compact_max_tokens = 4_000  # squeezes the input budget
        session._system_tokens = 0
        session._MAX_SUMMARY_DEPTH = 1  # positive, so depth 0 runs before the bail
        budget = session._summary_input_budget_chars()

        # ~30 messages, each block bigger than 1/6 of the budget → depth 0 packs
        # into several batches and recurses (depth 0 < MAX).
        session.messages = turns_from_dicts(
            [
                {
                    "role": "user" if i % 2 == 0 else "assistant",
                    "content": f"msg-{i:02d} " + "c" * 900,
                }
                for i in range(30)
            ]
        )
        session._msg_tokens = [1] * 30
        before = list(session.messages)

        # Each depth-0 partial is 0.4*budget chars: two pack per batch but not
        # three, so depth 1 still has >1 batch and the depth ceiling bails.
        partial = "P" * ((budget * 2) // 5)
        summary = SimpleNamespace(content=partial, finish_reason="stop")

        with patch.object(session, "_utility_completion", return_value=summary) as uc:
            result = session._compact_messages(auto=True)

        assert result is False
        assert session.messages == before  # untouched on the bail
        assert uc.call_count >= 1  # depth-0 ran before the depth-ceiling bail

    def test_irreducible_input_bails_to_false(self, session):
        """A genuinely irreducible case — where even a floor-truncated lone block
        still overflows the window — bails to False (the "too large" path) rather
        than fabricate a summary, leaving the history untouched.

        With per-block splitting the chunker no longer bails on packing alone; it
        bails only when a block truncated to ``_MIN_SUMMARY_BUDGET_CHARS`` STILL
        overflows the model — i.e. no body is small enough to summarize.
        """
        session.context_window = 900
        session.compact_max_tokens = 900
        session._system_tokens = 0
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "u " + "x" * 5000},
                {"role": "assistant", "content": "a " + "y" * 5000},
            ]
        )
        session._msg_tokens = [1, 1]
        before = list(session.messages)

        # Every summary call overflows — even a floor-truncated lone block — so no
        # body is ever small enough to summarize: bail irreducible, history intact.
        def always_overflow(*_a, **_k):
            raise RuntimeError("maximum context length is 900 tokens")

        with patch.object(session, "_utility_completion", side_effect=always_overflow):
            result = session._compact_messages(auto=True)

        assert result is False
        assert session.messages == before  # untouched

    def test_default_config_summary_call_fits_window(self, session):
        """Regression for the keystone bug: at the shipped defaults
        (context_window == compact_max_tokens == 32768, max_output_tokens 64000)
        the old ``min(compact_max_tokens, max_output_tokens)`` reserve ate the
        whole window — the input budget floored to 2000 and the summary call
        requested 32768 output tokens on a 32768 window, overflowing.  Fix 1
        bounds the reserve to half the window, so compaction actually runs.
        """
        session.context_window = 32768
        session.compact_max_tokens = 32768
        # Pin the default-collision scenario regardless of how the fixture's
        # model happens to resolve caps.
        from turnstone.core.providers._protocol import ModelCapabilities

        caps = ModelCapabilities(context_window=32768, max_output_tokens=64000)
        with patch.object(session, "_get_capabilities", return_value=caps):
            assert session._get_capabilities().max_output_tokens == 64000
            # Output reserve never claims more than half the window...
            assert session._summary_output_tokens() <= session.context_window // 2
            # ...so the input budget is healthy, not floored to 2000.
            assert session._summary_input_budget_chars() > 10_000

            session._system_tokens = 0
            session.messages = turns_from_dicts(
                [
                    {
                        "role": "user" if i % 2 == 0 else "assistant",
                        "content": f"turn-{i:02d}: " + "word " * 40,
                    }
                    for i in range(8)
                ]
            )
            session._msg_tokens = [1] * 8

            recorded: list[int] = []

            def fake_uc(messages, *, max_tokens, **_kwargs):
                recorded.append(max_tokens)
                return SimpleNamespace(content="## Decisions\ndense", finish_reason="stop")

            with patch.object(session, "_utility_completion", side_effect=fake_uc):
                assert session._compact_messages(auto=True) is True

        # The summary call's output reserve stayed within half the window, and a
        # representative input rides comfortably under the full window alongside it.
        assert recorded  # at least one summary call happened
        out_tokens = recorded[0]
        assert out_tokens <= session.context_window // 2
        rep_input_tokens = session._summary_input_budget_chars() / session._chars_per_token
        assert out_tokens + rep_input_tokens < session.context_window

    def test_empty_summary_keeps_history(self, session):
        """If the summary model returns empty/reasoning-only content, compaction
        must keep the conversation rather than swap in an empty summary and
        silently discard everything (returning True)."""
        session.compact_max_tokens = 100  # positive summary budget at ctx=10k
        session._system_tokens = 0
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "do the thing"},
                {"role": "assistant", "content": "working on it"},
                {"role": "user", "content": "and the next thing"},
            ]
        )
        session._msg_tokens = [5, 5, 5]
        before = list(session.messages)
        empty = SimpleNamespace(content="", finish_reason="stop")

        with patch.object(session, "_utility_completion", return_value=empty):
            result = session._compact_messages(auto=True)

        assert result is False
        assert session.messages == before  # no swap, no data loss

    def test_post_compaction_anchor_includes_tool_defs(self, session):
        """#4/#9: the synthetic ``_last_usage`` written after a successful
        compaction must fold in tool-def tokens — the same thing the provider
        counts in ``prompt_tokens`` — so the next ``_remaining_token_budget``
        doesn't over-state free space by the whole tool-def count."""
        # A small but non-empty tool set so _tool_def_tokens() > 0 makes the
        # assertion meaningful.
        session._tool_search = None
        session._tools = [
            {
                "type": "function",
                "function": {"name": "noop", "description": "does nothing", "parameters": {}},
            }
        ]
        assert session._tool_def_tokens() > 0  # sanity: tools contribute tokens

        session.compact_max_tokens = 100  # positive summary budget at ctx=10k
        session._system_tokens = 7
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "do the thing"},
                {"role": "assistant", "content": "did the thing"},
            ]
        )
        session._msg_tokens = [5, 5]
        session._last_usage = {"prompt_tokens": 9_000, "total_tokens": 9_000}
        summary = SimpleNamespace(content="## Decisions\ndense", finish_reason="stop")

        with patch.object(session, "_utility_completion", return_value=summary):
            assert session._compact_messages(auto=True) is True

        expected = session._system_tokens + sum(session._msg_tokens) + session._tool_def_tokens()
        assert session._last_usage["prompt_tokens"] == expected

    def test_mid_chunk_failure_leaves_messages_untouched(self, session):
        """Atomicity: batch 1 summarizes, batch 2's call raises non-retryably →
        no partial swap, returns False, ``self.messages`` / ``_msg_tokens`` intact."""
        session.context_window = 5_000
        session.compact_max_tokens = 4_000
        session._system_tokens = 0
        session.messages = turns_from_dicts(
            [
                {
                    "role": "user" if i % 2 == 0 else "assistant",
                    "content": f"m{i:02d} " + "c" * 200,
                }
                for i in range(30)
            ]
        )
        session._msg_tokens = [1] * 30
        before = list(session.messages)
        before_toks = list(session._msg_tokens)

        calls = {"n": 0}

        def fake_uc(_messages, **_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return SimpleNamespace(content="PARTIAL", finish_reason="stop")
            raise RuntimeError("summary backend exploded")  # non-retryable

        with patch.object(session, "_utility_completion", side_effect=fake_uc):
            result = session._compact_messages(auto=True)

        assert result is False
        assert calls["n"] >= 2  # failed part-way through the batches
        assert session.messages == before  # no partial swap
        assert session._msg_tokens == before_toks


class TestProactiveToolDefFallback:
    """The ``_last_usage``-less fallback in ``_estimated_prompt_tokens`` must add a
    tool-def estimate (tools are resent every request) — otherwise a just-resumed
    session undercounts and skips proactive compaction."""

    def test_fallback_includes_tool_defs_when_active(self, session):
        session._last_usage = None
        session._system_tokens = 100
        session._msg_tokens = [10, 20]
        bare = session._system_tokens + sum(session._msg_tokens)
        assert session._get_active_tools()  # sanity: the default tool set is present
        assert session._estimated_prompt_tokens() > bare

    def test_fallback_equals_bare_sum_without_tools(self, session):
        session._tools = []
        session._last_usage = None
        session._system_tokens = 100
        session._msg_tokens = [10, 20]
        assert session._estimated_prompt_tokens() == 130


def test_compaction_advisory_is_registered():
    """The advisory source and template must be wired across both modules so
    ``_append_system_turn('compaction_pending', ...)`` cannot raise."""
    from turnstone.core.metacognition import format_nudge
    from turnstone.core.tool_advisory import SYSTEM_TURN_SOURCES, make_system_turn

    assert "compaction_pending" in SYSTEM_TURN_SOURCES
    text = format_nudge("compaction_pending")
    assert text and "compact" in text.lower()
    turn = make_system_turn("compaction_pending", text)
    assert turn["role"] == "system"
    assert turn["_source"] == "compaction_pending"


# ---------------------------------------------------------------------------
# Context-overflow handling: detection, proactive pre-send compaction (Layer A),
# and the closed-loop adaptive chunker — the resume-rehydration overflow fix.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message,expected",
    [
        # Real overflow messages (vLLM / OpenAI / Anthropic) — must match.
        ("This model's maximum context length is 524288 tokens", True),
        (
            "maximum context length is 524288 tokens ... your prompt contains at "
            "least 523777 input tokens",
            True,
        ),
        ("prompt is too long: 200000 > 100000", True),
        ("the input is too long for this model", True),
        ("Please reduce the length of the input prompt", True),
        ("request exceeds the context window", True),
        # Anthropic (input + max_tokens) and Google/Gemini wordings — match NONE of
        # the old phrase set; regression guard for the centralized detector.
        (
            "input length and max_tokens exceed context limit: 9000 + 4000 > 8000, "
            "decrease input length or max_tokens and try again",
            True,
        ),
        (
            "The input token count (29000) exceeds the maximum number of tokens allowed (28000)",
            True,
        ),
        # Retryable / unrelated — must NOT match (esp. token-quota 429s, which a
        # bare "input tokens" substring would false-match into a hard failure).
        ("rate limit exceeded: 40000 input tokens per minute", False),
        ("This request would exceed your organization's rate limit", False),
        ("Connection refused", False),
        ("invalid api key", False),
    ],
)
def test_is_ctx_overflow_detection(message, expected):
    """Overflow is detected by text, not exception class: vLLM returns the same
    condition as a 400 ``BadRequestError`` on /v1/chat/completions but a 500
    ``InternalServerError`` on /v1/messages."""
    assert _is_ctx_overflow(RuntimeError(message)) is expected


def test_is_ctx_overflow_excludes_recognized_rate_limit_class():
    """A 429 RateLimitError whose token-quota text contains an overflow phrase must
    NOT be classified as overflow.  _stop_retrying calls _is_ctx_overflow with no
    class gate of its own, so without this a retryable rate-limit ("… maximum number
    of tokens allowed per minute …") would be made non-retryable.  The SAME text in
    an unrecognized class is still overflow — proving it's the class gate at work."""

    class RateLimitError(Exception):  # name is in _BACKEND_RATE_LIMIT_EXC_NAMES
        pass

    msg = "exceeds the maximum number of tokens allowed per minute"
    assert _is_ctx_overflow(RateLimitError(msg)) is False  # retryable, not overflow
    assert _is_ctx_overflow(RuntimeError(msg)) is True  # unknown class → text decides


def test_format_backend_error_renders_overflow(session):
    """The text-first overflow branch in _format_backend_error renders a clear
    "Context window exceeded" message (with a raw tail) for an exception class
    OUTSIDE _BACKEND_KNOWN_EXC_NAMES — the anthropic-compat 500 case — and a
    non-overflow unknown class still falls through to None."""

    class InternalServerError(Exception):  # not in _BACKEND_KNOWN_EXC_NAMES
        pass

    msg = session._format_backend_error(
        InternalServerError("This model's maximum context length is 524288 tokens")
    )
    assert msg is not None
    assert "Context window exceeded" in msg
    assert "raw=" in msg
    assert session._format_backend_error(InternalServerError("boom")) is None


def test_generate_title_skips_synthetic_summary_label(session):
    """After a compaction the first 'user' turn is the synthetic [Conversation
    summary] label; _generate_title must not title from it — with no real user
    message it skips regeneration and rebroadcasts the current title, instead of
    issuing a model call that titles the conversation '[Conversation summary]'."""
    session.messages = turns_from_dicts(
        [
            {
                "role": "user",
                "content": COMPACTION_SUMMARY_LABEL,
                "_source": COMPACTION_SOURCE,
            },
            {"role": "assistant", "content": "the dense summary"},
        ]
    )
    with (
        patch.object(session, "_utility_completion") as uc,
        patch.object(session, "ui", new=MagicMock()) as ui_mock,
    ):
        session._generate_title("Existing Title")

    uc.assert_not_called()  # no real user message → no title model call
    ui_mock.on_rename.assert_called_once_with("Existing Title")  # current title rebroadcast


class TestProactivePreSend:
    """Layer A: a send whose history already exceeds the window (e.g. a
    rehydrated resume) compacts BEFORE the first stream call, so an over-window
    payload is never put on the wire."""

    def test_proactive_pre_send_compaction_runs_before_stream(self, session):
        session.messages = turns_from_dicts([{"role": "user", "content": "task"}])
        session._msg_tokens = [1]
        session._title_generated = True
        session._compaction_advised = False
        order: list[str] = []
        forwarded: dict[str, object] = {}

        def fake_compact(*args, **kwargs):
            where = args[0] if args else ""
            order.append(f"compact:{where}")
            if where == "pre-send":  # capture only the Layer-A call, not end-of-turn
                forwarded["preserve_tail"] = kwargs.get("preserve_tail")
            return True

        def fake_stream(*_args, **_kwargs):
            order.append("stream")
            return iter([])

        with (
            # 9999 > hard (9000) → compaction is owed at send time.
            patch.object(session, "_estimated_prompt_tokens", return_value=9_999),
            patch.object(session, "_check_metacognitive_nudge", return_value=None),
            patch.object(session, "_do_auto_compact", side_effect=fake_compact),
            patch.object(session, "_create_stream_with_retry", side_effect=fake_stream),
            patch.object(
                session, "_stream_response", return_value={"role": "assistant", "content": "done"}
            ),
            patch.object(session, "_full_messages", return_value=[]),
            patch.object(session, "_update_token_table"),
            patch.object(session, "_print_status_line"),
            patch.object(session, "_emit_state"),
            patch("turnstone.core.session.save_message"),
        ):
            session.send("go")

        assert order[0] == "compact:pre-send", order
        assert "stream" in order
        # End-to-end through send(): the pre-existing "task" turn + the just-sent
        # "go" turn, last USER boundary at index 1 → preserve exactly the trailing
        # "go" turn (no nudge fired), pinning len(messages) - boundaries[-1].
        assert forwarded["preserve_tail"] == 1

    def test_pre_send_preserves_user_turn_past_trailing_nudge(self, session):
        """The just-sent user message survives compaction verbatim even when a
        system nudge was appended after it — pre-send preserves from the last USER
        boundary, not messages[-1]."""
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "THE ACTUAL QUESTION"},
                {"role": "system", "_source": "output_guard", "content": "a trailing nudge"},
            ]
        )
        session._msg_tokens = [1, 1, 1, 1]
        summary = SimpleNamespace(content="SUMMARY", finish_reason="stop")

        # The real pre-send preserve computation, then the real _compact_messages.
        boundaries = session._find_turn_boundaries()
        preserve = len(session.messages) - boundaries[-1]
        # Pin the formula: last USER turn at index 2 → preserve the user msg AND the
        # trailing nudge (indices 2,3), i.e. exactly 2 — not 1 (which would drop the
        # user turn under the nudge) and not the whole history.
        assert preserve == 2
        with patch.object(session, "_utility_completion", return_value=summary):
            assert session._do_auto_compact("pre-send", preserve_tail=preserve) is True

        texts = [m.text or "" for m in session.messages]
        assert any("THE ACTUAL QUESTION" in t for t in texts)  # user msg verbatim
        assert any("a trailing nudge" in t for t in texts)  # trailing nudge kept too
        assert not any("old answer" in t for t in texts)  # older turns summarized away

    def test_continuation_hint_references_last_summarized_user_message(self, session):
        """When the last user turn is summarized away (reactive, preserve_tail=0),
        the summary carries a ``## Continue`` hint quoting that message so the model
        knows where to resume."""
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "FIRST question"},
                {"role": "assistant", "content": "first reply"},
                {"role": "user", "content": "LASTQ the recent ask"},
                {"role": "assistant", "content": "second reply"},
            ]
        )
        session._msg_tokens = [1, 1, 1, 1]
        summary = SimpleNamespace(content="DENSE SUMMARY", finish_reason="stop")
        with patch.object(session, "_utility_completion", return_value=summary):
            assert session._do_auto_compact("reactive", preserve_tail=0) is True

        summ = session.messages[1].text or ""  # the summary_asst turn
        assert "## Continue" in summ
        assert "LASTQ the recent ask" in summ

    def test_continuation_hint_skipped_when_last_user_preserved(self, session):
        """When preserve_tail keeps the last user turn verbatim (the pre-send path),
        NO continuation hint is added — the preserved tail already carries the
        message, so a hint would duplicate it and reframe a fresh ask as 'continue
        where we left off'."""
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "FIRST question"},
                {"role": "assistant", "content": "first reply"},
                {"role": "user", "content": "LASTQ the recent ask"},
            ]
        )
        session._msg_tokens = [1, 1, 1]
        preserve = len(session.messages) - session._find_turn_boundaries()[-1]  # == 1
        summary = SimpleNamespace(content="DENSE SUMMARY", finish_reason="stop")
        with patch.object(session, "_utility_completion", return_value=summary):
            assert session._do_auto_compact("pre-send", preserve_tail=preserve) is True

        summ = session.messages[1].text or ""  # the summary_asst turn
        assert "## Continue" not in summ  # last user turn preserved, not summarized
        # The preserved tail carries the message — exactly once across the transcript.
        texts = [m.text or "" for m in session.messages]
        assert sum("LASTQ the recent ask" in t for t in texts) == 1

    def test_continuation_hint_skips_synthetic_summary_label(self, session):
        """Re-compacting an already-bare [Conversation summary] history must not quote
        the synthetic label as 'the user's last message' — it's a compaction artifact,
        not a real turn, so _find_turn_boundaries excludes it and no hint is added."""
        session.messages = turns_from_dicts(
            [
                {
                    "role": "user",
                    "content": COMPACTION_SUMMARY_LABEL,
                    "_source": COMPACTION_SOURCE,
                },
                {"role": "assistant", "content": "prior dense summary"},
            ]
        )
        session._msg_tokens = [1, 1]
        summary = SimpleNamespace(content="NEW SUMMARY", finish_reason="stop")
        with patch.object(session, "_utility_completion", return_value=summary):
            assert session._do_auto_compact("reactive", preserve_tail=0) is True

        summ = session.messages[1].text or ""  # the new summary_asst turn
        assert summ == "NEW SUMMARY"  # bare summary, no hint quoting the label
        assert "## Continue" not in summ


class TestChunkerOverflowSplit:
    """The chunker recovers from a char-budget under-estimate by splitting an
    over-window batch into per-block summaries — chunking, not truncation, and
    without re-summarizing completed siblings.  These drive the real
    _summarize_blocks / _summarize_batch / _pack_blocks path (only the leaf
    _summarize_once model call is mocked, by body size)."""

    def test_overflowing_batch_subdivides_then_merges(self, session):
        # All blocks pack into one batch (huge char budget), but the combined body
        # overflows the *token* window while smaller sub-batches fit.
        blocks = ["A" * 4000, "B" * 4000, "C" * 4000]
        bodies: list[int] = []

        def fake_once(_system_prompt, body):
            bodies.append(len(body))
            if len(body) > 6_000:  # a multi-block body overflows the token window
                raise RuntimeError("maximum context length is 524288 tokens")
            return "S"

        with (
            patch.object(session, "_summary_input_budget_chars", return_value=100_000),
            patch.object(session, "_summarize_once", side_effect=fake_once),
        ):
            result = session._summarize_blocks(blocks)

        assert result == "S"  # produced a summary, never raised _CompactionIrreducible
        assert any(n > 6_000 for n in bodies)  # the combined batch overflowed…
        # …then it was halved until the pieces fit and merged (no whole-list re-run).
        assert sum(1 for n in bodies if n <= 6_000) >= 3

    def test_overflow_subdivides_not_per_block(self, session):
        """An over-window batch is halved (binary subdivision), NOT summarized one
        call per block — so a wide batch costs ~log2(N) calls, not N.  Regression
        guard for the per-block grind (a ~1000-block batch becoming ~1000 serial
        summary calls stuck in 'part 1/2')."""
        # 8 blocks packed into one batch; the model overflows only when a body holds
        # 5+ blocks, so the 8-block batch must subdivide but 4-block halves fit.
        blocks = [f"b{i:02d} " + "z" * 500 for i in range(8)]
        calls: list[str] = []

        def fake_once(_system_prompt, body):
            calls.append(body)
            if body.count("\n\n") >= 4:  # a body of 5+ blocks overflows the window
                raise RuntimeError("maximum context length is 524288 tokens")
            return "S"

        with (
            patch.object(session, "_summary_input_budget_chars", return_value=1_000_000),
            patch.object(session, "_summarize_once", side_effect=fake_once),
        ):
            result = session._summarize_blocks(blocks)

        assert result == "S"
        # Binary subdivision: [8] → two [4] halves that both fit — a handful of calls,
        # nowhere near 8 (per-block split would be ≥8 leaf calls).
        assert len(calls) <= 5, len(calls)
        # It never descended to single blocks (every summarized body is multi-block);
        # per-block split would have produced 8 single-block bodies.
        assert all("\n\n" in body for body in calls)

    def test_lone_oversized_block_floored_then_succeeds(self, session):
        # A single block that overflows even by itself is head/tail-truncated to
        # the floor and retried once — not bailed.
        floor = session._MIN_SUMMARY_BUDGET_CHARS
        calls: list[int] = []

        def fake_once(_system_prompt, body):
            calls.append(len(body))
            if len(body) > floor:
                raise RuntimeError("maximum context length is 524288 tokens")
            return "S"

        with (
            patch.object(session, "_summary_input_budget_chars", return_value=50_000),
            patch.object(session, "_summarize_once", side_effect=fake_once),
        ):
            result = session._summarize_blocks(["Z" * 20_000])

        assert result == "S"  # floored block summarized, not bailed
        assert any(n > floor for n in calls)  # the over-floor call overflowed…
        assert any(n <= floor for n in calls)  # …then the floored retry fit

    def test_lone_block_shrinks_progressively_not_straight_to_floor(self, session):
        """A lone over-window block is shrunk by halving (keeping as much as fits),
        NOT slammed straight to the 2 000-char floor — so when a mid-size truncation
        already fits the window, far more of the message survives than a floor jump
        would keep (the single-block analogue of the multi-block binary subdivision)."""
        floor = session._MIN_SUMMARY_BUDGET_CHARS
        calls: list[int] = []

        def fake_once(_system_prompt, body):
            calls.append(len(body))
            if len(body) > 9_000:  # only bodies well above the floor overflow
                raise RuntimeError("maximum context length is 524288 tokens")
            return "S"

        with (
            patch.object(session, "_summary_input_budget_chars", return_value=50_000),
            patch.object(session, "_summarize_once", side_effect=fake_once),
        ):
            result = session._summarize_blocks(["Z" * 16_000])

        assert result == "S"
        # First shrink budget is len//2 == 8 000 (< the 9 000 overflow line), so it
        # fits on the FIRST halving — the surviving body stays far above the floor,
        # which a straight-to-floor jump (~2 000) would have discarded.
        fitted = [n for n in calls if n <= 9_000]
        assert fitted and min(fitted) > 2 * floor

    def test_non_shrinking_merge_bails_at_depth_not_recursionerror(self, session):
        """If per-block summaries never compress (the merge keeps overflowing),
        recursion is bounded by the depth ceiling and bails to
        _CompactionIrreducibleError — NOT an unbounded recurse into RecursionError.
        Regression for the depth-check-only-on-the-multi-batch-path bug."""

        def no_shrink(_system_prompt, body):
            if "\n\n" in body:  # any multi-block body overflows the window
                raise RuntimeError("maximum context length is 524288 tokens")
            return body  # a single-block 'summary' is the block itself — no shrink

        with (
            patch.object(session, "_summary_input_budget_chars", return_value=100_000),
            patch.object(session, "_summarize_once", side_effect=no_shrink),
            pytest.raises(_CompactionIrreducibleError),
        ):
            session._summarize_blocks(["A" * 4000, "B" * 4000, "C" * 4000])

    def test_later_batch_overflow_keeps_completed_siblings(self, session):
        """A later batch overflowing and splitting does NOT re-summarize earlier
        completed batches — siblings are retained in the accumulator."""
        # budget ~4500 packs the 4 blocks into two 2-block batches; only the batch
        # holding 'C' overflows-and-splits, so the first batch's summary stands.
        blocks = ["A" * 2000, "B" * 2000, "C" * 2000, "D" * 2000]
        bodies: list[str] = []

        def fake_once(_system_prompt, body):
            bodies.append(body)
            if "CC" in body and "\n\n" in body:  # the multi-block batch holding C
                raise RuntimeError("maximum context length is 524288 tokens")
            return "S"

        with (
            patch.object(session, "_summary_input_budget_chars", return_value=4_500),
            patch.object(session, "_summarize_once", side_effect=fake_once),
        ):
            result = session._summarize_blocks(blocks)

        assert result == "S"
        # The first batch (A+B) was summarized exactly once, never recomputed after
        # the later (C+D) batch overflowed and split.
        assert sum(1 for b in bodies if "AAA" in b and "BBB" in b) == 1

    def test_cancel_mid_compaction_aborts_and_leaves_history(self, session):
        """A cancel observed during compaction raises GenerationCancelled (a
        BaseException) out of _summarize_batch before the message-swap, so the
        history is left untouched and the cancel propagates (not swallowed)."""
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "u " + "x" * 3000},
                {"role": "assistant", "content": "a " + "y" * 3000},
                {"role": "user", "content": "u2 " + "z" * 3000},
            ]
        )
        session._msg_tokens = [1, 1, 1]
        before = list(session.messages)

        def cancel_then_summarize(*_a, **_k):
            # The owner cancels after the first summary call lands.
            session._cancel_event.set()
            return SimpleNamespace(content="SUMMARY", finish_reason="stop")

        try:
            with (
                patch.object(session, "_summary_input_budget_chars", return_value=3_500),
                patch.object(session, "_utility_completion", side_effect=cancel_then_summarize),
                pytest.raises(GenerationCancelled),
            ):
                session._compact_messages(auto=True)
            assert session.messages == before  # history untouched
        finally:
            session._cancel_event.clear()

    def test_cancel_during_single_summary_call_aborts_before_swap(self, session):
        """A cancel that lands DURING the one-and-only summary call is honored by
        the pre-swap cancel-check — the per-batch check ran before the call, so it
        could not see it.  Regression guard for a single-batch compaction swapping
        despite a mid-call cancel."""
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "small u"},
                {"role": "assistant", "content": "small a"},
                {"role": "user", "content": "small u2"},
            ]
        )
        session._msg_tokens = [1, 1, 1]
        before = list(session.messages)

        def cancel_during_call(*_a, **_k):
            session._cancel_event.set()  # cancel lands while the single call runs
            return SimpleNamespace(content="SUMMARY", finish_reason="stop")

        try:
            with (
                # Huge budget → all blocks pack into ONE batch → exactly one call.
                patch.object(session, "_summary_input_budget_chars", return_value=100_000),
                patch.object(session, "_utility_completion", side_effect=cancel_during_call),
                pytest.raises(GenerationCancelled),
            ):
                session._compact_messages(auto=True)
            assert session.messages == before  # swap skipped, history intact
        finally:
            session._cancel_event.clear()

    def test_manual_compact_does_not_disarm_concurrent_cancel(self, session):
        """A manual /compact must NOT reset _cancel_event.  If a cancel is already in
        flight for a concurrent send worker (the /command handler runs on a separate
        thread with no worker gate), resetting it would silently disarm the cancel —
        the worker would never see it and run to completion.  Instead /compact
        observes the set event and aborts itself, leaving the cancel intact."""
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "u one"},
                {"role": "assistant", "content": "a one"},
                {"role": "user", "content": "u two"},
            ]
        )
        session._msg_tokens = [1, 1, 1]
        session._cancel_event.set()  # a concurrent send is mid-cancel
        before = list(session.messages)
        try:
            with (
                patch.object(session, "_summary_input_budget_chars", return_value=100_000),
                patch.object(session, "_utility_completion") as uc,
                pytest.raises(GenerationCancelled),
            ):
                session._compact_messages(auto=False)
            assert session._cancel_event.is_set()  # cancel left INTACT, not disarmed
            assert session.messages == before  # no swap
            uc.assert_not_called()  # bailed before issuing a summary call
        finally:
            session._cancel_event.clear()

    def test_send_clears_its_cancel_event_on_exit(self, session):
        """send() consumes its own generation's cancel signal in its finally, so a
        cancel that targeted a now-finished send can't later block an unrelated idle
        manual /compact.  A cancel is raised mid-stream here; after send() returns the
        event is clear."""
        session.messages = turns_from_dicts([{"role": "user", "content": "hi"}])
        session._msg_tokens = [1]
        session._title_generated = True

        def cancel_midstream(*_a, **_k):
            session._cancel_event.set()
            raise GenerationCancelled()

        with (
            patch.object(session, "_estimated_prompt_tokens", return_value=10),  # under hard
            patch.object(session, "_check_metacognitive_nudge", return_value=None),
            patch.object(session, "_create_stream_with_retry", side_effect=cancel_midstream),
            patch.object(session, "_full_messages", return_value=[]),
            patch.object(session, "_update_token_table"),
            patch.object(session, "_print_status_line"),
            patch.object(session, "_emit_state"),
            patch("turnstone.core.session.save_message"),
        ):
            session.send("go")

        assert not session._cancel_event.is_set()  # finally consumed this gen's cancel

    def test_compaction_aborts_swap_when_generation_superseded(self, session):
        """A stale send thread (a newer generation already started during the slow
        summary call) must NOT swap history — the pre-swap _check_cancelled(
        my_generation) raises so self.messages is left intact for the live
        generation.  Guards the history-corruption hole the pre-send layer opened by
        sitting ahead of the loop-top generation check."""
        session.messages = turns_from_dicts(
            [
                {"role": "user", "content": "u one"},
                {"role": "assistant", "content": "a one"},
                {"role": "user", "content": "u two"},
            ]
        )
        session._msg_tokens = [1, 1, 1]
        session._generation = 5  # a newer send is the live generation
        before = list(session.messages)
        summary = SimpleNamespace(content="SUMMARY", finish_reason="stop")
        with (
            patch.object(session, "_summary_input_budget_chars", return_value=100_000),
            patch.object(session, "_utility_completion", return_value=summary),
            pytest.raises(GenerationCancelled),
        ):
            # This thread belongs to the OLD generation 3 (superseded by 5).
            session._compact_messages(auto=True, my_generation=3)
        assert session.messages == before  # swap skipped — history intact for gen 5


class TestRetryRewindSkipSummary:
    """retry()/rewind() must treat the synthetic ``[Conversation summary]`` user
    turn as a non-target: it is a compaction artifact, not a real turn, so
    targeting it would re-send the bare label and regenerate over the summary."""

    def test_retry_on_bare_summary_is_noop(self, session):
        # Reactive compaction left only [summary_user, summary_asst] — no real turn.
        session.messages = turns_from_dicts(
            [
                {
                    "role": "user",
                    "content": COMPACTION_SUMMARY_LABEL,
                    "_source": COMPACTION_SOURCE,
                },
                {"role": "assistant", "content": "the dense summary"},
            ]
        )
        session._msg_tokens = [1, 1]
        before = list(session.messages)
        assert session.retry() is None  # nothing real to retry
        assert session.messages == before  # summary left intact

    def test_rewind_on_bare_summary_is_noop(self, session):
        session.messages = turns_from_dicts(
            [
                {
                    "role": "user",
                    "content": COMPACTION_SUMMARY_LABEL,
                    "_source": COMPACTION_SOURCE,
                },
                {"role": "assistant", "content": "the dense summary"},
            ]
        )
        session._msg_tokens = [1, 1]
        before = list(session.messages)
        assert session.rewind(1) == 0
        assert session.messages == before  # summary left intact

    def test_retry_targets_real_turn_and_keeps_summary(self, session):
        session.messages = turns_from_dicts(
            [
                {
                    "role": "user",
                    "content": COMPACTION_SUMMARY_LABEL,
                    "_source": COMPACTION_SOURCE,
                },
                {"role": "assistant", "content": "the dense summary"},
                {"role": "user", "content": "a real follow-up"},
                {"role": "assistant", "content": "the answer"},
            ]
        )
        session._msg_tokens = [1, 1, 1, 1]
        assert session.retry() == "a real follow-up"
        # Dropped from the real user turn onward; the summary prefix survives.
        assert [m.text for m in session.messages] == [
            COMPACTION_SUMMARY_LABEL,
            "the dense summary",
        ]

    def test_rewind_stops_at_summary_boundary(self, session):
        session.messages = turns_from_dicts(
            [
                {
                    "role": "user",
                    "content": COMPACTION_SUMMARY_LABEL,
                    "_source": COMPACTION_SOURCE,
                },
                {"role": "assistant", "content": "the dense summary"},
                {"role": "user", "content": "a real follow-up"},
                {"role": "assistant", "content": "the answer"},
            ]
        )
        session._msg_tokens = [1, 1, 1, 1]
        # Even an over-deep rewind can't cross into the summary.
        removed = session.rewind(5)
        assert removed == 2  # only the one real turn (user + assistant)
        assert [m.text for m in session.messages] == [
            COMPACTION_SUMMARY_LABEL,
            "the dense summary",
        ]
