"""Shared utilities for storage backends."""

from __future__ import annotations

import contextlib
import json
from typing import Any

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
TEMPLATE_MUTABLE = frozenset({"name", "content", "category", "variables", "is_default"})
WS_TEMPLATE_MUTABLE = frozenset(
    {
        "name",
        "description",
        "system_prompt",
        "prompt_template",
        "prompt_template_hash",
        "model",
        "auto_approve",
        "auto_approve_tools",
        "temperature",
        "reasoning_effort",
        "max_tokens",
        "token_budget",
        "agent_max_turns",
        "notify_on_complete",
        "enabled",
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
# Message reconstruction
# ---------------------------------------------------------------------------


def reconstruct_messages(rows: list[Any], ws_id: str) -> list[dict[str, Any]]:
    """Reconstruct OpenAI message format from stored conversation rows.

    Each *row* is a 7-element tuple of ``(role, content, tool_name,
    tool_args, tool_call_id, provider_data, tool_calls_json)`` ordered
    chronologically by row ID.

    Post-migration 013 the only roles are ``user``, ``assistant``, and
    ``tool``.  Assistant messages carry their ``tool_calls`` as a JSON
    column, so no heuristic merging is needed.
    """
    messages: list[dict[str, Any]] = []
    for row in rows:
        role, content, _tool_name, _tool_args, tc_id, provider_data, tool_calls_json = row

        if role == "user":
            messages.append({"role": "user", "content": content or ""})

        elif role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": content}
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
