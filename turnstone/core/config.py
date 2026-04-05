"""Unified configuration for turnstone.

Loads config.toml and applies values as argparse defaults.
Precedence: CLI args > config file > hardcoded defaults.

Config file resolution:
  1. ``--config PATH`` CLI flag (via ``add_config_arg`` pre-parser)
  2. ``$TURNSTONE_CONFIG`` environment variable
  3. ``~/.config/turnstone/config.toml`` (default)
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    import argparse

log = get_logger(__name__)

CONFIG_DIR = Path("~/.config/turnstone").expanduser()
_DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.toml"

# Resolved config path — set by set_config_path() or $TURNSTONE_CONFIG
_config_path: Path | None = None

# Cache: None = not loaded yet, {} = loaded but empty/missing
_cache: dict[str, Any] | None = None


def _resolve_config_path() -> Path:
    """Return the effective config file path."""
    if _config_path is not None:
        return _config_path
    env = os.environ.get("TURNSTONE_CONFIG", "").strip()
    if env:
        return Path(env).expanduser()
    return _DEFAULT_CONFIG_PATH


def set_config_path(path: str) -> None:
    """Override the config file path.

    Invalidates the cache so subsequent ``load_config()`` calls re-read
    from the new path.  Typically called from ``add_config_arg()``.
    """
    global _config_path, _cache
    _config_path = Path(path).expanduser()
    _cache = None  # invalidate cache so next load_config() re-reads


def load_config(section: str | None = None) -> dict[str, Any]:
    """Load config.toml and return the full dict or a specific section.

    Returns empty dict if file doesn't exist or can't be parsed.
    Result is cached after first call.
    """
    global _cache
    if _cache is None:
        _cache = {}
        cfg_path = _resolve_config_path()
        if cfg_path.is_file():
            try:
                _cache = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("Failed to parse %s: %s", cfg_path, exc)
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
        "web_search_backend": "web_search_backend",
    },
    "server": {
        "host": "host",
        "port": "port",
        "workstream_idle_timeout": "workstream_idle_timeout",
        "max_workstreams": "max_workstreams",
    },
    "console": {
        "host": "host",
        "port": "port",
        "url": "console_url",
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
        "failure_threshold": "health_failure_threshold",
    },
    "database": {
        "backend": "db_backend",
        "url": "db_url",
        "path": "db_path",
        "pool_size": "db_pool_size",
        "sslmode": "db_sslmode",
        "sslrootcert": "db_sslrootcert",
        "sslcert": "db_sslcert",
        "sslkey": "db_sslkey",
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


def add_config_arg(parser: argparse.ArgumentParser) -> None:
    """Add ``--config`` to *parser* and resolve the path before returning.

    Uses a separate pre-parser (``add_help=False``) so ``--help`` on the
    main parser still works and shows config-derived defaults.
    """
    import argparse as _ap
    import sys

    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="Path to config.toml (default: $TURNSTONE_CONFIG or ~/.config/turnstone/config.toml)",
    )
    # Pre-parse only --config without intercepting --help
    pre = _ap.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=None)
    pre_args, _ = pre.parse_known_args(sys.argv[1:])
    if pre_args.config:
        set_config_path(pre_args.config)


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
