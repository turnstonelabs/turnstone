"""Settings registry — code-defined catalog of database-storable configuration settings.

Every setting that can be stored in the ``system_settings`` table must
have an entry here.  Unknown keys are rejected at the API boundary.
Bootstrap settings (database, Redis, auth, server/console bind) are
excluded — they are needed before storage is available.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SettingDef:
    """Definition of a single configuration setting."""

    key: str  # dotted path: "memory.relevance_k"
    type: str  # "int" | "float" | "str" | "bool"
    default: Any
    description: str
    section: str  # TOML section name
    is_secret: bool = False
    min_value: float | None = None
    max_value: float | None = None
    choices: list[str] | None = field(default=None, hash=False)
    restart_required: bool = False


def _build_registry() -> dict[str, SettingDef]:
    """Build the settings registry from declarative definitions."""
    defs: list[SettingDef] = [
        # -- model ----------------------------------------------------------
        SettingDef("model.name", "str", "", "Default model name", "model"),
        SettingDef(
            "model.temperature",
            "float",
            0.5,
            "Sampling temperature",
            "model",
            min_value=0.0,
            max_value=2.0,
        ),
        SettingDef(
            "model.max_tokens",
            "int",
            32768,
            "Max output tokens",
            "model",
            min_value=1,
        ),
        SettingDef(
            "model.reasoning_effort",
            "str",
            "medium",
            "Reasoning effort level",
            "model",
            choices=["", "none", "minimal", "low", "medium", "high", "xhigh", "max"],
        ),
        SettingDef(
            "model.context_window",
            "int",
            131072,
            "Context window size in tokens",
            "model",
            min_value=1024,
        ),
        # -- session --------------------------------------------------------
        SettingDef("session.instructions", "str", "", "Default system instructions", "session"),
        SettingDef(
            "session.retention_days",
            "int",
            90,
            "Days to retain conversation history (0 = disabled)",
            "session",
            min_value=0,
        ),
        SettingDef(
            "session.compact_max_tokens",
            "int",
            32768,
            "Max tokens for compaction summary",
            "session",
            min_value=0,
        ),
        SettingDef(
            "session.auto_compact_pct",
            "float",
            0.8,
            "Auto-compact at this fraction of context window (0 = disabled)",
            "session",
            min_value=0.0,
            max_value=1.0,
        ),
        # -- tools ----------------------------------------------------------
        SettingDef(
            "tools.timeout",
            "int",
            120,
            "Tool execution timeout in seconds",
            "tools",
            min_value=1,
            max_value=3600,
        ),
        SettingDef(
            "tools.truncation",
            "int",
            0,
            "Tool output truncation limit in chars (0 = auto, 50% of context window)",
            "tools",
            min_value=0,
        ),
        SettingDef(
            "tools.agent_max_turns",
            "int",
            -1,
            "Max turns for plan/task agents (-1 = unlimited)",
            "tools",
            min_value=-1,
            max_value=200,
        ),
        SettingDef("tools.skip_permissions", "bool", False, "Skip tool approval prompts", "tools"),
        SettingDef(
            "tools.search",
            "str",
            "auto",
            "Tool search mode",
            "tools",
            choices=["auto", "on", "off"],
        ),
        SettingDef(
            "tools.search_threshold",
            "int",
            20,
            "Min tool count to enable search",
            "tools",
            min_value=1,
        ),
        SettingDef(
            "tools.search_max_results",
            "int",
            5,
            "Max tool search results",
            "tools",
            min_value=1,
            max_value=50,
        ),
        # -- server ---------------------------------------------------------
        SettingDef(
            "server.workstream_idle_timeout",
            "int",
            120,
            "Idle timeout for workstream eviction in minutes (0 = disabled)",
            "server",
            min_value=0,
            restart_required=True,
        ),
        SettingDef(
            "server.max_workstreams",
            "int",
            10,
            "Max concurrent workstreams",
            "server",
            min_value=1,
            restart_required=True,
        ),
        # -- mcp ------------------------------------------------------------
        SettingDef(
            "mcp.config_path",
            "str",
            "",
            "Path to MCP server configuration file",
            "mcp",
            restart_required=True,
        ),
        SettingDef(
            "mcp.refresh_interval",
            "int",
            14400,
            "MCP resource/prompt refresh interval in seconds (0 = disabled, default 4h)",
            "mcp",
            min_value=0,
        ),
        # -- ratelimit ------------------------------------------------------
        SettingDef(
            "ratelimit.enabled",
            "bool",
            False,
            "Enable per-IP rate limiting",
            "ratelimit",
            restart_required=True,
        ),
        SettingDef(
            "ratelimit.requests_per_second",
            "float",
            10.0,
            "Max requests per second per IP",
            "ratelimit",
            min_value=1.0,
            restart_required=True,
        ),
        SettingDef(
            "ratelimit.burst",
            "int",
            20,
            "Burst allowance above rate limit",
            "ratelimit",
            min_value=1,
            restart_required=True,
        ),
        SettingDef(
            "ratelimit.trusted_proxies",
            "str",
            "",
            "Trusted proxy CIDRs for X-Forwarded-For parsing (comma-separated)",
            "ratelimit",
            restart_required=True,
        ),
        # -- health ---------------------------------------------------------
        SettingDef(
            "health.backend_probe_interval",
            "int",
            30,
            "Backend health probe interval in seconds",
            "health",
            min_value=5,
        ),
        SettingDef(
            "health.backend_probe_timeout",
            "int",
            5,
            "Backend health probe timeout in seconds",
            "health",
            min_value=1,
        ),
        SettingDef(
            "health.circuit_breaker_threshold",
            "int",
            5,
            "Consecutive failures before circuit opens",
            "health",
            min_value=1,
        ),
        SettingDef(
            "health.circuit_breaker_cooldown",
            "int",
            60,
            "Seconds before half-open retry",
            "health",
            min_value=5,
        ),
        # -- judge ----------------------------------------------------------
        SettingDef("judge.enabled", "bool", True, "Enable intent validation judge", "judge"),
        SettingDef(
            "judge.model",
            "str",
            "",
            "Model for LLM judge (empty = same as session)",
            "judge",
        ),
        SettingDef("judge.provider", "str", "", "Provider for judge model", "judge"),
        SettingDef("judge.base_url", "str", "", "Base URL for judge model API", "judge"),
        SettingDef(
            "judge.api_key",
            "str",
            "",
            "API key for judge model",
            "judge",
            is_secret=True,
        ),
        SettingDef(
            "judge.confidence_threshold",
            "float",
            0.7,
            "Min confidence for judge verdict",
            "judge",
            min_value=0.0,
            max_value=1.0,
        ),
        SettingDef(
            "judge.max_context_ratio",
            "float",
            0.5,
            "Max fraction of context window for judge",
            "judge",
            min_value=0.1,
            max_value=1.0,
        ),
        SettingDef(
            "judge.timeout",
            "float",
            60.0,
            "Judge evaluation timeout in seconds",
            "judge",
            min_value=5.0,
        ),
        SettingDef(
            "judge.read_only_tools", "bool", True, "Restrict judge to read-only tools", "judge"
        ),
        # -- memory ---------------------------------------------------------
        SettingDef(
            "memory.relevance_k",
            "int",
            5,
            "Top-K memories for BM25 injection",
            "memory",
            min_value=1,
            max_value=50,
        ),
        SettingDef(
            "memory.fetch_limit",
            "int",
            50,
            "Max memories fetched from storage",
            "memory",
            min_value=1,
            max_value=500,
        ),
        SettingDef(
            "memory.max_content",
            "int",
            32768,
            "Max memory content size in characters",
            "memory",
            min_value=100,
            max_value=65536,
        ),
        SettingDef(
            "memory.nudge_cooldown",
            "int",
            300,
            "Seconds between metacognitive nudges",
            "memory",
            min_value=0,
        ),
        SettingDef("memory.nudges", "bool", True, "Enable metacognitive nudges", "memory"),
    ]
    return {d.key: d for d in defs}


SETTINGS: dict[str, SettingDef] = _build_registry()

# Sections that are NOT in the registry (bootstrap-critical)
BOOTSTRAP_SECTIONS: frozenset[str] = frozenset(
    {
        "api",
        "database",
        "redis",
        "auth",
        "bridge",
        "console",
    }
)


def validate_key(key: str) -> SettingDef:
    """Return the SettingDef for *key*, or raise ValueError if unknown."""
    defn = SETTINGS.get(key)
    if defn is None:
        raise ValueError(f"Unknown setting: {key}")
    return defn


def validate_value(key: str, raw_value: Any) -> Any:
    """Coerce and validate *raw_value* against the setting definition.

    Returns the typed value.  Raises ValueError on invalid input.
    """
    defn = validate_key(key)

    # Type coercion
    try:
        if defn.type == "int":
            typed: Any = int(raw_value)
        elif defn.type == "float":
            typed = float(raw_value)
        elif defn.type == "bool":
            if isinstance(raw_value, bool):
                typed = raw_value
            elif isinstance(raw_value, str):
                low = raw_value.lower()
                if low in ("true", "1", "yes"):
                    typed = True
                elif low in ("false", "0", "no"):
                    typed = False
                else:
                    raise ValueError(f"Cannot convert {raw_value!r} to bool for {key}")
            else:
                typed = bool(raw_value)
        else:  # str
            typed = "" if raw_value is None else str(raw_value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Cannot convert {raw_value!r} to {defn.type} for {key}") from exc

    # Range validation
    if typed is not None:
        if (
            defn.min_value is not None
            and isinstance(typed, (int, float))
            and typed < defn.min_value
        ):
            raise ValueError(f"{key}: {typed} < minimum {defn.min_value}")
        if (
            defn.max_value is not None
            and isinstance(typed, (int, float))
            and typed > defn.max_value
        ):
            raise ValueError(f"{key}: {typed} > maximum {defn.max_value}")

    # Choices validation
    if defn.choices is not None and typed not in defn.choices:
        raise ValueError(f"{key}: {typed!r} not in {defn.choices}")

    return typed


def serialize_value(value: Any) -> str:
    """JSON-encode a typed value for storage."""
    return json.dumps(value)


def deserialize_value(key: str, json_str: str) -> Any:
    """JSON-decode and type-coerce against registry."""
    raw = json.loads(json_str)
    defn = SETTINGS.get(key)
    if defn is None:
        return raw  # Unknown key — return raw
    return validate_value(key, raw)
