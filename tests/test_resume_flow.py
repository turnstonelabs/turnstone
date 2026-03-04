"""Tests for the atomic workstream resumption flow.

Covers CreateWorkstreamMessage resume_session field, SessionResumedEvent,
WorkstreamCreatedEvent resumed fields, and server endpoint handling.
"""

from __future__ import annotations

import json

from turnstone.mq.protocol import (
    CreateWorkstreamMessage,
    SessionResumedEvent,
    WorkstreamCreatedEvent,
)

# ---------------------------------------------------------------------------
# Protocol tests
# ---------------------------------------------------------------------------


class TestCreateWorkstreamMessageResumeField:
    def test_resume_session_defaults_empty(self) -> None:
        msg = CreateWorkstreamMessage(name="test")
        assert msg.resume_session == ""

    def test_resume_session_set(self) -> None:
        msg = CreateWorkstreamMessage(name="test", resume_session="sess-abc")
        assert msg.resume_session == "sess-abc"

    def test_resume_session_serializes(self) -> None:
        msg = CreateWorkstreamMessage(resume_session="sess-xyz")
        data = json.loads(msg.to_json())
        assert data["resume_session"] == "sess-xyz"

    def test_resume_session_deserializes(self) -> None:
        msg = CreateWorkstreamMessage(resume_session="sess-123")
        raw = msg.to_json()
        from turnstone.mq.protocol import InboundMessage

        restored = InboundMessage.from_json(raw)
        assert getattr(restored, "resume_session", "") == "sess-123"


class TestWorkstreamCreatedEventResumeFields:
    def test_default_not_resumed(self) -> None:
        event = WorkstreamCreatedEvent(ws_id="ws-1", name="test")
        assert event.resumed is False
        assert event.session_id == ""
        assert event.message_count == 0

    def test_resumed_fields(self) -> None:
        event = WorkstreamCreatedEvent(
            ws_id="ws-1", name="test", resumed=True, session_id="s-1", message_count=42
        )
        assert event.resumed is True
        assert event.session_id == "s-1"
        assert event.message_count == 42

    def test_serializes_resumed_fields(self) -> None:
        event = WorkstreamCreatedEvent(
            ws_id="ws-1", resumed=True, session_id="s-1", message_count=10
        )
        data = json.loads(event.to_json())
        assert data["resumed"] is True
        assert data["session_id"] == "s-1"
        assert data["message_count"] == 10

    def test_deserializes_resumed_fields(self) -> None:
        event = WorkstreamCreatedEvent(
            ws_id="ws-1", resumed=True, session_id="s-1", message_count=5
        )
        from turnstone.mq.protocol import OutboundEvent

        restored = OutboundEvent.from_json(event.to_json())
        assert isinstance(restored, WorkstreamCreatedEvent)
        assert restored.resumed is True
        assert restored.session_id == "s-1"
        assert restored.message_count == 5


class TestSessionResumedEvent:
    def test_defaults(self) -> None:
        event = SessionResumedEvent(ws_id="ws-1")
        assert event.type == "session_resumed"
        assert event.session_id == ""
        assert event.message_count == 0
        assert event.name == ""

    def test_with_values(self) -> None:
        event = SessionResumedEvent(
            ws_id="ws-1", session_id="s-abc", message_count=25, name="My Chat"
        )
        assert event.session_id == "s-abc"
        assert event.message_count == 25
        assert event.name == "My Chat"

    def test_round_trip(self) -> None:
        event = SessionResumedEvent(ws_id="ws-1", session_id="s-abc", message_count=10, name="Chat")
        from turnstone.mq.protocol import OutboundEvent

        restored = OutboundEvent.from_json(event.to_json())
        assert isinstance(restored, SessionResumedEvent)
        assert restored.session_id == "s-abc"
        assert restored.message_count == 10
        assert restored.name == "Chat"

    def test_registered_in_outbound_registry(self) -> None:
        from turnstone.mq.protocol import _OUTBOUND_REGISTRY

        assert "session_resumed" in _OUTBOUND_REGISTRY
        assert _OUTBOUND_REGISTRY["session_resumed"] is SessionResumedEvent
