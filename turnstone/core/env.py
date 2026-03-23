"""Subprocess environment scrubbing.

Builds a sanitized copy of ``os.environ`` that strips secrets
(API keys, tokens, passwords) while preserving variables needed
for normal tool operation (PATH, HOME, locale, etc.).
"""

from __future__ import annotations

import os

# Env var names that are always preserved regardless of pattern matching.
_SAFE_NAMES: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "SHELL",
        "LANG",
        "TERM",
        "TMPDIR",
        "TMP",
        "TEMP",
        "EDITOR",
        "VISUAL",
        "COLORTERM",
        "COLUMNS",
        "LINES",
        "PWD",
        "OLDPWD",
        "HOSTNAME",
        "LOGNAME",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "SSH_AUTH_SOCK",
        "GPG_AGENT_INFO",
        "SHLVL",
        "MANWIDTH",
        "MAN_KEEP_FORMATTING",
        "LESS",
        "LESSOPEN",
        "LESSCLOSE",
        "LESSPIPE",
        "LESSCHARSET",
    }
)

# Prefixes that are always preserved (locale, XDG, etc.).
_SAFE_PREFIXES: tuple[str, ...] = ("LC_", "XDG_")

# Substrings that cause a variable to be scrubbed.
_SECRET_SUBSTRINGS: tuple[str, ...] = (
    "KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "CREDENTIAL",
)

# Exact names that are always scrubbed (even if they don't match patterns).
_EXPLICIT_SCRUB: frozenset[str] = frozenset(
    {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "TAVILY_API_KEY",
        "TURNSTONE_JWT_SECRET",
        "TURNSTONE_AUTH_TOKEN",
        "TURNSTONE_DISCORD_TOKEN",
        "TURNSTONE_GITHUB_TOKEN",
        "TURNSTONE_OIDC_CLIENT_SECRET",
        "AWS_SECRET_ACCESS_KEY",
        "AZURE_CLIENT_SECRET",
        "GCP_SERVICE_ACCOUNT_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
    }
)


def _is_secret(name: str) -> bool:
    """Return True if *name* looks like a secret variable."""
    if name in _EXPLICIT_SCRUB:
        return True
    upper = name.upper()
    return any(sub in upper for sub in _SECRET_SUBSTRINGS)


def _is_safe(name: str) -> bool:
    """Return True if *name* should always be preserved."""
    if name in _SAFE_NAMES:
        return True
    return any(name.startswith(pfx) for pfx in _SAFE_PREFIXES)


def scrubbed_env(
    extra: dict[str, str] | None = None,
    passthrough: list[str] | None = None,
) -> dict[str, str]:
    """Return a copy of ``os.environ`` with secrets removed.

    Args:
        extra: Additional variables to merge on top (e.g. ``MANWIDTH``).
        passthrough: Explicit variable names to preserve even if they
            match secret patterns (operator override).
    """
    passthrough_set = frozenset(passthrough) if passthrough else frozenset()
    env: dict[str, str] = {}
    for name, value in os.environ.items():
        if name in passthrough_set or _is_safe(name):
            env[name] = value
        elif _is_secret(name):
            continue
        else:
            env[name] = value
    if extra:
        env.update(extra)
    return env
