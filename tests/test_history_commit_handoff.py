"""Adversarial tests for the live-to-durable conversation handoff (#981).

These tests deliberately stop threads at the two linearization boundaries:
assistant-row persistence and the frozen ``/history`` load.  Timeouts are
diagnostic backstops only; every race is otherwise driven by ``Event`` gates.
"""

from __future__ import annotations

import contextlib
import copy
import json
import threading
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from tests._session_helpers import make_registered_session, make_result, make_session
from tests.test_session_manager import _make_manager
from turnstone.core import session as session_module
from turnstone.core.attachments import Attachment
from turnstone.core.history_decoration import project_history_messages
from turnstone.core.session_routes import SessionEndpointConfig, _make_dispatch_attempt
from turnstone.core.trajectory import EffectStatus, dicts_from_turns
from turnstone.core.workstream import Workstream

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


_TOOL_CALL = {
    "id": "call-history-handoff",
    "type": "function",
    "function": {"name": "read_only_probe", "arguments": "{}"},
}


class _ConversationStore:
    """Thread-safe ``save_message`` fake with an assistant ACK barrier.

    ``ambiguous_assistant_ack`` models the hardest backend outcome: the row is
    durably visible, but the caller receives the facade's failure sentinel.
    """

    def __init__(
        self,
        *,
        block_assistant: bool = False,
        ambiguous_assistant_ack: bool = False,
        block_user: bool = False,
        ambiguous_user_ack: bool = False,
    ) -> None:
        self._lock = threading.Lock()
        self._next_id = 1
        self._rows: list[dict[str, Any]] = []
        self._ids_by_commit: dict[tuple[str, str], int] = {}
        # Raw ``meta`` JSON per accepted TOOL row, keyed by tool_call_id.  The
        # row dicts above are the public-shaped handoff projection; the tool
        # envelope carries private audit fields that never join it.
        self.tool_meta: dict[str, str | None] = {}
        self.block_assistant = block_assistant
        self.ambiguous_assistant_ack = ambiguous_assistant_ack
        self.block_user = block_user
        self.ambiguous_user_ack = ambiguous_user_ack
        self.assistant_entered = threading.Event()
        self.release_assistant = threading.Event()
        self.assistant_returning = threading.Event()
        self.user_entered = threading.Event()
        self.release_user = threading.Event()
        self.user_returning = threading.Event()
        if not block_assistant:
            self.release_assistant.set()
        if not block_user:
            self.release_user.set()

    def __call__(
        self,
        ws_id: str,
        role: str,
        content: str,
        *args: Any,
        **kwargs: Any,
    ) -> int:
        if role == "assistant":
            # The identity is what makes a storage row and a still-pending
            # ledger entry provably the same logical commit.  Content/event-id
            # equality is not an identity: legitimate rows can share both.
            commit_key = kwargs.get("commit_key")
            assert isinstance(commit_key, str) and commit_key
            self.assistant_entered.set()
            assert self.release_assistant.wait(5), "test did not release assistant persistence"
        elif role == "user":
            commit_key = kwargs.get("commit_key")
            assert isinstance(commit_key, str) and commit_key
            self.user_entered.set()
            assert self.release_user.wait(5), "test did not release user persistence"

        row: dict[str, Any] = {
            "role": role,
            "content": content,
        }
        event_id = kwargs.get("event_id")
        if isinstance(event_id, int) and not isinstance(event_id, bool):
            row["_event_id"] = event_id
        commit_key = kwargs.get("commit_key")
        if isinstance(commit_key, str) and commit_key:
            row["_commit_key"] = commit_key
        raw_tool_calls = kwargs.get("tool_calls")
        if raw_tool_calls:
            row["tool_calls"] = json.loads(raw_tool_calls)
        if role == "tool":
            if args:
                row["tool_name"] = args[0]
            if kwargs.get("tool_call_id"):
                row["tool_call_id"] = kwargs["tool_call_id"]
            if kwargs.get("is_error"):
                row["is_error"] = True
        raw_provider_data = kwargs.get("provider_data")
        if raw_provider_data is not None:
            row["_provider_content"] = json.loads(raw_provider_data)
        producer = kwargs.get("producer")
        if producer:
            row["_producer"] = producer
        source = kwargs.get("source")
        if source:
            row["_source"] = source
        raw_meta = kwargs.get("meta")
        if role == "user" and raw_meta:
            parsed_meta = json.loads(raw_meta)
            if parsed_meta.get("sender"):
                row["_sender"] = parsed_meta["sender"]
            if parsed_meta.get("client_send_ids"):
                row["_client_send_ids"] = parsed_meta["client_send_ids"]

        with self._lock:
            if role == "tool":
                self.tool_meta[str(kwargs.get("tool_call_id") or "")] = raw_meta
            identity = (ws_id, commit_key) if isinstance(commit_key, str) and commit_key else None
            existing_id = self._ids_by_commit.get(identity) if identity is not None else None
            if existing_id is not None:
                row_id = existing_id
            else:
                row_id = self._next_id
                self._next_id += 1
                self._rows.append(row)
                if identity is not None:
                    self._ids_by_commit[identity] = row_id

        if role == "assistant":
            self.assistant_returning.set()
            if self.ambiguous_assistant_ack:
                return 0
        elif role == "user":
            self.user_returning.set()
            if self.ambiguous_user_ack:
                return 0
        return row_id

    def snapshot(self, overscan: int = 0) -> list[dict[str, Any]]:
        # The store keeps the full prefix; the widened-window overscan the
        # production loader applies to a tail-bounded read is a no-op here.
        del overscan
        with self._lock:
            return copy.deepcopy(self._rows)

    def append_seed_row(self, row: dict[str, Any]) -> None:
        """Seed a durable row without exercising the fake ACK controls."""
        with self._lock:
            self._rows.append(copy.deepcopy(row))


@contextlib.contextmanager
def _send_environment(
    session: Any,
    results: list[Any],
    store: _ConversationStore,
    execute_tools: Callable[..., Any],
) -> Iterator[None]:
    """Patch only slow/orthogonal send seams; retain real commit ordering."""

    scripted = iter(results)
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(session, "_stream_response", side_effect=lambda _gen: next(scripted))
        )
        stack.enter_context(patch.object(session, "_execute_tools", side_effect=execute_tools))
        stack.enter_context(patch.object(session, "_full_messages", return_value=[]))
        stack.enter_context(patch.object(session, "_update_token_table"))
        stack.enter_context(patch.object(session, "_print_status_line"))
        stack.enter_context(patch.object(session, "_emit_state"))
        stack.enter_context(patch.object(session, "_visible_memory_count", return_value=0))
        stack.enter_context(patch.object(session, "_apply_post_execute_advisories"))
        stack.enter_context(patch("turnstone.core.session.save_message", side_effect=store))
        yield


def _ready_session(**kwargs: Any) -> Any:
    from turnstone.core.storage import is_storage_initialized

    session = (
        make_registered_session(**kwargs) if is_storage_initialized() else make_session(**kwargs)
    )
    session._title_generated = True
    return session


def _start_send(
    session: Any,
    text: str = "request",
    *,
    client_send_ids: tuple[str, ...] = (),
) -> tuple[threading.Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def _send() -> None:
        try:
            session.send(text, client_send_ids=client_send_ids)
        except BaseException as exc:  # diagnostic capture from the worker
            errors.append(exc)

    worker = threading.Thread(target=_send, daemon=True)
    worker.start()
    return worker, errors


def _assistant_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("role") == "assistant"]


def _user_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("role") == "user"]


def _drain_listener(listener: Any) -> list[dict[str, Any]]:
    delivered: list[dict[str, Any]] = []
    while not listener.empty():
        delivered.append(listener.get_nowait())
    return delivered


def _dispatch_as(
    ws: Workstream,
    session: Any,
    *,
    actor: str,
    message: str,
) -> tuple[bool, dict[str, Any]]:
    """Drive the production route's atomic queue-or-spawn admission seam."""

    cfg = SessionEndpointConfig(
        permission_gate=None,
        manager_lookup=lambda _request: (None, None),
        tenant_check=None,
        not_found_label="missing",
        audit_action_prefix="workstream",
    )
    attempt = _make_dispatch_attempt(
        ws,
        cfg,
        ws.ui,
        message=message,
        resolved_atts=[],
        ordered_taken=[],
        send_id=f"send-{actor}",
        acting_uid=actor,
    )
    return attempt(session)


def test_frozen_history_load_and_positive_ack_have_no_missing_window(tmp_db: Any) -> None:
    """A reader that wins before ACK returns old storage + pending exactly once.

    Persistence is admitted but paused before its visibility lane.  The reader
    freezes the old storage prefix, then persistence is released to contend
    with it.  Correctness requires the write + ACK to remain on the far side of
    that reader, which must still overlay the accepted pending row.
    """

    session = _ready_session()
    store = _ConversationStore()
    persistence_waiting = threading.Event()
    release_persistence = threading.Event()
    load_entered = threading.Event()
    release_frozen_load = threading.Event()
    capture_result: list[tuple[list[dict[str, Any]], str]] = []
    capture_errors: list[BaseException] = []

    def _frozen_load(overscan: int = 0) -> list[dict[str, Any]]:
        frozen = store.snapshot()
        load_entered.set()
        assert release_frozen_load.wait(5), "test did not release frozen history load"
        return frozen

    def _capture() -> None:
        try:
            capture_result.append(session.capture_history_handoff(_frozen_load))
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            capture_errors.append(exc)

    persist_pending = session._persist_pending_conversation_commit

    def _gated_persistence(entry: Any) -> int:
        if entry.message.get("role") != "assistant":
            return persist_pending(entry)
        persistence_waiting.set()
        assert release_persistence.wait(5), "test did not release assistant persistence"
        return persist_pending(entry)

    with (
        _send_environment(
            session,
            [make_result("durable answer")],
            store,
            MagicMock(return_value=([], None)),
        ),
        patch.object(
            session,
            "_persist_pending_conversation_commit",
            side_effect=_gated_persistence,
        ),
    ):
        sender, send_errors = _start_send(session)
        assert persistence_waiting.wait(5), "assistant persistence was not admitted"

        reader = threading.Thread(target=_capture, daemon=True)
        reader.start()
        assert load_entered.wait(5), "history never entered its frozen load"

        # Let persistence contend for the visibility lane.  It must not reach
        # storage (and therefore cannot ACK/remove pending) until this reader
        # has finished its old-prefix + pending snapshot.
        release_persistence.set()
        assert store.assistant_entered.wait(0.05) is False
        assert sender.is_alive()

        release_frozen_load.set()
        reader.join(5)
        sender.join(5)

    assert not reader.is_alive()
    assert not sender.is_alive()
    assert capture_errors == []
    assert send_errors == []
    assert store.assistant_returning.is_set()
    rows_during_handoff, token_during_handoff = capture_result[0]
    assert [row.get("content") for row in _assistant_rows(rows_during_handoff)] == [
        "durable answer"
    ]
    assert isinstance(token_during_handoff, str) and token_during_handoff

    rows_after_ack, token_after_ack = session.capture_history_handoff(store.snapshot)
    assert [row.get("content") for row in _assistant_rows(rows_after_ack)] == ["durable answer"]
    # Positive ACK changes representation (pending -> durable), not the
    # accepted-history revision exposed to the REST -> SSE handshake.
    assert token_after_ack == token_during_handoff
    assert session.has_unresolved_conversation_persistence() is False


def test_assistant_event_id_covers_the_final_on_commit_token_batch(tmp_db: Any) -> None:
    """The durable/pending cursor is stamped after the commit hook flushes."""

    session = _ready_session()
    store = _ConversationStore()
    committed = session.ui.on_turn_committed
    observed: dict[str, int] = {}

    def _inject_final_pending_batch() -> None:
        # Hold this fragment inside the time-window batcher so the production
        # commit hook, not token arrival, emits its content event.
        with session.ui._ws_lock:
            session.ui._last_token_flush = time.monotonic()
        session.ui.on_content_token("final batched fragment")
        observed["before"] = session.ui._event_id
        committed()
        observed["after"] = session.ui._event_id

    with (
        _send_environment(
            session,
            [make_result("final batched fragment")],
            store,
            MagicMock(return_value=([], None)),
        ),
        patch.object(
            session.ui,
            "on_turn_committed",
            side_effect=_inject_final_pending_batch,
        ),
    ):
        session.send("flush the final content batch")

    assert observed["after"] == observed["before"] + 1
    assistant = _assistant_rows(store.snapshot())[0]
    assert assistant["_event_id"] == observed["after"]
    assert assistant["_event_id"] == session.ui._event_id


def test_commit_between_history_response_and_listener_registration_forces_resync(
    tmp_db: Any,
) -> None:
    """A completed commit crossing the two HTTP requests invalidates the token."""

    session = _ready_session()
    store = _ConversationStore()
    _, before_commit = session.capture_history_handoff(store.snapshot)
    listener_count = len(session.ui._listeners)

    with _send_environment(
        session,
        [make_result("crossed the bootstrap gap")],
        store,
        MagicMock(return_value=([], None)),
    ):
        session.send("first request")

    assert session.register_listener_for_history_handoff(before_commit) is None
    assert len(session.ui._listeners) == listener_count

    _, after_commit = session.capture_history_handoff(store.snapshot)
    assert after_commit != before_commit
    registration = session.register_listener_for_history_handoff(after_commit)
    assert registration is not None
    assert len(session.ui._listeners) == listener_count + 1


def test_user_admission_projects_once_to_two_tabs_and_keeps_early_assistant_stream(
    tmp_db: Any,
) -> None:
    """Both shared-workstream tabs receive one canonical row before output.

    The initiating tab optimistically painted Bob's prompt; the sibling did
    not. Both receive the same accepted-user event. The event does not close
    either EventSource, so the model's earliest content cannot fall into a
    repair disconnect window.
    """

    session = _ready_session(user_id="owner")
    session.bind_acting_user("bob")
    store = _ConversationStore()
    _rows, token_0 = session.capture_history_handoff(store.snapshot)
    registration_a = session.register_listener_for_history_handoff(token_0)
    registration_b = session.register_listener_for_history_handoff(token_0)
    assert registration_a is not None and registration_b is not None
    listener_a = registration_a[0]
    listener_b = registration_b[0]
    assert listener_a is not listener_b

    model_entered = threading.Event()
    release_model = threading.Event()

    def _gated_results() -> Iterator[Any]:
        model_entered.set()
        assert release_model.wait(5), "test did not release model stream"
        session.ui.on_content_token("early ")
        session.ui.on_content_token("assistant output")
        yield make_result("early assistant output")

    with _send_environment(
        session,
        _gated_results(),  # type: ignore[arg-type]
        store,
        MagicMock(return_value=([], None)),
    ):
        sender, send_errors = _start_send(
            session,
            "Bob's shared prompt",
            client_send_ids=("browser-send-1",),
        )
        assert model_entered.wait(5), "user row did not persist before model entry"

        events_a = _drain_listener(listener_a)
        events_b = _drain_listener(listener_b)
        release_model.set()
        assert events_a == events_b
        user_events = [event for event in events_a if event.get("type") == "user_turn"]
        assert len(user_events) == 1
        assert user_events[0] == {
            "type": "user_turn",
            "content": "Bob's shared prompt",
            "client_send_ids": ["browser-send-1"],
            "sender": "bob",
            "ws_id": user_events[0]["ws_id"],
            "_event_id": user_events[0]["_event_id"],
        }
        assert all(event.get("type") != "history_resync" for event in events_a)

        # The old REST revision cannot register a third stale tab.
        assert session.register_listener_for_history_handoff(token_0) is None

        rows_1, token_1 = session.capture_history_handoff(store.snapshot)
        users = _user_rows(rows_1)
        assert len(users) == 1
        assert users[0]["content"] == "Bob's shared prompt"
        assert users[0]["_sender"] == "bob"
        assert users[0]["_client_send_ids"] == ["browser-send-1"]
        assert token_1 != token_0

        sender.join(5)

    assert not sender.is_alive()
    assert send_errors == []
    repaired_events = _drain_listener(listener_a)
    assert (
        "".join(
            str(event.get("text") or "")
            for event in repaired_events
            if event.get("type") == "content"
        )
        == "early assistant output"
    )


def test_tool_admission_projects_final_row_once_to_two_tabs_without_recounting(
    tmp_db: Any,
) -> None:
    """The executor receipt stays provisional; accepted TOOL replaces it.

    Both listeners receive the same canonical event, including the final
    guarded/error/effect/preview projection, while tool metrics count only the
    executor receipt. The accepted row never requests routine history repair.
    """

    session = _ready_session(user_id="owner")
    store = _ConversationStore()
    _rows, token = session.capture_history_handoff(store.snapshot)
    registration_a = session.register_listener_for_history_handoff(token)
    registration_b = session.register_listener_for_history_handoff(token)
    assert registration_a is not None and registration_b is not None
    listener_a = registration_a[0]
    listener_b = registration_b[0]
    descriptor = {
        "attachment_id": "p" * 64,
        "kind": "html",
        "filename": "preview.html",
    }
    preview_attachment = Attachment(
        attachment_id=descriptor["attachment_id"],
        filename="preview.html",
        mime_type="text/html",
        kind="preview",
        content=b"<p>accepted preview</p>",
    )
    attachment_saves: list[dict[str, Any]] = []

    def _save_tool_with_attachments(
        ws_id: str,
        content: str,
        tool_name: str,
        tool_call_id: str,
        attachments: Any,
        **kwargs: Any,
    ) -> int:
        attachment_saves.append(
            {
                "content": content,
                "attachments": tuple(attachments),
                "event_id": kwargs.get("event_id"),
                "meta": kwargs.get("meta"),
            }
        )
        return store(
            ws_id,
            "tool",
            content,
            tool_name,
            tool_call_id=tool_call_id,
            **kwargs,
        )

    def _execute_tools(*_args: Any, **_kwargs: Any) -> tuple[list[tuple[str, str]], None]:
        session.ui.on_tool_result(
            _TOOL_CALL["id"],
            "read_only_probe",
            "provisional unguarded output",
            is_error=True,
            preview=descriptor,
        )
        session._tool_error_flags[_TOOL_CALL["id"]] = True
        session._tool_status[_TOOL_CALL["id"]] = EffectStatus.UNKNOWN
        session._tool_previews[_TOOL_CALL["id"]] = (descriptor, preview_attachment)
        return [(_TOOL_CALL["id"], "final guarded output")], None

    with (
        _send_environment(
            session,
            [make_result("", tool_calls=[_TOOL_CALL]), make_result("done")],
            store,
            _execute_tools,
        ),
        patch(
            "turnstone.core.session.save_tool_message_with_attachments",
            side_effect=_save_tool_with_attachments,
        ),
    ):
        session.send("run the probe")

    events_a = _drain_listener(listener_a)
    events_b = _drain_listener(listener_b)
    tools_a = [event for event in events_a if event.get("type") == "tool_result"]
    tools_b = [event for event in events_b if event.get("type") == "tool_result"]
    assert tools_a == tools_b
    assert len(tools_a) == 2
    assert tools_a[0]["output"] == "provisional unguarded output"
    assert "accepted" not in tools_a[0]
    accepted = tools_a[1]
    assert accepted == {
        "type": "tool_result",
        "accepted": True,
        "call_id": _TOOL_CALL["id"],
        "name": "read_only_probe",
        "output": "final guarded output",
        "is_error": True,
        "preview": descriptor,
        "effect_status": "unknown",
        "ws_id": accepted["ws_id"],
        "_event_id": accepted["_event_id"],
    }
    assert all(event.get("type") != "history_resync" for event in events_a)
    assert session.ui._ws_tool_calls == {"read_only_probe": 1}
    assert len(attachment_saves) == 1
    assert attachment_saves[0]["content"] == "final guarded output"
    assert attachment_saves[0]["event_id"] == accepted["_event_id"]
    # Pin updated with the turn-identity symmetry work: an owner-driven send
    # is an attributed lane, so the attachment-bearing persist shape carries
    # the generation's principal beside the disposition — the same envelope
    # the plain ``save_message`` shape writes.  The accepted SSE payload
    # asserted above deliberately does NOT grow the key: it is audit
    # metadata, and every listener-facing projection stays public-safe.
    assert json.loads(attachment_saves[0]["meta"]) == {
        "effect_status": "unknown",
        "preview": descriptor,
        "acting_principal": "owner",
    }
    tool_row = next(row for row in store.snapshot() if row["role"] == "tool")
    assert tool_row["_event_id"] == accepted["_event_id"]
    assert session.messages[-2].meta.event_id == accepted["_event_id"]


@pytest.mark.parametrize("hook_shape", ["missing", "none", "raises"])
def test_tool_turn_unsupported_hook_emits_exceptional_history_repair(
    tmp_db: Any,
    hook_shape: str,
) -> None:
    """An older/custom UI keeps repair semantics without routine fan-out."""

    session = _ready_session()
    store = _ConversationStore()
    _rows, token = session.capture_history_handoff(store.snapshot)
    registration = session.register_listener_for_history_handoff(token)
    assert registration is not None
    listener = registration[0]

    if hook_shape == "missing":
        patcher = patch.object(session.ui, "on_tool_turn_accepted", None)
    elif hook_shape == "none":
        patcher = patch.object(session.ui, "on_tool_turn_accepted", return_value=None)
    else:
        patcher = patch.object(
            session.ui,
            "on_tool_turn_accepted",
            side_effect=RuntimeError("old UI"),
        )
    with (
        patcher,
        _send_environment(
            session,
            [make_result("", tool_calls=[_TOOL_CALL]), make_result("done")],
            store,
            MagicMock(return_value=([(_TOOL_CALL["id"], "result")], None)),
        ),
    ):
        session.send("run")

    events = _drain_listener(listener)
    repairs = [event for event in events if event.get("type") == "history_resync"]
    assert len(repairs) == 1
    assert repairs[0]["reason"] == "tool_turn_accepted"
    assert not any(event.get("accepted") is True for event in events)
    tool_row = next(row for row in store.snapshot() if row["role"] == "tool")
    assert tool_row["_event_id"] == repairs[0]["_event_id"]


def test_both_tool_persist_shapes_stamp_one_acting_principal(tmp_db: Any) -> None:
    """One batch, two persist shapes, one audit identity.

    The executor answers the first call and drops the second, so the ordinary
    result fold and the shared cancelled-result synthesizer each write an
    accepted TOOL row under the same generation.  If only the ordinary shape
    carried the principal, a revocation sweep over tool rows would silently
    miss every effect a cancelled or malformed batch left behind — exactly the
    rows whose disposition is already least certain.
    """

    session = _ready_session(user_id="owner")
    store = _ConversationStore()
    answered = {
        "id": "call-answered",
        "type": "function",
        "function": {"name": "read_only_probe", "arguments": "{}"},
    }
    dropped = {
        "id": "call-dropped",
        "type": "function",
        "function": {"name": "read_only_probe", "arguments": "{}"},
    }

    with _send_environment(
        session,
        [make_result("", tool_calls=[answered, dropped]), make_result("done")],
        store,
        MagicMock(return_value=([(answered["id"], "observed output")], None)),
    ):
        session.send("run both probes", acting_user_id="user-alice")

    assert json.loads(store.tool_meta["call-answered"]) == {"acting_principal": "user-alice"}
    assert json.loads(store.tool_meta["call-dropped"]) == {
        "effect_status": "unknown",
        "acting_principal": "user-alice",
    }


def test_unattributed_lane_tool_rows_omit_the_principal_key(tmp_db: Any) -> None:
    """A CLI / wake / internal turn writes no key, not an empty string.

    Presence is the attribution signal an audit query reads; an empty string
    would make every unattributed effect look like a row whose principal was
    recorded and happened to be blank.
    """

    session = _ready_session()
    store = _ConversationStore()

    with _send_environment(
        session,
        [make_result("", tool_calls=[_TOOL_CALL]), make_result("done")],
        store,
        MagicMock(return_value=([(_TOOL_CALL["id"], "result")], None)),
    ):
        session.send("run")

    assert store.tool_meta[_TOOL_CALL["id"]] is None


def test_tool_repair_stamps_exact_event_before_unrelated_concurrent_frame(
    tmp_db: Any,
) -> None:
    """A later ring event cannot advance the durable row past its repair cursor."""

    session = _ready_session()
    store = _ConversationStore()
    original_resync = session.ui.on_history_resync

    def _resync_then_race(reason: str) -> int:
        repair_id = original_resync(reason)
        session.ui._enqueue({"type": "content", "text": "unrelated concurrent frame"})
        return repair_id

    with (
        patch.object(session.ui, "on_tool_turn_accepted", return_value=None),
        patch.object(session.ui, "on_history_resync", side_effect=_resync_then_race),
        _send_environment(
            session,
            [make_result("", tool_calls=[_TOOL_CALL]), make_result("done")],
            store,
            MagicMock(return_value=([(_TOOL_CALL["id"], "result")], None)),
        ),
    ):
        session.send("run")

    events = [event for _event_id, event in session.ui._event_buffer]
    repair = next(event for event in events if event.get("type") == "history_resync")
    unrelated = next(
        event
        for event in events
        if event.get("type") == "content" and event.get("text") == "unrelated concurrent frame"
    )
    tool_row = next(row for row in store.snapshot() if row["role"] == "tool")
    assert repair["_event_id"] < unrelated["_event_id"]
    assert tool_row["_event_id"] == repair["_event_id"]


def test_duplicate_tool_ids_use_exceptional_repair_not_ambiguous_projection(
    tmp_db: Any,
) -> None:
    """Malformed duplicate ids fail before execution and close every occurrence."""

    session = _ready_session()
    store = _ConversationStore()
    duplicate_calls = [
        {
            "id": "duplicate-provider-id",
            "type": "function",
            "function": {"name": "first_probe", "arguments": '{"first": 1}'},
        },
        {
            "id": "duplicate-provider-id",
            "type": "function",
            "function": {"name": "second_probe", "arguments": '{"second": 2}'},
        },
    ]
    _rows, token = session.capture_history_handoff(store.snapshot)
    registration = session.register_listener_for_history_handoff(token)
    assert registration is not None
    listener = registration[0]
    execute_tools = MagicMock()

    with (
        _send_environment(
            session,
            [make_result("", tool_calls=duplicate_calls)],
            store,
            execute_tools,
        ),
        pytest.raises(RuntimeError, match="duplicate tool call ids"),
    ):
        session.send("run duplicates")

    execute_tools.assert_not_called()
    assert session._cancelled_tool_results == {}
    assert session._tool_error_flags == {}
    assert session._tool_status == {}
    assert session._tool_previews == {}

    live_rows = dicts_from_turns(session.messages)
    assert [row["role"] for row in live_rows] == ["user", "assistant", "tool", "tool"]
    assert [call["function"]["name"] for call in live_rows[1]["tool_calls"]] == [
        "first_probe",
        "second_probe",
    ]
    assert [call["function"]["arguments"] for call in live_rows[1]["tool_calls"]] == [
        '{"first": 1}',
        '{"second": 2}',
    ]
    for row in live_rows[2:]:
        assert row["tool_call_id"] == "duplicate-provider-id"
        assert row["is_error"] is True
        assert "rejected before execution" in row["content"]
        assert row["_effect_status"] == EffectStatus.NONE.value
        assert "_preview" not in row

    durable_rows = store.snapshot()
    assert [row["role"] for row in durable_rows] == ["user", "assistant", "tool", "tool"]
    assert [row["tool_name"] for row in durable_rows[2:]] == [
        "first_probe",
        "second_probe",
    ]
    assert all(row.get("is_error") is True for row in durable_rows[2:])

    events = _drain_listener(listener)
    repairs = [event for event in events if event.get("type") == "history_resync"]
    assert len(repairs) == 2
    assert {event["reason"] for event in repairs} == {"tool_turn_projection_ambiguous"}
    assert not any(event.get("accepted") is True for event in events)


def test_top_level_tool_id_reuse_across_batches_stays_occurrence_local(
    tmp_db: Any,
) -> None:
    """A provider may reuse one non-empty id in later assistant batches."""

    session = _ready_session()
    store = _ConversationStore()
    first_call = {
        "id": "provider-reused-id",
        "type": "function",
        "function": {"name": "first_probe", "arguments": '{"turn": 1}'},
    }
    second_call = {
        "id": "provider-reused-id",
        "type": "function",
        "function": {"name": "second_probe", "arguments": '{"turn": 2}'},
    }
    _rows, token = session.capture_history_handoff(store.snapshot)
    registration = session.register_listener_for_history_handoff(token)
    assert registration is not None
    listener = registration[0]
    executed: list[tuple[str, str]] = []

    def _execute_tools(
        tool_calls: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> tuple[list[tuple[str, str]], None]:
        assert len(tool_calls) == 1
        call = tool_calls[0]
        call_id = call["id"]
        name = call["function"]["name"]
        executed.append((call_id, name))
        if name == "first_probe":
            session._tool_error_flags[call_id] = True
            session._tool_status[call_id] = EffectStatus.UNKNOWN
            return [(call_id, "first guarded output")], None
        return [(call_id, "second guarded output")], None

    with _send_environment(
        session,
        [
            make_result("", tool_calls=[first_call]),
            make_result("", tool_calls=[second_call]),
            make_result("done"),
        ],
        store,
        _execute_tools,
    ):
        session.send("reuse one provider id in later batches")

    assert executed == [
        ("provider-reused-id", "first_probe"),
        ("provider-reused-id", "second_probe"),
    ]
    live_rows = dicts_from_turns(session.messages)
    assert [row["role"] for row in live_rows] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
    assert [live_rows[index]["tool_calls"][0]["function"]["name"] for index in (1, 3)] == [
        "first_probe",
        "second_probe",
    ]
    assert [live_rows[index]["content"] for index in (2, 4)] == [
        "first guarded output",
        "second guarded output",
    ]
    assert live_rows[2]["is_error"] is True
    assert live_rows[2]["_effect_status"] == EffectStatus.UNKNOWN.value
    assert live_rows[4].get("is_error") is not True
    assert "_effect_status" not in live_rows[4]

    durable_rows = store.snapshot()
    assert [row["role"] for row in durable_rows] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
    assert [durable_rows[index]["tool_name"] for index in (2, 4)] == [
        "first_probe",
        "second_probe",
    ]
    assert [durable_rows[index]["content"] for index in (2, 4)] == [
        "first guarded output",
        "second guarded output",
    ]
    assert durable_rows[2]["is_error"] is True
    assert durable_rows[4].get("is_error") is not True

    accepted = [
        event
        for event in _drain_listener(listener)
        if event.get("type") == "tool_result" and event.get("accepted") is True
    ]
    assert [
        (event["name"], event["output"], bool(event.get("is_error"))) for event in accepted
    ] == [
        ("first_probe", "first guarded output", True),
        ("second_probe", "second guarded output", False),
    ]
    assert accepted[0]["effect_status"] == EffectStatus.UNKNOWN.value
    assert "effect_status" not in accepted[1]
    assert session._cancelled_tool_results == {}
    assert session._tool_error_flags == {}
    assert session._tool_status == {}
    assert session._tool_previews == {}


def test_structured_tool_acceptance_uses_one_newline_scalar_without_inline_bytes(
    tmp_db: Any,
) -> None:
    """Pending, accepted, and durable projections share one safe text value."""

    session = _ready_session()
    store = _ConversationStore()
    image_call = {
        "id": "call-image-projection",
        "type": "function",
        "function": {"name": "read_file", "arguments": "{}"},
    }
    _rows, token = session.capture_history_handoff(store.snapshot)
    registration = session.register_listener_for_history_handoff(token)
    assert registration is not None
    listener = registration[0]
    saved: list[dict[str, Any]] = []

    def _save_tool_with_attachments(
        ws_id: str,
        content: str,
        tool_name: str,
        tool_call_id: str,
        attachments: Any,
        **kwargs: Any,
    ) -> int:
        saved.append(
            {
                "content": content,
                "attachments": tuple(attachments),
                "event_id": kwargs.get("event_id"),
            }
        )
        return store(
            ws_id,
            "tool",
            content,
            tool_name,
            tool_call_id=tool_call_id,
            **kwargs,
        )

    def _execute_tools(*_args: Any, **_kwargs: Any) -> tuple[list[tuple[str, Any]], None]:
        session.ui.on_tool_result(
            image_call["id"],
            "read_file",
            "image (5 bytes)",
        )
        return [
            (
                image_call["id"],
                [
                    {"type": "text", "text": "first guarded part"},
                    {"type": "text", "text": "second guarded part"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,aGVsbG8="},
                    },
                ],
            )
        ], None

    with (
        _send_environment(
            session,
            [make_result("", tool_calls=[image_call]), make_result("done")],
            store,
            _execute_tools,
        ),
        patch(
            "turnstone.core.session.save_tool_message_with_attachments",
            side_effect=_save_tool_with_attachments,
        ),
    ):
        session.send("read it")

    events = _drain_listener(listener)
    accepted = next(event for event in events if event.get("accepted") is True)
    expected = "first guarded part\nsecond guarded part"
    assert accepted["output"] == expected
    assert "data:image" not in json.dumps(accepted)
    assert saved[0]["content"] == expected
    assert saved[0]["event_id"] == accepted["_event_id"]
    assert len(saved[0]["attachments"]) == 1
    assert saved[0]["attachments"][0].content == b"hello"

    tool_message = next(
        message for message in dicts_from_turns(session.messages) if message["role"] == "tool"
    )
    assert tool_message["content"] == [
        {"type": "text", "text": "first guarded part"},
        {"type": "text", "text": "second guarded part"},
        {
            "type": "image",
            "attachment_id": saved[0]["attachments"][0].attachment_id,
        },
    ]
    assert "data:image" not in json.dumps(tool_message)
    assert project_history_messages([tool_message], False)[0]["content"] == expected


def test_frozen_old_storage_load_overlays_user_admitted_during_the_read(
    tmp_db: Any,
) -> None:
    """A reader with an old DB snapshot still returns the accepted user row."""

    session = _ready_session(user_id="owner")
    session.bind_acting_user("bob")
    store = _ConversationStore()
    _rows, token_0 = session.capture_history_handoff(store.snapshot)
    registration = session.register_listener_for_history_handoff(token_0)
    assert registration is not None
    listener = registration[0]
    load_entered = threading.Event()
    release_load = threading.Event()
    capture_result: list[tuple[list[dict[str, Any]], str]] = []

    def _frozen_load(overscan: int = 0) -> list[dict[str, Any]]:
        frozen = store.snapshot()
        load_entered.set()
        assert release_load.wait(5), "test did not release frozen user history load"
        return frozen

    reader = threading.Thread(
        target=lambda: capture_result.append(session.capture_history_handoff(_frozen_load)),
        daemon=True,
    )
    reader.start()
    assert load_entered.wait(5), "history did not freeze its old DB snapshot"

    model_entered = threading.Event()
    release_model = threading.Event()

    def _gated_results() -> Iterator[Any]:
        model_entered.set()
        assert release_model.wait(5), "test did not release model stream"
        yield make_result("answer")

    with _send_environment(
        session,
        _gated_results(),  # type: ignore[arg-type]
        store,
        MagicMock(return_value=([], None)),
    ):
        sender, send_errors = _start_send(session, "arrived during frozen load")
        event = listener.get(timeout=5)
        assert event["type"] == "user_turn"
        assert event["content"] == "arrived during frozen load"
        # Persistence shares the visibility lane and cannot slip into the DB
        # snapshot while the reader is frozen.
        assert store.user_entered.wait(0.05) is False

        release_load.set()
        reader.join(5)
        assert not reader.is_alive()
        rows_during, token_during = capture_result[0]
        users = _user_rows(rows_during)
        assert len(users) == 1
        assert users[0]["content"] == "arrived during frozen load"
        assert users[0]["_sender"] == "bob"
        assert token_during != token_0

        assert store.user_entered.wait(5), "user persistence did not resume after reader"
        assert model_entered.wait(5), "model did not start after user persistence"
        release_model.set()
        sender.join(5)

    assert not sender.is_alive()
    assert send_errors == []


def test_user_turn_hook_failure_emits_one_exceptional_history_resync(tmp_db: Any) -> None:
    """A custom/older UI missing the typed projection keeps repair semantics."""

    session = _ready_session(user_id="owner")
    _rows, token = session.capture_history_handoff(lambda _overscan: [])
    registration = session.register_listener_for_history_handoff(token)
    assert registration is not None
    listener = registration[0]

    with patch.object(session.ui, "on_user_turn", side_effect=RuntimeError("old UI")):
        session._append_user_turn(
            "fallback row",
            (),
            sender_user_id="bob",
            client_send_ids=("fallback-send",),
        )

    events = _drain_listener(listener)
    assert [event["type"] for event in events] == ["history_resync"]
    assert events[0]["reason"] == "user_turn_accepted"
    assert session.messages[-1].meta.event_id == events[0]["_event_id"]


@pytest.mark.parametrize("hook_shape", ["missing", "none"])
def test_user_turn_unsupported_hook_emits_one_exceptional_history_resync(
    tmp_db: Any,
    hook_shape: str,
) -> None:
    """A missing or non-publishing adapter cannot silently claim delivery."""

    session = _ready_session(user_id="owner")
    _rows, token = session.capture_history_handoff(lambda _overscan: [])
    registration = session.register_listener_for_history_handoff(token)
    assert registration is not None
    listener = registration[0]

    if hook_shape == "missing":
        patcher = patch.object(session.ui, "on_user_turn", None)
    else:
        patcher = patch.object(session.ui, "on_user_turn", return_value=None)
    with patcher:
        session._append_user_turn(
            "fallback row",
            (),
            sender_user_id="bob",
            client_send_ids=("fallback-send",),
        )

    events = _drain_listener(listener)
    assert [event["type"] for event in events] == ["history_resync"]
    assert events[0]["reason"] == "user_turn_accepted"
    assert session.messages[-1].meta.event_id == events[0]["_event_id"]


def test_repeated_client_send_id_keeps_distinct_user_turn_event_ids(tmp_db: Any) -> None:
    """Correlation is per-send UI matching, never storage idempotency."""

    session = _ready_session(user_id="owner")
    _rows, token = session.capture_history_handoff(lambda _overscan: [])
    registration = session.register_listener_for_history_handoff(token)
    assert registration is not None
    listener = registration[0]

    session._append_user_turn(
        "first",
        (),
        sender_user_id="bob",
        client_send_ids=("reused-token",),
    )
    session._append_user_turn(
        "second",
        (),
        sender_user_id="bob",
        client_send_ids=("reused-token",),
    )

    events = _drain_listener(listener)
    user_events = [event for event in events if event["type"] == "user_turn"]
    assert [event["content"] for event in user_events] == ["first", "second"]
    assert [event["client_send_ids"] for event in user_events] == [
        ["reused-token"],
        ["reused-token"],
    ]
    assert user_events[0]["_event_id"] != user_events[1]["_event_id"]


def test_ambiguous_user_commit_is_visible_once_and_blocks_a_causal_suffix(
    tmp_db: Any,
) -> None:
    """A lost user ACK is keyed, overlaid, and fail-stops the model and next send."""

    session = _ready_session(user_id="owner")
    session.bind_acting_user("bob")
    store = _ConversationStore(ambiguous_user_ack=True)
    execute_tools = MagicMock(return_value=([], None))

    with _send_environment(
        session,
        [make_result("must never run")],
        store,
        execute_tools,
    ):
        with pytest.raises(session_module.ConversationPersistenceError):
            session.send("ambiguous user row")
        assert session.has_unresolved_conversation_persistence() is True

        # Reconciliation happens before a successor can append its own row.
        with pytest.raises(session_module.ConversationPersistenceError):
            session.send("must not become a suffix")

        durable_users = _user_rows(store.snapshot())
        assert len(durable_users) == 1
        assert isinstance(durable_users[0].get("_commit_key"), str)
        assert execute_tools.call_count == 0

        # A history read sees the stable keyed row, reconciles the lost ACK,
        # and returns one sender-attributed logical row, never storage+journal
        # twins.
        rows, _token = session.capture_history_handoff(store.snapshot)

    users = _user_rows(rows)
    assert len(users) == 1
    assert users[0]["content"] == "ambiguous user row"
    assert users[0]["_sender"] == "bob"
    # The shared-participant SYSTEM row was accepted in the same generation
    # batch after the USER. The USER's lost ACK fail-stopped that suffix, so
    # history overlays it and a later reconciliation persists it in FIFO order.
    assert any(row.get("role") == "system" and row.get("_pending_durability") for row in rows)
    assert session.has_unresolved_conversation_persistence() is True
    with patch("turnstone.core.session.save_message", side_effect=store):
        assert session.reconcile_unresolved_persistence_if_due(now=float("inf")) is True
    assert session.has_unresolved_conversation_persistence() is False


def test_atomic_attachment_user_row_visibility_proves_the_complete_commit(tmp_db: Any) -> None:
    """A keyed attachment row is a durable witness for blobs, refs, and list.

    Shared-workstream actor identity does not replace staged-byte ownership:
    Bob's authenticated turn consumes the upload filed under durable owner
    ``owner``.  A lost ACK keeps the immutable bytes in the journal closure,
    while idempotent retries retain the blob reference only once.
    """

    from turnstone.core.attachment_buffer import get_attachment_buffer

    session = _ready_session(user_id="owner")
    session.bind_acting_user("bob")
    buffer = get_attachment_buffer()
    buffer.clear()
    staged = buffer.stage(
        ws_id=session._ws_id,
        user_id="owner",
        filename="evidence.txt",
        mime_type="text/plain",
        kind="text",
        content=b"evidence",
    )
    attachment = Attachment(
        attachment_id=staged.attachment_id,
        filename=staged.filename,
        mime_type=staged.mime_type,
        kind=staged.kind,
        content=staged.content,
    )

    real_atomic_save = session_module.save_user_message_with_attachments

    def _commit_but_hide_ack(*args: Any, **kwargs: Any) -> int:
        real_atomic_save(*args, **kwargs)
        return 0

    with (
        patch(
            "turnstone.core.session.save_user_message_with_attachments",
            side_effect=_commit_but_hide_ack,
        ),
        pytest.raises(session_module.ConversationPersistenceError),
    ):
        session._append_user_turn("inspect this", (attachment,), send_id="bob-send")

    assert session.has_unresolved_conversation_persistence() is True
    assert buffer.get(staged.attachment_id, ws_id=session._ws_id, user_id="owner") is None
    assert buffer.get(staged.attachment_id, ws_id=session._ws_id, user_id="bob") is None
    # The row and every attachment side effect share one backend transaction.
    # Seeing the keyed row can therefore reconcile a lost facade ACK.
    from turnstone.core.storage import get_storage

    storage = get_storage()
    rows, _token = session.capture_history_handoff(
        lambda _overscan: storage.load_messages(session._ws_id, repair=False)
    )
    users = _user_rows(rows)
    assert len(users) == 1
    assert users[0]["content"][0] == {"type": "text", "text": "inspect this"}
    assert users[0]["_attachments_meta"] == [
        {
            "attachment_id": staged.attachment_id,
            "kind": "text",
            "filename": "evidence.txt",
            "mime_type": "text/plain",
            "size_bytes": len(b"evidence"),
        }
    ]
    assert users[0]["_sender"] == "bob"
    assert storage.count_messages(session._ws_id) == 1
    stored_attachment = storage.get_attachment(staged.attachment_id)
    assert stored_attachment is not None
    assert stored_attachment["refcount"] == 1
    assert session.has_unresolved_conversation_persistence() is False


def test_attachment_side_effect_retry_does_not_double_increment_blob_refcounts(
    tmp_db: Any,
) -> None:
    """An ambiguous attachment ACK may not replay non-idempotent increments.

    Model a backend that commits the complete row/blob/ref-list transaction and
    then loses its acknowledgement.  Retrying the keyed operation must resolve
    the existing row without incrementing either blob a second time.
    """

    from turnstone.core.storage import get_storage

    session = _ready_session(user_id="owner")
    attachments = (
        Attachment(
            attachment_id="1" * 64,
            filename="one.txt",
            mime_type="text/plain",
            kind="text",
            content=b"one",
        ),
        Attachment(
            attachment_id="2" * 64,
            filename="two.txt",
            mime_type="text/plain",
            kind="text",
            content=b"two",
        ),
    )
    real_atomic_save = session_module.save_user_message_with_attachments
    lost_ack = False

    def _save_then_lose_ack(*args: Any, **kwargs: Any) -> int:
        nonlocal lost_ack
        row_id = real_atomic_save(*args, **kwargs)
        if not lost_ack:
            lost_ack = True
            raise RuntimeError("atomic attachment commit acknowledgement lost")
        return row_id

    with (
        patch(
            "turnstone.core.session.save_user_message_with_attachments",
            side_effect=_save_then_lose_ack,
        ),
        pytest.raises(session_module.ConversationPersistenceError),
    ):
        session._append_user_turn("two attachments", attachments)

    assert lost_ack is True
    assert session.has_unresolved_conversation_persistence() is True
    storage = get_storage()
    assert storage is not None
    session.capture_history_handoff(
        lambda _overscan: storage.load_messages(session.ws_id, repair=False)
    )
    assert session.has_unresolved_conversation_persistence() is False
    assert storage.get_attachment(attachments[0].attachment_id)["refcount"] == 1
    assert storage.get_attachment(attachments[1].attachment_id)["refcount"] == 1


def test_direct_same_hash_attachment_does_not_consume_web_staging(tmp_db: Any) -> None:
    """Only a web send_id may transfer matching staged-upload ownership."""

    from turnstone.core.attachment_buffer import get_attachment_buffer

    session = _ready_session(user_id="owner")
    buffer = get_attachment_buffer()
    buffer.clear()
    staged = buffer.stage(
        ws_id=session.ws_id,
        user_id="owner",
        filename="staged.txt",
        mime_type="text/plain",
        kind="text",
        content=b"same bytes",
    )
    direct = Attachment(
        attachment_id=staged.attachment_id,
        filename="direct.txt",
        mime_type="text/plain",
        kind="text",
        content=b"same bytes",
    )

    session._append_user_turn("direct input", (direct,))

    assert buffer.get(staged.attachment_id, ws_id=session.ws_id, user_id="owner") is not None


def test_handoff_revision_never_aba_after_pending_rows_are_acked(tmp_db: Any) -> None:
    """Removing pending rows must not make a later history token reusable."""

    session = _ready_session()
    store = _ConversationStore()
    _, token_0 = session.capture_history_handoff(store.snapshot)

    with _send_environment(
        session,
        [make_result("answer one")],
        store,
        MagicMock(return_value=([], None)),
    ):
        session.send("request one")
    _, token_1 = session.capture_history_handoff(store.snapshot)

    with _send_environment(
        session,
        [make_result("answer two")],
        store,
        MagicMock(return_value=([], None)),
    ):
        session.send("request two")
    _, token_2 = session.capture_history_handoff(store.snapshot)

    assert len({token_0, token_1, token_2}) == 3
    assert session.has_unresolved_conversation_persistence() is False


def test_matching_handoff_token_preserves_cursor_zero_for_tool_only_replay(tmp_db: Any) -> None:
    """Cursor ``0`` is a real boundary, not the absence of a cursor.

    ``tool_pending`` models the first reconstructing event of a tool-call-only
    assistant turn.  The matched handoff token must select numeric replay so
    that event 1 is not replaced by a content snapshot that cannot encode the
    tool call.
    """

    session = _ready_session()
    store = _ConversationStore()
    _, token = session.capture_history_handoff(store.snapshot)
    session.ui._enqueue(
        {
            "type": "tool_pending",
            "call_id": _TOOL_CALL["id"],
            "name": _TOOL_CALL["function"]["name"],
        }
    )

    with patch.object(
        session.ui,
        "register_listener_with_replay",
        wraps=session.ui.register_listener_with_replay,
    ) as replay:
        registration = session.register_listener_for_history_handoff(
            token,
            last_event_id=0,
        )

    assert registration is not None
    replay.assert_called_once()
    assert replay.call_args.args[0] == 0


def test_tool_only_persistence_failure_resyncs_a_previously_matched_empty_snapshot(
    tmp_db: Any,
) -> None:
    """A listener winning just before a failed tool-only commit must refetch.

    Content/reasoning snapshots cannot encode a tool call.  On the healthy
    path, later ``tool_pending``/``tool_info`` events reconstruct it; on a
    persistence failure tools never start, so the failure path itself must
    invalidate the listener's otherwise-successful bootstrap.
    """

    session = _ready_session()
    store = _ConversationStore(block_assistant=True, ambiguous_assistant_ack=True)
    _, token = session.capture_history_handoff(store.snapshot)
    registration = session.register_listener_for_history_handoff(token)
    assert registration is not None
    listener, _replay, status, _lost, _earliest, snapshot = registration
    assert status == "fresh"
    assert snapshot["content"] == ""
    assert snapshot["reasoning"] == ""

    execute_tools = MagicMock(return_value=([], None))
    with _send_environment(
        session,
        [make_result("", tool_calls=[_TOOL_CALL])],
        store,
        execute_tools,
    ):
        sender, send_errors = _start_send(session)
        assert store.assistant_entered.wait(5), "assistant persistence never started"
        store.release_assistant.set()
        sender.join(5)

    assert not sender.is_alive()
    assert len(send_errors) == 1
    assert isinstance(send_errors[0], session_module.ConversationPersistenceError)
    execute_tools.assert_not_called()
    delivered = []
    while not listener.empty():
        delivered.append(listener.get_nowait())
    assert "history_resync" in {event.get("type") for event in delivered}


def test_ambiguous_assistant_commit_is_visible_once_and_fail_stops_tools_and_queue(
    tmp_db: Any,
) -> None:
    """Committed-but-unacknowledged is deduped by key and poisons the turn.

    The generic failure cleanup historically drained queued user text.  A
    conversation persistence failure needs a dedicated arm: tools do not run,
    the queued message stays queued, and an unstarted TOOL row completes the
    accepted assistant block while the ledger awaits ordered reconciliation.
    """

    session = _ready_session()
    store = _ConversationStore(block_assistant=True, ambiguous_assistant_ack=True)
    execute_tools = MagicMock(return_value=([], None))
    result = make_result("", tool_calls=[_TOOL_CALL])

    with _send_environment(session, [result], store, execute_tools):
        sender, send_errors = _start_send(session)
        assert store.assistant_entered.wait(5), "assistant persistence never started"
        session.queue_message("do not drain me", queue_msg_id="queued-after-failure")
        store.release_assistant.set()
        sender.join(5)
        # Keep the storage fake installed: the journal's idempotent retry
        # closure resolves ``save_message`` at call time.
        assert session.prepare_soft_close() is False
        assert session._publication_shutdown is False

    assert not sender.is_alive()
    assert len(send_errors) == 1
    assert isinstance(send_errors[0], session_module.ConversationPersistenceError)
    execute_tools.assert_not_called()
    assert list(session._queued_messages) == ["queued-after-failure"]
    assert session.has_unresolved_conversation_persistence() is True

    rows, _token = session.capture_history_handoff(store.snapshot)
    assistants = _assistant_rows(rows)
    assert len(assistants) == 1
    assert assistants[0].get("tool_calls") == [_TOOL_CALL]
    assert isinstance(assistants[0].get("_commit_key"), str)
    tools = [row for row in rows if row.get("role") == "tool"]
    assert len(tools) == 1
    assert tools[0].get("tool_call_id") == _TOOL_CALL["id"]
    # History proves the ambiguous assistant durable, but the synthesized
    # completion still has its own journal boundary and remains fail-stop until
    # the normal due reconciler persists it.
    assert session.has_unresolved_conversation_persistence() is True
    with patch("turnstone.core.session.save_message", side_effect=store):
        assert session.reconcile_unresolved_persistence_if_due(now=float("inf")) is True
    assert session.has_unresolved_conversation_persistence() is False

    # Content, event id, and tool shape can all legitimately repeat.  Only the
    # durable commit identity may collapse storage + ledger representations.
    distinct_same_payload = dict(assistants[0])
    distinct_same_payload["_commit_key"] = "distinct-logical-assistant-row"
    store.append_seed_row(distinct_same_payload)
    rows_with_distinct_twin, _token = session.capture_history_handoff(store.snapshot)
    assert len(_assistant_rows(rows_with_distinct_twin)) == 2


def test_foreign_actor_cannot_drain_retained_queue_after_persistence_recovery(
    tmp_db: Any,
) -> None:
    """A recovery turn cannot borrow another participant's retained text.

    The #981 failure arm deliberately leaves interjections queued behind an
    unresolved assistant commit.  Their admitting principal must survive that
    pause: once storage recovers, a different participant may not turn the
    retained text into a user row attributed to their own identity and run it
    under their credentials.  The original participant can still reconcile
    and drain it on their next turn.
    """

    session = _ready_session(user_id="owner")
    store = _ConversationStore(block_assistant=True, ambiguous_assistant_ack=True)
    session.bind_acting_user("alice")

    with _send_environment(
        session,
        [make_result("alice's accepted answer")],
        store,
        MagicMock(return_value=([], None)),
    ):
        sender, send_errors = _start_send(session, "alice starts")
        assert store.assistant_entered.wait(5), "assistant persistence never started"
        session.queue_message(
            "alice retained interjection",
            queue_msg_id="alice-retained",
            interjector_user_id="alice",
        )
        store.release_assistant.set()
        sender.join(5)

    assert not sender.is_alive()
    assert len(send_errors) == 1
    assert isinstance(send_errors[0], session_module.ConversationPersistenceError)
    assert list(session._queued_messages) == ["alice-retained"]
    ws = Workstream(id=session._ws_id, name="persistence-recovery")
    ws.session = session
    ws.ui = session.ui

    # The durable row is now recoverable.  That does not transfer ownership of
    # the already-admitted queue item to the participant who happens to send
    # next.  Exercise the actual route dispatch seam: its fresh-worker
    # admission and slot claim are one workstream-lock transaction, so the
    # refusal happens before the worker can bind Bob or append his user row.
    store.ambiguous_assistant_ack = False
    bob_ok, bob_outcome = _dispatch_as(
        ws,
        session,
        actor="bob",
        message="bob must wait",
    )

    assert bob_ok is False
    assert bob_outcome == {"rejected": "cross_user_interjection"}
    assert ws.worker_thread is None
    assert list(session._queued_messages) == ["alice-retained"]
    assert not any(
        row.get("role") == "user" and row.get("content") == "bob must wait"
        for row in dicts_from_turns(session.messages)
    )

    with _send_environment(
        session,
        [make_result("alice resumes"), make_result("alice finishes")],
        store,
        MagicMock(return_value=([], None)),
    ):
        assert session.reconcile_unresolved_persistence_if_due(now=float("inf")) is True
        alice_ok, alice_outcome = _dispatch_as(
            ws,
            session,
            actor="alice",
            message="alice recovery turn",
        )
        assert alice_ok is True
        assert alice_outcome == {}
        worker = ws.worker_thread
        assert worker is not None
        worker.join(5)

    assert not worker.is_alive()

    assert session.has_unresolved_conversation_persistence() is False
    assert session._queued_messages == {}
    retained_rows = [
        row
        for row in dicts_from_turns(session.messages)
        if row.get("role") == "user" and row.get("content") == "alice retained interjection"
    ]
    assert len(retained_rows) == 1
    assert retained_rows[0].get("_sender") == "alice"


def test_foreign_actor_cannot_consume_owned_queue_at_advisory_drain(tmp_db: Any) -> None:
    """The advisory seam partitions: a foreign-owned row is structurally
    retained and produces NO spec (an advisory is a model-facing turn —
    announcing another participant's pending text would leak their activity
    into the acting user's transcript). No raise: the pop-side ownership
    assert is deleted; retention is the enforcement."""

    session = _ready_session(user_id="owner")
    session.bind_acting_user("alice")
    session.queue_message(
        "alice advisory",
        queue_msg_id="alice-advisory",
        interjector_user_id="alice",
    )
    session.bind_acting_user("bob")

    assert session._collect_advisories(None, "some_tool", True) == []
    assert list(session._queued_messages) == ["alice-advisory"]

    session.bind_acting_user("alice")
    specs = session._collect_advisories(None, "some_tool", True)
    assert [(source, meta.get("message")) for source, _content, meta in specs] == [
        ("user_interjection", "alice advisory")
    ]
    assert session._queued_messages == {}


def test_mixed_ownership_advisory_drain_emits_own_and_retains_foreign(tmp_db: Any) -> None:
    """Round-5 review pin: on a mixed queue the acting user's row becomes an
    advisory spec while the other participant's row stays queued."""

    session = _ready_session(user_id="owner")
    session.bind_acting_user("alice")
    session.queue_message(
        "alice advisory",
        queue_msg_id="alice-advisory",
        interjector_user_id="alice",
    )
    session.bind_acting_user("bob")
    session.queue_message(
        "bob advisory",
        queue_msg_id="bob-advisory",
        interjector_user_id="bob",
    )

    specs = session._collect_advisories(None, "some_tool", True)
    assert [(source, meta.get("message")) for source, _content, meta in specs] == [
        ("user_interjection", "bob advisory")
    ]
    assert list(session._queued_messages) == ["alice-advisory"]


def test_foreign_actor_cannot_consume_owned_queue_at_user_flush(tmp_db: Any) -> None:
    """No-tool/error flushes preserve both ownership and sender attribution.

    Partitioned: under a foreign actor the flush pops nothing (no raise, no
    append); the row waits for its owner, whose flush stamps their sender."""

    session = _ready_session(user_id="owner")
    session.bind_acting_user("alice")
    session.queue_message(
        "alice flush",
        queue_msg_id="alice-flush",
        interjector_user_id="alice",
    )
    session.bind_acting_user("bob")
    message_count = len(session.messages)

    assert session._flush_queued_messages() is False

    assert len(session.messages) == message_count
    assert list(session._queued_messages) == ["alice-flush"]
    session.bind_acting_user("alice")
    assert session._flush_queued_messages() is True
    assert session._queued_messages == {}
    flushed = [
        row
        for row in dicts_from_turns(session.messages)
        if row.get("role") == "user" and row.get("content") == "alice flush"
    ]
    assert len(flushed) == 1
    assert flushed[0].get("_sender") == "alice"


def test_mixed_ownership_flush_appends_own_and_retains_foreign(tmp_db: Any) -> None:
    """Round-5 review pin (the all-or-nothing defect): one flush on a mixed
    queue appends the acting user's row as a user turn AND leaves the other
    participant's row queued — no raise, nothing destroyed."""

    session = _ready_session(user_id="owner")
    session.bind_acting_user("alice")
    session.queue_message(
        "alice flush",
        queue_msg_id="alice-flush",
        interjector_user_id="alice",
    )
    session.bind_acting_user("bob")
    session.queue_message(
        "bob flush",
        queue_msg_id="bob-flush",
        interjector_user_id="bob",
    )

    assert session._flush_queued_messages() is True
    assert list(session._queued_messages) == ["alice-flush"]
    flushed = [
        row
        for row in dicts_from_turns(session.messages)
        if row.get("role") == "user" and row.get("content") == "bob flush"
    ]
    assert len(flushed) == 1
    assert flushed[0].get("_sender") == "bob"
    assert not any(
        "alice flush" in str(row.get("content")) for row in dicts_from_turns(session.messages)
    )


def test_empty_principal_queue_items_keep_internal_drain_compatibility(tmp_db: Any) -> None:
    """Legacy/internal queue producers remain intentionally unscoped."""

    session = _ready_session(user_id="owner")
    session.queue_message(
        "internal advisory",
        queue_msg_id="internal-advisory",
        interjector_user_id="",
        turn_principal_id="",
    )
    session.bind_acting_user("bob")
    specs = session._collect_advisories(None, "some_tool", True)
    assert [(source, meta.get("message")) for source, _content, meta in specs] == [
        ("user_interjection", "internal advisory")
    ]

    session.queue_message(
        "internal flush",
        queue_msg_id="internal-flush",
        interjector_user_id="",
        turn_principal_id="",
    )
    session.bind_acting_user("alice")
    assert session._flush_queued_messages() is True
    assert session._queued_messages == {}


def test_history_load_failure_rejects_pending_only_handoff_until_durable_prefix_loads(
    tmp_db: Any,
) -> None:
    """A pending journal suffix cannot authorize incomplete history.

    If the durable-prefix read fails, merging the resident pending row over an
    empty list is useful only as an internal fail-visible floor.  Returning it
    with 200 + a handoff token would let browsers replace older transcript
    rows, clear their repair latch, and reconnect against incomplete truth.
    The route must return a bounded 503; a later successful read may expose the
    pending row exactly once with a valid handoff token.
    """

    from tests._coord_test_helpers import _build_mgr, _fake_registry
    from tests.test_coordinator_endpoints import _COORD_HEADERS, _make_client
    from turnstone.core.storage import get_storage

    storage = get_storage()
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    session = _ready_session(ws_id=ws.id, user_id="user-1", kind="coordinator")
    ws.session = session
    ws.ui = session.ui
    store = _ConversationStore(ambiguous_assistant_ack=True)

    with (
        _send_environment(
            session,
            [make_result("pending while durable history is unavailable")],
            store,
            MagicMock(return_value=([], None)),
        ),
        pytest.raises(session_module.ConversationPersistenceError),
    ):
        session.send("request during outage")

    assert session.has_unresolved_conversation_persistence() is True
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    with patch.object(storage, "load_messages", side_effect=RuntimeError("storage unavailable")):
        response = client.get(
            f"/v1/api/workstreams/{ws.id}/history",
            headers=_COORD_HEADERS,
        )

    assert response.status_code == 503
    assert response.json() == {"error": "History temporarily unavailable"}
    assert session.has_unresolved_conversation_persistence() is True

    response = client.get(
        f"/v1/api/workstreams/{ws.id}/history",
        headers=_COORD_HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["handoff_token"], str) and body["handoff_token"]
    assistants = _assistant_rows(body["messages"])
    assert [row.get("content") for row in assistants] == [
        "pending while durable history is unavailable"
    ]
    assert "_commit_key" not in assistants[0]
    assert "_pending_durability" not in assistants[0]
    assert session.has_unresolved_conversation_persistence() is True


def test_failed_durability_head_poisons_an_already_admitted_successor(tmp_db: Any) -> None:
    """Ticket N+1 may not persist after ticket N loses conversation durability.

    Deliberate pin update: the head failure is injected through the real
    journal classifier (a journaled row whose save fails transiently), which
    latches the poison before raising — production's only remaining
    ConversationPersistenceError shape now that the commit lane's re-latch
    safety net for hand-rolled unlatched errors is deleted.
    """

    session = _ready_session()
    first_entered = threading.Event()
    release_first = threading.Event()
    second_admitted = threading.Event()
    second_persisted = threading.Event()
    errors: list[BaseException] = []

    def _failing_save(*_args: Any, **_kwargs: Any) -> int:
        return 0

    def _first_persist() -> None:
        first_entered.set()
        assert release_first.wait(5), "test did not release failed durability head"
        with session._history_handoff_lock:
            pending = session._journal_conversation_row_locked(
                commit_key="head-ambiguous-row",
                message={"role": "assistant", "content": "ambiguous"},
                persist=lambda: _failing_save(),
                event_id=None,
            )
        session._persist_pending_conversation_commit(pending)

    def _first_commit(durable: list[Callable[[], None]]) -> None:
        durable.append(_first_persist)

    def _second_commit(durable: list[Callable[[], None]]) -> None:
        second_admitted.set()
        durable.append(second_persisted.set)

    def _run(commit: Callable[[list[Callable[[], None]]], None]) -> None:
        try:
            session._commit_for_generation(0, commit)
        except BaseException as exc:  # diagnostic capture from each ticket owner
            errors.append(exc)

    first = threading.Thread(target=_run, args=(_first_commit,), daemon=True)
    second = threading.Thread(target=_run, args=(_second_commit,), daemon=True)
    first.start()
    assert first_entered.wait(5), "first durability ticket never started"
    second.start()
    assert second_admitted.wait(5), "successor was not admitted behind the head"

    release_first.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(errors) == 2
    assert all(isinstance(exc, session_module.ConversationPersistenceError) for exc in errors)
    assert second_persisted.is_set() is False

    # Poison must settle skipped tickets so terminal durability drain cannot
    # wait forever on a ticket that is intentionally never executed.
    drained = threading.Event()
    drain = threading.Thread(
        target=lambda: (session.shutdown_publication_and_drain_durability(), drained.set()),
        daemon=True,
    )
    drain.start()
    assert drained.wait(5), "poisoned durability lane did not drain"
    drain.join(1)


def _mark_session_unresolved(ws: Any) -> None:
    assert ws.session is not None
    ws.session.has_unresolved_conversation_persistence = lambda: True


def test_manual_soft_close_refuses_to_discard_an_unresolved_ledger() -> None:
    mgr, adapter, storage = _make_manager()
    ws = mgr.create(user_id="u1")
    _mark_session_unresolved(ws)

    assert mgr.close(ws.id) is False
    assert mgr.get(ws.id) is ws
    assert ws.id not in adapter.cleaned_up
    assert (ws.id, "closed") not in storage.state_updates
    assert adapter.events_of("closed") == []


def test_idle_reaper_refuses_to_discard_an_unresolved_ledger() -> None:
    mgr, adapter, storage = _make_manager()
    ws = mgr.create(user_id="u1")
    _mark_session_unresolved(ws)
    ws.last_active = time.monotonic() - 100

    assert ws.id not in mgr.close_idle(max_age_seconds=1)
    assert mgr.get(ws.id) is ws
    assert ws.id not in adapter.cleaned_up
    assert (ws.id, "closed") not in storage.state_updates


def test_capacity_eviction_refuses_to_discard_an_unresolved_ledger() -> None:
    mgr, adapter, _storage = _make_manager(max_active=1)
    incumbent = mgr.create(user_id="u1")
    _mark_session_unresolved(incumbent)
    incumbent.last_active = time.monotonic() - 100

    with pytest.raises(RuntimeError, match="All 1 slots are active"):
        mgr.create(user_id="u2")

    assert mgr.get(incumbent.id) is incumbent
    assert incumbent._closed is False
    assert incumbent.id not in adapter.cleaned_up
    assert mgr.eviction_count == 0


def test_widened_window_renders_lost_ack_rows_in_position_never_at_tail() -> None:
    """The overscan window contains every committed twin of a pending key.

    Two lost-ACK rows (durable, acknowledgement lost) with a tail bound
    smaller than the pending count: the widened load reaches both twins, so
    the merge acknowledges them in place — never re-appending an old row
    after the newest messages, the out-of-order render the deleted storage
    probe was chasing.
    """
    session = _ready_session()
    seen_overscan: list[int] = []

    def _save_zero(*_args: Any, **_kwargs: Any) -> int:
        return 0

    keys: list[str] = []
    with (
        patch("turnstone.core.session.save_message", side_effect=_save_zero),
        pytest.raises(session_module.ConversationPersistenceError),
    ):
        session._append_system_turn("correction", "first lost ack")
    keys.extend(session._pending_conversation_commits)
    assert len(keys) == 1

    durable_rows = [
        {"role": "system", "content": "first lost ack", "_commit_key": keys[0]},
    ]

    def _loader(overscan: int) -> list[dict[str, Any]]:
        seen_overscan.append(overscan)
        limit = 1
        return durable_rows[-(limit + overscan) :]

    merged, _token = session.capture_history_handoff(_loader)

    assert seen_overscan == [1]
    assert [row.get("content") for row in merged] == ["first lost ack"]
    assert session._pending_conversation_commits == {}
    # A second capture is pure durable prefix — the row appears exactly once.
    merged_again, _token2 = session.capture_history_handoff(_loader)
    assert [row.get("content") for row in merged_again] == ["first lost ack"]


def test_conflicted_pending_row_renders_in_place_inside_widened_window() -> None:
    """A same-key conflict stays fail-visible in its durable position.

    The deleted presence-probe suppressed a conflicted row whose twin lay
    beyond the bare window; the widened window keeps the twin in view and the
    merge replaces it in place with the journal's version, next to the
    persistence banner an operator debugs from.
    """
    session = _ready_session()

    from turnstone.core.storage import ConversationCommitConflictError

    with (
        patch(
            "turnstone.core.session.save_message",
            side_effect=ConversationCommitConflictError("different conversation commit"),
        ),
        pytest.raises(session_module.ConversationPersistenceError),
    ):
        session._append_system_turn("correction", "journal version")
    [conflict_key] = session._pending_conversation_commits
    entry = session._pending_conversation_commits[conflict_key]
    assert entry.ack_from_durable_row is False
    assert session.conversation_persistence_status()["state"] == "conflict"

    durable_rows = [
        {"role": "system", "content": "durable divergent twin", "_commit_key": conflict_key},
        {"role": "user", "content": "legacy second-writer row"},
    ]

    def _loader(overscan: int) -> list[dict[str, Any]]:
        limit = 1
        return durable_rows[-(limit + overscan) :]

    merged, _token = session.capture_history_handoff(_loader)

    assert [row.get("content") for row in merged] == [
        "journal version",
        "legacy second-writer row",
    ]
    # Conflicts never self-acknowledge: the entry survives for the operator.
    assert conflict_key in session._pending_conversation_commits


def test_capture_never_runs_the_loader_under_the_handoff_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structural pin for the deleted in-lock storage probe.

    The loader is the only storage touchpoint in a capture; running it under
    ``_history_handoff_lock`` would stall every row admission, publication,
    and SSE registration behind a slow database. The overscan is sampled
    first, the load runs unlocked, and the merge is pure in-memory work.
    """
    storage = MagicMock()
    storage.save_message.return_value = 1
    from turnstone.core.storage import _registry

    monkeypatch.setattr(_registry, "_storage", storage)
    session = make_session()
    session._title_generated = True
    session._append_system_turn("correction", "pending row")
    lock_free_during_load: list[bool] = []

    def _loader(overscan: int) -> list[dict[str, Any]]:
        del overscan
        acquired: list[bool] = []

        def _probe() -> None:
            # From another thread: an RLock held by the capturing thread
            # refuses a non-blocking acquire; a free lock grants it.
            got = session._history_handoff_lock.acquire(blocking=False)
            acquired.append(got)
            if got:
                session._history_handoff_lock.release()

        prober = threading.Thread(target=_probe, daemon=True)
        prober.start()
        prober.join(5)
        lock_free_during_load.append(bool(acquired and acquired[0]))
        return []

    session.capture_history_handoff(_loader)
    assert lock_free_during_load == [True]
