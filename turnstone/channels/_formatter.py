"""Message formatting utilities for channel adapters.

Handles chunking long messages for platforms with character limits, formatting
tool-approval requests, and plan-review prompts.
"""

from __future__ import annotations

from typing import Any


def chunk_message(text: str, max_length: int = 2000) -> list[str]:
    """Split *text* into chunks that fit within *max_length*.

    Respects code-block boundaries: if a fenced code block (````` ```)
    spans a chunk boundary the current chunk is closed with ````` ``` ``
    and the next chunk reopens it.  Prefers splitting at newline
    boundaries, then word boundaries, then hard-splits.
    """
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text
    in_code_block = False

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        # Reserve space for a closing ``` if we're inside a code block.
        limit = max_length - 4 if in_code_block else max_length
        limit = max(limit, 1)

        candidate = remaining[:limit]

        # Prefer a newline boundary.
        split_idx = candidate.rfind("\n")
        if split_idx <= 0:
            # Fall back to a word boundary.
            split_idx = candidate.rfind(" ")
        if split_idx <= 0:
            # Hard split.
            split_idx = limit

        chunk = remaining[:split_idx]
        remaining = remaining[split_idx:].lstrip("\n")

        # Track code-block fences in this chunk.
        fence_count = chunk.count("```")
        block_open = in_code_block

        if fence_count % 2 != 0:
            in_code_block = not in_code_block

        # If we end inside a code block, close it in this chunk and
        # reopen in the next.
        if in_code_block:
            chunk += "\n```"
            remaining = "```\n" + remaining
            in_code_block = False
        elif block_open and fence_count % 2 != 0:
            # We were inside a code block and the chunk closed it
            # properly -- nothing extra needed.
            pass

        chunks.append(chunk)

    return chunks


def format_approval_request(items: list[dict[str, Any]]) -> str:
    """Format tool-approval *items* into a human-readable message.

    Items use the server's SSE format: ``func_name``, ``preview``,
    ``approval_label``, ``header``.  Falls back to the nested
    ``function.name`` format for compatibility.
    """
    lines: list[str] = ["**Tool approval required:**"]
    for item in items:
        # Server SSE format: top-level func_name / preview
        name = item.get("func_name") or item.get("approval_label", "")
        if not name:
            # Fallback: nested function.name (SDK / older format)
            func = item.get("function", {})
            name = func.get("name", "unknown")
        preview = item.get("preview", "")
        if not preview:
            args = item.get("function", {}).get("arguments", "")
            if isinstance(args, dict):
                import json

                args = json.dumps(args, ensure_ascii=False)
            preview = str(args)
        preview = truncate(preview)
        header = item.get("header", "")
        if header:
            lines.append(f"\u2022 `{name}`: {header}")
        elif preview:
            lines.append(f"\u2022 `{name}`: {preview}")
        else:
            lines.append(f"\u2022 `{name}`")
    return "\n".join(lines)


def format_verdict(verdict: dict[str, Any]) -> str:
    """Format an intent verdict for display in a channel message.

    Accepts either a raw heuristic verdict dict (from ``_heuristic_verdict``
    in approval items) or an :class:`IntentVerdictEvent`-like dict with the
    same field names.  Returns Markdown text suitable for a Discord embed
    field.
    """
    risk = (verdict.get("risk_level") or "medium").upper()
    rec = verdict.get("recommendation", "review")
    raw_conf = verdict.get("confidence")
    conf = int((raw_conf if raw_conf is not None else 0.5) * 100)
    summary = verdict.get("intent_summary", "")
    tier = verdict.get("tier", "")

    emoji_map = {
        "LOW": "\U0001f7e2",
        "MEDIUM": "\U0001f7e1",
        "HIGH": "\U0001f534",
        "CRITICAL": "\u26d4",
    }
    emoji = emoji_map.get(risk, "\u2753")

    label = f"{tier.upper()} " if tier else ""
    parts = [f"{emoji} **{label}Risk: {risk}** ({conf}%) \u2014 {rec}"]
    if summary:
        parts.append(f"_{summary}_")
    return "\n".join(parts)


def format_plan_review(content: str) -> str:
    """Format a plan-review prompt with a header."""
    return f"**Plan review requested:**\n\n{content}"


def format_tool_result(name: str, output: str, *, is_error: bool = False) -> str:
    """Format a tool result into a compact code-block summary.

    Truncates to the first 10 lines (plus an ellipsis line if trimmed) or
    500 characters, whichever is shorter.
    """
    # Truncate to 10 lines.
    lines = output.split("\n", 10)
    if len(lines) > 10:
        lines = lines[:10]
        lines.append("\u2026")
    trimmed = "\n".join(lines)
    # Escape triple backticks to prevent code-block breakout.
    trimmed = trimmed.replace("```", "` ` `")
    # Truncate to 500 chars (after escaping, which can expand the string).
    if len(trimmed) > 500:
        trimmed = trimmed[:497] + "\u2026"
    return f"```\n{trimmed}\n```"


def truncate(text: str, max_length: int = 200) -> str:
    """Truncate *text* to *max_length*, appending an ellipsis if trimmed."""
    if len(text) <= max_length:
        return text
    return text[: max_length - 1] + "\u2026"
