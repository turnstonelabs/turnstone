"""Shared utilities for storage backends."""

from __future__ import annotations

import base64
import contextlib
import json
from typing import Any

from turnstone.core.attachments import unreadable_placeholder
from turnstone.core.log import get_logger

log = get_logger(__name__)


def _attachment_to_content_part(att: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a stored attachment row into an OpenAI-style content part.

    Returns ``None`` if the attachment's ``kind`` / ``content`` cannot be
    turned into a content part (logged but non-fatal so history still renders).
    """
    kind = att.get("kind")
    raw = att.get("content")
    mime = att.get("mime_type") or "application/octet-stream"
    if kind == "image" and isinstance(raw, bytes):
        b64 = base64.b64encode(raw).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        }
    if kind == "text" and isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            log.warning(
                "attachment id=%s stored as text but not valid UTF-8",
                att.get("attachment_id"),
            )
            return unreadable_placeholder(att.get("filename") or "")
        return {
            "type": "document",
            "document": {
                "name": att.get("filename") or "",
                "media_type": mime,
                "data": text,
            },
        }
    return None


# ---------------------------------------------------------------------------
# Text sanitization
# ---------------------------------------------------------------------------


def sanitize_text(value: str | None) -> str | None:
    """Strip NUL bytes that PostgreSQL text fields cannot store.

    SQLite tolerates NUL in TEXT but they cause downstream issues (API
    payloads, web UI rendering), so both backends use this.
    """
    if value and "\x00" in value:
        return value.replace("\x00", "")
    return value


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
MODEL_DEFINITION_MUTABLE = frozenset(
    {
        "alias",
        "model",
        "provider",
        "base_url",
        "api_key",
        "context_window",
        "capabilities",
        "enabled",
        "temperature",
        "max_tokens",
        "reasoning_effort",
    }
)
PROMPT_POLICY_MUTABLE = frozenset({"name", "content", "tool_gate", "priority", "enabled"})
HEURISTIC_RULE_MUTABLE = frozenset(
    {
        "name",
        "risk_level",
        "confidence",
        "recommendation",
        "tool_pattern",
        "arg_patterns",
        "intent_template",
        "reasoning_template",
        "tier",
        "priority",
        "builtin",
        "enabled",
    }
)
OUTPUT_GUARD_PATTERN_MUTABLE = frozenset(
    {
        "name",
        "category",
        "risk_level",
        "pattern",
        "pattern_flags",
        "flag_name",
        "annotation",
        "is_credential",
        "redact_label",
        "priority",
        "builtin",
        "enabled",
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
                pass  # falls back to None (no tool filter)
        result = scan_skill(content, tools)
        return result.tier, json.dumps(result.to_dict(), ensure_ascii=False), SCANNER_VERSION
    except Exception:
        log.debug("skill_scanner: scan failed", exc_info=True)
        return "", "{}", ""


# ---------------------------------------------------------------------------
# Message reconstruction
# ---------------------------------------------------------------------------


def reconstruct_messages(
    rows: list[Any],
    ws_id: str,
    attachments_by_msg: dict[int, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Reconstruct OpenAI message format from stored conversation rows.

    Each *row* is a 7-tuple ``(id, role, content, tool_name,
    tool_call_id, provider_data, tool_calls_json)``, ordered
    chronologically by row id.

    When ``attachments_by_msg`` is provided, any user row whose id has
    attachments is rebuilt with multipart list content (text +
    image_url/document parts).
    """
    messages: list[dict[str, Any]] = []
    for row in rows:
        row_id, role, content, _tool_name, tc_id, provider_data, tool_calls_json = row

        if role == "user":
            parts: list[dict[str, Any]] = []
            meta: list[dict[str, Any]] = []
            if attachments_by_msg and row_id is not None:
                for att in attachments_by_msg.get(row_id, []):
                    part = _attachment_to_content_part(att)
                    if part is not None:
                        parts.append(part)
                    # Track display-oriented metadata even when a part
                    # itself can't be reconstructed — keeps filenames
                    # available for history replay (e.g. image pills).
                    meta.append(
                        {
                            "kind": str(att.get("kind") or ""),
                            "filename": str(att.get("filename") or ""),
                            "mime_type": str(att.get("mime_type") or ""),
                        }
                    )
            if parts:
                user_content: list[dict[str, Any]] = [{"type": "text", "text": content or ""}]
                user_content.extend(parts)
                umsg: dict[str, Any] = {"role": "user", "content": user_content}
                if meta:
                    umsg["_attachments_meta"] = meta
                messages.append(umsg)
            else:
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

    # Repair: synthesize tool results for mid-conversation orphaned tool calls.
    # This happens when a cancel interrupts tool execution — the assistant
    # message with tool_calls is saved to DB but GenerationCancelled prevents
    # tool results from being created.  Both Anthropic (strict) and OpenAI
    # (lenient today, may tighten) benefit from well-formed histories.
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            expected_ids = [tc.get("id", "") for tc in msg["tool_calls"] if tc.get("id")]
            # Collect tool result IDs that follow
            j = i + 1
            result_ids: set[str] = set()
            while j < len(messages) and messages[j].get("role") == "tool":
                tc_id = messages[j].get("tool_call_id", "")
                if tc_id:
                    result_ids.add(tc_id)
                j += 1
            # Synthesize results for any missing IDs
            orphaned = [uid for uid in expected_ids if uid not in result_ids]
            if orphaned:
                synthetic = [
                    {
                        "role": "tool",
                        "tool_call_id": uid,
                        "content": "Tool execution was cancelled.",
                        "is_error": True,
                    }
                    for uid in orphaned
                ]
                # Insert after the last existing tool result (or after assistant)
                messages[j:j] = synthetic
            if orphaned:
                i = j + len(orphaned)  # skip past spliced synthetics
            elif j > i + 1:
                i = j  # skip past existing tool block
            else:
                i += 1  # no tools followed; just advance
        else:
            i += 1

    return messages
