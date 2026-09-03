"""Tests for capacity-aware tool output truncation and context overflow recovery."""

from __future__ import annotations

import contextlib
import re
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests._session_helpers import make_result
from turnstone.core.judge import JudgeConfig
from turnstone.core.session import (
    _AGENT_TOOL_OUTPUT_CAP,
    _BASH_OUTPUT_CONSUMED_NOTICE,
    _MIN_COUNTED_RESULT_CHARS,
    _REPEAT_WARNING,
    _TRUNCATION_FLOOR_CHARS,
    ChatSession,
    _active_task_agent_cancel_scope,
    _bash_output_truncation_marker,
)
from turnstone.core.trajectory import Role, turns_from_dicts
from turnstone.core.truncation import BoundedTextBuffer, ProjectedText, truncate_text

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def session(tmp_db, mock_openai_client):
    """Create a ChatSession with defaults for truncation testing."""
    return ChatSession(
        client=mock_openai_client,
        model="test-model",
        ui=MagicMock(),
        instructions=None,
        temperature=0.5,
        tool_timeout=10,
        context_window=10_000,
        max_tokens=1_000,
    )


# ---------------------------------------------------------------------------
# _truncate_output
# ---------------------------------------------------------------------------


class TestTruncateOutput:
    def test_auto_limit_is_one_fifth_of_the_remaining_budget(self, session):
        batch_budget_tokens = 1000
        fifth = int(batch_budget_tokens * session._chars_per_token * 0.20)

        assert (
            session._tool_result_truncation_limit(batch_budget_tokens=batch_budget_tokens) == fifth
        )
        # The session ceiling still bounds the share.
        session.tool_truncation = fifth // 2
        assert (
            session._tool_result_truncation_limit(batch_budget_tokens=batch_budget_tokens)
            == fifth // 2
        )

    def test_read_file_is_cut_once_by_the_fold_with_its_real_size(self, session, tmp_path):
        """Executors hand the fold their complete result, so the fold's marker
        reports the file's real size rather than an earlier projection's."""
        path = tmp_path / "big.txt"
        path.write_text("".join(f"line-{i:05d} {'x' * 40}\n" for i in range(600)))

        _, output = session._exec_read_file(
            {"call_id": "r1", "path": str(path), "offset": None, "limit": None}
        )

        assert len(output) > session.tool_truncation
        assert "chars truncated" not in output
        cap = session._tool_result_truncation_limit(batch_budget_tokens=2000)
        result = session._truncate_output_result(
            output, remaining_budget_tokens=2000, maximum_chars=cap
        )
        assert len(result.text) <= cap
        assert result.original_chars == len(output)
        assert result.retained_chars + result.omitted_chars == len(output)
        assert f"[{result.omitted_chars} chars truncated" in result.text
        assert result.text.count("chars truncated") == 1

    def test_operator_limit_replaces_the_automatic_share(self, session):
        session._manual_tool_truncation = True
        session.tool_truncation = 1234

        assert session._tool_result_truncation_limit(batch_budget_tokens=100) == 1234

    def test_allowance_below_the_counted_floor_takes_the_zero_budget_doors(self, session):
        """A cap too small to carry a counted marker never renders a bare
        fragment that reads as complete: the result is funded by the grace
        floor when the drain grants it, and dropped with the notice otherwise."""
        limit = session._tool_result_truncation_limit(batch_budget_tokens=1)
        output = "longer than one character"

        assert 0 <= limit < _MIN_COUNTED_RESULT_CHARS
        funded = session._truncate_output_result(
            output, remaining_budget_tokens=1, floor_chars=len(output), maximum_chars=limit
        )
        assert funded.text == output
        dropped = session._truncate_output_result(
            output, remaining_budget_tokens=1, maximum_chars=limit
        )
        assert "context budget exhausted" in dropped.text
        assert "…" not in dropped.text

    def test_manual_cap_below_the_counted_floor_is_the_operators_choice(self, session):
        session.tool_truncation = 40
        session._manual_tool_truncation = True

        result = session._truncate_output_result(
            "x" * 500, remaining_budget_tokens=10_000, maximum_chars=40
        )

        assert len(result.text) <= 40
        assert "context budget exhausted" not in result.text
        assert not session._allowance_is_exhausted(
            "x" * 500, budget_tokens=10_000, maximum_chars=40
        )
        session._manual_tool_truncation = False
        assert session._allowance_is_exhausted("x" * 500, budget_tokens=10_000, maximum_chars=40)

    def test_no_truncation_when_under_limit(self, session):
        result = session._truncate_output("short text")
        assert result == "short text"

    def test_truncates_to_tool_truncation_limit(self, session):
        session.tool_truncation = 100
        big = "x" * 500
        result = session._truncate_output(big)
        assert len(result) <= 100  # source edges + marker share the cap
        assert "chars truncated" in result

    def test_budget_aware_truncation(self, session):
        session.tool_truncation = 100_000
        session._chars_per_token = 4.0
        # Budget of 50 tokens = 200 chars
        big = "x" * 1000
        result = session._truncate_output(big, remaining_budget_tokens=50)
        assert len(result) <= 200  # source edges + marker share the budget
        assert "chars truncated" in result

    def test_budget_takes_precedence_when_smaller(self, session):
        session.tool_truncation = 10_000
        session._chars_per_token = 4.0
        # Budget of 50 tokens = 200 chars, smaller than tool_truncation
        big = "x" * 500
        result = session._truncate_output(big, remaining_budget_tokens=50)
        assert len(result) <= 200
        assert "chars truncated" in result

    def test_empty_output_passes_at_any_budget(self, session):
        # A 0-char result must never be replaced by a ~310-char drop notice
        # ("none of it could be added" would be false, and net-negative).
        assert session._truncate_output("") == ""
        assert session._truncate_output("", remaining_budget_tokens=0) == ""
        assert session._truncate_output("", remaining_budget_tokens=0, floor_chars=0) == ""

    def test_zero_budget_small_result_drops_without_floor(self, session):
        # The function itself has no small-pass: at zero budget an unfloored
        # result gets the drop notice regardless of size.  Verbatim
        # admission of small results is the DRAIN's per-batch grace-pool
        # decision, funded through floor_chars — see TestZeroBudgetDrain.
        small = "x" * 1000
        result = session._truncate_output(small, remaining_budget_tokens=0)
        assert "dropped" in result
        assert "context budget exhausted" in result

    def test_zero_budget_bulky_result_gets_honest_drop_notice(self, session):
        big = "x" * 5000
        result = session._truncate_output(big, remaining_budget_tokens=0)
        # States the truth: the call ran, the output is gone — never the
        # old successful-but-trimmed impersonation (#883).
        assert "dropped" in result
        assert "context budget exhausted" in result
        assert "5000" in result
        assert not result.startswith("[Output truncated")
        assert "xxxx" not in result  # no payload content leaks into the notice
        assert len(result) < 400

    def test_negative_budget_same_as_zero(self, session):
        big = "x" * 5000
        result = session._truncate_output(big, remaining_budget_tokens=-10)
        assert "dropped" in result
        assert "context budget exhausted" in result

    def test_floor_overrides_zero_budget(self, session):
        from turnstone.core.session import _TRUNCATION_FLOOR_CHARS

        big = "A" * 5000 + "Z" * 5000
        result = session._truncate_output(
            big, remaining_budget_tokens=0, floor_chars=_TRUNCATION_FLOOR_CHARS
        )
        # Floored: head+tail truncation at the floor, not the drop notice.
        assert "chars truncated" in result
        assert result.startswith("A")
        assert result.endswith("Z")
        assert len(result) <= _TRUNCATION_FLOOR_CHARS

    def test_floor_overrides_operator_cap(self, session):
        # The floor deliberately wins over a tiny operator-set cap:
        # framework integrity beats config for structural results.
        session.tool_truncation = 100
        big = "A" * 5000 + "Z" * 5000
        result = session._truncate_output(big, floor_chars=2048)
        assert "chars truncated" in result
        assert len(result) > 1000  # floored, not capped at 100

    def test_floor_no_effect_with_healthy_budget(self, session):
        session.tool_truncation = 100_000
        out = "x" * 500
        assert session._truncate_output(out, remaining_budget_tokens=5000, floor_chars=2048) == out

    def test_none_budget_uses_fixed_limit(self, session):
        session.tool_truncation = 100
        big = "x" * 500
        result = session._truncate_output(big, remaining_budget_tokens=None)
        assert "100 char limit" in result


# ---------------------------------------------------------------------------
# _remaining_token_budget
# ---------------------------------------------------------------------------


class TestRemainingTokenBudget:
    def test_empty_session(self, session):
        session._tools = []  # isolate the budget formula from the tool-def estimate
        session._system_tokens = 500
        session._msg_tokens = []
        budget = session._remaining_token_budget()
        # 10000 - 500 - 0 - 1000 - 500 (5%) = 8000
        assert budget == 8000

    def test_partially_full(self, session):
        session._tools = []  # isolate the budget formula from the tool-def estimate
        session._system_tokens = 500
        session._msg_tokens = [2000, 3000]
        budget = session._remaining_token_budget()
        # 10000 - 500 - 5000 - 1000 - 500 = 3000
        assert budget == 3000

    def test_overfull_returns_zero(self, session):
        session._system_tokens = 500
        session._msg_tokens = [9000]
        assert session._remaining_token_budget() == 0

    def test_exactly_full_returns_zero(self, session):
        session._system_tokens = 500
        session._msg_tokens = [8000]
        assert session._remaining_token_budget() == 0

    def test_max_tokens_equals_context_window(self, tmp_db, mock_openai_client):
        """Regression: max_tokens >= context_window must not zero the budget."""
        s = ChatSession(
            client=mock_openai_client,
            model="test-model",
            ui=MagicMock(),
            instructions=None,
            temperature=0.5,
            tool_timeout=10,
            context_window=32_768,
            max_tokens=32_768,
        )
        s._tools = []  # isolate the budget formula from the tool-def estimate
        s._system_tokens = 500
        s._msg_tokens = [1000]
        budget = s._remaining_token_budget()
        # response_reserve = min(32768, 32768//4) = 8192
        # safety = 32768 * 0.05 = 1638
        # budget = 32768 - 500 - 1000 - 8192 - 1638 = 21438
        assert budget > 20_000
        # Tool output should NOT be collapsed to a placeholder
        big = "x" * 5000
        result = s._truncate_output(big, remaining_budget_tokens=budget)
        assert result == big  # 5000 chars fits easily in 21K+ token budget


# ---------------------------------------------------------------------------
# Context overflow recovery
# ---------------------------------------------------------------------------


class TestContextOverflowRecovery:
    """Test that context-length errors trigger compact-and-retry."""

    @pytest.mark.parametrize(
        "overflow_text",
        [
            pytest.param("maximum context length exceeded", id="openai"),
            pytest.param("prompt is too long: 250000 tokens > 200000 maximum", id="anthropic"),
        ],
    )
    def test_context_overflow_triggers_compact(self, session, overflow_text):
        session.messages = turns_from_dicts([{"role": "user", "content": "hi"}])
        session._msg_tokens = [1]

        call_count = 0

        def mock_stream_response(my_generation=0):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception(overflow_text)
            return make_result(content="ok")

        compact_mock = MagicMock()
        with (
            patch.object(session, "_stream_response", side_effect=mock_stream_response),
            patch.object(session, "_compact_messages", compact_mock),
            patch.object(session, "_full_messages", return_value=[]),
            patch.object(
                session,
                "_compaction_policy",
                return_value=_policy(over_soft=False),
            ),
            patch.object(session, "_update_token_table"),
            patch.object(session, "_print_status_line"),
            patch.object(session, "_emit_state"),
            patch("turnstone.core.session.save_message"),
        ):
            session.send("hello")

        # my_generation must be the send's own generation — a stale send that
        # hits overflow must not compact-and-swap a newer generation's history.
        compact_mock.assert_called_once_with(auto=True, my_generation=session._generation)
        assert call_count == 2

    def test_non_context_error_propagates(self, session):
        session.messages = turns_from_dicts([{"role": "user", "content": "hi"}])
        session._msg_tokens = [1]

        with (
            patch.object(
                session,
                "_stream_response",
                side_effect=Exception("authentication failed"),
            ),
            patch.object(session, "_full_messages", return_value=[]),
            patch.object(session, "_emit_state"),
            patch("turnstone.core.session.save_message"),
            pytest.raises(Exception, match="authentication failed"),
        ):
            session.send("hello")

    def test_compact_failure_raises_original_error(self, session):
        session.messages = turns_from_dicts([{"role": "user", "content": "hi"}])
        session._msg_tokens = [1]

        with (
            patch.object(
                session,
                "_stream_response",
                side_effect=Exception("maximum context length exceeded"),
            ),
            patch.object(session, "_compact_messages", side_effect=RuntimeError("compact failed")),
            patch.object(session, "_full_messages", return_value=[]),
            patch.object(session, "_emit_state"),
            patch("turnstone.core.session.save_message"),
            pytest.raises(Exception, match="maximum context length exceeded"),
        ):
            session.send("hello")


# ---------------------------------------------------------------------------
# Zero-budget drain behavior (#883): the floor doors + the band-closing compact
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _send_with_tool_batches(session, batches, **extra_patches):
    """Drive one ``send()`` through the tool-execution drain with canned results.

    *batches* is a list of ``(tool_calls, results)`` pairs, one send-loop
    iteration each: ``_stream_response`` returns a ``ModelTurnResult`` whose
    ``.tool_calls`` carries each batch's *tool_calls* in order, then a plain
    reply ends the loop.  Each *results* is what ``_execute_tools`` hands the
    drain — the truncation/floor/compact path under test runs REAL code
    between the mocked boundaries.  Mirrors
    ``tests/test_session.py::_send_with_mocks``; kept local because these
    tests patch the budget/compaction seam differently per scenario.

    ``_estimated_prompt_tokens`` is pinned LOW so the end-of-turn/owed
    compaction paths stay quiet — every compaction observed by these tests
    is therefore the drain's own zero-budget trigger, keeping exact
    call-count assertions honest.  Title generation is pre-latched off so
    no background utility-completion thread churns against the mock client.
    """
    session._title_generated = True
    responses = [make_result(content="", tool_calls=tool_calls) for tool_calls, _ in batches] + [
        make_result(content="done")
    ]
    exec_results = [(results, []) for _, results in batches]

    def mock_response(_gen):
        return responses.pop(0)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(session, "_stream_response", side_effect=mock_response))
        stack.enter_context(patch.object(session, "_execute_tools", side_effect=exec_results))
        for attr, value in extra_patches.items():
            stack.enter_context(patch.object(session, attr, value))
        stack.enter_context(patch.object(session, "_estimated_prompt_tokens", return_value=100))
        stack.enter_context(patch.object(session, "_full_messages", return_value=[]))
        stack.enter_context(patch.object(session, "_update_token_table"))
        stack.enter_context(patch.object(session, "_print_status_line"))
        stack.enter_context(patch.object(session, "_emit_state"))
        stack.enter_context(patch.object(session, "_visible_memory_count", return_value=0))
        stack.enter_context(patch.object(session, "_apply_post_execute_advisories"))
        stack.enter_context(patch("turnstone.core.session.save_message"))
        yield


@contextlib.contextmanager
def _send_with_tool_batch(session, tool_calls, results, **extra_patches):
    """Single-batch form of :func:`_send_with_tool_batches`."""
    with _send_with_tool_batches(session, [(tool_calls, results)], **extra_patches):
        yield


def _tool_turn_texts(session):
    return [m.text for m in session.messages if m.role is Role.TOOL]


def _policy(*, owed: bool = False, over_soft: bool = False, over_hard: bool = False):
    return SimpleNamespace(
        owed=lambda *_args, **_kwargs: owed,
        over_soft=lambda *_args, **_kwargs: over_soft,
        over_hard=lambda *_args, **_kwargs: over_hard,
    )


class TestAutomaticBashDrain:
    def test_parallel_bash_results_share_one_remaining_budget_snapshot(self, session):
        output = "BEGIN\n" + "middle\n" * 5000 + "END\n"
        calls = [
            {"id": "tc_b1", "function": {"name": "bash", "arguments": "{}"}},
            {"id": "tc_b2", "function": {"name": "bash", "arguments": "{}"}},
        ]
        batch_budget_tokens = 4000
        with _send_with_tool_batch(
            session,
            calls,
            [("tc_b1", output), ("tc_b2", output)],
            _remaining_token_budget=MagicMock(return_value=batch_budget_tokens),
            _compact_messages=MagicMock(return_value=False),
        ):
            session.send("go")

        expected = session._tool_result_truncation_limit(
            batch_budget_tokens=batch_budget_tokens,
        )
        first, second = _tool_turn_texts(session)
        assert len(first) == expected
        assert len(second) == expected
        assert "chars truncated" in first
        assert "chars truncated" in second

    def test_whitespace_padded_tool_name_still_gets_the_bash_share(self, session):
        """Dispatch strips a padded provider name; the fold's policy must too."""
        output = "BEGIN\n" + "middle\n" * 5000 + "END\n"
        calls = [{"id": "tc_b1", "function": {"name": "  bash\n", "arguments": "{}"}}]
        batch_budget_tokens = 4000
        with _send_with_tool_batch(
            session,
            calls,
            [("tc_b1", output)],
            _remaining_token_budget=MagicMock(return_value=batch_budget_tokens),
            _compact_messages=MagicMock(return_value=False),
        ):
            session.send("go")

        (text,) = _tool_turn_texts(session)
        assert len(text) == session._tool_result_truncation_limit(
            batch_budget_tokens=batch_budget_tokens
        )

    def test_redaction_expansion_cannot_exceed_the_admitted_cap(self, session):
        """A guard that lengthens a capped result is re-capped before the fold."""
        session._judge_config = JudgeConfig(output_guard=True, output_guard_llm=False)
        assert session._judge_cfg is not None and session._judge_cfg.output_guard
        output = "BEGIN\n" + "middle\n" * 5000 + "END\n"
        calls = [{"id": "tc_b1", "function": {"name": "bash", "arguments": "{}"}}]
        batch_budget_tokens = 4000

        def expanding_guard(_call_id, text, *_args, **_kwargs):
            return text + "[REDACTED:secret]" * 500, None

        with _send_with_tool_batch(
            session,
            calls,
            [("tc_b1", output)],
            _remaining_token_budget=MagicMock(return_value=batch_budget_tokens),
            _compact_messages=MagicMock(return_value=False),
            _evaluate_output=MagicMock(side_effect=expanding_guard),
        ):
            session.send("go")

        (text,) = _tool_turn_texts(session)
        assert len(text) <= session._tool_result_truncation_limit(
            batch_budget_tokens=batch_budget_tokens
        )
        assert "chars truncated" in text

    def test_growth_of_whole_admitted_results_is_accepted(self, session):
        """A result the drain admitted whole is what the guard produced, grown
        or not: nothing small is cut for having grown or replaced by a notice
        several times its size, and the batch may exceed its allowance by that
        growth, which the pre-send guard absorbs."""
        session._judge_config = JudgeConfig(output_guard=True, output_guard_llm=False)
        session._chars_per_token = 4.0
        batch_budget_tokens = 160
        allowance_chars = int(batch_budget_tokens * session._chars_per_token)
        share = session._tool_result_truncation_limit(batch_budget_tokens=batch_budget_tokens)
        short = "PASSWORD=x\n" * 9
        count = 6
        calls = [
            {"id": f"tc_{i}", "function": {"name": "read_file", "arguments": "{}"}}
            for i in range(count)
        ]
        assert len(short) <= share and count * len(short) <= allowance_chars

        def expanding_guard(_call_id, text, *_args, **_kwargs):
            return text.replace("PASSWORD=x", "PASSWORD=[REDACTED:secret]"), None

        with _send_with_tool_batch(
            session,
            calls,
            [(f"tc_{i}", short) for i in range(count)],
            _remaining_token_budget=MagicMock(return_value=batch_budget_tokens),
            _compact_messages=MagicMock(return_value=False),
            _evaluate_output=MagicMock(side_effect=expanding_guard),
        ):
            session.send("go")

        texts = _tool_turn_texts(session)
        expanded = short.replace("PASSWORD=x", "PASSWORD=[REDACTED:secret]")
        assert texts == [expanded] * count
        assert sum(len(t) for t in texts) > allowance_chars

    def test_bash_output_notice_rides_the_fold_marker(self, session):
        """A cut ``bash_output`` delta says, inside its cap, that the consumed
        middle cannot be re-read; a tight cap keeps the ordinary marker."""
        output = "BEGIN\n" + "middle\n" * 5000 + "END\n"
        calls = [{"id": "tc_o1", "function": {"name": "bash_output", "arguments": "{}"}}]
        batch_budget_tokens = 4000
        with _send_with_tool_batch(
            session,
            calls,
            [("tc_o1", output)],
            _remaining_token_budget=MagicMock(return_value=batch_budget_tokens),
            _compact_messages=MagicMock(return_value=False),
        ):
            session.send("go")

        (text,) = _tool_turn_texts(session)
        cap = session._tool_result_truncation_limit(batch_budget_tokens=batch_budget_tokens)
        assert len(text) == cap
        assert text.count("chars truncated") == 1
        assert _BASH_OUTPUT_CONSUMED_NOTICE in text
        assert text.startswith("BEGIN\n") and text.endswith("END\n")

        tight = session._truncate_output_result(
            output,
            remaining_budget_tokens=4000,
            maximum_chars=200,
            marker_factory=_bash_output_truncation_marker,
        )
        assert len(tight.text) <= 200
        assert tight.text.count("chars truncated") == 1
        assert _BASH_OUTPUT_CONSUMED_NOTICE not in tight.text

    def test_bash_output_marker_count_matches_the_cut_at_every_cap(self):
        """The notice decision must not depend on the omitted count: a marker
        whose length toggles with that count cannot settle in the primitive's
        fixed point, and its count would lag the cut."""
        for original in (224, 1000, 60000):
            text = "x" * original
            for limit in range(1, 700):
                result = truncate_text(text, limit, marker_factory=_bash_output_truncation_marker)
                assert len(result.text) <= limit
                claimed = re.search(r"\[(\d+) chars truncated", result.text)
                if claimed is not None:
                    assert int(claimed.group(1)) == original - result.retained_chars, (
                        original,
                        limit,
                    )

    def test_lengthened_cut_result_is_recut_at_the_drain_marker(self, session):
        """A cut rendering the guard lengthened past its cap is re-cut as two
        edges around the drain's marker: one marker, within the cap, both
        edges kept."""
        session._judge_config = JudgeConfig(output_guard=True, output_guard_llm=False)
        secret = "PASSWORD=x\n" * 40
        cut = "H" * 2500 + "T" * 2500
        calls = [
            {"id": "tc_s", "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "tc_c", "function": {"name": "read_file", "arguments": "{}"}},
        ]
        batch_budget_tokens = 400

        def guard(_call_id, text, *_args, **_kwargs):
            if text.startswith("PASSWORD"):
                return text.replace("PASSWORD=x", "PASSWORD=[REDACTED:secret]" * 3), None
            shrunk_head = text.replace("H" * 100, "[REDACTED]", 1)
            return shrunk_head[:-10] + "[REDACTED:api_key:" + "k" * 100 + "]", None

        with _send_with_tool_batch(
            session,
            calls,
            [("tc_s", secret), ("tc_c", cut)],
            _remaining_token_budget=MagicMock(return_value=batch_budget_tokens),
            _compact_messages=MagicMock(return_value=False),
            _evaluate_output=MagicMock(side_effect=guard),
        ):
            session.send("go")

        _, text = _tool_turn_texts(session)
        cap = session._tool_result_truncation_limit(batch_budget_tokens=batch_budget_tokens)
        assert len(text) <= cap
        assert text.count("chars truncated") == 1
        assert text.startswith("[REDACTED]H")
        assert text.endswith("k]")

    def test_small_results_stay_whole_when_siblings_grow_at_an_exhausted_budget(self, session):
        """Redaction growth of earlier results never costs later small results
        their verbatim admission: what the drain admitted whole stays whole."""
        session._judge_config = JudgeConfig(output_guard=True, output_guard_llm=False)
        secret = "PASSWORD=a\n" * 3
        plain = "p" * 32
        calls = [
            {"id": f"tc_{i}", "function": {"name": "read_file", "arguments": "{}"}}
            for i in range(4)
        ]

        def guard(_call_id, text, *_args, **_kwargs):
            return text.replace("PASSWORD=a", "PASSWORD=[REDACTED:secret]"), None

        with _send_with_tool_batch(
            session,
            calls,
            [("tc_0", secret), ("tc_1", secret), ("tc_2", plain), ("tc_3", plain)],
            _remaining_token_budget=MagicMock(return_value=40),
            _compact_messages=MagicMock(return_value=False),
            _evaluate_output=MagicMock(side_effect=guard),
        ):
            session.send("go")

        texts = _tool_turn_texts(session)
        assert texts[2] == plain and texts[3] == plain
        assert all("context budget exhausted" not in t for t in texts)
        assert "[REDACTED:secret]" in texts[0]

    def test_a_whole_admitted_result_the_guard_lengthened_stays_whole(self, session):
        """A small result admitted whole through the grace pool and then
        lengthened by redaction is what the guard produced: it is not cut for
        having grown."""
        session._judge_config = JudgeConfig(output_guard=True, output_guard_llm=False)
        session._chars_per_token = 4.0
        batch_budget_tokens = 210
        allowance_chars = int(batch_budget_tokens * 4.0)
        share = session._tool_result_truncation_limit(batch_budget_tokens=batch_budget_tokens)
        # Enough share-sized siblings to spend the drain's whole allowance, so
        # the small result arrives at an exhausted budget and passes through
        # the grace pool; the guard withholds the first sibling, which frees
        # allowance for the replay.
        fillers = -(-allowance_chars // share)
        assert fillers * -(-share // 4) >= batch_budget_tokens
        small = "k" * 100 + "sk-abc123"
        calls = [
            {"id": f"tc_{i}", "function": {"name": "read_file", "arguments": "{}"}}
            for i in range(fillers + 1)
        ]
        results = [(f"tc_{i}", ("a" if i == 0 else "b") * share) for i in range(fillers)]
        results.append((f"tc_{fillers}", small))

        def guard(_call_id, text, *_args, **_kwargs):
            if text.startswith("a"):
                return "[withheld]", None
            return text.replace("sk-abc123", "[REDACTED:api_key]"), None

        with _send_with_tool_batch(
            session,
            calls,
            results,
            _remaining_token_budget=MagicMock(return_value=batch_budget_tokens),
            _compact_messages=MagicMock(return_value=False),
            _evaluate_output=MagicMock(side_effect=guard),
        ):
            session.send("go")

        texts = _tool_turn_texts(session)
        assert texts[0] == "[withheld]"
        assert texts[1:fillers] == ["b" * share] * (fillers - 1)
        assert texts[-1] == "k" * 100 + "[REDACTED:api_key]"

    def test_bash_output_result_takes_the_floor_at_an_exhausted_allowance(self, session):
        """A ``bash_output`` delta was consumed in producing it, so at an
        exhausted allowance it is cut to the guaranteed floor with its consumed
        notice rather than replaced by a drop notice that invites a re-read."""
        delta = "bash_1 (running)\n" + "line\n" * 2000
        calls = [{"id": "tc_o1", "function": {"name": "bash_output", "arguments": "{}"}}]
        with _send_with_tool_batch(
            session,
            calls,
            [("tc_o1", delta)],
            _remaining_token_budget=MagicMock(return_value=0),
            _compact_messages=MagicMock(return_value=False),
        ):
            session.send("go")

        (text,) = _tool_turn_texts(session)
        assert "context budget exhausted" not in text
        assert len(text) <= _TRUNCATION_FLOOR_CHARS
        assert text.count("chars truncated") == 1
        assert _BASH_OUTPUT_CONSUMED_NOTICE in text

    def test_bash_output_read_inside_a_task_agent_is_sized_to_the_agent_cap(self, session):
        """A task agent's results are clipped to its own cap rather than folded,
        so a read issued from one is bounded to that cap and leaves the rest
        unread instead of consuming lines the agent can never see."""
        session._chars_per_token = 4.0
        shell = session._background_shells.spawn(
            "for i in $(seq 1 400); do printf 'line-%03d-%s\\n' $i " + "y" * 50 + "; done"
        )
        deadline = time.monotonic() + 15
        while shell.status == "running" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert shell.status == "completed"

        token = _active_task_agent_cancel_scope.set(object())  # type: ignore[arg-type]
        try:
            with patch.object(session, "_remaining_token_budget", return_value=100_000):
                _, text = session._exec_bash_output({"call_id": "c1", "shell_id": shell.shell_id})
        finally:
            _active_task_agent_cancel_scope.reset(token)

        assert len(text) <= _AGENT_TOOL_OUTPUT_CAP
        assert "remain unread" in text
        assert "line-001-" in text and "line-400-" not in text

    def test_recut_never_splices_out_the_middle_the_drain_removed(self, session):
        """When redaction grows a cut rendering's edges past the producer's own
        size, the re-cut still counts the middle the drain removed instead of
        treating the grown edges as the complete source and splicing them."""
        session._judge_config = JudgeConfig(output_guard=True, output_guard_llm=False)
        session._chars_per_token = 4.0
        batch_budget_tokens = 1000
        share = session._tool_result_truncation_limit(batch_budget_tokens=batch_budget_tokens)
        # Five siblings leave the last result a budget below its share; the
        # first is withheld by the guard, which frees allowance afterwards.
        fillers = ["a" * share, *(["b" * share] * 3), "b" * (share // 2)]
        secret = "PASSWORD=x\n" * 64
        calls = [
            {"id": f"tc_{i}", "function": {"name": "read_file", "arguments": "{}"}}
            for i in range(len(fillers) + 1)
        ]
        results = [(f"tc_{i}", text) for i, text in enumerate(fillers)]
        results.append((f"tc_{len(fillers)}", secret))

        def guard(_call_id, text, *_args, **_kwargs):
            if text.startswith("a"):
                return "[withheld]", None
            return text.replace("PASSWORD=x", "PASSWORD=[REDACTED:secret]" * 3), None

        with _send_with_tool_batch(
            session,
            calls,
            results,
            _remaining_token_budget=MagicMock(return_value=batch_budget_tokens),
            _compact_messages=MagicMock(return_value=False),
            _evaluate_output=MagicMock(side_effect=guard),
        ):
            session.send("go")

        text = _tool_turn_texts(session)[-1]
        drain_limit = share // 2
        assert len(text) <= drain_limit
        assert text.count("chars truncated") == 1
        claimed = re.search(r"\[(\d+) chars truncated", text)
        assert claimed is not None and int(claimed.group(1)) >= len(secret) - drain_limit

    def test_recut_is_skipped_when_the_marker_is_not_unique(self, session):
        """A rendering whose text no longer locates the drain's marker exactly
        once (a one-character fallback marker the source also contains) passes
        as the guard left it rather than being split at the wrong place."""
        session._judge_config = JudgeConfig(output_guard=True, output_guard_llm=False)
        session.tool_truncation = 60
        session._manual_tool_truncation = True
        source = "token=SECRET … " + "x" * 200
        calls = [{"id": "tc_e", "function": {"name": "read_file", "arguments": "{}"}}]

        def guard(_call_id, text, *_args, **_kwargs):
            return text.replace("SECRET", "[REDACTED:secret]"), None

        with _send_with_tool_batch(
            session,
            calls,
            [("tc_e", source)],
            _remaining_token_budget=MagicMock(return_value=10_000),
            _compact_messages=MagicMock(return_value=False),
            _evaluate_output=MagicMock(side_effect=guard),
        ):
            session.send("go")

        (text,) = _tool_turn_texts(session)
        assert text.startswith("token=[REDACTED:secret] …")
        assert text.count("…") == 2 and len(text) > 60

    def test_bash_output_read_is_bounded_by_the_running_budget_in_manual_mode(self, session):
        """An operator cap is uniform, but the fold still applies the running
        allowance, so the read is bounded by both and never consumes lines the
        fold would cut away."""
        session._chars_per_token = 4.0
        session.tool_truncation = 100_000
        session._manual_tool_truncation = True
        shell = session._background_shells.spawn(
            "for i in $(seq 1 400); do printf 'line-%03d-%s\\n' $i " + "y" * 50 + "; done"
        )
        deadline = time.monotonic() + 15
        while shell.status == "running" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert shell.status == "completed"

        with patch.object(session, "_remaining_token_budget", return_value=250):
            _, text = session._exec_bash_output({"call_id": "c1", "shell_id": shell.shell_id})

        assert len(text) <= 1000
        assert "remain unread" in text
        assert "line-001-" in text and "line-400-" not in text

    def test_bounded_capture_never_passes_the_small_result_door(self, session):
        """The zero-budget doors judge a result by its producer's size, so a
        multi-megabyte capture whose rendering is small is not admitted as a
        small result and does not spend the grace pool."""
        buffer = BoundedTextBuffer(1000)
        buffer.append("x" * 1_000_000)
        capture = buffer.projected()
        assert isinstance(capture, ProjectedText) and len(capture) <= 1000

        assert session._allowance_is_exhausted(capture, budget_tokens=0, maximum_chars=2000)
        assert session._admission_floor(
            "bash", "tc", capture, exhausted=True, verbatim_pool=4096
        ) == (0, 4096)
        dropped = session._truncate_output_result(
            capture, remaining_budget_tokens=0, exhausted=True
        )
        assert "context budget exhausted" in dropped.text
        assert "1000000-char result" in dropped.text

    def test_bash_output_read_leaves_what_the_fold_could_not_admit_unread(self, session):
        """A delta larger than the fold's share is read in whole lines up to
        that share; the rest stays readable instead of being consumed by the
        cursor and cut away by the fold."""
        session._chars_per_token = 4.0
        shell = session._background_shells.spawn(
            "for i in $(seq 1 400); do printf 'line-%03d-%s\\n' $i " + "y" * 50 + "; done"
        )
        deadline = time.monotonic() + 15
        while shell.status == "running" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert shell.status == "completed"
        cap = session._tool_result_truncation_limit(batch_budget_tokens=5000)

        seen: list[str] = []
        reads = 0
        with patch.object(session, "_remaining_token_budget", return_value=5000):
            while reads < 100:
                reads += 1
                _, text = session._exec_bash_output(
                    {"call_id": f"c{reads}", "shell_id": shell.shell_id}
                )
                assert len(text) <= cap
                assert _BASH_OUTPUT_CONSUMED_NOTICE not in text
                seen.extend(re.findall(r"line-\d{3}-", text))
                if "remain unread" not in text:
                    break

        assert 1 < reads < 100
        assert seen == [f"line-{i:03d}-" for i in range(1, 401)]

    def test_true_size_survives_redaction_of_a_bounded_capture(self, session):
        """Redaction turns the rendering into a plain string; the replayed cut
        must still report omission against the producer's true size."""
        session._judge_config = JudgeConfig(output_guard=True, output_guard_llm=False)
        buffer = BoundedTextBuffer(300)
        buffer.append("PASSWORD=x\n" * 1000)
        capture = buffer.projected()
        assert isinstance(capture, ProjectedText)
        calls = [{"id": "tc_b1", "function": {"name": "bash", "arguments": "{}"}}]
        batch_budget_tokens = 200

        def expanding_guard(_call_id, text, *_args, **_kwargs):
            return text.replace("PASSWORD=x", "PASSWORD=[REDACTED:secret]"), None

        with _send_with_tool_batch(
            session,
            calls,
            [("tc_b1", capture)],
            _remaining_token_budget=MagicMock(return_value=batch_budget_tokens),
            _compact_messages=MagicMock(return_value=False),
            _evaluate_output=MagicMock(side_effect=expanding_guard),
        ):
            session.send("go")

        (text,) = _tool_turn_texts(session)
        cap = session._tool_result_truncation_limit(batch_budget_tokens=batch_budget_tokens)
        assert len(text) <= cap
        match = re.search(r"\[(\d+) chars truncated", text)
        assert match is not None
        assert int(match.group(1)) >= 11_000 - cap

    def test_repeat_warning_on_a_plain_result_never_copies_the_whole_result(self, session):
        """A plain result larger than the executor edges is projected to
        bounded edges before the warning is appended, and the fold still
        renders it with its true size."""
        retention = session._executor_capture_chars()
        big = "x" * (3 * retention)

        warned = session._bounded_result(big) + _REPEAT_WARNING

        assert isinstance(warned, ProjectedText)
        assert len(warned) <= retention + len(_REPEAT_WARNING)
        assert warned.source.original_chars == len(big) + len(_REPEAT_WARNING)
        assert len(warned.source.prefix) == retention
        assert session._bounded_result("short") == "short"
        cap = session._tool_result_truncation_limit(batch_budget_tokens=4000)
        result = session._truncate_output_result(warned, maximum_chars=cap)
        assert result.original_chars == len(big) + len(_REPEAT_WARNING)
        assert "identical repeat" in result.text

    def test_true_size_survives_the_repeat_warning(self, session):
        """The repeat-call warning is appended without discarding the source."""
        buffer = BoundedTextBuffer(300)
        buffer.append("PASSWORD=x\n" * 1000)
        warned = buffer.projected() + _REPEAT_WARNING

        assert isinstance(warned, ProjectedText)
        assert "identical repeat" in warned
        cap = session._tool_result_truncation_limit(batch_budget_tokens=4000)
        result = session._truncate_output_result(warned, maximum_chars=cap)
        assert len(result.text) <= cap
        assert result.original_chars == warned.source.original_chars
        assert result.original_chars > 11_000
        assert "identical repeat" in result.text
        assert f"[{result.omitted_chars} chars truncated" in result.text


_SPAWN_CALL = [
    {
        "id": "tc_spawn",
        "function": {"name": "spawn_workstream", "arguments": '{"name": "child"}'},
    }
]
_SPAWN_RESULT = (
    '{"child_ws_id":"ws-8f3a","name":"child","node_id":"n1","routing_strategy":"least_busy"}'
)


class TestZeroBudgetDrain:
    def test_structural_handle_survives_zero_budget(self, session):
        """The #883 regression: spawn_workstream's ws_id must reach the
        trajectory even at a fully exhausted context budget."""
        with _send_with_tool_batch(
            session,
            _SPAWN_CALL,
            [("tc_spawn", _SPAWN_RESULT)],
            _remaining_token_budget=MagicMock(return_value=0),
            _compact_messages=MagicMock(return_value=False),
        ):
            session.send("go")

        tool_texts = _tool_turn_texts(session)
        assert any("ws-8f3a" in t for t in tool_texts)
        assert not any("dropped" in t for t in tool_texts)

    def test_padded_structural_name_keeps_its_floor(self, session):
        """A whitespace-padded structural tool name must still be floored."""
        padded = [
            {
                "id": "tc_spawn",
                "function": {"name": " spawn_workstream ", "arguments": '{"name": "child"}'},
            }
        ]
        with _send_with_tool_batch(
            session,
            padded,
            [("tc_spawn", _SPAWN_RESULT)],
            _remaining_token_budget=MagicMock(return_value=0),
            _compact_messages=MagicMock(return_value=False),
        ):
            session.send("go")

        tool_texts = _tool_turn_texts(session)
        assert any("ws-8f3a" in t for t in tool_texts)
        assert not any("dropped" in t for t in tool_texts)

    def test_bulky_structural_result_floored_not_dropped(self, session):
        """A tasks list bigger than the floor keeps head+tail at zero budget."""
        from turnstone.core.session import _TRUNCATION_FLOOR_CHARS

        big_tasks = '{"tasks":[' + ",".join(f'{{"id":{i}}}' for i in range(800)) + "]}"
        assert len(big_tasks) > _TRUNCATION_FLOOR_CHARS
        calls = [{"id": "tc_t", "function": {"name": "tasks", "arguments": '{"action":"list"}'}}]
        with _send_with_tool_batch(
            session,
            calls,
            [("tc_t", big_tasks)],
            _remaining_token_budget=MagicMock(return_value=0),
            _compact_messages=MagicMock(return_value=False),
        ):
            session.send("go")

        (text,) = _tool_turn_texts(session)
        assert text.startswith('{"tasks":[')
        assert "chars truncated" in text
        assert "dropped" not in text

    def test_bulky_plain_result_dropped_honestly(self, session):
        """Non-structural bulky output at zero budget gets the drop notice,
        never the old successful-but-trimmed impersonation."""
        with _send_with_tool_batch(
            session,
            [{"id": "tc_f", "function": {"name": "web_fetch", "arguments": "{}"}}],
            [("tc_f", "page " * 2000)],
            _remaining_token_budget=MagicMock(return_value=0),
            _compact_messages=MagicMock(return_value=False),
        ):
            session.send("go")

        (text,) = _tool_turn_texts(session)
        assert "dropped" in text
        assert "context budget exhausted" in text
        assert "page" not in text
        assert "[Output truncated" not in text

    def test_error_result_floored_at_zero_budget(self, session):
        """A bulky error output keeps its lead: a masked failure reads as
        success, which is the dishonesty #883 removes."""
        err = "Error: deploy failed: " + "trace line\n" * 500

        def execute_error_batch(*_args, **_kwargs):
            # Side-map ownership begins inside the claimed generation.  A
            # pre-send flag is predecessor state and is intentionally cleared
            # by the claim before provider call ids may be reused.
            session._tool_error_flags["tc_e"] = True
            return ([("tc_e", err)], None)

        with _send_with_tool_batch(
            session,
            [{"id": "tc_e", "function": {"name": "bash", "arguments": "{}"}}],
            [("tc_e", err)],
            _remaining_token_budget=MagicMock(return_value=0),
            _compact_messages=MagicMock(return_value=False),
            _execute_tools=MagicMock(side_effect=execute_error_batch),
        ):
            session.send("go")

        (text,) = _tool_turn_texts(session)
        assert text.startswith("Error: deploy failed:")
        assert "dropped" not in text

    def test_mid_drain_zeroing_still_floors_structural(self, session):
        """A bulky earlier result exhausting the budget must not zero a
        structural sibling later in the same batch."""
        session.tool_truncation = 100_000
        calls = [
            {"id": "tc_f", "function": {"name": "web_fetch", "arguments": "{}"}},
            {"id": "tc_spawn", "function": {"name": "spawn_workstream", "arguments": "{}"}},
        ]
        results = [("tc_f", "page " * 5000), ("tc_spawn", _SPAWN_RESULT)]
        with _send_with_tool_batch(
            session,
            calls,
            results,
            # 100 tokens: the fetch consumes it all; the spawn arrives at 0.
            _remaining_token_budget=MagicMock(return_value=100),
            _compact_messages=MagicMock(return_value=False),
        ):
            session.send("go")

        spawn_texts = [t for t in _tool_turn_texts(session) if "ws-8f3a" in t]
        assert spawn_texts, "structural handle was zero-dropped mid-drain"

    def test_zero_budget_triggers_midturn_compact(self, session):
        """The band fix: a zero truncation budget below the owed thresholds
        fires one mid-turn compaction (no threshold_pct — none was
        evaluated), then re-reads the budget."""
        compact = MagicMock(return_value=True)
        budget = MagicMock(side_effect=[0, 5000])
        with _send_with_tool_batch(
            session,
            [{"id": "tc_f", "function": {"name": "web_fetch", "arguments": "{}"}}],
            [("tc_f", "page " * 2000)],
            _remaining_token_budget=budget,
            _compact_messages=compact,
            _compaction_policy=MagicMock(return_value=_policy(owed=False)),
        ):
            session.send("go")

        compact.assert_called_once_with(
            auto=True,
            preserve_tail=1,
            my_generation=session._generation,
            where="mid-turn, tool-result budget exhausted",
        )
        assert budget.call_count == 2
        # Budget recovered to 5000 tokens → the fetch is truncated normally,
        # not dropped.
        (text,) = _tool_turn_texts(session)
        assert "dropped" not in text
        assert "page" in text

    def test_zero_budget_compact_skipped_when_owed_already_ran(self, session):
        """One compaction attempt per drain: the owed path already compacted,
        so a still-zero budget goes straight to the floor/drop backstop."""
        compact = MagicMock(return_value=True)
        owed_compact = MagicMock(return_value=True)
        with _send_with_tool_batch(
            session,
            _SPAWN_CALL,
            [("tc_spawn", _SPAWN_RESULT)],
            _remaining_token_budget=MagicMock(return_value=0),
            _compact_messages=compact,
            _compaction_policy=MagicMock(return_value=_policy(owed=True)),
            _do_auto_compact=owed_compact,
        ):
            session.send("go")

        # The owed compaction is what must have suppressed the zero-budget
        # attempt — not merely a latch set without compacting.
        owed_compact.assert_called_once()
        compact.assert_not_called()
        assert any("ws-8f3a" in t for t in _tool_turn_texts(session))

    def test_zero_budget_compact_bail_backstop(self, session):
        """Compaction bails (returns False) → budget stays 0 → the floor and
        the honest drop notice are the backstop, and only one attempt fires."""
        compact = MagicMock(return_value=False)
        calls = [
            {"id": "tc_spawn", "function": {"name": "spawn_workstream", "arguments": "{}"}},
            {"id": "tc_f", "function": {"name": "web_fetch", "arguments": "{}"}},
        ]
        with _send_with_tool_batch(
            session,
            calls,
            [("tc_spawn", _SPAWN_RESULT), ("tc_f", "page " * 2000)],
            _remaining_token_budget=MagicMock(return_value=0),
            _compact_messages=compact,
            _compaction_policy=MagicMock(return_value=_policy(owed=False)),
        ):
            session.send("go")

        assert compact.call_count == 1
        texts = _tool_turn_texts(session)
        assert any("ws-8f3a" in t for t in texts)
        assert any("dropped" in t for t in texts)

    def test_unproductive_zero_budget_compact_fires_once_per_send(self, session):
        """An attempt that cannot clear the zero band must not re-fire on
        every later tool batch of the same send: the send-scoped latch caps
        the unproductive LLM summary call at one, and later batches fall
        through to the floor/drop backstop."""
        compact = MagicMock(return_value=False)  # never clears the band
        batches = [
            (_SPAWN_CALL, [("tc_spawn", _SPAWN_RESULT)]),
            (
                [{"id": "tc_f", "function": {"name": "web_fetch", "arguments": "{}"}}],
                [("tc_f", "page " * 2000)],
            ),
            (
                [{"id": "tc_f2", "function": {"name": "web_fetch", "arguments": "{}"}}],
                [("tc_f2", "page " * 2000)],
            ),
        ]
        with _send_with_tool_batches(
            session,
            batches,
            _remaining_token_budget=MagicMock(return_value=0),
            _compact_messages=compact,
            _compaction_policy=MagicMock(return_value=_policy(owed=False)),
        ):
            session.send("go")

        assert compact.call_count == 1
        # The backstop still held for every batch: handle admitted, bulky
        # results dropped honestly.
        texts = _tool_turn_texts(session)
        assert any("ws-8f3a" in t for t in texts)
        assert sum("dropped" in t for t in texts) == 2

    def test_productive_compact_rearms_after_budget_rezeroes(self, session):
        """A compaction that RECOVERS the budget does not latch: when later
        batches genuinely re-exhaust it there is new content to fold, so one
        fresh attempt is warranted — and an unproductive second attempt then
        latches for the rest of the send."""
        compact = MagicMock(return_value=True)
        # batch 1: read 0 → compact → re-read 5000 (recovered; no latch)
        # batch 2: read 0 → compact → re-read 0 (unproductive; latch)
        # batch 3: read 0 → latched, no third attempt
        budget = MagicMock(side_effect=[0, 5000, 0, 0, 0, 0, 0])
        batches = [
            (
                [{"id": f"tc_f{i}", "function": {"name": "web_fetch", "arguments": "{}"}}],
                [(f"tc_f{i}", "page " * 2000)],
            )
            for i in range(3)
        ]
        with _send_with_tool_batches(
            session,
            batches,
            _remaining_token_budget=budget,
            _compact_messages=compact,
            _compaction_policy=MagicMock(return_value=_policy(owed=False)),
        ):
            session.send("go")

        assert compact.call_count == 2

    def test_small_results_admitted_from_grace_pool(self, session):
        """Small non-structural results (denials, acks) pass verbatim at
        zero budget, funded by the per-batch grace pool."""
        calls = [
            {"id": "tc_d", "function": {"name": "bash", "arguments": "{}"}},
            {"id": "tc_a", "function": {"name": "bash", "arguments": "{}"}},
        ]
        results = [
            ("tc_d", "Denied: operator rejected the command"),
            ("tc_a", "Started background shell shell-4f2e (pid 1234)"),
        ]
        with _send_with_tool_batch(
            session,
            calls,
            results,
            _remaining_token_budget=MagicMock(return_value=0),
            _compact_messages=MagicMock(return_value=False),
        ):
            session.send("go")

        texts = _tool_turn_texts(session)
        assert "Denied: operator rejected the command" in texts
        assert "Started background shell shell-4f2e (pid 1234)" in texts

    def test_grace_pool_bounds_collective_admission(self, session):
        """A wide batch of small results cannot collectively bypass budget
        accounting: once the per-batch pool is spent, further non-structural
        results get the honest drop notice."""
        from turnstone.core.session import _ZERO_BUDGET_VERBATIM_POOL_CHARS

        calls = [
            {"id": f"tc_{i}", "function": {"name": "web_fetch", "arguments": "{}"}}
            for i in range(4)
        ]
        results = [(f"tc_{i}", chr(ord("A") + i) * 1800) for i in range(4)]
        assert 2 * 1800 <= _ZERO_BUDGET_VERBATIM_POOL_CHARS < 3 * 1800
        with _send_with_tool_batch(
            session,
            calls,
            results,
            _remaining_token_budget=MagicMock(return_value=0),
            _compact_messages=MagicMock(return_value=False),
        ):
            session.send("go")

        texts = _tool_turn_texts(session)
        assert "A" * 1800 in texts
        assert "B" * 1800 in texts
        assert sum("dropped" in t for t in texts) == 2

    def test_grace_pool_resets_per_batch(self, session):
        """The grace pool is per-batch: a later tool batch in the same send
        gets a fresh allowance."""
        batches = [
            (
                [
                    {"id": f"tc_{b}_{i}", "function": {"name": "web_fetch", "arguments": "{}"}}
                    for i in range(2)
                ],
                [(f"tc_{b}_{i}", f"{b}{i}" * 900) for i in range(2)],
            )
            for b in range(2)
        ]
        with _send_with_tool_batches(
            session,
            batches,
            _remaining_token_budget=MagicMock(return_value=0),
            _compact_messages=MagicMock(return_value=False),
        ):
            session.send("go")

        texts = _tool_turn_texts(session)
        assert len(texts) == 4
        assert not any("dropped" in t for t in texts)

    def test_marginal_recovery_thrash_capped(self, session):
        """A compaction that keeps landing the budget marginally positive
        (fixed overhead hovering just under the zero line) must not pay an
        LLM summary call on every batch: the attempt counter caps it."""
        compact = MagicMock(return_value=True)
        budget = MagicMock(side_effect=[0, 400, 0, 400, 0, 0, 0, 0, 0])
        batches = [
            (
                [{"id": f"tc_f{i}", "function": {"name": "web_fetch", "arguments": "{}"}}],
                [(f"tc_f{i}", "page " * 2000)],
            )
            for i in range(4)
        ]
        with _send_with_tool_batches(
            session,
            batches,
            _remaining_token_budget=budget,
            _compact_messages=compact,
            _compaction_policy=MagicMock(return_value=_policy(owed=False)),
        ):
            session.send("go")

        assert compact.call_count == 2

    def test_structural_floor_set_matches_coordinator_catalog(self, session):
        """Every name in the floor set must be a registered coordinator
        tool: a typo or a tool rename that drops a member would silently
        remove that handle's zero-budget floor and re-open the #883 wedge
        with a green suite."""
        from turnstone.core.session import _STRUCTURAL_FLOOR_TOOLS
        from turnstone.core.tools import COORDINATOR_TOOLS

        coordinator_names = {t["function"]["name"] for t in COORDINATOR_TOOLS}
        assert coordinator_names >= _STRUCTURAL_FLOOR_TOOLS

    def test_spawn_batch_and_wait_survive_zero_budget(self, session):
        """The two floor-set members without dedicated coverage: batch spawn
        handles and wait resolutions must reach the trajectory at zero
        budget like spawn_workstream's."""
        batch_json = (
            '{"results":{"0":{"child_ws_id":"ws-b1"},"1":{"child_ws_id":"ws-b2"}},"denied":[]}'
        )
        wait_json = '{"complete":true,"elapsed":4.2,"results":{"ws-b1":{"state":"idle"}}}'
        calls = [
            {"id": "tc_b", "function": {"name": "spawn_batch", "arguments": "{}"}},
            {"id": "tc_w", "function": {"name": "wait_for_workstream", "arguments": "{}"}},
        ]
        with _send_with_tool_batch(
            session,
            calls,
            [("tc_b", batch_json), ("tc_w", wait_json)],
            _remaining_token_budget=MagicMock(return_value=0),
            _compact_messages=MagicMock(return_value=False),
        ):
            session.send("go")

        texts = _tool_turn_texts(session)
        assert any("ws-b1" in t and "ws-b2" in t for t in texts)
        assert any('"complete":true' in t for t in texts)
        assert not any("dropped" in t for t in texts)


def test_tool_truncation_caps_never_exceed_what_retained_edges_can_render(session):
    """Every cap is bounded by the settings maximum and by the session cap, and
    the streaming executors retain half the session cap at each edge, so a
    saturated capture renders any cap the fold applies without clamping."""
    from turnstone.core.session import _auto_tool_truncation_chars
    from turnstone.core.settings_registry import TOOL_TRUNCATION_MAX_CHARS
    from turnstone.core.truncation import TextProjectionSource

    for cap in (999, 1000, TOOL_TRUNCATION_MAX_CHARS):
        session.tool_truncation = cap
        assert 2 * session._executor_capture_chars() >= cap
    assert _auto_tool_truncation_chars(1_000_000_000, 4.0) == TOOL_TRUNCATION_MAX_CHARS
    assert _auto_tool_truncation_chars(10_000, 4.0) == 8_000
    retention = 500
    saturated = TextProjectionSource("a" * retention, "b" * retention, 50 * retention)
    rendered = saturated.render(2 * retention)
    assert rendered.limit_chars == 2 * retention
    assert len(rendered.text) == 2 * retention


def test_manual_tool_truncation_is_clamped_to_the_maximum(tmp_db, mock_openai_client):
    from turnstone.core.settings_registry import TOOL_TRUNCATION_MAX_CHARS

    session = ChatSession(
        client=mock_openai_client,
        model="test-model",
        ui=MagicMock(),
        instructions=None,
        temperature=0.5,
        tool_timeout=10,
        context_window=10_000,
        max_tokens=1_000,
        tool_truncation=10 * TOOL_TRUNCATION_MAX_CHARS,
    )
    assert session.tool_truncation == TOOL_TRUNCATION_MAX_CHARS
