"""Emit-time micro-batching of content / reasoning tokens (SSE Fix B).

At local-inference rates (500+ tok/s) the per-delta ``_enqueue`` was the
load that overflowed listener queues (silent drops -> corrupted panes).
:meth:`SessionUIBase.on_content_token` / :meth:`on_reasoning_token` now
coalesce fragments over a small window and enqueue ONE event per batch.

The two conditions that make batching safe are pinned here because each
was a verified corruption mode in the design review:

- **Condition 1 — atomic flush.**  The pending accumulator is invisible
  to snapshot readers; the flush appends to the inflight buffers AND
  enqueues the batched event inside one ``_ws_lock`` section, so
  ``snap_seq`` stays a true high-water mark for the snapshot text.  If
  inflight were appended per-token while enqueueing per-batch, a
  snapshot straddling the batch would double-render (the batch arrives
  with ``_seq > snap_seq`` carrying already-snapshotted text; the client
  has no content dedup — its ``content`` case is a blind ``+=``).
- **Condition 2 — every non-token emit flushes first.**  ``stream_end``
  / ``tool_*`` / ``state_change`` bypass the batcher; if one overtook a
  pending batch, the client would reset its streaming refs and the late
  batch would paint into a NEW assistant bubble (the split/duplicate
  look).  The flush lives at the top of ``_enqueue`` itself so every
  emit path — base-class, subclass, and route-level — is covered.

Negative-test discipline: the double-render tests fail if the flush's
inflight-append + enqueue are split across ``_ws_lock`` sections, and
the ordering tests fail if the ``_enqueue`` choke-point flush is
removed — each was reverted-and-verified during development.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

import pytest

import turnstone.core.session_ui_base as suib
from turnstone.core.session_ui_base import SessionUIBase

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ConcreteUI(SessionUIBase):
    """Minimal concrete subclass for direct UI tests."""


def _make_ui(ws_id: str = "ws-batch") -> _ConcreteUI:
    return _ConcreteUI(ws_id=ws_id, user_id="u1")


def _drain(lq: queue.Queue[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while True:
        try:
            out.append(lq.get_nowait())
        except queue.Empty:
            return out


@pytest.fixture
def wide_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the batch window effectively infinite so tests control the
    flush points explicitly (via non-token emits / size cap) and a slow
    CI machine can't turn one expected batch into two."""
    monkeypatch.setattr(suib, "_TOKEN_BATCH_WINDOW_SECS", 60.0)


# ---------------------------------------------------------------------------
# Coalescing shape — one fresh id per batch, first fragment immediate
# ---------------------------------------------------------------------------


def test_fast_tokens_coalesce_into_single_batch_event(wide_window: None) -> None:
    """N tokens inside one window -> the first flushes immediately (the
    time-to-first-token protection), the rest coalesce into ONE enqueued
    event whose text is the concatenation, carrying one fresh
    ``_event_id`` / ``_seq``."""
    ui = _make_ui()
    lq = ui._register_listener()
    for i in range(6):
        ui.on_content_token(f"t{i}")
    ui.on_stream_end()
    events = _drain(lq)
    content = [ev for ev in events if ev["type"] == "content"]
    assert [ev["text"] for ev in content] == ["t0", "t1t2t3t4t5"]
    # One fresh id per batch, and the token-event dedup tag rides it.
    for ev in content:
        assert isinstance(ev["_event_id"], int)
        assert ev["_seq"] == ev["_event_id"]
    # The batch is one ring entry too — ids stay dense (no reserved
    # per-token ids leak into the ring numbering).
    _, replay, status, _, _, _ = ui.register_listener_with_replay(0)
    assert status == "replay_ok"
    assert [ev["_event_id"] for ev in replay] == list(range(1, len(replay) + 1))


def test_zero_window_flushes_every_token_individually(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the window forced to zero every token arrives past the
    window boundary and flushes on its own — batching self-disables
    with no behaviour change vs the pre-batching emit shape."""
    monkeypatch.setattr(suib, "_TOKEN_BATCH_WINDOW_SECS", 0.0)
    ui = _make_ui()
    lq = ui._register_listener()
    for i in range(4):
        ui.on_content_token(f"t{i}")
    events = [ev for ev in _drain(lq) if ev["type"] == "content"]
    assert [ev["text"] for ev in events] == ["t0", "t1", "t2", "t3"]


def test_slow_tokens_flush_individually_at_default_window() -> None:
    """Tokens arriving slower than the real (unpatched) window each
    flush individually — pins the constant's scale: a human-readable
    typewriter stream must not regress to visible 25 ms batching
    artifacts, and a mid-turn stall must not hold tokens hostage."""
    ui = _make_ui()
    lq = ui._register_listener()
    for i in range(3):
        time.sleep(suib._TOKEN_BATCH_WINDOW_SECS + 0.01)
        ui.on_content_token(f"t{i}")
    events = [ev for ev in _drain(lq) if ev["type"] == "content"]
    assert [ev["text"] for ev in events] == ["t0", "t1", "t2"]


def test_batch_size_cap_triggers_flush(wide_window: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pending batch reaching the size cap flushes without waiting for
    the window — bounds worst-case batch size (and client repaint cost)
    at fast rates."""
    monkeypatch.setattr(suib, "_TOKEN_BATCH_MAX_CHARS", 8)
    ui = _make_ui()
    lq = ui._register_listener()
    ui.on_content_token("x")  # immediate first flush
    ui.on_content_token("aaaa")  # pending (4 < 8)
    ui.on_content_token("bbbb")  # 8 >= 8 -> flush
    events = [ev for ev in _drain(lq) if ev["type"] == "content"]
    assert [ev["text"] for ev in events] == ["x", "aaaabbbb"]


# ---------------------------------------------------------------------------
# Condition 1 — snapshot readers never double-render across a batch
# ---------------------------------------------------------------------------


def test_snapshot_mid_batch_sees_only_flushed_text_no_double_render(
    wide_window: None,
) -> None:
    """A snapshot taken between two tokens of a pending batch must
    exclude the pending text (it has no event id yet), and the later
    flush must arrive with ``_seq > snap_seq`` so the client renders
    each character exactly once: snapshot text + post-``snap_seq`` live
    events == the full stream, no overlap."""
    ui = _make_ui()
    ui.on_content_token("aa")  # immediate first flush
    ui.on_content_token("bb")  # pending — invisible to snapshots
    lq, snap = ui.register_listener_with_in_progress_snapshot()
    assert snap["content"] == "aa", (
        "pending batch text leaked into the snapshot — the flush must be "
        "the only writer of the inflight buffers"
    )
    ui.on_stream_end()  # flushes the pending batch, then stream_end
    live = [ev for ev in _drain(lq) if ev["type"] == "content" and ev["_seq"] > snap["seq"]]
    assert "".join(ev["text"] for ev in live) == "bb"
    assert snap["content"] + "".join(ev["text"] for ev in live) == "aabb"
    # The flush wrote the inflight buffer too — the NEXT snapshotter
    # sees the full text (nothing stranded in the accumulator).
    _, snap2 = ui.register_listener_with_in_progress_snapshot()
    assert snap2["content"] == "aabb"


def test_replay_registration_mid_batch_no_double_render(wide_window: None) -> None:
    """Same straddle through the ``Last-Event-ID`` reconnect path: the
    replay slice must not contain the pending batch (not enqueued yet),
    and the post-registration flush lands exactly once in the live
    queue."""
    ui = _make_ui()
    ui.on_content_token("aa")  # immediate flush -> event id 1
    ui.on_content_token("bb")  # pending
    lq, replay, status, _, _, snap = ui.register_listener_with_replay(1)
    assert status == "replay_ok"
    assert replay == [], "pending batch must not appear in the replay slice"
    ui.on_stream_end()
    live = [ev for ev in _drain(lq) if ev["type"] == "content"]
    assert "".join(ev["text"] for ev in live) == "bb"
    # Full-stream integrity for a fresh reconnect afterwards.
    _, replay2, _, _, _, _ = ui.register_listener_with_replay(0)
    assert "".join(ev["text"] for ev in replay2 if ev["type"] == "content") == "aabb"


# ---------------------------------------------------------------------------
# Condition 2 — every non-token emit flushes the pending batch first
# ---------------------------------------------------------------------------


def test_stream_end_flushes_pending_batch_before_itself(wide_window: None) -> None:
    """``stream_end`` resets the client's streaming refs; a batch
    arriving after it would paint into a NEW assistant bubble.  The
    flush must therefore precede ``stream_end`` on the wire (strictly
    smaller event id, earlier queue position)."""
    ui = _make_ui()
    lq = ui._register_listener()
    ui.on_content_token("aa")
    ui.on_content_token("bb")  # pending
    ui.on_stream_end()
    events = _drain(lq)
    types = [ev["type"] for ev in events]
    assert types == ["content", "content", "stream_end"]
    assert events[1]["text"] == "bb"
    assert events[1]["_event_id"] < events[2]["_event_id"]


def test_direct_enqueue_flushes_pending_batch_first(wide_window: None) -> None:
    """The flush lives at the ``_enqueue`` choke point, so even
    route-level / subclass emits (``state_change``, ``cancelled``,
    ``clear_ui``) deliver the pending batch first — not just the
    ``on_*`` helpers."""
    ui = _make_ui()
    lq = ui._register_listener()
    ui.on_content_token("aa")
    ui.on_content_token("bb")  # pending
    ui._enqueue({"type": "state_change", "state": "idle"})
    events = _drain(lq)
    assert [ev["type"] for ev in events] == ["content", "content", "state_change"]
    assert events[1]["text"] == "bb"


def test_tool_and_status_emits_flush_pending_batch(wide_window: None) -> None:
    """Representative non-token ``on_*`` emitters (tool output chunk,
    status) deliver a pending batch before their own event."""
    ui = _make_ui()
    lq = ui._register_listener()
    ui.on_content_token("aa")
    ui.on_content_token("bb")  # pending
    ui.on_tool_output_chunk("call-1", "chunk")
    ui.on_content_token("cc")  # immediate?  No — window is wide and the
    # flush just ran, so this pends; the status emit must deliver it.
    ui.on_status({"prompt_tokens": 1, "completion_tokens": 2}, 1000, "med")
    events = _drain(lq)
    types = [ev["type"] for ev in events]
    assert types == ["content", "content", "tool_output_chunk", "content", "status"]
    assert events[1]["text"] == "bb"
    assert events[3]["text"] == "cc"


def test_reasoning_batches_and_kind_switch_flushes(wide_window: None) -> None:
    """Reasoning batches like content (own accumulator semantics), and a
    kind switch flushes the other kind first so wire order preserves
    arrival order between the two token streams."""
    ui = _make_ui()
    lq = ui._register_listener()
    ui.on_reasoning_token("r0")  # immediate first flush
    ui.on_reasoning_token("r1")  # pending
    ui.on_content_token("c0")  # must flush the reasoning batch first
    ui.on_stream_end()
    events = _drain(lq)
    reasoning = [ev for ev in events if ev["type"] == "reasoning"]
    content = [ev for ev in events if ev["type"] == "content"]
    assert "".join(ev["text"] for ev in reasoning) == "r0r1"
    assert "".join(ev["text"] for ev in content) == "c0"
    assert max(ev["_event_id"] for ev in reasoning) < min(ev["_event_id"] for ev in content)
    # Reasoning landed in ITS inflight buffer, content in its own.
    _, snap = ui.register_listener_with_in_progress_snapshot()
    assert snap["reasoning"] == "r0r1"
    assert snap["content"] == "c0"


# ---------------------------------------------------------------------------
# Buffer-cap and turn-boundary semantics under batching
# ---------------------------------------------------------------------------


def test_inflight_cap_respected_and_stream_continues_past_cap(
    wide_window: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 512 KiB inflight cap applies to the batched append exactly as
    it did per-token: check-before-append (bounded overshoot), and the
    live stream keeps flowing past the cap — the cap bounds the
    snapshot, it is NOT a stop-streaming signal."""
    monkeypatch.setattr(suib, "_MAX_TURN_CONTENT_CHARS", 6)
    ui = _make_ui()
    lq = ui._register_listener()
    ui.on_content_token("aaaa")  # immediate flush; inflight size 4 < 6
    ui.on_content_token("bbbb")  # pending
    ui.on_stream_end()  # flush appends (4 < 6 -> append; size 8)
    ui.on_content_token("cccc")  # immediate flush; 8 >= 6 -> NOT appended
    ui.on_stream_end()
    live = [ev for ev in _drain(lq) if ev["type"] == "content"]
    assert [ev["text"] for ev in live] == ["aaaa", "bbbb", "cccc"], (
        "live stream must continue past the inflight cap"
    )
    _, snap = ui.register_listener_with_in_progress_snapshot()
    assert snap["content"] == "aaaabbbb", (
        "snapshot text is capped (check-before-append overshoot only)"
    )


def test_on_turn_start_discards_stale_pending(wide_window: None) -> None:
    """``on_turn_start`` covers the crashed-prior-``send()`` case; a
    stale pending batch from that crash must be DISCARDED (never
    enqueued), not painted into the new turn's bubble."""
    ui = _make_ui()
    lq = ui._register_listener()
    ui.on_content_token("aa")
    ui.on_content_token("stale")  # pending, then the send crashes
    ui.on_turn_start()
    ui.on_stream_end()
    live = [ev for ev in _drain(lq) if ev["type"] == "content"]
    assert [ev["text"] for ev in live] == ["aa"]
    _, snap = ui.register_listener_with_in_progress_snapshot()
    assert snap["content"] == ""


def test_on_turn_committed_flushes_pending_before_reset(wide_window: None) -> None:
    """``on_turn_committed`` runs after the assistant message committed;
    any pending text is part of that committed message, so it flushes
    (live view + ring stay complete) BEFORE the inflight reset."""
    ui = _make_ui()
    lq = ui._register_listener()
    ui.on_content_token("aa")
    ui.on_content_token("bb")  # pending
    ui.on_turn_committed()
    live = [ev for ev in _drain(lq) if ev["type"] == "content"]
    assert [ev["text"] for ev in live] == ["aa", "bb"]
    _, snap = ui.register_listener_with_in_progress_snapshot()
    assert snap["content"] == "", "inflight reset still runs after the flush"


def test_idle_state_payload_includes_pending_batch(wide_window: None) -> None:
    """``snapshot_and_consume_state_payload('idle')`` is the cancel /
    error chokepoint that drains the turn-content accumulator; a pending
    batch must flush into it first so the dashboard payload carries the
    full turn."""
    ui = _make_ui()
    lq = ui._register_listener()
    ui.on_content_token("aa")
    ui.on_content_token("bb")  # pending
    payload = ui.snapshot_and_consume_state_payload("idle")
    assert payload["content"] == "aabb"
    live = [ev for ev in _drain(lq) if ev["type"] == "content"]
    assert "".join(ev["text"] for ev in live) == "aabb"


# ---------------------------------------------------------------------------
# Concurrency — flush choke point vs a concurrent snapshot reader
# ---------------------------------------------------------------------------


def test_concurrent_snapshots_never_double_render_batched_stream(
    wide_window: None,
) -> None:
    """Hammer test for Condition 1: a writer streams batched tokens
    while a reader repeatedly registers snapshot listeners; for every
    snapshot, snapshot-text + post-``snap_seq`` live events must equal
    the full stream exactly once (no overlap, no gap) — the invariant
    that breaks if the inflight append and the batch enqueue are ever
    split across ``_ws_lock`` sections."""
    ui = _make_ui()
    n = 200
    done = threading.Event()

    def _writer() -> None:
        for i in range(n):
            ui.on_content_token(f"[{i}]")
        ui.on_stream_end()
        done.set()

    results: list[tuple[str, int, queue.Queue[dict[str, Any]]]] = []

    def _reader() -> None:
        while not done.is_set():
            lq, snap = ui.register_listener_with_in_progress_snapshot()
            results.append((snap["content"], snap["seq"], lq))

    w = threading.Thread(target=_writer)
    r = threading.Thread(target=_reader)
    w.start()
    r.start()
    w.join()
    r.join()

    full = "".join(f"[{i}]" for i in range(n))
    for snap_content, snap_seq, lq in results:
        live = [ev for ev in _drain(lq) if ev["type"] == "content" and ev["_seq"] > snap_seq]
        rebuilt = snap_content + "".join(ev["text"] for ev in live)
        assert rebuilt == full, (
            f"client view diverged: snapshot({len(snap_content)} chars) + "
            f"{len(live)} live events != full stream"
        )
