"""Backend-parity coverage for conversation commit idempotency."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from typing import TYPE_CHECKING, cast

import pytest
from sqlalchemy.dialects import postgresql

from tests._storage_fakes import (
    ScriptedPostgresConnection,
    ScriptedPostgresResult,
)
from turnstone.console.coordinator_client import _serialize_messages
from turnstone.core import memory
from turnstone.core.history_decoration import project_history_messages
from turnstone.core.providers._openai_common import sanitize_messages
from turnstone.core.storage import (
    AttachmentWrite,
    ConversationCommitConflictError,
    ConversationCommitWorkstreamGoneError,
)
from turnstone.core.storage._postgresql import PostgreSQLBackend

if TYPE_CHECKING:
    from turnstone.core.storage import StorageBackend


def test_identical_retry_same_commit_key_returns_original_row(
    storage_backend: StorageBackend,
) -> None:
    ws_id = "commit-retry"
    commit_key = "assistant-commit-a"
    storage_backend.register_workstream(ws_id)

    first_id = storage_backend.save_message(
        ws_id,
        "assistant",
        "original",
        event_id=7,
        commit_key=commit_key,
    )
    retry_id = storage_backend.save_message(
        ws_id,
        "assistant",
        "original",
        event_id=7,
        commit_key=commit_key,
    )

    assert retry_id == first_id
    assert storage_backend.count_messages(ws_id) == 1
    messages = storage_backend.load_messages(ws_id, repair=False)
    assert len(messages) == 1
    assert messages[0]["content"] == "original"
    assert messages[0]["_event_id"] == 7
    assert messages[0]["_commit_key"] == commit_key
    turns = storage_backend.load_message_turns(ws_id, checkpointed=False)
    assert len(turns) == 1
    assert turns[0].meta.commit_key == commit_key


@pytest.mark.parametrize(
    "override",
    [
        {"role": "system"},
        {"content": "changed"},
        {"tool_name": "other-tool"},
        {"tool_call_id": "other-call"},
        {"provider_data": '[{"type":"reasoning","text":"changed"}]'},
        {
            "tool_calls": (
                '[{"id":"call-2","type":"function","function":{"name":"other","arguments":"{}"}}]'
            )
        },
        {"source": "other-source"},
        {"event_id": 8},
        {"is_error": True},
        {"meta": '{"other":true}'},
    ],
)
def test_same_key_with_different_normalized_row_fails_closed(
    storage_backend: StorageBackend,
    override: dict[str, object],
) -> None:
    ws_id = f"commit-mismatch-{next(iter(override))}"
    storage_backend.register_workstream(ws_id)
    base: dict[str, object] = {
        "role": "assistant",
        "content": "accepted",
        "tool_name": "tool-a",
        "tool_call_id": "call-a",
        "provider_data": None,
        "tool_calls": None,
        "source": "accepted-source",
        "event_id": 7,
        "is_error": False,
        "meta": '{"stable":true}',
    }

    def _save(values: dict[str, object]) -> int:
        return storage_backend.save_message(
            ws_id,
            str(values["role"]),
            str(values["content"]),
            tool_name=str(values["tool_name"]),
            tool_call_id=str(values["tool_call_id"]),
            provider_data=cast("str | None", values["provider_data"]),
            tool_calls=cast("str | None", values["tool_calls"]),
            source=str(values["source"]),
            event_id=int(values["event_id"]),
            is_error=bool(values["is_error"]),
            meta=str(values["meta"]),
            commit_key="one-immutable-row",
        )

    _save(base)
    with pytest.raises(ConversationCommitConflictError, match="different conversation commit"):
        _save({**base, **override})

    assert storage_backend.count_messages(ws_id) == 1
    assert storage_backend.load_messages(ws_id, repair=False)[0]["content"] == "accepted"


def test_plain_conflict_remains_typed_through_memory_facade(
    storage_backend: StorageBackend,
) -> None:
    ws_id = "commit-facade-conflict"
    storage_backend.register_workstream(ws_id)
    storage_backend.save_message(ws_id, "assistant", "accepted", commit_key="one-row")

    with pytest.raises(ConversationCommitConflictError):
        memory.save_message(ws_id, "assistant", "different", commit_key="one-row")
    assert storage_backend.count_messages(ws_id) == 1
    assert storage_backend.load_messages(ws_id, repair=False)[0]["content"] == "accepted"


def test_plain_key_cannot_acknowledge_attachment_bearing_row(
    storage_backend: StorageBackend,
) -> None:
    ws_id = "commit-cross-seam-conflict"
    commit_key = "one-cross-seam-row"
    content = b"attached"
    attachment = AttachmentWrite(
        attachment_id="e" * 64,
        filename="attached.txt",
        mime_type="text/plain",
        size_bytes=len(content),
        kind="text",
        content=content,
    )
    storage_backend.register_workstream(ws_id)
    storage_backend.save_user_message_with_attachments(
        ws_id,
        "same text",
        [attachment],
        commit_key=commit_key,
    )

    with pytest.raises(ConversationCommitConflictError, match="attachments"):
        storage_backend.save_message(
            ws_id,
            "user",
            "same text",
            commit_key=commit_key,
        )

    assert storage_backend.count_messages(ws_id) == 1
    assert storage_backend.get_attachment(attachment.attachment_id)["refcount"] == 1


def test_empty_commit_key_is_rejected_without_mutation(storage_backend: StorageBackend) -> None:
    ws_id = "commit-empty-key"
    storage_backend.register_workstream(ws_id)

    with pytest.raises(ValueError, match="non-empty"):
        storage_backend.save_message(ws_id, "assistant", "accepted", commit_key="")
    assert memory.save_message(ws_id, "assistant", "accepted", commit_key="") == 0
    assert storage_backend.count_messages(ws_id) == 0


def test_identical_rows_with_distinct_commit_keys_remain_distinct(
    storage_backend: StorageBackend,
) -> None:
    ws_id = "commit-distinct"
    storage_backend.register_workstream(ws_id)

    first_id = storage_backend.save_message(
        ws_id, "assistant", "same payload", commit_key="commit-one"
    )
    second_id = storage_backend.save_message(
        ws_id, "assistant", "same payload", commit_key="commit-two"
    )

    assert second_id != first_id
    assert storage_backend.count_messages(ws_id) == 2
    assert [m["_commit_key"] for m in storage_backend.load_messages(ws_id, repair=False)] == [
        "commit-one",
        "commit-two",
    ]


def test_concurrent_divergent_same_key_writers_fail_one_closed(
    storage_backend: StorageBackend,
) -> None:
    ws_id = "commit-concurrent"
    storage_backend.register_workstream(ws_id)
    barrier = threading.Barrier(3)

    def _save(content: str) -> tuple[str, int | None]:
        barrier.wait(timeout=10)
        try:
            row_id = storage_backend.save_message(
                ws_id,
                "assistant",
                content,
                commit_key="one-admission",
            )
        except ConversationCommitConflictError:
            return "conflict", None
        return "saved", row_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_save, "writer-a")
        second = pool.submit(_save, "writer-b")
        barrier.wait(timeout=10)
        outcomes = [first.result(timeout=10), second.result(timeout=10)]

    assert sorted(status for status, _row_id in outcomes) == ["conflict", "saved"]
    assert len([row_id for _status, row_id in outcomes if row_id is not None]) == 1
    assert storage_backend.count_messages(ws_id) == 1
    assert storage_backend.load_messages(ws_id, repair=False)[0]["content"] in {
        "writer-a",
        "writer-b",
    }


def test_commit_key_is_scoped_to_workstream_and_null_writes_still_append(
    storage_backend: StorageBackend,
) -> None:
    """NULL keys are explicit legacy parent-less writes, never live admission."""
    storage_backend.register_workstream("commit-ws-a")
    storage_backend.register_workstream("commit-ws-b")
    first_id = storage_backend.save_message("commit-ws-a", "assistant", "a", commit_key="same")
    second_id = storage_backend.save_message("commit-ws-b", "assistant", "b", commit_key="same")
    null_one = storage_backend.save_message("commit-null", "assistant", "same")
    null_two = storage_backend.save_message("commit-null", "assistant", "same")

    assert second_id != first_id
    assert null_two != null_one
    assert storage_backend.count_messages("commit-null") == 2
    assert all(
        "_commit_key" not in message
        for message in storage_backend.load_messages("commit-null", repair=False)
    )


def test_commit_key_stays_out_of_public_and_provider_projections(
    storage_backend: StorageBackend,
) -> None:
    storage_backend.register_workstream("commit-private")
    storage_backend.save_message(
        "commit-private", "assistant", "visible", commit_key="private-identity"
    )
    internal = storage_backend.load_messages("commit-private", repair=False)

    assert internal[0]["_commit_key"] == "private-identity"
    assert "_commit_key" not in project_history_messages(internal)[0]
    assert "_commit_key" not in sanitize_messages(internal)[0]
    assert "_commit_key" not in _serialize_messages(internal)[0]
    assert "_commit_key" not in _serialize_messages(internal, include_provider_content=True)[0]


def test_hard_delete_then_keyed_retry_refuses_orphan_recreation(
    storage_backend: StorageBackend,
) -> None:
    ws_id = "commit-delete-retry"
    storage_backend.register_workstream(ws_id)
    storage_backend.save_message(ws_id, "assistant", "accepted", commit_key="one-turn")
    assert storage_backend.delete_workstream(ws_id) is True

    with pytest.raises(ConversationCommitWorkstreamGoneError):
        storage_backend.save_message(ws_id, "assistant", "accepted", commit_key="one-turn")
    # The facade must propagate the typed permanence signal instead of
    # swallowing it into the operational-failure 0: the durability journal
    # classifies this as terminal, never retrying.
    with pytest.raises(ConversationCommitWorkstreamGoneError):
        memory.save_message(ws_id, "assistant", "accepted", commit_key="one-turn")

    assert storage_backend.count_messages(ws_id) == 0
    assert storage_backend.list_orphan_conversations() == []


def test_concurrent_keyed_save_and_hard_delete_leave_no_orphan(
    storage_backend: StorageBackend,
) -> None:
    ws_id = "commit-save-delete-race"
    storage_backend.register_workstream(ws_id)
    barrier = threading.Barrier(3)

    def _save() -> str:
        barrier.wait(timeout=10)
        try:
            storage_backend.save_message(ws_id, "assistant", "accepted", commit_key="one-turn")
        except RuntimeError as exc:
            assert "workstream no longer exists" in str(exc)
            return "refused"
        return "saved"

    def _delete() -> bool:
        barrier.wait(timeout=10)
        return storage_backend.delete_workstream(ws_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        save = pool.submit(_save)
        delete = pool.submit(_delete)
        barrier.wait(timeout=10)
        assert save.result(timeout=10) in {"saved", "refused"}
        assert delete.result(timeout=10) is True

    assert storage_backend.get_workstream(ws_id) is None
    assert storage_backend.count_messages(ws_id) == 0
    assert storage_backend.list_orphan_conversations() == []


def test_postgresql_plain_keyed_insert_uses_parent_lock_and_partial_conflict_target() -> None:
    conn = ScriptedPostgresConnection(
        [
            ScriptedPostgresResult(row=("postgres-plain",)),
            ScriptedPostgresResult(scalar_value=81),
            ScriptedPostgresResult(),
        ]
    )
    backend = PostgreSQLBackend.__new__(PostgreSQLBackend)
    backend._conn = lambda: nullcontext(conn)  # type: ignore[method-assign]

    assert (
        backend.save_message(
            "postgres-plain",
            "assistant",
            "accepted",
            commit_key="plain-key",
        )
        == 81
    )

    sql = [str(statement.compile(dialect=postgresql.dialect())) for statement in conn.statements]
    assert "SELECT workstreams.ws_id" in sql[0] and "FOR UPDATE" in sql[0]
    assert "INSERT INTO conversations" in sql[1]
    assert "ON CONFLICT (ws_id, commit_key)" in sql[1]
    assert "WHERE commit_key IS NOT NULL" in sql[1]
    assert "UPDATE workstreams" in sql[2]
    assert conn.commits == 1
    conn.assert_consumed()


def test_postgresql_null_key_locks_existing_parent_before_legacy_append() -> None:
    """Existing-parent NULL writers share prune's parent-first order."""

    conn = ScriptedPostgresConnection(
        [
            ScriptedPostgresResult(row=("postgres-legacy",)),
            ScriptedPostgresResult(row=("postgres-legacy",)),
            ScriptedPostgresResult(scalar_value=82),
            ScriptedPostgresResult(),
        ]
    )
    backend = PostgreSQLBackend.__new__(PostgreSQLBackend)
    backend._conn = lambda: nullcontext(conn)  # type: ignore[method-assign]

    assert backend.save_message("postgres-legacy", "assistant", "offline") == 82

    sql = [str(statement.compile(dialect=postgresql.dialect())) for statement in conn.statements]
    assert "SELECT workstreams.ws_id" in sql[0] and "FOR UPDATE" not in sql[0]
    assert "SELECT workstreams.ws_id" in sql[1] and "FOR UPDATE" in sql[1]
    assert "INSERT INTO conversations" in sql[2]
    assert "UPDATE workstreams" in sql[3]
    assert conn.commits == 1
    conn.assert_consumed()


def test_postgresql_null_key_preserves_genuinely_parentless_import() -> None:
    conn = ScriptedPostgresConnection(
        [
            ScriptedPostgresResult(row=None),
            ScriptedPostgresResult(row=None),
            ScriptedPostgresResult(scalar_value=83),
            ScriptedPostgresResult(),
        ]
    )
    backend = PostgreSQLBackend.__new__(PostgreSQLBackend)
    backend._conn = lambda: nullcontext(conn)  # type: ignore[method-assign]

    assert backend.save_message("postgres-parentless", "assistant", "offline") == 83

    sql = [str(statement.compile(dialect=postgresql.dialect())) for statement in conn.statements]
    assert "FOR UPDATE" not in sql[0]
    assert "FOR UPDATE" in sql[1]
    assert "INSERT INTO conversations" in sql[2]
    assert conn.commits == 1


def test_postgresql_null_key_refuses_parent_deleted_while_waiting_for_lock() -> None:
    """A blocked writer cannot wake and masquerade as a parentless import."""
    conn = ScriptedPostgresConnection(
        [
            ScriptedPostgresResult(row=("postgres-crossing",)),
            ScriptedPostgresResult(row=None),
        ]
    )
    backend = PostgreSQLBackend.__new__(PostgreSQLBackend)
    backend._conn = lambda: nullcontext(conn)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="crossed workstream deletion"):
        backend.save_message("postgres-crossing", "assistant", "offline")

    sql = [str(statement.compile(dialect=postgresql.dialect())) for statement in conn.statements]
    assert "FOR UPDATE" not in sql[0]
    assert "FOR UPDATE" in sql[1]
    assert all("INSERT INTO conversations" not in statement for statement in sql)
    assert conn.commits == 0


def test_postgresql_bulk_legacy_writer_locks_existing_parents_in_sorted_order() -> None:
    """The batched crossed-deletion gate (round-5 review): one non-locking
    IN-list probe, one ORDER BY ws_id FOR UPDATE IN-list lock (the sorted
    order that keeps concurrent bulk writers deadlock-free), then the batch
    insert and ONE batched updated bump — constant statement count, missing
    parents tolerated."""
    conn = ScriptedPostgresConnection(
        [
            ScriptedPostgresResult(rows=[("ws-a",)]),
            ScriptedPostgresResult(rows=[("ws-a",)]),
            ScriptedPostgresResult(),
            ScriptedPostgresResult(),
        ]
    )
    backend = PostgreSQLBackend.__new__(PostgreSQLBackend)
    backend._conn = lambda: nullcontext(conn)  # type: ignore[method-assign]

    backend.save_messages_bulk(
        [
            {"ws_id": "ws-b", "role": "assistant", "content": "parentless"},
            {"ws_id": "ws-a", "role": "assistant", "content": "attached"},
        ]
    )

    compiled = [statement.compile(dialect=postgresql.dialect()) for statement in conn.statements]
    sql = [str(item) for item in compiled]
    # Both probes carry the full sorted id set in one expanding IN list.
    assert list(compiled[0].params.values()) == [["ws-a", "ws-b"]]
    assert list(compiled[1].params.values()) == [["ws-a", "ws-b"]]
    assert "FOR UPDATE" not in sql[0]
    assert "FOR UPDATE" in sql[1]
    assert "ORDER BY workstreams.ws_id" in sql[1]
    assert "INSERT INTO conversations" in sql[2]
    assert "UPDATE workstreams" in sql[3]
    assert conn.commits == 1
