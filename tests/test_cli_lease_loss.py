"""A CLI workstream another process takes over: the user is told, and the prompt moves on.

The manager retires a workstream whose lease another process took. Its notice
must reach the terminal while the workstream is still the one in front (a
notice buffered into a workstream that already left the prompt is never
shown), and the REPL then says where the next input goes.
"""

from __future__ import annotations

import queue
import threading
from typing import Any

from tests.test_session_manager import FakeStorage
from turnstone.cli import (
    WorkstreamTerminalUI,
    _bring_forward_after_background_close,
    _classify_repl_input,
)
from turnstone.core.adapters.interactive_adapter import InteractiveAdapter
from turnstone.core.session import ChatSession
from turnstone.core.session_manager import SessionManager

NOTICE = "This copy of the workstream has stopped"


class _CliSession:
    """Just enough session for the CLI manager, with the real lease-loss notice."""

    handle_workstream_lease_lost = ChatSession.handle_workstream_lease_lost
    _close_publication_locked = ChatSession._close_publication_locked

    def __init__(self, ui: Any, ws_id: str, lease: Any) -> None:
        self.ui = ui
        self._ws_id = ws_id
        self._workstream_lease = lease
        self._generation_lock = threading.RLock()
        self._cancel_event = threading.Event()
        self._approval_cancel_epoch = 0
        self._workstream_lease_lost = False
        self._publication_shutdown = False
        self.closed = False

    def cancel(self) -> None:
        self._cancel_event.set()

    def close(self) -> None:
        self.closed = True


def _cli_manager(storage: FakeStorage) -> SessionManager:
    def factory(ui: Any, model: Any, ws_id: str, **kwargs: Any) -> _CliSession:
        return _CliSession(ui, ws_id, kwargs.get("workstream_lease"))

    adapter = InteractiveAdapter(
        global_queue=queue.Queue(maxsize=1),
        ui_factory=lambda ws: WorkstreamTerminalUI(ws.id, adapter.manager),
        session_factory=factory,
    )
    mgr = SessionManager(adapter, storage=storage, max_active=50)
    adapter.attach(mgr)
    return mgr


def _taken_over(storage: FakeStorage, ws_id: str) -> None:
    """A second CLI on the same SQLite file resumes ``ws_id`` (SQLite takes leases over)."""
    storage.allow_live_takeover = True
    grant = storage.acquire_workstream_lease(
        ws_id,
        incarnation_token=storage.fork_reservations[ws_id],
        holder="other-process/1",
        node_id=None,
        ttl_seconds=30.0,
    )
    assert grant is not None and grant.took_over_live


def test_the_notice_reaches_the_terminal_before_the_workstream_leaves(capsys: Any) -> None:
    storage = FakeStorage()
    mgr = _cli_manager(storage)
    ws = mgr.create(user_id="")
    _taken_over(storage, ws.id)
    capsys.readouterr()

    mgr.renew_leases_once()

    assert NOTICE in capsys.readouterr().out
    assert mgr.get(ws.id) is None
    assert mgr.active_id is None


def test_the_next_workstream_comes_forward_with_its_buffered_output(capsys: Any) -> None:
    storage = FakeStorage()
    mgr = _cli_manager(storage)
    first = mgr.create(user_id="")
    second = mgr.create(user_id="")
    assert mgr.active_id == first.id
    second.ui.on_info("finished in the background")
    _taken_over(storage, first.id)
    capsys.readouterr()

    mgr.renew_leases_once()
    _bring_forward_after_background_close(mgr)

    out = capsys.readouterr().out
    assert NOTICE in out
    assert mgr.active_id == second.id
    assert "finished in the background" in out
    assert f"Now in workstream {mgr.index_of(second.id)}:{second.name}" in out


def test_a_background_tab_taken_over_says_so(capsys: Any) -> None:
    storage = FakeStorage()
    mgr = _cli_manager(storage)
    first = mgr.create(user_id="")
    second = mgr.create(user_id="")
    assert mgr.active_id == first.id
    label = f"{mgr.index_of(second.id)}:{second.name}"
    _taken_over(storage, second.id)
    capsys.readouterr()

    mgr.renew_leases_once()

    out = capsys.readouterr().out
    assert f"Workstream {label}: {NOTICE}" in out
    assert mgr.get(second.id) is None and mgr.active_id == first.id


def test_with_no_workstream_left_the_cli_says_how_to_go_on(capsys: Any) -> None:
    storage = FakeStorage()
    mgr = _cli_manager(storage)
    ws = mgr.create(user_id="")
    _taken_over(storage, ws.id)
    mgr.renew_leases_once()
    capsys.readouterr()

    _bring_forward_after_background_close(mgr)

    out = capsys.readouterr().out
    assert "No workstream is open here any more" in out
    assert "/exit" in out


def test_input_typed_for_a_workstream_taken_over_meanwhile_is_not_sent() -> None:
    storage = FakeStorage()
    mgr = _cli_manager(storage)
    first = mgr.create(user_id="")
    second = mgr.create(user_id="")
    prompt_active = mgr.active_id
    assert prompt_active == first.id
    _taken_over(storage, first.id)

    mgr.renew_leases_once()

    assert mgr.active_id == second.id
    assert _classify_repl_input(mgr, "hello", prompt_active) == ("stale", None)
    assert _classify_repl_input(mgr, "hello", mgr.active_id) == ("dispatch", second)
    # A /ws command's implicit target and indices moved too.
    for line in ("/ws close", "/ws close 2", "/ws rename x", "/ws 1"):
        assert _classify_repl_input(mgr, line, prompt_active) == ("stale", None)
    assert _classify_repl_input(mgr, "/ws close", mgr.active_id) == ("ws", None)


def test_exit_works_in_any_case_with_nothing_open() -> None:
    mgr = _cli_manager(FakeStorage())

    for line in ("/exit", "/EXIT", "/Quit now", "/q"):
        assert _classify_repl_input(mgr, line, None) == ("exit", None)
    assert _classify_repl_input(mgr, "   ", None) == ("empty", None)
    assert _classify_repl_input(mgr, "/ws new", None) == ("ws", None)
    assert _classify_repl_input(mgr, "/cluster nodes", None) == ("cluster", None)
    assert _classify_repl_input(mgr, "hello", None) == ("no_workstream", None)
