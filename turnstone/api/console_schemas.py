"""Pydantic v2 models for turnstone-console API endpoints."""

from __future__ import annotations

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
    health: dict[str, str] = Field(default_factory=dict)
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
    health: dict[str, str] = Field(default_factory=dict)
    workstreams: list[ClusterWorkstreamInfo] = []
    aggregate: dict[str, int] = Field(default_factory=dict)
    reachable: bool = True


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
