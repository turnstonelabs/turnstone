"""Migration 081: the orphan reaper's partial index covers every state it closes.

A partial index serves the reaper only while the reaper's ``state IN (...)``
implies the index's condition, so the two lists must match.  These read the
index the migrations actually build, so a state added to
``BULK_CLOSE_STATE_VALUES`` without a migration fails here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from turnstone.core.workstream import BULK_CLOSE_STATE_VALUES

_FOUR_STATES = {"idle", "thinking", "attention", "running"}
_FIVE_STATES = _FOUR_STATES | {"evaluation"}


def _config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[1] / "turnstone/core/storage/migrations"),
    )
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def _reaper_index_states(url: str) -> set[str]:
    """The state literals in the built index's WHERE, on either dialect."""
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            if engine.dialect.name == "postgresql":
                valid, sql = conn.execute(
                    sa.text(
                        "SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) "
                        "FROM pg_class AS c JOIN pg_index AS i ON i.indexrelid = c.oid "
                        "WHERE c.relname = 'idx_workstreams_reaper'"
                    )
                ).one()
                assert valid, "the concurrent build left an INVALID index"
            else:
                sql = conn.execute(
                    sa.text(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type = 'index' AND name = 'idx_workstreams_reaper'"
                    )
                ).scalar_one()
    finally:
        engine.dispose()
    return set(re.findall(r"'([a-z_]+)'", sql.split("WHERE", 1)[1]))


def _roundtrip(url: str) -> None:
    """081 up, down to 080, up again: the second upgrade proves the
    drop-then-create runs over an index 080's state already holds."""
    cfg = _config(url)
    command.upgrade(cfg, "081")
    assert _reaper_index_states(url) == _FIVE_STATES
    command.downgrade(cfg, "080")
    assert _reaper_index_states(url) == _FOUR_STATES
    command.upgrade(cfg, "081")
    assert _reaper_index_states(url) == _FIVE_STATES


def test_reaper_index_migration_sqlite(tmp_path: Path) -> None:
    _roundtrip(f"sqlite:///{tmp_path / 'reaper.db'}")


def test_reaper_index_migration_postgresql(fresh_pg_url: Any) -> None:
    _roundtrip(fresh_pg_url.render_as_string(hide_password=False))


def test_reaper_index_covers_every_state_the_reaper_closes(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'head.db'}"
    command.upgrade(_config(url), "head")
    assert _reaper_index_states(url) == set(BULK_CLOSE_STATE_VALUES)
