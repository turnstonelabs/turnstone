"""Tests for the persona snapshot codec (turnstone.core.personas).

The stamp is the load-bearing seam of the feature: it must round-trip the
tri-state tool set byte-stably, treat a missing stamp as legacy, and treat a
partial or unparseable stamp as loud corruption — never as a silent fallback
to some default envelope.
"""

from __future__ import annotations

import pytest

from turnstone.core.personas import (
    PERSONA_CONFIG_KEYS,
    PersonaSnapshot,
    snapshot_from_config,
    snapshot_from_persona,
)


class TestSnapshotFromPersona:
    def test_full_row(self) -> None:
        snap = snapshot_from_persona(
            {
                "name": "scribe",
                "base_prompt": "You are a scribe.",
                "tool_allowlist": [],
                "mcp_enabled": False,
                "memory_enabled": False,
            }
        )
        assert snap.name == "scribe"
        assert snap.prompt == "You are a scribe."
        assert snap.tools == frozenset()
        assert snap.mcp is False
        assert snap.memory is False

    def test_null_levers_stay_open(self) -> None:
        snap = snapshot_from_persona({"name": "engineer", "base_prompt": None})
        assert snap.prompt == ""
        assert snap.tools is None
        assert snap.mcp is True
        assert snap.memory is True


class TestConfigRoundTrip:
    @pytest.mark.parametrize(
        "tools",
        [None, frozenset(), frozenset({"read_file", "search", "memory"})],
    )
    def test_tristate_roundtrip(self, tools: frozenset[str] | None) -> None:
        snap = PersonaSnapshot(name="p", prompt="base", tools=tools, mcp=False, memory=True)
        assert snapshot_from_config(snap.to_config()) == snap

    def test_to_config_is_byte_stable(self) -> None:
        snap = PersonaSnapshot(
            name="p", prompt="", tools=frozenset({"b", "a"}), mcp=True, memory=True
        )
        cfg = snap.to_config()
        assert cfg["persona_tools"] == '["a", "b"]'  # sorted → stable across saves
        assert set(cfg) == set(PERSONA_CONFIG_KEYS)
        assert snapshot_from_config(cfg).to_config() == cfg


class TestConfigParsing:
    def test_absent_is_legacy(self) -> None:
        assert snapshot_from_config({}) is None
        assert snapshot_from_config({"model": "x", "skill": "y"}) is None

    def test_partial_stamp_is_corrupt(self) -> None:
        cfg = PersonaSnapshot("p", "", None, True, True).to_config()
        del cfg["persona_tools"]
        with pytest.raises(ValueError, match="missing keys"):
            snapshot_from_config(cfg)

    def test_companions_without_name_are_corrupt(self) -> None:
        with pytest.raises(ValueError, match="without 'persona'"):
            snapshot_from_config({"persona_mcp": "1"})

    def test_empty_name_is_corrupt(self) -> None:
        cfg = PersonaSnapshot("p", "", None, True, True).to_config()
        cfg["persona"] = ""
        with pytest.raises(ValueError, match="empty persona name"):
            snapshot_from_config(cfg)

    def test_bad_tools_json_is_corrupt(self) -> None:
        cfg = PersonaSnapshot("p", "", None, True, True).to_config()
        cfg["persona_tools"] = "not json"
        with pytest.raises(ValueError, match="not JSON"):
            snapshot_from_config(cfg)

    def test_wrong_tools_shape_is_corrupt(self) -> None:
        cfg = PersonaSnapshot("p", "", None, True, True).to_config()
        cfg["persona_tools"] = '{"read_file": true}'
        with pytest.raises(ValueError, match="null or a list"):
            snapshot_from_config(cfg)

    def test_bad_flag_is_corrupt(self) -> None:
        cfg = PersonaSnapshot("p", "", None, True, True).to_config()
        cfg["persona_memory"] = "True"
        with pytest.raises(ValueError, match="persona_memory"):
            snapshot_from_config(cfg)
