"""Tests for node_metadata storage methods."""

from __future__ import annotations

import json


class TestNodeMetadata:
    def test_set_and_get(self, storage):
        storage.set_node_metadata("node-1", "rack", json.dumps("us-east-1a"))
        rows = storage.get_node_metadata("node-1")
        assert len(rows) == 1
        assert rows[0]["key"] == "rack"
        assert json.loads(rows[0]["value"]) == "us-east-1a"
        assert rows[0]["source"] == "user"

    def test_set_with_source(self, storage):
        storage.set_node_metadata("node-1", "hostname", json.dumps("web-01"), source="auto")
        rows = storage.get_node_metadata("node-1")
        assert rows[0]["source"] == "auto"

    def test_upsert_overwrites(self, storage):
        storage.set_node_metadata("node-1", "rack", json.dumps("old"))
        storage.set_node_metadata("node-1", "rack", json.dumps("new"))
        rows = storage.get_node_metadata("node-1")
        assert len(rows) == 1
        assert json.loads(rows[0]["value"]) == "new"

    def test_complex_value(self, storage):
        val = {"model": "A100", "count": 4}
        storage.set_node_metadata("node-1", "gpu", json.dumps(val))
        rows = storage.get_node_metadata("node-1")
        assert json.loads(rows[0]["value"]) == val

    def test_list_value(self, storage):
        val = ["inference", "eval"]
        storage.set_node_metadata("node-1", "roles", json.dumps(val))
        rows = storage.get_node_metadata("node-1")
        assert json.loads(rows[0]["value"]) == val

    def test_get_empty(self, storage):
        rows = storage.get_node_metadata("nonexistent")
        assert rows == []

    def test_get_all_node_metadata(self, storage):
        storage.set_node_metadata("node-1", "rack", json.dumps("a"))
        storage.set_node_metadata("node-2", "rack", json.dumps("b"))
        storage.set_node_metadata("node-2", "os", json.dumps("Linux"))
        result = storage.get_all_node_metadata()
        assert "node-1" in result
        assert "node-2" in result
        assert len(result["node-1"]) == 1
        assert len(result["node-2"]) == 2
        node2_keys = {r["key"] for r in result["node-2"]}
        assert node2_keys == {"rack", "os"}

    def test_get_all_empty(self, storage):
        result = storage.get_all_node_metadata()
        assert result == {}

    def test_bulk_set(self, storage):
        entries = [
            ("hostname", json.dumps("web-01"), "auto"),
            ("os", json.dumps("Linux"), "auto"),
            ("rack", json.dumps("us-east-1a"), "config"),
        ]
        storage.set_node_metadata_bulk("node-1", entries)
        rows = storage.get_node_metadata("node-1")
        assert len(rows) == 3
        keys = {r["key"] for r in rows}
        assert keys == {"hostname", "os", "rack"}

    def test_bulk_set_upsert(self, storage):
        storage.set_node_metadata("node-1", "rack", json.dumps("old"), source="config")
        entries = [("rack", json.dumps("new"), "config")]
        storage.set_node_metadata_bulk("node-1", entries)
        rows = storage.get_node_metadata("node-1")
        assert len(rows) == 1
        assert json.loads(rows[0]["value"]) == "new"

    def test_delete(self, storage):
        storage.set_node_metadata("node-1", "rack", json.dumps("a"))
        deleted = storage.delete_node_metadata("node-1", "rack")
        assert deleted is True
        assert storage.get_node_metadata("node-1") == []

    def test_delete_nonexistent(self, storage):
        deleted = storage.delete_node_metadata("node-1", "nope")
        assert deleted is False

    def test_delete_by_source(self, storage):
        storage.set_node_metadata("node-1", "hostname", json.dumps("h"), source="auto")
        storage.set_node_metadata("node-1", "os", json.dumps("Linux"), source="auto")
        storage.set_node_metadata("node-1", "rack", json.dumps("a"), source="user")
        count = storage.delete_node_metadata_by_source("node-1", "auto")
        assert count == 2
        rows = storage.get_node_metadata("node-1")
        assert len(rows) == 1
        assert rows[0]["key"] == "rack"

    def test_delete_by_source_empty(self, storage):
        count = storage.delete_node_metadata_by_source("node-1", "auto")
        assert count == 0

    def test_filter_single_key(self, storage):
        storage.set_node_metadata("node-1", "rack", json.dumps("us-east-1a"))
        storage.set_node_metadata("node-2", "rack", json.dumps("us-west-2a"))
        result = storage.filter_nodes_by_metadata({"rack": json.dumps("us-east-1a")})
        assert result == {"node-1"}

    def test_filter_multiple_keys(self, storage):
        storage.set_node_metadata("node-1", "rack", json.dumps("a"))
        storage.set_node_metadata("node-1", "os", json.dumps("Linux"))
        storage.set_node_metadata("node-2", "rack", json.dumps("a"))
        storage.set_node_metadata("node-2", "os", json.dumps("Windows"))
        result = storage.filter_nodes_by_metadata(
            {
                "rack": json.dumps("a"),
                "os": json.dumps("Linux"),
            }
        )
        assert result == {"node-1"}

    def test_filter_no_match(self, storage):
        storage.set_node_metadata("node-1", "rack", json.dumps("a"))
        result = storage.filter_nodes_by_metadata({"rack": json.dumps("z")})
        assert result == set()

    def test_filter_empty_filters(self, storage):
        result = storage.filter_nodes_by_metadata({})
        assert result == set()

    def test_filter_partial_intersection_eliminates_all(self, storage):
        """First filter matches 2 nodes, second filter matches neither."""
        storage.set_node_metadata("node-1", "rack", json.dumps("a"))
        storage.set_node_metadata("node-2", "rack", json.dumps("a"))
        storage.set_node_metadata("node-1", "os", json.dumps("Linux"))
        storage.set_node_metadata("node-2", "os", json.dumps("Linux"))
        result = storage.filter_nodes_by_metadata(
            {"rack": json.dumps("a"), "region": json.dumps("eu")}
        )
        assert result == set()

    def test_upsert_preserves_created(self, storage):
        storage.set_node_metadata("node-1", "rack", json.dumps("old"))
        rows = storage.get_node_metadata("node-1")
        first_created = rows[0]["created"]

        storage.set_node_metadata("node-1", "rack", json.dumps("new"))
        rows = storage.get_node_metadata("node-1")
        assert rows[0]["created"] == first_created
        assert json.loads(rows[0]["value"]) == "new"

    def test_bulk_set_empty_list(self, storage):
        storage.set_node_metadata_bulk("node-1", [])
        rows = storage.get_node_metadata("node-1")
        assert rows == []

    def test_ordered_by_key(self, storage):
        storage.set_node_metadata("node-1", "zz", json.dumps("last"))
        storage.set_node_metadata("node-1", "aa", json.dumps("first"))
        rows = storage.get_node_metadata("node-1")
        assert rows[0]["key"] == "aa"
        assert rows[1]["key"] == "zz"

    def test_upsert_changes_source(self, storage):
        storage.set_node_metadata("node-1", "rack", json.dumps("a"), source="auto")
        storage.set_node_metadata("node-1", "rack", json.dumps("a"), source="user")
        rows = storage.get_node_metadata("node-1")
        assert rows[0]["source"] == "user"

    def test_delete_by_source_does_not_affect_other_nodes(self, storage):
        storage.set_node_metadata("node-1", "hostname", json.dumps("h1"), source="auto")
        storage.set_node_metadata("node-2", "hostname", json.dumps("h2"), source="auto")
        storage.delete_node_metadata_by_source("node-1", "auto")
        rows = storage.get_node_metadata("node-2")
        assert len(rows) == 1
        assert rows[0]["key"] == "hostname"

    def test_filter_returns_multiple_matches(self, storage):
        storage.set_node_metadata("node-1", "rack", json.dumps("a"))
        storage.set_node_metadata("node-2", "rack", json.dumps("a"))
        storage.set_node_metadata("node-3", "rack", json.dumps("b"))
        result = storage.filter_nodes_by_metadata({"rack": json.dumps("a")})
        assert result == {"node-1", "node-2"}
