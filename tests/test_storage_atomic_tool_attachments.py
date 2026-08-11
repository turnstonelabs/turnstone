"""Atomic, idempotent TOOL-row plus attachment persistence coverage."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest
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
    filename: str = "tool-image.png",
    mime_type: str = "image/png",
    kind: str = "image",
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
    content: str = "Image file: result.png",
    tool_name: str = "read_file",
    tool_call_id: str = "call-image",
    event_id: int | None = 29,
    is_error: bool = False,
    meta: str | None = '{"effect_status":"succeeded"}',
    commit_key: str = "one-tool-result",
) -> int:
    return backend.save_tool_message_with_attachments(
        ws_id,
        content,
        tool_name,
        tool_call_id,
        attachments,
        event_id=event_id,
        is_error=is_error,
        meta=meta,
        commit_key=commit_key,
    )


def test_identical_tool_retry_returns_same_row_without_refcount_replay(
    storage_backend: Any,
) -> None:
    backend = storage_backend
    ws_id = "atomic-tool-retry"
    backend.register_workstream(ws_id)
    image = _attachment("a" * 64, b"image-bytes")

    first_id = _commit(backend, ws_id, [image])
    retry_id = _commit(backend, ws_id, [image])

    assert retry_id == first_id
    assert backend.count_messages(ws_id) == 1
    blob = backend.get_attachment(image.attachment_id)
    assert blob["refcount"] == 1
    assert blob["origin"] == "tool"
    turn = backend.load_message_turns(ws_id, checkpointed=False)[0]
    assert turn.tool_call_id == "call-image"
    assert turn.is_error is False
    assert turn.meta.extra["storage_attachment_ids"] == [image.attachment_id]


def test_concurrent_identical_tool_retries_retain_once(storage_backend: Any) -> None:
    backend = storage_backend
    ws_id = "atomic-tool-concurrent"
    backend.register_workstream(ws_id)
    image = _attachment("b" * 64, b"same-result")
    barrier = threading.Barrier(3)

    def _write() -> int:
        barrier.wait(timeout=10)
        return _commit(backend, ws_id, [image])

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_write)
        second = pool.submit(_write)
        barrier.wait(timeout=10)
        row_ids = {first.result(timeout=10), second.result(timeout=10)}

    assert len(row_ids) == 1
    assert backend.count_messages(ws_id) == 1
    assert backend.get_attachment(image.attachment_id)["refcount"] == 1


@pytest.mark.parametrize(
    ("override", "reverse"),
    [
        ({"content": "changed"}, False),
        ({"tool_name": "open_preview"}, False),
        ({"tool_call_id": "call-other"}, False),
        ({"event_id": 30}, False),
        ({"is_error": True}, False),
        ({"meta": '{"effect_status":"unknown"}'}, False),
        ({}, True),
    ],
)
def test_same_key_tool_mismatch_fails_before_refcount_mutation(
    storage_backend: Any,
    override: dict[str, Any],
    reverse: bool,
) -> None:
    backend = storage_backend
    ws_id = f"atomic-tool-conflict-{len(override)}-{reverse}-{next(iter(override), 'refs')}"
    backend.register_workstream(ws_id)
    first = _attachment("c" * 64, b"first")
    second = _attachment("d" * 64, b"second")
    _commit(backend, ws_id, [first, second])

    attachments = [second, first] if reverse else [first, second]
    with pytest.raises(ConversationCommitConflictError):
        _commit(backend, ws_id, attachments, **override)

    assert backend.count_messages(ws_id) == 1
    assert backend.get_attachment(first.attachment_id)["refcount"] == 1
    assert backend.get_attachment(second.attachment_id)["refcount"] == 1


def test_cancelled_preview_duplicate_refs_delete_exactly(storage_backend: Any) -> None:
    backend = storage_backend
    ws_id = "atomic-tool-cancelled-preview"
    backend.register_workstream(ws_id)
    preview = _attachment(
        "e" * 64,
        b"<html>preview</html>",
        filename="preview.html",
        mime_type="text/html",
        kind="preview",
    )
    meta = json.dumps(
        {
            "effect_status": "unknown",
            "preview": {"attachment_id": preview.attachment_id, "kind": "html"},
        }
    )

    _commit(
        backend,
        ws_id,
        [preview, preview],
        content="Tool execution was cancelled before its outcome was observed.",
        tool_name="open_preview",
        tool_call_id="call-preview",
        is_error=True,
        meta=meta,
    )

    blob = backend.get_attachment(preview.attachment_id)
    assert blob["refcount"] == 2
    assert blob["kind"] == "preview"
    assert blob["origin"] == "tool"
    turn = backend.load_message_turns(ws_id, checkpointed=False)[0]
    assert turn.is_error is True
    assert turn.meta.extra["storage_attachment_ids"] == [
        preview.attachment_id,
        preview.attachment_id,
    ]
    assert turn.meta.extra["preview"]["attachment_id"] == preview.attachment_id

    assert backend.delete_workstream(ws_id) is True
    assert backend.get_attachment(preview.attachment_id) is None


def test_hard_delete_then_tool_retry_refuses_row_and_refcount_recreation(
    storage_backend: Any,
) -> None:
    backend = storage_backend
    ws_id = "atomic-tool-delete-retry"
    backend.register_workstream(ws_id)
    attachment = _attachment("8" * 64, b"deleted-tool")
    _commit(backend, ws_id, [attachment])
    assert backend.delete_workstream(ws_id) is True

    with pytest.raises(RuntimeError, match="workstream no longer exists"):
        _commit(backend, ws_id, [attachment])
    # The facade propagates the typed permanence signal instead of swallowing
    # it into the operational-failure 0: the durability journal classifies a
    # deleted parent as terminal, never retrying.
    with pytest.raises(ConversationCommitWorkstreamGoneError):
        memory.save_tool_message_with_attachments(
            ws_id,
            "Image file: result.png",
            "read_file",
            "call-image",
            [attachment],
            event_id=29,
            meta='{"effect_status":"succeeded"}',
            commit_key="one-tool-result",
        )

    assert backend.count_messages(ws_id) == 0
    assert backend.get_attachment(attachment.attachment_id) is None
    assert backend.list_orphan_conversations() == []


def test_concurrent_tool_commit_and_delete_leave_no_row_or_refs(storage_backend: Any) -> None:
    backend = storage_backend
    ws_id = "atomic-tool-save-delete-race"
    backend.register_workstream(ws_id)
    attachment = _attachment("9" * 64, b"racing-tool")
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


def test_tool_partial_failure_rolls_back_row_blobs_refs_and_list(
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
    ws_id = "atomic-tool-rollback"
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
        "tool",
    )
    real_retain = _utils.retain_attachment_refs

    def _retain_then_fail(conn: Any, attachment_ids: list[str]) -> None:
        real_retain(conn, attachment_ids)
        raise RuntimeError("injected tool failure after refcount update")

    monkeypatch.setattr(_utils, "retain_attachment_refs", _retain_then_fail)
    with pytest.raises(RuntimeError, match="injected tool failure"):
        _commit(backend, ws_id, [existing, new])

    assert backend.count_messages(ws_id) == 0
    assert backend.get_attachment(existing.attachment_id)["refcount"] == 1
    assert backend.get_attachment(new.attachment_id) is None


@pytest.mark.parametrize(
    ("mime_type", "kind"),
    [("text/plain", "image"), ("image/png", "preview")],
)
def test_tool_cas_mime_and_kind_conflict_rolls_back(
    storage_backend: Any,
    mime_type: str,
    kind: str,
) -> None:
    backend = storage_backend
    ws_id = f"atomic-tool-cas-{kind}"
    backend.register_workstream(ws_id)
    attachment_id = "3" * 64
    backend.save_attachment(
        attachment_id,
        "existing.png",
        mime_type,
        len(b"same-bytes"),
        kind,
        b"same-bytes",
        "tool",
    )

    with pytest.raises(ConversationCommitConflictError):
        _commit(backend, ws_id, [_attachment(attachment_id, b"same-bytes")])

    assert backend.count_messages(ws_id) == 0
    assert backend.get_attachment(attachment_id)["refcount"] == 1


def test_tool_memory_facade_preserves_typed_commit_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ConflictStorage:
        def save_tool_message_with_attachments(self, *_args: Any, **_kwargs: Any) -> int:
            raise ConversationCommitConflictError("immutable tool commit mismatch")

    monkeypatch.setattr(memory, "get_storage", lambda: _ConflictStorage())
    with pytest.raises(ConversationCommitConflictError, match="immutable tool commit mismatch"):
        memory.save_tool_message_with_attachments(
            "atomic-tool-facade-conflict",
            "result",
            "read_file",
            "call-1",
            [_attachment("4" * 64, b"payload")],
            commit_key="conflicting-tool-commit",
        )


def test_tool_memory_facade_returns_explicit_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenStorage:
        def save_tool_message_with_attachments(self, *_args: Any, **_kwargs: Any) -> int:
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(memory, "get_storage", lambda: _BrokenStorage())
    result = memory.save_tool_message_with_attachments(
        "atomic-tool-facade",
        "result",
        "read_file",
        "call-1",
        [_attachment("4" * 64, b"payload")],
        commit_key="failed-tool-commit",
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
    def __init__(self, results: list[_PostgresResult | BaseException]) -> None:
        self._results = results
        self.statements: list[Any] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> _PostgresResult:
        self.statements.append(statement)
        if not self._results:
            raise AssertionError("unexpected PostgreSQL statement")
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _blob_row(attachment: AttachmentWrite) -> Any:
    return SimpleNamespace(
        _mapping={
            "attachment_id": attachment.attachment_id,
            "mime_type": attachment.mime_type,
            "size_bytes": attachment.size_bytes,
            "kind": attachment.kind,
            "content": attachment.content,
        }
    )


def test_postgresql_tool_insert_uses_one_conflict_safe_transaction() -> None:
    """The blob insert reports what it wrote; freshly written bytes aren't re-read.

    Rationale for the schedule change: the blob insert now carries
    ``RETURNING attachment_id``, so ids this transaction actually wrote need no
    content verification — it just supplied those bytes.  Re-reading them would
    pull the whole tool payload back out of the database while the parent row
    lock is held.  Only conflicted (pre-existing) ids are verified, covered by
    ``test_tool_cas_mime_and_kind_conflict_rolls_back``.
    """
    attachment = _attachment("5" * 64, b"postgres-tool")
    conn = _PostgresConnection(
        [
            _PostgresResult(row=("postgres-tool-insert",)),
            _PostgresResult(scalar=51),
            _PostgresResult(scalar_values=[attachment.attachment_id]),
            _PostgresResult(scalar_values=[attachment.attachment_id]),
            _PostgresResult(),
        ]
    )
    backend = PostgreSQLBackend.__new__(PostgreSQLBackend)
    backend._conn = lambda: nullcontext(conn)  # type: ignore[method-assign]

    assert _commit(backend, "postgres-tool-insert", [attachment], is_error=True) == 51

    compiled = [statement.compile(dialect=postgresql.dialect()) for statement in conn.statements]
    sql = [str(statement) for statement in compiled]
    assert "SELECT workstreams.ws_id" in sql[0] and "FOR UPDATE" in sql[0]
    assert "INSERT INTO conversations" in sql[1] and "ON CONFLICT" in sql[1]
    assert "WHERE commit_key IS NOT NULL" in sql[1]
    assert compiled[1].params["role"] == "tool"
    assert compiled[1].params["tool_name"] == "read_file"
    assert compiled[1].params["tool_call_id"] == "call-image"
    assert compiled[1].params["is_error"] is True
    assert "INSERT INTO workstream_attachments" in sql[2] and "ON CONFLICT" in sql[2]
    assert "RETURNING workstream_attachments.attachment_id" in sql[2]
    assert any(
        key.startswith("origin") and value == "tool" for key, value in compiled[2].params.items()
    )
    assert "UPDATE workstream_attachments" in sql[3]
    assert "UPDATE workstreams" in sql[4]
    assert all("SELECT workstream_attachments.attachment_id" not in item for item in sql)
    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert conn._results == []


def test_postgresql_identical_retry_emits_no_refcount_update() -> None:
    attachment = _attachment("6" * 64, b"postgres-retry")
    existing = SimpleNamespace(
        _mapping={
            "id": 61,
            "role": "tool",
            "content": "Image file: result.png",
            "tool_name": "read_file",
            "tool_call_id": "call-image",
            "provider_data": None,
            "tool_calls": None,
            "_source": None,
            "event_id": 29,
            "is_error": False,
            "attachments": json.dumps([attachment.attachment_id]),
            "meta": '{"effect_status":"succeeded"}',
            "commit_key": "one-tool-result",
        }
    )
    conn = _PostgresConnection(
        [
            _PostgresResult(row=("postgres-tool-retry",)),
            _PostgresResult(scalar=None),
            _PostgresResult(row=existing),
            _PostgresResult(rows=[_blob_row(attachment)]),
        ]
    )
    backend = PostgreSQLBackend.__new__(PostgreSQLBackend)
    backend._conn = lambda: nullcontext(conn)  # type: ignore[method-assign]

    assert _commit(backend, "postgres-tool-retry", [attachment]) == 61

    sql = [str(statement.compile(dialect=postgresql.dialect())) for statement in conn.statements]
    assert len(sql) == 4
    assert all("UPDATE workstream_attachments" not in statement for statement in sql)
    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert conn._results == []


def test_postgresql_partial_failure_rolls_back_the_transaction() -> None:
    attachment = _attachment("7" * 64, b"postgres-rollback")
    conn = _PostgresConnection(
        [
            _PostgresResult(row=("postgres-tool-rollback",)),
            _PostgresResult(scalar=71),
            _PostgresResult(scalar_values=[attachment.attachment_id]),
            RuntimeError("injected PostgreSQL retain failure"),
        ]
    )
    backend = PostgreSQLBackend.__new__(PostgreSQLBackend)
    backend._conn = lambda: nullcontext(conn)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="retain failure"):
        _commit(backend, "postgres-tool-rollback", [attachment])

    sql = [str(statement.compile(dialect=postgresql.dialect())) for statement in conn.statements]
    assert "SELECT workstreams.ws_id" in sql[0] and "FOR UPDATE" in sql[0]
    assert "INSERT INTO conversations" in sql[1]
    assert "INSERT INTO workstream_attachments" in sql[2]
    assert "UPDATE workstream_attachments" in sql[3]
    assert conn.commits == 0
    assert conn.rollbacks == 1
    assert conn._results == []
