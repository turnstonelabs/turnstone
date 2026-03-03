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
        "retention_days": "session_retention_days",
        "compact_max_tokens": "compact_max_tokens",
        "auto_compact_pct": "auto_compact_pct",
    },
    "tools": {
        "timeout": "tool_timeout",
        "truncation": "tool_truncation",
        "agent_max_turns": "agent_max_turns",
        "skip_permissions": "skip_permissions",
    },
    "server": {
        "host": "host",
        "port": "port",
        "workstream_idle_timeout": "workstream_idle_timeout",
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
}


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
