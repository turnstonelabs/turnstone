"""Tests for governance admin API endpoints (roles, orgs, policies, templates, usage, audit)."""

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
    admin_assign_role,
    admin_audit,
    admin_create_policy,
    admin_create_role,
    admin_delete_policy,
    admin_delete_role,
    admin_delete_user,
    admin_get_org,
    admin_list_orgs,
    admin_list_policies,
    admin_list_roles,
    admin_list_user_roles,
    admin_unassign_role,
    admin_update_org,
    admin_update_policy,
    admin_update_role,
    admin_usage,
)
from turnstone.core.auth import AuthResult
from turnstone.core.storage._sqlite import SQLiteBackend

# ---------------------------------------------------------------------------
# Auth bypass middleware — injects a full-access AuthResult on every request.
# ---------------------------------------------------------------------------


class _InjectAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id="test-admin",
            scopes=frozenset({"approve"}),
            token_source="config",
            permissions=frozenset(
                {
                    "read",
                    "write",
                    "approve",
                    "admin.roles",
                    "admin.users",
                    "admin.orgs",
                    "admin.policies",
                    "admin.prompt_policies",
                    "admin.skills",
                    "admin.usage",
                    "admin.audit",
                    "admin.schedules",
                    "admin.watches",
                    "tools.approve",
                    "workstreams.create",
                    "workstreams.close",
                }
            ),
        )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path):
    """Fresh SQLite backend for each test, seeded with test users."""
    backend = SQLiteBackend(str(tmp_path / "test.db"))
    # Seed users required by role assignment tests
    backend.create_user("test-admin", "testadmin", "Test Admin", "hash")
    backend.create_user("user-1", "user1", "User One", "hash")
    return backend


@pytest.fixture
def client(storage):
    """TestClient with storage and auth bypassed."""
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    # Roles
                    Route("/api/admin/roles", admin_list_roles),
                    Route("/api/admin/roles", admin_create_role, methods=["POST"]),
                    Route("/api/admin/roles/{role_id}", admin_update_role, methods=["PUT"]),
                    Route("/api/admin/roles/{role_id}", admin_delete_role, methods=["DELETE"]),
                    # Users
                    Route(
                        "/api/admin/users/{user_id}",
                        admin_delete_user,
                        methods=["DELETE"],
                    ),
                    # User-role assignments
                    Route("/api/admin/users/{user_id}/roles", admin_list_user_roles),
                    Route(
                        "/api/admin/users/{user_id}/roles",
                        admin_assign_role,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/admin/users/{user_id}/roles/{role_id}",
                        admin_unassign_role,
                        methods=["DELETE"],
                    ),
                    # Orgs
                    Route("/api/admin/orgs", admin_list_orgs),
                    Route("/api/admin/orgs/{org_id}", admin_get_org),
                    Route("/api/admin/orgs/{org_id}", admin_update_org, methods=["PUT"]),
                    # Policies
                    Route("/api/admin/policies", admin_list_policies),
                    Route("/api/admin/policies", admin_create_policy, methods=["POST"]),
                    Route(
                        "/api/admin/policies/{policy_id}",
                        admin_update_policy,
                        methods=["PUT"],
                    ),
                    Route(
                        "/api/admin/policies/{policy_id}",
                        admin_delete_policy,
                        methods=["DELETE"],
                    ),
                    # Usage & Audit
                    Route("/api/admin/usage", admin_usage),
                    Route("/api/admin/audit", admin_audit),
                ],
            ),
        ],
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.auth_storage = storage
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _role_payload(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "name": "analyst",
        "display_name": "Data Analyst",
        "permissions": "read,write",
    }
    defaults.update(overrides)
    return defaults


def _policy_payload(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "name": "Allow bash",
        "tool_pattern": "bash_*",
        "action": "allow",
        "priority": 10,
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Tests — Roles
# ---------------------------------------------------------------------------


class TestRoles:
    def test_list_empty(self, client):
        resp = client.get("/v1/api/admin/roles")
        assert resp.status_code == 200
        assert resp.json()["roles"] == []

    def test_create_role(self, client):
        resp = client.post("/v1/api/admin/roles", json=_role_payload())
        assert resp.status_code == 200
        role = resp.json()
        assert role["name"] == "analyst"
        assert role["display_name"] == "Data Analyst"
        assert role["permissions"] == "read,write"
        assert role["builtin"] is False
        assert "role_id" in role
        assert "created" in role

    def test_create_role_missing_name(self, client):
        resp = client.post("/v1/api/admin/roles", json=_role_payload(name=""))
        assert resp.status_code == 400
        assert "name" in resp.json()["error"].lower()

    def test_create_role_invalid_name(self, client):
        resp = client.post("/v1/api/admin/roles", json=_role_payload(name="bad name!@#"))
        assert resp.status_code == 400
        assert "name" in resp.json()["error"].lower()

    def test_create_role_default_display_name(self, client):
        resp = client.post(
            "/v1/api/admin/roles",
            json={"name": "ops", "permissions": ""},
        )
        assert resp.status_code == 200
        role = resp.json()
        # display_name defaults to name when not provided
        assert role["display_name"] == "ops"

    def test_list_after_create(self, client):
        client.post("/v1/api/admin/roles", json=_role_payload())
        resp = client.get("/v1/api/admin/roles")
        assert resp.status_code == 200
        roles = resp.json()["roles"]
        assert len(roles) == 1
        assert roles[0]["name"] == "analyst"

    def test_update_role(self, client):
        create_resp = client.post("/v1/api/admin/roles", json=_role_payload())
        role_id = create_resp.json()["role_id"]

        resp = client.put(
            f"/v1/api/admin/roles/{role_id}",
            json={"display_name": "Senior Analyst", "permissions": "read,write,approve"},
        )
        assert resp.status_code == 200
        role = resp.json()
        assert role["display_name"] == "Senior Analyst"
        assert role["permissions"] == "read,write,approve"

    def test_update_nonexistent_role(self, client):
        resp = client.put(
            "/v1/api/admin/roles/nonexistent",
            json={"display_name": "Nope"},
        )
        assert resp.status_code == 404

    def test_update_builtin_role_rejected(self, client, storage):
        # Seed a builtin role directly via storage
        storage.create_role(
            role_id="builtin-admin",
            name="admin",
            display_name="Administrator",
            permissions="*",
            builtin=True,
        )
        resp = client.put(
            "/v1/api/admin/roles/builtin-admin",
            json={"display_name": "Hacked"},
        )
        assert resp.status_code == 400
        assert "builtin" in resp.json()["error"].lower()

    def test_delete_role(self, client):
        create_resp = client.post("/v1/api/admin/roles", json=_role_payload())
        role_id = create_resp.json()["role_id"]

        resp = client.delete(f"/v1/api/admin/roles/{role_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        # Verify gone from listing
        list_resp = client.get("/v1/api/admin/roles")
        assert list_resp.json()["roles"] == []

    def test_delete_nonexistent_role(self, client):
        resp = client.delete("/v1/api/admin/roles/nonexistent")
        assert resp.status_code == 404

    def test_delete_builtin_role_rejected(self, client, storage):
        storage.create_role(
            role_id="builtin-viewer",
            name="viewer",
            display_name="Viewer",
            permissions="read",
            builtin=True,
        )
        resp = client.delete("/v1/api/admin/roles/builtin-viewer")
        assert resp.status_code == 400
        assert "builtin" in resp.json()["error"].lower()


# ---------------------------------------------------------------------------
# Tests — Role assignments
# ---------------------------------------------------------------------------


class TestRoleAssignments:
    def test_list_user_roles_empty(self, client):
        resp = client.get("/v1/api/admin/users/user-1/roles")
        assert resp.status_code == 200
        assert resp.json()["roles"] == []

    def test_assign_role(self, client):
        create_resp = client.post("/v1/api/admin/roles", json=_role_payload())
        role_id = create_resp.json()["role_id"]

        resp = client.post(
            "/v1/api/admin/users/user-1/roles",
            json={"role_id": role_id},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        # Verify listed
        list_resp = client.get("/v1/api/admin/users/user-1/roles")
        roles = list_resp.json()["roles"]
        assert len(roles) >= 1

    def test_assign_role_missing_role_id(self, client):
        resp = client.post(
            "/v1/api/admin/users/user-1/roles",
            json={},
        )
        assert resp.status_code == 400
        assert "role_id" in resp.json()["error"].lower()

    def test_unassign_role(self, client):
        create_resp = client.post("/v1/api/admin/roles", json=_role_payload())
        role_id = create_resp.json()["role_id"]

        # Assign first
        client.post(
            "/v1/api/admin/users/user-1/roles",
            json={"role_id": role_id},
        )

        # Now unassign
        resp = client.delete(f"/v1/api/admin/users/user-1/roles/{role_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        # Verify removed
        list_resp = client.get("/v1/api/admin/users/user-1/roles")
        assert list_resp.json()["roles"] == []

    def test_unassign_nonexistent(self, client):
        resp = client.delete("/v1/api/admin/users/user-1/roles/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests — Orgs
# ---------------------------------------------------------------------------


class TestOrgs:
    def test_list_empty(self, client):
        resp = client.get("/v1/api/admin/orgs")
        assert resp.status_code == 200
        assert resp.json()["orgs"] == []

    def test_get_org(self, client, storage):
        storage.create_org(
            org_id="org-1",
            name="acme",
            display_name="Acme Corp",
            settings='{"theme": "dark"}',
        )
        resp = client.get("/v1/api/admin/orgs/org-1")
        assert resp.status_code == 200
        org = resp.json()
        assert org["org_id"] == "org-1"
        assert org["name"] == "acme"
        assert org["display_name"] == "Acme Corp"

    def test_get_org_not_found(self, client):
        resp = client.get("/v1/api/admin/orgs/nonexistent")
        assert resp.status_code == 404

    def test_update_org(self, client, storage):
        storage.create_org(org_id="org-1", name="acme", display_name="Acme Corp")

        resp = client.put(
            "/v1/api/admin/orgs/org-1",
            json={"display_name": "Acme Inc.", "settings": '{"theme": "light"}'},
        )
        assert resp.status_code == 200
        org = resp.json()
        assert org["display_name"] == "Acme Inc."
        assert org["settings"] == '{"theme": "light"}'

    def test_update_org_not_found(self, client):
        resp = client.put(
            "/v1/api/admin/orgs/nonexistent",
            json={"display_name": "Nope"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests — Tool policies
# ---------------------------------------------------------------------------


class TestPolicies:
    def test_list_empty(self, client):
        resp = client.get("/v1/api/admin/policies")
        assert resp.status_code == 200
        assert resp.json()["policies"] == []

    def test_create_policy(self, client):
        resp = client.post("/v1/api/admin/policies", json=_policy_payload())
        assert resp.status_code == 200
        policy = resp.json()
        assert policy["name"] == "Allow bash"
        assert policy["tool_pattern"] == "bash_*"
        assert policy["action"] == "allow"
        assert policy["priority"] == 10
        assert "policy_id" in policy
        assert "created" in policy

    def test_create_policy_missing_name(self, client):
        resp = client.post("/v1/api/admin/policies", json=_policy_payload(name=""))
        assert resp.status_code == 400
        assert "name" in resp.json()["error"].lower()

    def test_create_policy_missing_tool_pattern(self, client):
        resp = client.post(
            "/v1/api/admin/policies",
            json=_policy_payload(tool_pattern=""),
        )
        assert resp.status_code == 400
        assert "tool_pattern" in resp.json()["error"].lower()

    def test_create_policy_invalid_action(self, client):
        resp = client.post(
            "/v1/api/admin/policies",
            json=_policy_payload(action="yolo"),
        )
        assert resp.status_code == 400
        assert "action" in resp.json()["error"].lower()

    def test_list_after_create(self, client):
        client.post("/v1/api/admin/policies", json=_policy_payload())
        resp = client.get("/v1/api/admin/policies")
        assert resp.status_code == 200
        policies = resp.json()["policies"]
        assert len(policies) == 1
        assert policies[0]["name"] == "Allow bash"

    def test_update_policy(self, client):
        create_resp = client.post("/v1/api/admin/policies", json=_policy_payload())
        policy_id = create_resp.json()["policy_id"]

        resp = client.put(
            f"/v1/api/admin/policies/{policy_id}",
            json={"name": "Deny bash", "action": "deny", "priority": 20},
        )
        assert resp.status_code == 200
        policy = resp.json()
        assert policy["name"] == "Deny bash"
        assert policy["action"] == "deny"
        assert policy["priority"] == 20

    def test_update_policy_invalid_action(self, client):
        create_resp = client.post("/v1/api/admin/policies", json=_policy_payload())
        policy_id = create_resp.json()["policy_id"]

        resp = client.put(
            f"/v1/api/admin/policies/{policy_id}",
            json={"action": "nope"},
        )
        assert resp.status_code == 400
        assert "action" in resp.json()["error"].lower()

    def test_update_policy_not_found(self, client):
        resp = client.put(
            "/v1/api/admin/policies/nonexistent",
            json={"name": "Nope"},
        )
        assert resp.status_code == 404

    def test_delete_policy(self, client):
        create_resp = client.post("/v1/api/admin/policies", json=_policy_payload())
        policy_id = create_resp.json()["policy_id"]

        resp = client.delete(f"/v1/api/admin/policies/{policy_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        # Verify gone
        list_resp = client.get("/v1/api/admin/policies")
        assert list_resp.json()["policies"] == []

    def test_delete_policy_not_found(self, client):
        resp = client.delete("/v1/api/admin/policies/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests — Usage
# ---------------------------------------------------------------------------


class TestUsage:
    def test_usage_defaults(self, client):
        """Query usage with no params — should return summary and breakdown."""
        resp = client.get("/v1/api/admin/usage")
        assert resp.status_code == 200
        data = resp.json()
        assert "summary" in data
        assert "breakdown" in data
        # Summary is a list with at least one row
        assert isinstance(data["summary"], list)
        assert len(data["summary"]) >= 1
        # All-zeros when no data
        assert data["summary"][0]["prompt_tokens"] == 0

    def test_usage_with_data(self, client, storage):
        """Seed usage events and verify they appear in the query."""
        storage.record_usage_event(
            event_id="evt-1",
            user_id="user-1",
            model="gpt-5",
            prompt_tokens=100,
            completion_tokens=50,
            tool_calls_count=2,
        )
        storage.record_usage_event(
            event_id="evt-2",
            user_id="user-1",
            model="gpt-5",
            prompt_tokens=200,
            completion_tokens=75,
            tool_calls_count=1,
        )
        resp = client.get("/v1/api/admin/usage")
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary[0]["prompt_tokens"] == 300
        assert summary[0]["completion_tokens"] == 125
        assert summary[0]["tool_calls_count"] == 3

    def test_usage_with_filters(self, client, storage):
        storage.record_usage_event(
            event_id="evt-f1",
            user_id="user-a",
            model="gpt-5",
            prompt_tokens=100,
            completion_tokens=10,
        )
        storage.record_usage_event(
            event_id="evt-f2",
            user_id="user-b",
            model="claude-4",
            prompt_tokens=200,
            completion_tokens=20,
        )
        resp = client.get("/v1/api/admin/usage?user_id=user-a")
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary[0]["prompt_tokens"] == 100

        resp2 = client.get("/v1/api/admin/usage?model=claude-4")
        assert resp2.status_code == 200
        summary2 = resp2.json()["summary"]
        assert summary2[0]["prompt_tokens"] == 200


# ---------------------------------------------------------------------------
# Tests — Audit
# ---------------------------------------------------------------------------


class TestAudit:
    def test_audit_empty(self, client):
        resp = client.get("/v1/api/admin/audit")
        assert resp.status_code == 200
        data = resp.json()
        assert data["events"] == []
        assert data["total"] == 0

    def test_audit_populated_by_mutations(self, client):
        """Creating a role should produce an audit event."""
        client.post("/v1/api/admin/roles", json=_role_payload())

        resp = client.get("/v1/api/admin/audit")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        actions = [e["action"] for e in data["events"]]
        assert "role.create" in actions

    def test_audit_filter_by_action(self, client):
        # Create a role and a policy to produce different audit actions
        client.post("/v1/api/admin/roles", json=_role_payload())
        client.post("/v1/api/admin/policies", json=_policy_payload())

        resp = client.get("/v1/api/admin/audit?action=policy.create")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert all(e["action"] == "policy.create" for e in data["events"])

    def test_audit_filter_by_user_id(self, client):
        client.post("/v1/api/admin/roles", json=_role_payload())

        resp = client.get("/v1/api/admin/audit?user_id=test-admin")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert all(e["user_id"] == "test-admin" for e in data["events"])

    def test_audit_pagination(self, client):
        # Create several resources to produce multiple audit events
        for i in range(5):
            client.post(
                "/v1/api/admin/roles",
                json=_role_payload(name=f"role-{i}"),
            )

        resp = client.get("/v1/api/admin/audit?limit=2&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["events"]) == 2
        assert data["total"] >= 5

        resp2 = client.get("/v1/api/admin/audit?limit=2&offset=2")
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert len(data2["events"]) == 2
        # The two pages should not overlap
        ids_page1 = {e["event_id"] for e in data["events"]}
        ids_page2 = {e["event_id"] for e in data2["events"]}
        assert ids_page1.isdisjoint(ids_page2)


# ---------------------------------------------------------------------------
# Tests — User self-deletion guard
# ---------------------------------------------------------------------------


class TestUserSelfDeletion:
    def test_cannot_delete_self(self, client):
        """Admin should not be able to delete their own account."""
        resp = client.delete("/v1/api/admin/users/test-admin")
        assert resp.status_code == 400
        assert "own account" in resp.json()["error"].lower()

    def test_can_delete_other_user(self, client):
        resp = client.delete("/v1/api/admin/users/user-1")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
