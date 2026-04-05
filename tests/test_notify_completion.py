"""Tests for scheduled task completion notification feature.

Covers: target validation, content extraction, notification delivery
(mock gateway), scheduler dispatch passthrough, schedule API CRUD
with notify_targets.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

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
    admin_get_schedule,
    admin_update_schedule,
)
from turnstone.core.auth import AuthResult
from turnstone.core.storage._sqlite import SQLiteBackend
from turnstone.server import (
    _deliver_notification,
    _extract_last_assistant_content,
    _fire_notify_targets,
    _validate_notify_targets,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    return SQLiteBackend(str(tmp_path / "test.db"))


@pytest.fixture
def client(storage):
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route("/api/admin/schedules", admin_create_schedule, methods=["POST"]),
                    Route("/api/admin/schedules/{task_id}", admin_get_schedule),
                    Route(
                        "/api/admin/schedules/{task_id}",
                        admin_update_schedule,
                        methods=["PUT"],
                    ),
                ],
            ),
        ],
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.auth_storage = storage
    return TestClient(app)


def _cron_payload(**overrides):
    defaults = {
        "name": "Notify test",
        "description": "Test schedule",
        "schedule_type": "cron",
        "cron_expr": "0 9 * * *",
        "target_mode": "auto",
        "model": "gpt-5",
        "initial_message": "Run the tests",
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Target validation
# ---------------------------------------------------------------------------


class TestValidateNotifyTargets:
    def test_empty_string(self):
        result, err = _validate_notify_targets("")
        assert result == "[]"
        assert err == ""

    def test_none(self):
        result, err = _validate_notify_targets(None)
        assert result == "[]"
        assert err == ""

    def test_valid_channel_id(self):
        targets = [{"channel_type": "discord", "channel_id": "123456"}]
        result, err = _validate_notify_targets(json.dumps(targets))
        assert err == ""
        assert json.loads(result) == targets

    def test_valid_user_id(self):
        targets = [{"channel_type": "discord", "user_id": "789"}]
        result, err = _validate_notify_targets(json.dumps(targets))
        assert err == ""
        assert json.loads(result) == targets

    def test_valid_list_input(self):
        targets = [{"channel_type": "discord", "channel_id": "123"}]
        result, err = _validate_notify_targets(targets)
        assert err == ""
        assert json.loads(result) == targets

    def test_multiple_targets(self):
        targets = [
            {"channel_type": "discord", "channel_id": "111"},
            {"channel_type": "discord", "user_id": "222"},
        ]
        result, err = _validate_notify_targets(json.dumps(targets))
        assert err == ""
        assert len(json.loads(result)) == 2

    def test_invalid_json(self):
        _, err = _validate_notify_targets("{not json")
        assert "valid JSON" in err

    def test_not_array(self):
        _, err = _validate_notify_targets('{"key": "val"}')
        assert "array" in err

    def test_missing_channel_type(self):
        targets = [{"channel_id": "123"}]
        _, err = _validate_notify_targets(json.dumps(targets))
        assert "channel_type" in err

    def test_missing_id_field(self):
        targets = [{"channel_type": "discord"}]
        _, err = _validate_notify_targets(json.dumps(targets))
        assert "channel_id or user_id" in err

    def test_non_object_element(self):
        _, err = _validate_notify_targets('["string"]')
        assert "object" in err

    def test_exceeds_max_targets(self):
        targets = [{"channel_type": "discord", "channel_id": str(i)} for i in range(11)]
        _, err = _validate_notify_targets(json.dumps(targets))
        assert "limited to" in err

    def test_max_targets_at_limit(self):
        targets = [{"channel_type": "discord", "channel_id": str(i)} for i in range(10)]
        result, err = _validate_notify_targets(json.dumps(targets))
        assert err == ""
        assert len(json.loads(result)) == 10

    def test_field_too_long(self):
        targets = [{"channel_type": "discord", "channel_id": "x" * 257}]
        _, err = _validate_notify_targets(json.dumps(targets))
        assert "256 chars" in err

    def test_non_string_field_value(self):
        _, err = _validate_notify_targets('[{"channel_type": 123, "channel_id": "1"}]')
        assert "string" in err

    def test_empty_string_channel_type(self):
        targets = [{"channel_type": "", "channel_id": "123"}]
        _, err = _validate_notify_targets(json.dumps(targets))
        assert "non-empty" in err

    def test_empty_string_channel_id(self):
        targets = [{"channel_type": "discord", "channel_id": ""}]
        _, err = _validate_notify_targets(json.dumps(targets))
        assert "non-empty" in err

    def test_whitespace_only_values_stripped(self):
        targets = [{"channel_type": "discord", "channel_id": "  123  "}]
        result, err = _validate_notify_targets(json.dumps(targets))
        assert err == ""
        parsed = json.loads(result)
        assert parsed[0]["channel_id"] == "123"

    def test_both_channel_id_and_user_id_rejected(self):
        targets = [{"channel_type": "discord", "channel_id": "1", "user_id": "2"}]
        _, err = _validate_notify_targets(json.dumps(targets))
        assert "only one of" in err


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------


class TestExtractLastAssistantContent:
    def test_string_content(self):
        session = MagicMock()
        session.messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        assert _extract_last_assistant_content(session) == "world"

    def test_structured_content(self):
        session = MagicMock()
        session.messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "part one"},
                    {"type": "text", "text": "part two"},
                ],
            },
        ]
        assert _extract_last_assistant_content(session) == "part one\npart two"

    def test_empty_messages(self):
        session = MagicMock()
        session.messages = []
        assert _extract_last_assistant_content(session) == ""

    def test_no_assistant_messages(self):
        session = MagicMock()
        session.messages = [{"role": "user", "content": "hello"}]
        assert _extract_last_assistant_content(session) == ""

    def test_picks_last_assistant(self):
        session = MagicMock()
        session.messages = [
            {"role": "assistant", "content": "first"},
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "second"},
        ]
        assert _extract_last_assistant_content(session) == "second"

    def test_skips_non_text_blocks(self):
        session = MagicMock()
        session.messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "123"},
                    {"type": "text", "text": "result"},
                ],
            },
        ]
        assert _extract_last_assistant_content(session) == "result"


# ---------------------------------------------------------------------------
# Notification delivery (mock gateway)
# ---------------------------------------------------------------------------


class TestDeliverNotification:
    @patch("httpx.post")
    def test_successful_delivery(self, mock_post):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"results": [{"status": "sent"}]}
        mock_post.return_value = mock_resp

        storage = MagicMock()
        storage.list_services.return_value = [{"url": "http://gateway:8080"}]

        payload = {
            "target": {"channel_type": "discord", "channel_id": "123"},
            "message": "Hello",
            "title": "Schedule: test",
            "ws_id": "ws_001",
        }
        _deliver_notification(storage, payload, {"Authorization": "Bearer tok"})

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args.kwargs
        assert call_kwargs["json"] == payload
        assert "Authorization" in call_kwargs["headers"]

    def test_no_services_retries(self):
        storage = MagicMock()
        storage.list_services.return_value = []

        with patch("time.sleep"):
            _deliver_notification(storage, {"ws_id": "ws_001"}, {})

        assert storage.list_services.call_count == 3

    @patch("httpx.post", side_effect=ConnectionError("refused"))
    def test_http_error_continues(self, mock_post):
        storage = MagicMock()
        storage.list_services.return_value = [{"url": "http://gw:8080"}]

        with patch("time.sleep"):
            _deliver_notification(storage, {"ws_id": "ws_001"}, {})

        assert mock_post.call_count >= 1


class TestFireNotifyTargets:
    @patch("turnstone.server._deliver_notification")
    @patch(
        "turnstone.core.session._notify_auth_headers",
        return_value={"Authorization": "Bearer x"},
    )
    def test_fires_for_each_target(self, mock_auth, mock_deliver):
        ws = MagicMock()
        ws.id = "ws_test"
        ws.name = "My Task"
        ws.notify_targets = json.dumps(
            [
                {"channel_type": "discord", "channel_id": "111"},
                {"channel_type": "discord", "user_id": "222"},
            ]
        )

        with patch("turnstone.core.storage.get_storage") as mock_storage:
            mock_storage.return_value = MagicMock()
            _fire_notify_targets(ws, "Task completed successfully")

        assert mock_deliver.call_count == 2
        # First call — channel_id target
        first_payload = mock_deliver.call_args_list[0][0][1]
        assert first_payload["target"]["channel_id"] == "111"
        assert first_payload["message"] == "Task completed successfully"
        assert first_payload["title"] == "Schedule: My Task"
        # Second call — user_id target
        second_payload = mock_deliver.call_args_list[1][0][1]
        assert second_payload["target"]["channel_id"] == "222"

    @patch("turnstone.server._deliver_notification")
    def test_empty_targets_skipped(self, mock_deliver):
        ws = MagicMock()
        ws.notify_targets = "[]"
        _fire_notify_targets(ws, "content")
        mock_deliver.assert_not_called()

    @patch("turnstone.server._deliver_notification")
    def test_empty_content_skipped(self, mock_deliver):
        ws = MagicMock()
        ws.notify_targets = '[{"channel_type":"discord","channel_id":"1"}]'
        _fire_notify_targets(ws, "")
        mock_deliver.assert_not_called()

    @patch("turnstone.server._deliver_notification")
    def test_invalid_json_targets_skipped(self, mock_deliver):
        ws = MagicMock()
        ws.notify_targets = "not json"
        _fire_notify_targets(ws, "content")
        mock_deliver.assert_not_called()


# ---------------------------------------------------------------------------
# Scheduler dispatch passthrough
# ---------------------------------------------------------------------------


class TestSchedulerDispatch:
    def test_notify_targets_passed_to_sdk(self):
        collector = MagicMock()
        storage = MagicMock()
        # Wire up lock acquisition
        state: dict[str, dict[str, str] | None] = {"scheduler_lock": None}

        def _get(key: str, **_kw: object) -> dict[str, str] | None:
            return state.get(key)

        def _upsert(key: str, value: str, **_kw: object) -> None:
            state[key] = {"value": value}

        def _delete(key: str, **_kw: object) -> None:
            state.pop(key, None)

        storage.get_system_setting.side_effect = _get
        storage.upsert_system_setting.side_effect = _upsert
        storage.delete_system_setting.side_effect = _delete

        targets = [{"channel_type": "discord", "channel_id": "123"}]
        task = {
            "task_id": "t1",
            "name": "Test",
            "description": "",
            "schedule_type": "cron",
            "cron_expr": "0 9 * * *",
            "at_time": "",
            "target_mode": "auto",
            "model": "gpt-5",
            "initial_message": "Run it",
            "auto_approve": 0,
            "auto_approve_tools": "",
            "skill": "",
            "notify_targets": json.dumps(targets),
            "enabled": 1,
            "created_by": "admin",
            "next_run": "2020-01-01T09:00:00",
            "last_run": "",
            "created": "2020-01-01T00:00:00",
            "updated": "2020-01-01T00:00:00",
        }

        mock_resp = MagicMock()
        mock_resp.ws_id = "ws_abc"
        mock_client = MagicMock()
        mock_client.create_workstream.return_value = mock_resp

        from turnstone.console.scheduler import TaskScheduler

        scheduler = TaskScheduler(collector, storage)

        collector.nodes.return_value = [
            {"node_id": "node-001", "reachable": True, "ws_total": 1, "max_ws": 10}
        ]

        with (
            patch.object(scheduler, "_get_sdk_client", return_value=mock_client),
            patch.object(scheduler, "_get_node_url", return_value="http://n:8000"),
        ):
            scheduler._dispatch_to_node(task, "node-001", "2020-01-01T09:00:00")

        mock_client.create_workstream.assert_called_once()
        call_kwargs = mock_client.create_workstream.call_args.kwargs
        assert call_kwargs["notify_targets"] == json.dumps(targets)


# ---------------------------------------------------------------------------
# Schedule API CRUD with notify_targets
# ---------------------------------------------------------------------------


class TestScheduleAPINotifyTargets:
    def test_create_with_notify_targets(self, client):
        targets = [{"channel_type": "discord", "channel_id": "123456"}]
        resp = client.post(
            "/v1/api/admin/schedules",
            json=_cron_payload(notify_targets=targets),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["notify_targets"] == targets

    def test_create_without_notify_targets(self, client):
        resp = client.post("/v1/api/admin/schedules", json=_cron_payload())
        assert resp.status_code == 200
        assert resp.json()["notify_targets"] == []

    def test_create_invalid_notify_targets(self, client):
        resp = client.post(
            "/v1/api/admin/schedules",
            json=_cron_payload(notify_targets="not json"),
        )
        assert resp.status_code == 400
        assert "notify_targets" in resp.json()["error"]

    def test_create_notify_targets_missing_channel_type(self, client):
        targets = [{"channel_id": "123"}]
        resp = client.post(
            "/v1/api/admin/schedules",
            json=_cron_payload(notify_targets=targets),
        )
        assert resp.status_code == 400

    def test_create_notify_targets_missing_id(self, client):
        targets = [{"channel_type": "discord"}]
        resp = client.post(
            "/v1/api/admin/schedules",
            json=_cron_payload(notify_targets=targets),
        )
        assert resp.status_code == 400

    def test_update_notify_targets(self, client):
        create_resp = client.post("/v1/api/admin/schedules", json=_cron_payload())
        task_id = create_resp.json()["task_id"]

        new_targets = [{"channel_type": "discord", "user_id": "999"}]
        resp = client.put(
            f"/v1/api/admin/schedules/{task_id}",
            json={"notify_targets": new_targets},
        )
        assert resp.status_code == 200
        assert resp.json()["notify_targets"] == new_targets

    def test_update_clear_notify_targets(self, client):
        targets = [{"channel_type": "discord", "channel_id": "123"}]
        create_resp = client.post(
            "/v1/api/admin/schedules",
            json=_cron_payload(notify_targets=targets),
        )
        task_id = create_resp.json()["task_id"]

        resp = client.put(
            f"/v1/api/admin/schedules/{task_id}",
            json={"notify_targets": []},
        )
        assert resp.status_code == 200
        assert resp.json()["notify_targets"] == []

    def test_get_includes_notify_targets(self, client):
        targets = [{"channel_type": "discord", "channel_id": "456"}]
        create_resp = client.post(
            "/v1/api/admin/schedules",
            json=_cron_payload(notify_targets=targets),
        )
        task_id = create_resp.json()["task_id"]

        get_resp = client.get(f"/v1/api/admin/schedules/{task_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["notify_targets"] == targets

    def test_update_invalid_notify_targets(self, client):
        create_resp = client.post("/v1/api/admin/schedules", json=_cron_payload())
        task_id = create_resp.json()["task_id"]

        resp = client.put(
            f"/v1/api/admin/schedules/{task_id}",
            json={"notify_targets": "not json"},
        )
        assert resp.status_code == 400
