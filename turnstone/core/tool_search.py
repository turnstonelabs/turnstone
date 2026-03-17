"""Dynamic tool search — BM25 index and session-scoped visibility manager.

When the total tool count exceeds a configurable threshold, deferred tools
are hidden from the LLM and discoverable via a ``tool_search`` function.
Native providers (Anthropic, OpenAI) handle search server-side; local
models (vLLM, llama.cpp) use the client-side BM25 fallback here.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from turnstone.core.bm25 import BM25Index, _tokenize  # noqa: F401

# ---------------------------------------------------------------------------
# Tool search manager — partitions tools, tracks visibility
# ---------------------------------------------------------------------------

_MCP_PREFIX_RE = re.compile(r"^mcp__(.+?)__")


def _tool_name(tool: dict[str, Any]) -> str:
    """Extract function name from an OpenAI-format tool dict."""
    fn: dict[str, Any] = tool.get("function", {})
    name: str = fn.get("name", "")
    return name


def _tool_text(tool: dict[str, Any]) -> str:
    """Build searchable text from tool name + description."""
    fn = tool.get("function", {})
    return f"{fn.get('name', '')} {fn.get('description', '')}"


def _mcp_server_summary(tools: list[dict[str, Any]]) -> str:
    """Summarise deferred tools by MCP server prefix for the hint."""
    servers: Counter[str] = Counter()
    other = 0
    for tool in tools:
        name = _tool_name(tool)
        m = _MCP_PREFIX_RE.match(name)
        if m:
            servers[m.group(1)] += 1
        else:
            other += 1
    parts = [f"{srv} ({cnt} tool{'s' if cnt != 1 else ''})" for srv, cnt in sorted(servers.items())]
    if other:
        parts.append(f"other ({other} tool{'s' if other != 1 else ''})")
    return ", ".join(parts)


class ToolSearchManager:
    """Session-scoped tool visibility manager with BM25 search.

    Partitions tools into always-on (built-in) and deferred (MCP) sets.
    Tracks which deferred tools have been discovered and expanded into
    the visible set for the current session.
    """

    def __init__(
        self,
        all_tools: list[dict[str, Any]],
        always_on_names: set[str],
        *,
        max_results: int = 5,
    ) -> None:
        self._always_on: list[dict[str, Any]] = []
        self._deferred: list[dict[str, Any]] = []
        self._deferred_by_name: dict[str, dict[str, Any]] = {}
        self._expanded: dict[str, None] = {}  # ordered set (preserves discovery order)
        self._max_results = max_results

        for tool in all_tools:
            name = _tool_name(tool)
            if name in always_on_names:
                self._always_on.append(tool)
            else:
                self._deferred.append(tool)
                self._deferred_by_name[name] = tool

        # BM25 index over deferred tools
        texts = [_tool_text(t) for t in self._deferred]
        self._index = BM25Index(texts)

        # Pre-compute server summary for the search tool description
        self._server_hint = _mcp_server_summary(self._deferred)

    def get_visible_tools(self) -> list[dict[str, Any]]:
        """Return always-on tools + any expanded (discovered) tools."""
        result = list(self._always_on)
        for name in self._expanded:
            tool = self._deferred_by_name.get(name)
            if tool:
                result.append(tool)
        return result

    def get_deferred_tools(self) -> list[dict[str, Any]]:
        """Return tools that are currently deferred (not yet discovered)."""
        return [t for t in self._deferred if _tool_name(t) not in self._expanded]

    def search(self, query: str) -> list[dict[str, Any]]:
        """Search deferred tools by query, return top-k matches.

        Already-expanded tools are excluded so every result is genuinely new.
        """
        # Request extra results to compensate for filtering out expanded tools
        indices = self._index.search(query, k=self._max_results + len(self._expanded))
        results = []
        for i in indices:
            if _tool_name(self._deferred[i]) not in self._expanded:
                results.append(self._deferred[i])
            if len(results) >= self._max_results:
                break
        return results

    def get_expanded_names(self) -> list[str]:
        """Return names of currently expanded (discovered) tools."""
        return list(self._expanded.keys())

    def expand_visible(self, tool_names: list[str]) -> list[dict[str, Any]]:
        """Promote discovered tools to the visible set.

        Returns the newly-expanded tool definitions (excludes tools
        that were already visible).
        """
        newly_added = []
        for name in tool_names:
            if name not in self._expanded and name in self._deferred_by_name:
                self._expanded[name] = None
                newly_added.append(self._deferred_by_name[name])
        return newly_added

    def get_search_tool_definition(self) -> dict[str, Any]:
        """Return the synthetic ``tool_search`` function tool definition.

        The description includes a dynamic hint listing available MCP
        server names and tool counts so the model can craft specific queries.
        """
        desc = (
            "Search for available tools by keyword. Returns matching tool "
            "names and descriptions. Use this when you need a capability "
            "not available in your current tool set."
        )
        if self._server_hint:
            desc += f" Available tool servers: {self._server_hint}."
        return {
            "type": "function",
            "function": {
                "name": "tool_search",
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query describing the capability you need.",
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    def format_search_results(self, tools: list[dict[str, Any]]) -> str:
        """Format search results as text for the tool_search response."""
        if not tools:
            return "No matching tools found. Try a different search query."
        lines = []
        for tool in tools:
            fn = tool.get("function", {})
            name = fn.get("name", "")
            desc = fn.get("description", "")
            lines.append(f"- **{name}**: {desc}")
        return (
            f"Found {len(tools)} matching tool(s):\n"
            + "\n".join(lines)
            + "\n\nThese tools are now available for use."
        )
