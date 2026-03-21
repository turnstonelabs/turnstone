"""Tests for system settings admin API endpoints."""

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
    admin_delete_setting,
    admin_list_settings,
    admin_settings_schema,
    admin_update_setting,
)
from turnstone.core.auth import AuthResult
from turnstone.core.storage._sqlite import SQLiteBackend

# ---------------------------------------------------------------------------
# Auth bypass middleware
# ---------------------------------------------------------------------------


class _InjectAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id="test-user",
            scopes=frozenset({"approve"}),
            token_source="config",
            permissions=frozenset(
                {
                    "read",
                    "write",
                    "approve",
                    "admin.settings",
                }
            ),
        )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path):
    return SQLiteBackend(str(tmp_path / "test.db"))


@pytest.fixture
def client(storage):
    """TestClient wired to console admin settings endpoints."""
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route("/api/admin/settings", admin_list_settings),
                    Route("/api/admin/settings/schema", admin_settings_schema),
                    Route(
                        "/api/admin/settings/{key:path}",
                        admin_update_setting,
                        methods=["PUT"],
                    ),
                    Route(
                        "/api/admin/settings/{key:path}",
                        admin_delete_setting,
                        methods=["DELETE"],
                    ),
                ],
            ),
        ],
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.auth_storage = storage
    return TestClient(app)


# ---------------------------------------------------------------------------
# List settings
# ---------------------------------------------------------------------------


class TestListSettings:
    def test_returns_all_registry_entries(self, client):
        from turnstone.core.settings_registry import SETTINGS

        r = client.get("/v1/api/admin/settings")
        assert r.status_code == 200
        data = r.json()
        assert len(data["settings"]) == len(SETTINGS)
        # Every entry has source "default" when nothing stored
        for entry in data["settings"]:
            assert entry["source"] == "default"

    def test_stored_value_shows_source_storage(self, client, storage):
        from turnstone.core.settings_registry import serialize_value

        storage.upsert_system_setting(
            key="tools.timeout",
            value=serialize_value(60),
            node_id="",
            is_secret=False,
            changed_by="admin",
        )
        r = client.get("/v1/api/admin/settings")
        assert r.status_code == 200
        by_key = {s["key"]: s for s in r.json()["settings"]}
        assert by_key["tools.timeout"]["source"] == "storage"
        assert by_key["tools.timeout"]["value"] == 60


# ---------------------------------------------------------------------------
# Update setting
# ---------------------------------------------------------------------------


class TestUpdateSetting:
    def test_update_valid(self, client):
        r = client.put(
            "/v1/api/admin/settings/tools.timeout",
            json={"value": 30},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["key"] == "tools.timeout"
        assert data["value"] == 30
        assert data["source"] == "storage"

    def test_update_invalid_key(self, client):
        r = client.put(
            "/v1/api/admin/settings/bogus.nonexistent",
            json={"value": "x"},
        )
        assert r.status_code == 400
        assert "Unknown setting" in r.json()["error"]

    def test_update_invalid_value_out_of_range(self, client):
        r = client.put(
            "/v1/api/admin/settings/tools.timeout",
            json={"value": 0},
        )
        assert r.status_code == 400
        assert "minimum" in r.json()["error"]

    def test_update_then_list_shows_storage(self, client):
        client.put(
            "/v1/api/admin/settings/tools.timeout",
            json={"value": 42},
        )
        r = client.get("/v1/api/admin/settings")
        by_key = {s["key"]: s for s in r.json()["settings"]}
        assert by_key["tools.timeout"]["source"] == "storage"
        assert by_key["tools.timeout"]["value"] == 42


# ---------------------------------------------------------------------------
# Delete setting
# ---------------------------------------------------------------------------


class TestDeleteSetting:
    def test_delete_stored(self, client):
        # First store a value
        client.put(
            "/v1/api/admin/settings/tools.timeout",
            json={"value": 30},
        )
        # Delete it
        r = client.delete("/v1/api/admin/settings/tools.timeout")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["key"] == "tools.timeout"
        assert body["default"] == 120  # registry default for tools.timeout

    def test_delete_then_list_shows_default(self, client):
        client.put(
            "/v1/api/admin/settings/tools.timeout",
            json={"value": 30},
        )
        client.delete("/v1/api/admin/settings/tools.timeout")
        r = client.get("/v1/api/admin/settings")
        by_key = {s["key"]: s for s in r.json()["settings"]}
        assert by_key["tools.timeout"]["source"] == "default"

    def test_delete_non_existent(self, client):
        r = client.delete("/v1/api/admin/settings/tools.timeout")
        assert r.status_code == 404
        assert "not found" in r.json()["error"]


# ---------------------------------------------------------------------------
# Schema endpoint
# ---------------------------------------------------------------------------


class TestSettingsSchema:
    def test_returns_registry(self, client):
        from turnstone.core.settings_registry import SETTINGS

        r = client.get("/v1/api/admin/settings/schema")
        assert r.status_code == 200
        data = r.json()
        assert len(data["schema"]) == len(SETTINGS)
        # Spot-check a few fields
        by_key = {s["key"]: s for s in data["schema"]}
        timeout = by_key["tools.timeout"]
        assert timeout["type"] == "int"
        assert timeout["default"] == 120
        assert timeout["min_value"] == 1
        assert timeout["max_value"] == 3600
        assert timeout["description"]

    def test_choices_present(self, client):
        r = client.get("/v1/api/admin/settings/schema")
        by_key = {s["key"]: s for s in r.json()["schema"]}
        assert by_key["tools.search"]["choices"] == ["auto", "on", "off"]

    def test_secret_flag(self, client):
        r = client.get("/v1/api/admin/settings/schema")
        by_key = {s["key"]: s for s in r.json()["schema"]}
        assert by_key["judge.api_key"]["is_secret"] is True
        assert by_key["tools.timeout"]["is_secret"] is False


# ---------------------------------------------------------------------------
# Secret masking
# ---------------------------------------------------------------------------


class TestSecretMasking:
    def test_secret_masked_in_list(self, client, storage):
        from turnstone.core.settings_registry import serialize_value

        storage.upsert_system_setting(
            key="judge.api_key",
            value=serialize_value("sk-real-secret"),
            node_id="",
            is_secret=True,
            changed_by="admin",
        )
        r = client.get("/v1/api/admin/settings")
        by_key = {s["key"]: s for s in r.json()["settings"]}
        assert by_key["judge.api_key"]["value"] == "***"

    def test_secret_write_blocked(self, client):
        """Secret settings cannot be modified via API."""
        r = client.put(
            "/v1/api/admin/settings/judge.api_key",
            json={"value": "sk-secret-123"},
        )
        assert r.status_code == 403
        assert "config.toml" in r.json()["error"]

    def test_secret_shows_managed_label(self, client):
        """Secret settings show a label instead of a value."""
        r = client.get("/v1/api/admin/settings")
        by_key = {s["key"]: s for s in r.json()["settings"]}
        assert "managed via" in by_key["judge.api_key"]["value"]


# ---------------------------------------------------------------------------
# Audit trail (verify endpoint returns 200, confirming record_audit call)
# ---------------------------------------------------------------------------


class TestAuditTrail:
    def test_update_returns_200(self, client):
        """Update succeeds — audit recording did not raise."""
        r = client.put(
            "/v1/api/admin/settings/tools.timeout",
            json={"value": 45},
        )
        assert r.status_code == 200

    def test_delete_returns_200(self, client):
        """Delete succeeds — audit recording did not raise."""
        client.put(
            "/v1/api/admin/settings/tools.timeout",
            json={"value": 45},
        )
        r = client.delete("/v1/api/admin/settings/tools.timeout")
        assert r.status_code == 200
