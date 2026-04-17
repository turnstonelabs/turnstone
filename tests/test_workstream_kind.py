"""Tests for Phase A schema additions: ``kind`` + ``parent_ws_id`` on workstreams.

Covers:

- ``register_workstream`` persists the two new columns.
- ``get_workstream`` returns the full row including the new fields.
- ``list_workstreams`` filters on ``kind`` and ``parent_ws_id`` correctly.
- ``parent_ws_id`` empty-string normalization at the storage edge.
- Defaults remain ``"interactive"`` / ``NULL`` when not specified.
- ``Workstream`` dataclass exposes ``kind`` / ``parent_ws_id`` / ``user_id``
  with safe defaults.
"""

from __future__ import annotations

import pytest

from turnstone.core.storage._sqlite import SQLiteBackend
from turnstone.core.workstream import Workstream


@pytest.fixture
def storage(tmp_path):
    return SQLiteBackend(str(tmp_path / "test.db"))


# ---------------------------------------------------------------------------
# register_workstream / get_workstream
# ---------------------------------------------------------------------------


def test_register_defaults_to_interactive_no_parent(storage):
    storage.register_workstream("ws-a")
    row = storage.get_workstream("ws-a")
    assert row is not None
    assert row["kind"] == "interactive"
    assert row["parent_ws_id"] is None


def test_register_coordinator_kind_and_parent(storage):
    storage.register_workstream(
        "ws-coord", node_id="console", user_id="user-1", kind="coordinator"
    )
    storage.register_workstream(
        "ws-child",
        node_id="node-a",
        user_id="user-1",
        kind="interactive",
        parent_ws_id="ws-coord",
    )

    coord = storage.get_workstream("ws-coord")
    child = storage.get_workstream("ws-child")

    assert coord is not None and child is not None
    assert coord["kind"] == "coordinator"
    assert coord["parent_ws_id"] is None
    assert coord["user_id"] == "user-1"

    assert child["kind"] == "interactive"
    assert child["parent_ws_id"] == "ws-coord"
    assert child["user_id"] == "user-1"


def test_register_normalizes_empty_parent_to_null(storage):
    """Empty-string parent_ws_id must be persisted as NULL so
    ``WHERE parent_ws_id IS NULL`` filters stay correct."""
    storage.register_workstream("ws-a", parent_ws_id="")
    row = storage.get_workstream("ws-a")
    assert row is not None
    assert row["parent_ws_id"] is None


def test_get_workstream_missing_returns_none(storage):
    assert storage.get_workstream("nonexistent") is None


def test_get_workstream_includes_all_fields(storage):
    storage.register_workstream(
        "ws-full",
        node_id="n1",
        user_id="u1",
        alias="alias-1",
        title="Title 1",
        name="name-1",
        state="idle",
        skill_id="skill-x",
        skill_version=3,
        kind="interactive",
        parent_ws_id="parent-x",
    )
    row = storage.get_workstream("ws-full")
    assert row is not None
    for expected in (
        "ws_id",
        "node_id",
        "user_id",
        "alias",
        "title",
        "name",
        "state",
        "skill_id",
        "skill_version",
        "kind",
        "parent_ws_id",
        "created",
        "updated",
    ):
        assert expected in row
    assert row["skill_version"] == 3
    assert row["parent_ws_id"] == "parent-x"


# ---------------------------------------------------------------------------
# list_workstreams filter params
# ---------------------------------------------------------------------------


def test_list_workstreams_no_filters_unchanged(storage):
    storage.register_workstream("ws-a")
    storage.register_workstream("ws-b")
    rows = storage.list_workstreams()
    assert len(rows) == 2


def test_list_workstreams_filter_by_kind(storage):
    storage.register_workstream("ws-int-1")
    storage.register_workstream("ws-int-2")
    storage.register_workstream("ws-coord", kind="coordinator")

    interactive = storage.list_workstreams(kind="interactive")
    coord = storage.list_workstreams(kind="coordinator")

    assert {r[0] for r in interactive} == {"ws-int-1", "ws-int-2"}
    assert {r[0] for r in coord} == {"ws-coord"}


def test_list_workstreams_filter_by_parent(storage):
    storage.register_workstream("ws-coord", kind="coordinator")
    storage.register_workstream("child-1", parent_ws_id="ws-coord")
    storage.register_workstream("child-2", parent_ws_id="ws-coord")
    storage.register_workstream("other-1")  # no parent

    children = storage.list_workstreams(parent_ws_id="ws-coord")
    assert {r[0] for r in children} == {"child-1", "child-2"}


def test_list_workstreams_combined_filters(storage):
    storage.register_workstream("ws-coord", kind="coordinator")
    storage.register_workstream("child-1", parent_ws_id="ws-coord")
    storage.register_workstream(
        "child-coord", parent_ws_id="ws-coord", kind="coordinator"
    )

    # Children of ws-coord that are themselves interactive.
    rows = storage.list_workstreams(parent_ws_id="ws-coord", kind="interactive")
    assert {r[0] for r in rows} == {"child-1"}


def test_list_workstreams_node_id_filter_still_works(storage):
    """The existing ``node_id`` filter keeps working after the signature change."""
    storage.register_workstream("ws-a", node_id="node-1")
    storage.register_workstream("ws-b", node_id="node-2")
    rows = storage.list_workstreams(node_id="node-1")
    assert {r[0] for r in rows} == {"ws-a"}


def test_list_workstreams_returns_kind_and_parent_columns(storage):
    storage.register_workstream("ws-coord", kind="coordinator")
    storage.register_workstream("child-1", parent_ws_id="ws-coord")
    rows = storage.list_workstreams()
    by_id = {r[0]: r for r in rows}
    # Columns: ws_id, node_id, name, state, created, updated, kind, parent_ws_id
    coord_row = by_id["ws-coord"]
    child_row = by_id["child-1"]
    assert coord_row[6] == "coordinator"
    assert coord_row[7] is None
    assert child_row[6] == "interactive"
    assert child_row[7] == "ws-coord"


# ---------------------------------------------------------------------------
# Workstream dataclass field additions
# ---------------------------------------------------------------------------


def test_workstream_dataclass_defaults():
    ws = Workstream()
    assert ws.user_id == ""
    assert ws.kind == "interactive"
    assert ws.parent_ws_id is None


def test_workstream_dataclass_accepts_coordinator_kind():
    ws = Workstream(kind="coordinator", user_id="user-1")
    assert ws.kind == "coordinator"
    assert ws.user_id == "user-1"
    assert ws.parent_ws_id is None


def test_workstream_dataclass_accepts_parent():
    ws = Workstream(parent_ws_id="parent-x")
    assert ws.parent_ws_id == "parent-x"
