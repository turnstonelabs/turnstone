"""Working-directory/workspace notes rendered into fs-tool descriptions.

Covers the pure renderer (``apply_cwd_context``), the metadata invariants the
session wiring relies on, and the ChatSession build sites (construction, MCP
rebuild) including the guarded ``os.getcwd()`` read and the task-agent lane.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from turnstone.core.session import ChatSession
from turnstone.core.tools import (
    _META,
    COORDINATOR_TOOLS,
    INTERACTIVE_TOOLS,
    TASK_AGENT_TOOLS,
    TOOLS,
    apply_cwd_context,
)

_FS_TOOLS = ("bash", "read_file", "write_file", "edit_file", "search", "diff_file")


def _desc(tools: list[dict], name: str) -> str:
    for t in tools:
        if t["function"]["name"] == name:
            return t["function"]["description"]
    raise AssertionError(f"tool {name!r} not in list")


# ---------------------------------------------------------------------------
# apply_cwd_context (pure renderer)
# ---------------------------------------------------------------------------


class TestApplyCwdContext:
    def test_notes_rendered_on_fs_tools(self):
        out = apply_cwd_context(INTERACTIVE_TOOLS, "/data", "/workspace")
        assert "Commands run in /data" in _desc(out, "bash")
        assert "cd does not persist" in _desc(out, "bash")
        assert "The user's workspace directory is /workspace." in _desc(out, "bash")
        for name in ("read_file", "write_file", "edit_file", "search", "diff_file"):
            assert "Relative paths resolve against /data." in _desc(out, name)
            assert "The user's workspace directory is /workspace." in _desc(out, name)

    def test_noteless_tools_pass_through_by_reference(self):
        out = apply_cwd_context(INTERACTIVE_TOOLS, "/data", "/workspace")
        by_name = {t["function"]["name"]: t for t in out}
        base_by_name = {t["function"]["name"]: t for t in INTERACTIVE_TOOLS}
        assert by_name["web_search"] is base_by_name["web_search"]
        # Noted tools are fresh copies.
        assert by_name["bash"] is not base_by_name["bash"]

    def test_module_constants_never_mutated(self):
        # The fs tool dicts are SHARED across TOOLS/INTERACTIVE_TOOLS/
        # TASK_AGENT_TOOLS and aliased through merge_mcp_tools output — an
        # in-place append would corrupt every list at once.
        before = {name: _desc(TOOLS, name) for name in _FS_TOOLS}
        apply_cwd_context(INTERACTIVE_TOOLS, "/data", "/workspace")
        apply_cwd_context(TASK_AGENT_TOOLS, "/data", "/workspace")
        for name in _FS_TOOLS:
            assert _desc(TOOLS, name) == before[name]
            assert "/data" not in _desc(INTERACTIVE_TOOLS, name)

    def test_empty_working_dir_drops_cwd_note_only(self):
        out = apply_cwd_context(INTERACTIVE_TOOLS, "", "/workspace")
        assert "Commands run in" not in _desc(out, "bash")
        assert "Relative paths resolve" not in _desc(out, "read_file")
        assert "The user's workspace directory is /workspace." in _desc(out, "bash")

    def test_empty_workspace_drops_workspace_note_only(self):
        out = apply_cwd_context(INTERACTIVE_TOOLS, "/data", "")
        assert "Commands run in /data" in _desc(out, "bash")
        assert "workspace directory" not in _desc(out, "bash")

    def test_both_empty_is_pass_through(self):
        out = apply_cwd_context(INTERACTIVE_TOOLS, "", "")
        assert out == INTERACTIVE_TOOLS
        assert out is not INTERACTIVE_TOOLS  # still a fresh list

    def test_mcp_style_tool_untouched(self):
        mcp_tool = {
            "type": "function",
            "function": {"name": "mcp__srv__thing", "description": "Does a thing."},
        }
        out = apply_cwd_context([mcp_tool], "/data", "/workspace")
        assert out[0] is mcp_tool
        assert out[0]["function"]["description"] == "Does a thing."

    def test_paths_with_braces_are_literal(self):
        # str.replace substitution — a path containing brace characters must
        # land verbatim (str.format would raise or mangle here).
        out = apply_cwd_context(INTERACTIVE_TOOLS, "/data/{odd}", "")
        assert "Commands run in /data/{odd}" in _desc(out, "bash")


# ---------------------------------------------------------------------------
# Metadata invariants the session wiring relies on
# ---------------------------------------------------------------------------


class TestNoteMetadataInvariants:
    def test_all_fs_tools_declare_both_notes(self):
        for name in _FS_TOOLS:
            assert _META[name].get("cwd_note"), name
            assert _META[name].get("workspace_note"), name

    def test_no_coordinator_tool_declares_notes(self):
        # The coordinator build sites skip _apply_cwd_notes on the strength
        # of this invariant.
        for t in COORDINATOR_TOOLS:
            name = t["function"]["name"]
            meta = _META.get(name) or {}
            assert not meta.get("cwd_note"), name
            assert not meta.get("workspace_note"), name

    def test_notes_stripped_from_wire_schema(self):
        # _META_KEYS extraction: the raw JSON keys must not leak into the
        # OpenAI function dict sent to providers.
        for t in TOOLS:
            assert "cwd_note" not in t["function"]
            assert "workspace_note" not in t["function"]

    def test_workspace_note_wording_uniform(self):
        # The workspace fact is one node-level value, so its sentence is
        # deliberately identical across the fs tools (unlike cwd_note, whose
        # prose is per-tool).  Guards a one-file reword from drifting the
        # copies apart, independent of the exact wording.
        notes = {_META[name]["workspace_note"] for name in _FS_TOOLS}
        assert len(notes) == 1, notes


# ---------------------------------------------------------------------------
# ChatSession build sites
# ---------------------------------------------------------------------------


def _make_session(**kwargs):
    defaults = dict(
        client=MagicMock(),
        model="test-model",
        ui=MagicMock(),
        instructions=None,
        temperature=0.5,
        max_tokens=1024,
        tool_timeout=10,
    )
    defaults.update(kwargs)
    return ChatSession(**defaults)


class TestSessionCwdNotes:
    def test_fresh_session_carries_cwd_note(self, tmp_db):
        with patch("turnstone.core.session.get_workspace_dir", return_value=None):
            session = _make_session()
        assert f"Commands run in {os.getcwd()}" in _desc(session._tools, "bash")
        assert f"Relative paths resolve against {os.getcwd()}." in _desc(
            session._tools, "read_file"
        )

    def test_task_lane_carries_cwd_note(self, tmp_db):
        # Sub-agents use self._task_tools, a separate list from self._tools —
        # they run in this same process, so the same cwd applies.
        with patch("turnstone.core.session.get_workspace_dir", return_value=None):
            session = _make_session()
        assert f"Commands run in {os.getcwd()}" in _desc(session._task_tools, "bash")

    def test_getcwd_failure_degrades_to_noteless(self, tmp_db):
        # A deleted cwd (eval workdir teardown) must not break session
        # construction or an MCP background-thread rebuild.
        with (
            patch("turnstone.core.session.get_workspace_dir", return_value=None),
            patch("os.getcwd", side_effect=OSError("cwd deleted")),
        ):
            session = _make_session()
        assert "Commands run in" not in _desc(session._tools, "bash")
        assert session._tools  # built fine, just note-less

    def test_workspace_rendered_when_dir_exists(self, tmp_db, tmp_path):
        with patch("turnstone.core.session.get_workspace_dir", return_value=str(tmp_path)):
            session = _make_session()
        assert f"The user's workspace directory is {tmp_path}." in _desc(session._tools, "bash")

    def test_workspace_skipped_when_dir_missing(self, tmp_db, tmp_path):
        missing = tmp_path / "does-not-exist"
        with patch("turnstone.core.session.get_workspace_dir", return_value=str(missing)):
            session = _make_session()
        assert "workspace directory" not in _desc(session._tools, "bash")

    def test_workspace_skipped_when_equal_to_cwd(self, tmp_db):
        # e.g. an operator who set working_dir: /workspace on the container —
        # one fact, not two copies of the same path.
        with patch("turnstone.core.session.get_workspace_dir", return_value=os.getcwd()):
            session = _make_session()
        desc = _desc(session._tools, "bash")
        assert f"Commands run in {os.getcwd()}" in desc
        assert "workspace directory" not in desc

    def test_constants_pristine_after_session_build(self, tmp_db):
        with patch("turnstone.core.session.get_workspace_dir", return_value=None):
            _make_session()
        assert os.getcwd() not in _desc(INTERACTIVE_TOOLS, "bash")
        assert os.getcwd() not in _desc(TOOLS, "bash")

    def test_mcp_rebuild_keeps_single_note(self, tmp_db):
        # The MCP list_changed rebuild re-derives from pristine bases — the
        # note must survive exactly once (double-append is the failure the
        # assignment-time design must never regress into).
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = []
        with patch("turnstone.core.session.get_workspace_dir", return_value=None):
            session = _make_session(mcp_client=mock_mcp)
            session._on_mcp_tools_changed()
            session._on_mcp_tools_changed()
        assert _desc(session._tools, "bash").count(f"Commands run in {os.getcwd()}") == 1
        assert _desc(session._task_tools, "bash").count(f"Commands run in {os.getcwd()}") == 1

    def test_mcp_drop_surface_keeps_single_note(self, tmp_db):
        # The MCP-disconnect rebuild (_drop_mcp_surface, reached via resume()
        # adopting an MCP-off persona) is the third rebuild trigger — the note
        # must survive it, exactly once, on both lanes.
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = []
        with patch("turnstone.core.session.get_workspace_dir", return_value=None):
            session = _make_session(mcp_client=mock_mcp)
            session._drop_mcp_surface()
        assert session._mcp_client is None
        assert _desc(session._tools, "bash").count(f"Commands run in {os.getcwd()}") == 1
        assert _desc(session._task_tools, "bash").count(f"Commands run in {os.getcwd()}") == 1
