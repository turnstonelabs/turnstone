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
    ClusterWorkstreamsResponse,
    ConsoleCreateWsResponse,
    ConsoleHealthResponse,
    NodeDetailResponse,
)
from turnstone.api.schemas import AuthLoginResponse, StatusResponse
from turnstone.sdk._base import _BaseClient
from turnstone.sdk._sync import _SyncRunner
from turnstone.sdk.events import ClusterEvent

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

    async def login(self, token: str) -> AuthLoginResponse:
        return await self._request(
            "POST",
            "/v1/api/auth/login",
            json_body={"token": token},
            response_model=AuthLoginResponse,
        )

    async def logout(self) -> StatusResponse:
        return await self._request("POST", "/v1/api/auth/logout", response_model=StatusResponse)

    # -- health --------------------------------------------------------------

    async def health(self) -> ConsoleHealthResponse:
        return await self._request("GET", "/health", response_model=ConsoleHealthResponse)


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

    def login(self, token: str) -> AuthLoginResponse:
        return self._runner.run(self._async.login(token))

    def logout(self) -> StatusResponse:
        return self._runner.run(self._async.logout())

    # -- health --------------------------------------------------------------

    def health(self) -> ConsoleHealthResponse:
        return self._runner.run(self._async.health())

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self._runner.run(self._async.aclose())
        self._runner.close()

    def __enter__(self) -> TurnstoneConsole:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
