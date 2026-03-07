"""Typed HTTP clients for the turnstone server API.

Usage::

    from turnstone.sdk import TurnstoneServer

    with TurnstoneServer("http://localhost:8080", token="tok_xxx") as client:
        ws = client.create_workstream(name="Analysis")
        result = client.send_and_wait("Hello", ws.ws_id)
        print(result.content)
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from turnstone.api.schemas import (
    AuthLoginResponse,
    AuthSetupResponse,
    AuthStatusResponse,
    StatusResponse,
)
from turnstone.api.server_schemas import (
    CreateWorkstreamResponse,
    DashboardResponse,
    HealthResponse,
    ListSavedWorkstreamsResponse,
    ListWorkstreamsResponse,
    SendResponse,
)
from turnstone.sdk._base import _BaseClient
from turnstone.sdk._sync import _SyncRunner
from turnstone.sdk._types import TurnResult
from turnstone.sdk.events import (
    ContentEvent,
    ErrorEvent,
    ReasoningEvent,
    ServerEvent,
    ToolResultEvent,
    WsStateEvent,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    import httpx


class AsyncTurnstoneServer(_BaseClient):
    """Async client for the turnstone server API."""

    def __init__(
        self,
        base_url: str = "http://localhost:8080",
        token: str = "",
        timeout: float = 30.0,
        httpx_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(base_url=base_url, token=token, timeout=timeout, httpx_client=httpx_client)

    # -- workstream management -----------------------------------------------

    async def list_workstreams(self) -> ListWorkstreamsResponse:
        return await self._request(
            "GET", "/v1/api/workstreams", response_model=ListWorkstreamsResponse
        )

    async def dashboard(self) -> DashboardResponse:
        return await self._request("GET", "/v1/api/dashboard", response_model=DashboardResponse)

    async def create_workstream(
        self,
        *,
        name: str = "",
        model: str = "",
        auto_approve: bool = False,
        resume_ws: str = "",
    ) -> CreateWorkstreamResponse:
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if model:
            body["model"] = model
        if auto_approve:
            body["auto_approve"] = True
        if resume_ws:
            body["resume_ws"] = resume_ws
        return await self._request(
            "POST",
            "/v1/api/workstreams/new",
            json_body=body,
            response_model=CreateWorkstreamResponse,
        )

    async def close_workstream(self, ws_id: str) -> StatusResponse:
        return await self._request(
            "POST",
            "/v1/api/workstreams/close",
            json_body={"ws_id": ws_id},
            response_model=StatusResponse,
        )

    # -- chat interaction ----------------------------------------------------

    async def send(self, message: str, ws_id: str) -> SendResponse:
        return await self._request(
            "POST",
            "/v1/api/send",
            json_body={"message": message, "ws_id": ws_id},
            response_model=SendResponse,
        )

    async def approve(
        self,
        *,
        ws_id: str,
        approved: bool = True,
        feedback: str | None = None,
        always: bool = False,
    ) -> StatusResponse:
        body: dict[str, Any] = {"ws_id": ws_id, "approved": approved}
        if feedback is not None:
            body["feedback"] = feedback
        if always:
            body["always"] = True
        return await self._request(
            "POST", "/v1/api/approve", json_body=body, response_model=StatusResponse
        )

    async def plan_feedback(self, *, ws_id: str, feedback: str = "") -> StatusResponse:
        return await self._request(
            "POST",
            "/v1/api/plan",
            json_body={"ws_id": ws_id, "feedback": feedback},
            response_model=StatusResponse,
        )

    async def command(self, *, ws_id: str, command: str) -> StatusResponse:
        return await self._request(
            "POST",
            "/v1/api/command",
            json_body={"ws_id": ws_id, "command": command},
            response_model=StatusResponse,
        )

    # -- streaming -----------------------------------------------------------

    async def stream_events(self, ws_id: str) -> AsyncIterator[ServerEvent]:
        """Iterate over per-workstream SSE events."""
        async for data in self._stream_sse("/v1/api/events", params={"ws_id": ws_id}):
            yield ServerEvent.from_dict(data)

    async def stream_global_events(self) -> AsyncIterator[ServerEvent]:
        """Iterate over global SSE events."""
        async for data in self._stream_sse("/v1/api/events/global"):
            yield ServerEvent.from_dict(data)

    # -- high-level convenience ----------------------------------------------

    async def send_and_wait(
        self,
        message: str,
        ws_id: str,
        *,
        timeout: float = 600,
        on_event: Callable[[ServerEvent], None] | None = None,
    ) -> TurnResult:
        """Send a message and wait for the turn to complete via SSE.

        Opens the per-workstream SSE stream *before* sending the message
        to avoid missing early events, then accumulates content / reasoning /
        tool results / errors until a ``ws_state`` event with
        ``state="idle"`` arrives, or the timeout expires.
        """
        result = TurnResult(ws_id=ws_id)

        async def _consume() -> None:
            async for data in self._stream_sse("/v1/api/events", params={"ws_id": ws_id}):
                event = ServerEvent.from_dict(data)
                if on_event:
                    on_event(event)

                if isinstance(event, ContentEvent):
                    result.content_parts.append(event.text)
                elif isinstance(event, ReasoningEvent):
                    result.reasoning_parts.append(event.text)
                elif isinstance(event, ToolResultEvent):
                    result.tool_results.append((event.name, event.output))
                elif isinstance(event, ErrorEvent):
                    result.errors.append(event.message)
                elif isinstance(event, WsStateEvent) and event.state == "idle":
                    return

        # Start SSE consumer BEFORE sending to avoid missing early events
        consume_task = asyncio.create_task(_consume())
        await asyncio.sleep(0)  # yield to let SSE connection establish

        try:
            send_resp = await self.send(message, ws_id)
            if send_resp.status == "busy":
                result.errors.append("Workstream is busy")
                return result

            await asyncio.wait_for(consume_task, timeout=timeout)
        except TimeoutError:
            result.timed_out = True
        finally:
            # Always clean up the SSE consumer to prevent connection leaks
            if not consume_task.done():
                consume_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await consume_task
        return result

    # -- saved workstreams ----------------------------------------------------

    async def list_saved_workstreams(self) -> ListSavedWorkstreamsResponse:
        return await self._request(
            "GET", "/v1/api/workstreams/saved", response_model=ListSavedWorkstreamsResponse
        )

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

    async def health(self) -> HealthResponse:
        return await self._request("GET", "/health", response_model=HealthResponse)


class TurnstoneServer:
    """Synchronous client for the turnstone server API.

    Wraps :class:`AsyncTurnstoneServer` via a background event loop.

    Usage::

        with TurnstoneServer("http://localhost:8080", token="tok_xxx") as client:
            ws = client.create_workstream(name="Analysis")
            result = client.send_and_wait("Hello", ws.ws_id)
            print(result.content)
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8080",
        token: str = "",
        timeout: float = 30.0,
    ) -> None:
        self._runner = _SyncRunner()
        self._async = AsyncTurnstoneServer(base_url=base_url, token=token, timeout=timeout)

    # -- workstream management -----------------------------------------------

    def list_workstreams(self) -> ListWorkstreamsResponse:
        return self._runner.run(self._async.list_workstreams())

    def dashboard(self) -> DashboardResponse:
        return self._runner.run(self._async.dashboard())

    def create_workstream(
        self,
        *,
        name: str = "",
        model: str = "",
        auto_approve: bool = False,
        resume_ws: str = "",
    ) -> CreateWorkstreamResponse:
        return self._runner.run(
            self._async.create_workstream(
                name=name, model=model, auto_approve=auto_approve, resume_ws=resume_ws
            )
        )

    def close_workstream(self, ws_id: str) -> StatusResponse:
        return self._runner.run(self._async.close_workstream(ws_id))

    # -- chat interaction ----------------------------------------------------

    def send(self, message: str, ws_id: str) -> SendResponse:
        return self._runner.run(self._async.send(message, ws_id))

    def approve(
        self,
        *,
        ws_id: str,
        approved: bool = True,
        feedback: str | None = None,
        always: bool = False,
    ) -> StatusResponse:
        return self._runner.run(
            self._async.approve(ws_id=ws_id, approved=approved, feedback=feedback, always=always)
        )

    def plan_feedback(self, *, ws_id: str, feedback: str = "") -> StatusResponse:
        return self._runner.run(self._async.plan_feedback(ws_id=ws_id, feedback=feedback))

    def command(self, *, ws_id: str, command: str) -> StatusResponse:
        return self._runner.run(self._async.command(ws_id=ws_id, command=command))

    # -- streaming -----------------------------------------------------------

    def stream_events(self, ws_id: str) -> Iterator[ServerEvent]:
        return self._runner.run_iter(self._async.stream_events(ws_id))

    def stream_global_events(self) -> Iterator[ServerEvent]:
        return self._runner.run_iter(self._async.stream_global_events())

    # -- high-level convenience ----------------------------------------------

    def send_and_wait(
        self,
        message: str,
        ws_id: str,
        *,
        timeout: float = 600,
        on_event: Callable[[ServerEvent], None] | None = None,
    ) -> TurnResult:
        return self._runner.run(
            self._async.send_and_wait(message, ws_id, timeout=timeout, on_event=on_event)
        )

    # -- saved workstreams ----------------------------------------------------

    def list_saved_workstreams(self) -> ListSavedWorkstreamsResponse:
        return self._runner.run(self._async.list_saved_workstreams())

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

    def health(self) -> HealthResponse:
        return self._runner.run(self._async.health())

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self._runner.run(self._async.aclose())
        self._runner.close()

    def __enter__(self) -> TurnstoneServer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
