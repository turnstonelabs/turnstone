"""Schema parity: `metadata.create_all` must match `alembic upgrade head`.

The codebase defines its schema twice — `_schema.py` (the SQLAlchemy metadata
that `create_all` builds, used for fast ephemeral test DBs and
``SQLiteBackend(create_tables=True)``) and the Alembic migration chain (which
builds production DBs incrementally).  They are kept in sync BY HAND.

Nothing else enforces that they agree, so a column added to a migration but not
to `_schema.py` (or the reverse) would silently give `create_all`-based tests a
different schema than production — and most tests use `create_all`, so a
migration bug could pass CI unnoticed.  This test is that enforcement: it fails
the moment the two paths drift on a table, column, named constraint, or selected
named index definition. Index coverage is opt-in because partial and
dialect-specific indexes require table-specific normalization.

(It does NOT check seed DATA: `create_all` builds structure only, so migration
seeds — e.g. the built-in personas — exist only on migrated DBs.  Tests that
need seed rows must run migrations or seed explicitly; that gap is by design.)
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

if TYPE_CHECKING:
    from collections.abc import Iterator

_MIGRATIONS = str(Path(__file__).resolve().parent.parent / "turnstone/core/storage/migrations")
_STRUCTURED_MEMORY_INDEXES = {
    "idx_smem_scope": ("scope", "scope_id"),
    "idx_smem_type": ("type",),
}


def _structured_memory_indexes(inspector: sa.Inspector) -> dict[str, tuple[str, ...]]:
    indexes: dict[str, tuple[str, ...]] = {}
    for index in inspector.get_indexes("structured_memories"):
        name = index.get("name")
        if name is None or not name.startswith("idx_smem_"):
            continue
        columns = index.get("column_names") or []
        if any(column is None for column in columns):
            raise AssertionError(f"expression-based structured-memory index: {index}")
        indexes[name] = tuple(column for column in columns if column is not None)
    return indexes


@pytest.fixture
def sqlite_inspectors(tmp_path: Path) -> Iterator[tuple[sa.Inspector, sa.Inspector]]:
    """Inspect both schema paths while owning their connection pools."""
    from turnstone.core.storage._schema import metadata

    migrated = sa.create_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    create_all = sa.create_engine(f"sqlite:///{tmp_path / 'create_all.db'}")
    try:
        cfg = Config()
        cfg.set_main_option("script_location", _MIGRATIONS)
        cfg.set_main_option("sqlalchemy.url", str(migrated.url))
        command.upgrade(cfg, "head")
        metadata.create_all(create_all)
        yield sa.inspect(migrated), sa.inspect(create_all)
    finally:
        create_all.dispose()
        migrated.dispose()


def test_create_all_matches_migrations(
    sqlite_inspectors: tuple[sa.Inspector, sa.Inspector],
) -> None:
    mig, meta = sqlite_inspectors

    mig_tables = set(mig.get_table_names()) - {"alembic_version"}
    meta_tables = set(meta.get_table_names())
    assert mig_tables == meta_tables, (
        f"table drift — only in migrations: {sorted(mig_tables - meta_tables)}; "
        f"only in create_all: {sorted(meta_tables - mig_tables)}"
    )

    col_drift: dict[str, dict[str, list[str]]] = {}
    check_drift: dict[str, dict[str, list[str]]] = {}
    for t in sorted(mig_tables):
        mc = {c["name"] for c in mig.get_columns(t)}
        ec = {c["name"] for c in meta.get_columns(t)}
        if mc != ec:
            col_drift[t] = {
                "only_migrations": sorted(mc - ec),
                "only_create_all": sorted(ec - mc),
            }
        # Named CHECK constraints only — unnamed ones reflect as backend noise.
        mck = {c["name"] for c in mig.get_check_constraints(t) if c.get("name")}
        eck = {c["name"] for c in meta.get_check_constraints(t) if c.get("name")}
        if mck != eck:
            check_drift[t] = {
                "only_migrations": sorted(mck - eck),
                "only_create_all": sorted(eck - mck),
            }

    assert not col_drift, f"column drift: {col_drift}"
    assert not check_drift, f"check-constraint drift: {check_drift}"
    assert _structured_memory_indexes(mig) == _STRUCTURED_MEMORY_INDEXES
    assert _structured_memory_indexes(meta) == _STRUCTURED_MEMORY_INDEXES


def test_postgresql_structured_memory_indexes_match_both_paths(
    fresh_pg_url: sa.URL,
) -> None:
    from turnstone.core.storage._schema import metadata

    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS)
    cfg.set_main_option("sqlalchemy.url", fresh_pg_url.render_as_string(hide_password=False))
    command.upgrade(cfg, "head")

    engine = sa.create_engine(fresh_pg_url)
    try:
        migrated_indexes = _structured_memory_indexes(sa.inspect(engine))

        # The database is fixture-owned and disposable. Rebuild it through the
        # second schema path so PostgreSQL reflection covers both definitions.
        metadata.drop_all(engine)
        metadata.create_all(engine)
        create_all_indexes = _structured_memory_indexes(sa.inspect(engine))
    finally:
        engine.dispose()

    assert migrated_indexes == _STRUCTURED_MEMORY_INDEXES
    assert create_all_indexes == _STRUCTURED_MEMORY_INDEXES


def test_personas_prompt_source_check_present_on_both_paths(
    sqlite_inspectors: tuple[sa.Inspector, sa.Inspector],
) -> None:
    # Guards the personas feature specifically: the base_prompt/base_prompt_file
    # source CHECK must exist on BOTH build paths, not just the one under test.
    for insp in sqlite_inspectors:
        names = {c.get("name") for c in insp.get_check_constraints("personas")}
        assert "ck_personas_prompt_source" in names
