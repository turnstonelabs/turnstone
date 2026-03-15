"""Tests for bridge event publishing — TurnCompleteEvent on idle transitions."""

from unittest.mock import MagicMock, patch

from turnstone.mq.bridge import Bridge
from turnstone.mq.protocol import ContentEvent, StateChangeEvent, TurnCompleteEvent


def _make_bridge():
    """Create a Bridge with a mock broker (no Redis or HTTP needed)."""
    broker = MagicMock()
    bridge = Bridge(server_url="http://localhost:8080", broker=broker, node_id="test-node")
    return bridge


class TestIdleTurnComplete:
    """TurnCompleteEvent should be emitted on every idle transition."""

    def test_idle_emits_turn_complete_with_correlation_id(self):
        """Bridge-initiated turn: TurnCompleteEvent has the correlation_id."""
        bridge = _make_bridge()
        bridge._active_sends["ws-1"] = "cid-abc"

        published = []
        with patch.object(
            bridge, "_publish_ws", side_effect=lambda ws, ev: published.append((ws, ev))
        ):
            bridge._handle_global_event({"type": "ws_state", "ws_id": "ws-1", "state": "idle"})

        turn_completes = [(ws, ev) for ws, ev in published if isinstance(ev, TurnCompleteEvent)]
        assert len(turn_completes) == 1
        ws, ev = turn_completes[0]
        assert ws == "ws-1"
        assert ev.correlation_id == "cid-abc"
        # correlation_id should be removed from _active_sends
        assert "ws-1" not in bridge._active_sends

    def test_idle_emits_turn_complete_without_correlation_id(self):
        """Server-UI-initiated turn: TurnCompleteEvent has empty correlation_id."""
        bridge = _make_bridge()
        # No entry in _active_sends for this workstream

        published = []
        with patch.object(
            bridge, "_publish_ws", side_effect=lambda ws, ev: published.append((ws, ev))
        ):
            bridge._handle_global_event({"type": "ws_state", "ws_id": "ws-2", "state": "idle"})

        turn_completes = [(ws, ev) for ws, ev in published if isinstance(ev, TurnCompleteEvent)]
        assert len(turn_completes) == 1
        ws, ev = turn_completes[0]
        assert ws == "ws-2"
        assert ev.correlation_id == ""

    def test_non_idle_state_does_not_emit_turn_complete(self):
        """Non-idle state transitions should emit StateChangeEvent but not TurnCompleteEvent."""
        bridge = _make_bridge()

        published = []
        with patch.object(
            bridge, "_publish_ws", side_effect=lambda ws, ev: published.append((ws, ev))
        ):
            bridge._handle_global_event({"type": "ws_state", "ws_id": "ws-3", "state": "thinking"})

        state_changes = [ev for _, ev in published if isinstance(ev, StateChangeEvent)]
        turn_completes = [ev for _, ev in published if isinstance(ev, TurnCompleteEvent)]
        assert len(state_changes) == 1
        assert state_changes[0].state == "thinking"
        assert len(turn_completes) == 0


class TestContentBuffer:
    """Bridge should accumulate content tokens and attach to TurnCompleteEvent."""

    def test_content_buffer_accumulated_in_turn_complete(self):
        """Content events should be accumulated and included in TurnCompleteEvent."""
        bridge = _make_bridge()

        # Simulate content events from per-ws SSE
        bridge._handle_ws_event("ws-1", {"type": "content", "text": "Hello "})
        bridge._handle_ws_event("ws-1", {"type": "content", "text": "world"})

        published = []
        with patch.object(
            bridge, "_publish_ws", side_effect=lambda ws, ev: published.append((ws, ev))
        ):
            bridge._handle_global_event({"type": "ws_state", "ws_id": "ws-1", "state": "idle"})

        turn_completes = [(ws, ev) for ws, ev in published if isinstance(ev, TurnCompleteEvent)]
        assert len(turn_completes) == 1
        _, ev = turn_completes[0]
        assert ev.content == "Hello world"
        # Buffer should be cleared
        assert "ws-1" not in bridge._ws_content_buffer

    def test_content_buffer_empty_for_no_content_turn(self):
        """TurnCompleteEvent.content should be empty when no content events fired."""
        bridge = _make_bridge()

        published = []
        with patch.object(
            bridge, "_publish_ws", side_effect=lambda ws, ev: published.append((ws, ev))
        ):
            bridge._handle_global_event({"type": "ws_state", "ws_id": "ws-1", "state": "idle"})

        turn_completes = [(ws, ev) for ws, ev in published if isinstance(ev, TurnCompleteEvent)]
        assert len(turn_completes) == 1
        _, ev = turn_completes[0]
        assert ev.content == ""

    def test_content_buffer_cleared_on_ws_closed(self):
        """ws_closed should clean up the content buffer."""
        bridge = _make_bridge()

        bridge._handle_ws_event("ws-1", {"type": "content", "text": "orphan"})
        assert "ws-1" in bridge._ws_content_buffer

        bridge._handle_global_event({"type": "ws_closed", "ws_id": "ws-1"})
        assert "ws-1" not in bridge._ws_content_buffer

    def test_content_buffer_publishes_content_event(self):
        """Content events should still be published to per-ws channel."""
        bridge = _make_bridge()

        published = []
        with patch.object(
            bridge, "_publish_ws", side_effect=lambda ws, ev: published.append((ws, ev))
        ):
            bridge._handle_ws_event("ws-1", {"type": "content", "text": "hello"})

        content_events = [(ws, ev) for ws, ev in published if isinstance(ev, ContentEvent)]
        assert len(content_events) == 1
        _, ev = content_events[0]
        assert ev.text == "hello"

    def test_multi_round_content_accumulates(self):
        """Content from multiple tool-use rounds accumulates in a single turn."""
        bridge = _make_bridge()

        # Round 1
        bridge._handle_ws_event("ws-1", {"type": "content", "text": "I'll run "})
        bridge._handle_ws_event("ws-1", {"type": "stream_end"})
        # Round 2 (after tool execution)
        bridge._handle_ws_event("ws-1", {"type": "content", "text": "the command."})
        bridge._handle_ws_event("ws-1", {"type": "stream_end"})

        published = []
        with patch.object(
            bridge, "_publish_ws", side_effect=lambda ws, ev: published.append((ws, ev))
        ):
            bridge._handle_global_event({"type": "ws_state", "ws_id": "ws-1", "state": "idle"})

        turn_completes = [(ws, ev) for ws, ev in published if isinstance(ev, TurnCompleteEvent)]
        assert len(turn_completes) == 1
        _, ev = turn_completes[0]
        assert ev.content == "I'll run the command."
