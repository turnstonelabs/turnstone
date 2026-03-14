"""Typed HTTP clients for the turnstone console API.

Usage::

    from turnstone.sdk import TurnstoneConsole

    with TurnstoneConsole("http://localhost:8081", token="tok_xxx") as client:
        overview = client.overview()
        print(f"Nodes: {overview.nodes}, Workstreams: {overview.workstreams}")
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from turnstone.api.console_schemas import (
    AdminMemoryInfo,
    ClusterNodesResponse,
    ClusterOverviewResponse,
    ClusterSnapshotResponse,
    ClusterWorkstreamsResponse,
    ConsoleCreateWsResponse,
    ConsoleHealthResponse,
    ListAdminMemoriesResponse,
    ListAuditEventsResponse,
    ListOrgsResponse,
    ListPromptTemplatesResponse,
    ListRolesResponse,
    ListToolPoliciesResponse,
    ListUserRolesResponse,
    ListWsTemplatesResponse,
    ListWsTemplateVersionsResponse,
    NodeDetailResponse,
    OrgInfo,
    PromptTemplateInfo,
    RoleInfo,
    ToolPolicyInfo,
    UsageResponse,
    WsTemplateInfo,
)
from turnstone.api.schemas import (
    AuthLoginResponse,
    AuthSetupResponse,
    AuthStatusResponse,
    ListScheduleRunsResponse,
    ListSchedulesResponse,
    ScheduleInfo,
    StatusResponse,
)
from turnstone.sdk._base import _BaseClient
from turnstone.sdk._sync import _SyncRunner
from turnstone.sdk.events import ClusterEvent

_UNSET: Any = object()

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    import httpx


class AsyncTurnstoneConsole(_BaseClient):
    """Async client for the turnstone console API."""

    def __init__(
        self,
        base_url: str = "http://localhost:8081",
        token: str = "",
        timeout: float = 30.0,
        httpx_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(base_url=base_url, token=token, timeout=timeout, httpx_client=httpx_client)

    # -- cluster overview ----------------------------------------------------

    async def overview(self) -> ClusterOverviewResponse:
        return await self._request(
            "GET", "/v1/api/cluster/overview", response_model=ClusterOverviewResponse
        )

    async def nodes(
        self,
        *,
        sort: str = "activity",
        limit: int = 100,
        offset: int = 0,
    ) -> ClusterNodesResponse:
        params: dict[str, Any] = {"sort": sort, "limit": limit, "offset": offset}
        return await self._request(
            "GET", "/v1/api/cluster/nodes", params=params, response_model=ClusterNodesResponse
        )

    async def workstreams(
        self,
        *,
        state: str = "",
        node: str = "",
        search: str = "",
        sort: str = "state",
        page: int = 1,
        per_page: int = 50,
    ) -> ClusterWorkstreamsResponse:
        params: dict[str, Any] = {"sort": sort, "page": page, "per_page": per_page}
        if state:
            params["state"] = state
        if node:
            params["node"] = node
        if search:
            params["search"] = search
        return await self._request(
            "GET",
            "/v1/api/cluster/workstreams",
            params=params,
            response_model=ClusterWorkstreamsResponse,
        )

    async def node_detail(self, node_id: str) -> NodeDetailResponse:
        return await self._request(
            "GET", f"/v1/api/cluster/node/{node_id}", response_model=NodeDetailResponse
        )

    async def snapshot(self) -> ClusterSnapshotResponse:
        return await self._request(
            "GET", "/v1/api/cluster/snapshot", response_model=ClusterSnapshotResponse
        )

    async def create_workstream(
        self,
        *,
        node_id: str = "",
        name: str = "",
        model: str = "",
        initial_message: str = "",
        template: str = "",
        ws_template: str = "",
    ) -> ConsoleCreateWsResponse:
        body: dict[str, Any] = {}
        if node_id:
            body["node_id"] = node_id
        if name:
            body["name"] = name
        if model:
            body["model"] = model
        if initial_message:
            body["initial_message"] = initial_message
        if template:
            body["template"] = template
        if ws_template:
            body["ws_template"] = ws_template
        return await self._request(
            "POST",
            "/v1/api/cluster/workstreams/new",
            json_body=body,
            response_model=ConsoleCreateWsResponse,
        )

    # -- streaming -----------------------------------------------------------

    async def stream_cluster_events(self) -> AsyncIterator[ClusterEvent]:
        """Iterate over cluster SSE events."""
        async for data in self._stream_sse("/v1/api/cluster/events"):
            yield ClusterEvent.from_dict(data)

    # -- auth ----------------------------------------------------------------

    async def login(
        self,
        token: str = "",
        *,
        username: str = "",
        password: str = "",
    ) -> AuthLoginResponse:
        """Authenticate via API token or username:password."""
        if username and password:
            body: dict[str, str] = {"username": username, "password": password}
        else:
            body = {"token": token}
        return await self._request(
            "POST",
            "/v1/api/auth/login",
            json_body=body,
            response_model=AuthLoginResponse,
        )

    async def auth_status(self) -> AuthStatusResponse:
        """Get auth status (public -- no auth required)."""
        return await self._request("GET", "/v1/api/auth/status", response_model=AuthStatusResponse)

    async def setup(
        self,
        username: str,
        display_name: str,
        password: str,
    ) -> AuthSetupResponse:
        """First-time setup: create initial admin user (public, one-time only)."""
        return await self._request(
            "POST",
            "/v1/api/auth/setup",
            json_body={
                "username": username,
                "display_name": display_name,
                "password": password,
            },
            response_model=AuthSetupResponse,
        )

    async def logout(self) -> StatusResponse:
        return await self._request("POST", "/v1/api/auth/logout", response_model=StatusResponse)

    # -- health --------------------------------------------------------------

    async def health(self) -> ConsoleHealthResponse:
        return await self._request("GET", "/health", response_model=ConsoleHealthResponse)

    # -- schedules -----------------------------------------------------------

    async def list_schedules(self) -> ListSchedulesResponse:
        return await self._request(
            "GET", "/v1/api/admin/schedules", response_model=ListSchedulesResponse
        )

    async def create_schedule(
        self,
        *,
        name: str,
        schedule_type: str,
        initial_message: str,
        description: str = "",
        cron_expr: str = "",
        at_time: str = "",
        target_mode: str = "auto",
        model: str = "",
        auto_approve: bool = False,
        auto_approve_tools: list[str] | None = None,
        enabled: bool = True,
    ) -> ScheduleInfo:
        body: dict[str, Any] = {
            "name": name,
            "schedule_type": schedule_type,
            "initial_message": initial_message,
            "target_mode": target_mode,
            "auto_approve": auto_approve,
            "enabled": enabled,
        }
        if description:
            body["description"] = description
        if cron_expr:
            body["cron_expr"] = cron_expr
        if at_time:
            body["at_time"] = at_time
        if model:
            body["model"] = model
        if auto_approve_tools:
            body["auto_approve_tools"] = auto_approve_tools
        return await self._request(
            "POST", "/v1/api/admin/schedules", json_body=body, response_model=ScheduleInfo
        )

    async def get_schedule(self, task_id: str) -> ScheduleInfo:
        return await self._request(
            "GET", f"/v1/api/admin/schedules/{task_id}", response_model=ScheduleInfo
        )

    async def update_schedule(
        self,
        task_id: str,
        *,
        name: Any = _UNSET,
        description: Any = _UNSET,
        schedule_type: Any = _UNSET,
        cron_expr: Any = _UNSET,
        at_time: Any = _UNSET,
        target_mode: Any = _UNSET,
        model: Any = _UNSET,
        initial_message: Any = _UNSET,
        auto_approve: Any = _UNSET,
        auto_approve_tools: Any = _UNSET,
        enabled: Any = _UNSET,
    ) -> ScheduleInfo:
        body: dict[str, Any] = {}
        for key, val in [
            ("name", name),
            ("description", description),
            ("schedule_type", schedule_type),
            ("cron_expr", cron_expr),
            ("at_time", at_time),
            ("target_mode", target_mode),
            ("model", model),
            ("initial_message", initial_message),
            ("auto_approve", auto_approve),
            ("auto_approve_tools", auto_approve_tools),
            ("enabled", enabled),
        ]:
            if val is not _UNSET:
                body[key] = val
        return await self._request(
            "PUT",
            f"/v1/api/admin/schedules/{task_id}",
            json_body=body,
            response_model=ScheduleInfo,
        )

    async def delete_schedule(self, task_id: str) -> StatusResponse:
        return await self._request(
            "DELETE", f"/v1/api/admin/schedules/{task_id}", response_model=StatusResponse
        )

    async def list_schedule_runs(
        self, task_id: str, *, limit: int = 50
    ) -> ListScheduleRunsResponse:
        return await self._request(
            "GET",
            f"/v1/api/admin/schedules/{task_id}/runs",
            params={"limit": limit},
            response_model=ListScheduleRunsResponse,
        )

    # -- governance: roles ---------------------------------------------------

    async def list_roles(self) -> ListRolesResponse:
        """List all roles."""
        return await self._request("GET", "/v1/api/admin/roles", response_model=ListRolesResponse)

    async def create_role(
        self, name: str, display_name: str = "", permissions: str = "read"
    ) -> RoleInfo:
        """Create a custom role."""
        body: dict[str, Any] = {"name": name, "permissions": permissions}
        if display_name:
            body["display_name"] = display_name
        return await self._request(
            "POST", "/v1/api/admin/roles", json_body=body, response_model=RoleInfo
        )

    async def update_role(self, role_id: str, **fields: Any) -> RoleInfo:
        """Update a role's display_name and/or permissions."""
        return await self._request(
            "PUT", f"/v1/api/admin/roles/{role_id}", json_body=fields, response_model=RoleInfo
        )

    async def delete_role(self, role_id: str) -> StatusResponse:
        """Delete a custom role."""
        return await self._request(
            "DELETE", f"/v1/api/admin/roles/{role_id}", response_model=StatusResponse
        )

    async def list_user_roles(self, user_id: str) -> ListUserRolesResponse:
        """List roles assigned to a user."""
        return await self._request(
            "GET", f"/v1/api/admin/users/{user_id}/roles", response_model=ListUserRolesResponse
        )

    async def assign_role(self, user_id: str, role_id: str) -> StatusResponse:
        """Assign a role to a user."""
        return await self._request(
            "POST",
            f"/v1/api/admin/users/{user_id}/roles",
            json_body={"role_id": role_id},
            response_model=StatusResponse,
        )

    async def unassign_role(self, user_id: str, role_id: str) -> StatusResponse:
        """Unassign a role from a user."""
        return await self._request(
            "DELETE",
            f"/v1/api/admin/users/{user_id}/roles/{role_id}",
            response_model=StatusResponse,
        )

    # -- governance: organizations -------------------------------------------

    async def list_orgs(self) -> ListOrgsResponse:
        """List organizations."""
        return await self._request("GET", "/v1/api/admin/orgs", response_model=ListOrgsResponse)

    async def get_org(self, org_id: str) -> OrgInfo:
        """Get organization details."""
        return await self._request("GET", f"/v1/api/admin/orgs/{org_id}", response_model=OrgInfo)

    async def update_org(self, org_id: str, **fields: Any) -> OrgInfo:
        """Update organization settings."""
        return await self._request(
            "PUT", f"/v1/api/admin/orgs/{org_id}", json_body=fields, response_model=OrgInfo
        )

    # -- governance: tool policies -------------------------------------------

    async def list_policies(self) -> ListToolPoliciesResponse:
        """List tool policies ordered by priority."""
        return await self._request(
            "GET", "/v1/api/admin/policies", response_model=ListToolPoliciesResponse
        )

    async def create_policy(
        self,
        name: str,
        tool_pattern: str,
        action: str,
        priority: int = 0,
        **kwargs: Any,
    ) -> ToolPolicyInfo:
        """Create a tool policy."""
        body: dict[str, Any] = {
            "name": name,
            "tool_pattern": tool_pattern,
            "action": action,
            "priority": priority,
            **kwargs,
        }
        return await self._request(
            "POST", "/v1/api/admin/policies", json_body=body, response_model=ToolPolicyInfo
        )

    async def update_policy(self, policy_id: str, **fields: Any) -> ToolPolicyInfo:
        """Update a tool policy."""
        return await self._request(
            "PUT",
            f"/v1/api/admin/policies/{policy_id}",
            json_body=fields,
            response_model=ToolPolicyInfo,
        )

    async def delete_policy(self, policy_id: str) -> StatusResponse:
        """Delete a tool policy."""
        return await self._request(
            "DELETE", f"/v1/api/admin/policies/{policy_id}", response_model=StatusResponse
        )

    # -- governance: prompt templates ----------------------------------------

    async def list_templates(self) -> ListPromptTemplatesResponse:
        """List prompt templates."""
        return await self._request(
            "GET", "/v1/api/admin/templates", response_model=ListPromptTemplatesResponse
        )

    async def create_template(
        self,
        name: str,
        content: str,
        category: str = "general",
        variables: str = "[]",
        is_default: bool = False,
        **kwargs: Any,
    ) -> PromptTemplateInfo:
        """Create a prompt template."""
        body: dict[str, Any] = {
            "name": name,
            "content": content,
            "category": category,
            "variables": variables,
            "is_default": is_default,
            **kwargs,
        }
        return await self._request(
            "POST", "/v1/api/admin/templates", json_body=body, response_model=PromptTemplateInfo
        )

    async def update_template(self, template_id: str, **fields: Any) -> PromptTemplateInfo:
        """Update a prompt template."""
        return await self._request(
            "PUT",
            f"/v1/api/admin/templates/{template_id}",
            json_body=fields,
            response_model=PromptTemplateInfo,
        )

    async def delete_template(self, template_id: str) -> StatusResponse:
        """Delete a prompt template."""
        return await self._request(
            "DELETE",
            f"/v1/api/admin/templates/{template_id}",
            response_model=StatusResponse,
        )

    # -- governance: workstream templates ------------------------------------

    async def list_ws_templates(self) -> ListWsTemplatesResponse:
        """List all workstream templates."""
        return await self._request(
            "GET", "/v1/api/admin/ws-templates", response_model=ListWsTemplatesResponse
        )

    async def create_ws_template(self, name: str, **kwargs: Any) -> WsTemplateInfo:
        """Create a workstream template."""
        payload: dict[str, Any] = {"name": name, **kwargs}
        return await self._request(
            "POST", "/v1/api/admin/ws-templates", json_body=payload, response_model=WsTemplateInfo
        )

    async def get_ws_template(self, ws_template_id: str) -> WsTemplateInfo:
        """Get a workstream template by ID."""
        return await self._request(
            "GET",
            f"/v1/api/admin/ws-templates/{ws_template_id}",
            response_model=WsTemplateInfo,
        )

    async def update_ws_template(self, ws_template_id: str, **kwargs: Any) -> WsTemplateInfo:
        """Update a workstream template."""
        return await self._request(
            "PUT",
            f"/v1/api/admin/ws-templates/{ws_template_id}",
            json_body=kwargs,
            response_model=WsTemplateInfo,
        )

    async def delete_ws_template(self, ws_template_id: str) -> StatusResponse:
        """Delete a workstream template."""
        return await self._request(
            "DELETE",
            f"/v1/api/admin/ws-templates/{ws_template_id}",
            response_model=StatusResponse,
        )

    async def list_ws_template_versions(
        self, ws_template_id: str
    ) -> ListWsTemplateVersionsResponse:
        """List version history for a workstream template."""
        return await self._request(
            "GET",
            f"/v1/api/admin/ws-templates/{ws_template_id}/versions",
            response_model=ListWsTemplateVersionsResponse,
        )

    # -- governance: usage & audit -------------------------------------------

    async def get_usage(
        self,
        since: str,
        until: str = "",
        user_id: str = "",
        model: str = "",
        group_by: str = "",
    ) -> UsageResponse:
        """Query aggregated usage data."""
        params: dict[str, Any] = {"since": since}
        if until:
            params["until"] = until
        if user_id:
            params["user_id"] = user_id
        if model:
            params["model"] = model
        if group_by:
            params["group_by"] = group_by
        return await self._request(
            "GET", "/v1/api/admin/usage", params=params, response_model=UsageResponse
        )

    async def get_audit(
        self,
        action: str = "",
        user_id: str = "",
        since: str = "",
        until: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> ListAuditEventsResponse:
        """Query paginated audit events."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if action:
            params["action"] = action
        if user_id:
            params["user_id"] = user_id
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        return await self._request(
            "GET", "/v1/api/admin/audit", params=params, response_model=ListAuditEventsResponse
        )

    # -- governance: memories ------------------------------------------------

    async def list_memories(
        self,
        *,
        mem_type: str = "",
        scope: str = "",
        scope_id: str = "",
        limit: int = 100,
    ) -> ListAdminMemoriesResponse:
        params: dict[str, Any] = {"limit": limit}
        if mem_type:
            params["type"] = mem_type
        if scope:
            params["scope"] = scope
        if scope_id:
            params["scope_id"] = scope_id
        return await self._request(
            "GET",
            "/v1/api/admin/memories",
            params=params,
            response_model=ListAdminMemoriesResponse,
        )

    async def search_memories(
        self,
        query: str,
        *,
        mem_type: str = "",
        scope: str = "",
        scope_id: str = "",
        limit: int = 20,
    ) -> ListAdminMemoriesResponse:
        params: dict[str, Any] = {"q": query, "limit": limit}
        if mem_type:
            params["type"] = mem_type
        if scope:
            params["scope"] = scope
        if scope_id:
            params["scope_id"] = scope_id
        return await self._request(
            "GET",
            "/v1/api/admin/memories/search",
            params=params,
            response_model=ListAdminMemoriesResponse,
        )

    async def get_memory(self, memory_id: str) -> AdminMemoryInfo:
        return await self._request(
            "GET",
            f"/v1/api/admin/memories/{memory_id}",
            response_model=AdminMemoryInfo,
        )

    async def delete_memory(self, memory_id: str) -> StatusResponse:
        return await self._request(
            "DELETE",
            f"/v1/api/admin/memories/{memory_id}",
            response_model=StatusResponse,
        )


class TurnstoneConsole:
    """Synchronous client for the turnstone console API.

    Wraps :class:`AsyncTurnstoneConsole` via a background event loop.

    Usage::

        with TurnstoneConsole("http://localhost:8081", token="tok_xxx") as client:
            overview = client.overview()
            print(f"Nodes: {overview.nodes}")
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8081",
        token: str = "",
        timeout: float = 30.0,
    ) -> None:
        self._runner = _SyncRunner()
        self._async = AsyncTurnstoneConsole(base_url=base_url, token=token, timeout=timeout)

    # -- cluster overview ----------------------------------------------------

    def overview(self) -> ClusterOverviewResponse:
        return self._runner.run(self._async.overview())

    def nodes(
        self,
        *,
        sort: str = "activity",
        limit: int = 100,
        offset: int = 0,
    ) -> ClusterNodesResponse:
        return self._runner.run(self._async.nodes(sort=sort, limit=limit, offset=offset))

    def workstreams(
        self,
        *,
        state: str = "",
        node: str = "",
        search: str = "",
        sort: str = "state",
        page: int = 1,
        per_page: int = 50,
    ) -> ClusterWorkstreamsResponse:
        return self._runner.run(
            self._async.workstreams(
                state=state, node=node, search=search, sort=sort, page=page, per_page=per_page
            )
        )

    def node_detail(self, node_id: str) -> NodeDetailResponse:
        return self._runner.run(self._async.node_detail(node_id))

    def snapshot(self) -> ClusterSnapshotResponse:
        return self._runner.run(self._async.snapshot())

    def create_workstream(
        self,
        *,
        node_id: str = "",
        name: str = "",
        model: str = "",
        initial_message: str = "",
        template: str = "",
        ws_template: str = "",
    ) -> ConsoleCreateWsResponse:
        return self._runner.run(
            self._async.create_workstream(
                node_id=node_id,
                name=name,
                model=model,
                initial_message=initial_message,
                template=template,
                ws_template=ws_template,
            )
        )

    # -- streaming -----------------------------------------------------------

    def stream_cluster_events(self) -> Iterator[ClusterEvent]:
        return self._runner.run_iter(self._async.stream_cluster_events())

    # -- auth ----------------------------------------------------------------

    def login(
        self, token: str = "", *, username: str = "", password: str = ""
    ) -> AuthLoginResponse:
        return self._runner.run(self._async.login(token, username=username, password=password))

    def auth_status(self) -> AuthStatusResponse:
        return self._runner.run(self._async.auth_status())

    def setup(self, username: str, display_name: str, password: str) -> AuthSetupResponse:
        return self._runner.run(self._async.setup(username, display_name, password))

    def logout(self) -> StatusResponse:
        return self._runner.run(self._async.logout())

    # -- health --------------------------------------------------------------

    def health(self) -> ConsoleHealthResponse:
        return self._runner.run(self._async.health())

    # -- schedules -----------------------------------------------------------

    def list_schedules(self) -> ListSchedulesResponse:
        return self._runner.run(self._async.list_schedules())

    def create_schedule(
        self,
        *,
        name: str,
        schedule_type: str,
        initial_message: str,
        description: str = "",
        cron_expr: str = "",
        at_time: str = "",
        target_mode: str = "auto",
        model: str = "",
        auto_approve: bool = False,
        auto_approve_tools: list[str] | None = None,
        enabled: bool = True,
    ) -> ScheduleInfo:
        return self._runner.run(
            self._async.create_schedule(
                name=name,
                schedule_type=schedule_type,
                initial_message=initial_message,
                description=description,
                cron_expr=cron_expr,
                at_time=at_time,
                target_mode=target_mode,
                model=model,
                auto_approve=auto_approve,
                auto_approve_tools=auto_approve_tools,
                enabled=enabled,
            )
        )

    def get_schedule(self, task_id: str) -> ScheduleInfo:
        return self._runner.run(self._async.get_schedule(task_id))

    def update_schedule(
        self,
        task_id: str,
        *,
        name: Any = _UNSET,
        description: Any = _UNSET,
        schedule_type: Any = _UNSET,
        cron_expr: Any = _UNSET,
        at_time: Any = _UNSET,
        target_mode: Any = _UNSET,
        model: Any = _UNSET,
        initial_message: Any = _UNSET,
        auto_approve: Any = _UNSET,
        auto_approve_tools: Any = _UNSET,
        enabled: Any = _UNSET,
    ) -> ScheduleInfo:
        return self._runner.run(
            self._async.update_schedule(
                task_id,
                name=name,
                description=description,
                schedule_type=schedule_type,
                cron_expr=cron_expr,
                at_time=at_time,
                target_mode=target_mode,
                model=model,
                initial_message=initial_message,
                auto_approve=auto_approve,
                auto_approve_tools=auto_approve_tools,
                enabled=enabled,
            )
        )

    def delete_schedule(self, task_id: str) -> StatusResponse:
        return self._runner.run(self._async.delete_schedule(task_id))

    def list_schedule_runs(self, task_id: str, *, limit: int = 50) -> ListScheduleRunsResponse:
        return self._runner.run(self._async.list_schedule_runs(task_id, limit=limit))

    # -- governance: roles ---------------------------------------------------

    def list_roles(self) -> ListRolesResponse:
        return self._runner.run(self._async.list_roles())

    def create_role(self, name: str, display_name: str = "", permissions: str = "read") -> RoleInfo:
        return self._runner.run(
            self._async.create_role(name, display_name=display_name, permissions=permissions)
        )

    def update_role(self, role_id: str, **fields: Any) -> RoleInfo:
        return self._runner.run(self._async.update_role(role_id, **fields))

    def delete_role(self, role_id: str) -> StatusResponse:
        return self._runner.run(self._async.delete_role(role_id))

    def list_user_roles(self, user_id: str) -> ListUserRolesResponse:
        return self._runner.run(self._async.list_user_roles(user_id))

    def assign_role(self, user_id: str, role_id: str) -> StatusResponse:
        return self._runner.run(self._async.assign_role(user_id, role_id))

    def unassign_role(self, user_id: str, role_id: str) -> StatusResponse:
        return self._runner.run(self._async.unassign_role(user_id, role_id))

    # -- governance: organizations -------------------------------------------

    def list_orgs(self) -> ListOrgsResponse:
        return self._runner.run(self._async.list_orgs())

    def get_org(self, org_id: str) -> OrgInfo:
        return self._runner.run(self._async.get_org(org_id))

    def update_org(self, org_id: str, **fields: Any) -> OrgInfo:
        return self._runner.run(self._async.update_org(org_id, **fields))

    # -- governance: tool policies -------------------------------------------

    def list_policies(self) -> ListToolPoliciesResponse:
        return self._runner.run(self._async.list_policies())

    def create_policy(
        self,
        name: str,
        tool_pattern: str,
        action: str,
        priority: int = 0,
        **kwargs: Any,
    ) -> ToolPolicyInfo:
        return self._runner.run(
            self._async.create_policy(name, tool_pattern, action, priority=priority, **kwargs)
        )

    def update_policy(self, policy_id: str, **fields: Any) -> ToolPolicyInfo:
        return self._runner.run(self._async.update_policy(policy_id, **fields))

    def delete_policy(self, policy_id: str) -> StatusResponse:
        return self._runner.run(self._async.delete_policy(policy_id))

    # -- governance: prompt templates ----------------------------------------

    def list_templates(self) -> ListPromptTemplatesResponse:
        return self._runner.run(self._async.list_templates())

    def create_template(
        self,
        name: str,
        content: str,
        category: str = "general",
        variables: str = "[]",
        is_default: bool = False,
        **kwargs: Any,
    ) -> PromptTemplateInfo:
        return self._runner.run(
            self._async.create_template(
                name,
                content,
                category=category,
                variables=variables,
                is_default=is_default,
                **kwargs,
            )
        )

    def update_template(self, template_id: str, **fields: Any) -> PromptTemplateInfo:
        return self._runner.run(self._async.update_template(template_id, **fields))

    def delete_template(self, template_id: str) -> StatusResponse:
        return self._runner.run(self._async.delete_template(template_id))

    # -- governance: workstream templates ------------------------------------

    def list_ws_templates(self) -> ListWsTemplatesResponse:
        return self._runner.run(self._async.list_ws_templates())

    def create_ws_template(self, name: str, **kwargs: Any) -> WsTemplateInfo:
        return self._runner.run(self._async.create_ws_template(name, **kwargs))

    def get_ws_template(self, ws_template_id: str) -> WsTemplateInfo:
        return self._runner.run(self._async.get_ws_template(ws_template_id))

    def update_ws_template(self, ws_template_id: str, **kwargs: Any) -> WsTemplateInfo:
        return self._runner.run(self._async.update_ws_template(ws_template_id, **kwargs))

    def delete_ws_template(self, ws_template_id: str) -> StatusResponse:
        return self._runner.run(self._async.delete_ws_template(ws_template_id))

    def list_ws_template_versions(self, ws_template_id: str) -> ListWsTemplateVersionsResponse:
        return self._runner.run(self._async.list_ws_template_versions(ws_template_id))

    # -- governance: usage & audit -------------------------------------------

    def get_usage(
        self,
        since: str,
        until: str = "",
        user_id: str = "",
        model: str = "",
        group_by: str = "",
    ) -> UsageResponse:
        return self._runner.run(
            self._async.get_usage(
                since, until=until, user_id=user_id, model=model, group_by=group_by
            )
        )

    def get_audit(
        self,
        action: str = "",
        user_id: str = "",
        since: str = "",
        until: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> ListAuditEventsResponse:
        return self._runner.run(
            self._async.get_audit(
                action=action,
                user_id=user_id,
                since=since,
                until=until,
                limit=limit,
                offset=offset,
            )
        )

    # -- governance: memories ------------------------------------------------

    def list_memories(
        self, *, mem_type: str = "", scope: str = "", scope_id: str = "", limit: int = 100
    ) -> ListAdminMemoriesResponse:
        return self._runner.run(
            self._async.list_memories(
                mem_type=mem_type, scope=scope, scope_id=scope_id, limit=limit
            )
        )

    def search_memories(
        self,
        query: str,
        *,
        mem_type: str = "",
        scope: str = "",
        scope_id: str = "",
        limit: int = 20,
    ) -> ListAdminMemoriesResponse:
        return self._runner.run(
            self._async.search_memories(
                query, mem_type=mem_type, scope=scope, scope_id=scope_id, limit=limit
            )
        )

    def get_memory(self, memory_id: str) -> AdminMemoryInfo:
        return self._runner.run(self._async.get_memory(memory_id))

    def delete_memory(self, memory_id: str) -> StatusResponse:
        return self._runner.run(self._async.delete_memory(memory_id))

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self._runner.run(self._async.aclose())
        self._runner.close()

    def __enter__(self) -> TurnstoneConsole:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
