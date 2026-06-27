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
        """Before the first API call there is no provider anchor."""
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
