"""Tests for turnstone.console.scheduler — TaskScheduler tick and dispatch."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from turnstone.console.scheduler import TaskScheduler


@pytest.fixture
def mocks():
    """Broker, collector, and storage mocks for scheduler tests."""
    broker = MagicMock()
    broker._redis = MagicMock()
    collector = MagicMock()
    storage = MagicMock()
    return broker, collector, storage


def _make_task(**overrides):
    """Build a minimal task dict matching storage row format."""
    defaults = {
        "task_id": "task_001",
        "name": "Test task",
        "description": "",
        "schedule_type": "cron",
        "cron_expr": "0 9 * * *",
        "at_time": "",
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


class TestSchedulerTick:
    """Tests for _tick() lock acquisition and dispatch logic."""

    def test_tick_acquires_lock(self, mocks):
        broker, collector, storage = mocks
        broker._redis.set.return_value = True
        storage.list_due_tasks.return_value = []

        scheduler = TaskScheduler(broker, collector, storage)
        scheduler._tick()

        broker._redis.set.assert_called_once()
        storage.list_due_tasks.assert_called_once()
        # Lock released via Lua eval (conditional delete)
        broker._redis.eval.assert_called_once()

    def test_tick_skips_when_locked(self, mocks):
        broker, collector, storage = mocks
        broker._redis.set.return_value = None  # lock held by another console

        scheduler = TaskScheduler(broker, collector, storage)
        scheduler._tick()

        storage.list_due_tasks.assert_not_called()

    def test_dispatch_auto_mode(self, mocks):
        broker, collector, storage = mocks
        broker._redis.set.return_value = True

        task = _make_task(target_mode="auto")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node()], 1)

        scheduler = TaskScheduler(broker, collector, storage)
        scheduler._tick()

        broker.push_inbound.assert_called_once()
        _, kwargs = broker.push_inbound.call_args
        assert (
            kwargs.get("node_id") == "node-001"
            or broker.push_inbound.call_args[1].get("node_id") == "node-001"
        )
        storage.record_task_run.assert_called_once()
        run_kwargs = storage.record_task_run.call_args[1]
        assert run_kwargs["node_id"] == "node-001"
        assert run_kwargs["status"] == "dispatched"

    def test_dispatch_pool_mode(self, mocks):
        broker, collector, storage = mocks
        broker._redis.set.return_value = True

        task = _make_task(target_mode="pool")
        storage.list_due_tasks.return_value = [task]

        scheduler = TaskScheduler(broker, collector, storage)
        scheduler._tick()

        broker.push_inbound.assert_called_once()
        # Pool dispatch calls push_inbound without node_id kwarg
        args, kwargs = broker.push_inbound.call_args
        assert kwargs.get("node_id") is None or "node_id" not in kwargs
        storage.record_task_run.assert_called_once()
        run_kwargs = storage.record_task_run.call_args[1]
        assert run_kwargs["node_id"] == "pool"

    def test_dispatch_all_mode(self, mocks):
        broker, collector, storage = mocks
        broker._redis.set.return_value = True

        task = _make_task(target_mode="all")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = (
            [_make_node("node-001"), _make_node("node-002")],
            2,
        )

        scheduler = TaskScheduler(broker, collector, storage)
        scheduler._tick()

        assert broker.push_inbound.call_count == 2
        assert storage.record_task_run.call_count == 2

    def test_dispatch_specific_node(self, mocks):
        broker, collector, storage = mocks
        broker._redis.set.return_value = True

        task = _make_task(target_mode="node-001")
        storage.list_due_tasks.return_value = [task]

        scheduler = TaskScheduler(broker, collector, storage)
        scheduler._tick()

        broker.push_inbound.assert_called_once()
        _, kwargs = broker.push_inbound.call_args
        assert kwargs["node_id"] == "node-001"

    def test_at_task_disables_after_dispatch(self, mocks):
        broker, collector, storage = mocks
        broker._redis.set.return_value = True

        task = _make_task(schedule_type="at", cron_expr="", at_time="2099-01-01T00:00:00")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node()], 1)

        scheduler = TaskScheduler(broker, collector, storage)
        scheduler._tick()

        # At-task should be disabled after dispatch
        update_calls = storage.update_scheduled_task.call_args_list
        assert len(update_calls) == 1
        args, kwargs = update_calls[0]
        assert args[0] == "task_001"
        assert kwargs["enabled"] is False
        assert kwargs["next_run"] == ""

    def test_cron_task_updates_next_run(self, mocks):
        broker, collector, storage = mocks
        broker._redis.set.return_value = True

        task = _make_task(schedule_type="cron", cron_expr="0 9 * * *")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node()], 1)

        scheduler = TaskScheduler(broker, collector, storage)
        scheduler._tick()

        update_calls = storage.update_scheduled_task.call_args_list
        assert len(update_calls) == 1
        _, kwargs = update_calls[0]
        assert kwargs["next_run"] != ""
        assert "enabled" not in kwargs  # cron tasks stay enabled

    def test_no_reachable_nodes_records_failure(self, mocks):
        broker, collector, storage = mocks
        broker._redis.set.return_value = True

        task = _make_task(target_mode="auto")
        storage.list_due_tasks.return_value = [task]
        # No reachable nodes
        collector.get_nodes.return_value = (
            [_make_node("node-001", reachable=False)],
            1,
        )

        scheduler = TaskScheduler(broker, collector, storage)
        scheduler._tick()

        broker.push_inbound.assert_not_called()
        storage.record_task_run.assert_called_once()
        run_kwargs = storage.record_task_run.call_args[1]
        assert run_kwargs["status"] == "failed"
        assert run_kwargs["error"] != ""

    def test_failure_does_not_advance_schedule(self, mocks):
        """When dispatch fails, last_run/next_run should not be updated."""
        broker, collector, storage = mocks
        broker._redis.set.return_value = True

        task = _make_task(target_mode="auto")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([], 0)  # no nodes at all

        scheduler = TaskScheduler(broker, collector, storage)
        scheduler._tick()

        # update_scheduled_task should NOT be called (no last_run/next_run advance)
        storage.update_scheduled_task.assert_not_called()

    def test_fan_out_capped(self, mocks):
        """Fan-out 'all' mode should respect max_fan_out limit."""
        broker, collector, storage = mocks
        broker._redis.set.return_value = True

        task = _make_task(target_mode="all")
        storage.list_due_tasks.return_value = [task]
        # 10 reachable nodes but max_fan_out=3
        nodes = [_make_node(f"node-{i:03d}") for i in range(10)]
        collector.get_nodes.return_value = (nodes, 10)

        scheduler = TaskScheduler(broker, collector, storage, max_fan_out=3)
        scheduler._tick()

        assert broker.push_inbound.call_count == 3
        assert storage.record_task_run.call_count == 3

    def test_specific_node_target(self, mocks):
        """Non-enum target_mode is treated as a specific node_id."""
        broker, collector, storage = mocks
        broker._redis.set.return_value = True

        task = _make_task(target_mode="node-custom-123")
        storage.list_due_tasks.return_value = [task]

        scheduler = TaskScheduler(broker, collector, storage)
        scheduler._tick()

        broker.push_inbound.assert_called_once()
        call_kwargs = broker.push_inbound.call_args
        assert call_kwargs[1]["node_id"] == "node-custom-123"

    def test_user_id_in_dispatched_message(self, mocks):
        """Dispatched message should include created_by as user_id."""
        import json

        broker, collector, storage = mocks
        broker._redis.set.return_value = True

        task = _make_task(target_mode="pool", created_by="u_scheduler_admin")
        storage.list_due_tasks.return_value = [task]

        scheduler = TaskScheduler(broker, collector, storage)
        scheduler._tick()

        msg_json = broker.push_inbound.call_args[0][0]
        msg_data = json.loads(msg_json)
        assert msg_data["user_id"] == "u_scheduler_admin"
