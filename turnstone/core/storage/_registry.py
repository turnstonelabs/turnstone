"""Storage backend singleton registry."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from turnstone.core.storage._protocol import StorageBackend

log = get_logger(__name__)

_storage: StorageBackend | None = None


class StorageUnavailableError(Exception):
    """Raised when the database is unreachable.

    The storage layer has already logged a clean one-liner — callers
    should catch this to avoid duplicate tracebacks.
    """


def init_storage(
    backend: str = "sqlite",
    *,
    path: str = "",
    url: str = "",
    pool_size: int = 2,
    run_migrations: bool = True,
    sslmode: str = "",
    sslrootcert: str = "",
    sslcert: str = "",
    sslkey: str = "",
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
        # Append SSL params to URL if provided (validated + encoded)
        valid_sslmodes = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
        if sslmode and sslmode not in valid_sslmodes:
            msg = f"Invalid sslmode: {sslmode!r} (expected one of {sorted(valid_sslmodes)})"
            raise ValueError(msg)
        ssl_params = {
            k: v
            for k, v in {
                "sslmode": sslmode,
                "sslrootcert": sslrootcert,
                "sslcert": sslcert,
                "sslkey": sslkey,
            }.items()
            if v
        }
        if ssl_params:
            from urllib.parse import urlencode

            sep = "&" if "?" in url else "?"
            url += sep + urlencode(ssl_params)
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
