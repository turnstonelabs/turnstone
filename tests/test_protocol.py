"""Tests for turnstone.mq.protocol message serialization."""

import json

import pytest

from turnstone.mq.protocol import (
    AckEvent,
    ApprovalRequestEvent,
    ApproveMessage,
    CancelMessage,
    CloseWorkstreamMessage,
    CommandMessage,
    ContentEvent,
    CreateWorkstreamMessage,
    ErrorEvent,
    HealthMessage,
    HealthResponseEvent,
    InboundMessage,
    InfoEvent,
    ListNodesMessage,
    ListWorkstreamsMessage,
    NodeListEvent,
    OutboundEvent,
    PlanFeedbackMessage,
    PlanReviewEvent,
    ReasoningEvent,
    SendMessage,
    StateChangeEvent,
    StatusEvent,
    StreamEndEvent,
    ToolInfoEvent,
    ToolResultEvent,
    TurnCompleteEvent,
    WorkstreamClosedEvent,
    WorkstreamCreatedEvent,
    WorkstreamListEvent,
    WorkstreamRenameEvent,
)

# ---------------------------------------------------------------------------
# Inbound message round-trip tests
# ---------------------------------------------------------------------------

INBOUND_TYPES = [
    (
        SendMessage,
        {
            "message": "hello",
            "ws_id": "abc",
            "auto_approve": True,
            "auto_approve_tools": ["bash"],
        },
    ),
    (
        ApproveMessage,
        {"ws_id": "abc", "request_id": "r1", "approved": True, "feedback": "ok"},
    ),
    (
        PlanFeedbackMessage,
        {"ws_id": "abc", "request_id": "r2", "feedback": "looks good"},
    ),
    (CommandMessage, {"ws_id": "abc", "command": "/clear"}),
    (
        CreateWorkstreamMessage,
        {"name": "test-ws", "auto_approve": False, "auto_approve_tools": ["read_file"]},
    ),
    (CloseWorkstreamMessage, {"ws_id": "abc"}),
    (ListWorkstreamsMessage, {}),
    (HealthMessage, {}),
    (ListNodesMessage, {}),
    (CancelMessage, {"ws_id": "abc"}),
]


@pytest.mark.parametrize("cls,kwargs", INBOUND_TYPES)
def test_inbound_round_trip(cls, kwargs):
    msg = cls(**kwargs)
    raw = msg.to_json()
    parsed = json.loads(raw)

    # type field matches
    assert parsed["type"] == msg.type

    # correlation_id auto-generated
    assert len(msg.correlation_id) == 12
    assert parsed["correlation_id"] == msg.correlation_id

    # timestamp present
    assert msg.timestamp > 0

    # Deserialize back
    restored = InboundMessage.from_json(raw)
    assert type(restored) is cls
    assert restored.type == msg.type
    assert restored.correlation_id == msg.correlation_id

    # Check custom fields
    for k, v in kwargs.items():
        assert getattr(restored, k) == v


def test_inbound_unknown_type():
    with pytest.raises(ValueError, match="Unknown inbound"):
        InboundMessage.from_json('{"type": "nonexistent"}')


def test_inbound_extra_fields_ignored():
    raw = json.dumps({"type": "send", "message": "hi", "extra_field": 42})
    msg = InboundMessage.from_json(raw)
    assert isinstance(msg, SendMessage)
    assert msg.message == "hi"
    assert not hasattr(msg, "extra_field")


# ---------------------------------------------------------------------------
# Outbound event round-trip tests
# ---------------------------------------------------------------------------

OUTBOUND_TYPES = [
    (AckEvent, {"status": "ok", "detail": "done"}),
    (ContentEvent, {"text": "hello world"}),
    (ReasoningEvent, {"text": "thinking..."}),
    (ToolInfoEvent, {"items": [{"name": "bash", "preview": "ls"}]}),
    (ApprovalRequestEvent, {"items": [{"name": "bash", "needs_approval": True}]}),
    (ToolResultEvent, {"call_id": "call_123", "name": "bash", "output": "file.txt"}),
    (PlanReviewEvent, {"content": "# Plan\n\nStep 1: ..."}),
    (StatusEvent, {"prompt_tokens": 100, "completion_tokens": 50, "pct": 0.42}),
    (StateChangeEvent, {"state": "thinking"}),
    (TurnCompleteEvent, {}),
    (StreamEndEvent, {}),
    (WorkstreamCreatedEvent, {"name": "test-ws"}),
    (WorkstreamClosedEvent, {}),
    (WorkstreamListEvent, {"workstreams": [{"id": "abc", "name": "ws"}]}),
    (WorkstreamRenameEvent, {"name": "renamed"}),
    (HealthResponseEvent, {"data": {"status": "ok"}}),
    (ErrorEvent, {"message": "something broke"}),
    (InfoEvent, {"message": "heads up"}),
    (
        NodeListEvent,
        {"nodes": [{"node_id": "server-12", "server_url": "http://x:8080"}]},
    ),
]


@pytest.mark.parametrize("cls,kwargs", OUTBOUND_TYPES)
def test_outbound_round_trip(cls, kwargs):
    event = cls(ws_id="ws1", correlation_id="c1", **kwargs)
    raw = event.to_json()
    parsed = json.loads(raw)

    assert parsed["type"] == event.type
    assert parsed["ws_id"] == "ws1"
    assert parsed["correlation_id"] == "c1"

    restored = OutboundEvent.from_json(raw)
    assert type(restored) is cls
    assert restored.ws_id == "ws1"
    assert restored.correlation_id == "c1"

    for k, v in kwargs.items():
        assert getattr(restored, k) == v


def test_outbound_unknown_type_falls_back():
    raw = json.dumps({"type": "future_event", "ws_id": "x"})
    event = OutboundEvent.from_json(raw)
    assert isinstance(event, OutboundEvent)
    assert event.ws_id == "x"


def test_send_message_defaults():
    msg = SendMessage(message="hello")
    assert msg.ws_id == ""
    assert msg.auto_approve is False
    assert msg.auto_approve_tools == []
    assert msg.name == ""
    assert msg.target_node == ""
    assert len(msg.correlation_id) == 12


def test_create_workstream_with_tools():
    msg = CreateWorkstreamMessage(
        name="ci-runner",
        auto_approve=False,
        auto_approve_tools=["bash", "read_file", "search"],
    )
    raw = msg.to_json()
    restored = InboundMessage.from_json(raw)
    assert restored.auto_approve_tools == ["bash", "read_file", "search"]
    assert restored.name == "ci-runner"


def test_send_message_target_node():
    msg = SendMessage(message="check disk", target_node="server-12")
    raw = msg.to_json()
    restored = InboundMessage.from_json(raw)
    assert isinstance(restored, SendMessage)
    assert restored.target_node == "server-12"
    assert restored.message == "check disk"


def test_create_workstream_target_node():
    msg = CreateWorkstreamMessage(name="debug-ws", target_node="gpu-node-3")
    raw = msg.to_json()
    restored = InboundMessage.from_json(raw)
    assert isinstance(restored, CreateWorkstreamMessage)
    assert restored.target_node == "gpu-node-3"
    assert restored.name == "debug-ws"


def test_create_workstream_template_field():
    msg = CreateWorkstreamMessage(name="ws", template="code-review")
    assert msg.template == "code-review"
    raw = msg.to_json()
    restored = InboundMessage.from_json(raw)
    assert isinstance(restored, CreateWorkstreamMessage)
    assert restored.template == "code-review"


def test_create_workstream_template_default_empty():
    msg = CreateWorkstreamMessage(name="ws")
    assert msg.template == ""


def test_list_nodes_round_trip():
    msg = ListNodesMessage()
    raw = msg.to_json()
    restored = InboundMessage.from_json(raw)
    assert isinstance(restored, ListNodesMessage)


def test_node_list_event_round_trip():
    nodes = [{"node_id": "a", "server_url": "http://a:8080"}]
    event = NodeListEvent(nodes=nodes, correlation_id="c1")
    raw = event.to_json()
    restored = OutboundEvent.from_json(raw)
    assert isinstance(restored, NodeListEvent)
    assert restored.nodes == nodes
