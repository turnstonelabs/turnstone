"""Adversarial worker/terminal admission races for ``ChatSession``."""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests._session_helpers import make_session
from turnstone.core import session_worker
from turnstone.core.session import ConversationPersistenceError, GenerationCancelled
from turnstone.core.trajectory import turn_to_dict
from turnstone.core.workstream import Workstream


class _ObservedRLock:
    """Reentrant lock that exposes one named thread's acquisition attempt."""

    def __init__(self, observed_thread: str) -> None:
        self._lock = threading.RLock()
        self._observed_thread = observed_thread
        self.acquire_attempted = threading.Event()

    def acquire(self, *args: Any, **kwargs: Any) -> bool:
        if threading.current_thread().name == self._observed_thread:
            self.acquire_attempted.set()
        return self._lock.acquire(*args, **kwargs)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> _ObservedRLock:
        self.acquire()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.release()


def _workstream(session: Any) -> Workstream:
    ws = Workstream(id="ws-lifecycle-admission", name="lifecycle-admission", user_id="owner")
    ws.session = session
    ws.ui = session.ui
    return ws


def test_stop_invalidates_worker_slot_before_delayed_send_entry(tmp_db: Any) -> None:
    """A Stop cannot be erased by a worker that has not entered ``send`` yet."""

    session = make_session(user_id="owner", ws_id="ws-lifecycle-admission")
    ws = _workstream(session)
    worker_entered = threading.Event()
    release_worker = threading.Event()
    errors: list[BaseException] = []
    refresh = MagicMock()

    def _run() -> None:
        worker_entered.set()
        assert release_worker.wait(5), "test did not release delayed worker"
        try:
            # Mirrors the production wrapper.  The bind is intentionally before
            # send; the active WorkerClaim makes it defer until generation claim.
            session.bind_acting_user("alice")
            session.send("must remain stopped")
        except BaseException as exc:
            errors.append(exc)

    with patch.object(session, "_refresh_model_from_registry", refresh):
        assert session_worker.send(
            ws,
            enqueue=lambda: None,
            run=_run,
            principal_id="alice",
            thread_name="delayed-stopped-worker",
        )
        worker = ws.worker_thread
        assert worker is not None and worker_entered.wait(5)
        session.cancel()
        release_worker.set()
        worker.join(5)

    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], GenerationCancelled)
    assert session._generation == 0
    assert session.messages == []
    refresh.assert_not_called()


def test_stop_during_pre_generation_registry_refresh_reaches_no_turn_admission(tmp_db: Any) -> None:
    """Registry work now follows claim, so Stop remains visible when it returns."""

    session = make_session(user_id="owner", ws_id="ws-lifecycle-admission")
    ws = _workstream(session)
    refresh_entered = threading.Event()
    release_refresh = threading.Event()
    errors: list[BaseException] = []
    session.ui.on_state_change = MagicMock()  # type: ignore[attr-defined,method-assign]

    def _blocked_refresh() -> None:
        refresh_entered.set()
        assert release_refresh.wait(5), "test did not release registry refresh"

    def _run() -> None:
        try:
            session.bind_acting_user("alice")
            session.send("stop before user admission")
        except BaseException as exc:
            errors.append(exc)

    with patch.object(session, "_refresh_model_from_registry", side_effect=_blocked_refresh):
        assert session_worker.send(
            ws,
            enqueue=lambda: None,
            run=_run,
            principal_id="alice",
            thread_name="registry-refresh-worker",
        )
        worker = ws.worker_thread
        assert worker is not None and refresh_entered.wait(5)
        session.cancel()
        release_refresh.set()
        worker.join(5)

    assert not worker.is_alive()
    assert errors == []  # send handles a cancellation after generation claim
    assert session._generation == 1
    assert session.messages == []
    assert session.has_unresolved_conversation_persistence() is False


def test_force_successor_keeps_generation_actor_and_worker_slot_from_delayed_predecessor(
    tmp_db: Any,
) -> None:
    """A force-abandoned delayed thread cannot supersede its live successor."""

    session = make_session(user_id="owner", ws_id="ws-lifecycle-admission")
    ws = _workstream(session)
    predecessor_entered = threading.Event()
    release_predecessor = threading.Event()
    successor_claimed = threading.Event()
    release_successor = threading.Event()
    predecessor_errors: list[BaseException] = []
    successor_generations: list[int] = []
    refresh = MagicMock()

    def _predecessor() -> None:
        predecessor_entered.set()
        assert release_predecessor.wait(5), "test did not release predecessor"
        try:
            session.bind_acting_user("alice")
            session.send("stale predecessor")
        except BaseException as exc:
            predecessor_errors.append(exc)

    def _successor() -> None:
        claim = session_worker.current_worker_claim(session)
        assert claim is not None
        generation = session._claim_generation(
            principal_id=claim.principal_id,
            expected_cancel_epoch=claim.cancel_epoch,
        )
        successor_generations.append(generation)
        session._bind_acting_user_for_generation("bob", generation)
        successor_claimed.set()
        assert release_successor.wait(5), "test did not release successor"
        session._generation_principals.pop(generation, None)
        session._consume_cancel(generation)

    with patch.object(session, "_refresh_model_from_registry", refresh):
        assert session_worker.send(
            ws,
            enqueue=lambda: None,
            run=_predecessor,
            principal_id="alice",
            thread_name="force-predecessor",
        )
        predecessor_thread = ws.worker_thread
        assert predecessor_thread is not None and predecessor_entered.wait(5)

        session.cancel()
        # Exact force ownership transition from the HTTP cancel path.
        with ws._lock:
            ws.worker_thread = None
            ws._worker_running = False
            ws._worker_principal_id = ""

        assert session_worker.send(
            ws,
            enqueue=lambda: None,
            run=_successor,
            principal_id="bob",
            thread_name="force-successor",
        )
        successor_thread = ws.worker_thread
        assert successor_thread is not None and successor_claimed.wait(5)

        release_predecessor.set()
        predecessor_thread.join(5)

        assert successor_thread.is_alive()
        assert ws.worker_thread is successor_thread
        assert ws._worker_running is True
        assert ws._worker_principal_id == "bob"
        assert session._generation == 1
        assert session._mcp_effective_user_id == "bob"

        release_successor.set()
        successor_thread.join(5)

    assert successor_generations == [1]
    assert len(predecessor_errors) == 1
    assert isinstance(predecessor_errors[0], GenerationCancelled)
    assert session._generation == 1
    assert session._mcp_effective_user_id == "bob"
    refresh.assert_not_called()


def test_refused_soft_close_invalidates_old_slot_but_allows_fresh_worker(tmp_db: Any) -> None:
    """Rollback reopens admission without resurrecting a pre-close worker."""

    session = make_session(user_id="owner", ws_id="ws-lifecycle-admission")
    ws = _workstream(session)
    old_entered = threading.Event()
    release_old = threading.Event()
    old_errors: list[BaseException] = []
    fresh_generations: list[int] = []
    refresh = MagicMock()

    def _old_worker() -> None:
        old_entered.set()
        assert release_old.wait(5), "test did not release old worker"
        try:
            session.send("pre-close worker")
        except BaseException as exc:
            old_errors.append(exc)

    def _fresh_worker() -> None:
        claim = session_worker.current_worker_claim(session)
        assert claim is not None
        fresh_generations.append(
            session._claim_generation(
                principal_id=claim.principal_id,
                expected_cancel_epoch=claim.cancel_epoch,
            )
        )

    with patch.object(session, "_refresh_model_from_registry", refresh):
        assert session_worker.send(ws, enqueue=lambda: None, run=_old_worker)
        old_thread = ws.worker_thread
        assert old_thread is not None and old_entered.wait(5)

        before_epoch = session._approval_cancel_epoch
        with patch.object(
            session,
            "_reconcile_pending_conversation_commits",
            side_effect=ConversationPersistenceError("still unavailable"),
        ):
            assert session.prepare_soft_close() is False
        assert session._publication_shutdown is False
        assert session._approval_cancel_epoch == before_epoch + 1

        release_old.set()
        old_thread.join(5)
        assert len(old_errors) == 1 and isinstance(old_errors[0], GenerationCancelled)
        assert session._generation == 1  # rollback bump only; old slot did not claim

        assert session_worker.send(ws, enqueue=lambda: None, run=_fresh_worker)
        fresh_thread = ws.worker_thread
        assert fresh_thread is not None
        fresh_thread.join(5)

    assert fresh_generations == [2]
    refresh.assert_not_called()


def _terminal_action(session: Any, kind: str) -> bool:
    if kind == "soft":
        return bool(session.prepare_soft_close())
    session.shutdown_publication_and_drain_durability()
    return True


@pytest.mark.parametrize("terminal_kind", ["soft", "hard"])
def test_terminal_admission_wins_before_direct_conversation_mutation(
    tmp_db: Any,
    terminal_kind: str,
) -> None:
    """A direct row that loses terminal admission mutates no surface."""

    session = make_session(user_id="owner", ws_id="ws-lifecycle-admission")
    prepare_entered = threading.Event()
    release_prepare = threading.Event()
    errors: list[BaseException] = []
    original_prepare = session._prepare_direct_conversation_mutation

    def _blocked_prepare(deferred: Any) -> None:
        prepare_entered.set()
        assert release_prepare.wait(5), "test did not release direct preparation"
        original_prepare(deferred)

    def _append() -> None:
        try:
            session._append_system_turn("correction", "must not cross terminal admission")
        except BaseException as exc:
            errors.append(exc)

    with (
        patch.object(
            session,
            "_prepare_direct_conversation_mutation",
            side_effect=_blocked_prepare,
        ),
        patch("turnstone.core.session.save_message", return_value=1) as save,
    ):
        mutator = threading.Thread(target=_append, daemon=True)
        mutator.start()
        assert prepare_entered.wait(5)
        assert _terminal_action(session, terminal_kind) is True
        release_prepare.set()
        mutator.join(5)

    assert not mutator.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
    assert session.messages == []
    assert session.has_unresolved_conversation_persistence() is False
    save.assert_not_called()


@pytest.mark.parametrize("terminal_kind", ["soft", "hard"])
def test_direct_conversation_mutation_admission_makes_terminal_wait_for_ack(
    tmp_db: Any,
    terminal_kind: str,
) -> None:
    """Once a direct row is admitted, terminal handoff drains its ticket."""

    session = make_session(user_id="owner", ws_id="ws-lifecycle-admission")
    save_entered = threading.Event()
    release_save = threading.Event()
    terminal_done = threading.Event()
    errors: list[BaseException] = []
    terminal_results: list[bool] = []

    def _blocked_save(*_args: Any, **_kwargs: Any) -> int:
        save_entered.set()
        assert release_save.wait(5), "test did not release durable ACK"
        return 1

    def _append() -> None:
        try:
            session._append_system_turn("correction", "admitted before terminal handoff")
        except BaseException as exc:
            errors.append(exc)

    def _terminate() -> None:
        try:
            terminal_results.append(_terminal_action(session, terminal_kind))
        finally:
            terminal_done.set()

    with patch("turnstone.core.session.save_message", side_effect=_blocked_save) as save:
        mutator = threading.Thread(target=_append, daemon=True)
        mutator.start()
        assert save_entered.wait(5)

        terminal = threading.Thread(target=_terminate, daemon=True)
        terminal.start()
        assert terminal_done.wait(0.1) is False

        release_save.set()
        mutator.join(5)
        terminal.join(5)

    assert not mutator.is_alive() and not terminal.is_alive()
    assert errors == []
    assert terminal_results == [True]
    assert save.call_count == 1
    assert turn_to_dict(session.messages[-1]) == {
        "role": "system",
        "content": "admitted before terminal handoff",
        "_source": "correction",
    }
    assert session.has_unresolved_conversation_persistence() is False


def test_hard_delete_barrier_waits_for_started_ambiguous_ack_reconciliation(
    tmp_db: Any,
) -> None:
    """A repair that wins visibility must ACK before hard deletion can run."""

    session = make_session(user_id="owner", ws_id="ws-lifecycle-admission")
    with (
        patch("turnstone.core.session.save_message", return_value=0),
        pytest.raises(ConversationPersistenceError),
    ):
        session._append_system_turn("correction", "ambiguous predecessor")
    assert session.has_unresolved_conversation_persistence() is True
    # Admit this explicit repair attempt; ordinary pre-due mutations now fail
    # fast without a storage call so user activity cannot bypass backoff.
    session._conversation_persistence_next_retry_at = 0.0

    visibility = _ObservedRLock("hard-terminal")
    session._history_visibility_lock = visibility  # type: ignore[assignment]
    repair_entered = threading.Event()
    release_repair = threading.Event()
    terminal_done = threading.Event()
    durable_deleted = threading.Event()
    mutation_errors: list[BaseException] = []

    def _blocked_repair(*_args: Any, **_kwargs: Any) -> int:
        repair_entered.set()
        assert release_repair.wait(5), "test did not release reconciliation"
        # This is the storage safety property the hard-delete caller relies on:
        # its delete step cannot follow the drain primitive until this save ACKs.
        assert not durable_deleted.is_set()
        return 41

    def _append_suffix() -> None:
        try:
            session._append_system_turn("correction", "must lose terminal admission")
        except BaseException as exc:
            mutation_errors.append(exc)

    def _hard_terminal() -> None:
        session.shutdown_publication_and_drain_durability()
        durable_deleted.set()  # stands in for the caller's immediately following delete
        terminal_done.set()

    with patch("turnstone.core.session.save_message", side_effect=_blocked_repair) as save:
        mutator = threading.Thread(target=_append_suffix, daemon=True, name="direct-repair")
        mutator.start()
        assert repair_entered.wait(5)

        terminal = threading.Thread(target=_hard_terminal, daemon=True, name="hard-terminal")
        terminal.start()
        assert visibility.acquire_attempted.wait(5)
        assert terminal_done.is_set() is False

        release_repair.set()
        mutator.join(5)
        terminal.join(5)

    assert not mutator.is_alive() and not terminal.is_alive()
    assert durable_deleted.is_set()
    assert save.call_count == 1
    assert len(mutation_errors) == 1 and isinstance(mutation_errors[0], RuntimeError)
    assert [turn_to_dict(turn)["content"] for turn in session.messages] == ["ambiguous predecessor"]
    assert session.has_unresolved_conversation_persistence() is False


def test_hard_delete_barrier_rejects_late_ambiguous_ack_reconciliation(
    tmp_db: Any,
) -> None:
    """A repair that loses the terminal latch performs no storage write."""

    session = make_session(user_id="owner", ws_id="ws-lifecycle-admission")
    with (
        patch("turnstone.core.session.save_message", return_value=0),
        pytest.raises(ConversationPersistenceError),
    ):
        session._append_system_turn("correction", "ambiguous predecessor")
    assert session.has_unresolved_conversation_persistence() is True

    session.shutdown_publication_and_drain_durability()

    with (
        patch("turnstone.core.session.save_message", return_value=42) as save,
        pytest.raises(RuntimeError, match="closed session"),
    ):
        session._append_system_turn("correction", "late suffix")

    save.assert_not_called()
    assert [turn_to_dict(turn)["content"] for turn in session.messages] == ["ambiguous predecessor"]
    assert session.has_unresolved_conversation_persistence() is True
