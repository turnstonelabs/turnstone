"""Concurrency regression test for the PostgreSQL migration advisory lock.

Reproduces the multi-node boot scenario: several workers run migrations against
the *same fresh* PostgreSQL database simultaneously (as 10 containers do on
``docker compose up``). Migration 041 rebuilds an index with ``CREATE INDEX
CONCURRENTLY``, which cannot run inside a transaction and waits for every
concurrent transaction to drain. A previous bug held ``pg_advisory_lock`` inside
an open transaction, so the lock-holder's own ``idle in transaction`` connection
deadlocked the concurrent index build — and waiters blocked on the lock piled on
more open transactions. The fix (``turnstone/core/storage/_migrate.py``) takes
the lock on an AUTOCOMMIT connection and polls ``pg_try_advisory_lock`` so no
waiter pins a snapshot.

PostgreSQL-only — skipped on the SQLite backend (no CONCURRENTLY, no advisory
lock path). Runs in CI's ``test-postgres`` job (``--storage-backend=postgresql``).
"""

from __future__ import annotations

import os
import threading
import uuid
from typing import Any

import pytest
import sqlalchemy as sa


def _pg_base_url() -> str:
    return os.environ.get(
        "TURNSTONE_TEST_PG_URL",
        "postgresql+psycopg://postgres:postgres@localhost:5432/turnstone_test",
    )


@pytest.fixture
def fresh_pg_url(request: pytest.FixtureRequest) -> Any:
    """Create a throwaway PostgreSQL database, yield its URL, drop it after.

    Skips unless the suite is running against PostgreSQL — migrations must run
    from scratch (so 041's CONCURRENTLY actually executes), which the shared
    ``turnstone_test`` schema can't provide.
    """
    if request.config.getoption("--storage-backend") != "postgresql":
        pytest.skip("PostgreSQL-only (advisory-lock / CREATE INDEX CONCURRENTLY path)")

    base = sa.make_url(_pg_base_url())
    db_name = f"ts_migtest_{uuid.uuid4().hex[:12]}"
    # CREATE/DROP DATABASE can't run in a transaction → AUTOCOMMIT admin engine.
    admin = sa.create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{db_name}"'))
        yield base.set(database=db_name)
    finally:
        with admin.connect() as conn:
            # Terminate any lingering backends before dropping.
            conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"
                ),
                {"d": db_name},
            )
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        admin.dispose()


class _EngineStub:
    """Minimal stand-in for a StorageBackend — run_migrations only reads _engine."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine


def test_concurrent_run_migrations_no_deadlock(fresh_pg_url: Any) -> None:
    from turnstone.core.storage._migrate import run_migrations

    n_workers = 4
    errors: list[BaseException] = []
    barrier = threading.Barrier(n_workers)

    def _worker() -> None:
        engine = sa.create_engine(fresh_pg_url)
        try:
            barrier.wait(timeout=30)  # release together → maximise overlap
            run_migrations(_EngineStub(engine), "postgresql")
        except BaseException as exc:  # noqa: BLE001 — capture for the assertion
            errors.append(exc)
        finally:
            engine.dispose()

    threads = [threading.Thread(target=_worker, name=f"migrate-{i}") for i in range(n_workers)]
    for t in threads:
        t.start()
    # A join timeout is essential: under the old bug these threads deadlock, and
    # we want a clean test failure, not a hung suite.
    for t in threads:
        t.join(timeout=60)

    stuck = [t.name for t in threads if t.is_alive()]
    assert not stuck, f"migration thread(s) deadlocked (alive after 60s): {stuck}"
    assert not errors, f"migration(s) raised: {errors!r}"

    # All workers converged on head, and migration 041's CONCURRENTLY rebuild ran
    # (partial parent index present, low-cardinality kind index dropped).
    engine = sa.create_engine(fresh_pg_url)
    try:
        with engine.connect() as conn:
            rev = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
            indexes = set(
                conn.execute(
                    sa.text("SELECT indexname FROM pg_indexes WHERE tablename = 'workstreams'")
                ).scalars()
            )
    finally:
        engine.dispose()

    assert rev is not None
    assert "idx_workstreams_parent" in indexes
    assert "idx_workstreams_kind" not in indexes
