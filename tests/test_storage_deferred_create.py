"""Backend and manager regressions for durable deferred-create publication."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from tests.test_session_manager import FakeAdapter
from turnstone.core.session_manager import SessionManager
from turnstone.core.storage import ForkCloneExpectation, ForkDestinationConflictError
from turnstone.core.storage._postgresql import PostgreSQLBackend
from turnstone.core.storage._protocol import FORK_RESERVATION_CONFIG_KEY
from turnstone.core.storage._schema import workstream_config, workstreams


def _row_ids(rows: list[Any]) -> set[str]:
    return {str(row._mapping["ws_id"] if hasattr(row, "_mapping") else row[0]) for row in rows}


def _raw_config(backend: Any, ws_id: str) -> dict[str, str]:
    with backend._conn() as conn:
        rows = conn.execute(
            sa.select(workstream_config.c.key, workstream_config.c.value).where(
                workstream_config.c.ws_id == ws_id
            )
        ).all()
    return {str(key): str(value) for key, value in rows}


def _force_updated(backend: Any, ws_ids: list[str], updated: str) -> None:
    with backend._engine.connect() as conn:
        conn.execute(
            sa.update(workstreams).where(workstreams.c.ws_id.in_(ws_ids)).values(updated=updated)
        )
        conn.commit()


class _UnknownRowcountResult:
    """Successful PostgreSQL DML result from a driver without sane rowcount."""

    rowcount = -1

    def __init__(
        self,
        *,
        row: tuple[Any, ...] | None = None,
        rows: list[tuple[Any, ...]] | None = None,
    ) -> None:
        self._row = row
        self._rows = rows or []

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _ScriptedPostgresConnection:
    def __init__(self, *results: _UnknownRowcountResult) -> None:
        self._results = list(results)
        self.statements: list[Any] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> _UnknownRowcountResult:
        self.statements.append(statement)
        if not self._results:
            raise AssertionError("unexpected PostgreSQL execute")
        return self._results.pop(0)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def assert_consumed(self) -> None:
        assert self._results == []


def _scripted_postgres_backend(
    *results: _UnknownRowcountResult,
) -> tuple[PostgreSQLBackend, _ScriptedPostgresConnection]:
    backend = PostgreSQLBackend.__new__(PostgreSQLBackend)
    conn = _ScriptedPostgresConnection(*results)
    backend._conn = lambda: nullcontext(conn)  # type: ignore[method-assign]
    return backend, conn


def _register_creating(
    backend: Any,
    ws_id: str,
    token: str,
    *,
    alias: str | None = None,
) -> None:
    assert (
        backend.register_workstream(
            ws_id,
            node_id="node-a",
            name="hidden create",
            state="creating",
            user_id="alice",
            alias=alias,
            kind="interactive",
            fork_reservation_token=token,
        )
        is True
    )


def _seed_visible_and_creating(backend: Any) -> tuple[str, str, str]:
    visible_id = "visible-row-1234"
    creating_id = "creating-row-5678"
    token = "history-incarnation"
    assert (
        backend.register_workstream(
            visible_id,
            node_id="node-a",
            name="published control",
            state="idle",
            user_id="alice",
            kind="interactive",
        )
        is True
    )
    _register_creating(backend, creating_id, token)
    backend.save_message(visible_id, "user", "deferredvisibilityneedle published")
    backend.save_message(creating_id, "user", "deferredvisibilityneedle hidden")
    return visible_id, creating_id, token


def test_creating_row_is_hidden_from_ordinary_storage_discovery(storage_backend: Any) -> None:
    backend = storage_backend
    ws_id = "creating-row-1234"
    token = "creating-incarnation"
    _register_creating(backend, ws_id, token, alias="hidden-alias")
    backend.save_message(ws_id, "user", "history must not make this visible")

    # Raw identity reads remain available to reservation owners and cleanup.
    raw = backend.get_workstream(ws_id)
    assert raw is not None
    assert raw["state"] == "creating"
    assert "fork_reservation_token" not in raw
    assert backend.load_workstream_config(ws_id) == {}
    assert backend.get_workstream_reservation_token(ws_id) == token
    assert _raw_config(backend, ws_id) == {FORK_RESERVATION_CONFIG_KEY: token}

    # User-facing discovery never exposes a half-constructed durable row,
    # even when it already has an alias and conversation history.
    assert ws_id not in _row_ids(backend.list_workstreams(user_id="alice"))
    assert ws_id not in _row_ids(backend.list_workstreams_with_history(user_id="alice"))
    assert backend.resolve_workstream(ws_id) is None
    assert backend.resolve_workstream("creating-row") is None
    assert backend.resolve_workstream("hidden-alias") is None


def test_stale_creating_reaper_hard_deletes_dependents_and_attachment_refs(
    storage_backend: Any,
) -> None:
    backend = storage_backend
    ws_id = "abandoned-create-1234"
    token = "abandoned-incarnation"
    attachment_id = "a" * 64
    _register_creating(backend, ws_id, token)
    message_id = backend.save_message(ws_id, "user", "private cloned content")
    backend.save_attachment(
        attachment_id,
        "private.txt",
        "text/plain",
        7,
        "text",
        b"private",
    )
    backend.set_message_attachments(ws_id, message_id, [attachment_id])
    assert backend.finalize_deferred_create(
        ws_id,
        token,
        alias="reclaimable-alias",
        config={"model_alias": "private-model"},
        node_id="stable-node",
    )
    _force_updated(backend, [ws_id], "2020-01-01T00:00:00")

    deleted = backend.delete_stale_creating_reservations(
        "interactive",
        "2024-01-01T00:00:00",
        [],
        live_node_ids=["stable-node"],
        local_node_id="stable-node",
    )

    assert deleted == [ws_id]
    assert backend.get_workstream(ws_id) is None
    assert backend.load_message_turns(ws_id) == []
    assert _raw_config(backend, ws_id) == {}
    assert backend.get_attachment(attachment_id) is None
    assert ws_id not in {row["ws_id"] for row in backend.list_workstream_overrides()}
    assert (
        backend.register_workstream(
            ws_id,
            alias="reclaimable-alias",
            state="idle",
            kind="interactive",
        )
        is True
    )


def test_stale_creating_reaper_fences_state_age_owner_token_and_loaded_ids(
    storage_backend: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = storage_backend
    stale_ids = [
        "same-node-abandoned",
        "same-node-loaded",
        "dead-peer-abandoned",
        "live-peer-protected",
        "published-protected",
        "tokenless-protected",
    ]
    for ws_id, node_id, token in [
        ("same-node-abandoned", "stable-node", "same-token"),
        ("same-node-loaded", "stable-node", "loaded-token"),
        ("dead-peer-abandoned", "dead-peer", "dead-token"),
        ("live-peer-protected", "live-peer", "live-token"),
        ("published-protected", "dead-peer", "published-token"),
        ("tokenless-protected", "dead-peer", ""),
    ]:
        assert (
            backend.register_workstream(
                ws_id,
                node_id=node_id,
                state="creating",
                kind="interactive",
                fork_reservation_token=token,
            )
            is True
        )
    assert backend.publish_deferred_create("published-protected", "published-token") is True
    _force_updated(backend, stale_ids, "2020-01-01T00:00:00")
    _register_creating(backend, "fresh-protected", "fresh-token")

    deleted = backend.delete_stale_creating_reservations(
        "interactive",
        "2024-01-01T00:00:00",
        ["same-node-loaded"],
        live_node_ids=["stable-node", "live-peer"],
        local_node_id="stable-node",
    )

    assert set(deleted) == {
        "same-node-abandoned",
        "dead-peer-abandoned",
        "tokenless-protected",
    }
    assert "storage.stale_create_tokenless_reaped" in caplog.text
    for ws_id in [
        "same-node-loaded",
        "live-peer-protected",
        "published-protected",
        "fresh-protected",
    ]:
        assert backend.get_workstream(ws_id) is not None
    assert backend.get_workstream("published-protected")["state"] == "idle"

    # The public protocol requires an authoritative liveness result. Backends
    # still fail closed if a non-conforming caller passes uncertainty through.
    assert (
        backend.delete_stale_creating_reservations(
            "interactive",
            "2024-01-01T00:00:00",
            [],
            live_node_ids=None,  # type: ignore[arg-type]
            local_node_id="stable-node",
        )
        == []
    )


def test_retention_prune_leaves_stale_creating_for_complete_reaper(
    storage_backend: Any,
) -> None:
    backend = storage_backend
    ws_id = "creating-owned-by-reaper"
    _register_creating(backend, ws_id, "retention-incarnation")
    backend.save_message(ws_id, "user", "cloned history with dependent data")
    _force_updated(backend, [ws_id], "2020-01-01T00:00:00")

    orphans, stale = backend.prune_workstreams(retention_days=30)

    assert (orphans, stale) == (0, 0)
    row = backend.get_workstream(ws_id)
    assert row is not None
    assert row["state"] == "creating"


def test_history_child_count_excludes_creating_until_publication(storage_backend: Any) -> None:
    backend = storage_backend
    parent_id = "published-parent-1234"
    child_id = "creating-child-1234"
    token = "child-incarnation"
    assert (
        backend.register_workstream(
            parent_id,
            name="published parent",
            state="idle",
            user_id="alice",
            kind="coordinator",
        )
        is True
    )
    backend.save_message(parent_id, "user", "make the parent listable")
    assert (
        backend.register_workstream(
            child_id,
            name="hidden child",
            state="creating",
            user_id="alice",
            kind="interactive",
            parent_ws_id=parent_id,
            fork_reservation_token=token,
        )
        is True
    )

    rows = backend.list_workstreams_with_history(kind="coordinator", user_id="alice")
    assert len(rows) == 1
    assert rows[0][0] == parent_id
    assert rows[0][12] == 0

    assert backend.publish_deferred_create(child_id, token) is True

    rows = backend.list_workstreams_with_history(kind="coordinator", user_id="alice")
    assert len(rows) == 1
    assert rows[0][0] == parent_id
    assert rows[0][12] == 1


def test_history_apis_exclude_creating_until_publication(storage_backend: Any) -> None:
    backend = storage_backend
    visible_id, creating_id, token = _seed_visible_and_creating(backend)

    assert _row_ids(backend.list_workstreams_with_history(user_id="alice")) == {visible_id}
    assert {str(row[1]) for row in backend.search_history("deferredvisibilityneedle")} == {
        visible_id
    }
    assert {str(row[1]) for row in backend.search_history_recent(limit=20)} == {visible_id}

    assert backend.publish_deferred_create(creating_id, token) is True

    expected = {visible_id, creating_id}
    assert _row_ids(backend.list_workstreams_with_history(user_id="alice")) == expected
    assert {str(row[1]) for row in backend.search_history("deferredvisibilityneedle")} == expected
    assert {str(row[1]) for row in backend.search_history_recent(limit=20)} == expected


def test_workstream_counts_exclude_creating_until_publication(storage_backend: Any) -> None:
    backend = storage_backend
    _visible_id, creating_id, token = _seed_visible_and_creating(backend)
    since = "1970-01-01T00:00:00"

    assert backend.count_workstreams_by_state(user_id="alice") == {"idle": 1}
    assert backend.count_workstreams_since(since, user_id="alice") == 1

    assert backend.publish_deferred_create(creating_id, token) is True

    assert backend.count_workstreams_by_state(user_id="alice") == {"idle": 2}
    assert backend.count_workstreams_since(since, user_id="alice") == 2


def test_publish_deferred_create_is_exact_token_cas_and_exposes_idle_row(
    storage_backend: Any,
) -> None:
    backend = storage_backend
    ws_id = "publish-row-1234"
    token = "publish-incarnation"
    _register_creating(backend, ws_id, token, alias="published-alias")
    backend.save_message(ws_id, "user", "visible only after publication")
    before = backend.get_workstream(ws_id)
    before_config = _raw_config(backend, ws_id)

    assert backend.publish_deferred_create(ws_id, "wrong-incarnation") is False
    assert backend.get_workstream(ws_id) == before
    assert backend.get_workstream_reservation_token(ws_id) == token
    assert _raw_config(backend, ws_id) == before_config
    assert backend.resolve_workstream(ws_id) is None

    assert backend.publish_deferred_create(ws_id, token) is True
    published = backend.get_workstream(ws_id)
    assert published is not None
    assert published["state"] == "idle"
    # Publication consumes the creating marker, not the private incarnation
    # fence. Rollback and clone authorization still compare this exact token.
    assert backend.get_workstream_reservation_token(ws_id) == token
    assert _raw_config(backend, ws_id) == {FORK_RESERVATION_CONFIG_KEY: token}
    assert ws_id in _row_ids(backend.list_workstreams(user_id="alice"))
    assert ws_id in _row_ids(backend.list_workstreams_with_history(user_id="alice"))
    assert backend.resolve_workstream(ws_id) == ws_id
    assert backend.resolve_workstream("published-alias") == ws_id

    # State is part of the CAS. A duplicate publication is a refusal and must
    # leave the already-visible incarnation unchanged.
    published_config = _raw_config(backend, ws_id)
    assert backend.publish_deferred_create(ws_id, token) is False
    assert backend.get_workstream(ws_id) == published
    assert _raw_config(backend, ws_id) == published_config


def test_published_destination_cannot_reuse_retained_token_for_clone(
    storage_backend: Any,
) -> None:
    backend = storage_backend
    token = "destination-incarnation"
    assert backend.register_workstream("source", user_id="alice", kind="interactive") is True
    backend.save_message("source", "user", "must not copy after publication")
    source_snapshot = backend.ensure_workstream_incarnation_snapshot("source")
    assert source_snapshot is not None
    _register_creating(backend, "destination", token)
    assert backend.publish_deferred_create("destination", token) is True

    expectation = ForkCloneExpectation(
        persona_config=(),
        project_id="",
        project_name="",
        project_writable=False,
        destination_reservation_token=token,
        source_reservation_token=source_snapshot["fork_reservation_token"],
    )
    with pytest.raises(ForkDestinationConflictError, match="destination is not available"):
        backend.clone_workstream(
            "source",
            "destination",
            principal_id="alice",
            expected_session=expectation,
        )

    destination = backend.get_workstream("destination")
    assert destination is not None
    assert destination["state"] == "idle"
    assert backend.load_message_turns("destination") == []
    assert backend.get_workstream_reservation_token("destination") == token
    assert _raw_config(backend, "destination") == {FORK_RESERVATION_CONFIG_KEY: token}


def test_other_manager_cannot_open_until_exact_create_publication(storage_backend: Any) -> None:
    backend = storage_backend
    creator_adapter = FakeAdapter()
    observer_adapter = FakeAdapter()
    state_at_emit: list[str | None] = []
    original_emit_created = creator_adapter.emit_created

    def _record_durable_state_at_emit(ws: Any) -> None:
        row = backend.get_workstream(ws.id)
        state_at_emit.append(None if row is None else str(row["state"]))
        original_emit_created(ws)

    creator_adapter.emit_created = _record_durable_state_at_emit  # type: ignore[method-assign]
    creator = SessionManager(
        creator_adapter,
        storage=backend,
        max_active=2,
        node_id="creator-node",
        event_emitter=creator_adapter,
    )
    observer = SessionManager(
        observer_adapter,
        storage=backend,
        max_active=2,
        node_id="observer-node",
        event_emitter=observer_adapter,
    )
    ws_id = "manager-publication-1234"
    pending = creator.create(
        ws_id=ws_id,
        user_id="alice",
        name="manager publication",
        defer_emit_created=True,
    )
    token = pending._fork_reservation_token
    assert token
    raw = backend.get_workstream(ws_id)
    assert raw is not None
    assert raw["state"] == "creating"

    assert observer.open(ws_id) is None
    assert observer_adapter.events == []
    assert creator.commit_create(pending) is True
    assert state_at_emit == ["idle"]

    published = backend.get_workstream(ws_id)
    assert published is not None
    assert published["state"] == "idle"
    assert _raw_config(backend, ws_id)[FORK_RESERVATION_CONFIG_KEY] == token
    reopened = observer.open(ws_id)
    assert reopened is not None
    assert reopened.id == ws_id
    assert [event.kind for event in observer_adapter.events] == ["rehydrated"]


def test_postgresql_register_uses_returning_when_driver_rowcount_is_unknown() -> None:
    """A successful insert is not a collision merely because rowcount is -1."""
    ws_id = "postgres-register-returning"
    token = "postgres-register-incarnation"
    backend, conn = _scripted_postgres_backend(
        _UnknownRowcountResult(row=(ws_id,)),
        _UnknownRowcountResult(),
    )

    assert (
        backend.register_workstream(
            ws_id,
            state="creating",
            fork_reservation_token=token,
        )
        is True
    )
    conn.assert_consumed()
    assert conn.commits == 1
    assert conn.rollbacks == 0


def test_postgresql_publish_uses_returning_when_driver_rowcount_is_unknown() -> None:
    """A successful creating-to-idle CAS is recognized through RETURNING."""
    ws_id = "postgres-publish-returning"
    token = "postgres-publish-incarnation"
    backend, conn = _scripted_postgres_backend(
        _UnknownRowcountResult(row=("creating",)),
        _UnknownRowcountResult(row=(token,)),
        _UnknownRowcountResult(row=(ws_id,)),
    )

    assert backend.publish_deferred_create(ws_id, token) is True
    conn.assert_consumed()
    assert conn.commits == 1
    assert conn.rollbacks == 0


def test_postgresql_conditional_delete_uses_returning_when_rowcount_is_unknown() -> None:
    """Exact-token deletion recognizes a successful final DELETE via RETURNING."""
    ws_id = "postgres-delete-returning"
    token = "postgres-delete-incarnation"
    backend, conn = _scripted_postgres_backend(
        _UnknownRowcountResult(row=(ws_id,)),
        _UnknownRowcountResult(row=(token,)),
        _UnknownRowcountResult(rows=[]),
        _UnknownRowcountResult(),
        _UnknownRowcountResult(),
        _UnknownRowcountResult(),
        _UnknownRowcountResult(),
        _UnknownRowcountResult(row=(ws_id,)),
    )

    assert backend.delete_workstream_if_fork_reserved(ws_id, token) is True
    conn.assert_consumed()
    assert conn.commits == 1
    assert conn.rollbacks == 0


def test_postgresql_stale_creating_reaper_locks_state_age_and_exact_incarnation() -> None:
    ws_id = "postgres-stale-create"
    token = "postgres-stale-incarnation"
    backend, conn = _scripted_postgres_backend(
        _UnknownRowcountResult(rows=[(ws_id,)]),
        _UnknownRowcountResult(row=(token,)),
        _UnknownRowcountResult(row=(ws_id,)),
        _UnknownRowcountResult(rows=[]),
        _UnknownRowcountResult(),
        _UnknownRowcountResult(),
        _UnknownRowcountResult(),
        _UnknownRowcountResult(),
        _UnknownRowcountResult(row=(ws_id,)),
    )

    assert backend.delete_stale_creating_reservations(
        "interactive",
        "2024-01-01T00:00:00",
        [],
        live_node_ids=["stable-node", "live-peer"],
        local_node_id="stable-node",
    ) == [ws_id]

    conn.assert_consumed()
    assert conn.commits == 1
    assert conn.rollbacks == 0
    candidate_sql = str(conn.statements[0].compile(dialect=postgresql.dialect())).lower()
    assert "workstreams.state" in candidate_sql
    assert "workstreams.updated" in candidate_sql
    assert "for update skip locked" in candidate_sql
    token_sql = str(conn.statements[1].compile(dialect=postgresql.dialect())).lower()
    assert "workstream_config" in token_sql
    assert "for update" in token_sql
    exact_sql = str(conn.statements[2].compile(dialect=postgresql.dialect())).lower()
    assert "workstreams.state" in exact_sql
    assert "workstreams.updated" in exact_sql
    assert "workstream_config.value" in exact_sql


def test_postgresql_stale_creating_reaper_recovers_tokenless_locked_row(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ws_id = "postgres-tokenless-stale-create"
    backend, conn = _scripted_postgres_backend(
        _UnknownRowcountResult(rows=[(ws_id,)]),
        _UnknownRowcountResult(),
        _UnknownRowcountResult(row=(ws_id,)),
        _UnknownRowcountResult(rows=[]),
        _UnknownRowcountResult(),
        _UnknownRowcountResult(),
        _UnknownRowcountResult(),
        _UnknownRowcountResult(),
        _UnknownRowcountResult(row=(ws_id,)),
    )

    assert backend.delete_stale_creating_reservations(
        "interactive",
        "2024-01-01T00:00:00",
        [],
        live_node_ids=[],
        local_node_id="stable-node",
    ) == [ws_id]

    conn.assert_consumed()
    assert conn.commits == 1
    exact_sql = str(conn.statements[2].compile(dialect=postgresql.dialect())).lower()
    assert "workstreams.state" in exact_sql
    assert "workstreams.updated" in exact_sql
    assert "workstream_config.value" not in exact_sql
    assert "storage.stale_create_tokenless_reaped" in caplog.text


def test_postgresql_retention_prune_excludes_creating_rows() -> None:
    """Both candidate discoveries exclude provisional rows, and take no locks.

    Discovery moved out of the deleting transaction when prune became one
    bounded transaction per candidate: it is a plain read now, with no
    ``FOR UPDATE SKIP LOCKED`` and nothing to commit.  That lock rides each
    candidate's own transaction instead — see
    ``test_postgresql_prune_candidate_locks_rechecks_and_commits_alone`` in
    tests/test_storage_prune_commit_races.py.  The predicates themselves are
    unchanged, which is what this test pins.
    """
    backend, conn = _scripted_postgres_backend(
        _UnknownRowcountResult(rows=[]),
        _UnknownRowcountResult(rows=[]),
    )

    assert backend.prune_workstreams(retention_days=30) == (0, 0)

    conn.assert_consumed()
    assert conn.commits == 0
    orphan_select_sql = str(
        conn.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    stale_select_sql = str(
        conn.statements[1].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    for candidate_sql in (orphan_select_sql, stale_select_sql):
        assert "workstreams.state" in candidate_sql
        assert "creating" in candidate_sql
        assert "for update" not in candidate_sql
    assert "not (exists" in orphan_select_sql
    # Round-3 review guards: the orphan category excludes named workstreams
    # (explicit user intent) and rows younger than the grace (a user
    # mid-first-turn whose rows may still be journal-held on another node).
    assert "workstreams.alias is null" in orphan_select_sql
    assert "workstreams.updated" in orphan_select_sql
    assert "workstreams.updated" in stale_select_sql
