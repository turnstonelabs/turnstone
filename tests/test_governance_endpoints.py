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
    admin_role_effective,
    admin_role_overrides,
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
                    Route("/api/admin/roles/{role_id}/effective", admin_role_effective),
                    Route(
                        "/api/admin/roles/{role_id}/overrides",
                        admin_role_overrides,
                        methods=["PUT"],
                    ),
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

    def test_create_role_with_model_skills_write_permission(self, client):
        """``model.skills.write`` is enumerated in ``_VALID_PERMISSIONS`` and
        passes role-create validation.  Catches the case where the constant
        is added on the server but missed by the validator or the constant
        list."""
        resp = client.post(
            "/v1/api/admin/roles",
            json=_role_payload(name="skillwriter", permissions="read,model.skills.write"),
        )
        assert resp.status_code == 200, resp.json()
        assert "model.skills.write" in resp.json()["permissions"]

    def test_create_role_with_persona_permissions(self, client):
        """``persona.{create,read,write}`` (migration 063) are enumerated in
        ``_VALID_PERMISSIONS`` and pass role-create validation.  Before the fix
        they 400'd — a custom role could never carry a persona grant."""
        resp = client.post(
            "/v1/api/admin/roles",
            json=_role_payload(
                name="personaeditor",
                permissions="read,persona.create,persona.read,persona.write",
            ),
        )
        assert resp.status_code == 200, resp.json()
        perms = resp.json()["permissions"]
        for p in ("persona.create", "persona.read", "persona.write"):
            assert p in perms

    def test_permission_sections_js_covers_valid_permissions(self):
        """F-5: ``_PERMISSION_SECTIONS`` in governance.js mirrors
        ``_VALID_PERMISSIONS`` in console/server.py.  A new perm added
        to the Python validator without a matching JS toggle becomes
        silently un-customizable through the admin Roles UI — the only
        documented path for granting/revoking perms on a builtin.
        Catches the same shape that surfaced ``coordinator.trust.send``
        missing from the validator during manual verification of the
        overlay editor (a similar drift, in the opposite direction)."""
        import re
        from pathlib import Path

        from turnstone.console.server import _VALID_PERMISSIONS

        src = Path("turnstone/console/static/governance.js").read_text()
        # _PERMISSION_SECTIONS is a `const X = [...]` containing nested
        # `permissions: ["a", "b", ...]` arrays.  Pull every quoted
        # string out of every permissions: [...] block; we don't need
        # a full JS parser to enumerate the perm names.
        m = re.search(
            r"const _PERMISSION_SECTIONS\s*=\s*\[(.*?)\];",
            src,
            re.DOTALL,
        )
        assert m, "could not locate _PERMISSION_SECTIONS in governance.js"
        body = m.group(1)
        in_ui = set(re.findall(r'"([a-z][a-z._]*)"', body))
        # Exclude the section labels themselves (they're sentence-case
        # like "Scopes", "Admin"; the regex above already excludes them
        # by anchoring on lowercase, but be explicit about intent).
        missing_in_ui = sorted(_VALID_PERMISSIONS - in_ui)
        extra_in_ui = sorted(in_ui - _VALID_PERMISSIONS)
        assert not missing_in_ui, (
            f"perms in _VALID_PERMISSIONS but not _PERMISSION_SECTIONS "
            f"(silently un-customizable in admin UI): {missing_in_ui}"
        )
        assert not extra_in_ui, (
            f"perms in _PERMISSION_SECTIONS but not _VALID_PERMISSIONS "
            f"(toggle would 400 on save): {extra_in_ui}"
        )

    def test_valid_permissions_covers_all_seeded_builtin_perms(self):
        """Every permission migration 008/011/014/015/029/032/033/035/040/042
        adds to a builtin role must be in ``_VALID_PERMISSIONS`` — otherwise
        the overrides editor cannot round-trip the baseline (a perm dropped
        from the toggle universe gets stripped to satisfy the validator,
        producing a silent capability loss).  Caught by the manual
        verification run of feat/builtin-role-overrides:
        ``coordinator.trust.send`` was in the baseline but not the
        validator, so the very first Save through the overrides editor
        400'd."""
        from turnstone.console.server import _VALID_PERMISSIONS

        # Mirror the union the bootstrap migrations write into the baseline
        # ``permissions`` column for builtin-admin.  Keep this in sync with
        # 017_catchup_admin_permissions.py and every subsequent migration
        # that touches builtin-admin.
        seeded = {
            "read",
            "write",
            "approve",
            "admin.users",
            "admin.roles",
            "admin.orgs",
            "admin.policies",
            "admin.prompt_policies",
            "admin.skills",
            "admin.audit",
            "admin.usage",
            "admin.schedules",
            "admin.watches",
            "admin.judge",
            "admin.memories",
            "admin.settings",
            "admin.mcp",
            "admin.models",
            "admin.nodes",
            "admin.coordinator",
            "admin.cluster.inspect",
            "tools.approve",
            "workstreams.create",
            "workstreams.close",
            "conversation.modify",
            "coordinator.trust.send",
        }
        missing = sorted(seeded - _VALID_PERMISSIONS)
        assert not missing, f"perms in baseline but not _VALID_PERMISSIONS: {missing}"

    def test_create_role_rejects_unknown_permission(self, client):
        """Unknown permission strings are rejected — guards the validator
        against typos in the constant list and would-be capability inflation
        via the admin API."""
        resp = client.post(
            "/v1/api/admin/roles",
            json=_role_payload(name="bogus", permissions="read,model.does.not.exist"),
        )
        assert resp.status_code == 400
        assert "invalid" in resp.json()["error"].lower()

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

    def test_update_role_accepts_persona_permissions(self, client):
        """Editing a custom role to carry ``persona.*`` must validate (they were
        rejected before 063 added them to ``_VALID_PERMISSIONS``)."""
        create_resp = client.post("/v1/api/admin/roles", json=_role_payload())
        role_id = create_resp.json()["role_id"]
        resp = client.put(
            f"/v1/api/admin/roles/{role_id}",
            json={"permissions": "read,persona.read,persona.write"},
        )
        assert resp.status_code == 200, resp.json()
        perms = resp.json()["permissions"]
        assert "persona.read" in perms and "persona.write" in perms

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
# Tests — Role permission overrides (builtin customization)
# ---------------------------------------------------------------------------


def _seed_builtin_admin(storage: Any, perms: str = "read,write,admin.roles") -> None:
    storage.create_role(
        role_id="builtin-admin",
        name="admin",
        display_name="Admin",
        permissions=perms,
        builtin=True,
    )
    storage.assign_role("test-admin", "builtin-admin")


class TestRoleOverrides:
    def test_effective_returns_baseline_when_no_overrides(self, client, storage):
        _seed_builtin_admin(storage, "read,admin.roles")
        resp = client.get("/v1/api/admin/roles/builtin-admin/effective")
        assert resp.status_code == 200
        body = resp.json()
        assert body["baseline"] == ["admin.roles", "read"]
        assert body["grants"] == []
        assert body["revokes"] == []
        assert body["effective"] == ["admin.roles", "read"]

    def test_effective_404_unknown_role(self, client):
        resp = client.get("/v1/api/admin/roles/nope/effective")
        assert resp.status_code == 404

    def test_overrides_grant_skills_write(self, client, storage):
        # The motivating case: model.skills.write is default-ungranted,
        # operator opts in via the overrides endpoint.
        _seed_builtin_admin(storage, "read,write,admin.roles")
        resp = client.put(
            "/v1/api/admin/roles/builtin-admin/overrides",
            json={"grant": ["model.skills.write"], "revoke": []},
        )
        assert resp.status_code == 200, resp.json()
        body = resp.json()
        assert "model.skills.write" in body["effective"]
        assert body["grants"] == ["model.skills.write"]

    def test_overrides_grant_persona_write(self, client, storage):
        # persona.write is admin-default (063) but grantable to any builtin
        # role via the overrides layer — the endpoint must accept it, not 400
        # it as an unknown permission.
        _seed_builtin_admin(storage, "read,write,admin.roles")
        resp = client.put(
            "/v1/api/admin/roles/builtin-admin/overrides",
            json={"grant": ["persona.write"], "revoke": []},
        )
        assert resp.status_code == 200, resp.json()
        body = resp.json()
        assert "persona.write" in body["effective"]
        assert body["grants"] == ["persona.write"]

    def test_overrides_replace_semantics(self, client, storage):
        _seed_builtin_admin(storage, "read,write,admin.roles")
        client.put(
            "/v1/api/admin/roles/builtin-admin/overrides",
            json={"grant": ["model.skills.write"], "revoke": []},
        )
        # PUT replaces — the prior grant should be gone after sending an
        # empty body, leaving only the new revoke (which IS in baseline).
        resp = client.put(
            "/v1/api/admin/roles/builtin-admin/overrides",
            json={"grant": [], "revoke": ["write"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["grants"] == []
        assert body["revokes"] == ["write"]
        assert "model.skills.write" not in body["effective"]

    def test_overrides_invalid_permission_rejected(self, client, storage):
        _seed_builtin_admin(storage)
        resp = client.put(
            "/v1/api/admin/roles/builtin-admin/overrides",
            json={"grant": ["totally.fake.perm"], "revoke": []},
        )
        assert resp.status_code == 400
        assert "invalid" in resp.json()["error"].lower()

    def test_overrides_disjoint_grant_revoke_rejected(self, client, storage):
        _seed_builtin_admin(storage)
        resp = client.put(
            "/v1/api/admin/roles/builtin-admin/overrides",
            json={"grant": ["approve"], "revoke": ["approve"]},
        )
        assert resp.status_code == 400

    def test_overrides_non_builtin_rejected(self, client, storage):
        storage.create_role(
            role_id="custom-1",
            name="custom",
            display_name="Custom",
            permissions="read",
            builtin=False,
        )
        resp = client.put(
            "/v1/api/admin/roles/custom-1/overrides",
            json={"grant": ["write"], "revoke": []},
        )
        assert resp.status_code == 400
        assert "builtin" in resp.json()["error"].lower()

    def test_overrides_no_op_grant_and_revoke_normalize(self, client, storage):
        # A grant of a perm already in baseline AND a revoke of a perm not
        # in baseline both have zero behavioural effect; the endpoint
        # strips them rather than persisting redundant rows.
        _seed_builtin_admin(storage, "read,write,admin.roles")
        resp = client.put(
            "/v1/api/admin/roles/builtin-admin/overrides",
            json={
                "grant": ["read", "model.skills.write"],
                "revoke": ["tools.approve"],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        # Only the meaningful delta survived.
        assert body["grants"] == ["model.skills.write"]
        assert body["revokes"] == []

    def test_overrides_lockout_guard_blocks_last_admin_revoke(self, client, storage):
        _seed_builtin_admin(storage, "read,admin.roles")
        resp = client.put(
            "/v1/api/admin/roles/builtin-admin/overrides",
            json={"grant": [], "revoke": ["admin.roles"]},
        )
        assert resp.status_code == 409
        assert "admin.roles" in resp.json()["error"]
        # Verify the override was NOT applied — the user must still be admin.
        assert "admin.roles" in storage.get_user_permissions("test-admin")

    def test_overrides_lockout_guard_permits_revoke_when_other_admin_exists(self, client, storage):
        _seed_builtin_admin(storage, "read,admin.roles")
        # Second role on a different user that also carries admin.roles —
        # revoking from builtin-admin no longer locks the deployment out.
        storage.create_role(
            role_id="custom-admin",
            name="custom-admin",
            display_name="Custom Admin",
            permissions="read,admin.roles",
            builtin=False,
        )
        storage.assign_role("user-1", "custom-admin")
        resp = client.put(
            "/v1/api/admin/roles/builtin-admin/overrides",
            json={"grant": [], "revoke": ["admin.roles"]},
        )
        assert resp.status_code == 200

    def test_list_roles_includes_overlay_fields(self, client, storage):
        _seed_builtin_admin(storage, "read,admin.roles")
        client.put(
            "/v1/api/admin/roles/builtin-admin/overrides",
            json={"grant": ["model.skills.write"], "revoke": []},
        )
        resp = client.get("/v1/api/admin/roles")
        roles = resp.json()["roles"]
        # Find builtin-admin in the listing
        row = next(r for r in roles if r["role_id"] == "builtin-admin")
        assert row["grants"] == ["model.skills.write"]
        assert row["revokes"] == []
        assert "model.skills.write" in row["effective"]

    def test_overrides_lockout_guard_blocks_grant_removal(self, client, storage):
        # F-1: PUT-replace semantics mean an existing grant of admin.roles
        # on a role whose baseline lacks it is silently dropped when the
        # new payload omits it.  Old guard only fired on explicit revokes
        # and missed this path entirely — concrete cluster-bricking scenario.
        # Setup: only builtin-operator users hold admin.roles, via overlay grant.
        storage.create_role(
            role_id="builtin-operator",
            name="operator",
            display_name="Operator",
            permissions="read,write",  # baseline lacks admin.roles
            builtin=True,
        )
        # Grant admin.roles to operator via overlay, then unassign builtin-admin
        # from the test user so operator is the only path to admin.roles.
        storage.set_role_overrides("builtin-operator", {"admin.roles"}, set())
        storage.assign_role("test-admin", "builtin-operator")
        # The test-admin user keeps builtin-admin assigned by _seed_builtin_admin
        # which would normally hold admin.roles — but we seed without it so the
        # only source is the overlay on builtin-operator.
        if storage.get_role("builtin-admin") is None:
            storage.create_role(
                role_id="builtin-admin",
                name="admin",
                display_name="Admin",
                permissions="read,write",  # baseline lacks admin.roles
                builtin=True,
            )
            storage.assign_role("test-admin", "builtin-admin")
        # Sanity: admin.roles only reachable via operator's overlay
        assert "admin.roles" in storage.get_user_permissions("test-admin")
        # The lockout-triggering call: Reset operator's overrides (drops
        # the admin.roles grant).  Old guard short-circuited because
        # revoke=[] doesn't contain "admin.roles"; new guard simulates
        # the post-PUT effective set on the target role.
        resp = client.put(
            "/v1/api/admin/roles/builtin-operator/overrides",
            json={"grant": [], "revoke": []},
        )
        assert resp.status_code == 409, resp.json()
        assert "admin.roles" in resp.json()["error"]
        # Override was NOT applied — admin.roles still reachable.
        assert "admin.roles" in storage.get_user_permissions("test-admin")

    def test_assign_role_blocks_escalation_via_overlay_grant(self, storage, client):
        # F-2 reframed.  Simulates the attack path where a previous
        # admin.roles holder injected an overlay grant on a builtin
        # role, then a separate admin.users holder (who does NOT hold
        # the granted perm) tries to assign that role to a new user.
        # Without this fix the assign-time subset check would read the
        # baseline column and miss the overlay, silently escalating
        # the assignee.
        #
        # Operator's baseline is unchanged production default
        # ("read,write" — no model.skills.write).  The overlay grant
        # below is the simulated attack step, not the system default.
        _seed_builtin_admin(storage, "read,write,admin.roles,admin.users")
        storage.create_role(
            role_id="builtin-operator",
            name="operator",
            display_name="Operator",
            permissions="read,write",  # production default
            builtin=True,
        )
        storage.set_role_overrides(
            "builtin-operator", {"model.skills.write"}, set()
        )  # simulated prior poisoning by an admin.roles holder

        # The harness AuthResult holds admin.roles + admin.users + many
        # admin.* perms but NOT model.skills.write.  Assigning operator
        # — whose POST-OVERLAY effective set in this test scenario
        # contains model.skills.write — must 403, because the assignee
        # would otherwise gain a perm the assigner doesn't hold.
        resp = client.post(
            "/v1/api/admin/users/user-1/roles",
            json={"role_id": "builtin-operator"},
        )
        assert resp.status_code == 403
        assert "permissions you do not hold" in resp.json()["error"]


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
