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

from turnstone.core.workstream import Workstream

# ``storage`` comes from tests/conftest.py — backend-parametrized fixture that
# respects the ``--storage-backend`` flag so the same assertions run against
# both SQLite (default) and PostgreSQL (CI), closing the q-3 drift risk that
# sqlite↔postgres register/list/normalize semantics could diverge silently.


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
    storage.register_workstream("ws-coord", node_id="console", user_id="user-1", kind="coordinator")
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


def test_register_rejects_unknown_kind(storage):
    """Storage edge validates kind via WorkstreamKind(kind).value —
    SDK / restore / direct callers can't silently corrupt the NOT NULL column
    with typos or unknown values the way pre-PR #1 they could."""
    import pytest as _pytest

    with _pytest.raises(ValueError):
        storage.register_workstream("ws-bogus", kind="interative")  # typo
    # Row was never inserted — no side effects on failure.
    assert storage.get_workstream("ws-bogus") is None


def test_delete_workstream_nulls_child_parent_ws_id(storage):
    """Deleting a coordinator must null-out its children's parent_ws_id
    so list_workstreams(parent_ws_id=<deleted>) doesn't keep returning
    ghost-parented rows."""
    storage.register_workstream("coord", kind="coordinator", user_id="user-1")
    storage.register_workstream(
        "child-a", kind="interactive", parent_ws_id="coord", user_id="user-1"
    )
    storage.register_workstream(
        "child-b", kind="interactive", parent_ws_id="coord", user_id="user-1"
    )

    assert storage.delete_workstream("coord") is True

    # Children still exist but with NULL parent_ws_id.
    for cid in ("child-a", "child-b"):
        row = storage.get_workstream(cid)
        assert row is not None, f"{cid} should survive parent deletion"
        assert row["parent_ws_id"] is None, f"{cid} still points at ghost coord"
    # No rows match the deleted coord's parent filter.
    assert storage.list_workstreams(parent_ws_id="coord") == []


def test_list_workstreams_filter_by_user_id(storage):
    """The user_id kwarg pushes tenant scoping into SQL so callers
    can't forget to filter client-side."""
    storage.register_workstream("ws-a", user_id="user-1")
    storage.register_workstream("ws-b", user_id="user-1")
    storage.register_workstream("ws-c", user_id="user-2")
    storage.register_workstream("ws-ownerless")  # no user_id

    mine = storage.list_workstreams(user_id="user-1")
    theirs = storage.list_workstreams(user_id="user-2")
    ownerless = storage.list_workstreams(user_id="")
    unfiltered = storage.list_workstreams()

    assert {r[0] for r in mine} == {"ws-a", "ws-b"}
    assert {r[0] for r in theirs} == {"ws-c"}
    # Empty string is a real filter value (matches rows with stored "" owner).
    # Rows with NULL owner are distinct and not matched.
    assert "ws-ownerless" not in {r[0] for r in ownerless}
    # No filter → all rows.
    assert {r[0] for r in unfiltered} == {"ws-a", "ws-b", "ws-c", "ws-ownerless"}


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
    storage.register_workstream("child-coord", parent_ws_id="ws-coord", kind="coordinator")

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


# ---------------------------------------------------------------------------
# Tool-namespace isolation between kinds
# ---------------------------------------------------------------------------


def test_interactive_and_coordinator_tool_sets_are_disjoint():
    """Interactive sessions must not see coordinator tools and vice versa.

    Regression guard for the latent threshold bug where coordinator tools
    counted against the interactive session's tool-search threshold, and
    a future reader might naively expose ``TOOLS`` (the union) to an
    interactive session.
    """
    from turnstone.core.tools import COORDINATOR_TOOLS, INTERACTIVE_TOOLS, TOOLS

    interactive_names = {t["function"]["name"] for t in INTERACTIVE_TOOLS}
    coord_names = {t["function"]["name"] for t in COORDINATOR_TOOLS}

    # No overlap.
    assert interactive_names.isdisjoint(coord_names), (
        f"interactive ∩ coordinator tools should be empty, got {interactive_names & coord_names}"
    )
    # Coordinator set is non-empty (spawn/inspect/send/close/delete/list).
    assert coord_names, "expected at least one coordinator tool"
    # Union covers every loaded tool (no tool is in neither set).
    all_names = {t["function"]["name"] for t in TOOLS}
    assert interactive_names | coord_names == all_names


def test_chatsession_interactive_kind_excludes_coordinator_tools(tmp_db):
    """An interactive ``ChatSession`` does not surface coordinator tools."""
    from unittest.mock import MagicMock

    from turnstone.core.session import ChatSession

    class _NullUI:
        def __getattr__(self, _name):
            return lambda *a, **kw: None

    sess = ChatSession(
        client=MagicMock(),
        model="test-model",
        ui=_NullUI(),
        instructions=None,
        temperature=0.5,
        max_tokens=4096,
        tool_timeout=30,
    )
    names = {t["function"]["name"] for t in sess._tools}
    # None of the coordinator-only names should be in the interactive
    # session's tool set.
    for coord_name in (
        "spawn_workstream",
        "inspect_workstream",
        "send_to_workstream",
        "close_workstream",
        "cancel_workstream",
        "delete_workstream",
        "list_workstreams",
        "list_nodes",
        "list_skills",
        "task_list",
        "wait_for_workstream",
    ):
        assert coord_name not in names, f"{coord_name} leaked into interactive session tools"


def test_chatsession_coordinator_kind_excludes_interactive_tools(tmp_db):
    """A coordinator ``ChatSession`` sees only coordinator tools."""
    from unittest.mock import MagicMock

    from turnstone.core.session import ChatSession

    class _NullUI:
        def __getattr__(self, _name):
            return lambda *a, **kw: None

    sess = ChatSession(
        client=MagicMock(),
        model="test-model",
        ui=_NullUI(),
        instructions=None,
        temperature=0.5,
        max_tokens=4096,
        tool_timeout=30,
        kind="coordinator",
    )
    names = {t["function"]["name"] for t in sess._tools}
    # Coordinator tools present, interactive tools absent.
    assert "spawn_workstream" in names
    assert "bash" not in names
    assert "edit_file" not in names
    assert "memory" not in names
    # Sub-agent tool lists are zeroed for coordinators.
    assert sess._task_tools == []
    assert sess._agent_tools == []


def test_chatsession_coordinator_kind_does_not_merge_mcp_tools(tmp_db):
    """Coordinator ChatSession ignores any attached MCP client tool surface.

    Coordinators are meta-orchestrators that spawn child workstreams;
    MCP tools live on the children.  Giving the coordinator direct MCP
    access defeats the child-spawning pattern.
    """
    from unittest.mock import MagicMock

    from turnstone.core.session import ChatSession

    class _NullUI:
        def __getattr__(self, _name):
            return lambda *a, **kw: None

    mcp_client = MagicMock()
    mcp_client.get_tools.return_value = [
        {"type": "function", "function": {"name": "mcp__foo__bar", "parameters": {}}}
    ]
    sess = ChatSession(
        client=MagicMock(),
        model="test-model",
        ui=_NullUI(),
        instructions=None,
        temperature=0.5,
        max_tokens=4096,
        tool_timeout=30,
        kind="coordinator",
        mcp_client=mcp_client,
    )
    names = {t["function"]["name"] for t in sess._tools}
    # No MCP tools in the coordinator surface.
    assert "mcp__foo__bar" not in names
    # And no MCP listeners were registered (defence-in-depth: MCP tool
    # refreshes can't mutate the coordinator's fixed tool set).
    mcp_client.add_listener.assert_not_called()
    mcp_client.add_resource_listener.assert_not_called()
    mcp_client.add_prompt_listener.assert_not_called()
