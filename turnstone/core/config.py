"""Unified configuration for turnstone.

Loads ``~/.config/turnstone/config.toml`` and applies values as argparse defaults.
Precedence: CLI args > env vars > config file > hardcoded defaults.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import argparse

log = logging.getLogger(__name__)

CONFIG_DIR = Path("~/.config/turnstone").expanduser()
CONFIG_PATH = CONFIG_DIR / "config.toml"

# Cache: None = not loaded yet, {} = loaded but empty/missing
_cache: dict[str, Any] | None = None


def load_config(section: str | None = None) -> dict[str, Any]:
    """Load config.toml and return the full dict or a specific section.

    Returns empty dict if file doesn't exist or can't be parsed.
    Result is cached after first call.
    """
    global _cache
    if _cache is None:
        _cache = {}
        if CONFIG_PATH.is_file():
            try:
                _cache = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("Failed to parse %s: %s", CONFIG_PATH, exc)
    if section:
        result = _cache.get(section, {})
        return result if isinstance(result, dict) else {}
    return _cache


# ---------------------------------------------------------------------------
# Section → {config_key: argparse_dest}
# ---------------------------------------------------------------------------

_CONFIG_MAP: dict[str, dict[str, str]] = {
    "api": {
        "base_url": "base_url",
        "api_key": "api_key",
    },
    "model": {
        "name": "model",
        "temperature": "temperature",
        "max_tokens": "max_tokens",
        "reasoning_effort": "reasoning_effort",
        "context_window": "context_window",
    },
    "session": {
        "instructions": "instructions",
        "retention_days": "retention_days",
        "compact_max_tokens": "compact_max_tokens",
        "auto_compact_pct": "auto_compact_pct",
    },
    "tools": {
        "timeout": "tool_timeout",
        "truncation": "tool_truncation",
        "agent_max_turns": "agent_max_turns",
        "skip_permissions": "skip_permissions",
        "search": "tool_search",
        "search_threshold": "tool_search_threshold",
        "search_max_results": "tool_search_max_results",
    },
    "server": {
        "host": "host",
        "port": "port",
        "workstream_idle_timeout": "workstream_idle_timeout",
        "max_workstreams": "max_workstreams",
    },
    "bridge": {
        "server_url": "server_url",
        "node_id": "node_id",
        "approval_timeout": "approval_timeout",
        "heartbeat_ttl": "heartbeat_ttl",
        "log_level": "log_level",
    },
    "redis": {
        "host": "redis_host",
        "port": "redis_port",
        "password": "redis_password",
        "db": "redis_db",
    },
    "console": {
        "host": "host",
        "port": "port",
        "url": "console_url",
        "poll_interval": "poll_interval",
        "log_level": "log_level",
    },
    "auth": {
        "token": "auth_token",
    },
    "mcp": {
        "config_path": "mcp_config",
        "refresh_interval": "mcp_refresh_interval",
    },
    "ratelimit": {
        "enabled": "ratelimit_enabled",
        "requests_per_second": "ratelimit_rps",
        "burst": "ratelimit_burst",
        "trusted_proxies": "ratelimit_trusted_proxies",
    },
    "health": {
        "backend_probe_interval": "health_probe_interval",
        "backend_probe_timeout": "health_probe_timeout",
        "circuit_breaker_threshold": "circuit_breaker_threshold",
        "circuit_breaker_cooldown": "circuit_breaker_cooldown",
    },
    "database": {
        "backend": "db_backend",
        "url": "db_url",
        "path": "db_path",
        "pool_size": "db_pool_size",
    },
    "judge": {
        "enabled": "judge_enabled",
        "model": "judge_model",
        "provider": "judge_provider",
        "base_url": "judge_base_url",
        "api_key": "judge_api_key",
        "confidence_threshold": "judge_confidence",
        "max_context_ratio": "judge_context_ratio",
        "timeout": "judge_timeout",
        "read_only_tools": "judge_read_only_tools",
    },
    "memory": {
        "relevance_k": "memory_relevance_k",
        "fetch_limit": "memory_fetch_limit",
        "max_content": "memory_max_content",
        "nudge_cooldown": "memory_nudge_cooldown",
        "nudges": "memory_nudges",
    },
}

# -- Tavily API key (cached) --------------------------------------------------

_tavily_key: str | None = None
_tavily_key_loaded: bool = False


def get_tavily_key() -> str | None:
    """Load Tavily API key (cached after first call).

    Precedence: config.toml [api] tavily_key -> $TAVILY_API_KEY
    """
    import os

    global _tavily_key, _tavily_key_loaded
    if _tavily_key_loaded:
        return _tavily_key
    _tavily_key_loaded = True
    cfg_key = load_config("api").get("tavily_key", "").strip()
    if cfg_key:
        _tavily_key = cfg_key
        return _tavily_key
    env_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if env_key:
        _tavily_key = env_key
    return _tavily_key


def nonneg_float(val: str) -> float:
    """Argparse type for non-negative floats (``>= 0``)."""
    f = float(val)
    if f < 0:
        import argparse

        raise argparse.ArgumentTypeError("must be >= 0")
    return f


def apply_config(parser: argparse.ArgumentParser, sections: list[str]) -> None:
    """Set argparse defaults from config file.

    Only sets defaults for keys present in the config file.
    Called before ``parse_args()`` so CLI flags still override.
    """
    cfg = load_config()
    if not cfg:
        return
    defaults: dict[str, object] = {}
    for section in sections:
        mapping = _CONFIG_MAP.get(section, {})
        section_data = cfg.get(section, {})
        for config_key, argparse_dest in mapping.items():
            if config_key in section_data:
                defaults[argparse_dest] = section_data[config_key]
    if defaults:
        parser.set_defaults(**defaults)


def warn_migrated_settings() -> None:
    """Log warnings for config.toml keys that are now managed by ConfigStore.

    Called after storage is initialized so the warning can direct users
    to the Settings API.  Only relevant for server/console entry points
    that use ConfigStore — the CLI still reads config.toml directly.
    """
    from turnstone.core.settings_registry import SETTINGS

    cfg = load_config()
    if not cfg:
        return

    for key, defn in SETTINGS.items():
        section = defn.section
        config_key = key.split(".", 1)[1]
        section_data = cfg.get(section, {})
        if isinstance(section_data, dict) and config_key in section_data:
            log.warning(
                "config.toml [%s] %s is now managed via Settings API — "
                "this value will be ignored. Use the admin Settings tab "
                "or PUT /v1/api/admin/settings/%s to configure.",
                section,
                config_key,
                key,
            )
