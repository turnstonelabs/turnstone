"""Tests for alembic migration 062 (Projects: containers + type project→general rename).

Drives ``command.upgrade``/``downgrade`` against an isolated SQLite database per test
(the 060/061 harness pattern), then asserts:

* the ``projects`` + ``project_members`` tables and ``workstreams.project_id`` are created;
* ``structured_memories`` rows with ``type='project'`` are relabelled ``'general'`` while
  other types pass through untouched;
* ``project.{create,read,write}`` are appended to the ``builtin-admin`` role;
* ``downgrade`` drops the schema, removes the perms, and relabels ``'general'`` → ``'project'``.
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


def _seed_memory(
    conn: sa.Connection,
    memory_id: str,
    name: str,
    mem_type: str,
    scope: str = "user",
    scope_id: str = "u1",
) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO structured_memories "
            "(memory_id, name, type, scope, scope_id, content, created, updated) "
            "VALUES (:id, :name, :type, :scope, :sid, 'c', "
            "'2026-06-01T00:00:00', '2026-06-01T00:00:00')"
        ),
        {"id": memory_id, "name": name, "type": mem_type, "scope": scope, "sid": scope_id},
    )


def _admin_perms(engine: sa.Engine) -> str:
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT permissions FROM roles WHERE role_id = 'builtin-admin'")
        ).fetchone()
    return str(row[0]) if row else ""


class TestMigration062:
    def test_creates_projects_schema(self, tmp_path: Path) -> None:
        db_path = tmp_path / "062-schema.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "062")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            insp = sa.inspect(engine)
            assert {"projects", "project_members"} <= set(insp.get_table_names())
            proj_cols = {c["name"] for c in insp.get_columns("projects")}
            assert {
                "project_id",
                "name",
                "owner_id",
                "visibility",
                "state",
                "parent_project_id",
                "created",
                "updated",
            } <= proj_cols
            member_cols = {c["name"] for c in insp.get_columns("project_members")}
            assert {"project_id", "user_id", "created"} <= member_cols
            assert "project_id" in {c["name"] for c in insp.get_columns("workstreams")}
        finally:
            engine.dispose()

    def test_renames_type_project_to_general(self, tmp_path: Path) -> None:
        db_path = tmp_path / "062-type.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "061")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_memory(conn, "m-proj", "a", "project")
                _seed_memory(conn, "m-feed", "b", "feedback")
                _seed_memory(conn, "m-user", "c", "user")

            command.upgrade(cfg, "062")

            with engine.connect() as conn:
                rows = {
                    str(r[0]): str(r[1])
                    for r in conn.execute(
                        sa.text("SELECT memory_id, type FROM structured_memories")
                    ).fetchall()
                }
            assert rows["m-proj"] == "general"
            assert rows["m-feed"] == "feedback"
            assert rows["m-user"] == "user"
        finally:
            engine.dispose()

    def test_grants_project_perms_to_admin(self, tmp_path: Path) -> None:
        db_path = tmp_path / "062-perms.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "062")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            perms = _admin_perms(engine)
            for perm in (
                "project.create",
                "project.read",
                "project.write",
                "project.delete",
            ):
                assert perm in perms
        finally:
            engine.dispose()

    def test_downgrade_reverses_everything(self, tmp_path: Path) -> None:
        db_path = tmp_path / "062-down.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "062")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_memory(conn, "m-gen", "a", "general")

            command.downgrade(cfg, "061")

            insp = sa.inspect(engine)
            tables = set(insp.get_table_names())
            assert "projects" not in tables
            assert "project_members" not in tables
            assert "project_id" not in {c["name"] for c in insp.get_columns("workstreams")}
            assert "project.create" not in _admin_perms(engine)
            with engine.connect() as conn:
                row = conn.execute(
                    sa.text("SELECT type FROM structured_memories WHERE memory_id = 'm-gen'")
                ).fetchone()
            assert row is not None and row[0] == "project"
        finally:
            engine.dispose()
