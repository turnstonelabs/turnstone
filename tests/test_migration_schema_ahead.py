"""Regression test for issue #591 (rolling git-installs vs. shared schema).

Reproduces the rolling-upgrade race: one node applies a migration the *other*
node's checkout has never heard of. Before this fix, ``run_migrations()``:

* on SQLite, swallowed Alembic's "Can't locate revision" failure as a
  non-fatal warning and returned normally — the node booted up silently
  attached to a schema its code didn't understand;
* on PostgreSQL, propagated Alembic's raw, cryptic ``CommandError`` instead
  of a clear, actionable one.

``run_migrations()`` now runs a pre-flight check (``_check_schema_not_ahead``)
that raises ``SchemaAheadOfCodeError`` before either backend path gets a
chance to run — and, on SQLite, before the broad non-fatal ``except`` can
swallow it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from turnstone.core.storage._migrate import SchemaAheadOfCodeError, run_migrations

_MIGRATIONS_DIR = str(
    Path(__file__).resolve().parent.parent / "turnstone" / "core" / "storage" / "migrations"
)


def _alembic_cfg(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS_DIR)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


class _EngineStub:
    """Minimal stand-in for a StorageBackend — run_migrations only reads _engine."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine


def test_run_migrations_rejects_unknown_future_revision(tmp_path: Path) -> None:
    """A node whose code is behind the schema must fail fast, not silently boot."""
    db_path = tmp_path / "ahead.db"
    command.upgrade(_alembic_cfg(db_path), "head")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        # Simulate node1 having already applied a migration this (older)
        # checkout's script directory doesn't contain.
        with engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE alembic_version SET version_num = 'FUTURE_MIGRATION_UNKNOWN'")
            )

        try:
            run_migrations(_EngineStub(engine), "sqlite")
        except SchemaAheadOfCodeError as exc:
            assert "FUTURE_MIGRATION_UNKNOWN" in str(exc)
        else:
            raise AssertionError(
                "run_migrations() silently succeeded against a schema ahead of "
                "this checkout's migrations — issue #591 regression"
            )
    finally:
        engine.dispose()


def test_run_migrations_succeeds_on_compatible_schema(tmp_path: Path) -> None:
    """Sanity check: a DB at a known (mid-upgrade) revision migrates normally."""
    db_path = tmp_path / "compatible.db"
    command.upgrade(_alembic_cfg(db_path), "064")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        run_migrations(_EngineStub(engine), "sqlite")
        with engine.connect() as conn:
            rev = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
        assert rev is not None
    finally:
        engine.dispose()


def test_run_migrations_succeeds_on_fresh_database(tmp_path: Path) -> None:
    """Sanity check: a brand-new DB (no alembic_version yet) migrates normally."""
    db_path = tmp_path / "fresh.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        run_migrations(_EngineStub(engine), "sqlite")
        with engine.connect() as conn:
            rev = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
        assert rev is not None
    finally:
        engine.dispose()
