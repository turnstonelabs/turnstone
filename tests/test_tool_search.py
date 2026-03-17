"""Tests for turnstone.core.tool_search — BM25 index and tool search manager."""

from __future__ import annotations

import pytest

from turnstone.core.tool_search import (
    BM25Index,
    ToolSearchManager,
    _mcp_server_summary,
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


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


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
