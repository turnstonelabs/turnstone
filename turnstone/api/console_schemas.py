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
    skill: str = Field(default="", description="Skill name (replaces default skills)")


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
# Governance: Skills
# ---------------------------------------------------------------------------


class SkillInfo(BaseModel):
    template_id: str = Field(description="Skill ID")
    name: str
    category: str
    content: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    variables: str = "[]"
    is_default: bool
    activation: str = "named"
    org_id: str
    created_by: str
    origin: str = "manual"
    mcp_server: str = ""
    readonly: bool = False
    source_url: str = ""
    version: str = "1.0.0"
    author: str = ""
    token_estimate: int = 0
    model: str = ""
    auto_approve: bool = False
    temperature: float | None = None
    reasoning_effort: str = ""
    max_tokens: int | None = None
    token_budget: int = 0
    agent_max_turns: int | None = None
    notify_on_complete: str = "{}"
    enabled: bool = True
    allowed_tools: str = "[]"
    license: str = ""
    compatibility: str = ""
    scan_status: str = ""
    scan_report: str = "{}"
    scan_version: str = ""
    resource_count: int = 0
    created: str
    updated: str


class CreateSkillRequest(BaseModel):
    name: str
    content: str
    category: str = "general"
    description: str = ""
    tags: str = "[]"
    variables: str = "[]"
    is_default: bool = False
    activation: str = "named"
    org_id: str = ""
    author: str = ""
    version: str = "1.0.0"
    model: str = ""
    auto_approve: bool = False
    temperature: float | None = None
    reasoning_effort: str = ""
    max_tokens: int | None = None
    token_budget: int = 0
    agent_max_turns: int | None = None
    notify_on_complete: str = "{}"
    enabled: bool = True
    allowed_tools: str = "[]"
    license: str = ""
    compatibility: str = ""


class UpdateSkillRequest(BaseModel):
    name: str | None = None
    content: str | None = None
    category: str | None = None
    description: str | None = None
    tags: str | None = None
    variables: str | None = None
    is_default: bool | None = None
    activation: str | None = None
    author: str | None = None
    version: str | None = None
    model: str | None = None
    auto_approve: bool | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None
    max_tokens: int | None = None
    token_budget: int | None = None
    agent_max_turns: int | None = None
    notify_on_complete: str | None = None
    enabled: bool | None = None
    allowed_tools: str | None = None
    license: str | None = None
    compatibility: str | None = None


class ListSkillsResponse(BaseModel):
    skills: list[SkillInfo]


# ---------------------------------------------------------------------------
# Governance: Skill Versions
# ---------------------------------------------------------------------------


class SkillVersionInfo(BaseModel):
    id: int
    skill_id: str
    version: int
    snapshot: str
    changed_by: str
    created: str


class ListSkillVersionsResponse(BaseModel):
    versions: list[SkillVersionInfo]


# ---------------------------------------------------------------------------
# Governance: Skill Resources
# ---------------------------------------------------------------------------


class SkillResourceInfo(BaseModel):
    resource_id: str
    skill_id: str
    path: str
    content: str = ""
    content_type: str = "text/plain"
    size: int = 0
    created: str


class ListSkillResourcesResponse(BaseModel):
    resources: list[SkillResourceInfo]


class CreateSkillResourceRequest(BaseModel):
    path: str
    content: str
    content_type: str = "text/plain"


# ---------------------------------------------------------------------------
# Governance: Usage
# ---------------------------------------------------------------------------


class UsageBreakdownItem(BaseModel):
    key: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls_count: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0


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
# Governance: Intent Verdicts
# ---------------------------------------------------------------------------


class VerdictInfo(BaseModel):
    """Intent validation verdict."""

    verdict_id: str
    ws_id: str
    call_id: str
    func_name: str
    func_args: str = ""
    intent_summary: str
    risk_level: str
    confidence: float
    recommendation: str
    reasoning: str
    evidence: str = "[]"
    tier: str
    judge_model: str = ""
    user_decision: str = ""
    latency_ms: int = 0
    created: str


class ListVerdictsResponse(BaseModel):
    """Response for verdict listing."""

    verdicts: list[VerdictInfo]
    total: int


class OutputAssessmentInfo(BaseModel):
    """Output guard assessment."""

    assessment_id: str
    ws_id: str
    call_id: str
    func_name: str
    flags: str = "[]"
    risk_level: str = "none"
    annotations: str = "[]"
    output_length: int = 0
    redacted: int = 0
    created: str


class ListOutputAssessmentsResponse(BaseModel):
    """Response for output assessment listing."""

    assessments: list[OutputAssessmentInfo]
    total: int


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


# ---------------------------------------------------------------------------
# Admin: Memories
# ---------------------------------------------------------------------------


class AdminMemoryInfo(BaseModel):
    memory_id: str
    name: str
    description: str = ""
    type: str
    scope: str
    scope_id: str = ""
    content: str
    created: str
    updated: str
    last_accessed: str = ""
    access_count: int = 0


class ListAdminMemoriesResponse(BaseModel):
    memories: list[AdminMemoryInfo]
    total: int = 0


# ---------------------------------------------------------------------------
# Admin: System Settings
# ---------------------------------------------------------------------------


class SettingInfo(BaseModel):
    key: str
    value: Any = None
    source: str = "default"  # "storage" | "default"
    type: str = "str"
    description: str = ""
    section: str = ""
    is_secret: bool = False
    node_id: str = ""
    changed_by: str = ""
    updated: str = ""
    restart_required: bool = False


class ListSettingsResponse(BaseModel):
    settings: list[SettingInfo]


class SettingSchemaInfo(BaseModel):
    key: str
    type: str
    default: Any = None
    description: str = ""
    section: str = ""
    is_secret: bool = False
    min_value: float | None = None
    max_value: float | None = None
    choices: list[str] | None = None
    restart_required: bool = False


class ListSettingSchemaResponse(BaseModel):
    settings_schema: list[SettingSchemaInfo] = Field(alias="schema")


class UpdateSettingRequest(BaseModel):
    value: Any
    node_id: str = ""


# ---------------------------------------------------------------------------
# Admin: MCP Servers
# ---------------------------------------------------------------------------


class McpServerInfo(BaseModel):
    server_id: str
    name: str
    transport: str
    command: str = ""
    args: str = "[]"
    url: str = ""
    headers: str = "{}"
    env: str = "{}"
    auto_approve: bool = False
    enabled: bool = True
    created_by: str = ""
    registry_name: str | None = None
    registry_version: str = ""
    registry_meta: str = "{}"
    created: str
    updated: str


class McpServerStatus(BaseModel):
    connected: bool = False
    tools: int = 0
    resources: int = 0
    prompts: int = 0
    error: str = ""
    transport: str = ""
    command: str = ""
    url: str = ""


class McpServerDetail(McpServerInfo):
    status: dict[str, McpServerStatus] = Field(default_factory=dict)
    source: str = ""  # "config" for config-file servers, empty for DB-managed


class CreateMcpServerRequest(BaseModel):
    name: str
    transport: str  # "stdio" | "streamable-http"
    command: str = ""
    args: list[str] = []
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)
    auto_approve: bool = False
    enabled: bool = True


class UpdateMcpServerRequest(BaseModel):
    name: str | None = None
    transport: str | None = None
    command: str | None = None
    args: list[str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    env: dict[str, str] | None = None
    auto_approve: bool | None = None
    enabled: bool | None = None


class ListMcpServersResponse(BaseModel):
    servers: list[McpServerDetail]


class ImportMcpConfigRequest(BaseModel):
    config: dict[str, Any] = Field(..., description="JSON config object with mcpServers key")


class ImportMcpConfigResponse(BaseModel):
    imported: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []


class McpReloadResponse(BaseModel):
    status: str = "ok"
    results: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Admin: MCP Registry
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Admin: Skill Discovery
# ---------------------------------------------------------------------------


class SkillDiscoverListing(BaseModel):
    id: str
    name: str
    description: str = ""
    author: str = ""
    source: str = ""
    source_url: str = ""
    install_count: int = 0
    tags: list[str] = Field(default_factory=list)
    installed: bool = False
    scan_status: str = ""
    template_id: str = ""


class SkillDiscoverResponse(BaseModel):
    skills: list[SkillDiscoverListing]


class SkillInstallRequest(BaseModel):
    source: str  # "skills.sh" or "github"
    skill_id: str = ""  # for skills.sh
    url: str = ""  # for github


class SkillInstallSkipped(BaseModel):
    name: str
    reason: str


class SkillInstallResponse(BaseModel):
    installed: list[SkillInfo]
    skipped: list[SkillInstallSkipped] = []
    total: int = 0


# ---------------------------------------------------------------------------
# Admin: MCP Registry
# ---------------------------------------------------------------------------


class RegistryRemoteInfo(BaseModel):
    type: str = "streamable-http"
    url: str = ""
    headers: list[dict[str, Any]] = Field(default_factory=list)
    variables: dict[str, dict[str, Any]] = Field(default_factory=dict)


class RegistryPackageInfo(BaseModel):
    registry_type: str = ""
    identifier: str = ""
    version: str = ""
    transport_type: str = "stdio"
    environment_variables: list[dict[str, Any]] = Field(default_factory=list)


class RegistryServerInfo(BaseModel):
    name: str
    description: str = ""
    title: str = ""
    version: str = ""
    website_url: str = ""
    repository: dict[str, str] = Field(default_factory=dict)
    icons: list[dict[str, str]] = Field(default_factory=list)
    remotes: list[RegistryRemoteInfo] = Field(default_factory=list)
    packages: list[RegistryPackageInfo] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    installed: bool = False
    installed_server_id: str = ""
    installed_version: str = ""
    update_available: bool = False


class RegistrySearchResponse(BaseModel):
    servers: list[RegistryServerInfo]
    total: int = 0
    next_cursor: str | None = None


class RegistryInstallRequest(BaseModel):
    registry_name: str
    source: str  # "remote" | "package"
    index: int = 0
    name: str = ""
    variables: dict[str, str] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
