"""Tests for the coordinator prepare/exec dispatch on ChatSession.

We construct a ChatSession with ``kind="coordinator"`` and a mocked
``CoordinatorClient``, then drive ``_prepare_tool`` directly with tool
call dicts matching the shape the provider layer produces.  This is a
unit-level test of the dispatch plumbing — end-to-end flows land in
Phase D's test_coordinator_end_to_end.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from turnstone.core.session import ChatSession
from turnstone.prompts import ClientType


class _StubUI:
    """Minimal SessionUI that records signals without doing anything with them."""

    def __init__(self) -> None:
        self._user_id = "user-1"
        self.infos: list[str] = []
        self.errors: list[str] = []
        self.tool_results: list[tuple[str, str, str, bool]] = []

    def on_info(self, msg: str) -> None:
        self.infos.append(msg)

    def on_error(self, msg: str) -> None:
        self.errors.append(msg)

    def on_tool_result(self, call_id: str, name: str, output: str, is_error: bool = False) -> None:
        self.tool_results.append((call_id, name, output, is_error))

    # Other SessionUI methods — only stubs, not exercised here.
    def on_turn_start(self) -> None:
        pass

    def on_turn_end(self) -> None:
        pass

    def on_stream_start(self) -> None:
        pass

    def on_stream_end(self) -> None:
        pass

    def on_message_delta(self, delta: str) -> None:
        pass

    def on_reasoning_delta(self, delta: str) -> None:
        pass

    def on_tool_call(self, call_id: str, name: str, header: str, preview: str) -> None:
        pass

    def on_completion(self, content: str) -> None:
        pass

    def on_attention(self, header: str, preview: str = "") -> None:
        pass

    def wait_for_approval(
        self,
        call_id: str,
        name: str,
        header: str,
        preview: str,
        *,
        label: str = "",
    ) -> tuple[bool, str | None]:
        return True, None


@pytest.fixture
def coord_session(monkeypatch):
    """Build a coordinator ChatSession with a mocked CoordinatorClient.

    Patches heavyweight init steps (_load_skills, _init_system_messages,
    _save_config) to keep the test fast + isolated from the storage
    registry.
    """
    monkeypatch.setattr(ChatSession, "_load_skills", lambda self: None)
    monkeypatch.setattr(ChatSession, "_init_system_messages", lambda self: None)
    monkeypatch.setattr(ChatSession, "_save_config", lambda self: None)

    ui = _StubUI()
    coord_client = MagicMock()
    sess = ChatSession(
        client=MagicMock(),
        model="gpt-test",
        ui=ui,  # type: ignore[arg-type]
        instructions=None,
        temperature=0.0,
        max_tokens=1024,
        tool_timeout=30,
        context_window=16384,
        ws_id="coord-1",
        user_id="user-1",
        client_type=ClientType.WEB,
        kind="coordinator",
        coord_client=coord_client,
    )
    return sess, coord_client, ui


# ---------------------------------------------------------------------------
# Tool set shape
# ---------------------------------------------------------------------------


def test_coordinator_session_uses_coordinator_tools(coord_session):
    sess, _coord, _ui = coord_session
    names = {t["function"]["name"] for t in sess._tools}
    assert names == {
        "spawn_workstream",
        "inspect_workstream",
        "send_to_workstream",
        "close_workstream",
        "delete_workstream",
        "list_workstreams",
    }
    # Sub-agent tool sets are zeroed on coordinator sessions.
    assert sess._task_tools == []
    assert sess._agent_tools == []


# ---------------------------------------------------------------------------
# Helper: build a ChatCompletion-style tool_call dict
# ---------------------------------------------------------------------------


def _tc(name: str, args: dict[str, Any], call_id: str = "call-1") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


# ---------------------------------------------------------------------------
# spawn_workstream
# ---------------------------------------------------------------------------


def test_spawn_prepare_allows_empty_initial_message(coord_session):
    """Empty initial_message creates an idle child — matches tool JSON advertisement."""
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": ""}))
    assert "error" not in item
    assert item["needs_approval"] is True
    assert "idle workstream" in item["header"]
    assert item["initial_message"] == ""


def test_spawn_prepare_needs_approval(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(
        _tc("spawn_workstream", {"initial_message": "do a thing", "skill": "s"})
    )
    assert item["needs_approval"] is True
    assert item["execute"].__func__ is ChatSession._exec_spawn_workstream
    assert item["skill"] == "s"


def test_spawn_exec_calls_client_and_returns_summary(coord_session):
    sess, coord, _ui = coord_session
    coord.spawn.return_value = {
        "ws_id": "child-7",
        "name": "c",
        "node_id": "node-1",
        "status": 200,
    }
    item = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "hi"}))
    call_id, output = sess._exec_spawn_workstream(item)
    coord.spawn.assert_called_once()
    _, kwargs = coord.spawn.call_args
    assert kwargs["parent_ws_id"] == "coord-1"
    assert kwargs["user_id"] == "user-1"
    assert kwargs["initial_message"] == "hi"
    assert call_id == "call-1"
    assert "child-7" in output


def test_spawn_exec_surfaces_client_error(coord_session):
    sess, coord, ui = coord_session
    coord.spawn.return_value = {"error": "upstream unreachable", "status": 502}
    item = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "hi"}))
    _call_id, output = sess._exec_spawn_workstream(item)
    assert "upstream unreachable" in output
    # UI got an error result
    assert ui.tool_results[-1][3] is True  # is_error


# ---------------------------------------------------------------------------
# inspect_workstream
# ---------------------------------------------------------------------------


def test_inspect_prepare_is_auto_approved(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("inspect_workstream", {"ws_id": "child-x", "message_limit": 5}))
    assert item["needs_approval"] is False
    assert item["execute"].__func__ is ChatSession._exec_inspect_workstream
    assert item["message_limit"] == 5


def test_inspect_prepare_requires_ws_id(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("inspect_workstream", {}))
    assert "error" in item


def test_inspect_prepare_clamps_message_limit(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("inspect_workstream", {"ws_id": "x", "message_limit": 10000}))
    assert item["message_limit"] == 200  # clamped


def test_inspect_exec_dispatches_to_client(coord_session):
    sess, coord, _ui = coord_session
    coord.inspect.return_value = {
        "ws_id": "child-x",
        "state": "idle",
        "messages": [],
        "verdicts": [],
    }
    item = sess._prepare_tool(_tc("inspect_workstream", {"ws_id": "child-x"}))
    _call_id, output = sess._exec_inspect_workstream(item)
    coord.inspect.assert_called_once_with("child-x", message_limit=20)
    assert "child-x" in output


# ---------------------------------------------------------------------------
# send_to_workstream
# ---------------------------------------------------------------------------


def test_send_prepare_needs_approval(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("send_to_workstream", {"ws_id": "x", "message": "hello"}))
    assert item["needs_approval"] is True


def test_send_prepare_rejects_empty_message(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("send_to_workstream", {"ws_id": "x", "message": ""}))
    assert "error" in item


def test_send_exec_dispatches(coord_session):
    sess, coord, _ui = coord_session
    coord.send.return_value = {"status": 200}
    item = sess._prepare_tool(_tc("send_to_workstream", {"ws_id": "x", "message": "hi"}))
    _call_id, output = sess._exec_send_to_workstream(item)
    coord.send.assert_called_once_with("x", "hi")
    assert "x" in output


# ---------------------------------------------------------------------------
# close_workstream
# ---------------------------------------------------------------------------


def test_close_prepare_needs_approval(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("close_workstream", {"ws_id": "x"}))
    assert item["needs_approval"] is True


def test_close_exec_dispatches(coord_session):
    sess, coord, _ui = coord_session
    coord.close_workstream.return_value = {"status": 200}
    item = sess._prepare_tool(_tc("close_workstream", {"ws_id": "x"}))
    _call_id, output = sess._exec_close_workstream(item)
    # Default (no reason) — kwargs carry empty reason through the call.
    coord.close_workstream.assert_called_once_with("x", reason="")
    parsed = json.loads(output)
    assert parsed["closed"] is True
    assert "reason" not in parsed  # omitted when empty


def test_close_exec_forwards_reason(coord_session):
    """reason is wired through both CoordinatorClient.close_workstream
    and the tool-result payload so the coordinator's message stream
    records why the close happened."""
    sess, coord, _ui = coord_session
    coord.close_workstream.return_value = {"status": 200}
    item = sess._prepare_tool(_tc("close_workstream", {"ws_id": "x", "reason": "task done"}))
    _call_id, output = sess._exec_close_workstream(item)
    coord.close_workstream.assert_called_once_with("x", reason="task done")
    parsed = json.loads(output)
    assert parsed["reason"] == "task done"


# ---------------------------------------------------------------------------
# delete_workstream
# ---------------------------------------------------------------------------


def test_delete_prepare_needs_approval(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("delete_workstream", {"ws_id": "x"}))
    assert item["needs_approval"] is True
    assert "irreversible" in item["header"].lower()


def test_delete_exec_dispatches(coord_session):
    sess, coord, _ui = coord_session
    coord.delete.return_value = {"status": 200}
    item = sess._prepare_tool(_tc("delete_workstream", {"ws_id": "x"}))
    _call_id, output = sess._exec_delete_workstream(item)
    coord.delete.assert_called_once_with("x")
    parsed = json.loads(output)
    assert parsed["deleted"] is True


# ---------------------------------------------------------------------------
# list_workstreams
# ---------------------------------------------------------------------------


def test_list_prepare_is_auto_approved(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("list_workstreams", {}))
    assert item["needs_approval"] is False


def test_list_prepare_defaults_parent_to_self_ws(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("list_workstreams", {}))
    assert item["parent_ws_id"] == "coord-1"


def test_list_prepare_accepts_explicit_parent(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(
        _tc("list_workstreams", {"parent_ws_id": "other-coord", "state": "idle"})
    )
    assert item["parent_ws_id"] == "other-coord"
    assert item["state"] == "idle"


def test_list_exec_dispatches(coord_session):
    sess, coord, _ui = coord_session
    coord.list_children.return_value = {
        "children": [
            {"ws_id": "a", "state": "idle"},
            {"ws_id": "b", "state": "running"},
        ],
        "truncated": False,
    }
    item = sess._prepare_tool(_tc("list_workstreams", {}))
    _call_id, output = sess._exec_list_workstreams(item)
    coord.list_children.assert_called_once()
    parsed = json.loads(output)
    assert parsed["parent_ws_id"] == "coord-1"
    assert len(parsed["children"]) == 2
    assert parsed["truncated"] is False


def test_list_exec_surfaces_truncated_sentinel(coord_session):
    sess, coord, _ui = coord_session
    coord.list_children.return_value = {
        "children": [{"ws_id": "a", "state": "idle"}],
        "truncated": True,
    }
    item = sess._prepare_tool(_tc("list_workstreams", {}))
    _call_id, output = sess._exec_list_workstreams(item)
    parsed = json.loads(output)
    assert parsed["truncated"] is True


# ---------------------------------------------------------------------------
# Defensive guard: missing coord_client
# ---------------------------------------------------------------------------


def test_prepare_fails_cleanly_when_coord_client_missing(monkeypatch):
    """If somehow a coordinator-kind session is built without a coord_client,
    prepare methods return an error item rather than NPE."""
    monkeypatch.setattr(ChatSession, "_load_skills", lambda self: None)
    monkeypatch.setattr(ChatSession, "_init_system_messages", lambda self: None)
    monkeypatch.setattr(ChatSession, "_save_config", lambda self: None)
    ui = _StubUI()
    sess = ChatSession(
        client=MagicMock(),
        model="m",
        ui=ui,  # type: ignore[arg-type]
        instructions=None,
        temperature=0.0,
        max_tokens=1024,
        tool_timeout=30,
        context_window=16384,
        ws_id="coord-1",
        kind="coordinator",
        coord_client=None,
    )
    for tool, args in (
        ("spawn_workstream", {"initial_message": "hi"}),
        ("inspect_workstream", {"ws_id": "x"}),
        ("send_to_workstream", {"ws_id": "x", "message": "m"}),
        ("close_workstream", {"ws_id": "x"}),
        ("delete_workstream", {"ws_id": "x"}),
        ("list_workstreams", {}),
    ):
        item = sess._prepare_tool(_tc(tool, args))
        assert "error" in item, f"{tool} did not error on missing coord_client"
