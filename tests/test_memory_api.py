"""Tests for memory API endpoints (server + console admin)."""

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
    admin_delete_memory,
    admin_get_memory,
    admin_list_memories,
    admin_search_memories,
)
from turnstone.core.auth import AuthResult
from turnstone.core.storage._sqlite import SQLiteBackend
from turnstone.server import (
    delete_memory_endpoint,
    list_memories,
    save_memory,
    search_memories,
)

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
                    "admin.memories",
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
def server_client(storage):
    """TestClient wired to server memory endpoints."""
    import turnstone.core.storage._registry as reg

    old = reg._storage
    reg._storage = storage
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route("/api/memories", list_memories),
                    Route("/api/memories", save_memory, methods=["POST"]),
                    Route("/api/memories/search", search_memories, methods=["POST"]),
                    Route("/api/memories/{name}", delete_memory_endpoint, methods=["DELETE"]),
                ],
            ),
        ],
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    yield TestClient(app)
    reg._storage = old


@pytest.fixture
def admin_client(storage):
    """TestClient wired to console admin memory endpoints."""
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route("/api/admin/memories", admin_list_memories),
                    Route("/api/admin/memories/search", admin_search_memories),
                    Route("/api/admin/memories/{memory_id}", admin_get_memory),
                    Route(
                        "/api/admin/memories/{memory_id}",
                        admin_delete_memory,
                        methods=["DELETE"],
                    ),
                ],
            ),
        ],
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.auth_storage = storage
    return TestClient(app)


def _seed_memory(storage, name="test_key", content="test content", **kw):
    """Helper to insert a memory directly into storage."""
    import uuid

    mid = kw.pop("memory_id", str(uuid.uuid4()))
    storage.create_structured_memory(
        mid,
        name,
        kw.get("description", ""),
        kw.get("mem_type", "project"),
        kw.get("scope", "global"),
        kw.get("scope_id", ""),
        content,
    )
    return mid


# ===========================================================================
# Server endpoint tests
# ===========================================================================


class TestServerListMemories:
    def test_empty(self, server_client):
        r = server_client.get("/v1/api/memories")
        assert r.status_code == 200
        data = r.json()
        assert data["memories"] == []
        assert data["total"] == 0

    def test_with_data(self, server_client, storage):
        _seed_memory(storage, "key_a", "content a")
        _seed_memory(storage, "key_b", "content b")
        r = server_client.get("/v1/api/memories")
        assert r.status_code == 200
        assert r.json()["total"] == 2

    def test_filter_by_type(self, server_client, storage):
        _seed_memory(storage, "a", "x", mem_type="user")
        _seed_memory(storage, "b", "y", mem_type="project")
        r = server_client.get("/v1/api/memories?type=user")
        assert r.json()["total"] == 1
        assert r.json()["memories"][0]["name"] == "a"

    def test_filter_by_scope(self, server_client, storage):
        _seed_memory(storage, "a", "x", scope="global")
        _seed_memory(storage, "b", "y", scope="workstream", scope_id="ws1")
        r = server_client.get("/v1/api/memories?scope=workstream&scope_id=ws1")
        assert r.json()["total"] == 1
        assert r.json()["memories"][0]["name"] == "b"

    def test_limit(self, server_client, storage):
        for i in range(5):
            _seed_memory(storage, f"k{i}", f"v{i}")
        r = server_client.get("/v1/api/memories?limit=2")
        assert r.json()["total"] == 2

    def test_invalid_limit(self, server_client):
        r = server_client.get("/v1/api/memories?limit=abc")
        assert r.status_code == 400


class TestServerSaveMemory:
    def test_create(self, server_client):
        r = server_client.post(
            "/v1/api/memories",
            json={"name": "my_key", "content": "my content"},
        )
        assert r.status_code == 201
        data = r.json()
        assert data["name"] == "my_key"
        assert data["content"] == "my content"
        assert data["type"] == "project"
        assert data["scope"] == "global"

    def test_upsert(self, server_client):
        server_client.post(
            "/v1/api/memories",
            json={"name": "key", "content": "v1"},
        )
        r = server_client.post(
            "/v1/api/memories",
            json={"name": "key", "content": "v2"},
        )
        assert r.status_code == 200
        assert r.json()["content"] == "v2"

    def test_with_type_and_scope(self, server_client):
        r = server_client.post(
            "/v1/api/memories",
            json={
                "name": "feedback_key",
                "content": "data",
                "type": "feedback",
                "scope": "workstream",
                "scope_id": "ws1",
            },
        )
        assert r.status_code == 201
        assert r.json()["type"] == "feedback"
        assert r.json()["scope"] == "workstream"

    def test_missing_name(self, server_client):
        r = server_client.post("/v1/api/memories", json={"content": "data"})
        assert r.status_code == 400

    def test_missing_content(self, server_client):
        r = server_client.post("/v1/api/memories", json={"name": "k"})
        assert r.status_code == 400

    def test_invalid_type(self, server_client):
        r = server_client.post(
            "/v1/api/memories",
            json={"name": "k", "content": "c", "type": "bogus"},
        )
        assert r.status_code == 400
        assert "invalid type" in r.json()["error"]

    def test_invalid_scope(self, server_client):
        r = server_client.post(
            "/v1/api/memories",
            json={"name": "k", "content": "c", "scope": "bogus"},
        )
        assert r.status_code == 400
        assert "invalid scope" in r.json()["error"]

    def test_content_too_large(self, server_client):
        r = server_client.post(
            "/v1/api/memories",
            json={"name": "k", "content": "x" * 70000},
        )
        assert r.status_code == 400
        assert "limit" in r.json()["error"]

    def test_name_normalisation(self, server_client):
        r = server_client.post(
            "/v1/api/memories",
            json={"name": "My-Key Name", "content": "data"},
        )
        assert r.status_code == 201
        assert r.json()["name"] == "my_key_name"


class TestServerUserScopeSecurity:
    def test_user_scope_binds_to_auth(self, server_client):
        """User scope auto-resolves scope_id from authenticated user."""
        r = server_client.post(
            "/v1/api/memories",
            json={"name": "priv", "content": "secret", "scope": "user"},
        )
        assert r.status_code == 201
        assert r.json()["scope_id"] == "test-user"

    def test_user_scope_rejects_cross_user(self, server_client):
        """Cannot access another user's memories via scope_id."""
        r = server_client.post(
            "/v1/api/memories",
            json={"name": "x", "content": "y", "scope": "user", "scope_id": "other-user"},
        )
        assert r.status_code == 403

    def test_user_scope_allows_own_scope_id(self, server_client):
        """Passing own user_id as scope_id is allowed."""
        r = server_client.post(
            "/v1/api/memories",
            json={"name": "x", "content": "y", "scope": "user", "scope_id": "test-user"},
        )
        assert r.status_code == 201

    def test_list_rejects_cross_user(self, server_client):
        r = server_client.get("/v1/api/memories?scope=user&scope_id=other-user")
        assert r.status_code == 403

    def test_delete_rejects_cross_user(self, server_client, storage):
        _seed_memory(storage, "k", "v", scope="user", scope_id="other-user")
        r = server_client.delete("/v1/api/memories/k?scope=user&scope_id=other-user")
        assert r.status_code == 403


class TestServerSearchMemories:
    def test_search(self, server_client, storage):
        _seed_memory(storage, "db_config", "postgresql host", description="database")
        _seed_memory(storage, "api_key", "secret_value")
        r = server_client.post(
            "/v1/api/memories/search",
            json={"query": "database"},
        )
        assert r.status_code == 200
        assert r.json()["total"] == 1
        assert r.json()["memories"][0]["name"] == "db_config"

    def test_no_results(self, server_client, storage):
        _seed_memory(storage, "a", "b")
        r = server_client.post(
            "/v1/api/memories/search",
            json={"query": "nonexistent_xyz"},
        )
        assert r.status_code == 200
        assert r.json()["total"] == 0

    def test_missing_query(self, server_client):
        r = server_client.post("/v1/api/memories/search", json={})
        assert r.status_code == 400


class TestServerDeleteMemory:
    def test_delete(self, server_client, storage):
        _seed_memory(storage, "doomed")
        r = server_client.delete("/v1/api/memories/doomed")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_not_found(self, server_client):
        r = server_client.delete("/v1/api/memories/nope")
        assert r.status_code == 404

    def test_delete_scoped(self, server_client, storage):
        _seed_memory(storage, "k", "data", scope="workstream", scope_id="ws1")
        # Wrong scope → not found
        r = server_client.delete("/v1/api/memories/k")
        assert r.status_code == 404
        # Correct scope → success
        r = server_client.delete("/v1/api/memories/k?scope=workstream&scope_id=ws1")
        assert r.status_code == 200

    def test_invalid_scope(self, server_client):
        r = server_client.delete("/v1/api/memories/k?scope=bogus")
        assert r.status_code == 400


# ===========================================================================
# Console admin endpoint tests
# ===========================================================================


class TestAdminListMemories:
    def test_empty(self, admin_client):
        r = admin_client.get("/v1/api/admin/memories")
        assert r.status_code == 200
        assert r.json()["memories"] == []

    def test_with_data(self, admin_client, storage):
        _seed_memory(storage, "a", "1")
        _seed_memory(storage, "b", "2")
        r = admin_client.get("/v1/api/admin/memories")
        assert r.json()["total"] == 2

    def test_filter(self, admin_client, storage):
        _seed_memory(storage, "a", "1", mem_type="user")
        _seed_memory(storage, "b", "2", mem_type="project")
        r = admin_client.get("/v1/api/admin/memories?type=user")
        assert r.json()["total"] == 1


class TestAdminSearchMemories:
    def test_search(self, admin_client, storage):
        _seed_memory(storage, "db_config", "pg host", description="database")
        _seed_memory(storage, "other", "unrelated")
        r = admin_client.get("/v1/api/admin/memories/search?q=database")
        assert r.status_code == 200
        assert r.json()["total"] == 1

    def test_missing_query(self, admin_client):
        r = admin_client.get("/v1/api/admin/memories/search")
        assert r.status_code == 400


class TestAdminGetMemory:
    def test_found(self, admin_client, storage):
        mid = _seed_memory(storage, "k", "content")
        r = admin_client.get(f"/v1/api/admin/memories/{mid}")
        assert r.status_code == 200
        assert r.json()["name"] == "k"

    def test_not_found(self, admin_client):
        r = admin_client.get("/v1/api/admin/memories/nonexistent-id")
        assert r.status_code == 404


class TestAdminDeleteMemory:
    def test_delete(self, admin_client, storage):
        mid = _seed_memory(storage, "doomed", "data")
        r = admin_client.delete(f"/v1/api/admin/memories/{mid}")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        # Verify it's gone
        assert storage.get_structured_memory(mid) is None

    def test_not_found(self, admin_client):
        r = admin_client.delete("/v1/api/admin/memories/nonexistent-id")
        assert r.status_code == 404


# ===========================================================================
# Storage: delete_structured_memory_by_id
# ===========================================================================


class TestDeleteByIdStorage:
    def test_delete_existing(self, storage):
        storage.create_structured_memory("m1", "k", "d", "project", "global", "", "data")
        assert storage.delete_structured_memory_by_id("m1")
        assert storage.get_structured_memory("m1") is None

    def test_delete_nonexistent(self, storage):
        assert not storage.delete_structured_memory_by_id("nope")
