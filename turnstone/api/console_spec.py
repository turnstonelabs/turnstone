"""Console endpoint catalog for OpenAPI spec generation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic import BaseModel

from turnstone.api.console_schemas import (
    ClusterNodesResponse,
    ClusterOverviewResponse,
    ClusterWorkstreamsResponse,
    ConsoleCreateWsRequest,
    ConsoleCreateWsResponse,
    ConsoleHealthResponse,
    NodeDetailResponse,
)
from turnstone.api.openapi import EndpointSpec, QueryParam, build_openapi
from turnstone.api.schemas import (
    AuthLoginRequest,
    AuthLoginResponse,
    AuthSetupRequest,
    AuthSetupResponse,
    AuthStatusResponse,
    CreateScheduleRequest,
    CreateTokenRequest,
    CreateTokenResponse,
    CreateUserRequest,
    ErrorResponse,
    ListScheduleRunsResponse,
    ListSchedulesResponse,
    ListTokensResponse,
    ListUsersResponse,
    ScheduleInfo,
    StatusResponse,
    UpdateScheduleRequest,
    UserInfo,
)

CONSOLE_ENDPOINTS: list[EndpointSpec] = [
    # --- Cluster ---
    EndpointSpec(
        "/v1/api/cluster/overview",
        "GET",
        "Cluster state summary",
        response_model=ClusterOverviewResponse,
        tags=["Cluster"],
    ),
    EndpointSpec(
        "/v1/api/cluster/nodes",
        "GET",
        "Paginated node list",
        response_model=ClusterNodesResponse,
        query_params=[
            QueryParam(
                "sort", "Sort field", default="activity", enum=["activity", "tokens", "name"]
            ),
            QueryParam("limit", "Page size", schema_type="integer", default=100),
            QueryParam("offset", "Pagination offset", schema_type="integer", default=0),
        ],
        tags=["Cluster"],
    ),
    EndpointSpec(
        "/v1/api/cluster/workstreams",
        "GET",
        "Filtered workstream list",
        response_model=ClusterWorkstreamsResponse,
        query_params=[
            QueryParam(
                "state",
                "Filter by state",
                enum=["running", "thinking", "attention", "idle", "error"],
            ),
            QueryParam("node", "Filter by node_id"),
            QueryParam("search", "Search in name/title/node"),
            QueryParam("sort", "Sort field", default="state", enum=["state", "tokens", "name"]),
            QueryParam("page", "Page number", schema_type="integer", default=1),
            QueryParam("per_page", "Items per page (max 200)", schema_type="integer", default=50),
        ],
        tags=["Cluster"],
    ),
    EndpointSpec(
        "/v1/api/cluster/node/{node_id}",
        "GET",
        "Single node detail",
        response_model=NodeDetailResponse,
        error_codes=[404],
        tags=["Cluster"],
    ),
    EndpointSpec(
        "/v1/api/cluster/workstreams/new",
        "POST",
        "Create workstream via MQ dispatch",
        request_model=ConsoleCreateWsRequest,
        response_model=ConsoleCreateWsResponse,
        error_codes=[400, 404, 503],
        tags=["Cluster"],
    ),
    # --- Streaming ---
    EndpointSpec(
        "/v1/api/cluster/events",
        "GET",
        "Cluster SSE event stream",
        description="Server-Sent Events stream for real-time cluster updates. "
        "Returns text/event-stream with node_joined, node_lost, cluster_state, "
        "ws_created, ws_closed, ws_rename events.",
        tags=["Streaming"],
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
    # --- Admin ---
    EndpointSpec(
        "/v1/api/admin/users",
        "GET",
        "List all users",
        response_model=ListUsersResponse,
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/users",
        "POST",
        "Create a user",
        request_model=CreateUserRequest,
        response_model=UserInfo,
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/users/{user_id}",
        "DELETE",
        "Delete a user and their tokens",
        response_model=StatusResponse,
        error_codes=[404],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/users/{user_id}/tokens",
        "GET",
        "List tokens for a user",
        response_model=ListTokensResponse,
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/users/{user_id}/tokens",
        "POST",
        "Create an API token (raw token shown once)",
        request_model=CreateTokenRequest,
        response_model=CreateTokenResponse,
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/tokens/{token_id}",
        "DELETE",
        "Revoke an API token",
        response_model=StatusResponse,
        error_codes=[404],
        tags=["Admin"],
    ),
    # --- Schedules ---
    EndpointSpec(
        "/v1/api/admin/schedules",
        "GET",
        "List all scheduled tasks",
        response_model=ListSchedulesResponse,
        tags=["Schedules"],
    ),
    EndpointSpec(
        "/v1/api/admin/schedules",
        "POST",
        "Create a scheduled task",
        request_model=CreateScheduleRequest,
        response_model=ScheduleInfo,
        error_codes=[400],
        tags=["Schedules"],
    ),
    EndpointSpec(
        "/v1/api/admin/schedules/{task_id}",
        "GET",
        "Get a scheduled task",
        response_model=ScheduleInfo,
        error_codes=[404],
        tags=["Schedules"],
    ),
    EndpointSpec(
        "/v1/api/admin/schedules/{task_id}",
        "PUT",
        "Update a scheduled task",
        request_model=UpdateScheduleRequest,
        response_model=ScheduleInfo,
        error_codes=[400, 404],
        tags=["Schedules"],
    ),
    EndpointSpec(
        "/v1/api/admin/schedules/{task_id}",
        "DELETE",
        "Delete a scheduled task",
        response_model=StatusResponse,
        error_codes=[404],
        tags=["Schedules"],
    ),
    EndpointSpec(
        "/v1/api/admin/schedules/{task_id}/runs",
        "GET",
        "List run history for a scheduled task",
        response_model=ListScheduleRunsResponse,
        query_params=[
            QueryParam(
                "limit", "Max results (default 50, max 200)", schema_type="integer", default=50
            ),
        ],
        error_codes=[404],
        tags=["Schedules"],
    ),
    # --- Observability ---
    EndpointSpec(
        "/health",
        "GET",
        "Console health check",
        response_model=ConsoleHealthResponse,
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
    CreateUserRequest,
    UserInfo,
    ListUsersResponse,
    CreateTokenRequest,
    CreateTokenResponse,
    ListTokensResponse,
    ClusterOverviewResponse,
    ClusterNodesResponse,
    ClusterWorkstreamsResponse,
    NodeDetailResponse,
    ConsoleCreateWsRequest,
    ConsoleCreateWsResponse,
    ConsoleHealthResponse,
    CreateScheduleRequest,
    UpdateScheduleRequest,
    ScheduleInfo,
    ListSchedulesResponse,
    ListScheduleRunsResponse,
]


def build_console_spec() -> dict[str, Any]:
    """Build the OpenAPI spec for the turnstone console."""
    return build_openapi(
        title="turnstone Console API",
        description="Cluster-wide visibility and control across all turnstone nodes.",
        endpoints=CONSOLE_ENDPOINTS,
        models=_ALL_MODELS,
    )
