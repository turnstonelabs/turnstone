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

import threading
from typing import Any

import sqlalchemy as sa


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
