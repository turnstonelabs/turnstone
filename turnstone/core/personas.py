"""Persona snapshot — the per-workstream stamp of a persona's four levers.

A persona (see migration 063 / ``storage.list_personas``) is resolved ONCE at
workstream creation and stamped into ``workstream_config`` as five keys.  From
then on the session reads only the stamp: editing or archiving the persona
never changes an existing workstream, and a workstream outlives its persona.

The five keys (all-or-none — a partial stamp is corruption, not a fallback):

- ``persona``          — the persona's slug (display + forensics)
- ``persona_prompt``   — BASE-module override; ``""`` = the kind's stock base
- ``persona_tools``    — JSON tri-state: ``null`` = unrestricted, ``[]`` =
  hard empty, ``[names]`` = exact visibility set (``tool_search`` membership
  decides soft vs hard)
- ``persona_mcp``      — ``"1"``/``"0"``: whether the workstream talks to MCP
  at all (session-wide, including task-agent merges)
- ``persona_memory``   — ``"1"``/``"0"``: whether the persona's own hands get
  memory (recall injection, nudges, the memory tool); task agents keep theirs

Workstreams with none of the keys predate personas (or were created against a
pre-seed database) and keep legacy behaviour — byte-identical to the
``engineer``/``orchestrator`` defaults.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

PERSONA_CONFIG_KEYS = (
    "persona",
    "persona_prompt",
    "persona_tools",
    "persona_mcp",
    "persona_memory",
)


@dataclass(frozen=True)
class PersonaSnapshot:
    """Immutable, self-contained persona stamp held by a live session."""

    name: str
    prompt: str  # "" = use the kind's stock BASE module
    tools: frozenset[str] | None  # None = unrestricted; frozenset() = hard empty
    mcp: bool
    memory: bool

    def to_config(self) -> dict[str, str]:
        """Serialize to the five ``workstream_config`` values.

        The tool set is written sorted so save/load round-trips are
        byte-stable (the semantics are set-based; order carries nothing).
        """
        return {
            "persona": self.name,
            "persona_prompt": self.prompt,
            "persona_tools": (json.dumps(sorted(self.tools)) if self.tools is not None else "null"),
            "persona_mcp": "1" if self.mcp else "0",
            "persona_memory": "1" if self.memory else "0",
        }


def resolve_persona_for_kind(
    storage: Any, name: str, kind: str
) -> tuple[dict[str, Any] | None, str]:
    """Resolve a persona slug for attaching to a ``kind`` workstream.

    Returns ``(row, "")`` on success or ``(None, error)`` when the name is
    unknown, disabled, or does not apply to the kind.  ONE shared eligibility
    rule — the HTTP create handler, the CLI ``--persona`` path, and the
    coordinator spawn precheck all consume this, so a future rule change
    (per-org personas, a new kind) cannot leave the surfaces disagreeing.
    ``storage is None`` reports a distinct storage-unavailable error — a
    storage outage must never masquerade as "unknown persona".
    """
    if storage is None:
        return None, "persona storage unavailable"
    row = storage.get_persona_by_name(name)
    if not row or not row.get("enabled", False):
        return None, f"Persona not found or disabled: {name}"
    if kind not in (row.get("applies_to_kinds") or []):
        return None, f"Persona {name!r} does not apply to kind {kind!r}"
    return row, ""


def snapshot_from_persona(persona: Mapping[str, Any]) -> PersonaSnapshot:
    """Build the stamp from a storage persona row — the resolve-once moment."""
    tools = persona.get("tool_allowlist")
    return PersonaSnapshot(
        name=str(persona["name"]),
        prompt=persona.get("base_prompt") or "",
        tools=None if tools is None else frozenset(tools),
        mcp=bool(persona.get("mcp_enabled", True)),
        memory=bool(persona.get("memory_enabled", True)),
    )


def snapshot_from_config(config: Mapping[str, str]) -> PersonaSnapshot | None:
    """Parse the stamp back out of persisted ``workstream_config`` values.

    Returns ``None`` when no persona was stamped (legacy pre-063 workstream).
    Raises ``ValueError`` when the stamp is partial or unparseable — the
    session must fail loudly rather than silently fall back to a default
    envelope the operator never chose for this workstream.
    """
    if "persona" not in config:
        stray = [k for k in PERSONA_CONFIG_KEYS if k in config]
        if stray:
            raise ValueError(
                f"corrupt persona snapshot: companion keys {stray} present without 'persona'"
            )
        return None
    missing = [k for k in PERSONA_CONFIG_KEYS if k not in config]
    if missing:
        raise ValueError(f"corrupt persona snapshot: missing keys {missing}")
    name = config["persona"]
    if not name:
        raise ValueError("corrupt persona snapshot: empty persona name")
    raw_tools = config["persona_tools"]
    try:
        tools_val = json.loads(raw_tools)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(
            f"corrupt persona snapshot: persona_tools is not JSON: {raw_tools!r}"
        ) from exc
    tools: frozenset[str] | None
    if tools_val is None:
        tools = None
    elif isinstance(tools_val, list) and all(isinstance(t, str) for t in tools_val):
        tools = frozenset(tools_val)
    else:
        raise ValueError(
            f"corrupt persona snapshot: persona_tools must be null or a list "
            f"of names, got {raw_tools!r}"
        )
    flags = {}
    for key in ("persona_mcp", "persona_memory"):
        if config[key] not in ("0", "1"):
            raise ValueError(
                f"corrupt persona snapshot: {key} must be '0' or '1', got {config[key]!r}"
            )
        flags[key] = config[key] == "1"
    return PersonaSnapshot(
        name=name,
        prompt=config["persona_prompt"],
        tools=tools,
        mcp=flags["persona_mcp"],
        memory=flags["persona_memory"],
    )
