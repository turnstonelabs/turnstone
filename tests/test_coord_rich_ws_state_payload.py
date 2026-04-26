"""Tests for the rich ``ws_state`` payload on coord (Stage 2 follow-up).

Pre-lift coord's ``ConsoleCoordinatorUI`` populated none of the per-ws
metric fields ``SessionUIBase`` defines (``_ws_prompt_tokens`` /
``_ws_context_ratio`` / ``_ws_current_activity`` / ``_ws_turn_content``)
and the ``coord_adapter.emit_state`` broadcast was state-only —
``tokens=0`` / ``content=""`` were hardcoded into
``collector.emit_console_ws_state``. The lift turned ``on_status`` /
``on_content_token`` / ``on_thinking_*`` / ``on_tool_result`` into
shared bodies on :class:`SessionUIBase` so coord populates the same
fields, then enriched ``coord_adapter.emit_state`` to read them under
lock and pass through to the cluster collector with the rich kwargs.
The cluster dashboard's coord rows now render with the same
tokens / activity / content / context_ratio fields interactive rows do.
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from turnstone.console.coordinator_ui import ConsoleCoordinatorUI
from turnstone.core.session_ui_base import _MAX_TURN_CONTENT_CHARS
from turnstone.core.workstream import WorkstreamState

# ---------------------------------------------------------------------------
# Per-ws metric writes — lifted to SessionUIBase, both subclasses inherit
# ---------------------------------------------------------------------------


def _patch_get_storage(storage: Any):
    return patch("turnstone.core.storage._registry.get_storage", return_value=storage)


def test_coord_on_status_writes_per_ws_metrics() -> None:
    """Pre-lift coord ``on_status`` was an enqueue-only stub — ``_ws_*``
    fields stayed at their initial zero values regardless of token usage.
    Post-lift coord inherits SessionUIBase's body, so token counters and
    context ratio populate just like interactive."""
    ui = ConsoleCoordinatorUI(ws_id="coord-ws", user_id="u1")
    with _patch_get_storage(MagicMock()):
        ui.on_status(
            {"prompt_tokens": 100, "completion_tokens": 50},
            context_window=1000,
            effort="medium",
        )
    assert ui._ws_prompt_tokens == 100
    assert ui._ws_completion_tokens == 50
    assert ui._ws_context_ratio == pytest.approx(0.15)


def test_coord_on_status_persists_usage_event() -> None:
    """Pre-lift coord didn't persist usage_event rows — only WebUI did.
    Lift extends usage tracking to coord so governance dashboards see
    coordinator token consumption alongside interactive."""
    storage = MagicMock()
    ui = ConsoleCoordinatorUI(ws_id="coord-ws", user_id="u1")
    with _patch_get_storage(storage):
        ui.on_status(
            {"prompt_tokens": 7, "completion_tokens": 3, "model": "gpt-x"},
            context_window=200,
            effort="low",
        )
    storage.record_usage_event.assert_called_once()
    kwargs = storage.record_usage_event.call_args.kwargs
    assert kwargs["ws_id"] == "coord-ws"
    assert kwargs["user_id"] == "u1"
    assert kwargs["model"] == "gpt-x"
    assert kwargs["prompt_tokens"] == 7
    assert kwargs["completion_tokens"] == 3


def test_coord_on_content_token_accumulates() -> None:
    """Pre-lift coord ``on_content_token`` only enqueued; lift turns it
    into the same per-ws accumulator WebUI uses so the collector
    broadcast can piggyback the joined turn content on the IDLE
    state-change event."""
    ui = ConsoleCoordinatorUI(ws_id="coord-ws", user_id="u1")
    ui.on_content_token("Hello ")
    ui.on_content_token("world")
    assert ui._ws_turn_content == ["Hello ", "world"]
    assert ui._ws_turn_content_size == len("Hello world")


def test_coord_on_content_token_caps_at_ceiling() -> None:
    """Same content cap interactive enforces — keeps a runaway turn from
    ballooning the cluster broadcast event past listener queue size."""
    ui = ConsoleCoordinatorUI(ws_id="coord-ws", user_id="u1")
    chunk = "x" * 1024
    rounds = (_MAX_TURN_CONTENT_CHARS // 1024) + 50
    for _ in range(rounds):
        ui.on_content_token(chunk)
    # Cap is enforced at the size check; one over-cap chunk still
    # gets in (per the original ``< _MAX``-not-``<=`` semantics) but
    # nothing past that lands.
    assert ui._ws_turn_content_size <= _MAX_TURN_CONTENT_CHARS + 1024


def test_coord_on_thinking_start_sets_activity() -> None:
    """Live activity tracking — coord's dashboard row now flips
    ``activity_state`` to ``"thinking"`` when the model starts."""
    ui = ConsoleCoordinatorUI(ws_id="coord-ws", user_id="u1")
    ui.on_thinking_start()
    assert ui._ws_current_activity == "Thinking…"
    assert ui._ws_activity_state == "thinking"


def test_coord_on_tool_result_clears_activity_and_increments_counters() -> None:
    """Lifted ``on_tool_result`` body increments ``_ws_tool_calls`` /
    ``_ws_turn_tool_calls`` and clears the activity. Pre-lift coord
    just enqueued without touching counters."""
    ui = ConsoleCoordinatorUI(ws_id="coord-ws", user_id="u1")
    ui._ws_current_activity = "⚙ bash: ls -la"
    ui._ws_activity_state = "tool"
    ui.on_tool_result("call-1", "bash", "output")
    assert ui._ws_tool_calls == {"bash": 1}
    assert ui._ws_turn_tool_calls == 1
    assert ui._ws_current_activity == ""
    assert ui._ws_activity_state == ""


# ---------------------------------------------------------------------------
# Snapshot helper — drains turn content on IDLE/ERROR
# ---------------------------------------------------------------------------


def test_snapshot_idle_returns_content_and_clears_accumulator() -> None:
    """IDLE snapshot piggybacks the joined assistant content onto the
    state-change broadcast (so the dashboard renders the turn without
    a storage round-trip), then clears the accumulator for the next
    turn."""
    ui = ConsoleCoordinatorUI(ws_id="coord-ws", user_id="u1")
    ui.on_content_token("Here's ")
    ui.on_content_token("the result.")
    payload = ui.snapshot_and_consume_state_payload("idle")
    assert payload["content"] == "Here's the result."
    assert ui._ws_turn_content == []
    assert ui._ws_turn_content_size == 0


def test_snapshot_error_clears_accumulator_without_emitting_content() -> None:
    """ERROR clears the partial content (the turn's broken; nothing to
    render) but the broadcast itself doesn't carry it — the state
    transition is what matters."""
    ui = ConsoleCoordinatorUI(ws_id="coord-ws", user_id="u1")
    ui.on_content_token("partial...")
    payload = ui.snapshot_and_consume_state_payload("error")
    assert payload["content"] == ""
    assert ui._ws_turn_content == []


def test_snapshot_thinking_does_not_touch_accumulator() -> None:
    """Mid-turn state transitions (running / thinking / attention)
    don't drain the accumulator — only IDLE / ERROR are terminal."""
    ui = ConsoleCoordinatorUI(ws_id="coord-ws", user_id="u1")
    ui.on_content_token("partial mid-turn")
    payload = ui.snapshot_and_consume_state_payload("thinking")
    assert payload["content"] == ""
    # Accumulator preserved.
    assert ui._ws_turn_content == ["partial mid-turn"]


def test_snapshot_carries_token_and_activity_snapshot() -> None:
    """Snapshot reads tokens / context_ratio / activity under one lock
    acquisition so concurrent on_status / on_thinking_start writes
    don't tear the snapshot."""
    ui = ConsoleCoordinatorUI(ws_id="coord-ws", user_id="u1")
    with _patch_get_storage(MagicMock()):
        ui.on_status(
            {"prompt_tokens": 80, "completion_tokens": 20},
            context_window=400,
            effort="medium",
        )
    ui.on_thinking_start()  # sets activity = "Thinking…"
    payload = ui.snapshot_and_consume_state_payload("running")
    assert payload["tokens"] == 100
    assert payload["context_ratio"] == pytest.approx(0.25)
    assert payload["activity"] == "Thinking…"
    assert payload["activity_state"] == "thinking"


# ---------------------------------------------------------------------------
# Coord adapter — passes rich payload to collector
# ---------------------------------------------------------------------------


class _FakeCollectorRecorder:
    """Captures emit_console_ws_state calls so we can assert on the
    rich kwargs the lifted coord_adapter.emit_state passes through."""

    def __init__(self) -> None:
        self.state_calls: list[dict[str, Any]] = []
        self.activity_calls: list[dict[str, Any]] = []

    def emit_console_ws_state(
        self,
        ws_id: str,
        state: str,
        *,
        tokens: int = 0,
        context_ratio: float = 0.0,
        activity: str = "",
        activity_state: str = "",
        content: str = "",
    ) -> None:
        self.state_calls.append(
            {
                "ws_id": ws_id,
                "state": state,
                "tokens": tokens,
                "context_ratio": context_ratio,
                "activity": activity,
                "activity_state": activity_state,
                "content": content,
            }
        )

    def update_console_ws_activity(self, ws_id: str, *, activity: str, activity_state: str) -> None:
        self.activity_calls.append(
            {"ws_id": ws_id, "activity": activity, "activity_state": activity_state}
        )

    def emit_console_ws_created(self, *_a: Any, **_kw: Any) -> None:
        pass

    def emit_console_ws_closed(self, *_a: Any, **_kw: Any) -> None:
        pass

    def emit_console_ws_rename(self, *_a: Any, **_kw: Any) -> None:
        pass

    def ensure_console_pseudo_node(self) -> None:
        pass


def _build_adapter_and_ws(ws_id: str = "coord-ws-1") -> tuple[Any, Any, _FakeCollectorRecorder]:
    """Construct a minimal adapter + Workstream + UI for emit_state tests.

    Skips the full SessionManager wire-up — the adapter's ``emit_state``
    only reads ``ws.id`` and ``ws.ui``, so a real ``Workstream`` with
    a populated ``ConsoleCoordinatorUI`` is enough.
    """
    from turnstone.console.coordinator_adapter import CoordinatorAdapter
    from turnstone.core.workstream import Workstream

    recorder = _FakeCollectorRecorder()
    adapter = CoordinatorAdapter(
        collector=recorder,  # type: ignore[arg-type]
        ui_factory=lambda ws: ConsoleCoordinatorUI(ws_id=ws.id, user_id=ws.user_id),
        session_factory=lambda ws: MagicMock(),
    )
    ws = Workstream(id=ws_id, user_id="u1", name="my-coord")
    ws.ui = ConsoleCoordinatorUI(ws_id=ws_id, user_id="u1")
    return adapter, ws, recorder


def test_coord_adapter_emit_state_passes_rich_payload_to_collector() -> None:
    """Pre-lift coord_adapter.emit_state called collector with state-only;
    post-lift it reads the UI's per-ws snapshot under lock and passes
    tokens / context_ratio / activity / content kwargs through."""
    adapter, ws, recorder = _build_adapter_and_ws()
    with _patch_get_storage(MagicMock()):
        ws.ui.on_status(
            {"prompt_tokens": 60, "completion_tokens": 40},
            context_window=400,
            effort="medium",
        )
    ws.ui.on_content_token("partial answer")
    ws.ui.on_thinking_start()
    adapter.emit_state(ws, WorkstreamState.RUNNING)
    assert len(recorder.state_calls) == 1
    call = recorder.state_calls[0]
    assert call["ws_id"] == ws.id
    assert call["state"] == "running"
    assert call["tokens"] == 100
    assert call["context_ratio"] == pytest.approx(0.25)
    assert call["activity"] == "Thinking…"
    assert call["activity_state"] == "thinking"
    # Mid-turn (RUNNING) — content stays accumulated for the eventual IDLE drain.
    assert call["content"] == ""


def test_coord_adapter_emit_state_idle_drains_content() -> None:
    """IDLE state-change drains the turn-content accumulator and
    piggybacks the joined content on the broadcast — same shape WebUI
    uses on global_queue. Subsequent emit_state must see the
    accumulator cleared."""
    adapter, ws, recorder = _build_adapter_and_ws()
    ws.ui.on_content_token("Here's ")
    ws.ui.on_content_token("the result.")
    adapter.emit_state(ws, WorkstreamState.IDLE)
    assert len(recorder.state_calls) == 1
    assert recorder.state_calls[0]["content"] == "Here's the result."
    # Accumulator drained — next emit_state sees nothing carried over.
    adapter.emit_state(ws, WorkstreamState.IDLE)
    assert recorder.state_calls[1]["content"] == ""


def test_coord_adapter_emit_state_handles_missing_ui_defensively() -> None:
    """``ws.ui`` can be ``None`` mid-eviction; emit_state still
    broadcasts the state-change with empty rich fields so the
    dashboard's coord row still flips state instead of going stale."""
    adapter, ws, recorder = _build_adapter_and_ws()
    ws.ui = None  # simulate teardown race
    adapter.emit_state(ws, WorkstreamState.RUNNING)
    assert len(recorder.state_calls) == 1
    call = recorder.state_calls[0]
    assert call["state"] == "running"
    assert call["tokens"] == 0
    assert call["content"] == ""


# ---------------------------------------------------------------------------
# Coord activity broadcast — UI fans out directly to the collector
# ---------------------------------------------------------------------------


def test_coord_ui_broadcast_activity_calls_collector() -> None:
    """Live activity transitions on coord (between state changes) reach
    the cluster collector via the new ``update_console_ws_activity``
    method. WebUI's analog goes via the global SSE queue; coord's
    UI calls the collector directly since the console isn't a node."""
    recorder = _FakeCollectorRecorder()
    ui = ConsoleCoordinatorUI(ws_id="coord-ws", user_id="u1")
    ConsoleCoordinatorUI._collector = recorder  # type: ignore[assignment]
    try:
        ui.on_thinking_start()  # base impl calls _broadcast_activity
        assert len(recorder.activity_calls) == 1
        call = recorder.activity_calls[0]
        assert call["ws_id"] == "coord-ws"
        assert call["activity"] == "Thinking…"
        assert call["activity_state"] == "thinking"
    finally:
        ConsoleCoordinatorUI._collector = None


def test_coord_ui_broadcast_activity_swallows_collector_failure() -> None:
    """A flaky collector must NOT block the worker thread — activity
    fan-out is observational, the worker keeps running on collector
    failure."""
    recorder = MagicMock()
    recorder.update_console_ws_activity.side_effect = RuntimeError("collector dead")
    ui = ConsoleCoordinatorUI(ws_id="coord-ws", user_id="u1")
    ConsoleCoordinatorUI._collector = recorder
    try:
        ui.on_thinking_start()  # must not raise
        recorder.update_console_ws_activity.assert_called_once()
    finally:
        ConsoleCoordinatorUI._collector = None


def test_coord_ui_broadcast_activity_no_op_when_collector_unset() -> None:
    """Tests / tooling that don't wire a collector shouldn't crash."""
    ui = ConsoleCoordinatorUI(ws_id="coord-ws", user_id="u1")
    ConsoleCoordinatorUI._collector = None
    ui.on_thinking_start()  # must not raise


# ---------------------------------------------------------------------------
# Spawn metrics — coord wires its own hook
# ---------------------------------------------------------------------------


def test_coord_spawn_metrics_increments_messages_and_resets_tool_count() -> None:
    """Coord's ``_coord_spawn_metrics`` mirrors interactive's per-spawn
    counter writes (sans the Prometheus call) so the rich ``ws_state``
    broadcast renders the same per-turn shape."""
    from turnstone.console.server import _coord_spawn_metrics

    ui = ConsoleCoordinatorUI(ws_id="coord-ws", user_id="u1")
    ui._ws_messages = 5
    ui._ws_turn_tool_calls = 3
    _coord_spawn_metrics(MagicMock(), ui)
    assert ui._ws_messages == 6
    assert ui._ws_turn_tool_calls == 0


def test_coord_spawn_metrics_tolerates_ui_without_counters() -> None:
    """A SessionUI subclass without the per-ws counters shouldn't trip
    the hook — defensive guard mirrors the interactive analog."""
    from turnstone.console.server import _coord_spawn_metrics

    class _StubUI:
        pass

    _coord_spawn_metrics(MagicMock(), _StubUI())  # must not raise


# ---------------------------------------------------------------------------
# Snapshot lock — single-acquisition guarantee
# ---------------------------------------------------------------------------


def test_snapshot_acquires_ws_lock_exactly_once() -> None:
    """Snapshot must read all four fields under a single lock acquisition
    so concurrent on_status / on_thinking_start writes can't tear the
    payload."""
    ui = ConsoleCoordinatorUI(ws_id="coord-ws", user_id="u1")
    acquire_count = 0
    inner = ui._ws_lock

    class _CountingLock:
        def __enter__(self) -> None:
            nonlocal acquire_count
            acquire_count += 1
            inner.acquire()

        def __exit__(self, *a: Any) -> None:
            inner.release()

        def acquire(self, *a: Any, **kw: Any) -> bool:
            return inner.acquire(*a, **kw)

        def release(self) -> None:
            inner.release()

    ui._ws_lock = _CountingLock()  # type: ignore[assignment]
    ui.snapshot_and_consume_state_payload("idle")
    assert acquire_count == 1, (
        f"snapshot acquired _ws_lock {acquire_count} times; concurrent "
        "writes could tear the rich payload"
    )


# ---------------------------------------------------------------------------
# Concurrency — snapshot under load
# ---------------------------------------------------------------------------


def test_snapshot_under_concurrent_writes_does_not_crash() -> None:
    """Sanity stress: snapshot reads while on_status / on_thinking_start /
    on_content_token write concurrently. Reader cycles through
    ``("running", "idle", "error")`` so the IDLE/ERROR drain branches
    that mutate ``_ws_turn_content`` actually get exercised against
    concurrent appends — running-only would only hit the read-only
    snapshot path. Each thread's exception (if any) is captured + raised
    on join so a silent worker crash can't slip through as a bare
    deadlock-check pass."""
    ui = ConsoleCoordinatorUI(ws_id="coord-ws", user_id="u1")
    writer_exc: list[BaseException] = []
    reader_exc: list[BaseException] = []

    def _writer() -> None:
        try:
            with _patch_get_storage(MagicMock()):
                for i in range(50):
                    ui.on_status(
                        {"prompt_tokens": i, "completion_tokens": i},
                        context_window=1000,
                        effort="low",
                    )
                    ui.on_content_token(f"chunk-{i}")
                    ui.on_thinking_start()
        except BaseException as exc:  # noqa: BLE001 — surface to main thread
            writer_exc.append(exc)

    def _reader() -> None:
        try:
            states = ("running", "idle", "error")
            for i in range(50):
                ui.snapshot_and_consume_state_payload(states[i % len(states)])
        except BaseException as exc:  # noqa: BLE001 — surface to main thread
            reader_exc.append(exc)

    writer = threading.Thread(target=_writer)
    reader = threading.Thread(target=_reader)
    writer.start()
    reader.start()
    writer.join(timeout=5)
    reader.join(timeout=5)
    assert not writer.is_alive(), "writer thread deadlocked"
    assert not reader.is_alive(), "reader thread deadlocked"
    assert not writer_exc, f"writer raised: {writer_exc[0]!r}"
    assert not reader_exc, f"reader raised: {reader_exc[0]!r}"


def test_coord_on_stream_end_clears_activity() -> None:
    """Lifted ``on_stream_end`` body clears ``_ws_current_activity``
    and ``_ws_activity_state`` so the dashboard's coord row stops
    showing the stale 'Thinking…' indicator after the stream
    finishes. Pre-lift coord just enqueued ``stream_end`` without
    touching activity — this test pins the new clear path so a
    future re-stub doesn't silently re-introduce a stuck activity
    indicator."""
    ui = ConsoleCoordinatorUI(ws_id="coord-ws", user_id="u1")
    ui._ws_current_activity = "Thinking…"
    ui._ws_activity_state = "thinking"
    ui.on_stream_end()
    assert ui._ws_current_activity == ""
    assert ui._ws_activity_state == ""


# ---------------------------------------------------------------------------
# WebUI override semantics still preserved
# ---------------------------------------------------------------------------


def test_webui_on_status_still_records_prometheus_metrics() -> None:
    """The lift moves the per-ws writes to SessionUIBase but WebUI's
    override must still fire ``_metrics.record_*`` (Prometheus on the
    node /metrics endpoint). Regression guard against a future refactor
    accidentally dropping the override."""
    import queue

    from turnstone.server import WebUI

    WebUI._global_queue = queue.Queue()
    try:
        ui = WebUI(ws_id="ws-int", user_id="u1")
        with patch("turnstone.server._metrics") as mock_metrics, _patch_get_storage(MagicMock()):
            ui.on_status(
                {"prompt_tokens": 10, "completion_tokens": 5},
                context_window=200,
                effort="low",
            )
        mock_metrics.record_tokens.assert_called_once_with(10, 5)
        mock_metrics.record_cache_tokens.assert_called_once()
        mock_metrics.record_context_ratio.assert_called_once()
    finally:
        WebUI._global_queue = None


def test_webui_on_tool_result_still_records_prometheus_tool_call() -> None:
    """Same as above for ``on_tool_result``."""
    import queue

    from turnstone.server import WebUI

    WebUI._global_queue = queue.Queue()
    try:
        ui = WebUI(ws_id="ws-int", user_id="u1")
        with patch("turnstone.server._metrics") as mock_metrics:
            ui.on_tool_result("call-1", "bash", "output")
        mock_metrics.record_tool_call.assert_called_once_with("bash")
        # Per-ws counter writes happened too (inherited from base).
        assert ui._ws_tool_calls == {"bash": 1}
        assert ui._ws_turn_tool_calls == 1
    finally:
        WebUI._global_queue = None
