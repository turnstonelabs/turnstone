"""BM25-based memory relevance scoring and system message formatting."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape as _html_escape
from typing import Any

from turnstone.core.bm25 import BM25Index


@dataclass
class MemoryConfig:
    """Configuration for the structured memory system."""

    relevance_k: int = 5
    fetch_limit: int = 50
    max_content: int = 32768
    nudge_cooldown: int = 300
    nudges: bool = True


def score_memories(
    memories: list[dict[str, str]],
    query: str,
    k: int = 5,
) -> list[dict[str, str]]:
    """Return the top-k memories most relevant to *query*.

    Builds a BM25 index over ``name + description + content prefix``
    for each memory and returns matches sorted by relevance.  If *query*
    is empty, returns the most recent *k* memories (they are already
    ordered by ``updated DESC`` from storage).
    """
    if not memories:
        return []
    if not query or not query.strip():
        return memories[:k]

    documents = [
        f"{m.get('name', '')} {m.get('description', '')} {m.get('content', '')[:200]}"
        for m in memories
    ]
    index = BM25Index(documents)
    top_indices = index.search(query, k)
    return [memories[i] for i in top_indices]


def build_memory_context(memories: list[dict[str, str]]) -> str:
    """Format selected memories as an XML block for system message injection.

    Produces a compact ``<memories>`` section matching the style used
    for MCP resources (``<mcp-resources>``).
    """
    if not memories:
        return ""
    lines = ["<memories>"]
    for m in memories:
        name = _html_escape(m.get("name", ""))
        mem_type = _html_escape(m.get("type", "project"))
        scope = _html_escape(m.get("scope", "global"))
        desc = m.get("description", "")
        content = m.get("content", "")
        # Truncate content to avoid bloating system message
        if len(content) > 500:
            content = content[:500] + "..."
        desc_attr = f' description="{_html_escape(desc)}"' if desc else ""
        lines.append(
            f'  <memory name="{name}" type="{mem_type}" scope="{scope}"{desc_attr}>'
            f"{_html_escape(content)}</memory>"
        )
    lines.append("</memories>")
    return "\n".join(lines)


def extract_recent_context(messages: list[dict[str, Any]], max_messages: int = 3) -> str:
    """Extract text from the last N user messages for relevance scoring.

    Handles both string and list content formats.
    """
    user_texts: list[str] = []
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            user_texts.append(content)
        elif isinstance(content, list):
            # Multi-part content (text + images)
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    user_texts.append(part.get("text", ""))
                elif isinstance(part, str):
                    user_texts.append(part)
        if len(user_texts) >= max_messages:
            break
    return " ".join(user_texts)
