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

from turnstone.core.workstream import WorkstreamKind

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
    SCHEDULED = "scheduled"


# Subset of ``ClientType`` values where the user is present to complete
# an in-flight OAuth consent flow (browser redirect + return).  CHAT and
# SCHEDULED users cannot drive a browser redirect from inside their
# delivery surface, so consent-required errors must be persisted to
# ``mcp_pending_consent`` for later surfacing rather than relying on the
# in-flight SSE rendering path.  Used by ``ChatSession`` to set
# ``_is_interactive_for_consent`` at construction time.
INTERACTIVE_CONSENT_CLIENT_TYPES: frozenset[ClientType] = frozenset(
    {ClientType.WEB, ClientType.CLI}
)


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
    ClientType.SCHEDULED: "env/scheduled.md",
}


def _build_context(ctx: SessionContext, kind: WorkstreamKind) -> str:
    """Build the CONTEXT module from session variables."""
    return (
        "## Session Context\n"
        "\n"
        f"- **Current date/time:** {ctx.current_datetime} ({ctx.timezone})\n"
        f"- **User:** {ctx.username}\n"
        f"- **Session kind:** {kind.value}"
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
    kind: WorkstreamKind = WorkstreamKind.INTERACTIVE,
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
    kind:
        Workstream kind — ``"interactive"`` (default) loads the
        IC-focused ``tools.md`` with read_file / bash / write_file
        patterns; ``"coordinator"`` loads ``tools_coordinator.md``
        which documents spawn_workstream / send_to_workstream /
        inspect_workstream / list_nodes / list_skills / tasks etc.
        A coordinator session has a disjoint tool schema (see
        COORDINATOR_TOOLS), so composing it with the IC tools block
        would instruct the model to hallucinate tool calls that fail.

    Returns
    -------
    str
        The fully assembled system message, modules separated by double newlines.
    """
    parts: list[str] = []

    # Coerce kind: callers (and tests) sometimes pass the raw string from a
    # DB row or HTTP payload. WorkstreamKind is a StrEnum so equality works
    # either way, but ``.value`` access does not — normalise once here.
    kind = WorkstreamKind.from_raw(kind)

    # 1. BASE — kind-specific persona.  The default base.md frames the
    #    model as an IC engineer ("you read before you edit, commits
    #    you make..."); coordinators need an orchestrator framing
    #    instead ("you decompose, delegate, monitor, synthesise").
    base_module = "base_coordinator.md" if kind == WorkstreamKind.COORDINATOR else "base.md"
    parts.append(_load(base_module))

    # 2. ENV — exactly one, selected by client type.  Coordinators
    #    skip ENV: they orchestrate rather than render rich output to
    #    the user, so the rendering capability matrix (Mermaid, KaTeX,
    #    terminal width, chat-platform table quirks) is not actionable
    #    for them.  Synthesis output rides on the child's response or
    #    the operator's renderer.  ``client_type`` still validates so
    #    a malformed call fails loud.
    if client_type not in _ENV_MAP:
        raise ValueError(f"Unknown client_type: {client_type!r}")
    if kind != WorkstreamKind.COORDINATOR:
        parts.append(_load(_ENV_MAP[client_type]))

    # 3. CONTEXT — built programmatically (no template engine)
    _validate_context(context)
    parts.append(_build_context(context, kind))

    # 4. TOOLS — kind-specific patterns.  Coordinators get the
    #    orchestrator block; interactive sessions get the IC block.
    if available_tools:
        tools_module = "tools_coordinator.md" if kind == WorkstreamKind.COORDINATOR else "tools.md"
        parts.append(_load(tools_module))

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
