"""Dynamic tool search — BM25 index and session-scoped visibility manager.

When the total tool count exceeds a configurable threshold, deferred tools
are hidden from the LLM and discoverable via a ``tool_search`` function.
Native providers (Anthropic, OpenAI) handle search server-side; local
models (vLLM, llama.cpp) use the client-side BM25 fallback here.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING, Any

from turnstone.core.bm25 import BM25Index, _tokenize  # noqa: F401

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.rerank import Reranker

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


def _status_reason(status: dict[str, Any]) -> str:
    """Human-readable reason a server is unavailable, or ``""`` when it looks
    healthy. Reads only fields ``MCPClientManager.get_server_status`` already
    exposes, so this stays decoupled from the client internals.

    Deliberately conservative: a merely un-primed server (``connected=False``
    with no error) is NOT treated as a failure — surfacing that would cry wolf
    on every server the user simply hasn't reached yet. Only hard signals
    (open circuit breaker, a recorded error, or a recorded discovery failure)
    mark a server unavailable.
    """
    if status.get("circuit_open"):
        return "circuit breaker open"
    err = str(status.get("error") or "").strip()
    if err:
        return f"error: {err[:120]}"
    disc = str(status.get("discovery_error") or "").strip()
    if disc:
        return f"tool discovery failed: {disc[:120]}"
    return ""


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
        reranker: Reranker | None = None,
        status_provider: Callable[[], dict[str, dict[str, Any]]] | None = None,
    ) -> None:
        self._always_on: list[dict[str, Any]] = []
        self._deferred: list[dict[str, Any]] = []
        self._deferred_by_name: dict[str, dict[str, Any]] = {}
        self._expanded: dict[str, None] = {}  # ordered set (preserves discovery order)
        self._max_results = max_results
        # Optional callback -> {server_name: status_dict} (the shape
        # MCPClientManager.get_all_server_status returns). Lets search results
        # flag servers that are down/unauthorized so a failed discovery is
        # never mistaken for "no such tool". None keeps the legacy behaviour.
        self._status_provider = status_provider
        # Total matches from the most recent search(), before the max_results
        # slice — so format_search_results can report honest truncation.
        self._last_total_matched = 0

        for tool in all_tools:
            name = _tool_name(tool)
            if name in always_on_names:
                self._always_on.append(tool)
            else:
                self._deferred.append(tool)
                self._deferred_by_name[name] = tool

        # BM25 index over deferred tools
        texts = [_tool_text(t) for t in self._deferred]
        self._index = BM25Index(texts, reranker=reranker)

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
        Records the full (pre-slice) match count on ``_last_total_matched`` so
        callers can report honest truncation instead of silently dropping
        matches past ``max_results``.
        """
        # Rank the WHOLE deferred corpus (k = len(deferred)) so the count
        # reflects every match, not just the max_results slice the model gets.
        # Indices map 1:1 to self._deferred (the index was built over it in
        # order). Cheap: the tool corpus is small (tens, not thousands).
        indices = self._index.search(query, k=max(len(self._deferred), 1))
        matched = [
            self._deferred[i]
            for i in indices
            if _tool_name(self._deferred[i]) not in self._expanded
        ]
        self._last_total_matched = len(matched)
        return matched[: self._max_results]

    def get_expanded_names(self) -> list[str]:
        """Return names of currently expanded (discovered) tools."""
        return list(self._expanded.keys())

    def is_expanded(self, name: str) -> bool:
        """O(1) membership check for the discovered set — the per-tool
        visibility filter runs per LLM turn, so no list construction."""
        return name in self._expanded

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

    def _unavailable_servers(self) -> list[tuple[str, str]]:
        """``(server_name, reason)`` for every MCP server the status provider
        reports as failing, sorted by name.

        Empty when no provider is wired or every server looks healthy. A
        provider that raises is treated as "no info" (empty) so a status
        glitch can never break tool search itself.
        """
        if self._status_provider is None:
            return []
        try:
            statuses = self._status_provider() or {}
        except Exception:
            return []
        out: list[tuple[str, str]] = []
        for name, status in statuses.items():
            if not isinstance(status, dict):
                continue
            reason = _status_reason(status)
            if reason:
                out.append((str(name), reason))
        out.sort()
        return out

    @staticmethod
    def _format_unavailable(items: list[tuple[str, str]]) -> str:
        return ", ".join(f"{name} ({reason})" for name, reason in items)

    def format_search_results(self, tools: list[dict[str, Any]]) -> str:
        """Format search results as text for the tool_search response.

        Beyond the matched tools, this surfaces two things the raw list hides:
        an honest truncation note when more tools matched than were returned,
        and a warning when known MCP servers are currently unavailable — so a
        down server (which contributes zero searchable tools) is never
        mistaken for a missing capability.
        """
        unavailable = self._unavailable_servers()
        if not tools:
            if unavailable:
                return (
                    "No matching tools found for that query. Note: "
                    + self._format_unavailable(unavailable)
                    + ". A server that is unavailable contributes no searchable "
                    "tools until it recovers, so this may be an outage rather "
                    "than a missing capability."
                )
            return "No matching tools found. Try a different search query."
        lines = []
        for tool in tools:
            fn = tool.get("function", {})
            name = fn.get("name", "")
            desc = fn.get("description", "")
            lines.append(f"- **{name}**: {desc}")
        out = f"Found {len(tools)} matching tool(s):\n" + "\n".join(lines)
        if self._last_total_matched > len(tools):
            out += (
                f"\n\n(Showing the top {len(tools)} of {self._last_total_matched} "
                "matches — narrow your query to surface the rest.)"
            )
        out += "\n\nThese tools are now available for use."
        if unavailable:
            out += (
                "\n\n⚠ Some tool servers are currently unavailable, so their "
                "tools are not searchable: " + self._format_unavailable(unavailable) + "."
            )
        return out
