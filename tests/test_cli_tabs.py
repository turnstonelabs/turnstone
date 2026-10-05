"""CLI ``/resume`` and ``/new`` open tabs (#988).

No session changes its workstream in place. ``/resume`` opens the saved
workstream in a tab of its own, or switches to the tab that already has it;
``/new`` starts a workstream with the current tab's settings. The tab left
closes once closing it loses nothing: one with unsaved messages closes by
itself once they are saved, and one whose background programs still run stays
until the user closes it.

These drive the CLI's own manager wiring (``_build_cli_manager``: terminal UIs,
the default-model fallback) with real sessions on the selected storage backend.
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests._session_helpers import make_session
from tests.test_skills import _create_template, _sys_content
from turnstone.cli import (
    _build_cli_manager,
    _close_parked_tabs,
    _handle_tab_command,
    _handle_ws_command,
    _start_persistence_retries,
)
from turnstone.core.personas import PersonaSnapshot
from turnstone.core.session import ChatSession

KNOWN_ALIASES = frozenset({"default", "other"})


@pytest.fixture
def make_cli(storage_backend: Any) -> Any:
    """Build CLI managers; every workstream they hold is closed at teardown."""
    managers: list[Any] = []

    def build(*, startup_skill: str | None = None) -> Any:
        def session_factory(
            ui: Any,
            model_alias: str | None = None,
            ws_id: str | None = None,
            *,
            skill: str | None = None,
            persona_snapshot: PersonaSnapshot | None = None,
            fork_reservation_token: str = "",
            workstream_lease: Any = None,
            **_host_options: Any,
        ) -> ChatSession:
            alias = model_alias or "default"
            if alias not in KNOWN_ALIASES:
                # The CLI's factory binds the alias through its model
                # registry, which raises for an alias it does not define.
                raise ValueError(f"unknown model alias {alias!r}")
            return make_session(
                ui=ui,
                ws_id=ws_id,
                model_alias=alias,
                # As the CLI does: a skill named at startup applies to
                # every workstream it creates.
                skill=skill or startup_skill,
                persona_snapshot=persona_snapshot,
                fork_reservation_token=fork_reservation_token,
                workstream_lease=workstream_lease,
            )

        manager = _build_cli_manager(session_factory, KNOWN_ALIASES.__contains__)
        managers.append(manager)
        return manager

    yield build
    for manager in managers:
        for ws in manager.list_all():
            manager.close(ws.id)


def _saved(storage: Any, ws_id: str, *messages: str, config: dict[str, str] | None = None) -> str:
    """A saved workstream nobody has open."""
    assert storage.register_workstream(ws_id, fork_reservation_token=f"tok-{ws_id}") is True
    for text in messages:
        storage.save_message(ws_id, "user", text)
    if config is not None:
        storage.save_workstream_config(ws_id, config)
    return ws_id


def _texts(ws: Any) -> list[str]:
    return [turn.text for turn in ws.session.messages]


# -- /resume --------------------------------------------------------------------


def test_resume_opens_the_workstream_in_a_tab_and_closes_the_one_left(
    make_cli: Any, storage_backend: Any, capsys: Any
) -> None:
    mgr = make_cli()
    left = mgr.create(user_id="")
    target = _saved(storage_backend, "b" * 32, "earlier")
    parked: dict[str, str] = {}

    _handle_tab_command(mgr, f"/resume {target}", left, False, parked)

    opened = mgr.get(target)
    assert opened is not None and mgr.active_id == target
    assert opened.session.ws_id == target
    assert _texts(opened) == ["earlier"]
    assert mgr.get(left.id) is None and parked == {}
    assert "(1 messages loaded)" in capsys.readouterr().out


@pytest.mark.parametrize("separator", ["\t", chr(0xA0), "  "], ids=["tab", "nbsp", "spaces"])
def test_resume_names_its_target_after_any_whitespace(
    make_cli: Any, storage_backend: Any, separator: str
) -> None:
    """A pasted tab or non-breaking space after /resume still resumes, never starts /new."""
    mgr = make_cli()
    left = mgr.create(user_id="")
    target = _saved(storage_backend, "f" * 32, "earlier")

    _handle_tab_command(mgr, f"/resume{separator}{target}", left, False, {})

    assert mgr.active_id == target
    assert [ws.id for ws in mgr.list_all()] == [target]


def test_resume_of_a_workstream_another_tab_has_switches_there(make_cli: Any, capsys: Any) -> None:
    mgr = make_cli()
    first = mgr.create(user_id="")
    first.ui.auto_approve_tools.add("bash")
    second = mgr.create(user_id="")
    second_session = second.session
    assert mgr.active_id == first.id

    _handle_tab_command(mgr, f"/resume {second.id}", first, False, {})

    assert mgr.active_id == second.id
    assert mgr.get(second.id).session is second_session  # the same tab, not a reopen
    # That tab keeps its own approvals: only a tab opened here takes the left one's.
    assert "bash" not in second.ui.auto_approve_tools
    assert mgr.get(first.id) is None
    assert "Resumed" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("line", "message"),
    [("/resume", "Usage: /resume"), ("/resume nope", "Workstream not found: nope")],
)
def test_resume_without_a_target_stays_put(
    make_cli: Any, capsys: Any, line: str, message: str
) -> None:
    mgr = make_cli()
    left = mgr.create(user_id="")

    _handle_tab_command(mgr, line, left, False, {})

    assert mgr.active_id == left.id and [ws.id for ws in mgr.list_all()] == [left.id]
    assert message in capsys.readouterr().out


def test_resume_of_the_current_workstream_stays_put(make_cli: Any, capsys: Any) -> None:
    mgr = make_cli()
    left = mgr.create(user_id="")

    _handle_tab_command(mgr, f"/resume {left.id}", left, False, {})

    assert mgr.get(left.id) is left and mgr.active_id == left.id
    assert "Already in that workstream." in capsys.readouterr().out


def test_resume_of_a_workstream_saved_with_an_alias_this_cli_lacks_uses_the_default(
    make_cli: Any, storage_backend: Any
) -> None:
    mgr = make_cli()
    left = mgr.create(user_id="")
    target = _saved(
        storage_backend, "c" * 32, "earlier", config={"model_alias": "gone", "model": "gone-model"}
    )

    _handle_tab_command(mgr, f"/resume {target}", left, False, {})

    opened = mgr.get(target)
    assert opened is not None and mgr.active_id == target
    assert opened.session._model_alias == "default"
    assert _texts(opened) == ["earlier"]


def test_a_resumed_tab_keeps_the_approvals_of_the_tab_left(
    make_cli: Any, storage_backend: Any
) -> None:
    mgr = make_cli()
    left = mgr.create(user_id="")
    left.ui.auto_approve_tools.add("bash")
    target = _saved(storage_backend, "d" * 32, "earlier")

    _handle_tab_command(mgr, f"/resume {target}", left, False, {})

    ui = mgr.get(target).ui
    assert "bash" in ui.auto_approve_tools and ui.auto_approve is False


def test_skip_permissions_carries_into_a_resumed_tab(make_cli: Any, storage_backend: Any) -> None:
    mgr = make_cli()
    left = mgr.create(user_id="")
    target = _saved(storage_backend, "e" * 32, "earlier")

    _handle_tab_command(mgr, f"/resume {target}", left, True, {})

    assert mgr.get(target).ui.auto_approve is True


# -- /new -----------------------------------------------------------------------


def test_new_starts_a_workstream_with_the_tab_settings(
    make_cli: Any, storage_backend: Any, capsys: Any
) -> None:
    mgr = make_cli()
    snapshot = PersonaSnapshot(name="writer", prompt="", tools=None, mcp=True, memory=True)
    left = mgr.create(user_id="", model="other", persona="writer", persona_snapshot=snapshot)
    old = left.session
    old.temperature = 0.2
    old.max_tokens = 1234
    old.instructions = "be terse"
    old.show_reasoning = not old.show_reasoning
    old.debug = True

    _handle_tab_command(mgr, "/new", left, False, {})

    new = mgr.get(mgr.active_id)
    assert new is not None and new.id != left.id
    session = new.session
    assert session.ws_id == new.id
    assert session._model_alias == "other"
    assert session._current_persona_snapshot() == snapshot
    assert (session.temperature, session.max_tokens, session.instructions) == (
        0.2,
        1234,
        "be terse",
    )
    assert session.show_reasoning == old.show_reasoning and session.debug is True
    stored = storage_backend.load_workstream_config(new.id)
    assert (stored["temperature"], stored["max_tokens"]) == ("0.2", "1234")
    assert _texts(new) == []
    assert mgr.get(left.id) is None
    assert "New workstream started." in capsys.readouterr().out


def test_new_keeps_a_cleared_skill_cleared(make_cli: Any, storage_backend: Any) -> None:
    """A skill named at startup applies to each new workstream, unless this tab cleared it."""
    _create_template(storage_backend, "s1", "startup", "STARTUP_SKILL_CONTENT")
    mgr = make_cli(startup_skill="startup")
    left = mgr.create(user_id="")
    assert left.session._skill_name == "startup"
    left.session.handle_command("/skill clear")
    assert "STARTUP_SKILL_CONTENT" not in _sys_content(left.session)

    _handle_tab_command(mgr, "/new", left, False, {})

    session = mgr.get(mgr.active_id).session
    assert session._skill_name is None
    assert "STARTUP_SKILL_CONTENT" not in _sys_content(session)


def test_new_that_fails_after_creating_leaves_no_tab_and_no_row(
    make_cli: Any, storage_backend: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    mgr = make_cli()
    left = mgr.create(user_id="")
    created: list[str] = []

    def broken(self: ChatSession, config: dict[str, str]) -> None:
        created.append(self.ws_id)
        raise RuntimeError("settings could not be applied")

    monkeypatch.setattr(ChatSession, "adopt_settings", broken)
    _handle_tab_command(mgr, "/new", left, False, {})

    assert mgr.active_id == left.id and [ws.id for ws in mgr.list_all()] == [left.id]
    [new_id] = created
    assert storage_backend.get_workstream(new_id) is None
    assert "Cannot start a new workstream: settings could not be applied" in (
        capsys.readouterr().out
    )


# -- a tab whose lease moved ----------------------------------------------------------


def test_resuming_the_tab_whose_lease_moved_opens_it_again(make_cli: Any, capsys: Any) -> None:
    """A stopped tab counts as gone: /resume reopens it instead of calling it open already."""
    mgr = make_cli()
    left = mgr.create(user_id="")
    left._lease.mark_lost()  # a refused write stopped this copy

    _handle_tab_command(mgr, f"/resume {left.id}", left, False, {})

    assert "Already in that workstream." not in capsys.readouterr().out
    reopened = mgr.loaded(left.id)
    assert reopened is not None and reopened is not left
    assert mgr.active_id == left.id


def test_resuming_a_stopped_background_tab_lands_on_a_live_copy(make_cli: Any) -> None:
    mgr = make_cli()
    background = mgr.create(user_id="")
    current = mgr.create(user_id="")
    background._lease.mark_lost()

    _handle_tab_command(mgr, f"/resume {background.id}", current, False, {})

    reopened = mgr.loaded(background.id)
    assert reopened is not None and reopened is not background
    assert mgr.active_id == background.id


def test_a_failed_resume_of_the_stopped_tab_brings_the_next_one_forward(
    make_cli: Any, capsys: Any, monkeypatch: Any
) -> None:
    """The open retired the stopped copy, then another process refused it: say what is in front."""
    from turnstone.core.storage import WorkstreamLeaseHeldError

    mgr = make_cli()
    behind = mgr.create(user_id="")
    current = mgr.create(user_id="")
    current._lease.mark_lost()

    def held(ws_id: str, *_args: Any, **_kwargs: Any) -> Any:
        raise WorkstreamLeaseHeldError(ws_id, holder_node_id="node-b")

    monkeypatch.setattr(mgr, "_acquire_lease", held)
    replayed: list[bool] = []
    monkeypatch.setattr(behind.ui, "flush_buffer", lambda: replayed.append(True))

    _handle_tab_command(mgr, f"/resume {current.id}", current, False, {})

    out = capsys.readouterr().out
    assert "Cannot resume" in out and "node 'node-b'" in out
    assert mgr.get(current.id) is None and mgr.active_id == behind.id
    assert f"Now in workstream {mgr.index_of(behind.id)}:" in out
    assert replayed == [True]  # its buffered output, shown once it is in front


def test_a_resume_that_finds_the_stopped_tab_unopenable_brings_the_next_one_forward(
    make_cli: Any, capsys: Any, monkeypatch: Any
) -> None:
    mgr = make_cli()
    behind = mgr.create(user_id="")
    current = mgr.create(user_id="")
    current._lease.mark_lost()
    # Deleted meanwhile: the open retires the stopped copy, then has nothing to open.
    monkeypatch.setattr(mgr._storage, "ensure_workstream_incarnation_snapshot", lambda _id: None)

    _handle_tab_command(mgr, f"/resume {current.id}", current, False, {})

    out = capsys.readouterr().out
    assert mgr.get(current.id) is None and mgr.active_id == behind.id
    assert f"Now in workstream {mgr.index_of(behind.id)}:" in out


def test_an_interrupted_resume_of_the_stopped_tab_still_brings_the_next_one_forward(
    make_cli: Any, capsys: Any, monkeypatch: Any
) -> None:
    """Ctrl-C after the open retired the stopped copy: the user is still told what is in front."""
    mgr = make_cli()
    behind = mgr.create(user_id="")
    current = mgr.create(user_id="")
    current._lease.mark_lost()

    def interrupted(*_args: Any, **_kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(mgr, "_acquire_lease", interrupted)

    with pytest.raises(KeyboardInterrupt):
        _handle_tab_command(mgr, f"/resume {current.id}", current, False, {})

    assert mgr.get(current.id) is None and mgr.active_id == behind.id
    assert f"Now in workstream {mgr.index_of(behind.id)}:" in capsys.readouterr().out


def test_ws_close_says_what_happened(make_cli: Any, capsys: Any, monkeypatch: Any) -> None:
    from turnstone.core.session_manager import CloseOutcome

    mgr = make_cli()
    ws = mgr.create(user_id="")
    idx = mgr.index_of(ws.id)
    outcomes = {
        CloseOutcome.OWNED_ELSEWHERE: "here; this copy no longer owned it",
        CloseOutcome.UNRESOLVED_PERSISTENCE: "its last messages are not saved yet",
        CloseOutcome.CLEANUP_PENDING: "is still cleaning up",
    }
    for outcome, message in outcomes.items():
        monkeypatch.setattr(mgr, "close_with_outcome", lambda _ws_id, o=outcome: o)
        _handle_ws_command(mgr, f"/ws close {idx}", False)
        assert message in capsys.readouterr().out


# -- the tab left -----------------------------------------------------------------


def _unsaved(monkeypatch: pytest.MonkeyPatch, ws: Any, unsaved: list[bool]) -> None:
    """Make ``ws`` report unsaved messages while ``unsaved[0]`` is true."""
    monkeypatch.setattr(
        ws.session, "has_unresolved_conversation_persistence_nowait", lambda: unsaved[0]
    )


def test_a_tab_left_with_unsaved_messages_closes_once_they_are_saved(
    make_cli: Any, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = make_cli()
    left = mgr.create(user_id="")
    unsaved = [True]
    _unsaved(monkeypatch, left, unsaved)
    parked: dict[str, str] = {}

    _handle_tab_command(mgr, "/new", left, False, parked)

    assert parked == {left.id: "unsaved"} and mgr.get(left.id) is left
    assert "stays open until its last messages are saved" in capsys.readouterr().out
    _close_parked_tabs(mgr, parked)
    assert mgr.get(left.id) is left  # still unsaved: it waits

    unsaved[0] = False
    _close_parked_tabs(mgr, parked)

    assert mgr.get(left.id) is None and parked == {}
    assert "closed (its messages are saved)" in capsys.readouterr().out


def test_a_tab_left_whose_lock_is_busy_is_not_closed_on_a_guess(
    make_cli: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clean check never blocks: a busy answer (``None``) counts as unsaved."""
    mgr = make_cli()
    left = mgr.create(user_id="")
    monkeypatch.setattr(
        left.session, "has_unresolved_conversation_persistence_nowait", lambda: None
    )
    parked: dict[str, str] = {}

    _handle_tab_command(mgr, "/new", left, False, parked)

    assert parked == {left.id: "unsaved"} and mgr.get(left.id) is left


def test_a_tab_left_running_programs_stays_until_the_user_closes_it(
    make_cli: Any, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = make_cli()
    left = mgr.create(user_id="")
    monkeypatch.setattr(left.session, "has_running_background_shells", lambda: True)
    parked: dict[str, str] = {}

    _handle_tab_command(mgr, "/new", left, False, parked)

    assert parked == {left.id: "programs"}
    out = capsys.readouterr().out
    assert "programs it started are still running" in out
    assert f"/ws close {mgr.index_of(left.id)}" in out
    _close_parked_tabs(mgr, parked)
    assert mgr.get(left.id) is left and parked == {left.id: "programs"}


def test_a_parked_tab_the_user_goes_back_to_is_no_longer_parked(
    make_cli: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = make_cli()
    left = mgr.create(user_id="")
    _unsaved(monkeypatch, left, [True])
    parked: dict[str, str] = {}
    _handle_tab_command(mgr, "/new", left, False, parked)

    mgr.switch(left.id)
    _close_parked_tabs(mgr, parked)

    assert parked == {} and mgr.get(left.id) is left


# -- retrying unsaved rows ----------------------------------------------------------


def test_unsaved_rows_are_retried_until_the_cli_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("turnstone.cli._PERSISTENCE_RETRY_SECONDS", 0.01)
    retried = threading.Event()
    calls: list[int] = []

    def reconcile() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("storage still down")
        retried.set()

    manager = MagicMock()
    manager.reconcile_unresolved_persistence.side_effect = reconcile

    stop = _start_persistence_retries(manager)
    try:
        # A failed retry does not end the loop.
        assert retried.wait(5)
    finally:
        stop.set()
    [thread] = [t for t in threading.enumerate() if t.name == "cli-persistence-retry"]
    thread.join(5)
    assert not thread.is_alive()


def test_deleting_a_workstream_a_tab_has_open_says_to_close_it_first(
    make_cli: Any, capsys: Any
) -> None:
    from turnstone.cli import _refuse_deleting_an_open_tab

    mgr = make_cli()
    first = mgr.create(user_id="")
    second = mgr.create(user_id="")

    assert _refuse_deleting_an_open_tab(mgr, f"/delete {second.id}") is True

    idx = mgr.index_of(second.id)
    assert f"close it first with /ws close {idx}" in capsys.readouterr().out
    assert mgr.get(second.id) is second and mgr.get(first.id) is first
    # Anything else goes on to the session's own /delete.
    assert _refuse_deleting_an_open_tab(mgr, "/delete nope") is False
    assert _refuse_deleting_an_open_tab(mgr, "/history") is False
