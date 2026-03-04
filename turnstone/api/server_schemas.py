"""Pydantic v2 models for turnstone-server API endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Workstream management
# ---------------------------------------------------------------------------


class SendRequest(BaseModel):
    message: str = Field(description="User message text")
    ws_id: str = Field(description="Target workstream ID")


class SendResponse(BaseModel):
    status: str = Field(description="'ok' or 'busy'", examples=["ok", "busy"])


class ApproveRequest(BaseModel):
    approved: bool = Field(description="True to approve, false to deny")
    feedback: str | None = Field(default=None, description="Optional denial reason")
    always: bool = Field(default=False, description="Enable auto-approve for this tool")
    ws_id: str = Field(description="Target workstream ID")


class PlanFeedbackRequest(BaseModel):
    feedback: str = Field(description="Feedback text; empty string means approval")
    ws_id: str = Field(description="Target workstream ID")


class CommandRequest(BaseModel):
    command: str = Field(description="Slash command (e.g. /clear, /new, /resume)")
    ws_id: str = Field(description="Target workstream ID")


class CreateWorkstreamRequest(BaseModel):
    name: str = Field(default="", description="Workstream display name (auto-generated if empty)")
    model: str = Field(default="", description="Model alias from registry")
    auto_approve: bool = Field(default=False, description="Auto-approve all tool calls")


class CreateWorkstreamResponse(BaseModel):
    ws_id: str = Field(description="Unique ID of the new workstream")
    name: str = Field(description="Assigned workstream name")


class CloseWorkstreamRequest(BaseModel):
    ws_id: str = Field(description="Workstream ID to close")


# ---------------------------------------------------------------------------
# List / dashboard
# ---------------------------------------------------------------------------


class WorkstreamInfo(BaseModel):
    id: str
    name: str
    state: str
    session_id: str | None = None


class ListWorkstreamsResponse(BaseModel):
    workstreams: list[WorkstreamInfo]


class DashboardWorkstream(BaseModel):
    id: str
    name: str
    state: str
    session_id: str | None = None
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
# Sessions
# ---------------------------------------------------------------------------


class SessionInfo(BaseModel):
    session_id: str
    alias: str | None = None
    title: str | None = None
    created: str
    updated: str
    message_count: int


class ListSessionsResponse(BaseModel):
    sessions: list[SessionInfo]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class BackendStatus(BaseModel):
    status: str = Field(examples=["up", "down"])
    circuit_state: str = Field(examples=["closed", "open", "half_open"])


class WorkstreamCounts(BaseModel):
    total: int = 0
    idle: int = 0
    thinking: int = 0
    running: int = 0
    attention: int = 0
    error: int = 0


class HealthResponse(BaseModel):
    status: str = Field(examples=["ok", "degraded"])
    version: str = ""
    uptime_seconds: float = 0.0
    model: str = ""
    workstreams: WorkstreamCounts = WorkstreamCounts()
    backend: BackendStatus | None = None
