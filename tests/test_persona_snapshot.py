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
        # tools NULL, and mcp/memory absent, default to the open envelope.
        snap = snapshot_from_persona({"name": "p", "base_prompt": "base"})
        assert snap.prompt == "base"
        assert snap.tools is None
        assert snap.mcp is True
        assert snap.memory is True

    def test_file_backed_prompt_resolves_from_file(self) -> None:
        # A built-in row (base_prompt NULL, base_prompt_file set) resolves its
        # BASE from prompts/personas/<file> and freezes it into the stamp.
        from turnstone.prompts import load_persona_prompt

        snap = snapshot_from_persona(
            {"name": "scribe", "base_prompt": None, "base_prompt_file": "scribe.md"}
        )
        assert snap.prompt == load_persona_prompt("scribe.md")
        assert snap.prompt.startswith("You turn raw material")

    def test_operator_override_wins_over_file(self) -> None:
        # base_prompt ?? load(file): an operator override on a built-in row wins.
        snap = snapshot_from_persona(
            {"name": "scribe", "base_prompt": "OVERRIDE", "base_prompt_file": "scribe.md"}
        )
        assert snap.prompt == "OVERRIDE"

    def test_sourceless_persona_raises(self) -> None:
        # The storage CHECK forbids this row; if one reaches resolution it must
        # fail loudly rather than compose an empty BASE.
        with pytest.raises(ValueError, match="no prompt source"):
            snapshot_from_persona({"name": "broken", "base_prompt": None})


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


class _FakeStorage:
    """Minimal storage double for resolve tests — exact-name index + list."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def get_persona_by_name(self, name: str) -> dict | None:
        return next((dict(r) for r in self._rows if r["name"] == name), None)

    def list_personas(self, include_disabled: bool = False) -> list[dict]:
        return [dict(r) for r in self._rows if include_disabled or r.get("enabled")]


def _rows() -> list[dict]:
    return [
        {
            "name": "engineer",
            "display_name": "Engineer",
            "enabled": True,
            "is_default": True,
            "applies_to_kinds": ["interactive"],
        },
        {
            "name": "writer",
            "display_name": "Creative Writer",
            "enabled": True,
            "applies_to_kinds": ["interactive"],
        },
        {
            "name": "executive",
            "display_name": "Executive",
            "enabled": True,
            "applies_to_kinds": ["coordinator"],
        },
        {
            "name": "retired",
            "display_name": "Retired Persona",
            "enabled": False,
            "applies_to_kinds": ["interactive"],
        },
    ]


class TestForgivingResolution:
    """resolve_persona_for_kind — one shared rule, forgiving on all surfaces.

    Exact slug first, then the lowercased input, then a UNIQUE
    case-insensitive display-name match; every failure enumerates the
    kind's live names (the self-correction path for stale tool
    descriptions), and callers stamp the returned row's canonical slug.
    """

    def _resolve(self, name: str, kind: str = "interactive", rows: list[dict] | None = None):
        from turnstone.core.personas import resolve_persona_for_kind

        return resolve_persona_for_kind(_FakeStorage(rows or _rows()), name, kind)

    def test_exact_slug_resolves(self) -> None:
        row, err = self._resolve("writer")
        assert err == "" and row is not None and row["name"] == "writer"

    def test_case_variants_resolve_to_canonical_row(self) -> None:
        for variant in ("Writer", "WRITER", " writer "):
            row, err = self._resolve(variant)
            assert err == "" and row is not None and row["name"] == "writer"

    def test_unique_display_name_resolves_to_slug(self) -> None:
        for variant in ("Creative Writer", "creative writer"):
            row, err = self._resolve(variant)
            assert err == "" and row is not None and row["name"] == "writer"

    def test_ambiguous_display_name_names_the_candidates(self) -> None:
        rows = _rows() + [
            {
                "name": "novelist",
                "display_name": "creative writer",
                "enabled": True,
                "applies_to_kinds": ["interactive"],
            }
        ]
        row, err = self._resolve("Creative Writer", rows=rows)
        assert row is None
        assert "more than one display name" in err
        assert "novelist" in err and "writer" in err
        assert "use the exact name" in err

    def test_same_display_name_across_kinds_resolves_per_kind(self) -> None:
        # The label the caller saw came from a kind-filtered surface, so a
        # same-label persona of the OTHER kind must neither block (spurious
        # ambiguity) nor win (cross-kind resolution).
        rows = _rows() + [
            {
                "name": "helper-coord",
                "display_name": "Helper",
                "enabled": True,
                "applies_to_kinds": ["coordinator"],
            },
            {
                "name": "helper-int",
                "display_name": "Helper",
                "enabled": True,
                "applies_to_kinds": ["interactive"],
            },
        ]
        row, err = self._resolve("Helper", rows=rows)
        assert err == "" and row is not None and row["name"] == "helper-int"
        row, err = self._resolve("Helper", kind="coordinator", rows=rows)
        assert err == "" and row is not None and row["name"] == "helper-coord"

    def test_wrong_kind_display_match_is_not_found_with_choices(self) -> None:
        # Display names are labels, not identifiers: a label that only exists
        # on another kind's persona reads as unknown for THIS kind (with the
        # kind's live choices attached) — never as a cross-kind resolution.
        rows = _rows() + [
            {
                "name": "chief",
                "display_name": "The Chief",
                "enabled": True,
                "applies_to_kinds": ["coordinator"],
            }
        ]
        row, err = self._resolve("The Chief", rows=rows)
        assert row is None
        assert "not found or disabled" in err
        assert "Available for interactive: engineer (default), writer" in err

    def test_whitespace_input_never_matches_blank_display_names(self) -> None:
        # display_name defaults to "" — a whitespace-only input (reachable via
        # CLI `--persona "  "`) must read as unknown, never resolve to a
        # blank-labelled persona or report a bogus ambiguity.
        rows = _rows() + [
            {
                "name": "unlabelled",
                "display_name": "",
                "enabled": True,
                "applies_to_kinds": ["interactive"],
            },
            {
                "name": "unlabelled-too",
                "display_name": "   ",
                "enabled": True,
                "applies_to_kinds": ["interactive"],
            },
        ]
        for raw in ("", " ", "   "):
            row, err = self._resolve(raw, rows=rows)
            assert row is None
            assert "not found or disabled" in err
            assert "more than one display name" not in err

    def test_unknown_error_lists_kind_names_default_first(self) -> None:
        row, err = self._resolve("nope")
        assert row is None
        assert "Persona not found or disabled: nope" in err
        assert "Available for interactive: engineer (default), writer" in err
        assert "executive" not in err  # wrong kind
        assert "retired" not in err  # disabled

    def test_kind_mismatch_reports_canonical_slug_and_choices(self) -> None:
        row, err = self._resolve("Executive")  # case-forgiven, then kind-refused
        assert row is None
        assert "'executive' does not apply to kind 'interactive'" in err
        assert "Available for interactive: engineer (default), writer" in err

    def test_disabled_persona_is_not_resolvable_by_any_route(self) -> None:
        for variant in ("retired", "RETIRED", "Retired Persona"):
            row, err = self._resolve(variant)
            assert row is None
            assert "not found or disabled" in err

    def test_storage_none_is_a_distinct_error(self) -> None:
        from turnstone.core.personas import resolve_persona_for_kind

        row, err = resolve_persona_for_kind(None, "writer", "interactive")
        assert row is None and err == "persona storage unavailable"

    def test_listing_failure_degrades_to_plain_error(self) -> None:
        class _Broken(_FakeStorage):
            def list_personas(self, include_disabled: bool = False) -> list[dict]:
                raise RuntimeError("db gone")

        from turnstone.core.personas import resolve_persona_for_kind

        row, err = resolve_persona_for_kind(_Broken(_rows()), "nope", "interactive")
        assert row is None
        assert "Persona not found or disabled: nope" in err
