"""Standalone SSE event dataclasses for the turnstone SDK.

These types match the JSON payloads emitted by the server and console
SSE endpoints.  They are intentionally decoupled from the MQ protocol
events in ``turnstone.mq.protocol`` so that SDK consumers do not need
the ``redis`` optional dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fields_of(cls: type[Any]) -> frozenset[str]:
    return frozenset(f.name for f in fields(cls))


# ---------------------------------------------------------------------------
# Server per-workstream events  (/v1/api/events?ws_id=X)
# ---------------------------------------------------------------------------


@dataclass
class ServerEvent:
    """Base class for all server SSE events."""

    type: str = ""
    ws_id: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServerEvent:
        """Deserialize an SSE JSON payload into a typed event."""
        etype = data.get("type", "")
        klass = _SERVER_REGISTRY.get(etype, ServerEvent)
        valid = _fields_of(klass)
        return klass(**{k: v for k, v in data.items() if k in valid})


@dataclass
class ConnectedEvent(ServerEvent):
    type: str = "connected"
    model: str = ""
    model_alias: str = ""
    skip_permissions: bool = False


@dataclass
class HistoryEvent(ServerEvent):
    type: str = "history"
    messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ThinkingStartEvent(ServerEvent):
    type: str = "thinking_start"


@dataclass
class ThinkingStopEvent(ServerEvent):
    type: str = "thinking_stop"


@dataclass
class ReasoningEvent(ServerEvent):
    type: str = "reasoning"
    text: str = ""


@dataclass
class ContentEvent(ServerEvent):
    type: str = "content"
    text: str = ""


@dataclass
class StreamEndEvent(ServerEvent):
    type: str = "stream_end"


@dataclass
class ToolInfoEvent(ServerEvent):
    type: str = "tool_info"
    items: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ApproveRequestEvent(ServerEvent):
    type: str = "approve_request"
    items: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ToolResultEvent(ServerEvent):
    type: str = "tool_result"
    call_id: str = ""
    name: str = ""
    output: str = ""


@dataclass
class ToolOutputChunkEvent(ServerEvent):
    type: str = "tool_output_chunk"
    call_id: str = ""
    chunk: str = ""


@dataclass
class StatusEvent(ServerEvent):
    type: str = "status"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    context_window: int = 0
    pct: float = 0.0
    effort: str = ""


@dataclass
class PlanReviewEvent(ServerEvent):
    type: str = "plan_review"
    content: str = ""


@dataclass
class InfoEvent(ServerEvent):
    type: str = "info"
    message: str = ""


@dataclass
class ErrorEvent(ServerEvent):
    type: str = "error"
    message: str = ""


@dataclass
class BusyErrorEvent(ServerEvent):
    type: str = "busy_error"
    message: str = ""


@dataclass
class ClearUiEvent(ServerEvent):
    type: str = "clear_ui"


# ---------------------------------------------------------------------------
# Server global events  (/v1/api/events/global)
# ---------------------------------------------------------------------------


@dataclass
class WsStateEvent(ServerEvent):
    type: str = "ws_state"
    state: str = ""
    tokens: int = 0
    context_ratio: float = 0.0
    activity: str = ""
    activity_state: str = ""


@dataclass
class WsActivityEvent(ServerEvent):
    type: str = "ws_activity"
    activity: str = ""
    activity_state: str = ""


@dataclass
class WsRenameEvent(ServerEvent):
    type: str = "ws_rename"
    name: str = ""


@dataclass
class WsClosedEvent(ServerEvent):
    type: str = "ws_closed"
    name: str = ""


# ---------------------------------------------------------------------------
# Console cluster events  (/v1/api/cluster/events)
# ---------------------------------------------------------------------------


@dataclass
class ClusterEvent:
    """Base class for console cluster SSE events."""

    type: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClusterEvent:
        etype = data.get("type", "")
        klass = _CLUSTER_REGISTRY.get(etype, ClusterEvent)
        valid = _fields_of(klass)
        return klass(**{k: v for k, v in data.items() if k in valid})


@dataclass
class NodeJoinedEvent(ClusterEvent):
    type: str = "node_joined"
    node_id: str = ""


@dataclass
class NodeLostEvent(ClusterEvent):
    type: str = "node_lost"
    node_id: str = ""


@dataclass
class ClusterStateEvent(ClusterEvent):
    type: str = "cluster_state"
    ws_id: str = ""
    node_id: str = ""
    state: str = ""
    tokens: int = 0
    context_ratio: float = 0.0
    activity: str = ""
    activity_state: str = ""


@dataclass
class ClusterWsCreatedEvent(ClusterEvent):
    type: str = "ws_created"
    ws_id: str = ""
    node_id: str = ""
    name: str = ""


@dataclass
class ClusterWsClosedEvent(ClusterEvent):
    type: str = "ws_closed"
    ws_id: str = ""


@dataclass
class ClusterWsRenameEvent(ClusterEvent):
    type: str = "ws_rename"
    ws_id: str = ""
    name: str = ""


# ---------------------------------------------------------------------------
# Type registries (built after all classes are defined)
# ---------------------------------------------------------------------------


def _type_default(cls: type[Any]) -> str:
    """Return the default value of the ``type`` field for a dataclass."""
    for f in fields(cls):
        if f.name == "type":
            return f.default  # type: ignore[return-value]
    return ""


_SERVER_REGISTRY: dict[str, type[ServerEvent]] = {
    _type_default(cls): cls
    for cls in [
        ConnectedEvent,
        HistoryEvent,
        ThinkingStartEvent,
        ThinkingStopEvent,
        ReasoningEvent,
        ContentEvent,
        StreamEndEvent,
        ToolInfoEvent,
        ApproveRequestEvent,
        ToolResultEvent,
        ToolOutputChunkEvent,
        StatusEvent,
        PlanReviewEvent,
        InfoEvent,
        ErrorEvent,
        BusyErrorEvent,
        ClearUiEvent,
        WsStateEvent,
        WsActivityEvent,
        WsRenameEvent,
        WsClosedEvent,
    ]
}

_CLUSTER_REGISTRY: dict[str, type[ClusterEvent]] = {
    _type_default(cls): cls
    for cls in [
        NodeJoinedEvent,
        NodeLostEvent,
        ClusterStateEvent,
        ClusterWsCreatedEvent,
        ClusterWsClosedEvent,
        ClusterWsRenameEvent,
    ]
}
