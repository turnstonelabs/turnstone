"""Tests for settings registry validation."""

from __future__ import annotations

import pytest

from turnstone.core.settings_registry import (
    BOOTSTRAP_SECTIONS,
    SETTINGS,
    deserialize_value,
    serialize_value,
    validate_key,
    validate_value,
)

# ---------------------------------------------------------------------------
# validate_key
# ---------------------------------------------------------------------------


class TestValidateKey:
    def test_known_key(self):
        defn = validate_key("memory.relevance_k")
        assert defn.key == "memory.relevance_k"
        assert defn.type == "int"

    def test_unknown_key(self):
        with pytest.raises(ValueError, match="Unknown setting"):
            validate_key("nonexistent.key")


# ---------------------------------------------------------------------------
# validate_value — type coercion
# ---------------------------------------------------------------------------


class TestValidateValueCoercion:
    def test_int(self):
        assert validate_value("tools.timeout", "60") == 60
        assert validate_value("tools.timeout", 60) == 60
        assert isinstance(validate_value("tools.timeout", "60"), int)

    def test_float(self):
        assert validate_value("model.temperature", "0.7") == 0.7
        assert validate_value("model.temperature", 1.5) == 1.5
        assert isinstance(validate_value("model.temperature", "0.7"), float)

    def test_bool_native(self):
        assert validate_value("tools.skip_permissions", True) is True
        assert validate_value("tools.skip_permissions", False) is False

    def test_bool_string_true(self):
        for s in ("true", "True", "1", "yes"):
            assert validate_value("tools.skip_permissions", s) is True

    def test_bool_string_false(self):
        for s in ("false", "False", "0", "no"):
            assert validate_value("tools.skip_permissions", s) is False

    def test_bool_garbage_string(self):
        with pytest.raises(ValueError, match="Cannot convert"):
            validate_value("tools.skip_permissions", "banana")

    def test_none_rejected_for_numeric(self):
        """None is not a valid value for numeric settings."""
        with pytest.raises((ValueError, TypeError)):
            validate_value("model.temperature", None)
        with pytest.raises((ValueError, TypeError)):
            validate_value("tools.timeout", None)

    def test_str(self):
        assert validate_value("model.default_alias", "gpt5-prod") == "gpt5-prod"
        assert validate_value("session.instructions", "be nice") == "be nice"


# ---------------------------------------------------------------------------
# validate_value — range constraints
# ---------------------------------------------------------------------------


class TestValidateValueRange:
    def test_min_value(self):
        with pytest.raises(ValueError, match="minimum"):
            validate_value("tools.timeout", 0)  # min_value=1

    def test_max_value(self):
        with pytest.raises(ValueError, match="maximum"):
            validate_value("tools.timeout", 9999)  # max_value=3600

    def test_min_value_float(self):
        with pytest.raises(ValueError, match="minimum"):
            validate_value("model.temperature", -0.1)  # min_value=0.0

    def test_max_value_float(self):
        with pytest.raises(ValueError, match="maximum"):
            validate_value("model.temperature", 2.1)  # max_value=2.0

    def test_boundary_ok(self):
        # Exact boundary values should pass
        assert validate_value("tools.timeout", 1) == 1
        assert validate_value("tools.timeout", 3600) == 3600
        assert validate_value("model.temperature", 0.0) == 0.0
        assert validate_value("model.temperature", 2.0) == 2.0


# ---------------------------------------------------------------------------
# validate_value — choices
# ---------------------------------------------------------------------------


class TestValidateValueChoices:
    def test_valid_choice(self):
        assert validate_value("tools.search", "auto") == "auto"
        assert validate_value("tools.search", "on") == "on"
        assert validate_value("tools.search", "off") == "off"

    def test_invalid_choice(self):
        with pytest.raises(ValueError, match="not in"):
            validate_value("tools.search", "maybe")

    def test_reasoning_effort_choices(self):
        for ch in ("", "none", "low", "medium", "high", "max"):
            assert validate_value("model.reasoning_effort", ch) == ch

    def test_plan_task_alias_accept_any_string(self):
        # plan/task aliases are validated dynamically against live registry
        # at apply time; here we just confirm the static validator accepts
        # arbitrary strings (including "" for "use server default").
        assert validate_value("model.plan_alias", "") == ""
        assert validate_value("model.task_alias", "") == ""
        assert validate_value("model.plan_alias", "smart") == "smart"
        assert validate_value("model.task_alias", "fast") == "fast"

    def test_plan_task_effort_choices(self):
        for ch in ("", "none", "minimal", "low", "medium", "high", "xhigh", "max"):
            assert validate_value("model.plan_effort", ch) == ch
            assert validate_value("model.task_effort", ch) == ch

    def test_plan_task_effort_invalid(self):
        with pytest.raises(ValueError, match="not in"):
            validate_value("model.plan_effort", "extreme")
        with pytest.raises(ValueError, match="not in"):
            validate_value("model.task_effort", "supercharged")


# ---------------------------------------------------------------------------
# serialize / deserialize round-trip
# ---------------------------------------------------------------------------


class TestSerializeDeserialize:
    def test_int_round_trip(self):
        v = 42
        assert deserialize_value("tools.timeout", serialize_value(v)) == v

    def test_float_round_trip(self):
        v = 0.75
        assert deserialize_value("model.temperature", serialize_value(v)) == v

    def test_bool_round_trip(self):
        for v in (True, False):
            assert deserialize_value("tools.skip_permissions", serialize_value(v)) is v

    def test_str_round_trip(self):
        v = "hello world"
        assert deserialize_value("model.default_alias", serialize_value(v)) == v

    def test_str_round_trip_empty(self):
        assert deserialize_value("model.default_alias", serialize_value("")) == ""

    def test_plan_task_round_trip(self):
        for k in (
            "model.plan_alias",
            "model.task_alias",
            "model.plan_effort",
            "model.task_effort",
        ):
            assert deserialize_value(k, serialize_value("")) == ""
            assert deserialize_value(k, serialize_value("high")) == "high"


# ---------------------------------------------------------------------------
# Registry integrity
# ---------------------------------------------------------------------------


class TestRegistryIntegrity:
    def test_all_keys_have_valid_types(self):
        valid_types = {"int", "float", "str", "bool"}
        for key, defn in SETTINGS.items():
            assert defn.type in valid_types, f"{key} has invalid type {defn.type!r}"

    def test_no_bootstrap_section_keys(self):
        for key, defn in SETTINGS.items():
            assert defn.section not in BOOTSTRAP_SECTIONS, (
                f"{key} in bootstrap section {defn.section!r}"
            )

    def test_all_entries_have_descriptions(self):
        for key, defn in SETTINGS.items():
            assert defn.description, f"{key} has empty description"
