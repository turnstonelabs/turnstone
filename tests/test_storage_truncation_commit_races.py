"""Tail-truncation ordering and attachment-GC regression coverage."""

from __future__ import annotations

import json
import threading
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any, cast

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from tests._storage_fakes import (
    ScriptedPostgresConnection,
    ScriptedPostgresResult,
    make_attachment,
    save_keyed,
)
from turnstone.core.storage._postgresql import PostgreSQLBackend

if TYPE_CHECKING:
    from turnstone.core.storage import AttachmentWrite


def _attachment(attachment_id: str, content: bytes) -> AttachmentWrite:
    return make_attachment(attachment_id, content)


def _save_keyed(
    backend: Any,
    ws_id: str,
    kind: str,
    shared: AttachmentWrite,
    added: AttachmentWrite,
) -> int:
    return save_keyed(
        backend,
        ws_id,
        kind,
        content="accepted after truncation",
        commit_key=f"truncate-race-{kind}",
        attachments=[shared, added, added],
        tool_call_id="call-truncate-race",
    )


@pytest.mark.parametrize("operation", ["keep_count", "remove_count"])
@pytest.mark.parametrize("kind", ["plain", "user", "tool"])
def test_storage_tail_truncation_orders_before_keyed_commit_and_releases_exact_refs(
    storage_backend: Any,
    kind: str,
    operation: str,
) -> None:
    """An ACKed keyed commit cannot disappear behind tail truncation.

    Pause legacy truncation after its cutoff or strict truncation after its
    in-transaction total. The transaction must already own the SQLite writer
    reservation or PostgreSQL parent lock, so a later keyed writer cannot
    finish until the doomed row and its exact attachment references have been
    removed and committed.
    """

    backend = storage_backend
    ws_id = f"truncate-keyed-race-{operation}-{kind}"
    shared = _attachment("a" * 64, b"shared")
    doomed = _attachment("b" * 64, b"doomed")
    added = _attachment("c" * 64, b"added")
    backend.register_workstream(ws_id)
    backend.save_tool_message_with_attachments(
        ws_id,
        "keep",
        "read_file",
        "call-keep",
        [shared],
        commit_key="truncate-keep",
    )
    backend.save_tool_message_with_attachments(
        ws_id,
        "remove",
        "read_file",
        "call-remove",
        [shared, doomed, doomed],
        commit_key="truncate-remove",
    )
    assert backend.get_attachment(shared.attachment_id)["refcount"] == 2
    assert backend.get_attachment(doomed.attachment_id)["refcount"] == 2

    # Exercise SQLite's supported no-FTS path so this test also catches a
    # deferred read-to-write upgrade; FTS happens to acquire a writer lock as a
    # side effect, but it is not the truncation transaction's ordering contract.
    if backend.__class__.__name__ == "SQLiteBackend":
        backend._fts5_available = False

    boundary_selected = threading.Event()
    save_attempted = threading.Event()
    save_done = threading.Event()
    outcomes: dict[str, Any] = {}
    errors: list[BaseException] = []

    def _is_pause_select(statement: str) -> bool:
        sql = " ".join(statement.lower().split())
        if operation == "keep_count":
            return (
                sql.startswith("select conversations.id")
                and "order by conversations.id" in sql
                and " offset " in sql
            )
        return sql.startswith("select count(*)") and "from conversations" in sql

    def _before_cursor_execute(
        _conn: Any,
        _cursor: Any,
        _statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if threading.current_thread().name == "keyed-save-after-truncation":
            save_attempted.set()

    def _after_cursor_execute(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if (
            threading.current_thread().name == "paused-tail-truncation"
            and not boundary_selected.is_set()
            and _is_pause_select(statement)
        ):
            boundary_selected.set()
            if not save_attempted.wait(timeout=10):
                raise AssertionError("keyed save never reached its first database operation")
            # An unsafe implementation lets the save commit in this window;
            # the later id-range DELETE then removes an already-ACKed row.
            outcomes["save_crossed_boundary"] = save_done.wait(timeout=0.5)

    def _truncate() -> None:
        try:
            if operation == "keep_count":
                outcomes["deleted"] = backend.delete_messages_after(ws_id, 1)
            else:
                outcomes["deleted"] = backend.truncate_messages_tail(ws_id, 1)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def _save() -> None:
        try:
            outcomes["saved_id"] = _save_keyed(backend, ws_id, kind, shared, added)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            save_done.set()

    sa.event.listen(backend._engine, "before_cursor_execute", _before_cursor_execute)
    sa.event.listen(backend._engine, "after_cursor_execute", _after_cursor_execute)
    truncate_thread = threading.Thread(target=_truncate, name="paused-tail-truncation")
    save_thread = threading.Thread(target=_save, name="keyed-save-after-truncation")
    try:
        truncate_thread.start()
        assert boundary_selected.wait(timeout=10), "truncation never reached its read boundary"
        save_thread.start()
        truncate_thread.join(timeout=10)
        save_thread.join(timeout=10)
    finally:
        sa.event.remove(backend._engine, "before_cursor_execute", _before_cursor_execute)
        sa.event.remove(backend._engine, "after_cursor_execute", _after_cursor_execute)

    assert not truncate_thread.is_alive()
    assert not save_thread.is_alive()
    assert errors == []
    assert outcomes["save_crossed_boundary"] is False
    assert outcomes["deleted"] == 1
    assert isinstance(outcomes["saved_id"], int)

    messages = backend.load_messages(ws_id, repair=False)
    assert [message["_commit_key"] for message in messages] == [
        "truncate-keep",
        f"truncate-race-{kind}",
    ]
    assert backend.count_messages(ws_id) == 2
    expected_shared_refs = 1 if kind == "plain" else 2
    assert backend.get_attachment(shared.attachment_id)["refcount"] == expected_shared_refs
    assert backend.get_attachment(doomed.attachment_id) is None
    if kind == "plain":
        assert backend.get_attachment(added.attachment_id) is None
    else:
        assert backend.get_attachment(added.attachment_id)["refcount"] == 2
    assert backend.list_orphan_conversations() == []


def test_atomic_tail_truncation_clamps_at_latest_compaction_marker(
    storage_backend: Any,
) -> None:
    backend = storage_backend
    ws_id = "truncate-compaction-floor"
    backend.register_workstream(ws_id)
    for index in range(3):
        backend.save_message(ws_id, "user", f"prefix-{index}")
    watermark = backend.get_compaction_watermark(ws_id, 0)
    backend.save_message(
        ws_id,
        "assistant",
        "summary",
        source="compaction",
        meta=json.dumps({"watermark": watermark}),
    )
    backend.save_message(ws_id, "user", "tail-1")
    backend.save_message(ws_id, "assistant", "tail-2")
    assert backend.count_messages(ws_id) == 6
    assert backend.get_compaction_floor(ws_id) == 4

    assert backend.truncate_messages_tail(ws_id, 1) == 1
    assert backend.count_messages(ws_id) == 5
    assert backend.truncate_messages_tail(ws_id, 100) == 1
    assert backend.truncate_messages_tail(ws_id, 1) == 0

    rows = backend.load_messages(
        ws_id,
        repair=False,
        include_compaction=True,
    )
    assert [row["content"] for row in rows] == [
        "prefix-0",
        "prefix-1",
        "prefix-2",
        "summary",
    ]
    assert backend.get_compaction_floor(ws_id) == 4


def test_atomic_tail_truncation_requires_parent_and_valid_count(
    storage_backend: Any,
) -> None:
    backend = storage_backend
    backend.register_workstream("truncate-strict-input")
    backend.save_message("truncate-strict-input", "user", "keep")

    with pytest.raises(ValueError, match="non-negative"):
        backend.truncate_messages_tail("truncate-strict-input", -1)
    assert backend.truncate_messages_tail("truncate-strict-input", 0) == 0
    assert backend.count_messages("truncate-strict-input") == 1

    # Legacy unkeyed storage can contain an orphan row. The strict operation
    # must refuse it rather than mutating history without a durable lock target.
    backend.save_message("truncate-orphan", "assistant", "orphan")
    with pytest.raises(RuntimeError, match="workstream no longer exists"):
        backend.truncate_messages_tail("truncate-orphan", 1)
    assert backend.count_messages("truncate-orphan") == 1


def test_atomic_tail_truncation_rolls_back_rows_and_refs_on_release_failure(
    storage_backend: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = storage_backend
    ws_id = "truncate-release-rollback"
    shared = _attachment("f" * 64, b"shared-rollback")
    doomed = _attachment("0" * 64, b"doomed-rollback")
    backend.register_workstream(ws_id)
    backend.save_tool_message_with_attachments(
        ws_id,
        "keep",
        "read_file",
        "call-rollback-keep",
        [shared],
        commit_key="rollback-keep",
    )
    backend.save_tool_message_with_attachments(
        ws_id,
        "remove",
        "read_file",
        "call-rollback-remove",
        [shared, doomed, doomed],
        commit_key="rollback-remove",
    )

    # The tail-delete body (and its release call) lives in the shared _utils
    # core since the round-4 dedup — patch where the call resolves.
    from turnstone.core.storage import _utils as utils_module

    real_release = utils_module.release_attachment_refs

    def _release_then_fail(conn: Any, attachment_ids: list[str]) -> None:
        real_release(conn, attachment_ids)
        raise RuntimeError("injected truncation release failure")

    monkeypatch.setattr(utils_module, "release_attachment_refs", _release_then_fail)
    with pytest.raises(RuntimeError, match="injected truncation release failure"):
        backend.truncate_messages_tail(ws_id, 1)

    assert backend.count_messages(ws_id) == 2
    assert backend.get_attachment(shared.attachment_id)["refcount"] == 2
    assert backend.get_attachment(doomed.attachment_id)["refcount"] == 2


def test_postgresql_tail_truncation_locks_parent_and_releases_returned_refs() -> None:
    first = "d" * 64
    repeated = "e" * 64
    conn = ScriptedPostgresConnection(
        [
            ScriptedPostgresResult(row=("postgres-truncate",)),
            ScriptedPostgresResult(row=(42,)),
            ScriptedPostgresResult(rows=[(json.dumps([first, repeated, repeated]),), (None,)]),
            ScriptedPostgresResult(),
            ScriptedPostgresResult(),
        ]
    )
    backend = PostgreSQLBackend.__new__(PostgreSQLBackend)
    backend._conn = cast("Any", lambda: nullcontext(conn))  # type: ignore[method-assign]

    assert backend.delete_messages_after("postgres-truncate", 3) == 2

    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    compiled = [statement.compile(dialect=dialect) for statement in conn.statements]
    sql = [" ".join(str(statement).split()) for statement in compiled]
    assert "SELECT workstreams.ws_id" in sql[0] and "FOR UPDATE" in sql[0]
    assert "SELECT conversations.id" in sql[1] and "OFFSET" in sql[1]
    assert "DELETE FROM conversations" in sql[2]
    assert "RETURNING conversations.attachments" in sql[2]
    assert all("SELECT conversations.attachments" not in statement for statement in sql)
    assert "UPDATE workstream_attachments" in sql[3]
    assert "DELETE FROM workstream_attachments" in sql[4]
    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert conn._results == []


def test_postgresql_atomic_tail_truncation_computes_floor_under_parent_lock() -> None:
    first = "1" * 64
    repeated = "2" * 64
    conn = ScriptedPostgresConnection(
        [
            ScriptedPostgresResult(row=("postgres-atomic-truncate",)),
            ScriptedPostgresResult(scalar_value=6),
            ScriptedPostgresResult(scalar_value=14),
            ScriptedPostgresResult(scalar_value=4),
            ScriptedPostgresResult(row=(15,)),
            ScriptedPostgresResult(rows=[(json.dumps([first, repeated, repeated]),), (None,)]),
            ScriptedPostgresResult(),
            ScriptedPostgresResult(),
        ]
    )
    backend = PostgreSQLBackend.__new__(PostgreSQLBackend)
    backend._conn = cast("Any", lambda: nullcontext(conn))  # type: ignore[method-assign]

    assert backend.truncate_messages_tail("postgres-atomic-truncate", 2) == 2

    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    assert str(conn.statements[0]).startswith("SET LOCAL lock_timeout")
    compiled = [statement.compile(dialect=dialect) for statement in conn.statements[1:]]
    sql = [" ".join(str(statement).split()) for statement in compiled]
    assert "SELECT workstreams.ws_id" in sql[0] and "FOR UPDATE" in sql[0]
    assert "count(*)" in sql[1] and "FROM conversations" in sql[1]
    assert "max(conversations.id)" in sql[2]
    assert "count(*)" in sql[3] and "conversations.id <=" in sql[3]
    assert "SELECT conversations.id" in sql[4] and "OFFSET" in sql[4]
    assert 4 in compiled[4].params.values()
    assert "DELETE FROM conversations" in sql[5]
    assert "RETURNING conversations.attachments" in sql[5]
    assert all("SELECT conversations.attachments" not in statement for statement in sql)
    assert "UPDATE workstream_attachments" in sql[6]
    assert "DELETE FROM workstream_attachments" in sql[7]
    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert conn._results == []


def test_postgresql_atomic_tail_truncation_rolls_back_when_parent_is_missing() -> None:
    conn = ScriptedPostgresConnection([ScriptedPostgresResult(row=None)])
    backend = PostgreSQLBackend.__new__(PostgreSQLBackend)
    backend._conn = cast("Any", lambda: nullcontext(conn))  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="workstream no longer exists"):
        backend.truncate_messages_tail("postgres-missing", 1)

    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    assert str(conn.statements[0]).startswith("SET LOCAL lock_timeout")
    sql = " ".join(str(conn.statements[1].compile(dialect=dialect)).split())
    assert "SELECT workstreams.ws_id" in sql and "FOR UPDATE" in sql
    assert conn.commits == 0
    assert conn.rollbacks == 1
    assert conn._results == []
