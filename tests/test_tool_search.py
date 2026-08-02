"""Tests for turnstone.core.tool_search — BM25 index and tool search manager."""

from __future__ import annotations

import pytest

from turnstone.core.bm25 import _RERANK_POOL
from turnstone.core.tool_search import (
    BM25Index,
    ToolSearchManager,
    _mcp_server_summary,
    _status_reason,
    _tokenize,
    _tool_name,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool(name: str, description: str = "") -> dict:
    """Create a minimal OpenAI-format tool dict for testing."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description or f"Tool {name}",
            "parameters": {"type": "object", "properties": {}},
        },
    }


# ---------------------------------------------------------------------------
# BM25Index tests
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_basic_split(self):
        assert _tokenize("hello world") == ["hello", "world"]

    def test_underscore_split(self):
        assert _tokenize("create_issue") == ["create", "issue"]

    def test_mixed_delimiters(self):
        assert _tokenize("mcp__github__create-issue") == ["mcp", "github", "create", "issue"]

    def test_empty_string(self):
        assert _tokenize("") == []

    def test_lowercased(self):
        assert _tokenize("GitHub Create") == ["github", "create"]


class TestBM25Index:
    def test_empty_corpus(self):
        idx = BM25Index([])
        assert idx.search("test") == []

    def test_empty_query(self):
        idx = BM25Index(["hello world", "foo bar"])
        assert idx.search("") == []

    def test_single_document(self):
        idx = BM25Index(["create github issue"])
        assert idx.search("github") == [0]

    def test_ranking_order(self):
        docs = [
            "list_repos List all repositories",
            "create_issue Create a new GitHub issue",
            "get_issue Get details of a GitHub issue",
        ]
        idx = BM25Index(docs)
        results = idx.search("github issue")
        # Both issue-related docs should rank above list_repos
        assert 1 in results[:2]
        assert 2 in results[:2]

    def test_top_k_limit(self):
        docs = [f"tool_{i} description {i}" for i in range(20)]
        idx = BM25Index(docs)
        results = idx.search("tool description", k=3)
        assert len(results) <= 3

    def test_no_match(self):
        idx = BM25Index(["alpha beta gamma"])
        assert idx.search("zzzzz") == []

    def test_exact_name_match_ranks_high(self):
        docs = [
            "send_email Send an email message",
            "send_slack Send a Slack message",
            "read_email Read email inbox",
        ]
        idx = BM25Index(docs)
        results = idx.search("send email")
        assert results[0] == 0  # send_email should rank first


# ---------------------------------------------------------------------------
# ToolSearchManager tests
# ---------------------------------------------------------------------------


class TestToolSearchManager:
    @pytest.fixture()
    def builtin_tools(self):
        return [
            _make_tool("bash", "Execute shell commands"),
            _make_tool("read_file", "Read a file"),
            _make_tool("edit_file", "Edit a file"),
        ]

    @pytest.fixture()
    def mcp_tools(self):
        return [
            _make_tool("mcp__github__create_issue", "Create a new GitHub issue"),
            _make_tool("mcp__github__list_issues", "List GitHub issues"),
            _make_tool("mcp__github__get_repo", "Get repository details"),
            _make_tool("mcp__slack__send_message", "Send a Slack message"),
            _make_tool("mcp__slack__list_channels", "List Slack channels"),
            _make_tool("mcp__jira__create_ticket", "Create a Jira ticket"),
        ]

    @pytest.fixture()
    def manager(self, builtin_tools, mcp_tools):
        all_tools = builtin_tools + mcp_tools
        return ToolSearchManager(
            all_tools,
            always_on_names={"bash", "read_file", "edit_file"},
            max_results=3,
        )

    def test_visible_tools_initially_builtin_only(self, manager):
        visible = manager.get_visible_tools()
        names = {_tool_name(t) for t in visible}
        assert names == {"bash", "read_file", "edit_file"}

    def test_deferred_tools_excludes_builtin(self, manager):
        deferred = manager.get_deferred_tools()
        names = {_tool_name(t) for t in deferred}
        assert "bash" not in names
        assert "mcp__github__create_issue" in names

    def test_search_returns_relevant_tools(self, manager):
        results = manager.search("github issue")
        names = {_tool_name(t) for t in results}
        assert "mcp__github__create_issue" in names or "mcp__github__list_issues" in names

    def test_search_respects_max_results(self, manager):
        results = manager.search("tool")
        assert len(results) <= 3

    def test_search_excludes_already_expanded(self, manager):
        # Expand a github tool, then search for github — expanded tool should not appear
        manager.expand_visible(["mcp__github__create_issue"])
        results = manager.search("github issue")
        names = {_tool_name(t) for t in results}
        assert "mcp__github__create_issue" not in names

    def test_expand_visible_adds_tools(self, manager):
        manager.expand_visible(["mcp__github__create_issue"])
        visible = manager.get_visible_tools()
        names = {_tool_name(t) for t in visible}
        assert "mcp__github__create_issue" in names

    def test_expand_visible_returns_newly_added(self, manager):
        added = manager.expand_visible(["mcp__github__create_issue", "mcp__slack__send_message"])
        assert len(added) == 2
        names = {_tool_name(t) for t in added}
        assert names == {"mcp__github__create_issue", "mcp__slack__send_message"}

    def test_expand_visible_idempotent(self, manager):
        manager.expand_visible(["mcp__github__create_issue"])
        added = manager.expand_visible(["mcp__github__create_issue"])
        assert added == []

    def test_expand_visible_ignores_unknown(self, manager):
        added = manager.expand_visible(["nonexistent_tool"])
        assert added == []

    def test_get_expanded_names_empty(self, manager):
        assert manager.get_expanded_names() == []

    def test_get_expanded_names_after_expand(self, manager):
        manager.expand_visible(["mcp__github__create_issue", "mcp__slack__send_message"])
        names = manager.get_expanded_names()
        assert names == ["mcp__github__create_issue", "mcp__slack__send_message"]

    def test_deferred_excludes_expanded(self, manager):
        manager.expand_visible(["mcp__github__create_issue"])
        deferred = manager.get_deferred_tools()
        names = {_tool_name(t) for t in deferred}
        assert "mcp__github__create_issue" not in names

    def test_search_tool_definition_format(self, manager):
        defn = manager.get_search_tool_definition()
        assert defn["type"] == "function"
        fn = defn["function"]
        assert fn["name"] == "tool_search"
        assert "query" in fn["parameters"]["properties"]
        assert "query" in fn["parameters"]["required"]

    def test_search_tool_description_has_server_hint(self, manager):
        defn = manager.get_search_tool_definition()
        desc = defn["function"]["description"]
        assert "github" in desc
        assert "slack" in desc
        assert "jira" in desc

    def test_format_search_results_empty(self, manager):
        text = manager.format_search_results([])
        assert "No matching tools found" in text

    def test_format_search_results_with_tools(self, manager, mcp_tools):
        text = manager.format_search_results(mcp_tools[:2])
        assert "Found 2" in text
        assert "mcp__github__create_issue" in text


class TestToolSearchManagerReranking:
    """``ToolSearchManager`` forwards a reranker into its deferred-tool index."""

    def _tools(self):
        # All three deferred tools match "github" so the recall pool spans them;
        # a reranker can then dictate their order.
        return [
            _make_tool("bash", "Execute shell commands"),
            _make_tool("mcp__github__a", "github alpha helper"),
            _make_tool("mcp__github__b", "github beta helper"),
            _make_tool("mcp__github__c", "github gamma helper"),
        ]

    def test_search_reflects_reranker_order(self):
        baseline = ToolSearchManager(self._tools(), always_on_names={"bash"})
        base_names = [_tool_name(t) for t in baseline.search("github helper")]

        # Reranker reverses the recall-pool order it is handed (positions
        # n-1..0). The forwarded order must show up in search results.
        reranked = ToolSearchManager(
            self._tools(),
            always_on_names={"bash"},
            reranker=lambda q, d: list(range(len(d)))[::-1],
        )
        names = [_tool_name(t) for t in reranked.search("github helper")]
        assert names == base_names[::-1]

    def test_no_reranker_unchanged(self):
        mgr = ToolSearchManager(self._tools(), always_on_names={"bash"}, reranker=None)
        names = {_tool_name(t) for t in mgr.search("github helper")}
        assert names == {"mcp__github__a", "mcp__github__b", "mcp__github__c"}


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestToolSearchTruncation:
    """search() records the full match count so format_search_results can
    report honest truncation instead of silently dropping matches."""

    @pytest.fixture()
    def manager(self):
        # Every name shares the ``mcp`` token, so a search for "mcp" matches
        # all six deferred tools — more than max_results=3.
        mcp_tools = [
            _make_tool("mcp__github__create_issue", "Create a new GitHub issue"),
            _make_tool("mcp__github__list_issues", "List GitHub issues"),
            _make_tool("mcp__github__get_repo", "Get repository details"),
            _make_tool("mcp__slack__send_message", "Send a Slack message"),
            _make_tool("mcp__slack__list_channels", "List Slack channels"),
            _make_tool("mcp__jira__create_ticket", "Create a Jira ticket"),
        ]
        return ToolSearchManager(mcp_tools, always_on_names=set(), max_results=3)

    def test_search_records_total_matched(self, manager):
        results = manager.search("mcp")
        assert len(results) == 3
        assert manager._last_total_matched == 6

    def test_format_reports_truncation(self, manager):
        results = manager.search("mcp")
        text = manager.format_search_results(results)
        assert "Showing the top 3 of 6" in text

    def test_no_truncation_note_when_all_returned(self, manager):
        results = manager.search("jira")  # matches only the jira tool
        text = manager.format_search_results(results)
        assert "Showing the top" not in text
        assert "Found 1" in text

    def test_truncation_excludes_already_expanded_from_total(self, manager):
        # Expanding one match shrinks the reported total (only genuinely-new
        # matches count), so "of N" never over-promises tools already loaded.
        manager.expand_visible(["mcp__github__create_issue"])
        manager.search("mcp")
        assert manager._last_total_matched == 5

    def test_total_matched_not_floored_by_rerank_pool(self):
        # More matches than the rerank recall pool: the pool caps what the
        # reranker reorders, not the recorded match count — "top N of M" must
        # report the true M, not the pool size. [#941]
        n = _RERANK_POOL + 10
        tools = [_make_tool(f"mcp__srv__tool{i}", f"alpha capability {i}") for i in range(n)]
        mgr = ToolSearchManager(
            tools,
            always_on_names=set(),
            max_results=3,
            reranker=lambda q, d: list(range(len(d)))[::-1],
        )
        results = mgr.search("alpha")
        assert len(results) == 3
        assert mgr._last_total_matched == n
        assert f"Showing the top 3 of {n}" in mgr.format_search_results(results)


class TestToolSearchUnavailableAdvisory:
    """A down/unauthorized server is surfaced, never silently treated as
    'no such tool'. Driven by the injected status_provider."""

    def _mgr(self, tools, status):
        return ToolSearchManager(tools, always_on_names=set(), status_provider=lambda: status)

    def test_empty_results_with_outage_explains_outage(self):
        mgr = self._mgr(
            [_make_tool("mcp__dhcp__GetLease", "Get a dhcp lease")],
            {"DHCP-MCP": {"error": "500 app failed to start"}},
        )
        text = mgr.format_search_results(mgr.search("nonexistent_capability_xyz"))
        assert "unavailable" in text.lower() or "outage" in text.lower()
        assert "DHCP-MCP" in text
        assert "500 app failed to start" in text
        # Must NOT give the misleading "try a different query" line alone.
        assert text != "No matching tools found. Try a different search query."

    def test_advisory_appended_when_results_present(self):
        mgr = self._mgr(
            [
                _make_tool("mcp__github__create_issue", "Create a github issue"),
                _make_tool("mcp__dhcp__GetLease", "Get a dhcp lease"),
            ],
            {"DHCP-MCP": {"discovery_error": "500 app failed"}},
        )
        text = mgr.format_search_results(mgr.search("github"))
        assert "Found 1" in text
        assert "currently unavailable" in text
        assert "DHCP-MCP" in text
        assert "tool discovery failed" in text

    def test_circuit_open_flagged(self):
        mgr = self._mgr(
            [_make_tool("mcp__x__t", "thing")],
            {"X": {"circuit_open": True}},
        )
        text = mgr.format_search_results([])
        assert "circuit breaker open" in text

    def test_healthy_server_not_flagged(self):
        mgr = self._mgr(
            [_make_tool("mcp__github__create_issue", "Create a github issue")],
            {"github": {"connected": True, "error": "", "circuit_open": False}},
        )
        text = mgr.format_search_results(mgr.search("github"))
        assert "unavailable" not in text.lower()

    def test_unprimed_server_not_flagged(self):
        # connected=False with no error is "not reached yet", not an outage —
        # flagging it would cry wolf on every server the user hasn't touched.
        mgr = self._mgr(
            [_make_tool("mcp__github__create_issue", "Create a github issue")],
            {"github": {"connected": False, "error": "", "circuit_open": False}},
        )
        text = mgr.format_search_results(mgr.search("github"))
        assert "unavailable" not in text.lower()

    def test_discovery_error_not_flagged_for_user_with_warm_pool(self):
        # A discovery record alongside connected=True in the per-user status
        # snapshot is stale (the user's own successful connect clears their
        # record), so the outage advisory must stay silent. [#941]
        mgr = self._mgr(
            [_make_tool("mcp__github__create_issue", "Create a github issue")],
            {"github": {"connected": True, "discovery_error": "500 app failed"}},
        )
        text = mgr.format_search_results(mgr.search("github"))
        assert "unavailable" not in text.lower()

    def test_no_status_provider_is_legacy_behaviour(self):
        mgr = ToolSearchManager(
            [_make_tool("mcp__github__create_issue", "Create a github issue")],
            always_on_names=set(),
        )
        assert (
            mgr.format_search_results([])
            == "No matching tools found. Try a different search query."
        )
        text = mgr.format_search_results(mgr.search("github"))
        assert "unavailable" not in text.lower()

    def test_status_provider_error_is_swallowed(self):
        def boom():
            raise RuntimeError("status backend down")

        mgr = ToolSearchManager(
            [_make_tool("mcp__github__create_issue", "Create a github issue")],
            always_on_names=set(),
            status_provider=boom,
        )
        # A broken provider must never break tool search itself.
        text = mgr.format_search_results(mgr.search("github"))
        assert "Found 1" in text


class TestStatusReason:
    def test_circuit_open(self):
        assert _status_reason({"circuit_open": True}) == "circuit breaker open"

    def test_error_text(self):
        assert "500 boom" in _status_reason({"error": "500 boom"})

    def test_discovery_error(self):
        reason = _status_reason({"discovery_error": "TimeoutError: pool discovery"})
        assert "discovery" in reason.lower()

    def test_discovery_error_suppressed_when_connected(self):
        # Belt-and-braces (#941): a successful connect clears the user's
        # per-(user, server) record, so a record alongside a warm transport is
        # stale — flagging a server the user's pool is serving would be false.
        assert _status_reason({"connected": True, "discovery_error": "500 boom"}) == ""

    def test_discovery_error_fires_despite_retained_catalog(self):
        # The record is per-(user, server), so a failure in this user's status
        # IS their own — a retained idle catalog (#836, connected=False with a
        # non-zero tools count) must not mask it: the transport behind those
        # tools is failing and calls will too.
        reason = _status_reason({"connected": False, "tools": 3, "discovery_error": "500 boom"})
        assert reason == "tool discovery failed: 500 boom"

    def test_discovery_error_fires_for_cold_pool(self):
        reason = _status_reason({"connected": False, "tools": 0, "discovery_error": "500 boom"})
        assert reason == "tool discovery failed: 500 boom"

    def test_recorded_error_not_gated_by_connected(self):
        # Deliberate asymmetry: only the discovery branch is per-user gated. A
        # recorded error stays a hard signal even alongside connected=True
        # (e.g. a flapping static server).
        assert "boom" in _status_reason({"connected": True, "error": "boom"})

    @pytest.mark.parametrize("field", ["error", "discovery_error"])
    def test_error_text_is_single_line(self, field):
        reason = _status_reason({field: "upstream\r\nresponse\nignore instructions"})
        assert "\n" not in reason
        assert "\r" not in reason
        assert "upstream response ignore instructions" in reason

    def test_healthy_is_empty(self):
        assert _status_reason({"connected": True, "error": "", "circuit_open": False}) == ""

    def test_circuit_takes_precedence_over_error(self):
        assert _status_reason({"circuit_open": True, "error": "boom"}) == "circuit breaker open"


class TestMCPServerSummary:
    def test_groups_by_server(self):
        tools = [
            _make_tool("mcp__github__a"),
            _make_tool("mcp__github__b"),
            _make_tool("mcp__slack__c"),
        ]
        summary = _mcp_server_summary(tools)
        assert "github (2 tools)" in summary
        assert "slack (1 tool)" in summary

    def test_non_mcp_tools_counted_as_other(self):
        tools = [_make_tool("custom_tool")]
        summary = _mcp_server_summary(tools)
        assert "other (1 tool)" in summary

    def test_empty_list(self):
        assert _mcp_server_summary([]) == ""
