"""Rule registry — thread-safe merged view of built-in + DB rules.

Provides the heuristic rule table and output guard pattern set used by
the intent judge (Facet 1) and output guard (Facet 2).  Built-in rules
are defined in ``judge.py`` and ``output_guard.py``.  Custom rules are
stored in the ``heuristic_rules`` and ``output_guard_patterns`` tables.

Merge strategy (per name):
  - DB row with matching name → replaces built-in
  - DB row with builtin=1, enabled=0 → disables built-in
  - DB row with builtin=0 → new custom rule
  - No DB row → built-in used as-is

The registry is thread-safe: ``reload()`` acquires a lock, rebuilds the
merged view, then atomically swaps the cached snapshots.
"""

from __future__ import annotations

import logging
import re
import threading
import types
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from turnstone.core.output_guard import OutputGuardPatternDef as OutputGuardPatternDef

if TYPE_CHECKING:
    from turnstone.core.storage._protocol import StorageBackend

log = logging.getLogger(__name__)

# -- Public dataclasses ------------------------------------------------------

_TIER_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_RE_FLAGS_MAP = {
    "IGNORECASE": re.IGNORECASE,
    "MULTILINE": re.MULTILINE,
    "DOTALL": re.DOTALL,
}


@dataclass(frozen=True)
class HeuristicRuleDef:
    """A heuristic pattern-matching rule for intent validation."""

    name: str
    risk_level: str  # critical/high/medium/low
    confidence: float  # 0.0-1.0
    recommendation: str  # approve/review/deny
    tool_pattern: str  # fnmatch pattern for func_name
    arg_patterns: list[str]  # regex patterns matched against args
    intent_template: str  # may use {func_name}, {arg_snippet}
    reasoning_template: str
    tier: str  # critical/high/medium/low — evaluation order
    priority: int = 0  # within-tier ordering (higher = first)


def _compile_flags(flags_str: str) -> int:
    """Parse comma-separated flag names into regex flags integer."""
    if not flags_str:
        return 0
    result = 0
    for f in flags_str.split(","):
        f = f.strip()
        if f in _RE_FLAGS_MAP:
            result |= _RE_FLAGS_MAP[f]
    return result


class RuleRegistry:
    """Thread-safe in-memory cache of merged built-in + DB rules.

    When ``storage`` is None (standalone CLI, tests), only built-in rules
    are used.  Call ``reload()`` after admin writes to refresh the cache.
    """

    def __init__(self, storage: StorageBackend | None = None) -> None:
        self._storage = storage
        self._lock = threading.Lock()
        self._heuristic_rules: tuple[HeuristicRuleDef, ...] = ()
        self._output_patterns: dict[str, tuple[OutputGuardPatternDef, ...]] = {}
        self._version = 0
        self.reload()

    def reload(self) -> None:
        """Re-read DB, merge with built-ins, and swap cache atomically."""
        h_rules = self._merge_heuristic_rules()
        o_patterns = self._merge_output_patterns()
        with self._lock:
            self._heuristic_rules = tuple(h_rules)
            self._output_patterns = {cat: tuple(pats) for cat, pats in o_patterns.items()}
            self._version += 1

    @property
    def heuristic_rules(self) -> tuple[HeuristicRuleDef, ...]:
        """Immutable snapshot of merged heuristic rules."""
        return self._heuristic_rules

    @property
    def output_patterns(
        self,
    ) -> types.MappingProxyType[str, tuple[OutputGuardPatternDef, ...]]:
        """Immutable snapshot of output guard patterns grouped by category."""
        return types.MappingProxyType(self._output_patterns)

    @property
    def version(self) -> int:
        """Monotonic counter incremented on each reload."""
        return self._version

    # -- Merge logic -----------------------------------------------------------

    def _merge_heuristic_rules(self) -> list[HeuristicRuleDef]:
        """Merge built-in heuristic rules with DB overrides/custom rules."""
        from turnstone.core.judge import _HEURISTIC_RULES

        # Start with built-ins keyed by name
        by_name: dict[str, HeuristicRuleDef] = {}
        for rule in _HEURISTIC_RULES:
            by_name[rule.name] = HeuristicRuleDef(
                name=rule.name,
                risk_level=rule.risk_level,
                confidence=rule.confidence,
                recommendation=rule.recommendation,
                tool_pattern=rule.tool_pattern,
                arg_patterns=list(rule.arg_patterns),
                intent_template=rule.intent_template,
                reasoning_template=rule.reasoning_template,
                tier=rule.risk_level,  # built-in tier = risk_level
                priority=0,
            )

        if self._storage is None:
            return self._sort_heuristic(list(by_name.values()))

        # Overlay DB rules
        try:
            db_rules = self._storage.list_heuristic_rules()
        except Exception:
            log.exception("Failed to load heuristic rules from storage")
            return self._sort_heuristic(list(by_name.values()))

        disabled_builtins: set[str] = set()
        for row in db_rules:
            name = row["name"]
            if row.get("builtin") and not row.get("enabled"):
                disabled_builtins.add(name)
                continue
            if not row.get("enabled"):
                continue
            import json

            arg_patterns_raw: Any = row.get("arg_patterns", "[]")
            if isinstance(arg_patterns_raw, str):
                try:
                    arg_patterns_raw = json.loads(arg_patterns_raw)
                except (json.JSONDecodeError, TypeError):
                    arg_patterns_raw = []
            by_name[name] = HeuristicRuleDef(
                name=name,
                risk_level=row.get("risk_level", "medium"),
                confidence=row.get("confidence", 0.7),
                recommendation=row.get("recommendation", "review"),
                tool_pattern=row.get("tool_pattern", "*"),
                arg_patterns=arg_patterns_raw,
                intent_template=row.get("intent_template", ""),
                reasoning_template=row.get("reasoning_template", ""),
                tier=row.get("tier", "medium"),
                priority=row.get("priority", 0),
            )

        for name in disabled_builtins:
            by_name.pop(name, None)

        return self._sort_heuristic(list(by_name.values()))

    @staticmethod
    def _sort_heuristic(rules: list[HeuristicRuleDef]) -> list[HeuristicRuleDef]:
        """Sort: critical first, then high, medium, low; within tier by priority desc."""
        return sorted(
            rules,
            key=lambda r: (_TIER_ORDER.get(r.tier, 4), -r.priority),
        )

    def _merge_output_patterns(self) -> dict[str, list[OutputGuardPatternDef]]:
        """Merge built-in output guard patterns with DB overrides/custom patterns."""
        from turnstone.core.output_guard import _BUILTIN_OG_PATTERNS

        by_name: dict[str, OutputGuardPatternDef] = {}
        for pat in _BUILTIN_OG_PATTERNS:
            by_name[pat.name] = pat

        if self._storage is None:
            return self._group_by_category(list(by_name.values()))

        try:
            db_patterns = self._storage.list_output_guard_patterns()
        except Exception:
            log.exception("Failed to load output guard patterns from storage")
            return self._group_by_category(list(by_name.values()))

        disabled_builtins: set[str] = set()
        for row in db_patterns:
            name = row["name"]
            if row.get("builtin") and not row.get("enabled"):
                disabled_builtins.add(name)
                continue
            if not row.get("enabled"):
                continue
            try:
                flags_int = _compile_flags(row.get("pattern_flags", ""))
                compiled = re.compile(row["pattern"], flags_int)
            except re.error:
                log.warning("Invalid regex in output guard pattern %r, skipping", name)
                continue
            by_name[name] = OutputGuardPatternDef(
                name=name,
                category=row.get("category", "info_disclosure"),
                risk_level=row.get("risk_level", "medium"),
                compiled=compiled,
                flag_name=row.get("flag_name", name),
                annotation=row.get("annotation", ""),
                is_credential=bool(row.get("is_credential")),
                redact_label=row.get("redact_label", ""),
                priority=row.get("priority", 0),
            )

        for name in disabled_builtins:
            by_name.pop(name, None)

        return self._group_by_category(list(by_name.values()))

    @staticmethod
    def _group_by_category(
        patterns: list[OutputGuardPatternDef],
    ) -> dict[str, list[OutputGuardPatternDef]]:
        """Group patterns by category, sorted by priority desc within each."""
        grouped: dict[str, list[OutputGuardPatternDef]] = {}
        for pat in patterns:
            grouped.setdefault(pat.category, []).append(pat)
        for cat in grouped:
            grouped[cat].sort(key=lambda p: -p.priority)
        return grouped
