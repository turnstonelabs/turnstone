"""Pluggable web search backends.

``web_search`` is an abstract capability with swappable clients:

* **SearXNGClient** — self-hosted `SearxNG <https://searxng.org>`_ metasearch,
  no API key. Aggregates DuckDuckGo, Wikipedia, and ~200 other engines behind a
  stable JSON API. Bundled into the docker-compose stack as the ``searxng``
  service.
* **MCPSearchClient** — delegates to a web-search tool exposed by an MCP server.

Auto-detection (default): SearxNG when a base URL is configured, else ``None``
(tool removed from the tool list). Native provider-side web search on Anthropic
and OpenAI search models is handled at the API boundary and never reaches these
clients.
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


class SearXNGClient:
    """Self-hosted SearxNG metasearch backend (no API key).

    Talks to the JSON API (``GET /search?format=json``). The instance MUST have
    ``json`` enabled in ``search.formats`` — the bundled ``deploy/searxng``
    config does this; a stock instance returns 403/HTML otherwise.
    """

    # The web_search tool's ``category`` arg → SearxNG ``categories``.
    # ``"general"`` is intentionally absent so the param is omitted and SearxNG
    # uses its default category mix.
    _CATEGORIES = {"news": "news", "it": "it", "science": "science"}

    def __init__(self, base_url: str, engines: str = "", timeout: float = 120) -> None:
        self._base_url = base_url.rstrip("/")
        self._engines = engines.strip()
        self._timeout = timeout

    def search(self, query: str, max_results: int = 5, **kwargs: Any) -> str:
        params: dict[str, str] = {"q": query, "format": "json"}
        category = self._CATEGORIES.get(str(kwargs.get("category", "general")))
        if category:
            params["categories"] = category
        if self._engines:
            params["engines"] = self._engines
        resp = httpx.get(
            f"{self._base_url}/search",
            params=params,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return _format_searxng(resp.json(), query, max_results)


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
        category = kwargs.get("category")
        if category:
            args["category"] = category
        return self._mcp.call_tool_sync(self._tool, args, timeout=max(1, math.ceil(self._timeout)))


# ---------------------------------------------------------------------------
# Result formatters
# ---------------------------------------------------------------------------


def _format_searxng(data: dict[str, Any], query: str, max_results: int = 5) -> str:
    parts: list[str] = []

    # Instant answers (calculator, Wikipedia summaries, …) — engine-dependent,
    # often absent. Each entry is a dict (``{"answer": ...}``) on modern
    # SearxNG, a bare string on older builds.
    answers: list[str] = []
    for a in data.get("answers") or []:
        raw = a.get("answer", "") if isinstance(a, dict) else a
        text = str(raw or "").strip()  # coerce: some engines return non-str answers
        if text:
            answers.append(text)
    if answers:
        parts.append("Answer: " + " ".join(answers))

    # Infoboxes (Wikipedia/Wikidata side panels). One is plenty of context.
    for box in data.get("infoboxes") or []:
        content = (box.get("content") or "").strip()
        if content:
            parts.append(content[:500])
            break

    results = data.get("results") or []
    if results:
        lines = []
        for i, r in enumerate(results[:max_results], 1):
            title = r.get("title", "")
            url = r.get("url", "")
            content = (r.get("content") or "")[:500]
            lines.append(f"{i}. [{title}]({url})\n   {content}")
        parts.append("\n".join(lines))

    if parts:
        return "\n\n".join(parts)

    # No usable results. Surface unresponsive engines when present — this is
    # the tell-tale of an all-rate-limited or misconfigured instance, and a
    # plain "no results" would otherwise hide it from the operator.
    unresponsive = data.get("unresponsive_engines") or []
    if unresponsive:
        names = ", ".join(
            str(u[0]) if isinstance(u, (list, tuple)) and u else str(u) for u in unresponsive
        )
        return f"No results for '{query}'. Unresponsive engines: {names}."
    return f"No results for '{query}'."


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def resolve_web_search_client(
    backend: str,
    searxng_url: str | None,
    searxng_engines: str = "",
    mcp_client: Any | None = None,
    timeout: float = 120,
) -> WebSearchClient | None:
    """Return a search client based on configuration, or None if unavailable.

    Args:
        backend: ``""`` (auto), ``"searxng"``, or ``"mcp:server:tool"``
        searxng_url: SearxNG base URL (None/empty if not configured)
        searxng_engines: comma-separated SearxNG engine list ("" = instance default)
        mcp_client: MCPClientManager instance (for MCP backends)
        timeout: HTTP/tool timeout in seconds
    """
    url = (searxng_url or "").strip()

    if backend == "searxng":
        if url:
            return SearXNGClient(url, engines=searxng_engines, timeout=timeout)
        return None

    if backend.startswith("mcp:"):
        parts = backend.split(":", 2)
        if len(parts) == 3 and mcp_client is not None:
            _, server, tool = parts
            prefixed = f"mcp__{server}__{tool}"
            # Boot-time gate: ``is_mcp_tool`` without ``user_id`` returns
            # True only for static-path catalogs. Pool-backed
            # (``auth_type=oauth_user``) servers are NEVER reachable via
            # the per-node web_search client because the boot-time
            # resolver has no per-user identity to attach a bearer to —
            # the resolved client would be shared across requests, but
            # the bearer can't be (RFC §3, invariant 8 corollary).
            if mcp_client.is_mcp_tool(prefixed):
                # Defence-in-depth: even if a future change widens
                # ``_tool_map`` to include oauth_user names by accident,
                # refuse the backend explicitly. ``server_auth_type``
                # is an in-memory accessor — this resolver is invoked
                # per LLM turn, so a SQL hop here would amplify token
                # cost on every chat round.
                if mcp_client.server_auth_type(server) == "oauth_user":
                    log.warning(
                        "web_search_backend %r points at oauth_user MCP server; "
                        "per-node web search cannot use per-user tokens — disabling",
                        backend,
                    )
                    return None
                return MCPSearchClient(mcp_client, prefixed, timeout=timeout)
        return None

    if backend == "":
        # Auto-detect: SearxNG (URL configured) > None. MCP backends are
        # explicit-only — there is no canonical "the search tool" to pick.
        if url:
            return SearXNGClient(url, engines=searxng_engines, timeout=timeout)
        return None

    log.warning("Unknown web_search_backend %r — web search disabled", backend)
    return None
