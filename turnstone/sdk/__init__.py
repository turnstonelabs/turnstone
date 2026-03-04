"""Turnstone Client SDK — typed HTTP clients for server and console APIs.

Quick start::

    from turnstone.sdk import TurnstoneServer

    with TurnstoneServer("http://localhost:8080", token="tok_xxx") as client:
        ws = client.create_workstream(name="demo")
        result = client.send_and_wait("Hello!", ws.ws_id)
        print(result.content)
"""

from __future__ import annotations

from turnstone.sdk._types import TurnResult, TurnstoneAPIError
from turnstone.sdk.console import AsyncTurnstoneConsole, TurnstoneConsole
from turnstone.sdk.events import (
    ApproveRequestEvent,
    BusyErrorEvent,
    ClearUiEvent,
    ClusterEvent,
    ClusterStateEvent,
    ClusterWsClosedEvent,
    ClusterWsCreatedEvent,
    ClusterWsRenameEvent,
    ConnectedEvent,
    ContentEvent,
    ErrorEvent,
    HistoryEvent,
    InfoEvent,
    NodeJoinedEvent,
    NodeLostEvent,
    PlanReviewEvent,
    ReasoningEvent,
    ServerEvent,
    StatusEvent,
    StreamEndEvent,
    ThinkingStartEvent,
    ThinkingStopEvent,
    ToolInfoEvent,
    ToolOutputChunkEvent,
    ToolResultEvent,
    WsActivityEvent,
    WsClosedEvent,
    WsRenameEvent,
    WsStateEvent,
)
from turnstone.sdk.server import AsyncTurnstoneServer, TurnstoneServer

__all__ = [
    # Clients
    "AsyncTurnstoneServer",
    "TurnstoneServer",
    "AsyncTurnstoneConsole",
    "TurnstoneConsole",
    # Result types
    "TurnResult",
    "TurnstoneAPIError",
    # Server events
    "ServerEvent",
    "ConnectedEvent",
    "HistoryEvent",
    "ThinkingStartEvent",
    "ThinkingStopEvent",
    "ReasoningEvent",
    "ContentEvent",
    "StreamEndEvent",
    "ToolInfoEvent",
    "ApproveRequestEvent",
    "ToolResultEvent",
    "ToolOutputChunkEvent",
    "StatusEvent",
    "PlanReviewEvent",
    "InfoEvent",
    "ErrorEvent",
    "BusyErrorEvent",
    "ClearUiEvent",
    "WsStateEvent",
    "WsActivityEvent",
    "WsRenameEvent",
    "WsClosedEvent",
    # Cluster events
    "ClusterEvent",
    "NodeJoinedEvent",
    "NodeLostEvent",
    "ClusterStateEvent",
    "ClusterWsCreatedEvent",
    "ClusterWsClosedEvent",
    "ClusterWsRenameEvent",
]
