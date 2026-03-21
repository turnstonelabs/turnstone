"""Programmatic Alembic migration runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from turnstone.core.log import get_logger

log = get_logger(__name__)

_MIGRATIONS_DIR = str(Path(__file__).parent / "migrations")


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

    Advisory lock ID 7_475_283 (arbitrary, derived from 'turnstone').
    ``pg_advisory_lock`` blocks until the lock is available, so
    concurrent containers wait in line rather than racing.

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
                conn.execute(sa.text("SELECT pg_advisory_lock(7475283)"))
                try:
                    command.upgrade(cfg, "head")
                finally:
                    conn.execute(sa.text("SELECT pg_advisory_unlock(7475283)"))
                    conn.commit()
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
