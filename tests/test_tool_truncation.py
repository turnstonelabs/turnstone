"""Tests for capacity-aware tool output truncation and context overflow recovery."""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch

import pytest

from turnstone.core.session import ChatSession
from turnstone.core.trajectory import Role, turns_from_dicts

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
    def test_no_truncation_when_under_limit(self, session):
        result = session._truncate_output("short text")
        assert result == "short text"

    def test_truncates_to_tool_truncation_limit(self, session):
        session.tool_truncation = 100
        big = "x" * 500
        result = session._truncate_output(big)
        assert len(result) <= 200  # head + tail + marker
        assert "chars truncated" in result

    def test_budget_aware_truncation(self, session):
        session.tool_truncation = 100_000
        session._chars_per_token = 4.0
        # Budget of 50 tokens = 200 chars
        big = "x" * 1000
        result = session._truncate_output(big, remaining_budget_tokens=50)
        assert len(result) <= 400  # head + tail + marker
        assert "chars truncated" in result

    def test_budget_takes_precedence_when_smaller(self, session):
        session.tool_truncation = 10_000
        session._chars_per_token = 4.0
        # Budget of 25 tokens = 100 chars, smaller than tool_truncation
        big = "x" * 500
        result = session._truncate_output(big, remaining_budget_tokens=25)
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
        assert len(result) <= _TRUNCATION_FLOOR_CHARS + 200  # + marker

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

    def test_openai_context_length_error_triggers_compact(self, session):
        session.messages = turns_from_dicts([{"role": "user", "content": "hi"}])
        session._msg_tokens = [1]

        call_count = 0

        def mock_create_stream(msgs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("maximum context length exceeded")
            return iter([])

        compact_mock = MagicMock()
        with (
            patch.object(session, "_create_stream_with_retry", side_effect=mock_create_stream),
            patch.object(session, "_compact_messages", compact_mock),
            patch.object(
                session, "_stream_response", return_value={"role": "assistant", "content": "ok"}
            ),
            patch.object(session, "_full_messages", return_value=[]),
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

    def test_anthropic_prompt_too_long_triggers_compact(self, session):
        session.messages = turns_from_dicts([{"role": "user", "content": "hi"}])
        session._msg_tokens = [1]

        call_count = 0

        def mock_create_stream(msgs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("prompt is too long: 250000 tokens > 200000 maximum")
            return iter([])

        compact_mock = MagicMock()
        with (
            patch.object(session, "_create_stream_with_retry", side_effect=mock_create_stream),
            patch.object(session, "_compact_messages", compact_mock),
            patch.object(
                session, "_stream_response", return_value={"role": "assistant", "content": "ok"}
            ),
            patch.object(session, "_full_messages", return_value=[]),
            patch.object(session, "_update_token_table"),
            patch.object(session, "_print_status_line"),
            patch.object(session, "_emit_state"),
            patch("turnstone.core.session.save_message"),
        ):
            session.send("hello")

        compact_mock.assert_called_once_with(auto=True, my_generation=session._generation)

    def test_non_context_error_propagates(self, session):
        session.messages = turns_from_dicts([{"role": "user", "content": "hi"}])
        session._msg_tokens = [1]

        with (
            patch.object(
                session,
                "_create_stream_with_retry",
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
                "_create_stream_with_retry",
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
    iteration each: ``_stream_response`` returns an assistant turn carrying
    each batch's *tool_calls* in order, then a plain reply ends the loop.
    Each *results* is what ``_execute_tools`` hands the drain — the
    truncation/floor/compact path under test runs REAL code between the
    mocked boundaries.  Mirrors ``tests/test_session.py::_send_with_mocks``;
    kept local because these tests patch the budget/compaction seam
    differently per scenario.

    ``_estimated_prompt_tokens`` is pinned LOW so the end-of-turn/owed
    compaction paths stay quiet — every compaction observed by these tests
    is therefore the drain's own zero-budget trigger, keeping exact
    call-count assertions honest.  Title generation is pre-latched off so
    no background utility-completion thread churns against the mock client.
    """
    session._title_generated = True
    responses = [
        {"role": "assistant", "content": "", "tool_calls": tool_calls} for tool_calls, _ in batches
    ] + [{"role": "assistant", "content": "done"}]
    exec_results = [(results, []) for _, results in batches]

    def mock_stream(_msgs):
        return iter([])

    def mock_response(_stream, _gen):
        return responses.pop(0)

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(session, "_create_stream_with_retry", side_effect=mock_stream)
        )
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
        session._tool_error_flags["tc_e"] = True
        with _send_with_tool_batch(
            session,
            [{"id": "tc_e", "function": {"name": "bash", "arguments": "{}"}}],
            [("tc_e", err)],
            _remaining_token_budget=MagicMock(return_value=0),
            _compact_messages=MagicMock(return_value=False),
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
            _compaction_owed=MagicMock(return_value=False),
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
            _compaction_owed=MagicMock(return_value=True),
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
            _compaction_owed=MagicMock(return_value=False),
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
            _compaction_owed=MagicMock(return_value=False),
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
            _compaction_owed=MagicMock(return_value=False),
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
            _compaction_owed=MagicMock(return_value=False),
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
