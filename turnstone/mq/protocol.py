"""Message protocol for turnstone message queue integration.

Defines all structured message types exchanged between the client and bridge.
Inbound messages flow from client → bridge via a reliable queue.
Outbound events flow from bridge → client via pub/sub channels.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Any

# ---------------------------------------------------------------------------
# Inbound messages (client → bridge)
# ---------------------------------------------------------------------------


@dataclass
class InboundMessage:
    """Base for all messages sent by clients to the bridge."""

    type: str = ""
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> InboundMessage:
        data = json.loads(raw)
        msg_type = data.get("type", "")
        klass = _INBOUND_REGISTRY.get(msg_type)
        if klass is None:
            raise ValueError(f"Unknown inbound message type: {msg_type!r}")
        valid = {f.name for f in fields(klass)}
        return klass(**{k: v for k, v in data.items() if k in valid})


@dataclass
class SendMessage(InboundMessage):
    """Send a user message to a workstream."""

    type: str = "send"
    ws_id: str = ""
    message: str = ""
    auto_approve: bool = False
    auto_approve_tools: list[str] = field(default_factory=list)
    name: str = ""
    target_node: str = ""


@dataclass
class ApproveMessage(InboundMessage):
    """Respond to a tool approval request."""

    type: str = "approve"
    ws_id: str = ""
    request_id: str = ""
    approved: bool = True
    feedback: str | None = None
    always: bool = False


@dataclass
class PlanFeedbackMessage(InboundMessage):
    """Respond to a plan review request."""

    type: str = "plan_feedback"
    ws_id: str = ""
    request_id: str = ""
    feedback: str = ""


@dataclass
class CommandMessage(InboundMessage):
    """Execute a slash command."""

    type: str = "command"
    ws_id: str = ""
    command: str = ""


@dataclass
class CreateWorkstreamMessage(InboundMessage):
    """Create a new workstream."""

    type: str = "create_workstream"
    name: str = ""
    auto_approve: bool = False
    auto_approve_tools: list[str] = field(default_factory=list)
    target_node: str = ""
    model: str = ""
    initial_message: str = ""
    resume_ws: str = ""
    user_id: str = ""
    skill: str = ""


@dataclass
class CloseWorkstreamMessage(InboundMessage):
    """Close a workstream."""

    type: str = "close_workstream"
    ws_id: str = ""


@dataclass
class ListWorkstreamsMessage(InboundMessage):
    """Request the list of active workstreams."""

    type: str = "list_workstreams"


@dataclass
class HealthMessage(InboundMessage):
    """Request health status."""

    type: str = "health"


@dataclass
class ListNodesMessage(InboundMessage):
    """Request the list of active bridge nodes."""

    type: str = "list_nodes"


@dataclass
class CancelMessage(InboundMessage):
    """Cancel the active generation in a workstream."""

    type: str = "cancel"
    ws_id: str = ""


# ---------------------------------------------------------------------------
# Outbound events (bridge → client)
# ---------------------------------------------------------------------------


@dataclass
class OutboundEvent:
    """Base for all events published by the bridge."""

    type: str = ""
    ws_id: str = ""
    correlation_id: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> OutboundEvent:
        data = json.loads(raw)
        msg_type = data.get("type", "")
        klass = _OUTBOUND_REGISTRY.get(msg_type, OutboundEvent)
        valid = {f.name for f in fields(klass)}
        return klass(**{k: v for k, v in data.items() if k in valid})


@dataclass
class AckEvent(OutboundEvent):
    """Acknowledgment that an inbound message was received."""

    type: str = "ack"
    status: str = "ok"
    detail: str = ""


@dataclass
class ContentEvent(OutboundEvent):
    """Streamed content token from the assistant."""

    type: str = "content"
    text: str = ""


@dataclass
class ReasoningEvent(OutboundEvent):
    """Streamed reasoning token."""

    type: str = "reasoning"
    text: str = ""


@dataclass
class ToolInfoEvent(OutboundEvent):
    """Tool call info (auto-approved tools)."""

    type: str = "tool_info"
    items: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ApprovalRequestEvent(OutboundEvent):
    """Tool approval request forwarded from the server.

    The client must respond with an ApproveMessage whose
    request_id matches this event's correlation_id.
    """

    type: str = "approval_request"
    items: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ToolOutputChunkEvent(OutboundEvent):
    """Incremental streaming output from a bash tool."""

    type: str = "tool_output_chunk"
    call_id: str = ""
    chunk: str = ""


@dataclass
class ToolResultEvent(OutboundEvent):
    """Tool execution result."""

    type: str = "tool_result"
    call_id: str = ""
    name: str = ""
    output: str = ""


@dataclass
class PlanReviewEvent(OutboundEvent):
    """Plan review request forwarded from the server.

    The client must respond with a PlanFeedbackMessage whose
    request_id matches this event's correlation_id.
    """

    type: str = "plan_review"
    content: str = ""


@dataclass
class StatusEvent(OutboundEvent):
    """Token usage status update."""

    type: str = "status"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    context_window: int = 0
    pct: float = 0.0
    effort: str = ""
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass
class StateChangeEvent(OutboundEvent):
    """Workstream state transition."""

    type: str = "state_change"
    state: str = ""


@dataclass
class TurnCompleteEvent(OutboundEvent):
    """Emitted when a workstream finishes processing (returns to IDLE).

    This is a synthetic event produced by the bridge when it detects
    the ws_state transition to 'idle'.  ``correlation_id`` is set for
    MQ-initiated turns and empty for turns initiated from the server UI.

    ``content`` carries the full assistant response text piggybacked on
    the server's idle SSE event (accumulated server-side in WebUI).
    Downstream consumers (e.g. Discord bot) use it for catch-up when the
    streaming path missed events, and as the primary delivery path for
    bidirectional notification DM forwarding.
    """

    type: str = "turn_complete"
    content: str = ""


@dataclass
class StreamEndEvent(OutboundEvent):
    """LLM stream ended."""

    type: str = "stream_end"


@dataclass
class WorkstreamCreatedEvent(OutboundEvent):
    """New workstream created."""

    type: str = "ws_created"
    name: str = ""
    node_id: str = ""
    resumed: bool = False
    message_count: int = 0


@dataclass
class WorkstreamClosedEvent(OutboundEvent):
    """Workstream closed."""

    type: str = "ws_closed"


@dataclass
class WorkstreamListEvent(OutboundEvent):
    """Workstream list response."""

    type: str = "ws_list"
    workstreams: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class WorkstreamRenameEvent(OutboundEvent):
    """Workstream renamed."""

    type: str = "ws_rename"
    name: str = ""


@dataclass
class HealthResponseEvent(OutboundEvent):
    """Health status response."""

    type: str = "health_response"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ErrorEvent(OutboundEvent):
    """Error event."""

    type: str = "error"
    message: str = ""


@dataclass
class InfoEvent(OutboundEvent):
    """Informational event."""

    type: str = "info"
    message: str = ""


@dataclass
class NodeListEvent(OutboundEvent):
    """List of active bridge nodes."""

    type: str = "node_list"
    nodes: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class WorkstreamResumedEvent(OutboundEvent):
    """Confirmation that a workstream was resumed during creation."""

    type: str = "ws_resumed"
    message_count: int = 0
    name: str = ""


@dataclass
class ClusterStateEvent(OutboundEvent):
    """Workstream state change with node attribution for cluster dashboard."""

    type: str = "cluster_state"
    ws_id: str = ""
    state: str = ""
    node_id: str = ""
    tokens: int = 0
    context_ratio: float = 0.0
    activity: str = ""
    activity_state: str = ""


@dataclass
class IntentVerdictEvent(OutboundEvent):
    """Intent validation verdict for a pending tool approval."""

    type: str = "intent_verdict"
    call_id: str = ""
    func_name: str = ""
    intent_summary: str = ""
    risk_level: str = ""
    confidence: float = 0.0
    recommendation: str = ""
    reasoning: str = ""
    evidence: str = "[]"  # JSON array string
    tier: str = ""
    judge_model: str = ""
    verdict_id: str = ""
    latency_ms: int = 0


@dataclass
class OutputWarningEvent(OutboundEvent):
    """Output guard warning for tool execution result."""

    type: str = "output_warning"
    call_id: str = ""
    func_name: str = ""
    risk_level: str = "none"
    flags: str = "[]"
    annotations: str = "[]"
    redacted: int = 0


@dataclass
class ConfigChangeEvent(OutboundEvent):
    """System setting changed — nodes should invalidate config cache."""

    type: str = "config_change"
    key: str = ""
    node_id: str = ""
    action: str = ""  # "set" | "delete"


# ---------------------------------------------------------------------------
# Type registries (built after all classes are defined)
# ---------------------------------------------------------------------------


def _type_default(cls: type[Any]) -> str:
    """Return the default value of the 'type' field for a dataclass."""
    for f in fields(cls):
        if f.name == "type":
            return f.default  # type: ignore[return-value]
    return ""


_INBOUND_REGISTRY: dict[str, type[InboundMessage]] = {
    _type_default(cls): cls
    for cls in [
        SendMessage,
        ApproveMessage,
        PlanFeedbackMessage,
        CommandMessage,
        CreateWorkstreamMessage,
        CloseWorkstreamMessage,
        ListWorkstreamsMessage,
        HealthMessage,
        ListNodesMessage,
        CancelMessage,
    ]
}

_OUTBOUND_REGISTRY: dict[str, type[OutboundEvent]] = {
    _type_default(cls): cls
    for cls in [
        AckEvent,
        ContentEvent,
        ReasoningEvent,
        ToolInfoEvent,
        ApprovalRequestEvent,
        ToolOutputChunkEvent,
        ToolResultEvent,
        PlanReviewEvent,
        StatusEvent,
        StateChangeEvent,
        TurnCompleteEvent,
        StreamEndEvent,
        WorkstreamCreatedEvent,
        WorkstreamClosedEvent,
        WorkstreamListEvent,
        WorkstreamRenameEvent,
        HealthResponseEvent,
        ErrorEvent,
        InfoEvent,
        NodeListEvent,
        WorkstreamResumedEvent,
        ClusterStateEvent,
        IntentVerdictEvent,
        OutputWarningEvent,
        ConfigChangeEvent,
    ]
}
