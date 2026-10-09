"""Skill session-setting column drop keeps every other skill field and round-trips."""

import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

_DROPPED = {
    "model",
    "temperature",
    "reasoning_effort",
    "max_tokens",
    "agent_max_turns",
    "activation",
    "is_default",
    "token_budget",
}
_KEPT = "name, content, auto_approve, priority"

# notify_on_complete values before the upgrade, and what each holds after it.
_NOTIFY = {
    "n-good": ('[{"channel_type": "discord", "channel_id": "1"}]', None),
    "n-legacy": ("{}", None),
    "n-empty": ("[]", None),
    "n-no-id": ('[{"channel_type": "discord"}]', "[]"),
    "n-numeric-id": ('[{"channel_type": "discord", "channel_id": 1}]', "[]"),
    "n-both-ids": ('[{"channel_type": "discord", "channel_id": "1", "user_id": "2"}]', "[]"),
    "n-object": ('{"channel": "discord"}', "[]"),
    "n-not-json": ("discord", "[]"),
    "n-null-type": ('[{"channel_type": null, "channel_id": "1"}]', "[]"),
    "n-string-entry": ('["discord"]', "[]"),
    "n-no-type": ('[{"channel_id": "1"}]', "[]"),
    "n-eleven": (
        json.dumps([{"channel_type": "discord", "channel_id": str(i)} for i in range(11)]),
        "[]",
    ),
    "n-long-id": (json.dumps([{"channel_type": "discord", "channel_id": "x" * 257}]), "[]"),
    "n-blank-id": ('[{"channel_type": "discord", "channel_id": "   "}]', "[]"),
}

# Saved skill stamps before the upgrade: (skill name, stamped id) and whether 080 clears the text.
_STAMPS = {
    # Switched to another existing skill before the upgrade: the stamp is cleared.
    "ws-switched": ("other", "t1", True),
    # The skill it was created with, under its own name.
    "ws-created": ("reviewer", "t1", False),
    # Saved nameless by an older reopen.
    "ws-nameless": ("", "t1", False),
    # Created with a skill renamed since: the saved name matches no row. A switch whose target
    # skill was later deleted or renamed looks the same and keeps its stamp too.
    "ws-renamed": ("old-name", "t1", False),
    # The stamped skill is gone; a newer row holds its name (MCP prompt sync re-creates rows).
    # A switch whose original was deleted looks the same and keeps its stamp too.
    "ws-stamp-gone": ("reviewer", "t-gone", False),
}


def _columns(engine: sa.Engine) -> set[str]:
    return {col["name"] for col in sa.inspect(engine).get_columns("prompt_templates")}


def _config_rows(engine: sa.Engine) -> set[tuple[str, str, str]]:
    with engine.connect() as conn:
        return {
            (r[0], r[1], r[2])
            for r in conn.execute(sa.text("SELECT ws_id, key, value FROM workstream_config"))
        }


def _notify(engine: sa.Engine) -> dict[str, str]:
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT template_id, notify_on_complete FROM prompt_templates"))
        return {r[0]: r[1] for r in rows}


def _stamp_texts(engine: sa.Engine) -> dict[str, str]:
    return {ws: v for ws, k, v in _config_rows(engine) if k == "applied_skill_content"}


def _seed(engine: sa.Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO prompt_templates (template_id, name, content, model, "
                "temperature, reasoning_effort, max_tokens, auto_approve, token_budget, "
                "agent_max_turns, activation, is_default, priority, created, updated) "
                "VALUES ('t1', 'reviewer', 'Review.', 'big-model', 0.3, 'max', 4096, 1, "
                "500, 7, 'default', 1, 3, '2026-01-01', '2026-01-01')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO prompt_templates (template_id, name, content, created, updated) "
                "VALUES ('t2', 'other', 'Other.', '2026-01-01', '2026-01-01')"
            )
        )
        for tid, (raw, _) in _NOTIFY.items():
            conn.execute(
                sa.text(
                    "INSERT INTO prompt_templates (template_id, name, content, "
                    "notify_on_complete, created, updated) "
                    "VALUES (:t, :t, 'x', :raw, '2026-01-01', '2026-01-01')"
                ),
                {"t": tid, "raw": raw},
            )
        conn.execute(
            sa.text(
                "INSERT INTO workstream_config (ws_id, key, value) VALUES "
                "('ws1', 'token_budget', '500'), ('ws1', 'skill', 'reviewer'), "
                "('ws2', 'token_budget', '0')"
            )
        )
        for ws, (skill, stamped_id, _) in _STAMPS.items():
            conn.execute(
                sa.text(
                    "INSERT INTO workstream_config (ws_id, key, value) VALUES "
                    "(:ws, 'skill', :skill), (:ws, 'applied_skill_id', :tid), "
                    "(:ws, 'applied_skill_content', 'Saved text.')"
                ),
                {"ws": ws, "skill": skill, "tid": stamped_id},
            )


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
        _seed(engine)
        command.upgrade(cfg, "080")
        assert not _DROPPED & _columns(engine)
        with engine.connect() as conn:
            assert conn.execute(
                sa.text(f"SELECT {_KEPT} FROM prompt_templates WHERE template_id = 't1'")
            ).one() == ("reviewer", "Review.", 1, 3)
        # Only the saved token-budget keys go.
        assert not {row for row in _config_rows(engine) if row[1] == "token_budget"}
        assert ("ws1", "skill", "reviewer") in _config_rows(engine)
        # Only a stamp left behind by a switch to another existing skill is cleared.
        assert _stamp_texts(engine) == {
            ws: ("" if cleared else "Saved text.") for ws, (_, _, cleared) in _STAMPS.items()
        }
        # Targets workstream create would ignore are reset; the rest are untouched.
        notify = _notify(engine)
        for tid, (raw, after) in _NOTIFY.items():
            assert notify[tid] == (raw if after is None else after), tid
        # The table rebuild keeps the unique skill name.
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO prompt_templates (template_id, name, content, created, updated) "
                    "VALUES ('t9', 'reviewer', 'Again.', '2026-01-01', '2026-01-01')"
                )
            )
        command.downgrade(cfg, "079")
        assert _columns(engine) >= _DROPPED
        with engine.connect() as conn:
            assert conn.execute(
                sa.text(
                    "SELECT model, temperature, reasoning_effort, max_tokens, agent_max_turns, "
                    "activation, is_default, token_budget, priority FROM prompt_templates "
                    "WHERE template_id = 't1'"
                )
            ).one() == ("", None, "", None, None, "named", 0, 0, 3)
        # Neither data fix is undone.
        assert not {row for row in _config_rows(engine) if row[1] == "token_budget"}
        assert _stamp_texts(engine)["ws-switched"] == ""
        assert _notify(engine)["n-no-id"] == "[]"
        command.upgrade(cfg, "080")
        assert not _DROPPED & _columns(engine)
    finally:
        engine.dispose()


def test_skill_settings_drop_migration_sqlite(tmp_path):
    _roundtrip(f"sqlite:///{tmp_path / 'skills.db'}")


def test_skill_settings_drop_migration_postgresql(fresh_pg_url):
    _roundtrip(fresh_pg_url.render_as_string(hide_password=False))


def test_frozen_notify_check_matches_the_live_one():
    """080's frozen copy of the check agrees with the live check on the cases above."""
    import importlib

    from turnstone.core.notify_targets import validate_notify_targets

    migration = importlib.import_module(
        "turnstone.core.storage.migrations.versions.080_drop_skill_settings"
    )
    for raw, after in _NOTIFY.values():
        if raw.strip() in ("", "[]", "{}"):
            continue
        assert migration._notify_targets_valid(raw) == (after is None), raw
        assert (validate_notify_targets(raw)[1] == "") == (after is None), raw
