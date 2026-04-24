"""Tests for InteractiveAdapter.

Focus: the transport contract (what gets pushed onto the global
queue) and cleanup_ui behavior (unblock pending events, broadcast
ws_closed to per-UI listeners, cancel + close session). The
SessionManager-level tests in ``test_session_manager.py`` cover the
adapter-agnostic lifecycle.
"""

from __future__ import annotations

import queue
import threading
from typing import Any
from unittest.mock import MagicMock

from turnstone.core.adapters.interactive_adapter import InteractiveAdapter
from turnstone.core.workstream import Workstream, WorkstreamKind, WorkstreamState


class _StubUI:
    """Stub matching the subset of WebUI the adapter touches."""

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
        self.model = "gpt-5"
        self.model_alias = "default"

    def cancel(self) -> None:
        self.cancelled = True

    def close(self) -> None:
        self.closed = True


def _make_adapter(
    *,
    ui_factory: Any = None,
    session_factory: Any = None,
) -> tuple[InteractiveAdapter, queue.Queue[dict[str, Any]]]:
    gq: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=100)
    adapter = InteractiveAdapter(
        global_queue=gq,
        ui_factory=ui_factory or (lambda ws: _StubUI()),
        session_factory=session_factory or (lambda *a, **kw: _StubSession()),
    )
    return adapter, gq


def _make_ws(**overrides: Any) -> Workstream:
    ws = Workstream(id="ws-1", name="hello")
    ws.kind = WorkstreamKind.INTERACTIVE
    ws.user_id = "u1"
    ws.ui = _StubUI()
    ws.session = _StubSession()
    for k, v in overrides.items():
        setattr(ws, k, v)
    return ws


# ---------------------------------------------------------------------------
# Transport — emit_created / emit_state / emit_closed
# ---------------------------------------------------------------------------


def test_emit_created_is_noop_on_interactive() -> None:
    """The create_workstream HTTP handler fires ws_created after
    attachment validation; the adapter intentionally no-ops so the
    event isn't duplicated (and so failed-attachment creates don't
    surface a phantom create→close pair)."""
    adapter, gq = _make_adapter()
    ws = _make_ws()
    adapter.emit_created(ws)
    assert gq.empty()


def test_emit_rehydrated_is_noop_on_interactive() -> None:
    adapter, gq = _make_adapter()
    ws = _make_ws()
    adapter.emit_rehydrated(ws)
    assert gq.empty()


def test_emit_state_is_noop_on_interactive() -> None:
    """WebUI._broadcast_state emits the full ws_state payload (tokens,
    context_ratio, activity). The adapter no-ops to avoid duplicating
    that with a thinner payload."""
    adapter, gq = _make_adapter()
    ws = _make_ws()
    adapter.emit_state(ws, WorkstreamState.RUNNING)
    assert gq.empty()


def test_emit_closed_defaults_to_closed_reason() -> None:
    adapter, gq = _make_adapter()
    adapter.emit_closed("ws-1", name="my-ws")
    event = gq.get_nowait()
    assert event == {
        "type": "ws_closed",
        "ws_id": "ws-1",
        "reason": "closed",
        "name": "my-ws",
    }


def test_emit_closed_propagates_evicted_reason_and_name() -> None:
    adapter, gq = _make_adapter()
    adapter.emit_closed("ws-1", reason="evicted", name="my-ws")
    event = gq.get_nowait()
    assert event["reason"] == "evicted"
    assert event["name"] == "my-ws"


def test_emit_closed_default_name_is_empty_string() -> None:
    adapter, gq = _make_adapter()
    adapter.emit_closed("ws-1")
    assert gq.get_nowait()["name"] == ""


def test_emit_swallows_queue_full_without_raising() -> None:
    gq: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
    gq.put({"type": "filler"})
    adapter = InteractiveAdapter(
        global_queue=gq,
        ui_factory=lambda ws: _StubUI(),
        session_factory=lambda *a, **kw: _StubSession(),
    )
    adapter.emit_closed("ws-1")  # must not raise even though queue is full
    assert gq.qsize() == 1  # nothing added on a full queue


# ---------------------------------------------------------------------------
# cleanup_ui
# ---------------------------------------------------------------------------


def test_cleanup_ui_unblocks_pending_approval_plan_fg_events() -> None:
    adapter, _ = _make_adapter()
    ws = _make_ws()
    # Simulate pending events
    ws.ui._approval_event.clear()  # type: ignore[attr-defined]
    ws.ui._plan_event.clear()  # type: ignore[attr-defined]
    ws.ui._fg_event.clear()  # type: ignore[attr-defined]

    adapter.cleanup_ui(ws)

    assert ws.ui._approval_event.is_set()  # type: ignore[attr-defined]
    assert ws.ui._plan_event.is_set()  # type: ignore[attr-defined]
    assert ws.ui._fg_event.is_set()  # type: ignore[attr-defined]
    # Approval result flipped to "deny" so the waiter sees a sensible value.
    assert ws.ui._approval_result == (False, None)  # type: ignore[attr-defined]
    assert ws.ui._plan_result == "reject"  # type: ignore[attr-defined]


def test_cleanup_ui_broadcasts_ws_closed_to_listener_queues() -> None:
    adapter, _ = _make_adapter()
    ws = _make_ws()
    lq1: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=10)
    lq2: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=10)
    ws.ui._listeners.extend([lq1, lq2])  # type: ignore[attr-defined]

    adapter.cleanup_ui(ws)

    assert lq1.get_nowait() == {"type": "ws_closed"}
    assert lq2.get_nowait() == {"type": "ws_closed"}
    # Listeners cleared so subsequent events don't fan out to dead generators.
    assert ws.ui._listeners == []  # type: ignore[attr-defined]


def test_cleanup_ui_broadcast_evicts_stale_head_when_listener_queue_full() -> None:
    """Per the old _cleanup_ui fallback: when a listener queue is full,
    drop the oldest event and put ws_closed. Ensures an unresponsive
    browser tab doesn't block close."""
    adapter, _ = _make_adapter()
    ws = _make_ws()
    lq: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
    lq.put_nowait({"type": "stale"})
    ws.ui._listeners.append(lq)  # type: ignore[attr-defined]

    adapter.cleanup_ui(ws)

    assert lq.get_nowait() == {"type": "ws_closed"}
    assert lq.empty()


def test_cleanup_ui_cancels_and_closes_session() -> None:
    adapter, _ = _make_adapter()
    ws = _make_ws()
    adapter.cleanup_ui(ws)
    assert ws.session.cancelled is True  # type: ignore[attr-defined]
    assert ws.session.closed is True  # type: ignore[attr-defined]


def test_cleanup_ui_tolerates_missing_session_and_ui() -> None:
    """A placeholder workstream whose session build failed may arrive
    at cleanup_ui with session=None or ui=None. Must not crash."""
    adapter, _ = _make_adapter()
    ws = _make_ws()
    ws.session = None
    ws.ui = None
    adapter.cleanup_ui(ws)  # no crash


def test_cleanup_ui_tolerates_stub_ui_without_events() -> None:
    """A stub UI missing _approval_event / etc. (test scaffolding
    code) must not crash cleanup_ui — the hasattr guards matter."""
    adapter, _ = _make_adapter()
    ws = _make_ws()
    ws.ui = MagicMock(spec=[])  # empty spec — attribute accesses miss
    adapter.cleanup_ui(ws)  # no crash


# ---------------------------------------------------------------------------
# Construction passthrough
# ---------------------------------------------------------------------------


def test_build_ui_delegates_to_ui_factory() -> None:
    captured_ws: list[Workstream] = []

    def _ui_factory(ws: Workstream) -> Any:
        captured_ws.append(ws)
        return _StubUI()

    adapter, _ = _make_adapter(ui_factory=_ui_factory)
    ws = _make_ws()
    result = adapter.build_ui(ws)
    assert captured_ws == [ws]
    assert isinstance(result, _StubUI)


def test_build_session_forwards_all_kwargs_to_session_factory() -> None:
    captured: dict[str, Any] = {}

    def _sf(ui: Any, model: str | None, ws_id: str, **kwargs: Any) -> Any:
        captured["ui"] = ui
        captured["model"] = model
        captured["ws_id"] = ws_id
        captured.update(kwargs)
        return _StubSession()

    adapter, _ = _make_adapter(session_factory=_sf)
    ws = _make_ws()
    adapter.build_session(
        ws, skill="coder", model="gpt-5", client_type="web", judge_model="gpt-4.1"
    )
    assert captured["ui"] is ws.ui
    assert captured["model"] == "gpt-5"
    assert captured["ws_id"] == ws.id
    assert captured["skill"] == "coder"
    assert captured["client_type"] == "web"
    assert captured["kind"] == WorkstreamKind.INTERACTIVE
    assert captured["parent_ws_id"] is None
    # Kind-specific passthrough — interactive session_factory accepts judge_model.
    assert captured["judge_model"] == "gpt-4.1"
