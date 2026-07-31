"""Regression tests for the bash tool hanging on a backgrounded child.

A bash command that backgrounds a long-lived process (``server &``,
``python -m http.server &``, any daemon) used to wedge the whole workstream
forever: the child inherits the tool's stdout/stderr pipe, so the foreground
read never hit EOF, and the timeout watchdog bailed the moment the tracked
``bash`` exited.  ``_exec_bash`` now waits on the tracked process (not pipe
EOF) bounded by ``tool_timeout`` and kills the whole session group on exit, so
the call always returns and never leaks the background child.
"""

import threading
import time

from tests._proc_helpers import kill_pid as _kill_pid
from tests._proc_helpers import pid_alive as _pid_alive
from tests._session_helpers import NullUI, make_session
from turnstone.core.trajectory import EffectStatus


def _run_in_thread(fn, timeout):
    """Run ``fn`` in a daemon thread; return ``(finished, result)``."""
    box = {}

    def _target():
        box["result"] = fn()

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout)
    return (not t.is_alive()), box.get("result")


def test_backgrounded_child_does_not_hang_and_is_reaped(tmp_path):
    """Foreground exits immediately but leaves ``sleep 60 &`` holding the pipe.

    Old behaviour: infinite hang (EOF never arrives, watchdog bails once the
    tracked bash exits).  New behaviour: returns promptly and the background
    child is reaped by the session-group kill.
    """
    pidfile = str(tmp_path / "bg.pid")
    # A generous tool_timeout proves the return comes from foreground-exit, not
    # from the deadline firing.
    session = make_session(tool_timeout=30)
    command = f"sleep 60 & echo $! > {pidfile}; echo done"
    bg_pid = None
    try:
        finished, result = _run_in_thread(
            lambda: session._exec_bash({"call_id": "c1", "command": command}),
            timeout=15,
        )
        assert finished, "_exec_bash hung on a backgrounded child"
        assert result is not None
        call_id, output = result
        assert call_id == "c1"
        assert "done" in output

        # The backgrounded process must have been reaped by the group kill.
        with open(pidfile) as f:
            bg_pid = int(f.read().strip())
        deadline = time.monotonic() + 5
        while _pid_alive(bg_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _pid_alive(bg_pid), f"backgrounded child {bg_pid} leaked"
    finally:
        if bg_pid is not None:
            _kill_pid(bg_pid)


def test_timeout_still_fires_with_backgrounded_child():
    """A silent foreground command plus a backgrounded child still hits the
    deadline: the watchdog kills the whole group and the result reads UNKNOWN
    (the ``unknown, never none`` timeout discipline)."""
    session = make_session(tool_timeout=1)
    command = "sleep 60 & sleep 60"

    finished, result = _run_in_thread(
        lambda: session._exec_bash({"call_id": "c1", "command": command}),
        timeout=10,
    )
    assert finished, "_exec_bash did not return at its deadline"
    assert result is not None
    call_id, output = result
    assert call_id == "c1"
    assert "timed out" in output.lower()
    assert "UNKNOWN" in output
    assert session._tool_status.get("c1") is EffectStatus.UNKNOWN


def test_undecodable_output_is_preserved_not_swallowed():
    """Undecodable bytes on stdout must not silently vanish.

    The drain's broad ``except (ValueError, OSError)`` would otherwise catch the
    ``UnicodeDecodeError`` (a ``ValueError``) and kill the thread before any line
    was yielded — dropping ALL output and reporting a clean success.  ``Popen``
    now decodes with ``errors="replace"`` so output always survives.
    """
    session = make_session(tool_timeout=30)
    # Valid lines bracketing a raw invalid-UTF-8 byte sequence.
    command = r"printf 'before\n'; printf '\xff\xfe'; printf 'after\n'"
    finished, result = _run_in_thread(
        lambda: session._exec_bash({"call_id": "c1", "command": command}),
        timeout=15,
    )
    assert finished
    assert result is not None
    _call_id, output = result
    assert output != "(no output)"
    assert "before" in output
    assert "after" in output


def test_stdout_streams_to_ui_from_drain_thread():
    """stdout chunks are now emitted from the drain thread; they must still reach
    ``on_tool_output_chunk``."""
    chunks: list[str] = []

    class RecordingUI(NullUI):
        def on_tool_output_chunk(self, call_id, chunk):
            chunks.append(chunk)

    session = make_session(tool_timeout=30, ui=RecordingUI())
    finished, result = _run_in_thread(
        lambda: session._exec_bash({"call_id": "c1", "command": "echo streamed-line"}),
        timeout=15,
    )
    assert finished
    assert any("streamed-line" in c for c in chunks)


def test_leaked_drain_stops_emitting_chunks_after_return(tmp_path):
    """A double-``setsid`` grandchild escapes the session-group kill and
    holds the stdout pipe open past the drain join (``bash.drain_leaked``)
    — the leaked drain thread must STOP forwarding chunks to the UI once
    ``_exec_bash`` returns (the ``emit_done`` gate).  Without the gate,
    its lines land in later turns' panes: the UI batches per call_id,
    some providers reuse call_ids across turns, and the client grafts
    stray chunks under the completed row.

    The grandchild's writer loop is time-bounded (~8s) so even a failed
    cleanup cannot outlive the test session, and the finally kills it so
    the drain thread hits EOF before the leaked-thread guard sweeps.
    """
    pidfile = str(tmp_path / "leak.pid")
    chunks: list[str] = []

    class RecordingUI(NullUI):
        def on_tool_output_chunk(self, call_id, chunk):
            chunks.append(chunk)

    session = make_session(tool_timeout=30, ui=RecordingUI())
    # setsid detaches the grandchild from the session group (killpg
    # misses it); it inherits our stdout pipe and keeps writing.
    command = (
        f"setsid bash -c 'echo $$ > {pidfile}; "
        "for i in $(seq 1 80); do echo leak-$i; sleep 0.1; done' & echo fg-done"
    )
    leak_pid = None
    try:
        finished, result = _run_in_thread(
            lambda: session._exec_bash({"call_id": "c1", "command": command}),
            timeout=20,
        )
        assert finished, "_exec_bash hung on the escaped grandchild"
        assert result is not None
        _call_id, output = result
        assert "fg-done" in output
        # The call has RETURNED (gate set).  The grandchild is still
        # writing; give its lines time to traverse the leaked drain.
        seen_at_return = len(chunks)
        time.sleep(1.0)
        # ``<= +1``: the gate documents a one-line residual race (a line
        # already past the is_set() check when the event sets).  A broken
        # gate keeps forwarding ~10 lines/sec and still fails loudly.
        assert len(chunks) <= seen_at_return + 1, (
            "leaked drain thread kept forwarding chunks to the UI after "
            "the tool returned — the emit_done gate is not holding"
        )
    finally:
        deadline = time.monotonic() + 5
        while leak_pid is None and time.monotonic() < deadline:
            try:
                with open(pidfile) as f:
                    leak_pid = int(f.read().strip())
            except (FileNotFoundError, ValueError):
                time.sleep(0.05)
        if leak_pid is not None:
            _kill_pid(leak_pid)
            # Let the drain thread hit EOF before the leaked-thread
            # guard sweeps the test's thread table.
            deadline = time.monotonic() + 5
            while _pid_alive(leak_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(0.2)


def test_cancel_midbash_reports_unknown():
    """An external ``cancel()`` during a running bash unblocks the process-bounded
    wait and reports UNKNOWN (unknown-never-none), not a clean result."""
    session = make_session(tool_timeout=30)

    def _cancel_soon():
        time.sleep(0.5)
        session.cancel()

    threading.Thread(target=_cancel_soon, daemon=True).start()
    finished, result = _run_in_thread(
        lambda: session._exec_bash({"call_id": "c1", "command": "sleep 30"}),
        timeout=15,
    )
    assert finished, "cancel did not unblock _exec_bash"
    assert result is not None
    _call_id, output = result
    assert "cancelled" in output.lower()
    assert session._tool_status.get("c1") is EffectStatus.UNKNOWN


def test_popen_failure_reports_cleanly(monkeypatch):
    """If ``Popen`` itself raises, the ``finally`` must not mask the real error
    with ``UnboundLocalError`` — ``proc`` is pre-bound to ``None``."""
    from turnstone.core import session as session_mod

    session = make_session(tool_timeout=30)

    def _boom(*args, **kwargs):
        raise OSError("cannot fork")

    monkeypatch.setattr(session_mod.subprocess, "Popen", _boom)
    call_id, output = session._exec_bash({"call_id": "c1", "command": "echo hi"})
    assert call_id == "c1"
    assert "cannot fork" in output
