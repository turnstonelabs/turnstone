"""Shared utilities for storage backends."""

from __future__ import annotations

import contextlib
import json
from typing import Any

from turnstone.core.log import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Row helper
# ---------------------------------------------------------------------------


def row_to_dict(row: Any, *bool_fields: str) -> dict[str, Any]:
    """Convert a SQLAlchemy row to a dict, casting named fields to bool."""
    d = dict(row._mapping)
    for key in bool_fields:
        if key in d:
            d[key] = bool(d[key])
    return d


# ---------------------------------------------------------------------------
# Field allowlists for governance update methods
# ---------------------------------------------------------------------------

ROLE_MUTABLE = frozenset({"display_name", "permissions"})
ORG_MUTABLE = frozenset({"display_name", "settings"})
POLICY_MUTABLE = frozenset({"name", "tool_pattern", "action", "priority", "enabled"})
SKILL_MUTABLE = frozenset(
    {
        "name",
        "content",
        "category",
        "variables",
        "is_default",
        "description",
        "tags",
        "source_url",
        "version",
        "author",
        "activation",
        "token_estimate",
        "model",
        "auto_approve",
        "temperature",
        "reasoning_effort",
        "max_tokens",
        "token_budget",
        "agent_max_turns",
        "notify_on_complete",
        "enabled",
        "allowed_tools",
        "license",
        "compatibility",
        "scan_version",
        "scan_status",
        "scan_report",
        "priority",
    }
)
STRUCTURED_MEMORY_MUTABLE = frozenset({"content", "description", "type"})
MCP_SERVER_MUTABLE = frozenset(
    {
        "name",
        "transport",
        "command",
        "args",
        "url",
        "headers",
        "env",
        "auto_approve",
        "enabled",
        "registry_name",
        "registry_version",
        "registry_meta",
    }
)
VERDICT_MUTABLE = frozenset(
    {
        "user_decision",
        "intent_summary",
        "risk_level",
        "confidence",
        "recommendation",
        "reasoning",
        "evidence",
        "tier",
        "judge_model",
        "latency_ms",
    }
)


# ---------------------------------------------------------------------------
# Skill scanning helper
# ---------------------------------------------------------------------------


def scan_skill_content(content: str, allowed_tools: str) -> tuple[str, str, str]:
    """Run the skill scanner and return ``(scan_status, scan_report_json, scanner_version)``.

    Uses a lazy import to avoid circular dependencies.  Silently returns
    empty results on import or scan errors so skill creation is never
    blocked by a scanner bug.
    """
    try:
        from turnstone.core.skill_scanner import SCANNER_VERSION, scan_skill

        tools: list[str] | None = None
        if allowed_tools and allowed_tools.strip() != "[]":
            try:
                parsed = json.loads(allowed_tools)
                if isinstance(parsed, list):
                    tools = [str(x) for x in parsed if isinstance(x, str)]
                    if not tools:
                        tools = None
            except (json.JSONDecodeError, TypeError):
                pass
        result = scan_skill(content, tools)
        return result.tier, json.dumps(result.to_dict(), ensure_ascii=False), SCANNER_VERSION
    except Exception:
        log.debug("skill_scanner: scan failed", exc_info=True)
        return "", "{}", ""


# ---------------------------------------------------------------------------
# Message reconstruction
# ---------------------------------------------------------------------------


def reconstruct_messages(rows: list[Any], ws_id: str) -> list[dict[str, Any]]:
    """Reconstruct OpenAI message format from stored conversation rows.

    Each *row* is a 6-element tuple of ``(role, content, tool_name,
    tool_call_id, provider_data, tool_calls_json)`` ordered
    chronologically by row ID.

    Post-migration 013 the only roles are ``user``, ``assistant``, and
    ``tool``.  Assistant messages carry their ``tool_calls`` as a JSON
    column, so no heuristic merging is needed.
    """
    messages: list[dict[str, Any]] = []
    for row in rows:
        role, content, _tool_name, tc_id, provider_data, tool_calls_json = row

        if role == "user":
            messages.append({"role": "user", "content": content or ""})

        elif role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": content or ""}
            if provider_data:
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    msg["_provider_content"] = json.loads(provider_data)
            if tool_calls_json:
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    msg["tool_calls"] = json.loads(tool_calls_json)
            messages.append(msg)

        elif role == "tool":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id or "",
                    "content": content or "",
                }
            )

    # Repair: strip trailing incomplete tool call turns
    while messages:
        tail_tools = 0
        for j in range(len(messages) - 1, -1, -1):
            if messages[j].get("role") == "tool":
                tail_tools += 1
            else:
                break
        asst_idx = len(messages) - 1 - tail_tools
        if asst_idx < 0:
            break
        asst = messages[asst_idx]
        if asst.get("role") != "assistant" or not asst.get("tool_calls"):
            break
        if tail_tools >= len(asst["tool_calls"]):
            break
        del messages[asst_idx:]

    return messages
