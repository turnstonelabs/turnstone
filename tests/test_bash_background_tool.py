"""Session-level tests for the background-shell tool surface (#817).

Covers the wiring around :class:`BackgroundShellRegistry`:

* ``bash`` gains ``run_in_background: true`` (alias ``is_background``) —
  same approval gate, returns immediately with a ``bash_N`` handle.
* ``bash_output`` — auto-approved delta reader (status + exit code + only
  new output since the last call, optional ``filter`` regex).
* ``kill_shell`` — auto-approved kill of a registered shell's whole group.
* Exit notices ride the NudgeQueue on channel ``"any"`` (the watch rail) so
  they drain at the next seam and can wake an idle workstream.
* Lifecycle: ``close()`` reaps everything; generation-``cancel()`` does NOT
  (a deliberately-detached server survives a stopped turn); shells spawned
  inside a task_agent are owner-scoped and reaped when the agent finishes.
"""

import time

import pytest

from tests._proc_helpers import pid_alive as _pid_alive
from tests._proc_helpers import poll_until as _wait_until
from tests._session_helpers import make_session


@pytest.fixture
def session():
    s = make_session()
    yield s
    s.close()


def _start_background(session, command, call_id="bg1", **extra_args):
    """Prepare + execute a backgrounded bash call; return the result text."""
    args = {"command": command, "run_in_background": True, **extra_args}
    prepared = session._prepare_bash(call_id, args)
    assert "error" not in prepared, prepared.get("error")
    _cid, output = prepared["execute"](prepared)
    return output


def _only_shell(session):
    shells = session._background_shells.shells()
    assert len(shells) == 1
    return shells[0]


# ---------------------------------------------------------------------------
# bash: run_in_background routing
# ---------------------------------------------------------------------------


def test_prepare_bash_background_keeps_approval_gate(session):
    prepared = session._prepare_bash("c1", {"command": "sleep 30", "run_in_background": True})
    assert prepared["needs_approval"] is True
    assert prepared["approval_label"] == "bash"


def test_prepare_bash_background_header_says_background(session):
    prepared = session._prepare_bash("c1", {"command": "sleep 30", "run_in_background": True})
    assert "background" in prepared["header"]


def test_background_bash_returns_immediately_with_handle(session):
    start = time.monotonic()
    output = _start_background(session, "sleep 30")
    elapsed = time.monotonic() - start
    assert elapsed < 5, f"backgrounded call blocked for {elapsed:.1f}s"
    assert "bash_1" in output
    shell = _only_shell(session)
    assert shell.status == "running"
    assert _pid_alive(shell.pid)


def test_background_start_mentions_reader_and_killer(session):
    """The immediate result must teach the follow-up tools — weak-prior
    models (GPT-5.6) only reach for the poll pattern if the result names it."""
    output = _start_background(session, "sleep 30")
    assert "bash_output" in output
    assert "kill_shell" in output


def test_is_background_alias_accepted(session):
    output = _start_background(session, "sleep 30", is_background=True)
    assert "bash_1" in output
    assert _only_shell(session).status == "running"


def test_foreground_bash_routing_unchanged(session):
    prepared = session._prepare_bash("c1", {"command": "echo hi"})
    assert prepared["execute"] == session._exec_bash
    prepared_false = session._prepare_bash("c2", {"command": "echo hi", "run_in_background": False})
    assert prepared_false["execute"] == session._exec_bash


def test_background_respects_command_blocklist(session):
    prepared = session._prepare_bash("c1", {"command": "shutdown now", "run_in_background": True})
    assert "error" in prepared
    assert session._background_shells.shells() == []


def test_background_ignores_timeout(session):
    """No bounded wait exists to time out — a 1s timeout must not kill the
    detached shell."""
    _start_background(session, "sleep 30", timeout=1)
    shell = _only_shell(session)
    time.sleep(1.5)
    assert shell.status == "running"
    assert _pid_alive(shell.pid)


def test_background_spawn_failure_reports_error(session, monkeypatch):
    from turnstone.core import background_shells as bg_mod

    def _boom(*args, **kwargs):
        raise OSError("cannot fork")

    monkeypatch.setattr(bg_mod.subprocess, "Popen", _boom)
    prepared = session._prepare_bash("c1", {"command": "echo hi", "run_in_background": True})
    _cid, output = prepared["execute"](prepared)
    assert "cannot fork" in output


def test_too_many_background_shells_reports_error(session, monkeypatch):
    monkeypatch.setattr(session._background_shells, "_max_shells", 1)
    _start_background(session, "sleep 30", call_id="bg1")
    output = _start_background(session, "sleep 30", call_id="bg2")
    assert "bash_1" in output  # the live shell is named so the model can kill it
    assert len(session._background_shells.shells()) == 1


# ---------------------------------------------------------------------------
# bash_output
# ---------------------------------------------------------------------------


def test_bash_output_is_auto_approved(session):
    prepared = session._prepare_bash_output("c1", {"id": "bash_1"})
    assert prepared["needs_approval"] is False


def test_bash_output_missing_id_errors(session):
    prepared = session._prepare_bash_output("c1", {})
    assert "error" in prepared


def test_bash_output_returns_delta_then_no_new_output(session):
    _start_background(session, "echo hello; sleep 30")
    shell = _only_shell(session)
    assert _wait_until(lambda: shell.status == "running")

    def _read():
        prepared = session._prepare_bash_output("r", {"id": shell.shell_id})
        assert "error" not in prepared
        return prepared["execute"](prepared)[1]

    assert _wait_until(lambda: "hello" in _read())
    again = _read()
    assert "hello" not in again
    assert "no new output" in again.lower()
    assert "running" in again.lower()


def test_bash_output_reports_exit_code_when_completed(session):
    _start_background(session, "exit 3")
    shell = _only_shell(session)
    assert _wait_until(lambda: shell.status == "completed")
    prepared = session._prepare_bash_output("r", {"id": shell.shell_id})
    _cid, output = prepared["execute"](prepared)
    assert "completed" in output.lower()
    assert "3" in output


def test_bash_output_filter_applies(session):
    _start_background(session, "echo match-a; echo skip-b")
    shell = _only_shell(session)
    assert _wait_until(lambda: shell.status == "completed")
    prepared = session._prepare_bash_output("r", {"id": shell.shell_id, "filter": "^match"})
    _cid, output = prepared["execute"](prepared)
    assert "match-a" in output
    assert "skip-b" not in output


def test_bash_output_invalid_filter_reports_error(session):
    _start_background(session, "sleep 30")
    shell = _only_shell(session)
    prepared = session._prepare_bash_output("r", {"id": shell.shell_id, "filter": "[bad"})
    _cid, output = prepared["execute"](prepared)
    assert "regex" in output.lower() or "filter" in output.lower()


def test_bash_output_unknown_id_lists_live_shells(session):
    _start_background(session, "sleep 30")
    prepared = session._prepare_bash_output("r", {"id": "bash_42"})
    _cid, output = prepared["execute"](prepared)
    assert "bash_42" in output
    assert "bash_1" in output


# ---------------------------------------------------------------------------
# kill_shell
# ---------------------------------------------------------------------------


def test_kill_shell_is_auto_approved(session):
    prepared = session._prepare_kill_shell("c1", {"id": "bash_1"})
    assert prepared["needs_approval"] is False


def test_kill_shell_missing_id_errors(session):
    prepared = session._prepare_kill_shell("c1", {})
    assert "error" in prepared


def test_kill_shell_kills_and_reports(session):
    _start_background(session, "sleep 60")
    shell = _only_shell(session)
    prepared = session._prepare_kill_shell("k", {"id": shell.shell_id})
    _cid, output = prepared["execute"](prepared)
    assert "killed" in output.lower()
    assert _wait_until(lambda: not _pid_alive(shell.pid))


def test_kill_shell_unknown_id_reports_error(session):
    prepared = session._prepare_kill_shell("k", {"id": "bash_9"})
    _cid, output = prepared["execute"](prepared)
    assert "bash_9" in output


# ---------------------------------------------------------------------------
# Exit notices (NudgeQueue, channel "any", wake)
# ---------------------------------------------------------------------------


def test_natural_exit_enqueues_any_channel_notice(session):
    _start_background(session, "echo done")
    assert _wait_until(
        lambda: any(t == "background_shell_exit" for t, _ in session._nudge_queue.pending())
    )
    entries = session._nudge_queue.pending(channel="any")
    texts = [text for t, text in entries if t == "background_shell_exit"]
    assert texts, "notice must ride channel 'any' so it can wake an idle workstream"
    assert "bash_1" in texts[0]
    assert "bash_output" in texts[0]


def test_exit_notice_carries_metadata(session):
    _start_background(session, "exit 5")
    assert _wait_until(
        lambda: any(t == "background_shell_exit" for t, _ in session._nudge_queue.pending())
    )
    metadata = [
        meta
        for t, _text, meta in session._nudge_queue.pending_with_metadata()
        if t == "background_shell_exit"
    ][0]
    assert metadata["shell_id"] == "bash_1"
    assert metadata["exit_code"] == 5


def test_exit_notice_triggers_wake_fn(session):
    wakes = []
    session._watch_wake_fn = lambda: wakes.append(1)
    _start_background(session, "echo done")
    assert _wait_until(lambda: wakes), "natural exit must wake an idle workstream"


def test_kill_shell_suppresses_exit_notice(session):
    _start_background(session, "sleep 60")
    shell = _only_shell(session)
    prepared = session._prepare_kill_shell("k", {"id": shell.shell_id})
    prepared["execute"](prepared)
    assert _wait_until(lambda: not _pid_alive(shell.pid))
    time.sleep(0.3)  # a buggy late notice would land within this window
    assert not any(t == "background_shell_exit" for t, _ in session._nudge_queue.pending())


def test_close_drops_pending_exit_notice_via_valid_until(session):
    """A notice for a shell that no longer exists (registry closed) must not
    deliver — the valid_until predicate drops it at drain time."""
    _start_background(session, "echo done")
    assert _wait_until(
        lambda: any(t == "background_shell_exit" for t, _ in session._nudge_queue.pending())
    )
    session.close()
    from turnstone.core.nudge_queue import USER_DRAIN

    drained = session._nudge_queue.drain(USER_DRAIN)
    assert not any(t == "background_shell_exit" for t, _text, _m in drained)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_close_reaps_background_shells(session):
    _start_background(session, "sleep 60")
    shell = _only_shell(session)
    session.close()
    assert not _pid_alive(shell.pid)


def test_generation_cancel_does_not_reap_background_shells(session):
    """cancel() fires on mere stop-generation — a deliberately-detached
    server must survive it.  Only close()/kill_shell end it."""
    _start_background(session, "sleep 60")
    shell = _only_shell(session)
    session.cancel()
    time.sleep(0.3)
    assert _pid_alive(shell.pid), "generation cancel must not kill detached shells"


# ---------------------------------------------------------------------------
# Review-hardening regressions (#817 code review)
# ---------------------------------------------------------------------------


def test_string_typed_background_flag_is_honored(session):
    """Providers intermittently send booleans as strings; 'true' must not
    silently fall through to the foreground executor (where the group kill
    would reap the server the model believed it detached)."""
    for call_id, args in (
        ("s1", {"command": "sleep 30", "run_in_background": "true"}),
        ("s2", {"command": "sleep 30", "is_background": "True"}),
    ):
        prepared = session._prepare_bash(call_id, args)
        assert prepared["execute"] == session._exec_bash_background, args


def test_kill_shell_on_completed_shell_reports_already_exited(session):
    _start_background(session, "true")
    shell = _only_shell(session)
    assert _wait_until(lambda: shell.status == "completed")
    prepared = session._prepare_kill_shell("k", {"id": shell.shell_id})
    _cid, output = prepared["execute"](prepared)
    assert "already exited" in output.lower()


def test_exit_notice_survives_generation_abandon_without_waking(session):
    """cancel/interrupt/exception clear generation-scoped advisories, but an
    external event (a background shell exited) still happened — its notice
    must survive to the next seam or the model keeps talking to a dead
    server.  It survives DEMOTED to 'quiet': still deliverable, but no
    longer wake-eligible, so the workstream the user just stopped cannot
    resume itself over it."""
    from turnstone.core.nudge_queue import USER_DRAIN, WAKE_PENDING

    _start_background(session, "echo done")
    assert _wait_until(
        lambda: any(t == "background_shell_exit" for t, _ in session._nudge_queue.pending())
    )
    session._queue_tool_advisory("tool_error", "3 consecutive tool errors")
    session._drain_pending_advisories()
    kinds = [t for t, _ in session._nudge_queue.pending()]
    assert "background_shell_exit" in kinds
    assert "tool_error" not in kinds
    # Post-cancel quiescence: nothing is wake-eligible...
    assert not session._nudge_queue.has_pending(WAKE_PENDING)
    # ...yet the notice still delivers at the next legitimate seam.
    drained = session._nudge_queue.drain(USER_DRAIN)
    assert any(t == "background_shell_exit" for t, _x, _m in drained)


def test_int_typed_background_flag_is_honored(session):
    prepared = session._prepare_bash("i1", {"command": "sleep 30", "run_in_background": 1})
    assert prepared["execute"] == session._exec_bash_background
    prepared_zero = session._prepare_bash("i2", {"command": "echo hi", "run_in_background": 0})
    assert prepared_zero["execute"] == session._exec_bash


def test_bash_output_non_string_filter_errors_without_consuming(session):
    _start_background(session, "echo hello; sleep 30")
    shell = _only_shell(session)
    prepared = session._prepare_bash_output("r", {"id": shell.shell_id, "filter": 123})
    assert "error" in prepared
    assert "filter" in prepared["error"].lower()
    # Nothing was consumed by the refused call.
    assert _wait_until(lambda: shell.unread_lines > 0)


def test_filter_timeout_reports_error_without_consuming(session, monkeypatch):
    from turnstone.core.background_shells import FilterTimeoutError

    _start_background(session, "sleep 30")
    shell = _only_shell(session)

    def _boom(*a, **kw):
        raise FilterTimeoutError("filter regex took longer than 2s to run")

    monkeypatch.setattr(session._background_shells, "read", _boom)
    prepared = session._prepare_bash_output("r", {"id": shell.shell_id, "filter": "(a+)+$"})
    _cid, output = prepared["execute"](prepared)
    assert "filter" in output.lower()
    assert "error" in output.lower()


def test_registries_are_isolated_per_session():
    """Workstream isolation: a handle from one session must be unresolvable
    from another — buffers, ids, and kills never cross ChatSessions."""
    session_a = make_session()
    session_b = make_session()
    try:
        _start_background(session_a, "sleep 30")
        shell_a = _only_shell(session_a)
        read_b = session_b._prepare_bash_output("r", {"id": shell_a.shell_id})
        _cid, output = read_b["execute"](read_b)
        assert "no background shell" in output.lower()
        kill_b = session_b._prepare_kill_shell("k", {"id": shell_a.shell_id})
        _cid, kill_output = kill_b["execute"](kill_b)
        assert "no background shell" in kill_output.lower()
        assert _pid_alive(shell_a.pid), "another session must not be able to kill the shell"
    finally:
        session_a.close()
        session_b.close()


def test_bash_output_polling_is_repeat_exempt(session):
    """Repeated identical bash_output calls ARE the documented monitoring
    pattern — the repeat detector must not brand them 'identical repeat'
    (the delta result differs by construction) nor queue a repeat nudge."""
    import json as _json

    _start_background(session, "sleep 30")
    shell = _only_shell(session)
    args = _json.dumps({"id": shell.shell_id})
    for i in range(5):
        tool_calls = [{"id": f"t{i}", "function": {"name": "bash_output", "arguments": args}}]
        results = [(f"t{i}", "bash_1 (running)\nNo new output since the last read.")]
        session._apply_post_execute_advisories(tool_calls, results)
        assert "identical repeat" not in results[0][1]
    assert not any(t == "repeat" for t, _ in session._nudge_queue.pending())


def test_repeat_exempt_calls_still_break_other_streaks(session):
    """The exemption suppresses the WARNING, not the recording: a
    bash_output poll interleaved between identical bash calls must reset
    the bash streak — otherwise the documented monitor-and-probe loop
    (poll, curl health, poll, curl health…) draws a false 'identical
    repeat' on the probe."""
    import json as _json

    _start_background(session, "sleep 30")
    shell = _only_shell(session)
    poll_args = _json.dumps({"id": shell.shell_id})
    probe_args = _json.dumps({"command": "curl -s localhost:8080/health"})
    for i in range(6):
        probe = [{"id": f"p{i}", "function": {"name": "bash", "arguments": probe_args}}]
        probe_results = [(f"p{i}", "ok")]
        session._apply_post_execute_advisories(probe, probe_results)
        assert "identical repeat" not in probe_results[0][1], (
            "interleaved probes are not a stuck loop"
        )
        poll = [{"id": f"q{i}", "function": {"name": "bash_output", "arguments": poll_args}}]
        session._apply_post_execute_advisories(poll, [(f"q{i}", "no new output")])


def test_bash_repeats_still_warn(session):
    """The exemption is bash_output-specific: a genuinely stuck identical
    bash loop still gets the warning."""
    import json as _json

    args = _json.dumps({"command": "echo test"})
    warned = False
    for i in range(5):
        tool_calls = [{"id": f"b{i}", "function": {"name": "bash", "arguments": args}}]
        results = [(f"b{i}", "test")]
        session._apply_post_execute_advisories(tool_calls, results)
        warned = warned or "identical repeat" in results[0][1]
    assert warned


def test_quiet_only_entries_do_not_trigger_wake_delivery(session, monkeypatch):
    """A dispatched wake whose wake-eligible entries all evaporated must be
    a no-op: quiet entries alone never resume a stopped workstream, and
    they stay queued for the next legitimate seam."""
    calls = []
    monkeypatch.setattr(session, "send", lambda *a, **k: calls.append(1))
    session._nudge_queue.enqueue("background_shell_exit", "old news", "quiet")
    session.deliver_wake_nudge_from_queue()
    assert calls == []
    assert session._nudge_queue.pending(channel="quiet") == [("background_shell_exit", "old news")]


def test_wake_delivers_quiet_alongside_eligible_in_insertion_order(session, monkeypatch):
    """Quiet entries ride the wake AND cross-channel chronology holds: an
    older demoted notice renders before the newer fire that earned the
    wake (a poll counter must never run backwards)."""
    seen = {}

    def _fake_send(*a, **k):
        seen["reminders"] = list(session._wake_drained_reminders or [])
        session._wake_drained_reminders = None  # emulate emission consuming

    monkeypatch.setattr(session, "send", _fake_send)
    session._nudge_queue.enqueue("background_shell_exit", "old", "quiet")
    session._nudge_queue.enqueue("watch_triggered", "new", "any")
    session.deliver_wake_nudge_from_queue()
    types = [e["type"] for e in seen["reminders"]]
    assert types == ["background_shell_exit", "watch_triggered"], (
        "older quiet entry must precede the newer wake-eligible one"
    )
    assert session._nudge_queue.pending() == []


def test_failed_wake_reenqueue_preserves_valid_until(session, monkeypatch):
    """The re-enqueued notice keeps its staleness predicate — a stale
    notice re-queued by a failed wake must still be droppable at its next
    drain, not delivered against a gone shell."""
    from turnstone.core.nudge_queue import USER_DRAIN

    alive = {"value": True}

    def _fail(*a, **k):
        raise RuntimeError("storage down")

    monkeypatch.setattr(session, "send", _fail)
    session._nudge_queue.enqueue(
        "background_shell_exit",
        "server died",
        "any",
        valid_until=lambda: alive["value"],
    )
    with pytest.raises(RuntimeError):
        session.deliver_wake_nudge_from_queue()
    assert session._nudge_queue.pending(channel="quiet"), "notice must be re-queued"
    alive["value"] = False  # the shell record is gone now
    drained = session._nudge_queue.drain(USER_DRAIN)
    assert drained == [], "stale re-queued notice must drop via its predicate"


def test_mid_emit_failure_restashes_unemitted_tail(session, monkeypatch):
    """A failure while emitting reminder k of n must leave k..n recoverable
    — the wake caller's finally re-enqueues them instead of losing the
    suffix."""
    calls = {"n": 0}

    def _append(source, text, **meta):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("storage down")

    monkeypatch.setattr(session, "_append_system_turn", _append)
    session._wake_drained_reminders = [
        {"type": "a", "text": "1"},
        {"type": "b", "text": "2"},
        {"type": "c", "text": "3"},
    ]
    with pytest.raises(RuntimeError):
        session._emit_pending_user_nudges()
    assert session._wake_drained_reminders == [
        {"type": "b", "text": "2"},
        {"type": "c", "text": "3"},
    ]


def test_failed_wake_reenqueues_undelivered_as_quiet(session, monkeypatch):
    """A wake send that dies before emitting its drained reminders must not
    eat them — a shell's exit notice fires exactly once."""

    def _fail(*a, **k):
        raise RuntimeError("storage down")

    monkeypatch.setattr(session, "send", _fail)
    session._nudge_queue.enqueue(
        "background_shell_exit", "server died", "any", metadata={"shell_id": "bash_1"}
    )
    with pytest.raises(RuntimeError):
        session.deliver_wake_nudge_from_queue()
    pending = session._nudge_queue.pending_with_metadata(channel="quiet")
    assert [(t, x) for t, x, _m in pending] == [("background_shell_exit", "server died")]
    assert pending[0][2] == {"shell_id": "bash_1"}


def test_failed_wake_preserves_chronology_and_stays_wake_quiescent(session, monkeypatch):
    """Failed-wake recovery invariants: (a) the re-queued external notice
    keeps its seq, so the retry renders it BEFORE a newer event that
    arrived during the failure; (b) NOTHING wake-eligible remains after
    the failure — external notices demote to quiet and user-channel
    advisories are dropped outright, because a re-armed WAKE_PENDING gate
    plus the zero-backoff worker-exit retry would respawn wake workers in
    an unbounded hot loop against a persistent failure."""
    from turnstone.core.nudge_queue import WAKE_PENDING

    calls = {"n": 0}
    seen = {}

    def _send(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient storage failure")
        seen["reminders"] = list(session._wake_drained_reminders or [])
        session._wake_drained_reminders = None

    monkeypatch.setattr(session, "send", _send)
    session._nudge_queue.enqueue("watch_triggered", "poll-4", "any")
    session._nudge_queue.enqueue("correction", "user advisory", "user")
    with pytest.raises(RuntimeError):
        session.deliver_wake_nudge_from_queue()
    # (b) bounded: nothing left that could re-trigger the wake gate.
    assert not session._nudge_queue.has_pending(WAKE_PENDING), (
        "a failed wake must not leave wake-eligible entries (respawn hot loop)"
    )
    assert [t for t, _x in session._nudge_queue.pending(channel="quiet")] == ["watch_triggered"]
    # A NEWER event lands after the failure...
    session._nudge_queue.enqueue("watch_triggered", "poll-5", "any")
    session.deliver_wake_nudge_from_queue()
    texts = [e["text"] for e in seen["reminders"]]
    # (a) ...and the retry renders old-before-new despite the round trip.
    assert texts.index("poll-4") < texts.index("poll-5")


def test_exit_notice_emits_end_to_end_as_system_turn(session):
    """THE test whose absence hid an undeliverable notice for six review
    rounds: drive the notice through REAL emission (make_system_turn +
    _append_system_turn), not just queue assertions — an unregistered
    ``_source`` raises ValueError only at this layer."""
    _start_background(session, "echo done")
    assert _wait_until(
        lambda: any(t == "background_shell_exit" for t, _ in session._nudge_queue.pending())
    )
    from turnstone.core.trajectory import Role

    before = len(session.messages)
    session._emit_pending_user_nudges()  # must not raise
    new_turns = session.messages[before:]
    assert any(
        turn.role is Role.SYSTEM and turn.source == "background_shell_exit" for turn in new_turns
    ), f"exit notice must land as a first-class system turn, got {new_turns!r}"


def test_cli_exit_closes_every_loaded_session():
    """CLI exit must reap background shells in EVERY workstream, not just
    the active one — a server started before /new must not outlive /exit."""
    from unittest.mock import MagicMock

    from turnstone.cli import _close_all_sessions

    ws_a, ws_b, ws_never_loaded = MagicMock(), MagicMock(), MagicMock()
    ws_never_loaded.session = None
    ws_a.session.close.side_effect = RuntimeError("bad teardown")
    manager = MagicMock()
    manager.list_all.return_value = [ws_a, ws_b, ws_never_loaded]
    _close_all_sessions(manager)  # must not raise
    ws_a.session.close.assert_called_once()
    ws_b.session.close.assert_called_once(), "one bad teardown must not stop the rest"
    # Signal phase ran for every loaded session, before any close.
    ws_a.session._background_shells.signal_all.assert_called_once()
    ws_b.session._background_shells.signal_all.assert_called_once()


def test_cli_exit_ctrl_c_does_not_abort_the_reap():
    """Ctrl-C during the close phase must not escape the helper: the kill
    signals already landed on every session in phase 1, and an escaping
    KeyboardInterrupt would also skip MCP/registry shutdown in main()."""
    from unittest.mock import MagicMock

    from turnstone.cli import _close_all_sessions

    ws_a, ws_b = MagicMock(), MagicMock()
    ws_a.session.close.side_effect = KeyboardInterrupt
    manager = MagicMock()
    manager.list_all.return_value = [ws_a, ws_b]
    _close_all_sessions(manager)  # must not raise
    ws_a.session._background_shells.signal_all.assert_called_once()
    (
        ws_b.session._background_shells.signal_all.assert_called_once(),
        ("signals must land on every session before the interruptible close phase"),
    )


def test_non_string_reminder_text_drops_silently(session):
    """A dict reminder with non-str text must drop at the rail, not
    TypeError out of the dispatch closure (WatchRunner would re-fire the
    row every tick)."""
    runner = type(
        "R",
        (),
        {
            "set_dispatch_fn": lambda self, ws, fn: None,
            "remove_dispatch_fn": lambda self, ws, owner=None: None,
        },
    )()
    session.set_watch_runner(runner)
    session._watch_dispatch_fn({"text": 123, "watch_name": "w"}, "watch-1")  # must not raise
    assert session._nudge_queue.pending() == []


def test_string_typed_stop_on_error_is_honored(session):
    """One coercion dialect for every bash boolean: a string-typed
    stop_on_error must add set -e in both branches, not silently drop it."""
    fg = session._prepare_bash("f1", {"command": "echo hi", "stop_on_error": "true"})
    assert fg["stop_on_error"] is True
    bg = session._prepare_bash(
        "b1", {"command": "echo hi", "run_in_background": True, "stop_on_error": "true"}
    )
    assert bg["stop_on_error"] is True


def test_non_dict_watch_reminder_drops_silently(session):
    """The rebuilt dispatch closure must drop a non-dict reminder like the
    old code did — a TypeError would make WatchRunner hold and re-fire the
    row every tick."""
    runner = type(
        "R",
        (),
        {
            "set_dispatch_fn": lambda self, ws, fn: None,
            "remove_dispatch_fn": lambda self, ws, owner=None: None,
        },
    )()
    session.set_watch_runner(runner)
    dispatch = session._watch_dispatch_fn
    dispatch("not a dict", "watch-1")  # must not raise
    assert session._nudge_queue.pending() == []


def test_truthy_flag_dialect_is_unified():
    """One coercion dialect file-wide — 'on' and nonzero numbers count, so a
    provider quirk honored on coordinator tools is honored on bash too."""
    from turnstone.core.session import _is_truthy_flag

    assert _is_truthy_flag(True)
    assert _is_truthy_flag("on")
    assert _is_truthy_flag(2)
    assert not _is_truthy_flag("off")
    assert not _is_truthy_flag(0)
    assert not _is_truthy_flag(None)
    assert not _is_truthy_flag(False)


def test_bash_output_notes_clipped_lines_under_filter(session):
    _start_background(session, "printf 'x%.0s' $(seq 1 5000); echo tail")
    shell = _only_shell(session)
    assert _wait_until(lambda: shell.status == "completed")
    prepared = session._prepare_bash_output("r", {"id": shell.shell_id, "filter": "zzz"})
    _cid, output = prepared["execute"](prepared)
    assert "partially visible" in output


# ---------------------------------------------------------------------------
# task_agent scoping
# ---------------------------------------------------------------------------


def test_task_agent_shells_are_owner_scoped_and_reaped(session, monkeypatch):
    seen = {}

    def fake_run_agent(agent_turns, label="task", **kwargs):
        out = _start_background(session, "sleep 60", call_id="sub-bash")
        seen["start_output"] = out
        agent_shells = session._background_shells.shells(owner="task-1")
        seen["agent_shells"] = list(agent_shells)
        seen["pid"] = agent_shells[0].pid if agent_shells else None
        # The sub-agent's shell is invisible to the main scope.
        seen["visible_to_parent"] = [s.shell_id for s in session._background_shells.shells()]
        return "agent done"

    monkeypatch.setattr(session, "_run_agent", fake_run_agent)
    call_id, result = session._exec_task({"call_id": "task-1", "prompt": "start a server"})
    assert "agent done" in result
    assert seen["agent_shells"], "shell spawned inside the agent must carry its owner"
    # Scope honesty in the start message: the sub-agent must not promise its
    # caller a server that dies the moment it returns.
    assert "terminated when the agent finishes" in seen["start_output"]
    assert seen["visible_to_parent"] == []
    assert seen["pid"] is not None
    assert _wait_until(lambda: not _pid_alive(seen["pid"])), (
        "sub-agent shells must be reaped when the agent finishes"
    )


def test_task_agent_cannot_touch_parent_shells(session, monkeypatch):
    _start_background(session, "sleep 60", call_id="parent-bash")
    parent_shell = _only_shell(session)
    seen = {}

    def fake_run_agent(agent_turns, label="task", **kwargs):
        prepared = session._prepare_bash_output("r", {"id": parent_shell.shell_id})
        seen["read_output"] = prepared["execute"](prepared)[1]
        prepared_kill = session._prepare_kill_shell("k", {"id": parent_shell.shell_id})
        seen["kill_output"] = prepared_kill["execute"](prepared_kill)[1]
        return "done"

    monkeypatch.setattr(session, "_run_agent", fake_run_agent)
    session._exec_task({"call_id": "task-1", "prompt": "snoop"})
    assert "no background shell" in seen["read_output"].lower()
    assert "no background shell" in seen["kill_output"].lower()
    assert _pid_alive(parent_shell.pid), "agent must not be able to kill a parent shell"


def test_parent_scope_restored_after_task_agent(session, monkeypatch):
    monkeypatch.setattr(session, "_run_agent", lambda *a, **k: "done")
    session._exec_task({"call_id": "task-1", "prompt": "noop"})
    output = _start_background(session, "sleep 30", call_id="after-task")
    assert "bash_1" in output
    assert _only_shell(session).owner is None
