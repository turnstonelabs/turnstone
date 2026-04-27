"""Typed HTTP clients for the turnstone server API.

Usage::

    from turnstone.sdk import TurnstoneServer

    with TurnstoneServer("http://localhost:8080", token="ts_your_api_token") as client:
        ws = client.create_workstream(name="Analysis")
        result = client.send_and_wait("Hello", ws.ws_id)
        print(result.content)
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
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
    ListAttachmentsResponse,
    ListAvailableModelsResponse,
    ListMemoriesResponse,
    ListSavedWorkstreamsResponse,
    ListSkillSummaryResponse,
    ListWorkstreamsResponse,
    MemoryInfo,
    SendResponse,
    UploadAttachmentResponse,
)
from turnstone.sdk._base import _BaseClient
from turnstone.sdk._sync import _SyncRunner
from turnstone.sdk._types import AttachmentUpload, TurnResult
from turnstone.sdk.events import (
    ClusterEvent,
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
        ca_cert: str | None = None,
        client_cert: str | None = None,
        client_key: str | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            token=token,
            timeout=timeout,
            httpx_client=httpx_client,
            ca_cert=ca_cert,
            client_cert=client_cert,
            client_key=client_key,
            token_factory=token_factory,
        )

    # -- workstream management -----------------------------------------------

    async def list_workstreams(self) -> ListWorkstreamsResponse:
        return await self._request(
            "GET", "/v1/api/workstreams", response_model=ListWorkstreamsResponse
        )

    async def dashboard(self) -> DashboardResponse:
        return await self._request("GET", "/v1/api/dashboard", response_model=DashboardResponse)

    async def list_models(self) -> ListAvailableModelsResponse:
        """GET /v1/api/models — available model aliases and defaults."""
        return await self._request(
            "GET", "/v1/api/models", response_model=ListAvailableModelsResponse
        )

    async def create_workstream(
        self,
        *,
        name: str = "",
        model: str = "",
        auto_approve: bool = False,
        resume_ws: str = "",
        skill: str = "",
        initial_message: str = "",
        auto_approve_tools: str = "",
        user_id: str = "",
        ws_id: str = "",
        client_type: str = "",
        notify_targets: str = "",
        attachments: list[AttachmentUpload] | None = None,
    ) -> CreateWorkstreamResponse:
        """Create a new workstream.

        When *attachments* is non-empty the request is sent as
        ``multipart/form-data`` with the metadata in a ``meta`` JSON
        field and one ``file`` part per attachment.  A ws_id is
        auto-generated client-side when not supplied so cluster-routed
        callers can bind the body to the owning node up front.  When
        *initial_message* is also set, the server reserves the
        attachments onto that turn before its background worker
        dispatches.
        """
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if model:
            body["model"] = model
        if auto_approve:
            body["auto_approve"] = True
        if resume_ws:
            body["resume_ws"] = resume_ws
        if skill:
            body["skill"] = skill
        if initial_message:
            body["initial_message"] = initial_message
        if auto_approve_tools:
            body["auto_approve_tools"] = auto_approve_tools
        if user_id:
            body["user_id"] = user_id
        if client_type:
            body["client_type"] = client_type
        if notify_targets and notify_targets != "[]":
            body["notify_targets"] = notify_targets

        if attachments:
            if not ws_id:
                ws_id = secrets.token_hex(16)
            body["ws_id"] = ws_id
            import json as _json

            files: list[tuple[str, tuple[str, bytes, str]]] = [
                (
                    "file",
                    (
                        att.filename,
                        att.data,
                        att.mime_type or "application/octet-stream",
                    ),
                )
                for att in attachments
            ]
            return await self._request(
                "POST",
                "/v1/api/workstreams/new",
                files=files,
                data={"meta": _json.dumps(body)},
                response_model=CreateWorkstreamResponse,
            )

        if ws_id:
            body["ws_id"] = ws_id
        return await self._request(
            "POST",
            "/v1/api/workstreams/new",
            json_body=body,
            response_model=CreateWorkstreamResponse,
        )

    async def close_workstream(self, ws_id: str) -> StatusResponse:
        return await self._request(
            "POST",
            f"/v1/api/workstreams/{ws_id}/close",
            response_model=StatusResponse,
        )

    # -- chat interaction ----------------------------------------------------

    async def send(
        self,
        message: str,
        ws_id: str,
        *,
        attachment_ids: list[str] | None = None,
    ) -> SendResponse:
        body: dict[str, Any] = {"message": message}
        if attachment_ids is not None:
            body["attachment_ids"] = list(attachment_ids)
        return await self._request(
            "POST",
            f"/v1/api/workstreams/{ws_id}/send",
            json_body=body,
            response_model=SendResponse,
        )

    # -- attachments ---------------------------------------------------------

    async def upload_attachment(
        self,
        ws_id: str,
        filename: str,
        data: bytes,
        *,
        mime_type: str | None = None,
    ) -> UploadAttachmentResponse:
        """Upload one file as a pending attachment for this workstream.

        The server validates size + MIME (magic-byte sniff for images,
        UTF-8 decode for text) and rejects with 400/413 on any mismatch.
        Returns the persisted ``AttachmentInfo`` so the caller can pass
        the id into a subsequent ``send(attachment_ids=...)``.
        """
        files: list[tuple[str, tuple[str, bytes, str]]] = [
            (
                "file",
                (filename, data, mime_type or "application/octet-stream"),
            )
        ]
        return await self._request(
            "POST",
            f"/v1/api/workstreams/{ws_id}/attachments",
            files=files,
            response_model=UploadAttachmentResponse,
        )

    async def list_attachments(self, ws_id: str) -> ListAttachmentsResponse:
        """List the caller's pending (unconsumed) attachments for *ws_id*."""
        return await self._request(
            "GET",
            f"/v1/api/workstreams/{ws_id}/attachments",
            response_model=ListAttachmentsResponse,
        )

    async def get_attachment_content(self, ws_id: str, attachment_id: str) -> bytes:
        """Return the raw bytes of an attachment."""
        return await self._request_bytes(
            "GET",
            f"/v1/api/workstreams/{ws_id}/attachments/{attachment_id}/content",
        )

    async def delete_attachment(self, ws_id: str, attachment_id: str) -> StatusResponse:
        """Remove a pending attachment.  Consumed attachments return 404."""
        return await self._request(
            "DELETE",
            f"/v1/api/workstreams/{ws_id}/attachments/{attachment_id}",
            response_model=StatusResponse,
        )

    async def approve(
        self,
        *,
        ws_id: str,
        approved: bool = True,
        feedback: str | None = None,
        always: bool = False,
    ) -> StatusResponse:
        body: dict[str, Any] = {"approved": approved}
        if feedback is not None:
            body["feedback"] = feedback
        if always:
            body["always"] = True
        return await self._request(
            "POST",
            f"/v1/api/workstreams/{ws_id}/approve",
            json_body=body,
            response_model=StatusResponse,
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

    async def cancel(self, ws_id: str, *, force: bool = False) -> StatusResponse:
        body: dict[str, object] = {}
        if force:
            body["force"] = True
        return await self._request(
            "POST",
            f"/v1/api/workstreams/{ws_id}/cancel",
            json_body=body,
            response_model=StatusResponse,
        )

    # -- streaming -----------------------------------------------------------

    async def stream_events(self, ws_id: str) -> AsyncIterator[ServerEvent]:
        """Iterate over per-workstream SSE events."""
        async for data in self._stream_sse(f"/v1/api/workstreams/{ws_id}/events"):
            yield ServerEvent.from_dict(data)

    async def stream_global_events(self) -> AsyncIterator[ServerEvent]:
        """Iterate over global SSE events."""
        async for data in self._stream_sse("/v1/api/events/global"):
            yield ServerEvent.from_dict(data)

    async def stream_node_events(
        self, *, expected_node_id: str = ""
    ) -> AsyncIterator[ClusterEvent]:
        """Iterate over node-level SSE events (snapshot + deltas).

        Connects to ``/v1/api/events/global`` with the optional
        ``expected_node_id`` param for identity verification.  Yields
        ``ClusterEvent`` instances (``NodeSnapshotEvent``, ``HealthChangedEvent``,
        etc.) suitable for console collector consumption.
        """
        params: dict[str, str] = {}
        if expected_node_id:
            params["expected_node_id"] = expected_node_id
        async for data in self._stream_sse("/v1/api/events/global", params=params):
            yield ClusterEvent.from_dict(data)

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
            async for data in self._stream_sse(f"/v1/api/workstreams/{ws_id}/events"):
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

    # -- skills --------------------------------------------------------------

    async def list_skills(self) -> ListSkillSummaryResponse:
        return await self._request("GET", "/v1/api/skills", response_model=ListSkillSummaryResponse)

    # -- memories ------------------------------------------------------------

    async def list_memories(
        self,
        *,
        mem_type: str = "",
        scope: str = "",
        scope_id: str = "",
        limit: int = 100,
    ) -> ListMemoriesResponse:
        params: dict[str, Any] = {"limit": limit}
        if mem_type:
            params["type"] = mem_type
        if scope:
            params["scope"] = scope
        if scope_id:
            params["scope_id"] = scope_id
        return await self._request(
            "GET", "/v1/api/memories", params=params, response_model=ListMemoriesResponse
        )

    async def save_memory(
        self,
        name: str,
        content: str,
        *,
        description: str = "",
        mem_type: str = "project",
        scope: str = "global",
        scope_id: str = "",
    ) -> MemoryInfo:
        body: dict[str, Any] = {
            "name": name,
            "content": content,
            "type": mem_type,
            "scope": scope,
        }
        if description:
            body["description"] = description
        if scope_id:
            body["scope_id"] = scope_id
        return await self._request(
            "POST", "/v1/api/memories", json_body=body, response_model=MemoryInfo
        )

    async def search_memories(
        self,
        query: str,
        *,
        mem_type: str = "",
        scope: str = "",
        scope_id: str = "",
        limit: int = 20,
    ) -> ListMemoriesResponse:
        body: dict[str, Any] = {"query": query, "limit": limit}
        if mem_type:
            body["type"] = mem_type
        if scope:
            body["scope"] = scope
        if scope_id:
            body["scope_id"] = scope_id
        return await self._request(
            "POST",
            "/v1/api/memories/search",
            json_body=body,
            response_model=ListMemoriesResponse,
        )

    async def delete_memory(
        self,
        name: str,
        *,
        scope: str = "global",
        scope_id: str = "",
    ) -> StatusResponse:
        params: dict[str, Any] = {"scope": scope}
        if scope_id:
            params["scope_id"] = scope_id
        return await self._request(
            "DELETE",
            f"/v1/api/memories/{name}",
            params=params,
            response_model=StatusResponse,
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

        with TurnstoneServer("http://localhost:8080", token="ts_your_api_token") as client:
            ws = client.create_workstream(name="Analysis")
            result = client.send_and_wait("Hello", ws.ws_id)
            print(result.content)
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8080",
        token: str = "",
        timeout: float = 30.0,
        ca_cert: str | None = None,
        client_cert: str | None = None,
        client_key: str | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._runner = _SyncRunner()
        self._async = AsyncTurnstoneServer(
            base_url=base_url,
            token=token,
            timeout=timeout,
            ca_cert=ca_cert,
            client_cert=client_cert,
            client_key=client_key,
            token_factory=token_factory,
        )

    # -- workstream management -----------------------------------------------

    def list_workstreams(self) -> ListWorkstreamsResponse:
        return self._runner.run(self._async.list_workstreams())

    def dashboard(self) -> DashboardResponse:
        return self._runner.run(self._async.dashboard())

    def list_models(self) -> ListAvailableModelsResponse:
        return self._runner.run(self._async.list_models())

    def create_workstream(
        self,
        *,
        name: str = "",
        model: str = "",
        auto_approve: bool = False,
        resume_ws: str = "",
        skill: str = "",
        initial_message: str = "",
        auto_approve_tools: str = "",
        user_id: str = "",
        ws_id: str = "",
        client_type: str = "",
        notify_targets: str = "",
        attachments: list[AttachmentUpload] | None = None,
    ) -> CreateWorkstreamResponse:
        return self._runner.run(
            self._async.create_workstream(
                name=name,
                model=model,
                auto_approve=auto_approve,
                resume_ws=resume_ws,
                skill=skill,
                initial_message=initial_message,
                auto_approve_tools=auto_approve_tools,
                user_id=user_id,
                ws_id=ws_id,
                client_type=client_type,
                notify_targets=notify_targets,
                attachments=attachments,
            )
        )

    def close_workstream(self, ws_id: str) -> StatusResponse:
        return self._runner.run(self._async.close_workstream(ws_id))

    # -- chat interaction ----------------------------------------------------

    def send(
        self,
        message: str,
        ws_id: str,
        *,
        attachment_ids: list[str] | None = None,
    ) -> SendResponse:
        return self._runner.run(self._async.send(message, ws_id, attachment_ids=attachment_ids))

    # -- attachments ---------------------------------------------------------

    def upload_attachment(
        self,
        ws_id: str,
        filename: str,
        data: bytes,
        *,
        mime_type: str | None = None,
    ) -> UploadAttachmentResponse:
        return self._runner.run(
            self._async.upload_attachment(ws_id, filename, data, mime_type=mime_type)
        )

    def list_attachments(self, ws_id: str) -> ListAttachmentsResponse:
        return self._runner.run(self._async.list_attachments(ws_id))

    def get_attachment_content(self, ws_id: str, attachment_id: str) -> bytes:
        return self._runner.run(self._async.get_attachment_content(ws_id, attachment_id))

    def delete_attachment(self, ws_id: str, attachment_id: str) -> StatusResponse:
        return self._runner.run(self._async.delete_attachment(ws_id, attachment_id))

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

    def cancel(self, ws_id: str, *, force: bool = False) -> StatusResponse:
        return self._runner.run(self._async.cancel(ws_id, force=force))

    # -- streaming -----------------------------------------------------------

    def stream_events(self, ws_id: str) -> Iterator[ServerEvent]:
        return self._runner.run_iter(self._async.stream_events(ws_id))

    def stream_global_events(self) -> Iterator[ServerEvent]:
        return self._runner.run_iter(self._async.stream_global_events())

    def stream_node_events(self, *, expected_node_id: str = "") -> Iterator[ClusterEvent]:
        return self._runner.run_iter(
            self._async.stream_node_events(expected_node_id=expected_node_id)
        )

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

    # -- skills --------------------------------------------------------------

    def list_skills(self) -> ListSkillSummaryResponse:
        return self._runner.run(self._async.list_skills())

    # -- memories ------------------------------------------------------------

    def list_memories(
        self, *, mem_type: str = "", scope: str = "", scope_id: str = "", limit: int = 100
    ) -> ListMemoriesResponse:
        return self._runner.run(
            self._async.list_memories(
                mem_type=mem_type, scope=scope, scope_id=scope_id, limit=limit
            )
        )

    def save_memory(
        self,
        name: str,
        content: str,
        *,
        description: str = "",
        mem_type: str = "project",
        scope: str = "global",
        scope_id: str = "",
    ) -> MemoryInfo:
        return self._runner.run(
            self._async.save_memory(
                name,
                content,
                description=description,
                mem_type=mem_type,
                scope=scope,
                scope_id=scope_id,
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
    ) -> ListMemoriesResponse:
        return self._runner.run(
            self._async.search_memories(
                query, mem_type=mem_type, scope=scope, scope_id=scope_id, limit=limit
            )
        )

    def delete_memory(
        self, name: str, *, scope: str = "global", scope_id: str = ""
    ) -> StatusResponse:
        return self._runner.run(self._async.delete_memory(name, scope=scope, scope_id=scope_id))

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
