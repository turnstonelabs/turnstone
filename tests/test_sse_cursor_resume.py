"""Tests for the SSE event-id cursor-resume model.

The fresh-connect fast-forward (issue: completed siblings of a parallel
tool batch render empty until refresh).  ``/history`` returns the
committed snapshot up to a cursor and OMITS the trailing executing
in-flight turn; the client opens its initial SSE with that cursor so the
existing ``replay_ok`` delta replays the in-flight turn whole.

Covers the four seams that decide correctness:
  - ``_resume_cursor_and_trim`` — the cut decision + the resolved-boundary
    cursor (the property that makes out-of-order result saves safe).
  - ``SessionUIBase.can_replay_from`` — the buffer-liveness gate.
  - ``save_message(event_id=)`` round-trip + ``get_max_event_id`` +
    ``_event_id`` reseed on UI construction.
  - the in-flight delta actually flows through ``register_listener_with_replay``
    from a cursor, and the orphan's content is in /history not the snapshot.
"""

from __future__ import annotations

import collections
import os
import tempfile
from typing import TYPE_CHECKING, Any

os.environ.setdefault("TURNSTONE_JWT_SECRET", "x" * 32)

from tests._session_helpers import make_session
from turnstone.core.session_routes import _resume_cursor_and_trim
from turnstone.core.session_ui_base import SessionUIBase
from turnstone.core.storage._sqlite import SQLiteBackend

if TYPE_CHECKING:
    import pytest


class _ConcreteUI(SessionUIBase):
    pass


class _FakeUI:
    """Minimal stand-in exposing only ``can_replay_from`` for the helper."""

    def __init__(self, can_replay: bool = True) -> None:
        self._can = can_replay
        self.seen_cursor: int | None = None

    def can_replay_from(self, cursor: int) -> bool:
        self.seen_cursor = cursor
        return self._can


def _assistant(event_id: int, *call_ids: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "fetching",
        "tool_calls": [
            {"id": c, "function": {"name": "web_fetch", "arguments": "{}"}} for c in call_ids
        ],
        "_event_id": event_id,
    }


def _user(event_id: int | None) -> dict[str, Any]:
    m: dict[str, Any] = {"role": "user", "content": "go"}
    if event_id is not None:
        m["_event_id"] = event_id
    return m


def _tool(call_id: str, event_id: int) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": "result", "_event_id": event_id}


# ---------------------------------------------------------------------------
# _resume_cursor_and_trim — the cut decision + resolved-boundary cursor
# ---------------------------------------------------------------------------


def test_trim_live_executing_orphan_returns_resolved_boundary_cursor() -> None:
    """The bug case: a trailing assistant tool-call turn with no results
    saved, ws executing (not awaiting), buffer replayable → drop the
    orphan turn, cursor = the last resolved message's event_id."""
    msgs = [_user(10), _assistant(12, "A", "B", "C")]
    ui = _FakeUI(can_replay=True)
    trimmed, cursor = _resume_cursor_and_trim(msgs, ui, awaiting_approval=False)
    assert cursor == 10
    assert trimmed == [msgs[0]]  # orphan assistant dropped
    assert ui.seen_cursor == 10  # gate consulted with the resolved boundary


def test_trim_unaffected_by_out_of_order_partial_result_saves() -> None:
    """THE (B') property: while the post-batch loop saves results in input
    order (here B landed first, with a HIGH event_id), the cursor stays
    pinned at the resolved boundary — so a fresh connect mid-save-loop
    never drops the not-yet-saved siblings (they fast-forward via the
    delta).  A max(saved-event_id) cursor would jump to 15 and strip
    A/C; the resolved-boundary cursor does not."""
    msgs = [
        _user(10),
        _assistant(12, "A", "B", "C"),
        _tool("B", 15),  # B saved out of order with a high stamp; A, C pending
    ]
    trimmed, cursor = _resume_cursor_and_trim(msgs, _FakeUI(True), awaiting_approval=False)
    assert cursor == 10  # NOT 15 — the race-saved sibling can't move the cut
    assert trimmed == [msgs[0]]  # whole in-flight turn (assistant + B) dropped


def test_no_trim_when_awaiting_approval() -> None:
    """Awaiting-approval orphans stay on the _pending_approval re-emit
    path — no cursor, full messages."""
    msgs = [_user(10), _assistant(12, "A")]
    trimmed, cursor = _resume_cursor_and_trim(msgs, _FakeUI(True), awaiting_approval=True)
    assert cursor is None
    assert trimmed is msgs


def test_no_trim_when_buffer_cannot_replay() -> None:
    """Reloaded / evicted (buffer can't fast-forward) → keep the orphan in
    /history (#610 block), no cursor."""
    msgs = [_user(10), _assistant(12, "A")]
    trimmed, cursor = _resume_cursor_and_trim(
        msgs, _FakeUI(can_replay=False), awaiting_approval=False
    )
    assert cursor is None
    assert trimmed is msgs


def test_no_trim_when_no_orphan() -> None:
    """Fully-resolved trailing turn → nothing in-flight, no cursor."""
    msgs = [_user(10), _assistant(12, "A", "B"), _tool("A", 13), _tool("B", 14)]
    trimmed, cursor = _resume_cursor_and_trim(msgs, _FakeUI(True), awaiting_approval=False)
    assert cursor is None
    assert trimmed is msgs


def test_no_trim_without_resolved_boundary_event_id() -> None:
    """Orphan present but the resolved prefix carries no event_id (old /
    bulk-saved NULL rows) → no cursor to hand back → snapshot floor."""
    msgs = [_user(None), _assistant(12, "A")]
    trimmed, cursor = _resume_cursor_and_trim(msgs, _FakeUI(True), awaiting_approval=False)
    assert cursor is None
    assert trimmed is msgs


def test_no_trim_when_orphan_is_first_message() -> None:
    """Orphan at index 0 has no resolved boundary before it → no cursor."""
    msgs = [_assistant(12, "A")]
    trimmed, cursor = _resume_cursor_and_trim(msgs, _FakeUI(True), awaiting_approval=False)
    assert cursor is None
    assert trimmed is msgs


def test_trim_cuts_before_orphan_across_prior_resolved_turns() -> None:
    """Multi-turn: prior turn fully resolved, trailing turn in-flight →
    cursor = the prior turn's last event_id; only the trailing turn drops."""
    msgs = [
        _user(10),
        _assistant(12, "X"),
        _tool("X", 14),  # prior turn resolved (event_id 14)
        _assistant(16, "A", "B"),  # trailing in-flight orphan
    ]
    trimmed, cursor = _resume_cursor_and_trim(msgs, _FakeUI(True), awaiting_approval=False)
    assert cursor == 14
    assert trimmed == msgs[:3]


# ---------------------------------------------------------------------------
# SessionUIBase.can_replay_from — buffer-liveness gate
# ---------------------------------------------------------------------------


def test_can_replay_from_empty_buffer_false() -> None:
    ui = _ConcreteUI(ws_id="ws", user_id="u")
    assert ui.can_replay_from(0) is False


def test_can_replay_from_within_buffer_true() -> None:
    ui = _ConcreteUI(ws_id="ws", user_id="u")
    for _ in range(5):
        ui._enqueue({"type": "t"})  # ids 1..5
    assert ui.can_replay_from(2) is True
    assert ui.can_replay_from(0) is True


def test_can_replay_from_no_events_past_cursor_false() -> None:
    ui = _ConcreteUI(ws_id="ws", user_id="u")
    for _ in range(5):
        ui._enqueue({"type": "t"})
    assert ui.can_replay_from(5) is False  # nothing in-flight to fast-forward


def test_can_replay_from_truncated_false() -> None:
    ui = _ConcreteUI(ws_id="ws", user_id="u")
    ui._event_buffer = collections.deque(maxlen=3)
    for _ in range(20):
        ui._enqueue({"type": "t"})  # buffer holds ids 18,19,20
    assert ui.can_replay_from(2) is False  # cursor evicted → would be truncated


# ---------------------------------------------------------------------------
# Operator-context system turn — row event_id == its own SSE event id
# (the metacognition-nudge double-render regression)
# ---------------------------------------------------------------------------


def test_on_system_turn_returns_buffered_event_id() -> None:
    """``on_system_turn`` returns the SSE ``_event_id`` it assigned — the same
    id stamped on the buffered event — so ``_append_system_turn`` persists the
    row with the id matching its own live event."""
    ui = _ConcreteUI(ws_id="ws", user_id="u")
    ui._enqueue({"type": "content"})  # advance the counter
    eid = ui.on_system_turn("ground yourself", "start", None)
    assert eid == ui._event_buffer[-1][0]
    assert ui._event_buffer[-1][1]["type"] == "system_turn"


def test_append_system_turn_stamps_row_with_its_sse_event_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a system turn's persisted row carries the SAME ``event_id``
    as its live ``on_system_turn`` event.  Stamping the row with the pre-emit
    counter left it one below its own event, so an in-flight-orphan
    ``/history`` resume cursor derived from the row re-replayed the live event
    and the operator bubble rendered twice (the metacognition-nudge double)."""
    session = make_session()
    ui = session.ui  # NullUI is a real SessionUIBase → increments _event_id
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "turnstone.core.session.save_message",
        lambda *a, **k: captured.update(event_id=k.get("event_id")),
    )
    ui._enqueue({"type": "content"})  # advance past the prior turn
    session._append_system_turn("start", "ground yourself")
    assert captured["event_id"] == ui._event_buffer[-1][0]
    assert ui._event_buffer[-1][1]["type"] == "system_turn"


# ---------------------------------------------------------------------------
# Storage: event_id round-trip, get_max_event_id, _event_id reseed
# ---------------------------------------------------------------------------


def _backend() -> SQLiteBackend:
    return SQLiteBackend(os.path.join(tempfile.mkdtemp(), "t.db"))


def test_event_id_round_trip_and_null() -> None:
    s = _backend()
    s.save_message("ws1", "assistant", "hi", tool_calls='[{"id":"A"}]', event_id=46)
    s.save_message("ws1", "user", "next")  # no event_id → NULL
    msgs = s.load_messages("ws1", repair=False)
    assert [m.get("_event_id") for m in msgs] == [46, None]


def test_get_max_event_id() -> None:
    s = _backend()
    assert s.get_max_event_id("ws1") is None  # no rows
    s.save_message("ws1", "user", "a", event_id=5)
    s.save_message("ws1", "assistant", "b", event_id=9)
    s.save_message("ws1", "user", "c")  # NULL doesn't lower the max
    assert s.get_max_event_id("ws1") == 9
    assert s.get_max_event_id("other") is None


def test_event_id_seeded_on_ui_construction(monkeypatch: Any) -> None:
    """A rebuilt UI reseeds _event_id from the persisted high-water so the
    cursor space stays monotonic across process restarts."""

    class _Stub:
        def get_max_event_id(self, ws_id: str) -> int | None:
            return 99

    monkeypatch.setattr(
        "turnstone.core.storage._registry.get_storage", lambda: _Stub(), raising=True
    )
    ui = _ConcreteUI(ws_id="ws-reopen", user_id="u")
    assert ui._event_id == 99
    # Next emitted event continues strictly above the seed (no collision).
    ui._enqueue({"type": "t"})
    assert ui._event_buffer[-1][0] == 100


# ---------------------------------------------------------------------------
# The in-flight delta flows from the cursor; orphan content is in /history
# ---------------------------------------------------------------------------


def test_cursor_replays_inflight_discrete_events_not_content() -> None:
    """With cursor = the resolved boundary, register_listener_with_replay
    yields exactly the in-flight turn's events (here: the tool_result the
    fresh connect was missing), and the content tokens that streamed
    BEFORE the assistant committed are <= cursor (carried by /history,
    not re-streamed)."""
    ui = _ConcreteUI(ws_id="ws", user_id="u")
    # prior resolved turn ends at event 10 (the cursor)
    for _ in range(10):
        ui._enqueue({"type": "noise"})
    cursor = ui._event_id  # 10
    # in-flight turn: assistant content streamed + committed, then tools
    ui.on_content_token("Let me fetch")
    ui.on_turn_committed()  # resets inflight buffers BEFORE tools run
    ui._enqueue({"type": "tool_info", "items": [{"call_id": "A"}]})
    ui.on_tool_result("A", "web_fetch", "result-A")
    _lq, replay, status, *_ = ui.register_listener_with_replay(cursor)
    assert status == "replay_ok"
    types = [e.get("type") for e in replay]
    assert "tool_info" in types and "tool_result" in types
    # Snapshot is EMPTY during the tool-execution window (committed reset it),
    # so the orphan's content must come from /history — confirmed here.
    _lq2, snap = ui.register_listener_with_in_progress_snapshot()
    assert snap["content"] == ""
