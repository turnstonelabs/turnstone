"""Tests for skill resource admin API endpoints."""

from __future__ import annotations

import uuid
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
    admin_create_skill_resource,
    admin_delete_skill_resource,
    admin_get_skill,
    admin_get_skill_resource,
    admin_list_skill_resources,
    admin_list_skills,
)
from turnstone.core.auth import AuthResult
from turnstone.core.storage._sqlite import SQLiteBackend

# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


class _InjectAuthMiddleware(BaseHTTPMiddleware):
    """Inject an admin auth result with admin.skills permission."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id="test-user",
            scopes=frozenset({"approve"}),
            token_source="config",
            permissions=frozenset({"read", "write", "approve", "admin.skills"}),
        )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ROUTES = [
    Mount(
        "/v1",
        routes=[
            Route("/api/admin/skills", admin_list_skills),
            Route("/api/admin/skills/{skill_id}", admin_get_skill),
            Route(
                "/api/admin/skills/{skill_id}/resources",
                admin_list_skill_resources,
            ),
            Route(
                "/api/admin/skills/{skill_id}/resources",
                admin_create_skill_resource,
                methods=["POST"],
            ),
            Route(
                "/api/admin/skills/{skill_id}/resources/{path:path}",
                admin_get_skill_resource,
            ),
            Route(
                "/api/admin/skills/{skill_id}/resources/{path:path}",
                admin_delete_skill_resource,
                methods=["DELETE"],
            ),
        ],
    ),
]


@pytest.fixture()
def storage(tmp_path):
    return SQLiteBackend(str(tmp_path / "test.db"))


@pytest.fixture()
def client(storage):
    app = Starlette(
        routes=_ROUTES,
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.auth_storage = storage
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_test_skill(storage: SQLiteBackend, *, readonly: bool = False) -> str:
    """Create a minimal skill in storage and return its template_id."""
    skill_id = uuid.uuid4().hex
    storage.create_prompt_template(
        template_id=skill_id,
        name=f"test-skill-{skill_id[:8]}",
        category="general",
        content="Test skill content.",
        variables="[]",
        is_default=False,
        org_id="",
        created_by="test",
        readonly=readonly,
    )
    return skill_id


# ---------------------------------------------------------------------------
# Tests: List resources
# ---------------------------------------------------------------------------


class TestListSkillResources:
    def test_list_empty(self, client, storage):
        skill_id = _create_test_skill(storage)
        resp = client.get(f"/v1/api/admin/skills/{skill_id}/resources")
        assert resp.status_code == 200
        data = resp.json()
        assert data["resources"] == []

    def test_list_with_resources(self, client, storage):
        skill_id = _create_test_skill(storage)
        storage.create_skill_resource(uuid.uuid4().hex, skill_id, "scripts/a.sh", "content")
        resp = client.get(f"/v1/api/admin/skills/{skill_id}/resources")
        assert resp.status_code == 200
        resources = resp.json()["resources"]
        assert len(resources) == 1
        assert resources[0]["path"] == "scripts/a.sh"
        assert "content" not in resources[0]  # Content NOT in list view

    def test_skill_not_found(self, client):
        resp = client.get("/v1/api/admin/skills/nonexistent/resources")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Create resource
# ---------------------------------------------------------------------------


class TestCreateSkillResource:
    def test_create_valid(self, client, storage):
        skill_id = _create_test_skill(storage)
        resp = client.post(
            f"/v1/api/admin/skills/{skill_id}/resources",
            json={"path": "scripts/setup.sh", "content": "#!/bin/bash\necho hello"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["path"] == "scripts/setup.sh"
        assert data["size"] > 0

    def test_invalid_path(self, client, storage):
        skill_id = _create_test_skill(storage)
        resp = client.post(
            f"/v1/api/admin/skills/{skill_id}/resources",
            json={"path": "malicious/file.sh", "content": "x"},
        )
        assert resp.status_code == 400

    def test_path_traversal_rejected(self, client, storage):
        skill_id = _create_test_skill(storage)
        resp = client.post(
            f"/v1/api/admin/skills/{skill_id}/resources",
            json={"path": "scripts/../../etc/passwd", "content": "x"},
        )
        assert resp.status_code == 400

    def test_null_byte_in_path_rejected(self, client, storage):
        skill_id = _create_test_skill(storage)
        resp = client.post(
            f"/v1/api/admin/skills/{skill_id}/resources",
            json={"path": "scripts/a\x00.sh", "content": "x"},
        )
        assert resp.status_code == 400

    def test_duplicate_409(self, client, storage):
        skill_id = _create_test_skill(storage)
        storage.create_skill_resource(uuid.uuid4().hex, skill_id, "scripts/a.sh", "content")
        resp = client.post(
            f"/v1/api/admin/skills/{skill_id}/resources",
            json={"path": "scripts/a.sh", "content": "new"},
        )
        assert resp.status_code == 409

    def test_size_cap(self, client, storage):
        skill_id = _create_test_skill(storage)
        resp = client.post(
            f"/v1/api/admin/skills/{skill_id}/resources",
            json={"path": "scripts/big.sh", "content": "x" * (100 * 1024 + 1)},
        )
        assert resp.status_code == 400

    def test_max_count(self, client, storage):
        skill_id = _create_test_skill(storage)
        for i in range(10):
            storage.create_skill_resource(uuid.uuid4().hex, skill_id, f"scripts/s{i}.sh", "content")
        resp = client.post(
            f"/v1/api/admin/skills/{skill_id}/resources",
            json={"path": "scripts/extra.sh", "content": "x"},
        )
        assert resp.status_code == 400

    def test_readonly_skill_blocked(self, client, storage):
        skill_id = _create_test_skill(storage, readonly=True)
        resp = client.post(
            f"/v1/api/admin/skills/{skill_id}/resources",
            json={"path": "scripts/a.sh", "content": "x"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Tests: Get resource
# ---------------------------------------------------------------------------


class TestGetSkillResource:
    def test_get_existing(self, client, storage):
        skill_id = _create_test_skill(storage)
        storage.create_skill_resource(uuid.uuid4().hex, skill_id, "scripts/a.sh", "hello world")
        resp = client.get(
            f"/v1/api/admin/skills/{skill_id}/resources/scripts/a.sh",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["content"] == "hello world"
        assert data["path"] == "scripts/a.sh"

    def test_not_found(self, client, storage):
        skill_id = _create_test_skill(storage)
        resp = client.get(
            f"/v1/api/admin/skills/{skill_id}/resources/scripts/nope.sh",
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Delete resource
# ---------------------------------------------------------------------------


class TestDeleteSkillResource:
    def test_delete_existing(self, client, storage):
        skill_id = _create_test_skill(storage)
        storage.create_skill_resource(uuid.uuid4().hex, skill_id, "scripts/a.sh", "content")
        resp = client.delete(
            f"/v1/api/admin/skills/{skill_id}/resources/scripts/a.sh",
        )
        assert resp.status_code == 200
        assert storage.get_skill_resource(skill_id, "scripts/a.sh") is None

    def test_not_found(self, client, storage):
        skill_id = _create_test_skill(storage)
        resp = client.delete(
            f"/v1/api/admin/skills/{skill_id}/resources/scripts/nope.sh",
        )
        assert resp.status_code == 404

    def test_readonly_blocked(self, client, storage):
        skill_id = _create_test_skill(storage, readonly=True)
        storage.create_skill_resource(uuid.uuid4().hex, skill_id, "scripts/a.sh", "content")
        resp = client.delete(
            f"/v1/api/admin/skills/{skill_id}/resources/scripts/a.sh",
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Tests: Resource count in skill responses
# ---------------------------------------------------------------------------


class TestResourceCountInSkillResponse:
    def test_list_includes_count(self, client, storage):
        skill_id = _create_test_skill(storage)
        storage.create_skill_resource(uuid.uuid4().hex, skill_id, "scripts/a.sh", "a")
        storage.create_skill_resource(uuid.uuid4().hex, skill_id, "scripts/b.sh", "b")
        resp = client.get("/v1/api/admin/skills")
        skills = resp.json()["skills"]
        skill = [s for s in skills if s["template_id"] == skill_id][0]
        assert skill["resource_count"] == 2

    def test_get_includes_count(self, client, storage):
        skill_id = _create_test_skill(storage)
        storage.create_skill_resource(uuid.uuid4().hex, skill_id, "scripts/a.sh", "a")
        resp = client.get(f"/v1/api/admin/skills/{skill_id}")
        assert resp.json()["resource_count"] == 1
