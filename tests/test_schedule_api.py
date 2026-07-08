"""Tests for scheduled task admin API endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

from turnstone.console.server import (
    admin_create_schedule,
    admin_delete_schedule,
    admin_get_schedule,
    admin_list_schedule_runs,
    admin_list_schedules,
    admin_preview_schedule,
    admin_update_schedule,
)
from turnstone.core.auth import AuthResult
from turnstone.core.storage._sqlite import SQLiteBackend


class _InjectAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id="test-admin",
            scopes=frozenset({"approve"}),
            token_source="config",
            permissions=frozenset({"admin.schedules"}),
        )
        return await call_next(request)


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
                    Route(
                        "/api/admin/schedules/preview",
                        admin_preview_schedule,
                        methods=["POST"],
                    ),
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
        middleware=[Middleware(_InjectAuthMiddleware)],
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

    @staticmethod
    def _seed_persona(storage, name="researcher", kinds=None):
        storage.create_persona(
            {
                "persona_id": f"id-{name}",
                "name": name,
                "display_name": name.title(),
                "description": "",
                "base_prompt": "You are a test persona.",
                "applies_to_kinds": kinds or ["interactive"],
            }
        )

    def test_create_with_persona_and_project(self, client, storage):
        self._seed_persona(storage)
        # Owned by the authenticated admin (created_by) → attachable.
        storage.create_project("proj_1", "My Project", "test-admin")
        resp = client.post(
            "/v1/api/admin/schedules",
            json=_cron_payload(persona="researcher", project_id="proj_1"),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["persona"] == "researcher"
        assert data["project_id"] == "proj_1"

    def test_create_defaults_persona_project_empty(self, client):
        resp = client.post("/v1/api/admin/schedules", json=_cron_payload())
        assert resp.status_code == 200
        data = resp.json()
        assert data["persona"] == ""
        assert data["project_id"] == ""

    def test_create_unknown_persona_rejected(self, client):
        resp = client.post(
            "/v1/api/admin/schedules",
            json=_cron_payload(persona="ghost"),
        )
        assert resp.status_code == 400
        assert "persona" in resp.json()["error"].lower()

    def test_create_persona_wrong_kind_rejected(self, client, storage):
        # A coordinator-only persona is refused — schedules only ever dispatch
        # interactive workstreams, so the picker/validation are kind-scoped.
        self._seed_persona(storage, name="orchestrator", kinds=["coordinator"])
        resp = client.post(
            "/v1/api/admin/schedules",
            json=_cron_payload(persona="orchestrator"),
        )
        assert resp.status_code == 400

    def test_create_unattachable_project_rejected(self, client, storage):
        # A private project owned by someone else — the admin isn't a member.
        storage.create_project("proj_x", "Theirs", "someone-else", visibility="private")
        resp = client.post(
            "/v1/api/admin/schedules",
            json=_cron_payload(project_id="proj_x"),
        )
        assert resp.status_code == 403

    def test_update_persona_and_project(self, client, storage):
        self._seed_persona(storage, name="scribe")
        storage.create_project("proj_2", "Proj Two", "test-admin")
        task_id = client.post("/v1/api/admin/schedules", json=_cron_payload()).json()["task_id"]
        resp = client.put(
            f"/v1/api/admin/schedules/{task_id}",
            json={"persona": "scribe", "project_id": "proj_2"},
        )
        assert resp.status_code == 200, resp.text
        data = client.get(f"/v1/api/admin/schedules/{task_id}").json()
        assert data["persona"] == "scribe"
        assert data["project_id"] == "proj_2"

    @staticmethod
    def _legacy_task(storage, task_id="legacy"):
        """A schedule from before the created_by fix — created_by is ''."""
        storage.create_scheduled_task(
            task_id=task_id,
            name="Legacy",
            description="",
            schedule_type="cron",
            cron_expr="0 9 * * *",
            at_time="",
            target_mode="auto",
            model="",
            initial_message="go",
            auto_approve=False,
            auto_approve_tools=[],
            created_by="",
            next_run="2099-01-01T09:00:00",
        )

    def test_update_assign_project_heals_empty_created_by(self, client, storage):
        # Assigning a project to an orphaned schedule adopts the editing admin
        # as owner so the attach — and every future dispatch — has an identity.
        self._legacy_task(storage)
        storage.create_project("proj_heal", "Heal", "test-admin")
        resp = client.put(
            "/v1/api/admin/schedules/legacy",
            json={"project_id": "proj_heal"},
        )
        assert resp.status_code == 200, resp.text
        row = storage.get_scheduled_task("legacy")
        assert row["project_id"] == "proj_heal"
        assert row["created_by"] == "test-admin"

    def test_update_denied_project_does_not_heal_created_by(self, client, storage):
        # Healing must not become an attach bypass: a project the editing admin
        # can't reach is still 403, and created_by/project stay untouched.
        self._legacy_task(storage, task_id="legacy2")
        storage.create_project("proj_other", "Other", "someone-else", visibility="private")
        resp = client.put(
            "/v1/api/admin/schedules/legacy2",
            json={"project_id": "proj_other"},
        )
        assert resp.status_code == 403
        row = storage.get_scheduled_task("legacy2")
        assert row["created_by"] == ""
        assert row["project_id"] == ""

    def test_update_project_keeps_existing_owner(self, client, storage):
        # A schedule that already has a real owner is NOT re-owned by an editing
        # admin — created_by is only adopted for the orphaned "" case.
        self._seed_persona(storage, name="researcher")
        storage.create_scheduled_task(
            task_id="owned",
            name="Owned",
            description="",
            schedule_type="cron",
            cron_expr="0 9 * * *",
            at_time="",
            target_mode="auto",
            model="",
            initial_message="go",
            auto_approve=False,
            auto_approve_tools=[],
            created_by="original-owner",
            next_run="2099-01-01T09:00:00",
        )
        # A public project the original owner (and anyone) can attach to.
        storage.create_project("proj_pub", "Pub", "someone-else", visibility="public")
        resp = client.put(
            "/v1/api/admin/schedules/owned",
            json={"project_id": "proj_pub"},
        )
        assert resp.status_code == 200, resp.text
        row = storage.get_scheduled_task("owned")
        assert row["project_id"] == "proj_pub"
        assert row["created_by"] == "original-owner"

    def test_update_unchanged_persona_skips_revalidation(self, client, storage):
        # A persona disabled after creation must not block editing other fields
        # when the shelf resends the unchanged slug (it still fails at dispatch).
        self._seed_persona(storage, name="researcher")
        task_id = client.post(
            "/v1/api/admin/schedules", json=_cron_payload(persona="researcher")
        ).json()["task_id"]
        storage.update_persona("id-researcher", enabled=False)
        resp = client.put(
            f"/v1/api/admin/schedules/{task_id}",
            json={"name": "Renamed", "persona": "researcher"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "Renamed"
        assert resp.json()["persona"] == "researcher"

    def test_update_unchanged_project_skips_regate(self, client, storage):
        # Project attach isn't re-gated when unchanged, so a project deleted (or
        # membership lost) out from under the schedule doesn't block edits.
        storage.create_project("proj_keep", "Keep", "test-admin")
        task_id = client.post(
            "/v1/api/admin/schedules", json=_cron_payload(project_id="proj_keep")
        ).json()["task_id"]
        storage.delete_project("proj_keep")  # a re-gate would now 400
        resp = client.put(
            f"/v1/api/admin/schedules/{task_id}",
            json={"name": "Renamed", "project_id": "proj_keep"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["project_id"] == "proj_keep"

    def test_update_ignores_created_by_in_body(self, client, storage):
        # created_by is never sourced from the request body — a spoofed value
        # in the PUT payload is ignored (only the heal path from auth writes it).
        task_id = client.post("/v1/api/admin/schedules", json=_cron_payload()).json()["task_id"]
        client.put(
            f"/v1/api/admin/schedules/{task_id}",
            json={"name": "X", "created_by": "attacker"},
        )
        row = storage.get_scheduled_task(task_id)
        assert row["created_by"] == "test-admin"

    def test_update_unknown_persona_rejected(self, client):
        task_id = client.post("/v1/api/admin/schedules", json=_cron_payload()).json()["task_id"]
        resp = client.put(
            f"/v1/api/admin/schedules/{task_id}",
            json={"persona": "ghost"},
        )
        assert resp.status_code == 400

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


class TestPreviewSchedule:
    """POST /v1/api/admin/schedules/preview — the editor's NEXT RUNS read-out."""

    def test_valid_cron_returns_three_ascending_runs(self, client):
        resp = client.post(
            "/v1/api/admin/schedules/preview",
            json={"schedule_type": "cron", "cron_expr": "0 6 * * *"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["error"] == ""
        assert len(data["next"]) == 3
        assert data["next"] == sorted(data["next"])
        # All at 06:00 (the daily expression's only firing time), in the
        # uniform offset-bearing shape the 'at' branch also uses
        assert all(t.endswith("T06:00:00+00:00") for t in data["next"])

    def test_invalid_cron_is_a_200_with_the_message(self, client):
        resp = client.post(
            "/v1/api/admin/schedules/preview",
            json={"schedule_type": "cron", "cron_expr": "not a cron"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is False
        assert "Invalid cron expression" in data["error"]
        assert data["next"] == []

    def test_missing_cron_expr(self, client):
        resp = client.post(
            "/v1/api/admin/schedules/preview",
            json={"schedule_type": "cron", "cron_expr": ""},
        )
        data = resp.json()
        assert data["valid"] is False
        assert "cron_expr is required" in data["error"]

    def test_at_future_echoes_the_time(self, client):
        resp = client.post(
            "/v1/api/admin/schedules/preview",
            json={"schedule_type": "at", "at_time": "2030-01-01T12:00:00+00:00"},
        )
        data = resp.json()
        assert data["valid"] is True
        assert data["next"] == ["2030-01-01T12:00:00+00:00"]

    def test_at_in_the_past_is_invalid(self, client):
        resp = client.post(
            "/v1/api/admin/schedules/preview",
            json={"schedule_type": "at", "at_time": "2020-01-01T12:00:00+00:00"},
        )
        data = resp.json()
        assert data["valid"] is False
        assert "future" in data["error"]

    def test_unknown_schedule_type(self, client):
        resp = client.post(
            "/v1/api/admin/schedules/preview",
            json={"schedule_type": "sometimes"},
        )
        data = resp.json()
        assert data["valid"] is False
        assert "schedule_type" in data["error"]

    def test_impossible_calendar_date_cron_is_a_200_not_a_500(self, client):
        """croniter.is_valid passes '0 0 30 2 *' (Feb 30) but get_next raises
        CroniterBadDateError — the preview must answer its 200/valid:false
        contract, not crash."""
        resp = client.post(
            "/v1/api/admin/schedules/preview",
            json={"schedule_type": "cron", "cron_expr": "0 0 30 2 *"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is False
        assert "calendar" in data["error"]
        assert data["next"] == []

    def test_create_with_impossible_date_cron_does_not_500(self, client):
        """_compute_next_run shares the guard: creating such a schedule must
        not crash (next_run computes as empty)."""
        resp = client.post(
            "/v1/api/admin/schedules",
            json=_cron_payload(cron_expr="0 0 31 4 *"),
        )
        assert resp.status_code == 200
        assert resp.json()["next_run"] == ""

    def test_cron_next_runs_carry_a_utc_offset(self, client):
        """next[] must be one shape: the 'at' branch echoes offset-bearing
        ISO, so the cron branch appends the UTC offset too."""
        resp = client.post(
            "/v1/api/admin/schedules/preview",
            json={"schedule_type": "cron", "cron_expr": "0 6 * * *"},
        )
        assert all(t.endswith("+00:00") for t in resp.json()["next"])
