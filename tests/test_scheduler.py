"""Tests for turnstone.console.scheduler — TaskScheduler tick and dispatch."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

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
