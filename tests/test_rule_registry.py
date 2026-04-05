"""Tests for rule_registry — merge logic for heuristic rules and output guard patterns."""

from __future__ import annotations

from turnstone.core.rule_registry import (
    RuleRegistry,
)

# ---------------------------------------------------------------------------
# Mock storage helper
# ---------------------------------------------------------------------------


class _MockStorage:
    """Minimal storage stub that returns configurable rule/pattern lists."""

    def __init__(
        self,
        heuristic_rows: list[dict] | None = None,
        output_pattern_rows: list[dict] | None = None,
    ) -> None:
        self._heuristic_rows = heuristic_rows or []
        self._output_pattern_rows = output_pattern_rows or []

    def list_heuristic_rules(self, enabled_only: bool = False) -> list[dict]:
        return list(self._heuristic_rows)

    def list_output_guard_patterns(self, enabled_only: bool = False) -> list[dict]:
        return list(self._output_pattern_rows)


class _BrokenStorage(_MockStorage):
    """Storage stub that raises on every call."""

    def list_heuristic_rules(self, enabled_only: bool = False) -> list[dict]:
        raise RuntimeError("DB connection lost")

    def list_output_guard_patterns(self, enabled_only: bool = False) -> list[dict]:
        raise RuntimeError("DB connection lost")


# ---------------------------------------------------------------------------
# 1. RuleRegistry with no storage — only built-in rules
# ---------------------------------------------------------------------------


class TestBuiltinsOnly:
    def test_builtin_heuristic_rules_loaded(self) -> None:
        reg = RuleRegistry(storage=None)
        assert len(reg.heuristic_rules) == 37

    def test_builtin_output_patterns_loaded(self) -> None:
        reg = RuleRegistry(storage=None)
        total = sum(len(pats) for pats in reg.output_patterns.values())
        assert total == 19
        assert len(reg.output_patterns) == 5

    def test_heuristic_rules_sorted_by_tier(self) -> None:
        reg = RuleRegistry(storage=None)
        tier_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        tiers = [tier_order[r.tier] for r in reg.heuristic_rules]
        assert tiers == sorted(tiers)

    def test_output_patterns_grouped_by_category(self) -> None:
        reg = RuleRegistry(storage=None)
        expected_categories = {
            "prompt_injection",
            "credentials",
            "encoded_payloads",
            "adversarial_urls",
            "info_disclosure",
        }
        assert set(reg.output_patterns.keys()) == expected_categories


# ---------------------------------------------------------------------------
# 2. RuleRegistry with mock storage — merge logic
# ---------------------------------------------------------------------------


class TestHeuristicMerge:
    def test_custom_rule_added(self) -> None:
        storage = _MockStorage(
            heuristic_rows=[
                {
                    "name": "my-custom-rule",
                    "enabled": True,
                    "builtin": False,
                    "risk_level": "high",
                    "confidence": 0.85,
                    "recommendation": "review",
                    "tool_pattern": "bash",
                    "arg_patterns": '["rm -rf /tmp"]',
                    "intent_template": "Custom: {arg_snippet}",
                    "reasoning_template": "Custom reasoning.",
                    "tier": "high",
                    "priority": 0,
                },
            ]
        )
        reg = RuleRegistry(storage=storage)
        names = [r.name for r in reg.heuristic_rules]
        assert "my-custom-rule" in names
        # Built-ins still present
        assert len(reg.heuristic_rules) == 38

    def test_builtin_overridden(self) -> None:
        storage = _MockStorage(
            heuristic_rows=[
                {
                    "name": "rm-root",  # same name as built-in
                    "enabled": True,
                    "builtin": True,
                    "risk_level": "high",  # changed from critical
                    "confidence": 0.50,
                    "recommendation": "review",
                    "tool_pattern": "bash",
                    "arg_patterns": "[]",
                    "intent_template": "Overridden: {arg_snippet}",
                    "reasoning_template": "Overridden reasoning.",
                    "tier": "high",
                    "priority": 0,
                },
            ]
        )
        reg = RuleRegistry(storage=storage)
        matched = [r for r in reg.heuristic_rules if r.name == "rm-root"]
        assert len(matched) == 1
        assert matched[0].risk_level == "high"
        assert matched[0].confidence == 0.50
        assert matched[0].intent_template == "Overridden: {arg_snippet}"

    def test_builtin_disabled(self) -> None:
        storage = _MockStorage(
            heuristic_rows=[
                {
                    "name": "rm-root",
                    "enabled": False,
                    "builtin": True,
                },
            ]
        )
        reg = RuleRegistry(storage=storage)
        names = [r.name for r in reg.heuristic_rules]
        assert "rm-root" not in names
        assert len(reg.heuristic_rules) == 36

    def test_custom_rule_disabled_excluded(self) -> None:
        storage = _MockStorage(
            heuristic_rows=[
                {
                    "name": "my-disabled-rule",
                    "enabled": False,
                    "builtin": False,
                    "risk_level": "medium",
                    "confidence": 0.70,
                    "recommendation": "review",
                    "tool_pattern": "*",
                    "arg_patterns": "[]",
                    "intent_template": "",
                    "reasoning_template": "",
                    "tier": "medium",
                    "priority": 0,
                },
            ]
        )
        reg = RuleRegistry(storage=storage)
        names = [r.name for r in reg.heuristic_rules]
        assert "my-disabled-rule" not in names
        assert len(reg.heuristic_rules) == 37

    def test_reload_updates_rules(self) -> None:
        storage = _MockStorage()
        reg = RuleRegistry(storage=storage)
        assert len(reg.heuristic_rules) == 37

        # Simulate admin adding a rule
        storage._heuristic_rows.append(
            {
                "name": "late-addition",
                "enabled": True,
                "builtin": False,
                "risk_level": "medium",
                "confidence": 0.70,
                "recommendation": "review",
                "tool_pattern": "bash",
                "arg_patterns": "[]",
                "intent_template": "Late: {arg_snippet}",
                "reasoning_template": "Added after init.",
                "tier": "medium",
                "priority": 0,
            }
        )
        reg.reload()
        assert len(reg.heuristic_rules) == 38
        assert "late-addition" in [r.name for r in reg.heuristic_rules]

    def test_version_increments_on_reload(self) -> None:
        reg = RuleRegistry(storage=None)
        v1 = reg.version
        assert v1 == 1  # __init__ calls reload() once
        reg.reload()
        assert reg.version == 2
        reg.reload()
        assert reg.version == 3


# ---------------------------------------------------------------------------
# 3. OutputGuardPatternDef merge
# ---------------------------------------------------------------------------


class TestOutputPatternMerge:
    def test_custom_output_pattern_added(self) -> None:
        storage = _MockStorage(
            output_pattern_rows=[
                {
                    "name": "custom-ssn",
                    "enabled": True,
                    "builtin": False,
                    "category": "info_disclosure",
                    "risk_level": "high",
                    "pattern": r"\b\d{3}-\d{2}-\d{4}\b",
                    "pattern_flags": "",
                    "flag_name": "ssn_leak",
                    "annotation": "Output contains what appears to be a Social Security number.",
                    "is_credential": True,
                    "redact_label": "ssn",
                    "priority": 50,
                },
            ]
        )
        reg = RuleRegistry(storage=storage)
        info_pats = reg.output_patterns.get("info_disclosure", ())
        names = [p.name for p in info_pats]
        assert "custom-ssn" in names

        total = sum(len(pats) for pats in reg.output_patterns.values())
        assert total == 20

    def test_builtin_output_pattern_disabled(self) -> None:
        storage = _MockStorage(
            output_pattern_rows=[
                {
                    "name": "override_phrases",
                    "enabled": False,
                    "builtin": True,
                },
            ]
        )
        reg = RuleRegistry(storage=storage)
        pi_pats = reg.output_patterns.get("prompt_injection", ())
        names = [p.name for p in pi_pats]
        assert "override_phrases" not in names

        total = sum(len(pats) for pats in reg.output_patterns.values())
        assert total == 18

    def test_invalid_regex_skipped(self) -> None:
        storage = _MockStorage(
            output_pattern_rows=[
                {
                    "name": "bad-regex",
                    "enabled": True,
                    "builtin": False,
                    "category": "credentials",
                    "risk_level": "high",
                    "pattern": "[invalid(",  # broken regex
                    "pattern_flags": "",
                    "flag_name": "bad",
                    "annotation": "Should be skipped.",
                    "is_credential": False,
                    "redact_label": "",
                    "priority": 0,
                },
            ]
        )
        reg = RuleRegistry(storage=storage)
        all_names = [p.name for pats in reg.output_patterns.values() for p in pats]
        assert "bad-regex" not in all_names
        # Built-ins intact
        total = sum(len(pats) for pats in reg.output_patterns.values())
        assert total == 19


# ---------------------------------------------------------------------------
# 4. Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_storage_error_falls_back_to_builtins(self) -> None:
        storage = _BrokenStorage()
        reg = RuleRegistry(storage=storage)
        assert len(reg.heuristic_rules) == 37
        total = sum(len(pats) for pats in reg.output_patterns.values())
        assert total == 19

    def test_empty_storage_equals_builtins(self) -> None:
        no_storage = RuleRegistry(storage=None)
        empty_storage = RuleRegistry(storage=_MockStorage())
        assert len(no_storage.heuristic_rules) == len(empty_storage.heuristic_rules)
        assert set(no_storage.output_patterns.keys()) == set(empty_storage.output_patterns.keys())
        for cat in no_storage.output_patterns:
            no_names = {p.name for p in no_storage.output_patterns[cat]}
            empty_names = {p.name for p in empty_storage.output_patterns[cat]}
            assert no_names == empty_names
