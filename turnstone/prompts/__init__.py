"""System message composition harness.

Assembles modular system messages from BASE (persona), ENV (client surface),
CONTEXT (session variables), TOOLS (usage patterns), and POLICIES (behavioral
rules).  Replaces the monolithic persona+tools section of
``ChatSession._init_system_messages()``.
"""

from __future__ import annotations

import dataclasses
import enum
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent  # turnstone/prompts/
# Files are read once at import time — they're static markdown.
# This matches the pattern in tools.py where JSON schemas are loaded once.
_FILE_CACHE: dict[Path, str] = {}


def _load(relpath: str) -> str:
    """Load and cache a prompt module file."""
    path = _PROMPTS_DIR / relpath
    if path not in _FILE_CACHE:
        _FILE_CACHE[path] = path.read_text()
    return _FILE_CACHE[path]


class ClientType(enum.StrEnum):
    WEB = "web"
    CLI = "cli"
    CHAT = "chat"


@dataclasses.dataclass
class SessionContext:
    current_datetime: str  # ISO 8601, required
    timezone: str  # system tz abbreviation, required
    username: str  # users.username, required


# File-based policy-to-tool gating (defaults).
# DB policies carry their own tool_gate field.
POLICY_TOOL_GATES: dict[str, str] = {
    "web_search": "web_search",
}

_ENV_MAP: dict[ClientType, str] = {
    ClientType.WEB: "env/web.md",
    ClientType.CLI: "env/cli.md",
    ClientType.CHAT: "env/chat.md",
}


def _build_context(ctx: SessionContext) -> str:
    """Build the CONTEXT module from session variables."""
    return (
        "## Session Context\n"
        "\n"
        f"- **Current date/time:** {ctx.current_datetime} ({ctx.timezone})\n"
        f"- **User:** {ctx.username}"
    )


def _validate_context(ctx: SessionContext) -> None:
    """Validate required fields and format constraints."""
    if not ctx.current_datetime:
        raise ValueError("current_datetime is required")
    if not ctx.timezone:
        raise ValueError("timezone is required")
    if not ctx.username:
        raise ValueError("username is required")
    # Validate ISO 8601
    try:
        datetime.fromisoformat(ctx.current_datetime)
    except ValueError as exc:
        raise ValueError(f"current_datetime is not valid ISO 8601: {ctx.current_datetime}") from exc


def compose_system_message(
    client_type: ClientType,
    context: SessionContext,
    available_tools: frozenset[str],
    policies: list[str] | None = None,
    db_policies: list[dict[str, Any]] | None = None,
) -> str:
    """Compose a system message from modular components.

    Parameters
    ----------
    client_type:
        Target rendering surface (web, cli, chat).
    context:
        Per-session variables (datetime, timezone, username).
    available_tools:
        Set of available tool names (used for policy gating).
    policies:
        Explicit file-based policy names to include (e.g. ``["web_search"]``).
    db_policies:
        Database-backed policies from ``storage.list_prompt_policies()``.

    Returns
    -------
    str
        The fully assembled system message, modules separated by double newlines.
    """
    parts: list[str] = []

    # 1. BASE — always included
    parts.append(_load("base.md"))

    # 2. ENV — exactly one, selected by client type
    if client_type not in _ENV_MAP:
        raise ValueError(f"Unknown client_type: {client_type!r}")
    parts.append(_load(_ENV_MAP[client_type]))

    # 3. CONTEXT — built programmatically (no template engine)
    _validate_context(context)
    parts.append(_build_context(context))

    # 4. TOOLS — if any tools are available
    if available_tools:
        parts.append(_load("tools.md"))

    # 5. POLICIES — resolve from DB first, fall back to files
    #    DB policies indexed by name for O(1) override lookup.
    db_by_name: dict[str, dict[str, Any]] = {}
    if db_policies:
        db_by_name = {p["name"]: p for p in db_policies if p.get("enabled", True)}

    for policy_name in policies or []:
        db_row = db_by_name.pop(policy_name, None)
        if db_row:
            # DB override — use its content and tool_gate
            gate = db_row.get("tool_gate", "")
            if gate and gate not in available_tools:
                log.debug("Skipping DB policy %r: requires tool %r", policy_name, gate)
                continue
            parts.append(db_row["content"])
        else:
            # File-based fallback
            path = _PROMPTS_DIR / "policies" / f"{policy_name}.md"
            if not path.exists():
                raise FileNotFoundError(f"Policy module not found: {path}")
            gate = POLICY_TOOL_GATES.get(policy_name, "")
            if gate and gate not in available_tools:
                log.debug("Skipping file policy %r: requires tool %r", policy_name, gate)
                continue
            parts.append(_load(f"policies/{policy_name}.md"))

    # DB-only policies (not in the explicit list) — sorted by priority
    for db_row in sorted(db_by_name.values(), key=lambda p: p.get("priority", 0)):
        gate = db_row.get("tool_gate", "")
        if gate and gate not in available_tools:
            continue
        parts.append(db_row["content"])

    return "\n\n".join(parts)
