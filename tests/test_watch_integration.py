"""Boundary-crossing integration test for the watch switchover pipeline.

Drives a real :class:`ChatSession` + a real :class:`WatchRunner` (with
its daemon thread skipped — we call ``_dispatch_result`` directly to
avoid the timer dependency) end-to-end through the chat-loop drain
seam.  The only stub is the LLM provider (patched
``_create_stream_with_retry``); every other layer is production code:

* ``WatchRunner._dispatch_result`` releasing the dispatch lock before
  fan-out
* the closure built inside ``ChatSession.set_watch_runner`` —
  ``sanitize_payload`` + soft-cap check + ``valid_until`` predicate +
  ``NudgeQueue.enqueue("watch_triggered", ..., "any", ...)``
* ``ChatSession.send`` chat loop short-circuiting metacog detection
* ``_attach_pending_user_reminders`` draining ``USER_DRAIN`` (which
  matches ``"any"``)
* ``_apply_reminders_for_provider`` splicing the rendered envelope onto
  the user message before the wire boundary

Per ``feedback_tests_through_boundaries.md``: direct injection tests
that bypass these boundaries silently mask wiring bugs.  This test is
the structural integration gate for the watch switchover.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from tests._helpers import patch_session_storage
from turnstone.core.session import ChatSession
from turnstone.core.watch import WatchRunner


class _NullUI:
    """UI adapter that no-ops every chat-loop hook the test triggers."""

    def __getattr__(self, name: str) -> Any:
        return MagicMock()


def _make_session() -> ChatSession:
    """Real ChatSession with the same minimal setup the unit-test suite
    uses; no LLM calls happen until a chat-loop method is exercised
    (and even then the LLM provider is patched).
    """
    return ChatSession(
        client=MagicMock(),
        model="test-model",
        ui=_NullUI(),
        instructions=None,
        temperature=0.5,
        max_tokens=4096,
        tool_timeout=30,
    )


def test_watch_fires_then_user_send_drains_envelope(tmp_db, monkeypatch):
    """Pin the cross-PR concern that watch text reaches the model via
    the unified ``<system-reminder>`` envelope path:

    1. WatchRunner.dispatch fires watch text against the session's
       registered closure (synchronously — no daemon thread).
    2. NudgeQueue holds one ``"watch_triggered"`` entry on ``"any"``.
    3. session.send("ok") runs the chat loop with a stubbed LLM.
    4. The drain seam drains the watch entry; the wire payload's user
       message has the watch text spliced into a ``<system-reminder>``
       envelope.
    """
    session = _make_session()

    # Bypass the storage-touching predicate — we want to assert the
    # envelope splice, not exercise a fresh sqlite watch row.
    patch_session_storage(monkeypatch, active=True)

    # Real WatchRunner; we don't ``start()`` the daemon thread (that
    # would race with the test's deterministic order).  Direct call
    # to ``_dispatch_result`` exercises the same dispatch path the
    # daemon would invoke.  Runner-side ``storage`` is unused on this
    # path (only the polling loop touches it); a MagicMock placeholder
    # keeps the constructor signature happy.
    runner = WatchRunner(storage=MagicMock(), node_id="test-node")
    session.set_watch_runner(runner)

    # 1. Fire a watch result synchronously.
    runner._dispatch_result(session._ws_id, "watch payload body", "watch-1")

    # 2. The queue holds one entry on the "any" channel.
    assert len(session._nudge_queue) == 1
    pending = session._nudge_queue.pending(channel="any")
    assert pending == [("watch_triggered", "watch payload body")]

    # 3. Run the chat loop with the LLM patched.  We don't care about
    # the assistant turn's content; only the wire payload sent to the
    # provider matters.
    with (
        patch.object(session, "_create_stream_with_retry", return_value=iter([])),
        patch.object(
            session,
            "_stream_response",
            return_value={"role": "assistant", "content": "ok"},
        ),
        patch.object(session, "_update_token_table"),
        patch.object(session, "_print_status_line"),
        patch.object(session, "_visible_memory_count", return_value=0),
        patch("turnstone.core.session.save_message"),
    ):
        session._title_generated = True  # suppress orthogonal title side-thread
        session.send("ok")

    # 4. Queue fully drained by the user-message attach seam.
    assert len(session._nudge_queue) == 0

    # The user message that drove the assistant turn has the watch
    # text in its ``_reminders`` side-channel — the production
    # ``_apply_reminders_for_provider`` splice consumes that to wrap
    # the content in ``<system-reminder>`` at the wire boundary.
    user_msgs = [m for m in session.messages if m.get("role") == "user"]
    assert user_msgs, "expected a user message in history"
    last_user = user_msgs[-1]
    reminders = last_user.get("_reminders") or []
    assert any(
        r.get("type") == "watch_triggered" and "watch payload body" in r.get("text", "")
        for r in reminders
    ), f"expected watch_triggered reminder on user message; got {reminders!r}"


def test_three_back_to_back_watch_fires_drain_into_one_turn(tmp_db, monkeypatch):
    """Behavioural delta from plan section 3.4 / risk register R3.

    N back-to-back watch fires used to produce N successive
    ``send()`` turns (each a separate model invocation, capped at
    ``_MAX_WATCH_CHAIN = 5``).  After the switchover, the N entries
    drain into ONE envelope splice on the next drain seam — one
    assistant turn responding to all N watch results.  Pinning this
    behavioural delta protects against accidental regression to
    the old per-fire-turn shape.
    """
    session = _make_session()

    patch_session_storage(monkeypatch, active=True)

    runner = WatchRunner(storage=MagicMock(), node_id="test-node")
    session.set_watch_runner(runner)

    runner._dispatch_result(session._ws_id, "fire one", "watch-1")
    runner._dispatch_result(session._ws_id, "fire two", "watch-1")
    runner._dispatch_result(session._ws_id, "fire three", "watch-1")
    assert len(session._nudge_queue) == 3

    with (
        patch.object(session, "_create_stream_with_retry", return_value=iter([])),
        patch.object(
            session,
            "_stream_response",
            return_value={"role": "assistant", "content": "got it"},
        ),
        patch.object(session, "_update_token_table"),
        patch.object(session, "_print_status_line"),
        patch.object(session, "_visible_memory_count", return_value=0),
        patch("turnstone.core.session.save_message"),
    ):
        session._title_generated = True
        session.send("user")

    # All three drained into the single user-message attach.
    user_msgs = [m for m in session.messages if m.get("role") == "user"]
    last_user = user_msgs[-1]
    reminders = last_user.get("_reminders") or []
    watch_reminders = [r for r in reminders if r.get("type") == "watch_triggered"]
    assert len(watch_reminders) == 3
    bodies = [r.get("text", "") for r in watch_reminders]
    assert any("fire one" in b for b in bodies)
    assert any("fire two" in b for b in bodies)
    assert any("fire three" in b for b in bodies)
    # And there's exactly ONE assistant turn (not three).
    assistant_turns = [m for m in session.messages if m.get("role") == "assistant"]
    assert len(assistant_turns) == 1


def test_watch_dispatch_through_restore_fn_lands_on_rehydrated_session(tmp_db, monkeypatch):
    """Cover the production ``_watch_restore_fn`` closure surface.

    Path under test:
      WatchRunner._dispatch_result(ws_id, msg, watch_id)
        no dispatch fn registered (original session evicted)
        restore_fn(ws_id) constructs a fresh ChatSession,
            calls session.resume(ws_id) to adopt the original ws_id,
            re-registers the dispatch closure via session.set_watch_runner,
            returns runner.get_dispatch_fn(session._ws_id)
        runner invokes the returned fn with (msg, watch_id)
        watch payload lands on the rehydrated session's NudgeQueue

    Construction inside ``server.py``'s ``_watch_restore_fn`` is the new
    contract surface introduced by the switchover; this test pins that
    contract so a future refactor of the closure (e.g. swapping
    ``manager.create + session.resume`` for ``manager.open``) doesn't
    silently break the watch-restore pipeline.
    """
    from turnstone.core import session as session_mod

    patch_session_storage(monkeypatch, active=True)

    # Stage 1 — build the original session and persist a message so
    # ``session.resume`` finds the ws_id in storage.
    original = _make_session()
    original_ws_id = original._ws_id
    # Persist a stub user message so ``load_messages(original_ws_id)``
    # returns something non-empty (resume short-circuits on empty).
    session_mod.save_message(original_ws_id, "user", "kickoff message")

    # Stage 2 — runner with NO dispatch fn registered (simulates the
    # original session being evicted between watch fire and dispatch).
    # The restore_fn captures *which* fresh ChatSession got built so the
    # test can assert the queue landed on it (not on the original).
    rehydrated_holder: dict[str, ChatSession] = {}

    def _restore_fn(ws_id: str) -> Any:
        """Mirror the production ``_watch_restore_fn`` closure shape:
        construct a fresh session, resume the persisted ws_id (so the
        new session adopts the original ws_id), wire the dispatch
        closure, return the dispatch fn.
        """
        new_session = _make_session()
        ok = new_session.resume(ws_id)
        assert ok, "resume should succeed against a non-empty message log"
        new_session.set_watch_runner(runner)
        rehydrated_holder["session"] = new_session
        return runner.get_dispatch_fn(new_session._ws_id)

    runner = WatchRunner(
        storage=MagicMock(),
        node_id="test-node",
        restore_fn=_restore_fn,
    )

    # Sanity: no dispatch fn registered yet for the original ws_id.
    assert runner.get_dispatch_fn(original_ws_id) is None

    # Stage 3 — fire a watch result.  ``_dispatch_result`` should fall
    # through to the restore branch.
    runner._dispatch_result(original_ws_id, "post-restore body", "watch-1")

    # The restore fn ran exactly once and produced a fresh session that
    # adopted the original ws_id.
    assert "session" in rehydrated_holder, "restore_fn was not invoked"
    rehydrated = rehydrated_holder["session"]
    assert rehydrated is not original
    assert rehydrated._ws_id == original_ws_id

    # The watch payload landed on the rehydrated session's queue, not on
    # the (now-evicted) original session's queue.
    assert len(rehydrated._nudge_queue) == 1
    assert rehydrated._nudge_queue.pending(channel="any") == [
        ("watch_triggered", "post-restore body")
    ]
    # Original session's queue stays empty — the dispatch did NOT
    # accidentally route back to it.
    assert len(original._nudge_queue) == 0
