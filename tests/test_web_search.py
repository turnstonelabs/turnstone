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


# ---------------------------------------------------------------------------
# Reranking
# ---------------------------------------------------------------------------


def _results(*titles):
    return {
        "results": [
            {"title": t, "url": f"http://{t.lower()}", "content": f"{t} snippet"} for t in titles
        ]
    }


class TestWebSearchReranking:
    def test_reorders_results_by_reranker_output(self):
        # Reranker promotes index 2, then 0, then 1.
        out = _format_searxng(
            _results("A", "B", "C"), "q", max_results=3, reranker=lambda q, d: [2, 0, 1]
        )
        assert out.index("[C]") < out.index("[A]") < out.index("[B]")

    def test_reranker_receives_title_and_snippet(self):
        seen: dict = {}

        def rr(query, docs):
            seen["query"] = query
            seen["docs"] = docs
            return list(range(len(docs)))

        _format_searxng(_results("Py", "X"), "find me", reranker=rr)
        assert seen["query"] == "find me"
        assert seen["docs"][0] == "Py\nPy snippet"

    def test_error_falls_back_to_native_order(self):
        def boom(query, docs):
            raise RuntimeError("rerank endpoint down")

        out = _format_searxng(_results("A", "B"), "q", reranker=boom)
        assert out.index("[A]") < out.index("[B]")  # native order preserved

    def test_none_order_falls_back_to_native_order(self):
        # A reranker that returns None (or any non-iterable) must fall back, not
        # raise — list() materializes it inside the guarded block.
        out = _format_searxng(_results("A", "B"), "q", reranker=lambda q, d: None)
        assert out.index("[A]") < out.index("[B]")  # native order preserved

    def test_non_int_indices_ignored(self):
        # bool is an int subclass; True/False must not be honored as indices 1/0.
        # Junk entries are skipped while valid ints still apply: only 2 and 0 here.
        out = _format_searxng(
            _results("A", "B", "C"),
            "q",
            max_results=3,
            reranker=lambda q, d: [True, "1", 2, 0],
        )
        # 2 -> C, 0 -> A; B (no valid index) is kept after. If True were honored
        # as index 1, B would jump to the front — this pins it last.
        assert out.index("[C]") < out.index("[A]") < out.index("[B]")

    def test_skipped_for_single_result(self):
        called = {"n": 0}

        def rr(query, docs):
            called["n"] += 1
            return [0]

        _format_searxng(_results("Only"), "q", reranker=rr)
        assert called["n"] == 0  # <=1 result: nothing to reorder

    def test_answers_and_infoboxes_untouched(self):
        # Reranking only reorders the results list, never answers/infoboxes.
        out = _format_searxng(SEARXNG_JSON, "python", reranker=lambda q, d: [1, 0])
        assert "Answer: Python is a programming language." in out
        assert "A high-level language." in out

    def test_partial_order_keeps_all_results(self):
        # A top_n-style reranker returns only a subset; the rest must survive.
        out = _format_searxng(
            _results("T0", "T1", "T2"), "q", max_results=3, reranker=lambda q, d: [1]
        )
        assert "[T0]" in out and "[T1]" in out and "[T2]" in out
        assert out.index("[T1]") < out.index("[T0]")  # T1 promoted

    def test_searxng_client_threads_reranker_kwarg(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_results("A", "B"))

        with patch("turnstone.core.web_search.httpx.get", _mock_httpx_get(handler)):
            out = SearXNGClient("http://searxng:8080").search("q", reranker=lambda q, d: [1, 0])

        assert out.index("[B]") < out.index("[A]")  # reranker applied via search()

    def test_pool_cap_preserves_tail_beyond_50(self):
        # >_RERANK_POOL (50) results: only the first 50 are reranked; the tail
        # must survive, appended in native order after the reranked pool.
        data = {
            "results": [
                {"title": f"R{i}", "url": f"http://r/{i}", "content": f"c{i}"} for i in range(60)
            ]
        }
        # Reranker reverses the 50-item pool it is handed.
        out = _format_searxng(
            data, "q", max_results=60, reranker=lambda q, d: list(range(len(d)))[::-1]
        )
        assert all(f"[R{i}]" in out for i in range(60))  # nothing dropped
        assert out.index("[R49]") < out.index("[R0]")  # pool reversed
        assert out.index("[R0]") < out.index("[R50]")  # reranked pool before the tail
        assert out.index("[R50]") < out.index("[R59]")  # tail kept in native order
