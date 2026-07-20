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


def test_interactive_and_coordinator_tool_sets_overlap_only_on_dual_kind():
    """Interactive ∩ coordinator must be exactly the explicitly dual-kind tools.

    Regression guard for the latent threshold bug where coordinator-only
    tools counted against the interactive session's tool-search
    threshold, and a future reader might naively expose ``TOOLS`` (the
    union) to an interactive session.

    A small, explicit overlap is allowed: tools tagged with BOTH
    ``"coordinator": true`` and ``"interactive": true`` (e.g. ``memory``)
    intentionally appear in both sets.  The whitelist below is the
    canonical list of dual-kind tools — any drift here is a real
    review-worthy change, not just a count tweak.
    """
    from turnstone.core.tools import COORDINATOR_TOOLS, INTERACTIVE_TOOLS, TOOLS

    interactive_names = {t["function"]["name"] for t in INTERACTIVE_TOOLS}
    coord_names = {t["function"]["name"] for t in COORDINATOR_TOOLS}

    # Explicit dual-kind tools — deliberately in both sets.  ``skills``
    # joined in 1.6.0 (replaces legacy ``skill`` + ``list_skills``) — read
    # actions auto-approve on both kinds, write actions gate on
    # ``model.skills.write`` permission, and ``load`` errors on coord
    # sessions where it doesn't apply.  ``notify`` joined in 1.6.0 so
    # coords can post status updates at narrative beats without spawning
    # a child purely to ship a message.  ``read_resource``/``use_prompt``
    # joined in 1.8 (#725): coordinators get the full MCP surface —
    # tools, resources, prompts — persona-gated like every session.
    dual_kind = {"memory", "skills", "notify", "read_resource", "use_prompt"}

    overlap = interactive_names & coord_names
    assert overlap == dual_kind, (
        f"interactive ∩ coordinator should be exactly {dual_kind}, got {overlap}. "
        f"Update dual_kind if a new tool legitimately joins both sets."
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

    sess = ChatSession(
        client=MagicMock(),
        model="test-model",
        ui=_null_ui(),
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
        "tasks",
        "wait_for_workstream",
    ):
        assert coord_name not in names, f"{coord_name} leaked into interactive session tools"


def test_chatsession_coordinator_kind_excludes_interactive_tools(tmp_db):
    """A coordinator ``ChatSession`` sees only coordinator-kind tools.

    ``memory`` IS in the coord set (it's marked dual-kind in
    ``memory.json`` so coordinators can persist orchestration context
    via the ``coordinator`` scope), but the IC-only tools (bash,
    edit_file, ...) stay out — those operate on the local node and
    have no meaningful semantics from the console.
    """
    sess = _make_coordinator(mcp_client=None)
    names = {t["function"]["name"] for t in sess._tools}
    # Coordinator tools present, IC-only tools absent.
    assert "spawn_workstream" in names
    assert "bash" not in names
    assert "edit_file" not in names
    # Memory is intentionally exposed — see docstring.
    assert "memory" in names
    # Sub-agent tool list is zeroed for coordinators.
    assert sess._task_tools == []


def _null_ui():
    class _NullUI:
        def __getattr__(self, _name):
            return lambda *a, **kw: None

    return _NullUI()


def _mcp_client_mock():
    from unittest.mock import MagicMock

    mcp_client = MagicMock()
    mcp_client.get_tools.return_value = [
        {"type": "function", "function": {"name": "mcp__foo__bar", "parameters": {}}}
    ]
    return mcp_client


def _make_coordinator(mcp_client, persona_snapshot=None):
    from unittest.mock import MagicMock

    from turnstone.core.session import ChatSession

    return ChatSession(
        client=MagicMock(),
        model="test-model",
        ui=_null_ui(),
        instructions=None,
        temperature=0.5,
        max_tokens=4096,
        tool_timeout=30,
        user_id="user-1",  # the constructor refuses anonymous coordinators
        kind="coordinator",
        mcp_client=mcp_client,
        persona_snapshot=persona_snapshot,
    )


def test_chatsession_coordinator_mcp_keyed_on_client_presence(tmp_db):
    """Coordinator MCP is keyed on CLIENT PRESENCE (#725).

    The console session factory — the only coordinator producer (the
    CLI is blocked by the anonymous-coordinator guard; nodes reject
    non-interactive kinds at create and at session-load) — passes the
    live console manager unconditionally; the persona MCP toggle
    governs the surface, like interactive.  Session-level contract: a
    coordinator constructed WITH an mcp_client treats MCP as enabled and
    runs the same listener/prime skeleton as interactive over the
    COORDINATOR_TOOLS base; one constructed WITHOUT keeps the fixed
    builtin surface.
    """
    from unittest.mock import patch

    # Cell (a): no client → today's fixed invariant, verbatim.
    sess = _make_coordinator(mcp_client=None)
    names = {t["function"]["name"] for t in sess._tools}
    assert "mcp__foo__bar" not in names
    assert "spawn_workstream" in names
    assert sess._task_tools == []

    # Cell (b): client present → merged surface + ALL THREE listeners +
    # the creator's pools primed (full parity with interactive).
    mcp_client = _mcp_client_mock()
    with patch("turnstone.core.session.try_prime_user_pools") as prime:
        sess = _make_coordinator(mcp_client=mcp_client)
    names = {t["function"]["name"] for t in sess._tools}
    assert "mcp__foo__bar" in names
    assert "spawn_workstream" in names  # base is additive, never replaced
    assert sess._task_tools == []  # coordinators have no task-agent lane
    mcp_client.add_listener.assert_called_once()
    assert mcp_client.add_listener.call_args.kwargs.get("user_id") == "user-1"
    mcp_client.add_resource_listener.assert_called_once()
    mcp_client.add_prompt_listener.assert_called_once()
    prime.assert_called_once()
    assert prime.call_args.args[1] == "user-1"


def test_chatsession_coordinator_persona_mcp_off_wins(tmp_db):
    """A coordinator persona with mcp=False gates the surface off even
    when the factory passed a live client — same precedence as
    interactive (the 1762 persona gate composes upstream of the kind
    branch), and ``_mcp_gated_off`` records that a real client was
    withheld so resume() refuses to adopt an MCP-on stamp."""
    from turnstone.core.personas import PersonaSnapshot

    mcp_client = _mcp_client_mock()
    snap = PersonaSnapshot(name="p", prompt="x", tools=None, mcp=False, memory=True)
    sess = _make_coordinator(mcp_client=mcp_client, persona_snapshot=snap)
    names = {t["function"]["name"] for t in sess._tools}
    assert "mcp__foo__bar" not in names
    mcp_client.add_listener.assert_not_called()
    mcp_client.add_resource_listener.assert_not_called()
    mcp_client.add_prompt_listener.assert_not_called()
    assert sess._mcp_gated_off is True


def test_chatsession_coordinator_drop_mcp_surface_resets_to_coordinator_tools(tmp_db):
    """The resume()-adopts-MCP-off-stamp path (_drop_mcp_surface) on a
    COORDINATOR resets to COORDINATOR_TOOLS — never the interactive lanes
    — and removes all three listeners under the tracked registration
    identity.  Direct cell for the docstring's both-kinds claim; every
    sibling coordinator MCP transition has one."""
    from unittest.mock import patch

    mcp_client = _mcp_client_mock()
    with patch("turnstone.core.session.try_prime_user_pools"):
        sess = _make_coordinator(mcp_client=mcp_client)
    assert "mcp__foo__bar" in {t["function"]["name"] for t in sess._tools}

    sess._drop_mcp_surface()

    names = {t["function"]["name"] for t in sess._tools}
    assert "mcp__foo__bar" not in names
    assert "spawn_workstream" in names
    assert sess._task_tools == []
    assert sess._mcp_client is None
    mcp_client.remove_listener.assert_called_once()
    assert mcp_client.remove_listener.call_args.kwargs.get("user_id") == "user-1"
    mcp_client.remove_resource_listener.assert_called_once()
    mcp_client.remove_prompt_listener.assert_called_once()


def test_chatsession_coordinator_drop_mcp_surface_after_rebind_uses_tracked_id(tmp_db):
    """After bind_acting_user re-scopes the listener registrations to a
    NEW operator, a subsequent surface drop must remove them under the
    tracked _mcp_listener_user_id (the rebound identity) — removal keyed
    on the owner id would leave the rebound registrations leaking."""
    from unittest.mock import patch

    mcp_client = _mcp_client_mock()
    with patch("turnstone.core.session.try_prime_user_pools"):
        sess = _make_coordinator(mcp_client=mcp_client)
        sess.bind_acting_user("user-2")

    assert sess._mcp_listener_user_id == "user-2"

    sess._drop_mcp_surface()

    assert mcp_client.remove_listener.call_args.kwargs.get("user_id") == "user-2"
    assert mcp_client.remove_resource_listener.call_args.kwargs.get("user_id") == "user-2"
    assert mcp_client.remove_prompt_listener.call_args.kwargs.get("user_id") == "user-2"


def test_coordinator_catalog_change_rebuilds_merged_tools(tmp_db):
    """A registered coordinator's tool listener rebuilds the merged set on
    catalog change — the surface tracks admin edits and reconnects instead
    of freezing at construction (the reversed early-return)."""
    mcp_client = _mcp_client_mock()
    sess = _make_coordinator(mcp_client=mcp_client)
    cb = mcp_client.add_listener.call_args.args[0]
    mcp_client.get_tools.return_value = [
        {"type": "function", "function": {"name": "mcp__foo__baz", "parameters": {}}}
    ]
    cb()
    names = {t["function"]["name"] for t in sess._tools}
    assert "mcp__foo__baz" in names
    assert "mcp__foo__bar" not in names  # fresh merge, not accretion
    assert "spawn_workstream" in names  # base survives every rebuild
    assert sess._task_tools == []


def test_coordinator_construction_race_converges_post_snapshot_change(tmp_db):
    """A catalog change landing AFTER the authoritative read must be
    converged by the end-of-construction recheck.  The seq bump rides
    get_tools' FIRST call so it lands strictly after the constructor's
    snapshot — a bump at add_listener time would fold into the snapshot
    and pass whether or not the recheck runs for coordinators."""
    from unittest.mock import MagicMock

    mcp_client = MagicMock()
    v1 = [{"type": "function", "function": {"name": "mcp__foo__v1", "parameters": {}}}]
    v2 = [{"type": "function", "function": {"name": "mcp__foo__v2", "parameters": {}}}]
    calls = {"n": 0}

    def _get_tools(user_id=None):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate a notification landing between the authoritative
            # read and the end of tool setup: advance the bound session's
            # change counter (the listener callback's __self__).
            cb = mcp_client.add_listener.call_args.args[0]
            cb.__self__._mcp_tools_change_seq += 1
            return v1
        return v2

    mcp_client.get_tools.side_effect = _get_tools
    sess = _make_coordinator(mcp_client=mcp_client)
    names = {t["function"]["name"] for t in sess._tools}
    assert "mcp__foo__v2" in names, "post-snapshot catalog change was lost for a coordinator"


def test_coordinator_bind_acting_user_rescopes_listeners_and_primes(tmp_db):
    """Coordinators are multi-sender: a fresh turn from operator B must
    re-scope the MCP listeners to B and prime B's pools, so B dispatches
    against B's catalog — never A's (#725, security-critical cell)."""
    from unittest.mock import patch

    mcp_client = _mcp_client_mock()
    with patch("turnstone.core.session.try_prime_user_pools") as prime:
        sess = _make_coordinator(mcp_client=mcp_client)
        sess.bind_acting_user("user-2")
    assert mcp_client.remove_listener.call_args.kwargs.get("user_id") == "user-1"
    assert mcp_client.add_listener.call_args.kwargs.get("user_id") == "user-2"
    assert mcp_client.remove_resource_listener.call_args.kwargs.get("user_id") == "user-1"
    assert mcp_client.add_resource_listener.call_args.kwargs.get("user_id") == "user-2"
    assert mcp_client.remove_prompt_listener.call_args.kwargs.get("user_id") == "user-1"
    assert mcp_client.add_prompt_listener.call_args.kwargs.get("user_id") == "user-2"
    assert prime.call_args.args[1] == "user-2"
    assert sess._mcp_effective_user_id == "user-2"


def test_coordinator_close_removes_listeners_owner_only(tmp_db):
    """Exit-path cell (i): closing an owner-only coordinator removes all
    three listeners under the owner identity."""
    mcp_client = _mcp_client_mock()
    sess = _make_coordinator(mcp_client=mcp_client)
    sess.close()
    assert mcp_client.remove_listener.call_args.kwargs.get("user_id") == "user-1"
    assert mcp_client.remove_resource_listener.call_args.kwargs.get("user_id") == "user-1"
    assert mcp_client.remove_prompt_listener.call_args.kwargs.get("user_id") == "user-1"


def test_coordinator_close_after_rebind_removes_under_new_identity(tmp_db):
    """Exit-path cell (ii), the wrong-field regression pin: after operator
    B rebinds, eviction/close must remove listeners under B's uid (the
    tracked ``_mcp_listener_user_id``), not the owner's ``_mcp_user_id`` —
    owner-only closes cannot distinguish the two fields (both equal the
    owner at construction)."""
    from unittest.mock import patch

    mcp_client = _mcp_client_mock()
    with patch("turnstone.core.session.try_prime_user_pools"):
        sess = _make_coordinator(mcp_client=mcp_client)
        sess.bind_acting_user("user-2")
    sess.close()
    assert mcp_client.remove_listener.call_args.kwargs.get("user_id") == "user-2"
    assert mcp_client.remove_resource_listener.call_args.kwargs.get("user_id") == "user-2"
    assert mcp_client.remove_prompt_listener.call_args.kwargs.get("user_id") == "user-2"


def test_coordinator_read_resource_use_prompt_on_wire_and_approval(tmp_db):
    """Dual-kind read_resource/use_prompt (#725) — the WIRE matrix via
    _get_active_tools(): present with a client exposing nonzero
    resource/prompt counts; STRIPPED without a client; STRIPPED with a
    client whose counts are zero (the _without_tool gate keys on client
    presence AND per-user catalog count).  Base-list membership is
    asserted separately — both tools sit in COORDINATOR_TOOLS
    unconditionally.  Approval: needs_approval preserved
    (auto_approve:false)."""
    mcp_client = _mcp_client_mock()
    # Explicit integers: a bare MagicMock return is truthy, which would
    # let the present-cell pass without the count gate ever working.
    mcp_client.resource_count_for_user.return_value = 2
    mcp_client.prompt_count_for_user.return_value = 1
    sess = _make_coordinator(mcp_client=mcp_client)
    assert {"read_resource", "use_prompt"} <= {t["function"]["name"] for t in sess._tools}
    wire = {t["function"]["name"] for t in sess._get_active_tools()}
    assert {"read_resource", "use_prompt"} <= wire
    item = sess._prepare_read_resource("c1", {"uri": "res://x"})
    assert item.get("needs_approval") is True

    bare = _make_coordinator(mcp_client=None)
    assert {"read_resource", "use_prompt"} <= {t["function"]["name"] for t in bare._tools}
    bare_wire = {t["function"]["name"] for t in bare._get_active_tools()}
    assert not ({"read_resource", "use_prompt"} & bare_wire), (
        "client-less coordinator must not advertise MCP catalog tools on the wire"
    )

    zero = _mcp_client_mock()
    zero.resource_count_for_user.return_value = 0
    zero.prompt_count_for_user.return_value = 0
    sess_zero = _make_coordinator(mcp_client=zero)
    zero_wire = {t["function"]["name"] for t in sess_zero._get_active_tools()}
    assert not ({"read_resource", "use_prompt"} & zero_wire), (
        "zero-count catalogs must strip read_resource/use_prompt from the wire"
    )
