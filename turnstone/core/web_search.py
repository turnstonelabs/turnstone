"""Pluggable web search backends.

``web_search`` is an abstract capability with swappable clients:

* **TavilyClient** — paid, high quality, requires API key
* **DuckDuckGoClient** — free, no API key, uses ``duckduckgo-search``

Auto-detection (default): Tavily if key present, else DDG if installed,
else ``None`` (tool removed from tool list).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

import httpx

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from turnstone.core.mcp_client import MCPClientManager

log = get_logger(__name__)


class WebSearchClient(Protocol):
    """Minimal interface for a web search backend."""

    def search(self, query: str, max_results: int = 5, **kwargs: Any) -> str:
        """Run a search and return formatted markdown results."""
        ...


class TavilyClient:
    """Tavily search backend (paid, requires API key)."""

    def __init__(self, api_key: str, timeout: float = 120) -> None:
        self._api_key = api_key
        self._timeout = timeout

    def search(self, query: str, max_results: int = 5, **kwargs: Any) -> str:
        topic = kwargs.get("topic", "general")
        resp = httpx.post(
            "https://api.tavily.com/search",
            json={
                "query": query,
                "max_results": max_results,
                "topic": topic,
                "include_answer": True,
            },
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return _format_tavily(data, query)


class DuckDuckGoClient:
    """DuckDuckGo search backend (free, no API key)."""

    def __init__(self, timeout: float = 120) -> None:
        self._timeout = timeout

    def search(self, query: str, max_results: int = 5, **kwargs: Any) -> str:
        from duckduckgo_search import DDGS  # type: ignore[import-not-found]

        with DDGS(timeout=int(self._timeout)) as ddgs:
            raw = list(ddgs.text(query, max_results=max_results))
        return _format_ddg(raw, query)


class MCPSearchClient:
    """Delegates web_search to an MCP server tool."""

    def __init__(self, mcp_client: MCPClientManager, tool_name: str, timeout: float = 120) -> None:
        self._mcp = mcp_client
        self._tool = tool_name
        self._timeout = timeout

    def search(self, query: str, max_results: int = 5, **kwargs: Any) -> str:
        import math

        args: dict[str, Any] = {"query": query}
        if max_results != 5:
            args["max_results"] = max_results
        topic = kwargs.get("topic")
        if topic:
            args["topic"] = topic
        return self._mcp.call_tool_sync(self._tool, args, timeout=max(1, math.ceil(self._timeout)))


# ---------------------------------------------------------------------------
# Result formatters
# ---------------------------------------------------------------------------


def _format_tavily(data: dict[str, Any], query: str) -> str:
    parts: list[str] = []
    answer = (data.get("answer") or "").strip()
    if answer:
        parts.append(f"Answer: {answer}")
    results = data.get("results") or []
    if results:
        lines = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            url = r.get("url", "")
            content = (r.get("content") or "")[:500]
            lines.append(f"{i}. [{title}]({url})\n   {content}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts) if parts else f"No results for '{query}'."


def _format_ddg(results: list[dict[str, Any]], query: str) -> str:
    if not results:
        return f"No results for '{query}'."
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        url = r.get("href", "")
        body = (r.get("body") or "")[:500]
        lines.append(f"{i}. [{title}]({url})\n   {body}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def _ddg_available() -> bool:
    """Check if duckduckgo-search is installed."""
    try:
        import duckduckgo_search  # noqa: F401

        return True
    except ImportError:
        return False


def resolve_web_search_client(
    backend: str,
    tavily_key: str | None,
    mcp_client: Any | None = None,
    timeout: float = 120,
) -> WebSearchClient | None:
    """Return a search client based on configuration, or None if unavailable.

    Args:
        backend: ``""`` (auto), ``"tavily"``, ``"ddg"``, or ``"mcp:server:tool"``
        tavily_key: Tavily API key (None if not configured)
        mcp_client: MCPClientManager instance (for MCP backends)
        timeout: HTTP/tool timeout in seconds
    """
    if backend == "tavily":
        if tavily_key:
            return TavilyClient(tavily_key, timeout=timeout)
        return None

    if backend == "ddg":
        if _ddg_available():
            return DuckDuckGoClient(timeout=timeout)
        return None

    if backend.startswith("mcp:"):
        parts = backend.split(":", 2)
        if len(parts) == 3 and mcp_client is not None:
            _, server, tool = parts
            prefixed = f"mcp__{server}__{tool}"
            if mcp_client.is_mcp_tool(prefixed):
                return MCPSearchClient(mcp_client, prefixed, timeout=timeout)
        return None

    if backend == "":
        # Auto-detect: Tavily > DDG > None
        if tavily_key:
            return TavilyClient(tavily_key, timeout=timeout)
        if _ddg_available():
            return DuckDuckGoClient(timeout=timeout)
        return None

    log.warning("Unknown web_search_backend %r — web search disabled", backend)
    return None
