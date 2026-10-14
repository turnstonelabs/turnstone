"""Migration 082: existing setups keep the default model they route to (#1359).

A load no longer hands the default to another model when its definition is
left out, and the console now stores the first chat model added as the
default. Setups that never set ``model.default_alias`` get one stored here:
among the enabled chat definitions, the one named ``default``, else the first
in alias order. A global row already present, cleared or not, is left alone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


def _config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[1] / "turnstone/core/storage/migrations"),
    )
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def _upgrade_with(
    url: str,
    rows: list[tuple[Any, ...]],
    *,
    setting: tuple[str, str] | None = None,
) -> Any:
    """081, seed definitions ``(alias, enabled[, model[, capabilities]])`` and an
    optional setting row ``(value, node_id)``, 082; return the stored global
    value, or None."""
    cfg = _config(url)
    command.upgrade(cfg, "081")
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            for alias, enabled, *rest in rows:
                model = rest[0] if rest else "m"
                caps = rest[1] if len(rest) > 1 else "{}"
                conn.execute(
                    sa.text(
                        "INSERT INTO model_definitions "
                        "(definition_id, alias, model, capabilities, enabled, created, updated) "
                        "VALUES (:id, :alias, :model, :caps, :enabled, 'now', 'now')"
                    ),
                    {
                        "id": "def-" + alias,
                        "alias": alias,
                        "model": model,
                        "caps": caps,
                        "enabled": enabled,
                    },
                )
            if setting is not None:
                value, node_id = setting
                conn.execute(
                    sa.text(
                        "INSERT INTO system_settings "
                        "(key, value, node_id, is_secret, changed_by, created, updated) "
                        "VALUES ('model.default_alias', :value, :node, 0, '', 'now', 'now')"
                    ),
                    {"value": json.dumps(value), "node": node_id},
                )
        command.upgrade(cfg, "082")
        with engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT value FROM system_settings "
                    "WHERE key = 'model.default_alias' AND node_id = ''"
                )
            ).first()
    finally:
        engine.dispose()
    return json.loads(row[0]) if row is not None else None


@pytest.fixture
def sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'pin.db'}"


def test_pins_the_first_enabled_definition(sqlite_url: str) -> None:
    rows = [("zeta", 1), ("alpha", 0), ("beta", 1)]
    assert _upgrade_with(sqlite_url, rows) == "beta"


def test_pins_the_definition_named_default(sqlite_url: str) -> None:
    assert _upgrade_with(sqlite_url, [("alpha", 1), ("default", 1)]) == "default"


def test_pins_a_single_model_too(sqlite_url: str) -> None:
    # A later model whose alias sorts first must not take over.
    assert _upgrade_with(sqlite_url, [("only", 1)]) == "only"


def test_leaves_a_set_default_alone(sqlite_url: str) -> None:
    assert _upgrade_with(sqlite_url, [("alpha", 1)], setting=("chosen", "")) == "chosen"


def test_leaves_a_cleared_default_alone(sqlite_url: str) -> None:
    # An admin who cleared the setting chose config.toml or the loader's pick.
    assert _upgrade_with(sqlite_url, [("alpha", 1)], setting=("", "")) == ""


def test_a_node_scoped_default_does_not_block_the_global_pin(sqlite_url: str) -> None:
    rows = [("alpha", 1)]
    assert _upgrade_with(sqlite_url, rows, setting=("other", "node-1")) == "alpha"


def test_nothing_enabled_writes_nothing(sqlite_url: str) -> None:
    assert _upgrade_with(sqlite_url, [("alpha", 0)]) is None
    engine = sa.create_engine(sqlite_url)
    try:
        with engine.connect() as conn:
            count = conn.execute(sa.text("SELECT COUNT(*) FROM system_settings")).scalar_one()
    finally:
        engine.dispose()
    assert count == 0


def test_skips_rerankers_and_speech_models(sqlite_url: str) -> None:
    # The setting outranks every node's config.toml default: a setup whose
    # chat models live in config.toml must not have its chats routed to the
    # one reranker its Models tab holds.
    rows = [
        ("a-rerank", 1, "bge-reranker", '{"supports_rerank": true}'),
        ("b-stt", 1, "whisper-1"),
        ("c-tts", 1, "voice", '{"supports_speech_synthesis": true}'),
        ("d-chat", 1),
    ]
    assert _upgrade_with(sqlite_url, rows) == "d-chat"


def test_only_non_chat_models_pin_nothing(sqlite_url: str) -> None:
    rows = [("rerank", 1, "bge-reranker", '{"supports_rerank": true}')]
    assert _upgrade_with(sqlite_url, rows) is None


def test_an_omni_model_counts_as_chat(sqlite_url: str) -> None:
    rows = [("a-omni", 1, "gpt-4o-audio", '{"supports_audio_input": true}'), ("b-chat", 1)]
    assert _upgrade_with(sqlite_url, rows) == "a-omni"


def test_pins_on_postgresql(fresh_pg_url: Any) -> None:
    url = fresh_pg_url.render_as_string(hide_password=False)
    assert _upgrade_with(url, [("zeta", 1), ("beta", 1)]) == "beta"
