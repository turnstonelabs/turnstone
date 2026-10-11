"""A background CLI workstream keeps what it writes until the user brings it forward.

The REPL runs each turn inline on the main thread, so the workstream in front is
the one running, yet output still reaches one in the background: a judge verdict
that lands after a ``/ws`` switch, or the last output of a call an interrupted
batch left running.  It waits in the workstream's buffer instead of
printing over the workstream in front, and replays on the stream it was meant
for.
"""

from __future__ import annotations

import inspect
import re
import threading
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from turnstone.cli import TerminalUI, WorkstreamTerminalUI, _close_all_sessions
from turnstone.core.adapters._ui_cleanup import cleanup_session_ui

if TYPE_CHECKING:
    from collections.abc import Callable

# Each terminal writer: a call that makes it write, a fragment of what it writes,
# and the stream that fragment goes to (in front and on replay).
_WRITERS: dict[str, tuple[Callable[[WorkstreamTerminalUI], Any], str, str]] = {
    # A complete line renders as its tokens arrive; the rest at the stream's end.
    "reply": (
        lambda ui: (
            ui.on_content_token("All tests "),
            ui.on_content_token("pass.\nDone"),
            ui.on_stream_end(),
        ),
        "All tests pass.",
        "out",
    ),
    "info": (lambda ui: ui.on_info("Context compacted."), "Context compacted.", "out"),
    "error": (lambda ui: ui.on_error("Provider timed out."), "Provider timed out.", "out"),
    "reasoning": (lambda ui: ui.on_reasoning_token("weighing the options"), "weighing", "out"),
    "tool_error": (
        lambda ui: ui.on_tool_result("c1", "bash", "exit status 2", is_error=True),
        "bash: exit status 2",
        "err",
    ),
    "task_agent_step": (
        lambda ui: ui.on_agent_step("t1", {"header": "read_file notes.md"}),
        "- read_file notes.md",
        "out",
    ),
    "output_warning": (
        lambda ui: ui.on_output_warning(
            "c1", {"risk_level": "high", "flags": ["prompt_injection"], "annotations": ["line 3"]}
        ),
        "OUTPUT WARNING: HIGH",
        "out",
    ),
    "judge_verdict": (
        lambda ui: ui.on_intent_verdict(
            {
                "risk_level": "low",
                "recommendation": "approve",
                "intent_summary": "Lists the project files.",
                "confidence": 0.9,
                "tier": "llm",
            }
        ),
        "LLM VERDICT: LOW",
        "out",
    ),
    "operator_turn": (
        lambda ui: ui.on_system_turn("Background shell 1 exited.", "background_shell_exit"),
        "Background shell 1 exited.",
        "out",
    ),
}


def _terminal_ui(*, in_front: bool) -> tuple[WorkstreamTerminalUI, MagicMock]:
    manager = MagicMock()
    manager.active_id = "ws-1" if in_front else "another-workstream"
    ui = WorkstreamTerminalUI("ws-1", manager)
    ui.set_foreground(in_front)
    return ui, manager


@pytest.mark.parametrize("writer", sorted(_WRITERS))
def test_background_output_waits_until_brought_forward(capsys: Any, writer: str) -> None:
    write, fragment, stream = _WRITERS[writer]
    ui, manager = _terminal_ui(in_front=False)
    write(ui)
    assert capsys.readouterr() == ("", "")
    # As ``/ws <N>`` does.
    manager.active_id = "ws-1"
    ui.set_foreground(True)
    ui.flush_buffer()
    captured = capsys.readouterr()
    assert fragment in (captured.out if stream == "out" else captured.err)
    assert ui._output_buffer == []


@pytest.mark.parametrize("in_front", [True, False], ids=["in_front", "in_background"])
@pytest.mark.parametrize("writer", sorted(_WRITERS))
def test_a_write_never_waits_behind_an_open_prompt(
    capsys: Any, writer: str, in_front: bool
) -> None:
    """A prompt holds the print lock while it waits for an answer, a task
    agent's prompt included.  A write meanwhile, from a thread that may hold
    session locks (a sibling call's tool error, a judge verdict), does not wait
    for it: in front it prints, in the background it goes to the buffer."""
    ui, _manager = _terminal_ui(in_front=in_front)
    write, fragment, stream = _WRITERS[writer]
    written = threading.Event()

    def _deliver() -> None:
        write(ui)
        written.set()

    deliverer = threading.Thread(target=_deliver)
    with ui._print_lock:  # an open prompt
        deliverer.start()
        delivered_meanwhile = written.wait(2)
    deliverer.join(2)
    assert delivered_meanwhile
    if in_front:
        captured = capsys.readouterr()
        assert fragment in (captured.out if stream == "out" else captured.err)
        assert ui._output_buffer == []
    else:
        assert any(fragment in text for _stderr, text in ui._output_buffer)


def test_a_takeover_notice_prints_at_once_even_behind_an_open_prompt(capsys: Any) -> None:
    """Another process took the workstream over, and it leaves this CLI right
    after: the notice prints at once from the background, without waiting for
    a prompt that holds the print lock."""
    ui, manager = _terminal_ui(in_front=False)
    manager.get.return_value.name = "notes"
    manager.index_of.return_value = 2
    written = threading.Event()

    def _deliver() -> None:
        ui.on_workstream_stopped("Another process took this workstream over.")
        written.set()

    deliverer = threading.Thread(target=_deliver)
    with ui._print_lock:  # an open prompt
        deliverer.start()
        delivered_meanwhile = written.wait(2)
    deliverer.join(2)
    assert delivered_meanwhile
    assert "Workstream 2:notes: Another process took this workstream over." in (
        capsys.readouterr().out
    )


def _closing_workstream(ui: WorkstreamTerminalUI) -> MagicMock:
    ws = MagicMock()
    ws._lock = threading.Lock()
    ws.name = "notes"
    ws.ui = ui
    return ws


def test_a_workstream_closing_in_the_background_shows_what_it_kept(capsys: Any) -> None:
    """A background workstream that leaves without coming forward (a close, a
    lost lease, an eviction: each ends in ``cleanup_session_ui``) shows what it
    kept, under its name, once its session has closed."""
    ui, _manager = _terminal_ui(in_front=False)
    write, fragment, _stream = _WRITERS["judge_verdict"]
    write(ui)
    ws = _closing_workstream(ui)
    kept_when_the_session_closed: list[bool] = []
    ws.session.close.side_effect = lambda: kept_when_the_session_closed.append(
        bool(ui._output_buffer)
    )
    cleanup_session_ui(ws)
    out = capsys.readouterr().out
    assert fragment in out
    assert "notes closed; its buffered output" in out
    assert kept_when_the_session_closed == [True]
    assert ui._output_buffer == []


def test_a_workstream_whose_close_fails_still_shows_what_it_kept(capsys: Any) -> None:
    """When the session's close raises, the output the background workstream
    kept still shows as it leaves, since nothing would show it later; the
    close's error still reaches the caller."""
    ui, _manager = _terminal_ui(in_front=False)
    write, fragment, _stream = _WRITERS["judge_verdict"]
    write(ui)
    ws = _closing_workstream(ui)
    ws.session.close.side_effect = RuntimeError("listener removal failed")
    with pytest.raises(RuntimeError, match="listener removal failed"):
        cleanup_session_ui(ws)
    out = capsys.readouterr().out
    assert fragment in out
    assert "notes closed; its buffered output" in out


def test_exit_shows_what_a_background_workstream_kept(capsys: Any) -> None:
    """``/exit`` closes each session directly; a background workstream's kept
    output shows then too."""
    ui, _manager = _terminal_ui(in_front=False)
    write, fragment, _stream = _WRITERS["judge_verdict"]
    write(ui)
    ws = _closing_workstream(ui)
    manager = MagicMock()
    manager.list_all.return_value = [ws]
    _close_all_sessions(manager)
    out = capsys.readouterr().out
    ws.session.close.assert_called_once_with()
    assert fragment in out
    assert "notes closed; its buffered output" in out


def test_a_replay_holds_the_print_lock_throughout() -> None:
    """Every line of a replay is written under the print lock, so a gate the
    switch woke paints its card and prompt before or after the replay, never
    in the middle of it."""
    ui, manager = _terminal_ui(in_front=False)
    for writer in ("info", "tool_error"):
        _WRITERS[writer][0](ui)
    # As ``/ws <N>`` does.
    manager.active_id = "ws-1"
    ui.set_foreground(True)
    writes: list[tuple[bool, str]] = []
    with patch.object(
        ui,
        "_write_terminal",
        side_effect=lambda text, **_kw: writes.append((ui._print_lock.locked(), text)),
    ):
        ui.flush_buffer()
    assert all(locked for locked, _text in writes)
    for writer in ("info", "tool_error"):
        assert any(_WRITERS[writer][1] in text for _locked, text in writes)


def test_a_ctrl_c_during_exit_still_shows_what_each_workstream_kept(capsys: Any) -> None:
    """A Ctrl-C during /exit's closes skips the slow joins, not the replay:
    each background workstream, closed or interrupted, still shows what it
    kept."""
    write, fragment, _stream = _WRITERS["judge_verdict"]
    first, _first_manager = _terminal_ui(in_front=False)
    second, _second_manager = _terminal_ui(in_front=False)
    write(first)
    write(second)
    closed_cleanly, interrupted = _closing_workstream(first), _closing_workstream(second)
    interrupted.name = "drafts"
    interrupted.session.close.side_effect = KeyboardInterrupt
    manager = MagicMock()
    manager.list_all.return_value = [closed_cleanly, interrupted]
    _close_all_sessions(manager)  # must not raise
    out = capsys.readouterr().out
    shown = out.count(fragment)
    assert "notes closed; its buffered output" in out
    assert "drafts closed; its buffered output" in out
    assert shown == 2


def test_a_workstream_closing_behind_an_open_prompt_does_not_wait_for_it(capsys: Any) -> None:
    """A close can run on a lease keeper's thread while an approval prompt holds
    the print lock; showing what the workstream kept never waits for it."""
    ui, _manager = _terminal_ui(in_front=False)
    write, fragment, _stream = _WRITERS["judge_verdict"]
    write(ui)
    ws = _closing_workstream(ui)
    closed = threading.Event()

    def _close() -> None:
        cleanup_session_ui(ws)
        closed.set()

    closer = threading.Thread(target=_close)
    with ui._print_lock:  # an open prompt
        closer.start()
        closed_meanwhile = closed.wait(2)
    closer.join(2)
    assert closed_meanwhile
    assert fragment in capsys.readouterr().out


class _ReportingLock:
    """A lock that reports when a second thread starts waiting for it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.contended = threading.Event()

    def __enter__(self) -> _ReportingLock:
        if not self._lock.acquire(blocking=False):
            self.contended.set()
            self._lock.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._lock.release()


def test_a_write_racing_a_switch_lands_in_front_not_in_a_replayed_buffer(capsys: Any) -> None:
    """A write decides between the terminal and the buffer under the buffer
    lock.  When ``/ws <N>`` moves the front while the write waits for that
    lock, the write sees the new front and prints, instead of landing in a
    buffer the switch has already replayed."""
    ui, manager = _terminal_ui(in_front=False)
    ui._buffer_lock = lock = _ReportingLock()  # type: ignore[assignment]
    write, fragment, _stream = _WRITERS["judge_verdict"]
    writer = threading.Thread(target=write, args=(ui,))
    with lock:  # the switch's replay holds it
        writer.start()
        assert lock.contended.wait(2)  # the write now waits for the lock
        manager.active_id = "ws-1"
        ui.set_foreground(True)
    writer.join(2)
    assert fragment in capsys.readouterr().out
    assert ui._output_buffer == []


# A terminal write that bypasses both the buffer and ``_write_terminal``.
_RAW_WRITE = r"sys\.std(?:out|err)\.write|\bprint\(|\bSpinner\(|\.spinner\."
# A terminal write that bypasses the buffering ``_show``.
_DIRECT_WRITE = re.compile(_RAW_WRITE + r"|\binput\(|\._write_terminal\(")


def test_every_terminal_writer_buffers_in_the_background() -> None:
    """A TerminalUI method that writes to the terminal without ``_show`` must be
    one WorkstreamTerminalUI overrides, or a workstream in the background prints
    over the one in front.  ``approve_tools`` alone prompts, only once its
    workstream is in front (``_wait_for_foreground``), with the card that goes
    with the prompt written by ``_write_terminal``; nothing else in it writes to
    the terminal directly, so a settled batch's lines reach the buffer."""
    unbuffered = [
        name
        for name, member in vars(TerminalUI).items()
        if inspect.isfunction(member)
        and name != "approve_tools"
        and name not in vars(WorkstreamTerminalUI)
        and _DIRECT_WRITE.search(inspect.getsource(member))
    ]
    assert unbuffered == []
    assert re.search(_RAW_WRITE, inspect.getsource(TerminalUI.approve_tools)) is None
