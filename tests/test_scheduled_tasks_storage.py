"""Tests for scheduled_tasks and scheduled_task_runs storage CRUD."""

from __future__ import annotations

import time


def _make_task_kwargs(**overrides):
    """Build default kwargs for create_scheduled_task."""
    defaults = {
        "task_id": "task_001",
        "name": "Daily report",
        "description": "Generate the daily summary",
        "schedule_type": "cron",
        "cron_expr": "0 9 * * *",
        "at_time": "",
        "target_mode": "auto",
        "model": "gpt-5",
        "initial_message": "Generate the daily report",
        "auto_approve": False,
        "auto_approve_tools": [],
        "created_by": "u_admin",
        "next_run": "2099-01-01T09:00:00",
    }
    defaults.update(overrides)
    return defaults


class TestScheduledTaskCRUD:
    """Tests for scheduled_tasks table operations."""

    def test_create_and_get(self, db):
        db.create_scheduled_task(**_make_task_kwargs())
        result = db.get_scheduled_task("task_001")
        assert result is not None
        assert result["task_id"] == "task_001"
        assert result["name"] == "Daily report"
        assert result["description"] == "Generate the daily summary"
        assert result["schedule_type"] == "cron"
        assert result["cron_expr"] == "0 9 * * *"
        assert result["at_time"] == ""
        assert result["target_mode"] == "auto"
        assert result["model"] == "gpt-5"
        assert result["initial_message"] == "Generate the daily report"
        assert result["auto_approve"] == 0
        assert result["auto_approve_tools"] == ""
        assert result["enabled"] == 1
        assert result["created_by"] == "u_admin"
        assert result["next_run"] == "2099-01-01T09:00:00"
        assert "created" in result
        assert "updated" in result

    def test_get_nonexistent(self, db):
        assert db.get_scheduled_task("no_such_task") is None

    def test_create_duplicate_noop(self, db):
        db.create_scheduled_task(**_make_task_kwargs(name="First"))
        db.create_scheduled_task(**_make_task_kwargs(name="Second"))
        result = db.get_scheduled_task("task_001")
        assert result is not None
        assert result["name"] == "First"  # first write wins

    def test_list_tasks(self, db):
        db.create_scheduled_task(**_make_task_kwargs(task_id="task_a", name="Alpha"))
        # Ensure different created timestamps (resolution is 1 second)
        time.sleep(1.1)
        db.create_scheduled_task(**_make_task_kwargs(task_id="task_b", name="Beta"))
        tasks = db.list_scheduled_tasks()
        assert len(tasks) == 2
        # Ordered by created DESC — most recent first
        assert tasks[0]["task_id"] == "task_b"
        assert tasks[1]["task_id"] == "task_a"

    def test_update_task(self, db):
        db.create_scheduled_task(**_make_task_kwargs())
        original = db.get_scheduled_task("task_001")
        assert original is not None
        original_updated = original["updated"]

        time.sleep(0.05)
        result = db.update_scheduled_task("task_001", name="Weekly report")
        assert result is True

        updated = db.get_scheduled_task("task_001")
        assert updated is not None
        assert updated["name"] == "Weekly report"
        assert updated["updated"] >= original_updated

    def test_update_enable_disable(self, db):
        db.create_scheduled_task(**_make_task_kwargs())
        task = db.get_scheduled_task("task_001")
        assert task is not None
        assert task["enabled"] == 1

        db.update_scheduled_task("task_001", enabled=False)
        task = db.get_scheduled_task("task_001")
        assert task is not None
        assert task["enabled"] == 0

        db.update_scheduled_task("task_001", enabled=True)
        task = db.get_scheduled_task("task_001")
        assert task is not None
        assert task["enabled"] == 1

    def test_delete_task(self, db):
        db.create_scheduled_task(**_make_task_kwargs())
        assert db.delete_scheduled_task("task_001") is True
        assert db.get_scheduled_task("task_001") is None
        # Deleting again returns False
        assert db.delete_scheduled_task("task_001") is False

    def test_delete_cascades_runs(self, db):
        db.create_scheduled_task(**_make_task_kwargs())
        db.record_task_run(
            run_id="run_001",
            task_id="task_001",
            node_id="node_1",
            ws_id="ws_abc",
            correlation_id="corr_001",
            started="2025-01-01T09:00:00",
            status="dispatched",
            error="",
        )
        assert len(db.list_task_runs("task_001")) == 1

        db.delete_scheduled_task("task_001")
        assert db.list_task_runs("task_001") == []

    def test_list_due_tasks(self, db):
        db.create_scheduled_task(
            **_make_task_kwargs(task_id="past", next_run="2020-01-01T00:00:00")
        )
        db.create_scheduled_task(
            **_make_task_kwargs(task_id="future", next_run="2099-12-31T23:59:59")
        )
        now = "2025-06-01T12:00:00"
        due = db.list_due_tasks(now)
        assert len(due) == 1
        assert due[0]["task_id"] == "past"

    def test_list_due_tasks_skips_disabled(self, db):
        db.create_scheduled_task(
            **_make_task_kwargs(task_id="disabled_task", next_run="2020-01-01T00:00:00")
        )
        db.update_scheduled_task("disabled_task", enabled=False)
        due = db.list_due_tasks("2025-06-01T12:00:00")
        assert len(due) == 0

    def test_list_due_tasks_empty_next_run(self, db):
        db.create_scheduled_task(**_make_task_kwargs(task_id="empty_next", next_run=""))
        due = db.list_due_tasks("2099-12-31T23:59:59")
        assert len(due) == 0

    def test_at_task_fields(self, db):
        db.create_scheduled_task(
            **_make_task_kwargs(
                task_id="at_task",
                schedule_type="at",
                cron_expr="",
                at_time="2099-06-15T14:00:00",
                next_run="2099-06-15T14:00:00",
            )
        )
        result = db.get_scheduled_task("at_task")
        assert result is not None
        assert result["schedule_type"] == "at"
        assert result["at_time"] == "2099-06-15T14:00:00"


class TestScheduledTaskRuns:
    """Tests for scheduled_task_runs table operations."""

    def test_record_and_list(self, db):
        db.create_scheduled_task(**_make_task_kwargs())
        db.record_task_run(
            run_id="run_a",
            task_id="task_001",
            node_id="node_1",
            ws_id="ws_1",
            correlation_id="corr_a",
            started="2025-01-01T09:00:00",
            status="dispatched",
            error="",
        )
        db.record_task_run(
            run_id="run_b",
            task_id="task_001",
            node_id="node_2",
            ws_id="ws_2",
            correlation_id="corr_b",
            started="2025-01-02T09:00:00",
            status="dispatched",
            error="",
        )
        runs = db.list_task_runs("task_001")
        assert len(runs) == 2
        # Ordered by started DESC — most recent first
        assert runs[0]["run_id"] == "run_b"
        assert runs[1]["run_id"] == "run_a"

    def test_list_runs_respects_limit(self, db):
        db.create_scheduled_task(**_make_task_kwargs())
        for i in range(3):
            db.record_task_run(
                run_id=f"run_{i}",
                task_id="task_001",
                node_id="node_1",
                ws_id="",
                correlation_id=f"corr_{i}",
                started=f"2025-01-0{i + 1}T09:00:00",
                status="dispatched",
                error="",
            )
        runs = db.list_task_runs("task_001", limit=2)
        assert len(runs) == 2

    def test_list_runs_empty(self, db):
        assert db.list_task_runs("no_such_task") == []

    def test_prune_task_runs(self, db):
        db.create_scheduled_task(**_make_task_kwargs())
        # Old run (should be pruned)
        db.record_task_run(
            run_id="old_run",
            task_id="task_001",
            node_id="node_1",
            ws_id="",
            correlation_id="c_old",
            started="2020-01-01T00:00:00",
            status="dispatched",
            error="",
        )
        # Recent run (should survive)
        db.record_task_run(
            run_id="new_run",
            task_id="task_001",
            node_id="node_1",
            ws_id="",
            correlation_id="c_new",
            started="2099-01-01T00:00:00",
            status="dispatched",
            error="",
        )
        pruned = db.prune_task_runs(retention_days=90)
        assert pruned == 1
        runs = db.list_task_runs("task_001")
        assert len(runs) == 1
        assert runs[0]["run_id"] == "new_run"
