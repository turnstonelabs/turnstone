"""Persona snapshot — the per-workstream stamp of a persona's four levers.

A persona (see migration 063 / ``storage.list_personas``) is resolved ONCE at
workstream creation and stamped into ``workstream_config`` as five keys.  From
then on the session reads only the stamp: editing or archiving the persona
never changes an existing workstream, and a workstream outlives its persona.

The five keys (all-or-none — a partial stamp is corruption, not a fallback):

- ``persona``          — the persona's slug (display + forensics)
- ``persona_prompt``   — the resolved BASE text, frozen at create (an
  operator override, else the built-in's file content).  Legacy stamps may
  carry ``""``; compose then falls back to the kind's default file.
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
    prompt: str  # resolved BASE text, frozen at create ("" only in legacy stamps)
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


def _enabled_personas(storage: Any) -> list[dict[str, Any]]:
    """Enabled persona rows, or ``[]`` when listing fails.

    Swallowing here keeps the forgiving-lookup and error-enrichment paths
    from introducing raise paths the exact-match lookup never had (the CLI
    calls ``resolve_persona_for_kind`` uncaught).
    """
    try:
        return list(storage.list_personas())
    except Exception:
        return []


def persona_names_for_kind(storage: Any, kind: str) -> list[str]:
    """Enabled persona names applying to ``kind`` — default first, then A→Z.

    The default carries a ``" (default)"`` suffix so error text and tool
    descriptions read the same way everywhere.
    """
    rows = [r for r in _enabled_personas(storage) if kind in (r.get("applies_to_kinds") or [])]
    rows.sort(key=lambda r: (not r.get("is_default"), str(r.get("name") or "")))
    return [
        str(r["name"]) + (" (default)" if r.get("is_default") else "")
        for r in rows
        if r.get("name")
    ]


def _available_for_kind(storage: Any, kind: str) -> str:
    names = persona_names_for_kind(storage, kind)
    return f" Available for {kind}: {', '.join(names)}." if names else ""


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

    Lookup is forgiving: exact name first (stored names are lowercase slugs,
    create-path validated), then the lowercased input, then a case-insensitive
    match on display names.  ``display_name`` carries no uniqueness
    constraint, so the fallback is deliberately narrow: candidates are the
    ENABLED personas ELIGIBLE FOR ``kind`` (the label the caller saw came
    from a kind-filtered surface — picker or injected tool description — so
    a same-label persona of another kind must neither block nor win), and
    the match is accepted only when exactly one candidate remains; duplicates
    refuse loudly, naming the candidate slugs.  Callers must stamp/emit the
    returned row's ``name``, never the input, so a forgiven variant can't
    leak into ``workstream_config`` or approval chrome.  Failure messages
    enumerate the kind's valid names: tool descriptions render the persona
    list at session start, so this is how a caller with a stale list (or a
    typo) self-corrects.
    """
    if storage is None:
        return None, "persona storage unavailable"
    wanted = name.strip()
    row = storage.get_persona_by_name(wanted)
    if row is None and wanted != wanted.lower():
        row = storage.get_persona_by_name(wanted.lower())
    if row is None and wanted:
        # The non-empty gate is load-bearing: display_name defaults to "", so
        # a whitespace-only input would otherwise match every blank-labelled
        # persona and silently stamp an envelope the caller never named.
        target = wanted.lower()
        matches = [
            r
            for r in _enabled_personas(storage)
            if kind in (r.get("applies_to_kinds") or [])
            and str(r.get("display_name") or "").strip().lower() == target
        ]
        if len(matches) == 1:
            row = matches[0]
        elif len(matches) > 1:
            slugs = ", ".join(sorted(str(m["name"]) for m in matches))
            return None, (
                f"Persona name {name!r} matches more than one display name "
                f"(personas: {slugs}); use the exact name"
            )
    if not row or not row.get("enabled", False):
        # ``!r`` matters: forgiven inputs include whitespace-only and
        # trailing-space typos, which an unquoted interpolation renders
        # invisible in CLI output and logs.
        return None, f"Persona not found or disabled: {name!r}.{_available_for_kind(storage, kind)}"
    if kind not in (row.get("applies_to_kinds") or []):
        return None, (
            f"Persona {row['name']!r} does not apply to kind {kind!r}."
            f"{_available_for_kind(storage, kind)}"
        )
    return row, ""


def _resolve_base_prompt(persona: Mapping[str, Any]) -> str:
    """Coalesce a persona row to its BASE prompt text.

    ``base_prompt ?? load(base_prompt_file)``: an operator's inline override
    wins; otherwise the built-in's repo file under ``prompts/personas/``.  The
    storage CHECK guarantees at least one is set, so a row reaching the final
    branch is corrupt and fails loudly rather than composing an empty BASE.
    """
    text = persona.get("base_prompt")
    if text:
        return str(text)
    pfile = persona.get("base_prompt_file")
    if pfile:
        from turnstone.prompts import load_persona_prompt  # lazy: avoid import cycle

        return load_persona_prompt(str(pfile))
    raise ValueError(
        f"persona {persona.get('name')!r} has no prompt source: "
        "base_prompt and base_prompt_file are both empty"
    )


def snapshot_from_persona(persona: Mapping[str, Any]) -> PersonaSnapshot:
    """Build the stamp from a storage persona row — the resolve-once moment.

    The BASE prompt is resolved to concrete text here (operator override, else
    the built-in's file) and frozen into the snapshot, so a later edit to the
    file or the row never changes an already-created workstream.
    """
    tools = persona.get("tool_allowlist")
    return PersonaSnapshot(
        name=str(persona["name"]),
        prompt=_resolve_base_prompt(persona),
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
