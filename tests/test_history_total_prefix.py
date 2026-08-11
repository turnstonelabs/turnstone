"""Adversarial contract tests for a total live conversation-row prefix.

Each test freezes a different accepted-row boundary after the UI-visible
transition but before durable acknowledgement.  The authoritative history
handoff must expose one ordered logical prefix at every cut: no later USER row
without its TOOL/SYSTEM predecessors, and no accepted cancellation/compaction
row that exists only on one side of the REST-to-SSE bootstrap.

The gates are event-driven; timeouts are diagnostics, not race scheduling.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import json
import threading
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from tests._session_helpers import make_result, make_session
from tests.test_history_commit_handoff import _send_environment, _start_send
from tests.test_session_manager import _make_manager
from turnstone.core import session as session_module
from turnstone.core import session_worker
from turnstone.core.attachment_buffer import get_attachment_buffer
from turnstone.core.attachments import Attachment, resolve_staged_attachments
from turnstone.core.storage._registry import get_storage
from turnstone.core.trajectory import turns_from_dicts

if TYPE_CHECKING:
    from collections.abc import Callable


_TOOL_CALL = {
    "id": "call-total-prefix",
    "type": "function",
    "function": {"name": "prefix_probe", "arguments": "{}"},
}


class _PrefixStore:
    """Small keyed conversation store with a selectable pre-commit gate."""

    def __init__(
        self,
        *,
        block_when: Callable[[str, str | None, dict[str, Any]], bool] | None = None,
        ambiguous_when: Callable[[str, str | None, dict[str, Any]], bool] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._next_id = 1
        self._rows: list[dict[str, Any]] = []
        self._ids_by_key: dict[tuple[str, str], int] = {}
        self._block_when = block_when or (lambda _role, _content, _kwargs: False)
        self.ambiguous_when = ambiguous_when or (lambda _role, _content, _kwargs: False)
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(
        self,
        ws_id: str,
        role: str,
        content: str | None,
        tool_name: str | None = None,
        **kwargs: Any,
    ) -> int:
        if self._block_when(role, content, kwargs):
            self.entered.set()
            assert self.release.wait(5), "test did not release conversation persistence"

        row: dict[str, Any] = {"role": role, "content": content or ""}
        if tool_name:
            row["name"] = tool_name
        tool_call_id = kwargs.get("tool_call_id")
        if tool_call_id:
            row["tool_call_id"] = tool_call_id
        source = kwargs.get("source")
        if source:
            row["_source"] = source
        event_id = kwargs.get("event_id")
        if isinstance(event_id, int) and not isinstance(event_id, bool):
            row["_event_id"] = event_id
        if kwargs.get("is_error"):
            row["is_error"] = True
        raw_tool_calls = kwargs.get("tool_calls")
        if raw_tool_calls:
            row["tool_calls"] = json.loads(raw_tool_calls)
        raw_provider_data = kwargs.get("provider_data")
        if raw_provider_data:
            row["_provider_content"] = json.loads(raw_provider_data)
        producer = kwargs.get("producer")
        if producer:
            row["_producer"] = producer
        raw_meta = kwargs.get("meta")
        if raw_meta:
            parsed_meta = json.loads(raw_meta)
            if role == "user" and parsed_meta.get("sender"):
                row["_sender"] = parsed_meta["sender"]
            else:
                row["_source_meta"] = parsed_meta
        commit_key = kwargs.get("commit_key")
        if isinstance(commit_key, str) and commit_key:
            row["_commit_key"] = commit_key

        with self._lock:
            identity = (ws_id, commit_key) if isinstance(commit_key, str) and commit_key else None
            existing = self._ids_by_key.get(identity) if identity is not None else None
            if existing is not None:
                row_id = existing
            else:
                row_id = self._next_id
                self._next_id += 1
                self._rows.append(row)
                if identity is not None:
                    self._ids_by_key[identity] = row_id
        if self.ambiguous_when(role, content, kwargs):
            return 0
        return row_id

    def seed(self, *rows: dict[str, Any]) -> None:
        with self._lock:
            self._rows.extend(copy.deepcopy(rows))
            self._next_id += len(rows)

    def snapshot(self, overscan: int = 0) -> list[dict[str, Any]]:
        # The store keeps the full prefix; the widened-window overscan the
        # production loader applies to a tail-bounded read is a no-op here.
        del overscan
        with self._lock:
            return copy.deepcopy(self._rows)


def _ready_session(**kwargs: Any) -> Any:
    session = make_session(**kwargs)
    session._title_generated = True
    session._system_composed_with_context = True
    return session


def _gate_pending_before_visibility(
    session: Any,
    *,
    matches: Callable[[dict[str, Any]], bool],
    entered: threading.Event,
    release: threading.Event,
) -> Callable[[Any], int]:
    """Gate a journal implementation before it acquires the visibility lane.

    The store carries the same gate as a compatibility fallback for the
    currently unjournaled implementation.  Once all row kinds use the generic
    pending path, this wrapper is the deterministic cut and the store's wait is
    already released by the time persistence reaches it.
    """

    persist = session._persist_pending_conversation_commit

    def _gated(entry: Any) -> int:
        if matches(entry.message):
            entered.set()
            assert release.wait(5), "test did not release pending-row persistence"
        return persist(entry)

    return _gated


def _roles_and_content(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(str(row.get("role") or ""), str(row.get("content") or "")) for row in rows]


def test_tool_system_user_fold_is_one_visible_causal_prefix(tmp_db: Any) -> None:
    """A trailing USER may not overtake its TOOL and SYSTEM predecessors."""

    session = _ready_session()
    store = _PrefixStore(block_when=lambda role, _content, _kwargs: role == "tool")
    persist_release = store.release

    def _execute_tools(*_args: Any, **_kwargs: Any) -> tuple[list[tuple[str, str]], str]:
        return [(_TOOL_CALL["id"], "tool output")], "approval feedback"

    gated_pending = _gate_pending_before_visibility(
        session,
        matches=lambda message: message.get("role") == "tool",
        entered=store.entered,
        release=persist_release,
    )

    with (
        _send_environment(
            session,
            [
                make_result("", tool_calls=[_TOOL_CALL]),
                make_result("final answer"),
            ],
            store,
            _execute_tools,
        ),
        patch.object(
            session,
            "_collect_advisories",
            return_value=[("correction", "guarded operator context", {})],
        ),
        patch.object(
            session,
            "_persist_pending_conversation_commit",
            side_effect=gated_pending,
        ),
    ):
        sender, send_errors = _start_send(session, "opening user")
        assert store.entered.wait(5), "tool-row persistence never reached the frozen cut"
        rows_during, _token = session.capture_history_handoff(store.snapshot)
        persist_release.set()
        sender.join(5)

    assert not sender.is_alive()
    assert send_errors == []
    assert _roles_and_content(rows_during)[-3:] == [
        ("tool", "tool output"),
        ("system", "guarded operator context"),
        ("user", "approval feedback"),
    ]
    assert all(row.get("_commit_key") for row in rows_during[-3:])


def test_initial_user_and_nudge_are_visible_in_append_order(tmp_db: Any) -> None:
    """The initialization batch cannot expose USER without its accepted nudge."""

    session = _ready_session()
    store = _PrefixStore()
    persistence_entered = threading.Event()
    persistence_release = threading.Event()

    def _emit_init_nudge(*, deferred_persistence: list[Callable[[], None]] | None = None) -> None:
        session._append_system_turn(
            "start",
            "initial metacognitive nudge",
            deferred_persistence=deferred_persistence,
        )

    gated_pending = _gate_pending_before_visibility(
        session,
        matches=lambda message: (
            message.get("role") == "user" and message.get("content") == "opening user"
        ),
        entered=persistence_entered,
        release=persistence_release,
    )

    with (
        _send_environment(
            session,
            [make_result("answer")],
            store,
            MagicMock(return_value=([], None)),
        ),
        patch.object(session, "_emit_pending_user_nudges", side_effect=_emit_init_nudge),
        patch.object(
            session,
            "_persist_pending_conversation_commit",
            side_effect=gated_pending,
        ),
    ):
        sender, send_errors = _start_send(session, "opening user")
        assert persistence_entered.wait(5), "opening USER did not reach the frozen cut"
        rows_during, _token = session.capture_history_handoff(store.snapshot)
        persistence_release.set()
        sender.join(5)

    assert not sender.is_alive()
    assert send_errors == []
    assert _roles_and_content(rows_during)[-2:] == [
        ("user", "opening user"),
        ("system", "initial metacognitive nudge"),
    ]
    assert all(row.get("_commit_key") for row in rows_during[-2:])


def test_zero_token_cancelled_partial_crossing_forces_history_repair(tmp_db: Any) -> None:
    """A zero-token cancellation marker is accepted history, not an SSE ghost."""

    marker = "[generation cancelled before completion]"
    session = _ready_session()
    store = _PrefixStore(
        block_when=lambda role, content, _kwargs: role == "assistant" and content == marker
    )
    stream_entered = threading.Event()
    release_cancel = threading.Event()

    def _cancel_before_first_token(_generation: int) -> Any:
        stream_entered.set()
        assert release_cancel.wait(5), "test did not release zero-token cancellation"
        session._cancelled_partial_msg = {"role": "assistant", "content": ""}
        raise session_module.GenerationCancelled()

    gated_pending = _gate_pending_before_visibility(
        session,
        matches=lambda message: (
            message.get("role") == "assistant" and message.get("content") == marker
        ),
        entered=store.entered,
        release=store.release,
    )

    registration: Any = None
    with (
        _send_environment(
            session,
            [make_result("unused")],
            store,
            MagicMock(return_value=([], None)),
        ),
        patch.object(session, "_stream_response", side_effect=_cancel_before_first_token),
        patch.object(
            session,
            "_persist_pending_conversation_commit",
            side_effect=gated_pending,
        ),
    ):
        sender, send_errors = _start_send(session, "cancel immediately")
        assert stream_entered.wait(5), "send never reached its zero-token stream"
        _before_rows, token_before_marker = session.capture_history_handoff(store.snapshot)

        release_cancel.set()
        assert store.entered.wait(5), "cancel marker did not reach the frozen persistence cut"
        rows_during, token_during = session.capture_history_handoff(store.snapshot)
        registration = session.register_listener_for_history_handoff(token_before_marker)

        store.release.set()
        sender.join(5)

    if registration is not None:
        session.ui._unregister_listener(registration[0])
    assert not sender.is_alive()
    assert send_errors == []
    assert token_during != token_before_marker
    assert registration is None
    assert _roles_and_content(rows_during)[-1] == ("assistant", marker)
    assert rows_during[-1].get("_commit_key")


def test_compaction_end_crossing_is_visible_to_fresh_history_handoff(tmp_db: Any) -> None:
    """A listener born after compaction END must not miss its checkpoint card."""

    summary = "bounded compacted summary"
    session = _ready_session()
    seed_rows = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]
    session.messages = turns_from_dicts(seed_rows)
    session._msg_tokens = [2, 2]
    store = _PrefixStore(
        block_when=lambda role, _content, kwargs: (
            role == "assistant" and kwargs.get("source") == session_module.COMPACTION_SOURCE
        )
    )
    store.seed(*seed_rows)
    _rows_before, token_before = session.capture_history_handoff(store.snapshot)

    gated_pending = _gate_pending_before_visibility(
        session,
        matches=lambda message: message.get("_source") == session_module.COMPACTION_SOURCE,
        entered=store.entered,
        release=store.release,
    )
    compacted: list[bool] = []
    compact_errors: list[BaseException] = []

    def _compact() -> None:
        try:
            compacted.append(session._compact_messages())
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            compact_errors.append(exc)

    registration: Any = None
    with (
        patch.object(
            session,
            "_summarize_blocks",
            return_value=session_module._SummaryResult(
                text=summary,
                producer="openai-compatible",
            ),
        ),
        patch.object(get_storage(), "get_compaction_watermark", return_value=2),
        patch("turnstone.core.session.save_message", side_effect=store),
        patch.object(
            session,
            "_persist_pending_conversation_commit",
            side_effect=gated_pending,
        ),
    ):
        worker = threading.Thread(target=_compact, daemon=True)
        worker.start()
        reached_marker = store.entered.wait(5)
        if not reached_marker:
            store.release.set()
            worker.join(5)
        assert reached_marker, (
            "compaction marker never reached the frozen cut; "
            f"errors={compact_errors!r}, results={compacted!r}"
        )

        rows_during, token_during = session.capture_history_handoff(store.snapshot)
        registration = session.register_listener_for_history_handoff(token_before)

        store.release.set()
        worker.join(5)

    if registration is not None:
        session.ui._unregister_listener(registration[0])
    assert not worker.is_alive()
    assert compact_errors == []
    assert compacted == [True]
    assert token_during != token_before
    assert registration is None
    markers = [row for row in rows_during if row.get("_source") == session_module.COMPACTION_SOURCE]
    assert len(markers) == 1
    assert markers[0]["content"] == summary
    assert markers[0].get("_commit_key")


def test_attachment_ownership_transfers_at_journal_admission(tmp_db: Any) -> None:
    """Accepted journal bytes leave staging; a rejected pre-admission turn does not."""

    buffer = get_attachment_buffer()
    buffer.clear()
    ws_id = "ws-total-prefix-attachments"
    user_id = "attachment-owner"
    session = _ready_session(ws_id=ws_id, user_id=user_id)

    first = buffer.stage(
        ws_id=ws_id,
        user_id=user_id,
        filename="owned.txt",
        mime_type="text/plain",
        kind="text",
        content=b"journal owns these bytes",
    )
    attachment = Attachment(
        attachment_id=first.attachment_id,
        filename=first.filename,
        mime_type=first.mime_type,
        kind=first.kind,
        content=first.content,
    )
    deferred: list[Callable[[], None]] = []

    try:
        session._append_user_turn(
            "first accepted row",
            (attachment,),
            send_id="send-first",
            deferred_persistence=deferred,
        )
        removed_at_admission = buffer.get(first.attachment_id, ws_id=ws_id, user_id=user_id) is None
        second_resolution, _taken, _dropped = resolve_staged_attachments(
            [first.attachment_id], ws_id, user_id
        )

        rejected_ws_id = "ws-total-prefix-rejected-attachment"
        rejected = buffer.stage(
            ws_id=rejected_ws_id,
            user_id=user_id,
            filename="retry.txt",
            mime_type="text/plain",
            kind="text",
            content=b"must remain retryable",
        )
        rejected_session = _ready_session(ws_id=rejected_ws_id, user_id=user_id)
        rejected_attachment = Attachment(
            attachment_id=rejected.attachment_id,
            filename=rejected.filename,
            mime_type=rejected.mime_type,
            kind=rejected.kind,
            content=rejected.content,
        )
        rejected_session._publication_shutdown = True
        accepted = rejected_session._commit_for_generation(
            0,
            lambda durable: rejected_session._append_user_turn(
                "must not be admitted",
                (rejected_attachment,),
                send_id="send-rejected",
                deferred_persistence=durable,
            ),
        )
        retained_before_admission = (
            buffer.get(rejected.attachment_id, ws_id=rejected_ws_id, user_id=user_id) is not None
        )
    finally:
        buffer.clear()

    assert deferred, "journal admission must retain a retryable durable closure"
    assert session.has_unresolved_conversation_persistence() is True
    assert removed_at_admission is True
    assert second_resolution == []
    assert accepted is False
    assert retained_before_admission is True


def test_system_lost_ack_reconciles_one_keyed_row(tmp_db: Any) -> None:
    """A committed SYSTEM row with a lost ACK is never duplicated or lost."""

    session = _ready_session()
    store = _PrefixStore(ambiguous_when=lambda role, _content, _kwargs: role == "system")

    with (
        patch("turnstone.core.session.save_message", side_effect=store),
        pytest.raises(session_module.ConversationPersistenceError),
    ):
        session._append_system_turn("correction", "accepted operator context")

    assert session.has_unresolved_conversation_persistence() is True
    assert len(store.snapshot()) == 1
    rows, _token = session.capture_history_handoff(store.snapshot)
    assert _roles_and_content(rows) == [("system", "accepted operator context")]
    assert rows[0].get("_commit_key")
    assert session.has_unresolved_conversation_persistence() is False


def test_mixed_batch_failure_stops_durable_suffix_but_keeps_visible_prefix(
    tmp_db: Any,
) -> None:
    """A TOOL 0/0 stops SYSTEM/USER saves while all accepted rows stay visible."""

    session = _ready_session()
    store = _PrefixStore(ambiguous_when=lambda role, _content, _kwargs: role == "tool")

    def _execute_tools(*_args: Any, **_kwargs: Any) -> tuple[list[tuple[str, str]], str]:
        return [(_TOOL_CALL["id"], "tool output")], "approval feedback"

    with (
        _send_environment(
            session,
            [make_result("", tool_calls=[_TOOL_CALL])],
            store,
            _execute_tools,
        ),
        patch.object(
            session,
            "_collect_advisories",
            return_value=[("correction", "guarded operator context", {})],
        ),
        pytest.raises(session_module.ConversationPersistenceError),
    ):
        session.send("opening user")

    durable = store.snapshot()
    assert _roles_and_content(durable)[-1] == ("tool", "tool output")
    assert ("system", "guarded operator context") not in _roles_and_content(durable)
    assert ("user", "approval feedback") not in _roles_and_content(durable)

    rows, _token = session.capture_history_handoff(store.snapshot)
    assert _roles_and_content(rows)[-3:] == [
        ("tool", "tool output"),
        ("system", "guarded operator context"),
        ("user", "approval feedback"),
    ]
    assert session.has_unresolved_conversation_persistence() is True

    store.ambiguous_when = lambda _role, _content, _kwargs: False
    with patch("turnstone.core.session.save_message", side_effect=store):
        assert session.reconcile_unresolved_persistence_if_due(now=float("inf")) is True
    assert session.has_unresolved_conversation_persistence() is False
    assert _roles_and_content(store.snapshot())[-3:] == _roles_and_content(rows)[-3:]


def test_multi_tool_rows_keep_distinct_repair_event_ids(tmp_db: Any) -> None:
    """Each deferred TOOL closure captures its own admission event cursor."""

    session = _ready_session()
    store = _PrefixStore()
    tool_calls = [
        _TOOL_CALL,
        {
            "id": "call-total-prefix-2",
            "type": "function",
            "function": {"name": "prefix_probe", "arguments": "{}"},
        },
    ]

    def _execute_tools(*_args: Any, **_kwargs: Any) -> tuple[list[tuple[str, str]], str]:
        return [(tool_calls[0]["id"], "first"), (tool_calls[1]["id"], "second")], ""

    with _send_environment(
        session,
        [make_result("", tool_calls=tool_calls), make_result("done")],
        store,
        _execute_tools,
    ):
        session.send("run both")

    tool_rows = [row for row in store.snapshot() if row.get("role") == "tool"]
    assert [row.get("tool_call_id") for row in tool_rows] == [
        "call-total-prefix",
        "call-total-prefix-2",
    ]
    event_ids = [row.get("_event_id") for row in tool_rows]
    assert all(isinstance(event_id, int) for event_id in event_ids)
    assert event_ids[0] < event_ids[1]


def test_tool_attachment_lost_ack_retries_without_refcount_replay(tmp_db: Any) -> None:
    """Session-level TOOL retry keeps its row/blob/ref-list one atomic commit."""

    from turnstone.core.memory import register_workstream
    from turnstone.core.storage import get_storage

    ws_id = "tool-attachment-total-prefix"
    register_workstream(ws_id)
    session = _ready_session(ws_id=ws_id)
    raw = b"one immutable tool image"
    attachment_id = hashlib.sha256(raw).hexdigest()
    data_uri = "data:image/png;base64," + base64.b64encode(raw).decode()
    output = [
        {"type": "text", "text": "captured image"},
        {"type": "image_url", "image_url": {"url": data_uri}},
    ]
    real_atomic_save = session_module.save_tool_message_with_attachments
    save_calls = 0

    def _commit_then_lose_ack(*args: Any, **kwargs: Any) -> int:
        nonlocal save_calls
        save_calls += 1
        assert real_atomic_save(*args, **kwargs) > 0
        return 0

    scripted = iter([make_result("", tool_calls=[_TOOL_CALL])])
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(session, "_stream_response", side_effect=lambda _gen: next(scripted))
        )
        stack.enter_context(
            patch.object(
                session,
                "_execute_tools",
                return_value=([(_TOOL_CALL["id"], output)], ""),
            )
        )
        stack.enter_context(patch.object(session, "_full_messages", return_value=[]))
        stack.enter_context(patch.object(session, "_update_token_table"))
        stack.enter_context(patch.object(session, "_print_status_line"))
        stack.enter_context(patch.object(session, "_emit_state"))
        stack.enter_context(patch.object(session, "_visible_memory_count", return_value=0))
        stack.enter_context(patch.object(session, "_apply_post_execute_advisories"))
        stack.enter_context(
            patch.object(
                session,
                "_evaluate_output",
                side_effect=lambda _call_id, value, *_args, **_kwargs: (value, None),
            )
        )
        stack.enter_context(
            patch(
                "turnstone.core.session.save_tool_message_with_attachments",
                side_effect=_commit_then_lose_ack,
            )
        )
        with pytest.raises(session_module.ConversationPersistenceError):
            session.send("capture it")

    storage = get_storage()
    tool_turns = [
        turn
        for turn in storage.load_message_turns(session.ws_id, checkpointed=False)
        if turn.role.value == "tool"
    ]
    assert save_calls == 1
    assert len(tool_turns) == 1
    assert tool_turns[0].meta.extra["storage_attachment_ids"] == [attachment_id]
    assert storage.get_attachment(attachment_id)["refcount"] == 1
    session.capture_history_handoff(
        lambda _overscan: storage.load_messages(
            session.ws_id,
            repair=False,
            include_compaction=True,
        )
    )
    assert session.has_unresolved_conversation_persistence() is False


def test_cancelled_partial_lost_ack_is_recoverable_history(tmp_db: Any) -> None:
    """Cancellation cleanup journals its marker before ambiguous durability."""

    marker = "partial\n\n[generation cancelled before completion]"
    session = _ready_session()
    store = _PrefixStore(
        ambiguous_when=lambda role, content, _kwargs: role == "assistant" and content == marker
    )

    def _cancel(_generation: int) -> Any:
        session._cancelled_partial_msg = {"role": "assistant", "content": "partial"}
        raise session_module.GenerationCancelled()

    with (
        _send_environment(
            session,
            [make_result("unused")],
            store,
            MagicMock(return_value=([], None)),
        ),
        patch.object(session, "_stream_response", side_effect=_cancel),
        pytest.raises(session_module.ConversationPersistenceError),
    ):
        session.send("cancel me")

    assert session.has_unresolved_conversation_persistence() is True
    rows, _token = session.capture_history_handoff(store.snapshot)
    assert _roles_and_content(rows)[-1] == ("assistant", marker)
    assert rows[-1].get("_commit_key")
    assert session.has_unresolved_conversation_persistence() is False


def test_compaction_marker_lost_ack_has_one_success_end_and_one_row(tmp_db: Any) -> None:
    """Checkpoint durability poison does not fabricate a second failed END."""

    summary = "accepted compacted prefix"
    session = _ready_session()
    seed = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]
    session.messages = turns_from_dicts(seed)
    session._msg_tokens = [2, 2]
    store = _PrefixStore(
        ambiguous_when=lambda _role, _content, kwargs: (
            kwargs.get("source") == session_module.COMPACTION_SOURCE
        )
    )
    store.seed(*seed)

    with (
        patch.object(
            session,
            "_summarize_blocks",
            return_value=session_module._SummaryResult(text=summary, producer="kernel"),
        ),
        patch.object(session.ui, "on_compaction", side_effect=[41, 42]) as compaction_events,
        patch.object(get_storage(), "get_compaction_watermark", return_value=2),
        patch("turnstone.core.session.save_message", side_effect=store),
        pytest.raises(session_module.ConversationPersistenceError),
    ):
        session._compact_messages()

    assert compaction_events.call_count == 2
    assert compaction_events.call_args_list[-1].args[0]["ok"] is True
    markers = [
        row for row in store.snapshot() if row.get("_source") == session_module.COMPACTION_SOURCE
    ]
    assert len(markers) == 1
    assert markers[0].get("_commit_key")
    rows, _token = session.capture_history_handoff(store.snapshot)
    assert len([row for row in rows if row.get("_source") == session_module.COMPACTION_SOURCE]) == 1
    assert session.has_unresolved_conversation_persistence() is False


@pytest.mark.parametrize("storage_recovers", [True, False])
def test_soft_close_retries_the_latched_pending_prefix(
    tmp_db: Any,
    storage_recovers: bool,
) -> None:
    """Close retries safely after 0/0 and unloads only on a complete ACK."""

    mgr, adapter, _storage = _make_manager()
    adapter.cleanup_ui = MagicMock()
    ws = mgr.create(user_id="owner")
    session = _ready_session(ws_id=ws.id, user_id="owner")
    ws.session = session
    ws.ui = session.ui
    store = _PrefixStore(ambiguous_when=lambda role, _content, _kwargs: role == "system")

    with (
        patch("turnstone.core.session.save_message", side_effect=store),
        pytest.raises(session_module.ConversationPersistenceError),
    ):
        session._append_system_turn("correction", "close must reconcile me")

    if storage_recovers:
        store.ambiguous_when = lambda _role, _content, _kwargs: False
    with patch("turnstone.core.session.save_message", side_effect=store):
        closed = mgr.close(ws.id)

    assert closed is storage_recovers
    if storage_recovers:
        assert mgr.get(ws.id) is None
        assert session.has_unresolved_conversation_persistence() is False
        assert session._publication_shutdown is True
    else:
        assert mgr.get(ws.id) is ws
        assert ws._closed is False
        assert session.has_unresolved_conversation_persistence() is True
        assert session._publication_shutdown is False


def test_soft_close_terminal_latch_refuses_a_fresh_worker_claim() -> None:
    """No POST-equivalent dispatch may be acknowledged inside close's latch gap."""

    ws_id = "ws-soft-close-dispatch-gap"
    mgr, adapter, _storage = _make_manager()
    ws = mgr.create(user_id="owner", ws_id=ws_id)
    session = _ready_session(ws_id=ws_id, user_id="owner")
    ws.session = session
    ws.ui = session.ui
    adapter.cleanup_ui = MagicMock()

    prepared = threading.Event()
    release_close = threading.Event()
    close_results: list[bool] = []
    close_errors: list[BaseException] = []
    run_entered = threading.Event()
    run_errors: list[BaseException] = []
    real_prepare = session.prepare_soft_close

    def _gated_prepare() -> bool:
        result = real_prepare()
        assert result is True
        prepared.set()
        assert release_close.wait(5), "test did not release soft close"
        return result

    def _close() -> None:
        try:
            close_results.append(mgr.close(ws_id))
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            close_errors.append(exc)

    def _run_doomed_send() -> None:
        run_entered.set()
        try:
            session.send("must not be acknowledged")
        except BaseException as exc:
            run_errors.append(exc)

    with patch.object(session, "prepare_soft_close", side_effect=_gated_prepare):
        closer = threading.Thread(target=_close, daemon=True)
        closer.start()
        assert prepared.wait(5), "soft close never reached its terminal session latch"

        dispatch_ok = session_worker.send(
            ws,
            enqueue=lambda: None,
            run=_run_doomed_send,
            thread_name="doomed-soft-close-send",
        )
        if dispatch_ok:
            assert run_entered.wait(5), "acknowledged dispatch never ran"

        release_close.set()
        closer.join(5)
        worker = ws.worker_thread
        if worker is not None:
            worker.join(5)

    assert not closer.is_alive()
    assert close_errors == []
    assert close_results == [True]
    assert dispatch_ok is False
    assert run_entered.is_set() is False
    assert run_errors == []
