"""Programmatic Alembic migration runner."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_MIGRATIONS_DIR = str(Path(__file__).parent / "migrations")


def run_migrations(storage: Any, backend: str) -> None:
    """Run pending Alembic migrations.

    For SQLite backends, also handles bootstrapping existing databases
    that were created before the migration system existed.
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

    try:
        command.upgrade(cfg, "head")
    except Exception as exc:
        if backend == "sqlite":
            log.warning("Migration failed (non-fatal for SQLite): %s", exc)
        else:
            raise


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

        # Check if sessions table exists (indicates pre-existing database)
        has_sessions = conn.execute(
            sa.text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'")
        ).fetchone()
        if has_sessions:
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

    logging.basicConfig(level=logging.INFO)
    init_storage(backend, path=path, url=url, run_migrations=True)
    log.info("Migrations complete")
    get_storage().close()
