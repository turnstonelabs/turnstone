"""Tests for turnstone.console.scheduler — TaskScheduler tick and dispatch."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from turnstone.console.scheduler import TaskScheduler


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
        with patch.object(scheduler._http_client, "post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            mock_post.return_value.raise_for_status = MagicMock()
            scheduler._tick()

        mock_post.assert_called_once()
        url = mock_post.call_args[0][0]
        assert "http://node-001:8080/v1/api/workstreams/new" in url
        storage.record_task_run.assert_called_once()
        run_kwargs = storage.record_task_run.call_args[1]
        assert run_kwargs["node_id"] == "node-001"
        assert run_kwargs["status"] == "dispatched"

    def test_dispatch_pool_mode(self, mocks):
        collector, storage = mocks

        task = _make_task(target_mode="pool")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node("node-001")], 1)
        collector.get_node_detail.return_value = {
            "server_url": "http://node-001:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch.object(scheduler._http_client, "post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            mock_post.return_value.raise_for_status = MagicMock()
            scheduler._tick()

        mock_post.assert_called_once()
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
        with patch.object(scheduler._http_client, "post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            mock_post.return_value.raise_for_status = MagicMock()
            scheduler._tick()

        assert mock_post.call_count == 2
        assert storage.record_task_run.call_count == 2

    def test_dispatch_specific_node(self, mocks):
        collector, storage = mocks

        task = _make_task(target_mode="node-001")
        storage.list_due_tasks.return_value = [task]
        collector.get_node_detail.return_value = {
            "server_url": "http://node-001:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch.object(scheduler._http_client, "post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            mock_post.return_value.raise_for_status = MagicMock()
            scheduler._tick()

        mock_post.assert_called_once()
        url = mock_post.call_args[0][0]
        assert "node-001" in url

    def test_at_task_disables_after_dispatch(self, mocks):
        collector, storage = mocks

        task = _make_task(schedule_type="at", cron_expr="", at_time="2099-01-01T00:00:00")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {
            "server_url": "http://node-001:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch.object(scheduler._http_client, "post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            mock_post.return_value.raise_for_status = MagicMock()
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
        with patch.object(scheduler._http_client, "post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            mock_post.return_value.raise_for_status = MagicMock()
            scheduler._tick()

        update_calls = storage.update_scheduled_task.call_args_list
        assert len(update_calls) == 1
        _, kwargs = update_calls[0]
        assert kwargs["next_run"] != ""
        assert "enabled" not in kwargs  # cron tasks stay enabled

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
        with patch.object(scheduler._http_client, "post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            mock_post.return_value.raise_for_status = MagicMock()
            scheduler._tick()

        assert mock_post.call_count == 3
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
        with patch.object(scheduler._http_client, "post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            mock_post.return_value.raise_for_status = MagicMock()
            scheduler._tick()

        mock_post.assert_called_once()
        url = mock_post.call_args[0][0]
        assert "node-custom-123" in url

    def test_user_id_in_dispatched_body(self, mocks):
        """Dispatched HTTP body should include created_by as user_id."""
        collector, storage = mocks

        task = _make_task(target_mode="auto", created_by="u_scheduler_admin")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {
            "server_url": "http://node-001:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch.object(scheduler._http_client, "post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            mock_post.return_value.raise_for_status = MagicMock()
            scheduler._tick()

        body = mock_post.call_args[1]["json"]
        assert body["user_id"] == "u_scheduler_admin"

    def test_http_failure_records_failure(self, mocks):
        """HTTP errors during dispatch should record a failure."""
        import httpx

        collector, storage = mocks

        task = _make_task(target_mode="auto")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {
            "server_url": "http://node-001:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch.object(scheduler._http_client, "post") as mock_post:
            mock_post.side_effect = httpx.ConnectError("connection refused")
            scheduler._tick()

        storage.record_task_run.assert_called_once()
        run_kwargs = storage.record_task_run.call_args[1]
        assert run_kwargs["status"] == "failed"
