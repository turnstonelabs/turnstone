"""Accepted model-turn provenance across dispatch, storage, and projections."""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from tests._session_helpers import (
    RecordingUI,
    arm_session,
    make_session,
    replace_session_lane,
    scripted_chat_client,
    seam_provider,
)
from turnstone.console.coordinator_client import _serialize_messages
from turnstone.core.export import export_workstream
from turnstone.core.history_decoration import project_history_messages
from turnstone.core.model_registry import ModelConfig, ModelRegistry
from turnstone.core.model_turn import ModelLane, model_turn
from turnstone.core.providers import ModelCapabilities, StreamChunk, UsageInfo
from turnstone.core.session import ConversationPersistenceError, GenerationCancelled
from turnstone.core.storage._utils import _fork_turn_insert_row
from turnstone.core.trajectory import (
    PROVENANCE_META_KEY,
    EffectStatus,
    Turn,
    TurnProvenance,
    turn_from_dict,
    turn_to_dict,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from turnstone.core.storage import StorageBackend


def _good_stream(text: str) -> list[StreamChunk]:
    return [
        StreamChunk(content_delta=text),
        StreamChunk(
            finish_reason="stop",
            usage=UsageInfo(prompt_tokens=10, completion_tokens=2, total_tokens=12),
        ),
    ]


def _dying_stream(text: str) -> Iterator[StreamChunk]:
    yield StreamChunk(content_delta=text)
    raise httpx.ReadError("old binding died")


def _provenance(turn: Turn) -> dict[str, str | int]:
    raw = turn.meta.extra.get(PROVENANCE_META_KEY)
    assert isinstance(raw, dict)
    return raw


def _log_has_field(record: logging.LogRecord, key: str, value: str | int) -> bool:
    """Accept either the console or JSON/dict structlog renderer.

    Logging configuration is process-global, so a full-suite predecessor may
    select a different renderer than this file sees in isolation — including
    the colored console renderer, whose ANSI escapes would otherwise split
    ``key=value``. The event fields are the contract; their presentation
    (renderer AND styling) is not.
    """
    message = re.sub(r"\x1b\[[0-9;]*m", "", record.getMessage())
    return any(
        candidate in message
        for candidate in (
            f"{key}={value}",
            f"'{key}': {value!r}",
            f'"{key}": {json.dumps(value)}',
        )
    )


def _register_session_parent(session: Any) -> None:
    """Mirror production's parent-before-keyed-conversation ordering."""
    from turnstone.core.storage import get_storage

    storage = get_storage()
    assert storage is not None
    storage.register_workstream(session.ws_id, user_id=session._user_id)


def test_model_turn_stamps_one_immutable_serving_identity() -> None:
    provider = seam_provider("accepted")
    lane = ModelLane(
        provider=provider,
        client=MagicMock(),
        model="backend-v2",
        alias="assistant-fast",
        registry_generation=17,
        capabilities=ModelCapabilities(),
    )

    result = model_turn(
        lane,
        [Turn.user("hello")],
        acting_principal_id="user-alice",
    )

    expected = {
        "model_alias": "assistant-fast",
        "backend_model_id": "backend-v2",
        "registry_generation": 17,
        "acting_principal_id": "user-alice",
    }
    assert result.provenance.to_meta() == expected
    assert _provenance(result.turn) == expected


def test_model_turn_drain_retry_logs_kernel_axes_without_principal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = seam_provider("unused")
    provider.create_streaming.side_effect = [
        _dying_stream("discarded"),
        _good_stream("accepted"),
    ]
    lane = ModelLane(
        provider=provider,
        client=MagicMock(),
        model="retry-kernel",
        alias="retry-alias",
        registry_generation=12,
        capabilities=ModelCapabilities(),
    )

    with (
        patch("turnstone.core.model_turn.time.sleep"),
        caplog.at_level(logging.WARNING, logger="turnstone.core.model_turn"),
    ):
        result = model_turn(
            lane,
            [Turn.user("hello")],
            acting_principal_id="private-principal-id",
        )

    assert result.content == "accepted"
    [retry] = [
        record for record in caplog.records if "model_turn.drain_retry" in record.getMessage()
    ]
    assert _log_has_field(retry, "alias", "retry-alias")
    assert _log_has_field(retry, "model", "retry-kernel")
    assert _log_has_field(retry, "registry_generation", 12)
    assert "private-principal-id" not in retry.getMessage()
    assert "acting_principal_id" not in retry.getMessage()


def test_creation_fallback_stamps_fallback_binding_and_principal(tmp_db: str) -> None:
    registry = ModelRegistry(
        models={
            "primary": ModelConfig(
                "primary", "http://primary/v1", "key", "primary-id", provider="openai-compatible"
            ),
            "fallback": ModelConfig(
                "fallback",
                "http://fallback/v1",
                "key",
                "fallback-id",
                provider="openai-compatible",
            ),
        },
        default="primary",
        fallback=["fallback"],
    )
    session = make_session(
        registry=registry,
        model_alias="primary",
        user_id="owner",
        ui=RecordingUI(),  # type: ignore[no-untyped-call]
    )
    _register_session_parent(session)
    session._title_generated = True
    session._primary_lane().client.chat.completions.create = MagicMock(
        side_effect=ConnectionError("primary unavailable")
    )
    registry.get_client("fallback").chat.completions.create = scripted_chat_client(
        {"content": "served by fallback"}
    )

    session.send("hello", acting_user_id="user-alice")

    assert _provenance(session.messages[-1]) == {
        "model_alias": "fallback",
        "backend_model_id": "fallback-id",
        "registry_generation": registry.generation,
        "acting_principal_id": "user-alice",
    }


def test_midstream_rebind_stamps_only_the_successful_replacement(
    tmp_db: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = make_session(
        model_alias="primary",
        registry_generation=3,
        user_id="owner",
        ui=RecordingUI(),  # type: ignore[no-untyped-call]
    )
    _register_session_parent(session)
    provider = arm_session(
        session,
        _dying_stream("discarded"),
        _good_stream("accepted"),
    )
    refresh_count = 0

    def _refresh() -> None:
        nonlocal refresh_count
        refresh_count += 1
        if refresh_count != 2:
            return
        replace_session_lane(session, model="replacement-id")
        session._model_binding = dataclasses.replace(
            session._model_binding,
            registry_generation=9,
        )

    with (
        patch.object(session, "_refresh_model_from_registry", side_effect=_refresh),
        caplog.at_level(logging.DEBUG, logger="turnstone.core.session"),
    ):
        session.send("hello", acting_user_id="private-principal-id")

    assert provider.create_streaming.call_count == 2
    assert _provenance(session.messages[-1]) == {
        "model_alias": "primary",
        "backend_model_id": "replacement-id",
        "registry_generation": 9,
        "acting_principal_id": "private-principal-id",
    }
    [retry] = [record for record in caplog.records if "stream.retry" in record.getMessage()]
    assert _log_has_field(retry, "alias", "primary")
    assert _log_has_field(retry, "model", "test-model")
    assert _log_has_field(retry, "registry_generation", 3)
    assert "private-principal-id" not in retry.getMessage()
    assert "acting_principal_id" not in retry.getMessage()

    [finished] = [record for record in caplog.records if "stream.finished" in record.getMessage()]
    assert _log_has_field(finished, "alias", "primary")
    assert _log_has_field(finished, "model", "replacement-id")
    assert _log_has_field(finished, "registry_generation", 9)
    assert "private-principal-id" not in finished.getMessage()
    assert "acting_principal_id" not in finished.getMessage()


def test_headless_send_stamps_effective_owner_principal(tmp_db: str) -> None:
    """Scheduled/internal sends record the credential principal they use."""
    session = make_session(
        model_alias="headless",
        registry_generation=6,
        user_id="owner-principal",
        ui=RecordingUI(),  # type: ignore[no-untyped-call]
    )
    _register_session_parent(session)
    arm_session(session, _good_stream("accepted"))

    session.send("scheduled work")

    assert _provenance(session.messages[-1]) == {
        "model_alias": "headless",
        "backend_model_id": "test-model",
        "registry_generation": 6,
        "acting_principal_id": "owner-principal",
    }


def test_shared_workstream_rebind_cannot_relabel_inflight_turn(tmp_db: str) -> None:
    session = make_session(
        model_alias="shared",
        registry_generation=4,
        user_id="owner",
        ui=RecordingUI(),  # type: ignore[no-untyped-call]
    )
    _register_session_parent(session)

    def _stream() -> Iterator[StreamChunk]:
        # A second browser binds a new actor while Alice's response is in
        # flight.  The accepted result must retain the generation principal.
        session.bind_acting_user("user-bob")
        yield from _good_stream("alice's answer")

    arm_session(session, _stream())
    session.send("alice's question", acting_user_id="user-alice")

    assert session._acting_user_id == "user-bob"
    assert _provenance(session.messages[-1])["acting_principal_id"] == "user-alice"


def test_tool_rows_record_the_same_principal_as_their_assistant_turn(tmp_db: str) -> None:
    """Turn identity is symmetric across the roles one generation persists.

    The assistant row carries the principal on its four-axis provenance
    envelope; the TOOL rows its batch produced carry the same identity as a
    plain sibling of their disposition — not a second four-axis tuple, since a
    tool row is an effect receipt and its kernel axes would be empty.  Both
    read the generation's bound principal, so revocation can query tool rows
    directly instead of joining each one back to its batch head.
    """
    session = make_session(
        model_alias="main",
        registry_generation=5,
        user_id="owner",
        ui=RecordingUI(),  # type: ignore[no-untyped-call]
    )
    _register_session_parent(session)
    session._title_generated = True
    session._primary_lane().client.chat.completions.create = scripted_chat_client(
        {
            "tool_calls": [{"id": "call-audit", "name": "read_only_probe", "arguments": "{}"}],
            "finish_reason": "tool_calls",
        },
        {"content": "done"},
    )
    tool_meta: dict[str, str | None] = {}

    def _record(_ws_id: str, role: str, _content: str, *_args: Any, **kwargs: Any) -> int:
        if role == "tool":
            tool_meta[str(kwargs.get("tool_call_id") or "")] = kwargs.get("meta")
        return 1

    with (
        patch.object(
            session,
            "_execute_tools",
            return_value=([("call-audit", "observed output")], None),
        ),
        patch("turnstone.core.session.save_message", side_effect=_record),
    ):
        session.send("run the probe", acting_user_id="user-alice")

    assert _provenance(session.messages[-1])["acting_principal_id"] == "user-alice"
    assert json.loads(tool_meta["call-audit"]) == {"acting_principal": "user-alice"}


def test_utility_completion_stamps_snapshotted_acting_principal(tmp_db: str) -> None:
    """History-visible utility output can never inherit a later actor rebind."""
    session = make_session(user_id="owner")
    session.bind_acting_user("user-alice")
    provider = seam_provider("summary")
    lane = replace_session_lane(
        session,
        provider=provider,
        model="summary-kernel",
        alias="summary-alias",
    )

    result = session._utility_completion([Turn.user("summarize")], lane=lane)

    assert result.provenance == TurnProvenance(
        model_alias="summary-alias",
        backend_model_id="summary-kernel",
        registry_generation=lane.registry_generation,
        acting_principal_id="user-alice",
    )


def test_token_calibration_isolated_across_primary_fallback_primary() -> None:
    """A -> B -> A restores A's ratio and A's own prompt-count anchor."""
    session = make_session(model_alias="primary", registry_generation=7)
    primary = session._primary_lane()
    fallback = dataclasses.replace(
        primary,
        alias="fallback",
        model="fallback-kernel",
        registry_generation=11,
    )
    session.messages = [Turn.user("first")]
    session._msg_tokens = [1]

    session._activate_token_calibration(primary)
    session._last_usage = {
        "prompt_tokens": 50,
        "completion_tokens": 5,
        "total_tokens": 55,
    }
    session._update_token_table(
        msgs=[{"role": "user", "content": "a" * 246}],
        tool_def_chars=0,
        provenance=TurnProvenance(
            model_alias=primary.alias,
            backend_model_id=primary.model,
            registry_generation=primary.registry_generation,
        ),
    )
    primary_ratio = session._chars_per_token
    assert primary_ratio == 5.0

    session.messages.extend([Turn.assistant("primary answer"), Turn.user("second")])
    session._msg_tokens.extend([5, 2])
    session._activate_token_calibration(fallback)
    session._last_usage = {
        "prompt_tokens": 25,
        "completion_tokens": 7,
        "total_tokens": 32,
    }
    session._update_token_table(
        msgs=[{"role": "user", "content": "b" * 46}],
        tool_def_chars=0,
        provenance=TurnProvenance(
            model_alias=fallback.alias,
            backend_model_id=fallback.model,
            registry_generation=fallback.registry_generation,
        ),
    )
    assert session._chars_per_token == 2.0

    session.messages.append(Turn.assistant("fallback answer"))
    session._msg_tokens.append(7)
    session._activate_token_calibration(primary)

    assert session._chars_per_token == primary_ratio
    expected = 50 + sum(session._msg_tokens[1:])
    assert session._estimated_prompt_tokens() == expected
    assert session._estimated_prompt_tokens() != 25 + sum(session._msg_tokens[3:])


def test_cancelled_partial_stamps_the_armed_fallback_lane_and_principal(
    tmp_db: str,
) -> None:
    """A partial accepted on Stop is an assistant turn, not unattributed UI."""
    session = make_session(
        model_alias="primary",
        registry_generation=3,
        user_id="owner",
        ui=RecordingUI(),  # type: ignore[no-untyped-call]
    )
    _register_session_parent(session)
    fallback_provider = seam_provider("unused", provider_name="fallback-provider")
    fallback_lane = ModelLane(
        provider=fallback_provider,
        client=MagicMock(),
        model="fallback-kernel",
        alias="fallback-alias",
        registry_generation=13,
        capabilities=ModelCapabilities(),
    )

    def _cancel_from_fallback(consumer, *_args, **_kwargs):
        consumer.begin_attempt(MagicMock(armed=True), None, fallback_lane)
        consumer(StreamChunk(content_delta="partial answer"))
        session.cancel()
        raise GenerationCancelled()

    session._title_generated = True
    with patch.object(
        session,
        "_model_turn_with_fallback",
        side_effect=_cancel_from_fallback,
    ):
        session.send("hello", acting_user_id="user-alice")

    assert _provenance(session.messages[-1]) == {
        "model_alias": "fallback-alias",
        "backend_model_id": "fallback-kernel",
        "registry_generation": 13,
        "acting_principal_id": "user-alice",
    }
    from turnstone.core.storage import get_storage

    storage = get_storage()
    assert storage is not None
    durable = storage.load_message_turns(session.ws_id, checkpointed=False)
    assert _provenance(durable[-1]) == _provenance(session.messages[-1])


def test_provenance_dict_bridge_is_strict_and_lossless() -> None:
    raw = {
        "model_alias": "main",
        "backend_model_id": "kernel",
        "registry_generation": 8,
        "acting_principal_id": "alice",
    }
    msg = {"role": "assistant", "content": "ok", "_provenance": raw}
    turn = turn_from_dict(msg)
    assert _provenance(turn) == raw
    assert turn_to_dict(turn) == msg

    torn = turn_from_dict(
        {
            "role": "assistant",
            "content": "bad",
            "_provenance": {**raw, "registry_generation": "8"},
        }
    )
    assert PROVENANCE_META_KEY not in torn.meta.extra
    assert "_provenance" not in turn_to_dict(torn)


def test_storage_rehydrates_and_fork_preserves_assistant_provenance(
    storage_backend: StorageBackend,
) -> None:
    raw = {
        "model_alias": "main",
        "backend_model_id": "kernel",
        "registry_generation": 8,
        "acting_principal_id": "alice",
    }
    ws_id = "provenance-storage-roundtrip"
    storage_backend.save_message(
        ws_id,
        "assistant",
        "accepted",
        meta=json.dumps({PROVENANCE_META_KEY: raw}),
    )

    turns = storage_backend.load_message_turns(ws_id, checkpointed=False)
    assert len(turns) == 1
    assert _provenance(turns[0]) == raw
    loaded = storage_backend.load_messages(ws_id, repair=False)
    assert loaded[0]["_provenance"] == raw

    insert_row, attachment_ids = _fork_turn_insert_row(
        turns[0],
        "provenance-fork-destination",
        "2026-08-09T00:00:00",
    )
    assert attachment_ids == []
    assert json.loads(insert_row["meta"])[PROVENANCE_META_KEY] == raw


def test_storage_rehydrates_and_fork_preserves_tool_acting_principal(
    storage_backend: StorageBackend,
) -> None:
    """A TOOL row answers "whose turn ran this effect" without a join.

    Revocation and audit ask that question per effect.  Deriving it from the
    assistant row that opened the batch is a join that breaks exactly where it
    matters — a cancelled batch whose receipts are synthesized separately — so
    the row carries the identity itself, as a sibling of its disposition and
    never as a fabricated four-axis model-attempt envelope.
    """
    ws_id = "tool-principal-storage-roundtrip"
    storage_backend.save_message(
        ws_id,
        "tool",
        "probe output",
        "read_only_probe",
        tool_call_id="call-audit",
        meta=json.dumps({"effect_status": "committed", "acting_principal": "user-alice"}),
    )

    turns = storage_backend.load_message_turns(ws_id, checkpointed=False)
    assert len(turns) == 1
    assert turns[0].effect_status is EffectStatus.COMMITTED
    assert turns[0].meta.extra["acting_principal"] == "user-alice"
    assert PROVENANCE_META_KEY not in turns[0].meta.extra

    insert_row, attachment_ids = _fork_turn_insert_row(
        turns[0],
        "tool-principal-fork-destination",
        "2026-08-10T00:00:00",
    )
    assert attachment_ids == []
    assert json.loads(insert_row["meta"]) == {
        "effect_status": "committed",
        "acting_principal": "user-alice",
    }


def test_tool_acting_principal_reaches_no_public_or_model_facing_payload(
    storage_backend: StorageBackend,
) -> None:
    """The audit identity has no dict-bridge key, so no projection can carry it.

    A principal-only envelope is the adversarial shape: were the tool branch in
    ``reconstruct_turns`` not to claim it, it would fall through to
    ``source_meta`` — which /history publishes as a turn's display ``meta``.
    """
    ws_id = "tool-principal-private"
    storage_backend.save_message(
        ws_id,
        "tool",
        "probe output",
        "read_only_probe",
        tool_call_id="call-audit",
        meta=json.dumps({"acting_principal": "private-user-id"}),
    )

    turns = storage_backend.load_message_turns(ws_id, checkpointed=False)
    assert turns[0].meta.extra["acting_principal"] == "private-user-id"
    assert "private-user-id" not in json.dumps(turn_to_dict(turns[0]))

    loaded = storage_backend.load_messages(ws_id, repair=False)
    assert "_source_meta" not in loaded[0]
    assert "private-user-id" not in json.dumps(loaded)

    history = project_history_messages(loaded)
    assert "meta" not in history[0]
    assert "private-user-id" not in json.dumps(history)
    assert "private-user-id" not in json.dumps(_serialize_messages(loaded))
    assert "private-user-id" not in json.dumps(
        _serialize_messages(loaded, include_provider_content=True)
    )

    storage = MagicMock()
    storage.get_workstream.return_value = {"state": "idle"}
    storage.load_message_turns.return_value = turns
    storage.get_attachments.return_value = []
    assert b"private-user-id" not in export_workstream(storage, ws_id).data


def test_provider_bound_wire_never_carries_the_tool_acting_principal() -> None:
    """Sidecar meta stays sidecar: the lowered wire has no field for it."""
    provider = seam_provider("done")
    lane = ModelLane(
        provider=provider,
        client=MagicMock(),
        model="backend-v2",
        alias="assistant-fast",
        registry_generation=17,
        capabilities=ModelCapabilities(),
    )
    tool_turn = Turn.tool("call-audit", "probe output", effect_status=EffectStatus.COMMITTED)
    tool_turn.meta.extra["acting_principal"] = "private-user-id"

    model_turn(
        lane,
        [
            Turn.user("run the probe"),
            turn_from_dict(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-audit",
                            "function": {"name": "read_only_probe", "arguments": "{}"},
                        }
                    ],
                }
            ),
            tool_turn,
        ],
        acting_principal_id="private-user-id",
    )

    wire = provider.create_streaming.call_args.kwargs["messages"]
    assert any(message.get("role") == "tool" for message in wire)
    assert "private-user-id" not in json.dumps(wire)
    assert "acting_principal" not in json.dumps(wire)


def test_pending_and_ambiguous_ack_keep_one_exact_provenance_tuple(tmp_db: str) -> None:
    """A lost ACK cannot relabel or duplicate the accepted assistant row."""
    session = make_session(
        model_alias="main",
        registry_generation=5,
        user_id="owner",
        ui=RecordingUI(),  # type: ignore[no-untyped-call]
    )
    arm_session(session, _good_stream("accepted"))
    durable_rows: list[dict[str, Any]] = []
    ids_by_commit: dict[tuple[str, str], int] = {}
    assistant_attempts = 0

    def _ambiguous_save(ws_id: str, role: str, content: str, **kwargs: object) -> int:
        nonlocal assistant_attempts
        commit_key = kwargs.get("commit_key")
        # Participant-join operator context is intentionally outside the
        # conversation-row journal.  It may precede the keyed user turn in a
        # shared workstream and is irrelevant to this lost-ACK seam.
        if not isinstance(commit_key, str) or not commit_key:
            return 1
        identity = (ws_id, commit_key)
        if identity not in ids_by_commit:
            ids_by_commit[identity] = len(ids_by_commit) + 1
            row: dict[str, Any] = {
                "role": role,
                "content": content,
                "_commit_key": commit_key,
            }
            raw_meta = kwargs.get("meta")
            if role == "assistant":
                assert isinstance(raw_meta, str)
                row["_provenance"] = json.loads(raw_meta)[PROVENANCE_META_KEY]
            durable_rows.append(row)
        if role == "assistant":
            assistant_attempts += 1
            return 0
        return ids_by_commit[identity]

    with (
        patch("turnstone.core.session.save_message", side_effect=_ambiguous_save),
        pytest.raises(ConversationPersistenceError),
    ):
        session.send("hello", acting_user_id="alice")

    expected = {
        "model_alias": "main",
        "backend_model_id": "test-model",
        "registry_generation": 5,
        "acting_principal_id": "alice",
    }
    assert assistant_attempts == 1
    physical_assistants = [row for row in durable_rows if row["role"] == "assistant"]
    assert len(physical_assistants) == 1
    assert physical_assistants[0]["_provenance"] == expected

    # A storage outage leaves the immutable pending projection authoritative.
    pending, _token = session.capture_history_handoff(lambda _overscan: [])
    pending_assistants = [row for row in pending if row.get("role") == "assistant"]
    assert len(pending_assistants) == 1
    assert pending_assistants[0]["_provenance"] == expected

    # Once the durable keyed row is visible it reconciles in place, still once
    # and with the exact same request-time identity.
    reconciled, _token = session.capture_history_handoff(lambda _overscan: durable_rows)
    reconciled_assistants = [row for row in reconciled if row.get("role") == "assistant"]
    assert len(reconciled_assistants) == 1
    assert reconciled_assistants[0]["_provenance"] == expected


def test_public_and_model_facing_projections_do_not_expose_principal() -> None:
    provenance = TurnProvenance(
        model_alias="main",
        backend_model_id="kernel",
        registry_generation=8,
        acting_principal_id="private-user-id",
    )
    turn = Turn.assistant("accepted")
    turn.meta.extra[PROVENANCE_META_KEY] = provenance.to_meta()
    internal = turn_to_dict(turn)

    history = project_history_messages([internal])
    assert history == [{"role": "assistant", "content": "accepted"}]
    assert "private-user-id" not in json.dumps(history)
    assert "_provenance" not in _serialize_messages([internal])[0]
    assert "_provenance" not in _serialize_messages([internal], include_provider_content=True)[0]

    storage = MagicMock()
    storage.get_workstream.return_value = {"state": "idle"}
    storage.load_message_turns.return_value = [turn]
    storage.get_attachments.return_value = []
    exported = export_workstream(storage, "ws")
    payload = json.loads(exported.data)
    assert payload == {"messages": [{"role": "assistant", "content": "accepted"}]}
    assert b"private-user-id" not in exported.data


def test_history_projection_failure_returns_503_without_private_row_fields(
    tmp_db: str,
) -> None:
    """No public decoration/projection failure can authorize raw history."""
    from tests._coord_test_helpers import _fake_registry
    from tests.test_coordinator_endpoints import (
        _COORD_HEADERS,
        _build_history_mgr,
        _make_client,
    )
    from turnstone.core.storage import get_storage

    storage = get_storage()
    assert storage is not None
    # A 200 history response is authoritative only when a concrete live
    # session captures the durable rows and returns a handoff token.  Use the
    # endpoint suite's token-bearing session fixture; the generic coordinator
    # stub intentionally has no history-handoff protocol and must now fail
    # closed with 503.
    manager = _build_history_mgr(storage)
    workstream = manager.create(user_id="user-1")
    private_principal = "PRIVATE-PRINCIPAL-SENTINEL"
    private_reasoning = "PRIVATE-REASONING-SENTINEL"
    private_signature = "PRIVATE-SIGNATURE-SENTINEL"
    private_producer = "PRIVATE-PRODUCER-SENTINEL"
    private_commit_key = "PRIVATE-COMMIT-KEY-SENTINEL"
    raw = TurnProvenance(
        model_alias="main",
        backend_model_id="kernel",
        registry_generation=8,
        acting_principal_id=private_principal,
    ).to_meta()
    storage.save_message(
        workstream.id,
        "assistant",
        "accepted",
        provider_data=json.dumps(
            {
                "producer": private_producer,
                "blocks": [
                    {
                        "type": "reasoning_text",
                        "text": private_reasoning,
                        "signature": private_signature,
                    }
                ],
            }
        ),
        meta=json.dumps({PROVENANCE_META_KEY: raw}),
        commit_key=private_commit_key,
    )
    client = _make_client(storage, coord_mgr=manager, registry=_fake_registry())

    for projection_seam in (
        "decorate_history_messages",
        "extract_reasoning_for_history",
        "project_history_messages",
    ):
        with patch(
            f"turnstone.core.history_decoration.{projection_seam}",
            side_effect=RuntimeError("public projection unavailable"),
        ):
            response = client.get(
                f"/v1/api/workstreams/{workstream.id}/history",
                headers=_COORD_HEADERS,
            )

        assert response.status_code == 503
        assert response.json() == {"error": "History temporarily unavailable"}
        assert "messages" not in response.json()
        assert "cursor" not in response.json()
        assert "handoff_token" not in response.json()
        for private_value in (
            private_principal,
            private_reasoning,
            private_signature,
            private_producer,
            private_commit_key,
            "_provider_content",
            "_producer",
            "_provenance",
            "_commit_key",
        ):
            assert private_value not in response.text
