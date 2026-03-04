"""Storage backend singleton registry."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from turnstone.core.storage._protocol import StorageBackend

log = logging.getLogger(__name__)

_storage: StorageBackend | None = None


def init_storage(
    backend: str = "sqlite",
    *,
    path: str = "",
    url: str = "",
    pool_size: int = 5,
    run_migrations: bool = True,
) -> StorageBackend:
    """Initialize the storage backend singleton.

    Args:
        backend: "sqlite" or "postgresql"
        path: SQLite database file path (default: .turnstone.db in cwd)
        url: PostgreSQL connection URL (e.g. postgresql+psycopg://user:pass@host/db)
        pool_size: Connection pool size (PostgreSQL only)
        run_migrations: Whether to run Alembic migrations on init
    """
    global _storage

    # When Alembic migrations will run, skip create_all() to avoid
    # bypassing migration-managed DDL.  Tests pass run_migrations=False
    # and rely on create_all() instead.
    create_tables = not run_migrations

    if backend == "sqlite":
        from turnstone.core.storage._sqlite import SQLiteBackend

        db_path = path or os.path.join(os.getcwd(), ".turnstone.db")
        _storage = SQLiteBackend(db_path, create_tables=create_tables)
        log.info("Storage initialized: SQLite at %s", db_path)

    elif backend == "postgresql":
        from turnstone.core.storage._postgresql import PostgreSQLBackend

        if not url:
            msg = "PostgreSQL backend requires a connection URL (db_url)"
            raise ValueError(msg)
        _storage = PostgreSQLBackend(url, pool_size=pool_size, create_tables=create_tables)
        log.info("Storage initialized: PostgreSQL")

    else:
        msg = f"Unknown storage backend: {backend!r} (expected 'sqlite' or 'postgresql')"
        raise ValueError(msg)

    if run_migrations:
        from turnstone.core.storage._migrate import run_migrations as _run_migrations

        _run_migrations(_storage, backend)

    return _storage


def get_storage() -> StorageBackend:
    """Return the initialized storage backend.

    Auto-initializes with SQLite defaults if not yet initialized.
    """
    global _storage
    if _storage is None:
        init_storage("sqlite")
    assert _storage is not None
    return _storage


def reset_storage() -> None:
    """Close and clear the storage backend singleton (for tests)."""
    global _storage
    if _storage is not None:
        _storage.close()
        _storage = None
