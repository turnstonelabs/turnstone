"""Skill model/sampling/turn-cap column drop keeps every other skill field and round-trips."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

_DROPPED = {"model", "temperature", "reasoning_effort", "max_tokens", "agent_max_turns"}
_KEPT = "name, content, auto_approve, token_budget"


def _columns(engine: sa.Engine) -> set[str]:
    return {col["name"] for col in sa.inspect(engine).get_columns("prompt_templates")}


def _roundtrip(url: str) -> None:
    cfg = Config()
    cfg.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[1] / "turnstone/core/storage/migrations"),
    )
    cfg.set_main_option("sqlalchemy.url", str(url).replace("%", "%%"))
    command.upgrade(cfg, "079")
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO prompt_templates (template_id, name, content, model, "
                    "temperature, reasoning_effort, max_tokens, auto_approve, token_budget, "
                    "agent_max_turns, created, updated) VALUES ('t1', 'reviewer', 'Review.', "
                    "'big-model', 0.3, 'max', 4096, 1, 500, 7, '2026-01-01', '2026-01-01')"
                )
            )
        command.upgrade(cfg, "080")
        assert not _DROPPED & _columns(engine)
        with engine.connect() as conn:
            assert conn.execute(
                sa.text(f"SELECT {_KEPT} FROM prompt_templates WHERE template_id = 't1'")
            ).one() == ("reviewer", "Review.", 1, 500)
        # The table rebuild keeps the unique skill name.
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO prompt_templates (template_id, name, content, created, updated) "
                    "VALUES ('t2', 'reviewer', 'Again.', '2026-01-01', '2026-01-01')"
                )
            )
        command.downgrade(cfg, "079")
        assert _columns(engine) >= _DROPPED
        with engine.connect() as conn:
            assert conn.execute(
                sa.text(
                    "SELECT model, temperature, reasoning_effort, max_tokens, agent_max_turns, "
                    "token_budget FROM prompt_templates WHERE template_id = 't1'"
                )
            ).one() == ("", None, "", None, None, 500)
        command.upgrade(cfg, "080")
        assert not _DROPPED & _columns(engine)
    finally:
        engine.dispose()


def test_skill_settings_drop_migration_sqlite(tmp_path):
    _roundtrip(f"sqlite:///{tmp_path / 'skills.db'}")


def test_skill_settings_drop_migration_postgresql(fresh_pg_url):
    _roundtrip(fresh_pg_url.render_as_string(hide_password=False))
