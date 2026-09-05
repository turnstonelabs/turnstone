"""Tests for turnstone.console.scheduler — TaskScheduler tick and dispatch."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, PropertyMock, patch

import httpx
import pytest

from turnstone.console.scheduler import TaskScheduler
from turnstone.sdk._types import TurnstoneAPIError


def _wire_lock_storage(storage: MagicMock, initial: dict[str, str] | None = None) -> None:
    """Configure *storage* mock so upsert/get track scheduler_lock state.

    The scheduler's ``_try_acquire_lock`` now writes then reads back to
    verify ownership.  The mock must reflect what was most recently
    upserted so the read-back succeeds.
    """
    state: dict[str, dict[str, str] | None] = {"scheduler_lock": initial}

    def _get(key: str, **_kw: object) -> dict[str, str] | None:
        return state.get(key)

    def _upsert(key: str, value: str, **_kw: object) -> None:
        state[key] = {"value": value}

    def _delete(key: str, **_kw: object) -> None:
        state.pop(key, None)

    storage.get_system_setting.side_effect = _get
    storage.upsert_system_setting.side_effect = _upsert
    storage.delete_system_setting.side_effect = _delete


@pytest.fixture
def mocks():
    """Collector and storage mocks for scheduler tests."""
    collector = MagicMock()
    storage = MagicMock()
    # Default: no existing lock
    _wire_lock_storage(storage, initial=None)
    return collector, storage


def _make_task(**overrides):
    """Build a minimal task dict matching storage row format."""
    defaults = {
        "task_id": "task_001",
        "name": "Test task",
        "description": "",
        "schedule_type": "cron",
        "cron_expr": "0 9 * * *",
        "at_time": "",
        "timezone": "UTC",
        "target_mode": "auto",
        "model": "gpt-5",
        "initial_message": "Run the tests",
        "auto_approve": 0,
        "auto_approve_tools": "",
        "enabled": 1,
        "created_by": "u_admin",
        "next_run": "2020-01-01T09:00:00",
        "last_run": "",
        "created": "2020-01-01T00:00:00",
        "updated": "2020-01-01T00:00:00",
    }
    defaults.update(overrides)
    return defaults


def _make_node(node_id="node-001", reachable=True, ws_total=2, max_ws=10):
    """Build a minimal node dict matching collector output."""
    return {
        "node_id": node_id,
        "reachable": reachable,
        "ws_total": ws_total,
        "max_ws": max_ws,
    }


def _mock_create_response(ws_id: str = "ws_abc123") -> MagicMock:
    """Build a mock CreateWorkstreamResponse with the given ws_id."""
    resp = MagicMock()
    resp.ws_id = ws_id
    return resp


class TestSchedulerTick:
    """Tests for _tick() lock acquisition and dispatch logic."""

    def test_tick_acquires_lock(self, mocks):
        collector, storage = mocks
        storage.list_due_tasks.return_value = []

        scheduler = TaskScheduler(collector, storage)
        scheduler._tick()

        storage.get_system_setting.assert_called()
        storage.upsert_system_setting.assert_called()
        storage.list_due_tasks.assert_called_once()

    def test_tick_skips_when_locked(self, mocks):
        collector, storage = mocks
        # Another instance holds the lock (recent timestamp)
        from datetime import UTC, datetime

        now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        _wire_lock_storage(
            storage,
            initial={"value": json.dumps({"owner": "other-instance", "acquired": now_str})},
        )

        scheduler = TaskScheduler(collector, storage)
        scheduler._tick()

        storage.list_due_tasks.assert_not_called()

    def test_tick_takes_expired_lock(self, mocks):
        """An expired lock from another instance should be taken over."""
        collector, storage = mocks
        _wire_lock_storage(
            storage,
            initial={
                "value": json.dumps({"owner": "other-instance", "acquired": "2020-01-01T00:00:00"})
            },
        )
        storage.list_due_tasks.return_value = []

        scheduler = TaskScheduler(collector, storage)
        scheduler._tick()

        storage.list_due_tasks.assert_called_once()

    def test_dispatch_auto_mode(self, mocks):
        collector, storage = mocks

        task = _make_task(target_mode="auto")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {
            "server_url": "http://node-001:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ) as mock_create:
            scheduler._tick()

        mock_create.assert_called_once()
        storage.record_task_run.assert_called_once()
        run_kwargs = storage.record_task_run.call_args[1]
        assert run_kwargs["node_id"] == "node-001"
        assert run_kwargs["status"] == "dispatched"
        assert run_kwargs["ws_id"] == "ws_abc123"

    def test_dispatch_passes_persona_and_project(self, mocks):
        """persona + project_id ride to create_workstream; created_by becomes
        the user_id the node gates the project attach against."""
        collector, storage = mocks

        task = _make_task(persona="researcher", project_id="proj_42")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {"server_url": "http://node-001:8080"}

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ) as mock_create:
            scheduler._tick()

        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["persona"] == "researcher"
        assert call_kwargs["project_id"] == "proj_42"
        assert call_kwargs["user_id"] == "u_admin"

    def test_dispatch_defaults_persona_project_empty(self, mocks):
        """A task row without persona/project keys dispatches with empty
        strings — the node then resolves the current kind default / no attach."""
        collector, storage = mocks

        task = _make_task()
        task.pop("persona", None)
        task.pop("project_id", None)
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {"server_url": "http://node-001:8080"}

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ) as mock_create:
            scheduler._tick()

        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["persona"] == ""
        assert call_kwargs["project_id"] == ""

    def test_dispatch_pool_mode(self, mocks):
        collector, storage = mocks

        task = _make_task(target_mode="pool")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node("node-001")], 1)
        collector.get_node_detail.return_value = {
            "server_url": "http://node-001:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ) as mock_create:
            scheduler._tick()

        mock_create.assert_called_once()
        storage.record_task_run.assert_called_once()

    def test_dispatch_all_mode(self, mocks):
        collector, storage = mocks

        task = _make_task(target_mode="all")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = (
            [_make_node("node-001"), _make_node("node-002")],
            2,
        )
        collector.get_node_detail.side_effect = lambda nid: {
            "server_url": f"http://{nid}:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ) as mock_create:
            scheduler._tick()

        assert mock_create.call_count == 2
        assert storage.record_task_run.call_count == 2

    def test_dispatch_specific_node(self, mocks):
        collector, storage = mocks

        task = _make_task(target_mode="node-001")
        storage.list_due_tasks.return_value = [task]
        collector.get_node_detail.return_value = {
            "server_url": "http://node-001:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ) as mock_create:
            scheduler._tick()

        mock_create.assert_called_once()
        run_kwargs = storage.record_task_run.call_args[1]
        assert run_kwargs["node_id"] == "node-001"

    def test_at_task_disables_after_dispatch(self, mocks):
        collector, storage = mocks

        task = _make_task(schedule_type="at", cron_expr="", at_time="2099-01-01T00:00:00")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {
            "server_url": "http://node-001:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ):
            scheduler._tick()

        # At-task should be disabled after dispatch
        update_calls = storage.update_scheduled_task.call_args_list
        assert len(update_calls) == 1
        args, kwargs = update_calls[0]
        assert args[0] == "task_001"
        assert kwargs["enabled"] is False
        assert kwargs["next_run"] == ""

    def test_cron_task_updates_next_run(self, mocks):
        collector, storage = mocks

        task = _make_task(schedule_type="cron", cron_expr="0 9 * * *")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {
            "server_url": "http://node-001:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ):
            scheduler._tick()

        update_calls = storage.update_scheduled_task.call_args_list
        assert len(update_calls) == 1
        _, kwargs = update_calls[0]
        assert kwargs["next_run"] != ""
        assert "enabled" not in kwargs  # cron tasks stay enabled

    @pytest.mark.parametrize(
        ("row", "utc_time"),
        [
            # 06:30 in Asia/Kolkata (+05:30, no daylight saving) is 01:00 UTC.
            ({"timezone": "Asia/Kolkata"}, "T01:00:00"),
            # A row from before the zone was stored evaluates in UTC.
            ({"timezone": None}, "T06:30:00"),
        ],
    )
    def test_cron_next_run_is_evaluated_in_the_task_zone(self, mocks, row, utc_time):
        collector, storage = mocks

        task = _make_task(cron_expr="30 6 * * *", **row)
        if row["timezone"] is None:
            del task["timezone"]
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {
            "server_url": "http://node-001:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ):
            scheduler._tick()

        _, kwargs = storage.update_scheduled_task.call_args_list[0]
        assert kwargs["next_run"].endswith(utc_time)

    @pytest.mark.parametrize(
        ("row", "reason"),
        [
            ({"timezone": "Nowhere/Land"}, "time zone Nowhere/Land could not be resolved"),
            ({"cron_expr": "0 0 30 2 *"}, "never matches a real calendar date"),
            ({"cron_expr": "not a cron"}, "is not a valid cron"),
        ],
    )
    def test_no_next_run_disables_the_task_with_the_reason(self, mocks, row, reason):
        # A zone this host cannot resolve, or an expression with no future
        # date, must not abort the tick: the task still dispatches, is then
        # disabled with the reason in its run history (the shelf shows it
        # stopped, not active with no next run), and the task behind it in
        # the same tick still runs.
        collector, storage = mocks

        bad = _make_task(task_id="bad", **row)
        good = _make_task(task_id="good")
        storage.list_due_tasks.return_value = [bad, good]
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {
            "server_url": "http://node-001:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ) as create:
            scheduler._tick()

        assert create.call_count == 2
        updates = {c.args[0]: c.kwargs for c in storage.update_scheduled_task.call_args_list}
        assert updates["bad"]["next_run"] == ""
        assert updates["bad"]["enabled"] is False
        assert updates["good"]["next_run"] != ""
        by_status: dict[str, list[str]] = {}
        for c in storage.record_task_run.call_args_list:
            by_status.setdefault(c.kwargs["status"], []).append(c.kwargs["task_id"])
        # The firing itself succeeded for both, and only the bad one was
        # disabled — recorded as such, not as a failed dispatch.
        assert by_status == {"dispatched": ["bad", "good"], "disabled": ["bad"]}
        disabled = [
            c.kwargs
            for c in storage.record_task_run.call_args_list
            if c.kwargs["status"] == "disabled"
        ]
        assert reason in disabled[0]["error"]

    def test_disabling_survives_a_failed_history_write(self, mocks):
        # The state change is what stops the re-dispatch, so it must not
        # wait on the run-history insert: with that insert failing, the
        # schedule is still disabled and the tick still completes.
        collector, storage = mocks
        storage.list_due_tasks.return_value = [_make_task(timezone="Nowhere/Land")]

        def record(**kwargs):
            if kwargs["status"] == "disabled":
                raise RuntimeError("db")

        storage.record_task_run.side_effect = record
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {
            "server_url": "http://node-001:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ):
            scheduler._tick()

        _, kwargs = storage.update_scheduled_task.call_args_list[0]
        assert kwargs["next_run"] == "" and kwargs["enabled"] is False
        assert storage.delete_system_setting.called, "the lock is released after the tick"

    def test_a_lost_failure_record_does_not_block_the_advance(self, mocks):
        # Fan-out: one node dispatched, one without a URL (a failed row), and
        # the history insert failing for the failed row — the schedule still
        # advances, or the fan-out would re-create its workstreams each tick.
        collector, storage = mocks
        storage.list_due_tasks.return_value = [_make_task(target_mode="all")]
        collector.get_nodes.return_value = ([_make_node("node-001"), _make_node("node-002")], 2)
        collector.get_node_detail.side_effect = lambda node_id: (
            {"server_url": "http://node-001:8080"} if node_id == "node-001" else {}
        )

        def record(**kwargs):
            if kwargs["status"] == "failed":
                raise RuntimeError("db")

        storage.record_task_run.side_effect = record
        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ) as create:
            scheduler._tick()

        assert create.call_count == 1
        _, kwargs = storage.update_scheduled_task.call_args_list[0]
        assert kwargs["next_run"] != ""

    def test_a_lost_run_record_does_not_redispatch(self, mocks):
        # record_task_run failing after the workstream exists is logged and
        # the schedule still advances; left unadvanced it would re-create
        # the workstream on every tick.
        collector, storage = mocks
        storage.list_due_tasks.return_value = [_make_task()]
        storage.record_task_run.side_effect = RuntimeError("db")
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {
            "server_url": "http://node-001:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ) as create:
            scheduler._tick()

        assert create.call_count == 1
        _, kwargs = storage.update_scheduled_task.call_args_list[0]
        assert kwargs["next_run"] != ""

    def test_one_failing_dispatch_does_not_starve_the_next(self, mocks):
        # An exception out of one row's dispatch is logged and the loop moves
        # on; otherwise that row, its next_run never advanced, would be the
        # earliest due task again every tick and nothing behind it would run.
        collector, storage = mocks
        storage.list_due_tasks.return_value = [
            _make_task(task_id="first"),
            _make_task(task_id="second"),
        ]

        scheduler = TaskScheduler(collector, storage)
        with patch.object(
            scheduler, "_dispatch_task", side_effect=[RuntimeError("boom"), None]
        ) as dispatch:
            scheduler._tick()

        assert [c.args[0]["task_id"] for c in dispatch.call_args_list] == ["first", "second"]
        # The lock is still released after the guarded loop.
        assert storage.delete_system_setting.called

    def test_no_reachable_nodes_records_failure(self, mocks):
        collector, storage = mocks

        task = _make_task(target_mode="auto")
        storage.list_due_tasks.return_value = [task]
        # No reachable nodes
        collector.get_nodes.return_value = (
            [_make_node("node-001", reachable=False)],
            1,
        )

        scheduler = TaskScheduler(collector, storage)
        scheduler._tick()

        storage.record_task_run.assert_called_once()
        run_kwargs = storage.record_task_run.call_args[1]
        assert run_kwargs["status"] == "failed"
        assert run_kwargs["error"] != ""

    def test_failure_does_not_advance_schedule(self, mocks):
        """When dispatch fails, last_run/next_run should not be updated."""
        collector, storage = mocks

        task = _make_task(target_mode="auto")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([], 0)  # no nodes at all

        scheduler = TaskScheduler(collector, storage)
        scheduler._tick()

        # update_scheduled_task should NOT be called (no last_run/next_run advance)
        storage.update_scheduled_task.assert_not_called()

    @pytest.mark.parametrize("target_mode", ["auto", "pool", "node-001", "all"])
    def test_a_firing_no_node_took_is_held_and_retried(self, mocks, target_mode):
        # A node that cannot be reached is a failed firing in every target
        # mode, not only the no-node paths: the schedule keeps its
        # next_run, records the failure, and the next tick tries again.
        collector, storage = mocks
        storage.list_due_tasks.return_value = [_make_task(target_mode=target_mode)]
        collector.get_nodes.return_value = ([_make_node("node-001")], 1)
        collector.get_node_detail.return_value = {"server_url": "http://node-001:8080"}

        scheduler = TaskScheduler(collector, storage, retry_interval=0)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            side_effect=httpx.ConnectError("refused"),
        ) as create:
            scheduler._tick()
            scheduler._tick()

        assert create.call_count == 2
        storage.update_scheduled_task.assert_not_called()
        rows = [
            (c.kwargs["status"], c.kwargs["error"]) for c in storage.record_task_run.call_args_list
        ]
        assert rows == [("failed", "node-001 could not be reached (ConnectError)")] * 2

    def test_a_node_without_a_url_is_a_failed_firing(self, mocks):
        collector, storage = mocks
        storage.list_due_tasks.return_value = [_make_task(target_mode="node-gone")]
        collector.get_node_detail.return_value = {}

        scheduler = TaskScheduler(collector, storage)
        with patch("turnstone.console.scheduler.TurnstoneServer.create_workstream") as create:
            scheduler._tick()

        create.assert_not_called()
        storage.update_scheduled_task.assert_not_called()
        assert storage.record_task_run.call_args.kwargs["error"] == "No URL for node node-gone"

    def test_a_fan_out_records_one_row_per_attempt_naming_each_node(self, mocks):
        # The fan-out reached its nodes, so the history names each failure
        # rather than claiming no node was reachable — in one row per
        # attempt, so a held fan-out does not write a page of rows a tick.
        collector, storage = mocks
        storage.list_due_tasks.return_value = [_make_task(target_mode="all")]
        collector.get_nodes.return_value = ([_make_node("node-001"), _make_node("node-002")], 2)
        collector.get_node_detail.side_effect = lambda nid: {"server_url": f"http://{nid}:8080"}

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            side_effect=TurnstoneAPIError(400, "unknown persona"),
        ):
            scheduler._tick()

        storage.update_scheduled_task.assert_not_called()
        rows = [
            (c.kwargs["status"], c.kwargs["error"]) for c in storage.record_task_run.call_args_list
        ]
        assert rows == [
            (
                "failed",
                "node-001 answered HTTP 400: unknown persona; "
                "node-002 answered HTTP 400: unknown persona",
            )
        ]

    def test_a_cron_firing_is_given_up_after_the_retry_window(self, mocks):
        # The window runs from the first failed attempt, not the due time:
        # a firing first seen late (the console was down) gets its full five
        # minutes.  It is judged after an attempt, so a console back from a
        # long gap still tries the node once; then the schedule moves on
        # from the clock, and nothing ran, so last_run is left alone.
        collector, storage = mocks
        collector.get_nodes.return_value = ([], 0)
        task = _make_task(next_run="2030-01-01T00:00:00")

        scheduler = TaskScheduler(collector, storage, retry_interval=0)
        scheduler._dispatch_task(task, "2030-01-01T00:10:00")
        storage.update_scheduled_task.assert_not_called()
        scheduler._dispatch_task(task, "2030-01-01T00:14:59")
        storage.update_scheduled_task.assert_not_called()
        scheduler._dispatch_task(task, "2030-01-01T00:15:00")

        _, kwargs = storage.update_scheduled_task.call_args
        assert set(kwargs) == {"next_run"} and kwargs["next_run"].endswith("T09:00:00")
        assert storage.record_task_run.call_count == 3
        assert scheduler._holds == {}

    def test_a_console_behind_the_clock_attempts_rather_than_waits(self, mocks):
        # Holds are shared, and a console whose clock is behind the writer's
        # sees a negative wait; it attempts, so pacing cannot wedge it.
        collector, storage = mocks
        collector.get_nodes.return_value = ([], 0)
        task = _make_task(next_run="2030-01-01T00:00:00")

        scheduler = TaskScheduler(collector, storage, retry_interval=60)
        scheduler._dispatch_task(task, "2030-01-01T00:00:00")
        scheduler._dispatch_task(task, "2029-12-31T23:00:00")

        assert storage.record_task_run.call_count == 2

    def test_a_one_shot_given_up_is_disabled_with_the_reason(self, mocks):
        # A one-shot has no next firing to move on to: it is disabled, the
        # run history says why and what to do, and last_run stays empty so
        # the shelf shows it disabled rather than completed.
        collector, storage = mocks
        collector.get_nodes.return_value = ([], 0)
        task = _make_task(
            schedule_type="at", cron_expr="", at_time="", next_run="2030-01-01T00:00:00"
        )

        scheduler = TaskScheduler(collector, storage)
        scheduler._dispatch_task(task, "2030-01-01T00:00:00")
        scheduler._dispatch_task(task, "2030-01-01T00:05:00")

        _, kwargs = storage.update_scheduled_task.call_args
        assert kwargs == {"next_run": "", "enabled": False}
        rows = [
            (c.kwargs["status"], c.kwargs["error"]) for c in storage.record_task_run.call_args_list
        ]
        assert rows == [
            ("failed", "No reachable nodes"),
            ("failed", "No reachable nodes"),
            ("disabled", "the firing was given up; set a new time to run it again"),
        ]

    def test_a_held_firing_is_attempted_at_the_retry_interval(self, mocks):
        # Between attempts the firing is due on every tick and skipped
        # cheaply: one attempt, and one failed row, per interval.
        collector, storage = mocks
        collector.get_nodes.return_value = ([], 0)
        task = _make_task(next_run="2030-01-01T00:00:00")

        scheduler = TaskScheduler(collector, storage, retry_interval=60)
        for now in ("00:00:00", "00:00:15", "00:00:45", "00:01:00", "00:01:15"):
            scheduler._dispatch_task(task, "2030-01-01T" + now)

        assert storage.record_task_run.call_count == 2
        storage.update_scheduled_task.assert_not_called()

    def test_a_hold_is_shared_through_storage(self, mocks):
        # Consoles take turns at the lock, and one may restart mid-hold:
        # the hold lives in storage, so a second console paces against the
        # first's attempt, and a restarted one gives up on the first's
        # window rather than starting its own.
        collector, storage = mocks
        collector.get_nodes.return_value = ([], 0)
        storage.list_due_tasks.return_value = [_make_task()]

        TaskScheduler(collector, storage, retry_interval=3600)._tick()
        assert storage.record_task_run.call_count == 1
        TaskScheduler(collector, storage, retry_interval=3600)._tick()
        assert storage.record_task_run.call_count == 1, "the other console holds off"
        storage.update_scheduled_task.assert_not_called()

        TaskScheduler(collector, storage, retry_window=0, retry_interval=0)._tick()
        assert storage.record_task_run.call_count == 2, "one more attempt, then given up"
        assert set(storage.update_scheduled_task.call_args.kwargs) == {"next_run"}
        assert storage.get_system_setting("scheduler_holds") is None

    def test_holds_are_not_written_by_a_tick_that_lost_its_lock(self, mocks):
        # A tick that outlived the lock TTL must not overwrite the holds a
        # console that took the lock since has been advancing.
        collector, storage = mocks
        collector.get_nodes.return_value = ([], 0)
        storage.list_due_tasks.return_value = [_make_task()]
        scheduler = TaskScheduler(collector, storage)

        def dispatch(task, now):
            TaskScheduler._dispatch_task(scheduler, task, now)
            storage.upsert_system_setting(
                "scheduler_lock", json.dumps({"owner": "other", "acquired": now})
            )

        with patch.object(scheduler, "_dispatch_task", side_effect=dispatch):
            scheduler._tick()

        assert storage.record_task_run.call_count == 1
        assert storage.get_system_setting("scheduler_holds") is None
        assert scheduler._holds, "kept in memory for the next tick"

    def test_an_unreadable_holds_row_keeps_the_holds_in_memory(self, mocks):
        # A row this version cannot read costs the console its shared view,
        # not the bound on its own attempts: pacing continues from memory.
        collector, storage = mocks
        collector.get_nodes.return_value = ([], 0)
        storage.list_due_tasks.return_value = [_make_task()]
        scheduler = TaskScheduler(collector, storage, retry_interval=3600)

        scheduler._tick()
        storage.upsert_system_setting("scheduler_holds", "not json")
        scheduler._tick()

        assert storage.record_task_run.call_count == 1

    def test_a_console_fault_leaves_the_firing_due_and_unrecorded(self, mocks):
        # A token that will not mint is the console's fault, not an outcome
        # of the firing: no row is written and the schedule is left as it
        # was, for the tick's guard to log.
        collector, storage = mocks
        storage.list_due_tasks.return_value = [_make_task()]
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {"server_url": "http://node-001:8080"}
        token_manager = MagicMock()
        type(token_manager).token = PropertyMock(side_effect=RuntimeError("no signing key"))

        scheduler = TaskScheduler(collector, storage, token_manager=token_manager)
        scheduler._tick()

        storage.record_task_run.assert_not_called()
        storage.update_scheduled_task.assert_not_called()

    @pytest.mark.parametrize(
        ("row", "kept"),
        [
            (None, False),
            ({"enabled": 0}, False),
            ({"next_run": "2021-01-01T09:00:00"}, False),
            ({}, True),
        ],
    )
    def test_a_hold_outlives_the_due_page_but_not_its_firing(self, mocks, row, kept):
        # A held task absent from the due page is still held if its row
        # says the same firing is pending (the page is capped); deleted,
        # disabled or re-timed, its hold goes, and it starts afresh when
        # next due.
        collector, storage = mocks
        collector.get_nodes.return_value = ([], 0)
        task = _make_task()
        storage.list_due_tasks.return_value = [task]
        storage.get_scheduled_task.return_value = None if row is None else {**task, **row}

        scheduler = TaskScheduler(collector, storage, retry_interval=3600)
        scheduler._tick()
        storage.list_due_tasks.return_value = []
        scheduler._tick()
        assert (storage.get_system_setting("scheduler_holds") is not None) is kept
        storage.list_due_tasks.return_value = [task]
        scheduler._tick()

        assert storage.record_task_run.call_count == (1 if kept else 2)

    @pytest.mark.parametrize(
        ("failure", "held"),
        [
            (httpx.ConnectError("refused"), True),
            (httpx.ConnectTimeout("no route"), True),
            (httpx.PoolTimeout("busy"), True),
            (httpx.ProxyError("refused"), True),
            (httpx.UnsupportedProtocol("no scheme"), True),
            (TurnstoneAPIError(429, "manager at capacity"), True),
            (TurnstoneAPIError(400, "unknown persona"), True),
            (TurnstoneAPIError(401, "token not yet valid"), True),
            (TurnstoneAPIError(503, "Unknown model alias"), False),
            (httpx.ReadTimeout("slow node"), False),
            (RuntimeError("unexpected"), False),
        ],
    )
    def test_only_a_certain_non_creation_is_retried(self, mocks, failure, held):
        # A connection that never opened, or a node's 4xx, made nothing:
        # worth another attempt.  A lost reply or a 5xx may have made the
        # workstream, so the firing is not retried but recorded as
        # unresolved and the schedule moves on without a last_run.
        collector, storage = mocks
        storage.list_due_tasks.return_value = [_make_task()]
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {"server_url": "http://node-001:8080"}

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream", side_effect=failure
        ):
            scheduler._tick()

        row = storage.record_task_run.call_args.kwargs
        assert row["status"] == "failed"
        assert row["error"].endswith("so the firing was not retried") is not held
        assert storage.update_scheduled_task.called is not held
        if not held:
            assert "last_run" not in storage.update_scheduled_task.call_args.kwargs

    def test_an_unresolved_one_shot_is_disabled_without_a_last_run(self, mocks):
        collector, storage = mocks
        storage.list_due_tasks.return_value = [
            _make_task(schedule_type="at", cron_expr="", at_time="")
        ]
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {"server_url": "http://node-001:8080"}

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            side_effect=httpx.ReadTimeout("slow node"),
        ):
            scheduler._tick()

        assert storage.update_scheduled_task.call_args.kwargs == {"next_run": "", "enabled": False}
        rows = [
            (c.kwargs["status"], c.kwargs["error"]) for c in storage.record_task_run.call_args_list
        ]
        assert rows == [
            (
                "failed",
                "node-001 gave no answer (ReadTimeout); whether the workstream was "
                "created is not known, so the firing was not retried",
            ),
            (
                "disabled",
                "whether the firing ran is not known; check the node before setting a new time",
            ),
        ]

    def test_a_fan_out_with_one_unresolved_node_is_not_retried(self, mocks):
        # One node whose answer was lost makes the firing unresolved even
        # when the others certainly made nothing: a retry could hand that
        # node a second workstream.
        collector, storage = mocks
        storage.list_due_tasks.return_value = [_make_task(target_mode="all")]
        collector.get_nodes.return_value = ([_make_node("node-001"), _make_node("node-002")], 2)
        collector.get_node_detail.side_effect = lambda nid: {"server_url": f"http://{nid}:8080"}

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            side_effect=[httpx.ConnectError("refused"), httpx.ReadTimeout("slow node")],
        ):
            scheduler._tick()

        assert set(storage.update_scheduled_task.call_args.kwargs) == {"next_run"}
        error = storage.record_task_run.call_args.kwargs["error"]
        assert error.startswith(
            "node-001 could not be reached (ConnectError); node-002 gave no answer"
        )
        assert error.endswith("so the firing was not retried")

    def test_fan_out_capped(self, mocks):
        """Fan-out 'all' mode should respect max_fan_out limit."""
        collector, storage = mocks

        task = _make_task(target_mode="all")
        storage.list_due_tasks.return_value = [task]
        # 10 reachable nodes but max_fan_out=3
        nodes = [_make_node(f"node-{i:03d}") for i in range(10)]
        collector.get_nodes.return_value = (nodes, 10)
        collector.get_node_detail.side_effect = lambda nid: {
            "server_url": f"http://{nid}:8080",
        }

        scheduler = TaskScheduler(collector, storage, max_fan_out=3)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ) as mock_create:
            scheduler._tick()

        assert mock_create.call_count == 3
        assert storage.record_task_run.call_count == 3

    def test_specific_node_target(self, mocks):
        """Non-enum target_mode is treated as a specific node_id."""
        collector, storage = mocks

        task = _make_task(target_mode="node-custom-123")
        storage.list_due_tasks.return_value = [task]
        collector.get_node_detail.return_value = {
            "server_url": "http://node-custom-123:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ) as mock_create:
            scheduler._tick()

        mock_create.assert_called_once()
        run_kwargs = storage.record_task_run.call_args[1]
        assert run_kwargs["node_id"] == "node-custom-123"

    def test_user_id_in_dispatched_call(self, mocks):
        """Dispatched SDK call should include created_by as user_id."""
        collector, storage = mocks

        task = _make_task(target_mode="auto", created_by="u_scheduler_admin")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {
            "server_url": "http://node-001:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ) as mock_create:
            scheduler._tick()

        _, kwargs = mock_create.call_args
        assert kwargs["user_id"] == "u_scheduler_admin"

    def test_sdk_failure_records_failure(self, mocks):
        """SDK errors during dispatch should record a failure."""
        collector, storage = mocks

        task = _make_task(target_mode="auto")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {
            "server_url": "http://node-001:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            side_effect=TurnstoneAPIError(502, "Bad Gateway"),
        ):
            scheduler._tick()

        storage.record_task_run.assert_called_once()
        run_kwargs = storage.record_task_run.call_args[1]
        assert run_kwargs["status"] == "failed"
