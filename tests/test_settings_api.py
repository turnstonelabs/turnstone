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


@pytest.fixture
def secret_key():
    """Register a synthetic ``is_secret`` setting for the duration of a test.

    The settings registry currently has no production ``is_secret`` setting, but
    the write-only / ``"***"``-sentinel masking machinery remains and must stay
    covered for the next secret setting that lands. Injecting a test-only def
    exercises that machinery without coupling the tests to any specific
    production key.
    """
    from turnstone.core import settings_registry as reg

    key = "tools.test_secret"
    reg.SETTINGS[key] = reg.SettingDef(
        key, "str", "", "Test-only secret setting", "tools", is_secret=True
    )
    try:
        yield key
    finally:
        reg.SETTINGS.pop(key, None)


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

    def test_secret_flag(self, client, secret_key):
        r = client.get("/v1/api/admin/settings/schema")
        by_key = {s["key"]: s for s in r.json()["schema"]}
        assert by_key[secret_key]["is_secret"] is True
        assert by_key["tools.timeout"]["is_secret"] is False


# ---------------------------------------------------------------------------
# Secret masking
# ---------------------------------------------------------------------------


class TestSecretMasking:
    def test_secret_masked_in_list(self, client, storage, secret_key):
        from turnstone.core.settings_registry import serialize_value

        storage.upsert_system_setting(
            key=secret_key,
            value=serialize_value("sk-real-secret"),
            node_id="",
            is_secret=True,
            changed_by="admin",
        )
        r = client.get("/v1/api/admin/settings")
        by_key = {s["key"]: s for s in r.json()["settings"]}
        assert by_key[secret_key]["value"] == "***"

    def test_secret_writable_via_api(self, client, secret_key):
        """Secret settings can be written via API (write-only pattern)."""
        r = client.put(
            f"/v1/api/admin/settings/{secret_key}",
            json={"value": "sk-secret-123"},
        )
        assert r.status_code == 200
        # Response value is masked even for the write confirmation
        assert r.json()["value"] == "***"

    def test_secret_sentinel_preserves_existing(self, client, secret_key):
        """Submitting '***' for a secret setting is a no-op (preserve existing)."""
        # First write a real value
        r1 = client.put(
            f"/v1/api/admin/settings/{secret_key}",
            json={"value": "sk-real-key"},
        )
        assert r1.status_code == 200
        # Now submit the sentinel — should return unchanged with full response shape
        r2 = client.put(
            f"/v1/api/admin/settings/{secret_key}",
            json={"value": "***"},
        )
        assert r2.status_code == 200
        data = r2.json()
        assert data.get("unchanged") is True
        assert data["key"] == secret_key
        assert data["value"] == "***"
        assert data["type"] == "str"
        assert data["is_secret"] is True

    def test_secret_still_masked_in_list(self, client, secret_key):
        """After writing a secret, list still shows '***'."""
        client.put(
            f"/v1/api/admin/settings/{secret_key}",
            json={"value": "sk-written-via-api"},
        )
        r = client.get("/v1/api/admin/settings")
        by_key = {s["key"]: s for s in r.json()["settings"]}
        assert by_key[secret_key]["value"] == "***"


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
