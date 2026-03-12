"""Tests for bridge event publishing — TurnCompleteEvent on idle transitions."""

from unittest.mock import MagicMock, patch

from turnstone.mq.bridge import Bridge
from turnstone.mq.protocol import StateChangeEvent, TurnCompleteEvent


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
