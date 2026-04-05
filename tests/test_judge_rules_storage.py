"""Tests for heuristic_rules and output_guard_patterns storage CRUD operations."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from turnstone.core.storage._sqlite import SQLiteBackend


def _make_id() -> str:
    return uuid.uuid4().hex


class TestHeuristicRuleStorage:
    def test_create_and_get_heuristic_rule(self, db: SQLiteBackend) -> None:
        rid = _make_id()
        db.create_heuristic_rule(
            rule_id=rid,
            name="dangerous-exec",
            risk_level="critical",
            confidence=0.95,
            recommendation="deny",
            tool_pattern="execute_code",
            arg_patterns='[".*exec.*", ".*eval.*"]',
            intent_template="User wants to run code",
            reasoning_template="Executing arbitrary code is dangerous",
            tier="critical",
            priority=100,
            builtin=True,
            enabled=True,
            created_by="admin",
        )
        r = db.get_heuristic_rule(rid)
        assert r is not None
        assert r["rule_id"] == rid
        assert r["name"] == "dangerous-exec"
        assert r["risk_level"] == "critical"
        assert r["confidence"] == 0.95
        assert r["recommendation"] == "deny"
        assert r["tool_pattern"] == "execute_code"
        assert r["arg_patterns"] == '[".*exec.*", ".*eval.*"]'
        assert r["intent_template"] == "User wants to run code"
        assert r["reasoning_template"] == "Executing arbitrary code is dangerous"
        assert r["tier"] == "critical"
        assert r["priority"] == 100
        assert r["builtin"] is True
        assert r["enabled"] is True
        assert r["created_by"] == "admin"

    def test_get_heuristic_rule_by_name(self, db: SQLiteBackend) -> None:
        rid = _make_id()
        db.create_heuristic_rule(
            rule_id=rid,
            name="by-name-lookup",
            risk_level="high",
            confidence=0.8,
            recommendation="review",
            tool_pattern="file_write",
        )
        r = db.get_heuristic_rule_by_name("by-name-lookup")
        assert r is not None
        assert r["rule_id"] == rid
        assert r["name"] == "by-name-lookup"

    def test_get_heuristic_rule_by_name_not_found(self, db: SQLiteBackend) -> None:
        assert db.get_heuristic_rule_by_name("nonexistent") is None

    def test_list_heuristic_rules(self, db: SQLiteBackend) -> None:
        db.create_heuristic_rule(
            rule_id=_make_id(),
            name="low-tier-rule",
            risk_level="low",
            confidence=0.5,
            recommendation="approve",
            tool_pattern="read_file",
            tier="low",
            priority=10,
        )
        db.create_heuristic_rule(
            rule_id=_make_id(),
            name="critical-tier-rule",
            risk_level="critical",
            confidence=0.99,
            recommendation="deny",
            tool_pattern="delete_all",
            tier="critical",
            priority=50,
        )
        db.create_heuristic_rule(
            rule_id=_make_id(),
            name="medium-tier-rule",
            risk_level="medium",
            confidence=0.7,
            recommendation="review",
            tool_pattern="web_search",
            tier="medium",
            priority=20,
        )
        rules = db.list_heuristic_rules()
        assert len(rules) == 3
        # Ordered by tier (critical=0, medium=2, low=3) then priority desc
        assert rules[0]["name"] == "critical-tier-rule"
        assert rules[1]["name"] == "medium-tier-rule"
        assert rules[2]["name"] == "low-tier-rule"

    def test_list_heuristic_rules_enabled_only(self, db: SQLiteBackend) -> None:
        db.create_heuristic_rule(
            rule_id=_make_id(),
            name="enabled-rule",
            risk_level="medium",
            confidence=0.7,
            recommendation="approve",
            tool_pattern="tool_a",
            enabled=True,
        )
        db.create_heuristic_rule(
            rule_id=_make_id(),
            name="disabled-rule",
            risk_level="low",
            confidence=0.3,
            recommendation="deny",
            tool_pattern="tool_b",
            enabled=False,
        )
        enabled = db.list_heuristic_rules(enabled_only=True)
        assert len(enabled) == 1
        assert enabled[0]["name"] == "enabled-rule"
        assert enabled[0]["enabled"] is True

    def test_update_heuristic_rule(self, db: SQLiteBackend) -> None:
        rid = _make_id()
        db.create_heuristic_rule(
            rule_id=rid,
            name="orig-name",
            risk_level="low",
            confidence=0.5,
            recommendation="review",
            tool_pattern="orig_tool",
        )
        ok = db.update_heuristic_rule(
            rid,
            name="updated-name",
            risk_level="high",
            confidence=0.9,
            recommendation="deny",
            enabled=False,
            builtin=True,
        )
        assert ok is True
        r = db.get_heuristic_rule(rid)
        assert r is not None
        assert r["name"] == "updated-name"
        assert r["risk_level"] == "high"
        assert r["confidence"] == 0.9
        assert r["recommendation"] == "deny"
        assert r["enabled"] is False
        assert r["builtin"] is True

    def test_update_heuristic_rule_not_found(self, db: SQLiteBackend) -> None:
        ok = db.update_heuristic_rule("nonexistent", name="x")
        assert ok is False

    def test_delete_heuristic_rule(self, db: SQLiteBackend) -> None:
        rid = _make_id()
        db.create_heuristic_rule(
            rule_id=rid,
            name="delete-me",
            risk_level="low",
            confidence=0.3,
            recommendation="review",
            tool_pattern="temp_tool",
        )
        ok = db.delete_heuristic_rule(rid)
        assert ok is True
        assert db.get_heuristic_rule(rid) is None

    def test_delete_heuristic_rule_not_found(self, db: SQLiteBackend) -> None:
        ok = db.delete_heuristic_rule("nonexistent")
        assert ok is False

    def test_create_duplicate_id_noop(self, db: SQLiteBackend) -> None:
        rid = _make_id()
        db.create_heuristic_rule(
            rule_id=rid,
            name="first-insert",
            risk_level="high",
            confidence=0.8,
            recommendation="approve",
            tool_pattern="tool_orig",
        )
        # Second insert with same ID should be no-op (OR IGNORE)
        db.create_heuristic_rule(
            rule_id=rid,
            name="second-insert",
            risk_level="low",
            confidence=0.1,
            recommendation="deny",
            tool_pattern="tool_new",
        )
        r = db.get_heuristic_rule(rid)
        assert r is not None
        assert r["name"] == "first-insert"  # original preserved
        assert r["risk_level"] == "high"

    def test_defaults(self, db: SQLiteBackend) -> None:
        """Verify default values for optional fields."""
        rid = _make_id()
        db.create_heuristic_rule(
            rule_id=rid,
            name="defaults-test",
            risk_level="medium",
            confidence=0.5,
            recommendation="review",
            tool_pattern="some_tool",
        )
        r = db.get_heuristic_rule(rid)
        assert r is not None
        assert r["arg_patterns"] == "[]"
        assert r["intent_template"] == ""
        assert r["reasoning_template"] == ""
        assert r["tier"] == "medium"
        assert r["priority"] == 0
        assert r["builtin"] is False
        assert r["enabled"] is True
        assert r["created_by"] == ""


class TestOutputGuardPatternStorage:
    def test_create_and_get_output_guard_pattern(self, db: SQLiteBackend) -> None:
        pid = _make_id()
        db.create_output_guard_pattern(
            pattern_id=pid,
            name="aws-key-pattern",
            category="credentials",
            risk_level="high",
            pattern=r"AKIA[0-9A-Z]{16}",
            flag_name="aws_access_key",
            annotation="AWS access key detected",
            pattern_flags="IGNORECASE",
            is_credential=True,
            redact_label="[AWS_KEY]",
            priority=100,
            builtin=True,
            enabled=True,
            created_by="system",
        )
        p = db.get_output_guard_pattern(pid)
        assert p is not None
        assert p["pattern_id"] == pid
        assert p["name"] == "aws-key-pattern"
        assert p["category"] == "credentials"
        assert p["risk_level"] == "high"
        assert p["pattern"] == r"AKIA[0-9A-Z]{16}"
        assert p["flag_name"] == "aws_access_key"
        assert p["annotation"] == "AWS access key detected"
        assert p["pattern_flags"] == "IGNORECASE"
        assert p["is_credential"] is True
        assert p["redact_label"] == "[AWS_KEY]"
        assert p["priority"] == 100
        assert p["builtin"] is True
        assert p["enabled"] is True
        assert p["created_by"] == "system"

    def test_get_output_guard_pattern_by_name(self, db: SQLiteBackend) -> None:
        pid = _make_id()
        db.create_output_guard_pattern(
            pattern_id=pid,
            name="lookup-by-name",
            category="credentials",
            risk_level="high",
            pattern=r"ghp_[A-Za-z0-9_]{36}",
            flag_name="github_pat",
            annotation="GitHub PAT detected",
        )
        p = db.get_output_guard_pattern_by_name("lookup-by-name")
        assert p is not None
        assert p["pattern_id"] == pid
        assert p["name"] == "lookup-by-name"

    def test_get_output_guard_pattern_by_name_not_found(self, db: SQLiteBackend) -> None:
        assert db.get_output_guard_pattern_by_name("nonexistent") is None

    def test_list_output_guard_patterns(self, db: SQLiteBackend) -> None:
        db.create_output_guard_pattern(
            pattern_id=_make_id(),
            name="secrets-high",
            category="credentials",
            risk_level="high",
            pattern=r"secret_.*",
            flag_name="generic_secret",
            annotation="Secret detected",
            priority=50,
        )
        db.create_output_guard_pattern(
            pattern_id=_make_id(),
            name="credentials-high",
            category="credentials",
            risk_level="high",
            pattern=r"password=.*",
            flag_name="password",
            annotation="Password detected",
            priority=100,
        )
        db.create_output_guard_pattern(
            pattern_id=_make_id(),
            name="credentials-low",
            category="credentials",
            risk_level="low",
            pattern=r"token=test",
            flag_name="test_token",
            annotation="Test token",
            priority=10,
        )
        patterns = db.list_output_guard_patterns()
        assert len(patterns) == 3
        # Ordered by category then priority desc
        assert patterns[0]["name"] == "credentials-high"
        assert patterns[1]["name"] == "secrets-high"
        assert patterns[2]["name"] == "credentials-low"

    def test_list_output_guard_patterns_enabled_only(self, db: SQLiteBackend) -> None:
        db.create_output_guard_pattern(
            pattern_id=_make_id(),
            name="active-pattern",
            category="credentials",
            risk_level="high",
            pattern=r"AKIA.*",
            flag_name="aws_key",
            annotation="AWS key",
            enabled=True,
        )
        db.create_output_guard_pattern(
            pattern_id=_make_id(),
            name="inactive-pattern",
            category="credentials",
            risk_level="low",
            pattern=r"test_.*",
            flag_name="test",
            annotation="Test pattern",
            enabled=False,
        )
        enabled = db.list_output_guard_patterns(enabled_only=True)
        assert len(enabled) == 1
        assert enabled[0]["name"] == "active-pattern"
        assert enabled[0]["enabled"] is True

    def test_update_output_guard_pattern(self, db: SQLiteBackend) -> None:
        pid = _make_id()
        db.create_output_guard_pattern(
            pattern_id=pid,
            name="orig-pattern",
            category="credentials",
            risk_level="medium",
            pattern=r"old_pattern",
            flag_name="old_flag",
            annotation="Old annotation",
            is_credential=False,
        )
        ok = db.update_output_guard_pattern(
            pid,
            name="updated-pattern",
            category="credentials",
            risk_level="high",
            pattern=r"new_pattern",
            flag_name="new_flag",
            annotation="Updated annotation",
            is_credential=True,
            enabled=False,
            builtin=True,
        )
        assert ok is True
        p = db.get_output_guard_pattern(pid)
        assert p is not None
        assert p["name"] == "updated-pattern"
        assert p["category"] == "credentials"
        assert p["risk_level"] == "high"
        assert p["pattern"] == r"new_pattern"
        assert p["flag_name"] == "new_flag"
        assert p["annotation"] == "Updated annotation"
        assert p["is_credential"] is True
        assert p["enabled"] is False
        assert p["builtin"] is True

    def test_update_output_guard_pattern_not_found(self, db: SQLiteBackend) -> None:
        ok = db.update_output_guard_pattern("nonexistent", name="x")
        assert ok is False

    def test_delete_output_guard_pattern(self, db: SQLiteBackend) -> None:
        pid = _make_id()
        db.create_output_guard_pattern(
            pattern_id=pid,
            name="delete-me",
            category="credentials",
            risk_level="low",
            pattern=r"temp",
            flag_name="temp_flag",
            annotation="Temporary",
        )
        ok = db.delete_output_guard_pattern(pid)
        assert ok is True
        assert db.get_output_guard_pattern(pid) is None

    def test_delete_output_guard_pattern_not_found(self, db: SQLiteBackend) -> None:
        ok = db.delete_output_guard_pattern("nonexistent")
        assert ok is False

    def test_defaults(self, db: SQLiteBackend) -> None:
        """Verify default values for optional fields."""
        pid = _make_id()
        db.create_output_guard_pattern(
            pattern_id=pid,
            name="defaults-test",
            category="credentials",
            risk_level="medium",
            pattern=r"some_pattern",
            flag_name="some_flag",
            annotation="Some annotation",
        )
        p = db.get_output_guard_pattern(pid)
        assert p is not None
        assert p["pattern_flags"] == ""
        assert p["is_credential"] is False
        assert p["redact_label"] == ""
        assert p["priority"] == 0
        assert p["builtin"] is False
        assert p["enabled"] is True
        assert p["created_by"] == ""
