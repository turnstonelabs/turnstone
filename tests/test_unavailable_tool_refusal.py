"""The refusal of a tool call that dispatch cannot run (#1287).

A session's catalog can change mid-conversation: an MCP server drops a tool or goes away, or
another user sends on a shared workstream and the catalog follows the sender. The name the model
calls may be one it saw earlier, so the refusal says the tool is not available now, and lists only
the tools the request offers, leaving deferred tools to tool search.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests._session_helpers import make_result, make_session
from turnstone.core.personas import PersonaSnapshot
from turnstone.core.providers._protocol import ModelCapabilities
from turnstone.core.trajectory import Role, Turn

GONE = "mcp__gone__lookup"


def _tool(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"The {name} tool.",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _call(name: str = GONE) -> dict[str, Any]:
    return {"id": "call-gone", "type": "function", "function": {"name": name, "arguments": "{}"}}


def _mcp(*names: str) -> MagicMock:
    mcp = MagicMock()
    mcp.get_tools.return_value = [_tool(name) for name in names]
    # Dispatch finds no MCP tool by the called name: it left the catalog.
    mcp.is_mcp_tool.return_value = False
    mcp.resource_count_for_user.return_value = 0
    mcp.prompt_count_for_user.return_value = 0
    return mcp


def _listed(error: str) -> list[str]:
    return error.split("Other tools offered now: ", 1)[1].split(".", 1)[0].split(", ")


def test_refusal_says_the_tool_is_gone_and_lists_the_offer(tmp_db) -> None:
    ui = MagicMock()
    session = make_session(ui=ui, mcp_client=_mcp("mcp__docs__read"), tool_search="off")

    item = session._prepare_tool(_call())

    error = item["error"]
    # A single-user workstream: another user cannot be the cause, so it is not named.
    assert error.startswith(
        f"Tool '{GONE}' is not available now. It may have been removed since it was offered, "
        "or its name may be misspelled."
    )
    assert error.endswith("To call one, use its name exactly as listed.")
    listed = _listed(error)
    assert "bash" in listed
    assert "mcp__docs__read" in listed
    # A coordinator tool has a preparer, but an interactive session does not offer it.
    assert "spawn_workstream" not in listed
    assert "tool search" not in error
    assert item["header"].endswith(f"{GONE}: not available")
    ui.on_error.assert_called_once_with(f"Model called a tool that is not available now: '{GONE}'")


def test_a_shared_workstream_names_another_user_as_a_cause(tmp_db) -> None:
    session = make_session(mcp_client=_mcp("mcp__docs__read"), tool_search="off")
    # Latched once a non-owner speaks; the catalog then follows the sender.
    session._shared_workstream = True

    error = session._prepare_tool(_call())["error"]

    assert error.startswith(
        f"Tool '{GONE}' is not available now. It may have been removed since it was offered, "
        "or be available only to another user of this workstream, or its name may be misspelled."
    )


def test_revoked_tools_are_not_offered_as_alternatives(tmp_db) -> None:
    session = make_session(mcp_client=_mcp("mcp__docs__read"), tool_search="off")
    # Revocation leaves the tool on the wire; dispatch refuses it.
    session.revoke_tools({"bash"})

    listed = _listed(session._prepare_tool(_call())["error"])

    assert "bash" not in listed
    assert "read_file" in listed


def test_an_offer_of_nothing_else_says_so(tmp_db) -> None:
    session = make_session()

    error = session._prepare_tool_for_principal(_call(), "user-a", offered=frozenset({GONE}))[
        "error"
    ]

    # An offered name is no misspelling, and with nothing else listed there is nothing to call.
    assert error == (
        f"Tool '{GONE}' is not available now. It may have been removed since it was offered. "
        "No other tools are offered now."
    )


def test_a_persona_refusal_lists_only_the_tools_its_set_shows(tmp_db) -> None:
    persona = PersonaSnapshot(
        name="guard", prompt="", tools=frozenset({"bash", "read_file"}), mcp=True, memory=True
    )
    session = make_session(mcp_client=_mcp("mcp__docs__read"), persona_snapshot=persona)

    error = session._prepare_tool(_call())["error"]

    assert "Other tools offered now: bash, read_file." in error
    assert "tool search" not in error


@pytest.mark.parametrize("native", [True, False], ids=["native", "client-side"])
def test_deferred_tools_are_left_to_tool_search(tmp_db, native: bool) -> None:
    session = make_session(mcp_client=_mcp("mcp__docs__read", "mcp__docs__write"), tool_search="on")
    assert session._tool_search is not None
    session._tool_search.expand_visible(["mcp__docs__read"])
    caps = ModelCapabilities(supports_tool_search=native)

    with patch.object(session, "_get_capabilities", return_value=caps):
        error = session._prepare_tool(_call())["error"]

    listed = _listed(error)
    # A discovered tool is offered outright; a deferred one is not listed.
    assert "mcp__docs__read" in listed
    assert "mcp__docs__write" not in listed
    # Client-side search is itself an offered tool; native search is the provider's.
    assert ("tool_search" in listed) is not native
    assert "More tools may be found with tool search." in error


def test_a_task_agent_is_shown_its_own_tools(tmp_db) -> None:
    session = make_session()
    contexts: list[list[Turn]] = []
    responses = [
        make_result("", tool_calls=[_call()], finish_reason="tool_calls"),
        make_result("done"),
    ]

    def plant(_lane: Any, turns: list[Turn], **_kwargs: Any) -> Any:
        contexts.append(list(turns))
        return responses.pop(0)

    with (
        patch.object(session, "_context_window_for_lane", return_value=1_000_000),
        patch("turnstone.core.session.model_turn", side_effect=plant),
    ):
        session._run_agent(
            [Turn.system("task identity"), Turn.user("look it up")],
            label="task",
            tools=[_tool("read_file"), _tool(GONE)],
            auto_tools={"read_file"},
            parent_call_id="task-parent",
            principal_id="user-a",
        )

    assert responses == []
    [result] = [turn for turn in contexts[1] if turn.role == Role.TOOL]
    assert f"Tool '{GONE}' is not available now." in result.text
    # The agent was offered that exact name, so it is no misspelling.
    assert "misspelled" not in result.text
    # The agent's list, not the session's: the session offers bash, the agent does not.
    assert "Other tools offered now: read_file." in result.text


def test_an_agents_offer_does_not_outlive_its_call(tmp_db) -> None:
    session = make_session()

    agent_item = session._prepare_tool_for_principal(
        _call(), "user-a", offered=frozenset({"read_file"})
    )
    session_item = session._prepare_tool(_call())

    assert "Other tools offered now: read_file." in agent_item["error"]
    assert "bash" in session_item["error"]


def test_deferred_names_survive_the_catalog_dropping_tool_search_mid_read(tmp_db) -> None:
    # The refusal and every request read the deferred names. An MCP catalog refresh on another
    # thread can drop the tool search manager at any point; here it lands right after the
    # manager's truth test, the window a free-threaded build (or a caller passing no caps) opens.
    session = make_session()

    class DroppedMidRead:
        def __bool__(self) -> bool:
            session._tool_search = None
            return True

        def get_deferred_tools(self) -> list[dict[str, Any]]:
            return [_tool("mcp__docs__write")]

    session._tool_search = DroppedMidRead()  # type: ignore[assignment]

    names = session._get_deferred_names(ModelCapabilities(supports_tool_search=True))

    assert names == frozenset({"mcp__docs__write"})
