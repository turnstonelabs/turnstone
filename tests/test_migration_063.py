"""Tests for alembic migration 063 (Personas: template shelf + seeds + perms).

Drives ``command.upgrade``/``downgrade`` against an isolated SQLite database per
test (the 060/062 harness pattern), then asserts:

* the ``personas`` table and ``workstreams.persona`` column are created;
* the six seed personas land with the locked lever matrix — ``engineer`` /
  ``orchestrator`` as per-kind defaults with NULL prompt + NULL allowlist (the
  byte-identical zero-touch guarantee), the other four with their restricted
  envelopes;
* ``persona.{create,read,write}`` are appended to ``builtin-admin`` (and no
  ``persona.delete`` exists — archive only);
* ``downgrade`` drops the schema and removes the perms.
"""

from __future__ import annotations

import json
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


def _admin_perms(engine: sa.Engine) -> str:
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT permissions FROM roles WHERE role_id = 'builtin-admin'")
        ).fetchone()
    return str(row[0]) if row else ""


def _personas_by_name(engine: sa.Engine) -> dict[str, dict]:
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT * FROM personas")).fetchall()
    return {str(r._mapping["name"]): dict(r._mapping) for r in rows}


class TestMigration063:
    def test_creates_personas_schema(self, tmp_path: Path) -> None:
        db_path = tmp_path / "063-schema.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "063")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            insp = sa.inspect(engine)
            assert "personas" in insp.get_table_names()
            cols = {c["name"] for c in insp.get_columns("personas")}
            assert {
                "persona_id",
                "name",
                "display_name",
                "description",
                "base_prompt",
                "tool_allowlist",
                "mcp_enabled",
                "memory_enabled",
                "applies_to_kinds",
                "is_default",
                "enabled",
                "org_id",
                "created_by",
                "created",
                "updated",
            } <= cols
            assert "persona" in {c["name"] for c in insp.get_columns("workstreams")}
        finally:
            engine.dispose()

    def test_seeds_six_personas_with_locked_matrix(self, tmp_path: Path) -> None:
        db_path = tmp_path / "063-seeds.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "063")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            rows = _personas_by_name(engine)
            assert set(rows) == {
                "scribe",
                "researcher",
                "writer",
                "engineer",
                "orchestrator",
                "executive",
            }
            # Zero-touch guarantee: the per-kind defaults carry NO overrides.
            for name, kind in (("engineer", "interactive"), ("orchestrator", "coordinator")):
                p = rows[name]
                assert p["base_prompt"] is None
                assert p["tool_allowlist"] is None
                assert p["mcp_enabled"] == 1
                assert p["memory_enabled"] == 1
                assert p["is_default"] == 1
                assert json.loads(p["applies_to_kinds"]) == [kind]
            # Restricted envelopes.
            assert json.loads(rows["scribe"]["tool_allowlist"]) == []
            assert rows["scribe"]["mcp_enabled"] == 0
            assert rows["scribe"]["memory_enabled"] == 0
            assert json.loads(rows["researcher"]["tool_allowlist"]) == [
                "read_file",
                "search",
                "web_fetch",
                "web_search",
                "recall",
                "memory",
            ]
            assert json.loads(rows["writer"]["tool_allowlist"]) == []
            assert rows["writer"]["memory_enabled"] == 1
            exec_tools = json.loads(rows["executive"]["tool_allowlist"])
            assert "spawn_workstream" in exec_tools
            assert "delete_workstream" not in exec_tools
            assert "tool_search" not in exec_tools  # hard set — no escape hatch
            assert json.loads(rows["executive"]["applies_to_kinds"]) == ["coordinator"]
            # /creative parity: writer folds the old prompt.
            assert "creative writing partner" in str(rows["writer"]["base_prompt"])
            # All seeds enabled.
            assert all(p["enabled"] == 1 for p in rows.values())
        finally:
            engine.dispose()

    def test_grants_persona_perms_to_admin(self, tmp_path: Path) -> None:
        db_path = tmp_path / "063-perms.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "063")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            perms = _admin_perms(engine)
            for perm in ("persona.create", "persona.read", "persona.write"):
                assert perm in perms
            assert "persona.delete" not in perms  # archive only — no delete verb
        finally:
            engine.dispose()

    def test_downgrade_reverses_everything(self, tmp_path: Path) -> None:
        db_path = tmp_path / "063-down.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "063")
        command.downgrade(cfg, "062")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            insp = sa.inspect(engine)
            assert "personas" not in insp.get_table_names()
            assert "persona" not in {c["name"] for c in insp.get_columns("workstreams")}
            assert "persona." not in _admin_perms(engine)
        finally:
            engine.dispose()
