"""Console endpoint catalog for OpenAPI spec generation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic import BaseModel

from turnstone.api.console_schemas import (
    AdminMemoryInfo,
    AssignRoleRequest,
    AuditEventInfo,
    AvailableModelInfo,
    BulkSetNodeMetadataRequest,
    ChannelUserInfo,
    ClusterNodesResponse,
    ClusterOverviewResponse,
    ClusterSnapshotResponse,
    ClusterWorkstreamsResponse,
    ClusterWsDetailResponse,
    ConsoleCreateWsRequest,
    ConsoleCreateWsResponse,
    ConsoleHealthResponse,
    CoordinatorApproveRequest,
    CoordinatorChildInfo,
    CoordinatorChildrenResponse,
    CoordinatorCloseAllChildrenRequest,
    CoordinatorCloseAllChildrenResponse,
    CoordinatorCreateRequest,
    CoordinatorCreateResponse,
    CoordinatorDetailResponse,
    CoordinatorHistoryResponse,
    CoordinatorInfo,
    CoordinatorListResponse,
    CoordinatorOpenResponse,
    CoordinatorQuotaRequest,
    CoordinatorQuotaResponse,
    CoordinatorRestrictRequest,
    CoordinatorRestrictResponse,
    CoordinatorSendRequest,
    CoordinatorSpawnRateInput,
    CoordinatorSpawnRateState,
    CoordinatorStopCascadeResponse,
    CoordinatorTaskInfo,
    CoordinatorTasksResponse,
    CoordinatorTrustRequest,
    CoordinatorTrustResponse,
    CreateChannelUserRequest,
    CreateMcpServerRequest,
    CreateModelDefinitionRequest,
    CreateRoleRequest,
    CreateSkillRequest,
    CreateSkillResourceRequest,
    CreateToolPolicyRequest,
    DetectModelRequest,
    DetectModelResponse,
    ImportMcpConfigRequest,
    ImportMcpConfigResponse,
    KnownModelsResponse,
    ListAdminMemoriesResponse,
    ListAuditEventsResponse,
    ListAvailableModelsResponse,
    ListChannelUsersResponse,
    ListMcpServersResponse,
    ListModelDefinitionsResponse,
    ListOrgsResponse,
    ListOutputAssessmentsResponse,
    ListRolesResponse,
    ListSettingSchemaResponse,
    ListSettingsResponse,
    ListSkillResourcesResponse,
    ListSkillsResponse,
    ListSkillVersionsResponse,
    ListToolPoliciesResponse,
    ListUserRolesResponse,
    ListVerdictsResponse,
    McpReloadResponse,
    McpServerDetail,
    ModelCapabilitiesResponse,
    ModelDefinitionInfo,
    ModelReloadResponse,
    NodeDetailResponse,
    NodeMetadataResponse,
    OrgInfo,
    OutputAssessmentInfo,
    RegistryInstallRequest,
    RegistrySearchResponse,
    RoleInfo,
    RouteCreateResponse,
    RouteResponse,
    SetNodeMetadataValueRequest,
    SettingInfo,
    SettingSchemaInfo,
    SkillDiscoverResponse,
    SkillInfo,
    SkillInstallRequest,
    SkillInstallResponse,
    SkillResourceInfo,
    SkillVersionInfo,
    ToolPolicyInfo,
    UpdateMcpServerRequest,
    UpdateModelDefinitionRequest,
    UpdateOrgRequest,
    UpdateRoleRequest,
    UpdateSettingRequest,
    UpdateSkillRequest,
    UpdateToolPolicyRequest,
    UsageBreakdownItem,
    UsageResponse,
    UserRoleInfo,
    VerdictInfo,
)
from turnstone.api.openapi import EndpointSpec, QueryParam, build_openapi
from turnstone.api.schemas import (
    AuthLoginRequest,
    AuthLoginResponse,
    AuthSetupRequest,
    AuthSetupResponse,
    AuthStatusResponse,
    AuthWhoamiResponse,
    CreateScheduleRequest,
    CreateTokenRequest,
    CreateTokenResponse,
    CreateUserRequest,
    DeleteSettingResponse,
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
from turnstone.api.server_schemas import (
    ListSkillSummaryResponse,
    SkillSummary,
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
        "Create workstream via HTTP dispatch",
        request_model=ConsoleCreateWsRequest,
        response_model=ConsoleCreateWsResponse,
        error_codes=[400, 404, 503],
        tags=["Cluster"],
    ),
    EndpointSpec(
        "/v1/api/cluster/snapshot",
        "GET",
        "Full cluster state snapshot",
        description="Returns the complete cluster state: all nodes with their workstreams "
        "and overview aggregates. Used for initial load and reconnection.",
        response_model=ClusterSnapshotResponse,
        tags=["Cluster"],
    ),
    # --- Streaming ---
    EndpointSpec(
        "/v1/api/cluster/events",
        "GET",
        "Cluster SSE event stream",
        description="Server-Sent Events stream for real-time cluster updates. "
        "First event is a 'snapshot' with full cluster state, followed by "
        "node_joined, node_lost, cluster_state, ws_created, ws_closed, ws_rename events.",
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
    # --- Channels ---
    EndpointSpec(
        "/v1/api/admin/users/{user_id}/channels",
        "GET",
        "List channel links for a user",
        response_model=ListChannelUsersResponse,
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/users/{user_id}/channels",
        "POST",
        "Link a channel account to a user",
        request_model=CreateChannelUserRequest,
        response_model=ChannelUserInfo,
        error_codes=[400, 404, 409],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/channels/{channel_type}/{channel_user_id}",
        "DELETE",
        "Unlink a channel account",
        response_model=StatusResponse,
        error_codes=[404],
        tags=["Admin"],
    ),
    # --- OIDC Identities ---
    EndpointSpec(
        "/v1/api/admin/users/{user_id}/oidc-identities",
        "GET",
        "List OIDC identities linked to a user",
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/oidc-identities",
        "DELETE",
        "Unlink an OIDC identity (issuer + subject as query params)",
        error_codes=[400, 404],
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
    # --- Governance: Roles ---
    EndpointSpec(
        "/v1/api/admin/roles",
        "GET",
        "List all roles",
        response_model=ListRolesResponse,
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/roles",
        "POST",
        "Create a custom role",
        request_model=CreateRoleRequest,
        response_model=RoleInfo,
        error_codes=[400],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/roles/{role_id}",
        "PUT",
        "Update a role",
        request_model=UpdateRoleRequest,
        response_model=RoleInfo,
        error_codes=[400, 404],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/roles/{role_id}",
        "DELETE",
        "Delete a custom role",
        response_model=StatusResponse,
        error_codes=[400, 404],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/users/{user_id}/roles",
        "GET",
        "List roles assigned to a user",
        response_model=ListUserRolesResponse,
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/users/{user_id}/roles",
        "POST",
        "Assign a role to a user",
        request_model=AssignRoleRequest,
        response_model=StatusResponse,
        error_codes=[400, 404],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/users/{user_id}/roles/{role_id}",
        "DELETE",
        "Unassign a role from a user",
        response_model=StatusResponse,
        error_codes=[404],
        tags=["Admin"],
    ),
    # --- Governance: Orgs ---
    EndpointSpec(
        "/v1/api/admin/orgs",
        "GET",
        "List organizations",
        response_model=ListOrgsResponse,
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/orgs/{org_id}",
        "GET",
        "Get organization details",
        response_model=OrgInfo,
        error_codes=[404],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/orgs/{org_id}",
        "PUT",
        "Update organization settings",
        request_model=UpdateOrgRequest,
        response_model=OrgInfo,
        error_codes=[404],
        tags=["Admin"],
    ),
    # --- Governance: Tool Policies ---
    EndpointSpec(
        "/v1/api/admin/policies",
        "GET",
        "List tool policies",
        response_model=ListToolPoliciesResponse,
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/policies",
        "POST",
        "Create a tool policy",
        request_model=CreateToolPolicyRequest,
        response_model=ToolPolicyInfo,
        error_codes=[400],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/policies/{policy_id}",
        "PUT",
        "Update a tool policy",
        request_model=UpdateToolPolicyRequest,
        response_model=ToolPolicyInfo,
        error_codes=[404],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/policies/{policy_id}",
        "DELETE",
        "Delete a tool policy",
        response_model=StatusResponse,
        error_codes=[404],
        tags=["Admin"],
    ),
    # --- Governance: Skill Discovery ---
    EndpointSpec(
        "/v1/api/admin/skills/discover",
        "GET",
        "Search external skill registries for available skills",
        response_model=SkillDiscoverResponse,
        query_params=[
            QueryParam("q", "Search query"),
            QueryParam("limit", "Max results (default 20, max 100)", schema_type="integer"),
        ],
        error_codes=[502],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/skills/install",
        "POST",
        "Install skill(s) from an external source",
        request_model=SkillInstallRequest,
        response_model=SkillInstallResponse,
        error_codes=[400, 404, 409, 502],
        tags=["Admin"],
    ),
    # --- Governance: Skills ---
    EndpointSpec(
        "/v1/api/admin/skills",
        "GET",
        "List skills",
        response_model=ListSkillsResponse,
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/skills",
        "POST",
        "Create a skill",
        request_model=CreateSkillRequest,
        response_model=SkillInfo,
        error_codes=[400],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/skills/{skill_id}",
        "GET",
        "Get a skill by ID",
        response_model=SkillInfo,
        error_codes=[404],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/skills/{skill_id}",
        "PUT",
        "Update a skill",
        request_model=UpdateSkillRequest,
        response_model=SkillInfo,
        error_codes=[404],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/skills/{skill_id}",
        "DELETE",
        "Delete a skill",
        error_codes=[404],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/skills/{skill_id}/versions",
        "GET",
        "List version history for a skill",
        response_model=ListSkillVersionsResponse,
        tags=["Admin"],
    ),
    # --- Models ---
    EndpointSpec(
        "/v1/api/models",
        "GET",
        "List enabled model aliases for workstream creation",
        response_model=ListAvailableModelsResponse,
        tags=["Models"],
    ),
    # --- Skills ---
    EndpointSpec(
        "/v1/api/skills",
        "GET",
        "List available skills (summary)",
        response_model=ListSkillSummaryResponse,
        tags=["Skills"],
    ),
    # --- Governance: Usage & Audit ---
    EndpointSpec(
        "/v1/api/admin/usage",
        "GET",
        "Aggregated usage data",
        response_model=UsageResponse,
        query_params=[
            QueryParam("since", "Start timestamp (ISO8601, defaults to last 7 days)"),
            QueryParam("until", "End timestamp (ISO8601)"),
            QueryParam("user_id", "Filter by user"),
            QueryParam("model", "Filter by model"),
            QueryParam(
                "group_by",
                "Group results",
                enum=["day", "hour", "model", "user"],
            ),
        ],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/audit",
        "GET",
        "Paginated audit events",
        response_model=ListAuditEventsResponse,
        query_params=[
            QueryParam("action", "Filter by action type"),
            QueryParam("user_id", "Filter by user"),
            QueryParam("since", "Start timestamp (ISO8601)"),
            QueryParam("until", "End timestamp (ISO8601)"),
            QueryParam("limit", "Page size", schema_type="integer", default=50),
            QueryParam("offset", "Pagination offset", schema_type="integer", default=0),
        ],
        tags=["Admin"],
    ),
    # --- Governance: Intent Verdicts ---
    EndpointSpec(
        "/v1/api/admin/verdicts",
        "GET",
        "Paginated intent verdicts",
        response_model=ListVerdictsResponse,
        query_params=[
            QueryParam("ws_id", "Filter by workstream"),
            QueryParam("since", "Start timestamp (ISO8601)"),
            QueryParam("until", "End timestamp (ISO8601)"),
            QueryParam(
                "risk_level",
                "Filter by risk level",
                enum=["low", "medium", "high", "critical"],
            ),
            QueryParam("limit", "Page size (max 500)", schema_type="integer", default=100),
            QueryParam("offset", "Pagination offset", schema_type="integer", default=0),
        ],
        tags=["Admin"],
    ),
    # --- Admin: Output Guard ---
    EndpointSpec(
        "/v1/api/admin/output-assessments",
        "GET",
        "Paginated output guard assessments",
        response_model=ListOutputAssessmentsResponse,
        query_params=[
            QueryParam("ws_id", "Filter by workstream"),
            QueryParam("risk_level", "Filter by risk level", enum=["low", "medium", "high"]),
            QueryParam("since", "Start timestamp (ISO8601)"),
            QueryParam("until", "End timestamp (ISO8601)"),
            QueryParam("limit", "Page size (max 500)", schema_type="integer", default=100),
            QueryParam("offset", "Pagination offset", schema_type="integer", default=0),
        ],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/skills/{skill_id}/rescan",
        "POST",
        "Re-scan a skill for security signals",
        tags=["Admin"],
    ),
    # --- Governance: Skill Resources ---
    EndpointSpec(
        "/v1/api/admin/skills/{skill_id}/resources",
        "GET",
        "List resource files for a skill",
        response_model=ListSkillResourcesResponse,
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/skills/{skill_id}/resources",
        "POST",
        "Upload a resource file to a skill",
        request_model=CreateSkillResourceRequest,
        response_model=SkillResourceInfo,
        response_code=201,
        error_codes=[400, 404, 409],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/skills/{skill_id}/resources/{path}",
        "GET",
        "Get a single skill resource by path",
        response_model=SkillResourceInfo,
        error_codes=[404],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/skills/{skill_id}/resources/{path}",
        "DELETE",
        "Delete a skill resource by path",
        error_codes=[404],
        tags=["Admin"],
    ),
    # --- Admin: Memories ---
    EndpointSpec(
        "/v1/api/admin/memories",
        "GET",
        "List structured memories",
        response_model=ListAdminMemoriesResponse,
        query_params=[
            QueryParam("type", "Filter by memory type"),
            QueryParam("scope", "Filter by scope"),
            QueryParam("scope_id", "Filter by scope identifier"),
            QueryParam("limit", "Page size (max 200)", schema_type="integer", default=100),
        ],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/memories/search",
        "GET",
        "Search memories by query",
        response_model=ListAdminMemoriesResponse,
        query_params=[
            QueryParam("q", "Search query", required=True),
            QueryParam("type", "Filter by memory type"),
            QueryParam("scope", "Filter by scope"),
            QueryParam("scope_id", "Filter by scope identifier"),
            QueryParam("limit", "Max results", schema_type="integer", default=20),
        ],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/memories/{memory_id}",
        "GET",
        "Get a single memory by ID",
        response_model=AdminMemoryInfo,
        error_codes=[404],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/memories/{memory_id}",
        "DELETE",
        "Delete a memory by ID",
        response_model=StatusResponse,
        error_codes=[404],
        tags=["Admin"],
    ),
    # --- Admin: System Settings ---
    EndpointSpec(
        "/v1/api/admin/settings",
        "GET",
        "List all settings with effective values",
        response_model=ListSettingsResponse,
        query_params=[
            QueryParam("reveal", "Show secret values in plaintext", schema_type="boolean"),
        ],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/settings/schema",
        "GET",
        "Return the full settings registry schema",
        response_model=ListSettingSchemaResponse,
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/settings/{key}",
        "PUT",
        "Set a configuration setting value",
        request_model=UpdateSettingRequest,
        response_model=SettingInfo,
        error_codes=[400],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/settings/{key}",
        "DELETE",
        "Reset a setting to its default value",
        response_model=DeleteSettingResponse,
        query_params=[
            QueryParam("node_id", "Node ID for node-scoped settings"),
        ],
        error_codes=[400, 404],
        tags=["Admin"],
    ),
    # --- Admin: MCP Registry ---
    EndpointSpec(
        "/v1/api/admin/mcp-registry/search",
        "GET",
        "Search the MCP Registry for available servers",
        response_model=RegistrySearchResponse,
        query_params=[
            QueryParam("search", "Search query"),
            QueryParam("limit", "Max results (default 20, max 100)", schema_type="integer"),
            QueryParam("cursor", "Pagination cursor for next page"),
        ],
        error_codes=[502],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/mcp-registry/install",
        "POST",
        "Install an MCP server from the registry",
        request_model=RegistryInstallRequest,
        response_model=McpServerDetail,
        error_codes=[400, 404, 409, 502],
        tags=["Admin"],
    ),
    # --- Admin: MCP Servers ---
    EndpointSpec(
        "/v1/api/admin/mcp-servers",
        "GET",
        "List MCP server definitions with live status",
        response_model=ListMcpServersResponse,
        query_params=[
            QueryParam("reveal", "Show secret env/header values", schema_type="boolean"),
        ],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/mcp-servers",
        "POST",
        "Create an MCP server definition",
        request_model=CreateMcpServerRequest,
        response_model=McpServerDetail,
        error_codes=[400, 409],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/mcp-servers/{server_id}",
        "GET",
        "Get a single MCP server with status",
        response_model=McpServerDetail,
        error_codes=[404],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/mcp-servers/{server_id}",
        "PUT",
        "Update an MCP server definition",
        request_model=UpdateMcpServerRequest,
        response_model=McpServerDetail,
        error_codes=[400, 404, 409],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/mcp-servers/{server_id}",
        "DELETE",
        "Delete an MCP server definition",
        response_model=StatusResponse,
        error_codes=[404],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/mcp-servers/reload",
        "POST",
        "Tell all nodes to re-read MCP server config from DB and reconcile",
        response_model=McpReloadResponse,
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/mcp-servers/import",
        "POST",
        "Import MCP servers from a JSON config file",
        request_model=ImportMcpConfigRequest,
        response_model=ImportMcpConfigResponse,
        error_codes=[400],
        tags=["Admin"],
    ),
    # --- Admin: Model Definitions ---
    EndpointSpec(
        "/v1/api/admin/model-definitions",
        "GET",
        "List model definitions with live status from cluster nodes",
        response_model=ListModelDefinitionsResponse,
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/model-definitions",
        "POST",
        "Create a model definition",
        request_model=CreateModelDefinitionRequest,
        response_model=ModelDefinitionInfo,
        error_codes=[400, 409],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/model-definitions/reload",
        "POST",
        "Tell all nodes to re-read model definitions from DB and rebuild registry",
        response_model=ModelReloadResponse,
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/model-definitions/{definition_id}",
        "GET",
        "Get a single model definition",
        response_model=ModelDefinitionInfo,
        error_codes=[404],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/model-definitions/{definition_id}",
        "PUT",
        "Update a model definition",
        request_model=UpdateModelDefinitionRequest,
        response_model=ModelDefinitionInfo,
        error_codes=[400, 404, 409],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/model-definitions/{definition_id}",
        "DELETE",
        "Delete a model definition",
        error_codes=[404],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/model-definitions/detect",
        "POST",
        "Probe a model endpoint: verify reachability, list models, detect context window and server type",
        request_model=DetectModelRequest,
        response_model=DetectModelResponse,
        error_codes=[400],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/model-capabilities",
        "GET",
        "Look up static capabilities for a known model",
        response_model=ModelCapabilitiesResponse,
        query_params=[
            QueryParam(name="provider", description="Provider name", required=True),
            QueryParam(name="model", description="Model ID to look up", required=True),
        ],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/model-capabilities/known",
        "GET",
        "List known model name prefixes for a provider",
        response_model=KnownModelsResponse,
        query_params=[
            QueryParam(name="provider", description="Provider name", required=True),
        ],
        tags=["Admin"],
    ),
    # --- Admin: Prompt Policies ---
    EndpointSpec(
        "/v1/api/admin/prompt-policies",
        "GET",
        "List all prompt policies for system message composition",
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/prompt-policies",
        "POST",
        "Create a prompt policy",
        error_codes=[400],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/prompt-policies/{policy_id}",
        "GET",
        "Get a single prompt policy",
        error_codes=[404],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/prompt-policies/{policy_id}",
        "PUT",
        "Update a prompt policy",
        error_codes=[400, 404],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/prompt-policies/{policy_id}",
        "DELETE",
        "Delete a prompt policy",
        error_codes=[404],
        tags=["Admin"],
    ),
    # --- Admin: Node metadata ---
    EndpointSpec(
        "/v1/api/admin/node-metadata",
        "GET",
        "Get metadata for all nodes (bulk)",
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/nodes/{node_id}/metadata",
        "GET",
        "Get all metadata for a node",
        response_model=NodeMetadataResponse,
        error_codes=[400],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/nodes/{node_id}/metadata",
        "PUT",
        "Bulk set user metadata for a node",
        request_model=BulkSetNodeMetadataRequest,
        error_codes=[400],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/nodes/{node_id}/metadata/{key}",
        "PUT",
        "Set a single metadata key for a node",
        request_model=SetNodeMetadataValueRequest,
        error_codes=[400],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/nodes/{node_id}/metadata/{key}",
        "DELETE",
        "Delete a single metadata key for a node",
        error_codes=[400, 404],
        tags=["Admin"],
    ),
    # --- Admin: TLS / ACME ---
    EndpointSpec(
        "/v1/api/admin/tls/ca",
        "GET",
        "CA status: initialization state, CN, cert count, cert inventory",
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/tls/ca.pem",
        "GET",
        "Download CA root certificate (PEM format)",
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/tls/certs",
        "GET",
        "List all issued TLS certificates",
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/tls/certs/{domain}/renew",
        "POST",
        "Force-renew a certificate by domain",
        error_codes=[404, 500],
        tags=["Admin"],
    ),
    EndpointSpec(
        "/v1/api/admin/tls/certs/{domain}",
        "DELETE",
        "Delete a certificate by domain",
        error_codes=[404],
        tags=["Admin"],
    ),
    # --- Routing ---
    EndpointSpec(
        "/v1/api/route/workstreams/new",
        "POST",
        "Create workstream via rendezvous routing proxy",
        response_model=RouteCreateResponse,
        error_codes=[400, 503],
        tags=["Routing"],
    ),
    EndpointSpec(
        "/v1/api/route/send",
        "POST",
        "Proxy send to routed node",
        error_codes=[503],
        tags=["Routing"],
    ),
    EndpointSpec(
        "/v1/api/route/approve",
        "POST",
        "Proxy approve to routed node",
        error_codes=[503],
        tags=["Routing"],
    ),
    EndpointSpec(
        "/v1/api/route/cancel",
        "POST",
        "Proxy cancel to routed node",
        error_codes=[503],
        tags=["Routing"],
    ),
    EndpointSpec(
        "/v1/api/route/command",
        "POST",
        "Proxy command to routed node",
        error_codes=[503],
        tags=["Routing"],
    ),
    EndpointSpec(
        "/v1/api/route/workstreams/close",
        "POST",
        "Proxy workstream close to routed node",
        error_codes=[503],
        tags=["Routing"],
    ),
    EndpointSpec(
        "/v1/api/route",
        "GET",
        "Look up which node owns a workstream",
        response_model=RouteResponse,
        query_params=[
            QueryParam("ws_id", "Workstream ID to look up", required=True),
        ],
        error_codes=[400, 503],
        tags=["Routing"],
    ),
    # --- Coordinator workstream API ---
    # All require the ``admin.coordinator`` permission.  Ownership is
    # enforced per-row (callers without ``admin.system`` see only their
    # own coordinators); cross-tenant misses 404-mask.
    EndpointSpec(
        "/v1/api/coordinator/new",
        "POST",
        "Create a new coordinator workstream",
        description=(
            'Allocates a console-hosted ``kind="coordinator"`` ChatSession.  '
            "201 on create; 429 when the ``coordinator.max_active`` cap is "
            "reached and no idle coordinator can be evicted."
        ),
        request_model=CoordinatorCreateRequest,
        response_model=CoordinatorCreateResponse,
        response_code=201,
        error_codes=[400, 401, 403, 429, 500, 503],
        tags=["Coordinator"],
    ),
    EndpointSpec(
        "/v1/api/coordinator",
        "GET",
        "List coordinator workstreams visible to the caller",
        description=(
            "Returns coordinators owned by the caller.  Callers with "
            "``admin.system`` see every coordinator across tenants."
        ),
        response_model=CoordinatorListResponse,
        error_codes=[403, 503],
        tags=["Coordinator"],
    ),
    EndpointSpec(
        "/v1/api/coordinator/{ws_id}",
        "GET",
        "Get coordinator detail (rehydrates lazily on miss)",
        description=(
            "Returns the persisted coordinator's display fields.  If the "
            "session isn't currently in memory the manager rehydrates it "
            "before responding; ``500`` on rehydrate failure carries a "
            "correlation id matching the server log line."
        ),
        response_model=CoordinatorDetailResponse,
        error_codes=[400, 403, 404, 500, 503],
        tags=["Coordinator"],
    ),
    EndpointSpec(
        "/v1/api/coordinator/{ws_id}/open",
        "POST",
        "Open (rehydrate) a coordinator workstream by ws_id",
        description=(
            "Parity with ``POST /v1/api/workstreams/{ws_id}/open`` — gives "
            "SDK callers and operators a way to warm a coordinator without "
            "browsing to it.  Idempotent: ``already_loaded=true`` when the "
            "session was already in memory."
        ),
        response_model=CoordinatorOpenResponse,
        error_codes=[400, 403, 404, 500, 503],
        tags=["Coordinator"],
    ),
    EndpointSpec(
        "/v1/api/coordinator/{ws_id}/send",
        "POST",
        "Queue a user message onto the coordinator session",
        description=(
            "Worker thread picks up the message via the session's queue.  "
            "``429`` when the worker queue is full — caller should back off."
        ),
        request_model=CoordinatorSendRequest,
        response_model=StatusResponse,
        error_codes=[400, 403, 404, 429, 500, 503],
        tags=["Coordinator"],
    ),
    EndpointSpec(
        "/v1/api/coordinator/{ws_id}/approve",
        "POST",
        "Resolve a pending tool approval on the coordinator session",
        description=(
            "Approves or denies the pending tool call(s).  Set ``always`` to "
            "True to also add the pending tool name(s) to the session's "
            "auto-approve set so subsequent calls of the same tool skip the "
            "prompt."
        ),
        request_model=CoordinatorApproveRequest,
        response_model=StatusResponse,
        error_codes=[400, 403, 404, 409, 503],
        tags=["Coordinator"],
    ),
    EndpointSpec(
        "/v1/api/coordinator/{ws_id}/cancel",
        "POST",
        "Cancel in-flight generation on the coordinator session",
        description=(
            "Drops the in-flight LLM call and unblocks any pending approval "
            "or plan review.  The coordinator state moves to idle; storage "
            "is preserved."
        ),
        response_model=StatusResponse,
        error_codes=[403, 404, 503],
        tags=["Coordinator"],
    ),
    EndpointSpec(
        "/v1/api/coordinator/{ws_id}/close",
        "POST",
        "Soft-close the coordinator (unload from memory; storage preserved)",
        description=(
            "Releases the worker thread + UI listeners and marks the row "
            "``state=closed`` in storage.  The row remains queryable (audit "
            "/ history) but cannot be reopened — a closed coordinator is "
            "terminal from the manager's perspective."
        ),
        response_model=StatusResponse,
        error_codes=[403, 404, 500, 503],
        tags=["Coordinator"],
    ),
    EndpointSpec(
        "/v1/api/coordinator/{ws_id}/events",
        "GET",
        "Subscribe to the coordinator's SSE event stream",
        description=(
            "Server-Sent Events stream carrying ``status``, ``message``, "
            "``tool_call``, ``tool_result``, ``approval``, ``error``, and "
            "the phase-3 ``child_ws_*`` fan-out events.  Pings every 5s.  "
            "Body is text/event-stream — the response schema is omitted "
            "from the catalog because OpenAPI 3.1 has no first-class SSE "
            "type."
        ),
        error_codes=[403, 404, 409, 503],
        tags=["Coordinator"],
    ),
    EndpointSpec(
        "/v1/api/coordinator/{ws_id}/history",
        "GET",
        "Read the coordinator's reconstructed message history",
        description=(
            "Returns the tail of the conversation in OpenAI-like message "
            "format.  Used by the page-load handshake; SSE handles updates "
            "after that.  Bounded by the ``limit`` query parameter."
        ),
        response_model=CoordinatorHistoryResponse,
        query_params=[
            QueryParam(
                "limit",
                "Max conversation rows to fetch from storage (default 100, max 500).",
                schema_type="integer",
                default=100,
            ),
        ],
        error_codes=[403, 404, 503],
        tags=["Coordinator"],
    ),
    EndpointSpec(
        "/v1/api/coordinator/{ws_id}/children",
        "GET",
        "List the coordinator's spawned child workstreams",
        description=(
            "Returns interactive child workstreams whose ``parent_ws_id`` "
            "is this coordinator.  Same row shape as the model-facing "
            "``list_workstreams`` tool so the tree UI and the tool agree."
        ),
        response_model=CoordinatorChildrenResponse,
        error_codes=[400, 403, 404, 500, 503],
        tags=["Coordinator"],
    ),
    EndpointSpec(
        "/v1/api/coordinator/{ws_id}/tasks",
        "GET",
        "Read the coordinator's task list envelope",
        description=(
            "Returns the ``{version, tasks}`` envelope persisted via the "
            "``task_list`` model tool.  Corrupt envelopes return an empty "
            "list (the tool itself surfaces corruption errors on mutation)."
        ),
        response_model=CoordinatorTasksResponse,
        error_codes=[400, 403, 404, 503],
        tags=["Coordinator"],
    ),
    EndpointSpec(
        "/v1/api/coordinator/{ws_id}/trust",
        "POST",
        "Toggle trusted-session mode for send_to_workstream",
        description=(
            "When enabled, ``send_to_workstream`` calls that target a "
            "ws_id in the coordinator's own subtree skip the approval "
            "prompt.  Foreign ws_ids continue to require approval — "
            "trust only relaxes the guard for work the orchestrator "
            "itself spawned.  Every auto-approved send still emits a "
            "``coordinator.send.auto_approved`` audit row so the trail "
            "isn't lost.  Gated on both ``admin.coordinator`` AND "
            "``coordinator.trust.send`` so the trust feature is an "
            "explicit opt-in capability separate from ordinary "
            "coordinator administration."
        ),
        request_model=CoordinatorTrustRequest,
        response_model=CoordinatorTrustResponse,
        error_codes=[400, 403, 404, 503],
        tags=["Coordinator"],
    ),
    EndpointSpec(
        "/v1/api/coordinator/{ws_id}/restrict",
        "POST",
        "Revoke tool access on a live coordinator session",
        description=(
            "Adds the named tools to the coordinator session's revoked set "
            "without closing the session.  The model can keep working on "
            "whatever is already in flight but cannot invoke the revoked "
            "tools again.  Idempotent and additive — calling twice with "
            "disjoint lists unions them.  Revocations do not survive a "
            "session close / reopen; operators opt in per session.  Writes "
            "``coordinator.restricted`` with the revocation delta and the "
            "full post-state."
        ),
        request_model=CoordinatorRestrictRequest,
        response_model=CoordinatorRestrictResponse,
        error_codes=[400, 403, 404, 503],
        tags=["Coordinator"],
    ),
    EndpointSpec(
        "/v1/api/coordinator/{ws_id}/stop_cascade",
        "POST",
        "Cancel the coordinator and every direct child",
        description=(
            "Cancels the coordinator's in-flight generation AND dispatches "
            "``cancel_workstream`` through the routing proxy for every "
            "direct child in the in-memory registry.  Grandchildren are "
            "not touched directly — they sit behind their parent's cancel, "
            "which propagates via the child's SSE stream.  Returns the "
            "per-child disposition (``cancelled`` / ``failed``) so the UI "
            "can show which children responded.  Writes "
            "``coordinator.stopped_cascade`` with the two lists."
        ),
        response_model=CoordinatorStopCascadeResponse,
        error_codes=[400, 403, 404, 503],
        tags=["Coordinator"],
    ),
    EndpointSpec(
        "/v1/api/coordinator/{ws_id}/close_all_children",
        "POST",
        "Soft-close every direct child of the coordinator",
        description=(
            "Reads the in-memory child registry and dispatches "
            "``close_workstream`` via the routing proxy for every direct "
            "child under a bounded (16-concurrency) semaphore.  Unlike "
            "``stop_cascade`` this does not touch grandchildren — the "
            "model-facing tool asks for a bounded teardown of its own "
            "fan-out.  Returns ``{closed, failed, skipped}`` where "
            "``skipped`` distinguishes already-gone (404) from dispatch-"
            "broken (``failed``).  The optional ``reason`` propagates to "
            "every closed child's audit + workstream_config.  Writes "
            "``coordinator.closed_all_children`` at the coord level."
        ),
        request_model=CoordinatorCloseAllChildrenRequest,
        response_model=CoordinatorCloseAllChildrenResponse,
        error_codes=[400, 403, 404, 503],
        tags=["Coordinator"],
    ),
    EndpointSpec(
        "/v1/api/coordinator/{ws_id}/quota",
        "GET",
        "Read the coordinator session's live spawn-quota state",
        description=(
            "Returns the current ``spawn_budget`` (active-children cap) and "
            "``spawn_rate`` bucket (``tokens_per_minute``, ``burst``, "
            "``tokens_available``).  Values reflect the in-memory override "
            "when an admin has mutated the session via POST; otherwise they "
            "reflect the global defaults baked in at session construction."
        ),
        response_model=CoordinatorQuotaResponse,
        error_codes=[400, 403, 404, 503],
        tags=["Coordinator"],
    ),
    EndpointSpec(
        "/v1/api/coordinator/{ws_id}/quota",
        "POST",
        "Mutate the coordinator session's spawn quota (partial update)",
        description=(
            "Updates any subset of ``spawn_budget``, "
            "``spawn_rate.tokens_per_minute``, and ``spawn_rate.burst``.  "
            "Nested ``spawn_rate`` and flat ``tokens_per_minute`` / ``burst`` "
            "aliases are both accepted.  Missing fields keep their current "
            "values.  Overrides are in-memory only — a session reopen "
            "re-seeds from the global settings, matching the /trust and "
            "/restrict contract.  Writes ``coordinator.quota.updated`` "
            "with the before/after snapshot."
        ),
        request_model=CoordinatorQuotaRequest,
        response_model=CoordinatorQuotaResponse,
        error_codes=[400, 403, 404, 503],
        tags=["Coordinator"],
    ),
    EndpointSpec(
        "/v1/api/cluster/ws/{ws_id}/detail",
        "GET",
        "Cluster-wide live workstream detail (storage + live block + tail)",
        description=(
            "Aggregates the persisted row, a best-effort live block from "
            "the owning node (or the in-process coordinator manager for "
            '``kind="coordinator"`` rows), and the tail of the message '
            "history.  Gated on the ``admin.cluster.inspect`` permission "
            "(granted to ``builtin-admin`` via migration 040; revoke or "
            "reassign to a custom role for tighter control).  ``live`` "
            "is null on node unreachability / 5xx so callers can degrade "
            "gracefully."
        ),
        response_model=ClusterWsDetailResponse,
        query_params=[
            QueryParam(
                "message_limit",
                "Max conversation rows in the tail (default 20, clamped to 200).",
                schema_type="integer",
                default=20,
            ),
        ],
        error_codes=[400, 403, 404, 503],
        tags=["Coordinator"],
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
    DeleteSettingResponse,
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
    ChannelUserInfo,
    CreateChannelUserRequest,
    ListChannelUsersResponse,
    ClusterOverviewResponse,
    ClusterNodesResponse,
    ClusterWorkstreamsResponse,
    NodeDetailResponse,
    ClusterSnapshotResponse,
    ClusterWsDetailResponse,
    ConsoleCreateWsRequest,
    ConsoleCreateWsResponse,
    ConsoleHealthResponse,
    CoordinatorApproveRequest,
    CoordinatorChildInfo,
    CoordinatorChildrenResponse,
    CoordinatorCloseAllChildrenRequest,
    CoordinatorCloseAllChildrenResponse,
    CoordinatorCreateRequest,
    CoordinatorCreateResponse,
    CoordinatorDetailResponse,
    CoordinatorHistoryResponse,
    CoordinatorInfo,
    CoordinatorListResponse,
    CoordinatorOpenResponse,
    CoordinatorQuotaRequest,
    CoordinatorQuotaResponse,
    CoordinatorRestrictRequest,
    CoordinatorRestrictResponse,
    CoordinatorSendRequest,
    CoordinatorSpawnRateInput,
    CoordinatorSpawnRateState,
    CoordinatorStopCascadeResponse,
    CoordinatorTaskInfo,
    CoordinatorTasksResponse,
    CoordinatorTrustRequest,
    CoordinatorTrustResponse,
    CreateScheduleRequest,
    UpdateScheduleRequest,
    ScheduleInfo,
    ListSchedulesResponse,
    ListScheduleRunsResponse,
    RoleInfo,
    CreateRoleRequest,
    UpdateRoleRequest,
    ListRolesResponse,
    AssignRoleRequest,
    UserRoleInfo,
    ListUserRolesResponse,
    OrgInfo,
    UpdateOrgRequest,
    ListOrgsResponse,
    ToolPolicyInfo,
    CreateToolPolicyRequest,
    UpdateToolPolicyRequest,
    ListToolPoliciesResponse,
    UsageBreakdownItem,
    UsageResponse,
    AuditEventInfo,
    ListAuditEventsResponse,
    VerdictInfo,
    ListVerdictsResponse,
    OutputAssessmentInfo,
    ListOutputAssessmentsResponse,
    AdminMemoryInfo,
    ListAdminMemoriesResponse,
    SettingInfo,
    ListSettingsResponse,
    SettingSchemaInfo,
    ListSettingSchemaResponse,
    UpdateSettingRequest,
    McpServerDetail,
    CreateMcpServerRequest,
    UpdateMcpServerRequest,
    ListMcpServersResponse,
    ImportMcpConfigRequest,
    ImportMcpConfigResponse,
    McpReloadResponse,
    ModelDefinitionInfo,
    CreateModelDefinitionRequest,
    UpdateModelDefinitionRequest,
    ListModelDefinitionsResponse,
    ModelReloadResponse,
    DetectModelRequest,
    DetectModelResponse,
    ModelCapabilitiesResponse,
    KnownModelsResponse,
    AvailableModelInfo,
    ListAvailableModelsResponse,
    RegistrySearchResponse,
    RegistryInstallRequest,
    SkillDiscoverResponse,
    SkillInstallRequest,
    SkillInstallResponse,
    SkillInfo,
    SkillVersionInfo,
    CreateSkillRequest,
    UpdateSkillRequest,
    ListSkillsResponse,
    ListSkillVersionsResponse,
    SkillResourceInfo,
    CreateSkillResourceRequest,
    ListSkillResourcesResponse,
    RouteResponse,
    RouteCreateResponse,
    SkillSummary,
    ListSkillSummaryResponse,
]


def build_console_spec() -> dict[str, Any]:
    """Build the OpenAPI spec for the turnstone console."""
    return build_openapi(
        title="turnstone Console API",
        description="Cluster-wide visibility and control across all turnstone nodes.",
        endpoints=CONSOLE_ENDPOINTS,
        models=_ALL_MODELS,
    )
