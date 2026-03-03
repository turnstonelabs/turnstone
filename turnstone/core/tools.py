"""Tool definitions — auto-loaded from turnstone/tools/*.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
_META_KEYS = {"agent", "task_agent", "auto_approve", "primary_key"}


def _load_tools() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load all .json files from the tools directory.

    Returns (tool_defs, metadata) where:
      - tool_defs: list of OpenAI function-calling dicts
      - metadata: dict mapping tool_name -> {agent, task_agent, auto_approve, primary_key}
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


TOOLS, _META = _load_tools()

AGENT_TOOLS = [t for t in TOOLS if _META[t["function"]["name"]].get("agent")]
TASK_AGENT_TOOLS = [t for t in TOOLS if _META[t["function"]["name"]].get("task_agent")]
AGENT_AUTO_TOOLS = {n for n, m in _META.items() if m.get("auto_approve")}
TASK_AUTO_TOOLS = {n for n, m in _META.items() if m.get("auto_approve")}
PRIMARY_KEY_MAP = {n: m["primary_key"] for n, m in _META.items() if "primary_key" in m}
