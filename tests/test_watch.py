"""Tests for the watch module — duration parsing, condition evaluation, WatchRunner."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from turnstone.core.watch import (
    WatchRunner,
    build_watch_reminder,
    evaluate_condition,
    format_interval,
    format_watch_message,
    parse_duration,
    validate_condition,
)

# ---------------------------------------------------------------------------
# parse_duration
# ---------------------------------------------------------------------------


class TestParseDuration:
    def test_seconds(self):
        assert parse_duration("30s") == 30.0

    def test_minutes(self):
        assert parse_duration("5m") == 300.0

    def test_hours(self):
        assert parse_duration("1h") == 3600.0

    def test_compound(self):
        assert parse_duration("2h30m") == 9000.0

    def test_bare_number(self):
        assert parse_duration("90") == 90.0

    def test_bare_float(self):
        assert parse_duration("10.5") == 10.5

    def test_whitespace(self):
        assert parse_duration("  5m  ") == 300.0

    def test_case_insensitive(self):
        assert parse_duration("1H30M") == 5400.0

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            parse_duration("")

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="invalid duration"):
            parse_duration("abc")

    def test_negative_raises(self):
        with pytest.raises(ValueError, match="positive"):
            parse_duration("-5")

    def test_zero_raises(self):
        with pytest.raises(ValueError, match="positive"):
            parse_duration("0")

    def test_zero_duration_raises(self):
        with pytest.raises(ValueError, match="positive"):
            parse_duration("0s")


# ---------------------------------------------------------------------------
# validate_condition
# ---------------------------------------------------------------------------


class TestValidateCondition:
    def test_valid_expression(self):
        assert validate_condition('data["state"] == "MERGED"') is None

    def test_valid_simple(self):
        assert validate_condition('"error" in output') is None

    def test_valid_compound(self):
        assert validate_condition('changed and "ready" in output.lower()') is None

    def test_syntax_error(self):
        result = validate_condition("if True:")
        assert result is not None
        assert "syntax" in result.lower()

    def test_incomplete_expression(self):
        result = validate_condition("==")
        assert result is not None


# ---------------------------------------------------------------------------
# evaluate_condition
# ---------------------------------------------------------------------------


class TestEvaluateCondition:
    def test_none_first_poll_no_fire(self):
        """With stop_on=None, first poll (prev_output=None) should not fire."""
        fired, reason = evaluate_condition(None, "hello", 0, None)
        assert not fired

    def test_none_change_detected(self):
        fired, reason = evaluate_condition(None, "world", 0, "hello")
        assert fired
        assert "changed" in reason

    def test_none_no_change(self):
        fired, reason = evaluate_condition(None, "same", 0, "same")
        assert not fired

    def test_string_match(self):
        fired, reason = evaluate_condition('"error" in output', "has error here", 0, None)
        assert fired

    def test_string_no_match(self):
        fired, reason = evaluate_condition('"error" in output', "all good", 0, None)
        assert not fired

    def test_exit_code(self):
        fired, reason = evaluate_condition("exit_code != 0", "fail", 1, None)
        assert fired

    def test_exit_code_zero(self):
        fired, reason = evaluate_condition("exit_code != 0", "ok", 0, None)
        assert not fired

    def test_json_data(self):
        output = '{"state": "MERGED"}'
        fired, reason = evaluate_condition('data["state"] == "MERGED"', output, 0, None)
        assert fired

    def test_json_data_no_match(self):
        output = '{"state": "OPEN"}'
        fired, reason = evaluate_condition('data["state"] == "MERGED"', output, 0, None)
        assert not fired

    def test_json_data_none_for_non_json(self):
        """Non-JSON output should have data=None."""
        fired, reason = evaluate_condition("data is None", "plain text", 0, None)
        assert fired

    def test_changed_variable(self):
        fired, reason = evaluate_condition("changed", "new", 0, "old")
        assert fired

    def test_changed_false(self):
        fired, reason = evaluate_condition("changed", "same", 0, "same")
        assert not fired

    def test_compound_condition(self):
        fired, reason = evaluate_condition(
            'changed and "ready" in output.lower()',
            "System Ready",
            0,
            "System Starting",
        )
        assert fired

    def test_invalid_expression_no_crash(self):
        fired, reason = evaluate_condition("1/0", "hello", 0, None)
        assert not fired
        assert "error" in reason.lower()

    def test_no_import_builtin(self):
        """__import__ should not be accessible."""
        fired, reason = evaluate_condition("__import__('os')", "hello", 0, None)
        assert not fired
        assert "error" in reason.lower()

    def test_no_open_builtin(self):
        fired, reason = evaluate_condition("open('/etc/passwd')", "hello", 0, None)
        assert not fired
        assert "error" in reason.lower()

    def test_no_exec_builtin(self):
        fired, reason = evaluate_condition("exec('print(1)')", "hello", 0, None)
        assert not fired
        assert "error" in reason.lower()

    def test_no_eval_builtin(self):
        fired, reason = evaluate_condition("eval('1+1')", "hello", 0, None)
        assert not fired
        assert "error" in reason.lower()

    def test_no_compile_builtin(self):
        fired, reason = evaluate_condition("compile('1','','eval')", "hello", 0, None)
        assert not fired
        assert "error" in reason.lower()

    def test_safe_len(self):
        fired, reason = evaluate_condition("len(output) > 0", "hello", 0, None)
        assert fired

    def test_safe_sorted(self):
        fired, reason = evaluate_condition("sorted([3,1,2]) == [1,2,3]", "x", 0, None)
        assert fired

    def test_data_get_method(self):
        output = '{"mergedAt": "2024-01-15"}'
        fired, reason = evaluate_condition('data.get("mergedAt") is not None', output, 0, None)
        assert fired

    def test_prev_output_available(self):
        fired, reason = evaluate_condition(
            "prev_output is not None and output != prev_output",
            "new",
            0,
            "old",
        )
        assert fired


# ---------------------------------------------------------------------------
# format_interval
# ---------------------------------------------------------------------------


class TestFormatInterval:
    def test_seconds(self):
        assert format_interval(30) == "30s"

    def test_exactly_60(self):
        assert format_interval(60) == "1m"

    def test_minutes(self):
        assert format_interval(300) == "5m"

    def test_exactly_3600(self):
        assert format_interval(3600) == "1h"

    def test_hours_and_minutes(self):
        assert format_interval(5400) == "1h30m"

    def test_hours_only(self):
        assert format_interval(7200) == "2h"

    def test_large_value(self):
        assert format_interval(86400) == "24h"


# ---------------------------------------------------------------------------
# format_watch_message
# ---------------------------------------------------------------------------


class TestFormatWatchMessage:
    def test_basic(self):
        msg = format_watch_message(
            name="pr-review",
            command="gh pr view --json state",
            output='{"state": "MERGED"}',
            poll_count=5,
            max_polls=100,
            elapsed_secs=1500,
            stop_on='data["state"] == "MERGED"',
            is_final=True,
            reason='condition met: data["state"] == "MERGED"',
        )
        assert "pr-review" in msg
        assert "poll #5/100" in msg
        assert "25m" in msg
        assert "gh pr view --json state" in msg
        assert "MERGED" in msg
        assert "auto-cancelled" in msg.lower()
        # Model should see the condition it was waiting for
        assert "condition:" in msg.lower()

    def test_non_final(self):
        msg = format_watch_message(
            name="deploy",
            command="curl -s http://localhost/health",
            output="ok",
            poll_count=3,
            max_polls=50,
            elapsed_secs=90,
            stop_on=None,
            is_final=False,
            reason="",
        )
        assert "deploy" in msg
        assert "auto-cancelled" not in msg.lower()
        # Change-detection mode should be indicated
        assert "output change" in msg.lower()

    def test_max_polls_final(self):
        msg = format_watch_message(
            name="test",
            command="echo hello",
            output="hello",
            poll_count=100,
            max_polls=100,
            elapsed_secs=6000,
            stop_on=None,
            is_final=True,
            reason="",
        )
        assert "max polls" in msg.lower()


# ---------------------------------------------------------------------------
# build_watch_reminder
# ---------------------------------------------------------------------------


class TestBuildWatchReminder:
    """The structured-reminder builder lifts ``format_watch_message``'s
    args into a dict the dispatch closure can pass to
    ``WatchRunner._dispatch_result``.  ``text`` matches the formatter's
    output verbatim (so compaction / channel adapters / wire splice
    keep their behaviour), and the optional fields ride alongside for
    the frontend's ``.msg.watch-result`` card.
    """

    def test_emits_text_body_and_fields(self):
        kwargs = dict(
            name="pr-review",
            command="gh pr view --json state",
            output='{"state": "MERGED"}',
            poll_count=5,
            max_polls=100,
            elapsed_secs=1500,
            stop_on='data["state"] == "MERGED"',
            is_final=True,
            reason='condition met: data["state"] == "MERGED"',
        )
        reminder = build_watch_reminder(**kwargs)
        # Round-trip with format_watch_message — text is the same body
        # the wire splice + channel adapters have always seen.
        assert reminder["text"] == format_watch_message(**kwargs)
        # Optional fields ride alongside.
        assert reminder["type"] == "watch_triggered"
        assert reminder["watch_name"] == "pr-review"
        assert reminder["command"] == "gh pr view --json state"
        assert reminder["poll_count"] == 5
        assert reminder["max_polls"] == 100
        assert reminder["is_final"] is True

    def test_non_final_carries_is_final_false(self):
        reminder = build_watch_reminder(
            name="deploy",
            command="curl -s http://localhost/health",
            output="ok",
            poll_count=3,
            max_polls=50,
            elapsed_secs=90,
            stop_on=None,
            is_final=False,
            reason="",
        )
        assert reminder["is_final"] is False
        assert reminder["poll_count"] == 3
        # No "auto-cancelled" body for non-final fires.
        assert "auto-cancelled" not in reminder["text"].lower()


# ---------------------------------------------------------------------------
# WatchRunner
# ---------------------------------------------------------------------------


class TestWatchRunner:
    def _make_runner(self, storage=None, **kwargs):
        if storage is None:
            storage = MagicMock()
            storage.list_due_watches.return_value = []
        return WatchRunner(
            storage=storage,
            node_id="test-node",
            check_interval=0.1,
            tool_timeout=5,
            **kwargs,
        )

    def test_start_stop(self):
        runner = self._make_runner()
        runner.start()
        assert runner._thread is not None
        assert runner._thread.is_alive()
        runner.stop()
        assert runner._thread is None

    def test_tick_calls_list_due(self):
        storage = MagicMock()
        storage.list_due_watches.return_value = []
        runner = self._make_runner(storage=storage)
        runner._tick()
        storage.list_due_watches.assert_called_once()

    def test_poll_watch_runs_command(self):
        storage = MagicMock()
        storage.update_watch.return_value = True
        runner = self._make_runner(storage=storage)
        dispatch_fn = MagicMock()
        runner.set_dispatch_fn("ws-1", dispatch_fn)

        watch_row = {
            "watch_id": "abc123",
            "ws_id": "ws-1",
            "name": "test-watch",
            "command": "echo hello",
            "stop_on": '"hello" in output',
            "max_polls": 100,
            "poll_count": 0,
            "last_output": None,
            "interval_secs": 60,
            "created": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        }
        runner._poll_watch(watch_row)

        # Should update the watch in storage
        storage.update_watch.assert_called_once()
        call_kwargs = storage.update_watch.call_args
        assert call_kwargs[0][0] == "abc123"  # watch_id
        assert call_kwargs[1]["poll_count"] == 1
        # Condition should fire (output contains "hello")
        assert call_kwargs[1]["active"] is False  # deactivated
        # Should dispatch result
        dispatch_fn.assert_called_once()

    def test_poll_watch_no_fire_on_first_change_detection(self):
        storage = MagicMock()
        storage.update_watch.return_value = True
        runner = self._make_runner(storage=storage)
        dispatch_fn = MagicMock()
        runner.set_dispatch_fn("ws-1", dispatch_fn)

        watch_row = {
            "watch_id": "abc123",
            "ws_id": "ws-1",
            "name": "test-watch",
            "command": "echo hello",
            "stop_on": None,  # change detection
            "max_polls": 100,
            "poll_count": 0,
            "last_output": None,  # first poll
            "interval_secs": 60,
            "created": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        }
        runner._poll_watch(watch_row)

        # First poll with change detection should not fire
        dispatch_fn.assert_not_called()
        call_kwargs = storage.update_watch.call_args
        # Watch should remain active
        assert "active" not in call_kwargs[1] or call_kwargs[1].get("active") is not False

    def test_max_polls_deactivates(self):
        storage = MagicMock()
        storage.update_watch.return_value = True
        runner = self._make_runner(storage=storage)
        dispatch_fn = MagicMock()
        runner.set_dispatch_fn("ws-1", dispatch_fn)

        watch_row = {
            "watch_id": "abc123",
            "ws_id": "ws-1",
            "name": "test-watch",
            "command": "echo hello",
            "stop_on": '"never" in output',  # won't fire
            "max_polls": 5,
            "poll_count": 4,  # next is #5 = max
            "last_output": "hello\n",
            "interval_secs": 60,
            "created": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        }
        runner._poll_watch(watch_row)

        call_kwargs = storage.update_watch.call_args
        assert call_kwargs[1]["active"] is False
        assert call_kwargs[1]["poll_count"] == 5
        dispatch_fn.assert_called_once()

    def test_blocked_command_deactivates(self):
        storage = MagicMock()
        storage.update_watch.return_value = True
        runner = self._make_runner(storage=storage)

        watch_row = {
            "watch_id": "abc123",
            "ws_id": "ws-1",
            "name": "test-watch",
            "command": "rm -rf /",
            "stop_on": None,
            "max_polls": 100,
            "poll_count": 0,
            "last_output": None,
            "interval_secs": 60,
            "created": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        }
        runner._poll_watch(watch_row)

        storage.update_watch.assert_called_once()
        call_kwargs = storage.update_watch.call_args
        assert call_kwargs[0][0] == "abc123"
        assert call_kwargs[1]["active"] is False

    def test_dispatch_fn_registry(self):
        runner = self._make_runner()
        fn1 = MagicMock()
        fn2 = MagicMock()

        runner.set_dispatch_fn("ws-1", fn1)
        runner.set_dispatch_fn("ws-2", fn2)

        # ``_dispatch_result`` takes a structured reminder dict, not a
        # bare string.
        reminder1 = {"type": "watch_triggered", "text": "msg1"}
        runner._dispatch_result("ws-1", reminder1, "watch-a")
        fn1.assert_called_once_with(reminder1, "watch-a")
        fn2.assert_not_called()

        runner.remove_dispatch_fn("ws-1")
        # After removal, dispatch should try restore_fn
        reminder2 = {"type": "watch_triggered", "text": "msg2"}
        runner._dispatch_result("ws-1", reminder2, "watch-b")
        fn1.assert_called_once()  # still just the one call

    def test_restore_fn_called_for_evicted(self):
        restored_fn = MagicMock()
        restore_fn = MagicMock(return_value=restored_fn)
        runner = self._make_runner(restore_fn=restore_fn)

        reminder = {"type": "watch_triggered", "text": "hello"}
        runner._dispatch_result("ws-evicted", reminder, "watch-x")
        restore_fn.assert_called_once_with("ws-evicted")
        restored_fn.assert_called_once_with(reminder, "watch-x")

    def test_get_dispatch_fn_returns_registered_fn(self):
        """``get_dispatch_fn`` is the public accessor used by the
        server-side restore path to retrieve the per-ws closure that
        ``set_watch_runner`` constructed during workstream rehydrate.
        """
        runner = self._make_runner()
        fn = MagicMock()
        runner.set_dispatch_fn("ws-1", fn)
        assert runner.get_dispatch_fn("ws-1") is fn
        # Unknown ws → None.
        assert runner.get_dispatch_fn("ws-missing") is None
        # After removal → None.
        runner.remove_dispatch_fn("ws-1")
        assert runner.get_dispatch_fn("ws-1") is None

    def test_run_command_success(self):
        runner = self._make_runner()
        output, code = runner._run_command("echo hello")
        assert "hello" in output
        assert code == 0

    def test_run_command_failure(self):
        runner = self._make_runner()
        output, code = runner._run_command("exit 42")
        assert code == 42

    def test_run_command_timeout(self):
        runner = self._make_runner()
        runner._tool_timeout = 1
        output, code = runner._run_command("sleep 30")
        assert "timed out" in output.lower()
        assert code == -1
