"""Tests for turnstone.sdk.events — SSE event deserialization."""

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

# ---------------------------------------------------------------------------
# Per-workstream events
# ---------------------------------------------------------------------------


def test_connected_event():
    e = ServerEvent.from_dict(
        {"type": "connected", "model": "gpt-5", "model_alias": "fast", "skip_permissions": True}
    )
    assert isinstance(e, ConnectedEvent)
    assert e.model == "gpt-5"
    assert e.model_alias == "fast"
    assert e.skip_permissions is True


def test_history_event():
    msgs = [{"role": "user", "content": "hi"}]
    e = ServerEvent.from_dict({"type": "history", "messages": msgs})
    assert isinstance(e, HistoryEvent)
    assert e.messages == msgs


def test_thinking_start_stop():
    e1 = ServerEvent.from_dict({"type": "thinking_start"})
    e2 = ServerEvent.from_dict({"type": "thinking_stop"})
    assert isinstance(e1, ThinkingStartEvent)
    assert isinstance(e2, ThinkingStopEvent)


def test_content_event():
    e = ServerEvent.from_dict({"type": "content", "text": "hello"})
    assert isinstance(e, ContentEvent)
    assert e.text == "hello"


def test_reasoning_event():
    e = ServerEvent.from_dict({"type": "reasoning", "text": "step 1"})
    assert isinstance(e, ReasoningEvent)
    assert e.text == "step 1"


def test_stream_end_event():
    e = ServerEvent.from_dict({"type": "stream_end"})
    assert isinstance(e, StreamEndEvent)


def test_tool_info_event():
    items = [{"name": "search", "call_id": "c1"}]
    e = ServerEvent.from_dict({"type": "tool_info", "items": items})
    assert isinstance(e, ToolInfoEvent)
    assert e.items == items


def test_approve_request_event():
    items = [{"name": "bash", "call_id": "c2", "arguments": "ls"}]
    e = ServerEvent.from_dict({"type": "approve_request", "items": items})
    assert isinstance(e, ApproveRequestEvent)
    assert len(e.items) == 1


def test_tool_result_event():
    e = ServerEvent.from_dict(
        {"type": "tool_result", "call_id": "c1", "name": "search", "output": "found it"}
    )
    assert isinstance(e, ToolResultEvent)
    assert e.call_id == "c1"
    assert e.name == "search"
    assert e.output == "found it"


def test_tool_output_chunk_event():
    e = ServerEvent.from_dict({"type": "tool_output_chunk", "call_id": "c1", "chunk": "line1\n"})
    assert isinstance(e, ToolOutputChunkEvent)
    assert e.chunk == "line1\n"


def test_status_event():
    e = ServerEvent.from_dict(
        {
            "type": "status",
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "context_window": 128000,
            "pct": 0.12,
            "effort": "medium",
        }
    )
    assert isinstance(e, StatusEvent)
    assert e.prompt_tokens == 100
    assert e.total_tokens == 150
    assert e.pct == 0.12
    assert e.effort == "medium"


def test_plan_review_event():
    e = ServerEvent.from_dict({"type": "plan_review", "content": "## Plan\n1. Do X"})
    assert isinstance(e, PlanReviewEvent)
    assert "Plan" in e.content


def test_info_event():
    e = ServerEvent.from_dict({"type": "info", "message": "[compacted]"})
    assert isinstance(e, InfoEvent)
    assert e.message == "[compacted]"


def test_error_event():
    e = ServerEvent.from_dict({"type": "error", "message": "Something broke"})
    assert isinstance(e, ErrorEvent)
    assert e.message == "Something broke"


def test_busy_error_event():
    e = ServerEvent.from_dict({"type": "busy_error", "message": "Already processing a request."})
    assert isinstance(e, BusyErrorEvent)
    assert "Already" in e.message


def test_clear_ui_event():
    e = ServerEvent.from_dict({"type": "clear_ui"})
    assert isinstance(e, ClearUiEvent)


# ---------------------------------------------------------------------------
# Global events
# ---------------------------------------------------------------------------


def test_ws_state_event():
    e = ServerEvent.from_dict(
        {
            "type": "ws_state",
            "ws_id": "ws1",
            "state": "thinking",
            "tokens": 500,
            "context_ratio": 0.3,
            "activity": "Writing code",
            "activity_state": "thinking",
        }
    )
    assert isinstance(e, WsStateEvent)
    assert e.ws_id == "ws1"
    assert e.state == "thinking"
    assert e.tokens == 500


def test_ws_activity_event():
    e = ServerEvent.from_dict(
        {"type": "ws_activity", "ws_id": "ws1", "activity": "reading", "activity_state": "tool"}
    )
    assert isinstance(e, WsActivityEvent)
    assert e.activity == "reading"


def test_ws_rename_event():
    e = ServerEvent.from_dict({"type": "ws_rename", "ws_id": "ws1", "name": "My Chat"})
    assert isinstance(e, WsRenameEvent)
    assert e.name == "My Chat"


def test_ws_closed_event():
    e = ServerEvent.from_dict({"type": "ws_closed", "ws_id": "ws1", "name": "old"})
    assert isinstance(e, WsClosedEvent)
    assert e.name == "old"


# ---------------------------------------------------------------------------
# Cluster events
# ---------------------------------------------------------------------------


def test_node_joined_event():
    e = ClusterEvent.from_dict({"type": "node_joined", "node_id": "host1_abc"})
    assert isinstance(e, NodeJoinedEvent)
    assert e.node_id == "host1_abc"


def test_node_lost_event():
    e = ClusterEvent.from_dict({"type": "node_lost", "node_id": "host2_def"})
    assert isinstance(e, NodeLostEvent)
    assert e.node_id == "host2_def"


def test_cluster_state_event():
    e = ClusterEvent.from_dict(
        {
            "type": "cluster_state",
            "ws_id": "ws1",
            "node_id": "n1",
            "state": "running",
            "tokens": 1000,
            "context_ratio": 0.5,
            "activity": "executing tool",
            "activity_state": "tool",
        }
    )
    assert isinstance(e, ClusterStateEvent)
    assert e.node_id == "n1"
    assert e.state == "running"
    assert e.tokens == 1000


def test_cluster_ws_created_event():
    e = ClusterEvent.from_dict(
        {"type": "ws_created", "ws_id": "ws2", "node_id": "n1", "name": "New WS"}
    )
    assert isinstance(e, ClusterWsCreatedEvent)
    assert e.ws_id == "ws2"
    assert e.name == "New WS"


def test_cluster_ws_closed_event():
    e = ClusterEvent.from_dict({"type": "ws_closed", "ws_id": "ws2"})
    assert isinstance(e, ClusterWsClosedEvent)
    assert e.ws_id == "ws2"


def test_cluster_ws_rename_event():
    e = ClusterEvent.from_dict({"type": "ws_rename", "ws_id": "ws2", "name": "Renamed"})
    assert isinstance(e, ClusterWsRenameEvent)
    assert e.name == "Renamed"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_unknown_server_event_falls_back():
    e = ServerEvent.from_dict({"type": "future_event", "ws_id": "ws1"})
    assert type(e) is ServerEvent
    assert e.type == "future_event"
    assert e.ws_id == "ws1"


def test_unknown_cluster_event_falls_back():
    e = ClusterEvent.from_dict({"type": "future_cluster_event"})
    assert type(e) is ClusterEvent
    assert e.type == "future_cluster_event"


def test_extra_fields_ignored():
    e = ServerEvent.from_dict({"type": "content", "text": "hi", "extra_field": 999})
    assert isinstance(e, ContentEvent)
    assert e.text == "hi"


def test_missing_type_defaults_to_base():
    e = ServerEvent.from_dict({"ws_id": "ws1"})
    assert type(e) is ServerEvent
    assert e.ws_id == "ws1"
    assert e.type == ""
