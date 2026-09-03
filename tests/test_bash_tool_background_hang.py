"""Regression tests for the bash tool hanging on a backgrounded child.

A bash command that backgrounds a long-lived process (``server &``,
``python -m http.server &``, any daemon) used to wedge the whole workstream
forever: the child inherits the tool's stdout/stderr pipe, so the foreground
read never hit EOF, and the timeout watchdog bailed the moment the tracked
``bash`` exited.  ``_exec_bash`` now waits on the tracked process (not pipe
EOF) bounded by ``tool_timeout`` and kills the whole session group on exit, so
the call always returns and never leaks the background child.
"""

import io
import threading
import time

from tests._proc_helpers import kill_pid as _kill_pid
from tests._proc_helpers import pid_alive as _pid_alive
from tests._session_helpers import NullUI, make_session
from turnstone.core.background_shells import (
    _DRAIN_CHUNK_CHARS,
    drain_pipe_lines,
    drain_pipe_logical_lines,
)
from turnstone.core.trajectory import EffectStatus
from turnstone.core.truncation import BoundedTextBuffer, ProjectedText


def _run_in_thread(fn, timeout):
    """Run ``fn`` in a daemon thread; return ``(finished, result)``."""
    box = {}

    def _target():
        box["result"] = fn()

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout)
    return (not t.is_alive()), box.get("result")


def test_drain_bounds_a_single_line_before_callback() -> None:
    source = "x" * (_DRAIN_CHUNK_CHARS * 3 + 17)
    chunks: list[str] = []

    drain_pipe_lines(io.StringIO(source), chunks.append)

    assert "".join(chunks) == source
    assert len(chunks) == 4
    assert max(map(len, chunks)) <= _DRAIN_CHUNK_CHARS


def test_logical_line_drain_delivers_whole_lines_across_chunks() -> None:
    long_line = "e" * (_DRAIN_CHUNK_CHARS * 2 + 5)
    source = f"{long_line}\nshort\n"
    lines: list[str] = []

    drain_pipe_logical_lines(io.StringIO(source), lines.append, max_line_chars=len(source))

    assert lines == [f"{long_line}\n", "short\n"]


def test_logical_line_drain_bounds_a_line_that_fits_one_chunk() -> None:
    """The whole-line fast path honors the bound too: a line within it passes
    verbatim, a longer one is the same bounded record a spanning line gets."""
    lines: list[str] = []

    drain_pipe_logical_lines(io.StringIO("bb\nccccc\n"), lines.append, max_line_chars=3)

    assert lines[0] == "bb\n"
    assert len(lines[1]) <= 3


def test_logical_line_drain_keeps_an_over_long_line_one_record() -> None:
    """An over-long line is bounded with a marker that carries no newline, so
    the record is still one line for the cursor, the filter, and the prefix."""
    lines: list[str] = []

    drain_pipe_logical_lines(
        io.StringIO("x" * 1000 + "\nshort\n"), lines.append, max_line_chars=200
    )

    assert len(lines) == 2 and lines[1] == "short\n"
    record = lines[0]
    assert len(record) <= 200
    assert record.count("\n") == 1 and record.endswith("\n")
    assert "chars truncated — line exceeded 200 char limit" in record


def test_bounded_buffer_snapshots_never_lose_or_reorder_chunks() -> None:
    """Snapshots race the producer: every chunk appended before a snapshot is
    in it, and the final capture is the producer's exact stream."""
    buffer = BoundedTextBuffer(1 << 20)
    total = 100_000

    def produce() -> None:
        for i in range(total):
            buffer.append(f"{i}\n")

    producer = threading.Thread(target=produce)
    producer.start()
    seen = 0
    while producer.is_alive():
        snapshot = buffer.source()
        assert snapshot.original_chars >= seen
        seen = snapshot.original_chars
    producer.join()

    final = buffer.source()
    expected = "".join(f"{i}\n" for i in range(total))
    assert final.original_chars == len(expected)
    assert final.prefix == expected


def test_executor_capture_retention_is_half_the_session_cap_or_the_agent_window() -> None:
    """Each edge retains half the session cap, rounded up, so the two edges
    always cover the cap and a capture never holds more than a fold on that
    session could show; and never less than a task agent's guard window."""
    from turnstone.core.session import _AGENT_GUARD_WINDOW_CHARS

    session = make_session()
    session.tool_truncation = 100_001
    assert session._executor_capture_chars() == 50_001

    manual = make_session(tool_truncation=1000)
    assert manual._executor_capture_chars() == _AGENT_GUARD_WINDOW_CHARS


def test_foreground_stderr_long_line_gets_one_prefix():
    """A stderr line spanning several drain chunks is one logical line and
    therefore carries exactly one ``[stderr]`` prefix in the captured output."""
    session = make_session(tool_timeout=30)
    session.tool_truncation = 1 << 20
    width = _DRAIN_CHUNK_CHARS * 2 + 5
    command = f"head -c {width} /dev/zero | tr '\\0' e >&2; echo >&2; echo ok"

    finished, result = _run_in_thread(
        lambda: session._exec_bash({"call_id": "c1", "command": command}),
        timeout=15,
    )
    assert finished, "_exec_bash did not return"
    assert result is not None
    _, output = result
    assert output.count("[stderr]") == 1
    assert "e" * width in output
    assert "ok" in output


def test_bounded_bash_capture_carries_true_size_through_a_smaller_fold_cut():
    """Output beyond the executor retention keeps its edges and true size, so a
    later, smaller fold cut reports omission against the command's real output."""
    session = make_session(tool_timeout=30)
    session.tool_truncation = 600
    command = "head -c 100000 /dev/zero | tr '\\0' x"

    finished, result = _run_in_thread(
        lambda: session._exec_bash({"call_id": "c1", "command": command}),
        timeout=15,
    )
    assert finished, "_exec_bash did not return"
    assert result is not None
    _, output = result
    assert isinstance(output, ProjectedText)
    assert output.source.original_chars == 100_000
    fold = session._truncate_output_result(output, maximum_chars=150)
    assert len(fold.text) <= 150
    assert fold.original_chars == 100_000
    assert f"[{100_000 - fold.retained_chars} chars truncated" in fold.text


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
    ``_exec_bash`` returns (the ``capture_closed`` gate).  Without the gate,
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
            "the tool returned — the capture_closed gate is not holding"
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


def test_foreground_stderr_beyond_retention_keeps_its_true_size():
    """stderr is bounded by the same executor retention as stdout rather than
    per line: one over-long stderr line reaches the fold with its edges, one
    prefix, and the command's real size, so the fold's marker is honest."""
    session = make_session(tool_timeout=30)
    session.tool_truncation = 600
    command = "head -c 100000 /dev/zero | tr '\\0' e >&2"

    finished, result = _run_in_thread(
        lambda: session._exec_bash({"call_id": "c1", "command": command}),
        timeout=15,
    )
    assert finished, "_exec_bash did not return"
    assert result is not None
    _, output = result
    assert isinstance(output, ProjectedText)
    assert output.source.original_chars == 100_000 + len("[stderr] ")
    assert output.count("[stderr]") == 1
    fold = session._truncate_output_result(output, maximum_chars=150)
    assert fold.original_chars == 100_000 + len("[stderr] ")
    assert f"[{fold.original_chars - fold.retained_chars} chars truncated" in fold.text


def test_stderr_never_lands_inside_a_long_stdout_line():
    """A stdout line longer than one drain chunk must reach the model intact.
    stderr is presented after stdout, never spliced into it by arrival order."""
    session = make_session(tool_timeout=30)
    session.tool_truncation = 1 << 20
    width = _DRAIN_CHUNK_CHARS * 3 + 11
    command = f"head -c {width} /dev/zero | tr '\\0' x; echo; echo warn >&2; echo tail"

    finished, result = _run_in_thread(
        lambda: session._exec_bash({"call_id": "c1", "command": command}),
        timeout=15,
    )
    assert finished, "_exec_bash did not return"
    assert result is not None
    _, output = result
    assert "x" * width + "\n" in output
    assert output.index("tail") < output.index("[stderr] warn")
    assert output.count("[stderr]") == 1


def test_exit_code_note_keeps_a_lossy_capture_rerenderable():
    """The exit-code note is appended to the projection, not to a flattened
    string, so the fold still renders the failure from the true edges."""
    session = make_session(tool_timeout=30)
    session.tool_truncation = 600
    command = "head -c 100000 /dev/zero | tr '\\0' x; exit 3"

    finished, result = _run_in_thread(
        lambda: session._exec_bash({"call_id": "c1", "command": command}),
        timeout=15,
    )
    assert finished, "_exec_bash did not return"
    assert result is not None
    _, output = result
    assert isinstance(output, ProjectedText)
    assert output.endswith("[exit code: 3]")
    fold = session._truncate_output_result(output, maximum_chars=150)
    assert fold.text.endswith("[exit code: 3]")
    assert fold.original_chars == 100_000 + len("\n[exit code: 3]")
