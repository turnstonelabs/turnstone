"""Tests for alembic migration 061 (coordinator memories re-keyed to user_id).

Drives ``command.upgrade`` from a programmatic Alembic config against an
isolated SQLite database per test (the 060-test harness pattern), then
asserts:

* a ``scope='coordinator'`` row keyed by a live coordinator's ws_id is
  re-keyed to that workstream's owner ``user_id``;
* rows that cannot be attributed — scope_id matching no workstreams row, or
  matching one whose ``user_id`` is NULL/empty — are deleted (documented
  lossy step);
* name collisions that would violate ``uq_smem_name_scope`` after the re-key
  (same name, two coordinator sessions of the same user) keep only the
  newest ``updated`` row, with ``memory_id`` as the deterministic tiebreak;
* the same name under two DIFFERENT users' coordinators survives as two
  rows (one per user namespace);
* non-coordinator scopes are untouched, including a ``user``-scope row whose
  scope_id equals an owner user_id (the post-rekey value collision the
  scope column is meant to keep disjoint).
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


def _seed_ws(conn: sa.Connection, ws_id: str, user_id: object, kind: str = "coordinator") -> None:
    conn.execute(
        sa.text(
            "INSERT INTO workstreams (ws_id, user_id, kind, created, updated) "
            "VALUES (:ws_id, :user_id, :kind, '2026-06-01T00:00:00', '2026-06-01T00:00:00')"
        ),
        {"ws_id": ws_id, "user_id": user_id, "kind": kind},
    )


def _seed_memory(
    conn: sa.Connection,
    memory_id: str,
    name: str,
    scope: str,
    scope_id: str,
    updated: str = "2026-06-01T00:00:00",
) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO structured_memories "
            "(memory_id, name, scope, scope_id, content, created, updated) "
            "VALUES (:memory_id, :name, :scope, :scope_id, :content, :created, :updated)"
        ),
        {
            "memory_id": memory_id,
            "name": name,
            "scope": scope,
            "scope_id": scope_id,
            "content": f"content of {memory_id}",
            "created": "2026-06-01T00:00:00",
            "updated": updated,
        },
    )


def _all_memories(engine: sa.Engine) -> dict[str, tuple[str, str, str]]:
    """memory_id -> (name, scope, scope_id) for every surviving row."""
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT memory_id, name, scope, scope_id FROM structured_memories")
        ).fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


class TestMigration061:
    def test_rekeys_to_owner_user_id_and_leaves_other_scopes_alone(self, tmp_path: Path) -> None:
        db_path = tmp_path / "061-rekey.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "060")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_ws(conn, "coord-a", "user-1")
                _seed_memory(conn, "m1", "runbook", "coordinator", "coord-a")
                # Other scopes must pass through untouched — including a
                # user-scope row already keyed by the SAME user_id the
                # coordinator row is about to be re-keyed onto.
                _seed_memory(conn, "m2", "runbook", "user", "user-1")
                _seed_memory(conn, "m3", "runbook", "workstream", "coord-a")
                _seed_memory(conn, "m4", "runbook", "global", "")

            command.upgrade(cfg, "061")

            mems = _all_memories(engine)
            assert mems["m1"] == ("runbook", "coordinator", "user-1")
            assert mems["m2"] == ("runbook", "user", "user-1")
            assert mems["m3"] == ("runbook", "workstream", "coord-a")
            assert mems["m4"] == ("runbook", "global", "")
        finally:
            engine.dispose()

    def test_deletes_unattributable_rows(self, tmp_path: Path) -> None:
        db_path = tmp_path / "061-orphans.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "060")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_ws(conn, "coord-null", None)
                _seed_ws(conn, "coord-empty", "")
                _seed_memory(conn, "m-gone", "a", "coordinator", "no-such-ws")
                _seed_memory(conn, "m-null", "b", "coordinator", "coord-null")
                _seed_memory(conn, "m-empty", "c", "coordinator", "coord-empty")

            command.upgrade(cfg, "061")

            assert _all_memories(engine) == {}
        finally:
            engine.dispose()

    def test_dedups_same_user_collisions_keeping_newest(self, tmp_path: Path) -> None:
        db_path = tmp_path / "061-dedup.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "060")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_ws(conn, "coord-old", "user-1")
                _seed_ws(conn, "coord-new", "user-1")
                _seed_ws(conn, "coord-tie", "user-1")
                # Same name across two sessions of the same user — only
                # the newer ``updated`` survives.
                _seed_memory(
                    conn,
                    "m-old",
                    "plan",
                    "coordinator",
                    "coord-old",
                    updated="2026-06-01T00:00:00",
                )
                _seed_memory(
                    conn,
                    "m-new",
                    "plan",
                    "coordinator",
                    "coord-new",
                    updated="2026-06-02T00:00:00",
                )
                # Equal ``updated``: memory_id breaks the tie (max wins).
                _seed_memory(
                    conn,
                    "m-tie-a",
                    "pinned",
                    "coordinator",
                    "coord-new",
                    updated="2026-06-03T00:00:00",
                )
                _seed_memory(
                    conn,
                    "m-tie-b",
                    "pinned",
                    "coordinator",
                    "coord-tie",
                    updated="2026-06-03T00:00:00",
                )
                # Distinct names never collide — both survive.
                _seed_memory(conn, "m-keep", "notes", "coordinator", "coord-old")

            command.upgrade(cfg, "061")

            mems = _all_memories(engine)
            assert "m-old" not in mems
            assert mems["m-new"] == ("plan", "coordinator", "user-1")
            assert "m-tie-a" not in mems
            assert mems["m-tie-b"] == ("pinned", "coordinator", "user-1")
            assert mems["m-keep"] == ("notes", "coordinator", "user-1")
        finally:
            engine.dispose()

    def test_same_name_across_users_survives_as_two_rows(self, tmp_path: Path) -> None:
        db_path = tmp_path / "061-two-users.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "060")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_ws(conn, "coord-u1", "user-1")
                _seed_ws(conn, "coord-u2", "user-2")
                _seed_memory(conn, "m-u1", "plan", "coordinator", "coord-u1")
                _seed_memory(conn, "m-u2", "plan", "coordinator", "coord-u2")

            command.upgrade(cfg, "061")

            mems = _all_memories(engine)
            assert mems["m-u1"] == ("plan", "coordinator", "user-1")
            assert mems["m-u2"] == ("plan", "coordinator", "user-2")
        finally:
            engine.dispose()
