"""Server endpoint catalog for OpenAPI spec generation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from turnstone.api.console_schemas import ListWsTemplateSummaryResponse, WsTemplateSummary
from turnstone.api.openapi import EndpointSpec, QueryParam, build_openapi

if TYPE_CHECKING:
    from pydantic import BaseModel
from turnstone.api.schemas import (
    AuthLoginRequest,
    AuthLoginResponse,
    AuthSetupRequest,
    AuthSetupResponse,
    AuthStatusResponse,
    ErrorResponse,
    StatusResponse,
)
from turnstone.api.server_schemas import (
    ApproveRequest,
    CancelRequest,
    CloseWorkstreamRequest,
    CommandRequest,
    CreateWorkstreamRequest,
    CreateWorkstreamResponse,
    DashboardResponse,
    HealthResponse,
    ListMemoriesResponse,
    ListPromptTemplateSummaryResponse,
    ListSavedWorkstreamsResponse,
    ListWorkstreamsResponse,
    MemoryInfo,
    PlanFeedbackRequest,
    PromptTemplateSummary,
    SaveMemoryRequest,
    SearchMemoriesRequest,
    SendRequest,
    SendResponse,
)

SERVER_ENDPOINTS: list[EndpointSpec] = [
    # --- Workstream management ---
    EndpointSpec(
        "/v1/api/workstreams",
        "GET",
        "List active workstreams",
        response_model=ListWorkstreamsResponse,
        tags=["Workstreams"],
    ),
    EndpointSpec(
        "/v1/api/dashboard",
        "GET",
        "Dashboard with workstream details and aggregates",
        response_model=DashboardResponse,
        tags=["Workstreams"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/new",
        "POST",
        "Create a new workstream",
        request_model=CreateWorkstreamRequest,
        response_model=CreateWorkstreamResponse,
        error_codes=[400],
        tags=["Workstreams"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/close",
        "POST",
        "Close a workstream",
        request_model=CloseWorkstreamRequest,
        response_model=StatusResponse,
        error_codes=[400],
        tags=["Workstreams"],
    ),
    # --- Chat ---
    EndpointSpec(
        "/v1/api/send",
        "POST",
        "Send a user message",
        request_model=SendRequest,
        response_model=SendResponse,
        error_codes=[400, 404],
        tags=["Chat"],
    ),
    EndpointSpec(
        "/v1/api/approve",
        "POST",
        "Approve or deny a tool call",
        request_model=ApproveRequest,
        response_model=StatusResponse,
        error_codes=[404],
        tags=["Chat"],
    ),
    EndpointSpec(
        "/v1/api/plan",
        "POST",
        "Respond to a plan review",
        request_model=PlanFeedbackRequest,
        response_model=StatusResponse,
        error_codes=[404],
        tags=["Chat"],
    ),
    EndpointSpec(
        "/v1/api/command",
        "POST",
        "Execute a slash command",
        request_model=CommandRequest,
        response_model=StatusResponse,
        error_codes=[400, 404],
        tags=["Chat"],
    ),
    EndpointSpec(
        "/v1/api/cancel",
        "POST",
        "Cancel the active generation in a workstream",
        request_model=CancelRequest,
        response_model=StatusResponse,
        error_codes=[400, 404],
        tags=["Chat"],
    ),
    # --- Streaming ---
    EndpointSpec(
        "/v1/api/events",
        "GET",
        "Per-workstream SSE event stream",
        description="Opens a Server-Sent Events stream scoped to a single workstream. "
        "Returns text/event-stream. See API reference for event types.",
        query_params=[QueryParam("ws_id", "Workstream identifier", required=True)],
        error_codes=[404],
        tags=["Streaming"],
    ),
    EndpointSpec(
        "/v1/api/events/global",
        "GET",
        "Global SSE event stream",
        description="Global Server-Sent Events stream for state-change broadcasts "
        "across all workstreams. Returns text/event-stream.",
        tags=["Streaming"],
    ),
    # --- Saved workstreams ---
    EndpointSpec(
        "/v1/api/workstreams/saved",
        "GET",
        "List saved workstreams",
        response_model=ListSavedWorkstreamsResponse,
        tags=["Workstreams"],
    ),
    # --- Prompt templates ---
    EndpointSpec(
        "/v1/api/templates",
        "GET",
        "List available prompt templates (summary)",
        response_model=ListPromptTemplateSummaryResponse,
        tags=["Templates"],
    ),
    # --- Workstream templates ---
    EndpointSpec(
        "/v1/api/ws-templates",
        "GET",
        "List enabled workstream templates (summary)",
        response_model=ListWsTemplateSummaryResponse,
        tags=["Templates"],
    ),
    # --- Auth ---
    EndpointSpec(
        "/v1/api/auth/login",
        "POST",
        "Authenticate with a token",
        request_model=AuthLoginRequest,
        response_model=AuthLoginResponse,
        error_codes=[401],
        tags=["Auth"],
    ),
    EndpointSpec(
        "/v1/api/auth/setup",
        "POST",
        "Create first admin user",
        request_model=AuthSetupRequest,
        response_model=AuthSetupResponse,
        error_codes=[400, 409, 503],
        tags=["Auth"],
    ),
    EndpointSpec(
        "/v1/api/auth/status",
        "GET",
        "Return auth state",
        response_model=AuthStatusResponse,
        tags=["Auth"],
    ),
    EndpointSpec(
        "/v1/api/auth/logout",
        "POST",
        "Clear auth cookie",
        response_model=StatusResponse,
        tags=["Auth"],
    ),
    # --- Memories ---
    EndpointSpec(
        "/v1/api/memories",
        "GET",
        "List structured memories",
        response_model=ListMemoriesResponse,
        query_params=[
            QueryParam("type", "Filter by memory type"),
            QueryParam("scope", "Filter by scope"),
            QueryParam("scope_id", "Filter by scope identifier"),
            QueryParam(
                "limit", "Max results (default 100, max 200)", schema_type="integer", default=100
            ),
        ],
        tags=["Memories"],
    ),
    EndpointSpec(
        "/v1/api/memories",
        "POST",
        "Save (upsert) a structured memory",
        request_model=SaveMemoryRequest,
        response_model=MemoryInfo,
        error_codes=[400],
        tags=["Memories"],
    ),
    EndpointSpec(
        "/v1/api/memories/search",
        "POST",
        "Search structured memories by query",
        request_model=SearchMemoriesRequest,
        response_model=ListMemoriesResponse,
        tags=["Memories"],
    ),
    EndpointSpec(
        "/v1/api/memories/{name}",
        "DELETE",
        "Delete a structured memory by name and scope",
        response_model=StatusResponse,
        query_params=[
            QueryParam("scope", "Scope (default: global)"),
            QueryParam("scope_id", "Scope identifier"),
        ],
        error_codes=[404],
        tags=["Memories"],
    ),
    # --- Observability ---
    EndpointSpec(
        "/health",
        "GET",
        "Server health check",
        response_model=HealthResponse,
        tags=["Observability"],
    ),
]

_ALL_MODELS: list[type[BaseModel]] = [
    ErrorResponse,
    StatusResponse,
    AuthLoginRequest,
    AuthLoginResponse,
    AuthSetupRequest,
    AuthSetupResponse,
    AuthStatusResponse,
    SendRequest,
    SendResponse,
    ApproveRequest,
    PlanFeedbackRequest,
    CommandRequest,
    CancelRequest,
    CreateWorkstreamRequest,
    CreateWorkstreamResponse,
    CloseWorkstreamRequest,
    ListWorkstreamsResponse,
    DashboardResponse,
    ListSavedWorkstreamsResponse,
    HealthResponse,
    SaveMemoryRequest,
    MemoryInfo,
    ListMemoriesResponse,
    SearchMemoriesRequest,
    PromptTemplateSummary,
    ListPromptTemplateSummaryResponse,
    WsTemplateSummary,
    ListWsTemplateSummaryResponse,
]


def build_server_spec() -> dict[str, Any]:
    """Build the OpenAPI spec for the turnstone server."""
    return build_openapi(
        title="turnstone Server API",
        description="Single-node workstream management, chat interaction, and real-time streaming.",
        endpoints=SERVER_ENDPOINTS,
        models=_ALL_MODELS,
    )
