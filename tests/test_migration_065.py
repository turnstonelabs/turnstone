"""Tests for alembic migration 065 (capture Entra oid/tid on oidc_identities).

Drives ``command.upgrade``/``downgrade`` against an isolated SQLite database per
test (the 060/062/063 harness pattern), then asserts:

* upgrade adds the ``oid``/``tid`` columns and the ``idx_oidc_identities_oid``
  index;
* a pre-065 row migrates cleanly, gaining ``""`` for the new columns;
* downgrade removes the columns + index, returning ``oidc_identities`` to its
  exact pre-065 shape — this pins the **clean-rollback** guarantee (the change
  can be backed out with no orphaned state if the upstream PR is rejected).
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

_MIGRATIONS_DIR = str(
    Path(__file__).resolve().parent.parent / "turnstone" / "core" / "storage" / "migrations"
)


def _alembic_cfg(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS_DIR)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


class TestMigration065:
    def test_upgrade_adds_oid_tid_and_index(self, tmp_path: Path) -> None:
        db_path = tmp_path / "065-up.db"
        command.upgrade(_alembic_cfg(db_path), "065")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            insp = sa.inspect(engine)
            cols = {c["name"] for c in insp.get_columns("oidc_identities")}
            assert {"oid", "tid"} <= cols
            idx = {i["name"] for i in insp.get_indexes("oidc_identities")}
            assert "idx_oidc_identities_oid" in idx
        finally:
            engine.dispose()

    def test_preexisting_row_migrates_with_empty_default(self, tmp_path: Path) -> None:
        db_path = tmp_path / "065-default.db"
        cfg = _alembic_cfg(db_path)
        # Stop at 064, insert a pre-065 identity, THEN upgrade to 065.
        command.upgrade(cfg, "064")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "INSERT INTO oidc_identities "
                        "(issuer, subject, user_id, email, created, last_login) "
                        "VALUES ('iss', 'sub', 'u1', '', "
                        "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
                    )
                )
            command.upgrade(cfg, "065")
            with engine.connect() as conn:
                row = conn.execute(
                    sa.text("SELECT oid, tid FROM oidc_identities WHERE subject = 'sub'")
                ).fetchone()
            assert row is not None
            assert row[0] == "" and row[1] == ""
        finally:
            engine.dispose()

    def test_downgrade_removes_oid_tid_and_index(self, tmp_path: Path) -> None:
        db_path = tmp_path / "065-down.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "065")
        command.downgrade(cfg, "064")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            insp = sa.inspect(engine)
            cols = {c["name"] for c in insp.get_columns("oidc_identities")}
            assert "oid" not in cols and "tid" not in cols
            idx = {i["name"] for i in insp.get_indexes("oidc_identities")}
            assert "idx_oidc_identities_oid" not in idx
        finally:
            engine.dispose()

    def test_downgrade_then_upgrade_round_trip(self, tmp_path: Path) -> None:
        """up -> down -> up must land cleanly (no leftover column/index conflict)."""
        db_path = tmp_path / "065-roundtrip.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "065")
        command.downgrade(cfg, "064")
        command.upgrade(cfg, "065")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            cols = {c["name"] for c in sa.inspect(engine).get_columns("oidc_identities")}
            assert {"oid", "tid"} <= cols
        finally:
            engine.dispose()
