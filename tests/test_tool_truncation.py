"""Tests for capacity-aware tool output truncation and context overflow recovery."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from turnstone.core.session import ChatSession

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

    def test_zero_budget_returns_placeholder(self, session):
        big = "x" * 1000
        result = session._truncate_output(big, remaining_budget_tokens=0)
        assert "exceeded context budget" in result
        assert len(result) < 100

    def test_negative_budget_returns_placeholder(self, session):
        big = "x" * 1000
        result = session._truncate_output(big, remaining_budget_tokens=-10)
        assert "exceeded context budget" in result

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
        session._system_tokens = 500
        session._msg_tokens = []
        budget = session._remaining_token_budget()
        # 10000 - 500 - 0 - 1000 - 500 (5%) = 8000
        assert budget == 8000

    def test_partially_full(self, session):
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
        session.messages = [{"role": "user", "content": "hi"}]
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

        compact_mock.assert_called_once_with(auto=True)
        assert call_count == 2

    def test_anthropic_prompt_too_long_triggers_compact(self, session):
        session.messages = [{"role": "user", "content": "hi"}]
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

        compact_mock.assert_called_once_with(auto=True)

    def test_non_context_error_propagates(self, session):
        session.messages = [{"role": "user", "content": "hi"}]
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
        session.messages = [{"role": "user", "content": "hi"}]
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
