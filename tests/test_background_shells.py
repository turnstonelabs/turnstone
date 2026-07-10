"""Unit tests for the per-session background-shell registry (#817).

The registry backs the ``bash(run_in_background=true)`` / ``bash_output`` /
``kill_shell`` tool surface: it spawns detached shells (``bash_N`` handles),
buffers their merged output in a capped rolling buffer, serves delta reads
(only lines since the last read), and reaps whole session groups on kill /
owner reap / close — the #816 rule (the tracked command defines the lifetime,
nothing escapes its process group) extended to explicit backgrounding.

Pure registry tests — no ChatSession.  Session wiring is covered in
``test_bash_background_tool.py``.
"""

import re
import threading
import time

import pytest

from tests._proc_helpers import kill_pid as _kill_pid
from tests._proc_helpers import pid_alive as _pid_alive
from tests._proc_helpers import poll_until as _wait_until
from turnstone.core.background_shells import (
    BackgroundShellRegistry,
    FilterTimeoutError,
    TooManyShellsError,
    UnknownShellError,
)


def _wait_status(shell, status, timeout=10.0):
    return _wait_until(lambda: shell.status == status, timeout=timeout)


@pytest.fixture
def registry():
    reg = BackgroundShellRegistry()
    yield reg
    reg.close()


# ---------------------------------------------------------------------------
# Handles + spawning
# ---------------------------------------------------------------------------


def test_spawn_returns_incrementing_bash_handles(registry):
    s1 = registry.spawn("sleep 30")
    s2 = registry.spawn("sleep 30")
    assert s1.shell_id == "bash_1"
    assert s2.shell_id == "bash_2"


def test_spawned_shell_is_running_with_live_pid(registry):
    shell = registry.spawn("sleep 30")
    assert shell.status == "running"
    assert _pid_alive(shell.pid)


def test_spawn_records_command(registry):
    shell = registry.spawn("sleep 30")
    assert shell.command == "sleep 30"


def test_spawn_after_close_is_refused():
    reg = BackgroundShellRegistry()
    reg.close()
    with pytest.raises(RuntimeError):
        reg.spawn("echo hi")


def test_max_live_shells_cap():
    reg = BackgroundShellRegistry(max_shells=2)
    try:
        reg.spawn("sleep 30")
        s2 = reg.spawn("sleep 30")
        with pytest.raises(TooManyShellsError):
            reg.spawn("sleep 30")
        # Cap counts LIVE shells: killing one frees a slot.
        reg.kill(s2.shell_id)
        s3 = reg.spawn("sleep 30")
        assert s3.status == "running"
    finally:
        reg.close()


def test_completed_shells_do_not_count_toward_cap():
    reg = BackgroundShellRegistry(max_shells=1)
    try:
        s1 = reg.spawn("true")
        assert _wait_status(s1, "completed")
        s2 = reg.spawn("sleep 30")
        assert s2.status == "running"
    finally:
        reg.close()


# ---------------------------------------------------------------------------
# Exit tracking
# ---------------------------------------------------------------------------


def test_natural_exit_sets_completed_and_exit_code(registry):
    shell = registry.spawn("exit 7")
    assert _wait_status(shell, "completed")
    assert shell.exit_code == 7


def test_output_is_complete_once_completed(registry):
    """Status flips to completed only after the drains finish: a read at
    completed must see everything the command wrote."""
    shell = registry.spawn("echo alpha; echo beta")
    assert _wait_status(shell, "completed")
    read = registry.read(shell.shell_id)
    assert [ln.strip() for ln in read.lines] == ["alpha", "beta"]


def test_leader_exit_reaps_backgrounded_grandchild(registry, tmp_path):
    """#816 consistency: the tracked command defines the lifetime.  When the
    leader exits, the whole session group is killed — a child the command
    backgrounded does not outlive it."""
    pidfile = tmp_path / "bg.pid"
    shell = registry.spawn(f"sleep 60 & echo $! > {pidfile}; echo done")
    bg_pid = None
    try:
        assert _wait_status(shell, "completed")
        bg_pid = int(pidfile.read_text().strip())
        assert _wait_until(lambda: not _pid_alive(bg_pid)), (
            f"grandchild {bg_pid} leaked past leader exit"
        )
        read = registry.read(shell.shell_id)
        assert "done" in "".join(read.lines)
    finally:
        if bg_pid is not None:
            _kill_pid(bg_pid)


def test_stderr_lines_are_tagged_inline(registry):
    shell = registry.spawn("echo out; echo err >&2")
    assert _wait_status(shell, "completed")
    lines = [ln.strip() for ln in registry.read(shell.shell_id).lines]
    assert "out" in lines
    assert "[stderr] err" in lines


# ---------------------------------------------------------------------------
# Delta reads
# ---------------------------------------------------------------------------


def test_read_returns_only_new_lines_since_last_read(registry):
    """The load-bearing convention: consecutive reads never overlap and never
    drop a line — collecting across polls yields each line exactly once."""
    shell = registry.spawn("echo one; echo two; sleep 0.4; echo three; sleep 30")
    collected: list[str] = []

    def _collect():
        collected.extend(ln.strip() for ln in registry.read(shell.shell_id).lines)
        return "three" in collected

    assert _wait_until(_collect)
    assert collected == ["one", "two", "three"]
    registry.kill(shell.shell_id)


def test_read_after_exit_then_again_reports_no_new_output(registry):
    shell = registry.spawn("echo hi")
    assert _wait_status(shell, "completed")
    first = registry.read(shell.shell_id)
    assert [ln.strip() for ln in first.lines] == ["hi"]
    second = registry.read(shell.shell_id)
    assert second.lines == []
    assert second.status == "completed"
    assert second.exit_code == 0


def test_read_reports_status_and_exit_code(registry):
    shell = registry.spawn("sleep 30")
    read = registry.read(shell.shell_id)
    assert read.shell_id == shell.shell_id
    assert read.status == "running"
    assert read.exit_code is None
    registry.kill(shell.shell_id)


def test_read_unknown_id_raises_with_live_ids(registry):
    registry.spawn("sleep 30")
    with pytest.raises(UnknownShellError) as excinfo:
        registry.read("bash_99")
    assert "bash_99" in str(excinfo.value)
    assert "bash_1" in str(excinfo.value)


def test_read_unknown_id_when_registry_empty(registry):
    with pytest.raises(UnknownShellError):
        registry.read("bash_1")


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


def test_filter_selects_matching_lines_only(registry):
    shell = registry.spawn("echo match-a; echo skip-b; echo match-c")
    assert _wait_status(shell, "completed")
    read = registry.read(shell.shell_id, filter_pattern="^match")
    assert [ln.strip() for ln in read.lines] == ["match-a", "match-c"]


def test_filter_is_display_only_and_consumes_the_delta(registry):
    """Filtered-out lines are consumed, not deferred — the cursor advances
    past the whole delta (Claude Code ``BashOutput`` semantics)."""
    shell = registry.spawn("echo match-a; echo skip-b")
    assert _wait_status(shell, "completed")
    first = registry.read(shell.shell_id, filter_pattern="^match")
    assert [ln.strip() for ln in first.lines] == ["match-a"]
    assert first.new_line_count == 2  # both lines were new, one shown
    second = registry.read(shell.shell_id)
    assert second.lines == []
    assert second.new_line_count == 0


def test_filter_uses_search_not_match(registry):
    shell = registry.spawn("echo prefix-needle-suffix")
    assert _wait_status(shell, "completed")
    read = registry.read(shell.shell_id, filter_pattern="needle")
    assert len(read.lines) == 1


def test_invalid_filter_regex_raises(registry):
    shell = registry.spawn("echo hi")
    assert _wait_status(shell, "completed")
    with pytest.raises(re.error):
        registry.read(shell.shell_id, filter_pattern="[unclosed")


# ---------------------------------------------------------------------------
# Buffer cap
# ---------------------------------------------------------------------------


def test_buffer_cap_drops_oldest_and_reports_gap():
    reg = BackgroundShellRegistry(max_buffer_chars=200)
    try:
        shell = reg.spawn('for i in $(seq 1 50); do echo "line-$i-padded-to-length"; done')
        assert _wait_status(shell, "completed")
        read = reg.read(shell.shell_id)
        assert read.dropped_lines > 0
        # Newest output survives; the tail is intact.
        assert read.lines, "cap must retain the newest lines, not drop everything"
        assert read.lines[-1].strip() == "line-50-padded-to-length"
    finally:
        reg.close()


def test_unread_lines_excludes_buffer_evicted():
    """The exit notice's line count must not promise evicted output."""
    reg = BackgroundShellRegistry(max_buffer_chars=200)
    try:
        shell = reg.spawn('for i in $(seq 1 50); do echo "line-$i-padded-to-length"; done')
        assert _wait_status(shell, "completed")
        with shell.lock:
            retained = len(shell._buffer)
        assert shell.unread_lines == retained
    finally:
        reg.close()


def test_buffer_gap_is_relative_to_cursor():
    """Lines dropped BEFORE being read are a reported gap; lines already
    read and then dropped are not."""
    reg = BackgroundShellRegistry(max_buffer_chars=10_000)
    try:
        shell = reg.spawn("echo early; sleep 30")
        # Each poll consumes whatever has arrived; stop once something did.
        assert _wait_until(lambda: bool(reg.read(shell.shell_id).lines))
        # Everything emitted so far is read; nothing has been dropped.
        read = reg.read(shell.shell_id)
        assert read.dropped_lines == 0
        reg.kill(shell.shell_id)
    finally:
        reg.close()


# ---------------------------------------------------------------------------
# Kill / reap / close
# ---------------------------------------------------------------------------


def test_kill_marks_killed_and_reaps_group(registry, tmp_path):
    pidfile = tmp_path / "bg.pid"
    shell = registry.spawn(f"sleep 60 & echo $! > {pidfile}; sleep 60")
    assert _wait_until(pidfile.exists)
    bg_pid = int(pidfile.read_text().strip())
    try:
        killed = registry.kill(shell.shell_id)
        assert killed.status == "killed"
        assert _wait_until(lambda: not _pid_alive(shell.pid))
        assert _wait_until(lambda: not _pid_alive(bg_pid)), "grandchild survived kill"
    finally:
        _kill_pid(bg_pid)


def test_kill_unknown_id_raises(registry):
    with pytest.raises(UnknownShellError):
        registry.kill("bash_7")


def test_killed_shell_output_remains_readable(registry, tmp_path):
    """Output that arrived before the kill survives it: the record keeps its
    buffer, and ``kill`` returns only after the drains have flushed."""
    sentinel = tmp_path / "started"
    shell = registry.spawn(f"echo before-kill; touch {sentinel}; sleep 60")
    assert _wait_until(sentinel.exists)
    registry.kill(shell.shell_id)
    read = registry.read(shell.shell_id)
    assert read.status == "killed"
    assert "before-kill" in "".join(read.lines)


def test_signal_all_kills_live_shells_without_closing(registry):
    """signal_all is the instant half of teardown: every live group dies,
    but the registry stays open (records intact, spawns still allowed) —
    close() remains the complete teardown."""
    s1 = registry.spawn("sleep 60")
    s2 = registry.spawn("sleep 60")
    registry.signal_all()
    assert _wait_until(lambda: not _pid_alive(s1.pid))
    assert _wait_until(lambda: not _pid_alive(s2.pid))
    assert registry.has(s1.shell_id), "signal_all must not drop records"
    s3 = registry.spawn("true")
    assert _wait_status(s3, "completed"), "registry must remain usable after signal_all"


def test_close_kills_everything_and_is_idempotent():
    reg = BackgroundShellRegistry()
    s1 = reg.spawn("sleep 60")
    s2 = reg.spawn("sleep 60")
    reg.close()
    assert not _pid_alive(s1.pid)
    assert not _pid_alive(s2.pid)
    reg.close()  # second close is a no-op


def test_reap_owner_kills_only_that_owners_shells(registry):
    mine = registry.spawn("sleep 60", owner="agent-1")
    other = registry.spawn("sleep 60", owner="agent-2")
    main = registry.spawn("sleep 60")
    registry.reap(owner="agent-1")
    assert _wait_until(lambda: not _pid_alive(mine.pid))
    assert _pid_alive(other.pid)
    assert _pid_alive(main.pid)


# ---------------------------------------------------------------------------
# Owner scoping
# ---------------------------------------------------------------------------


def test_owner_scoped_lookup_isolates_shells(registry):
    agent_shell = registry.spawn("sleep 30", owner="agent-1")
    main_shell = registry.spawn("sleep 30")
    # Main scope cannot see the agent's shell...
    with pytest.raises(UnknownShellError):
        registry.read(agent_shell.shell_id)
    # ...and the agent scope cannot see the main shell.
    with pytest.raises(UnknownShellError):
        registry.read(main_shell.shell_id, owner="agent-1")
    # Each side reads its own.
    assert registry.read(agent_shell.shell_id, owner="agent-1").status == "running"
    assert registry.read(main_shell.shell_id).status == "running"


def test_shells_snapshot_is_owner_scoped(registry):
    registry.spawn("sleep 30", owner="agent-1")
    registry.spawn("sleep 30")
    assert [s.owner for s in registry.shells(owner="agent-1")] == ["agent-1"]
    assert [s.owner for s in registry.shells()] == [None]


def test_handles_are_unique_across_owners(registry):
    a = registry.spawn("sleep 30", owner="agent-1")
    b = registry.spawn("sleep 30")
    assert a.shell_id != b.shell_id


# ---------------------------------------------------------------------------
# Exit callback (the notice hook)
# ---------------------------------------------------------------------------


def test_on_exit_fires_once_on_natural_exit():
    fired = threading.Event()
    seen = []

    def _on_exit(shell):
        seen.append(shell)
        fired.set()

    reg = BackgroundShellRegistry(on_exit=_on_exit)
    try:
        shell = reg.spawn("echo done")
        assert fired.wait(10)
        assert len(seen) == 1
        assert seen[0].shell_id == shell.shell_id
        assert seen[0].exit_code == 0
    finally:
        reg.close()


def test_on_exit_not_fired_for_kill():
    seen = []
    reg = BackgroundShellRegistry(on_exit=seen.append)
    try:
        shell = reg.spawn("sleep 60")
        reg.kill(shell.shell_id)
        assert _wait_until(lambda: not _pid_alive(shell.pid))
        time.sleep(0.2)  # give a buggy late callback a chance to land
        assert seen == []
    finally:
        reg.close()


def test_on_exit_not_fired_for_close():
    seen = []
    reg = BackgroundShellRegistry(on_exit=seen.append)
    shell = reg.spawn("sleep 60")
    reg.close()
    assert not _pid_alive(shell.pid)
    time.sleep(0.2)
    assert seen == []


# ---------------------------------------------------------------------------
# Review-hardening regressions (#817 code review)
# ---------------------------------------------------------------------------


def test_kill_on_completed_shell_does_not_signal_group(registry, monkeypatch):
    """A completed shell's pgid is a stale snapshot the OS may have recycled
    to an unrelated process group — kill() must not signal it (the waiter's
    own group kill already ran at exit, when the pgid was fresh)."""
    import turnstone.core.background_shells as bg_mod

    shell = registry.spawn("true")
    assert _wait_status(shell, "completed")
    calls = []
    monkeypatch.setattr(bg_mod.os, "killpg", lambda *a: calls.append(a))
    killed = registry.kill(shell.shell_id)
    assert calls == [], "killpg must not fire for an already-exited shell"
    assert killed.status == "completed", "a natural exit must not be relabelled 'killed'"


def test_close_is_time_bounded_with_pipe_holding_escapee(registry, tmp_path):
    """An escaped-group grandchild that holds the output pipes wedges the
    drain threads.  close() must still return within its total budget —
    it can run under the server's async close route, where an unbounded
    join would freeze the whole node's event loop."""
    pidfile = tmp_path / "holder.pid"
    # ``setsid`` puts the sleep in a NEW session (outside our kill group)
    # while it still inherits our stdout/stderr pipes — the accepted
    # leaked-daemon case from the module docstring.
    shell = registry.spawn(f"setsid sleep 60 & echo $! > {pidfile}; echo started")
    assert _wait_until(pidfile.exists)
    holder_pid = int(pidfile.read_text().strip())
    try:
        start = time.monotonic()
        registry.close()
        elapsed = time.monotonic() - start
        assert elapsed < 8, f"close() took {elapsed:.1f}s — teardown must be budget-bounded"
    finally:
        _kill_pid(holder_pid)
        # The holder is dead, so the wedged drains EOF promptly; wait for
        # them here so the conftest leak guard sees a clean teardown.
        assert _wait_until(lambda: not any(t.is_alive() for t in shell._threads))


def test_exited_records_are_pruned_at_cap():
    reg = BackgroundShellRegistry(max_exited_records=2)
    try:
        shells = [reg.spawn(f"echo job-{i}") for i in range(3)]
        for s in shells:
            assert _wait_status(s, "completed")
        # Eviction happens on each exit; poll until the oldest is gone
        # (waiter threads race, prune runs per-exit).
        assert _wait_until(lambda: not reg.has(shells[0].shell_id))
        assert reg.has(shells[1].shell_id)
        assert reg.has(shells[2].shell_id)
        with pytest.raises(UnknownShellError):
            reg.read(shells[0].shell_id)
    finally:
        reg.close()


def test_catastrophic_filter_times_out_without_consuming(registry):
    """A backtracking-bomb filter must error within the bound and consume
    NOTHING — the retry without a filter still gets the output.  The match
    runs in a killable child process: sre holds the GIL, so an in-process
    bomb would freeze the whole interpreter, watchdogs included."""
    # One ~3000-char line of a's ending in 'b' — the classic (a+)+$ bomb
    # subject — followed by a sentinel line.
    shell = registry.spawn("printf 'a%.0s' $(seq 1 3000); echo b; echo tail-line")
    assert _wait_status(shell, "completed")
    start = time.monotonic()
    with pytest.raises(FilterTimeoutError):
        registry.read(shell.shell_id, filter_pattern=r"(a+)+$")
    assert time.monotonic() - start < 10, "filter timeout must be bounded"
    # Nothing was consumed: an unfiltered read sees the whole delta.
    read = registry.read(shell.shell_id)
    assert any("tail-line" in ln for ln in read.lines)


def test_overlong_filter_pattern_is_rejected(registry):
    shell = registry.spawn("echo hi")
    assert _wait_status(shell, "completed")
    with pytest.raises(re.error):
        registry.read(shell.shell_id, filter_pattern="x" * 600)


def test_cap_error_is_owner_scope_honest():
    """The cap is registry-wide, but the advice must only name shells the
    caller can actually kill — kill_shell is owner-scoped."""
    reg = BackgroundShellRegistry(max_shells=1)
    try:
        reg.spawn("sleep 30")  # main scope fills the cap
        with pytest.raises(TooManyShellsError) as excinfo:
            reg.spawn("sleep 30", owner="agent-1")
        msg = str(excinfo.value)
        assert "bash_1" not in msg, "must not advise killing another scope's shell"
        assert "other agents" in msg
        # The same-scope variant names the killable shell.
        with pytest.raises(TooManyShellsError) as excinfo2:
            reg.spawn("sleep 30")
        assert "bash_1" in str(excinfo2.value)
        assert "kill_shell" in str(excinfo2.value)
    finally:
        reg.close()


def test_prune_evicts_by_exit_order_not_spawn_order():
    """A long-lived first-spawned server must never be evicted by its OWN
    exit's prune once enough later jobs have finished — eviction follows
    exit order, so the just-exited shell is always the newest record."""
    reg = BackgroundShellRegistry(max_exited_records=2)
    try:
        server = reg.spawn("sleep 30")  # bash_1, exits LAST
        jobs = [reg.spawn(f"echo job-{i}") for i in range(3)]
        for job in jobs:
            assert _wait_status(job, "completed")
        reg.kill(server.shell_id)
        assert reg.has(server.shell_id), "the just-exited shell must survive its own exit's prune"
        # The earliest-EXITED job is the eviction victim, not bash_1.
        assert _wait_until(lambda: len(reg.shells()) <= 3)
        assert reg.read(server.shell_id).status == "killed"
    finally:
        reg.close()


def test_thread_start_failure_leaves_no_orphan_record(registry, monkeypatch, tmp_path):
    """If Thread.start raises (thread exhaustion), the record must be
    unregistered and the fresh group reaped — an orphan with never-started
    Thread objects would make every later close()/reap() join raise and
    abort session teardown."""
    import turnstone.core.background_shells as bg_mod

    pidfile = tmp_path / "leader.pid"
    real_thread = bg_mod.threading.Thread

    class FailingWaiterThread(real_thread):
        def start(self):
            if "bg-shell-wait" in (self.name or ""):
                raise RuntimeError("can't start new thread")
            super().start()

    monkeypatch.setattr(bg_mod.threading, "Thread", FailingWaiterThread)
    with pytest.raises(RuntimeError):
        registry.spawn(f"echo $$ > {pidfile}; sleep 60")
    assert registry.shells() == [], "failed spawn must not strand a record"
    if pidfile.exists():
        leader_pid = int(pidfile.read_text().strip())
        assert _wait_until(lambda: not _pid_alive(leader_pid)), "fresh group leaked"
    monkeypatch.undo()
    registry.close()  # must not raise on the (empty) registry


def test_filter_helper_failure_reports_exec_error_not_timeout(registry, monkeypatch):
    """A crashed helper must not tell the model its (fine) pattern was too
    slow — and must not consume the delta."""
    import turnstone.core.background_shells as bg_mod
    from turnstone.core.background_shells import FilterExecError

    shell = registry.spawn("echo hello")
    assert _wait_status(shell, "completed")
    monkeypatch.setattr(bg_mod.sys, "executable", "/bin/false")
    with pytest.raises(FilterExecError) as excinfo:
        registry.read(shell.shell_id, filter_pattern="hello")
    assert "not a problem with your pattern" in str(excinfo.value)
    monkeypatch.undo()
    read = registry.read(shell.shell_id)
    assert [ln.strip() for ln in read.lines] == ["hello"]


def test_filter_matches_only_within_line_cap_and_reports_clipping(registry):
    """Lines are truncated parent-side before shipping to the helper: a
    match beyond the per-line cap is not found (a filter targets log
    lines), and a huge retained line cannot burn the time budget on I/O.
    The clipping is NEVER silent — the read reports how many lines were
    only partially visible to the pattern."""
    shell = registry.spawn("printf 'x%.0s' $(seq 1 5000); echo needle-suffix")
    assert _wait_status(shell, "completed")
    read = registry.read(shell.shell_id, filter_pattern="needle")
    assert read.lines == []
    assert read.new_line_count == 1
    assert read.clipped_lines == 1


def test_concurrent_reads_never_double_deliver(registry):
    """Two simultaneous reads of one shell must SPLIT the delta between
    them, never both return it — the whole pass (snapshot → commit)
    serializes per shell.  Without that, a parallel tool batch reading the
    same handle gets every line twice."""
    shell = registry.spawn("seq 1 200")
    assert _wait_status(shell, "completed")
    results: list[list[str]] = [[], []]
    barrier = threading.Barrier(2)

    def _reader(slot: int) -> None:
        barrier.wait()
        results[slot] = [ln.strip() for ln in registry.read(shell.shell_id).lines]

    threads = [threading.Thread(target=_reader, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    combined = results[0] + results[1]
    assert len(combined) == 200, f"expected each line exactly once, got {len(combined)}"
    assert sorted(combined, key=int) == [str(i) for i in range(1, 201)]


def test_filter_helper_spawn_failure_is_exec_error(registry, monkeypatch):
    """A helper that fails to LAUNCH (fork pressure) must land in the same
    honest FilterExecError as a crashed helper — not escape as a raw
    OSError blaming nothing — and must not consume the delta."""
    import turnstone.core.background_shells as bg_mod
    from turnstone.core.background_shells import FilterExecError

    shell = registry.spawn("echo hello")
    assert _wait_status(shell, "completed")

    def _boom(*args, **kwargs):
        raise BlockingIOError("Resource temporarily unavailable")

    monkeypatch.setattr(bg_mod.subprocess, "Popen", _boom)
    with pytest.raises(FilterExecError):
        registry.read(shell.shell_id, filter_pattern="hello")
    monkeypatch.undo()
    read = registry.read(shell.shell_id)
    assert [ln.strip() for ln in read.lines] == ["hello"]


def test_on_exit_exception_does_not_wedge_the_shell():
    def _boom(shell):
        raise RuntimeError("callback bug")

    reg = BackgroundShellRegistry(on_exit=_boom)
    try:
        shell = reg.spawn("echo hi")
        # The waiter thread must survive the callback raising: status still
        # lands and output is still readable.
        assert _wait_status(shell, "completed")
        assert [ln.strip() for ln in reg.read(shell.shell_id).lines] == ["hi"]
    finally:
        reg.close()
