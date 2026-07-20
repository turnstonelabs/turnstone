"""Per-call_id emit-time batching of ``tool_output_chunk`` (SSE fix 3).

Line-chatty tools under the 4-wide pool were the second event-storm
source (one SSE event per stdout LINE, per concurrent tool) — and each
line's ``_enqueue`` also force-flushed the pending token batch, so chunk
traffic defeated token batching too (the amplifier).
:meth:`SessionUIBase.on_tool_output_chunk` now buffers per call_id and
flushes ONE concatenated event per window/size, bypassing ``_enqueue``.

The conditions pinned here map to the dataflow rulings:

- **Per-call keying.** Up to four concurrent tools stream at once; a
  batch must never interleave text across call_ids.
- **Terminal ordering (load-bearing).** The client REMOVES the
  streaming ``<pre>`` when it renders ``tool_result``, so a call's
  trailing chunks must precede its result on the wire —
  ``on_tool_result`` flushes+closes the call's stream first.
- **Closed-call discard (leaked-drain ruling).** A chunk arriving after
  the call's terminal is a drain thread past its join timeout; it is
  discarded, never mispainted below the already-rendered result.
- **Teardown backstops.** ``stream_end`` / the idle-error snapshot /
  turn commit / ``on_error`` flush all pending chunk batches (chunks
  bypass ``_enqueue``, so its chokepoint flush cannot be their
  backstop); ``on_turn_start`` DISCARDS stale residue and resets the
  closed-call ledger (call_ids never span turns).
- **Ring/reconnect contract unchanged.** A batch is one ordinary ring
  entry (fresh ``_event_id``), carries no ``_seq`` (chunks never feed
  the snapshot floor), and concatenation preserves the byte stream
  (chunks are whole lines).
"""

from __future__ import annotations

import pathlib
import queue
from typing import Any

import pytest

from turnstone.core.session_ui_base import SessionUIBase

# ---------------------------------------------------------------------------
# Helpers (mirrors test_sse_token_batching.py)
# ---------------------------------------------------------------------------


class _ConcreteUI(SessionUIBase):
    """Minimal concrete subclass for direct UI tests."""


def _make_ui(ws_id: str = "ws-chunk") -> _ConcreteUI:
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
    """Make the shared batch window effectively infinite so tests control
    flush points explicitly (terminal / backstops / size cap)."""
    monkeypatch.setattr("turnstone.core.session_ui_base._TOKEN_BATCH_WINDOW_SECS", 60.0)


# ---------------------------------------------------------------------------
# Coalescing shape
# ---------------------------------------------------------------------------


def test_fast_chunks_coalesce_and_result_is_the_terminal_flush(wide_window: None) -> None:
    """First line flushes immediately (time-to-first-output protection);
    the rest coalesce until the call's ``tool_result``, which delivers
    the batch BEFORE the result event (the client removes the streaming
    <pre> at the result render — trailing chunks must precede it)."""
    ui = _make_ui()
    lq = ui._register_listener()
    for i in range(5):
        ui.on_tool_output_chunk("call-1", f"l{i}\n")
    ui.on_tool_result("call-1", "bash", "full output")
    events = _drain(lq)
    types = [ev["type"] for ev in events]
    assert types == ["tool_output_chunk", "tool_output_chunk", "tool_result"]
    assert events[0]["chunk"] == "l0\n"
    assert events[1]["chunk"] == "l1\nl2\nl3\nl4\n", (
        "concatenation must preserve the exact byte stream (whole lines)"
    )
    assert events[1]["call_id"] == "call-1"
    # One fresh ring id per batch; chunks carry NO _seq (they never feed
    # the snapshot floor, so the live-drain dedup must not drop them).
    for ev in events:
        assert isinstance(ev["_event_id"], int)
        assert "_seq" not in ev


def test_concurrent_calls_batch_independently(wide_window: None) -> None:
    """Interleaved lines from two concurrent tools must never share a
    batch — per-call keying is the whole point of the dict."""
    ui = _make_ui()
    lq = ui._register_listener()
    ui.on_tool_output_chunk("call-a", "a0\n")  # immediate (TTFO)
    ui.on_tool_output_chunk("call-b", "b0\n")  # immediate (TTFO)
    ui.on_tool_output_chunk("call-a", "a1\n")  # pends on a
    ui.on_tool_output_chunk("call-b", "b1\n")  # pends on b
    ui.on_tool_output_chunk("call-a", "a2\n")  # pends on a
    ui.on_tool_result("call-a", "bash", "out-a")
    ui.on_tool_result("call-b", "bash", "out-b")
    events = _drain(lq)
    a_chunks = [
        ev["chunk"]
        for ev in events
        if ev["type"] == "tool_output_chunk" and ev["call_id"] == "call-a"
    ]
    b_chunks = [
        ev["chunk"]
        for ev in events
        if ev["type"] == "tool_output_chunk" and ev["call_id"] == "call-b"
    ]
    assert a_chunks == ["a0\n", "a1\na2\n"]
    assert b_chunks == ["b0\n", "b1\n"]
    # Each call's trailing batch precedes its own result.
    order = [(ev["type"], ev.get("call_id")) for ev in events]
    assert order.index(("tool_output_chunk", "call-a")) < order.index(("tool_result", "call-a"))
    assert order.index(("tool_output_chunk", "call-b")) < order.index(("tool_result", "call-b"))


def test_size_cap_flushes_mid_stream(wide_window: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The size cap bounds worst-case batch size (client repaint cost)
    independent of rate — same check-before-append overshoot semantics
    as the token batcher."""
    monkeypatch.setattr("turnstone.core.session_ui_base._TOKEN_BATCH_MAX_CHARS", 8)
    ui = _make_ui()
    lq = ui._register_listener()
    ui.on_tool_output_chunk("c", "0123\n")  # immediate first flush
    ui.on_tool_output_chunk("c", "45\n")  # pends (3 chars < 8)
    ui.on_tool_output_chunk("c", "6789a\n")  # 3+6=9 >= 8 -> flush
    events = _drain(lq)
    assert [ev["chunk"] for ev in events] == ["0123\n", "45\n6789a\n"]


# ---------------------------------------------------------------------------
# Terminal close + leaked-drain discard
# ---------------------------------------------------------------------------


def test_result_finishes_the_stream_and_drops_the_entry(wide_window: None) -> None:
    """``tool_result`` is the terminal flush: trailing chunks precede it
    on the wire and the per-call entry (window clock included) is
    dropped.  A chunk arriving AFTER the terminal — only reachable
    through the producer gate's one-line race, since ``_exec_bash``'s
    ``emit_done`` stops the leaked drain thread at the source — simply
    re-buffers and emits, identical to pre-batching behaviour for the
    same line.  (The UI deliberately keeps NO closed-call ledger: keyed
    on call_id it would either discard a reusing provider's NEW stream
    or silently re-open under per-turn resets / LRU churn — both found
    in review.)"""
    ui = _make_ui()
    lq = ui._register_listener()
    ui.on_tool_output_chunk("call-1", "live\n")
    ui.on_tool_result("call-1", "bash", "out")
    assert "call-1" not in ui._pending_chunks, "terminal must drop the entry"
    ui.on_tool_output_chunk("call-1", "raced\n")  # gate-race survivor
    events = _drain(lq)
    chunks = [ev["chunk"] for ev in events if ev["type"] == "tool_output_chunk"]
    # The racy line emits immediately (fresh entry, first-line-immediate)
    # rather than stranding — pre-batching-equivalent, not silent loss.
    assert chunks == ["live\n", "raced\n"]
    types = [ev["type"] for ev in events]
    assert types.index("tool_result") > types.index("tool_output_chunk"), (
        "trailing chunks must precede the call's own result on the wire"
    )


def test_turn_start_discards_stale_chunk_residue(wide_window: None) -> None:
    """``on_turn_start`` is the stale-crash residue path: pending chunk
    text from a dead ``send()`` is DISCARDED (mirror of the token
    discard — never enqueued, never ring-buffered, so discarding is
    consistent everywhere).  A provider REUSING a finished call's id in
    a later turn streams normally: the UI keeps no per-call_id closed
    state across the terminal (the leaked-drain gate lives in the
    producer's execution closure, which id reuse can't confuse)."""
    ui = _make_ui()
    lq = ui._register_listener()
    ui.on_tool_output_chunk("call-1", "first\n")  # immediate
    ui.on_tool_output_chunk("call-1", "stale\n")  # pends
    ui.on_tool_result("call-2", "bash", "out")  # finishes call-2
    ui.on_turn_start()
    ui.on_tool_output_chunk("call-2", "fresh\n")  # id reuse -> streams normally
    events = _drain(lq)
    chunks = [(ev["call_id"], ev["chunk"]) for ev in events if ev["type"] == "tool_output_chunk"]
    assert ("call-1", "stale\n") not in chunks, "stale residue must be discarded, never emitted"
    assert ("call-2", "fresh\n") in chunks, "a reused call_id must stream normally next turn"


# ---------------------------------------------------------------------------
# Teardown backstops
# ---------------------------------------------------------------------------


def test_stream_end_flushes_pending_chunks_first(wide_window: None) -> None:
    ui = _make_ui()
    lq = ui._register_listener()
    ui.on_tool_output_chunk("c", "one\n")  # immediate
    ui.on_tool_output_chunk("c", "tail\n")  # pends
    ui.on_stream_end()
    events = _drain(lq)
    types = [ev["type"] for ev in events]
    assert types == ["tool_output_chunk", "tool_output_chunk", "stream_end"]
    assert events[1]["chunk"] == "tail\n"


def test_idle_snapshot_chokepoint_flushes_pending_chunks(wide_window: None) -> None:
    """``snapshot_and_consume_state_payload`` is the cancel/error
    chokepoint — a batch stranded in the accumulator would vanish from
    connected panes on paths that skip ``stream_end``."""
    ui = _make_ui()
    lq = ui._register_listener()
    ui.on_tool_output_chunk("c", "one\n")
    ui.on_tool_output_chunk("c", "tail\n")  # pends
    ui.snapshot_and_consume_state_payload("idle")
    events = _drain(lq)
    chunks = [ev["chunk"] for ev in events if ev["type"] == "tool_output_chunk"]
    assert chunks == ["one\n", "tail\n"]


def test_turn_committed_and_error_flush_pending_chunks(wide_window: None) -> None:
    ui = _make_ui()
    lq = ui._register_listener()
    ui.on_tool_output_chunk("c1", "x\n")
    ui.on_tool_output_chunk("c1", "y\n")  # pends
    ui.on_turn_committed()
    ui.on_tool_output_chunk("c2", "p\n")
    ui.on_tool_output_chunk("c2", "q\n")  # pends
    ui.on_error("boom")
    events = _drain(lq)
    chunks = [ev["chunk"] for ev in events if ev["type"] == "tool_output_chunk"]
    assert chunks == ["x\n", "y\n", "p\n", "q\n"]
    # The error teardown delivers the pending batch before the error event.
    types = [ev["type"] for ev in events]
    assert types.index("error") > types.index("tool_output_chunk")


# ---------------------------------------------------------------------------
# Producer-surface pin (dataflow ruling: order-within-key assumes ONE
# producer thread per call_id)
# ---------------------------------------------------------------------------


def test_chunk_producer_surface_is_single_site() -> None:
    """Exactly one production call site feeds the per-call batcher
    (``_exec_bash``'s stdout drain, one thread per call_id) plus the
    CLI's Protocol-level no-op delegation (which never reaches
    ``SessionUIBase``).  Order within a batcher key assumes the
    single-producer topology — a future streaming tool that adds a
    producer must revisit :meth:`_buffer_chunk_locked`'s ordering note
    (and this pin).

    Deliberately a raw line grep (comments excluded): a
    ``.on_tool_output_chunk(`` occurrence inside a docstring or string
    literal would also trip it — over-triggering is acceptable for an
    architectural tripwire whose failure message says exactly what to
    re-examine."""
    pkg = pathlib.Path(__file__).resolve().parents[1] / "turnstone"
    calls: dict[str, int] = {}
    for path in pkg.rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "def on_tool_output_chunk" in stripped:
                continue
            if ".on_tool_output_chunk(" in stripped:
                rel = str(path.relative_to(pkg.parent))
                calls[rel] = calls.get(rel, 0) + 1
    assert calls == {
        "turnstone/core/session.py": 1,  # _exec_bash stdout drain (the producer)
        "turnstone/cli.py": 1,  # WorkstreamTerminalUI super() delegation (no-op sink)
    }, f"chunk producer surface changed: {calls}"
