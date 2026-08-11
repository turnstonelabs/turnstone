"""Prune ordering and attachment-GC coverage for keyed conversation commits."""

from __future__ import annotations

import threading
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

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
from turnstone.core.storage._schema import workstreams
from turnstone.core.storage._sqlite import SQLiteBackend

if TYPE_CHECKING:
    from turnstone.core.storage import AttachmentWrite


def _make_prune_eligible(backend: Any, *ws_ids: str) -> None:
    """Backdate ``updated`` past the orphan grace so a just-registered row is
    prune-eligible.  The grace itself (a fresh empty workstream survives) is
    pinned in test_sessions.py; these tests exercise lock ordering and
    transaction boundaries, not eligibility."""
    with backend._engine.connect() as conn:
        conn.execute(
            sa.update(workstreams)
            .where(workstreams.c.ws_id.in_(ws_ids))
            .values(updated="2020-01-01T00:00:00")
        )
        conn.commit()


def _attachment() -> AttachmentWrite:
    return make_attachment("d" * 64, b"prune-race-payload", filename="prune.txt")


def _save_keyed(backend: Any, ws_id: str, kind: str, attachment: AttachmentWrite) -> int:
    return save_keyed(
        backend,
        ws_id,
        kind,
        content="accepted" if kind == "plain" else "inspect the evidence",
        commit_key=f"prune-{kind}-admission",
        attachments=[attachment],
        tool_content="Tool result: prune.txt",
        tool_call_id="call-prune",
    )


@pytest.mark.parametrize("kind", ["plain", "user", "tool"])
def test_prune_candidate_lock_orders_before_keyed_commit(
    storage_backend: Any,
    kind: str,
) -> None:
    """A commit admitted after prune's exact recheck cannot land behind it.

    Discovery is only a hint on both backends, which deliberately hold no
    candidate lock (SQLite: no global writer slot) while discovering. Pause
    after the exact predicate recheck that admits deletion. The candidate row
    lock (or SQLite ``BEGIN IMMEDIATE`` reservation) must hold the later keyed
    commit until prune deletes and commits. The old snapshot-then-bulk-delete
    path allowed that commit to finish first and then orphaned its
    conversation.

    ``admission_selects`` counts workstream selects up to that recheck and
    differs by dialect because the transaction shapes do: SQLite discovers (1)
    then rechecks inside its per-candidate writer transaction (2), while
    PostgreSQL discovers (1), relocks the candidate row (2), and rechecks on a
    fresh READ COMMITTED snapshot (3).
    """

    backend = storage_backend
    ws_id = f"prune-keyed-race-{kind}"
    attachment = _attachment()
    backend.register_workstream(ws_id)
    _make_prune_eligible(backend, ws_id)
    admission_selects = 3 if isinstance(backend, PostgreSQLBackend) else 2
    prune_selected = threading.Event()
    save_attempted = threading.Event()
    save_done = threading.Event()
    outcomes: dict[str, Any] = {}
    errors: list[BaseException] = []
    prune_selects = 0

    def _before_cursor_execute(
        _conn: Any,
        _cursor: Any,
        _statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if threading.current_thread().name == "keyed-save-after-prune":
            save_attempted.set()

    def _after_cursor_execute(
        _conn: Any,
        _cursor: Any,
        _statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        nonlocal prune_selects
        if (
            threading.current_thread().name == "paused-prune-candidate"
            and "SELECT workstreams.ws_id" in _statement
        ):
            prune_selects += 1
        if (
            threading.current_thread().name == "paused-prune-candidate"
            and prune_selects == admission_selects
            and not prune_selected.is_set()
        ):
            prune_selected.set()
            if not save_attempted.wait(timeout=10):
                raise AssertionError("keyed save never reached its first database operation")
            # On the unsafe implementation the save does not share prune's
            # parent lock and completes in this window.  The fixed path stays
            # blocked until this callback returns and prune commits deletion.
            save_done.wait(timeout=0.5)

    def _prune() -> None:
        try:
            outcomes["prune"] = backend.prune_workstreams(retention_days=90)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def _save() -> None:
        try:
            _save_keyed(backend, ws_id, kind, attachment)
        except RuntimeError as exc:
            if "workstream no longer exists" not in str(exc):
                errors.append(exc)
            outcomes["save"] = "refused"
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        else:
            outcomes["save"] = "saved"
        finally:
            save_done.set()

    sa.event.listen(backend._engine, "before_cursor_execute", _before_cursor_execute)
    sa.event.listen(backend._engine, "after_cursor_execute", _after_cursor_execute)
    prune_thread = threading.Thread(target=_prune, name="paused-prune-candidate")
    save_thread = threading.Thread(target=_save, name="keyed-save-after-prune")
    try:
        prune_thread.start()
        assert prune_selected.wait(timeout=10), "prune never selected its candidate"
        save_thread.start()
        prune_thread.join(timeout=10)
        save_thread.join(timeout=10)
    finally:
        sa.event.remove(backend._engine, "before_cursor_execute", _before_cursor_execute)
        sa.event.remove(backend._engine, "after_cursor_execute", _after_cursor_execute)

    assert not prune_thread.is_alive()
    assert not save_thread.is_alive()
    assert errors == []
    assert outcomes == {"prune": (1, 0), "save": "refused"}
    assert backend.get_workstream(ws_id) is None
    assert backend.count_messages(ws_id) == 0
    assert backend.get_attachment(attachment.attachment_id) is None
    assert backend.list_orphan_conversations() == []


def test_postgresql_prune_delete_refuses_blocked_null_writer(
    storage_backend: Any,
) -> None:
    """A NULL writer that saw a parent cannot wake as a parentless import.

    Pause prune after its locked exact predicate recheck, then start the legacy
    writer. Its first MVCC observation sees the still-uncommitted parent; its
    second ``FOR UPDATE`` must stay blocked until prune deletes and commits.
    Waking to a missing locked row is therefore a crossing deletion and must
    refuse the insert.

    The recheck is prune's third workstream select since candidates moved to
    bounded per-candidate transactions: discovery (unlocked), the candidate's
    own ``FOR UPDATE SKIP LOCKED``, then the exact predicate recheck.
    """
    backend = storage_backend
    if not isinstance(backend, PostgreSQLBackend):
        pytest.skip("PostgreSQL row-lock schedule")

    ws_id = "prune-null-crossing"
    backend.register_workstream(ws_id)
    _make_prune_eligible(backend, ws_id)
    prune_selected = threading.Event()
    writer_observed_parent = threading.Event()
    writer_done = threading.Event()
    outcomes: dict[str, Any] = {}
    errors: list[BaseException] = []
    prune_selects = 0
    writer_selects = 0

    def _after_cursor_execute(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        nonlocal prune_selects, writer_selects
        thread_name = threading.current_thread().name
        if thread_name == "blocked-null-writer" and "SELECT workstreams.ws_id" in statement:
            writer_selects += 1
            if writer_selects == 1:
                writer_observed_parent.set()
        if thread_name == "prune-before-null-writer" and "SELECT workstreams.ws_id" in statement:
            prune_selects += 1
            if prune_selects == 3 and not prune_selected.is_set():
                prune_selected.set()
                if not writer_observed_parent.wait(timeout=10):
                    raise AssertionError("NULL writer never observed the pre-delete parent")
                assert not writer_done.wait(timeout=0.5), (
                    "NULL writer crossed prune's parent lock before deletion committed"
                )

    def _prune() -> None:
        try:
            outcomes["prune"] = backend.prune_workstreams(retention_days=90)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def _save() -> None:
        try:
            backend.save_message(ws_id, "assistant", "legacy append")
        except RuntimeError as exc:
            if "crossed workstream deletion" not in str(exc):
                errors.append(exc)
            outcomes["save"] = "refused"
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        else:
            outcomes["save"] = "saved"
        finally:
            writer_done.set()

    sa.event.listen(backend._engine, "after_cursor_execute", _after_cursor_execute)
    prune_thread = threading.Thread(target=_prune, name="prune-before-null-writer")
    writer_thread = threading.Thread(target=_save, name="blocked-null-writer")
    try:
        prune_thread.start()
        assert prune_selected.wait(timeout=10), "prune never locked its exact candidate"
        writer_thread.start()
        prune_thread.join(timeout=10)
        writer_thread.join(timeout=10)
    finally:
        sa.event.remove(backend._engine, "after_cursor_execute", _after_cursor_execute)

    assert not prune_thread.is_alive()
    assert not writer_thread.is_alive()
    assert errors == []
    assert writer_selects == 2
    assert outcomes == {"prune": (1, 0), "save": "refused"}
    assert backend.get_workstream(ws_id) is None
    assert backend.count_messages(ws_id) == 0


def _prune_candidate_backend(
    conn: ScriptedPostgresConnection, deleted: list[str]
) -> PostgreSQLBackend:
    backend = PostgreSQLBackend.__new__(PostgreSQLBackend)
    backend._conn = lambda: nullcontext(conn)  # type: ignore[method-assign]

    def _delete(_conn: Any, ws_id: str) -> bool:
        deleted.append(ws_id)
        return True

    backend._delete_workstream_on_connection = _delete  # type: ignore[method-assign]
    return backend


def test_postgresql_prune_candidate_locks_rechecks_and_commits_alone() -> None:
    """Each PostgreSQL prune candidate owns a bounded, locked transaction.

    Prune used to lock the whole candidate set at discovery and delete inside
    one long transaction, so a keyed commit to any candidate waited for the
    entire prune to commit.  Per candidate now: ``FOR UPDATE SKIP LOCKED`` on
    that row alone, then the exact predicate as a separate statement — no lock
    clause, so it reads a fresh READ COMMITTED snapshot and sees a commit that
    landed while this transaction waited for the row — then one commit.
    """
    conn = ScriptedPostgresConnection(
        [ScriptedPostgresResult(row=("bounded",)), ScriptedPostgresResult(row=("bounded",))]
    )
    deleted: list[str] = []
    backend = _prune_candidate_backend(conn, deleted)

    assert backend._delete_prune_candidate("bounded", (workstreams.c.alias.is_(None),)) is True

    sql = [str(statement.compile(dialect=postgresql.dialect())) for statement in conn.statements]
    assert len(sql) == 2
    assert "FOR UPDATE SKIP LOCKED" in sql[0] and "workstreams.alias IS NULL" not in sql[0]
    assert "workstreams.alias IS NULL" in sql[1] and "FOR UPDATE" not in sql[1]
    assert deleted == ["bounded"]
    assert conn.commits == 1
    assert conn.rollbacks == 0


def test_postgresql_prune_candidate_skips_a_row_another_writer_owns() -> None:
    """A locked candidate is left to the next prune, unrechecked and undeleted."""
    conn = ScriptedPostgresConnection([ScriptedPostgresResult(row=None)])
    deleted: list[str] = []
    backend = _prune_candidate_backend(conn, deleted)

    assert backend._delete_prune_candidate("locked", (workstreams.c.alias.is_(None),)) is False

    assert len(conn.statements) == 1
    assert deleted == []
    assert conn.commits == 1
    assert conn.rollbacks == 0


def test_postgresql_prune_commits_each_candidate_before_the_next(
    storage_backend: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A candidate's deletion is durable before prune reaches the next one.

    PostgreSQL prune used to lock the whole candidate set at discovery and
    commit once at the end, so every candidate's row lock — and any keyed
    commit waiting on it — was held for the entire run. An independent
    connection must be able to observe candidate 1 already gone while prune is
    still working.
    """
    backend = storage_backend
    if not isinstance(backend, PostgreSQLBackend):
        pytest.skip("PostgreSQL per-candidate transaction boundary")

    backend.register_workstream("prune-bounded-a")
    backend.register_workstream("prune-bounded-b")
    _make_prune_eligible(backend, "prune-bounded-a", "prune-bounded-b")
    real_delete_candidate = backend._delete_prune_candidate
    observed: list[str | None] = []

    def _gated_delete_candidate(ws_id: str, predicates: tuple[Any, ...]) -> bool:
        deleted = real_delete_candidate(ws_id, predicates)
        if deleted and len(observed) == 0:
            # Read on a fresh connection: an uncommitted delete would still
            # show the row here.
            observed.append(backend.get_workstream(ws_id))
        return deleted

    monkeypatch.setattr(backend, "_delete_prune_candidate", _gated_delete_candidate)

    assert backend.prune_workstreams(retention_days=90) == (2, 0)
    assert observed == [None]
    assert backend.get_workstream("prune-bounded-b") is None


def test_sqlite_prune_releases_writer_slot_between_candidates(
    storage_backend: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrelated live commits do not wait for the complete prune candidate set."""

    backend = storage_backend
    if not isinstance(backend, SQLiteBackend):
        pytest.skip("SQLite's database-wide writer slot is dialect-specific")

    backend.register_workstream("prune-bounded-a")
    backend.register_workstream("prune-bounded-b")
    _make_prune_eligible(backend, "prune-bounded-a", "prune-bounded-b")
    backend.register_workstream("prune-unrelated-live")
    backend.save_message("prune-unrelated-live", "user", "durable prefix")

    between_candidates = threading.Event()
    writer_done = threading.Event()
    writer_completed_between = threading.Event()
    outcomes: dict[str, Any] = {}
    errors: list[BaseException] = []
    real_delete_candidate = backend._delete_prune_candidate
    deleted_candidates = 0

    def _gated_delete_candidate(ws_id: str, predicates: tuple[Any, ...]) -> bool:
        nonlocal deleted_candidates
        deleted = real_delete_candidate(ws_id, predicates)
        if deleted:
            deleted_candidates += 1
            if deleted_candidates == 1:
                # The helper has committed and returned: no SQLite writer
                # reservation may remain while Python advances to candidate 2.
                between_candidates.set()
                if writer_done.wait(timeout=5):
                    writer_completed_between.set()
                else:
                    raise AssertionError("unrelated writer stayed blocked between candidates")
        return deleted

    monkeypatch.setattr(backend, "_delete_prune_candidate", _gated_delete_candidate)

    def _prune() -> None:
        try:
            outcomes["prune"] = backend.prune_workstreams(retention_days=90)
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    def _write() -> None:
        try:
            outcomes["row"] = backend.save_message(
                "prune-unrelated-live",
                "assistant",
                "committed between prune candidates",
                commit_key="prune-unrelated-key",
            )
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)
        finally:
            writer_done.set()

    prune_thread = threading.Thread(target=_prune, name="bounded-prune")
    writer_thread = threading.Thread(target=_write, name="unrelated-prune-writer")
    prune_thread.start()
    assert between_candidates.wait(timeout=10), "prune never completed its first candidate"
    writer_thread.start()
    writer_thread.join(timeout=10)
    prune_thread.join(timeout=10)

    assert not writer_thread.is_alive()
    assert not prune_thread.is_alive()
    assert errors == []
    assert writer_completed_between.is_set()
    assert outcomes["prune"] == (2, 0)
    assert int(outcomes["row"]) > 0
    assert backend.count_messages("prune-unrelated-live") == 2


@pytest.mark.parametrize("kind", ["user", "tool"])
def test_stale_prune_releases_exact_attachment_reference_counts(
    storage_backend: Any,
    kind: str,
) -> None:
    backend = storage_backend
    stale_ws_id = f"prune-stale-attachment-{kind}"
    survivor_ws_id = f"prune-surviving-attachment-{kind}"
    attachment = _attachment()
    backend.register_workstream(stale_ws_id)
    backend.register_workstream(survivor_ws_id)

    if kind == "user":
        backend.save_user_message_with_attachments(
            stale_ws_id,
            "stale",
            [attachment, attachment],
            commit_key="stale-user",
        )
        backend.save_user_message_with_attachments(
            survivor_ws_id,
            "survives",
            [attachment],
            commit_key="surviving-user",
        )
    else:
        backend.save_tool_message_with_attachments(
            stale_ws_id,
            "stale",
            "read_file",
            "call-stale",
            [attachment, attachment],
            commit_key="stale-tool",
        )
        backend.save_tool_message_with_attachments(
            survivor_ws_id,
            "survives",
            "read_file",
            "call-surviving",
            [attachment],
            commit_key="surviving-tool",
        )

    assert backend.get_attachment(attachment.attachment_id)["refcount"] == 3
    with backend._engine.connect() as conn:
        conn.execute(
            sa.update(workstreams)
            .where(workstreams.c.ws_id == stale_ws_id)
            .values(updated="2020-01-01T00:00:00")
        )
        conn.commit()

    assert backend.prune_workstreams(retention_days=30) == (0, 1)

    assert backend.get_workstream(stale_ws_id) is None
    assert backend.count_messages(stale_ws_id) == 0
    assert backend.count_messages(survivor_ws_id) == 1
    assert backend.get_attachment(attachment.attachment_id)["refcount"] == 1
    assert backend.list_orphan_conversations() == []
