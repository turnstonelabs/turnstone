"""Boundary-crossing integration test for the wake trigger pipeline.

Drives a *real* :class:`SessionManager` + a *real* :class:`ChatSession`
+ a *real* :class:`IdleNudgeWatcher` end-to-end.  The only stub is the
model turn (patched ``_stream_response``, returning a canned
``ModelTurnResult``); every other layer is production code:

* ``SessionManager.set_state`` snapshotting + iterating subscribers
* ``IdleNudgeWatcher._on_state`` peeking the queue
* ``session_worker.send`` atomic-spawn + daemon thread
* ``ChatSession.deliver_wake_nudge_from_queue`` opening / closing
  ``_wake_source_tag``
* ``ChatSession.send`` chat loop short-circuiting metacog detection
* ``_append_user_turn`` stamping ``_source = "system_nudge"``
* ``_emit_pending_user_nudges`` draining ``USER_DRAIN`` into a
  first-class ``system`` turn after the synthetic empty user turn

Per ``feedback_tests_through_boundaries.md``: direct injection tests
that bypass these boundaries silently mask wiring bugs.  This test is
the structural integration gate.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests._helpers import wait_until as _wait_until
from tests._session_helpers import make_result
from turnstone.core import session_worker
from turnstone.core.idle_nudge_watcher import IdleNudgeWatcher, wake_workstream_if_pending
from turnstone.core.metacognition import (
    NUDGE_CHILD_RUNNING_LINE,
    NUDGE_IDLE_TASKS_CHILD_DOOR,
)
from turnstone.core.session import ChatSession
from turnstone.core.session_manager import SessionManager
from turnstone.core.trajectory import dicts_from_turns, turn_from_dict
from turnstone.core.workstream import Workstream, WorkstreamKind, WorkstreamState

# ---------------------------------------------------------------------------
# Minimal fake adapter / UI for this integration test. The session and
# manager share the disposable backend supplied by the storage fixture.
# ---------------------------------------------------------------------------


class _FakeUI:
    """Minimal UI surface for ChatSession + SessionManager.cleanup_ui."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def _unblock(self) -> None:  # SessionManager.close calls this
        pass

    def broadcast_ws_closed(self) -> None:
        pass

    # ChatSession callbacks (no-op for this test)
    def on_turn_start(self) -> None:
        pass

    def on_turn_committed(self) -> None:
        pass

    def on_thinking_start(self) -> None:
        pass

    def on_thinking_end(self) -> None:
        pass

    def on_state_change(self, state: str) -> None:
        self.events.append(("state", state))

    def on_system_turn(self, content: str, source: str, meta: dict | None = None) -> None:
        self.events.append(("system_turn", content, source))

    def on_error(self, message: str) -> None:
        pass

    def on_rename(self, name: str) -> None:
        pass

    def on_output_warning(self, call_id: Any, assessment: Any) -> None:
        pass

    def record_output_assessment(
        self,
        call_id: Any,
        assessment: Any,
        *,
        tier: str = "heuristic",
        reasoning: str = "",
        judge_model: str = "",
        latency_ms: int = 0,
        confidence: float = 0.0,
    ) -> None:
        pass

    def __getattr__(self, name: str) -> Any:
        # Catch-all for any UI hook not enumerated above so the chat
        # loop's ``self.ui.<something>()`` call doesn't blow up.
        return MagicMock()


class _BuildRealSessionAdapter:
    """Adapter that returns a real :class:`ChatSession` instead of a stub.

    Tracks emit_* events the integration test asserts on.  Mirrors the
    ``SessionKindAdapter`` + ``SessionEventEmitter`` Protocol surface
    that production ``WebUI`` / coord adapters expose.
    """

    def __init__(self, storage: Any, kind: WorkstreamKind = WorkstreamKind.INTERACTIVE) -> None:
        self.storage = storage
        self.kind = kind
        self.events: list[str] = []
        self.cleaned_up: list[str] = []

    def emit_created(self, ws: Workstream) -> None:
        self.events.append(f"created:{ws.id}")

    def emit_rehydrated(self, ws: Workstream) -> None:
        self.events.append(f"rehydrated:{ws.id}")

    def emit_state(self, ws: Workstream, state: WorkstreamState) -> None:
        self.events.append(f"state:{ws.id}:{state.value}")

    def emit_closed(self, ws_id: str, *, reason: str = "closed", name: str = "") -> None:
        self.events.append(f"closed:{ws_id}")

    def cleanup_ui(self, ws: Workstream) -> None:
        # Real production cleanup_ui calls ws.session.cancel() + close().
        # We don't need that here — the test exits cleanly via pytest
        # teardown without exercising the cleanup path.  Just record
        # the call for any test that wants to assert on it.
        self.cleaned_up.append(ws.id)

    def build_ui(self, ws: Workstream) -> Any:
        return _FakeUI()

    def build_session(
        self,
        ws: Workstream,
        *,
        skill: Any = None,
        model: Any = None,
        client_type: Any = None,
        **extra: Any,
    ) -> Any:
        # Mirror SessionManager.create's keyword set so config-threading
        # bugs surface here rather than being silently swallowed by
        # **kwargs.  ``model`` flows to the real ChatSession; the rest
        # are accepted but not used by this test.
        client = MagicMock()
        return ChatSession(
            client=client,
            model=str(model) if model else "test-model",
            ui=ws.ui,
            instructions=None,
            temperature=0.5,
            max_tokens=4096,
            tool_timeout=30,
            ws_id=ws.id,
            user_id=ws.user_id,
            kind=self.kind,
            parent_ws_id=ws.parent_ws_id,
            project_id=ws.project_id,
        )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.fixture
def real_mgr(tmp_db: str) -> tuple[SessionManager, _BuildRealSessionAdapter]:
    """Real SessionManager wired to an adapter that builds real ChatSessions.

    No StateWriter is wired so ``set_state`` writes directly to storage
    on the calling thread (we want subscriber dispatch to fire in the
    same thread the test invokes ``set_state`` on).
    """
    from turnstone.core.storage import get_storage

    storage = get_storage()
    adapter = _BuildRealSessionAdapter(storage)
    mgr = SessionManager(
        adapter,
        storage=storage,
        max_active=5,
        event_emitter=adapter,
    )
    return mgr, adapter


def _wait_for_worker_done(ws: Workstream, timeout: float = 5.0) -> None:
    """Poll ``ws._worker_running`` until it clears or timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with ws._lock:
            if not ws._worker_running:
                return
        time.sleep(0.01)
    raise AssertionError(f"worker thread for ws={ws.id[:8]} didn't exit within {timeout}s")


@pytest.mark.parametrize("channel", ["any", "wake"])
def test_idle_event_through_real_session_manager_drives_wake_send(real_mgr, tmp_db, channel):
    """The full wake pipeline, no direct-injection shortcuts.

    Parametrized over both wake-eligible producer channels: ``"any"``
    (watch fires, external events) and ``"wake"`` (the coordinator idle
    nudges' wake-only channel) — the watcher's ``WAKE_PENDING`` gate
    must arm and the wake must deliver for each.

    Boundary path under test:
      enqueue → mgr.set_state(IDLE)
        → SessionManager._state_subscribers iteration (real)
        → IdleNudgeWatcher._on_state (real)
        → session_worker.send (real)
        → real daemon thread
        → ChatSession.deliver_wake_nudge_from_queue (real)
        → ChatSession.send("") (real, with patched LLM stream)
        → _append_user_turn stamps ``_source``
        → _attach_pending_user_reminders drains the wake batch
        → _apply_reminders_for_provider splices envelope onto empty content
    """
    mgr, _adapter = real_mgr
    watcher = IdleNudgeWatcher(mgr)
    watcher.start()

    try:
        ws = mgr.create(user_id="u1", name="wake-int", skill=None)
        assert ws.session is not None
        # Patch the LLM-facing surface so send() runs the chat loop end-to-end
        # without any real provider.  We patch on the just-built ChatSession;
        # the patches are reverted by the `with` block.
        with (
            patch.object(ws.session, "_stream_response", return_value=make_result(content="ok")),
            patch.object(ws.session, "_update_token_table"),
            patch.object(ws.session, "_print_status_line"),
            patch.object(ws.session, "_visible_memory_count", return_value=0),
        ):
            # Suppress the auto-title side-thread; orthogonal to wake.
            ws.session._title_generated = True

            ws.session._nudge_queue.enqueue("idle_children", "your kids", channel)
            assert len(ws.session._nudge_queue) == 1

            # Trigger IDLE.  This runs subscriber dispatch synchronously on
            # the calling thread → IdleNudgeWatcher._on_state → session_worker.send
            # → spawn daemon thread → deliver_wake_nudge_from_queue.
            mgr.set_state(ws.id, WorkstreamState.IDLE)

            # Wait for the daemon thread to clear ``_worker_running`` so the
            # post-conditions are stable.
            _wait_for_worker_done(ws)

        # Queue fully drained by the wake.
        assert len(ws.session._nudge_queue) == 0

        # The synthesized empty user message landed in history with the
        # ``_source`` audit tag; the nudge follows it as a first-class
        # ``system`` turn (no _reminders side-channel).
        msgs = dicts_from_turns(ws.session.messages)
        user_msgs = [m for m in msgs if m.get("role") == "user"]
        assert user_msgs, "expected a synthesized user message from the wake"
        wake_msg = user_msgs[-1]
        assert wake_msg["content"] == ""
        assert wake_msg.get("_source") == "system_nudge"
        assert "_reminders" not in wake_msg
        sys_turns = [m for m in msgs if m.get("role") == "system"]
        assert {
            "role": "system",
            "_source": "idle_children",
            "content": "your kids",
        } in sys_turns

        # The wake-source tag is reset post-send so subsequent activity
        # behaves normally.
        assert ws.session._wake_source_tag == ""
    finally:
        watcher.shutdown()


def test_idle_event_with_empty_queue_does_not_dispatch_wake(real_mgr, tmp_db):
    """Non-empty queue is the gate.  An IDLE event on a workstream with
    nothing queued must NOT call ``session_worker.send``.

    Patches the dispatch primitive directly rather than racing a
    ``time.sleep`` against an erroneous spawn — the question is
    whether the watcher's gate fired, which is a deterministic
    decision the patch captures.
    """
    mgr, _adapter = real_mgr
    watcher = IdleNudgeWatcher(mgr)
    watcher.start()

    try:
        ws = mgr.create(user_id="u1", name="empty-int", skill=None)
        # No enqueue.
        with patch("turnstone.core.session_worker.send") as mock_send:
            mgr.set_state(ws.id, WorkstreamState.IDLE)
            assert mock_send.call_count == 0, "wake must not dispatch for an empty queue"
    finally:
        watcher.shutdown()


def test_watch_fire_on_already_idle_session_drives_wake_send(real_mgr, tmp_db):
    """A watch firing on an ALREADY-idle workstream sees no IDLE
    transition, so :class:`IdleNudgeWatcher` never re-checks the queue —
    the dispatch closure's ``wake_fn`` must drive the wake itself.

    Boundary path under test (only the LLM stream is patched):
      dispatch closure (real, built by ``set_watch_runner``)
        → NudgeQueue.enqueue (real)
        → wake_fn → wake_workstream_if_pending (real)
        → session_worker.send (real) → daemon thread
        → ChatSession.deliver_wake_nudge_from_queue (real)
        → ChatSession.send("") → watch_triggered system turn in history
    """
    mgr, _adapter = real_mgr
    ws = mgr.create(user_id="u1", name="watch-wake-int", skill=None)
    assert ws.session is not None

    captured: dict[str, Any] = {}

    class _StubRunner:
        def set_dispatch_fn(self, ws_id: str, fn: Any) -> None:
            captured["fn"] = fn

    # Production wiring shape (server.py): wake_fn closes over the
    # Workstream OBJECT — not its id — so eviction+restore id drift
    # can't strand the wake.
    ws.session.set_watch_runner(
        _StubRunner(), wake_fn=lambda: wake_workstream_if_pending(ws, trigger="watch-fire")
    )

    with (
        patch.object(ws.session, "_stream_response", return_value=make_result(content="ok")),
        patch.object(ws.session, "_update_token_table"),
        patch.object(ws.session, "_print_status_line"),
        patch.object(ws.session, "_visible_memory_count", return_value=0),
    ):
        ws.session._title_generated = True
        # Idle all along — no worker, and no state transition coming.
        assert ws.state is WorkstreamState.IDLE

        # Simulate the WatchRunner poll thread delivering a fire.
        captured["fn"]({"type": "watch_triggered", "text": "deploy finished: OK"}, "watch-1")

        _wait_for_worker_done(ws)

    # Queue drained by the wake — not parked until the next user message.
    assert len(ws.session._nudge_queue) == 0

    msgs = dicts_from_turns(ws.session.messages)
    user_msgs = [m for m in msgs if m.get("role") == "user"]
    assert user_msgs, "expected a synthesized user message from the wake"
    assert user_msgs[-1]["content"] == ""
    assert user_msgs[-1].get("_source") == "system_nudge"
    sys_turns = [m for m in msgs if m.get("role") == "system"]
    assert any(
        m.get("_source") == "watch_triggered" and "deploy finished: OK" in m.get("content", "")
        for m in sys_turns
    ), f"expected a watch_triggered system turn, got {sys_turns!r}"


@pytest.fixture
def coord_mgr(tmp_db: str) -> tuple[SessionManager, _BuildRealSessionAdapter, Any]:
    """Real coord-side SessionManager with the adapter's kind set to
    COORDINATOR.  Same shape as ``real_mgr`` but for the coord half of
    the lifespan.  No StateWriter wired so subscriber dispatch fires
    synchronously on the test thread.
    """
    from turnstone.core.storage import get_storage

    storage = get_storage()
    adapter = _BuildRealSessionAdapter(storage, kind=WorkstreamKind.COORDINATOR)
    mgr = SessionManager(
        adapter,
        storage=storage,
        max_active=5,
        event_emitter=adapter,
    )
    return mgr, adapter, storage


def test_coord_idle_with_active_children_emits_envelope_via_real_managers(coord_mgr, tmp_db):
    """Full coord-path integration test (matches design doc §7.4).

    Drives the production install order — ``CoordinatorIdleObserver``
    registered FIRST, then ``IdleNudgeWatcher`` — and asserts the
    full chain: observer enqueues on IDLE → watcher peeks → wake
    spawns a worker → ``deliver_wake_nudge_from_queue`` drains and
    runs the synthetic empty-user turn → reminder envelope reaches
    the synthesized user message via the side-channel.

    The boundary-crossing path tested here mirrors what
    ``console/server.py``'s lifespan does at production startup; if
    the install order is ever reversed, this test fails.
    """
    from turnstone.console.coordinator_idle_observer import CoordinatorIdleObserver
    from turnstone.core.workstream import WorkstreamKind as _Kind

    mgr, adapter, storage = coord_mgr
    # Observer FIRST, then watcher.  Same order as
    # ``console/server.py:4435-4443`` — production correctness depends
    # on subscribers firing in registration order on the same IDLE.
    observer = CoordinatorIdleObserver(mgr, storage)
    observer.start()
    watcher = IdleNudgeWatcher(mgr)
    watcher.start()

    try:
        coord = mgr.create(user_id="u1", name="parent-coord", skill=None)
        assert coord.session is not None

        # Two interactive children of the coord, both running.  Use
        # the storage's register_workstream API so the rows match
        # production shape (the observer queries via list_workstreams).
        storage.register_workstream(
            "child-a",
            user_id="u1",
            name="research-pricing",
            kind=_Kind.INTERACTIVE,
            parent_ws_id=coord.id,
            state="running",
        )
        storage.register_workstream(
            "child-b",
            user_id="u1",
            name="draft-rfc",
            kind=_Kind.INTERACTIVE,
            parent_ws_id=coord.id,
            state="thinking",
        )

        # Pretend the coord has already had a real conversation so
        # ``should_nudge``'s message_count > 1 gate passes.
        coord.session.messages.append(turn_from_dict({"role": "user", "content": "spawn 2"}))
        coord.session.messages.append(turn_from_dict({"role": "assistant", "content": "ok"}))

        with (
            patch.object(
                coord.session, "_stream_response", return_value=make_result(content="ack")
            ),
            patch.object(coord.session, "_full_messages", return_value=[]),
            patch.object(coord.session, "_update_token_table"),
            patch.object(coord.session, "_print_status_line"),
            patch.object(coord.session, "_visible_memory_count", return_value=0),
        ):
            coord.session._title_generated = True
            mgr.set_state(coord.id, WorkstreamState.IDLE)
            _wait_for_worker_done(coord)

        # Queue drained — the wake delivered the observer's enqueue.
        assert len(coord.session._nudge_queue) == 0
        # The synthetic empty-user turn landed; the idle_children nudge
        # follows it as a first-class ``system`` turn containing both
        # children.
        msgs = dicts_from_turns(coord.session.messages)
        user_msgs = [m for m in msgs if m.get("role") == "user"]
        wake_msg = user_msgs[-1]
        assert wake_msg["content"] == ""
        assert wake_msg.get("_source") == "system_nudge"
        sys_turns = [m for m in msgs if m.get("role") == "system"]
        idle_turns = [m for m in sys_turns if m["_source"] == "idle_children"]
        assert len(idle_turns) == 1
        text = idle_turns[0]["content"]
        assert "child-a" in text
        assert "child-b" in text
        assert "wait_for_workstream" in text
        # The roster is ids and states only — the children's model-authored
        # names must not be lowered into the system turn.
        assert "research-pricing" not in text
        assert "draft-rfc" not in text
    finally:
        watcher.shutdown()
        observer.shutdown()


def test_coord_idle_with_children_and_open_tasks_delivers_both(coord_mgr, tmp_db):
    """The de-exclusivity ruling crossing every boundary end to end:
    observer → queue → watcher → wake worker → transcript.

    One IDLE event with BOTH conditions true must deliver BOTH system
    turns in one synthetic wake turn, tasks first — the co-delivered
    batch ends on the park instruction.  This is the only end-to-end
    proof of the pair; the unit suite pins it at the queue only.
    """
    import json as _json

    from turnstone.console.coordinator_idle_observer import CoordinatorIdleObserver
    from turnstone.core.workstream import WorkstreamKind as _Kind

    mgr, adapter, storage = coord_mgr
    observer = CoordinatorIdleObserver(mgr, storage)
    observer.start()
    watcher = IdleNudgeWatcher(mgr)
    watcher.start()

    try:
        coord = mgr.create(user_id="u1", name="parent-coord", skill=None)
        assert coord.session is not None

        storage.register_workstream(
            "child-a",
            user_id="u1",
            name="research-pricing",
            kind=_Kind.INTERACTIVE,
            parent_ws_id=coord.id,
            state="running",
        )
        storage.save_workstream_config(
            coord.id,
            {
                "tasks": _json.dumps(
                    {
                        "version": 1,
                        "tasks": [
                            {"id": "tsk_a", "title": "audit auth.py", "status": "in_progress"}
                        ],
                    }
                )
            },
        )

        coord.session.messages.append(turn_from_dict({"role": "user", "content": "spawn"}))
        coord.session.messages.append(turn_from_dict({"role": "assistant", "content": "ok"}))

        with (
            patch.object(
                coord.session, "_stream_response", return_value=make_result(content="ack")
            ),
            patch.object(coord.session, "_full_messages", return_value=[]),
            patch.object(coord.session, "_update_token_table"),
            patch.object(coord.session, "_print_status_line"),
            patch.object(coord.session, "_visible_memory_count", return_value=0),
        ):
            coord.session._title_generated = True
            mgr.set_state(coord.id, WorkstreamState.IDLE)
            _wait_for_worker_done(coord)

        assert len(coord.session._nudge_queue) == 0
        msgs = dicts_from_turns(coord.session.messages)
        sys_sources = [m["_source"] for m in msgs if m.get("role") == "system"]
        assert sys_sources == ["idle_tasks", "idle_children"]
        tasks_text = next(
            m["content"] for m in msgs if m.get("role") == "system" and m["_source"] == "idle_tasks"
        )
        # The counts line is the situation statement; the id block and
        # the populated calls are under it, and no task TEXT rides along.
        assert tasks_text.startswith("You still have 1 open task: 1 in_progress, 0 pending.")
        assert chr(10) + "  - tsk_a (in_progress)" in tasks_text
        assert "task_id='tsk_a'" in tasks_text
        assert "audit auth.py" not in tasks_text
        # The children-aware branch the read selects when a live child
        # row is really in storage — the populated half of the pair
        # whose empty half is the test below.  The fact line renders the
        # OBSERVED state (registered running) with the full id, end to
        # end through the real storage round-trip.
        assert NUDGE_CHILD_RUNNING_LINE.format(ws_id="child-a").removeprefix(chr(10)) in tasks_text
        assert "may still be running" not in tasks_text
        # CO-DELIVERY COHERENCE, end to end: both bodies in one drain now
        # name the same child, from two independent storage reads.  The
        # tasks body populates its blocked-on-a-child branch with the
        # registered ws_id and emits the same ``wait_for_workstream``
        # call shape the roster does — the inconsistency this change
        # removed was one body handing over a runnable call while the
        # other ended in the bare prose "then wait_for_workstream."
        assert "child_ws_id='child-a'" in tasks_text
        assert "wait_for_workstream(ws_ids=['child-a'], mode=\"any\", timeout=120)" in tasks_text
        assert "research-pricing" not in tasks_text
        children_text = next(
            m["content"]
            for m in msgs
            if m.get("role") == "system" and m["_source"] == "idle_children"
        )
        # Ids-and-states roster: the child is named by its ws_id, never by
        # its model-authored name.
        assert "child-a" in children_text
        assert "research-pricing" not in children_text
    finally:
        watcher.shutdown()
        observer.shutdown()


def test_coord_idle_with_open_tasks_and_no_children_omits_children_content(coord_mgr, tmp_db):
    """The childless sibling of the test above, over the same chain:
    no child rows registered, so the enqueue-time live-children read
    answers "none" and the DELIVERED body says nothing about children —
    no fact lines, no blocked-on-a-child branch.

    Asserted on the transcript rather than on the formatter's return,
    because the claim is about what the coordinator is actually told:
    the body's children read queries the storage backend through the
    real ``list_workstreams``, and every boundary between that read
    and the system turn — observer, queue, watcher, wake worker,
    ``deliver_wake_nudge_from_queue`` — is production code.
    """
    import json as _json

    from turnstone.console.coordinator_idle_observer import CoordinatorIdleObserver

    mgr, adapter, storage = coord_mgr
    observer = CoordinatorIdleObserver(mgr, storage)
    observer.start()
    watcher = IdleNudgeWatcher(mgr)
    watcher.start()

    try:
        coord = mgr.create(user_id="u1", name="parent-coord", skill=None)
        assert coord.session is not None

        storage.save_workstream_config(
            coord.id,
            {
                "tasks": _json.dumps(
                    {
                        "version": 1,
                        "tasks": [
                            {"id": "tsk_a", "title": "audit auth.py", "status": "in_progress"}
                        ],
                    }
                )
            },
        )

        coord.session.messages.append(turn_from_dict({"role": "user", "content": "work"}))
        coord.session.messages.append(turn_from_dict({"role": "assistant", "content": "ok"}))

        with (
            patch.object(
                coord.session, "_stream_response", return_value=make_result(content="ack")
            ),
            patch.object(coord.session, "_full_messages", return_value=[]),
            patch.object(coord.session, "_update_token_table"),
            patch.object(coord.session, "_print_status_line"),
            patch.object(coord.session, "_visible_memory_count", return_value=0),
        ):
            coord.session._title_generated = True
            mgr.set_state(coord.id, WorkstreamState.IDLE)
            _wait_for_worker_done(coord)

        assert len(coord.session._nudge_queue) == 0
        msgs = dicts_from_turns(coord.session.messages)
        sys_sources = [m["_source"] for m in msgs if m.get("role") == "system"]
        assert sys_sources == ["idle_tasks"]
        tasks_text = next(
            m["content"] for m in msgs if m.get("role") == "system" and m["_source"] == "idle_tasks"
        )
        assert tasks_text.startswith("You still have 1 open task: 1 in_progress, 0 pending.")
        assert "Child " not in tasks_text
        assert "child" not in tasks_text
        assert NUDGE_IDLE_TASKS_CHILD_DOOR not in tasks_text
        # The nudge is otherwise the shipped one: the conditional drops
        # the fact lines and the blocked-on-a-child branch, not the
        # opener, the id block or the other instructions.
        assert "needs_user" in tasks_text
        # End to end, through the REAL storage round-trip: the id that
        # went into ``workstream_config`` comes back out populated into
        # the branch calls the model is handed.
        assert chr(10) + "  - tsk_a (in_progress)" in tasks_text
        assert "task_id='tsk_a'" in tasks_text
        assert "audit auth.py" not in tasks_text
    finally:
        watcher.shutdown()
        observer.shutdown()


def test_stop_latch_survives_the_liveness_wake(coord_mgr, tmp_db):
    """Operator Stop → liveness wake fires (by design) → the wake's own
    send must NOT clear ``_generation_abandoned`` → advice stays
    suppressed at the wake turn's terminal IDLE.

    The latch is cleared at the top of ``send()`` — and the liveness
    wake is itself a ``send("", from_wake=True)``, so an unconditional
    clear let one class's machinery erase the other's suppression: Stop
    bought exactly the task-reminder resume it exists to prevent, one
    bracket late.  The wake send must leave the latch alone; a real
    (non-wake) send is what lifts it.

    Every boundary is production code except the LLM stream: observer →
    queue → watcher → wake worker → ``deliver_wake_nudge_from_queue`` →
    ``send("", from_wake=True)``.
    """
    import json as _json

    from turnstone.console.coordinator_idle_observer import CoordinatorIdleObserver
    from turnstone.core.workstream import WorkstreamKind as _Kind

    mgr, adapter, storage = coord_mgr
    observer = CoordinatorIdleObserver(mgr, storage)
    observer.start()
    watcher = IdleNudgeWatcher(mgr)
    watcher.start()

    try:
        coord = mgr.create(user_id="u1", name="parent-coord", skill=None)
        assert coord.session is not None

        # Both conditions hold: a running child (liveness) and an open
        # task (advice).
        storage.register_workstream(
            "child-a",
            user_id="u1",
            name="research-pricing",
            kind=_Kind.INTERACTIVE,
            parent_ws_id=coord.id,
            state="running",
        )
        storage.save_workstream_config(
            coord.id,
            {
                "tasks": _json.dumps(
                    {
                        "version": 1,
                        "tasks": [
                            {"id": "tsk_a", "title": "audit auth.py", "status": "in_progress"}
                        ],
                    }
                )
            },
        )
        coord.session.messages.append(turn_from_dict({"role": "user", "content": "work"}))
        coord.session.messages.append(turn_from_dict({"role": "assistant", "content": "ok"}))

        # The operator presses Stop: the cancel path's real setter runs
        # (latch + demote), then the abandoned turn's terminal IDLE fans
        # out below.
        coord.session._drain_pending_advisories()
        assert coord.session._generation_abandoned is True
        # Control the advice cooldown stamp: it must be the LATCH that
        # suppresses idle_tasks throughout, never the per-class cooldown
        # G1 reinstated — a stamp here would mask a broken latch.
        assert coord.session._metacog_state.get("idle_tasks") is None

        with (
            patch.object(
                coord.session, "_stream_response", return_value=make_result(content="ack")
            ),
            patch.object(coord.session, "_full_messages", return_value=[]),
            patch.object(coord.session, "_update_token_table"),
            patch.object(coord.session, "_print_status_line"),
            patch.object(coord.session, "_visible_memory_count", return_value=0),
        ):
            coord.session._title_generated = True

            # The abandoned generation's terminal IDLE.  Advice returns
            # at the latch; liveness fires by design (the children's
            # results still need collecting) and the watcher wakes the
            # coordinator.
            mgr.set_state(coord.id, WorkstreamState.IDLE)
            _wait_for_worker_done(coord)

            msgs = dicts_from_turns(coord.session.messages)
            sys_sources = [m["_source"] for m in msgs if m.get("role") == "system"]
            assert sys_sources == ["idle_children"], "only the liveness wake may deliver"

            # THE PIN: the wake's own send must not have cleared the
            # latch.
            assert coord.session._generation_abandoned is True

            # The wake turn's terminal IDLE re-enters the observer (in
            # production via the coordinator UI's state bridge).  The
            # stamp store is still clean — the cooldown cannot be what
            # refuses — and advice must STILL be suppressed by the latch.
            coord.session._metacog_state.pop("idle_tasks", None)
            mgr.set_state(coord.id, WorkstreamState.IDLE)
            _wait_for_worker_done(coord)
            assert len(coord.session._nudge_queue) == 0
            msgs = dicts_from_turns(coord.session.messages)
            assert not any(
                m.get("_source") == "idle_tasks" for m in msgs if m.get("role") == "system"
            ), "Stop must keep suppressing advice across the wake"

            # The control: a REAL send lifts the latch, and the next
            # idle bracket's advice fires again.
            coord.session.send("resume the audit work")
            assert coord.session._generation_abandoned is False
            mgr.set_state(coord.id, WorkstreamState.IDLE)
            _wait_for_worker_done(coord)

        msgs = dicts_from_turns(coord.session.messages)
        assert any(m.get("_source") == "idle_tasks" for m in msgs if m.get("role") == "system"), (
            "a real send must restore the advice path"
        )
    finally:
        watcher.shutdown()
        observer.shutdown()


def test_coord_idle_emitted_from_worker_thread_still_wakes(coord_mgr, tmp_db):
    """The production-shaped race the test above does NOT exercise: in
    production, IDLE is emitted from INSIDE the worker (``set_state``
    subscribers fire on the calling thread — the coord's send emits IDLE
    before its worker exits).  The watcher's wake dispatch therefore
    lands on ``session_worker.send``'s reuse path while the
    transitioning worker still owns the flag, and no-ops.  Without the
    ownership-clear backstop the ``idle_children`` nudge strands until
    the next user message — a coord that forgot ``wait_for_workstream``
    never revives.

    Boundary path under test:
      worker thread: mgr.set_state(IDLE)
        → observer enqueues (real) → watcher wake no-ops (worker owns flag)
        → run() returns → session_worker._runner finally clears the flag
        → _retry_pending_wake → wake_workstream_if_pending (real)
        → wake daemon → deliver_wake_nudge_from_queue → send("")
        → idle_children system turn in history
    """
    from turnstone.console.coordinator_idle_observer import CoordinatorIdleObserver
    from turnstone.core.workstream import WorkstreamKind as _Kind

    mgr, adapter, storage = coord_mgr
    observer = CoordinatorIdleObserver(mgr, storage)
    observer.start()
    watcher = IdleNudgeWatcher(mgr)
    watcher.start()

    try:
        coord = mgr.create(user_id="u1", name="parent-coord-2", skill=None)
        assert coord.session is not None

        storage.register_workstream(
            "child-x",
            user_id="u1",
            name="crawl-docs",
            kind=_Kind.INTERACTIVE,
            parent_ws_id=coord.id,
            state="running",
        )

        coord.session.messages.append(turn_from_dict({"role": "user", "content": "spawn 1"}))
        coord.session.messages.append(turn_from_dict({"role": "assistant", "content": "ok"}))

        with (
            patch.object(
                coord.session, "_stream_response", return_value=make_result(content="ack")
            ),
            patch.object(coord.session, "_full_messages", return_value=[]),
            patch.object(coord.session, "_update_token_table"),
            patch.object(coord.session, "_print_status_line"),
            patch.object(coord.session, "_visible_memory_count", return_value=0),
        ):
            coord.session._title_generated = True

            # Drive the IDLE transition from INSIDE a session_worker
            # worker, as production does.
            ok = session_worker.send(
                coord,
                enqueue=lambda: None,
                run=lambda: mgr.set_state(coord.id, WorkstreamState.IDLE),
                thread_name="coord-send-sim",
            )
            assert ok is True
            # Without the backstop the queue never drains (the watcher's
            # transition-time wake no-opped against the sim worker) and
            # this poll times out.  Queue-empty implies the wake worker's
            # drain ran, so the follow-up flag poll waits for ITS exit.
            _wait_until(lambda: len(coord.session._nudge_queue) == 0)
            _wait_for_worker_done(coord)

        # Queue drained by the wake, not waiting on the next user message.
        assert len(coord.session._nudge_queue) == 0
        msgs = dicts_from_turns(coord.session.messages)
        user_msgs = [m for m in msgs if m.get("role") == "user"]
        wake_msg = user_msgs[-1]
        assert wake_msg["content"] == ""
        assert wake_msg.get("_source") == "system_nudge"
        idle_turns = [
            m for m in msgs if m.get("role") == "system" and m["_source"] == "idle_children"
        ]
        assert len(idle_turns) == 1
        # Ids-and-states roster: the child rides as its ws_id, never its
        # model-authored name.
        assert "child-x" in idle_turns[0]["content"]
        assert "crawl-docs" not in idle_turns[0]["content"]
        assert "wait_for_workstream" in idle_turns[0]["content"]
    finally:
        watcher.shutdown()
        observer.shutdown()


def test_wake_delivery_contains_generation_cancelled(tmp_db):
    """A close/force-cancel racing the wake turn raises
    ``GenerationCancelled`` (a BaseException) out of ``send("")`` — the
    wake method must contain it: it IS the wake worker's ``run()``
    closure, and ``session_worker._runner`` catches only ``Exception``,
    so an escape would land in ``threading.excepthook`` as stderr noise
    on every close-vs-wake race."""
    from tests._helpers import make_chat_session
    from turnstone.core.session import GenerationCancelled

    session = make_chat_session()
    session._nudge_queue.enqueue("idle_children", "kids waiting", "any")

    def _cancelled_send(*_a: Any, **_k: Any) -> None:
        raise GenerationCancelled

    session.send = _cancelled_send  # type: ignore[method-assign]

    session.deliver_wake_nudge_from_queue()  # must not raise

    assert session._wake_source_tag == ""
    assert session._wake_drained_reminders is None


# ---------------------------------------------------------------------------
# Wake-only channel discipline + the interjection-owns-the-seam handoff
# ---------------------------------------------------------------------------


def _patch_llm_surface(session: Any) -> tuple[Any, ...]:
    """The file's standard LLM-stub patch set, for make_chat_session tests."""
    return (
        patch.object(session, "_stream_response", return_value=make_result(content="ok")),
        patch.object(session, "_update_token_table"),
        patch.object(session, "_print_status_line"),
        patch.object(session, "_visible_memory_count", return_value=0),
    )


def test_wake_channel_survives_real_seam_drains_and_delivers_via_wake(tmp_db):
    """Channel discipline at the REAL drain sites: a wake-channel idle
    nudge is invisible to ``_emit_pending_user_nudges`` (the user-seam
    drain) and to ``_collect_advisories`` (the tool-seam drain), then
    delivers through the real ``deliver_wake_nudge_from_queue``."""
    from tests._helpers import make_chat_session

    session = make_chat_session()
    session._title_generated = True
    session._nudge_queue.enqueue("idle_tasks", "open tasks remain", "wake")
    session._nudge_queue.enqueue("idle_children", "kids waiting", "wake")

    p = _patch_llm_surface(session)
    with p[0], p[1], p[2], p[3]:
        # Real user-seam drain: appends any drained entry as a system
        # turn — a wake-channel entry must neither drain nor render.
        session._emit_pending_user_nudges()
        assert [m for m in dicts_from_turns(session.messages) if m.get("role") == "system"] == []
        assert len(session._nudge_queue) == 2

        # Real tool-seam drain (last result of a batch): same discipline.
        specs = session._collect_advisories(None, "some_tool", True)
        assert specs == []
        assert len(session._nudge_queue) == 2

        # The wake is the one seam that delivers them.
        session.deliver_wake_nudge_from_queue()

    msgs = dicts_from_turns(session.messages)
    wake_msg = [m for m in msgs if m.get("role") == "user"][-1]
    assert wake_msg["content"] == ""
    assert wake_msg.get("_source") == "system_nudge"
    sys_sources = [m["_source"] for m in msgs if m.get("role") == "system"]
    assert sys_sources == ["idle_tasks", "idle_children"]
    assert len(session._nudge_queue) == 0


def test_stop_drops_wake_entries_while_quiet_externals_survive(tmp_db):
    """Operator Stop (``_drain_pending_advisories``) DROPS wake-channel
    idle nudges — both types — while ``any``-channel externals survive
    demoted to quiet and still deliver at the next legitimate seam."""
    from tests._helpers import make_chat_session
    from turnstone.core.nudge_queue import USER_DRAIN, WAKE_PENDING

    session = make_chat_session()
    session._nudge_queue.enqueue("watch_triggered", "deploy finished", "any")
    session._nudge_queue.enqueue("idle_tasks", "open tasks remain", "wake")
    session._nudge_queue.enqueue("idle_children", "kids waiting", "wake")

    session._drain_pending_advisories()

    kinds = [t for t, _ in session._nudge_queue.pending()]
    assert kinds == ["watch_triggered"]  # both wake entries dropped, external kept
    # Post-Stop quiescence: nothing may re-arm the wake gate...
    assert not session._nudge_queue.has_pending(WAKE_PENDING)
    # ...and the external still rides the next legitimate seam.
    drained = session._nudge_queue.drain(USER_DRAIN)
    assert [t for t, _x, _m in drained] == ["watch_triggered"]


def test_wake_send_failure_drops_wake_entries_and_requeues_externals_quiet(tmp_db):
    """The failed-wake recovery path: an ``any``-channel external is
    given back on the quiet channel (seq preserved), while a
    wake-channel idle nudge is DROPPED — quiet would deliver it at the
    user/tool seams its channel exists to be invisible to, and a
    wake-eligible requeue would re-arm the worker-exit backstop into a
    hot loop."""
    from tests._helpers import make_chat_session
    from turnstone.core.session import GenerationCancelled

    session = make_chat_session()
    session._nudge_queue.enqueue("watch_triggered", "deploy finished", "any")
    session._nudge_queue.enqueue("idle_tasks", "open tasks remain", "wake")
    session._nudge_queue.enqueue("idle_children", "kids waiting", "wake")

    def _cancelled_send(*_a: Any, **_k: Any) -> None:
        raise GenerationCancelled

    session.send = _cancelled_send  # type: ignore[method-assign]
    session.deliver_wake_nudge_from_queue()  # must not raise

    assert session._nudge_queue.pending(channel="quiet") == [("watch_triggered", "deploy finished")]
    assert [t for t, _ in session._nudge_queue.pending()] == ["watch_triggered"]


def test_quiet_ride_along_still_delivers_when_wake_proceeds(tmp_db):
    """Corner: the two-pass drain's quiet ride-along survives the wake
    channel joining the WAKE_PENDING pass — a Stop-demoted external
    rides a wake earned by a wake-channel idle nudge, rendered in seq
    order (older external first)."""
    from tests._helpers import make_chat_session

    session = make_chat_session()
    session._title_generated = True
    session._nudge_queue.enqueue("watch_triggered", "older external", "any")
    session._nudge_queue.demote_channel("any", "quiet")  # a Stop demoted it
    session._nudge_queue.enqueue("idle_children", "kids waiting", "wake")

    p = _patch_llm_surface(session)
    with p[0], p[1], p[2], p[3]:
        session.deliver_wake_nudge_from_queue()

    msgs = dicts_from_turns(session.messages)
    sys_sources = [m["_source"] for m in msgs if m.get("role") == "system"]
    assert sys_sources == ["watch_triggered", "idle_children"]
    assert len(session._nudge_queue) == 0


def test_interjection_handoff_delivers_externals_and_drops_only_idle_nudges(tmp_db):
    """External events are NOT idle nudges, and the interjection handoff
    must not treat them as such (owner addendum, 2026-07-29): with a
    user interjection waiting at the idle seam, BOTH fire — the
    interjection as the genuine user turn AND the queued external-event
    entries on that same turn's drain seam (``send``'s
    ``_emit_pending_user_nudges`` drains ``{user, any, quiet}`` right
    after the user turn).  Only the wake-channel idle nudges drop; a
    quiet-demoted external keeps its next-legitimate-seam behaviour,
    which this genuine send is.

    Proven over the real send loop, not assumed from the drain sets."""
    from tests._helpers import make_chat_session

    session = make_chat_session()
    session._title_generated = True
    # One quiet-demoted external (a Stop demoted it earlier)...
    session._nudge_queue.enqueue("background_shell_exit", "dev server exited", "any")
    session._nudge_queue.demote_channel("any", "quiet")
    # ...one live "any" external (a watch fired), and both idle nudges.
    session._nudge_queue.enqueue("watch_triggered", "deploy finished: OK", "any")
    session._nudge_queue.enqueue("idle_tasks", "open tasks remain", "wake")
    session._nudge_queue.enqueue("idle_children", "kids waiting", "wake")
    # The user interjection is waiting when the wake worker starts.
    session.queue_message("pivot: focus on the flaky login test")

    p = _patch_llm_surface(session)
    with p[0], p[1], p[2], p[3]:
        session.deliver_wake_nudge_from_queue()

    msgs = dicts_from_turns(session.messages)
    user_msgs = [m for m in msgs if m.get("role") == "user"]
    # Exactly one genuine user turn: the interjection, un-tagged, and no
    # synthetic empty wake turn anywhere.
    assert [m["content"] for m in user_msgs] == ["pivot: focus on the flaky login test"]
    assert user_msgs[0].get("_source") is None
    # BOTH externals delivered on that turn's drain seam, in queue order;
    # the idle nudges are the only entries that dropped.
    sys_sources = [m["_source"] for m in msgs if m.get("role") == "system"]
    assert sys_sources == ["background_shell_exit", "watch_triggered"]
    assert len(session._nudge_queue) == 0
    assert session._wake_source_tag == ""


def test_queued_interjection_owns_the_idle_seam(coord_mgr, tmp_db):
    """The interjection handoff, end to end over production code:
    observer enqueues both wake-channel idle nudges on IDLE → watcher
    spawns the wake worker → ``deliver_wake_nudge_from_queue`` finds a
    queued user interjection and yields the seam to it.

    Pins the delivery-discipline ruling: the interjection lands as
    exactly ONE genuine user turn (no synthetic empty user turn, no
    wake ``_source`` tag), the wake-channel idle nudges are dropped
    (no idle system turns, nothing left to re-arm the wake gate), and
    — because the turn is genuine — the observer's cap reset fires on
    the next leave-IDLE, so the next genuine idle bracket re-derives
    both nudges over fresh reads."""
    import json as _json

    from turnstone.console.coordinator_idle_observer import CoordinatorIdleObserver
    from turnstone.core.workstream import WorkstreamKind as _Kind

    mgr, adapter, storage = coord_mgr
    observer = CoordinatorIdleObserver(mgr, storage)
    observer.start()
    watcher = IdleNudgeWatcher(mgr)
    watcher.start()

    try:
        coord = mgr.create(user_id="u1", name="parent-coord", skill=None)
        assert coord.session is not None

        # Both nudge conditions hold: a running child and an open task.
        storage.register_workstream(
            "child-a",
            user_id="u1",
            name="research-pricing",
            kind=_Kind.INTERACTIVE,
            parent_ws_id=coord.id,
            state="running",
        )
        storage.save_workstream_config(
            coord.id,
            {
                "tasks": _json.dumps(
                    {
                        "version": 1,
                        "tasks": [
                            {"id": "tsk_a", "title": "audit auth.py", "status": "in_progress"}
                        ],
                    }
                )
            },
        )
        coord.session.messages.append(turn_from_dict({"role": "user", "content": "spawn"}))
        coord.session.messages.append(turn_from_dict({"role": "assistant", "content": "ok"}))

        with (
            patch.object(
                coord.session, "_stream_response", return_value=make_result(content="ack")
            ),
            patch.object(coord.session, "_full_messages", return_value=[]),
            patch.object(coord.session, "_update_token_table"),
            patch.object(coord.session, "_print_status_line"),
            patch.object(coord.session, "_visible_memory_count", return_value=0),
        ):
            coord.session._title_generated = True

            # The user's message raced in while a worker owned the slot
            # (the only path that fills this queue) and is still waiting
            # when the coord goes idle.
            coord.session.queue_message("check the deploy logs first")

            mgr.set_state(coord.id, WorkstreamState.IDLE)
            _wait_for_worker_done(coord)

            msgs = dicts_from_turns(coord.session.messages)
            user_msgs = [m for m in msgs if m.get("role") == "user"]
            # Exactly one user turn carries the interjection; no empty
            # synthetic wake turn was appended anywhere.
            assert [m["content"] for m in user_msgs] == ["spawn", "check the deploy logs first"]
            # A genuine turn: no wake tag on it (the persistent artifact
            # ``_wake_source_tag`` would have stamped).
            assert user_msgs[-1].get("_source") is None
            # The idle nudges were dropped, not delivered — the seam was
            # the interjection's.
            assert [m for m in msgs if m.get("role") == "system"] == []
            assert len(coord.session._nudge_queue) == 0
            assert coord.session._wake_source_tag == ""

            # CAP RESET, through the real observer: the interjection turn
            # was genuine, so leaving IDLE without a wake tag clears the
            # per-bracket caps (in production the coordinator UI's state
            # bridge emits this transition from inside the send).
            mgr.set_state(coord.id, WorkstreamState.THINKING)
            # The advice cooldown stamp is test-controlled, as in the
            # stop-latch test: the claim under test is the cap/bracket
            # machinery, not the per-class cooldown window.
            coord.session._metacog_state.pop("idle_tasks", None)

            mgr.set_state(coord.id, WorkstreamState.IDLE)
            _wait_for_worker_done(coord)

        # The next genuine idle bracket re-derived BOTH nudges over fresh
        # reads and the wake delivered them.
        msgs = dicts_from_turns(coord.session.messages)
        sys_sources = [m["_source"] for m in msgs if m.get("role") == "system"]
        assert sys_sources == ["idle_tasks", "idle_children"]
        assert len(coord.session._nudge_queue) == 0
    finally:
        watcher.shutdown()
        observer.shutdown()


def test_interjection_handoff_dispatches_exactly_one_real_send(tmp_db):
    """The handoff's dispatch shape, pinned at the session boundary: a
    queued interjection plus queued wake nudges produce ONE ``send``
    call carrying the interjection text with ``from_wake`` unset — never
    ``send("")`` (that shape appends an empty untagged user turn and
    delivers the interjection one assistant turn late via the flush
    seam)."""
    from tests._helpers import make_chat_session

    session = make_chat_session()
    session._nudge_queue.enqueue("idle_tasks", "open tasks remain", "wake")
    session.queue_message("!!!stop the deploy")
    session.queue_message("then check the logs")

    calls: list[tuple[Any, ...]] = []

    def _recording_send(*a: Any, **k: Any) -> None:
        calls.append((a, k))

    session.send = _recording_send  # type: ignore[method-assign]
    session.deliver_wake_nudge_from_queue()

    assert len(calls) == 1
    args, kwargs = calls[0]
    # One combined genuine send: priority framing preserved, both queued
    # items folded in queue order, no wake flag.
    assert args == ("[IMPORTANT] stop the deploy\n\nthen check the logs",)
    assert kwargs == {}
    # The wake-channel nudge was dropped before the send, so nothing can
    # re-arm the wake gate at this worker's exit.
    assert len(session._nudge_queue) == 0
    assert session._queued_messages == {}
    assert session._wake_source_tag == ""


def test_interjection_handoff_forwards_nonempty_client_send_ids(tmp_db):
    """Correlated queued rows retain every browser token in queue order."""
    from tests._helpers import make_chat_session

    session = make_chat_session()
    session._nudge_queue.enqueue("idle_tasks", "open tasks remain", "wake")
    session.queue_message("first", client_send_id="browser-first")
    session.queue_message("second", client_send_id="browser-second")
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _recording_send(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    session.send = _recording_send  # type: ignore[method-assign]
    session.deliver_wake_nudge_from_queue()

    assert calls == [
        (
            ("first\n\nsecond",),
            {"client_send_ids": ("browser-first", "browser-second")},
        )
    ]


def test_interjection_handoff_contains_generation_cancelled(tmp_db):
    """A Stop landing inside the handed-off interjection send must not
    escape the wake worker: ``GenerationCancelled`` is a BaseException
    ``session_worker`` does not catch.  Deliberately NO restore on this
    arm — the Stop supersedes the queued words — so the queue stays
    empty and the wake entries stay dropped."""
    from tests._helpers import make_chat_session
    from turnstone.core.session import GenerationCancelled

    session = make_chat_session()
    session._nudge_queue.enqueue("idle_tasks", "open tasks remain", "wake")
    session._nudge_queue.enqueue("idle_children", "children active", "wake")
    session.queue_message("urgent question")

    def _cancelled_send(*a: Any, **k: Any) -> None:
        raise GenerationCancelled()

    session.send = _cancelled_send  # type: ignore[method-assign]
    session.deliver_wake_nudge_from_queue()  # must not raise

    assert len(session._nudge_queue) == 0
    assert session._queued_messages == {}


def test_interjection_handoff_restores_the_queue_when_send_raises(tmp_db):
    """Any escape other than a cancel happened in send's preamble,
    before the user turn was appended — the popped items must be
    restored (ids and priorities intact) and the failure must surface,
    so the next send's flush seams deliver the user's words instead of
    a log line being their gravestone."""
    from tests._helpers import make_chat_session

    session = make_chat_session()
    session._nudge_queue.enqueue("idle_tasks", "open tasks remain", "wake")
    _c, _p, msg_id = session.queue_message("!!!do not lose this")

    def _exploding_send(*a: Any, **k: Any) -> None:
        raise RuntimeError("preamble failure")

    session.send = _exploding_send  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="preamble failure"):
        session.deliver_wake_nudge_from_queue()

    # Restored verbatim: same id, same cleaned text, same priority.
    assert msg_id in session._queued_messages
    text, priority = session._queued_messages[msg_id][:2]
    assert text == "do not lose this"
    assert priority == "important"


def test_interjection_handoff_does_not_restore_after_a_late_raise(tmp_db):
    """send can raise from its LATE handlers, after the user turn was
    appended and persisted — restoring there would deliver the user's
    words twice at the next flush seam.  The length snapshot is the
    discriminator: an appended turn means no restore."""
    from tests._helpers import make_chat_session

    session = make_chat_session()
    session._nudge_queue.enqueue("idle_tasks", "open tasks remain", "wake")
    session.queue_message("delivered then failed")

    def _append_then_raise(text: str, *a: Any, **k: Any) -> None:
        session.messages.append({"role": "user", "content": text})
        raise RuntimeError("late failure")

    session.send = _append_then_raise  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="late failure"):
        session.deliver_wake_nudge_from_queue()

    # The turn reached history; the queue must NOT get it back.
    assert session._queued_messages == {}


def test_a_retraction_during_the_handoff_is_honoured_by_the_restore(tmp_db):
    """The DELETE route can land while the dispatcher holds the popped
    items — it finds the id gone and answers as already-sent.  A
    failure-path restore must not resurrect the message the user just
    cancelled."""
    from tests._helpers import make_chat_session

    session = make_chat_session()
    session._nudge_queue.enqueue("idle_tasks", "open tasks remain", "wake")
    _c, _p, keep_id = session.queue_message("keep this one")
    _c, _p, retract_id = session.queue_message("cancel this one")

    def _retract_mid_send(text: str, *a: Any, **k: Any) -> None:
        session.dequeue_message(retract_id)
        raise RuntimeError("preamble failure")

    session.send = _retract_mid_send  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="preamble failure"):
        session.deliver_wake_nudge_from_queue()

    assert keep_id in session._queued_messages
    assert retract_id not in session._queued_messages


def test_interjection_handoff_skips_the_pop_when_budget_exhausted(tmp_db):
    """On the budget latch ``send`` refuses without appending a turn
    unless a human approves, and a wake is unattended — the handoff
    must not pop (the message stays queued for the user's next real
    send) and must fall through to the wake drain so the worker's exit
    convergence holds."""
    from tests._helpers import make_chat_session

    session = make_chat_session()
    session._nudge_queue.enqueue("idle_tasks", "open tasks remain", "wake")
    _c, _p, msg_id = session.queue_message("held message")
    session._budget_exhausted = True

    sends: list[tuple[Any, ...]] = []

    def _recording_send(*a: Any, **k: Any) -> None:
        sends.append((a, k))

    session.send = _recording_send  # type: ignore[method-assign]
    session.deliver_wake_nudge_from_queue()

    # No interjection dispatch; the wake drain path ran instead (the
    # wake-eligible entry was drained toward the synthetic wake send).
    assert msg_id in session._queued_messages
    assert all(args != ("held message",) for args, _k in sends)
    assert any(a == ("",) for a, _k in sends)


def test_interjection_handoff_skips_the_pop_on_a_gone_workstream(tmp_db):
    """The delivery-site gate must be at least as strong as the claim gate:
    a nudge-driven wake reaches this method without ever consulting
    ``claim_pending_interjection_wake``, and under the gone latch send's
    admission refusal is converged INTERNALLY (no re-raise), so a pop here
    would destroy the user's words with no restore arm running.  No pop,
    rows retained — their disposition on a deleted workstream is #1001's."""
    from tests._helpers import make_chat_session

    session = make_chat_session()
    session._nudge_queue.enqueue("idle_tasks", "open tasks remain", "wake")
    _c, _p, msg_id = session.queue_message("held message")
    session._workstream_gone_ws = session._ws_id

    sends: list[tuple[Any, ...]] = []

    def _recording_send(*a: Any, **k: Any) -> None:
        sends.append((a, k))

    session.send = _recording_send  # type: ignore[method-assign]
    session.deliver_wake_nudge_from_queue()

    assert msg_id in session._queued_messages
    assert session._popped_in_flight == set()
    assert all(args != ("held message",) for args, _k in sends)


def test_interjection_handoff_falls_through_on_content_free_items(tmp_db):
    """A bare priority marker ('!!!') renders as nothing deliverable:
    the handoff must not spend the seam on a content-free user turn —
    the husk is discarded and the wake proceeds normally, nudges
    intact."""
    from tests._helpers import make_chat_session

    session = make_chat_session()
    session._nudge_queue.enqueue("idle_tasks", "open tasks remain", "wake")
    session.queue_message("!!!")
    session.queue_message("   ")

    sends: list[tuple[Any, ...]] = []

    def _recording_send(*a: Any, **k: Any) -> None:
        sends.append((a, k))

    session.send = _recording_send  # type: ignore[method-assign]
    session.deliver_wake_nudge_from_queue()

    # The husks were consumed, no interjection turn was dispatched, and
    # the wake path ran: the ONLY send is the wake's synthetic "" one —
    # a reverted husk skip would dispatch a rendered husk turn here and
    # fail the equality, so this line is the live mutation control.
    assert session._queued_messages == {}
    assert [a for a, _k in sends] == [("",)]
