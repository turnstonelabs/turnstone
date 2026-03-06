"""Tests for scheduled task admin API endpoints."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from turnstone.console.server import (
    admin_create_schedule,
    admin_delete_schedule,
    admin_get_schedule,
    admin_list_schedule_runs,
    admin_list_schedules,
    admin_update_schedule,
)
from turnstone.core.storage._sqlite import SQLiteBackend


@pytest.fixture
def storage(tmp_path):
    """Fresh SQLite backend for each test."""
    return SQLiteBackend(str(tmp_path / "test.db"))


@pytest.fixture
def client(storage):
    """TestClient with storage and auth bypassed."""
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route("/api/admin/schedules", admin_list_schedules),
                    Route("/api/admin/schedules", admin_create_schedule, methods=["POST"]),
                    Route("/api/admin/schedules/{task_id}", admin_get_schedule),
                    Route(
                        "/api/admin/schedules/{task_id}",
                        admin_update_schedule,
                        methods=["PUT"],
                    ),
                    Route(
                        "/api/admin/schedules/{task_id}",
                        admin_delete_schedule,
                        methods=["DELETE"],
                    ),
                    Route(
                        "/api/admin/schedules/{task_id}/runs",
                        admin_list_schedule_runs,
                    ),
                ],
            ),
        ],
    )
    app.state.auth_storage = storage
    return TestClient(app)


def _cron_payload(**overrides):
    """Build default cron schedule creation payload."""
    defaults = {
        "name": "Daily report",
        "description": "Generate the summary",
        "schedule_type": "cron",
        "cron_expr": "0 9 * * *",
        "target_mode": "auto",
        "model": "gpt-5",
        "initial_message": "Generate the daily report",
    }
    defaults.update(overrides)
    return defaults


def _at_payload(**overrides):
    """Build default at-time schedule creation payload."""
    defaults = {
        "name": "One-shot task",
        "description": "Run once",
        "schedule_type": "at",
        "at_time": "2099-01-01T00:00:00+00:00",
        "target_mode": "auto",
        "model": "gpt-5",
        "initial_message": "Do the thing",
    }
    defaults.update(overrides)
    return defaults


class TestScheduleAPI:
    """Tests for the 6 admin schedule endpoints."""

    def test_list_empty(self, client):
        resp = client.get("/v1/api/admin/schedules")
        assert resp.status_code == 200
        data = resp.json()
        assert data["schedules"] == []

    def test_create_cron(self, client):
        resp = client.post("/v1/api/admin/schedules", json=_cron_payload())
        assert resp.status_code == 200
        task = resp.json()
        assert task["name"] == "Daily report"
        assert task["schedule_type"] == "cron"
        assert task["cron_expr"] == "0 9 * * *"
        assert task["enabled"] is True
        assert "task_id" in task
        assert "created" in task
        assert "next_run" in task
        assert task["next_run"] != ""

    def test_create_at(self, client):
        resp = client.post("/v1/api/admin/schedules", json=_at_payload())
        assert resp.status_code == 200
        task = resp.json()
        assert task["schedule_type"] == "at"
        assert task["at_time"] == "2099-01-01T00:00:00+00:00"
        assert task["next_run"] == "2099-01-01T00:00:00+00:00"

    def test_create_missing_name(self, client):
        payload = _cron_payload()
        del payload["name"]
        resp = client.post("/v1/api/admin/schedules", json=payload)
        assert resp.status_code == 400
        assert "name" in resp.json()["error"].lower()

    def test_create_invalid_cron(self, client):
        resp = client.post(
            "/v1/api/admin/schedules",
            json=_cron_payload(cron_expr="not a cron"),
        )
        assert resp.status_code == 400
        assert "cron" in resp.json()["error"].lower()

    def test_create_naive_at_time(self, client):
        """Naive timestamps (no timezone) should be rejected."""
        resp = client.post(
            "/v1/api/admin/schedules",
            json=_at_payload(at_time="2099-01-01T00:00:00"),
        )
        assert resp.status_code == 400
        assert "timezone" in resp.json()["error"].lower()

    def test_create_past_at_time(self, client):
        resp = client.post(
            "/v1/api/admin/schedules",
            json=_at_payload(at_time="2000-01-01T00:00:00+00:00"),
        )
        assert resp.status_code == 400
        assert "future" in resp.json()["error"].lower()

    def test_get_schedule(self, client):
        create_resp = client.post("/v1/api/admin/schedules", json=_cron_payload())
        task_id = create_resp.json()["task_id"]

        resp = client.get(f"/v1/api/admin/schedules/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["task_id"] == task_id
        assert resp.json()["name"] == "Daily report"

    def test_get_nonexistent(self, client):
        resp = client.get("/v1/api/admin/schedules/nonexistent_id")
        assert resp.status_code == 404

    def test_update_schedule(self, client):
        create_resp = client.post("/v1/api/admin/schedules", json=_cron_payload())
        task_id = create_resp.json()["task_id"]

        resp = client.put(
            f"/v1/api/admin/schedules/{task_id}",
            json={"name": "Weekly report"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Weekly report"

        # Verify via GET
        get_resp = client.get(f"/v1/api/admin/schedules/{task_id}")
        assert get_resp.json()["name"] == "Weekly report"

    def test_update_nonexistent(self, client):
        resp = client.put(
            "/v1/api/admin/schedules/nonexistent_id",
            json={"name": "Nope"},
        )
        assert resp.status_code == 404

    def test_delete_schedule(self, client):
        create_resp = client.post("/v1/api/admin/schedules", json=_cron_payload())
        task_id = create_resp.json()["task_id"]

        resp = client.delete(f"/v1/api/admin/schedules/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        # Verify gone
        get_resp = client.get(f"/v1/api/admin/schedules/{task_id}")
        assert get_resp.status_code == 404

    def test_delete_nonexistent(self, client):
        resp = client.delete("/v1/api/admin/schedules/nonexistent_id")
        assert resp.status_code == 404

    def test_list_runs_empty(self, client):
        create_resp = client.post("/v1/api/admin/schedules", json=_cron_payload())
        task_id = create_resp.json()["task_id"]

        resp = client.get(f"/v1/api/admin/schedules/{task_id}/runs")
        assert resp.status_code == 200
        assert resp.json()["runs"] == []

    def test_list_runs_nonexistent(self, client):
        resp = client.get("/v1/api/admin/schedules/nonexistent_id/runs")
        assert resp.status_code == 404

    def test_create_specific_node_target(self, client):
        payload = _cron_payload(target_mode="node-custom-001")
        resp = client.post("/v1/api/admin/schedules", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["target_mode"] == "node-custom-001"

    def test_list_runs_with_data(self, client, storage):
        create_resp = client.post("/v1/api/admin/schedules", json=_cron_payload())
        task_id = create_resp.json()["task_id"]

        # Record runs directly in storage
        storage.record_task_run(
            run_id="run_001",
            task_id=task_id,
            node_id="node-1",
            ws_id="ws_abc",
            correlation_id="corr_001",
            started="2025-06-01T09:00:00",
            status="dispatched",
            error="",
        )
        storage.record_task_run(
            run_id="run_002",
            task_id=task_id,
            node_id="node-2",
            ws_id="",
            correlation_id="corr_002",
            started="2025-06-01T09:01:00",
            status="failed",
            error="No reachable nodes",
        )

        resp = client.get(f"/v1/api/admin/schedules/{task_id}/runs")
        assert resp.status_code == 200
        runs = resp.json()["runs"]
        assert len(runs) == 2
        # Most recent first
        assert runs[0]["run_id"] == "run_002"
        assert runs[0]["status"] == "failed"
        assert runs[1]["run_id"] == "run_001"

    def test_list_runs_invalid_limit(self, client):
        create_resp = client.post("/v1/api/admin/schedules", json=_cron_payload())
        task_id = create_resp.json()["task_id"]

        # Invalid limit should not crash — falls back to 50
        resp = client.get(f"/v1/api/admin/schedules/{task_id}/runs?limit=abc")
        assert resp.status_code == 200
        assert resp.json()["runs"] == []
