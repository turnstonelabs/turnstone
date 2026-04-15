"""Pydantic v2 models for turnstone-server API endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Workstream management
# ---------------------------------------------------------------------------


class SendRequest(BaseModel):
    message: str = Field(description="User message text")
    ws_id: str = Field(description="Target workstream ID")
    attachment_ids: list[str] | None = Field(
        default=None,
        description=(
            "Explicit list of attachment ids to inject into this turn. "
            "When omitted, any pending attachments for the caller on "
            "this workstream are auto-consumed. An empty list disables "
            "auto-consumption for this send."
        ),
    )


class SendResponse(BaseModel):
    status: str = Field(description="'ok' or 'busy'", examples=["ok", "busy"])


class AttachmentInfo(BaseModel):
    attachment_id: str = Field(description="Opaque id for this attachment")
    filename: str = Field(description="Original upload filename")
    mime_type: str = Field(description="Canonicalized MIME type")
    size_bytes: int = Field(description="Payload size in bytes")
    kind: str = Field(description="'image' or 'text'", examples=["image", "text"])


class UploadAttachmentResponse(AttachmentInfo):
    """Returned after a successful upload."""


class ListAttachmentsResponse(BaseModel):
    attachments: list[AttachmentInfo] = Field(
        description="Pending (unconsumed) attachments for caller+workstream"
    )


class ApproveRequest(BaseModel):
    approved: bool = Field(description="True to approve, false to deny")
    feedback: str | None = Field(default=None, description="Optional denial reason")
    always: bool = Field(
        default=False, description="Auto-approve the tools in this batch going forward"
    )
    ws_id: str = Field(description="Target workstream ID")


class PlanFeedbackRequest(BaseModel):
    feedback: str = Field(description="Feedback text; empty string means approval")
    ws_id: str = Field(description="Target workstream ID")


class CommandRequest(BaseModel):
    command: str = Field(description="Slash command (e.g. /clear, /new, /resume)")
    ws_id: str = Field(description="Target workstream ID")


class CancelRequest(BaseModel):
    ws_id: str = Field(description="Target workstream ID")
    force: bool = Field(
        default=False,
        description="Force cancel: abandon the stuck worker thread immediately. "
        "Use when cooperative cancel has not resolved within a few seconds.",
    )


class CreateWorkstreamRequest(BaseModel):
    name: str = Field(default="", description="Workstream display name (auto-generated if empty)")
    model: str = Field(default="", description="Model alias from registry")
    auto_approve: bool = Field(default=False, description="Auto-approve all tool calls")
    resume_ws: str = Field(
        default="",
        description="Workstream ID to resume atomically during creation (empty = fresh start)",
    )
    skill: str = Field(default="", description="Skill name (replaces default skills)")
    notify_targets: str | list[dict[str, str]] = Field(
        default="[]",
        description=(
            "Notification targets, accepted as either a JSON string or a structured "
            "array of objects containing channel_type + channel_id/user_id"
        ),
    )
    client_type: str = Field(
        default="",
        description="Client surface type (web, cli, chat). Defaults to web for server-created sessions.",
    )


class CreateWorkstreamResponse(BaseModel):
    ws_id: str = Field(description="Unique ID of the new workstream")
    name: str = Field(description="Assigned workstream name")
    resumed: bool = Field(default=False, description="Whether a previous workstream was resumed")
    message_count: int = Field(
        default=0, description="Number of messages in the resumed workstream"
    )


class CloseWorkstreamRequest(BaseModel):
    ws_id: str = Field(description="Workstream ID to close")


# ---------------------------------------------------------------------------
# List / dashboard
# ---------------------------------------------------------------------------


class WorkstreamInfo(BaseModel):
    id: str
    name: str
    state: str


class ListWorkstreamsResponse(BaseModel):
    workstreams: list[WorkstreamInfo]


class DashboardWorkstream(BaseModel):
    id: str
    name: str
    state: str
    title: str = ""
    tokens: int = 0
    context_ratio: float = 0.0
    activity: str = ""
    activity_state: str = ""
    tool_calls: int = 0
    node: str = ""
    model: str = ""
    model_alias: str = ""


class DashboardAggregate(BaseModel):
    total_tokens: int = 0
    total_tool_calls: int = 0
    active_count: int = 0
    total_count: int = 0
    uptime_seconds: int = 0
    node: str = "local"


class DashboardResponse(BaseModel):
    workstreams: list[DashboardWorkstream]
    aggregate: DashboardAggregate


# ---------------------------------------------------------------------------
# Saved workstreams
# ---------------------------------------------------------------------------


class SavedWorkstreamInfo(BaseModel):
    ws_id: str
    alias: str | None = None
    title: str | None = None
    created: str
    updated: str
    message_count: int


class ListSavedWorkstreamsResponse(BaseModel):
    workstreams: list[SavedWorkstreamInfo]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class BackendStatus(BaseModel):
    status: str = Field(examples=["up", "down"])


class WorkstreamCounts(BaseModel):
    total: int = 0
    idle: int = 0
    thinking: int = 0
    running: int = 0
    attention: int = 0
    error: int = 0


class McpStatus(BaseModel):
    servers: int = 0
    resources: int = 0
    prompts: int = 0


class HealthResponse(BaseModel):
    status: str = Field(examples=["ok", "degraded"])
    version: str = ""
    node_id: str = ""
    uptime_seconds: float = 0.0
    model: str = ""
    max_ws: int = Field(default=10, description="Maximum concurrent workstreams")
    workstreams: WorkstreamCounts = WorkstreamCounts()
    backend: BackendStatus | None = None
    mcp: McpStatus | None = None


# ---------------------------------------------------------------------------
# Memories
# ---------------------------------------------------------------------------

MemoryType = Literal["user", "project", "feedback", "reference"]
MemoryScope = Literal["global", "workstream", "user"]


class SaveMemoryRequest(BaseModel):
    name: str = Field(description="Memory identifier (normalized to snake_case)")
    content: str = Field(description="Memory content", max_length=65536)
    description: str = Field(default="", description="Short description for relevance matching")
    type: MemoryType = Field(default="project", description="Memory type")
    scope: MemoryScope = Field(default="global", description="Memory scope")
    scope_id: str = Field(
        default="",
        description="Scope identifier (ws_id for workstream, user_id for user scope)",
    )

    @model_validator(mode="after")
    def _validate_scope_scope_id(self) -> SaveMemoryRequest:
        scope_id = self.scope_id.strip()
        if self.scope == "global" and scope_id:
            raise ValueError("scope_id is not allowed with global scope")
        if self.scope == "workstream" and not scope_id:
            raise ValueError("scope_id is required for workstream scope")
        return self


class MemoryInfo(BaseModel):
    memory_id: str
    name: str
    description: str = ""
    type: MemoryType
    scope: MemoryScope
    scope_id: str = ""
    content: str
    created: str
    updated: str


class ListMemoriesResponse(BaseModel):
    memories: list[MemoryInfo]
    total: int = 0


MemoryTypeFilter = Literal["", "user", "project", "feedback", "reference"]
MemoryScopeFilter = Literal["", "global", "workstream", "user"]


class SearchMemoriesRequest(BaseModel):
    query: str = Field(description="Search query text")
    type: MemoryTypeFilter = Field(default="", description="Filter by memory type")
    scope: MemoryScopeFilter = Field(default="", description="Filter by scope")
    scope_id: str = Field(default="", description="Filter by scope_id")
    limit: int = Field(default=20, description="Max results (1-50)", ge=1, le=50)

    @model_validator(mode="after")
    def _validate_scope_scope_id(self) -> SearchMemoriesRequest:
        scope_id = self.scope_id.strip()
        if self.scope == "global" and scope_id:
            raise ValueError("scope_id is not allowed with global scope")
        if scope_id and not self.scope:
            raise ValueError("scope is required when scope_id is provided")
        return self


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


class SkillSummary(BaseModel):
    name: str = Field(description="Skill name")
    category: str = Field(default="", description="Skill category")
    description: str = Field(default="", description="Skill description for discovery")
    tags: list[str] = Field(default_factory=list, description="Semantic tags")
    is_default: bool = Field(default=False, description="Whether auto-applied to all sessions")
    activation: str = Field(default="named", description="Activation mode: default, named, search")
    origin: str = Field(default="manual", description="Source: manual, mcp, skills.sh, github")
    author: str = Field(default="", description="Skill author")
    version: str = Field(default="1.0.0", description="Skill version")


class ListSkillSummaryResponse(BaseModel):
    skills: list[SkillSummary]


class AvailableModelInfo(BaseModel):
    alias: str
    model: str
    provider: str


class ListAvailableModelsResponse(BaseModel):
    models: list[AvailableModelInfo] = Field(default_factory=list)
    default_alias: str = ""
    channel_default_alias: str = ""
