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


def _named_config_path() -> Path | None:
    """Return the path named by ``--config`` or ``TURNSTONE_CONFIG``, or None for the default."""
    if _config_path is not None:
        return _config_path
    env = os.environ.get("TURNSTONE_CONFIG", "").strip()
    if env:
        return Path(env).expanduser()
    return None


def _resolve_config_path() -> Path:
    """Return the effective config file path."""
    named = _named_config_path()
    return named if named is not None else _DEFAULT_CONFIG_PATH


def config_file_path() -> Path:
    """Return the config.toml path this process reads (it need not exist)."""
    return _resolve_config_path()


def set_config_path(path: str) -> None:
    """Override the config file path.

    Invalidates the cache so subsequent ``load_config()`` calls re-read
    from the new path.  Typically called from ``add_config_arg()``.
    """
    global _config_path, _cache
    _config_path = Path(path).expanduser()
    _cache = None  # invalidate cache so next load_config() re-reads


class ConfigError(RuntimeError):
    """config.toml cannot be read or parsed, or a named path does not exist."""


def load_config(section: str | None = None) -> dict[str, Any]:
    """Load config.toml and return the full dict or a specific section.

    The default path may be absent, which is an empty configuration. A path named by
    ``--config`` or ``TURNSTONE_CONFIG`` that does not exist, and a file that exists but
    cannot be read or parsed, raise :class:`ConfigError`: a process must not run with none
    of its settings as though the file were absent. The result is cached after the first
    successful load; a failure leaves nothing cached, so every caller fails the same way.
    """
    global _cache
    if _cache is None:
        named = _named_config_path()
        cfg_path = named if named is not None else _DEFAULT_CONFIG_PATH
        # The read decides: is_file() answers False when a parent directory
        # cannot be entered, which would turn a permission problem into an
        # empty configuration.
        try:
            text: str | None = cfg_path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError) as exc:
            if named is not None:
                # Named by --config or TURNSTONE_CONFIG: a path that is not there is
                # a mistake, not an empty configuration. Only the default path may be
                # absent.
                log.error("config.missing", path=str(cfg_path), error=str(exc))
                raise ConfigError(f"{cfg_path}: {exc}") from exc
            text = None
        except (OSError, UnicodeDecodeError) as exc:
            log.error("config.unreadable", path=str(cfg_path), error=str(exc))
            raise ConfigError(f"{cfg_path}: {exc}") from exc
        loaded: dict[str, Any] = {}
        if text is not None:
            try:
                loaded = tomllib.loads(text)
            except (tomllib.TOMLDecodeError, RecursionError) as exc:
                log.error("config.unparseable", path=str(cfg_path), error=str(exc))
                raise ConfigError(f"{cfg_path}: {exc}") from exc
        _cache = loaded
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
        "listen_url": "db_listen_url",
    },
    "judge": {
        "enabled": "judge_enabled",
        "model": "judge_model",
        "confidence_threshold": "judge_confidence",
        "max_context_ratio": "judge_context_ratio",
        "timeout": "judge_timeout",
        "parallel_evaluations": "judge_parallel_evaluations",
        "read_only_tools": "judge_read_only_tools",
    },
    "memory": {
        "relevance_k": "memory_relevance_k",
        "index_budget_chars": "memory_index_budget_chars",
        "max_content": "memory_max_content",
        "nudge_cooldown": "memory_nudge_cooldown",
        "nudges": "memory_nudges",
    },
}

# -- SearxNG web search (cached) ----------------------------------------------

_searxng_url: str | None = None
_searxng_url_loaded: bool = False
_searxng_engines: str | None = None
_searxng_engines_loaded: bool = False


def get_searxng_url() -> str | None:
    """Load the SearxNG base URL (cached after first call).

    Precedence: config.toml [tools] searxng_url -> $TURNSTONE_SEARXNG_URL.
    Returns None when neither is set (web_search then has no backend unless
    the model supports native search). The docker-compose stack sets the env
    var to the bundled ``searxng`` service.
    """
    global _searxng_url, _searxng_url_loaded
    if _searxng_url_loaded:
        return _searxng_url
    cfg_url = load_config("tools").get("searxng_url", "").strip()
    _searxng_url_loaded = True
    if cfg_url:
        _searxng_url = cfg_url
        return _searxng_url
    env_url = os.environ.get("TURNSTONE_SEARXNG_URL", "").strip()
    if env_url:
        _searxng_url = env_url
    return _searxng_url


def get_searxng_engines() -> str:
    """Load the SearxNG engine list (cached after first call).

    Precedence: config.toml [tools] searxng_engines -> $TURNSTONE_SEARXNG_ENGINES
    -> "" (use the instance's default engine mix). Comma-separated engine names.
    """
    global _searxng_engines, _searxng_engines_loaded
    if _searxng_engines_loaded:
        return _searxng_engines or ""
    cfg = load_config("tools").get("searxng_engines", "").strip()
    _searxng_engines_loaded = True
    _searxng_engines = cfg or os.environ.get("TURNSTONE_SEARXNG_ENGINES", "").strip()
    return _searxng_engines


# -- Workspace directory hint (cached) -----------------------------------------

_workspace_dir: str | None = None
_workspace_dir_loaded: bool = False


def get_workspace_dir() -> str | None:
    """Load the operator-designated workspace directory (cached after first call).

    Precedence: config.toml [tools] workspace_dir -> $TURNSTONE_WORKSPACE.
    Returns None when neither is set.  For model tools, the value is
    rendered into the fs/shell tool descriptions (tools.apply_cwd_context)
    so the model knows where user files live when that differs from the
    process cwd — it does NOT chdir the process, and it does NOT confine
    tool paths. The Files pane also uses this as its workspace root,
    alongside its separate /data and /tmp roots. The Docker image sets
    the env var to its stock ``/workspace`` bind-mount point. Tool path
    confinement and per-workstream
    working directories are a separate planned launch-time grant; keep
    this a plain node-level hint.
    """
    global _workspace_dir, _workspace_dir_loaded
    if _workspace_dir_loaded:
        return _workspace_dir
    cfg_dir = load_config("tools").get("workspace_dir", "").strip()
    _workspace_dir_loaded = True
    if cfg_dir:
        _workspace_dir = cfg_dir
        return _workspace_dir
    env_dir = os.environ.get("TURNSTONE_WORKSPACE", "").strip()
    if env_dir:
        _workspace_dir = env_dir
    return _workspace_dir


# -- Rerank query instruction (cached) ----------------------------------------
# The reranker endpoint itself is a per-model definition (admin Models tab ->
# Reranker role); only the query instruction is a global knob.

_rerank_instruction: str | None = None
_rerank_instruction_loaded: bool = False


def get_rerank_instruction() -> str:
    """Load the rerank query instruction (cached after first call).

    Precedence: config.toml [tools] rerank_instruction -> $TURNSTONE_RERANK_INSTRUCTION
    -> "" (no instruction; a bare query, correct for Cohere/Jina/bge cross-encoders).
    Instruction-aware rerankers (the Qwen3-Reranker family) need it; the client
    then wraps the query with the model's ``<Instruct>:`` / ``<Query>:`` framing.
    """
    global _rerank_instruction, _rerank_instruction_loaded
    if not _rerank_instruction_loaded:
        cfg = load_config("tools").get("rerank_instruction", "").strip()
        _rerank_instruction_loaded = True
        _rerank_instruction = cfg or os.environ.get("TURNSTONE_RERANK_INSTRUCTION", "").strip()
    return _rerank_instruction or ""


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
    Called before ``parse_args()`` so CLI flags still override. A config.toml
    that cannot be read or parsed ends the process here, after the loader has
    logged why; the server, console, CLI, admin, eval and optimizer entry
    points call this before anything else reads the file.
    """
    try:
        cfg = load_config()
    except ConfigError as exc:
        raise SystemExit(1) from exc
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

    # Warn about removed settings whose config.toml keys are now ignored.
    # model.name → use model definitions (Models tab); model.context_window
    # → set per-model in the Models tab (context_window column); [api] →
    # the server has no bootstrap endpoint, every model definition carries
    # its own base_url and api_key (the CLI still reads [api]).
    removed_settings: dict[str, str] = {
        "model.name": "Use model definitions in the Models tab instead.",
        "model.context_window": "Set per-model in the Models tab instead.",
        "api.base_url": (
            "The server takes endpoints from model definitions; set base_url on "
            "each [models.*] entry or in the Models tab (the turnstone CLI still "
            "reads [api])."
        ),
        "api.api_key": (
            "Set api_key on each [models.*] entry or in the Models tab; an empty "
            "key falls back to OPENAI_API_KEY / ANTHROPIC_API_KEY, and a local "
            "server needs none."
        ),
    }
    for key, guidance in removed_settings.items():
        section, config_key = key.split(".", 1)
        section_data = cfg.get(section, {})
        if isinstance(section_data, dict) and config_key in section_data:
            log.warning(
                "config.toml [%s] %s has been removed and will be ignored. %s",
                section,
                config_key,
                guidance,
            )
