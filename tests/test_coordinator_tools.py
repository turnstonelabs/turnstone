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
from unittest.mock import ANY, MagicMock, patch

import pytest

from turnstone.core.metacognition import TASK_NOTE_MAX, TASK_TITLE_MAX
from turnstone.core.session import ChatSession
from turnstone.core.storage._sqlite import SQLiteBackend
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

    def on_tool_result(
        self,
        call_id: str,
        name: str,
        output: str,
        is_error: bool = False,
        preview: dict[str, Any] | None = None,
    ) -> None:
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

    def on_state_change(self, state: str) -> None:
        pass

    def approve_tools(self, items: list) -> tuple[bool, str | None]:
        # Permissive default — tests that exercise approval
        # pathways override the method directly on the instance.
        return True, None

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
        "spawn_batch",
        "close_all_children",
        "inspect_workstream",
        "send_to_workstream",
        "close_workstream",
        "cancel_workstream",
        "delete_workstream",
        "list_workstreams",
        "list_nodes",
        "tasks",
        "wait_for_workstream",
        # Memory is dual-kind (coordinator: true + interactive: true) so
        # the coord can persist orchestration context for its children
        # via the ``coordinator`` scope. The system message preamble's
        # "use memory(...)" hint is gated on the tool being in scope, so
        # without this the model would see memories listed but no tool
        # to act on them.
        "memory",
        # ``skills`` replaced ``list_skills`` in the 1.6.0 tool unification.
        # Dual-kind (interactive + coordinator) — read actions auto-approve
        # on both; writes gate on ``model.skills.write``.
        "skills",
        # ``notify`` joined the coord set in 1.6.0 — orchestrators have
        # natural "fan-out complete" / "batch failed" beats worth
        # surfacing to a human channel without spawning a child purely
        # to ship the message.  Routing is session-kind-agnostic.
        "notify",
        # ``read_resource``/``use_prompt`` joined in 1.8 (#725) — the
        # coordinator MCP surface covers tools, resources, AND prompts.
        # They sit in the BASE list unconditionally (like their
        # interactive siblings), but the WIRE strips them when no client
        # is attached or the per-user catalog counts are zero — the
        # _without_tool gate in _get_active_tools, pinned by the wire
        # matrix in test_workstream_kind.py.
        "read_resource",
        "use_prompt",
    }
    # Sub-agent tool set is zeroed on coordinator sessions.
    assert sess._task_tools == []


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


def test_spawn_prepare_denies_high_risk_skill(coord_session):
    """Review fix: the high/critical-risk gate that blocks skills(load) also
    blocks spawn_workstream(skill=…), so a child spawn can't route around it."""
    sess, _coord, _ui = coord_session
    with patch("turnstone.core.session.get_storage") as gs:
        gs.return_value.get_prompt_template_by_name.return_value = {
            "name": "danger",
            "risk_level": "critical",
        }
        item = sess._prepare_tool(
            _tc("spawn_workstream", {"initial_message": "go", "skill": "danger"})
        )
    assert "error" in item
    assert "/skill danger" in item["error"]
    assert item.get("needs_approval") is not True


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


def test_spawn_exec_does_not_surface_misleading_status_field(coord_session):
    """The routing-proxy ``status`` is the HTTP code (always 200 on
    success), not a lifecycle state — leaking it into the tool's
    summary tempted callers to write ``if result["status"] == "idle"``
    which silently never matched.  The summary now omits the field
    entirely; lifecycle state lives on the workstream row and is read
    via inspect_workstream.

    Also asserts the return key is ``child_ws_id`` (not ``ws_id``) so
    the coordinator LLM doesn't recency-bias toward feeding the spawn
    output back into another ``spawn_workstream(ws_id=...)`` call.
    """
    sess, coord, _ui = coord_session
    coord.spawn.return_value = {
        "ws_id": "child-7",
        "name": "c",
        "node_id": "node-1",
        "status": 200,
    }
    item = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "hi"}))
    _call_id, output = sess._exec_spawn_workstream(item)
    body = json.loads(output)
    assert "status" not in body
    assert "ws_id" not in body
    # The substantive fields are still here.
    assert body["child_ws_id"] == "child-7"
    assert body["node_id"] == "node-1"


def test_spawn_batch_exec_does_not_surface_misleading_status_field(coord_session):
    """Same shape constraint as ``spawn_workstream`` — per-result
    entries omit ``status`` so the model can't be confused by the
    HTTP-code-as-lifecycle-state ambiguity."""
    sess, coord, _ui = coord_session
    coord.spawn.return_value = {
        "ws_id": "c-x",
        "name": "n",
        "node_id": "node",
        "status": 200,
    }
    item = sess._prepare_tool(_tc("spawn_batch", {"children": [{"initial_message": "solo"}]}))
    _call_id, output = sess._exec_spawn_batch(item)
    body = json.loads(output)
    assert "0" in body["results"]
    assert "status" not in body["results"]["0"]
    # Per-result entries surface ``child_ws_id``, not ``ws_id`` — same
    # recency-bias rationale as the spawn_workstream test above.
    assert body["results"]["0"]["child_ws_id"] == "c-x"
    assert "ws_id" not in body["results"]["0"]


def test_spawn_exec_surfaces_client_error(coord_session):
    sess, coord, ui = coord_session
    coord.spawn.return_value = {"error": "upstream unreachable", "status": 502}
    item = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "hi"}))
    _call_id, output = sess._exec_spawn_workstream(item)
    assert "upstream unreachable" in output
    # UI got an error result
    assert ui.tool_results[-1][3] is True  # is_error


def test_spawn_exec_treats_missing_ws_id_on_success_path_as_error(coord_session):
    """A malformed upstream response (200-success-shape with no
    ``ws_id``) used to emit ``{"child_ws_id": null}`` to the LLM,
    which then chased a null id through follow-up tools.  Now matches
    the matching guard in ``_exec_spawn_batch``: surface as a tool
    error so the model retries instead of acting on garbage."""
    sess, coord, ui = coord_session
    # No ``error`` field, but ``ws_id`` is missing — the silent-null path.
    coord.spawn.return_value = {"name": "c", "node_id": "node-1", "status": 200}
    item = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "hi"}))
    _call_id, output = sess._exec_spawn_workstream(item)
    assert "no ws_id" in output
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
    }
    item = sess._prepare_tool(_tc("inspect_workstream", {"ws_id": "child-x"}))
    _call_id, output = sess._exec_inspect_workstream(item)
    coord.inspect.assert_called_once_with(
        "child-x", message_limit=20, include_provider_content=False
    )
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


# ---------------------------------------------------------------------------
# cancel_workstream
# ---------------------------------------------------------------------------


def test_cancel_prepare_needs_approval(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("cancel_workstream", {"ws_id": "x"}))
    assert item["needs_approval"] is True
    assert "cancel_workstream" in item["header"]


def test_cancel_prepare_requires_ws_id(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("cancel_workstream", {}))
    assert "error" in item


def test_cancel_exec_dispatches(coord_session):
    sess, coord, _ui = coord_session
    coord.cancel.return_value = {"status": 200}
    item = sess._prepare_tool(_tc("cancel_workstream", {"ws_id": "x"}))
    _call_id, output = sess._exec_cancel_workstream(item)
    coord.cancel.assert_called_once_with("x")
    parsed = json.loads(output)
    assert parsed["cancelled"] is True
    assert parsed["ws_id"] == "x"


def test_cancel_exec_surfaces_client_error(coord_session):
    sess, coord, ui = coord_session
    coord.cancel.return_value = {"error": "ws not found", "status": 404}
    item = sess._prepare_tool(_tc("cancel_workstream", {"ws_id": "x"}))
    _call_id, output = sess._exec_cancel_workstream(item)
    assert "ws not found" in output
    assert ui.tool_results[-1][3] is True  # is_error


# ---------------------------------------------------------------------------
# wait_for_workstream
# ---------------------------------------------------------------------------


def test_wait_prepare_is_auto_approved(coord_session):
    """Prepare is a thin pass-through — auto-approved, no validation;
    the client owns ws_ids dedup / cap / timeout clamp / mode whitelist."""
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(
        _tc(
            "wait_for_workstream",
            {"ws_ids": ["a", "b"], "timeout": 5, "mode": "all"},
        )
    )
    assert item["needs_approval"] is False
    # Raw args pass through verbatim — the client validates / dedups.
    assert item["ws_ids"] == ["a", "b"]
    assert item["mode"] == "all"
    assert item["timeout"] == 5


def test_wait_exec_surfaces_client_validation_error(coord_session):
    """Bad input is rejected by the client and surfaced as a tool error
    via the result.get('error') branch in exec — single source of truth
    for validation."""
    sess, coord, ui = coord_session
    coord.wait_for_workstream.return_value = {
        "error": "ws_ids must contain at least one valid id",
        "results": {},
        "complete": False,
        "elapsed": 0.0,
        "mode": "any",
    }
    item = sess._prepare_tool(_tc("wait_for_workstream", {"ws_ids": []}))
    _call_id, output = sess._exec_wait_for_workstream(item)
    assert "must contain at least one" in output
    assert ui.tool_results[-1][3] is True  # is_error


def test_wait_exec_dispatches_raw_args_to_client(coord_session):
    sess, coord, _ui = coord_session
    coord.wait_for_workstream.return_value = {
        "results": {"a": {"state": "idle", "tokens": 0}},
        "complete": True,
        "elapsed": 0.5,
        "mode": "any",
    }
    item = sess._prepare_tool(_tc("wait_for_workstream", {"ws_ids": ["a"], "timeout": 30}))
    _call_id, output = sess._exec_wait_for_workstream(item)
    # Args forwarded raw (timeout int, default mode="any") — client
    # handles the float coerce + clamp.  ``since`` + ``progress_callback``
    # are optional observability kwargs added for the wait dashboard /
    # diff-hint items (#14, #18); match them via ANY so this assertion
    # stays focused on the raw dispatch.
    coord.wait_for_workstream.assert_called_once_with(
        ["a"], timeout=30, mode="any", since=None, progress_callback=ANY
    )
    parsed = json.loads(output)
    assert parsed["complete"] is True
    assert parsed["mode"] == "any"


def test_wait_exec_progress_callback_observes_cancel(coord_session):
    """The wait progress heartbeat is the cancel seam.  ``wait_for_workstream``
    holds no cancel handle, so without this a cancelled coordinator parked in a
    wait stays pinned for up to WAIT_MAX_TIMEOUT.  A GenerationCancelled raised
    from the heartbeat callback propagates out of the (otherwise cancel-blind)
    wait — _exec_wait_for_workstream's ``except Exception`` can't swallow it
    (GenerationCancelled is a BaseException)."""
    from turnstone.core.session import GenerationCancelled

    sess, coord, _ui = coord_session

    def _wait(ws_ids, *, timeout, mode, since, progress_callback):
        # Simulate the wait loop's ~2s heartbeat firing after the owner cancels.
        sess._cancel_event.set()
        progress_callback({"a": {"state": "running"}}, 0.1)  # must raise
        return {"results": {}, "complete": True, "elapsed": 0.1, "mode": mode}

    coord.wait_for_workstream.side_effect = _wait
    item = sess._prepare_tool(_tc("wait_for_workstream", {"ws_ids": ["a"]}))
    with pytest.raises(GenerationCancelled):
        sess._exec_wait_for_workstream(item)


def test_wait_exec_default_timeout_when_omitted(coord_session):
    """timeout=None (omitted) becomes 60.0 in exec so the client receives
    a numeric value — explicit ``timeout=0`` is preserved (one-shot
    poll) by passing the raw arg straight through."""
    sess, coord, _ui = coord_session
    coord.wait_for_workstream.return_value = {
        "results": {"a": {"state": "idle", "tokens": 0}},
        "complete": True,
        "elapsed": 0.0,
        "mode": "any",
    }
    item = sess._prepare_tool(_tc("wait_for_workstream", {"ws_ids": ["a"]}))
    sess._exec_wait_for_workstream(item)
    coord.wait_for_workstream.assert_called_once_with(
        ["a"], timeout=60.0, mode="any", since=None, progress_callback=ANY
    )


def test_wait_exec_preserves_explicit_zero_timeout(coord_session):
    """Explicit ``timeout=0`` reaches the client untouched."""
    sess, coord, _ui = coord_session
    coord.wait_for_workstream.return_value = {
        "results": {"a": {"state": "idle", "tokens": 0}},
        "complete": True,
        "elapsed": 0.0,
        "mode": "any",
    }
    item = sess._prepare_tool(_tc("wait_for_workstream", {"ws_ids": ["a"], "timeout": 0}))
    sess._exec_wait_for_workstream(item)
    coord.wait_for_workstream.assert_called_once_with(
        ["a"], timeout=0, mode="any", since=None, progress_callback=ANY
    )


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
        user_id="user-1",  # the constructor refuses anonymous coordinators
        kind="coordinator",
        coord_client=None,
    )
    # ``skills`` is excluded here on purpose: it's dual-kind and talks
    # directly to storage, so it has no coord_client dependency and
    # legitimately prepares without erroring when coord_client is absent.
    for tool, args in (
        ("spawn_workstream", {"initial_message": "hi"}),
        ("inspect_workstream", {"ws_id": "x"}),
        ("send_to_workstream", {"ws_id": "x", "message": "m"}),
        ("close_workstream", {"ws_id": "x"}),
        ("delete_workstream", {"ws_id": "x"}),
        ("list_workstreams", {}),
        ("list_nodes", {}),
        ("tasks", {"action": "list"}),
    ):
        item = sess._prepare_tool(_tc(tool, args))
        assert "error" in item, f"{tool} did not error on missing coord_client"


# ---------------------------------------------------------------------------
# list_nodes
# ---------------------------------------------------------------------------


def test_list_nodes_prepare_is_auto_approved(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("list_nodes", {}))
    assert item["needs_approval"] is False
    assert item["filters"] == {}
    assert item["limit"] == 100


def test_list_nodes_prepare_accepts_filters(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(
        _tc("list_nodes", {"filters": {"arch": "x86_64", "capability": "gpu"}})
    )
    assert item["filters"] == {"arch": "x86_64", "capability": "gpu"}


def test_list_nodes_prepare_drops_invalid_filter_types(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(
        _tc(
            "list_nodes",
            {"filters": {"arch": "x86_64", "bad": {"nested": "dict"}, "": "empty-key"}},
        )
    )
    # Nested dict values + empty keys are filtered out; string + primitive kept.
    assert item["filters"] == {"arch": "x86_64"}


def test_list_nodes_prepare_accepts_flat_args_as_filters(coord_session):
    """The model frequently drops the ``filters`` nesting and passes
    each filter as a top-level kwarg (``list_nodes(os="Linux",
    has_gpu=true)``).  Operators saw this surface during shakedown:
    flat-arg calls returned the full cluster because the strict-
    nested prepare silently dropped the filter.  The relaxed prepare
    treats every top-level non-reserved kwarg as a flat filter."""
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(
        _tc("list_nodes", {"os": "Linux", "gpu_has_nvidia": True, "memory_gb": 64})
    )
    assert item["filters"] == {"os": "Linux", "gpu_has_nvidia": True, "memory_gb": 64}


def test_list_nodes_prepare_reserves_paging_and_visibility_kwargs(coord_session):
    """Top-level reserved kwargs (``limit``, ``include_network_detail``,
    ``include_inactive``, ``filters``) are control parameters, NOT
    filters.  A flat call like ``list_nodes(limit=10, os="Linux")``
    must put ``limit`` on the paging path and ``os`` in the
    filter dict — not vice-versa."""
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(
        _tc(
            "list_nodes",
            {
                "limit": 10,
                "include_network_detail": True,
                "include_inactive": True,
                "os": "Linux",
            },
        )
    )
    assert item["limit"] == 10
    assert item["include_network_detail"] is True
    assert item["include_inactive"] is True
    assert item["filters"] == {"os": "Linux"}


def test_list_nodes_prepare_nested_wins_on_key_collision(coord_session):
    """When the model accidentally passes the same filter key both
    nested AND flat (rare but possible mid-refactor), the canonical
    nested form wins so the call is deterministic."""
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(
        _tc(
            "list_nodes",
            {
                "filters": {"os": "Linux"},  # canonical
                "os": "DifferentOS",  # flat — should NOT override
            },
        )
    )
    assert item["filters"] == {"os": "Linux"}


def test_list_nodes_prepare_mixes_nested_and_flat(coord_session):
    """A model can split filters across both shapes.  Both contribute
    to the final filter set; nested wins only on direct collisions."""
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(
        _tc(
            "list_nodes",
            {
                "filters": {"os": "Linux"},
                "gpu_has_nvidia": True,
                "memory_gb": 64,
            },
        )
    )
    assert item["filters"] == {
        "os": "Linux",
        "gpu_has_nvidia": True,
        "memory_gb": 64,
    }


def test_list_nodes_exec_dispatches_flat_arg_filters(coord_session):
    """End-to-end: flat-arg filters must actually flow through to the
    coordinator client's ``list_nodes(filters=...)`` call.  The bug
    operators reported was the filters being silently dropped on the
    way to storage; this test pins the prepare→exec wiring."""
    sess, coord, _ui = coord_session
    coord.list_nodes.return_value = {"nodes": [], "truncated": False}
    item = sess._prepare_tool(_tc("list_nodes", {"os": "Linux"}))
    sess._exec_list_nodes(item)
    kwargs = coord.list_nodes.call_args.kwargs
    assert kwargs["filters"] == {"os": "Linux"}


def test_list_nodes_prepare_clamps_limit(coord_session):
    sess, _coord, _ui = coord_session
    over = sess._prepare_tool(_tc("list_nodes", {"limit": 9999}))
    assert over["limit"] == 500
    # limit == 0 falls back to the default (100), not 1 — consistent with
    # the other coordinator list tools' ``int(args.get("limit") or 100)``.
    zero = sess._prepare_tool(_tc("list_nodes", {"limit": 0}))
    assert zero["limit"] == 100
    neg = sess._prepare_tool(_tc("list_nodes", {"limit": -5}))
    assert neg["limit"] == 1  # negative values clamp to 1


def test_list_nodes_exec_dispatches_to_client(coord_session):
    sess, coord, ui = coord_session
    coord.list_nodes.return_value = {
        "nodes": [{"node_id": "n1", "metadata": {"arch": {"value": "x86_64", "source": "auto"}}}],
        "truncated": False,
    }
    item = sess._prepare_tool(_tc("list_nodes", {"filters": {"arch": "x86_64"}}))
    call_id, output = sess._exec_list_nodes(item)
    assert call_id == "call-1"
    parsed = json.loads(output)
    assert parsed["nodes"][0]["node_id"] == "n1"
    assert parsed["truncated"] is False
    coord.list_nodes.assert_called_once_with(
        filters={"arch": "x86_64"},
        limit=100,
        include_network_detail=False,
        include_inactive=False,
    )


def test_list_nodes_exec_surfaces_truncated_sentinel(coord_session):
    sess, coord, ui = coord_session
    coord.list_nodes.return_value = {"nodes": [], "truncated": True}
    item = sess._prepare_tool(_tc("list_nodes", {}))
    _, _ = sess._exec_list_nodes(item)
    # Summary reported to UI carries the "truncated" hint.
    assert any("truncated" in r[2] for r in ui.tool_results)


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------


def test_tasks_list_is_auto_approved(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("tasks", {"action": "list"}))
    assert item["needs_approval"] is False
    assert item["action"] == "list"


def test_tasks_bare_string_fallback_uses_action_primary_key(coord_session):
    """A model that emits an unquoted ``list`` as the arguments blob
    lands on the ``primary_key=action`` fallback and recovers.  Before
    the fix primary_key was ``title`` so the fallback produced
    ``{"title": "list"}`` and hit the required-action rejection."""
    sess, _coord, _ui = coord_session
    call = {
        "id": "c1",
        "type": "function",
        "function": {"name": "tasks", "arguments": "list"},
    }
    item = sess._prepare_tool(call)
    assert "error" not in item
    assert item["action"] == "list"


def test_tasks_mutating_actions_need_approval(coord_session):
    sess, _coord, _ui = coord_session
    add_item = sess._prepare_tool(_tc("tasks", {"action": "add", "title": "plan"}))
    assert add_item["needs_approval"] is True
    update_item = sess._prepare_tool(
        _tc("tasks", {"action": "update", "task_id": "tsk_1", "status": "done"})
    )
    assert update_item["needs_approval"] is True
    remove_item = sess._prepare_tool(_tc("tasks", {"action": "remove", "task_id": "tsk_1"}))
    assert remove_item["needs_approval"] is True
    reorder_item = sess._prepare_tool(_tc("tasks", {"action": "reorder", "task_ids": ["tsk_1"]}))
    assert reorder_item["needs_approval"] is True


def test_tasks_unknown_action_errors(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("tasks", {"action": "wat"}))
    assert "error" in item


def test_tasks_non_string_action_errors_cleanly(coord_session):
    """A malformed ``action=42`` must NOT raise AttributeError during
    ``.strip().lower()`` — coerce to the empty string and fall through
    to the enum-check error."""
    sess, _coord, _ui = coord_session
    for bad_action in (42, None, ["list"], {"a": 1}, True):
        item = sess._prepare_tool(_tc("tasks", {"action": bad_action}))
        assert "error" in item, f"action={bad_action!r} did not produce a clean error"


def test_tasks_add_rejects_non_string_title_and_status(coord_session):
    """Add branch: ``title=42`` / ``status=0`` must NOT raise
    AttributeError during ``.strip()``; produce a clean error item."""
    sess, _coord, _ui = coord_session
    for bad in ({"action": "add", "title": 42}, {"action": "add", "title": "ok", "status": 0}):
        item = sess._prepare_tool(_tc("tasks", bad))
        assert "error" in item, f"args={bad!r} did not produce a clean error"


def test_tasks_remove_non_string_task_id_errors_cleanly(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("tasks", {"action": "remove", "task_id": 42}))
    assert "error" in item


def test_tasks_add_requires_title(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("tasks", {"action": "add", "title": ""}))
    assert "error" in item


def test_tasks_update_requires_task_id(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("tasks", {"action": "update", "status": "done"}))
    assert "error" in item


def test_tasks_update_rejects_non_string_field_values(coord_session):
    """Preview must not diverge from execute: reject non-string field
    values at prepare time rather than silently coercing to None."""
    sess, _coord, _ui = coord_session
    for field in ("title", "status", "child_ws_id"):
        item = sess._prepare_tool(_tc("tasks", {"action": "update", "task_id": "t1", field: 42}))
        assert "error" in item, f"update with non-string {field} should error"


def test_tasks_update_requires_at_least_one_field(coord_session):
    """update with only task_id is a no-op — reject to save an approval prompt."""
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("tasks", {"action": "update", "task_id": "t1"}))
    assert "error" in item


def test_tasks_remove_requires_task_id(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("tasks", {"action": "remove"}))
    assert "error" in item


def test_tasks_reorder_requires_list_of_strings(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("tasks", {"action": "reorder", "task_ids": [1, 2]}))
    assert "error" in item


def test_tasks_mixed_read_and_write_in_batch_rejected(coord_session):
    """The only shape the guard now rejects: ``tasks(list)`` paralleled
    with a ``tasks`` mutating action.  Read-after-write ordering inside
    ``run_one``'s ThreadPoolExecutor is unspecified, so the read can
    land before or after the write and produce inconsistent state.
    Both ``tasks(...)`` calls in the batch get the rejection error."""
    sess, _coord, _ui = coord_session
    tool_calls = [
        _tc("tasks", {"action": "add", "title": "a thing"}, call_id="call-1"),
        _tc("tasks", {"action": "list"}, call_id="call-2"),
    ]
    results, _fb = sess._execute_tools(tool_calls)
    by_id = dict(results)
    assert "read" in by_id["call-1"].lower() and "write" in by_id["call-1"].lower()
    assert "read" in by_id["call-2"].lower() and "write" in by_id["call-2"].lower()


def test_tasks_all_writes_in_batch_permitted(coord_session):
    """All-write batches are SAFE: the dispatcher runs them serially
    in input order (see ``test_tasks_writes_run_in_input_order``) so
    the final task list ordering matches the model's emit order, and
    each per-call lock acquisition under ``CoordinatorClient`` keeps
    the storage row consistent.  Four parallel ``tasks(add=...)`` is
    the canonical "decompose plan into N tasks" shape."""
    sess, coord, _ui = coord_session
    # Real ``CoordinatorClient.tasks_add`` returns the task dict
    # directly with top-level ``id`` / ``title`` / ``status`` /
    # ``child_ws_id`` / ``created`` / ``updated``.  Stubbing with
    # the matching shape so a future refactor that depends on the
    # actual contract (``result.get("id")`` etc.) doesn't pass
    # vacuously here.
    next_task_num = [0]

    def _tasks_add(*_a, **kw):
        next_task_num[0] += 1
        return {
            "id": f"t{next_task_num[0]}",
            "title": kw.get("title", ""),
            "status": "pending",
            "child_ws_id": kw.get("child_ws_id", ""),
            "created": "2026-04-28T00:00:00",
            "updated": "2026-04-28T00:00:00",
        }

    coord.tasks_add.side_effect = _tasks_add
    tool_calls = [
        _tc("tasks", {"action": "add", "title": f"task {i}"}, call_id=f"call-{i}") for i in range(4)
    ]
    results, _fb = sess._execute_tools(tool_calls)
    for _cid, output in results:
        assert "read-after-write" not in output.lower(), output
        assert "cannot run" not in output.lower(), output


def test_tasks_writes_run_in_input_order(coord_session):
    """Regression guard: ``tasks_add`` calls must reach the
    coordinator client in the SAME order the model emitted them.
    Pre-fix, ``ThreadPoolExecutor.map`` dispatched in
    scheduler-dependent order — the SET of tasks ended up consistent
    but the final list ordering (and timestamps/IDs) varied
    run-to-run.  The fix runs any batch containing a tasks-write
    serially in input order; this test pins the property by capturing
    the title sequence as ``tasks_add`` sees it."""
    sess, coord, _ui = coord_session
    seen_titles: list[str] = []

    def _tasks_add(*_a, **kw):
        seen_titles.append(kw.get("title", ""))
        return {
            "id": f"t{len(seen_titles)}",
            "title": kw.get("title", ""),
            "status": "pending",
            "child_ws_id": "",
            "created": "2026-04-28T00:00:00",
            "updated": "2026-04-28T00:00:00",
        }

    coord.tasks_add.side_effect = _tasks_add
    titles = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
    tool_calls = [
        _tc("tasks", {"action": "add", "title": t}, call_id=f"call-{i}")
        for i, t in enumerate(titles)
    ]
    sess._execute_tools(tool_calls)
    # Exact input-order preservation — no scheduler-dependent
    # interleaving.
    assert seen_titles == titles


def test_tasks_writes_serial_when_mixed_with_non_tasks_siblings(coord_session):
    """Even when the batch mixes a tasks-write with non-tasks
    siblings, the tasks-write path must still preserve input order
    (the dispatcher runs the WHOLE batch serially in this case to
    keep the implementation simple).  A coord adding 2 tasks +
    listing nodes in one turn shouldn't see scheduler-shuffled task
    titles."""
    sess, coord, _ui = coord_session
    seen_titles: list[str] = []

    def _tasks_add(*_a, **kw):
        seen_titles.append(kw.get("title", ""))
        return {
            "id": f"t{len(seen_titles)}",
            "title": kw.get("title", ""),
            "status": "pending",
            "child_ws_id": "",
            "created": "2026-04-28T00:00:00",
            "updated": "2026-04-28T00:00:00",
        }

    coord.tasks_add.side_effect = _tasks_add
    coord.list_nodes.return_value = {"nodes": [], "truncated": False}
    tool_calls = [
        _tc("tasks", {"action": "add", "title": "first"}, call_id="call-1"),
        _tc("list_nodes", {}, call_id="call-2"),
        _tc("tasks", {"action": "add", "title": "second"}, call_id="call-3"),
    ]
    sess._execute_tools(tool_calls)
    assert seen_titles == ["first", "second"]


def test_tasks_all_reads_in_batch_permitted(coord_session):
    """All-read batches are SAFE: nothing to race against."""
    sess, coord, _ui = coord_session
    coord.tasks_get.return_value = {"tasks": []}
    tool_calls = [
        _tc("tasks", {"action": "list"}, call_id="call-1"),
        _tc("tasks", {"action": "list"}, call_id="call-2"),
    ]
    results, _fb = sess._execute_tools(tool_calls)
    for _cid, output in results:
        assert "read-after-write" not in output.lower(), output


def test_tasks_runs_normally_when_alone_in_batch(coord_session):
    """A single ``tasks(...)`` call is unaffected by the read-after-
    write guard — only multi-call batches with a mix can trip it."""
    sess, _coord, _ui = coord_session
    results, _fb = sess._execute_tools([_tc("tasks", {"action": "list"})])
    _call_id, output = results[0]
    assert "read-after-write" not in output.lower()


def test_tasks_write_with_non_tasks_sibling_permitted(coord_session):
    """A ``tasks`` write paralleled with a non-``tasks`` sibling is
    fine — the sibling doesn't touch tasks state, so there's no
    race regardless of dispatch order.  This is the natural batch
    shape for "add a task AND look up something else"."""
    sess, coord, _ui = coord_session
    # Match real ``CoordinatorClient.tasks_add`` shape — dict
    # returned directly, not wrapped in ``{"ok": True, "task": ...}``.
    coord.tasks_add.return_value = {
        "id": "t1",
        "title": "a",
        "status": "pending",
        "child_ws_id": "",
        "created": "2026-04-28T00:00:00",
        "updated": "2026-04-28T00:00:00",
    }
    tool_calls = [
        _tc("tasks", {"action": "add", "title": "a"}, call_id="call-1"),
        _tc("inspect_workstream", {"ws_id": "child-x"}, call_id="call-2"),
    ]
    results, _fb = sess._execute_tools(tool_calls)
    for _cid, output in results:
        assert "read-after-write" not in output.lower(), output


def test_tasks_read_with_non_tasks_sibling_permitted(coord_session):
    """Mirror of the write-with-sibling test for the read direction.
    Common shape: ``tasks(list)`` paralleled with ``list_workstreams``
    / ``list_nodes`` for a planning snapshot."""
    sess, coord, _ui = coord_session
    coord.tasks_get.return_value = {"tasks": []}
    tool_calls = [
        _tc("tasks", {"action": "list"}, call_id="call-1"),
        _tc("list_workstreams", {}, call_id="call-2"),
        _tc("list_nodes", {}, call_id="call-3"),
    ]
    results, _fb = sess._execute_tools(tool_calls)
    for _cid, output in results:
        assert "read-after-write" not in output.lower(), output


def test_non_tasks_parallel_batch_unaffected(coord_session):
    """Tools other than ``tasks`` keep working in parallel batches
    regardless of read/write semantics — the guard is scoped only
    to ``tasks``'s read-after-write hazard."""
    sess, _coord, _ui = coord_session
    tool_calls = [
        _tc("inspect_workstream", {"ws_id": "child-a"}, call_id="call-1"),
        _tc("list_workstreams", {}, call_id="call-2"),
    ]
    results, _fb = sess._execute_tools(tool_calls)
    for _cid, output in results:
        assert "read-after-write" not in output.lower()


def test_tasks_exec_list_returns_tasks(coord_session):
    sess, coord, _ui = coord_session
    coord.tasks_get.return_value = {
        "version": 1,
        "tasks": [{"id": "tsk_1", "title": "do", "status": "pending"}],
    }
    item = sess._prepare_tool(_tc("tasks", {"action": "list"}))
    _, output = sess._exec_tasks(item)
    parsed = json.loads(output)
    assert parsed["tasks"][0]["id"] == "tsk_1"
    assert parsed["truncated"] is False


def test_tasks_exec_list_page_caps_at_200(coord_session):
    sess, coord, _ui = coord_session
    coord.tasks_get.return_value = {
        "version": 1,
        "tasks": [{"id": f"tsk_{i}", "title": "x", "status": "pending"} for i in range(250)],
    }
    item = sess._prepare_tool(_tc("tasks", {"action": "list"}))
    _, output = sess._exec_tasks(item)
    parsed = json.loads(output)
    assert len(parsed["tasks"]) == 200
    assert parsed["truncated"] is True


def test_tasks_exec_add_dispatches(coord_session):
    sess, coord, _ui = coord_session
    coord.tasks_add.return_value = {"id": "tsk_new", "title": "plan"}
    item = sess._prepare_tool(_tc("tasks", {"action": "add", "title": "plan", "status": "pending"}))
    _, _ = sess._exec_tasks(item)
    coord.tasks_add.assert_called_once_with(
        sess._ws_id, title="plan", status="pending", child_ws_id="", note=""
    )


def test_tasks_exec_reorder_surfaces_permutation_error(coord_session):
    sess, coord, _ui = coord_session
    coord.tasks_reorder.return_value = {"error": "task_ids must be a permutation..."}
    item = sess._prepare_tool(_tc("tasks", {"action": "reorder", "task_ids": ["wrong"]}))
    _, output = sess._exec_tasks(item)
    parsed = json.loads(output)
    assert "error" in parsed


def test_tasks_exec_remove_passes_client_dict_through(coord_session):
    """The client returns a dict; exec must pass it through without
    synthesising a generic 'not found' message that would mask corrupt-
    envelope errors from the LLM."""
    sess, coord, _ui = coord_session
    coord.tasks_remove.return_value = {
        "error": "tasks envelope is corrupt on disk; refusing to overwrite."
    }
    item = sess._prepare_tool(_tc("tasks", {"action": "remove", "task_id": "x"}))
    _, output = sess._exec_tasks(item)
    parsed = json.loads(output)
    assert "corrupt" in parsed["error"]


def test_tasks_exec_remove_success_dispatches(coord_session):
    sess, coord, _ui = coord_session
    coord.tasks_remove.return_value = {"ok": True, "task_id": "tsk_1"}
    item = sess._prepare_tool(_tc("tasks", {"action": "remove", "task_id": "tsk_1"}))
    _, output = sess._exec_tasks(item)
    parsed = json.loads(output)
    assert parsed.get("ok") is True


# ---------------------------------------------------------------------------
# Smoke-test regressions — empty-arg tool calls, metadata stripping,
# provider-content trimming
# ---------------------------------------------------------------------------


def test_prepare_tool_empty_arguments_string_parses_as_object(coord_session):
    """Some providers emit an empty string when a tool is invoked with
    no arguments (all params optional).  The empty string must be
    treated as ``{}`` rather than dropped into the malformed-JSON
    error branch — otherwise zero-arg coordinator tool calls fail."""
    sess, coord, _ui = coord_session
    coord.list_nodes.return_value = {"nodes": [], "truncated": False}
    tc = {
        "id": "call-empty",
        "type": "function",
        "function": {"name": "list_nodes", "arguments": ""},
    }
    item = sess._prepare_tool(tc)
    # No error field, prepared for list_nodes exec.
    assert "error" not in item
    assert item["func_name"] == "list_nodes"


def test_list_nodes_strips_interfaces_by_default(coord_session):
    """Default ``list_nodes`` output omits the auto-populated
    ``interfaces`` key — it leaks internal RFC 1918 addresses and the
    model never uses it for routing decisions."""
    sess, coord, _ui = coord_session
    item = sess._prepare_tool(_tc("list_nodes", {}))
    coord.list_nodes.assert_not_called()  # prepare doesn't fire the client yet
    assert item["include_network_detail"] is False
    sess._exec_list_nodes(item)
    coord.list_nodes.assert_called_once()
    kwargs = coord.list_nodes.call_args.kwargs
    assert kwargs.get("include_network_detail") is False


def test_list_nodes_include_network_detail_opt_in(coord_session):
    """Opt-in flag flips include_network_detail=True through to the client."""
    sess, coord, _ui = coord_session
    item = sess._prepare_tool(_tc("list_nodes", {"include_network_detail": True}))
    assert item["include_network_detail"] is True
    sess._exec_list_nodes(item)
    kwargs = coord.list_nodes.call_args.kwargs
    assert kwargs.get("include_network_detail") is True


def test_inspect_workstream_default_trims_provider_content(coord_session):
    """Default ``inspect_workstream`` threads
    ``include_provider_content=False`` through to the client so the
    ``_provider_content`` / ``provider_blocks`` duplicates don't bloat
    the response."""
    sess, coord, _ui = coord_session
    item = sess._prepare_tool(_tc("inspect_workstream", {"ws_id": "abc123"}))
    assert item["include_provider_content"] is False
    sess._exec_inspect_workstream(item)
    kwargs = coord.inspect.call_args.kwargs
    assert kwargs.get("include_provider_content") is False


def test_inspect_workstream_include_provider_content_opt_in(coord_session):
    sess, coord, _ui = coord_session
    item = sess._prepare_tool(
        _tc(
            "inspect_workstream",
            {"ws_id": "abc123", "include_provider_content": True},
        )
    )
    assert item["include_provider_content"] is True
    sess._exec_inspect_workstream(item)
    kwargs = coord.inspect.call_args.kwargs
    assert kwargs.get("include_provider_content") is True


# ---------------------------------------------------------------------------
# spawn_batch
# ---------------------------------------------------------------------------


def _three_children() -> list[dict[str, Any]]:
    return [
        {"initial_message": "benchmark A", "skill": "researcher"},
        {"initial_message": "benchmark B", "skill": "researcher", "target_node": "n-1"},
        {"initial_message": "", "name": "idle-child"},
    ]


def test_spawn_batch_prepare_rejects_non_list(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("spawn_batch", {"children": "not-a-list"}))
    assert "error" in item
    assert "non-empty list" in item["error"]


def test_spawn_batch_prepare_rejects_empty_list(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("spawn_batch", {"children": []}))
    assert "error" in item


def test_spawn_batch_prepare_rejects_over_cap(coord_session):
    sess, _coord, _ui = coord_session
    too_many = [{"initial_message": f"msg-{i}"} for i in range(11)]
    item = sess._prepare_tool(_tc("spawn_batch", {"children": too_many}))
    assert "error" in item
    assert "cap" in item["error"].lower()


def test_spawn_batch_prepare_builds_approval_card(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("spawn_batch", {"children": _three_children()}))
    assert "error" not in item
    assert item["needs_approval"] is True
    assert item["func_name"] == "spawn_batch"
    assert "3 children" in item["header"]
    # Each row shows up in the preview (dim-wrapped).
    for idx in (0, 1, 2):
        assert f"{idx}." in item["preview"]
    assert "skill=researcher" in item["preview"]
    assert "node=n-1" in item["preview"]
    # Idle row renders as "(idle)".
    assert "(idle)" in item["preview"]


def test_spawn_batch_exec_serialises_spawns_and_returns_results(coord_session):
    sess, coord, _ui = coord_session
    spawned: list[dict[str, Any]] = []

    def _spawn(**kwargs):
        n = len(spawned)
        ws = {
            "ws_id": f"child-{n}",
            "name": kwargs.get("name") or f"auto-{n}",
            "node_id": kwargs.get("target_node") or "node-auto",
            "status": 200,
        }
        spawned.append(kwargs)
        return ws

    coord.spawn.side_effect = _spawn
    item = sess._prepare_tool(_tc("spawn_batch", {"children": _three_children()}))
    _call_id, output = sess._exec_spawn_batch(item)

    # Three serial spawn() calls, in input order.
    assert len(spawned) == 3
    assert [s["initial_message"] for s in spawned] == ["benchmark A", "benchmark B", ""]
    assert spawned[0]["skill"] == "researcher"
    assert spawned[1]["target_node"] == "n-1"
    assert spawned[2]["name"] == "idle-child"

    body = json.loads(output)
    assert "truncated" not in body  # prepare hard-errors >10, no truncate state
    assert body["denied"] == []
    # Keyed by input index (stringified).
    assert set(body["results"].keys()) == {"0", "1", "2"}
    assert body["results"]["0"]["child_ws_id"] == "child-0"
    assert body["results"]["1"]["node_id"] == "n-1"
    assert body["results"]["2"]["child_ws_id"] == "child-2"
    # Confirm we don't leak the old ``ws_id`` key alongside the new
    # ``child_ws_id`` — see test_spawn_exec_does_not_surface_misleading_status_field
    # for the rationale on the rename.
    assert "ws_id" not in body["results"]["0"]


def test_spawn_batch_exec_surfaces_per_item_errors_in_denied(coord_session):
    sess, coord, _ui = coord_session

    counter = {"n": 0}

    def _spawn(**kwargs):
        msg = kwargs.get("initial_message", "")
        if msg == "benchmark B":
            return {"error": "skill not found: researcher", "status": 400}
        counter["n"] += 1
        return {
            "ws_id": f"child-{counter['n']}",
            "name": "n",
            "node_id": "node-auto",
            "status": 200,
        }

    coord.spawn.side_effect = _spawn
    item = sess._prepare_tool(_tc("spawn_batch", {"children": _three_children()}))
    _call_id, output = sess._exec_spawn_batch(item)
    body = json.loads(output)
    assert set(body["results"].keys()) == {"0", "2"}
    assert len(body["denied"]) == 1
    assert body["denied"][0]["idx"] == 1
    assert "skill not found" in body["denied"][0]["reason"]


def test_spawn_batch_exec_stops_spawning_after_cancel(coord_session):
    """A cancel mid-batch stops creating the REST of the children.  The
    already-spawned child stays in ``results`` (it is a live remote
    workstream); the remainder are marked not-spawned rather than created."""
    sess, coord, _ui = coord_session

    spawned: list[dict[str, Any]] = []

    def _spawn(**kwargs):
        n = len(spawned)
        spawned.append(kwargs)
        # Owner cancels right after the first child is created.
        sess._cancel_event.set()
        return {"ws_id": f"child-{n}", "name": "n", "node_id": "node", "status": 200}

    coord.spawn.side_effect = _spawn
    item = sess._prepare_tool(_tc("spawn_batch", {"children": _three_children()}))
    _call_id, output = sess._exec_spawn_batch(item)
    body = json.loads(output)

    # Only the first child was actually spawned — the cancel halted the rest.
    assert len(spawned) == 1
    assert set(body["results"].keys()) == {"0"}
    assert body["results"]["0"]["child_ws_id"] == "child-0"
    # The remaining two are reported not-spawned (cancelled), not created.
    cancelled = [d for d in body["denied"] if "cancelled" in d["reason"].lower()]
    assert {d["idx"] for d in cancelled} == {1, 2}


def test_spawn_batch_exec_continues_past_client_exception(coord_session):
    sess, coord, _ui = coord_session

    def _spawn(**kwargs):
        if kwargs.get("initial_message") == "benchmark A":
            raise RuntimeError("transient network error")
        return {
            "ws_id": "ok",
            "name": "n",
            "node_id": "node",
            "status": 200,
        }

    coord.spawn.side_effect = _spawn
    item = sess._prepare_tool(_tc("spawn_batch", {"children": _three_children()}))
    _call_id, output = sess._exec_spawn_batch(item)
    body = json.loads(output)
    # First item raised; other two succeed — partial-success semantics.
    assert "0" not in body["results"]
    assert "1" in body["results"] and "2" in body["results"]
    assert any(d["idx"] == 0 and "transient network error" in d["reason"] for d in body["denied"])


def test_spawn_batch_exec_emits_batch_started_and_ended(coord_session):
    sess, coord, _ui = coord_session
    events: list[dict[str, Any]] = []
    # Attach a minimal _enqueue on the UI so _emit_batch_event fires.
    sess.ui._enqueue = events.append  # type: ignore[attr-defined]
    coord.spawn.return_value = {
        "ws_id": "c-x",
        "name": "n",
        "node_id": "node",
        "status": 200,
    }
    item = sess._prepare_tool(_tc("spawn_batch", {"children": [{"initial_message": "solo"}]}))
    sess._exec_spawn_batch(item)
    types = [e["type"] for e in events]
    assert types[0] == "batch_started"
    assert types[-1] == "batch_ended"
    assert events[0]["op"] == "spawn_batch"
    assert events[0]["total"] == 1
    assert events[-1]["succeeded"] == 1
    assert events[-1]["denied"] == 0


# ---------------------------------------------------------------------------
# spawn_batch — _evaluate_intent func_args projection (sec-3 follow-up)
# ---------------------------------------------------------------------------
#
# The judge (heuristic + LLM) reads ``item["func_args"]`` to reason about
# what the coordinator is about to do. Pre-fix, spawn_batch projected only
# the FIRST child's skill + initial_message — a malicious mid-batch entry
# was invisible to both tiers. These tests pin the full-children projection.


def _stub_judge_for_evaluate_intent(monkeypatch, sess):
    """Stub _ensure_judge so _evaluate_intent's setup loop runs.

    The actual judge.evaluate() is mocked to return one verdict per item
    so the heuristic-attach loop doesn't IndexError. Tests assert on the
    func_args populated BEFORE judge.evaluate is invoked.
    """
    fake_verdict = MagicMock()
    fake_verdict.to_dict.return_value = {"verdict_id": "v0", "tier": "heuristic"}
    fake_judge = MagicMock()
    # judge.evaluate(items, messages, callback=, cancel_event=) → list[verdict]
    fake_judge.evaluate.side_effect = lambda items, *_args, **_kw: [fake_verdict] * len(items)
    # arg_budget_chars() feeds honest_truncate in the projection loop and must
    # be a real int, not a MagicMock; large enough that nothing truncates.
    fake_judge.arg_budget_chars.return_value = 200_000
    monkeypatch.setattr(sess, "_ensure_judge", lambda: fake_judge)
    return fake_judge


def test_spawn_batch_evaluate_intent_projects_all_children(coord_session, monkeypatch):
    sess, _coord, _ui = coord_session
    _stub_judge_for_evaluate_intent(monkeypatch, sess)
    item = sess._prepare_tool(
        _tc(
            "spawn_batch",
            {
                "children": [
                    {"initial_message": "audit auth.py for CSRF", "skill": "engineer"},
                    {"initial_message": "rm -rf the docs tree", "skill": "bash-runner"},
                    {
                        "initial_message": "compare FastAPI vs Starlette",
                        "skill": "researcher",
                        "target_node": "node-7",
                    },
                ]
            },
        )
    )
    sess._evaluate_intent([item])

    fa = item["func_args"]
    assert fa["child_count"] == 3
    children = fa["children"]
    assert len(children) == 3
    assert children[0]["skill"] == "engineer"
    assert children[0]["initial_message"] == "audit auth.py for CSRF"
    assert children[0]["target_node"] == ""
    # Mid-batch entry is fully visible — the bug this fix exists to close.
    assert children[1]["skill"] == "bash-runner"
    assert children[1]["initial_message"] == "rm -rf the docs tree"
    assert children[2]["skill"] == "researcher"
    assert children[2]["target_node"] == "node-7"


def test_spawn_batch_evaluate_intent_truncates_long_messages(coord_session, monkeypatch):
    sess, _coord, _ui = coord_session
    fake_judge = _stub_judge_for_evaluate_intent(monkeypatch, sess)
    # Each child's initial_message is truncated to its share of the judge's
    # arg budget (window-based), not a fixed cap, and the omission is honest.
    fake_judge.arg_budget_chars.return_value = 300  # 1 child → 300 chars/child
    long_msg = "x" * 500
    item = sess._prepare_tool(
        _tc("spawn_batch", {"children": [{"initial_message": long_msg, "skill": "researcher"}]})
    )
    sess._evaluate_intent([item])

    children = item["func_args"]["children"]
    assert len(children) == 1
    msg = children[0]["initial_message"]
    assert msg.startswith("x" * 300)
    assert "200 of 500 chars omitted" in msg


def test_spawn_batch_evaluate_intent_handles_empty_children_defensively(coord_session, monkeypatch):
    """``_prepare_spawn_batch`` rejects an empty children list before this
    code runs, so we shouldn't reach _evaluate_intent with one in
    practice — but if a future caller bypasses the preparer the
    projection must still produce a valid dict. Pinning the defensive
    shape so the JSON-serialised verdict row stays well-formed."""
    sess, _coord, _ui = coord_session
    _stub_judge_for_evaluate_intent(monkeypatch, sess)
    # Synthesise an item directly — bypassing _prepare_tool, since the
    # preparer's empty-list rejection would prevent us reaching here.
    fake_item = {
        "call_id": "call-empty",
        "func_name": "spawn_batch",
        "needs_approval": True,
        "approval_label": "spawn_batch",
        "children": [],
    }
    sess._evaluate_intent([fake_item])

    fa = fake_item["func_args"]
    assert fa["child_count"] == 0
    assert fa["children"] == []


# ---------------------------------------------------------------------------
# Regression: tasks(update) without title — _prepare_tasks stores
# ``item["title"] = None`` (title is optional on update), then
# _evaluate_intent's projection sliced ``it.get("title", "")[:100]``.
# dict.get returns the stored ``None`` (the default applies only when
# the key is absent), so the slice raised TypeError and aborted the
# whole batch.  Sibling tool calls in the same parallel batch then
# surfaced as "Tool execution was cancelled" because the assistant
# message had recorded the tool calls but the evaluator never wrote
# tool-result entries.
# ---------------------------------------------------------------------------


def test_tasks_update_without_title_evaluates_intent_cleanly(coord_session, monkeypatch):
    """tasks(update) with status only (no title) must not crash the
    intent projection — the missing-but-optional title field stored as
    None used to TypeError on the [:100] slice."""
    sess, _coord, _ui = coord_session
    _stub_judge_for_evaluate_intent(monkeypatch, sess)
    item = sess._prepare_tool(
        _tc("tasks", {"action": "update", "task_id": "tsk_1", "status": "in_progress"})
    )
    assert "error" not in item
    # The crash trigger: item["title"] is None after _prepare_tasks.
    assert item["title"] is None
    sess._evaluate_intent([item])
    # title collapses None → "" (truncatable text); status is projected so the
    # judge can see what state is being set; child_ws_id and note pass through
    # as None ("unchanged"), never sliced.  note deliberately does NOT follow
    # title's collapse — an empty note is a legal value (it clears the field),
    # so None → "" would show the judge a clear nobody requested.
    assert item["func_args"] == {
        "action": "update",
        "task_id": "tsk_1",
        "title": "",
        "status": "in_progress",
        "child_ws_id": None,
        "note": None,
    }


def test_tasks_update_without_title_in_parallel_batch_does_not_cancel_siblings(
    coord_session, monkeypatch
):
    """Reproduce the parallel-batch failure mode: tasks(update) without
    title alongside other tools.  Pre-fix, the evaluator raised before
    any sibling executed, leaving every sibling reported as cancelled.
    Post-fix, all items get func_args populated and the batch proceeds
    to the judge."""
    sess, _coord, _ui = coord_session
    _stub_judge_for_evaluate_intent(monkeypatch, sess)
    update_item = sess._prepare_tool(
        _tc("tasks", {"action": "update", "task_id": "tsk_1", "status": "in_progress"})
    )
    # tasks(add) — sibling that previously got orphaned/cancelled.
    add_item = sess._prepare_tool(_tc("tasks", {"action": "add", "title": "next step"}))
    sess._evaluate_intent([update_item, add_item])
    # Both items projected; neither carried over the None crash.
    assert update_item["func_args"]["title"] == ""
    assert add_item["func_args"]["title"] == "next step"


# ---------------------------------------------------------------------------
# close_all_children
# ---------------------------------------------------------------------------


def test_close_all_children_prepare_builds_approval_card(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("close_all_children", {"reason": "batch done"}))
    assert "error" not in item
    assert item["needs_approval"] is True
    assert item["func_name"] == "close_all_children"
    assert "batch done" in item["header"]
    assert item["reason"] == "batch done"


def test_close_all_children_prepare_accepts_empty_reason(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("close_all_children", {}))
    assert "error" not in item
    assert item["reason"] == ""


def test_close_all_children_exec_posts_to_endpoint_and_summarises(coord_session):
    sess, coord, _ui = coord_session
    coord.close_all_children.return_value = {
        "status": "ok",
        "closed": ["c-1", "c-2"],
        "failed": [],
        "skipped": ["c-3"],
    }
    item = sess._prepare_tool(_tc("close_all_children", {"reason": "done"}))
    _call_id, output = sess._exec_close_all_children(item)
    coord.close_all_children.assert_called_once_with(reason="done")
    body = json.loads(output)
    assert body == {
        "closed": ["c-1", "c-2"],
        "failed": [],
        "skipped": ["c-3"],
        "reason": "done",
    }


def test_close_all_children_exec_surfaces_client_error(coord_session):
    sess, coord, ui = coord_session
    coord.close_all_children.return_value = {
        "error": "upstream unreachable",
        "status": 502,
    }
    item = sess._prepare_tool(_tc("close_all_children", {}))
    _call_id, output = sess._exec_close_all_children(item)
    assert "upstream unreachable" in output
    assert ui.tool_results[-1][3] is True  # is_error=True


def test_close_all_children_exec_surfaces_client_exception(coord_session):
    sess, coord, ui = coord_session
    coord.close_all_children.side_effect = RuntimeError("boom")
    item = sess._prepare_tool(_tc("close_all_children", {}))
    _call_id, output = sess._exec_close_all_children(item)
    assert "boom" in output
    assert ui.tool_results[-1][3] is True


def test_close_all_children_exec_emits_batch_events(coord_session):
    sess, coord, _ui = coord_session
    events: list[dict[str, Any]] = []
    sess.ui._enqueue = events.append  # type: ignore[attr-defined]
    coord.close_all_children.return_value = {
        "status": "ok",
        "closed": ["c-1"],
        "failed": [],
        "skipped": [],
    }
    item = sess._prepare_tool(_tc("close_all_children", {"reason": "r"}))
    sess._exec_close_all_children(item)
    types = [e["type"] for e in events]
    assert types[0] == "batch_started"
    assert types[-1] == "batch_ended"
    assert events[0]["op"] == "close_all_children"
    assert events[-1]["closed"] == 1


# ---------------------------------------------------------------------------
# _coord_client=None guard — covers the first-line bail-out in both new
# prepare methods.  The branch matters because a coord session hitting
# this state signals a construction bug, and the LLM needs a clean tool
# error (not a crashing tool-exec).
# ---------------------------------------------------------------------------


def test_spawn_batch_prepare_errors_when_coord_client_unavailable(coord_session):
    sess, _coord, _ui = coord_session
    sess._coord_client = None
    item = sess._prepare_tool(_tc("spawn_batch", {"children": [{"initial_message": "hi"}]}))
    assert "error" in item
    assert "unavailable" in item["error"]


def test_close_all_children_prepare_errors_when_coord_client_unavailable(coord_session):
    sess, _coord, _ui = coord_session
    sess._coord_client = None
    item = sess._prepare_tool(_tc("close_all_children", {}))
    assert "error" in item
    assert "unavailable" in item["error"]


# ---------------------------------------------------------------------------
# notify — dual-kind invocability on coordinator sessions
# ---------------------------------------------------------------------------
#
# notify joined the coord toolset so orchestrators can post status updates
# at narrative beats (fan-out complete, batch failed, phase done) without
# spawning a child purely to ship a message. _prepare_notify / _exec_notify
# are session-kind-agnostic — the routing logic is identical to interactive
# sessions; these tests pin the coord-side dispatch wiring.


def test_notify_prepare_on_coord_session_dispatches_cleanly(coord_session):
    """A coord session can reach _prepare_notify via the standard
    dispatcher and produce a well-formed execute item."""
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(
        _tc(
            "notify",
            {"message": "fan-out of 3 children complete", "username": "admin"},
        )
    )
    assert "error" not in item
    assert item["func_name"] == "notify"
    assert item["execute"].__func__ is ChatSession._exec_notify
    assert item["message"] == "fan-out of 3 children complete"
    assert item["username"] == "admin"
    # notify carries ``auto_approve: true`` in notify.json and
    # ``_prepare_notify`` hardcodes ``needs_approval: False`` — pin the
    # auto-approve contract on the coord surface so a future change that
    # tightens approval semantics has to update this test deliberately.
    assert item["needs_approval"] is False


def test_notify_exec_on_coord_session_sends_via_channel_gateway(coord_session, tmp_path):
    """End-to-end: coord-session notify reaches the channel gateway path
    with the same payload shape an interactive session would emit."""
    sess, _coord, _ui = coord_session
    storage = SQLiteBackend(str(tmp_path / "test.db"))
    storage.register_service("channel", "ch-1", "http://localhost:8091")

    item = sess._prepare_tool(
        _tc(
            "notify",
            {"message": "batch failed on child-x", "username": "admin"},
        )
    )
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "results": [{"channel_type": "discord", "channel_id": "123", "status": "sent"}]
    }
    with (
        patch("turnstone.core.session.get_storage", return_value=storage),
        patch("turnstone.core.session.httpx.post", return_value=mock_resp) as mock_post,
    ):
        call_id, msg = sess._exec_notify(item)

    assert call_id == "call-1"
    assert "sent successfully" in msg.lower()
    post_kwargs = mock_post.call_args.kwargs
    assert post_kwargs["json"]["target"] == {"username": "admin"}
    assert post_kwargs["json"]["message"] == "batch failed on child-x"
    assert post_kwargs["json"]["ws_id"] == "coord-1"


def test_tasks_prepare_add_rejects_non_string_note(coord_session):
    """Without the type check, ``note=42`` reaches ``tasks_add`` and blows
    up inside ``.strip()`` as a generic 'add failed'."""
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("tasks", {"action": "add", "title": "t", "note": 42}))
    assert "error" in item
    assert "note must be a string" in item["error"]


def test_tasks_prepare_update_rejects_non_string_note(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("tasks", {"action": "update", "task_id": "tsk_1", "note": ["x"]}))
    assert "error" in item
    assert "note must be a string" in item["error"]


def test_tasks_prepare_update_with_note_alone_is_accepted(coord_session):
    """A note-only update is exactly the shape the idle-tasks nudge tells
    the model to make, so it must count toward 'something to update'."""
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(
        _tc("tasks", {"action": "update", "task_id": "tsk_1", "note": "need a decision"})
    )
    assert "error" not in item
    assert item["note"] == "need a decision"


def test_tasks_prepare_update_with_nothing_still_rejected(coord_session):
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("tasks", {"action": "update", "task_id": "tsk_1"}))
    assert "error" in item
    assert "at least one of" in item["error"]


def test_tasks_prepare_add_preview_carries_note(coord_session):
    """The operator approves the mutation from the preview; a hidden note
    means approving an ask they cannot read."""
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(
        _tc(
            "tasks",
            {
                "action": "add",
                "title": "pick a backend",
                "status": "needs_user",
                "note": "which auth backend is canonical?",
            },
        )
    )
    assert "error" not in item
    assert "which auth backend is canonical?" in item["preview"]


def test_tasks_projection_carries_note_to_judge(coord_session, monkeypatch):
    """Smart Approvals rules on ``func_args`` alone — an unprojected field
    is invisible to it."""
    sess, _coord, _ui = coord_session
    _stub_judge_for_evaluate_intent(monkeypatch, sess)
    item = sess._prepare_tool(
        _tc(
            "tasks",
            {
                "action": "update",
                "task_id": "tsk_1",
                "status": "needs_user",
                "note": "need a decision on the schema",
            },
        )
    )
    sess._evaluate_intent([item])
    assert item["func_args"]["note"] == "need a decision on the schema"


def test_tasks_preview_marks_truncated_note(coord_session):
    """The operator rules on the preview.  A bare slice reads as the
    whole argument — and with notes capped at 200 the half they never saw
    can be the half naming the destructive option."""
    sess, _coord, _ui = coord_session
    long_note = "point the migration at " + ("x" * 200)
    item = sess._prepare_tool(
        _tc("tasks", {"action": "add", "title": "t", "note": long_note[:200]})
    )
    assert "error" not in item
    assert "chars omitted]" in item["preview"], (
        "an over-budget note must carry honest_truncate's marker, not a silent slice"
    )


def test_tasks_preview_marks_truncated_title(coord_session):
    """All four preview/header slices in _prepare_tasks use the same
    honest marker — converting only the note ones would leave the
    function internally inconsistent."""
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("tasks", {"action": "add", "title": "t" * 200}))
    assert "error" not in item
    assert "chars omitted]" in item["header"]


def test_tasks_update_whitespace_note_previews_as_clear(coord_session):
    """Preview/execute divergence: a whitespace-only note is truthy
    before the strip, so it previewed as a note being SET while
    tasks_update stripped it to "" and took the CLEAR branch — the
    operator approved "set a note" and the tool deleted one."""
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("tasks", {"action": "update", "task_id": "tsk_1", "note": "   "}))
    assert "error" not in item
    # Stripped once at prepare, so preview, projection and execute agree.
    assert item["note"] == ""
    assert "note=-" in item["preview"]


def test_tasks_update_strips_whitespace_only_string_fields(coord_session):
    """A whitespace-only title/status/child_ws_id is ordinary spaces,
    not steering codepoints.  The update branch strips every string
    field once, like the note, so the ``[unrenderable: N chars]``
    marker's trigger stays the genuinely-invisible class only — spaces
    must never be reported to the operator as control/zero-width/bidi
    text — and a whitespace-only ``child_ws_id`` previews as the
    explicit clear (``-``) it now performs."""
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(
        _tc(
            "tasks",
            {
                "action": "update",
                "task_id": "tsk_1",
                "title": "   ",
                "status": "   ",
                "child_ws_id": "   ",
            },
        )
    )
    assert "error" not in item
    assert "unrenderable" not in item["preview"]
    assert "child_ws_id=-" in item["preview"]
    # The stripped values are what execute hands the write path — the
    # operator ruled on the value that runs.
    assert item["title"] == ""
    assert item["status"] == ""
    assert item["child_ws_id"] == ""


def test_tasks_prepare_rejects_unrenderable_note_before_approval(coord_session):
    """The sanitise-to-empty sibling of the whitespace divergence above,
    in reverse: an update note made only of stripped characters previewed
    as ``note=-`` (the explicit-CLEAR marker) while execute stored the
    raw payload — the operator approved a clear and got a SET.  Rejected
    at prepare now (the write path rejects authoritatively too), so the
    approval card can never carry the lie."""
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(
        _tc("tasks", {"action": "update", "task_id": "tsk_1", "note": chr(0x200B) * 3})
    )
    assert "error" in item
    assert "no renderable characters" in item["error"]
    assert "retry" in item["error"]  # reject WITH hint


def test_tasks_prepare_rejects_unrenderable_add_title_and_note(coord_session):
    """The add-branch siblings: an unrenderable title creates a task no
    operator surface can name; an unrenderable note was silently OMITTED
    from the preview while storing verbatim.  Both fixtures are the
    genuinely-invisible class — brackets render now, so they are not
    part of it."""
    sess, _coord, _ui = coord_session
    t = sess._prepare_tool(_tc("tasks", {"action": "add", "title": chr(0x200B) * 2}))
    assert "error" in t and "no renderable characters" in t["error"]
    n = sess._prepare_tool(_tc("tasks", {"action": "add", "title": "ok", "note": chr(0x202E)}))
    assert "error" in n and "no renderable characters" in n["error"]


def test_prepare_rejects_over_cap_fields_before_approval(coord_session):
    """THE OPERATOR-SPARING PROPERTY.  An over-cap title or note is a tool
    error at prepare, not an approval card: the write path refuses it, so
    without this gate the operator spent a decision on a mutation that
    could never land — the same class the adjacent unrenderable reject
    exists to prevent.  ``needs_approval`` False and no ``execute`` is
    what "no approval item" means at this layer."""
    sess, _coord, _ui = coord_session
    for args, field in (
        ({"action": "add", "title": "x" * (TASK_TITLE_MAX + 1)}, "title"),
        ({"action": "add", "title": "ok", "note": "x" * (TASK_NOTE_MAX + 1)}, "note"),
        (
            {"action": "update", "task_id": "tsk_1", "title": "x" * (TASK_TITLE_MAX + 1)},
            "title",
        ),
        (
            {"action": "update", "task_id": "tsk_1", "note": "x" * (TASK_NOTE_MAX + 1)},
            "note",
        ),
        # Doubly over cap on an update: the write path checks the note
        # before the row loop and the title inside it, so both layers must
        # name the NOTE here.  Title-first at prepare would send the model
        # to fix the wrong field.
        (
            {
                "action": "update",
                "task_id": "tsk_1",
                "title": "x" * (TASK_TITLE_MAX + 1),
                "note": "x" * (TASK_NOTE_MAX + 1),
            },
            "note",
        ),
    ):
        item = sess._prepare_tool(_tc("tasks", args))
        assert "error" in item, args
        assert f"{field} too long" in item["error"], (args, item["error"])
        assert "Shorten and retry" in item["error"]  # reject WITH hint
        assert item["needs_approval"] is False
        assert "execute" not in item, "an unlandable call must not reach the approval surface"


def _write_path_client(tmp_path, name="oracle.db"):
    """A real ``CoordinatorClient`` over a real SQLite backend.

    The write path is the authoritative half of every prepare-versus-write
    agreement test below, so it has to be the real one — a MagicMock would
    accept whatever prepare passed it and agree with itself.
    """
    import httpx

    from turnstone.console.coordinator_client import CoordinatorClient
    from turnstone.core.child_event_bus import ChildEventBus

    storage = SQLiteBackend(str(tmp_path / name))
    storage.register_workstream("coord-1", kind="coordinator", user_id="user-1")
    return CoordinatorClient(
        console_base_url="http://x",
        storage=storage,
        token_factory=lambda: "t",
        coord_ws_id="coord-1",
        user_id="user-1",
        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        child_event_bus=ChildEventBus(),
    )


def test_prepare_and_write_path_agree_on_renderability_and_length(coord_session, tmp_path):
    """ONE ORACLE, for both admission rules.  ``_prepare_tasks`` carries
    an early copy of the unrenderable reject AND of the length caps;
    ``tasks_add``/``tasks_update`` carry the authoritative ones.  If the
    two measure with different sanitisers or different caps, one refuses
    what the other accepts: either an approval card for a call that
    cannot land, or a refusal at prepare that the write path would have
    allowed — a rejection no input can reach.  Both mutation branches are
    checked, because switching ``add`` alone just relocates the
    divergence to ``update``."""
    sess, _coord, _ui = coord_session
    client = _write_path_client(tmp_path)
    seed = client.tasks_add("coord-1", title="seed")
    assert "error" not in seed

    # Brackets render, so they are storable; only the genuinely-invisible
    # class is refused — and both copies must say so identically.
    for value, renders in (("<>", True), ("hold p99 <200ms", True), (chr(0x200B) * 2, False)):
        prepared_add = sess._prepare_tool(_tc("tasks", {"action": "add", "title": value}))
        prepared_upd = sess._prepare_tool(
            _tc("tasks", {"action": "update", "task_id": seed["id"], "title": value})
        )
        written_add = client.tasks_add("coord-1", title=value)
        written_upd = client.tasks_update("coord-1", task_id=seed["id"], title=value)
        verdicts = [
            "error" not in prepared_add,
            "error" not in prepared_upd,
            "error" not in written_add,
            "error" not in written_upd,
        ]
        assert verdicts == [renders] * 4

    # The same property for the caps.  At cap stores, one over is
    # refused, and leading whitespace does not count — both layers
    # measure AFTER the strip, so a prepare copy that measured the raw
    # argument would refuse a title the write path stores happily.
    for field, cap in (("title", TASK_TITLE_MAX), ("note", TASK_NOTE_MAX)):
        for value, fits in (
            ("x" * cap, True),
            ("x" * (cap + 1), False),
            (" " * 8 + "x" * cap, True),
        ):
            title = value if field == "title" else "ok"
            note = value if field == "note" else ""
            prepared_add = sess._prepare_tool(
                _tc("tasks", {"action": "add", "title": title, "note": note})
            )
            prepared_upd = sess._prepare_tool(
                _tc("tasks", {"action": "update", "task_id": seed["id"], field: value})
            )
            written_add = client.tasks_add("coord-1", title=title, note=note)
            written_upd = client.tasks_update("coord-1", task_id=seed["id"], **{field: value})
            verdicts = [
                "error" not in prepared_add,
                "error" not in prepared_upd,
                "error" not in written_add,
                "error" not in written_upd,
            ]
            assert verdicts == [fits] * 4, (field, len(value), fits)


def test_too_long_wins_over_unrenderable_at_both_layers(coord_session, tmp_path):
    """MASKING ORDER.  Length is measured before renderability at every
    gate, so a long run of zero-widths hears "too long" rather than
    "unrenderable".  The write path documents that order; the prepare
    copy has to keep it, or one value draws two different hints depending
    on which layer the model happens to reach first."""
    sess, _coord, _ui = coord_session
    client = _write_path_client(tmp_path, name="masking.db")
    seed = client.tasks_add("coord-1", title="seed")
    assert "error" not in seed

    # Zero-width space, built with chr() rather than a literal escape.
    payload = chr(0x200B) * 250
    assert len(payload) > TASK_TITLE_MAX

    errors = [
        sess._prepare_tool(_tc("tasks", {"action": "add", "title": payload}))["error"],
        sess._prepare_tool(
            _tc("tasks", {"action": "update", "task_id": seed["id"], "title": payload})
        )["error"],
        client.tasks_add("coord-1", title=payload)["error"],
        client.tasks_update("coord-1", task_id=seed["id"], title=payload)["error"],
    ]
    for message in errors:
        assert "too long" in message
        assert "no renderable characters" not in message


def test_unrenderable_errors_do_not_instruct_the_model_about_brackets(coord_session):
    """D5.  Both copies of the reject once told the model that operator
    displays strip angle brackets.  They no longer do — left in place,
    the hint talks a model out of a legitimate "<200ms" title, which is
    the same corruption the display fix removed, moved one layer up."""
    from turnstone.console.coordinator_client import _unrenderable_error

    sess, _coord, _ui = coord_session
    authoritative = _unrenderable_error("title", 3)["error"]
    early = sess._prepare_tool(_tc("tasks", {"action": "add", "title": chr(0x200B) * 3}))["error"]
    for message in (authoritative, early):
        assert "angle bracket" not in message
        assert "control/zero-width/bidi" in message


def test_prepare_and_write_path_refuse_in_the_same_words(coord_session):
    """ONE SENTENCE, not two that happen to agree.

    Both refusals are returned to the MODEL, and the prepare copy is the
    one it always hits first — so the layer documented as authoritative
    is the one it never sees.  The two were restated as separate literals
    differing only by the action prefix, narrowed in lockstep by hand,
    with nothing pinning them: an edit to one alone passed CI while the
    model was told two different reasons for the same refusal depending
    on which layer it reached.  The neighbouring tests cannot catch that
    — they assert substrings ("no renderable characters", "too long"),
    which survive any narrowing that keeps the phrase.

    Both layers now read one producer, so the one-sided edit is not
    merely detectable but unavailable.  This asserts the relation that
    makes re-inlining a literal fail: equality modulo the action prefix,
    for both refusals, both fields and both mutation branches — the
    prefix being the only thing prepare is entitled to add, so a batched
    update names the row that failed.  The comparison runs through
    ``_coord_tool_error`` rather than restating its ``"Error: "``
    envelope, so the envelope is not a third literal to keep in step,
    and the whole item is compared: a refusal that grew an ``execute``
    or an approval card would fail here too.
    """
    from turnstone.console.coordinator_client import _too_long_error, _unrenderable_error

    sess, _coord, _ui = coord_session
    blank = chr(0x200B) * 3  # zero-width space, built with chr()
    over_title = "x" * (TASK_TITLE_MAX + 1)
    over_note = "x" * (TASK_NOTE_MAX + 1)
    upd = {"action": "update", "task_id": "tsk_1"}
    for args, action, authoritative in (
        ({"action": "add", "title": blank}, "add", _unrenderable_error("title", len(blank))),
        (
            {"action": "add", "title": "ok", "note": blank},
            "add",
            _unrenderable_error("note", len(blank)),
        ),
        (upd | {"title": blank}, "update", _unrenderable_error("title", len(blank))),
        (upd | {"note": blank}, "update", _unrenderable_error("note", len(blank))),
        (
            {"action": "add", "title": over_title},
            "add",
            _too_long_error("title", len(over_title), TASK_TITLE_MAX),
        ),
        (
            {"action": "add", "title": "ok", "note": over_note},
            "add",
            _too_long_error("note", len(over_note), TASK_NOTE_MAX),
        ),
        (
            upd | {"title": over_title},
            "update",
            _too_long_error("title", len(over_title), TASK_TITLE_MAX),
        ),
        (
            upd | {"note": over_note},
            "update",
            _too_long_error("note", len(over_note), TASK_NOTE_MAX),
        ),
    ):
        item = sess._prepare_tool(_tc("tasks", args))
        assert "error" in item, args
        expected = sess._coord_tool_error("call-1", "tasks", f"{action}: {authoritative['error']}")
        assert item == expected, (args, item["error"])


def test_tasks_update_header_sanitizes_and_truncates_task_id(coord_session):
    """Every model-controlled string on the approval surface renders via
    ``_pf`` — a newline in ``task_id`` forges an extra header line in the
    channel formatter (which truncates preview but NOT header) and in
    buildConvCmd's line-classified view.  ``item["task_id"]`` stays raw:
    it feeds ``tasks_update`` by exact match, so sanitising the stored
    value would turn every mutation into "task not found"."""
    sess, _coord, _ui = coord_session
    raw_id = "tsk_a" + chr(10) + "FAKE APPROVAL LINE"
    item = sess._prepare_tool(
        _tc("tasks", {"action": "update", "task_id": raw_id, "status": "done"})
    )
    assert "error" not in item
    assert chr(10) not in item["header"]
    assert item["task_id"] == raw_id
    long_item = sess._prepare_tool(
        _tc("tasks", {"action": "update", "task_id": "t" * 200, "status": "done"})
    )
    assert "chars omitted]" in long_item["header"]


def test_tasks_remove_header_sanitizes_task_id(coord_session):
    sess, _coord, _ui = coord_session
    raw_id = "tsk" + chr(10) + "x"
    item = sess._prepare_tool(_tc("tasks", {"action": "remove", "task_id": raw_id}))
    assert "error" not in item
    assert chr(10) not in item["header"]
    assert item["task_id"] == raw_id


def test_tasks_preview_sanitizes_status_and_child_ws_id(coord_session):
    """``status``/``child_ws_id`` rode the preview raw — a bidi override
    there reorders the decision the operator reads, which is the exact
    attack the title/note sanitisation ruling was added to stop."""
    sess, _coord, _ui = coord_session
    raw_status = "done" + chr(0x202E)
    item = sess._prepare_tool(
        _tc(
            "tasks",
            {
                "action": "update",
                "task_id": "tsk_1",
                "status": raw_status,
                "child_ws_id": "ws-1" + chr(10) + "x",
            },
        )
    )
    assert "error" not in item
    assert chr(0x202E) not in item["preview"]
    assert chr(10) not in item["preview"]
    # Raw values still flow to execute untouched.
    assert item["status"] == raw_status


def test_tasks_preview_marks_unrenderable_child_ws_id(coord_session):
    """``child_ws_id``'s ``-`` means "explicit clear".  A value that
    sanitises to empty must render the unrenderable marker, NOT ``-`` —
    otherwise the operator approves a clear while execute stores the
    payload: the note divergence exported to a new field by the very
    sanitisation that fixed it there.  The marker's trigger is now the
    genuinely-invisible class only; ``"<>"`` renders (test below)."""
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(
        _tc(
            "tasks",
            {"action": "update", "task_id": "tsk_1", "child_ws_id": chr(0x200B) + chr(0x202E)},
        )
    )
    assert "error" not in item
    assert "child_ws_id=[unrenderable: 2 chars]" in item["preview"]
    assert item["child_ws_id"] == chr(0x200B) + chr(0x202E)


def test_tasks_preview_shows_bracket_child_ws_id_verbatim(coord_session):
    """The approval preview is an operator surface: it must show the
    text that will be stored.  Deleting angle brackets here asked the
    operator to approve a string the tool would not write."""
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(
        _tc("tasks", {"action": "update", "task_id": "tsk_1", "child_ws_id": "<>"})
    )
    assert "error" not in item
    assert "child_ws_id=<>" in item["preview"]
    assert item["child_ws_id"] == "<>"


def test_tasks_preview_shows_bracketed_title_verbatim(coord_session):
    """The reported bug at the approval surface: a title of
    "cut p99 latency to <200ms" previewed as "...to 200ms" — the
    operator approved the opposite constraint."""
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(_tc("tasks", {"action": "add", "title": "cut p99 latency to <200ms"}))
    assert "error" not in item
    assert "cut p99 latency to <200ms" in item["header"]


def test_tasks_reorder_preview_sanitizes_ids(coord_session):
    sess, _coord, _ui = coord_session
    raw_ids = ["a" + chr(10) + "b", "c"]
    item = sess._prepare_tool(_tc("tasks", {"action": "reorder", "task_ids": raw_ids}))
    assert "error" not in item
    assert chr(10) not in item["preview"]
    assert item["task_ids"] == raw_ids


def test_prepare_tasks_imports_honest_truncate_once():
    """ONE deferred import above the action branches — a second copy per
    branch is how the two drift (the old per-branch comment also claimed
    a judge import cycle that does not exist at HEAD)."""
    import inspect

    from turnstone.core.session import ChatSession

    src = inspect.getsource(ChatSession._prepare_tasks)
    assert src.count("from turnstone.core.judge import honest_truncate") == 1


def test_tasks_update_none_note_stays_unchanged(coord_session):
    """The strip must preserve the None/"" distinction: None means
    "unchanged", "" means "clear"."""
    sess, _coord, _ui = coord_session
    item = sess._prepare_tool(
        _tc("tasks", {"action": "update", "task_id": "tsk_1", "status": "done"})
    )
    assert "error" not in item
    assert item["note"] is None
    assert "note=" not in item["preview"]
