"""LLM-backed diagnostic tool for a running Turnstone install.

Entry point: turnstone-doctor

``turnstone-doctor`` inspects a *running* Turnstone deployment and helps the
operator troubleshoot it conversationally.  It is **diagnose-only**: it reads
state (config files, env, ``docker compose ps``, ``systemctl``, ``/health``,
the database) and explains what it finds, recommending the exact commands to
run — it never mutates the system.

Day-0 installation is owned by ``run.sh`` (the one-line installer), not by this
tool.

Startup sequence:

1. **Preflight** — :func:`detect_install_profile` deterministically detects how
   Turnstone is installed here (docker-compose / systemd / pip / git-source) by
   probing for ``config.toml`` files, ``TURNSTONE_*`` env vars, compose files,
   and systemd units.
2. **Self-configuring brain** — :func:`resolve_doctor_brain` powers doctor's own
   LLM from the cluster's *own* configuration (config/env/storage), exactly the
   way a node does.  Whether that succeeds is itself the first diagnostic (the
   LLM-backend verdict).  On failure it falls back to interactive provider
   selection so doctor still runs.
3. **Version check** — :func:`check_versions` reports the installed version,
   cluster version drift (via storage + ``/health``), and the latest upstream
   stable/experimental releases.
4. **Diagnose loop** — the LLM drives read-only diagnostic tools.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import getpass
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from turnstone import __version__
from turnstone.core.env import _is_secret
from turnstone.ui.colors import BOLD, CYAN, DIM, GREEN, RED, RESET, YELLOW
from turnstone.ui.markdown import MarkdownRenderer
from turnstone.ui.spinner import Spinner

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-5.4",
    "anthropic": "claude-sonnet-4-6",
}

# Default HTTP ports the server node and console bind (overridable per install).
DEFAULT_SERVER_PORT = 8080
DEFAULT_CONSOLE_PORT = 8090

# Public release tags for the upstream version check (no auth, no user data sent).
GITHUB_TAGS_URL = "https://api.github.com/repos/turnstonelabs/turnstone/tags"

# Env vars worth surfacing in the preflight report. Secret-named ones (per
# turnstone.core.env._is_secret) are shown as present-but-hidden; the rest have
# any embedded URL credentials redacted.
_RELEVANT_ENV_VARS: tuple[str, ...] = (
    "TURNSTONE_DB_BACKEND",
    "TURNSTONE_DB_URL",
    "TURNSTONE_DB_PATH",
    "TURNSTONE_JWT_SECRET",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "LLM_BASE_URL",
    "MODEL",
    "TURNSTONE_HOST_IP",
    "TURNSTONE_CONSOLE_URL",
    "TURNSTONE_SERVER_URL",
    "TURNSTONE_ADVERTISE_URL",
    "TURNSTONE_NODE_ID",
    "TURNSTONE_DISCORD_TOKEN",
    "TURNSTONE_SLACK_TOKEN",
    "TURNSTONE_IMAGE_TAG",
    "MCP_CONFIG",
    "TURNSTONE_CONFIG",
)

# Lowercased config-file keys that hold secrets even though they don't match the
# _KEY/_SECRET/_TOKEN/_PASSWORD suffix rule in turnstone.core.env._is_secret.
_SECRET_CONFIG_KEYS: frozenset[str] = frozenset(
    {"password", "secret", "api_key", "jwt_secret", "token"}
)

# Model providers doctor's built-in _DoctorLLM can drive, grouped by wire family.
_ANTHROPIC_PROVIDERS: frozenset[str] = frozenset({"anthropic", "anthropic-compatible"})
_OPENAI_PROVIDERS: frozenset[str] = frozenset({"openai", "openai-compatible", "xai"})

# read_file refuses to dump raw key/cert material outright, and caps large reads
# so a big log can't flood the model's context.
_SECRET_FILE_SUFFIXES: tuple[str, ...] = (".pem", ".key", ".crt", ".cer", ".p12", ".pfx")
_READ_MAX_CHARS = 64_000


# ---------------------------------------------------------------------------
# Secret masking (display-only)
# ---------------------------------------------------------------------------

# scheme://user:password@host  ->  scheme://user:****@host
_URL_CRED_RE = re.compile(r"(://[^:/@\s]+:)([^@/\s]+)(@)")
# A config-key token (left of '='/':' in env/TOML/YAML/JSON), optionally quoted.
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")
# A PEM private-key block (any flavour), redacted whole.
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)


def _redact_url_credentials(value: str) -> str:
    """Mask the password in any ``scheme://user:password@host`` URL in *value*."""
    return _URL_CRED_RE.sub(r"\1****\3", value)


def _mask_value(value: str) -> str:
    """Show only the first/last few characters of a secret value."""
    if len(value) > 8:
        return f"{value[:4]}****{value[-4:]}"
    return "****" if value else value


def _mask_line(line: str) -> str:
    """Mask a single ``KEY=value`` / ``key: value`` assignment if it's a secret.

    Uses the earliest of ``=`` / ``:`` as the separator and only treats the line
    as an assignment when the left side is a bare config key (so timestamps,
    URLs, and prose lines pass through untouched). The WHOLE value is masked —
    ``#`` is never treated as a comment, since a ``#`` can sit inside a secret.
    """
    seps = [i for i in (line.find("="), line.find(":")) if i != -1]
    if not seps:
        return line
    idx = min(seps)
    sep = line[idx]
    key_part, val_part = line[:idx], line[idx + 1 :]
    key = key_part.strip().strip("\"'")
    if not _KEY_RE.match(key):
        return line
    raw = val_part.strip()
    if not raw:
        return line
    if _is_secret(key) or key.lower() in _SECRET_CONFIG_KEYS:
        masked = _mask_value(raw.strip("\"',"))
    else:
        masked = _redact_url_credentials(raw)
    lead = val_part[: len(val_part) - len(val_part.lstrip())]
    return f"{key_part}{sep}{lead}{masked}"


def _mask_secrets(text: str) -> str:
    """Redact secrets in ``.env`` / TOML / YAML / JSON-ish text for display.

    Masks values whose key looks like a secret (reusing ``turnstone.core.env._is_secret``
    plus a few bare config key names) and redacts embedded URL credentials on every
    other value, so a database DSN's password never leaks under a non-secret-looking
    key like ``url``.
    """
    out: list[str] = []
    for line in text.split("\n"):
        out.append(line if line.lstrip().startswith("#") else _mask_line(line))
    return "\n".join(out)


def _scrub_tool_output(text: str) -> str:
    """Defang any diagnostic tool result before it reaches the model/console.

    A single chokepoint (applied in :func:`execute_tool`) so every tool — not
    just ``read_file`` — is covered: redacts URL credentials anywhere (DSNs in
    logs/stack traces), drops PEM private-key blocks, and masks secret-keyed
    ``KEY=value`` / ``key: value`` lines (env echoes in logs, config dumps).
    """
    text = _redact_url_credentials(text)
    text = _PEM_RE.sub("[REDACTED PRIVATE KEY]", text)
    return _mask_secrets(text)


def _resolve_safe(project_dir: Path, raw_path: str) -> Path | None:
    """Resolve a path and verify it stays within project_dir. Returns None if unsafe."""
    resolved = (project_dir / raw_path).resolve()
    if not resolved.is_relative_to(project_dir.resolve()):
        return None
    return resolved


# A plain systemd unit name. Model-supplied; must not start with '-' (which
# systemctl would parse as a global option like -H<host> / -M<container>).
_UNIT_RE = re.compile(r"^[A-Za-z0-9@._-]+$")


def _safe_unit(unit: str) -> str | None:
    """Return *unit* if it's a plain unit name, else None (rejects option injection)."""
    unit = unit.strip()
    if unit.startswith("-") or not _UNIT_RE.match(unit):
        return None
    return unit


def _reject_option(value: str) -> str | None:
    """Return *value* unless it would be parsed as a CLI option (leading '-')."""
    value = str(value).strip()
    return None if value.startswith("-") else value


# ---------------------------------------------------------------------------
# Small HTTP / subprocess helpers (read-only)
# ---------------------------------------------------------------------------


def _http_get_json(url: str, timeout: float = 5.0) -> Any:
    """GET *url* and parse JSON. Raises on network/parse failure or unsafe URL.

    Restricts the scheme to http/https (no ``file://``/``ftp://`` from a
    model-supplied URL) and blocks the cloud link-local metadata range —
    loopback and private cluster IPs stay allowed, since probing
    ``http://localhost:PORT/health`` and private node URLs is the job.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"refusing non-http(s) URL: {url!r}")
    host = (parts.hostname or "").lower()
    if host.startswith("169.254.") or host == "metadata.google.internal":
        raise ValueError(f"refusing link-local/metadata host: {host!r}")
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": f"turnstone-doctor/{__version__}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - scheme restricted above
        return json.loads(resp.read().decode("utf-8"))


def _run_readonly(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 20,
    max_chars: int = 8000,
) -> str:
    """Run a read-only command and return its combined output (truncated)."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError:
        return f"Error: '{cmd[0]}' not found on PATH."
    except subprocess.TimeoutExpired:
        return f"Error: '{' '.join(cmd)}' timed out after {timeout}s."
    except OSError as exc:
        return f"Error running {cmd[0]}: {exc}"
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if not out:
        out = f"(no output; exit code {proc.returncode})"
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n... (truncated, {len(out) - max_chars} more chars)"
    return out


# ---------------------------------------------------------------------------
# Preflight: install detection
# ---------------------------------------------------------------------------


@dataclass
class ConfigFileInfo:
    """A discovered ``config.toml`` and the diagnostics-relevant bits of it."""

    path: Path
    sections: list[str]
    db: dict[str, str]  # backend/url/path from [database], if present (real values)
    api: dict[str, str]  # base_url/api_key from [api], if present (real values)


@dataclass
class InstallProfile:
    """Deterministic snapshot of how Turnstone is installed on this machine."""

    project_dir: Path
    kinds: list[str]
    primary_kind: str
    install_source: str  # "source" | "site-packages" | "unknown"
    repo_root: Path | None
    docker_available: bool
    compose_files: list[Path]
    compose_ps: str
    systemd_units: list[str]
    config_files: list[ConfigFileInfo]
    env_present: dict[str, str]
    db_config: dict[str, str]
    health_urls: list[str]
    notes: list[str] = field(default_factory=list)


def _find_repo_root(start: Path) -> Path | None:
    """Walk up from *start* looking for a Turnstone source checkout."""
    cur = start
    for _ in range(6):
        if (cur / ".git").exists() and (cur / "pyproject.toml").is_file():
            try:
                txt = (cur / "pyproject.toml").read_text(encoding="utf-8")
            except OSError:
                txt = ""
            if 'name = "turnstone"' in txt:
                return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def _find_compose_files(
    project_dir: Path, env: dict[str, str], repo_root: Path | None
) -> list[Path]:
    """Find compose files in the project dir, run.sh's checkout, and the repo root."""
    names = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
    td = env.get("TURNSTONE_DIR")
    dirs = [project_dir, Path(td) if td else Path.home() / "turnstone"]
    if repo_root is not None:
        dirs.append(repo_root)
    found: list[Path] = []
    seen: set[Path] = set()
    for d in dirs:
        for n in names:
            try:
                p = d / n
                if p.is_file():
                    rp = p.resolve()
                    if rp not in seen:
                        seen.add(rp)
                        found.append(rp)
            except OSError:
                continue
    return found


def _docker_available() -> bool:
    """True if the Docker daemon is reachable."""
    try:
        proc = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=10)
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _systemd_units() -> list[str]:
    """Return Turnstone systemd service unit names, or [] if none / no systemd."""
    try:
        proc = subprocess.run(
            [
                "systemctl",
                "list-units",
                "--all",
                "--type=service",
                "--no-legend",
                "--plain",
                "turnstone-*",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode != 0:
        return []
    units: list[str] = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if parts and parts[0].endswith(".service"):
            units.append(parts[0])
    return units


def _parse_config_sections(path: Path) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """Return (section names, [database] subset, [api] subset) for a config.toml."""
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return [], {}, {}
    sections = sorted(k for k, v in data.items() if isinstance(v, dict))
    db: dict[str, str] = {}
    dbsec = data.get("database")
    if isinstance(dbsec, dict):
        for k in ("backend", "url", "path", "sslmode", "sslrootcert", "sslcert", "sslkey"):
            v = dbsec.get(k)
            if v:
                db[k] = str(v)
    api: dict[str, str] = {}
    apisec = data.get("api")
    if isinstance(apisec, dict):
        for k in ("base_url", "api_key"):
            v = apisec.get(k)
            if v:
                api[k] = str(v)
    return sections, db, api


def _discover_config_files(project_dir: Path, env: dict[str, str]) -> list[ConfigFileInfo]:
    """Discover config.toml files the way Turnstone resolves them, plus systemd/cwd."""
    candidates: list[Path] = []
    tc = env.get("TURNSTONE_CONFIG")
    if tc:
        candidates.append(Path(tc))
    candidates += [
        Path.home() / ".config" / "turnstone" / "config.toml",
        Path("/etc/turnstone/config.toml"),
        project_dir / "config.toml",
        project_dir / "turnstone.toml",
    ]
    out: list[ConfigFileInfo] = []
    seen: set[Path] = set()
    for p in candidates:
        try:
            if not p.is_file():
                continue
            rp = p.resolve()
        except OSError:
            continue
        if rp in seen:
            continue
        seen.add(rp)
        sections, db, api = _parse_config_sections(rp)
        out.append(ConfigFileInfo(path=rp, sections=sections, db=db, api=api))
    return out


def _resolve_db_config(config_files: list[ConfigFileInfo], env: dict[str, str]) -> dict[str, str]:
    """Resolve DB settings: config.toml [database] > TURNSTONE_DB_* env > defaults."""
    cfg: dict[str, str] = {}
    for cf in config_files:
        if cf.db:
            cfg = dict(cf.db)
            break
    return {
        "backend": cfg.get("backend") or env.get("TURNSTONE_DB_BACKEND") or "sqlite",
        "url": cfg.get("url") or env.get("TURNSTONE_DB_URL") or "",
        "path": cfg.get("path") or env.get("TURNSTONE_DB_PATH") or "",
        # Postgres TLS — needed to reach an SSL/mTLS-required database (mirrors
        # turnstone-admin's _get_storage). Values are file paths, not secrets.
        "sslmode": cfg.get("sslmode") or env.get("TURNSTONE_DB_SSLMODE") or "",
        "sslrootcert": cfg.get("sslrootcert") or env.get("TURNSTONE_DB_SSLROOTCERT") or "",
        "sslcert": cfg.get("sslcert") or env.get("TURNSTONE_DB_SSLCERT") or "",
        "sslkey": cfg.get("sslkey") or env.get("TURNSTONE_DB_SSLKEY") or "",
    }


def _relevant_env(env: dict[str, str]) -> dict[str, str]:
    """Map present, relevant env vars to a redacted display value."""
    out: dict[str, str] = {}
    for name in _RELEVANT_ENV_VARS:
        val = env.get(name)
        if not val:
            continue
        out[name] = "set (hidden)" if _is_secret(name) else _redact_url_credentials(val)
    return out


def _derive_health_urls(env: dict[str, str]) -> list[str]:
    """Best-guess local /health URLs for the server node and console."""
    sp = env.get("TURNSTONE_SERVER_PORT") or str(DEFAULT_SERVER_PORT)
    cp = env.get("TURNSTONE_CONSOLE_PORT") or str(DEFAULT_CONSOLE_PORT)
    return [f"http://localhost:{sp}/health", f"http://localhost:{cp}/health"]


def _primary_kind(kinds: list[str], compose_ps: str) -> str:
    """Pick the most likely primary install kind, favouring what's actually running."""
    running_compose = bool(compose_ps) and bool(
        re.search(r"\b(running|up)\b", compose_ps, re.IGNORECASE)
    )
    if "docker-compose" in kinds and running_compose:
        return "docker-compose"
    for k in ("systemd", "docker-compose", "git-source", "pip"):
        if k in kinds:
            return k
    return "unknown"


def detect_install_profile(project_dir: Path, env: dict[str, str] | None = None) -> InstallProfile:
    """Deterministically detect how Turnstone is installed here. Read-only, no LLM."""
    env = dict(env if env is not None else os.environ)
    kinds: list[str] = []

    import turnstone as _ts

    pkg_dir = Path(_ts.__file__).resolve().parent
    repo_root = _find_repo_root(pkg_dir.parent)
    if {"site-packages", "dist-packages"} & set(pkg_dir.parts):
        install_source = "site-packages"
    elif repo_root is not None:
        install_source = "source"
    else:
        install_source = "unknown"
    if install_source == "source":
        kinds.append("git-source")

    compose_files = _find_compose_files(project_dir, env, repo_root)
    # The systemd probe is independent of the docker chain, so run it concurrently
    # while we walk the (dependent) docker daemon → `compose ps` steps inline.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        units_future = ex.submit(_systemd_units)
        docker_available = _docker_available()
        compose_ps = ""
        if compose_files:
            kinds.append("docker-compose")
            if docker_available:
                compose_ps = _run_readonly(
                    ["docker", "compose", "-f", str(compose_files[0]), "ps", "-a"],
                    cwd=project_dir,
                    timeout=8,
                )
        systemd_units = units_future.result()
    if systemd_units:
        kinds.append("systemd")

    if install_source == "site-packages":
        kinds.append("pip")

    config_files = _discover_config_files(project_dir, env)
    db_config = _resolve_db_config(config_files, env)
    env_present = _relevant_env(env)
    health_urls = _derive_health_urls(env)
    primary = _primary_kind(kinds, compose_ps)

    return InstallProfile(
        project_dir=project_dir,
        kinds=kinds,
        primary_kind=primary,
        install_source=install_source,
        repo_root=repo_root,
        docker_available=docker_available,
        compose_files=compose_files,
        compose_ps=compose_ps,
        systemd_units=systemd_units,
        config_files=config_files,
        env_present=env_present,
        db_config=db_config,
        health_urls=health_urls,
    )


def render_profile_report(profile: InstallProfile) -> str:
    """Render an InstallProfile as a human/LLM-readable block (secrets redacted)."""
    lines: list[str] = ["## Install profile"]
    kinds = ", ".join(profile.kinds) if profile.kinds else "unknown"
    lines.append(f"- Detected kind(s): {kinds}  (primary: {profile.primary_kind})")
    lines.append(f"- Package install source: {profile.install_source}")
    if profile.repo_root:
        lines.append(f"- Source checkout: {profile.repo_root}")
    lines.append(f"- Docker daemon reachable: {'yes' if profile.docker_available else 'no'}")
    if profile.compose_files:
        lines.append("- Compose files:")
        lines += [f"    {p}" for p in profile.compose_files]
    if profile.compose_ps:
        lines.append("- `docker compose ps`:")
        lines += [f"    {ln}" for ln in profile.compose_ps.splitlines()]
    if profile.systemd_units:
        lines.append(f"- systemd units: {', '.join(profile.systemd_units)}")

    if profile.config_files:
        lines.append("- Config files:")
        for cf in profile.config_files:
            secs = f" [{', '.join(cf.sections)}]" if cf.sections else ""
            lines.append(f"    {cf.path}{secs}")
    else:
        lines.append("- Config files: none found")

    db = profile.db_config
    db_url = _redact_url_credentials(db.get("url", "")) if db.get("url") else ""
    db_desc = f"backend={db.get('backend', '?')}"
    if db_url:
        db_desc += f", url={db_url}"
    if db.get("path"):
        db_desc += f", path={db['path']}"
    if db.get("sslmode"):
        db_desc += f", sslmode={db['sslmode']}"
    lines.append(f"- Database: {db_desc}")

    if profile.env_present:
        lines.append("- Relevant env vars:")
        lines += [f"    {k}={v}" for k, v in profile.env_present.items()]
    else:
        lines.append("- Relevant env vars: none set")

    lines.append(f"- Candidate health URLs: {', '.join(profile.health_urls)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Storage (read-only) — shared by the brain (§3) and the version check (§4)
# ---------------------------------------------------------------------------


def open_storage(profile: InstallProfile) -> tuple[Any, str]:
    """Open the cluster's storage read-only (no migrations). Returns (storage, error).

    For SQLite, only an *existing* database file is opened — connecting to a
    missing path would create an empty file, which a diagnose-only tool must
    never do. A missing database is reported as the error (itself a finding).
    """
    db = profile.db_config
    backend = db.get("backend", "sqlite")
    path = db.get("path", "")
    if backend == "sqlite":
        # Only the configured path or the install dir's default — never a stray
        # .turnstone.db from an unrelated cwd.
        candidates = [Path(path)] if path else []
        candidates.append(profile.project_dir / ".turnstone.db")
        existing = next((p for p in candidates if p.is_file()), None)
        if existing is None:
            looked = ", ".join(str(c) for c in candidates)
            return None, f"no SQLite database file found (looked for: {looked})"
        path = str(existing)
    try:
        from turnstone.core.storage import init_storage

        # create_tables=False keeps this strictly read-only: no migrations AND no
        # create_all() DDL against the operator's live database. SSL params are
        # forwarded so an SSL/mTLS-required Postgres is reachable.
        storage = init_storage(
            backend,
            path=path,
            url=db.get("url", ""),
            pool_size=1,
            run_migrations=False,
            create_tables=False,
            sslmode=db.get("sslmode", ""),
            sslrootcert=db.get("sslrootcert", ""),
            sslcert=db.get("sslcert", ""),
            sslkey=db.get("sslkey", ""),
        )
        return storage, ""
    except Exception as exc:  # noqa: BLE001 - any failure is a diagnostic, not fatal
        return None, str(exc)


# ---------------------------------------------------------------------------
# Deterministic version check (§4)
# ---------------------------------------------------------------------------


@dataclass
class VersionReport:
    """Installed/running version, cluster drift, and upstream releases."""

    installed: str
    image_tag: str
    node_versions: dict[str, str]
    cluster_versions: list[str]
    unreachable_nodes: list[str]
    drift: bool
    upstream_stable: str
    upstream_experimental: str
    upstream_error: str
    behind_stable: bool
    behind_experimental: bool
    mtls: bool = False
    console_reachable: bool = False
    console_nodes: int = 0


def _compose_image_tag(profile: InstallProfile) -> str:
    """Read TURNSTONE_IMAGE_TAG from the compose checkout's .env, if present."""
    raw = profile.env_present.get("TURNSTONE_IMAGE_TAG", "")
    if raw:
        return raw
    for d in {p.parent for p in profile.compose_files}:
        envf = d / ".env"
        try:
            if envf.is_file():
                for line in envf.read_text(encoding="utf-8").splitlines():
                    if line.startswith("TURNSTONE_IMAGE_TAG="):
                        return line.partition("=")[2].strip()
        except OSError:
            continue
    return ""


def _probe_health(url: str) -> dict[str, Any] | None:
    """GET ``<url>/health`` and return the parsed JSON dict, or None on failure."""
    target = url.rstrip("/")
    if not target.endswith("/health"):
        target += "/health"
    try:
        data = _http_get_json(target, timeout=4)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    return data if isinstance(data, dict) else None


def _fetch_health_version(url: str) -> str:
    """GET ``<url>/health`` and return its reported version, or '' on failure."""
    data = _probe_health(url)
    return str(data.get("version", "")) if isinstance(data, dict) else ""


def _fetch_upstream_versions(timeout: float = 6.0) -> tuple[str, str, str]:
    """Return (latest_stable, latest_experimental, error) from the upstream tags."""
    try:
        data = _http_get_json(GITHUB_TAGS_URL + "?per_page=100", timeout=timeout)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        return "", "", str(exc)
    if not isinstance(data, list):
        return "", "", "unexpected response from GitHub tags API"
    stable: Version | None = None
    experimental: Version | None = None
    for tag in data:
        name = str(tag.get("name", "")).lstrip("v") if isinstance(tag, dict) else ""
        try:
            v = Version(name)
        except InvalidVersion:
            continue
        if experimental is None or v > experimental:
            experimental = v
        if not v.is_prerelease and (stable is None or v > stable):
            stable = v
    return (str(stable) if stable else ""), (str(experimental) if experimental else ""), ""


def _version_behind(current: str, latest: str) -> bool:
    """True if *current* parses below *latest*. False on missing/unparseable input."""
    if not current or not latest:
        return False
    try:
        return Version(current.lstrip("v")) < Version(latest)
    except InvalidVersion:
        return False


def _tls_cert_dir_present() -> bool:
    """True if a local Turnstone TLS cert dir exists — a node-host mTLS signal."""
    pem_dir = os.environ.get("TURNSTONE_TLS_PEM_DIR") or os.path.join(
        tempfile.gettempdir(), "turnstone-tls"
    )
    try:
        d = Path(pem_dir)
        return d.is_dir() and any(d.glob("lacme-pem-*"))
    except OSError:
        return False


def check_versions(
    profile: InstallProfile, storage: Any, *, offline: bool = False
) -> VersionReport:
    """Deterministic version check: installed, cluster drift, and upstream releases."""
    installed = __version__
    image_tag = _compose_image_tag(profile)

    node_versions: dict[str, str] = {}
    unreachable: list[str] = []
    versions_seen: set[str] = set()
    console_drift = False
    node_urls_https = False

    # Per-node: storage gives the node inventory; each node's /health gives its
    # version (reachable only when node URLs aren't container-internal).
    if storage is not None:
        services: list[dict[str, Any]] = []
        for svc_type in ("server", "console"):
            try:
                services += storage.list_services(svc_type, max_age_seconds=3600)
            except Exception:  # noqa: BLE001 - storage hiccup is itself diagnostic
                continue

        # https advertise URLs ⇒ the cluster runs TLS/mTLS on the node mesh.
        node_urls_https = any(str(s.get("url", "")).startswith("https://") for s in services)

        # Probe nodes concurrently — container-internal URLs each block the full
        # 4s timeout, so serial probing would stall ~N×4s on a large cluster.
        def _probe_node(svc: dict[str, Any]) -> tuple[str, str]:
            url = str(svc.get("url", ""))
            label = str(svc.get("service_id", "")) or url or "?"
            return label, (_fetch_health_version(url) if url else "")

        if services:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(services))) as ex:
                probed = list(ex.map(_probe_node, services))
            for label, ver in probed:
                if ver:
                    node_versions[label] = ver
                    versions_seen.add(ver)
                else:
                    unreachable.append(label)

    # The console (and any reachable candidate /health) reports cluster-wide
    # versions + drift + a live-node count — the authoritative source when node
    # URLs are only reachable inside the container network (the docker-compose case).
    console_reachable = False
    console_nodes = 0
    for url in profile.health_urls:
        data = _probe_health(url)
        if data is None:
            continue
        listed = data.get("versions")
        if isinstance(listed, list):
            versions_seen.update(str(v) for v in listed if v)
            if data.get("version_drift"):
                console_drift = True
        elif data.get("version"):
            versions_seen.add(str(data["version"]))
        if data.get("service") == "turnstone-console" or "versions" in data:
            console_reachable = True
            if isinstance(data.get("nodes"), int):
                console_nodes = max(console_nodes, data["nodes"])

    drift = console_drift or len(versions_seen) > 1
    cluster_versions = sorted(versions_seen)
    # https advertise URLs are authoritative; the local cert dir is only a fallback
    # signal when storage is unreachable (so a stray dir can't false-positive a
    # plain http compose cluster).
    mtls = node_urls_https or (storage is None and _tls_cert_dir_present())

    upstream_stable = upstream_experimental = upstream_error = ""
    if offline:
        upstream_error = "skipped (--offline)"
    else:
        upstream_stable, upstream_experimental, upstream_error = _fetch_upstream_versions()

    return VersionReport(
        installed=installed,
        image_tag=image_tag,
        node_versions=node_versions,
        cluster_versions=cluster_versions,
        unreachable_nodes=unreachable,
        drift=drift,
        upstream_stable=upstream_stable,
        upstream_experimental=upstream_experimental,
        upstream_error=upstream_error,
        behind_stable=_version_behind(installed, upstream_stable),
        behind_experimental=_version_behind(installed, upstream_experimental),
        mtls=mtls,
        console_reachable=console_reachable,
        console_nodes=console_nodes,
    )


def render_version_report(vr: VersionReport) -> str:
    """Render a VersionReport as a human/LLM-readable block."""
    lines: list[str] = ["## Versions"]
    inst = f"- Installed (this tool): {vr.installed}"
    if vr.image_tag:
        inst += f"  (compose image tag: {vr.image_tag})"
    lines.append(inst)

    if vr.cluster_versions:
        lines.append(f"- Cluster versions: {vr.cluster_versions}")
        lines.append(f"- Version drift across nodes: {'YES' if vr.drift else 'no'}")
    else:
        lines.append("- Cluster versions: none reported (no node or console /health reachable)")
    if vr.node_versions:
        lines.append(
            "- Per-node versions: "
            + ", ".join(f"{k}={v}" for k, v in sorted(vr.node_versions.items()))
        )
    if vr.unreachable_nodes:
        reason = (
            "per-node /health requires a client cert (mTLS), which doctor doesn't present"
            if vr.mtls
            else "their advertise URLs are cluster-internal (docker-compose), not host-routable"
        )
        nodes_csv = ", ".join(vr.unreachable_nodes)
        if vr.console_reachable:
            live = f"{vr.console_nodes} node(s) live" if vr.console_nodes else "the cluster live"
            lines.append(
                f"- Per-node /health not reached from the host — {reason}. The console "
                f"reports {live}, so the cluster is healthy (cluster versions above are "
                f"authoritative). Use `node_health` for a specific node: {nodes_csv}."
            )
        else:
            lines.append(
                f"- Registered nodes not reachable, and no console /health to confirm "
                f"health: {nodes_csv} ({reason})."
            )
    elif vr.mtls:
        lines.append("- TLS: cluster appears to run mTLS on the node mesh")

    if vr.upstream_error:
        lines.append(f"- Upstream check: {vr.upstream_error}")
    else:
        parts: list[str] = []
        if vr.upstream_stable:
            tag = " — UPDATE AVAILABLE" if vr.behind_stable else ""
            parts.append(f"stable {vr.upstream_stable}{tag}")
        if vr.upstream_experimental:
            tag = " — newer" if vr.behind_experimental else ""
            parts.append(f"experimental {vr.upstream_experimental}{tag}")
        lines.append("- Upstream: " + (", ".join(parts) if parts else "no tags found"))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-configuring LLM brain (§3)
# ---------------------------------------------------------------------------


@dataclass
class BackendVerdict:
    """Outcome of resolving doctor's brain from the cluster's own config."""

    ok: bool
    detail: str


def family_of(provider: str) -> str | None:
    """Map a model provider to the _DoctorLLM wire family.

    Returns ``"anthropic"`` or ``"openai"``, or ``None`` for providers
    _DoctorLLM cannot drive (e.g. ``"google"``) — the caller then falls back
    to interactive selection.
    """
    p = (provider or "").lower()
    if p in _ANTHROPIC_PROVIDERS:
        return "anthropic"
    if p in _OPENAI_PROVIDERS:
        return "openai"
    return None


def _read_api_creds(profile: InstallProfile, env: dict[str, str]) -> tuple[str, str]:
    """Resolve (base_url, api_key) from config.toml [api] then env, for brain seeding.

    Reads the ``[api]`` block already parsed into each ``ConfigFileInfo`` at
    discovery time (first non-empty wins), so the config files aren't re-opened.
    """
    base_url = api_key = ""
    for cf in profile.config_files:
        base_url = base_url or cf.api.get("base_url", "")
        api_key = api_key or cf.api.get("api_key", "")
    base_url = base_url or env.get("LLM_BASE_URL", "")
    api_key = api_key or env.get("OPENAI_API_KEY", "") or env.get("ANTHROPIC_API_KEY", "")
    return base_url, api_key


def resolve_doctor_brain(
    profile: InstallProfile,
    storage: Any,
    storage_err: str,
    *,
    env: dict[str, str] | None = None,
    validate: bool = True,
) -> tuple[_DoctorLLM | None, BackendVerdict]:
    """Build doctor's LLM from the cluster's config (the first diagnostic).

    Returns (brain, verdict). ``brain`` is None when resolution fails, in which
    case ``verdict.detail`` explains why and the caller should fall back to
    interactive provider selection.
    """
    env = dict(env if env is not None else os.environ)
    if storage is None:
        return None, BackendVerdict(False, f"storage unreachable: {storage_err}")

    try:
        from turnstone.core.config_store import ConfigStore
        from turnstone.core.model_registry import load_model_registry

        cs = ConfigStore(storage=storage, node_id="")
        alias = str(cs.get("model.default_alias", "") or "")
        base_url, api_key = _read_api_creds(profile, env)
        registry = load_model_registry(
            base_url=base_url,
            api_key=api_key,
            model=env.get("MODEL", ""),
            storage=storage,
            allow_empty=True,
        )
        client, model_name, cfg = registry.resolve(alias or None)
    except Exception as exc:  # noqa: BLE001 - any failure means "no usable model"
        return None, BackendVerdict(False, f"no usable model configured: {exc}")

    fam = family_of(cfg.provider)
    if fam is None:
        return None, BackendVerdict(
            False,
            f"auto-config found a {cfg.provider} model ({model_name}); doctor's "
            "built-in brain supports OpenAI/Anthropic-compatible backends — "
            "falling back to interactive selection",
        )

    brain = _DoctorLLM(fam, client, model_name)
    # Redact any embedded credentials in base_url — this string is printed and
    # also fed to the model as context.
    safe_base = _redact_url_credentials(cfg.base_url) or "default endpoint"
    where = f"{model_name} via {cfg.provider} @ {safe_base}"
    if validate:
        ok, err = _validate_connection(brain)
        if not ok:
            return None, BackendVerdict(False, f"resolved {where} but connection failed: {err}")
    return brain, BackendVerdict(True, f"resolved {where}")


def render_backend_verdict(verdict: BackendVerdict) -> str:
    """Render the LLM-backend verdict as a human/LLM-readable block."""
    status = "ok" if verdict.ok else "PROBLEM"
    return f"## LLM backend ({status})\n- {verdict.detail}"


def render_full_report(
    profile: InstallProfile, versions: VersionReport, verdict: BackendVerdict
) -> str:
    """Combine the preflight, version, and backend blocks into one report."""
    return "\n\n".join(
        [
            render_profile_report(profile),
            render_version_report(versions),
            render_backend_verdict(verdict),
        ]
    )


# ---------------------------------------------------------------------------
# Diagnostic tools (read-only) the LLM calls
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a file (config.toml, .env, compose.yaml, etc.) relative to the "
                "install directory. Secret values are masked in the result."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the install dir."}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_port",
            "description": "Check whether a TCP port on localhost has something listening.",
            "parameters": {
                "type": "object",
                "properties": {"port": {"type": "integer", "description": "Port number."}},
                "required": ["port"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_docker",
            "description": "Check whether Docker and the Compose plugin are installed and running.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compose_status",
            "description": (
                "Run `docker compose ps -a` to list the stack's containers and their state. "
                "Optionally pass an explicit compose file path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "compose_file": {
                        "type": "string",
                        "description": "Optional path to a compose file (-f).",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compose_logs",
            "description": (
                "Show recent `docker compose logs` for a service (no follow). Use this to "
                "see why a container is crashing or unhealthy."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {
                        "type": "string",
                        "description": "Service name (e.g. node-1, console).",
                    },
                    "tail": {"type": "integer", "description": "Lines to show (default 100)."},
                    "compose_file": {
                        "type": "string",
                        "description": "Optional compose file path (-f).",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "systemd_status",
            "description": "Show `systemctl status` for a Turnstone systemd unit (bare-metal installs).",
            "parameters": {
                "type": "object",
                "properties": {
                    "unit": {
                        "type": "string",
                        "description": "Unit name, e.g. turnstone-server.service.",
                    }
                },
                "required": ["unit"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "journal_tail",
            "description": "Show the last N journald lines for a Turnstone systemd unit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "unit": {
                        "type": "string",
                        "description": "Unit name, e.g. turnstone-server.service.",
                    },
                    "lines": {"type": "integer", "description": "Lines to show (default 100)."},
                },
                "required": ["unit"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_health",
            "description": (
                "GET a node or console /health endpoint and return its JSON. Pass a base URL "
                "or a full /health URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Base URL or /health URL to probe."}
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_llm_backend",
            "description": (
                "Probe an LLM endpoint (the model backend a node uses) for reachability and "
                "available models. Read-only; does not store anything."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "description": "openai | anthropic | openai-compatible | google | xai.",
                    },
                    "base_url": {"type": "string", "description": "Endpoint base URL."},
                    "api_key": {"type": "string", "description": "Optional API key."},
                    "model": {"type": "string", "description": "Optional model id to look for."},
                },
                "required": ["provider", "base_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "node_health",
            "description": (
                "Read one cluster node's /health when the cluster summary isn't enough "
                "(e.g. to pin down which node differs on a version drift). The reach "
                "mechanism is chosen automatically from the detected install kind: "
                "docker-compose execs into the node's container (nodes aren't reachable "
                "from the host); systemd/bare-metal/pip GETs the node's host/URL directly. "
                "Read-only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "node": {
                        "type": "string",
                        "description": (
                            "Compose service name (e.g. node-1) for docker-compose; a "
                            "host or advertise URL for other installs."
                        ),
                    },
                    "install_type": {
                        "type": "string",
                        "enum": ["docker-compose", "systemd", "pip", "source"],
                        "description": (
                            "Override the detected install kind for THIS node — use for "
                            "mixed clusters (e.g. local compose nodes + remote systemd hosts)."
                        ),
                    },
                    "compose_file": {
                        "type": "string",
                        "description": "Optional compose file path (-f); docker-compose only.",
                    },
                },
                "required": ["node"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "Call when the diagnosis is complete. Provide a summary of findings and the "
                "exact remediation commands the operator should run. Doctor never runs "
                "mutating commands itself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Diagnosis summary + recommended commands.",
                    }
                },
                "required": ["summary"],
            },
        },
    },
]


def _tool_read_file(project_dir: Path, args: dict[str, Any]) -> str:
    raw = str(args["path"])
    path = _resolve_safe(project_dir, raw)
    if path is None:
        return f"Error: path escapes install directory: {raw}"
    if path.suffix.lower() in _SECRET_FILE_SUFFIXES:
        return (
            f"Refused: {args['path']} looks like a key/cert file — doctor won't dump raw secrets."
        )
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"Error: file not found: {args['path']}"
    except (OSError, UnicodeDecodeError) as exc:
        return f"Error reading {args['path']}: {exc}"
    if "PRIVATE KEY-----" in content:
        return (
            f"Refused: {args['path']} contains a private-key block — doctor won't dump raw secrets."
        )
    if len(content) > _READ_MAX_CHARS:
        content = (
            content[:_READ_MAX_CHARS]
            + f"\n... (truncated, {len(content) - _READ_MAX_CHARS} more chars)"
        )
    # Secret masking happens centrally in execute_tool (_scrub_tool_output).
    return content


def _tool_check_port(args: dict[str, Any]) -> str:
    port = args["port"]
    if not isinstance(port, int) or port < 1 or port > 65535:
        return f"Error: invalid port number: {port}"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return (
                    f"Port {port} is IN USE (a service is listening — expected for a running node)."
                )
            return f"Port {port} is FREE (nothing listening — the service may be down)."
    except OSError as exc:
        return f"Error checking port {port}: {exc}"


def _tool_check_docker(args: dict[str, Any]) -> str:
    results: list[str] = []
    try:
        proc = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            results.append(f"Docker: installed (version {proc.stdout.strip()})")
        else:
            stderr = proc.stderr.strip()
            if "Cannot connect" in stderr or "Is the docker daemon running" in stderr:
                results.append("Docker: installed but daemon is NOT running")
            else:
                results.append(f"Docker: error — {stderr}")
    except FileNotFoundError:
        results.append("Docker: NOT installed")
    except subprocess.TimeoutExpired:
        results.append("Docker: timed out (daemon may be unresponsive)")

    try:
        proc = subprocess.run(
            ["docker", "compose", "version", "--short"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            results.append(f"Docker Compose: installed (version {proc.stdout.strip()})")
        else:
            results.append("Docker Compose: NOT available")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        results.append("Docker Compose: NOT available")
    return "\n".join(results)


def _tool_compose_status(project_dir: Path, args: dict[str, Any]) -> str:
    cmd = ["docker", "compose"]
    if args.get("compose_file"):
        f = _reject_option(args["compose_file"])
        if f is None:
            return "Error: invalid compose_file."
        cmd += ["-f", f]
    cmd += ["ps", "-a"]
    return _run_readonly(cmd, cwd=project_dir)


def _tool_compose_logs(project_dir: Path, args: dict[str, Any]) -> str:
    tail = args.get("tail", 100)
    if not isinstance(tail, int) or tail < 1 or tail > 2000:
        tail = 100
    cmd = ["docker", "compose"]
    if args.get("compose_file"):
        f = _reject_option(args["compose_file"])
        if f is None:
            return "Error: invalid compose_file."
        cmd += ["-f", f]
    cmd += ["logs", "--no-color", "--tail", str(tail)]
    if args.get("service"):
        service = _reject_option(args["service"])
        if service is None:
            return "Error: invalid service name."
        cmd.append(service)
    return _run_readonly(cmd, cwd=project_dir, timeout=30)


def _tool_systemd_status(args: dict[str, Any]) -> str:
    unit = _safe_unit(str(args["unit"]))
    if unit is None:
        return "Error: invalid unit name (expected a plain systemd unit, e.g. turnstone-server.service)."
    # Options first, then `--`, so the model-supplied unit can't be read as an option.
    return _run_readonly(["systemctl", "status", "--no-pager", "--lines", "20", "--", unit])


def _tool_journal_tail(args: dict[str, Any]) -> str:
    unit = _safe_unit(str(args["unit"]))
    if unit is None:
        return "Error: invalid unit name (expected a plain systemd unit, e.g. turnstone-server.service)."
    lines = args.get("lines", 100)
    if not isinstance(lines, int) or lines < 1 or lines > 2000:
        lines = 100
    return _run_readonly(["journalctl", "--no-pager", "-n", str(lines), "-u", unit], timeout=30)


def _tool_http_health(args: dict[str, Any]) -> str:
    url = str(args["url"]).rstrip("/")
    if not url.endswith("/health"):
        url = url + "/health"
    try:
        data = _http_get_json(url, timeout=5)
    except urllib.error.HTTPError as exc:
        return f"{url} → HTTP {exc.code} {exc.reason}"
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        return f"{url} → unreachable ({exc})"
    return json.dumps(data, indent=2, default=str)[:6000]


def _tool_check_llm_backend(args: dict[str, Any]) -> str:
    from turnstone.core.model_registry import probe_model_endpoint

    provider = str(args.get("provider", "openai"))
    base_url = str(args.get("base_url", ""))
    api_key = str(args.get("api_key", ""))
    model = str(args.get("model", ""))
    try:
        res = probe_model_endpoint(provider, base_url, api_key, target_model=model)
    except Exception as exc:  # noqa: BLE001 - report any probe failure to the model
        return f"Error probing {base_url}: {exc}"
    return json.dumps(res, default=str)[:4000]


# Read-only one-liner the docker-compose path runs INSIDE a node container to
# fetch its own /health (nodes aren't reachable from the host).
_NODE_HEALTH_SNIPPET = (
    "import urllib.request,sys;"
    f"sys.stdout.write(urllib.request.urlopen('http://localhost:{DEFAULT_SERVER_PORT}/health',"
    "timeout=4).read().decode())"
)


def _tool_node_health(project_dir: Path, primary_kind: str, args: dict[str, Any]) -> str:
    """Read one node's /health, choosing the mechanism from the install kind.

    Deterministic: the reach mechanism follows the *detected* install kind, not a
    model decision — overridable per-call via ``install_type`` for mixed clusters
    (e.g. local compose nodes + remote systemd hosts). docker-compose nodes aren't
    host-reachable (internal advertise URLs), so this execs into the container and
    fetches /health from inside; every other kind is a real host reached at its URL.
    """
    node = _reject_option(args.get("node", ""))
    if not node:
        return "Error: node is required (a compose service name, or a host/URL for non-compose installs)."
    kind = str(args.get("install_type") or primary_kind or "unknown").lower()

    if kind == "docker-compose":
        cmd = ["docker", "compose"]
        if args.get("compose_file"):
            cf = _reject_option(args["compose_file"])
            if cf is None:
                return "Error: invalid compose_file."
            cmd += ["-f", cf]
        # -T disables the pseudo-TTY (required for non-interactive exec); the
        # command is the fixed read-only snippet, so only `node` is variable.
        cmd += ["exec", "-T", node, "python", "-c", _NODE_HEALTH_SNIPPET]
        return _run_readonly(cmd, cwd=project_dir, timeout=20)

    # systemd / pip / source / unknown: the node is a real host reachable at its
    # advertise URL. Treat `node` as a URL or host[:port] and GET its /health.
    url = (
        node if node.startswith(("http://", "https://")) else f"http://{node}:{DEFAULT_SERVER_PORT}"
    )
    return _tool_http_health({"url": url})


class _FinishError(Exception):
    """Raised by the finish tool to signal the diagnosis is done."""

    def __init__(self, summary: str) -> None:
        self.summary = summary


def _tool_finish(args: dict[str, Any]) -> str:
    raise _FinishError(args.get("summary", "Diagnosis complete."))


TOOL_FUNCTIONS: dict[str, Any] = {
    "read_file": _tool_read_file,
    "check_port": _tool_check_port,
    "check_docker": _tool_check_docker,
    "compose_status": _tool_compose_status,
    "compose_logs": _tool_compose_logs,
    "systemd_status": _tool_systemd_status,
    "journal_tail": _tool_journal_tail,
    "http_health": _tool_http_health,
    "check_llm_backend": _tool_check_llm_backend,
    "node_health": _tool_node_health,
    "finish": _tool_finish,
}

# Tools that take the install dir (cwd / scoped reads) as their first argument.
_PROJECT_DIR_TOOLS = frozenset({"read_file", "compose_status", "compose_logs"})


def execute_tool(
    name: str, args: dict[str, Any], project_dir: Path, *, primary_kind: str = "unknown"
) -> str:
    """Execute a diagnostic tool and return the (secret-scrubbed) result string.

    Every tool result passes through :func:`_scrub_tool_output` here — the single
    chokepoint that keeps secrets (DSN passwords, keys, env echoes in logs) out of
    the model's context, so a newly added tool is covered automatically.

    Raises _FinishError when the finish tool is called.
    """
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return f"Error: unknown tool '{name}'"
    try:
        if name == "node_health":
            # Needs the detected install kind to pick its reach mechanism.
            result: str = _tool_node_health(project_dir, primary_kind, args)
        elif name in _PROJECT_DIR_TOOLS:
            result = fn(project_dir, args)
        else:
            result = fn(args)
    except _FinishError:
        raise
    except Exception as exc:  # noqa: BLE001 - tool errors are reported, not fatal
        return f"Error executing {name}: {exc}"
    return _scrub_tool_output(result)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are Turnstone Doctor, an expert SRE assistant that diagnoses a running \
Turnstone deployment and guides the operator through fixing it.

## About Turnstone
Turnstone is a multi-node AI orchestration platform. A deployment is made of:
- **Server nodes** (turnstone-server): web UI + chat workstreams + LLM calls. \
Each node serves `/health` (default :8080) and registers in the shared database.
- **Console** (turnstone-console): cluster dashboard + admin; serves `/health` \
(default :8090) and discovers nodes from the database.
- **Caddy**: fronts the console over HTTPS (the published browser entry point).
- **PostgreSQL** (or SQLite): the shared database — required for nodes to be \
discovered and for cluster state.
- **Channel** (optional): Discord/Slack gateway.

## Install kinds (you are told which one this is in the preflight report)
- **docker-compose**: a `compose.yaml` checkout (usually from `run.sh` in \
`~/turnstone`). Diagnose with `docker compose ps` / `docker compose logs`. Config \
lives in `.env` next to the compose file.
- **systemd / bare-metal**: `turnstone-*.service` units; config in \
`/etc/turnstone/config.toml`. Diagnose with `systemctl status` / `journalctl`.
- **pip**: installed package; config in `~/.config/turnstone/config.toml` (or \
`$TURNSTONE_CONFIG`) + env vars.
- **git-source**: a developer checkout run from source.

## Config precedence
Runtime settings resolve storage(database) > config.toml > environment > defaults. \
Bootstrap-critical settings (`[database]`, `[auth]`, ports, API keys) come from \
config.toml or env only — they are not hot-reloadable.

## Reaching individual nodes
On a **docker-compose** cluster the nodes are NOT reachable from the host — they \
advertise internal URLs (`http://node-1:8080`) and publish no host port, so the \
preflight can only confirm them via the console's aggregate `/health`. Do NOT call \
healthy-per-console nodes "down". When you need a specific node's `/health` (e.g. to \
find which node a version drift is on), use the `node_health` tool — it reaches the \
node the right way for the detected install kind (exec-into-container for compose, \
direct HTTP for systemd/bare-metal). Pass `install_type` to override per node on a \
mixed cluster.

## Common failure modes and how to confirm them
- **Node not joining the console**: node up but not in the dashboard → check the \
node `/health`, confirm it shares `TURNSTONE_JWT_SECRET` and the same \
`TURNSTONE_DB_URL` as the console, and that its `TURNSTONE_ADVERTISE_URL` is \
reachable from the console.
- **Database unreachable**: nodes crash-loop or the console shows no nodes → check \
`TURNSTONE_DB_URL`, the postgres container/port, and credentials.
- **LLM backend down**: chats error or hang → use `check_llm_backend` against the \
node's `LLM_BASE_URL`.
- **Port conflict**: a service won't bind → `check_port`.
- **TLS / ACME**: browser cert errors → Caddy fronts the console with a local CA; \
nodes enroll via the console's plain-HTTP ACME endpoint.
- **JWT secret mismatch**: 401s between services → all services must share \
`TURNSTONE_JWT_SECRET`.
- **Version drift**: nodes on different versions (see the version report) → \
realign by pulling/redeploying the lagging nodes.

## Your rules
- You are **DIAGNOSE-ONLY**. NEVER run or instruct a tool to run a mutating \
command. Investigate with the read-only tools, then hand the operator the EXACT \
commands to run themselves.
- Start from the preflight report you are given; use tools to CONFIRM specifics \
before drawing conclusions. Don't guess when you can check.
- Be concise. Ask 1–2 questions at a time.
- NEVER echo secrets (JWT secret, DB password, API keys) back to the user.
- For (re)installation or adding nodes, point the user at `run.sh` \
(`curl -fsSL https://raw.githubusercontent.com/turnstonelabs/turnstone/main/run.sh | bash`).
- When done, call `finish` with a clear summary and the precise remediation \
commands.
"""


# ---------------------------------------------------------------------------
# _DoctorLLM — thin wrapper over OpenAI / Anthropic SDKs
# ---------------------------------------------------------------------------


class _DoctorLLM:
    """Provider-agnostic wrapper for non-streaming tool-calling completions.

    ``provider`` is the wire *family* — ``"anthropic"`` or ``"openai"`` (see
    :func:`family_of`); anything not ``"anthropic"`` uses the OpenAI path.
    """

    def __init__(self, provider: str, client: Any, model: str) -> None:
        self.provider = provider
        self.client = client
        self.model = model

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]] | None, str]:
        """Run a completion and return (content, tool_calls, stop_reason)."""
        if self.provider == "anthropic":
            return self._complete_anthropic(messages, tools)
        return self._complete_openai(messages, tools)

    # -- OpenAI path --------------------------------------------------------

    def _complete_openai(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]] | None, str]:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools if tools else None,
        )
        # Guard against non-spec responses from proxies (Open WebUI, LiteLLM, etc.)
        if resp is None:
            raise RuntimeError(
                "Server returned null — your OpenAI-compatible endpoint may not "
                "support tool calling. Try a direct connection to the model server."
            )
        choices = getattr(resp, "choices", None)
        if not choices:
            raise RuntimeError(
                "Server returned an empty choices array. "
                "The model may have hit its context limit, or the proxy "
                "dropped the response."
            )
        choice = choices[0]
        message = getattr(choice, "message", None)
        if message is None:
            raise RuntimeError(
                "Server returned a choice with no message. "
                "Your OpenAI-compatible endpoint may not fully implement "
                "the chat completions API."
            )
        content = message.content or ""
        tool_calls = None
        if getattr(message, "tool_calls", None):
            tool_calls = [
                {
                    "id": getattr(tc, "id", None) or f"call_{os.urandom(4).hex()}",
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in message.tool_calls
            ]
        return content, tool_calls, getattr(choice, "finish_reason", None) or "stop"

    # -- Anthropic path -----------------------------------------------------

    def _complete_anthropic(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]] | None, str]:
        system_text = ""
        api_messages: list[dict[str, Any]] = []
        for msg in messages:
            if msg["role"] == "system":
                system_text = msg["content"]
            elif msg["role"] == "tool":
                api_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg["tool_call_id"],
                                "content": msg["content"],
                            }
                        ],
                    }
                )
            elif msg["role"] == "assistant" and msg.get("tool_calls"):
                blocks: list[dict[str, Any]] = []
                if msg.get("content"):
                    blocks.append({"type": "text", "text": msg["content"]})
                for tc in msg["tool_calls"]:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["function"]["name"],
                            "input": json.loads(tc["function"]["arguments"]),
                        }
                    )
                api_messages.append({"role": "assistant", "content": blocks})
            else:
                api_messages.append(msg)

        # Merge consecutive same-role messages (Anthropic requires alternation).
        merged: list[dict[str, Any]] = []
        for msg in api_messages:
            if merged and merged[-1]["role"] == msg["role"]:
                prev = merged[-1]
                prev_content = prev["content"]
                new_content = msg["content"]
                if isinstance(prev_content, str) and isinstance(new_content, str):
                    prev["content"] = prev_content + "\n" + new_content
                elif isinstance(prev_content, str):
                    prev["content"] = [{"type": "text", "text": prev_content}] + (
                        new_content if isinstance(new_content, list) else [new_content]
                    )
                elif isinstance(new_content, str):
                    prev["content"] = prev_content + [{"type": "text", "text": new_content}]
                else:
                    prev["content"] = prev_content + new_content
            else:
                merged.append(msg)
        api_messages = merged

        api_tools = [
            {
                "name": t["function"]["name"],
                "description": t["function"]["description"],
                "input_schema": t["function"]["parameters"],
            }
            for t in tools
        ]

        resp = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system_text,
            messages=api_messages,
            tools=api_tools if api_tools else [],
        )

        content_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in resp.content:
            if block.type == "text":
                content_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    {
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": json.dumps(block.input),
                        },
                    }
                )

        return (
            "\n".join(content_parts),
            tool_calls if tool_calls else None,
            resp.stop_reason or "end_turn",
        )


# ---------------------------------------------------------------------------
# Interactive provider selection (fallback when self-config fails)
# ---------------------------------------------------------------------------


def _prompt_api_key(env_var: str, label: str) -> str:
    """Prompt for an API key, checking the env var first."""
    env_val = os.environ.get(env_var, "")
    if env_val:
        prefix = env_val[:4] + "..." if len(env_val) > 4 else env_val
        print(f"\n Found {CYAN}${env_var}{RESET} in environment ({DIM}{prefix}{RESET})")
        try:
            use_env = input(f" Use it? {BOLD}[Y/n]{RESET} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            sys.exit(0)
        if use_env not in ("n", "no"):
            return env_val

    print(f"\n {label}")
    try:
        key = getpass.getpass(" API key: ")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        sys.exit(0)
    if not key.strip():
        print(f" {RED}API key cannot be empty.{RESET}")
        sys.exit(1)
    return key.strip()


def _prompt_model(provider: str) -> str:
    """Prompt for model name with a sensible default."""
    default = _DEFAULT_MODELS.get(provider, "")
    prompt = f" Model {DIM}[{default}]{RESET}: " if default else " Model name: "
    try:
        model = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        sys.exit(0)
    return model or default


def _setup_openai() -> tuple[str, Any, str]:
    from openai import OpenAI

    api_key = _prompt_api_key("OPENAI_API_KEY", "Enter your OpenAI API key:")
    model = _prompt_model("openai")
    return "openai", OpenAI(api_key=api_key), model


def _setup_anthropic() -> tuple[str, Any, str]:
    import anthropic

    api_key = _prompt_api_key("ANTHROPIC_API_KEY", "Enter your Anthropic API key:")
    model = _prompt_model("anthropic")
    return "anthropic", anthropic.Anthropic(api_key=api_key), model


def _detect_models(client: Any) -> list[str]:
    """Query /v1/models and return a sorted list of model IDs."""
    try:
        resp = client.models.list()
        return sorted(m.id for m in resp.data)
    except Exception:  # noqa: BLE001 - detection is best-effort
        return []


def _setup_local() -> tuple[str, Any, str]:
    from openai import OpenAI

    print("\n Enter the base URL of your OpenAI-compatible endpoint.")
    default_url = "http://localhost:8000/v1"
    try:
        url = input(f" Base URL {DIM}[{default_url}]{RESET}: ").strip() or default_url
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        sys.exit(0)

    env_key = os.environ.get("OPENAI_API_KEY", "")
    if env_key:
        api_key = env_key
        print(f" Using {CYAN}$OPENAI_API_KEY{RESET} from environment.")
    else:
        print(" API key (press Enter for 'none'):")
        try:
            api_key = getpass.getpass(" API key: ") or "none"
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            sys.exit(0)

    client = OpenAI(api_key=api_key, base_url=url)

    print(f"\n {DIM}Querying {url} for available models...{RESET}")
    available = _detect_models(client)
    if len(available) == 1:
        model = available[0]
        print(f" Found model: {CYAN}{model}{RESET}")
    elif available:
        print(f" Found {len(available)} model(s):")
        for i, m in enumerate(available, 1):
            print(f"   {CYAN}[{i}]{RESET} {m}")
        try:
            choice = input(f" Select model {DIM}[1]{RESET}: ").strip() or "1"
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            sys.exit(0)
        try:
            idx = int(choice) - 1
            model = available[idx] if 0 <= idx < len(available) else choice
        except ValueError:
            model = choice
    else:
        print(f" {YELLOW}Could not auto-detect models.{RESET}")
        try:
            model = input(" Model name: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            sys.exit(0)
        if not model:
            print(f" {RED}Model name is required for local endpoints.{RESET}")
            sys.exit(1)

    return "openai", client, model


def _select_provider() -> tuple[str, Any, str]:
    """Interactive provider/model/key selection. Returns (family, client, model)."""
    print(f" {BOLD}Which LLM should power Doctor?{RESET}")
    print(f"   {CYAN}[1]{RESET} OpenAI")
    print(f"   {CYAN}[2]{RESET} Anthropic")
    print(f"   {CYAN}[3]{RESET} OpenAI-compatible (local/vLLM)")
    print()
    while True:
        try:
            choice = input(f" {BOLD}>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            sys.exit(0)
        if choice in ("1", "2", "3"):
            break
        print(f" {RED}Please enter 1, 2, or 3.{RESET}")

    if choice == "1":
        return _setup_openai()
    if choice == "2":
        return _setup_anthropic()
    return _setup_local()


def _validate_connection(llm: _DoctorLLM) -> tuple[bool, str]:
    """Validate the LLM connection with a minimal, time-bounded request.

    Doctor runs precisely when the backend may be sick, so the probe goes through
    a bounded client (10s, no retries) — without it the SDK's ~600s default could
    wedge startup / ``--report`` against a reachable-but-hung endpoint.
    """
    client = llm.client
    with_options = getattr(client, "with_options", None)
    if callable(with_options):
        client = with_options(timeout=10.0, max_retries=0)
    probe = _DoctorLLM(llm.provider, client, llm.model)
    try:
        probe.complete(
            [
                {"role": "system", "content": "Reply with exactly: ok"},
                {"role": "user", "content": "ping"},
            ],
            [],
        )
        return True, ""
    except Exception as exc:  # noqa: BLE001 - any failure means unreachable
        return False, str(exc)


# ---------------------------------------------------------------------------
# Conversation loop
# ---------------------------------------------------------------------------


def _run_conversation(
    llm: _DoctorLLM, project_dir: Path, context_report: str, primary_kind: str = "unknown"
) -> None:
    """Main LLM-driven diagnostic loop."""
    renderer = MarkdownRenderer()

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Here is the deterministic preflight report for this machine:\n\n"
                f"{context_report}\n\n"
                "Greet the operator briefly, summarize the cluster's health at a "
                "glance from this report, and ask what symptom they're seeing (or "
                "offer to investigate the most likely problem). Confirm specifics "
                "with your tools before drawing conclusions."
            ),
        },
    ]

    _max_retries = 3
    retries = 0

    while True:
        with Spinner("Thinking"):
            try:
                content, tool_calls, _reason = llm.complete(messages, TOOLS)
            except KeyboardInterrupt:
                print(f"\n{DIM}(Interrupted. Type 'quit' to exit.){RESET}")
                messages.append(
                    {"role": "user", "content": "The user interrupted. Ask what they need."}
                )
                continue
            except Exception as exc:  # noqa: BLE001 - retry transient LLM errors
                retries += 1
                if retries >= _max_retries:
                    print(f"\n{RED}LLM error after {_max_retries} attempts: {exc}{RESET}")
                    print()
                    print("Troubleshooting:")
                    print(
                        f"  {DIM}• If using a proxy (Open WebUI, LiteLLM), try connecting directly{RESET}"
                    )
                    print(f"  {DIM}• Verify the endpoint supports tool/function calling{RESET}")
                    print(f"  {DIM}• Check that the model context window isn't exceeded{RESET}")
                    return
                print(f"\n{RED}LLM error: {exc}{RESET}")
                print(f"{DIM}Retrying ({retries}/{_max_retries})...{RESET}")
                continue

        retries = 0

        assistant_msg: dict[str, Any] = {"role": "assistant", "content": content or ""}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        if content:
            rendered = renderer.feed(content + "\n")
            flushed = renderer.flush()
            print(rendered + flushed, end="")

        if tool_calls:
            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError as exc:
                    result = f"Error: invalid JSON arguments: {exc}"
                    args = {}
                else:
                    hint = ""
                    if name == "read_file" and "path" in args:
                        hint = f" {args['path']}"
                    elif name in ("compose_logs", "systemd_status", "journal_tail") and (
                        args.get("service") or args.get("unit")
                    ):
                        hint = f" {args.get('service') or args.get('unit')}"
                    elif name == "http_health" and "url" in args:
                        hint = f" {args['url']}"
                    elif name == "check_port" and "port" in args:
                        hint = f" :{args['port']}"
                    print(f"  {DIM}[{name}]{hint}{RESET}")
                    try:
                        result = execute_tool(name, args, project_dir, primary_kind=primary_kind)
                    except _FinishError as fin:
                        print(f"\n{GREEN}{BOLD} Diagnosis complete.{RESET}\n")
                        rendered = renderer.feed(fin.summary + "\n")
                        flushed = renderer.flush()
                        print(rendered + flushed, end="")
                        return

                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
            continue

        print()
        try:
            user_input = input(f"{BOLD}>{RESET} ").strip()
        except EOFError:
            print("\nGoodbye!")
            return
        except KeyboardInterrupt:
            print(f"\n{DIM}(Press Ctrl+C again to quit, or type your response.){RESET}")
            try:
                user_input = input(f"{BOLD}>{RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                return

        if user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            return
        if not user_input:
            continue
        messages.append({"role": "user", "content": user_input})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _print_banner() -> None:
    print(f"\n{BOLD}{CYAN} Turnstone Doctor{RESET}  {DIM}v{__version__}{RESET}")
    print(f" {DIM}{'─' * 48}{RESET}")
    print()
    print(" Diagnoses a running Turnstone deployment. Read-only:")
    print(" it inspects and recommends fixes, but never changes")
    print(" your system. (Installs use run.sh.)")
    print()


def _build_report(
    project_dir: Path, *, offline: bool
) -> tuple[InstallProfile, VersionReport, _DoctorLLM | None, BackendVerdict, str]:
    """Run the full deterministic preflight: profile + versions + brain resolution."""
    profile = detect_install_profile(project_dir)
    storage, storage_err = open_storage(profile)
    versions = check_versions(profile, storage, offline=offline)
    brain, verdict = resolve_doctor_brain(profile, storage, storage_err)
    report = render_full_report(profile, versions, verdict)
    return profile, versions, brain, verdict, report


def main() -> None:
    """Entry point for the turnstone-doctor CLI."""
    parser = argparse.ArgumentParser(
        prog="turnstone-doctor",
        description=(
            "Diagnose a running Turnstone deployment with an LLM-backed assistant. "
            "Read-only — it never mutates your system. Installs use run.sh."
        ),
    )
    parser.add_argument(
        "--dir",
        default=None,
        help="Install directory to inspect (default: current directory).",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Print the preflight report and exit (no interactive chat; still runs a "
        "bounded backend-reachability probe).",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip the upstream GitHub version check.",
    )
    args = parser.parse_args()

    project_dir = Path(args.dir).expanduser().resolve() if args.dir else Path.cwd()

    if args.report:
        _, _, _, _, report = _build_report(project_dir, offline=args.offline)
        print(report)
        return

    _print_banner()
    print(f"{DIM}Running preflight…{RESET}")
    profile, _versions, brain, verdict, report = _build_report(project_dir, offline=args.offline)
    print()
    print(report)
    print()

    if brain is None:
        print(f"{YELLOW}Could not self-configure an LLM from the cluster config:{RESET}")
        print(f"  {DIM}{verdict.detail}{RESET}")
        print(f"{DIM}Falling back to interactive provider selection.{RESET}\n")
        provider, client, model = _select_provider()
        brain = _DoctorLLM(provider, client, model)
        ok, err = _validate_connection(brain)
        if not ok:
            print(f"\n {RED}Could not connect to the model: {err}{RESET}")
            sys.exit(1)
        print(f"\n {GREEN}Connected to {BOLD}{model}{RESET}{GREEN}.{RESET}")
    else:
        print(f"{GREEN}LLM backend healthy — {verdict.detail}{RESET}")

    print(f" {DIM}Handing off to the diagnostic assistant…{RESET}\n")
    try:
        _run_conversation(brain, project_dir, report, profile.primary_kind)
    except KeyboardInterrupt:
        print("\nGoodbye!")
        sys.exit(0)


if __name__ == "__main__":
    main()
