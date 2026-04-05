"""Integration tests for SDK governance methods against a real Starlette app.

Verifies round-trip serialization: SDK -> HTTP -> Starlette handler -> storage
-> JSON response -> Pydantic model validation in the SDK client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount, Route

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

from turnstone.api.console_schemas import (
    ListOrgsResponse,
    ListRolesResponse,
    ListToolPoliciesResponse,
    OrgInfo,
    RoleInfo,
    ToolPolicyInfo,
)
from turnstone.api.schemas import StatusResponse
from turnstone.console.server import (
    admin_create_policy,
    admin_create_role,
    admin_delete_policy,
    admin_delete_role,
    admin_get_org,
    admin_list_orgs,
    admin_list_policies,
    admin_list_roles,
    admin_update_org,
    admin_update_policy,
    admin_update_role,
)
from turnstone.core.auth import AuthResult
from turnstone.core.storage._sqlite import SQLiteBackend
from turnstone.sdk.console import AsyncTurnstoneConsole

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
                    "admin.orgs",
                    "admin.policies",
                    "admin.prompt_policies",
                }
            ),
        )
        resp: Response = await call_next(request)
        return resp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_app() -> Starlette:
    return Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    # Roles
                    Route("/api/admin/roles", admin_list_roles),
                    Route("/api/admin/roles", admin_create_role, methods=["POST"]),
                    Route("/api/admin/roles/{role_id}", admin_update_role, methods=["PUT"]),
                    Route("/api/admin/roles/{role_id}", admin_delete_role, methods=["DELETE"]),
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
                ],
            ),
        ],
        middleware=[Middleware(_InjectAuthMiddleware)],
    )


@pytest.fixture
def storage(tmp_path: Any) -> SQLiteBackend:
    return SQLiteBackend(str(tmp_path / "test.db"))


@pytest.fixture
async def sdk_client(storage: SQLiteBackend):
    """SDK client wired to a real Starlette app via ASGITransport."""
    app = _make_app()
    app.state.auth_storage = storage
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as hc:
        yield AsyncTurnstoneConsole(httpx_client=hc)


# ---------------------------------------------------------------------------
# Tests — Roles round-trip
# ---------------------------------------------------------------------------


class TestRolesRoundTrip:
    @pytest.mark.anyio
    async def test_list_roles_empty(self, sdk_client: AsyncTurnstoneConsole) -> None:
        resp = await sdk_client.list_roles()
        assert isinstance(resp, ListRolesResponse)
        assert resp.roles == []

    @pytest.mark.anyio
    async def test_create_and_list_role(self, sdk_client: AsyncTurnstoneConsole) -> None:
        role = await sdk_client.create_role(
            "analyst", display_name="Data Analyst", permissions="read,write"
        )
        assert isinstance(role, RoleInfo)
        assert role.name == "analyst"
        assert role.display_name == "Data Analyst"
        assert role.permissions == "read,write"
        assert role.builtin is False
        assert role.role_id  # non-empty

        # List should now contain the new role
        resp = await sdk_client.list_roles()
        assert len(resp.roles) == 1
        assert resp.roles[0].role_id == role.role_id
        assert resp.roles[0].name == "analyst"

    @pytest.mark.anyio
    async def test_create_update_role(self, sdk_client: AsyncTurnstoneConsole) -> None:
        role = await sdk_client.create_role("ops", permissions="read")
        assert role.permissions == "read"

        updated = await sdk_client.update_role(
            role.role_id, display_name="Operations", permissions="read,write,approve"
        )
        assert isinstance(updated, RoleInfo)
        assert updated.display_name == "Operations"
        assert updated.permissions == "read,write,approve"
        assert updated.role_id == role.role_id

    @pytest.mark.anyio
    async def test_create_delete_role(self, sdk_client: AsyncTurnstoneConsole) -> None:
        role = await sdk_client.create_role("temp-role", permissions="read")

        result = await sdk_client.delete_role(role.role_id)
        assert isinstance(result, StatusResponse)
        assert result.status == "ok"

        # Verify gone
        resp = await sdk_client.list_roles()
        assert resp.roles == []

    @pytest.mark.anyio
    async def test_full_lifecycle(self, sdk_client: AsyncTurnstoneConsole) -> None:
        """Create -> list -> update -> list -> delete -> list."""
        # Create
        role = await sdk_client.create_role(
            "lifecycle", display_name="Lifecycle", permissions="read"
        )
        role_id = role.role_id

        # List confirms creation
        roles = (await sdk_client.list_roles()).roles
        assert len(roles) == 1
        assert roles[0].role_id == role_id

        # Update
        updated = await sdk_client.update_role(role_id, permissions="read,write")
        assert updated.permissions == "read,write"

        # List still has one
        roles = (await sdk_client.list_roles()).roles
        assert len(roles) == 1
        assert roles[0].permissions == "read,write"

        # Delete
        await sdk_client.delete_role(role_id)

        # List is empty
        roles = (await sdk_client.list_roles()).roles
        assert roles == []

    @pytest.mark.anyio
    async def test_delete_nonexistent_role_raises(self, sdk_client: AsyncTurnstoneConsole) -> None:
        from turnstone.sdk._types import TurnstoneAPIError

        with pytest.raises(TurnstoneAPIError) as exc_info:
            await sdk_client.delete_role("nonexistent")
        assert exc_info.value.status_code == 404

    @pytest.mark.anyio
    async def test_update_nonexistent_role_raises(self, sdk_client: AsyncTurnstoneConsole) -> None:
        from turnstone.sdk._types import TurnstoneAPIError

        with pytest.raises(TurnstoneAPIError) as exc_info:
            await sdk_client.update_role("nonexistent", display_name="Nope")
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Tests — Policies round-trip
# ---------------------------------------------------------------------------


class TestPoliciesRoundTrip:
    @pytest.mark.anyio
    async def test_list_policies_empty(self, sdk_client: AsyncTurnstoneConsole) -> None:
        resp = await sdk_client.list_policies()
        assert isinstance(resp, ListToolPoliciesResponse)
        assert resp.policies == []

    @pytest.mark.anyio
    async def test_create_and_list_policy(self, sdk_client: AsyncTurnstoneConsole) -> None:
        policy = await sdk_client.create_policy("Allow bash", "bash_*", "allow", priority=10)
        assert isinstance(policy, ToolPolicyInfo)
        assert policy.name == "Allow bash"
        assert policy.tool_pattern == "bash_*"
        assert policy.action == "allow"
        assert policy.priority == 10
        assert policy.enabled is True
        assert policy.policy_id  # non-empty

        resp = await sdk_client.list_policies()
        assert len(resp.policies) == 1
        assert resp.policies[0].policy_id == policy.policy_id

    @pytest.mark.anyio
    async def test_create_update_policy(self, sdk_client: AsyncTurnstoneConsole) -> None:
        policy = await sdk_client.create_policy("Deny write", "write_*", "deny", priority=5)

        updated = await sdk_client.update_policy(
            policy.policy_id, name="Allow write", action="allow", priority=20
        )
        assert isinstance(updated, ToolPolicyInfo)
        assert updated.name == "Allow write"
        assert updated.action == "allow"
        assert updated.priority == 20
        assert updated.policy_id == policy.policy_id

    @pytest.mark.anyio
    async def test_create_delete_policy(self, sdk_client: AsyncTurnstoneConsole) -> None:
        policy = await sdk_client.create_policy("Temp policy", "temp_*", "ask")

        result = await sdk_client.delete_policy(policy.policy_id)
        assert isinstance(result, StatusResponse)
        assert result.status == "ok"

        resp = await sdk_client.list_policies()
        assert resp.policies == []

    @pytest.mark.anyio
    async def test_full_lifecycle(self, sdk_client: AsyncTurnstoneConsole) -> None:
        """Create -> list -> update -> list -> delete -> list."""
        policy = await sdk_client.create_policy("Lifecycle", "test_*", "deny", priority=1)
        pid = policy.policy_id

        policies = (await sdk_client.list_policies()).policies
        assert len(policies) == 1

        await sdk_client.update_policy(pid, action="allow", priority=99)
        policies = (await sdk_client.list_policies()).policies
        assert policies[0].action == "allow"
        assert policies[0].priority == 99

        await sdk_client.delete_policy(pid)
        policies = (await sdk_client.list_policies()).policies
        assert policies == []

    @pytest.mark.anyio
    async def test_delete_nonexistent_policy_raises(
        self, sdk_client: AsyncTurnstoneConsole
    ) -> None:
        from turnstone.sdk._types import TurnstoneAPIError

        with pytest.raises(TurnstoneAPIError) as exc_info:
            await sdk_client.delete_policy("nonexistent")
        assert exc_info.value.status_code == 404

    @pytest.mark.anyio
    async def test_update_nonexistent_policy_raises(
        self, sdk_client: AsyncTurnstoneConsole
    ) -> None:
        from turnstone.sdk._types import TurnstoneAPIError

        with pytest.raises(TurnstoneAPIError) as exc_info:
            await sdk_client.update_policy("nonexistent", name="Nope")
        assert exc_info.value.status_code == 404

    @pytest.mark.anyio
    async def test_create_policy_invalid_action_raises(
        self, sdk_client: AsyncTurnstoneConsole
    ) -> None:
        from turnstone.sdk._types import TurnstoneAPIError

        with pytest.raises(TurnstoneAPIError) as exc_info:
            await sdk_client.create_policy("Bad", "tool_*", "yolo")
        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# Tests — Orgs round-trip
# ---------------------------------------------------------------------------


class TestOrgsRoundTrip:
    @pytest.mark.anyio
    async def test_list_orgs_empty(self, sdk_client: AsyncTurnstoneConsole) -> None:
        resp = await sdk_client.list_orgs()
        assert isinstance(resp, ListOrgsResponse)
        assert resp.orgs == []

    @pytest.mark.anyio
    async def test_get_org(self, sdk_client: AsyncTurnstoneConsole, storage: SQLiteBackend) -> None:
        storage.create_org(
            org_id="org-1", name="acme", display_name="Acme Corp", settings='{"k": "v"}'
        )
        org = await sdk_client.get_org("org-1")
        assert isinstance(org, OrgInfo)
        assert org.org_id == "org-1"
        assert org.name == "acme"
        assert org.display_name == "Acme Corp"
        assert org.settings == '{"k": "v"}'

    @pytest.mark.anyio
    async def test_list_orgs_after_seed(
        self, sdk_client: AsyncTurnstoneConsole, storage: SQLiteBackend
    ) -> None:
        storage.create_org(org_id="org-a", name="alpha", display_name="Alpha")
        storage.create_org(org_id="org-b", name="beta", display_name="Beta")

        resp = await sdk_client.list_orgs()
        assert len(resp.orgs) == 2
        names = {o.name for o in resp.orgs}
        assert names == {"alpha", "beta"}

    @pytest.mark.anyio
    async def test_update_org(
        self, sdk_client: AsyncTurnstoneConsole, storage: SQLiteBackend
    ) -> None:
        storage.create_org(org_id="org-1", name="acme", display_name="Acme Corp")

        updated = await sdk_client.update_org("org-1", display_name="Acme Inc.")
        assert isinstance(updated, OrgInfo)
        assert updated.display_name == "Acme Inc."
        assert updated.org_id == "org-1"

    @pytest.mark.anyio
    async def test_get_nonexistent_org_raises(self, sdk_client: AsyncTurnstoneConsole) -> None:
        from turnstone.sdk._types import TurnstoneAPIError

        with pytest.raises(TurnstoneAPIError) as exc_info:
            await sdk_client.get_org("nonexistent")
        assert exc_info.value.status_code == 404

    @pytest.mark.anyio
    async def test_update_nonexistent_org_raises(self, sdk_client: AsyncTurnstoneConsole) -> None:
        from turnstone.sdk._types import TurnstoneAPIError

        with pytest.raises(TurnstoneAPIError) as exc_info:
            await sdk_client.update_org("nonexistent", display_name="Nope")
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Tests — Pydantic model field validation
# ---------------------------------------------------------------------------


class TestModelValidation:
    """Verify that all expected fields are populated and correctly typed."""

    @pytest.mark.anyio
    async def test_role_info_fields(self, sdk_client: AsyncTurnstoneConsole) -> None:
        role = await sdk_client.create_role("reviewer", permissions="read")
        assert isinstance(role.role_id, str)
        assert isinstance(role.name, str)
        assert isinstance(role.display_name, str)
        assert isinstance(role.permissions, str)
        assert isinstance(role.builtin, bool)
        assert isinstance(role.org_id, str)
        assert isinstance(role.created, str)
        assert isinstance(role.updated, str)

    @pytest.mark.anyio
    async def test_policy_info_fields(self, sdk_client: AsyncTurnstoneConsole) -> None:
        policy = await sdk_client.create_policy("Test", "read_*", "allow", priority=5)
        assert isinstance(policy.policy_id, str)
        assert isinstance(policy.name, str)
        assert isinstance(policy.tool_pattern, str)
        assert isinstance(policy.action, str)
        assert isinstance(policy.priority, int)
        assert isinstance(policy.org_id, str)
        assert isinstance(policy.enabled, bool)
        assert isinstance(policy.created_by, str)
        assert isinstance(policy.created, str)
        assert isinstance(policy.updated, str)

    @pytest.mark.anyio
    async def test_org_info_fields(
        self, sdk_client: AsyncTurnstoneConsole, storage: SQLiteBackend
    ) -> None:
        storage.create_org(org_id="org-v", name="validate", display_name="Validate")
        org = await sdk_client.get_org("org-v")
        assert isinstance(org.org_id, str)
        assert isinstance(org.name, str)
        assert isinstance(org.display_name, str)
        assert isinstance(org.settings, str)
        assert isinstance(org.created, str)
        assert isinstance(org.updated, str)
