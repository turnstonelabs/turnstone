"""Pydantic v2 models for turnstone-console API endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Cluster overview
# ---------------------------------------------------------------------------


class StateCounts(BaseModel):
    running: int = 0
    thinking: int = 0
    attention: int = 0
    idle: int = 0
    error: int = 0


class ClusterAggregate(BaseModel):
    total_tokens: int = 0
    total_tool_calls: int = 0


class ClusterOverviewResponse(BaseModel):
    nodes: int = 0
    workstreams: int = 0
    states: StateCounts = StateCounts()
    aggregate: ClusterAggregate = ClusterAggregate()
    version_drift: bool = False
    versions: list[str] = []


# ---------------------------------------------------------------------------
# Node list
# ---------------------------------------------------------------------------


class ClusterNodeInfo(BaseModel):
    node_id: str
    server_url: str = ""
    ws_total: int = 0
    ws_running: int = 0
    ws_thinking: int = 0
    ws_attention: int = 0
    ws_idle: int = 0
    ws_error: int = 0
    total_tokens: int = 0
    started: float = 0.0
    reachable: bool = True
    health: dict[str, Any] = Field(default_factory=dict)
    version: str = ""


class ClusterNodesResponse(BaseModel):
    nodes: list[ClusterNodeInfo]
    total: int = 0


# ---------------------------------------------------------------------------
# Workstream list
# ---------------------------------------------------------------------------


class ClusterWorkstreamInfo(BaseModel):
    id: str
    name: str = ""
    state: str = ""
    node: str = ""
    title: str = ""
    tokens: int = 0
    context_ratio: float = 0.0
    activity: str = ""
    activity_state: str = ""
    tool_calls: int = 0


class ClusterWorkstreamsResponse(BaseModel):
    workstreams: list[ClusterWorkstreamInfo]
    total: int = 0
    page: int = 1
    per_page: int = 50
    pages: int = 1


# ---------------------------------------------------------------------------
# Node detail
# ---------------------------------------------------------------------------


class NodeDetailResponse(BaseModel):
    node_id: str
    server_url: str = ""
    health: dict[str, Any] = Field(default_factory=dict)
    workstreams: list[ClusterWorkstreamInfo] = []
    aggregate: dict[str, int] = Field(default_factory=dict)
    reachable: bool = True


# ---------------------------------------------------------------------------
# Cluster snapshot
# ---------------------------------------------------------------------------


class ClusterSnapshotNode(BaseModel):
    node_id: str
    server_url: str = ""
    max_ws: int = 10
    reachable: bool = True
    version: str = ""
    health: dict[str, Any] = Field(default_factory=dict)
    aggregate: dict[str, int] = Field(default_factory=dict)
    workstreams: list[ClusterWorkstreamInfo] = []


class ClusterSnapshotResponse(BaseModel):
    nodes: list[ClusterSnapshotNode]
    overview: ClusterOverviewResponse
    timestamp: float = 0.0


# ---------------------------------------------------------------------------
# Workstream creation
# ---------------------------------------------------------------------------


class ConsoleCreateWsRequest(BaseModel):
    node_id: str = Field(
        default="",
        description="Target node: specific ID, 'auto', 'pool', or empty for auto",
    )
    name: str = Field(default="", description="Workstream name (auto-generated if empty)")
    model: str = Field(default="", description="Model alias from node registry")
    initial_message: str = Field(
        default="", description="Optional first message sent after creation"
    )
    template: str = Field(
        default="", description="Prompt template name (replaces default templates)"
    )


class ConsoleCreateWsResponse(BaseModel):
    status: str = "ok"
    correlation_id: str = ""
    target_node: str = ""


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class ConsoleHealthResponse(BaseModel):
    status: str = Field(default="ok", examples=["ok"])
    service: str = "turnstone-console"
    nodes: int = 0
    workstreams: int = 0
    version_drift: bool = False
    versions: list[str] = []


# ---------------------------------------------------------------------------
# Governance: Roles
# ---------------------------------------------------------------------------


class RoleInfo(BaseModel):
    role_id: str
    name: str
    display_name: str
    permissions: str
    builtin: bool
    org_id: str
    created: str
    updated: str


class CreateRoleRequest(BaseModel):
    name: str
    display_name: str = ""
    permissions: str = "read"


class UpdateRoleRequest(BaseModel):
    display_name: str | None = None
    permissions: str | None = None


class ListRolesResponse(BaseModel):
    roles: list[RoleInfo]


class AssignRoleRequest(BaseModel):
    role_id: str


class UserRoleInfo(BaseModel):
    role_id: str
    name: str
    display_name: str
    permissions: str
    builtin: bool
    org_id: str
    created: str
    updated: str
    assigned_by: str
    assignment_created: str


class ListUserRolesResponse(BaseModel):
    roles: list[UserRoleInfo]


# ---------------------------------------------------------------------------
# Governance: Orgs
# ---------------------------------------------------------------------------


class OrgInfo(BaseModel):
    org_id: str
    name: str
    display_name: str
    settings: str
    created: str
    updated: str


class UpdateOrgRequest(BaseModel):
    display_name: str | None = None
    settings: str | None = None


class ListOrgsResponse(BaseModel):
    orgs: list[OrgInfo]


# ---------------------------------------------------------------------------
# Governance: Tool Policies
# ---------------------------------------------------------------------------


class ToolPolicyInfo(BaseModel):
    policy_id: str
    name: str
    tool_pattern: str
    action: str
    priority: int
    org_id: str
    enabled: bool
    created_by: str
    created: str
    updated: str


class CreateToolPolicyRequest(BaseModel):
    name: str
    tool_pattern: str
    action: str  # allow, deny, ask
    priority: int = 0
    org_id: str = ""
    enabled: bool = True


class UpdateToolPolicyRequest(BaseModel):
    name: str | None = None
    tool_pattern: str | None = None
    action: str | None = None
    priority: int | None = None
    enabled: bool | None = None


class ListToolPoliciesResponse(BaseModel):
    policies: list[ToolPolicyInfo]


# ---------------------------------------------------------------------------
# Governance: Prompt Templates
# ---------------------------------------------------------------------------


class PromptTemplateInfo(BaseModel):
    template_id: str
    name: str
    category: str
    content: str
    variables: str
    is_default: bool
    org_id: str
    created_by: str
    origin: str = "manual"
    mcp_server: str = ""
    readonly: bool = False
    created: str
    updated: str


class CreatePromptTemplateRequest(BaseModel):
    name: str
    content: str
    category: str = "general"
    variables: str = "[]"
    is_default: bool = False
    org_id: str = ""


class UpdatePromptTemplateRequest(BaseModel):
    name: str | None = None
    content: str | None = None
    category: str | None = None
    variables: str | None = None
    is_default: bool | None = None


class ListPromptTemplatesResponse(BaseModel):
    templates: list[PromptTemplateInfo]


# ---------------------------------------------------------------------------
# Governance: Usage
# ---------------------------------------------------------------------------


class UsageBreakdownItem(BaseModel):
    key: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls_count: int = 0


class UsageResponse(BaseModel):
    summary: list[UsageBreakdownItem]
    breakdown: list[UsageBreakdownItem]


# ---------------------------------------------------------------------------
# Governance: Audit
# ---------------------------------------------------------------------------


class AuditEventInfo(BaseModel):
    event_id: str
    timestamp: str
    user_id: str
    action: str
    resource_type: str
    resource_id: str
    detail: str
    ip_address: str
    created: str


class ListAuditEventsResponse(BaseModel):
    events: list[AuditEventInfo]


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


class ChannelUserInfo(BaseModel):
    channel_type: str
    channel_user_id: str
    user_id: str
    created: str


class ListChannelUsersResponse(BaseModel):
    channels: list[ChannelUserInfo]


class CreateChannelUserRequest(BaseModel):
    channel_type: str = Field(..., description="Channel type (e.g. discord, slack)")
    channel_user_id: str = Field(..., description="External channel user identifier")
    total: int
