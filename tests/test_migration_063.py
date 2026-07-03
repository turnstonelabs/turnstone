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
            # Every built-in is file-backed: base_prompt NULL, prose in
            # prompts/personas/<slug>.md (the origin marker + built-in flag).
            for name in rows:
                assert rows[name]["base_prompt"] is None, name
                assert rows[name]["base_prompt_file"] == f"{name}.md", name
            # Zero-touch guarantee: the per-kind defaults carry no lever overrides.
            for name, kind in (("engineer", "interactive"), ("orchestrator", "coordinator")):
                p = rows[name]
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
                "tool_search",
            ]
            assert json.loads(rows["writer"]["tool_allowlist"]) == []
            assert rows["writer"]["memory_enabled"] == 1
            exec_tools = json.loads(rows["executive"]["tool_allowlist"])
            assert "spawn_workstream" in exec_tools
            assert "delete_workstream" not in exec_tools
            assert "tool_search" not in exec_tools  # hard set — no escape hatch
            assert json.loads(rows["executive"]["applies_to_kinds"]) == ["coordinator"]
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

    def test_converts_legacy_creative_workstreams_to_writer(self, tmp_path: Path) -> None:
        db_path = tmp_path / "063-creative.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "062")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                for ws_id, mode in (("ws-creative", "True"), ("ws-plain", "False")):
                    conn.execute(
                        sa.text(
                            "INSERT INTO workstreams (ws_id, name, state, created, updated) "
                            "VALUES (:ws, :ws, 'closed', '2026-01-01T00:00:00', "
                            "'2026-01-01T00:00:00')"
                        ),
                        {"ws": ws_id},
                    )
                    conn.execute(
                        sa.text(
                            "INSERT INTO workstream_config (ws_id, key, value) "
                            "VALUES (:ws, 'creative_mode', :mode)"
                        ),
                        {"ws": ws_id, "mode": mode},
                    )

            command.upgrade(cfg, "063")

            with engine.connect() as conn:
                stamped = {
                    str(r[0]): str(r[1])
                    for r in conn.execute(
                        sa.text("SELECT ws_id, value FROM workstream_config WHERE key='persona'")
                    ).fetchall()
                }
                cols = conn.execute(
                    sa.text(
                        "SELECT key, value FROM workstream_config "
                        "WHERE ws_id='ws-creative' AND key LIKE 'persona%'"
                    )
                ).fetchall()
                row_persona = conn.execute(
                    sa.text("SELECT persona FROM workstreams WHERE ws_id='ws-creative'")
                ).fetchone()
            # creative_mode='True' → the full writer stamp (all five keys), the
            # persona_prompt frozen from prompts/personas/writer.md…
            assert stamped["ws-creative"] == "writer"
            keys = {str(k): str(v) for k, v in cols}
            assert keys["persona_tools"] == "[]"
            assert keys["persona_mcp"] == "0"
            assert keys["persona_memory"] == "1"
            assert "creative writing partner" in keys["persona_prompt"]
            assert row_persona is not None and row_persona[0] == "writer"
            # …while a non-creative workstream gets its kind default (engineer),
            # so no workstream is left personaless.
            assert stamped["ws-plain"] == "engineer"
        finally:
            engine.dispose()

    def test_backfill_stamps_plain_workstreams_by_kind(self, tmp_path: Path) -> None:
        # The load-bearing new behaviour: no workstream is left personaless.
        # A plain (non-creative) workstream is stamped with its kind's default —
        # engineer for interactive, orchestrator for coordinator — carrying that
        # persona's resolved (frozen) base prompt.
        db_path = tmp_path / "063-backfill.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "062")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                for ws_id, kind in (("ws-ic", "interactive"), ("ws-coord", "coordinator")):
                    conn.execute(
                        sa.text(
                            "INSERT INTO workstreams (ws_id, name, state, kind, created, "
                            "updated) VALUES (:ws, :ws, 'closed', :kind, "
                            "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
                        ),
                        {"ws": ws_id, "kind": kind},
                    )
            command.upgrade(cfg, "063")

            with engine.connect() as conn:

                def _cfg(ws: str, key: str) -> str | None:
                    r = conn.execute(
                        sa.text("SELECT value FROM workstream_config WHERE ws_id=:ws AND key=:k"),
                        {"ws": ws, "k": key},
                    ).fetchone()
                    return None if r is None else str(r[0])

                assert _cfg("ws-ic", "persona") == "engineer"
                assert _cfg("ws-coord", "persona") == "orchestrator"
                # Frozen resolved text (from the persona's file), not a slug/empty.
                assert "software engineer" in (_cfg("ws-ic", "persona_prompt") or "")
                assert "coordinator" in (_cfg("ws-coord", "persona_prompt") or "")
                # Kind-default envelope: unrestricted tools, MCP + memory on.
                assert _cfg("ws-ic", "persona_tools") == "null"
                assert _cfg("ws-ic", "persona_mcp") == "1"
                assert _cfg("ws-ic", "persona_memory") == "1"
                # The workstreams.persona projection is set too.
                row = conn.execute(
                    sa.text("SELECT persona FROM workstreams WHERE ws_id='ws-coord'")
                ).fetchone()
                assert row is not None and row[0] == "orchestrator"
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

    def test_downgrade_purges_persona_config_keeps_creative_mode(self, tmp_path: Path) -> None:
        # The downgrade's load-bearing contract (its own docstring): strip every
        # persona* stamp the upgrade synthesized from a creative workstream, but
        # leave creative_mode='True' intact so pre-063 code resumes it as
        # creative again.
        db_path = tmp_path / "063-down-creative.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "062")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "INSERT INTO workstreams (ws_id, name, state, created, updated) "
                        "VALUES ('ws-creative', 'ws-creative', 'closed', "
                        "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
                    )
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO workstream_config (ws_id, key, value) "
                        "VALUES ('ws-creative', 'creative_mode', 'True')"
                    )
                )

            command.upgrade(cfg, "063")
            # Sanity: the upgrade actually stamped the five persona keys — else
            # the downgrade assertion below would pass vacuously.
            with engine.connect() as conn:
                stamped = {
                    str(r[0])
                    for r in conn.execute(
                        sa.text("SELECT key FROM workstream_config WHERE ws_id='ws-creative'")
                    ).fetchall()
                }
            assert {
                "persona",
                "persona_prompt",
                "persona_tools",
                "persona_mcp",
                "persona_memory",
            } <= stamped

            command.downgrade(cfg, "062")
            with engine.connect() as conn:
                keys = [
                    str(r[0])
                    for r in conn.execute(
                        sa.text("SELECT key FROM workstream_config WHERE ws_id='ws-creative'")
                    ).fetchall()
                ]
                creative = conn.execute(
                    sa.text(
                        "SELECT value FROM workstream_config "
                        "WHERE ws_id='ws-creative' AND key='creative_mode'"
                    )
                ).fetchone()
            # Every persona* key is gone…
            assert not any(k.startswith("persona") for k in keys)
            # …while creative_mode='True' survives the round-trip.
            assert creative is not None and str(creative[0]) == "True"
        finally:
            engine.dispose()

    def test_conversion_skips_workstream_with_existing_persona_key(self, tmp_path: Path) -> None:
        # Idempotency guard (063 ~297-324): the conversion SELECT excludes any
        # ws that already carries a persona key (NOT IN sub-select).  A ws with
        # BOTH creative_mode='True' AND a pre-existing persona stamp must upgrade
        # without a PK collision on workstream_config(ws_id, key), leave exactly
        # one persona row, and keep that stamp untouched.
        db_path = tmp_path / "063-idempotent.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "062")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "INSERT INTO workstreams (ws_id, name, state, created, updated) "
                        "VALUES ('ws-both', 'ws-both', 'closed', "
                        "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
                    )
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO workstream_config (ws_id, key, value) "
                        "VALUES ('ws-both', 'creative_mode', 'True')"
                    )
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO workstream_config (ws_id, key, value) "
                        "VALUES ('ws-both', 'persona', 'scribe')"
                    )
                )

            # No IntegrityError: the NOT IN guard skips ws-both, so the writer
            # stamp is never re-INSERTed over the existing persona row.
            command.upgrade(cfg, "063")

            with engine.connect() as conn:
                persona_rows = conn.execute(
                    sa.text(
                        "SELECT value FROM workstream_config "
                        "WHERE ws_id='ws-both' AND key='persona'"
                    )
                ).fetchall()
                row_persona = conn.execute(
                    sa.text("SELECT persona FROM workstreams WHERE ws_id='ws-both'")
                ).fetchone()
            # Exactly one stamp, and the pre-existing value is untouched.
            assert len(persona_rows) == 1
            assert str(persona_rows[0][0]) == "scribe"
            # The conversion's UPDATE never ran for this ws (not in creative_rows),
            # so the row-projection column stays NULL — untouched, not 'writer'.
            assert row_persona is not None and row_persona[0] is None
        finally:
            engine.dispose()
