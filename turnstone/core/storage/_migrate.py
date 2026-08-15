"""Programmatic Alembic migration runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from turnstone.core.log import get_logger

log = get_logger(__name__)

_MIGRATIONS_DIR = str(Path(__file__).parent / "migrations")


class SchemaAheadOfCodeError(RuntimeError):
    """Raised when the database's Alembic revision is unknown to this checkout.

    On a rolling git-pull upgrade, a node that has already applied a newer
    migration leaves ``alembic_version`` pointing at a revision this (older,
    not-yet-upgraded) node's migration scripts have never heard of. Left
    unchecked, Alembic fails deep inside dependency resolution with an opaque
    "Can't locate revision" error — or, on SQLite, that error gets logged as a
    non-fatal warning and the node boots up silently attached to a schema its
    code doesn't understand. See issue #591.
    """


def _check_schema_not_ahead(engine: Any, cfg: Any) -> None:
    """Refuse to proceed if the DB's current revision is unknown to this checkout."""
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(cfg)
    known_revisions = {rev.revision for rev in script.walk_revisions()}

    with engine.connect() as conn:
        current_rev = MigrationContext.configure(conn).get_current_revision()

    if current_rev is not None and current_rev not in known_revisions:
        msg = (
            f"Database schema is at revision {current_rev!r}, which this "
            "installation's migrations do not recognize. Another node in the "
            "cluster has likely already applied a newer migration than this "
            "node's code knows about (a rolling git-pull upgrade applied out "
            "of order). Upgrade this node's code to match before starting it "
            "against this database."
        )
        raise SchemaAheadOfCodeError(msg)


def run_migrations(storage: Any, backend: str) -> None:
    """Run pending Alembic migrations.

    For SQLite backends, also handles bootstrapping existing databases
    that were created before the migration system existed.

    For PostgreSQL, acquires an advisory lock so only one process runs
    migrations at a time (multiple containers share the same database).
    """
    from alembic import command
    from alembic.config import Config

    engine = storage._engine  # noqa: SLF001

    # Build Alembic config programmatically (no alembic.ini needed)
    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS_DIR)
    cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))

    # Check if this is an existing database without alembic_version
    if backend == "sqlite":
        _bootstrap_existing_sqlite(engine, cfg)

    # Fatal regardless of backend, and deliberately outside the try/excepts
    # below — those exist to swallow transient/connection-class failures, and
    # must not also swallow "this node's code is behind the schema."
    _check_schema_not_ahead(engine, cfg)

    if backend == "postgresql":
        try:
            _run_with_pg_lock(engine, cfg)
        except (OSError, EOFError) as exc:
            # Non-fatal for connection-class errors only (refused, reset,
            # timeout).  DDL / migration errors still propagate.  The Docker
            # entrypoint already runs migrations before the server starts, so
            # this second attempt is a safety net for stampede scenarios.
            log.warning("PostgreSQL migration failed (non-fatal): %s", exc)
    else:
        try:
            command.upgrade(cfg, "head")
        except Exception as exc:
            log.warning("Migration failed (non-fatal for SQLite): %s", exc)


def _run_with_pg_lock(engine: Any, cfg: Any) -> None:
    """Run Alembic upgrade under a PostgreSQL advisory lock.

    Advisory lock ID 7_475_283 (arbitrary, derived from 'turnstone') serializes
    concurrent containers so only one applies migrations at a time.

    The lock is held on an **autocommit** connection and acquired by *polling*
    ``pg_try_advisory_lock`` rather than the blocking ``pg_advisory_lock``. Both
    details are load-bearing. A migration that rebuilds an index with ``CREATE
    INDEX CONCURRENTLY`` (migration 041) waits for every concurrent transaction
    to drain before it can finish. If the lock-holder held the lock inside an
    open transaction — or waiters blocked on ``pg_advisory_lock`` inside one —
    those connections sit ``idle in transaction`` and never drain, so the
    concurrent build deadlocks against the very lock meant to protect it.
    Autocommit keeps the holder transaction-free; polling (with a sleep that
    holds no snapshot) keeps waiters transaction-free between attempts.

    Retries with jittered backoff if PostgreSQL is temporarily at
    max_connections (common during large-cluster startup stampedes).
    """
    import random
    import time

    import sqlalchemy as sa
    from alembic import command

    max_retries = 10
    for attempt in range(max_retries):
        try:
            with engine.connect() as conn:
                conn = conn.execution_options(isolation_level="AUTOCOMMIT")
                # Poll, don't block: a waiter blocked inside pg_advisory_lock
                # would pin a snapshot that CREATE INDEX CONCURRENTLY waits on.
                while not conn.execute(sa.text("SELECT pg_try_advisory_lock(7475283)")).scalar():
                    time.sleep(random.uniform(0.5, 1.5))  # noqa: S311
                try:
                    command.upgrade(cfg, "head")
                finally:
                    conn.execute(sa.text("SELECT pg_advisory_unlock(7475283)"))
            return
        except Exception as exc:
            err_str = str(exc).lower()
            if "too many clients" not in err_str and "connection" not in err_str:
                raise
            if attempt == max_retries - 1:
                raise
            delay = min(2**attempt + random.uniform(0, 1), 30)  # noqa: S311
            log.warning(
                "PG connection failed (attempt %d/%d), retrying in %.1fs: %s",
                attempt + 1,
                max_retries,
                delay,
                exc,
            )
            time.sleep(delay)


def _bootstrap_existing_sqlite(engine: Any, cfg: Any) -> None:
    """Stamp existing SQLite databases at the baseline revision.

    If the database has tables but no alembic_version, it was created
    before the migration system. Stamp it so Alembic knows the schema
    is already at the baseline.
    """
    import sqlalchemy as sa
    from alembic import command

    with engine.connect() as conn:
        # Check if alembic_version table exists
        has_alembic = conn.execute(
            sa.text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'")
        ).fetchone()
        if has_alembic:
            return  # Already managed by Alembic

        # Check if a known table exists (indicates pre-existing database)
        has_tables = conn.execute(
            sa.text(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name IN ('sessions', 'workstreams')"
            )
        ).fetchone()
        if has_tables:
            log.info("Bootstrapping existing database into Alembic (stamping at baseline)")
            command.stamp(cfg, "001")


if __name__ == "__main__":
    # Allow running as: python -m turnstone.core.storage._migrate
    # Used by Docker entrypoint to apply migrations before starting services.
    import os

    from turnstone.core.storage import get_storage, init_storage

    backend = os.environ.get("TURNSTONE_DB_BACKEND", "sqlite")
    url = os.environ.get("TURNSTONE_DB_URL", "")
    path = os.environ.get("TURNSTONE_DB_PATH", "")

    from turnstone.core.log import configure_logging

    configure_logging(level="INFO", service="migrate")
    init_storage(backend, path=path, url=url, run_migrations=True)
    log.info("Migrations complete")
    get_storage().close()
