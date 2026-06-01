"""Tests for turnstone.core.web_search — pluggable web search backends."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from turnstone.core.web_search import (
    MCPSearchClient,
    SearXNGClient,
    _format_searxng,
    resolve_web_search_client,
)

# A faithful SearxNG JSON payload — shape verified against searxng/searxng:latest
# (top-level keys, per-result fields, [name, reason] unresponsive_engines pairs).
SEARXNG_JSON = {
    "query": "python",
    "results": [
        {
            "title": "Python.org",
            "url": "https://python.org",
            "content": "The official home of the Python programming language.",
            "engine": "duckduckgo",
            "score": 3.0,
        },
        {
            "title": "PyPI",
            "url": "https://pypi.org",
            "content": "Find, install and publish Python packages.",
            "engine": "brave",
            "score": 2.0,
        },
    ],
    "answers": [{"answer": "Python is a programming language.", "engine": "wikidata"}],
    "infoboxes": [{"infobox": "Python", "content": "A high-level language.", "urls": []}],
    "corrections": [],
    "suggestions": ["python tutorial"],
    "unresponsive_engines": [],
}


def _mock_httpx_get(handler):
    """Patch target for ``web_search.httpx.get`` that routes the call through a
    real ``httpx.MockTransport``. The request flows through genuine httpx URL/
    param encoding and response parsing — a true boundary, not a bare MagicMock.
    """
    client = httpx.Client(transport=httpx.MockTransport(handler))

    def _get(url, **kwargs):
        return client.get(url, **kwargs)

    return _get


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------


class TestFormatSearXNG:
    def test_answer_infobox_and_results(self):
        out = _format_searxng(SEARXNG_JSON, "python")
        assert "Answer: Python is a programming language." in out
        assert "A high-level language." in out  # infobox content
        assert "[Python.org](https://python.org)" in out
        assert "[PyPI](https://pypi.org)" in out

    def test_results_only(self):
        data = {"results": [{"title": "T", "url": "http://t", "content": "C"}]}
        out = _format_searxng(data, "q")
        assert "Answer:" not in out
        assert "[T](http://t)" in out

    def test_respects_max_results(self):
        data = {
            "results": [
                {"title": f"T{i}", "url": f"http://t/{i}", "content": ""} for i in range(10)
            ]
        }
        out = _format_searxng(data, "q", max_results=3)
        assert "[T0]" in out and "[T2]" in out
        assert "[T3]" not in out

    def test_answer_as_bare_string(self):
        # Older SearxNG builds put bare strings in ``answers``.
        data = {"answers": ["42"], "results": []}
        out = _format_searxng(data, "q")
        assert "Answer: 42" in out

    def test_answer_non_string_value_coerced(self):
        # Some engines (calculator/wikidata) return a non-string answer value;
        # the formatter must coerce it, not crash on .strip().
        data = {"answers": [{"answer": 42}], "results": []}
        out = _format_searxng(data, "q")
        assert "Answer: 42" in out

    def test_no_results(self):
        out = _format_searxng({"results": []}, "nothing")
        assert "No results for 'nothing'" in out

    def test_no_results_surfaces_unresponsive_engines(self):
        data = {
            "results": [],
            "unresponsive_engines": [["duckduckgo", "CAPTCHA"], ["google", "timeout"]],
        }
        out = _format_searxng(data, "q")
        assert "No results for 'q'" in out
        assert "duckduckgo" in out and "google" in out


# ---------------------------------------------------------------------------
# SearXNGClient
# ---------------------------------------------------------------------------


class TestSearXNGClient:
    def test_request_construction_boundary(self):
        """Drive a real httpx request through MockTransport and assert the
        URL + query params the client builds."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json=SEARXNG_JSON)

        with patch("turnstone.core.web_search.httpx.get", _mock_httpx_get(handler)):
            client = SearXNGClient(
                "http://searxng:8080", engines="duckduckgo,wikipedia", timeout=10
            )
            out = client.search("python", max_results=2, category="news")

        assert captured["url"].startswith("http://searxng:8080/search")
        assert captured["params"]["q"] == "python"
        assert captured["params"]["format"] == "json"
        assert captured["params"]["categories"] == "news"  # category -> categories
        assert captured["params"]["engines"] == "duckduckgo,wikipedia"
        assert "[Python.org](https://python.org)" in out

    def test_category_it_maps_to_categories(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json={"results": []})

        with patch("turnstone.core.web_search.httpx.get", _mock_httpx_get(handler)):
            SearXNGClient("http://searxng:8080").search("q", category="it")

        assert captured["params"]["categories"] == "it"

    def test_general_category_omits_categories(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json={"results": []})

        with patch("turnstone.core.web_search.httpx.get", _mock_httpx_get(handler)):
            SearXNGClient("http://searxng:8080").search("q", category="general")

        assert "categories" not in captured["params"]

    def test_no_engines_omits_param(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json={"results": []})

        with patch("turnstone.core.web_search.httpx.get", _mock_httpx_get(handler)):
            SearXNGClient("http://searxng:8080", engines="").search("q")

        assert "engines" not in captured["params"]

    def test_base_url_trailing_slash_normalized(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"results": []})

        with patch("turnstone.core.web_search.httpx.get", _mock_httpx_get(handler)):
            SearXNGClient("http://searxng:8080/").search("q")

        assert captured["url"].startswith("http://searxng:8080/search")  # no // dupe

    def test_4xx_raises(self):
        """A 403 (the classic 'json not enabled' failure) propagates as an
        HTTPStatusError so the caller surfaces a real error."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="forbidden")

        with patch("turnstone.core.web_search.httpx.get", _mock_httpx_get(handler)):
            client = SearXNGClient("http://searxng:8080")
            with pytest.raises(httpx.HTTPStatusError):
                client.search("q")


# ---------------------------------------------------------------------------
# MCPSearchClient
# ---------------------------------------------------------------------------


class TestMCPSearchClient:
    def test_delegates_to_mcp(self):
        mcp = MagicMock()
        mcp.call_tool_sync.return_value = "MCP search results"
        client = MCPSearchClient(mcp, "mcp__ddg__search", timeout=30)
        result = client.search("test", max_results=3, category="news")
        mcp.call_tool_sync.assert_called_once_with(
            "mcp__ddg__search",
            {"query": "test", "max_results": 3, "category": "news"},
            timeout=30,
        )
        assert result == "MCP search results"


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class TestResolveClient:
    def test_auto_searxng_when_url_set(self):
        client = resolve_web_search_client("", searxng_url="http://searxng:8080")
        assert isinstance(client, SearXNGClient)

    def test_auto_none_when_no_url(self):
        assert resolve_web_search_client("", searxng_url=None) is None
        assert resolve_web_search_client("", searxng_url="") is None
        assert resolve_web_search_client("", searxng_url="   ") is None

    def test_explicit_searxng(self):
        client = resolve_web_search_client("searxng", searxng_url="http://searxng:8080")
        assert isinstance(client, SearXNGClient)

    def test_explicit_searxng_no_url(self):
        assert resolve_web_search_client("searxng", searxng_url=None) is None

    def test_engines_threaded_through(self):
        client = resolve_web_search_client(
            "searxng", searxng_url="http://searxng:8080", searxng_engines="duckduckgo"
        )
        assert isinstance(client, SearXNGClient)
        assert client._engines == "duckduckgo"

    def test_mcp_backend(self):
        mcp = MagicMock()
        mcp.is_mcp_tool.return_value = True
        mcp.server_auth_type.return_value = None
        client = resolve_web_search_client("mcp:search:web", searxng_url=None, mcp_client=mcp)
        assert isinstance(client, MCPSearchClient)
        mcp.is_mcp_tool.assert_called_with("mcp__search__web")

    def test_mcp_backend_not_connected(self):
        mcp = MagicMock()
        mcp.is_mcp_tool.return_value = False
        client = resolve_web_search_client("mcp:search:web", searxng_url=None, mcp_client=mcp)
        assert client is None

    def test_mcp_backend_no_client(self):
        client = resolve_web_search_client("mcp:search:web", searxng_url=None, mcp_client=None)
        assert client is None

    def test_unknown_backend_returns_none(self):
        client = resolve_web_search_client("typo_backend", searxng_url="http://searxng:8080")
        assert client is None

    def test_resolve_web_search_client_rejects_oauth_user_backend(self):
        """A web_search backend pointing at an ``auth_type=oauth_user``
        MCP server MUST be rejected at boot — per-node web_search
        cannot carry per-user tokens, so resolving the backend would
        guarantee a 401-on-call instead of a clean disablement.

        Phase 7 invariant 8 corollary: pool tools are user-scoped;
        every entry point that lacks per-user identity (web_search
        boot resolver, eval harness, CLI default) MUST refuse them
        rather than silently produce a broken client.

        Verified by reverting the ``server_auth_type(...) == 'oauth_user'``
        guard in ``resolve_web_search_client``: the resolver returns
        an ``MCPSearchClient`` whose ``call_tool_sync`` would surface
        a 401 / consent_required structured error on every search.
        """
        mcp = MagicMock()
        mcp.is_mcp_tool.return_value = True  # name resolves
        mcp.server_auth_type.return_value = "oauth_user"
        client = resolve_web_search_client(
            "mcp:oauth-search:search", searxng_url=None, mcp_client=mcp
        )
        assert client is None, (
            "oauth_user-backed web_search backend resolved to a non-None client; "
            "boot-time guard missing or regressed."
        )
        # Per-turn callers must read from the in-memory cache, never
        # the SQL helper — perf regression guard.
        mcp.server_auth_type.assert_called_with("oauth-search")
        assert not mcp._lookup_server_row.called, (
            "resolver issued a SQL roundtrip via _lookup_server_row; "
            "per-turn web_search backend resolution must use the "
            "in-memory server_auth_type accessor."
        )

    def test_resolve_web_search_client_accepts_static_backend(self):
        """Static-path (``auth_type=none`` or ``static``) MCP backends
        still resolve cleanly — the new guard ONLY rejects oauth_user.
        """
        mcp = MagicMock()
        mcp.is_mcp_tool.return_value = True
        mcp.server_auth_type.return_value = None
        client = resolve_web_search_client(
            "mcp:static-search:search", searxng_url=None, mcp_client=mcp
        )
        assert isinstance(client, MCPSearchClient)
