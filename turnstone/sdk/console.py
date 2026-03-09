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
    ClusterNodesResponse,
    ClusterOverviewResponse,
    ClusterSnapshotResponse,
    ClusterWorkstreamsResponse,
    ConsoleCreateWsResponse,
    ConsoleHealthResponse,
    NodeDetailResponse,
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
    ) -> ConsoleCreateWsResponse:
        return self._runner.run(
            self._async.create_workstream(
                node_id=node_id, name=name, model=model, initial_message=initial_message
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

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self._runner.run(self._async.aclose())
        self._runner.close()

    def __enter__(self) -> TurnstoneConsole:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
