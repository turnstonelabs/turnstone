"""Tests for CoordinatorAdapter.

Mirrors test_interactive_adapter.py: focuses on the transport contract
(what gets sent to the ClusterCollector) and cleanup_ui behavior
(unblock listener queues, cancel session). The SessionManager-level
tests in test_session_manager.py cover the lifecycle path.
"""

from __future__ import annotations

import queue
import threading
from typing import Any
from unittest.mock import MagicMock

from turnstone.console.coordinator_adapter import CoordinatorAdapter
from turnstone.core.workstream import Workstream, WorkstreamKind, WorkstreamState


class _StubCoordUI:
    """Stub matching the subset of ConsoleCoordinatorUI the adapter touches."""

    def __init__(self) -> None:
        self._approval_event = threading.Event()
        self._approval_result: tuple[bool, str | None] = (True, "initial")
        self._plan_event = threading.Event()
        self._plan_result: str = "accept"
        self._fg_event = threading.Event()
        self._listeners_lock = threading.Lock()
        self._listeners: list[queue.Queue[dict[str, Any]]] = []


class _StubSession:
    def __init__(self) -> None:
        self.cancelled = False
        self.closed = False

    def cancel(self) -> None:
        self.cancelled = True

    def close(self) -> None:
        self.closed = True


def _make_adapter(
    collector: Any = None,
    *,
    ui_factory: Any = None,
    session_factory: Any = None,
) -> tuple[CoordinatorAdapter, MagicMock]:
    collector = collector or MagicMock()
    adapter = CoordinatorAdapter(
        collector=collector,
        ui_factory=ui_factory or (lambda ws: _StubCoordUI()),
        session_factory=session_factory or (lambda *a, **kw: _StubSession()),
    )
    return adapter, collector


def _make_ws(**overrides: Any) -> Workstream:
    ws = Workstream(id="coord-1", name="my-coord")
    ws.kind = WorkstreamKind.COORDINATOR
    ws.user_id = "u1"
    ws.ui = _StubCoordUI()
    ws.session = _StubSession()
    for k, v in overrides.items():
        setattr(ws, k, v)
    return ws


# ---------------------------------------------------------------------------
# Transport — emit_created / emit_state / emit_closed
# ---------------------------------------------------------------------------


def test_emit_created_calls_collector_with_coord_fields() -> None:
    adapter, collector = _make_adapter()
    ws = _make_ws()
    adapter.emit_created(ws)
    collector.emit_console_ws_created.assert_called_once_with(
        "coord-1",
        name="my-coord",
        user_id="u1",
        kind=WorkstreamKind.COORDINATOR.value,
        state=WorkstreamState.IDLE.value,
        parent_ws_id=None,
    )


def test_emit_state_calls_collector_state() -> None:
    adapter, collector = _make_adapter()
    ws = _make_ws()
    adapter.emit_state(ws, WorkstreamState.RUNNING)
    collector.emit_console_ws_state.assert_called_once_with(
        "coord-1", WorkstreamState.RUNNING.value
    )


def test_emit_closed_calls_collector_closed() -> None:
    adapter, collector = _make_adapter()
    adapter.emit_closed("coord-1")
    collector.emit_console_ws_closed.assert_called_once_with("coord-1")


def test_emit_closed_swallows_reason_kwarg() -> None:
    """The console collector doesn't propagate a 'reason' — the console
    frontend's evicted special-case only fires for real-node
    workstreams. Protocol compatibility only."""
    adapter, collector = _make_adapter()
    adapter.emit_closed("coord-1", reason="evicted")
    collector.emit_console_ws_closed.assert_called_once_with("coord-1")


def test_emit_tolerates_collector_exception() -> None:
    collector = MagicMock()
    collector.emit_console_ws_created.side_effect = RuntimeError("collector dead")
    collector.emit_console_ws_state.side_effect = RuntimeError("collector dead")
    collector.emit_console_ws_closed.side_effect = RuntimeError("collector dead")
    adapter, _ = _make_adapter(collector=collector)
    ws = _make_ws()
    # All three must swallow — the session lifecycle must not break
    # because the collector had a transient failure.
    adapter.emit_created(ws)
    adapter.emit_state(ws, WorkstreamState.RUNNING)
    adapter.emit_closed("coord-1")


# ---------------------------------------------------------------------------
# cleanup_ui
# ---------------------------------------------------------------------------


def test_cleanup_ui_unblocks_events_and_broadcasts_to_listeners() -> None:
    adapter, _ = _make_adapter()
    ws = _make_ws()
    ws.ui._approval_event.clear()  # type: ignore[attr-defined]
    ws.ui._plan_event.clear()  # type: ignore[attr-defined]
    ws.ui._fg_event.clear()  # type: ignore[attr-defined]
    lq: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=5)
    ws.ui._listeners.append(lq)  # type: ignore[attr-defined]

    adapter.cleanup_ui(ws)

    assert ws.ui._approval_event.is_set()  # type: ignore[attr-defined]
    assert ws.ui._plan_event.is_set()  # type: ignore[attr-defined]
    assert ws.ui._fg_event.is_set()  # type: ignore[attr-defined]
    assert ws.ui._approval_result == (False, None)  # type: ignore[attr-defined]
    assert ws.ui._plan_result == "reject"  # type: ignore[attr-defined]
    assert lq.get_nowait() == {"type": "ws_closed"}
    assert ws.ui._listeners == []  # type: ignore[attr-defined]
    assert ws.session.cancelled is True  # type: ignore[attr-defined]
    assert ws.session.closed is True  # type: ignore[attr-defined]


def test_cleanup_ui_listener_full_queue_evicts_head() -> None:
    adapter, _ = _make_adapter()
    ws = _make_ws()
    lq: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
    lq.put_nowait({"type": "stale"})
    ws.ui._listeners.append(lq)  # type: ignore[attr-defined]
    adapter.cleanup_ui(ws)
    assert lq.get_nowait() == {"type": "ws_closed"}


def test_cleanup_ui_tolerates_missing_session_and_ui() -> None:
    adapter, _ = _make_adapter()
    ws = _make_ws()
    ws.session = None
    ws.ui = None
    adapter.cleanup_ui(ws)  # no crash


# ---------------------------------------------------------------------------
# Construction passthrough
# ---------------------------------------------------------------------------


def test_build_session_forwards_skill_model_kind_parent() -> None:
    captured: dict[str, Any] = {}

    def _sf(ui: Any, model: str | None, ws_id: str, **kwargs: Any) -> Any:
        captured["ui"] = ui
        captured["model"] = model
        captured["ws_id"] = ws_id
        captured.update(kwargs)
        return _StubSession()

    adapter, _ = _make_adapter(session_factory=_sf)
    ws = _make_ws()
    ws.parent_ws_id = None
    adapter.build_session(ws, skill="coordinator", model="gpt-5")
    assert captured["ui"] is ws.ui
    assert captured["model"] == "gpt-5"
    assert captured["skill"] == "coordinator"
    assert captured["kind"] == WorkstreamKind.COORDINATOR
    assert captured["parent_ws_id"] is None
    # client_type intentionally NOT forwarded — coord session_factory
    # doesn't accept it (fixed as 'console').
    assert "client_type" not in captured


def test_build_ui_delegates_to_ui_factory() -> None:
    captured_ws: list[Workstream] = []

    def _ui_factory(ws: Workstream) -> Any:
        captured_ws.append(ws)
        return _StubCoordUI()

    adapter, _ = _make_adapter(ui_factory=_ui_factory)
    ws = _make_ws()
    result = adapter.build_ui(ws)
    assert captured_ws == [ws]
    assert isinstance(result, _StubCoordUI)
