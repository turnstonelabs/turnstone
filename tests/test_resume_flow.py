"""Tests for the atomic workstream resumption flow.

Covers CreateWorkstreamMessage resume_ws field, WorkstreamResumedEvent,
WorkstreamCreatedEvent resumed fields, and server endpoint handling.
"""

from __future__ import annotations

import json

from turnstone.mq.protocol import (
    CreateWorkstreamMessage,
    WorkstreamCreatedEvent,
    WorkstreamResumedEvent,
)

# ---------------------------------------------------------------------------
# Protocol tests
# ---------------------------------------------------------------------------


class TestCreateWorkstreamMessageResumeField:
    def test_resume_ws_defaults_empty(self) -> None:
        msg = CreateWorkstreamMessage(name="test")
        assert msg.resume_ws == ""

    def test_resume_ws_set(self) -> None:
        msg = CreateWorkstreamMessage(name="test", resume_ws="ws-abc")
        assert msg.resume_ws == "ws-abc"

    def test_resume_ws_serializes(self) -> None:
        msg = CreateWorkstreamMessage(resume_ws="ws-xyz")
        data = json.loads(msg.to_json())
        assert data["resume_ws"] == "ws-xyz"

    def test_resume_ws_deserializes(self) -> None:
        msg = CreateWorkstreamMessage(resume_ws="ws-123")
        raw = msg.to_json()
        from turnstone.mq.protocol import InboundMessage

        restored = InboundMessage.from_json(raw)
        assert getattr(restored, "resume_ws", "") == "ws-123"


class TestWorkstreamCreatedEventResumeFields:
    def test_default_not_resumed(self) -> None:
        event = WorkstreamCreatedEvent(ws_id="ws-1", name="test")
        assert event.resumed is False
        assert event.message_count == 0

    def test_resumed_fields(self) -> None:
        event = WorkstreamCreatedEvent(ws_id="ws-1", name="test", resumed=True, message_count=42)
        assert event.resumed is True
        assert event.message_count == 42

    def test_serializes_resumed_fields(self) -> None:
        event = WorkstreamCreatedEvent(ws_id="ws-1", resumed=True, message_count=10)
        data = json.loads(event.to_json())
        assert data["resumed"] is True
        assert data["message_count"] == 10

    def test_deserializes_resumed_fields(self) -> None:
        event = WorkstreamCreatedEvent(ws_id="ws-1", resumed=True, message_count=5)
        from turnstone.mq.protocol import OutboundEvent

        restored = OutboundEvent.from_json(event.to_json())
        assert isinstance(restored, WorkstreamCreatedEvent)
        assert restored.resumed is True
        assert restored.message_count == 5


class TestWorkstreamResumedEvent:
    def test_defaults(self) -> None:
        event = WorkstreamResumedEvent(ws_id="ws-1")
        assert event.type == "ws_resumed"
        assert event.message_count == 0
        assert event.name == ""

    def test_with_values(self) -> None:
        event = WorkstreamResumedEvent(ws_id="ws-1", message_count=25, name="My Chat")
        assert event.message_count == 25
        assert event.name == "My Chat"

    def test_round_trip(self) -> None:
        event = WorkstreamResumedEvent(ws_id="ws-1", message_count=10, name="Chat")
        from turnstone.mq.protocol import OutboundEvent

        restored = OutboundEvent.from_json(event.to_json())
        assert isinstance(restored, WorkstreamResumedEvent)
        assert restored.message_count == 10
        assert restored.name == "Chat"

    def test_registered_in_outbound_registry(self) -> None:
        from turnstone.mq.protocol import _OUTBOUND_REGISTRY

        assert "ws_resumed" in _OUTBOUND_REGISTRY
        assert _OUTBOUND_REGISTRY["ws_resumed"] is WorkstreamResumedEvent
