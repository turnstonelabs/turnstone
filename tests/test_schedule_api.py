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

from turnstone.console.schedule_timing import cron_names_a_time, next_cron_runs
from turnstone.console.server import (
    SCHEDULE_MESSAGE_MAX_CHARS,
    SCHEDULE_NAME_MAX_CHARS,
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

    @pytest.mark.parametrize("field", ["model", "enabled", "auto_approve", "timezone", "name"])
    def test_create_rejects_a_null_field(self, client, field):
        # A null has no meaning here: read as a value it became bool(None)
        # or the string "None"; read as "not sent" it would hide a caller's
        # intent.  It is refused naming the field, and nothing is created.
        resp = client.post("/v1/api/admin/schedules", json=_cron_payload(**{field: None}))
        assert resp.status_code == 400
        assert resp.json()["error"] == f"{field} must not be null"
        assert client.get("/v1/api/admin/schedules").json()["schedules"] == []

    def test_create_defaults_the_zone_to_utc(self, client):
        # No zone named = UTC, the meaning every schedule had before the zone
        # was stored, so an older API client keeps its firing times.
        resp = client.post("/v1/api/admin/schedules", json=_cron_payload(cron_expr="30 6 * * *"))
        assert resp.status_code == 200
        task = resp.json()
        assert task["timezone"] == "UTC"
        assert task["next_run"].endswith("T06:30:00")

    def test_create_stores_the_zone_and_computes_next_run_in_it(self, client):
        # 06:30 in Asia/Kolkata (UTC+05:30, no daylight saving) is 01:00 UTC;
        # next_run itself stays UTC — the scheduler's due query compares it.
        resp = client.post(
            "/v1/api/admin/schedules",
            json=_cron_payload(cron_expr="30 6 * * *", timezone="Asia/Kolkata"),
        )
        assert resp.status_code == 200
        task = resp.json()
        assert task["timezone"] == "Asia/Kolkata"
        assert task["cron_expr"] == "30 6 * * *", "the expression is stored as written"
        assert task["next_run"].endswith("T01:00:00")

    def test_create_rejects_an_unknown_zone(self, client):
        resp = client.post(
            "/v1/api/admin/schedules",
            json=_cron_payload(timezone="Mars/Olympus_Mons"),
        )
        assert resp.status_code == 400
        assert "time zone" in resp.json()["error"]

    def test_create_at_validates_the_zone_too(self, client):
        # A one-shot ignores the zone, but the row's invariant is one valid
        # name: a later switch to cron must not inherit a bad zone.
        resp = client.post("/v1/api/admin/schedules", json=_at_payload(timezone="Not/A_Zone"))
        assert resp.status_code == 400
        assert "time zone" in resp.json()["error"]

    def test_create_at(self, client):
        resp = client.post("/v1/api/admin/schedules", json=_at_payload())
        assert resp.status_code == 200
        task = resp.json()
        assert task["schedule_type"] == "at"
        assert task["at_time"] == "2099-01-01T00:00:00+00:00"
        # next_run is naive UTC for both schedule types.
        assert task["next_run"] == "2099-01-01T00:00:00"

    def test_create_at_folds_the_offset_into_next_run(self, client, storage):
        # The due query compares next_run as a string against a naive-UTC
        # clock, so an offset-bearing at_time kept verbatim would fire hours
        # late (a "+05:30" sorts as if it were UTC).  at_time stays as sent.
        resp = client.post(
            "/v1/api/admin/schedules", json=_at_payload(at_time="2099-01-01T00:00:00+05:30")
        )
        assert resp.status_code == 200
        task = resp.json()
        assert task["at_time"] == "2099-01-01T00:00:00+05:30"
        assert task["next_run"] == "2098-12-31T18:30:00"
        due_ids = [t["task_id"] for t in storage.list_due_tasks("2098-12-31T18:30:00")]
        assert task["task_id"] in due_ids
        assert storage.list_due_tasks("2098-12-31T18:29:59") == []

    def test_reenabling_an_at_task_folds_the_offset_too(self, client):
        created = client.post(
            "/v1/api/admin/schedules",
            json=_at_payload(at_time="2099-01-01T00:00:00-05:00", enabled=False),
        ).json()
        assert created["next_run"] == ""
        resp = client.put("/v1/api/admin/schedules/" + created["task_id"], json={"enabled": True})
        assert resp.status_code == 200, resp.text
        assert resp.json()["next_run"] == "2099-01-01T05:00:00"

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
        assert storage.list_scheduled_tasks() == []

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

    def test_update_keeps_a_schedule_with_an_unresolvable_zone_editable(self, client, storage):
        # The shelf resends the stored zone, so a zone this host can no
        # longer resolve must not block edits to other fields; re-enabling or
        # changing the timing is refused loudly rather than re-enabled with
        # no next run, and naming a zone the host resolves repairs it.
        storage.create_scheduled_task(
            task_id="lost",
            name="Lost",
            description="",
            schedule_type="cron",
            cron_expr="0 9 * * *",
            at_time="",
            target_mode="auto",
            model="",
            initial_message="go",
            auto_approve=False,
            auto_approve_tools=[],
            created_by="test-admin",
            next_run="",
            timezone="Nowhere/Land",
        )
        storage.update_scheduled_task("lost", enabled=False)
        # The shelf resends every timing field on any edit.
        resp = client.put(
            "/v1/api/admin/schedules/lost",
            json={
                "schedule_type": "cron",
                "cron_expr": "0 9 * * *",
                "at_time": "",
                "timezone": "Nowhere/Land",
                "description": "still editable",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] == "still editable"
        assert resp.json()["enabled"] is False
        for body in ({"enabled": True}, {"cron_expr": "0 7 * * *"}):
            resp = client.put("/v1/api/admin/schedules/lost", json=body)
            assert resp.status_code == 400, body
            assert "time zone" in resp.json()["error"]
        resp = client.put(
            "/v1/api/admin/schedules/lost",
            json={"timezone": "Asia/Kolkata", "enabled": True},
        )
        assert resp.status_code == 200, resp.text
        task = resp.json()
        assert task["enabled"] is True
        assert task["next_run"].endswith("T03:30:00"), "09:00 in Kolkata is 03:30 UTC"

    def test_reenabling_a_cron_that_can_never_fire_is_refused(self, client, storage):
        # A row from before the create-time refusal: re-enabling it would
        # show it enabled with no next run, so it is refused with the reason.
        storage.create_scheduled_task(
            task_id="feb30",
            name="Never",
            description="",
            schedule_type="cron",
            cron_expr="0 0 30 2 *",
            at_time="",
            target_mode="auto",
            model="",
            initial_message="go",
            auto_approve=False,
            auto_approve_tools=[],
            created_by="test-admin",
            next_run="",
        )
        storage.update_scheduled_task("feb30", enabled=False)
        resp = client.put("/v1/api/admin/schedules/feb30", json={"enabled": True})
        assert resp.status_code == 400
        assert "calendar" in resp.json()["error"]
        assert client.get("/v1/api/admin/schedules/feb30").json()["enabled"] is False

    @staticmethod
    def _stored(storage, task_id, **fields):
        row = {
            "task_id": task_id,
            "name": task_id,
            "description": "",
            "schedule_type": "cron",
            "cron_expr": "0 9 * * *",
            "at_time": "",
            "target_mode": "auto",
            "model": "",
            "initial_message": "go",
            "auto_approve": False,
            "auto_approve_tools": [],
            "created_by": "test-admin",
            "next_run": "2099-01-01T09:00:00",
        }
        row.update(fields)
        storage.create_scheduled_task(**row)

    def test_update_resent_enabled_flag_is_not_a_toggle(self, client, storage):
        # The shelf resends enabled on every edit.  An ENABLED schedule whose
        # zone this host can no longer resolve must still take a description
        # edit carrying enabled: true; disabling it needs no validation; a
        # real re-enable is the transition that re-validates, and is refused.
        self._stored(storage, "live", timezone="Nowhere/Land", next_run="")
        resp = client.put(
            "/v1/api/admin/schedules/live",
            json={"enabled": True, "description": "edited while enabled"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] == "edited while enabled"
        assert (
            client.put("/v1/api/admin/schedules/live", json={"enabled": False}).status_code == 200
        )
        resp = client.put("/v1/api/admin/schedules/live", json={"enabled": True})
        assert resp.status_code == 400
        assert "time zone" in resp.json()["error"]

    def test_update_renaming_a_missed_one_shot_is_allowed(self, client, storage):
        # A one-shot whose time passed without dispatching (no reachable
        # node, say) is still enabled; renaming it from the shelf resends its
        # timing and enabled flag unchanged, which is not a re-enable.
        self._stored(
            storage,
            "missed",
            schedule_type="at",
            cron_expr="",
            at_time="2020-01-01T12:00:00+00:00",
            next_run="2020-01-01T12:00:00",
        )
        resp = client.put(
            "/v1/api/admin/schedules/missed",
            json={
                "name": "renamed",
                "schedule_type": "at",
                "cron_expr": "",
                "at_time": "2020-01-01T12:00:00+00:00",
                "enabled": True,
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "renamed"
        assert resp.json()["next_run"] == "2020-01-01T12:00:00", "still pending, not advanced"

    def test_update_resent_at_time_is_compared_as_an_instant(self, client, storage):
        # The shelf re-emits at_time as "+00:00" at minute precision; a
        # one-shot stored with another spelling of the same instant is not
        # changed by that, and keeps its spelling.
        self._stored(
            storage,
            "offset",
            schedule_type="at",
            cron_expr="",
            at_time="2020-01-01T12:00:00+05:30",
            next_run="2020-01-01T06:30:00",
        )
        resp = client.put(
            "/v1/api/admin/schedules/offset",
            json={
                "name": "renamed",
                "schedule_type": "at",
                "cron_expr": "",
                "at_time": "2020-01-01T06:30:00+00:00",
                "enabled": True,
            },
        )
        assert resp.status_code == 200, resp.text
        task = resp.json()
        assert task["at_time"] == "2020-01-01T12:00:00+05:30", "kept as submitted"
        assert task["next_run"] == "2020-01-01T06:30:00"

    def test_update_name_only_leaves_an_overdue_next_run_pending(self, client, storage):
        # An overdue next_run is a firing the scheduler still owes; a name
        # edit (with the shelf's resent flag) must not advance past it.
        self._stored(storage, "overdue", cron_expr="0 2 * * *", next_run="2020-01-01T02:00:00")
        resp = client.put(
            "/v1/api/admin/schedules/overdue", json={"name": "renamed", "enabled": True}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["next_run"] == "2020-01-01T02:00:00"

    def test_update_zone_recomputes_next_run(self, client):
        created = client.post(
            "/v1/api/admin/schedules", json=_cron_payload(cron_expr="30 6 * * *")
        ).json()
        assert created["next_run"].endswith("T06:30:00")
        resp = client.put(
            "/v1/api/admin/schedules/" + created["task_id"],
            json={"timezone": "Asia/Kolkata"},
        )
        assert resp.status_code == 200, resp.text
        task = resp.json()
        assert task["timezone"] == "Asia/Kolkata"
        assert task["next_run"].endswith("T01:00:00"), "the same wall-clock time, in the zone"

    def test_update_rejects_an_unknown_zone_and_keeps_the_row(self, client):
        created = client.post("/v1/api/admin/schedules", json=_cron_payload()).json()
        resp = client.put(
            "/v1/api/admin/schedules/" + created["task_id"],
            json={"timezone": "Nowhere/Land"},
        )
        assert resp.status_code == 400
        assert "time zone" in resp.json()["error"]
        row = client.get("/v1/api/admin/schedules/" + created["task_id"]).json()
        assert row["timezone"] == "UTC"
        assert row["next_run"] == created["next_run"]

    @pytest.mark.parametrize(
        "field", ["name", "enabled", "auto_approve", "cron_expr", "timezone", "notify_targets"]
    )
    def test_update_rejects_a_null_field_and_changes_nothing(self, client, field):
        # The request schema advertises every field as nullable and the SDK
        # forwards a caller's None, but a null has no meaning: as a value it
        # disabled the schedule or stored "None"; as "not sent" it would make
        # an intent to clear auto-approval a silent no-op.  Refused, whole.
        created = client.post(
            "/v1/api/admin/schedules",
            json=_cron_payload(cron_expr="30 6 * * *", timezone="Asia/Kolkata"),
        ).json()
        resp = client.put(
            "/v1/api/admin/schedules/" + created["task_id"],
            json={field: None, "description": "not applied either"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == f"{field} must not be null"
        row = client.get("/v1/api/admin/schedules/" + created["task_id"]).json()
        assert row == created

    def test_update_blank_zone_is_refused(self, client):
        # A blank zone means UTC on create, where nothing is lost; on update
        # it would silently re-zone the schedule, so it is refused.
        created = client.post(
            "/v1/api/admin/schedules",
            json=_cron_payload(cron_expr="30 6 * * *", timezone="Asia/Kolkata"),
        ).json()
        resp = client.put("/v1/api/admin/schedules/" + created["task_id"], json={"timezone": ""})
        assert resp.status_code == 400
        assert "blank" in resp.json()["error"]
        assert client.get("/v1/api/admin/schedules/" + created["task_id"]).json() == created

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
        # A public project is attachable with project.read even without membership.
        storage.create_project("proj_pub", "Pub", "someone-else", visibility="public")
        storage.create_user("original-owner", "original-owner", "Original Owner", "hash")
        storage.create_role(
            "schedule-project-reader",
            "schedule-project-reader",
            "Schedule project reader",
            "project.read",
            False,
        )
        storage.assign_role("original-owner", "schedule-project-reader")
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

    def test_create_with_impossible_date_cron_is_refused(self, client):
        """croniter.is_valid passes '0 0 31 4 *' but no date ever matches:
        a schedule that can never fire is refused, not stored dormant."""
        resp = client.post(
            "/v1/api/admin/schedules",
            json=_cron_payload(cron_expr="0 0 31 4 *"),
        )
        assert resp.status_code == 400
        assert "calendar" in resp.json()["error"]
        assert client.get("/v1/api/admin/schedules").json()["schedules"] == []

    def test_cron_is_evaluated_in_the_named_zone(self, client):
        resp = client.post(
            "/v1/api/admin/schedules/preview",
            json={"schedule_type": "cron", "cron_expr": "30 6 * * *", "timezone": "Asia/Kolkata"},
        )
        data = resp.json()
        assert data["valid"] is True
        # 06:30 IST every day is 01:00 UTC; the read-out still gets UTC
        # instants and converts to the browser's zone itself.
        assert all(t.endswith("T01:00:00+00:00") for t in data["next"])

    def test_unknown_zone_is_a_preview_outcome(self, client):
        resp = client.post(
            "/v1/api/admin/schedules/preview",
            json={"schedule_type": "cron", "cron_expr": "0 6 * * *", "timezone": "Nowhere/Land"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is False
        assert "time zone" in data["error"]
        assert data["next"] == []

    def test_null_field_is_a_preview_outcome(self, client):
        # The same rule as create and update, in the preview's own shape.
        resp = client.post(
            "/v1/api/admin/schedules/preview",
            json={"schedule_type": "cron", "cron_expr": "0 6 * * *", "timezone": None},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is False
        assert data["error"] == "timezone must not be null"
        assert data["next"] == []

    def test_blank_zone_previews_in_utc(self, client):
        resp = client.post(
            "/v1/api/admin/schedules/preview",
            json={"schedule_type": "cron", "cron_expr": "0 6 * * *", "timezone": ""},
        )
        assert all(t.endswith("T06:00:00+00:00") for t in resp.json()["next"])

    def test_cron_next_runs_carry_a_utc_offset(self, client):
        """next[] must be one shape: the 'at' branch echoes offset-bearing
        ISO, so the cron branch appends the UTC offset too."""
        resp = client.post(
            "/v1/api/admin/schedules/preview",
            json={"schedule_type": "cron", "cron_expr": "0 6 * * *"},
        )
        assert all(t.endswith("+00:00") for t in resp.json()["next"])


class TestNextCronRunsInZone:
    """next_cron_runs walks the cron in the schedule's zone and reports UTC.

    These pin the three failures a client-side local-to-UTC conversion has
    (the reason the zone is stored instead): an hour's drift across a
    daylight-saving change, and a weekly day that shifts near midnight.
    """

    @staticmethod
    def _start(iso: str):
        from datetime import datetime

        return datetime.fromisoformat(iso)

    def test_utc_is_the_default_and_the_old_behaviour(self) -> None:
        runs = next_cron_runs("0 6 * * *", 2, start=self._start("2026-01-01T00:00:00+00:00"))
        assert runs == ["2026-01-01T06:00:00", "2026-01-02T06:00:00"]

    def test_wall_clock_time_holds_across_spring_forward(self) -> None:
        # New York moves to daylight time on 2026-03-08 at 02:00, so 02:30
        # local is 07:30 UTC before and 06:30 UTC after; the removed 02:30 on
        # the change day fires at the first instant after the gap (03:00
        # EDT).  A UTC cron fixed at either offset would be an hour off for
        # half the year.
        runs = next_cron_runs(
            "30 2 * * *",
            3,
            "America/New_York",
            start=self._start("2026-03-06T12:00:00+00:00"),
        )
        assert runs == ["2026-03-07T07:30:00", "2026-03-08T07:00:00", "2026-03-09T06:30:00"]

    def test_wall_clock_time_holds_across_fall_back(self) -> None:
        # Daylight time ends 2026-11-01 at 02:00: 04:00 local is 08:00 UTC
        # before and 09:00 UTC after.
        runs = next_cron_runs(
            "0 4 * * *",
            2,
            "America/New_York",
            start=self._start("2026-10-30T12:00:00+00:00"),
        )
        assert runs == ["2026-10-31T08:00:00", "2026-11-01T09:00:00"]

    def test_a_fixed_time_fires_once_on_the_fall_back_day(self) -> None:
        # 01:30 exists twice on 2026-11-01 (EDT, then EST an hour later);
        # croniter visits both, and a time of day is de-duplicated to one.
        runs = next_cron_runs(
            "30 1 * * *",
            3,
            "America/New_York",
            start=self._start("2026-10-31T12:00:00+00:00"),
        )
        assert runs == ["2026-11-01T05:30:00", "2026-11-02T06:30:00", "2026-11-03T06:30:00"]

    def test_interleaved_minute_values_each_fire_once(self) -> None:
        # 01:00 and 01:30 alternate across the two offsets (01:00 EDT, 01:30
        # EDT, 01:00 EST, 01:30 EST): the second pass of each is skipped, so
        # neither adjacency nor memory of the last firing decides it.
        runs = next_cron_runs(
            "0,30 1 * * *",
            4,
            "America/New_York",
            start=self._start("2026-11-01T04:00:00+00:00"),
        )
        assert runs == [
            "2026-11-01T05:00:00",
            "2026-11-01T05:30:00",
            "2026-11-02T06:00:00",
            "2026-11-02T06:30:00",
        ]

    def test_a_range_names_times_too(self) -> None:
        # "0 1-2" is 01:00 and 02:00; on the fall-back day the repeated
        # 01:00 fires once and 02:00 (which exists only in standard time)
        # once.
        runs = next_cron_runs(
            "0 1-2 * * *",
            3,
            "America/New_York",
            start=self._start("2026-11-01T03:00:00+00:00"),
        )
        assert runs == ["2026-11-01T05:00:00", "2026-11-01T07:00:00", "2026-11-02T06:00:00"]

    def test_shorthand_names_a_time_too(self) -> None:
        # Havana's daylight time ends 2026-11-01 at 01:00, so midnight
        # repeats; "@daily" is the same time of day as "0 0 * * *".
        runs = next_cron_runs(
            "@daily",
            3,
            "America/Havana",
            start=self._start("2026-11-01T02:00:00+00:00"),
        )
        assert runs == ["2026-11-01T04:00:00", "2026-11-02T05:00:00", "2026-11-03T05:00:00"]

    def test_the_scheduler_advance_skips_the_repeated_hour(self) -> None:
        # The scheduler computes the next run seconds after a firing: from
        # 01:30 EDT the next 01:30 is tomorrow's, not the EST one an hour on.
        runs = next_cron_runs(
            "30 1 * * *",
            1,
            "America/New_York",
            start=self._start("2026-11-01T05:30:07+00:00"),
        )
        assert runs == ["2026-11-02T06:30:00"]

    def test_a_cadence_keeps_firing_through_the_repeated_hour(self) -> None:
        # The repeated hour is real time: every thirty minutes stays every
        # thirty minutes, so the local 01:00-02:00 hour fires four times.
        runs = next_cron_runs(
            "*/30 * * * *",
            5,
            "America/New_York",
            start=self._start("2026-11-01T04:50:00+00:00"),
        )
        assert runs == [
            "2026-11-01T05:00:00",
            "2026-11-01T05:30:00",
            "2026-11-01T06:00:00",
            "2026-11-01T06:30:00",
            "2026-11-01T07:00:00",
        ]

    def test_two_fixed_times_stay_distinct_across_spring_forward(self) -> None:
        # The removed 02:30 fires at 03:00 EDT and the real 03:30 follows:
        # different wall-clock times, so nothing is mistaken for a repeat.
        runs = next_cron_runs(
            "30 2,3 * * *",
            3,
            "America/New_York",
            start=self._start("2026-03-08T05:00:00+00:00"),
        )
        assert runs == ["2026-03-08T07:00:00", "2026-03-08T07:30:00", "2026-03-09T06:30:00"]

    @pytest.mark.parametrize(
        ("expr", "names_a_time"),
        [
            ("30 1 * * *", True),
            ("0 9,17 * * 1-5", True),
            ("*/15 * * * *", False),
            ("0 */4 * * *", False),
            ("0-59/15 1 * * *", False),
            ("30 * * * *", False),
            ("@daily", True),
            ("@MIDNIGHT", True),
            ("@weekly", True),
            ("@hourly", False),
            ("0 1-2 * * *", True),
            ("0-30 1 * * *", True),
            ("0 1-5/2 * * *", False),
        ],
    )
    def test_cron_names_a_time(self, expr: str, names_a_time: bool) -> None:
        assert cron_names_a_time(expr) is names_a_time

    def test_weekly_day_is_the_local_day(self) -> None:
        # Monday 00:30 in Auckland is Sunday in UTC — 12:30 under standard
        # time (+12) and 11:30 once daylight time starts on 2026-09-27 (+13).
        # A UTC cron would have to name Sunday and pick one of the two hours.
        runs = next_cron_runs(
            "30 0 * * 1",
            4,
            "Pacific/Auckland",
            start=self._start("2026-09-04T00:00:00+00:00"),
        )
        assert runs == [
            "2026-09-06T12:30:00",
            "2026-09-13T12:30:00",
            "2026-09-20T12:30:00",
            "2026-09-27T11:30:00",
        ]

    def test_impossible_date_is_none_in_any_zone(self) -> None:
        assert next_cron_runs("0 0 30 2 *", 1, "Europe/Berlin") is None

    def test_unparsable_expression_is_none_not_an_exception(self) -> None:
        # A stored expression this croniter no longer parses is the
        # scheduler's case too: disabled with the reason, not raised.
        assert next_cron_runs("not a cron", 1) is None

    def test_unresolvable_zone_is_none_not_an_exception(self) -> None:
        # A stored zone the host can no longer resolve is the scheduler's
        # case (requests validate first): the walk must retire the schedule,
        # not raise through the tick.
        assert next_cron_runs("0 6 * * *", 1, "Nowhere/Land") is None


def test_message_cap_is_mirrored_by_the_console_ui() -> None:
    """The launcher's client-side cap and the admin shelf's textarea maxlength
    carry the server's number.  The number only: the launcher counts code
    points like the server, while an HTML maxlength can only count UTF-16
    units, so the semantics are allowed to differ at the edge."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    app = (root / "turnstone/console/static/app.js").read_text(encoding="utf-8")
    index = (root / "turnstone/console/static/index.html").read_text(encoding="utf-8")
    assert f"const _SCHEDULE_TASK_MAX_CHARS = {SCHEDULE_MESSAGE_MAX_CHARS};" in app
    assert f'maxlength="{SCHEDULE_MESSAGE_MAX_CHARS}"' in index
    assert f"const _SCHEDULE_NAME_MAX_CHARS = {SCHEDULE_NAME_MAX_CHARS};" in app
    assert f'maxlength="{SCHEDULE_NAME_MAX_CHARS}"' in index


def test_create_keeps_the_capped_message_length(client) -> None:
    resp = client.post(
        "/v1/api/admin/schedules",
        json={
            "name": "long",
            "schedule_type": "cron",
            "cron_expr": "0 6 * * *",
            "initial_message": "x" * (SCHEDULE_MESSAGE_MAX_CHARS + 10),
        },
    )
    assert resp.status_code == 200
    assert len(resp.json()["initial_message"]) == SCHEDULE_MESSAGE_MAX_CHARS


def test_create_keeps_the_capped_name_length(client) -> None:
    resp = client.post(
        "/v1/api/admin/schedules",
        json={
            "name": "n" * (SCHEDULE_NAME_MAX_CHARS + 10),
            "schedule_type": "cron",
            "cron_expr": "0 6 * * *",
            "initial_message": "task",
        },
    )
    assert resp.status_code == 200
    assert len(resp.json()["name"]) == SCHEDULE_NAME_MAX_CHARS
