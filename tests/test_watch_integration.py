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

import contextlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests._helpers import patch_session_storage
from turnstone.core.session import ChatSession
from turnstone.core.storage import get_storage
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
    runner._dispatch_result(
        session._ws_id,
        {"type": "watch_triggered", "text": "watch payload body"},
        "watch-1",
    )

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

    runner._dispatch_result(
        session._ws_id, {"type": "watch_triggered", "text": "fire one"}, "watch-1"
    )
    runner._dispatch_result(
        session._ws_id, {"type": "watch_triggered", "text": "fire two"}, "watch-1"
    )
    runner._dispatch_result(
        session._ws_id, {"type": "watch_triggered", "text": "fire three"}, "watch-1"
    )
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
    # through to the restore branch.  The dispatch surface takes a
    # structured reminder dict.
    runner._dispatch_result(
        original_ws_id,
        {"type": "watch_triggered", "text": "post-restore body"},
        "watch-1",
    )

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


@pytest.mark.parametrize(
    ("stop_on", "max_polls", "label"),
    [
        ('"HIT" in output', 100, "stop_on_fired"),
        (None, 1, "max_polls_reached"),
    ],
)
def test_poll_watch_terminal_fire_survives_drain(
    tmp_db: str,
    monkeypatch: pytest.MonkeyPatch,
    stop_on: str | None,
    max_polls: int,
    label: str,
) -> None:
    """Regression for the dispatch-ordering bug.

    With the broken ordering (``update_watch(active=False)`` before
    ``_dispatch_result``) plus the ``_still_active`` ``valid_until``
    predicate that re-reads ``is_watch_active`` at drain time, every
    terminal watch fire was silently dropped — the closure enqueued
    the entry but the predicate immediately invalidated it because
    the row's ``active`` flag had already been flipped to ``0`` in
    the same poll.  The model never saw the fire.

    This test drives a REAL ``WatchRunner._poll_watch`` against a real
    ``tmp_db`` watch row (no ``patch_session_storage(active=True)``
    stub — that stub is exactly what masked the bug in earlier tests).
    Covers both terminal paths: ``stop_on`` condition matched and
    ``poll_count >= max_polls`` reached.
    """
    session = _make_session()
    storage = get_storage()

    runner = WatchRunner(storage=storage, node_id="test-node")
    session.set_watch_runner(runner)

    storage.create_watch(
        watch_id=f"w-regression-{label}",
        ws_id=session._ws_id,
        node_id="test-node",
        name=f"regression-{label}",
        command="echo HIT",
        interval_secs=10.0,
        stop_on=stop_on,
        max_polls=max_polls,
        created_by="model",
        next_poll="1970-01-01T00:00:00",
    )

    # Spy ``enqueue`` so the assertion can distinguish "dispatch never
    # called" (a different bug class) from "dispatch enqueued but the
    # predicate dropped it at drain" (this bug).
    enqueue_calls: list[tuple[str, str, str]] = []
    real_enqueue = session._nudge_queue.enqueue

    def _spy_enqueue(*args: Any, **kwargs: Any) -> None:
        enqueue_calls.append((args[0], args[1][:40], args[2]))
        return real_enqueue(*args, **kwargs)

    monkeypatch.setattr(session._nudge_queue, "enqueue", _spy_enqueue)

    # For the max_polls=1 case the first poll has prev_output=None and
    # would not normally fire on output change; the max_polls branch
    # at watch.py:412-414 still marks is_final=True so dispatch runs.
    due = storage.list_due_watches("2099-01-01T00:00:00")
    matching = [r for r in due if r["watch_id"] == f"w-regression-{label}"]
    assert len(matching) == 1, f"watch row not picked up by list_due_watches: {due!r}"
    runner._poll_watch(matching[0])

    assert len(enqueue_calls) == 1, (
        f"_poll_watch did not enqueue exactly one fire (got {enqueue_calls!r}); "
        "this is a different bug from the predicate-drop regression"
    )
    assert enqueue_calls[0][0] == "watch_triggered"

    assert storage.is_watch_active(f"w-regression-{label}") is False, (
        "terminal fire should have committed active=False on the row"
    )

    # The key assertion: drain delivers the entry.  Pre-fix this
    # returned ``[]`` because the ``_still_active`` predicate re-read
    # ``active=0``.  Post-fix the watch closure no longer wires a
    # predicate and the entry survives.
    out = session._nudge_queue.drain({"any"})
    assert len(out) == 1, (
        "Watch fire was enqueued but never reached drain — dispatch-ordering "
        "regression.  Check that WatchRunner._poll_watch dispatches BEFORE "
        "committing active=False, and that the watch closure in "
        "ChatSession.set_watch_runner does not wire an is_watch_active "
        "predicate."
    )
    nt, text, _meta = out[0]
    assert nt == "watch_triggered"
    assert "HIT" in text


def test_cancel_reports_already_completed_for_auto_cancelled_watch(tmp_db: str) -> None:
    """After a watch fires and auto-cancels, the cancel-by-name path
    should report 'already completed' rather than 'not found'.

    Pre-fix, ``_exec_watch`` cancel looked the watch up via
    ``list_watches_for_ws`` which filters ``active==1``, so a recently-
    auto-cancelled row was invisible and the model got the same
    'not found' message it would for a typo'd name.  Post-fix the
    cancel path uses ``find_watch_by_name`` (no active filter) and
    branches on ``row["active"]``.
    """
    session = _make_session()
    storage = get_storage()

    storage.create_watch(
        watch_id="w-completed-1",
        ws_id=session._ws_id,
        node_id="test-node",
        name="completed-watch",
        command="echo x",
        interval_secs=10.0,
        stop_on=None,
        max_polls=100,
        created_by="model",
        next_poll="",
    )
    # Simulate the post-fire state.
    storage.update_watch("w-completed-1", active=False, next_poll="")

    _call_id, msg = session._exec_watch(
        {"call_id": "c1", "action": "cancel", "watch_name": "completed-watch"}
    )

    assert "not found" not in msg.lower()
    assert "completed" in msg.lower()


def test_cancel_reports_not_found_for_unknown_watch(tmp_db: str) -> None:
    """The 'not found' message still applies when the watch genuinely
    does not exist — make sure the new ``find_watch_by_name`` path
    didn't accidentally turn every cancel into 'already completed'.
    """
    session = _make_session()

    _call_id, msg = session._exec_watch(
        {"call_id": "c1", "action": "cancel", "watch_name": "ghost-watch"}
    )

    assert "not found" in msg.lower()


def test_poll_watch_retry_deactivate_after_update_watch_failure(
    tmp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_terminal_dispatched`` lifecycle: if ``update_watch`` raises
    AFTER ``_dispatch_result`` shipped the reminder for a terminal
    fire, the next ``_poll_watch`` tick MUST retry the row write
    (so the row stops appearing in ``list_due_watches``) and MUST NOT
    re-dispatch the reminder the model already saw.

    This is the keystone path that prevents duplicate-fire under
    transient storage failure.  Pre-this-test, the entire branch was
    unexercised.
    """
    session = _make_session()
    storage = get_storage()
    runner = WatchRunner(storage=storage, node_id="test-node")
    session.set_watch_runner(runner)

    watch_id = "w-retry-1"
    storage.create_watch(
        watch_id=watch_id,
        ws_id=session._ws_id,
        node_id="test-node",
        name="retry-watch",
        command="echo HIT",
        interval_secs=10.0,
        stop_on='"HIT" in output',
        max_polls=100,
        created_by="model",
        next_poll="1970-01-01T00:00:00",
    )

    enqueue_calls: list[tuple[str, str]] = []
    real_enqueue = session._nudge_queue.enqueue

    def _spy_enqueue(*args: Any, **kwargs: Any) -> None:
        enqueue_calls.append((args[0], args[1][:32]))
        return real_enqueue(*args, **kwargs)

    monkeypatch.setattr(session._nudge_queue, "enqueue", _spy_enqueue)

    # Stage 1 — first poll.  ``update_watch`` raises AFTER dispatch.
    real_update = storage.update_watch
    update_raise = {"armed": True}

    def _failing_update(wid: str, **fields: Any) -> bool:
        if update_raise["armed"]:
            raise RuntimeError("simulated transient storage failure")
        return real_update(wid, **fields)

    monkeypatch.setattr(storage, "update_watch", _failing_update)

    due = storage.list_due_watches("2099-01-01T00:00:00")
    matching = [r for r in due if r["watch_id"] == watch_id]
    assert len(matching) == 1
    # ``_poll_watch`` doesn't catch the storage error; the outer
    # ``_tick`` would log it.  Suppress here so the test owns the
    # boundary and continues to its assertions.
    with contextlib.suppress(RuntimeError):
        runner._poll_watch(matching[0])

    # Dispatch ran exactly once and the watch_id sits in the
    # terminal-dispatched set awaiting retry.
    assert len(enqueue_calls) == 1
    assert enqueue_calls[0][0] == "watch_triggered"
    assert watch_id in runner._terminal_dispatched

    # The row is still active=1 because update_watch raised.  It
    # would re-appear in list_due_watches on the next tick.
    assert storage.is_watch_active(watch_id) is True

    # Stage 2 — second poll.  Storage now succeeds; retry-deactivate
    # branch must commit active=False WITHOUT re-dispatching.
    update_raise["armed"] = False

    due = storage.list_due_watches("2099-01-01T00:00:00")
    matching = [r for r in due if r["watch_id"] == watch_id]
    assert len(matching) == 1
    runner._poll_watch(matching[0])

    # Exactly one dispatch in total — the retry path took the
    # short-circuit return at the top of _poll_watch.
    assert len(enqueue_calls) == 1, f"retry-deactivate must not re-dispatch; got {enqueue_calls!r}"
    # Row is now inactive (the retry path's update_watch landed).
    assert storage.is_watch_active(watch_id) is False
    # Set is cleared so future watches with the same id (unlikely) /
    # process memory doesn't accumulate.
    assert watch_id not in runner._terminal_dispatched


def test_cancel_clears_pending_terminal_dispatched_entry(
    tmp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``update_watch`` raised after dispatch, leaving a pending
    entry in ``_terminal_dispatched``, and the user then cancels the
    watch out-of-band, the retry-deactivate branch never gets to run
    (the cancel sets ``next_poll=""`` which removes the row from
    ``list_due_watches``).  The cancel path itself must discard the
    pending entry; otherwise the runner leaks ``watch_id``s for the
    process lifetime.
    """
    session = _make_session()
    storage = get_storage()
    runner = WatchRunner(storage=storage, node_id="test-node")
    session.set_watch_runner(runner)

    watch_id = "w-leak-1"
    storage.create_watch(
        watch_id=watch_id,
        ws_id=session._ws_id,
        node_id="test-node",
        name="leak-watch",
        command="echo x",
        interval_secs=10.0,
        stop_on=None,
        max_polls=100,
        created_by="model",
        next_poll="",
    )
    # Simulate: dispatch shipped, update_watch raised, watch_id sits
    # in the runner's pending set.
    with runner._terminal_dispatched_lock:
        runner._terminal_dispatched.add(watch_id)

    # User cancels.  Because the cancel writes active=False, next_poll="",
    # the row leaves list_due_watches and the runner's retry-deactivate
    # branch never executes for it.  The cancel must discard the entry.
    storage.update_watch(watch_id, active=False, next_poll="")
    session._exec_watch({"call_id": "c1", "action": "cancel", "watch_name": "leak-watch"})

    assert watch_id not in runner._terminal_dispatched
