"""Unit tests for :class:`CoordinatorIdleObserver`.

Drives a fake :class:`SessionManager` that mirrors the real one's
``subscribe_to_state`` / ``get`` contract, plus a fake storage with the
``list_workstreams`` slice the observer queries.
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from turnstone.console.coordinator_idle_observer import CoordinatorIdleObserver
from turnstone.core.nudge_queue import NudgeQueue
from turnstone.core.workstream import WorkstreamKind, WorkstreamState


class _FakeRow:
    """SQLAlchemy-Row-like wrapper exposing ``_mapping``."""

    def __init__(self, **kwargs: Any) -> None:
        self._mapping = kwargs


class _FakeStorage:
    def __init__(self) -> None:
        self.children: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.count_calls: list[dict[str, Any]] = []
        self.list_raises: bool = False
        self.count_raises: bool = False

    def list_workstreams(
        self,
        node_id: str | None = None,
        limit: int = 100,
        *,
        parent_ws_id: str | None = None,
        kind: WorkstreamKind | str | None = None,
        user_id: str | None = None,
    ) -> list[Any]:
        self.list_calls.append(
            {
                "limit": limit,
                "parent_ws_id": parent_ws_id,
                "kind": kind,
                "user_id": user_id,
            }
        )
        if self.list_raises:
            raise RuntimeError("storage forced failure")
        return [_FakeRow(**c) for c in self.children]

    def count_workstreams_by_state(
        self,
        *,
        parent_ws_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, int]:
        self.count_calls.append({"parent_ws_id": parent_ws_id, "user_id": user_id})
        if self.count_raises:
            raise RuntimeError("count forced failure")
        counts: dict[str, int] = {}
        for c in self.children:
            counts[c["state"]] = counts.get(c["state"], 0) + 1
        return counts


class _FakeSession:
    def __init__(self) -> None:
        self._nudge_queue = NudgeQueue()
        self.messages: list[dict[str, Any]] = []
        self._wake_source_tag: str = ""
        self._metacog_state: dict[str, float] = {}
        self._mem_cfg = MagicMock(nudge_cooldown=300)

    def _visible_memory_count(self) -> int:
        return 0


class _FakeWorkstream:
    def __init__(
        self,
        ws_id: str = "ws-coord",
        kind: WorkstreamKind = WorkstreamKind.COORDINATOR,
        user_id: str = "u1",
    ) -> None:
        self.id = ws_id
        self.kind = kind
        self.user_id = user_id
        self.session: _FakeSession | None = _FakeSession()


class _FakeManager:
    def __init__(self) -> None:
        self._workstreams: dict[str, _FakeWorkstream] = {}
        self._subscribers: list[Any] = []
        self._lock = threading.Lock()

    def add_ws(self, ws: _FakeWorkstream) -> None:
        self._workstreams[ws.id] = ws

    def remove_ws(self, ws_id: str) -> None:
        self._workstreams.pop(ws_id, None)

    def get(self, ws_id: str) -> _FakeWorkstream | None:
        return self._workstreams.get(ws_id)

    def subscribe_to_state(self, callback: Any) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe_from_state(self, callback: Any) -> None:
        with self._lock, contextlib.suppress(ValueError):
            self._subscribers.remove(callback)

    def fire_state(self, ws_id: str, state: WorkstreamState) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for cb in subs:
            with contextlib.suppress(Exception):
                cb(ws_id, state)


@pytest.fixture
def coord_setup() -> tuple[_FakeManager, _FakeStorage, _FakeWorkstream]:
    mgr = _FakeManager()
    storage = _FakeStorage()
    ws = _FakeWorkstream()
    mgr.add_ws(ws)
    return mgr, storage, ws


def _add_active_child(storage: _FakeStorage, **overrides: Any) -> None:
    storage.children.append(
        {
            "ws_id": overrides.get("ws_id", "child-1"),
            "name": overrides.get("name", "research"),
            "state": overrides.get("state", "running"),
        }
    )


class TestEnqueueOnIdle:
    def test_idle_with_active_children_enqueues(self, coord_setup):
        mgr, storage, ws = coord_setup
        _add_active_child(storage, ws_id="child-a", state="running")
        _add_active_child(storage, ws_id="child-b", state="thinking")
        ws.session.messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "ok"},
        ]

        observer = CoordinatorIdleObserver(mgr, storage)
        observer.start()
        mgr.fire_state(ws.id, WorkstreamState.IDLE)

        snap = ws.session._nudge_queue.pending("any")
        assert len(snap) == 1
        nudge_type, text = snap[0]
        assert nudge_type == "idle_children"
        assert "child-a" in text
        assert "child-b" in text

    def test_idle_with_no_active_children_no_enqueue(self, coord_setup):
        mgr, storage, ws = coord_setup
        # storage.children is empty
        observer = CoordinatorIdleObserver(mgr, storage)
        observer.start()
        mgr.fire_state(ws.id, WorkstreamState.IDLE)
        assert len(ws.session._nudge_queue) == 0

    def test_idle_only_idle_state_children_no_enqueue(self, coord_setup):
        mgr, storage, ws = coord_setup
        # All children "idle" — terminal-from-coord-perspective; not active.
        _add_active_child(storage, state="idle")
        _add_active_child(storage, state="closed")
        _add_active_child(storage, state="error")
        # ≥2 messages so should_nudge's message_count > 1 gate clears.
        ws.session.messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "ok"},
        ]
        observer = CoordinatorIdleObserver(mgr, storage)
        observer.start()
        mgr.fire_state(ws.id, WorkstreamState.IDLE)
        assert len(ws.session._nudge_queue) == 0

    def test_non_idle_state_no_enqueue(self, coord_setup):
        mgr, storage, ws = coord_setup
        _add_active_child(storage)
        observer = CoordinatorIdleObserver(mgr, storage)
        observer.start()
        for state in (
            WorkstreamState.RUNNING,
            WorkstreamState.THINKING,
            WorkstreamState.ATTENTION,
            WorkstreamState.ERROR,
        ):
            mgr.fire_state(ws.id, state)
        assert len(ws.session._nudge_queue) == 0


class TestKindFilter:
    def test_interactive_workstream_skipped(self):
        mgr = _FakeManager()
        storage = _FakeStorage()
        _add_active_child(storage)
        ws = _FakeWorkstream(kind=WorkstreamKind.INTERACTIVE)
        mgr.add_ws(ws)
        observer = CoordinatorIdleObserver(mgr, storage)
        observer.start()
        mgr.fire_state(ws.id, WorkstreamState.IDLE)
        # Observer ignored the non-coord workstream entirely.
        assert len(ws.session._nudge_queue) == 0
        # Storage was NOT queried — kind check happens before list_workstreams.
        assert storage.list_calls == []


class TestWaitForWorkstreamSkip:
    def test_skips_when_last_assistant_used_wait(self, coord_setup):
        mgr, storage, ws = coord_setup
        _add_active_child(storage)
        ws.session.messages = [
            {"role": "user", "content": "kick off"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {"name": "wait_for_workstream", "arguments": "{}"},
                    }
                ],
            },
        ]
        observer = CoordinatorIdleObserver(mgr, storage)
        observer.start()
        mgr.fire_state(ws.id, WorkstreamState.IDLE)
        # Don't pile on — model is already using the right tool.
        assert len(ws.session._nudge_queue) == 0

    def test_fires_when_last_assistant_used_different_tool(self, coord_setup):
        mgr, storage, ws = coord_setup
        _add_active_child(storage)
        ws.session.messages = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call-1", "function": {"name": "spawn_workstream", "arguments": "{}"}}
                ],
            },
        ]
        observer = CoordinatorIdleObserver(mgr, storage)
        observer.start()
        mgr.fire_state(ws.id, WorkstreamState.IDLE)
        assert len(ws.session._nudge_queue) == 1


class TestHardCap:
    def test_hard_cap_blocks_after_n_fires(self, coord_setup):
        mgr, storage, ws = coord_setup
        _add_active_child(storage)
        # ≥2 messages so should_nudge's message_count > 1 gate clears.
        ws.session.messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "ok"},
        ]
        observer = CoordinatorIdleObserver(mgr, storage)
        observer.start()

        # Bypass cooldown for this test: each call burns a per-type slot
        # in ``_metacog_state`` so we need to clear it between fires.
        for _ in range(3):
            ws.session._metacog_state.clear()
            mgr.fire_state(ws.id, WorkstreamState.IDLE)

        # Cap = 3 fires.  Even with cooldown bypassed, the 4th doesn't fire.
        ws.session._metacog_state.clear()
        mgr.fire_state(ws.id, WorkstreamState.IDLE)
        # We enqueued 3 entries total; cap blocked the 4th.
        snap = ws.session._nudge_queue.pending("any")
        assert len(snap) == 3

    def test_cap_resets_when_state_leaves_idle_without_wake(self, coord_setup):
        mgr, storage, ws = coord_setup
        _add_active_child(storage)
        # ≥2 messages so should_nudge's message_count > 1 gate clears.
        ws.session.messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "ok"},
        ]
        observer = CoordinatorIdleObserver(mgr, storage)
        observer.start()

        # Burn the cap.
        for _ in range(3):
            ws.session._metacog_state.clear()
            mgr.fire_state(ws.id, WorkstreamState.IDLE)
        assert len(ws.session._nudge_queue.pending("any")) == 3

        # Drain the queue (simulate the watcher delivering them).
        ws.session._nudge_queue.drain({"any"})

        # Real (non-wake) leave-IDLE: tag is empty.  Cap resets.
        ws.session._wake_source_tag = ""
        mgr.fire_state(ws.id, WorkstreamState.RUNNING)

        # New IDLE — cap is fresh, fires again.
        ws.session._metacog_state.clear()
        mgr.fire_state(ws.id, WorkstreamState.IDLE)
        assert len(ws.session._nudge_queue.pending("any")) == 1

    def test_cap_does_not_reset_during_wake_driven_exit(self, coord_setup):
        mgr, storage, ws = coord_setup
        _add_active_child(storage)
        # ≥2 messages so should_nudge's message_count > 1 gate clears.
        ws.session.messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "ok"},
        ]
        observer = CoordinatorIdleObserver(mgr, storage)
        observer.start()

        # Burn the cap.
        for _ in range(3):
            ws.session._metacog_state.clear()
            mgr.fire_state(ws.id, WorkstreamState.IDLE)
        ws.session._nudge_queue.drain({"any"})

        # Wake-driven leave-IDLE: tag is set during the wake send.
        ws.session._wake_source_tag = "system_nudge"
        mgr.fire_state(ws.id, WorkstreamState.RUNNING)
        ws.session._wake_source_tag = ""  # tag cleared at end of wake send

        # Cap should NOT have reset — re-IDLE shouldn't fire.
        ws.session._metacog_state.clear()
        mgr.fire_state(ws.id, WorkstreamState.IDLE)
        assert len(ws.session._nudge_queue.pending("any")) == 0


class TestCooldown:
    def test_cooldown_blocks_within_window(self, coord_setup):
        mgr, storage, ws = coord_setup
        _add_active_child(storage)
        # ≥2 messages so should_nudge's message_count > 1 gate clears.
        ws.session.messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "ok"},
        ]
        observer = CoordinatorIdleObserver(mgr, storage)
        observer.start()

        mgr.fire_state(ws.id, WorkstreamState.IDLE)
        assert len(ws.session._nudge_queue.pending("any")) == 1

        # Drain so the queue isn't the gate.
        ws.session._nudge_queue.drain({"any"})

        # Second fire within the cooldown window → should_nudge returns False.
        mgr.fire_state(ws.id, WorkstreamState.IDLE)
        assert len(ws.session._nudge_queue.pending("any")) == 0


class TestStorageFailure:
    def test_storage_exception_is_swallowed(self, coord_setup):
        mgr, storage, ws = coord_setup
        # ≥2 messages so should_nudge's message_count > 1 gate clears.
        ws.session.messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "ok"},
        ]
        storage.list_raises = True
        observer = CoordinatorIdleObserver(mgr, storage)
        observer.start()
        # Must not raise / propagate.
        mgr.fire_state(ws.id, WorkstreamState.IDLE)
        assert len(ws.session._nudge_queue) == 0


class TestValidUntilPredicate:
    def test_predicate_drops_when_children_finish_before_drain(self, coord_setup):
        mgr, storage, ws = coord_setup
        _add_active_child(storage, ws_id="child-a", state="running")
        # ≥2 messages so should_nudge's message_count > 1 gate clears.
        ws.session.messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "ok"},
        ]
        observer = CoordinatorIdleObserver(mgr, storage)
        observer.start()
        mgr.fire_state(ws.id, WorkstreamState.IDLE)
        assert len(ws.session._nudge_queue) == 1

        # Children now complete (storage shows none active).
        storage.children.clear()

        # Drain at the user seam — predicate re-queries, finds 0 active,
        # drops the entry without delivering.
        from turnstone.core.nudge_queue import USER_DRAIN

        delivered = ws.session._nudge_queue.drain(USER_DRAIN)
        assert delivered == []
        assert len(ws.session._nudge_queue) == 0

    def test_predicate_delivers_when_children_still_active(self, coord_setup):
        mgr, storage, ws = coord_setup
        _add_active_child(storage, ws_id="child-a", state="running")
        # ≥2 messages so should_nudge's message_count > 1 gate clears.
        ws.session.messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "ok"},
        ]
        observer = CoordinatorIdleObserver(mgr, storage)
        observer.start()
        mgr.fire_state(ws.id, WorkstreamState.IDLE)

        # Children still active → predicate returns True → entry delivers.
        from turnstone.core.nudge_queue import USER_DRAIN

        delivered = ws.session._nudge_queue.drain(USER_DRAIN)
        assert len(delivered) == 1
        assert delivered[0][0] == "idle_children"

    def test_predicate_drops_on_storage_failure(self, coord_setup):
        mgr, storage, ws = coord_setup
        _add_active_child(storage)
        # ≥2 messages so should_nudge's message_count > 1 gate clears.
        ws.session.messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "ok"},
        ]
        observer = CoordinatorIdleObserver(mgr, storage)
        observer.start()
        mgr.fire_state(ws.id, WorkstreamState.IDLE)

        # Storage failure at drain time.  Predicate treats raises as
        # "no longer valid" (drop) — see NudgeQueue.drain's predicate
        # exception handling.
        storage.count_raises = True

        from turnstone.core.nudge_queue import USER_DRAIN

        delivered = ws.session._nudge_queue.drain(USER_DRAIN)
        assert delivered == []


class TestLifecycle:
    def test_start_idempotent(self, coord_setup):
        mgr, storage, ws = coord_setup
        _add_active_child(storage)
        # ≥2 messages so should_nudge's message_count > 1 gate clears.
        ws.session.messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "ok"},
        ]
        observer = CoordinatorIdleObserver(mgr, storage)
        observer.start()
        observer.start()  # no-op
        mgr.fire_state(ws.id, WorkstreamState.IDLE)
        # Double-subscribe would have produced 2 entries.
        assert len(ws.session._nudge_queue.pending("any")) == 1

    def test_shutdown_unsubscribes(self, coord_setup):
        mgr, storage, ws = coord_setup
        _add_active_child(storage)
        # ≥2 messages so should_nudge's message_count > 1 gate clears.
        ws.session.messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "ok"},
        ]
        observer = CoordinatorIdleObserver(mgr, storage)
        observer.start()
        observer.shutdown()
        mgr.fire_state(ws.id, WorkstreamState.IDLE)
        assert len(ws.session._nudge_queue) == 0

    def test_shutdown_idempotent(self, coord_setup):
        mgr, _storage, _ws = coord_setup
        observer = CoordinatorIdleObserver(mgr, _storage)
        observer.start()
        observer.shutdown()
        observer.shutdown()  # no error
