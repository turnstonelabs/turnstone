"""Schema parity: `metadata.create_all` must match `alembic upgrade head`.

The codebase defines its schema twice — `_schema.py` (the SQLAlchemy metadata
that `create_all` builds, used for fast ephemeral test DBs and
``SQLiteBackend(create_tables=True)``) and the Alembic migration chain (which
builds production DBs incrementally).  They are kept in sync BY HAND.

Nothing else enforces that they agree, so a column added to a migration but not
to `_schema.py` (or the reverse) would silently give `create_all`-based tests a
different schema than production — and most tests use `create_all`, so a
migration bug could pass CI unnoticed.  This test is that enforcement: it fails
the moment the two paths drift on a table, column, or named constraint.

(It does NOT check seed DATA: `create_all` builds structure only, so migration
seeds — e.g. the built-in personas — exist only on migrated DBs.  Tests that
need seed rows must run migrations or seed explicitly; that gap is by design.)
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

_MIGRATIONS = str(Path(__file__).resolve().parent.parent / "turnstone/core/storage/migrations")


def _inspect_migrated(db_path: Path) -> sa.Inspector:
    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    return sa.inspect(sa.create_engine(f"sqlite:///{db_path}"))


def _inspect_create_all(db_path: Path) -> sa.Inspector:
    from turnstone.core.storage._schema import metadata

    engine = sa.create_engine(f"sqlite:///{db_path}")
    metadata.create_all(engine)
    return sa.inspect(engine)


def test_create_all_matches_migrations(tmp_path: Path) -> None:
    mig = _inspect_migrated(tmp_path / "migrated.db")
    meta = _inspect_create_all(tmp_path / "create_all.db")

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


def test_personas_prompt_source_check_present_on_both_paths(tmp_path: Path) -> None:
    # Guards the personas feature specifically: the base_prompt/base_prompt_file
    # source CHECK must exist on BOTH build paths, not just the one under test.
    mig = _inspect_migrated(tmp_path / "m.db")
    meta = _inspect_create_all(tmp_path / "c.db")
    for insp in (mig, meta):
        names = {c.get("name") for c in insp.get_check_constraints("personas")}
        assert "ck_personas_prompt_source" in names
