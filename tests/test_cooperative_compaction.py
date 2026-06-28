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
from unittest.mock import patch

import pytest

from tests._session_helpers import make_session
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
            session._maybe_compact_midturn()
        compact.assert_called_once_with("mid-turn")
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
            session._maybe_compact_midturn()
        compact.assert_called_once_with("mid-turn")
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
        compact.assert_called_once_with(auto=True, preserve_tail=0)
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
            patch.object(session, "_estimated_prompt_tokens", return_value=9_999),
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
        truncated = [b for b in flat if "[truncated]" in b]
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
            body = messages[1]["content"]
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

        Distinct from ``test_irreducible_input_bails_to_false`` (which bails at
        depth 0 via the ``len(batches) >= len(blocks)`` arm before any model
        call): here depth 0 packs into several batches AND reduces, so the level
        succeeds and recurses; depth 1 still has >1 batch but a strictly smaller
        count (so the len arm is False), and ``depth >= 1`` fires the bail.  That
        the depth-0 calls ran first is proven by ``_utility_completion`` being
        called (≥1) despite the False return.
        """
        session.context_window = 5_000
        session.compact_max_tokens = 4_000  # squeezes the input budget
        session._system_tokens = 0
        session._MAX_SUMMARY_DEPTH = 1  # positive, so depth 0 runs before the bail
        budget = session._summary_input_budget_chars()

        # ~30 messages, each block bigger than 1/6 of the budget → depth 0 packs
        # into several batches (and len(batches) < len(blocks), so it recurses).
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
        # three, so depth 1 reduces the batch count without collapsing to one —
        # the len arm stays False and the depth ceiling is what bails.
        partial = "P" * ((budget * 2) // 5)
        summary = SimpleNamespace(content=partial, finish_reason="stop")

        with patch.object(session, "_utility_completion", return_value=summary) as uc:
            result = session._compact_messages(auto=True)

        assert result is False
        assert session.messages == before  # untouched on the bail
        assert uc.call_count >= 1  # depth-0 ran (depth arm), not the len arm

    def test_irreducible_input_bails_to_false(self, session):
        """A genuinely irreducible case still bails to False (the "too large"
        path) rather than fabricate, and without burning a model call.

        Needs a *tiny* window now that Fix 1 keeps the budget healthy on normal
        windows: at context_window=900 the output reserve + compactor prompt +
        safety already exceed the window, so the true input capacity is negative
        and ``_summary_input_budget_chars`` caps the budget to 0.  Each ~5000-char
        message head+tail-caps to ~1525, far over the 0/1-char budget, so
        ``_pack_blocks`` truncates each into its own batch:
        ``len(batches) == len(blocks)`` → irreducible bail at depth 0, no model call.
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

        with patch.object(session, "_utility_completion") as uc:
            result = session._compact_messages(auto=True)

        assert result is False
        uc.assert_not_called()  # no reduction at depth 0 → bail before any call
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
        session.creative_mode = False
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
