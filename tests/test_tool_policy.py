"""Tests for turnstone.core.policy."""

import pytest
from turnstone.core.storage._sqlite import SQLiteBackend
from turnstone.core.policy import evaluate_tool_policy, evaluate_tool_policies_batch


@pytest.fixture
def storage(tmp_path):
    path = str(tmp_path / "test.db")
    backend = SQLiteBackend(path)
    yield backend
    backend.close()


def test_no_policies_returns_none(storage):
    result = evaluate_tool_policy(storage, "bash")
    assert result is None


def test_exact_match_allow(storage):
    storage.create_tool_policy("p1", "allow-read", "read_file", "allow", 0)
    assert evaluate_tool_policy(storage, "read_file") == "allow"
    assert evaluate_tool_policy(storage, "write_file") is None


def test_glob_match_deny(storage):
    storage.create_tool_policy("p1", "block-bash", "bash*", "deny", 0)
    assert evaluate_tool_policy(storage, "bash") == "deny"
    assert evaluate_tool_policy(storage, "bash_exec") == "deny"
    assert evaluate_tool_policy(storage, "read_file") is None


def test_wildcard_match(storage):
    storage.create_tool_policy("p1", "ask-all", "*", "ask", 0)
    assert evaluate_tool_policy(storage, "anything") == "ask"


def test_priority_ordering(storage):
    # Higher priority wins
    storage.create_tool_policy("p1", "allow-all", "*", "allow", 0)
    storage.create_tool_policy("p2", "deny-bash", "bash*", "deny", 100)
    assert evaluate_tool_policy(storage, "bash") == "deny"  # p2 matches first (higher priority)
    assert evaluate_tool_policy(storage, "read_file") == "allow"  # p1 matches


def test_disabled_policy_skipped(storage):
    storage.create_tool_policy("p1", "block-bash", "bash*", "deny", 100, enabled=False)
    storage.create_tool_policy("p2", "allow-all", "*", "allow", 0)
    assert evaluate_tool_policy(storage, "bash") == "allow"  # p1 disabled, falls through to p2


def test_batch_evaluation(storage):
    storage.create_tool_policy("p1", "block-bash", "bash*", "deny", 100)
    storage.create_tool_policy("p2", "allow-read", "read_*", "allow", 50)
    results = evaluate_tool_policies_batch(storage, ["bash", "read_file", "write_file"])
    assert results["bash"] == "deny"
    assert results["read_file"] == "allow"
    assert results["write_file"] is None


def test_storage_failure_returns_none():
    """Graceful degradation on storage failure."""

    class BrokenStorage:
        def list_tool_policies(self, org_id=""):
            raise RuntimeError("boom")

    assert evaluate_tool_policy(BrokenStorage(), "bash") is None


def test_batch_storage_failure():
    class BrokenStorage:
        def list_tool_policies(self, org_id=""):
            raise RuntimeError("boom")

    results = evaluate_tool_policies_batch(BrokenStorage(), ["a", "b"])
    assert results == {"a": None, "b": None}


def test_first_match_wins(storage):
    # Two policies match, first by priority wins
    storage.create_tool_policy("p1", "deny-bash", "bash*", "deny", 100)
    storage.create_tool_policy("p2", "allow-bash", "bash*", "allow", 50)
    assert evaluate_tool_policy(storage, "bash_exec") == "deny"
