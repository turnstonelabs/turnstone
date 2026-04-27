"""Pydantic v2 models for turnstone-server API endpoints."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from turnstone.core.workstream import WorkstreamKind

# ---------------------------------------------------------------------------
# Workstream management
# ---------------------------------------------------------------------------


class SendRequest(BaseModel):
    message: str = Field(description="User message text")
    attachment_ids: list[str] | None = Field(
        default=None,
        description=(
            "Explicit list of attachment ids to inject into this turn. "
            "When omitted, any pending attachments for the caller on "
            "this workstream are auto-consumed. An empty list disables "
            "auto-consumption for this send."
        ),
    )


class DequeueRequest(BaseModel):
    """Body for ``DELETE /v1/api/workstreams/{ws_id}/send``.

    Removes a previously-queued message from the workstream's pending
    queue. ``msg_id`` is the id returned in a prior ``send`` response
    when the workstream was busy and the message was queued.
    """

    msg_id: str = Field(description="Id of the queued message to remove")


class SendResponse(BaseModel):
    status: str = Field(
        description="'ok', 'busy', 'queued', or 'queue_full'",
        examples=["ok", "busy", "queued", "queue_full"],
    )
    attached_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Attachment ids actually reserved onto this turn. Subset of "
            "the request's `attachment_ids` (or the auto-consumed pending "
            "set). Empty when the send carries no attachments."
        ),
    )
    dropped_attachment_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Attachment ids the caller requested that the server could "
            "not reserve (lost a race, already consumed, or cross-scope). "
            "The request still proceeds with whatever was reserved; the "
            "client can retry uploads or surface a partial-attach warning."
        ),
    )
    priority: str | None = Field(
        default=None,
        description="Set on `queued` responses: relative priority of the queued message.",
    )
    msg_id: str | None = Field(
        default=None,
        description="Set on `queued` responses: id used to dequeue the message.",
    )


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


class PlanFeedbackRequest(BaseModel):
    feedback: str = Field(description="Feedback text; empty string means approval")
    ws_id: str = Field(description="Target workstream ID")


class CommandRequest(BaseModel):
    command: str = Field(description="Slash command (e.g. /clear, /new, /resume)")
    ws_id: str = Field(description="Target workstream ID")


class CancelRequest(BaseModel):
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
    initial_message: str = Field(
        default="",
        description=(
            "Optional first user message dispatched as a background turn after "
            "the workstream is created. When attachments are also provided "
            "(via the multipart variant), they are reserved onto this turn."
        ),
    )
    ws_id: str = Field(
        default="",
        description=(
            "Optional caller-supplied workstream id (32-hex). Required when "
            "creating with attachments via the cluster routing layer so the "
            "console can hash to the owning node before the multipart body "
            "lands. Auto-generated when omitted."
        ),
    )
    kind: WorkstreamKind = Field(
        default=WorkstreamKind.INTERACTIVE,
        description=(
            "Workstream kind — 'interactive' (default) or 'coordinator'. "
            "Coordinator workstreams are created by the console's own "
            "/v1/api/workstreams/new endpoint; clients hitting "
            "/v1/api/workstreams/new should leave this at the default."
        ),
    )
    parent_ws_id: str | None = Field(
        default=None,
        description=(
            "Optional parent workstream id. Populated on children spawned "
            "by a coordinator so the parent/child relationship survives "
            "restart and appears in audit / list views."
        ),
    )


class CreateWorkstreamResponse(BaseModel):
    ws_id: str = Field(description="Unique ID of the new workstream")
    name: str = Field(description="Assigned workstream name")
    resumed: bool = Field(default=False, description="Whether a previous workstream was resumed")
    message_count: int = Field(
        default=0, description="Number of messages in the resumed workstream"
    )
    attachment_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Ids of attachments saved by this request (multipart variant only). "
            "Already reserved onto the initial_message turn when one was provided; "
            "otherwise left pending for a follow-up POST "
            "/v1/api/workstreams/{ws_id}/send."
        ),
    )


class CloseWorkstreamRequest(BaseModel):
    """Body for ``POST /v1/api/workstreams/{ws_id}/close``.

    The body must be valid JSON; send ``{}`` when omitting all
    fields. Pre-1.5 the model also carried a body-keyed ``ws_id``;
    1.5 moved that to the path so the body shrinks to the optional
    ``reason``. Coord ignores the body entirely (its close handler
    is wired ``supports_close_reason=False``).
    """

    reason: str | None = Field(
        default=None,
        description=(
            "Optional close reason persisted to ``workstream_config`` "
            "for postmortem. Capped at 512 UTF-8 bytes server-side; "
            "credential-redaction is applied via the output guard."
        ),
    )


# ---------------------------------------------------------------------------
# List / dashboard
# ---------------------------------------------------------------------------


class WorkstreamInfo(BaseModel):
    """Active-list row shape, shared across both kinds.

    Renamed ``id`` → ``ws_id`` and added ``user_id`` in the Stage 2
    ``list``/``saved`` verb lift so the active-list response shape
    matches the rest of the v1 surface (every other shared verb's
    payload uses ``ws_id``). ``user_id`` was previously coord-only;
    interactive now populates it too. SDK consumers reading
    ``row.id`` should swap to ``row.ws_id``.
    """

    ws_id: str
    name: str
    state: str
    kind: WorkstreamKind = WorkstreamKind.INTERACTIVE
    parent_ws_id: str | None = None
    user_id: str = ""


class ListWorkstreamsResponse(BaseModel):
    """Response body for ``GET /v1/api/workstreams`` on either kind.

    Top-level key is ``workstreams`` regardless of the kind serving
    the request — pre-lift coord returned ``{"coordinators": [...]}``;
    convergence lifted both kinds onto the same shape. Coord SDK /
    frontend consumers branching on ``data.coordinators`` swap to
    ``data.workstreams``.
    """

    workstreams: list[WorkstreamInfo]


class DashboardWorkstream(BaseModel):
    """Dashboard row shape for ``GET /v1/api/dashboard``.

    Renamed ``id`` → ``ws_id`` for v1 row-shape consistency with
    the rest of the workstream surface (active list, saved list,
    history, detail, etc.). Frontend consumers reading
    ``dashboard.workstreams[].id`` swap to ``.ws_id``.
    """

    ws_id: str
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
    kind: WorkstreamKind = WorkstreamKind.INTERACTIVE
    parent_ws_id: str | None = None
    user_id: str = ""


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
# Detail / history (Stage 2 verb lift — both kinds expose these)
# ---------------------------------------------------------------------------


class WorkstreamDetailResponse(BaseModel):
    """Response body for ``GET /v1/api/workstreams/{ws_id}``.

    Renamed and relocated from ``CoordinatorDetailResponse`` in the
    Stage 2 history/detail verb lift. Both kinds populate every field;
    SDK consumers don't branch on kind to read them. The lift adds the
    endpoint to interactive as a feature gain (pre-lift only coord
    exposed it).
    """

    ws_id: str
    name: str
    state: str
    user_id: str
    kind: WorkstreamKind = WorkstreamKind.INTERACTIVE


class WorkstreamHistoryResponse(BaseModel):
    """Response body for ``GET /v1/api/workstreams/{ws_id}/history``.

    Renamed and relocated from ``CoordinatorHistoryResponse`` in the
    Stage 2 history/detail verb lift. Same OpenAI-like message-row
    shape on both kinds; the lift adds the endpoint to interactive as
    a feature gain (pre-lift interactive only exposed history through
    the SSE replay on ``/events``).
    """

    ws_id: str
    messages: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Tail of the workstream's reconstructed message history "
            "(provider-fidelity OpenAI-like shape). Bounded by the "
            "``limit`` query parameter (default 100, max 500)."
        ),
    )


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
