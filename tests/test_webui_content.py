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


class TestPendingApprovalDetailNotPiggybacked:
    """Stage 3 cleanup — ``pending_approval_detail`` is no longer
    piggybacked on ``ws_state`` events. Approval items now arrive via
    bulk fetch when the coord tree's reducer sees the
    ``activity_state="approval"`` transition; verdicts via the explicit
    ``intent_verdict`` event class; resolution via
    ``approval_resolved``. These tests lock the no-piggyback contract
    down so a future regression doesn't silently re-introduce the
    duplicated path."""

    def test_state_broadcast_omits_field_when_no_approval_pending(self):
        ui = _make_ui()
        assert ui._pending_approval is None
        ui._broadcast_state("running")

        events = _drain_global()
        running_events = [e for e in events if e.get("state") == "running"]
        assert len(running_events) == 1
        assert "pending_approval_detail" not in running_events[0]

    def test_state_broadcast_omits_field_even_when_approval_pending(self):
        """The piggyback is gone: even when ``_pending_approval`` is set,
        the state broadcast must NOT carry ``pending_approval_detail``.
        The browser triggers a bulk fetch off the
        ``activity_state="approval"`` transition to get the items."""
        ui = _make_ui()
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
        assert "pending_approval_detail" not in attn[0]

    def test_field_stays_absent_after_approval_resolves(self):
        ui = _make_ui()
        ui._pending_approval = {
            "type": "approve_request",
            "items": [{"call_id": "c1", "header": "x"}],
            "judge_pending": False,
        }
        ui._broadcast_state("attention")
        _drain_global()

        ui._pending_approval = None
        ui._broadcast_state("running")
        events = _drain_global()
        running = [e for e in events if e.get("state") == "running"]
        assert len(running) == 1
        assert "pending_approval_detail" not in running[0]


class TestBroadcastIntentVerdict:
    """Producer-side coverage for ``WebUI._broadcast_intent_verdict``.

    The collector-side test (``test_apply_delta_intent_verdict_*`` in
    test_console.py) covers consumption; this pins the event shape the
    producer puts on the global queue. A field rename or missed key
    here would slip past the consumer test because the consumer reads
    via ``data.get(...)``.
    """

    def test_pushes_intent_verdict_event_to_global_queue(self):
        ui = _make_ui()
        verdict = {
            "call_id": "c1",
            "risk_level": "low",
            "confidence": 0.92,
            "recommendation": "approve",
            "reasoning": "tool reads only",
        }
        ui._broadcast_intent_verdict(verdict)

        events = _drain_global()
        assert len(events) == 1
        ev = events[0]
        assert ev["type"] == "intent_verdict"
        assert ev["ws_id"] == "ws-test"
        assert ev["verdict"] == verdict

    def test_no_op_when_global_queue_unset(self):
        WebUI._global_queue = None
        ui = _make_ui()
        # Doesn't raise.
        ui._broadcast_intent_verdict({"call_id": "c1"})

    def test_queue_full_swallowed(self):
        # Force a tiny queue then fill it so the next put_nowait
        # raises queue.Full — the broadcast must absorb it without
        # propagating (matches _broadcast_state's queue.Full handling).
        WebUI._global_queue = queue.Queue(maxsize=1)
        WebUI._global_queue.put_nowait({"sentinel": True})
        ui = _make_ui()
        # Doesn't raise.
        ui._broadcast_intent_verdict({"call_id": "c1"})


class TestBroadcastApprovalResolved:
    """Producer-side coverage for ``WebUI._broadcast_approval_resolved``."""

    def test_pushes_approval_resolved_event_to_global_queue(self):
        ui = _make_ui()
        ui._broadcast_approval_resolved(True, "lgtm", always=False)

        events = _drain_global()
        assert len(events) == 1
        ev = events[0]
        assert ev["type"] == "approval_resolved"
        assert ev["ws_id"] == "ws-test"
        assert ev["approved"] is True
        assert ev["feedback"] == "lgtm"
        assert ev["always"] is False

    def test_normalises_none_feedback_to_empty_string(self):
        ui = _make_ui()
        ui._broadcast_approval_resolved(False, None)

        events = _drain_global()
        assert events[0]["feedback"] == ""
        assert events[0]["approved"] is False
        assert events[0]["always"] is False

    def test_always_kwarg_propagates(self):
        ui = _make_ui()
        ui._broadcast_approval_resolved(True, "ok", always=True)
        events = _drain_global()
        assert events[0]["always"] is True

    def test_no_op_when_global_queue_unset(self):
        WebUI._global_queue = None
        ui = _make_ui()
        # Doesn't raise.
        ui._broadcast_approval_resolved(True, None)


class TestBroadcastApproveRequest:
    """Producer-side coverage for ``WebUI._broadcast_approve_request`` —
    push path for the initial approval items so a coord parent's tree
    UI can render the inline approve/deny block immediately without
    waiting for a bulk-fetch round-trip."""

    def test_pushes_approve_request_event_to_global_queue(self):
        ui = _make_ui()
        detail = {
            "type": "approve_request",
            "items": [{"call_id": "c1", "header": "tool x"}],
            "judge_pending": True,
        }
        ui._broadcast_approve_request(detail)

        events = _drain_global()
        assert len(events) == 1
        ev = events[0]
        assert ev["type"] == "approve_request"
        assert ev["ws_id"] == "ws-test"
        assert ev["detail"] == detail

    def test_no_op_when_global_queue_unset(self):
        WebUI._global_queue = None
        ui = _make_ui()
        # Doesn't raise.
        ui._broadcast_approve_request({"items": []})
