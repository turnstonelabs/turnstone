"""Adversarial coverage for accepted-row persistence reconciliation."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from tests._session_helpers import RecordingUI, make_result, make_session
from tests.test_session_manager import _make_manager
from turnstone.core.session import ConversationPersistenceError, EffectStatus
from turnstone.core.storage import ConversationCommitConflictError
from turnstone.core.trajectory import Role
from turnstone.core.workstream import WorkstreamState

if TYPE_CHECKING:
    from collections.abc import Callable


def _journal(
    session: Any,
    persist: Callable[[], int],
    *,
    commit_key: str = "commit-a",
) -> Any:
    with session._history_handoff_lock:
        return session._journal_conversation_row_locked(
            commit_key=commit_key,
            message={"role": "assistant", "content": "accepted"},
            persist=persist,
            event_id=7,
        )


def test_transient_reconciliation_is_single_attempt_due_gated_and_capped() -> None:
    session = make_session()
    clock = [100.0]
    outcomes = iter([0, 0, 41])
    persist = MagicMock(side_effect=lambda: next(outcomes))
    entry = _journal(session, persist)
    assert session.conversation_persistence_status()["state"] == "pending"

    with (
        patch("turnstone.core.session.time.monotonic", side_effect=lambda: clock[0]),
        patch.object(
            session,
            "_conversation_persistence_retry_delay",
            side_effect=[2.0, 4.0],
        ),
    ):
        with pytest.raises(ConversationPersistenceError):
            session._persist_pending_conversation_commit(entry)

        assert persist.call_count == 1
        assert session.conversation_persistence_status()["state"] == "retrying"
        assert session._conversation_persistence_next_retry_at == 102.0

        clock[0] = 101.999
        assert session.reconcile_unresolved_persistence_if_due(now=clock[0]) is False
        with pytest.raises(ConversationPersistenceError):
            session._reconcile_pending_conversation_commits()
        assert persist.call_count == 1

        clock[0] = 102.0
        assert session.reconcile_unresolved_persistence_if_due(now=clock[0]) is True
        assert persist.call_count == 2
        assert session._conversation_persistence_next_retry_at == 106.0

        clock[0] = 106.0
        assert session.reconcile_unresolved_persistence_if_due(now=clock[0]) is True

    assert persist.call_count == 3
    assert session.has_unresolved_conversation_persistence() is False
    assert session.conversation_persistence_status() == {
        "state": "healthy",
        "pending_rows": 0,
        "attempts": 0,
        "first_failure_at": None,
        "last_failure_at": None,
        "next_retry_at": None,
    }
    with patch("turnstone.core.session.random.uniform", return_value=60.0) as jitter:
        assert session._conversation_persistence_retry_delay(10_000) == 60.0
    assert jitter.call_args.args == (30.0, 60.0)


def test_repeated_failure_dedupes_repair_fanout_until_recovery() -> None:
    session = make_session()
    session.ui.on_history_resync = MagicMock()
    session.ui.on_persistence_state_changed = MagicMock()
    persist = MagicMock(side_effect=[0, 0, 9])
    entry = _journal(session, persist)

    with patch.object(session, "_conversation_persistence_retry_delay", return_value=0.0):
        with pytest.raises(ConversationPersistenceError):
            session._persist_pending_conversation_commit(entry)
        assert session.reconcile_unresolved_persistence_if_due(now=float("inf")) is True
        assert session.reconcile_unresolved_persistence_if_due(now=float("inf")) is True

    session.ui.on_history_resync.assert_called_once_with("conversation_persistence_unresolved")
    assert session.ui.on_persistence_state_changed.call_count == 2
    assert session.conversation_persistence_status()["state"] == "healthy"


def test_concurrent_due_sweeps_cannot_duplicate_one_retry_attempt() -> None:
    session = make_session()
    retry_entered = threading.Event()
    release_retry = threading.Event()
    calls = 0

    def _persist() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return 0
        retry_entered.set()
        assert release_retry.wait(5)
        return 0

    entry = _journal(session, _persist)
    delays = iter([0.0, 10.0])
    with (
        patch("turnstone.core.session.time.monotonic", return_value=100.0),
        patch.object(
            session,
            "_conversation_persistence_retry_delay",
            side_effect=lambda _attempt: next(delays),
        ),
    ):
        with pytest.raises(ConversationPersistenceError):
            session._persist_pending_conversation_commit(entry)

        results: list[bool] = []
        sweeps = [
            threading.Thread(
                target=lambda: results.append(
                    session.reconcile_unresolved_persistence_if_due(now=100.0)
                ),
                daemon=True,
            )
            for _ in range(2)
        ]
        sweeps[0].start()
        assert retry_entered.wait(5)
        sweeps[1].start()
        release_retry.set()
        for sweep in sweeps:
            sweep.join(5)

    assert all(not sweep.is_alive() for sweep in sweeps)
    assert calls == 2
    assert sorted(results) == [False, True]
    assert session._conversation_persistence_next_retry_at == 110.0


def test_commit_conflict_is_chained_permanent_and_not_history_acked() -> None:
    session = make_session()
    conflict = ConversationCommitConflictError("immutable mismatch")
    persist = MagicMock(side_effect=conflict)
    entry = _journal(session, persist)

    with pytest.raises(ConversationPersistenceError) as raised:
        session._persist_pending_conversation_commit(entry)

    assert raised.value.__cause__ is conflict
    assert "immutable durable commit conflict" in str(raised.value)
    assert persist.call_count == 1
    status = session.conversation_persistence_status()
    assert status["state"] == "conflict"
    assert status["next_retry_at"] is None
    assert set(status) == {
        "state",
        "pending_rows",
        "attempts",
        "first_failure_at",
        "last_failure_at",
        "next_retry_at",
    }

    assert session.reconcile_unresolved_persistence_if_due(now=float("inf")) is False
    with pytest.raises(ConversationPersistenceError) as retried:
        session._reconcile_pending_conversation_commits(_force_retry=True)
    assert retried.value is raised.value
    assert persist.call_count == 1

    rows, _token = session.capture_history_handoff(
        lambda _overscan: [
            {
                "role": "assistant",
                "content": "different durable value",
                "_commit_key": entry.commit_key,
            }
        ]
    )
    assert session.has_unresolved_conversation_persistence() is True
    assert session.conversation_persistence_status()["state"] == "conflict"
    assert rows[0]["content"] == "accepted"


def test_history_lost_ack_reconciliation_clears_all_retry_metadata() -> None:
    session = make_session()
    session.ui.on_persistence_state_changed = MagicMock()
    entry = _journal(session, MagicMock(return_value=0))
    with pytest.raises(ConversationPersistenceError):
        session._persist_pending_conversation_commit(entry)

    assert session.conversation_persistence_status()["state"] == "retrying"
    session.capture_history_handoff(
        lambda _overscan: [
            {
                "role": "assistant",
                "content": "accepted",
                "_commit_key": entry.commit_key,
            }
        ]
    )

    assert session.conversation_persistence_status() == {
        "state": "healthy",
        "pending_rows": 0,
        "attempts": 0,
        "first_failure_at": None,
        "last_failure_at": None,
        "next_retry_at": None,
    }
    assert session.ui.on_persistence_state_changed.call_count == 2


def test_already_acked_race_has_no_fabricated_row_id() -> None:
    session = make_session()
    persist_entered = threading.Event()
    release_persist = threading.Event()

    def _persist() -> int:
        persist_entered.set()
        assert release_persist.wait(5)
        return 42

    persist = MagicMock(side_effect=_persist)
    entry = _journal(session, persist)
    results: list[object] = []

    first = threading.Thread(
        target=lambda: results.append(session._persist_pending_conversation_commit(entry)),
        daemon=True,
    )
    second = threading.Thread(
        target=lambda: results.append(session._persist_pending_conversation_commit(entry)),
        daemon=True,
    )
    first.start()
    assert persist_entered.wait(5)
    second.start()
    release_persist.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert persist.call_count == 1
    assert results == [None, None]


def test_terminal_barrier_blocks_a_late_due_sweep() -> None:
    session = make_session()
    persist = MagicMock(return_value=0)
    entry = _journal(session, persist)
    with pytest.raises(ConversationPersistenceError):
        session._persist_pending_conversation_commit(entry)
    assert persist.call_count == 1

    session.shutdown_publication_and_drain_durability()
    assert session.reconcile_unresolved_persistence_if_due(now=float("inf")) is False
    assert persist.call_count == 1


def test_terminal_race_is_a_benign_cancelled_maintenance_pass() -> None:
    session = make_session()
    entry = _journal(session, MagicMock(return_value=0))
    with pytest.raises(ConversationPersistenceError):
        session._persist_pending_conversation_commit(entry)

    from turnstone.core.session import GenerationCancelled

    with patch.object(
        session,
        "_reconcile_pending_conversation_commits",
        side_effect=GenerationCancelled(),
    ):
        assert session.reconcile_unresolved_persistence_if_due(now=float("inf")) is False


def test_soft_close_forces_transient_retry_but_never_retries_conflict() -> None:
    transient = make_session()
    transient_persist = MagicMock(side_effect=[0, 17])
    transient_entry = _journal(transient, transient_persist)
    with (
        patch.object(
            transient,
            "_conversation_persistence_retry_delay",
            return_value=60.0,
        ),
        pytest.raises(ConversationPersistenceError),
    ):
        transient._persist_pending_conversation_commit(transient_entry)

    assert transient.prepare_soft_close() is True
    assert transient_persist.call_count == 2
    assert transient.has_unresolved_conversation_persistence() is False

    conflicted = make_session()
    conflict = ConversationCommitConflictError("immutable mismatch")
    conflict_persist = MagicMock(side_effect=conflict)
    conflict_entry = _journal(conflicted, conflict_persist)
    with pytest.raises(ConversationPersistenceError):
        conflicted._persist_pending_conversation_commit(conflict_entry)

    assert conflicted.prepare_soft_close() is False
    assert conflicted._publication_shutdown is False
    assert conflict_persist.call_count == 1


def test_assistant_persistence_recovery_completes_unstarted_tool_prefix(
    tmp_db: Any,
) -> None:
    """Recovery cannot expose an assistant tool call without its TOOL row."""
    from turnstone.core.memory import register_workstream

    manager, _adapter, _storage = _make_manager(max_active=1)
    ws = manager.create(user_id="owner", ws_id="tool-prefix-recovery")
    session = make_session(ws_id=ws.id, user_id="owner", ui=RecordingUI())
    session._title_generated = True
    ws.session = session
    register_workstream(ws.id, user_id="owner")
    tool_calls = [
        {
            "id": call_id,
            "type": "function",
            "function": {"name": tool_name, "arguments": "{}"},
        }
        for call_id, tool_name in (
            ("call-never-started-a", "write_file"),
            ("call-never-started-b", "notify"),
        )
    ]
    assistant_attempts = 0
    saved_roles: list[tuple[str, str | None]] = []

    def _save_message(
        _ws_id: str,
        role: str,
        _content: str,
        *_args: Any,
        **kwargs: Any,
    ) -> int:
        nonlocal assistant_attempts
        saved_roles.append((role, kwargs.get("tool_call_id")))
        if role == "assistant" and kwargs.get("tool_calls"):
            assistant_attempts += 1
            if assistant_attempts == 1:
                return 0
        return len(saved_roles) + 10

    stream_calls = 0

    def _stream(_generation: int) -> Any:
        nonlocal stream_calls
        stream_calls += 1
        if stream_calls == 1:
            return make_result(tool_calls=tool_calls)
        # The next provider call sees a complete assistant/tool prefix before
        # the newly admitted user row. This is the structural provider seam
        # that the old recovery path violated.
        roles = [turn.role for turn in session.messages]
        assert roles == [
            Role.USER,
            Role.ASSISTANT,
            Role.TOOL,
            Role.TOOL,
            Role.USER,
        ]
        assistant = session.messages[1]
        assert [call.id for call in assistant.tool_calls] == [
            "call-never-started-a",
            "call-never-started-b",
        ]
        assert [turn.tool_call_id for turn in session.messages[2:4]] == [
            "call-never-started-a",
            "call-never-started-b",
        ]
        return make_result("continued safely")

    execute = MagicMock()
    with (
        patch("turnstone.core.session.save_message", side_effect=_save_message) as save,
        patch.object(session, "_stream_response", side_effect=_stream),
        patch.object(session, "_execute_tools", execute),
        patch.object(session, "_visible_memory_count", return_value=0),
        patch.object(session, "_print_status_line"),
    ):
        with pytest.raises(ConversationPersistenceError):
            session.send("use the tool", acting_user_id="owner")

        execute.assert_not_called()
        assert saved_roles == [("user", None), ("assistant", None)]
        assert session.conversation_persistence_status()["state"] == "retrying"
        assert session.conversation_persistence_status()["pending_rows"] == 3
        assert [turn.role for turn in session.messages] == [
            Role.USER,
            Role.ASSISTANT,
            Role.TOOL,
            Role.TOOL,
        ]
        cancelled = session.messages[-2:]
        assert [turn.tool_call_id for turn in cancelled] == [
            "call-never-started-a",
            "call-never-started-b",
        ]
        assert all(turn.effect_status is EffectStatus.NONE for turn in cancelled)
        assert all("no side effects" in turn.text.lower() for turn in cancelled)

        manager.set_state(ws.id, WorkstreamState.ERROR)
        retry_at = session._conversation_persistence_next_retry_at
        assert retry_at is not None
        assert manager.reconcile_unresolved_persistence(now=retry_at - 0.001) == []
        assert save.call_count == 2

        assert manager.reconcile_unresolved_persistence(now=float("inf")) == [ws.id]
        assert saved_roles == [
            ("user", None),
            ("assistant", None),
            ("assistant", None),
            ("tool", "call-never-started-a"),
            ("tool", "call-never-started-b"),
        ]
        assert session.conversation_persistence_status()["state"] == "healthy"
        assert ws.state is WorkstreamState.IDLE

        session.send("continue", acting_user_id="owner")

    execute.assert_not_called()
    assert stream_calls == 2
    assert session.has_unresolved_conversation_persistence() is False


def test_assistant_commit_conflict_completes_live_tool_prefix_but_never_retries(
    tmp_db: Any,
) -> None:
    """A permanent assistant conflict is structurally complete but fail-stop."""
    session = make_session(user_id="owner", ui=RecordingUI())
    session._title_generated = True
    conflict = ConversationCommitConflictError("immutable mismatch")
    conversation_writes: list[str] = []

    def _save_message(
        _ws_id: str,
        role: str,
        _content: str,
        *_args: Any,
        **kwargs: Any,
    ) -> int:
        conversation_writes.append(role)
        if role == "assistant" and kwargs.get("tool_calls"):
            raise conflict
        if role == "tool":
            raise AssertionError("conflicted tool row must never be written")
        return len(conversation_writes)

    tool_call = {
        "id": "conflicted-call",
        "type": "function",
        "function": {"name": "write_file", "arguments": "{}"},
    }
    execute = MagicMock()
    stream = MagicMock(return_value=make_result(tool_calls=[tool_call]))
    with (
        patch("turnstone.core.session.save_message", side_effect=_save_message),
        patch.object(session, "_stream_response", stream),
        patch.object(session, "_execute_tools", execute),
        patch.object(session, "_visible_memory_count", return_value=0),
        patch.object(session, "_print_status_line"),
    ):
        with pytest.raises(ConversationPersistenceError) as raised:
            session.send("use the tool", acting_user_id="owner")

        assert raised.value.__cause__ is conflict
        execute.assert_not_called()
        assert conversation_writes == ["user", "assistant"]
        assert [turn.role for turn in session.messages] == [
            Role.USER,
            Role.ASSISTANT,
            Role.TOOL,
        ]
        assert session.messages[-1].effect_status is EffectStatus.NONE
        assert session.conversation_persistence_status()["state"] == "conflict"
        assert session.conversation_persistence_status()["pending_rows"] == 2

        assert session.reconcile_unresolved_persistence_if_due(now=float("inf")) is False
        with pytest.raises(ConversationPersistenceError):
            session.send("must remain blocked", acting_user_id="owner")

    assert conversation_writes == ["user", "assistant"]
    execute.assert_not_called()
    stream.assert_called_once()


def test_terminal_barrier_waits_for_admitted_sweep_then_prevents_another() -> None:
    session = make_session()
    retry_entered = threading.Event()
    release_retry = threading.Event()
    calls = 0

    def _persist() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return 0
        retry_entered.set()
        assert release_retry.wait(5)
        return 23

    entry = _journal(session, _persist)
    with pytest.raises(ConversationPersistenceError):
        session._persist_pending_conversation_commit(entry)

    repair = threading.Thread(
        target=lambda: session.reconcile_unresolved_persistence_if_due(now=float("inf")),
        daemon=True,
    )
    repair.start()
    assert retry_entered.wait(5)

    terminal = threading.Thread(
        target=session.shutdown_publication_and_drain_durability,
        daemon=True,
    )
    terminal.start()
    terminal.join(0.05)
    assert terminal.is_alive(), "terminal delete barrier passed an in-flight repair"

    release_retry.set()
    repair.join(5)
    terminal.join(5)
    assert not repair.is_alive() and not terminal.is_alive()
    assert calls == 2
    assert session.reconcile_unresolved_persistence_if_due(now=float("inf")) is False
    assert calls == 2


class _RecoverableSession:
    def __init__(self, manager: Any) -> None:
        self._manager = manager
        self.unresolved = True
        self.reconcile_calls = 0
        self.manager_lock_was_free = False
        self.cancelled = False
        self.closed = False

    def has_unresolved_conversation_persistence(self) -> bool:
        return self.unresolved

    def reconcile_unresolved_persistence_if_due(self, *, now: float) -> bool:
        del now
        self.reconcile_calls += 1
        acquired = threading.Event()

        def _probe_manager_lock() -> None:
            with self._manager._lock:
                acquired.set()

        probe = threading.Thread(target=_probe_manager_lock, daemon=True)
        probe.start()
        self.manager_lock_was_free = acquired.wait(1.0)
        probe.join(1.0)
        self.unresolved = False
        return True

    def cancel(self) -> None:
        self.cancelled = True

    def close(self) -> None:
        self.closed = True


def test_manager_sweep_runs_outside_lock_and_capacity_retries_once() -> None:
    manager, _adapter, _storage = _make_manager(max_active=1)
    incumbent = manager.create(user_id="owner", ws_id="incumbent")
    recoverable = _RecoverableSession(manager)
    incumbent.session = recoverable

    replacement = manager.create(user_id="owner", ws_id="replacement")

    assert replacement.id == "replacement"
    assert recoverable.reconcile_calls == 1
    assert recoverable.manager_lock_was_free is True
    assert recoverable.cancelled is True
    assert recoverable.closed is True


def test_manager_repairs_exact_persistence_error_to_idle_and_frees_capacity(
    tmp_db: Any,
) -> None:
    from turnstone.core.memory import register_workstream

    manager, _adapter, _storage = _make_manager(max_active=1)
    ws = manager.create(user_id="owner", ws_id="real-persistence-error")
    session = make_session(ws_id=ws.id)
    session.ui.on_state_change = MagicMock()
    ws.session = session
    register_workstream(ws.id, user_id="owner")
    persist = MagicMock(side_effect=[0, 31])
    entry = _journal(session, persist)

    with pytest.raises(ConversationPersistenceError) as raised:
        session._persist_pending_conversation_commit(entry)
    session._record_fatal_error(raised.value)
    manager.set_state(ws.id, WorkstreamState.ERROR)
    assert session.conversation_persistence_fatal_revision() is not None

    # A still-running recovery send owns the state and suppresses maintenance.
    with ws._lock:
        ws._worker_running = True
    assert manager.reconcile_unresolved_persistence(now=float("inf")) == []
    assert persist.call_count == 1
    assert ws.state is WorkstreamState.ERROR
    with ws._lock:
        ws._worker_running = False

    assert manager.reconcile_unresolved_persistence(now=float("inf")) == [ws.id]
    assert persist.call_count == 2
    assert ws.state is WorkstreamState.IDLE
    assert session.conversation_persistence_fatal_revision() is None

    replacement = manager.create(user_id="owner", ws_id="replacement-after-repair")
    assert replacement.id == "replacement-after-repair"


def test_manager_does_not_clear_an_error_with_newer_fatal_provenance(tmp_db: Any) -> None:
    from turnstone.core.memory import register_workstream

    manager, _adapter, _storage = _make_manager(max_active=1)
    ws = manager.create(user_id="owner", ws_id="newer-fatal")
    session = make_session(ws_id=ws.id)
    session.ui.on_state_change = MagicMock()
    ws.session = session
    register_workstream(ws.id, user_id="owner")
    persist = MagicMock(side_effect=[0, 37])
    entry = _journal(session, persist)

    with pytest.raises(ConversationPersistenceError) as raised:
        session._persist_pending_conversation_commit(entry)
    session._record_fatal_error(raised.value)
    session._record_fatal_error(RuntimeError("newer independent failure"))
    manager.set_state(ws.id, WorkstreamState.ERROR)

    assert session.conversation_persistence_fatal_revision() is None
    assert manager.reconcile_unresolved_persistence(now=float("inf")) == [ws.id]
    assert session.has_unresolved_conversation_persistence() is False
    assert ws.state is WorkstreamState.ERROR


def test_manager_retires_persistence_error_after_history_proves_lost_ack(
    tmp_db: Any,
) -> None:
    from turnstone.core.memory import register_workstream

    manager, _adapter, _storage = _make_manager(max_active=1)
    ws = manager.create(user_id="owner", ws_id="history-repaired-error")
    session = make_session(ws_id=ws.id)
    session.ui.on_state_change = MagicMock()
    ws.session = session
    register_workstream(ws.id, user_id="owner")
    entry = _journal(session, MagicMock(return_value=0))

    with pytest.raises(ConversationPersistenceError) as raised:
        session._persist_pending_conversation_commit(entry)
    # The repair event can make another browser fetch history before the
    # failing worker reaches its fatal-record tail. Prove that this ordering
    # still retains persistence provenance even though history clears the
    # live journal error first.
    session.capture_history_handoff(
        lambda _overscan: [
            {
                "role": "assistant",
                "content": "accepted",
                "_commit_key": entry.commit_key,
            }
        ]
    )
    assert session.has_unresolved_conversation_persistence() is False

    session._record_fatal_error(raised.value)
    manager.set_state(ws.id, WorkstreamState.ERROR)
    assert ws.state is WorkstreamState.ERROR

    assert manager.reconcile_unresolved_persistence(now=float("inf")) == [ws.id]
    assert ws.state is WorkstreamState.IDLE
    assert session.conversation_persistence_fatal_revision() is None


class _MaintenanceManager:
    def __init__(self, stop: threading.Event) -> None:
        self.stop = stop
        self.reconcile_calls = 0
        self.reap_calls = 0
        self.close_calls = 0

    def reconcile_unresolved_persistence(self) -> list[str]:
        self.reconcile_calls += 1
        if self.reconcile_calls >= 2:
            self.stop.set()
        return []

    def reap_stale_creating_reservations(self, _max_age_seconds: float) -> list[str]:
        self.reap_calls += 1
        return []

    def close_idle(self, _max_age_seconds: float) -> list[str]:
        self.close_calls += 1
        return []

    def subscribe_to_state(self, _callback: Any) -> None:
        raise AssertionError("idle-disabled maintenance must not subscribe")

    def unsubscribe_from_state(self, _callback: Any) -> None:
        raise AssertionError("idle-disabled maintenance must not unsubscribe")


def test_server_maintenance_reconciles_when_idle_eviction_is_disabled() -> None:
    from turnstone import server

    stop = threading.Event()
    manager = _MaintenanceManager(stop)
    with patch.object(server, "PERSISTENCE_RECONCILE_INTERVAL_SECONDS", 0.01):
        server._idle_cleanup_thread(
            manager,  # type: ignore[arg-type]
            0.0,
            MagicMock(),
            stop=stop,
        )

    assert manager.reconcile_calls == 2
    assert manager.close_calls == 0
    assert manager.reap_calls == 1


def test_coordinator_maintenance_reconciles_when_idle_eviction_is_disabled() -> None:
    from turnstone.console.server import _coord_idle_cleanup_thread
    from turnstone.core import session_manager

    stop = threading.Event()
    manager = _MaintenanceManager(stop)
    with patch.object(session_manager, "PERSISTENCE_RECONCILE_INTERVAL_SECONDS", 0.01):
        _coord_idle_cleanup_thread(
            manager,  # type: ignore[arg-type]
            0.0,
            stop,
        )

    assert manager.reconcile_calls == 2
    assert manager.close_calls == 0
    assert manager.reap_calls == 1


def test_capacity_reconcile_forces_a_definite_probe(tmp_db: Any) -> None:
    """``_reserve_and_install``'s last-chance repair runs ONCE, so it must
    not skip a session whose locks are momentarily held.

    The maintenance sweep probes without blocking and re-probes a second
    later; this caller has no second pass — and the sessions likeliest to
    be contended are exactly the ones whose unresolved journals emptied
    its candidate list, so skipping them turns a repairable capacity
    stall into ``All N slots are active``."""
    manager, _adapter, _storage = _make_manager(max_active=1)
    session = make_session()
    ws = manager.create(user_id="u1", ws_id="cap-ws")
    ws.session = session
    ws.state = WorkstreamState.IDLE

    probes: list[str] = []
    session.has_unresolved_conversation_persistence = (  # type: ignore[method-assign]
        lambda: probes.append("blocking") or False
    )
    session.has_unresolved_conversation_persistence_nowait = (  # type: ignore[attr-defined]
        lambda: probes.append("nowait") or None
    )

    # The maintenance shape skips a contended session outright.
    manager.reconcile_unresolved_persistence()
    assert probes == ["nowait"]

    # The one-shot capacity shape forces an answer instead.
    probes.clear()
    manager.reconcile_unresolved_persistence(blocking=True)
    assert probes == ["blocking"]
