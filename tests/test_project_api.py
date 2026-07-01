"""Tests for the project HTTP endpoints (server-side CRUD).

Exercises the owner happy-path through a Starlette TestClient with an auth
middleware that injects the ``project.*`` capabilities (the per-project ACL +
RBAC composition itself is unit-tested in ``test_project_storage.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from turnstone.core.auth import AuthResult
from turnstone.core.storage._sqlite import SQLiteBackend
from turnstone.server import (
    add_project_member_endpoint,
    create_project,
    delete_project_endpoint,
    get_project_endpoint,
    list_project_members_endpoint,
    list_projects,
    project_resources_endpoint,
    remove_project_member_endpoint,
    update_project_endpoint,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from starlette.requests import Request
    from starlette.responses import Response

_PERMS = frozenset(
    {
        "read",
        "write",
        "approve",
        "project.create",
        "project.read",
        "project.write",
        "project.delete",
    }
)


class _InjectAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id="alice",
            scopes=frozenset({"approve"}),
            token_source="config",
            permissions=_PERMS,
        )
        response: Response = await call_next(request)
        return response


@pytest.fixture
def storage(tmp_path: Path) -> SQLiteBackend:
    return SQLiteBackend(str(tmp_path / "test.db"))


@pytest.fixture
def client(storage: SQLiteBackend) -> Iterator[TestClient]:
    import turnstone.core.storage._registry as reg

    old = reg._storage
    reg._storage = storage
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route("/api/projects", list_projects),
                    Route("/api/projects", create_project, methods=["POST"]),
                    Route("/api/projects/{project_id}", get_project_endpoint),
                    Route(
                        "/api/projects/{project_id}",
                        update_project_endpoint,
                        methods=["PATCH"],
                    ),
                    Route(
                        "/api/projects/{project_id}",
                        delete_project_endpoint,
                        methods=["DELETE"],
                    ),
                    Route(
                        "/api/projects/{project_id}/members",
                        list_project_members_endpoint,
                    ),
                    Route(
                        "/api/projects/{project_id}/members",
                        add_project_member_endpoint,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/projects/{project_id}/members/{user_id}",
                        remove_project_member_endpoint,
                        methods=["DELETE"],
                    ),
                    Route(
                        "/api/projects/{project_id}/resources",
                        project_resources_endpoint,
                    ),
                ],
            ),
        ],
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    yield TestClient(app)
    reg._storage = old


class TestProjectApi:
    def test_create_list_get(self, client: TestClient) -> None:
        r = client.post("/v1/api/projects", json={"name": "Research"})
        assert r.status_code == 201
        pid = r.json()["project_id"]
        assert r.json()["name"] == "Research"
        assert r.json()["owner_id"] == "alice"
        assert r.json()["visibility"] == "private"

        r = client.get("/v1/api/projects")
        assert r.status_code == 200
        assert pid in {p["project_id"] for p in r.json()["projects"]}

        r = client.get(f"/v1/api/projects/{pid}")
        assert r.status_code == 200
        assert r.json()["name"] == "Research"

    def test_create_requires_name(self, client: TestClient) -> None:
        r = client.post("/v1/api/projects", json={})
        assert r.status_code == 400

    def test_create_rejects_bad_visibility(self, client: TestClient) -> None:
        r = client.post("/v1/api/projects", json={"name": "X", "visibility": "bogus"})
        assert r.status_code == 400

    def test_update_rename_and_archive(self, client: TestClient) -> None:
        pid = client.post("/v1/api/projects", json={"name": "A"}).json()["project_id"]
        r = client.patch(f"/v1/api/projects/{pid}", json={"name": "B", "state": "archived"})
        assert r.status_code == 200
        assert r.json()["name"] == "B"
        assert r.json()["state"] == "archived"
        # Archived projects drop out of the default list...
        r = client.get("/v1/api/projects")
        assert pid not in {p["project_id"] for p in r.json()["projects"]}
        # ...but appear with include_archived.
        r = client.get("/v1/api/projects?include_archived=1")
        assert pid in {p["project_id"] for p in r.json()["projects"]}

    def test_visibility_change_is_owner_only(
        self, client: TestClient, storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The ACL's capability check reads from storage (not the injected
        # AuthResult); grant it so this test isolates the owner-vs-member gate.
        from turnstone.core import auth

        monkeypatch.setattr(auth, "user_has_permission", lambda *a, **k: True)
        # Alice owns this one → she may flip visibility.
        pid = client.post("/v1/api/projects", json={"name": "Mine"}).json()["project_id"]
        r = client.patch(f"/v1/api/projects/{pid}", json={"visibility": "public"})
        assert r.status_code == 200
        assert r.json()["visibility"] == "public"
        # Bob owns this one; alice is a write-tier member → may rename, but NOT
        # flip visibility (a confidentiality lever the owner did not delegate).
        storage.create_project("bobproj", "Bob's", "bob")
        storage.add_project_member("bobproj", "alice")
        r = client.patch("/v1/api/projects/bobproj", json={"name": "Renamed"})
        assert r.status_code == 200
        r = client.patch("/v1/api/projects/bobproj", json={"visibility": "public"})
        assert r.status_code == 403

    def test_members_add_list_remove(self, client: TestClient) -> None:
        pid = client.post("/v1/api/projects", json={"name": "A"}).json()["project_id"]
        r = client.post(f"/v1/api/projects/{pid}/members", json={"user_id": "bob"})
        assert r.status_code == 200
        assert "bob" in r.json()["members"]
        r = client.get(f"/v1/api/projects/{pid}/members")
        assert r.json()["members"] == ["bob"]
        r = client.delete(f"/v1/api/projects/{pid}/members/bob")
        assert r.status_code == 200
        assert r.json()["members"] == []

    def test_delete(self, client: TestClient) -> None:
        pid = client.post("/v1/api/projects", json={"name": "A"}).json()["project_id"]
        r = client.delete(f"/v1/api/projects/{pid}")
        assert r.status_code == 200
        r = client.get(f"/v1/api/projects/{pid}")
        assert r.status_code == 404

    def test_get_missing_404(self, client: TestClient) -> None:
        r = client.get("/v1/api/projects/nope")
        assert r.status_code == 404


class TestProjectResources:
    def _seed(self, client: TestClient, storage: SQLiteBackend) -> str:
        pid: str = client.post("/v1/api/projects", json={"name": "R"}).json()["project_id"]
        storage.register_workstream("ws-a", name="alpha", user_id="alice", project_id=pid)
        storage.register_workstream("ws-b", name="beta", user_id="alice", project_id=pid)
        storage.register_workstream("ws-x", name="other", user_id="alice")
        mid = storage.save_message("ws-a", "user", "see attached")
        storage.save_attachment("a" * 64, "notes.txt", "text/plain", 5, "text", b"hello")
        storage.set_message_attachments("ws-a", mid, ["a" * 64])
        storage.create_structured_memory("m1", "fact", "d", "general", "project", pid, "body")
        return pid

    def test_resources_aggregate(self, client: TestClient, storage: SQLiteBackend) -> None:
        pid = self._seed(client, storage)
        r = client.get(f"/v1/api/projects/{pid}/resources")
        assert r.status_code == 200
        body = r.json()
        assert body["project_id"] == pid
        assert body["name"] == "R"
        ws_ids = [w["ws_id"] for w in body["workstreams"]]
        assert set(ws_ids) == {"ws-a", "ws-b"}  # ws-x is not in the project
        atts = body["attachments"]
        assert len(atts) == 1
        assert atts[0]["attachment_id"] == "a" * 64
        assert atts[0]["filename"] == "notes.txt"
        assert atts[0]["ws_id"] == "ws-a"
        assert "content" not in atts[0]  # metadata only — never the blob
        assert body["memory_count"] == 1

    def test_resources_empty_project(self, client: TestClient) -> None:
        pid = client.post("/v1/api/projects", json={"name": "E"}).json()["project_id"]
        body = client.get(f"/v1/api/projects/{pid}/resources").json()
        assert body["workstreams"] == []
        assert body["attachments"] == []
        assert body["memory_count"] == 0

    def test_resources_missing_404(self, client: TestClient) -> None:
        assert client.get("/v1/api/projects/nope/resources").status_code == 404

    def test_resources_private_non_member_403(
        self, client: TestClient, storage: SQLiteBackend
    ) -> None:
        # Owned by someone else, private — alice holds project.read but no
        # membership, so the per-project ACL denies.
        storage.create_project("p-zed", "Z", "zed")
        assert client.get("/v1/api/projects/p-zed/resources").status_code == 403
