"""Atomic, idempotent USER-row plus attachment persistence coverage."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from tests._storage_fakes import make_attachment
from turnstone.core import memory
from turnstone.core.storage import (
    AttachmentWrite,
    ConversationCommitConflictError,
    ConversationCommitWorkstreamGoneError,
    _utils,
)
from turnstone.core.storage._postgresql import PostgreSQLBackend


def _attachment(
    attachment_id: str,
    content: bytes,
    *,
    filename: str = "evidence.txt",
    mime_type: str = "text/plain",
    kind: str = "text",
) -> AttachmentWrite:
    return make_attachment(
        attachment_id,
        content,
        filename=filename,
        mime_type=mime_type,
        kind=kind,
    )


def _commit(
    backend: Any,
    ws_id: str,
    attachments: list[AttachmentWrite] | tuple[AttachmentWrite, ...],
    *,
    content: str = "inspect the evidence",
    source: str | None = None,
    event_id: int | None = 17,
    meta: str | None = '{"sender":"alice"}',
    commit_key: str = "one-user-admission",
) -> int:
    return backend.save_user_message_with_attachments(
        ws_id,
        content,
        attachments,
        source=source,
        event_id=event_id,
        meta=meta,
        commit_key=commit_key,
    )


def test_identical_retry_returns_same_row_without_retain_replay(storage_backend: Any) -> None:
    backend = storage_backend
    ws_id = "atomic-attachment-retry"
    backend.register_workstream(ws_id)
    first = _attachment("a" * 64, b"first", filename="first.txt")
    second = _attachment("b" * 64, b"second", filename="second.txt")

    first_id = _commit(backend, ws_id, [first, second])
    retry_id = _commit(backend, ws_id, [first, second])

    assert retry_id == first_id
    assert backend.count_messages(ws_id) == 1
    assert backend.get_attachment(first.attachment_id)["refcount"] == 1
    assert backend.get_attachment(second.attachment_id)["refcount"] == 1
    turn = backend.load_message_turns(ws_id, checkpointed=False)[0]
    assert turn.meta.extra["storage_attachment_ids"] == [
        first.attachment_id,
        second.attachment_id,
    ]


def test_concurrent_identical_retries_retain_each_reference_once(storage_backend: Any) -> None:
    backend = storage_backend
    ws_id = "atomic-attachment-concurrent-retry"
    backend.register_workstream(ws_id)
    attachment = _attachment("9" * 64, b"concurrent")
    barrier = threading.Barrier(3)

    def _write() -> int:
        barrier.wait(timeout=10)
        return _commit(backend, ws_id, [attachment])

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_write)
        second = pool.submit(_write)
        barrier.wait(timeout=10)
        row_ids = {first.result(timeout=10), second.result(timeout=10)}

    assert len(row_ids) == 1
    assert backend.count_messages(ws_id) == 1
    assert backend.get_attachment(attachment.attachment_id)["refcount"] == 1


@pytest.mark.parametrize(
    ("override", "attachments"),
    [
        ({"content": "different text"}, None),
        ({"source": "different_source"}, None),
        ({"event_id": 18}, None),
        ({"meta": '{"sender":"bob"}'}, None),
        ({}, "reverse"),
    ],
)
def test_same_key_with_different_row_or_order_fails_without_mutation(
    storage_backend: Any,
    override: dict[str, Any],
    attachments: str | None,
) -> None:
    backend = storage_backend
    ws_id = f"atomic-attachment-conflict-{override or attachments}"
    backend.register_workstream(ws_id)
    first = _attachment("c" * 64, b"first")
    second = _attachment("d" * 64, b"second")
    _commit(backend, ws_id, [first, second])

    retried = [second, first] if attachments == "reverse" else [first, second]
    with pytest.raises(ConversationCommitConflictError):
        _commit(backend, ws_id, retried, **override)

    assert backend.count_messages(ws_id) == 1
    assert backend.get_attachment(first.attachment_id)["refcount"] == 1
    assert backend.get_attachment(second.attachment_id)["refcount"] == 1


def test_repeated_attachment_ids_count_and_delete_exact_references(storage_backend: Any) -> None:
    backend = storage_backend
    ws_id = "atomic-attachment-repeated-refs"
    backend.register_workstream(ws_id)
    shared = _attachment("e" * 64, b"shared")
    distinct = _attachment("f" * 64, b"distinct")

    _commit(backend, ws_id, [shared, shared, distinct])

    assert backend.get_attachment(shared.attachment_id)["refcount"] == 2
    assert backend.get_attachment(distinct.attachment_id)["refcount"] == 1
    turn = backend.load_message_turns(ws_id, checkpointed=False)[0]
    assert turn.meta.extra["storage_attachment_ids"] == [
        shared.attachment_id,
        shared.attachment_id,
        distinct.attachment_id,
    ]

    assert backend.delete_workstream(ws_id) is True
    assert backend.get_attachment(shared.attachment_id) is None
    assert backend.get_attachment(distinct.attachment_id) is None


def test_hard_delete_then_user_retry_refuses_row_and_refcount_recreation(
    storage_backend: Any,
) -> None:
    backend = storage_backend
    ws_id = "atomic-user-delete-retry"
    backend.register_workstream(ws_id)
    attachment = _attachment("7" * 64, b"deleted")
    _commit(backend, ws_id, [attachment])
    assert backend.delete_workstream(ws_id) is True

    with pytest.raises(RuntimeError, match="workstream no longer exists"):
        _commit(backend, ws_id, [attachment])
    # The facade propagates the typed permanence signal instead of swallowing
    # it into the operational-failure 0: the durability journal classifies a
    # deleted parent as terminal, never retrying.
    with pytest.raises(ConversationCommitWorkstreamGoneError):
        memory.save_user_message_with_attachments(
            ws_id,
            "inspect the evidence",
            [attachment],
            event_id=17,
            meta='{"sender":"alice"}',
            commit_key="one-user-admission",
        )

    assert backend.count_messages(ws_id) == 0
    assert backend.get_attachment(attachment.attachment_id) is None
    assert backend.list_orphan_conversations() == []


def test_concurrent_user_commit_and_delete_leave_no_row_or_refs(storage_backend: Any) -> None:
    backend = storage_backend
    ws_id = "atomic-user-save-delete-race"
    backend.register_workstream(ws_id)
    attachment = _attachment("8" * 64, b"racing-user")
    barrier = threading.Barrier(3)

    def _save() -> str:
        barrier.wait(timeout=10)
        try:
            _commit(backend, ws_id, [attachment])
        except RuntimeError as exc:
            assert "workstream no longer exists" in str(exc)
            return "refused"
        return "saved"

    def _delete() -> bool:
        barrier.wait(timeout=10)
        return backend.delete_workstream(ws_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        save = pool.submit(_save)
        delete = pool.submit(_delete)
        barrier.wait(timeout=10)
        assert save.result(timeout=10) in {"saved", "refused"}
        assert delete.result(timeout=10) is True

    assert backend.get_workstream(ws_id) is None
    assert backend.count_messages(ws_id) == 0
    assert backend.get_attachment(attachment.attachment_id) is None
    assert backend.list_orphan_conversations() == []


def test_failure_after_refcount_increment_rolls_back_every_side_effect(
    storage_backend: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injection target follows the increment into the shared commit body.

    Both backends now run the keyed attachment commit through
    ``_utils.save_attachment_commit_transaction``, so the refcount increment is
    called on ``_utils`` rather than through the dialect module's re-export.
    The property under test is unchanged: a failure after the increment must
    leave no row, no new blob, and no changed refcount.
    """
    backend = storage_backend
    ws_id = "atomic-attachment-rollback"
    backend.register_workstream(ws_id)
    existing = _attachment("1" * 64, b"existing")
    new = _attachment("2" * 64, b"new")
    backend.save_attachment(
        existing.attachment_id,
        existing.filename,
        existing.mime_type,
        existing.size_bytes,
        existing.kind,
        existing.content,
    )
    real_retain = _utils.retain_attachment_refs

    def _retain_then_fail(conn: Any, attachment_ids: list[str]) -> None:
        real_retain(conn, attachment_ids)
        raise RuntimeError("injected failure after refcount update")

    monkeypatch.setattr(_utils, "retain_attachment_refs", _retain_then_fail)
    with pytest.raises(RuntimeError, match="injected failure"):
        _commit(backend, ws_id, [existing, new])

    assert backend.count_messages(ws_id) == 0
    assert backend.get_attachment(existing.attachment_id)["refcount"] == 1
    assert backend.get_attachment(new.attachment_id) is None


@pytest.mark.parametrize("preexisting", [False, True])
def test_content_verification_reads_only_conflicted_blobs(
    storage_backend: Any,
    preexisting: bool,
) -> None:
    """Bytes this transaction just wrote are never read back to verify them.

    The blob insert reports the ids it actually wrote, so verification is
    confined to conflicted (pre-existing) ids — the only ones that can disagree
    with the request.  Re-reading a fresh upload would double the write
    transaction's I/O and extend the parent row's lock hold on the hot path.
    """
    backend = storage_backend
    ws_id = f"atomic-attachment-verify-{preexisting}"
    backend.register_workstream(ws_id)
    attachment = _attachment("2" * 64, b"verified-once")
    if preexisting:
        backend.save_attachment(
            attachment.attachment_id,
            attachment.filename,
            attachment.mime_type,
            attachment.size_bytes,
            attachment.kind,
            attachment.content,
        )
    blob_reads: list[str] = []

    def _before_cursor_execute(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if (
            statement.lstrip().upper().startswith("SELECT")
            and "workstream_attachments" in statement
        ):
            blob_reads.append(statement)

    sa.event.listen(backend._engine, "before_cursor_execute", _before_cursor_execute)
    try:
        _commit(backend, ws_id, [attachment])
    finally:
        sa.event.remove(backend._engine, "before_cursor_execute", _before_cursor_execute)

    assert len(blob_reads) == (1 if preexisting else 0)
    assert backend.count_messages(ws_id) == 1
    assert backend.get_attachment(attachment.attachment_id)["refcount"] == (2 if preexisting else 1)


def test_existing_blob_payload_conflict_rolls_back_conversation(storage_backend: Any) -> None:
    backend = storage_backend
    ws_id = "atomic-attachment-cas-conflict"
    backend.register_workstream(ws_id)
    attachment_id = "3" * 64
    backend.save_attachment(
        attachment_id,
        "original.txt",
        "text/plain",
        len(b"original"),
        "text",
        b"original",
    )

    with pytest.raises(ConversationCommitConflictError):
        _commit(backend, ws_id, [_attachment(attachment_id, b"different")])

    assert backend.count_messages(ws_id) == 0
    stored = backend.get_attachment(attachment_id)
    assert stored["content"] == b"original"
    assert stored["refcount"] == 1


def test_same_key_with_changed_blob_payload_fails_without_refcount_replay(
    storage_backend: Any,
) -> None:
    backend = storage_backend
    ws_id = "atomic-attachment-keyed-blob-conflict"
    backend.register_workstream(ws_id)
    attachment_id = "6" * 64
    original = _attachment(attachment_id, b"original")
    _commit(backend, ws_id, [original])

    with pytest.raises(ConversationCommitConflictError):
        _commit(backend, ws_id, [_attachment(attachment_id, b"changed!")])

    assert backend.count_messages(ws_id) == 1
    stored = backend.get_attachment(attachment_id)
    assert stored["content"] == b"original"
    assert stored["refcount"] == 1


def test_user_memory_facade_preserves_typed_commit_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ConflictStorage:
        def save_user_message_with_attachments(self, *_args: Any, **_kwargs: Any) -> int:
            raise ConversationCommitConflictError("immutable user commit mismatch")

    monkeypatch.setattr(memory, "get_storage", lambda: _ConflictStorage())
    with pytest.raises(ConversationCommitConflictError, match="immutable user commit mismatch"):
        memory.save_user_message_with_attachments(
            "atomic-facade-conflict",
            "text",
            [_attachment("4" * 64, b"payload")],
            commit_key="conflicting-commit",
        )


def test_memory_facade_returns_explicit_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenStorage:
        def save_user_message_with_attachments(self, *_args: Any, **_kwargs: Any) -> int:
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(memory, "get_storage", lambda: _BrokenStorage())
    result = memory.save_user_message_with_attachments(
        "atomic-facade-failure",
        "text",
        [_attachment("4" * 64, b"payload")],
        commit_key="failed-commit",
    )
    assert result == 0


class _PostgresResult:
    def __init__(
        self,
        *,
        scalar: int | None = None,
        row: Any | None = None,
        rows: list[Any] | None = None,
        scalar_values: list[str] | None = None,
    ) -> None:
        self._scalar = scalar
        self._row = row
        self._rows = rows or []
        self._scalar_values = scalar_values or []

    def scalar_one_or_none(self) -> int | None:
        return self._scalar

    def fetchone(self) -> Any | None:
        return self._row

    def fetchall(self) -> list[Any]:
        return self._rows

    def scalars(self) -> list[str]:
        return self._scalar_values


class _PostgresConnection:
    def __init__(self, results: list[_PostgresResult]) -> None:
        self._results = results
        self.statements: list[Any] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> _PostgresResult:
        self.statements.append(statement)
        if not self._results:
            raise AssertionError("unexpected PostgreSQL statement")
        return self._results.pop(0)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_postgresql_atomic_path_emits_conflict_safe_transaction_sql() -> None:
    """The blob insert reports what it wrote; freshly written bytes aren't re-read.

    Rationale for the schedule change: the blob insert now carries
    ``RETURNING attachment_id``, so ids this transaction actually wrote need no
    content verification — it just supplied those bytes.  Re-reading them would
    pull the whole upload back out of the database while the parent row lock is
    held.  Only conflicted (pre-existing) ids are verified, covered by
    ``test_existing_blob_payload_conflict_rolls_back_conversation``.
    """
    attachment = _attachment("5" * 64, b"postgres")
    conn = _PostgresConnection(
        [
            _PostgresResult(row=("postgres-atomic",)),
            _PostgresResult(scalar=41),
            _PostgresResult(scalar_values=[attachment.attachment_id]),
            _PostgresResult(scalar_values=[attachment.attachment_id]),
            _PostgresResult(),
        ]
    )
    backend = PostgreSQLBackend.__new__(PostgreSQLBackend)
    backend._conn = lambda: nullcontext(conn)  # type: ignore[method-assign]

    assert _commit(backend, "postgres-atomic", [attachment]) == 41

    sql = [str(statement.compile(dialect=postgresql.dialect())) for statement in conn.statements]
    assert "SELECT workstreams.ws_id" in sql[0] and "FOR UPDATE" in sql[0]
    assert "INSERT INTO conversations" in sql[1]
    assert "ON CONFLICT" in sql[1]
    assert "WHERE commit_key IS NOT NULL" in sql[1]
    assert "INSERT INTO workstream_attachments" in sql[2]
    assert "ON CONFLICT" in sql[2]
    assert "RETURNING workstream_attachments.attachment_id" in sql[2]
    assert "UPDATE workstream_attachments" in sql[3]
    assert "UPDATE workstreams" in sql[4]
    assert all("SELECT workstream_attachments.attachment_id" not in item for item in sql)
    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert conn._results == []
