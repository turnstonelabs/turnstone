"""Tests for WebUI content accumulation — server-side single source of truth."""

import queue

import pytest

from turnstone.server import WebUI


@pytest.fixture(autouse=True)
def _reset_global_queue():
    """Ensure WebUI._global_queue is set for tests and cleaned up after."""
    WebUI._global_queue = queue.Queue()
    yield
    WebUI._global_queue = None


def _make_ui() -> WebUI:
    """Create a WebUI with a global queue for capturing broadcast events."""
    return WebUI(ws_id="ws-test")


def _drain_global() -> list[dict]:
    """Drain all events from the global queue."""
    events = []
    assert WebUI._global_queue is not None
    while not WebUI._global_queue.empty():
        events.append(WebUI._global_queue.get_nowait())
    return events


class TestContentAccumulation:
    """WebUI should accumulate content tokens and include in idle broadcast."""

    def test_content_token_accumulates(self):
        """on_content_token should append to _ws_turn_content."""
        ui = _make_ui()
        ui.on_content_token("Hello ")
        ui.on_content_token("world")
        assert ui._ws_turn_content == ["Hello ", "world"]

    def test_idle_broadcast_includes_content(self):
        """_broadcast_state('idle') should include joined content and reset."""
        ui = _make_ui()
        ui.on_content_token("Hello ")
        ui.on_content_token("world")
        ui._broadcast_state("idle")

        events = _drain_global()
        idle_events = [e for e in events if e.get("state") == "idle"]
        assert len(idle_events) == 1
        assert idle_events[0]["content"] == "Hello world"
        # Accumulator should be reset
        assert ui._ws_turn_content == []
        assert ui._ws_turn_content_size == 0

    def test_error_broadcast_resets_without_content(self):
        """_broadcast_state('error') should reset accumulator without content in event."""
        ui = _make_ui()
        ui.on_content_token("partial")
        ui._broadcast_state("error")

        events = _drain_global()
        error_events = [e for e in events if e.get("state") == "error"]
        assert len(error_events) == 1
        assert "content" not in error_events[0]
        assert ui._ws_turn_content == []
        assert ui._ws_turn_content_size == 0

    def test_thinking_broadcast_does_not_touch_accumulator(self):
        """_broadcast_state('thinking') should not affect the accumulator."""
        ui = _make_ui()
        ui.on_content_token("in progress")
        ui._broadcast_state("thinking")

        assert ui._ws_turn_content == ["in progress"]
        events = _drain_global()
        thinking_events = [e for e in events if e.get("state") == "thinking"]
        assert len(thinking_events) == 1
        assert "content" not in thinking_events[0]

    def test_multi_round_accumulation(self):
        """Content from multiple streaming rounds accumulates before idle."""
        ui = _make_ui()
        # Round 1
        ui.on_content_token("I'll check ")
        ui.on_content_token("that. ")
        # Round 2 (after tool execution)
        ui.on_content_token("Here's ")
        ui.on_content_token("the result.")
        ui._broadcast_state("idle")

        events = _drain_global()
        idle_events = [e for e in events if e.get("state") == "idle"]
        assert len(idle_events) == 1
        assert idle_events[0]["content"] == "I'll check that. Here's the result."

    def test_empty_content_on_idle_without_tokens(self):
        """idle with no content tokens should include empty content string."""
        ui = _make_ui()
        ui._broadcast_state("idle")

        events = _drain_global()
        idle_events = [e for e in events if e.get("state") == "idle"]
        assert len(idle_events) == 1
        assert idle_events[0]["content"] == ""

    def test_cancellation_preserves_partial_content(self):
        """Partial content accumulated before cancel should appear in idle event."""
        ui = _make_ui()
        ui.on_content_token("I'll ")
        ui.on_content_token("start by...")
        # Cancellation triggers idle broadcast with partial content
        ui._broadcast_state("idle")

        events = _drain_global()
        idle_events = [e for e in events if e.get("state") == "idle"]
        assert len(idle_events) == 1
        assert idle_events[0]["content"] == "I'll start by..."

    def test_consecutive_turns_isolated(self):
        """Content from turn 1 should not leak into turn 2."""
        ui = _make_ui()
        # Turn 1
        ui.on_content_token("first response")
        ui._broadcast_state("idle")
        _drain_global()

        # Turn 2
        ui.on_content_token("second response")
        ui._broadcast_state("idle")

        events = _drain_global()
        idle_events = [e for e in events if e.get("state") == "idle"]
        assert len(idle_events) == 1
        assert idle_events[0]["content"] == "second response"

    def test_content_cap_prevents_unbounded_growth(self):
        """Content exceeding the cap should stop accumulating."""
        # Constant lifted from turnstone.server to turnstone.core.session_ui_base
        # in the rich ws_state payload work so coord enforces the same ceiling.
        from turnstone.core.session_ui_base import _MAX_TURN_CONTENT_CHARS

        ui = _make_ui()
        # Fill to capacity
        chunk = "x" * 1024
        for _ in range(_MAX_TURN_CONTENT_CHARS // 1024 + 10):
            ui.on_content_token(chunk)

        assert ui._ws_turn_content_size <= _MAX_TURN_CONTENT_CHARS + 1024
        ui._broadcast_state("idle")

        events = _drain_global()
        idle_events = [e for e in events if e.get("state") == "idle"]
        assert len(idle_events) == 1
        # Content should be capped, not contain everything
        assert len(idle_events[0]["content"]) <= _MAX_TURN_CONTENT_CHARS + 1024


class TestPendingApprovalDetailGate:
    """The Shape A SSE plumbing carries ``pending_approval_detail`` on the
    ``ws_state`` event so the coord tree UI can render inline approve/deny
    buttons in lockstep with the activity_state transition.  The gate
    (``if self._pending_approval is not None``) keeps the per-broadcast
    serializer cost off the common no-approval-pending path — these tests
    lock both branches down."""

    def test_state_broadcast_omits_field_when_no_approval_pending(self):
        """Common case: no approval pending → field absent from event so the
        per-broadcast verdict-cache deepcopy in
        ``serialize_pending_approval_detail`` never runs.  A regression
        that drops the gate would silently 10x the cost of every state
        broadcast in the steady state."""
        ui = _make_ui()
        assert ui._pending_approval is None
        ui._broadcast_state("running")

        events = _drain_global()
        running_events = [e for e in events if e.get("state") == "running"]
        assert len(running_events) == 1
        assert "pending_approval_detail" not in running_events[0]

    def test_state_broadcast_includes_field_when_approval_pending(self):
        """When an approval is pending the broadcast must carry the rich
        payload — the coord tree UI reads it directly to render inline
        approve/deny buttons.  Without this, a coord browser would have
        to chase a separate ``cluster/ws/live`` fetch on every
        activity_state transition (the load-storm pattern Shape A is
        unwinding)."""
        ui = _make_ui()
        # Mirror the shape ``pause_for_approval`` writes (session_ui_base
        # lines 576-580) — items with call_id + header is the minimum
        # the serializer needs to project.
        ui._pending_approval = {
            "type": "approve_request",
            "items": [
                {
                    "call_id": "c1",
                    "header": "tool x",
                    "func_args": "{}",
                    "intent_summary": "do x",
                    "needs_approval": True,
                }
            ],
            "judge_pending": False,
        }
        ui._broadcast_state("attention")

        events = _drain_global()
        attn = [e for e in events if e.get("state") == "attention"]
        assert len(attn) == 1
        # Field present and structurally sound — the serializer's
        # full shape is covered by tests/test_session_ui_base.py;
        # here we only need to confirm the gate fires and the
        # serializer's output is what lands on the event.
        assert "pending_approval_detail" in attn[0]
        detail = attn[0]["pending_approval_detail"]
        assert detail is not None
        assert detail.get("items")
        assert detail["items"][0]["call_id"] == "c1"

    def test_field_cleared_after_approval_resolves(self):
        """Once ``_pending_approval`` is cleared, subsequent state
        broadcasts must drop the field again — without this, the
        browser would render stale approve/deny buttons until the
        next bulk-poll TTL window expired."""
        ui = _make_ui()
        ui._pending_approval = {
            "type": "approve_request",
            "items": [{"call_id": "c1", "header": "x"}],
            "judge_pending": False,
        }
        ui._broadcast_state("attention")
        _drain_global()  # discard the with-detail event

        ui._pending_approval = None
        ui._broadcast_state("running")
        events = _drain_global()
        running = [e for e in events if e.get("state") == "running"]
        assert len(running) == 1
        assert "pending_approval_detail" not in running[0]
