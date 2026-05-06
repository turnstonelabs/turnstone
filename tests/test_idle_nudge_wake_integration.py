"""Boundary-crossing integration test for the wake trigger pipeline.

Drives a *real* :class:`SessionManager` + a *real* :class:`ChatSession`
+ a *real* :class:`IdleNudgeWatcher` end-to-end.  The only stub is the
LLM provider (patched ``_create_stream_with_retry``); every other layer
is production code:

* ``SessionManager.set_state`` snapshotting + iterating subscribers
* ``IdleNudgeWatcher._on_state`` peeking the queue
* ``session_worker.send`` atomic-spawn + daemon thread
* ``ChatSession.deliver_wake_nudge_from_queue`` opening / closing
  ``_wake_source_tag``
* ``ChatSession.send`` chat loop short-circuiting metacog detection
* ``_append_user_turn`` stamping ``_source = "system_nudge"``
* ``_attach_pending_user_reminders`` draining ``USER_DRAIN``
* ``_apply_reminders_for_provider`` splicing the rendered envelope
  onto empty content

Per ``feedback_tests_through_boundaries.md``: direct injection tests
that bypass these boundaries silently mask wiring bugs.  This test is
the structural integration gate.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests.test_session_manager import FakeStorage
from turnstone.core.metacognition import IdleNudgeWatcher
from turnstone.core.session import ChatSession
from turnstone.core.session_manager import SessionManager
from turnstone.core.workstream import Workstream, WorkstreamKind, WorkstreamState

# ---------------------------------------------------------------------------
# Minimal fake adapter / UI for this integration test.  Storage reuses
# the canonical FakeStorage from test_session_manager.py to avoid the
# drift risk of a parallel fake.
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
    def on_thinking_start(self) -> None:
        pass

    def on_thinking_end(self) -> None:
        pass

    def on_state_change(self, state: str) -> None:
        self.events.append(("state", state))

    def on_user_reminder(self, reminders: Any) -> None:
        self.events.append(("user_reminder", reminders))

    def on_error(self, message: str) -> None:
        pass

    def on_rename(self, name: str) -> None:
        pass

    def on_output_warning(self, call_id: Any, assessment: Any) -> None:
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

    def __init__(self) -> None:
        self.kind = WorkstreamKind.INTERACTIVE
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
        )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.fixture
def real_mgr() -> tuple[SessionManager, _BuildRealSessionAdapter]:
    """Real SessionManager wired to an adapter that builds real ChatSessions.

    No StateWriter is wired so ``set_state`` writes directly to storage
    on the calling thread (we want subscriber dispatch to fire in the
    same thread the test invokes ``set_state`` on).
    """
    adapter = _BuildRealSessionAdapter()
    storage = FakeStorage()
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


def test_idle_event_through_real_session_manager_drives_wake_send(real_mgr, tmp_db):
    """The full wake pipeline, no direct-injection shortcuts.

    Boundary path under test:
      enqueue → mgr.set_state(IDLE)
        → SessionManager._state_subscribers iteration (real)
        → IdleNudgeWatcher._on_state (real)
        → session_worker.send (real)
        → real daemon thread
        → ChatSession.deliver_wake_nudge_from_queue (real)
        → ChatSession.send("") (real, with patched LLM stream)
        → _append_user_turn stamps ``_source``
        → _attach_pending_user_reminders drains ``{"user","any"}``
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
            patch.object(ws.session, "_create_stream_with_retry", return_value=iter([])),
            patch.object(
                ws.session,
                "_stream_response",
                return_value={"role": "assistant", "content": "ok"},
            ),
            patch.object(ws.session, "_update_token_table"),
            patch.object(ws.session, "_print_status_line"),
            patch.object(ws.session, "_visible_memory_count", return_value=0),
            patch("turnstone.core.session.save_message"),
        ):
            # Suppress the auto-title side-thread; orthogonal to wake.
            ws.session._title_generated = True

            # Enqueue an any-channel nudge — the future ``idle_children`` shape.
            ws.session._nudge_queue.enqueue("idle_children", "your kids", "any")
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
        # ``_source`` audit tag and the reminder side-channel populated.
        user_msgs = [m for m in ws.session.messages if m.get("role") == "user"]
        assert user_msgs, "expected a synthesized user message from the wake"
        wake_msg = user_msgs[-1]
        assert wake_msg["content"] == ""
        assert wake_msg.get("_source") == "system_nudge"
        assert wake_msg.get("_reminders") == [{"type": "idle_children", "text": "your kids"}]

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
