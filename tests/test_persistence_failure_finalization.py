"""Crossing tests for structural cleanup after a conversation-save failure."""

from __future__ import annotations

import asyncio
import queue
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from starlette.requests import Request

from tests._session_helpers import NullUI, RecordingUI, make_result, make_session
from tests.test_session_manager import _make_manager
from turnstone.core import session_worker
from turnstone.core.session import ConversationPersistenceError
from turnstone.core.session_manager import WorkstreamAlreadyExistsError
from turnstone.core.session_routes import SessionEndpointConfig, make_cancel_handler
from turnstone.core.storage import ConversationCommitConflictError
from turnstone.core.trajectory import Role, ToolCall, Turn


def _run_force_cancel_handler(
    handler: Any,
    ws_id: str,
    *,
    after_to_thread: Any = None,
) -> Any:
    body = b'{"force":true}'
    delivered = False

    async def _receive() -> dict[str, Any]:
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": f"/workstreams/{ws_id}/cancel",
            "raw_path": f"/workstreams/{ws_id}/cancel".encode(),
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("test", 1),
            "server": ("test", 80),
            "path_params": {"ws_id": ws_id},
        },
        _receive,
    )

    async def _inline_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        # The handler's offload boundary is orthogonal to these lock-order
        # tests. Inline it so repeated asyncio.run() loops do not leave the
        # stdlib default executor's teardown in the measured crossing.
        result = func(*args, **kwargs)
        if after_to_thread is not None:
            after_to_thread()
        return result

    with patch("asyncio.to_thread", new=_inline_to_thread):
        return asyncio.run(handler(request))


def _seed_tool_structural_debt(session: Any, call_id: str) -> None:
    with session._generation_lock:
        session._generation = 1
        session.messages.extend(
            (
                Turn.user("use the tool"),
                Turn.assistant(
                    tool_calls=(ToolCall(id=call_id, name="write_file", arguments="{}"),)
                ),
            )
        )
        session._msg_tokens.extend((1, 1))
        session._admit_tool_structural_debt_locked(1, (call_id,))


def _journal_failed_row(session: Any, persist: Any, *, commit_key: str) -> None:
    with session._history_handoff_lock:
        pending = session._journal_conversation_row_locked(
            commit_key=commit_key,
            message={"role": "system", "content": "accepted overlay"},
            persist=persist,
            event_id=None,
        )
    with pytest.raises(ConversationPersistenceError):
        session._persist_pending_conversation_commit(pending)


def test_soft_close_cannot_overtake_failed_assistant_tool_prefix() -> None:
    """A successful close may not strand an assistant tool call without its result."""
    session = make_session(user_id="owner", ui=RecordingUI())
    session._title_generated = True
    assistant_attempts = 0
    conversation_writes: list[str] = []

    def _save_message(
        _ws_id: str,
        role: str,
        _content: str,
        *_args: Any,
        **kwargs: Any,
    ) -> int:
        nonlocal assistant_attempts
        conversation_writes.append(role)
        if role == "assistant" and kwargs.get("tool_calls"):
            assistant_attempts += 1
            if assistant_attempts == 1:
                return 0
        return len(conversation_writes) + 10

    persistence_failed = threading.Event()
    release_failure_handler = threading.Event()
    original_commit = session._commit_for_generation

    def _pause_before_failure_finalizer(*args: Any, **kwargs: Any) -> bool:
        try:
            return original_commit(*args, **kwargs)
        except ConversationPersistenceError:
            # The durability ticket has settled, but send() has not yet admitted
            # its structural failure finalizer. This is the exact window a
            # concurrent soft close must not overtake.
            persistence_failed.set()
            assert release_failure_handler.wait(5)
            raise

    session._commit_for_generation = _pause_before_failure_finalizer  # type: ignore[method-assign]
    tool_call = {
        "id": "call-close-crossing",
        "type": "function",
        "function": {"name": "write_file", "arguments": "{}"},
    }
    send_errors: list[BaseException] = []
    close_results: list[bool] = []

    def _send() -> None:
        try:
            session.send("use the tool", acting_user_id="owner")
        except BaseException as exc:
            send_errors.append(exc)

    with (
        patch("turnstone.core.session.save_message", side_effect=_save_message),
        patch("turnstone.core.memory.persist_last_error"),
        patch.object(session, "_stream_response", return_value=make_result(tool_calls=[tool_call])),
        patch.object(session, "_execute_tools", MagicMock()) as execute_tools,
        patch.object(session, "_visible_memory_count", return_value=0),
        patch.object(session, "_print_status_line"),
    ):
        send_thread = threading.Thread(target=_send, daemon=True)
        send_thread.start()
        assert persistence_failed.wait(5), "assistant save did not reach failure boundary"

        close_thread = threading.Thread(
            target=lambda: close_results.append(session.prepare_soft_close()),
            daemon=True,
        )
        close_thread.start()
        # A correct implementation may either refuse promptly or wait for the
        # structural finalizer. Releasing it makes both choices converge.
        release_failure_handler.set()
        send_thread.join(5)
        close_thread.join(5)

    assert not send_thread.is_alive() and not close_thread.is_alive()
    assert len(send_errors) == 1
    assert isinstance(send_errors[0], ConversationPersistenceError)
    assert close_results in ([False], [True])
    assert [turn.role for turn in session.messages] == [
        Role.USER,
        Role.ASSISTANT,
        Role.TOOL,
    ]
    execute_tools.assert_not_called()
    if close_results == [True]:
        assert session.has_unresolved_conversation_persistence() is False
        assert conversation_writes == ["user", "assistant", "assistant", "tool"]


def test_assistant_journal_rejection_cannot_persist_orphan_tool_suffix() -> None:
    """Failed assistant journal admission rolls back live debt before cleanup."""
    session = make_session(user_id="owner", ui=RecordingUI())
    session._title_generated = True
    writes: list[str] = []

    def _save_message(
        _ws_id: str,
        role: str,
        _content: str,
        *_args: Any,
        **_kwargs: Any,
    ) -> int:
        writes.append(role)
        return len(writes) + 10

    real_journal = session._journal_conversation_row_locked

    def _reject_assistant_journal(**kwargs: Any) -> Any:
        if kwargs["message"].get("role") == "assistant":
            raise RuntimeError("injected assistant journal rejection")
        return real_journal(**kwargs)

    tool_call = {
        "id": "call-journal-rejected",
        "type": "function",
        "function": {"name": "write_file", "arguments": "{}"},
    }
    with (
        patch("turnstone.core.session.save_message", side_effect=_save_message),
        patch("turnstone.core.memory.persist_last_error"),
        patch.object(session, "_stream_response", return_value=make_result(tool_calls=[tool_call])),
        patch.object(session, "_execute_tools", MagicMock()) as execute_tools,
        patch.object(
            session,
            "_journal_conversation_row_locked",
            side_effect=_reject_assistant_journal,
        ),
        patch.object(session, "_visible_memory_count", return_value=0),
        patch.object(session, "_print_status_line"),
        pytest.raises(RuntimeError, match="injected assistant journal rejection"),
    ):
        session.send("use the tool", acting_user_id="owner")

    assert [turn.role for turn in session.messages] == [Role.USER]
    assert writes == ["user"]
    assert session._tool_structural_debt is None
    assert session._pending_conversation_commits == {}
    execute_tools.assert_not_called()


def test_tool_journal_rejection_rolls_back_live_suffix_and_poison_claim() -> None:
    """A TOOL journal exception leaves the accepted assistant debt intact."""
    session = make_session(user_id="owner", ui=RecordingUI())
    session._title_generated = True
    writes: list[str] = []

    def _save_message(
        _ws_id: str,
        role: str,
        _content: str,
        *_args: Any,
        **_kwargs: Any,
    ) -> int:
        writes.append(role)
        return len(writes) + 20

    real_journal = session._journal_conversation_row_locked

    def _reject_tool_journal(**kwargs: Any) -> Any:
        if kwargs["message"].get("role") == "tool":
            raise RuntimeError("injected tool journal rejection")
        return real_journal(**kwargs)

    call_id = "call-tool-journal-rejected"
    tool_call = {
        "id": call_id,
        "type": "function",
        "function": {"name": "write_file", "arguments": "{}"},
    }
    with (
        patch("turnstone.core.session.save_message", side_effect=_save_message),
        patch("turnstone.core.memory.persist_last_error"),
        patch.object(session, "_stream_response", return_value=make_result(tool_calls=[tool_call])),
        patch.object(session, "_execute_tools", return_value=([(call_id, "observed")], "")),
        patch.object(
            session,
            "_journal_conversation_row_locked",
            side_effect=_reject_tool_journal,
        ),
        patch.object(session, "_visible_memory_count", return_value=0),
        patch.object(session, "_print_status_line"),
        pytest.raises(RuntimeError, match="injected tool journal rejection"),
    ):
        session.send("use the tool", acting_user_id="owner")

    assert [turn.role for turn in session.messages] == [Role.USER, Role.ASSISTANT]
    assert writes == ["user", "assistant"]
    assert session._tool_structural_debt is not None
    assert session._cancel_event.is_set()
    with pytest.raises(RuntimeError, match="tool cleanup"):
        session._capture_worker_claim("owner")


def test_failed_structural_finalizer_keeps_dispatch_claim_poisoned() -> None:
    """Cleanup failure poisons dispatch before send's exit finally runs."""
    session = make_session(user_id="owner", ui=RecordingUI())
    session._title_generated = True
    tool_call = {
        "id": "call-finalizer-failed",
        "type": "function",
        "function": {"name": "write_file", "arguments": "{}"},
    }

    before_consume = threading.Event()
    release_consume = threading.Event()
    send_errors: list[BaseException] = []
    real_consume = session._consume_cancel

    def _pause_before_consume(generation: int) -> bool:
        before_consume.set()
        assert release_consume.wait(5)
        return real_consume(generation)

    def _send() -> None:
        try:
            session.send("use the tool", acting_user_id="owner")
        except BaseException as exc:
            send_errors.append(exc)

    with (
        patch("turnstone.core.session.save_message", return_value=19),
        patch("turnstone.core.memory.persist_last_error"),
        patch.object(session, "_stream_response", return_value=make_result(tool_calls=[tool_call])),
        patch.object(session, "_execute_tools", side_effect=RuntimeError("executor failed")),
        patch.object(
            session,
            "_synthesize_cancelled_results",
            side_effect=RuntimeError("injected structural finalizer failure"),
        ),
        patch.object(session, "_consume_cancel", side_effect=_pause_before_consume),
        patch.object(session, "_visible_memory_count", return_value=0),
        patch.object(session, "_print_status_line"),
    ):
        send_thread = threading.Thread(target=_send, daemon=True)
        send_thread.start()
        assert before_consume.wait(5), "send did not reach its exit-finally seam"
        try:
            assert session._tool_structural_debt is not None
            assert session._cancel_event.is_set()
            with pytest.raises(RuntimeError, match="tool cleanup"):
                session._capture_worker_claim("owner")
            with pytest.raises(RuntimeError, match="tool cleanup"):
                session._claim_generation()
        finally:
            release_consume.set()
            send_thread.join(5)

    assert not send_thread.is_alive()
    assert len(send_errors) == 1
    assert "injected structural finalizer failure" in str(send_errors[0])


@pytest.mark.parametrize(
    "resume_before_owner_exit",
    [True, False],
    ids=["poisoned-enqueue", "poisoned-spawn"],
)
def test_pre_failure_worker_claim_cannot_dispatch_after_structural_poison(
    resume_before_owner_exit: bool,
) -> None:
    """A claim captured before cleanup fails must not enqueue or spawn."""
    manager, _adapter, _storage = _make_manager()
    ws = manager.create(user_id="owner", ws_id="stale-structural-claim")
    session = make_session(ws_id=ws.id, user_id="owner", ui=RecordingUI())
    session._title_generated = True
    ws.session = session
    ws.ui = session.ui
    tool_call = {
        "id": "call-stale-claim",
        "type": "function",
        "function": {"name": "write_file", "arguments": "{}"},
    }

    execute_entered = threading.Event()
    release_execute = threading.Event()
    first_run_finished = threading.Event()
    release_first_exit = threading.Event()
    claim_captured = threading.Event()
    release_claim = threading.Event()
    second_enqueued = threading.Event()
    second_started = threading.Event()
    first_errors: list[BaseException] = []
    second_errors: list[BaseException] = []
    dispatch_results: list[bool] = []

    def _fail_execute(*_args: Any, **_kwargs: Any) -> None:
        execute_entered.set()
        assert release_execute.wait(5), "test did not release tool execution"
        raise RuntimeError("injected executor failure")

    def _run_first() -> None:
        try:
            session.send("first", acting_user_id="owner")
        except BaseException as exc:
            first_errors.append(exc)
        finally:
            # Keep the predecessor's workstream slot advertised after its
            # structural finalizer has failed. This makes the enqueue crossing
            # deterministic instead of relying on lock-waiter scheduling.
            first_run_finished.set()
            assert release_first_exit.wait(5), "test did not release first worker exit"

    def _run_second() -> None:
        second_started.set()
        try:
            session.send("second", acting_user_id="owner")
        except BaseException as exc:
            second_errors.append(exc)

    with (
        patch("turnstone.core.session.save_message", return_value=19),
        patch.object(
            session,
            "_stream_response",
            return_value=make_result(tool_calls=[tool_call]),
        ),
        patch.object(session, "_execute_tools", side_effect=_fail_execute),
        patch.object(
            session,
            "_synthesize_cancelled_results",
            side_effect=RuntimeError("injected structural finalizer failure"),
        ),
        patch.object(session, "_visible_memory_count", return_value=0),
        patch.object(session, "_print_status_line"),
    ):
        assert session_worker.send(
            ws,
            enqueue=lambda: None,
            run=_run_first,
            principal_id="owner",
        )
        first_worker = ws.worker_thread
        assert first_worker is not None
        assert execute_entered.wait(5), "first worker did not reach tool execution"

        real_capture = session._capture_worker_claim

        def _capture_then_pause(principal_id: str = "") -> Any:
            claim = real_capture(principal_id)
            claim_captured.set()
            assert release_claim.wait(5), "test did not release stale claim"
            return claim

        def _dispatch_second() -> None:
            dispatch_results.append(
                session_worker.send(
                    ws,
                    enqueue=second_enqueued.set,
                    run=_run_second,
                    principal_id="owner",
                )
            )

        with patch.object(session, "_capture_worker_claim", side_effect=_capture_then_pause):
            dispatcher = threading.Thread(target=_dispatch_second, daemon=True)
            dispatcher.start()
            assert claim_captured.wait(5), "second dispatcher did not capture its claim"

            # The second dispatcher is paused before ``ws._lock``. Let the
            # first worker poison its incomplete structural prefix. Resume the
            # stale dispatcher either while the predecessor still advertises
            # the slot (enqueue arm), or after its runner finally releases the
            # slot (spawn arm).
            release_execute.set()
            assert first_run_finished.wait(5), "first send did not finish"
            assert session._tool_structural_debt is not None
            assert session._cancel_event.is_set()

            if resume_before_owner_exit:
                assert ws._worker_running is True
                release_claim.set()
                dispatcher.join(5)
                assert not dispatcher.is_alive()
                release_first_exit.set()
                first_worker.join(5)
            else:
                release_first_exit.set()
                first_worker.join(5)
                assert not first_worker.is_alive()
                assert ws._worker_running is False
                release_claim.set()
                dispatcher.join(5)
                assert not dispatcher.is_alive()

            assert not first_worker.is_alive()
            dispatcher.join(5)
            assert not dispatcher.is_alive()
            second_worker = ws.worker_thread
            if second_worker is not None and second_worker is not first_worker:
                second_worker.join(5)

    assert first_errors
    assert "injected structural finalizer failure" in str(first_errors[0])
    assert dispatch_results == [False]
    assert not second_enqueued.is_set()
    assert not second_started.is_set()
    assert second_errors == []


def test_worker_claim_after_existing_cancel_edge_can_start_successor() -> None:
    """A post-truncation claim may rotate an Event already set at capture."""
    manager, _adapter, _storage = _make_manager()
    ws = manager.create(user_id="owner", ws_id="post-truncation-claim")
    session = make_session(ws_id=ws.id, user_id="owner", ui=RecordingUI())
    ws.session = session
    ws.ui = session.ui

    # History truncation supersedes the current generation by setting its
    # Event and advancing the generation, but deliberately leaves that Event
    # installed for the next legitimate claim to rotate. A stale-claim check
    # must distinguish this post-edge capture from an unset->set transition
    # that happened after capture.
    with session._generation_lock:
        prior_event = session._cancel_event
        prior_event.set()
        session._generation += 1

    generations: list[int] = []
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            claim = session_worker.current_worker_claim(session)
            assert claim is not None
            generations.append(
                session._claim_generation(
                    principal_id=claim.principal_id,
                    expected_cancel_epoch=claim.cancel_epoch,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    assert session_worker.send(
        ws,
        enqueue=lambda: None,
        run=_run,
        principal_id="owner",
    )
    worker = ws.worker_thread
    assert worker is not None
    worker.join(5)

    assert not worker.is_alive()
    assert errors == []
    assert generations == [2]
    assert session._cancel_event is not prior_event
    assert not session._cancel_event.is_set()


def test_pre_generation_claim_can_enqueue_after_owner_rotates_event() -> None:
    """Normal generation rotation must not invalidate a queued sender."""
    manager, _adapter, _storage = _make_manager()
    ws = manager.create(user_id="owner", ws_id="normal-event-rotation")
    session = make_session(ws_id=ws.id, user_id="owner", ui=RecordingUI())
    ws.session = session
    ws.ui = session.ui

    first_run_entered = threading.Event()
    release_first_claim = threading.Event()
    first_claimed = threading.Event()
    release_first_exit = threading.Event()
    second_claim_captured = threading.Event()
    release_second_claim = threading.Event()
    second_enqueued = threading.Event()
    dispatch_results: list[bool] = []
    generations: list[int] = []

    def _run_first() -> None:
        first_run_entered.set()
        assert release_first_claim.wait(5), "test did not release first claim"
        claim = session_worker.current_worker_claim(session)
        assert claim is not None
        generations.append(
            session._claim_generation(
                principal_id=claim.principal_id,
                expected_cancel_epoch=claim.cancel_epoch,
            )
        )
        first_claimed.set()
        assert release_first_exit.wait(5), "test did not release first worker"

    assert session_worker.send(
        ws,
        enqueue=lambda: None,
        run=_run_first,
        principal_id="owner",
    )
    first_worker = ws.worker_thread
    assert first_worker is not None
    assert first_run_entered.wait(5), "first worker did not enter"

    prior_event = session._cancel_event
    real_capture = session._capture_worker_claim

    def _capture_then_pause(principal_id: str = "") -> Any:
        claim = real_capture(principal_id)
        second_claim_captured.set()
        assert release_second_claim.wait(5), "test did not release second claim"
        return claim

    def _dispatch_second() -> None:
        dispatch_results.append(
            session_worker.send(
                ws,
                enqueue=second_enqueued.set,
                run=lambda: None,
                principal_id="owner",
            )
        )

    with patch.object(session, "_capture_worker_claim", side_effect=_capture_then_pause):
        dispatcher = threading.Thread(target=_dispatch_second, daemon=True)
        dispatcher.start()
        assert second_claim_captured.wait(5), "second dispatcher did not capture"

        release_first_claim.set()
        assert first_claimed.wait(5), "first worker did not rotate its generation"
        assert session._cancel_event is not prior_event
        assert not prior_event.is_set()

        release_second_claim.set()
        dispatcher.join(5)
        assert not dispatcher.is_alive()

    release_first_exit.set()
    first_worker.join(5)

    assert not first_worker.is_alive()
    assert generations == [1]
    assert dispatch_results == [True]
    assert second_enqueued.is_set()


def test_post_synthesis_callback_failure_still_runs_tool_durability_ticket() -> None:
    """A throw after TOOL journal admission cannot discard its storage closure."""
    session = make_session(user_id="owner", ui=RecordingUI())
    session._title_generated = True
    call_id = "call-post-synthesis-throw"
    tool_call = {
        "id": call_id,
        "type": "function",
        "function": {"name": "write_file", "arguments": "{}"},
    }
    writes: list[str] = []

    def _save_message(
        _ws_id: str,
        role: str,
        _content: str,
        *_args: Any,
        **_kwargs: Any,
    ) -> int:
        writes.append(role)
        return len(writes) + 90

    real_synthesize = session._synthesize_cancelled_results

    def _synthesize_then_raise(*args: Any, **kwargs: Any) -> None:
        real_synthesize(*args, **kwargs)
        raise RuntimeError("injected post-synthesis failure")

    with (
        patch("turnstone.core.session.save_message", side_effect=_save_message),
        patch("turnstone.core.memory.persist_last_error"),
        patch.object(session, "_stream_response", return_value=make_result(tool_calls=[tool_call])),
        patch.object(session, "_execute_tools", return_value=([(call_id, "observed")], "")),
        patch.object(
            session,
            "_synthesize_cancelled_results",
            side_effect=_synthesize_then_raise,
        ),
        patch.object(session, "_visible_memory_count", return_value=0),
        patch.object(session, "_print_status_line"),
        pytest.raises(RuntimeError, match="injected post-synthesis failure"),
    ):
        session.send("use the tool", acting_user_id="owner")

    assert [turn.role for turn in session.messages] == [
        Role.USER,
        Role.ASSISTANT,
        Role.TOOL,
    ]
    assert writes == ["user", "assistant", "tool"]
    assert session._pending_conversation_commits == {}
    assert session._tool_structural_debt is None
    assert session._claim_generation() > 0


def test_manager_sweep_attempts_a_pending_journal_without_retry_metadata() -> None:
    """A pending-only head gets a maintenance arm even before its first failure."""
    manager, _adapter, _storage = _make_manager()
    ws = manager.create(user_id="owner", ws_id="pending-only-maintenance")
    session = make_session(ws_id=ws.id, user_id="owner", ui=RecordingUI())
    ws.session = session
    persist = MagicMock(return_value=101)
    with session._history_handoff_lock:
        session._journal_conversation_row_locked(
            commit_key="pending-only-key",
            message={"role": "system", "content": "accepted overlay"},
            persist=persist,
            event_id=None,
        )

    assert session.conversation_persistence_status()["state"] == "pending"
    assert manager.reconcile_unresolved_persistence(now=float("inf")) == [ws.id]
    persist.assert_called_once_with()
    assert session.conversation_persistence_status()["state"] == "healthy"


def test_ambiguous_hard_delete_hides_structural_debt_until_exact_retry() -> None:
    """Ambiguous delete retains debt without a fresh ws-id-only TOOL write."""
    manager, adapter, storage = _make_manager(max_active=1)
    ws = manager.create(user_id="owner", ws_id="hard-delete-tool-tombstone")
    session = make_session(ws_id=ws.id, user_id="owner", ui=RecordingUI())
    ws.session = session
    call_id = "hard-delete-unknown"
    _seed_tool_structural_debt(session, call_id)

    release_worker = threading.Event()
    worker = threading.Thread(target=release_worker.wait, daemon=True, name="blocked-tool")
    worker.start()
    with ws._lock:
        ws._worker_running = True
        ws.worker_thread = worker
        ws._worker_force_abandonable = True

    tool_save = MagicMock(return_value=0)

    def _raise_delete() -> bool:
        raise RuntimeError("injected durable delete failure")

    try:
        with (
            patch("turnstone.core.session.save_message", tool_save),
            pytest.raises(RuntimeError, match="injected durable delete failure"),
        ):
            manager.delete_persisted(
                ws.id,
                delete_fn=_raise_delete,
                expected_reservation_token=ws._fork_reservation_token,
            )

        assert [turn.role for turn in session.messages] == [Role.USER, Role.ASSISTANT]
        assert session._tool_structural_debt is not None
        assert session.conversation_persistence_status()["state"] == "pending"
        tool_save.assert_not_called()

        assert manager.get(ws.id) is None
        assert manager.list_all() == []
        assert manager.count == 0
        assert manager.open(ws.id) is None
        assert manager._failed_delete_tombstones[ws.id] is ws
        assert [event for event in adapter.events if event.kind == "closed"] == []
        with pytest.raises(WorkstreamAlreadyExistsError, match="retiring"):
            manager._reserve_and_install(
                ws.id,
                user_id="replacement",
                name="replacement",
            )

        peer = manager.create(user_id="peer", ws_id="capacity-peer")
        assert manager.count == 1
        assert manager.get(peer.id) is peer

        assert manager.delete_persisted(
            ws.id,
            delete_fn=lambda: storage.delete_workstream_if_fork_reserved(
                ws.id,
                ws._fork_reservation_token,
            ),
            expected_reservation_token=ws._fork_reservation_token,
        )
        assert ws.id not in manager._failed_delete_tombstones
        closed = [event for event in adapter.events if event.kind == "closed"]
        assert [(event.ws_id, event.reason) for event in closed] == [(ws.id, "deleted")]
    finally:
        release_worker.set()
        worker.join(5)


@pytest.mark.parametrize("failure_kind", ["transient", "conflict"])
def test_false_delete_retains_same_incarnation_unresolved_tombstone(
    failure_kind: str,
) -> None:
    """False is not proof enough to discard a surviving row's repair owner."""
    manager, adapter, _storage = _make_manager()
    ws = manager.create(user_id="owner", ws_id=f"false-delete-{failure_kind}")
    session = make_session(ws_id=ws.id, user_id="owner", ui=RecordingUI())
    ws.session = session
    persist = (
        MagicMock(return_value=0)
        if failure_kind == "transient"
        else MagicMock(side_effect=ConversationCommitConflictError("immutable mismatch"))
    )
    _journal_failed_row(session, persist, commit_key=f"false-{failure_kind}-row")
    delete = MagicMock(return_value=False)

    assert (
        manager.delete_persisted(
            ws.id,
            delete_fn=delete,
            expected_reservation_token=ws._fork_reservation_token,
        )
        is False
    )

    delete.assert_called_once_with()
    persist.assert_called_once_with()
    assert manager._failed_delete_tombstones[ws.id] is ws
    assert manager.get(ws.id) is None
    assert adapter.cleaned_up == []
    assert [event for event in adapter.events if event.kind == "closed"] == []


@pytest.mark.parametrize("durable_outcome", ["missing", "different"])
def test_false_delete_retires_only_proven_old_incarnation(
    durable_outcome: str,
) -> None:
    """A conforming missing/different false retires without journal replay."""
    manager, adapter, storage = _make_manager()
    ws = manager.create(user_id="owner", ws_id=f"false-delete-{durable_outcome}")
    session = make_session(ws_id=ws.id, user_id="owner", ui=RecordingUI())
    ws.session = session
    persist = MagicMock(return_value=0)
    _journal_failed_row(session, persist, commit_key=f"false-{durable_outcome}-row")

    def _false_after_durable_change() -> bool:
        if durable_outcome == "missing":
            storage.delete_workstream(ws.id)
        else:
            storage.fork_reservations[ws.id] = "replacement-token"
        return False

    assert (
        manager.delete_persisted(
            ws.id,
            delete_fn=_false_after_durable_change,
            expected_reservation_token=ws._fork_reservation_token,
        )
        is False
    )

    assert ws.id not in manager._failed_delete_tombstones
    persist.assert_called_once_with()
    closed = [event for event in adapter.events if event.kind == "closed"]
    assert closed == []
    assert manager.reconcile_unresolved_persistence(now=float("inf")) == []
    assert closed == []


def test_delete_ack_loss_probe_cannot_close_remote_successor() -> None:
    """A remote B created after the missing probe receives no late A close."""
    manager, adapter, storage = _make_manager()
    ws = manager.create(user_id="owner", ws_id="delete-ack-loss-inline")
    session = make_session(ws_id=ws.id, user_id="owner", ui=RecordingUI())
    ws.session = session
    persist = MagicMock(return_value=0)
    _journal_failed_row(session, persist, commit_key="ack-loss-row")

    def _delete_then_raise() -> bool:
        assert storage.delete_workstream_if_fork_reserved(
            ws.id,
            ws._fork_reservation_token,
        )
        raise RuntimeError("delete ACK lost")

    real_disposition = manager._failed_delete_durable_disposition

    def _probe_then_create_successor(candidate: Any) -> str:
        disposition = real_disposition(candidate)
        assert disposition == "missing"
        storage.register_workstream(
            ws.id,
            user_id="remote-owner",
            name="remote successor",
            fork_reservation_token="remote-successor-token",
        )
        return disposition

    with (
        patch.object(
            manager,
            "_failed_delete_durable_disposition",
            side_effect=_probe_then_create_successor,
        ),
        pytest.raises(RuntimeError, match="delete ACK lost"),
    ):
        manager.delete_persisted(
            ws.id,
            delete_fn=_delete_then_raise,
            expected_reservation_token=ws._fork_reservation_token,
        )

    assert ws.id not in manager._failed_delete_tombstones
    assert storage.fork_reservations[ws.id] == "remote-successor-token"
    assert [event for event in adapter.events if event.kind == "closed"] == []
    assert manager.reconcile_unresolved_persistence(now=float("inf")) == []
    assert [event for event in adapter.events if event.kind == "closed"] == []


def test_hard_delete_never_writes_predecessor_tool_into_remote_successor() -> None:
    """An A-to-B replacement before terminalization receives no A TOOL row."""
    manager, adapter, storage = _make_manager()
    ws = manager.create(user_id="owner", ws_id="delete-tool-incarnation-aba")
    session = make_session(ws_id=ws.id, user_id="owner", ui=RecordingUI())
    ws.session = session
    _seed_tool_structural_debt(session, "predecessor-tool-call")
    predecessor_token = ws._fork_reservation_token

    storage.delete_workstream(ws.id)
    storage.register_workstream(
        ws.id,
        user_id="remote-owner",
        name="remote successor",
        fork_reservation_token="successor-token",
    )
    tool_save = MagicMock(return_value=313)

    with patch("turnstone.core.session.save_message", tool_save):
        assert (
            manager.delete_persisted(
                ws.id,
                delete_fn=lambda: storage.delete_workstream_if_fork_reserved(
                    ws.id,
                    predecessor_token,
                ),
                expected_reservation_token=predecessor_token,
            )
            is False
        )

    tool_save.assert_not_called()
    assert storage.fork_reservations[ws.id] == "successor-token"
    assert ws.id not in manager._failed_delete_tombstones
    assert [event for event in adapter.events if event.kind == "closed"] == []


def test_same_or_unknown_tombstone_never_background_replays_rows() -> None:
    """A token snapshot is not authority to write a predecessor row by ws_id."""
    manager, adapter, storage = _make_manager()
    ws = manager.create(user_id="owner", ws_id="hard-delete-no-background-repair")
    session = make_session(ws_id=ws.id, user_id="owner", ui=RecordingUI())
    ws.session = session
    persist = MagicMock(side_effect=[0, 117])
    _journal_failed_row(session, persist, commit_key="terminal-retrying-row")

    with pytest.raises(RuntimeError, match="ambiguous delete"):
        manager.delete_persisted(
            ws.id,
            delete_fn=MagicMock(side_effect=RuntimeError("ambiguous delete")),
            expected_reservation_token=ws._fork_reservation_token,
        )

    assert manager.reconcile_unresolved_persistence(now=float("inf")) == []
    assert persist.call_count == 1
    assert manager._failed_delete_tombstones[ws.id] is ws

    with patch.object(
        storage,
        "ensure_workstream_incarnation_snapshot",
        side_effect=RuntimeError("snapshot unavailable"),
    ):
        assert manager.reconcile_unresolved_persistence(now=float("inf")) == []
    assert persist.call_count == 1
    assert manager._failed_delete_tombstones[ws.id] is ws
    assert [event for event in adapter.events if event.kind == "closed"] == []


def test_conflicted_delete_tombstone_is_retained_for_explicit_delete() -> None:
    """Permanent commit conflict never self-repairs or leaks onto a successor."""
    manager, adapter, _storage = _make_manager()
    ws = manager.create(user_id="owner", ws_id="hard-delete-conflict-tombstone")
    session = make_session(ws_id=ws.id, user_id="owner", ui=RecordingUI())
    ws.session = session
    persist = MagicMock(side_effect=ConversationCommitConflictError("immutable mismatch"))
    _journal_failed_row(session, persist, commit_key="terminal-conflict-row")

    with pytest.raises(RuntimeError, match="ambiguous delete"):
        manager.delete_persisted(
            ws.id,
            delete_fn=MagicMock(side_effect=RuntimeError("ambiguous delete")),
            expected_reservation_token=ws._fork_reservation_token,
        )

    assert session.conversation_persistence_status()["state"] == "conflict"
    assert manager.reconcile_unresolved_persistence(now=float("inf")) == []
    persist.assert_called_once_with()
    assert manager._failed_delete_tombstones[ws.id] is ws
    assert [event for event in adapter.events if event.kind == "closed"] == []


@pytest.mark.parametrize("durable_outcome", ["missing", "different"])
def test_terminal_maintenance_retires_only_proven_old_incarnation(
    durable_outcome: str,
) -> None:
    """Missing/different probes retire silently without tokenless close."""
    manager, adapter, storage = _make_manager()
    ws = manager.create(user_id="owner", ws_id=f"hard-delete-{durable_outcome}")
    session = make_session(ws_id=ws.id, user_id="owner", ui=RecordingUI())
    ws.session = session
    persist = MagicMock(return_value=0)
    _journal_failed_row(session, persist, commit_key=f"terminal-{durable_outcome}-row")

    with pytest.raises(RuntimeError, match="ambiguous delete"):
        manager.delete_persisted(
            ws.id,
            delete_fn=MagicMock(side_effect=RuntimeError("ambiguous delete")),
            expected_reservation_token=ws._fork_reservation_token,
        )
    assert manager._failed_delete_tombstones[ws.id] is ws

    if durable_outcome == "missing":
        storage.rows.pop(ws.id)
    else:
        storage.fork_reservations[ws.id] = "replacement-token"

    assert manager.reconcile_unresolved_persistence(now=float("inf")) == [ws.id]
    assert ws.id not in manager._failed_delete_tombstones
    persist.assert_called_once_with()
    closed = [event for event in adapter.events if event.kind == "closed"]
    assert closed == []
    assert manager.reconcile_unresolved_persistence(now=float("inf")) == []
    assert closed == []


def test_unadvertised_delete_tombstone_retry_emits_no_close_event() -> None:
    """A deferred create remains event-invisible across a failed delete retry."""
    manager, adapter, storage = _make_manager()
    ws = manager.create(
        user_id="owner",
        ws_id="unadvertised-delete-tombstone",
        defer_emit_created=True,
    )
    session = make_session(ws_id=ws.id, user_id="owner", ui=RecordingUI())
    ws.session = session
    persist = MagicMock(return_value=0)
    _journal_failed_row(session, persist, commit_key="unadvertised-terminal-row")

    with pytest.raises(RuntimeError, match="ambiguous delete"):
        manager.delete_persisted(
            ws.id,
            delete_fn=MagicMock(side_effect=RuntimeError("ambiguous delete")),
            expected_reservation_token=ws._fork_reservation_token,
        )
    assert ws.id in manager._failed_delete_unadvertised
    assert adapter.events == []

    assert manager.delete_persisted(
        ws.id,
        delete_fn=lambda: storage.delete_workstream_if_fork_reserved(
            ws.id,
            ws._fork_reservation_token,
        ),
        expected_reservation_token=ws._fork_reservation_token,
    )
    assert ws.id not in manager._failed_delete_tombstones
    assert ws.id not in manager._failed_delete_unadvertised
    assert adapter.events == []


def test_unadvertised_predecessor_does_not_suppress_successor_delete_event() -> None:
    """A hidden A's birth metadata cannot suppress an advertised B close."""
    manager, adapter, storage = _make_manager()
    ws = manager.create(
        user_id="owner",
        ws_id="unadvertised-predecessor-advertised-successor",
        defer_emit_created=True,
    )
    session = make_session(ws_id=ws.id, user_id="owner", ui=RecordingUI())
    ws.session = session
    persist = MagicMock(return_value=0)
    _journal_failed_row(session, persist, commit_key="unadvertised-predecessor-row")

    with pytest.raises(RuntimeError, match="ambiguous delete"):
        manager.delete_persisted(
            ws.id,
            delete_fn=MagicMock(side_effect=RuntimeError("ambiguous delete")),
            expected_reservation_token=ws._fork_reservation_token,
        )
    assert ws.id in manager._failed_delete_unadvertised

    storage.delete_workstream(ws.id)
    storage.register_workstream(
        ws.id,
        user_id="successor-owner",
        name="advertised successor",
        fork_reservation_token="advertised-successor-token",
    )
    assert manager.delete_persisted(
        ws.id,
        delete_fn=lambda: storage.delete_workstream_if_fork_reserved(
            ws.id,
            "advertised-successor-token",
        ),
        expected_reservation_token="advertised-successor-token",
    )

    closed = [event for event in adapter.events if event.kind == "closed"]
    assert [(event.ws_id, event.reason, event.name) for event in closed] == [
        (ws.id, "deleted", "advertised successor")
    ]


def test_retained_delete_tombstone_quiesces_sse_without_destroying_journal() -> None:
    """Hidden terminal state unwinds listeners but preserves repair ownership."""
    manager, adapter, _storage = _make_manager()
    ws = manager.create(user_id="owner", ws_id="delete-tombstone-listener-quiesce")
    session = make_session(ws_id=ws.id, user_id="owner", ui=RecordingUI())
    ws.session = session
    persist = MagicMock(return_value=0)
    _journal_failed_row(session, persist, commit_key="listener-quiesce-row")

    listener: queue.Queue[dict[str, Any]] = queue.Queue()

    class _ListenerUI:
        def __init__(self) -> None:
            self._listeners_lock = threading.Lock()
            self._listeners = [listener]

    listener_ui = _ListenerUI()
    ws.ui = listener_ui  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="ambiguous delete"):
        manager.delete_persisted(
            ws.id,
            delete_fn=MagicMock(side_effect=RuntimeError("ambiguous delete")),
            expected_reservation_token=ws._fork_reservation_token,
        )

    assert listener.get_nowait() == {"type": "ws_closed"}
    assert listener_ui._listeners == []
    assert manager._failed_delete_tombstones[ws.id] is ws
    assert session.has_unresolved_conversation_persistence()
    assert adapter.cleaned_up == []


@pytest.mark.parametrize("registration_kind", ["direct", "snapshot", "replay"])
def test_stale_listener_registration_after_tombstone_is_preclosed(
    registration_kind: str,
) -> None:
    """Every registration seam observes terminal quiesce under its UI lock."""
    manager, _adapter, _storage = _make_manager()
    ws = manager.create(
        user_id="owner",
        ws_id=f"delete-stale-listener-{registration_kind}",
    )
    ui = NullUI()
    session = make_session(ws_id=ws.id, user_id="owner", ui=ui)
    ws.session = session
    ws.ui = ui
    persist = MagicMock(return_value=0)
    _journal_failed_row(session, persist, commit_key=f"stale-{registration_kind}-row")

    request_has_ws = threading.Event()
    release_registration = threading.Event()
    registered: list[queue.Queue[dict[str, Any]]] = []

    def _register_after_stale_lookup() -> None:
        request_has_ws.set()
        assert release_registration.wait(5)
        if registration_kind == "direct":
            listener = ui._register_listener()
        elif registration_kind == "snapshot":
            listener, _snapshot = ui.register_listener_with_in_progress_snapshot()
        else:
            listener, *_rest = ui.register_listener_with_replay(0)
        registered.append(listener)

    register_thread = threading.Thread(target=_register_after_stale_lookup, daemon=True)
    register_thread.start()
    assert request_has_ws.wait(5)
    try:
        with pytest.raises(RuntimeError, match="ambiguous delete"):
            manager.delete_persisted(
                ws.id,
                delete_fn=MagicMock(side_effect=RuntimeError("ambiguous delete")),
                expected_reservation_token=ws._fork_reservation_token,
            )
    finally:
        release_registration.set()
        register_thread.join(5)

    assert not register_thread.is_alive()
    assert len(registered) == 1
    assert registered[0].get_nowait() == {"type": "ws_closed"}
    assert registered[0] not in ui._listeners
    assert ui._listeners_terminal is True
    assert manager._failed_delete_tombstones[ws.id] is ws


def test_soft_close_cannot_skip_active_tool_cancellation_finalizer() -> None:
    """Soft close must complete an already-accepted assistant/tool block."""
    session = make_session(user_id="owner", ui=RecordingUI())
    session._title_generated = True
    tool_entered = threading.Event()
    release_tool = threading.Event()
    conversation_writes: list[str] = []

    def _save_message(
        _ws_id: str,
        role: str,
        _content: str,
        *_args: Any,
        **_kwargs: Any,
    ) -> int:
        conversation_writes.append(role)
        return len(conversation_writes) + 20

    def _execute_tools(*_args: Any, **_kwargs: Any) -> tuple[list[tuple[str, str]], str]:
        tool_entered.set()
        assert release_tool.wait(5)
        return [("call-active-close", "tool completed")], ""

    tool_call = {
        "id": "call-active-close",
        "type": "function",
        "function": {"name": "write_file", "arguments": "{}"},
    }
    send_errors: list[BaseException] = []

    def _send() -> None:
        try:
            session.send("use the tool", acting_user_id="owner")
        except BaseException as exc:
            send_errors.append(exc)

    with (
        patch("turnstone.core.session.save_message", side_effect=_save_message),
        patch("turnstone.core.memory.persist_last_error"),
        patch.object(session, "_stream_response", return_value=make_result(tool_calls=[tool_call])),
        patch.object(session, "_execute_tools", side_effect=_execute_tools),
        patch.object(session, "_visible_memory_count", return_value=0),
        patch.object(session, "_print_status_line"),
    ):
        send_thread = threading.Thread(target=_send, daemon=True)
        send_thread.start()
        assert tool_entered.wait(5), "tool execution did not start"

        # Current close returns before the timer fires; a safe implementation
        # may wait for the cancellation finalizer. The timer makes both choices
        # deterministic without coupling the test to one policy.
        release_timer = threading.Timer(0.1, release_tool.set)
        release_timer.start()
        close_result = session.prepare_soft_close()
        release_timer.join(5)
        send_thread.join(5)

    assert not send_thread.is_alive()
    assert send_errors == []
    assert close_result in (False, True)
    assert [turn.role for turn in session.messages] == [
        Role.USER,
        Role.ASSISTANT,
        Role.TOOL,
    ]
    if close_result:
        assert session.has_unresolved_conversation_persistence() is False
        assert conversation_writes == ["user", "assistant", "tool"]


def test_force_successor_cannot_cross_an_incomplete_tool_prefix() -> None:
    """A force successor must refuse or synthesize before it can claim history."""
    session = make_session(user_id="owner", ui=RecordingUI())
    session._title_generated = True
    tool_entered = threading.Event()
    release_tool = threading.Event()
    conversation_writes: list[str] = []

    def _save_message(
        _ws_id: str,
        role: str,
        _content: str,
        *_args: Any,
        **_kwargs: Any,
    ) -> int:
        conversation_writes.append(role)
        return len(conversation_writes) + 30

    def _execute_tools(*_args: Any, **_kwargs: Any) -> tuple[list[tuple[str, str]], str]:
        tool_entered.set()
        assert release_tool.wait(5)
        return [("call-force-crossing", "late tool result")], ""

    tool_call = {
        "id": "call-force-crossing",
        "type": "function",
        "function": {"name": "write_file", "arguments": "{}"},
    }
    send_errors: list[BaseException] = []

    def _send() -> None:
        try:
            session.send("use the tool", acting_user_id="owner")
        except BaseException as exc:
            send_errors.append(exc)

    claim_generation: int | None = None
    claim_error: Exception | None = None
    prefix_complete_at_claim = False
    with (
        patch("turnstone.core.session.save_message", side_effect=_save_message),
        patch("turnstone.core.memory.persist_last_error"),
        patch.object(session, "_stream_response", return_value=make_result(tool_calls=[tool_call])),
        patch.object(session, "_execute_tools", side_effect=_execute_tools),
        patch.object(session, "_visible_memory_count", return_value=0),
        patch.object(session, "_print_status_line"),
    ):
        send_thread = threading.Thread(target=_send, daemon=True)
        send_thread.start()
        assert tool_entered.wait(5), "tool execution did not start"

        session.cancel()
        with pytest.raises(RuntimeError, match="tool cleanup"):
            session._claim_generation()
        direct_mutated: list[bool] = []
        assert (
            session._commit_for_generation(
                0,
                lambda _durable: direct_mutated.append(True),
            )
            is False
        )
        assert direct_mutated == []
        try:
            claim_generation = session._claim_generation()
        except Exception as exc:
            claim_error = exc
        else:
            prefix_complete_at_claim = [turn.role for turn in session.messages] == [
                Role.USER,
                Role.ASSISTANT,
                Role.TOOL,
            ]
        finally:
            release_tool.set()
            send_thread.join(5)

    assert not send_thread.is_alive()
    assert send_errors == []
    assert (claim_generation is None) is (claim_error is not None)
    if claim_generation is not None:
        assert prefix_complete_at_claim, "successor claimed an incomplete accepted prefix"
    assert [turn.role for turn in session.messages] == [
        Role.USER,
        Role.ASSISTANT,
        Role.TOOL,
    ]


def test_structural_repair_counts_duplicate_tool_ids_by_occurrence() -> None:
    """One existing result must not hide a second call with the same provider ID."""
    session = make_session(user_id="owner", ui=RecordingUI())
    duplicate_id = "provider-duplicate"
    assistant = Turn.assistant(
        tool_calls=(
            ToolCall(id=duplicate_id, name="first", arguments="{}"),
            ToolCall(id=duplicate_id, name="second", arguments="{}"),
        )
    )
    existing_result = Turn.tool(duplicate_id, "first result")
    with session._generation_lock:
        session._generation = 1
        session.messages.extend((assistant, existing_result))
        session._msg_tokens.extend((1, 1))
        session._admit_tool_structural_debt_locked(1, (duplicate_id, duplicate_id))

    writes: list[tuple[str, str | None]] = []

    def _save_message(
        _ws_id: str,
        role: str,
        _content: str,
        *_args: Any,
        **kwargs: Any,
    ) -> int:
        writes.append((role, kwargs.get("tool_call_id")))
        return 73

    def _repair(durable: list[Any]) -> None:
        session._synthesize_cancelled_results(
            "Tool execution returned no observable outcome.",
            deferred_persistence=durable,
            structural_generation=1,
        )

    with patch("turnstone.core.session.save_message", side_effect=_save_message):
        assert session._commit_for_generation(1, _repair) is True

    assert [turn.role for turn in session.messages] == [
        Role.ASSISTANT,
        Role.TOOL,
        Role.TOOL,
    ]
    assert [turn.tool_call_id for turn in session.messages[1:]] == [
        duplicate_id,
        duplicate_id,
    ]
    assert writes == [("tool", duplicate_id)]
    assert session._tool_structural_debt is None


def test_structural_debt_refuses_an_extra_tool_occurrence() -> None:
    """Completion is exact: an extra TOOL row cannot retire the generation."""
    session = make_session(user_id="owner", ui=RecordingUI())
    with session._generation_lock:
        session._generation = 1
        session._admit_tool_structural_debt_locked(1, ("exact-call",))
        with pytest.raises(RuntimeError, match=r"unexpected: exact-call"):
            session._complete_tool_structural_debt_locked(
                1,
                ("exact-call", "exact-call"),
            )
        assert session._tool_structural_debt is not None


def test_soft_close_timeout_rolls_back_latch_until_force_repairs_debt() -> None:
    """A refused close stays recoverable without admitting a successor early."""
    session = make_session(user_id="owner", ui=RecordingUI())
    call_id = "close-timeout-debt"
    with session._generation_lock:
        session._generation = 1
        session.messages.extend(
            (
                Turn.user("use the tool"),
                Turn.assistant(
                    tool_calls=(ToolCall(id=call_id, name="write_file", arguments="{}"),)
                ),
            )
        )
        session._msg_tokens.extend((1, 1))
        session._admit_tool_structural_debt_locked(1, (call_id,))

    with patch("turnstone.core.session._SOFT_CLOSE_STRUCTURAL_WAIT_SECONDS", 0.001):
        assert session.prepare_soft_close() is False

    assert session._soft_close_preparing is False
    assert session._publication_shutdown is False
    assert session._tool_structural_debt is not None
    with pytest.raises(RuntimeError, match="tool cleanup"):
        session._claim_generation()

    observed_at_clear: list[list[Role]] = []

    def _clear() -> bool:
        observed_at_clear.append([turn.role for turn in session.messages])
        return True

    with patch("turnstone.core.session.save_message", return_value=81):
        abandoned, persistence_error = session.force_abandon_generation(
            target_is_current=lambda: True,
            clear_target=_clear,
            publish_abandoned=lambda: None,
        )

    assert abandoned is True
    assert persistence_error is None
    assert observed_at_clear == [[Role.USER, Role.ASSISTANT, Role.TOOL]]
    assert session._tool_structural_debt is None
    assert session._claim_generation() > 0


@pytest.mark.parametrize("tool_row_id", [43, 0], ids=["healthy", "poisoned"])
def test_force_handler_journals_unknown_before_slot_release(tool_row_id: int) -> None:
    """HTTP force-abandon closes structural debt before exposing the slot."""
    manager, _adapter, _storage = _make_manager()
    ws = manager.create(user_id="owner", ws_id=f"force-prefix-{tool_row_id}")
    ui = RecordingUI()
    ui._enqueue = lambda _event: None  # type: ignore[attr-defined]
    session = make_session(ws_id=ws.id, user_id="owner", ui=ui)
    session._title_generated = True
    ws.session = session
    ws.ui = ui
    tool_entered = threading.Event()
    release_tool = threading.Event()
    writes: list[str] = []

    def _save_message(
        _ws_id: str,
        role: str,
        _content: str,
        *_args: Any,
        **_kwargs: Any,
    ) -> int:
        writes.append(role)
        if role == "tool":
            return tool_row_id
        return len(writes) + 50

    def _execute_tools(*_args: Any, **_kwargs: Any) -> tuple[list[tuple[str, str]], str]:
        tool_entered.set()
        assert release_tool.wait(5)
        return [("call-force-handler", "late result")], ""

    tool_call = {
        "id": "call-force-handler",
        "type": "function",
        "function": {"name": "write_file", "arguments": "{}"},
    }
    send_errors: list[BaseException] = []

    def _send() -> None:
        try:
            session.send("use the tool", acting_user_id="owner")
        except BaseException as exc:
            send_errors.append(exc)

    observed_at_clear: list[tuple[list[Role], str, int]] = []
    real_force_abandon = session.force_abandon_generation

    def _force_abandon_with_clear_probe(**kwargs: Any) -> Any:
        clear_target = kwargs["clear_target"]

        def _checked_clear() -> bool:
            status = session.conversation_persistence_status()
            observed_at_clear.append(
                (
                    [turn.role for turn in session.messages],
                    str(status["state"]),
                    int(status["pending_rows"]),
                )
            )
            return clear_target()

        kwargs["clear_target"] = _checked_clear
        return real_force_abandon(**kwargs)

    session.force_abandon_generation = _force_abandon_with_clear_probe  # type: ignore[method-assign]
    cfg = SessionEndpointConfig(
        permission_gate=None,
        manager_lookup=lambda _request: (manager, None),
        tenant_check=None,
        not_found_label="not found",
        audit_action_prefix="workstream",
    )
    handler = make_cancel_handler(cfg)

    with (
        patch("turnstone.core.session.save_message", side_effect=_save_message),
        patch("turnstone.core.memory.persist_last_error"),
        patch.object(session, "_stream_response", return_value=make_result(tool_calls=[tool_call])),
        patch.object(session, "_execute_tools", side_effect=_execute_tools),
        patch.object(session, "_visible_memory_count", return_value=0),
        patch.object(session, "_print_status_line"),
    ):
        send_thread = threading.Thread(target=_send, daemon=True)
        send_thread.start()
        assert tool_entered.wait(5), "tool execution did not start"
        with ws._lock:
            ws._worker_running = True
            ws.worker_thread = send_thread
            ws._worker_force_abandonable = True

        response = _run_force_cancel_handler(handler, ws.id)
        assert response.status_code == 200
        assert observed_at_clear == [([Role.USER, Role.ASSISTANT, Role.TOOL], "pending", 1)]
        assert ws._worker_running is False
        assert ws.worker_thread is None
        assert session.messages[-1].effect_status is not None
        assert session.messages[-1].effect_status.value == "unknown"
        assert session._claim_generation() > 0

        release_tool.set()
        send_thread.join(5)

    assert not send_thread.is_alive()
    assert send_errors == []
    assert [turn.role for turn in session.messages] == [
        Role.USER,
        Role.ASSISTANT,
        Role.TOOL,
    ]
    expected_state = "healthy" if tool_row_id else "retrying"
    assert session.conversation_persistence_status()["state"] == expected_state


@pytest.mark.parametrize("tool_row_id", [71, 0], ids=["healthy", "poisoned"])
def test_force_terminal_ui_precedes_successor_generation_publication(
    tool_row_id: int,
) -> None:
    """Force publishes stream-end/idle before a cleared slot can think."""
    manager, _adapter, _storage = _make_manager()
    ws = manager.create(user_id="owner", ws_id=f"force-ui-order-{tool_row_id}")
    ui = RecordingUI()
    ordered_events: list[str] = []
    ui._enqueue = lambda event: ordered_events.append(str(event["type"]))  # type: ignore[attr-defined]
    ui.on_state_change = lambda state: ordered_events.append(str(state))  # type: ignore[method-assign]
    session = make_session(ws_id=ws.id, user_id="owner", ui=ui)
    ws.session = session
    ws.ui = ui
    _seed_tool_structural_debt(session, "force-ui-order-call")
    predecessor = threading.Thread(target=lambda: None, name="force-ui-predecessor")
    with ws._lock:
        ws._worker_running = True
        ws.worker_thread = predecessor
        ws._worker_force_abandonable = True

    save_entered = threading.Event()
    release_save = threading.Event()
    force_returned = threading.Event()
    release_route = threading.Event()
    successor_published = threading.Event()
    force_responses: list[Any] = []

    def _save_message(
        _ws_id: str,
        role: str,
        _content: str,
        *_args: Any,
        **_kwargs: Any,
    ) -> int:
        if role == "tool":
            save_entered.set()
            assert release_save.wait(5)
            return tool_row_id
        return 1

    def _after_force_method() -> None:
        force_returned.set()
        assert release_route.wait(5)

    cfg = SessionEndpointConfig(
        permission_gate=None,
        manager_lookup=lambda _request: (manager, None),
        tenant_check=None,
        not_found_label="not found",
        audit_action_prefix="workstream",
    )
    handler = make_cancel_handler(cfg)

    def _run_force() -> None:
        force_responses.append(
            _run_force_cancel_handler(
                handler,
                ws.id,
                after_to_thread=_after_force_method,
            )
        )

    def _run_successor() -> None:
        session._claim_generation()
        ordered_events.append("thinking")
        successor_published.set()

    with patch("turnstone.core.session.save_message", side_effect=_save_message):
        force_thread = threading.Thread(target=_run_force, daemon=True)
        force_thread.start()
        assert save_entered.wait(5), "force TOOL durability did not block"

        assert session_worker.send(
            ws,
            enqueue=lambda: None,
            run=_run_successor,
            principal_id="owner",
        )
        successor_thread = ws.worker_thread
        assert successor_thread is not None
        assert not successor_published.wait(0.05)

        release_save.set()
        assert force_returned.wait(5), "force method did not settle"
        assert successor_published.wait(5), "successor did not claim after force"
        assert ordered_events[:3] == ["stream_end", "idle", "thinking"]

        release_route.set()
        force_thread.join(5)
        successor_thread.join(5)

    assert not force_thread.is_alive() and not successor_thread.is_alive()
    assert force_responses[0].status_code == 200
    assert ordered_events.count("stream_end") == 1
    assert ordered_events.count("idle") == 1


def test_force_handler_stale_target_cannot_clear_replacement() -> None:
    """The production force callbacks revalidate the exact pinned worker."""
    manager, _adapter, _storage = _make_manager()
    ws = manager.create(user_id="owner", ws_id="force-stale-target")
    ui = RecordingUI()
    ui._enqueue = lambda _event: None  # type: ignore[attr-defined]
    session = make_session(ws_id=ws.id, user_id="owner", ui=ui)
    ws.session = session
    ws.ui = ui
    predecessor = threading.Thread(target=lambda: None, name="predecessor")
    successor = threading.Thread(target=lambda: None, name="successor")
    with ws._lock:
        ws._worker_running = True
        ws.worker_thread = predecessor
        ws._worker_principal_id = "alice"

    def _cancel_and_replace() -> None:
        with ws._lock:
            ws.worker_thread = successor
            ws._worker_running = True
            ws._worker_principal_id = "bob"

    session.cancel = _cancel_and_replace  # type: ignore[method-assign]
    cfg = SessionEndpointConfig(
        permission_gate=None,
        manager_lookup=lambda _request: (manager, None),
        tenant_check=None,
        not_found_label="not found",
        audit_action_prefix="workstream",
    )
    handler = make_cancel_handler(cfg)

    generation_before = session._generation
    response = _run_force_cancel_handler(handler, ws.id)

    assert response.status_code == 200
    assert ws.worker_thread is successor
    assert ws._worker_running is True
    assert ws._worker_principal_id == "bob"
    assert session._generation == generation_before


def test_force_handler_repairs_idle_structural_debt_but_idle_noop_stays_silent() -> None:
    """Failed idle force stays poisoned; its retry is the recovery arm."""
    manager, _adapter, _storage = _make_manager()
    ws = manager.create(user_id="owner", ws_id="force-idle-debt")
    ui = RecordingUI()
    events: list[dict[str, Any]] = []
    ui._enqueue = events.append  # type: ignore[attr-defined]
    session = make_session(ws_id=ws.id, user_id="owner", ui=ui)
    ws.session = session
    ws.ui = ui
    _seed_tool_structural_debt(session, "idle-debt-call")
    session._cancel_event.set()

    cfg = SessionEndpointConfig(
        permission_gate=None,
        manager_lookup=lambda _request: (manager, None),
        tenant_check=None,
        not_found_label="not found",
        audit_action_prefix="workstream",
    )
    handler = make_cancel_handler(cfg)
    real_journal = session._journal_conversation_row_locked
    rejected = False

    def _reject_first_tool_journal(**kwargs: Any) -> Any:
        nonlocal rejected
        if kwargs["message"].get("role") == "tool" and not rejected:
            rejected = True
            raise RuntimeError("injected idle-force journal failure")
        return real_journal(**kwargs)

    with (
        patch("turnstone.core.session.save_message", return_value=131) as save,
        patch.object(
            session,
            "_journal_conversation_row_locked",
            side_effect=_reject_first_tool_journal,
        ),
        pytest.raises(RuntimeError, match="injected idle-force journal failure"),
    ):
        _run_force_cancel_handler(handler, ws.id)

    assert [turn.role for turn in session.messages] == [Role.USER, Role.ASSISTANT]
    assert session._tool_structural_debt is not None
    assert session._cancel_event.is_set()
    save.assert_not_called()
    with pytest.raises(RuntimeError, match="tool cleanup"):
        session._capture_worker_claim("owner")

    with patch("turnstone.core.session.save_message", return_value=131):
        response = _run_force_cancel_handler(handler, ws.id)

    assert response.status_code == 200
    assert [turn.role for turn in session.messages] == [
        Role.USER,
        Role.ASSISTANT,
        Role.TOOL,
    ]
    assert session.messages[-1].effect_status is not None
    assert session.messages[-1].effect_status.value == "unknown"
    assert session._tool_structural_debt is None
    assert session._claim_generation() > 0
    assert any(event.get("type") == "stream_end" for event in events)

    idle_ws = manager.create(user_id="owner", ws_id="force-idle-no-debt")
    idle_ui = RecordingUI()
    idle_events: list[dict[str, Any]] = []
    idle_ui._enqueue = idle_events.append  # type: ignore[attr-defined]
    idle_session = make_session(ws_id=idle_ws.id, user_id="owner", ui=idle_ui)
    idle_ws.session = idle_session
    idle_ws.ui = idle_ui
    generation_before = idle_session._generation

    response = _run_force_cancel_handler(handler, idle_ws.id)

    assert response.status_code == 200
    assert idle_session._generation == generation_before
    assert idle_events == []


def test_force_handler_does_not_wait_on_nonabandonable_truncation() -> None:
    """Force is prompt cancellation, not a wait on an owned history cut."""
    manager, _adapter, _storage = _make_manager()
    ws = manager.create(user_id="owner", ws_id="force-nonabandonable-cut")
    ui = RecordingUI()
    enqueued: list[dict[str, Any]] = []
    ui._enqueue = enqueued.append  # type: ignore[attr-defined]
    session = make_session(ws_id=ws.id, user_id="owner", ui=ui)
    ws.session = session
    ws.ui = ui
    owner = threading.Thread(target=lambda: None, name="history-cut-owner")
    with ws._lock:
        ws._worker_running = True
        ws.worker_thread = owner
        ws._worker_force_abandonable = False
    with session._history_truncation_condition:
        session._history_truncation_active = True

    cfg = SessionEndpointConfig(
        permission_gate=None,
        manager_lookup=lambda _request: (manager, None),
        tenant_check=None,
        not_found_label="not found",
        audit_action_prefix="workstream",
    )
    handler = make_cancel_handler(cfg)
    responses: list[Any] = []
    request_thread = threading.Thread(
        target=lambda: responses.append(_run_force_cancel_handler(handler, ws.id)),
        daemon=True,
    )
    request_thread.start()
    request_thread.join(1)
    try:
        assert not request_thread.is_alive(), "force waited on a non-abandonable history cut"
        assert responses[0].status_code == 200
        assert ws.worker_thread is owner
        assert ws._worker_running is True
        assert any(event.get("type") == "cancelled" for event in enqueued)
    finally:
        with session._history_truncation_condition:
            session._history_truncation_active = False
            session._history_truncation_condition.notify_all()
        request_thread.join(5)


def test_user_journal_rejection_rolls_back_live_append() -> None:
    """A USER journal exception must not leave a live turn no row represents."""
    session = make_session(user_id="owner", ui=RecordingUI())
    session._title_generated = True

    def _reject_user_journal(**kwargs: Any) -> Any:
        raise RuntimeError("injected user journal rejection")

    revision_before = session._history_handoff_revision
    with (
        patch("turnstone.core.memory.persist_last_error"),
        patch.object(
            session,
            "_journal_conversation_row_locked",
            side_effect=_reject_user_journal,
        ),
        patch.object(session, "_visible_memory_count", return_value=0),
        patch.object(session, "_print_status_line"),
        pytest.raises(RuntimeError, match="injected user journal rejection"),
    ):
        session.send("hello", acting_user_id="owner")

    assert session.messages == []
    assert session._msg_tokens == []
    assert session._pending_conversation_commits == {}
    assert session._history_handoff_revision == revision_before


def test_system_journal_rejection_rolls_back_live_append() -> None:
    """A SYSTEM journal exception rolls the operator-context append back."""
    session = make_session(user_id="owner", ui=RecordingUI())
    session._title_generated = True

    real_journal = session._journal_conversation_row_locked

    def _reject_system_journal(**kwargs: Any) -> Any:
        if kwargs["message"].get("role") == "system":
            raise RuntimeError("injected system journal rejection")
        return real_journal(**kwargs)

    revision_before = session._history_handoff_revision
    with (
        patch.object(
            session,
            "_journal_conversation_row_locked",
            side_effect=_reject_system_journal,
        ),
        pytest.raises(RuntimeError, match="injected system journal rejection"),
    ):
        session._append_system_turn("correction", "operator note")

    assert session.messages == []
    assert session._msg_tokens == []
    assert session._pending_conversation_commits == {}
    assert session._history_handoff_revision == revision_before


def test_workstream_gone_discard_is_terminal_nonraising_and_bumps_revision() -> None:
    """A hard-deleted parent resolves the journal by discard, not by error.

    The deletion is the user-facing event: the persist returns normally, the
    journal empties, no error latch survives, every token minted over the
    discarded rows is invalidated, exactly one repair event points panes at
    the authoritative (deleted) history, and lifecycle proceeds.
    """
    from turnstone.core.storage import ConversationCommitWorkstreamGoneError

    session = make_session(user_id="owner", ui=RecordingUI())
    session._title_generated = True

    durable: list[Any] = []
    session._append_system_turn("correction", "operator note", deferred_persistence=durable)
    assert len(session._pending_conversation_commits) == 1
    _rows, token = session.capture_history_handoff(lambda _overscan: [])
    # Installed after admission: RecordingUI has no on_system_turn hook, so
    # the append itself already fired one repair. Only the discard's single
    # repair is under test.
    resync = MagicMock(return_value=None)
    session.ui.on_history_resync = resync

    with patch(
        "turnstone.core.session.save_message",
        side_effect=ConversationCommitWorkstreamGoneError("workstream no longer exists"),
    ):
        for persist in durable:
            persist()

    assert session._pending_conversation_commits == {}
    assert session._conversation_persistence_error is None
    assert session.conversation_persistence_status()["state"] == "healthy"
    assert session.register_listener_for_history_handoff(token) is None
    # The discard's repair names the deletion (panes/SDKs treat reason as a
    # free string; the dedicated value is the hook for a deleted-workstream
    # banner) rather than the retryable persistence reason.
    assert resync.call_args_list == [
        (("workstream_gone",),),
    ]
    # The latch is set and capture refuses to mint over the deleted parent —
    # /history takes its fail-closed 503 arm instead of authorizing a
    # token-bearing render of an empty transcript (the silent-wipe mechanism).
    assert session.is_workstream_gone() is True
    with pytest.raises(ConversationCommitWorkstreamGoneError):
        session.capture_history_handoff(lambda _overscan: [])
    assert session.prepare_soft_close() is True


def test_gone_discard_is_a_terminal_latch_not_a_phantom_error() -> None:
    """Admission after the discard refuses via the monotonic gone latch —
    never via a stale persistence-error poison.

    Round-3 review: without the latch the live session kept accepting turns
    whose keyed saves discarded on every attempt — a permanent silent black
    hole. The refusal is clean (no error latch, empty journal, healthy
    status) and the convergence lanes stay open so cancel/failure finalizers
    still run.
    """
    from turnstone.core.session import GenerationCancelled
    from turnstone.core.storage import ConversationCommitWorkstreamGoneError

    session = make_session(user_id="owner", ui=RecordingUI())
    session._title_generated = True

    durable: list[Any] = []
    session._append_system_turn("correction", "first note", deferred_persistence=durable)
    with patch(
        "turnstone.core.session.save_message",
        side_effect=ConversationCommitWorkstreamGoneError("workstream no longer exists"),
    ):
        for persist in durable:
            persist()

    assert session._conversation_persistence_error is None
    assert session.is_workstream_gone() is True
    # New conversation admissions refuse...
    with pytest.raises(RuntimeError, match="closed session"):
        session._append_system_turn("correction", "second note")
    # ...cleanly: no journal residue, no error latch, healthy status.
    assert session._pending_conversation_commits == {}
    assert session.conversation_persistence_status()["state"] == "healthy"
    # The convergence lanes stay open for the finalizers.
    ran: list[int] = []
    assert session._commit_for_generation(0, lambda _d: ran.append(1)) is False
    assert ran == []
    assert (
        session._commit_for_generation(0, lambda _d: ran.append(1), allow_workstream_gone=True)
        is True
    )
    assert ran == [1]
    # Destructive history commands refuse the same way; the rewind route
    # converts the raise to its 503 error arm.
    with pytest.raises(GenerationCancelled):
        session.rewind(1)
    # An identity swap (the /new//resume shape) structurally un-poisons:
    # the latch names the DEAD workstream, not the session object, so a
    # session repointed at a different ws_id admits again with no reset
    # choreography (round-4 review).
    session._ws_id = "fresh-after-swap"
    assert session.is_workstream_gone() is False
    ran.clear()
    assert session._commit_for_generation(0, lambda _d: ran.append(1)) is True
    assert ran == [1]
