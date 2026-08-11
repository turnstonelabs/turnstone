"""Adversarial history-handoff tests for destructive tail mutations.

Every race is stopped on an explicit event boundary.  Timeout values are
diagnostic backstops only; no assertion infers correctness from elapsed time.
"""

from __future__ import annotations

import queue
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests._session_helpers import make_session
from tests.test_workstream_endpoints import (
    _build_history_app,
    _verb_cfg,
    _verb_client,
)
from turnstone.core import session_worker
from turnstone.core.memory import register_workstream, save_message
from turnstone.core.session import ConversationPersistenceError
from turnstone.core.session_routes import make_retry_handler, make_rewind_handler
from turnstone.core.storage import get_storage
from turnstone.core.trajectory import dicts_from_turns, turns_from_dicts
from turnstone.core.workstream import Workstream

_ROWS = (
    ("user", "first request"),
    ("assistant", "first answer"),
    ("user", "second request"),
    ("assistant", "second answer"),
)


def _seed_session(ws_id: str) -> Any:
    register_workstream(ws_id, kind="interactive", user_id="test-user")
    for role, content in _ROWS:
        save_message(ws_id, role, content)

    session = make_session(ws_id=ws_id, user_id="test-user")
    session.messages = turns_from_dicts(
        [{"role": role, "content": content} for role, content in _ROWS]
    )
    session._msg_tokens = [1] * len(_ROWS)
    return session


def _capture(session: Any) -> tuple[list[dict[str, Any]], str]:
    storage = get_storage()
    return session.capture_history_handoff(
        lambda _overscan: storage.load_messages(
            session.ws_id,
            repair=False,
            include_compaction=True,
        )
    )


def _truncate(session: Any, operation: str) -> Any:
    if operation == "rewind":
        return session.rewind(1)
    return session.retry()


@pytest.mark.parametrize("operation", ["rewind", "retry"])
def test_successful_truncation_invalidates_old_handoff_and_repairs_listener(
    tmp_db: Any,
    operation: str,
) -> None:
    """A deletion is one atomic history revision, not only a flight-key bump."""

    session = _seed_session(f"ws-{operation}-handoff")
    before_rows, before_token = _capture(session)
    registration = session.register_listener_for_history_handoff(before_token)
    assert registration is not None
    listener = registration[0]

    result = _truncate(session, operation)

    if operation == "rewind":
        assert result == 2
    else:
        assert result == "second request"
    after_rows, after_token = _capture(session)
    assert [row.get("content") for row in before_rows] == [content for _role, content in _ROWS]
    assert [row.get("content") for row in after_rows] == [
        "first request",
        "first answer",
    ]
    assert after_token != before_token
    assert session.register_listener_for_history_handoff(before_token) is None

    events: list[dict[str, Any]] = []
    while not listener.empty():
        events.append(listener.get_nowait())
    assert [event.get("type") for event in events].count("history_resync") == 1


@pytest.mark.parametrize("operation", ["rewind", "retry"])
def test_supplied_reset_publisher_replaces_generic_resync_exactly_once(
    tmp_db: Any,
    operation: str,
) -> None:
    """Web routes publish clear_ui atomically, without a competing resync."""

    session = _seed_session(f"ws-{operation}-clear-ui")
    _rows, token = _capture(session)
    registration = session.register_listener_for_history_handoff(token)
    assert registration is not None
    listener = registration[0]

    def _publish_clear_ui() -> None:
        session.ui._enqueue({"type": "clear_ui"})

    if operation == "rewind":
        assert session.rewind(1, publish_reset=_publish_clear_ui) == 2
    else:
        assert session.retry(publish_reset=_publish_clear_ui) == "second request"

    events: list[dict[str, Any]] = []
    while not listener.empty():
        events.append(listener.get_nowait())
    assert [event.get("type") for event in events] == ["clear_ui"]


def test_reset_publisher_failure_falls_back_to_one_generic_resync(tmp_db: Any) -> None:
    """A UI callback failure cannot make an already-committed cut look failed."""

    session = _seed_session("ws-reset-publisher-fallback")
    _rows, token = _capture(session)
    registration = session.register_listener_for_history_handoff(token)
    assert registration is not None
    listener = registration[0]

    def _raise() -> None:
        raise RuntimeError("injected reset publisher failure")

    assert session.rewind(1, publish_reset=_raise) == 2
    events: list[dict[str, Any]] = []
    while not listener.empty():
        events.append(listener.get_nowait())
    assert [event.get("type") for event in events] == ["history_resync"]
    assert events[0].get("reason") == "history_truncated"


@pytest.mark.parametrize("operation", ["rewind", "retry"])
def test_web_reset_publisher_runs_once_for_noop_truncation(
    tmp_db: Any,
    operation: str,
) -> None:
    """HTTP's historical empty-transcript clear_ui contract is preserved."""

    session = _seed_session(f"ws-{operation}-noop-reset")
    assert session.rewind(999) == len(_ROWS)
    published: list[str] = []

    if operation == "rewind":
        assert session.rewind(1, publish_reset=lambda: published.append("clear_ui")) == 0
    else:
        assert session.retry(publish_reset=lambda: published.append("clear_ui")) is None

    assert published == ["clear_ui"]


@pytest.mark.parametrize("operation", ["rewind", "retry"])
def test_truncation_storage_failure_leaves_memory_token_and_ui_unchanged(
    tmp_db: Any,
    operation: str,
) -> None:
    """A failed durable delete may not publish an in-memory-only truncation."""

    session = _seed_session(f"ws-{operation}-delete-failure")
    storage = get_storage()
    durable_before = storage.load_messages(
        session.ws_id,
        repair=False,
        include_compaction=True,
    )
    memory_before = dicts_from_turns(session.messages)
    tokens_before = list(session._msg_tokens)
    generation_before = session._history_generation
    _rows, token_before = _capture(session)
    registration = session.register_listener_for_history_handoff(token_before)
    assert registration is not None
    listener = registration[0]
    reset_publications: list[str] = []

    def _publish_reset() -> None:
        reset_publications.append("clear_ui")

    caught: Exception | None = None
    with patch.object(
        storage,
        "truncate_messages_tail",
        side_effect=RuntimeError("injected truncation failure"),
    ):
        try:
            if operation == "rewind":
                session.rewind(1, publish_reset=_publish_reset)
            else:
                session.retry(publish_reset=_publish_reset)
        except Exception as exc:  # the public failure shape may raise or refuse
            caught = exc

    assert caught is not None or dicts_from_turns(session.messages) == memory_before
    assert dicts_from_turns(session.messages) == memory_before
    assert session._msg_tokens == tokens_before
    assert session._history_generation == generation_before
    assert (
        storage.load_messages(
            session.ws_id,
            repair=False,
            include_compaction=True,
        )
        == durable_before
    )
    _after_rows, token_after = _capture(session)
    assert token_after == token_before
    assert reset_publications == []
    assert listener.empty()


def test_truncation_freezes_direct_admission_then_preserves_suffix_order(
    tmp_db: Any,
) -> None:
    """A direct row waits behind the cut, then survives after its new prefix."""

    session = _seed_session("ws-truncation-direct-admission")
    storage = get_storage()
    original_memory = dicts_from_turns(session.messages)
    _rows, token = _capture(session)
    registration = session.register_listener_for_history_handoff(token)
    assert registration is not None
    listener = registration[0]

    truncate_entered = threading.Event()
    release_truncate = threading.Event()
    append_prepare_entered = threading.Event()
    real_truncate = storage.truncate_messages_tail
    real_prepare = session._prepare_direct_conversation_mutation
    truncation_results: list[int] = []
    errors: list[BaseException] = []

    def _frozen_truncate(ws_id: str, remove_count: int) -> int:
        truncate_entered.set()
        assert release_truncate.wait(5), "test did not release strict truncation"
        return real_truncate(ws_id, remove_count)

    def _observed_prepare(deferred: Any) -> None:
        append_prepare_entered.set()
        real_prepare(deferred)

    def _rewind() -> None:
        try:
            truncation_results.append(session.rewind(1))
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    def _append() -> None:
        try:
            session._append_system_turn("correction", "accepted after cut")
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    truncator = threading.Thread(target=_rewind, daemon=True, name="frozen-truncation")
    appender = threading.Thread(target=_append, daemon=True, name="direct-system-append")
    with (
        patch.object(storage, "truncate_messages_tail", side_effect=_frozen_truncate),
        patch.object(
            session,
            "_prepare_direct_conversation_mutation",
            side_effect=_observed_prepare,
        ),
    ):
        truncator.start()
        assert truncate_entered.wait(5), "truncation did not enter strict storage cut"
        appender.start()
        try:
            assert append_prepare_entered.wait(5), "direct append did not reach its barrier"
            assert session._history_truncation_active is True
            assert dicts_from_turns(session.messages) == original_memory
            assert listener.empty()
        finally:
            release_truncate.set()
            truncator.join(5)
            appender.join(5)

    assert not truncator.is_alive() and not appender.is_alive()
    assert errors == []
    assert truncation_results == [2]
    expected = [
        ("user", "first request", None),
        ("assistant", "first answer", None),
        ("system", "accepted after cut", "correction"),
    ]
    memory_rows = dicts_from_turns(session.messages)
    durable_rows = storage.load_messages(
        session.ws_id,
        repair=False,
        include_compaction=True,
    )
    assert [(row.get("role"), row.get("content"), row.get("_source")) for row in memory_rows] == (
        expected
    )
    assert [(row.get("role"), row.get("content"), row.get("_source")) for row in durable_rows] == (
        expected
    )
    events: list[dict[str, Any]] = []
    while not listener.empty():
        events.append(listener.get_nowait())
    assert [event.get("type") for event in events] == ["history_resync", "system_turn"]
    assert events[0].get("reason") == "history_truncated"
    assert events[1].get("content") == "accepted after cut"


def test_truncation_freezes_generation_commit_admission(tmp_db: Any) -> None:
    """The total-prefix latch covers the shared generation commit primitive."""

    session = _seed_session("ws-truncation-generation-admission")
    storage = get_storage()
    truncate_entered = threading.Event()
    release_truncate = threading.Event()
    commit_attempted = threading.Event()
    commit_ran = threading.Event()
    real_truncate = storage.truncate_messages_tail
    errors: list[BaseException] = []

    def _frozen_truncate(ws_id: str, remove_count: int) -> int:
        truncate_entered.set()
        assert release_truncate.wait(5), "test did not release strict truncation"
        return real_truncate(ws_id, remove_count)

    def _rewind() -> None:
        try:
            session.rewind(1)
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    def _commit() -> None:
        commit_attempted.set()
        try:
            assert session._commit_for_generation(
                0,
                lambda _durable: commit_ran.set(),
            )
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    truncator = threading.Thread(target=_rewind, daemon=True, name="frozen-truncation")
    committer = threading.Thread(target=_commit, daemon=True, name="generation-commit")
    with patch.object(storage, "truncate_messages_tail", side_effect=_frozen_truncate):
        truncator.start()
        assert truncate_entered.wait(5), "truncation did not enter strict storage cut"
        committer.start()
        try:
            assert commit_attempted.wait(5), "generation commit did not start"
            assert session._history_truncation_active is True
            assert commit_ran.is_set() is False
        finally:
            release_truncate.set()
            truncator.join(5)
            committer.join(5)

    assert not truncator.is_alive() and not committer.is_alive()
    assert errors == []
    assert commit_ran.is_set()


def test_truncation_waiting_for_older_ticket_keeps_commit_admission_open(
    tmp_db: Any,
) -> None:
    """Waiting for the old FIFO prefix must not install the cut latch early."""

    session = _seed_session("ws-truncation-older-ticket")
    storage = get_storage()
    older_ticket_entered = threading.Event()
    release_older_ticket = threading.Event()
    truncation_wait_entered = threading.Event()
    strict_cut_entered = threading.Event()
    probe_admitted = threading.Event()
    real_wait_for = session._durability_cond.wait_for
    real_truncate = storage.truncate_messages_tail
    results: list[int] = []
    errors: list[BaseException] = []

    def _hold_older_ticket() -> None:
        def _admit(durable: list[Any]) -> None:
            def _persist() -> None:
                older_ticket_entered.set()
                assert release_older_ticket.wait(5), "test did not release older ticket"

            durable.append(_persist)

        try:
            assert session._commit_for_generation(0, _admit) is True
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    def _observe_wait(predicate: Any, timeout: float | None = None) -> bool:
        if threading.current_thread().name == "waiting-truncation":
            truncation_wait_entered.set()
        return real_wait_for(predicate, timeout)

    def _observe_truncate(ws_id: str, remove_count: int) -> int:
        strict_cut_entered.set()
        return real_truncate(ws_id, remove_count)

    def _rewind() -> None:
        try:
            results.append(session.rewind(1))
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    older = threading.Thread(target=_hold_older_ticket, daemon=True, name="older-ticket")
    truncator = threading.Thread(target=_rewind, daemon=True, name="waiting-truncation")
    older.start()
    assert older_ticket_entered.wait(5), "older ticket never entered durability"
    with (
        patch.object(session._durability_cond, "wait_for", side_effect=_observe_wait),
        patch.object(storage, "truncate_messages_tail", side_effect=_observe_truncate),
    ):
        truncator.start()
        try:
            assert truncation_wait_entered.wait(5), "truncation did not wait for old ticket"
            with session._generation_lock:
                assert session._history_truncation_active is False
            assert strict_cut_entered.is_set() is False

            def _probe(_durable: list[Any]) -> None:
                probe_admitted.set()

            assert session._commit_for_generation(0, _probe) is True
            assert probe_admitted.is_set()
        finally:
            release_older_ticket.set()
            older.join(5)
            truncator.join(5)

    assert not older.is_alive() and not truncator.is_alive()
    assert errors == []
    assert strict_cut_entered.is_set()
    assert results == [2]


def test_unresolved_prefix_repair_failure_refuses_truncation_without_cut_publication(
    tmp_db: Any,
) -> None:
    """The pending FIFO prefix must repair before a destructive cut can run."""

    session = _seed_session("ws-truncation-unresolved-prefix")
    storage = get_storage()
    _rows, initial_token = _capture(session)
    registration = session.register_listener_for_history_handoff(initial_token)
    assert registration is not None
    listener = registration[0]

    with (
        patch("turnstone.core.session.save_message", return_value=0),
        pytest.raises(ConversationPersistenceError),
    ):
        session._append_system_turn("correction", "ambiguous predecessor")

    initial_events: list[dict[str, Any]] = []
    while not listener.empty():
        initial_events.append(listener.get_nowait())
    assert [event.get("type") for event in initial_events] == [
        "system_turn",
        "history_resync",
    ]
    assert initial_events[-1].get("reason") == "conversation_persistence_unresolved"

    memory_before = dicts_from_turns(session.messages)
    tokens_before = list(session._msg_tokens)
    generation_before = session._history_generation
    durable_before = storage.load_messages(
        session.ws_id,
        repair=False,
        include_compaction=True,
    )
    _rows, token_before = _capture(session)

    with (
        patch("turnstone.core.session.save_message", return_value=0) as repair,
        patch.object(
            storage, "truncate_messages_tail", wraps=storage.truncate_messages_tail
        ) as cut,
        pytest.raises(ConversationPersistenceError),
    ):
        session.rewind(1)

    # A destructive mutation cannot bypass the transient backoff. Pre-due it
    # fails immediately on the stored poison without hammering storage.
    repair.assert_not_called()
    cut.assert_not_called()
    assert session._history_truncation_active is False
    assert dicts_from_turns(session.messages) == memory_before
    assert session._msg_tokens == tokens_before
    assert session._history_generation == generation_before
    assert (
        storage.load_messages(
            session.ws_id,
            repair=False,
            include_compaction=True,
        )
        == durable_before
    )
    _rows, token_after = _capture(session)
    assert token_after == token_before
    retry_events: list[dict[str, Any]] = []
    while not listener.empty():
        retry_events.append(listener.get_nowait())
    assert retry_events == []
    assert all(event.get("reason") != "history_truncated" for event in retry_events)


@pytest.mark.parametrize("terminal_kind", ["soft", "hard"])
def test_terminal_waits_for_admitted_truncation_ticket(
    tmp_db: Any,
    terminal_kind: str,
) -> None:
    """A terminal boundary drains an already-admitted destructive cut."""

    session = _seed_session(f"ws-truncation-{terminal_kind}-terminal")
    storage = get_storage()
    truncate_entered = threading.Event()
    release_truncate = threading.Event()
    terminal_wait_entered = threading.Event()
    terminal_done = threading.Event()
    real_truncate = storage.truncate_messages_tail
    real_wait_for = session._durability_cond.wait_for
    truncation_results: list[int] = []
    terminal_results: list[bool] = []
    errors: list[BaseException] = []

    def _frozen_truncate(ws_id: str, remove_count: int) -> int:
        truncate_entered.set()
        assert release_truncate.wait(5), "test did not release admitted truncation"
        return real_truncate(ws_id, remove_count)

    def _observe_wait(predicate: Any, timeout: float | None = None) -> bool:
        if threading.current_thread().name == f"{terminal_kind}-terminal":
            terminal_wait_entered.set()
        return real_wait_for(predicate, timeout)

    def _rewind() -> None:
        try:
            truncation_results.append(session.rewind(1))
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    def _terminate() -> None:
        try:
            if terminal_kind == "soft":
                terminal_results.append(bool(session.prepare_soft_close()))
            else:
                session.shutdown_publication_and_drain_durability()
                terminal_results.append(True)
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)
        finally:
            terminal_done.set()

    truncator = threading.Thread(target=_rewind, daemon=True, name="admitted-truncation")
    terminal = threading.Thread(
        target=_terminate,
        daemon=True,
        name=f"{terminal_kind}-terminal",
    )
    with (
        patch.object(storage, "truncate_messages_tail", side_effect=_frozen_truncate),
        patch.object(session._durability_cond, "wait_for", side_effect=_observe_wait),
    ):
        truncator.start()
        assert truncate_entered.wait(5), "truncation did not enter strict storage cut"
        terminal.start()
        try:
            assert terminal_wait_entered.wait(5), "terminal did not enter durability drain"
            with session._durability_cond:
                assert session._durability_serving_ticket < session._durability_next_ticket
            assert terminal_done.is_set() is False
        finally:
            release_truncate.set()
            truncator.join(5)
            terminal.join(5)

    assert not truncator.is_alive() and not terminal.is_alive()
    assert errors == []
    assert truncation_results == [2]
    assert terminal_results == [True]
    assert session._publication_shutdown is True
    assert [row.get("content") for row in dicts_from_turns(session.messages)] == [
        "first request",
        "first answer",
    ]
    assert [
        row.get("content")
        for row in storage.load_messages(
            session.ws_id,
            repair=False,
            include_compaction=True,
        )
    ] == ["first request", "first answer"]


class _ColdCrossingManager:
    """Manager fake whose cold incarnation can be installed exactly once."""

    def __init__(self, live: Workstream) -> None:
        self._lock = threading.Lock()
        self._live: Workstream | None = None
        self._prepared = live
        self.open_called = threading.Event()

    def get(self, ws_id: str) -> Workstream | None:
        assert ws_id == self._prepared.id
        with self._lock:
            return self._live

    def install(self) -> Workstream:
        with self._lock:
            if self._live is None:
                self._live = self._prepared
            return self._live

    def open(self, ws_id: str) -> Workstream:
        assert ws_id == self._prepared.id
        self.open_called.set()
        return self.install()


def test_cold_history_crossing_mid_install_stays_tokenless_and_unrehydrated(tmp_db: Any) -> None:
    """A session installed mid-flight never lends its token to a stale snapshot.

    Deliberate pin update: /history no longer rehydrates cold rows, so the
    crossing contract inverts — the flight that sampled a cold pool serves
    the storage-only snapshot TOKENLESS (claiming no splice authority over
    the row admitted during its load) and never calls ``mgr.open``. The
    admitted row reaches panes through the tokenless bootstrap's clear_ui
    convergence, or a later request whose ``mgr.get`` sees the session.
    """

    ws_id = "ws-cold-history-crossing"
    register_workstream(ws_id, kind="interactive", user_id="test-user")
    save_message(ws_id, "user", "durable prefix")
    session = make_session(ws_id=ws_id, user_id="test-user")
    live = Workstream(
        id=ws_id,
        user_id="test-user",
        session=session,
        ui=session.ui,
    )
    manager = _ColdCrossingManager(live)
    storage = get_storage()
    real_load = storage.load_messages
    load_entered = threading.Event()
    release_load = threading.Event()
    load_count = 0
    load_count_lock = threading.Lock()

    def _frozen_load(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        nonlocal load_count
        rows = real_load(*args, **kwargs)
        with load_count_lock:
            load_count += 1
            this_load = load_count
        if this_load == 1:
            load_entered.set()
            assert release_load.wait(5), "test did not release frozen cold history load"
        return rows

    client = _build_history_app(manager, storage)
    responses: list[Any] = []
    errors: list[BaseException] = []

    def _request_history() -> None:
        try:
            responses.append(client.get(f"/v1/api/workstreams/{ws_id}/history"))
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    requester = threading.Thread(target=_request_history, daemon=True)
    with patch.object(storage, "load_messages", side_effect=_frozen_load):
        requester.start()
        try:
            assert load_entered.wait(5), "cold history did not enter its frozen load"
            manager.install()
            deferred: list[Any] = []
            session._append_user_turn(
                "accepted across rehydrate",
                (),
                deferred_persistence=deferred,
            )
        finally:
            release_load.set()
            requester.join(5)

    assert not requester.is_alive()
    assert errors == []
    assert len(responses) == 1
    response = responses[0]
    assert response.status_code == 200
    body = response.json()
    assert body["handoff_token"] is None
    assert not manager.open_called.is_set()
    assert [message.get("content") for message in body["messages"]] == ["durable prefix"]
    # The admitted row is not lost — the live session's own capture (the path
    # a token-bearing response would take) owns it.
    merged, token = session.capture_history_handoff(
        lambda _overscan: get_storage().load_messages(ws_id, repair=False)
    )
    assert token
    assert [message.get("content") for message in merged] == [
        "durable prefix",
        "accepted across rehydrate",
    ]


@pytest.mark.parametrize("operation", ["rewind", "retry"])
def test_route_mutation_claim_precedes_concurrent_turn_admission(operation: str) -> None:
    """The idle check and destructive mutation must be one worker-slot claim."""

    mutation_entered = threading.Event()
    release_mutation = threading.Event()
    probe_spawned = threading.Event()
    enqueue_refused = threading.Event()
    session = MagicMock()
    reset_events: list[dict[str, Any]] = []

    def _blocking_mutation(
        *_args: Any,
        publish_reset: Any,
    ) -> Any:
        mutation_entered.set()
        assert release_mutation.wait(5), "test did not release history mutation"
        publish_reset()
        return 2 if operation == "rewind" else "second request"

    if operation == "rewind":
        session.rewind.side_effect = _blocking_mutation
    else:
        session.retry.side_effect = _blocking_mutation
    ui = MagicMock()
    ui._enqueue.side_effect = lambda event: reset_events.append(event)
    ws = Workstream(id=f"ws-route-{operation}", session=session, ui=ui)
    manager = MagicMock()
    manager.get.return_value = ws
    if operation == "rewind":
        handler = make_rewind_handler(_verb_cfg(manager))
        client = _verb_client("/api/workstreams/{ws_id}/rewind", handler)
        request = lambda: client.post(  # noqa: E731
            f"/v1/api/workstreams/{ws.id}/rewind",
            json={"turns": 1},
        )
    else:
        handler = make_retry_handler(_verb_cfg(manager))
        client = _verb_client("/api/workstreams/{ws_id}/retry", handler)
        request = lambda: client.post(f"/v1/api/workstreams/{ws.id}/retry")  # noqa: E731

    responses: list[Any] = []
    errors: list[BaseException] = []

    def _request_mutation() -> None:
        try:
            responses.append(request())
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    def _refuse_enqueue() -> None:
        enqueue_refused.set()
        raise queue.Full

    requester = threading.Thread(target=_request_mutation, daemon=True)
    requester.start()
    try:
        assert mutation_entered.wait(5), "route did not enter its history mutation"
        admitted = session_worker.send(
            ws,
            enqueue=_refuse_enqueue,
            run=probe_spawned.set,
            thread_name=f"concurrent-{operation}-probe",
        )
        assert reset_events == []
    finally:
        release_mutation.set()
        requester.join(5)

    assert not requester.is_alive()
    assert errors == []
    assert len(responses) == 1
    assert responses[0].status_code == 200
    assert admitted is False
    assert enqueue_refused.is_set()
    assert not probe_spawned.is_set()
    assert reset_events == [{"type": "clear_ui"}]


@pytest.mark.parametrize("operation", ["rewind", "retry"])
def test_truncation_counts_only_durable_rows_across_live_only_turns(
    tmp_db: Any,
    operation: str,
) -> None:
    """A live-only empty assistant turn must not cost an older durable row.

    The empty completion flows through the real provider path so the
    admission-site ``no_durable_row`` marker — not test scaffolding — carries
    the live/durable asymmetry into the cut accounting.
    """
    from tests._session_helpers import RecordingUI, arm_session
    from turnstone.core.providers import StreamChunk

    ws_id = f"ws-{operation}-live-only-turn"
    register_workstream(ws_id, kind="interactive", user_id="test-user")
    session = make_session(ws_id=ws_id, user_id="test-user", ui=RecordingUI())
    arm_session(
        session,
        iter([StreamChunk(content_delta="first answer", finish_reason="stop")]),
        iter([StreamChunk(finish_reason="stop")]),
    )
    session.send("first request")
    session.send("second request")

    assert session.messages[-1].meta.extra.get("no_durable_row") is True
    storage = get_storage()
    durable_before = [row.get("content") for row in storage.load_messages(ws_id, repair=False)]
    assert durable_before == ["first request", "first answer", "second request"]

    result = _truncate(session, operation)

    if operation == "rewind":
        assert result == 2
    else:
        assert result == "second request"
    durable_after = [row.get("content") for row in storage.load_messages(ws_id, repair=False)]
    assert durable_after == ["first request", "first answer"]
    assert [turn.text for turn in session.messages] == ["first request", "first answer"]
