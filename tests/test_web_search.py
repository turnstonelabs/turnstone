"""Tests for turnstone.core.web_search — pluggable web search backends."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from turnstone.core.web_search import (
    DuckDuckGoClient,
    MCPSearchClient,
    TavilyClient,
    _format_ddg,
    _format_tavily,
    resolve_web_search_client,
)

# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


class TestFormatTavily:
    def test_formats_answer_and_results(self):
        data = {
            "answer": "Python is great",
            "results": [
                {"title": "Python.org", "url": "https://python.org", "content": "Official site"},
                {"title": "PyPI", "url": "https://pypi.org", "content": "Package index"},
            ],
        }
        out = _format_tavily(data, "python")
        assert "Answer: Python is great" in out
        assert "[Python.org](https://python.org)" in out
        assert "[PyPI](https://pypi.org)" in out

    def test_no_results(self):
        out = _format_tavily({"results": []}, "nothing")
        assert "No results for 'nothing'" in out

    def test_no_answer(self):
        data = {
            "results": [{"title": "T", "url": "http://t", "content": "C"}],
        }
        out = _format_tavily(data, "q")
        assert "Answer:" not in out
        assert "[T](http://t)" in out


class TestFormatDDG:
    def test_formats_results(self):
        results = [
            {"title": "DDG Result", "href": "https://ddg.example.com", "body": "Search body"},
        ]
        out = _format_ddg(results, "test")
        assert "[DDG Result](https://ddg.example.com)" in out
        assert "Search body" in out

    def test_no_results(self):
        out = _format_ddg([], "nothing")
        assert "No results for 'nothing'" in out


# ---------------------------------------------------------------------------
# Client tests
# ---------------------------------------------------------------------------


class TestTavilyClient:
    def test_search_calls_api(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "answer": "42",
            "results": [{"title": "T", "url": "http://t", "content": "C"}],
        }
        with patch("turnstone.core.web_search.httpx.post", return_value=mock_resp) as mock_post:
            client = TavilyClient("test-key", timeout=10)
            result = client.search("meaning of life", max_results=3)

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs.kwargs["json"]["query"] == "meaning of life"
        assert call_kwargs.kwargs["json"]["max_results"] == 3
        assert "Answer: 42" in result


class TestDuckDuckGoClient:
    def test_integration_via_mock_ddgs(self):
        """Patch the duckduckgo_search import inside DuckDuckGoClient.search."""
        mock_ddgs = MagicMock()
        mock_ddgs.__enter__ = MagicMock(return_value=mock_ddgs)
        mock_ddgs.__exit__ = MagicMock(return_value=False)
        mock_ddgs.text.return_value = [
            {"title": "DDG Result", "href": "https://ddg.co", "body": "Found it"},
        ]
        mock_module = MagicMock()
        mock_module.DDGS.return_value = mock_ddgs
        with patch.dict("sys.modules", {"ddgs": mock_module}):
            client = DuckDuckGoClient(timeout=10)
            result = client.search("test query", max_results=3)
        mock_ddgs.text.assert_called_once_with("test query", max_results=3)
        assert "[DDG Result](https://ddg.co)" in result
        assert "Found it" in result


class TestMCPSearchClient:
    def test_delegates_to_mcp(self):
        mcp = MagicMock()
        mcp.call_tool_sync.return_value = "MCP search results"
        client = MCPSearchClient(mcp, "mcp__ddg__search", timeout=30)
        result = client.search("test", max_results=3, topic="news")
        mcp.call_tool_sync.assert_called_once_with(
            "mcp__ddg__search",
            {"query": "test", "max_results": 3, "topic": "news"},
            timeout=30,
        )
        assert result == "MCP search results"


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class TestResolveClient:
    def test_auto_tavily_when_key_present(self):
        client = resolve_web_search_client("", tavily_key="key")
        assert isinstance(client, TavilyClient)

    def test_auto_ddg_when_no_tavily(self):
        with patch("turnstone.core.web_search._ddg_available", return_value=True):
            client = resolve_web_search_client("", tavily_key=None)
        assert isinstance(client, DuckDuckGoClient)

    def test_auto_none_when_nothing_available(self):
        with patch("turnstone.core.web_search._ddg_available", return_value=False):
            client = resolve_web_search_client("", tavily_key=None)
        assert client is None

    def test_explicit_tavily(self):
        client = resolve_web_search_client("tavily", tavily_key="key")
        assert isinstance(client, TavilyClient)

    def test_explicit_tavily_no_key(self):
        client = resolve_web_search_client("tavily", tavily_key=None)
        assert client is None

    def test_explicit_ddg(self):
        with patch("turnstone.core.web_search._ddg_available", return_value=True):
            client = resolve_web_search_client("ddg", tavily_key=None)
        assert isinstance(client, DuckDuckGoClient)

    def test_explicit_ddg_not_installed(self):
        with patch("turnstone.core.web_search._ddg_available", return_value=False):
            client = resolve_web_search_client("ddg", tavily_key=None)
        assert client is None

    def test_mcp_backend(self):
        mcp = MagicMock()
        mcp.is_mcp_tool.return_value = True
        client = resolve_web_search_client("mcp:ddg:search", tavily_key=None, mcp_client=mcp)
        assert isinstance(client, MCPSearchClient)
        mcp.is_mcp_tool.assert_called_with("mcp__ddg__search")

    def test_mcp_backend_not_connected(self):
        mcp = MagicMock()
        mcp.is_mcp_tool.return_value = False
        client = resolve_web_search_client("mcp:ddg:search", tavily_key=None, mcp_client=mcp)
        assert client is None

    def test_mcp_backend_no_client(self):
        client = resolve_web_search_client("mcp:ddg:search", tavily_key=None, mcp_client=None)
        assert client is None

    def test_unknown_backend_returns_none(self):
        client = resolve_web_search_client("typo_backend", tavily_key="key")
        assert client is None
