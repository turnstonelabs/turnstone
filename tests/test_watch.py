"""Tests for the watch module — duration parsing, condition evaluation, WatchRunner."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests._helpers import wait_until
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
        # The raw shell output rides as its own field so the FE card body shows
        # it alone (no header / command repeat); the wire ``text`` keeps the
        # full prose for the model.
        assert reminder["output"] == '{"state": "MERGED"}'
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
        # Owner-checked removal: a non-owner's teardown must not remove a
        # still-live registration (restore shell vs reopened pane).
        runner.remove_dispatch_fn("ws-1", owner=MagicMock())
        assert runner.get_dispatch_fn("ws-1") is fn
        # The owner (or a blind removal) does remove it.
        runner.remove_dispatch_fn("ws-1", owner=fn)
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


def _watch_row(**over: Any) -> dict[str, Any]:
    """A firing watch row (condition matches ``echo hello``); override
    fields per test."""
    row: dict[str, Any] = {
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
    row.update(over)
    return row


def _slow_restore(runner: WatchRunner, ws_id: str, calls: list[str], lock: threading.Lock) -> Any:
    """Restore_fn stand-in: record the call, register a dispatch fn (as the
    real restore does via ``set_watch_runner``), and sleep briefly so a
    concurrent second caller is guaranteed to be waiting on ``_restore_lock``
    when we return.
    """
    with lock:
        calls.append(ws_id)
    time.sleep(0.05)
    fn = MagicMock()
    runner.set_dispatch_fn(ws_id, fn)
    return fn


class TestWatchRunnerDeliveryRetry:
    """Delivery failure HOLDS the built reminder and re-delivers it on a
    later tick — never re-running the command, so a transient stop_on match
    isn't lost — bounded by ``MAX_DELIVERY_ATTEMPTS``.  Until delivery lands
    the row commits only ``next_poll`` plus the fire's durable poll charge:
    no baseline advance and no deactivation of a fire the model never saw,
    while a restart mid-hold (which re-runs the command) stays bounded by
    ``max_polls``."""

    def _make_runner(self, storage: Any, **kwargs: Any) -> WatchRunner:
        return WatchRunner(
            storage=storage,
            node_id="test-node",
            check_interval=0.1,
            tool_timeout=5,
            **kwargs,
        )

    def test_delivery_failure_holds_reminder_and_defers_row(self):
        storage = MagicMock()
        storage.update_watch.return_value = True
        # No dispatch fn registered, no restore_fn → delivery fails.
        runner = self._make_runner(storage)

        runner._poll_watch(_watch_row())

        # Row commit is the retry cadence + this fire's durable poll charge
        # — the fire stays fully retryable, and a restart mid-hold (which
        # re-runs the command) stays bounded by max_polls.
        storage.update_watch.assert_called_once()
        args, kwargs = storage.update_watch.call_args
        assert args[0] == "abc123"
        assert set(kwargs) == {"next_poll", "poll_count"}
        assert kwargs["poll_count"] == 1  # charged durably at hold time
        assert kwargs["next_poll"]  # advanced, not cleared
        # The reminder is HELD for re-delivery; the row is NOT marked
        # terminal-dispatched (the model never saw it).
        with runner._pending_delivery_lock:
            assert "abc123" in runner._pending_delivery
            assert runner._pending_delivery["abc123"]["attempts"] == 1
        with runner._terminal_dispatched_lock:
            assert "abc123" not in runner._terminal_dispatched

    def test_redelivery_uses_held_reminder_without_rerunning_command(self):
        storage = MagicMock()
        storage.update_watch.return_value = True
        runner = self._make_runner(storage)

        # Poll 1: fails → holds.  Capture the exact held reminder object.
        runner._poll_watch(_watch_row())
        with runner._pending_delivery_lock:
            held = runner._pending_delivery["abc123"]["reminder"]

        # ws restored: register a fn, and make _run_command explode so the
        # test proves re-delivery does NOT re-run the command.
        dispatch_fn = MagicMock()
        runner.set_dispatch_fn("ws-1", dispatch_fn)
        runner._run_command = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("command must not re-run on re-delivery")
        )

        runner._poll_watch(_watch_row())

        # Delivered the SAME held reminder; command untouched; committed
        # terminal with the ORIGINAL fire's poll_count; hold cleared.
        dispatch_fn.assert_called_once()
        assert dispatch_fn.call_args[0][0] is held
        runner._run_command.assert_not_called()
        _a, kwargs = storage.update_watch.call_args
        assert kwargs["active"] is False
        assert kwargs["poll_count"] == 1  # retries consumed no ADDITIONAL budget
        with runner._pending_delivery_lock:
            assert "abc123" not in runner._pending_delivery

    def test_transient_exhaustion_keeps_watch_active(self):
        # A purely transient cause (no fn, no restore → returns False) that
        # outlasts the attempt budget must NOT silently deactivate the watch
        # — it drops the held reminder, charges ONE poll to the max_polls
        # budget, and leaves the watch active to re-fire on its next
        # interval.  The baseline (last_output) stays uncommitted so a
        # delta-style stop_on re-fires on the change the model never saw.
        from turnstone.core.watch import MAX_DELIVERY_ATTEMPTS

        storage = MagicMock()
        storage.update_watch.return_value = True
        runner = self._make_runner(storage)  # always fails, transiently

        # Poll 1 fires + holds (attempts=1); polls 2..MAX bump attempts, the
        # MAX-th hitting the exhaustion ceiling.
        for _ in range(MAX_DELIVERY_ATTEMPTS):
            runner._poll_watch(_watch_row())

        # Hold dropped, but the watch was NEVER deactivated — no active=False
        # commit anywhere; the final commit charges the poll and re-schedules
        # (no last_output → the fire re-detects next cycle).
        with runner._pending_delivery_lock:
            assert "abc123" not in runner._pending_delivery
        deactivations = [
            c for c in storage.update_watch.call_args_list if c.kwargs.get("active") is False
        ]
        assert deactivations == []
        _a, kwargs = storage.update_watch.call_args  # last commit
        assert set(kwargs) == {"poll_count", "next_poll"}
        assert kwargs["poll_count"] == 1  # one poll charged to the budget

    def test_transient_exhaustion_with_budget_spent_deactivates(self):
        # The keep-alive-on-transient behavior is bounded by the watch's own
        # max_polls budget: once poll_count reaches it, exhaustion commits
        # the held (deactivating) update instead of re-running the command
        # every interval forever against an unreachable workstream.
        from turnstone.core.watch import MAX_DELIVERY_ATTEMPTS

        storage = MagicMock()
        storage.update_watch.return_value = True
        runner = self._make_runner(storage)  # always fails, transiently

        for _ in range(MAX_DELIVERY_ATTEMPTS):
            runner._poll_watch(_watch_row(max_polls=1))  # budget spent on fire 1

        with runner._pending_delivery_lock:
            assert "abc123" not in runner._pending_delivery
        _a, kwargs = storage.update_watch.call_args  # last commit
        assert kwargs["active"] is False  # deactivated: budget spent
        assert kwargs["poll_count"] == 1

    def test_held_delivery_retries_on_capped_cadence_not_interval(self):
        # Re-delivery is a cheap in-memory dispatch — a daily watch whose
        # fire hit a busy restore slot must retry within
        # DELIVERY_RETRY_CAP_SECS, not sit on the reminder for 24 h.
        from turnstone.core.watch import DELIVERY_RETRY_CAP_SECS

        storage = MagicMock()
        storage.update_watch.return_value = True
        runner = self._make_runner(storage)

        runner._poll_watch(_watch_row(interval_secs=86_400))

        _a, kwargs = storage.update_watch.call_args
        assert set(kwargs) == {"next_poll", "poll_count"}
        retry_at = datetime.strptime(kwargs["next_poll"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        delta = (retry_at - datetime.now(UTC)).total_seconds()
        assert 0 < delta <= DELIVERY_RETRY_CAP_SECS + 5  # capped, not 86400

    def test_permanent_unrestorable_deactivates_immediately(self):
        # A permanent failure (restore raises WatchWorkstreamUnrestorable,
        # e.g. corrupt persona stamp) deactivates the watch on the FIRST
        # fire — no held reminder, no waiting out the attempt budget.
        from turnstone.core.watch import WatchWorkstreamUnrestorable

        storage = MagicMock()
        storage.update_watch.return_value = True
        restore_fn = MagicMock(side_effect=WatchWorkstreamUnrestorable("ws-1"))
        runner = self._make_runner(storage, restore_fn=restore_fn)

        runner._poll_watch(_watch_row())

        restore_fn.assert_called_once_with("ws-1")  # not retried 5×
        _a, kwargs = storage.update_watch.call_args
        assert kwargs["active"] is False  # deactivated now
        with runner._pending_delivery_lock:
            assert "abc123" not in runner._pending_delivery  # nothing held
        # The admission slot is released even on the raising path.
        with runner._restore_lock:
            assert "ws-1" not in runner._restoring

    def test_pending_cleared_on_already_dispatched_retry(self):
        # Constructs the id-in-both-sets state DIRECTLY: no current path
        # produces it (_redeliver_pending clears the hold before its
        # commit), but the already-dispatched branch deactivates the row —
        # after which it never re-lists — so it is the last line of
        # defense against any such hold leaking forever.  Pin that it
        # clears the hold alongside the retry-deactivate.
        storage = MagicMock()
        storage.update_watch.return_value = True
        runner = self._make_runner(storage)
        with runner._terminal_dispatched_lock:
            runner._terminal_dispatched.add("abc123")
        runner._stash_pending_delivery("abc123", {"text": "x"}, {"active": False}, attempts=1)

        runner._poll_watch(_watch_row())  # hits the already_dispatched branch

        with runner._pending_delivery_lock:
            assert "abc123" not in runner._pending_delivery
        with runner._terminal_dispatched_lock:
            assert "abc123" not in runner._terminal_dispatched

    def test_restore_capacity_full_defers_without_restoring(self):
        # When MAX_CONCURRENT_RESTORES restores are already in flight, a
        # new evicted-ws poll must DEFER (return False, hold) rather than
        # block a poll slot — and must not start a restore.
        from turnstone.core.watch import MAX_CONCURRENT_RESTORES

        storage = MagicMock()
        storage.update_watch.return_value = True
        restore_fn = MagicMock(return_value=MagicMock())
        runner = self._make_runner(storage, restore_fn=restore_fn)
        # Saturate the restore admission with other in-flight ws_ids.
        with runner._restore_lock:
            for i in range(MAX_CONCURRENT_RESTORES):
                runner._restoring[f"other-{i}"] = time.monotonic()

        result = runner._dispatch_result("ws-evicted", {"text": "x"}, "w1")

        assert result is False  # deferred
        restore_fn.assert_not_called()  # no restore admitted

    def test_race_won_admission_delivers_outside_restore_lock(self):
        # A dispatch fn registered between the fast-path miss and the
        # admission check must be delivered WITHOUT holding _restore_lock:
        # the closure can block (ws._lock, wake-thread spawn), and running
        # it under the lock serialises every restore admission on the node
        # behind one delivery.
        storage = MagicMock()
        storage.update_watch.return_value = True
        restore_fn = MagicMock(return_value=None)
        runner = self._make_runner(storage, restore_fn=restore_fn)

        lock_free_during_dispatch: list[bool] = []

        def probe(reminder: dict[str, Any], watch_id: str) -> None:
            ok = runner._restore_lock.acquire(blocking=False)
            lock_free_during_dispatch.append(ok)
            if ok:
                runner._restore_lock.release()

        real_try = runner._try_dispatch_fn
        calls = {"n": 0}

        def fake_try(ws_id: str, reminder: dict[str, Any], watch_id: str) -> bool | None:
            calls["n"] += 1
            if calls["n"] == 1:
                # Simulate a restore completing between the fast path and
                # the admission check: the fn appears "while we waited".
                runner.set_dispatch_fn("ws-1", probe)
                return None
            return real_try(ws_id, reminder, watch_id)

        runner._try_dispatch_fn = fake_try  # type: ignore[method-assign]

        result = runner._dispatch_result("ws-1", {"text": "x"}, "w1")

        assert result is True  # race-won fn delivered
        assert lock_free_during_dispatch == [True]  # ...outside the lock
        restore_fn.assert_not_called()  # no restore admitted for a live fn

    def test_restore_returning_none_holds(self):
        storage = MagicMock()
        storage.update_watch.return_value = True
        restore_fn = MagicMock(return_value=None)  # e.g. all slots active
        runner = self._make_runner(storage, restore_fn=restore_fn)

        runner._poll_watch(_watch_row())

        restore_fn.assert_called_once_with("ws-1")
        _a, kwargs = storage.update_watch.call_args
        assert set(kwargs) == {"next_poll", "poll_count"}
        with runner._pending_delivery_lock:
            assert "abc123" in runner._pending_delivery

    def test_live_fn_raise_holds_without_restoring(self):
        # A registered fn that RAISES means the ws is live; we must NOT fall
        # through to restore (that would spawn a duplicate session on a live
        # conversation).  The reminder is held for re-delivery instead.
        storage = MagicMock()
        storage.update_watch.return_value = True
        restore_fn = MagicMock()
        runner = self._make_runner(storage, restore_fn=restore_fn)
        runner.set_dispatch_fn("ws-1", MagicMock(side_effect=RuntimeError("stale closure")))

        runner._poll_watch(_watch_row())

        restore_fn.assert_not_called()
        with runner._pending_delivery_lock:
            assert "abc123" in runner._pending_delivery
        _a, kwargs = storage.update_watch.call_args
        assert set(kwargs) == {"next_poll", "poll_count"}

    def test_forget_terminal_dispatched_clears_held_reminder(self):
        # User-cancel takes the row out of the due view; its held reminder
        # must be dropped too or it would leak (never re-polled).
        storage = MagicMock()
        storage.update_watch.return_value = True
        runner = self._make_runner(storage)
        runner._poll_watch(_watch_row())
        with runner._pending_delivery_lock:
            assert "abc123" in runner._pending_delivery

        runner.forget_terminal_dispatched("abc123")

        with runner._pending_delivery_lock:
            assert "abc123" not in runner._pending_delivery

    def test_abandon_write_failure_keeps_hold_for_write_retry(self):
        # Reads-succeed/writes-fail storage (e.g. disk-full SQLite): a
        # failed abandon commit must keep the hold so the next tick retries
        # the WRITE via the redeliver path — the clear-first order let the
        # row re-list into a fresh COMMAND RUN every attempt-budget cycle,
        # forever, with the poll budget never advancing.
        from turnstone.core.watch import WatchWorkstreamUnrestorable

        storage = MagicMock()
        storage.update_watch.return_value = True
        runner = self._make_runner(storage)
        runner._poll_watch(_watch_row())  # fire → hold (write still OK here)
        with runner._pending_delivery_lock:
            pending = dict(runner._pending_delivery["abc123"])

        runner._restore_fn = MagicMock(  # type: ignore[assignment]
            side_effect=WatchWorkstreamUnrestorable("ws-1")
        )
        storage.update_watch.side_effect = RuntimeError("disk full")

        with pytest.raises(RuntimeError):
            runner._redeliver_pending(_watch_row(), pending)

        with runner._pending_delivery_lock:
            assert "abc123" in runner._pending_delivery  # hold survived

    def test_unrestorable_abandon_write_failure_keeps_hold(self):
        # Fresh-fire permanent failure whose deactivation write fails must
        # not strand the still-active row into a fresh command run every
        # tick: the stash routes the next tick into the redeliver path,
        # which retries the WRITE — never the command.
        from turnstone.core.watch import WatchWorkstreamUnrestorable

        storage = MagicMock()
        storage.update_watch.side_effect = RuntimeError("disk full")
        restore_fn = MagicMock(side_effect=WatchWorkstreamUnrestorable("ws-1"))
        runner = self._make_runner(storage, restore_fn=restore_fn)

        with pytest.raises(RuntimeError):
            runner._poll_watch(_watch_row())

        with runner._pending_delivery_lock:
            assert "abc123" in runner._pending_delivery  # hold survived

        # Next tick: the write retries and lands; the command never re-runs.
        runner._run_command = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("command must not re-run")
        )
        storage.update_watch.side_effect = None
        storage.update_watch.return_value = True

        runner._poll_watch(_watch_row())

        runner._run_command.assert_not_called()
        _a, kwargs = storage.update_watch.call_args
        assert kwargs["active"] is False  # deactivation landed on the retry
        with runner._pending_delivery_lock:
            assert "abc123" not in runner._pending_delivery

    def test_exhaustion_write_failure_keeps_hold_for_write_retry(self):
        # Same pathology on the transient-exhaustion branch: the charge
        # commit failing must keep the hold (write retried next tick), not
        # drop it into a fresh command cycle with the budget never durable.
        from turnstone.core.watch import MAX_DELIVERY_ATTEMPTS

        storage = MagicMock()
        storage.update_watch.return_value = True
        runner = self._make_runner(storage)  # no fn, no restore → transient
        runner._poll_watch(_watch_row())  # fire → hold
        with runner._pending_delivery_lock:
            runner._pending_delivery["abc123"]["attempts"] = MAX_DELIVERY_ATTEMPTS - 1
            pending = dict(runner._pending_delivery["abc123"])

        storage.update_watch.side_effect = RuntimeError("disk full")

        with pytest.raises(RuntimeError):
            runner._redeliver_pending(_watch_row(), pending)

        with runner._pending_delivery_lock:
            assert "abc123" in runner._pending_delivery  # hold survived


class TestWatchRunnerCancelRace:
    """User-cancel racing the poll pool: the delivery paths re-check the
    row's active state so a cancelled watch can neither deliver nor leak a
    held reminder, and the tick sweep mops up the one interleaving the
    point checks can't reach (a stash landing after the cancel path's
    ``forget_terminal_dispatched`` already cleared)."""

    def _make_runner(self, storage: Any, **kwargs: Any) -> WatchRunner:
        return WatchRunner(
            storage=storage,
            node_id="test-node",
            check_interval=0.1,
            tool_timeout=5,
            **kwargs,
        )

    def test_hold_dropped_when_watch_cancelled_mid_fire(self):
        # Cancel lands while the fire's command is running: the hold path
        # re-checks the row and DROPS instead of stashing — an inactive row
        # never re-lists, so a stash here would leak for the process
        # lifetime with nothing ever retrying or clearing it.
        storage = MagicMock()
        storage.update_watch.return_value = True
        storage.is_watch_active.return_value = False
        runner = self._make_runner(storage)  # no fn, no restore → would hold

        runner._poll_watch(_watch_row())

        with runner._pending_delivery_lock:
            assert "abc123" not in runner._pending_delivery
        # No retry-cadence commit either — the row already left the view.
        storage.update_watch.assert_not_called()

    def test_redelivery_dropped_when_watch_cancelled(self):
        # Cancel lands between the due listing and the redelivery dispatch:
        # deliver nothing (the model must not act on — nor a restore be
        # spawned for — a watch the user just cancelled) and drop the hold.
        storage = MagicMock()
        storage.update_watch.return_value = True
        runner = self._make_runner(storage)
        runner._poll_watch(_watch_row())  # fails → holds (row still active)
        with runner._pending_delivery_lock:
            assert "abc123" in runner._pending_delivery

        storage.is_watch_active.return_value = False  # user cancels
        dispatch_fn = MagicMock()
        runner.set_dispatch_fn("ws-1", dispatch_fn)  # ws even came back live

        runner._poll_watch(_watch_row())

        dispatch_fn.assert_not_called()
        with runner._pending_delivery_lock:
            assert "abc123" not in runner._pending_delivery
        # The only row write remains the initial hold's cadence commit —
        # no terminal commit lands over the cancel's row state.
        assert storage.update_watch.call_count == 1

    def test_tick_sweeps_cancelled_holds(self):
        # The residual interleaving: a stash that landed AFTER the cancel's
        # forget_terminal_dispatched cleared (its active re-check passed
        # just before the cancel's row write).  The sweep drops it within
        # one tick.
        storage = MagicMock()
        storage.update_watch.return_value = True
        storage.list_due_watches.return_value = []
        runner = self._make_runner(storage)
        runner._stash_pending_delivery("abc123", {"text": "x"}, {"active": False}, attempts=1)
        storage.is_watch_active.return_value = False  # row already cancelled

        runner._tick()

        with runner._pending_delivery_lock:
            assert "abc123" not in runner._pending_delivery

    def test_tick_sweep_keeps_active_holds(self):
        storage = MagicMock()
        storage.update_watch.return_value = True
        storage.list_due_watches.return_value = []
        runner = self._make_runner(storage)
        runner._stash_pending_delivery("abc123", {"text": "x"}, {"active": False}, attempts=1)
        storage.is_watch_active.return_value = True

        runner._tick()

        with runner._pending_delivery_lock:
            assert "abc123" in runner._pending_delivery

    def test_active_checks_bias_toward_delivery_on_storage_error(self):
        # is_watch_active RAISING must not drop a fire: the sweep keeps the
        # hold and the delivery paths proceed (bounded by their own attempt
        # and poll budgets) — a storage blip is not a cancellation.
        storage = MagicMock()
        storage.update_watch.return_value = True
        storage.list_due_watches.return_value = []
        storage.is_watch_active.side_effect = RuntimeError("storage down")
        runner = self._make_runner(storage)
        runner._stash_pending_delivery("abc123", {"text": "x"}, {"active": False}, attempts=1)

        runner._tick()  # sweep: biased active → kept

        with runner._pending_delivery_lock:
            assert "abc123" in runner._pending_delivery


class TestWatchRunnerRestoreSerialization:
    """Two watches on ONE evicted workstream, polled concurrently, must
    trigger the restore path at most once — otherwise each spawns a live
    auto-approved session racing writes into one conversation history."""

    def test_concurrent_same_ws_restores_once(self):
        storage = MagicMock()
        storage.update_watch.return_value = True
        restore_calls: list[str] = []
        calls_lock = threading.Lock()

        runner = WatchRunner(
            storage=storage,
            node_id="n",
            check_interval=0.1,
            tool_timeout=5,
            restore_fn=lambda ws_id: _slow_restore(runner, ws_id, restore_calls, calls_lock),
        )

        reminder = {"type": "watch_triggered", "text": "x"}
        barrier = threading.Barrier(2)
        results: list[bool] = []
        results_lock = threading.Lock()

        def call(wid: str) -> None:
            barrier.wait(timeout=2.0)
            ok = runner._dispatch_result("ws-shared", reminder, wid)
            with results_lock:
                results.append(ok)

        threads = [threading.Thread(target=call, args=(f"w{i}",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3.0)

        # Exactly one restore ran; the winner delivered (True) and the other
        # DEFERRED (False, holds + re-delivers next tick) rather than blocking
        # its poll slot on the in-flight restore or restoring a second time.
        assert restore_calls == ["ws-shared"]
        assert sorted(results) == [False, True]
        # Admission slot released after the restore.
        with runner._restore_lock:
            assert "ws-shared" not in runner._restoring

    def test_wedged_restore_admissions_defer_and_alert(self, caplog):
        # Admission entries older than RESTORE_STALL_ALERT_SECS are alerted
        # on but NEVER evicted: the wedged poll thread's pool slot is never
        # released, so reclaiming its admission would just readmit a restore
        # that can wedge another pool thread on the same cause — trading
        # this capped degraded state (restores blocked, polling intact) for
        # total poll-pool collapse.  New restores keep deferring; the error
        # log is the operator's restart signal.
        from turnstone.core.watch import RESTORE_STALL_ALERT_SECS

        storage = MagicMock()
        storage.update_watch.return_value = True
        restore_fn = MagicMock(return_value=MagicMock())
        runner = WatchRunner(
            storage=storage,
            node_id="n",
            check_interval=0.1,
            tool_timeout=5,
            restore_fn=restore_fn,
        )
        stalled_at = time.monotonic() - RESTORE_STALL_ALERT_SECS - 1
        with runner._restore_lock:
            runner._restoring["wedged-1"] = stalled_at
            runner._restoring["wedged-2"] = stalled_at  # both slots wedged

        with caplog.at_level("ERROR"):
            result = runner._dispatch_result("ws-new", {"text": "x"}, "w1")

        assert result is False  # wedged capacity stays consumed → defer
        restore_fn.assert_not_called()
        with runner._restore_lock:
            assert "wedged-1" in runner._restoring
            assert "wedged-2" in runner._restoring
        assert any("watch_runner.restore_admission_wedged" in r.message for r in caplog.records)


class TestWatchRunnerConcurrency:
    """The tick thread only enumerates due rows; polls run on bounded
    daemon threads.  Pins: genuine concurrency, per-watch in-flight
    dedup, saturation leaving rows due (not dropped), and ``stop``
    draining in-flight polls."""

    def _make_runner(self, rows: list[dict[str, Any]], **kwargs: Any) -> WatchRunner:
        storage = MagicMock()
        storage.update_watch.return_value = True
        storage.list_due_watches.return_value = rows
        return WatchRunner(
            storage=storage,
            node_id="test-node",
            check_interval=0.1,
            tool_timeout=5,
            **kwargs,
        )

    @staticmethod
    def _wait_in_flight_empty(runner: WatchRunner, timeout: float = 3.0) -> None:
        def _drained() -> bool:
            with runner._in_flight_lock:
                return not runner._in_flight

        wait_until(_drained, timeout=timeout)

    def test_tick_polls_concurrently(self):
        rows = [_watch_row(watch_id=f"w{i}", ws_id=f"ws-{i}") for i in range(3)]
        runner = self._make_runner(rows)

        all_in = threading.Event()
        release = threading.Event()
        barrier = threading.Barrier(3)

        def fake_poll(_row: dict[str, Any]) -> None:
            # All three poll threads must be inside simultaneously for the
            # barrier to trip — serial execution would deadlock here (and
            # fail via the barrier timeout instead).
            barrier.wait(timeout=2.0)
            all_in.set()
            release.wait(timeout=2.0)

        runner._poll_watch = fake_poll  # type: ignore[method-assign]
        runner._tick()

        assert all_in.wait(timeout=2.0), "polls did not run concurrently"
        release.set()
        self._wait_in_flight_empty(runner)

    def test_tick_skips_in_flight_watch(self):
        rows = [_watch_row(watch_id="w0", ws_id="ws-0")]
        runner = self._make_runner(rows)
        polled: list[str] = []
        runner._poll_watch = lambda row: polled.append(row["watch_id"])  # type: ignore[method-assign]

        # Simulate a slow poll from a previous tick still running.
        with runner._in_flight_lock:
            runner._in_flight.add("w0")

        runner._tick()

        assert polled == []
        # The foreign in-flight entry was not clobbered by the skip.
        with runner._in_flight_lock:
            assert "w0" in runner._in_flight

    def test_tick_saturation_leaves_rows_due(self):
        rows = [_watch_row(watch_id=f"w{i}", ws_id=f"ws-{i}") for i in range(2)]
        runner = self._make_runner(rows, max_concurrent_polls=1)

        started = threading.Event()
        release = threading.Event()
        polled: list[str] = []

        def fake_poll(row: dict[str, Any]) -> None:
            polled.append(row["watch_id"])
            started.set()
            release.wait(timeout=2.0)

        runner._poll_watch = fake_poll  # type: ignore[method-assign]
        runner._tick()
        assert started.wait(timeout=2.0)

        # Only the first row got a slot this tick; the second stays due
        # for the next tick rather than being dropped.
        assert polled == ["w0"]
        release.set()
        self._wait_in_flight_empty(runner)

        # Next tick (slot free again) picks up the remaining row.
        runner._storage.list_due_watches.return_value = [rows[1]]
        runner._tick()
        self._wait_in_flight_empty(runner)
        assert polled == ["w0", "w1"]

    def test_stop_waits_for_in_flight_polls(self):
        rows = [_watch_row(watch_id="w0", ws_id="ws-0")]
        runner = self._make_runner(rows)

        started = threading.Event()
        release = threading.Event()

        def fake_poll(_row: dict[str, Any]) -> None:
            started.set()
            release.wait(timeout=3.0)

        runner._poll_watch = fake_poll  # type: ignore[method-assign]
        runner._tick()
        assert started.wait(timeout=2.0)

        stopper = threading.Thread(target=runner.stop, daemon=True)
        stopper.start()
        # stop() must be draining (poll still pinned), not returned.
        time.sleep(0.15)
        assert stopper.is_alive(), "stop() returned while a poll was in flight"

        release.set()
        stopper.join(timeout=3.0)
        assert not stopper.is_alive()
        with runner._in_flight_lock:
            assert not runner._in_flight
