"""Server endpoint catalog for OpenAPI spec generation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from turnstone.api.openapi import EndpointSpec, QueryParam, build_openapi

if TYPE_CHECKING:
    from pydantic import BaseModel
from turnstone.api.schemas import (
    AuthLoginRequest,
    AuthLoginResponse,
    AuthSetupRequest,
    AuthSetupResponse,
    AuthStatusResponse,
    AuthWhoamiResponse,
    ErrorResponse,
    StatusResponse,
)
from turnstone.api.server_schemas import (
    ApproveRequest,
    AvailableModelInfo,
    CancelRequest,
    CommandRequest,
    CreateWorkstreamRequest,
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
    PlanFeedbackRequest,
    SaveMemoryRequest,
    SearchMemoriesRequest,
    SendRequest,
    SendResponse,
    SkillSummary,
    UploadAttachmentResponse,
    WorkstreamDetailResponse,
    WorkstreamHistoryResponse,
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
        description=(
            "Accepts two content types. Default is `application/json` with a "
            "`CreateWorkstreamRequest` body. Alternatively, `multipart/form-data` "
            "with one `meta` field (JSON-encoded `CreateWorkstreamRequest` shape) "
            "plus zero-or-more `file` parts saves each file as an attachment "
            "under the new workstream. When `initial_message` is also set, "
            "attachments are reserved onto that turn before the worker thread "
            "dispatches; otherwise they remain pending for a follow-up "
            "`POST /v1/api/send`."
        ),
        request_model=CreateWorkstreamRequest,
        response_model=CreateWorkstreamResponse,
        error_codes=[400, 409, 413],
        tags=["Workstreams"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/close",
        "POST",
        "Close a workstream",
        response_model=StatusResponse,
        error_codes=[400, 404],
        tags=["Workstreams"],
    ),
    # --- Chat ---
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/send",
        "POST",
        "Send a user message",
        request_model=SendRequest,
        response_model=SendResponse,
        error_codes=[400, 404],
        tags=["Chat"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/send",
        "DELETE",
        "Cancel a queued message",
        description=(
            "Removes a previously-queued message from the workstream's "
            'pending queue. Body: ``{"msg_id": "..."}``. Returns '
            "``status: removed`` when the queue had the entry, "
            "``status: not_found`` otherwise."
        ),
        error_codes=[400, 404],
        tags=["Chat"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/approve",
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
        "/v1/api/workstreams/{ws_id}/cancel",
        "POST",
        "Cancel the active generation in a workstream",
        request_model=CancelRequest,
        response_model=StatusResponse,
        error_codes=[400, 404],
        tags=["Chat"],
    ),
    # --- Streaming ---
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/events",
        "GET",
        "Per-workstream SSE event stream",
        description="Opens a Server-Sent Events stream scoped to a single workstream. "
        "Returns text/event-stream. See API reference for event types.",
        error_codes=[404],
        tags=["Streaming"],
    ),
    EndpointSpec(
        "/v1/api/events/global",
        "GET",
        "Global SSE event stream",
        description="Server-Sent Events stream for node-level state broadcasts. "
        "Emits a node_snapshot event on connect (workstreams, health, aggregate), "
        "followed by real-time delta events (ws_state, ws_activity, ws_created, "
        "ws_closed, ws_rename, health_changed, aggregate). "
        "Pass ?expected_node_id=X for identity verification (returns 409 on mismatch).",
        tags=["Streaming"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/delete",
        "POST",
        "Permanently delete a saved workstream",
        error_codes=[400, 404, 500],
        tags=["Workstreams"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/open",
        "POST",
        "Load a saved workstream into memory",
        error_codes=[400, 404, 500],
        tags=["Workstreams"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/title",
        "POST",
        "Set workstream title manually",
        error_codes=[400, 409],
        tags=["Workstreams"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/refresh-title",
        "POST",
        "Regenerate workstream title via LLM",
        error_codes=[404],
        tags=["Workstreams"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}",
        "GET",
        "Get workstream detail (rehydrates lazily on miss)",
        description=(
            "Returns the persisted workstream's display fields. If the "
            "session isn't currently in memory the manager rehydrates it "
            "before responding; ``500`` on rehydrate failure carries a "
            "correlation id matching the server log line. Lifted from "
            "the coord-only surface in the Stage 2 history/detail verb "
            "lift — interactive previously had no detail endpoint."
        ),
        response_model=WorkstreamDetailResponse,
        error_codes=[400, 404, 500, 503],
        tags=["Workstreams"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/history",
        "GET",
        "Read the workstream's reconstructed message history",
        description=(
            "Returns the tail of the conversation in OpenAI-like message "
            "format. Persisted-but-not-loaded workstreams (closed / "
            "evicted) serve history without rehydrating. Lifted from "
            "the coord-only surface in the Stage 2 history/detail verb "
            "lift — interactive previously only exposed history through "
            "the SSE replay on ``/events``."
        ),
        response_model=WorkstreamHistoryResponse,
        query_params=[
            QueryParam(
                "limit",
                "Max conversation rows to fetch from storage (default 100, max 500).",
                schema_type="integer",
                default=100,
            ),
        ],
        error_codes=[400, 404, 500, 503],
        tags=["Workstreams"],
    ),
    # --- Workstream attachments ---
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/attachments",
        "POST",
        "Upload a file (multipart/form-data, field 'file') and attach it "
        "to the caller's next user turn on this workstream.  Validates "
        "size, MIME, and UTF-8 for text; magic-byte sniff for images.  "
        "Ownership failures are masked as 404 so non-owners cannot "
        "enumerate workstream existence; a 403 indicates a scope/auth "
        "failure from the middleware layer.",
        response_model=UploadAttachmentResponse,
        error_codes=[400, 403, 404, 409, 413],
        tags=["Attachments"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/attachments",
        "GET",
        "List the caller's pending (unconsumed) attachments for this "
        "workstream.  Ownership failures are masked as 404.",
        response_model=ListAttachmentsResponse,
        error_codes=[403, 404],
        tags=["Attachments"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/attachments/{attachment_id}/content",
        "GET",
        "Return raw bytes of an attachment with its stored Content-Type.  "
        "Ownership failures are masked as 404.",
        error_codes=[403, 404],
        tags=["Attachments"],
    ),
    EndpointSpec(
        "/v1/api/workstreams/{ws_id}/attachments/{attachment_id}",
        "DELETE",
        "Remove a pending attachment (consumed attachments return 404).  "
        "Ownership failures are also masked as 404.",
        error_codes=[403, 404],
        tags=["Attachments"],
    ),
    # --- Saved workstreams ---
    EndpointSpec(
        "/v1/api/workstreams/saved",
        "GET",
        "List saved workstreams",
        response_model=ListSavedWorkstreamsResponse,
        tags=["Workstreams"],
    ),
    # --- Skills ---
    EndpointSpec(
        "/v1/api/skills",
        "GET",
        "List available skills (summary)",
        response_model=ListSkillSummaryResponse,
        tags=["Skills"],
    ),
    # --- Models ---
    EndpointSpec(
        "/v1/api/models",
        "GET",
        "List available model aliases",
        response_model=ListAvailableModelsResponse,
        tags=["Models"],
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
    EndpointSpec(
        "/v1/api/auth/oidc/authorize",
        "GET",
        "Redirect to OIDC provider for SSO login",
        response_code=302,
        error_codes=[404, 503],
        tags=["Auth"],
    ),
    EndpointSpec(
        "/v1/api/auth/oidc/callback",
        "GET",
        "OIDC callback — validates code, provisions user, sets JWT cookie, redirects to app",
        response_code=302,
        tags=["Auth"],
    ),
    EndpointSpec(
        "/v1/api/auth/whoami",
        "GET",
        "Return authenticated user info and permissions",
        response_model=AuthWhoamiResponse,
        error_codes=[401],
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
    # --- Admin settings ---
    EndpointSpec(
        "/v1/api/admin/settings",
        "GET",
        "List interface.* settings with values and sources",
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/settings/{key}",
        "PUT",
        "Update an interface.* setting",
        error_codes=[400, 503],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/settings/{key}",
        "POST",
        "Update an interface.* setting (alias for PUT)",
        error_codes=[400, 503],
        tags=["Admin"],
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
    ListWorkstreamsResponse,
    WorkstreamDetailResponse,
    WorkstreamHistoryResponse,
    DashboardResponse,
    ListSavedWorkstreamsResponse,
    UploadAttachmentResponse,
    ListAttachmentsResponse,
    HealthResponse,
    SaveMemoryRequest,
    MemoryInfo,
    ListMemoriesResponse,
    SearchMemoriesRequest,
    SkillSummary,
    ListSkillSummaryResponse,
    AvailableModelInfo,
    ListAvailableModelsResponse,
]


def build_server_spec() -> dict[str, Any]:
    """Build the OpenAPI spec for the turnstone server."""
    return build_openapi(
        title="turnstone Server API",
        description="Single-node workstream management, chat interaction, and real-time streaming.",
        endpoints=SERVER_ENDPOINTS,
        models=_ALL_MODELS,
    )
