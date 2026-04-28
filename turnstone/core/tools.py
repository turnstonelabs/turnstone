"""Tool definitions — auto-loaded from turnstone/tools/*.json."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
_META_KEYS = {
    "agent",
    "task_agent",
    "coordinator",
    "interactive",
    "auto_approve",
    "primary_key",
    # Per-kind variant overrides.  Schema:
    #   "kind_variants": {
    #       "<kind>": {
    #           "description": "kind-specific description",
    #           "parameter_overrides": {
    #               "<param-name>": {... partial JSON-Schema overlay ...}
    #           }
    #       }
    #   }
    # ``description`` REPLACES the base description for the kind; each
    # entry in ``parameter_overrides`` is dict-merged onto the matching
    # ``parameters.properties.<param>`` entry so a ``scope`` enum can
    # be narrowed per-kind without re-stating the rest of the param
    # schema.  See ``memory.json`` for the canonical example.
    "kind_variants",
}


def _load_tools() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load all .json files from the tools directory.

    Returns (tool_defs, metadata) where:
      - tool_defs: list of OpenAI function-calling dicts
      - metadata: dict mapping tool_name -> {agent, task_agent, coordinator,
                  interactive, auto_approve, primary_key, kind_variants}
    """
    tools = []
    meta = {}
    for path in sorted(_TOOLS_DIR.glob("*.json")):
        with open(path) as f:
            raw = json.load(f)
        name = raw["name"]
        # Extract turnstone metadata, leave only OpenAI schema fields
        tool_meta = {k: raw.pop(k) for k in list(raw) if k in _META_KEYS}
        meta[name] = tool_meta
        tools.append({"type": "function", "function": raw})
    return tools, meta


def _apply_kind_variant(tool: dict[str, Any], kind: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Return a kind-specific copy of ``tool`` with description / params overridden.

    Each kind sees only the surface it can actually use — for ``memory``,
    coord sessions get a description + scope enum that mention only the
    ``coordinator`` scope, while interactive sessions get a description
    + scope enum that omit ``coordinator`` entirely.  This keeps the
    LLM contract tight: the model never sees enum values it can't use,
    and never reads description sentences explaining why a scope is
    forbidden.

    No-op (returns the input tool unchanged) when the tool has no
    ``kind_variants`` metadata or no entry for ``kind``.  Otherwise
    deep-copies the tool's function dict so the per-kind list doesn't
    share mutable state with the union ``TOOLS`` list or the other
    kind's list.
    """
    variants = meta.get("kind_variants") or {}
    variant = variants.get(kind)
    if not variant:
        return tool
    new_tool = copy.deepcopy(tool)
    if "description" in variant:
        new_tool["function"]["description"] = variant["description"]
    overrides = variant.get("parameter_overrides") or {}
    if overrides:
        props = new_tool["function"].get("parameters", {}).get("properties", {})
        for param_name, overlay in overrides.items():
            if param_name in props and isinstance(overlay, dict):
                props[param_name].update(overlay)
    return new_tool


TOOLS, _META = _load_tools()

AGENT_TOOLS = [t for t in TOOLS if _META[t["function"]["name"]].get("agent")]
TASK_AGENT_TOOLS = [t for t in TOOLS if _META[t["function"]["name"]].get("task_agent")]
# COORDINATOR_TOOLS apply the ``coordinator`` kind variant (if any) so a
# coord session sees the coord-tailored description + parameter schema.
COORDINATOR_TOOLS = [
    _apply_kind_variant(t, "coordinator", _META[t["function"]["name"]])
    for t in TOOLS
    if _META[t["function"]["name"]].get("coordinator")
]
# Interactive sessions — the default session kind — must NOT see coordinator-only
# tools (``spawn_workstream`` et al.) in their tool set.  Coordinator-only tools
# require a ``coord_client`` that only console-hosted coordinator sessions
# have, and exposing them to interactive sessions also pollutes the
# tool-search threshold count.
#
# A tool can opt INTO both kinds with ``"interactive": true`` alongside
# ``"coordinator": true`` — used for tools whose behaviour makes sense in
# both contexts (e.g. ``memory``).  Dual-kind tools also apply the
# ``interactive`` kind variant when present so the IC-flavored
# description / param schema replaces the union default.  Without the
# explicit opt-in, ``"coordinator": true`` is read as "coord-only" and
# the tool is stripped from interactive sessions.  ``TOOLS`` stays as
# the union for introspection / schema docs / eval catalogs.
INTERACTIVE_TOOLS = [
    _apply_kind_variant(t, "interactive", _META[t["function"]["name"]])
    for t in TOOLS
    if (
        not _META[t["function"]["name"]].get("coordinator")
        or _META[t["function"]["name"]].get("interactive")
    )
]
INTERACTIVE_TOOL_NAMES = frozenset(t["function"]["name"] for t in INTERACTIVE_TOOLS)
AGENT_AUTO_TOOLS = {n for n, m in _META.items() if m.get("auto_approve")}
TASK_AUTO_TOOLS = {n for n, m in _META.items() if m.get("auto_approve")}
PRIMARY_KEY_MAP = {n: m["primary_key"] for n, m in _META.items() if "primary_key" in m}
BUILTIN_TOOL_NAMES = frozenset(_META)


def merge_mcp_tools(
    builtin: list[dict[str, Any]], mcp_tools: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge built-in tools with MCP tools.

    Built-in tools come first so the LLM sees them with natural priority.
    Returns a new list; neither input is mutated.
    """
    return builtin + mcp_tools
