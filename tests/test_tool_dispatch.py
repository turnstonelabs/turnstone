"""``tools.search_delivery = "dispatch"``: a fixed wire tool list and the ``call_tool`` dispatcher."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests._session_helpers import make_session
from turnstone.core.personas import PersonaSnapshot
from turnstone.core.providers._protocol import ModelCapabilities
from turnstone.core.tool_dispatch import (
    CALL_TOOL_NAME,
    call_tool_definition,
    repair_arguments,
    unwrap_call,
    validation_error,
)
from turnstone.core.tool_search import ToolSearchManager

LOGS = "mcp__logs__search"
DOCS = "mcp__docs__read"
LOGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {"type": "string"},
        "since_seconds": {"type": "integer", "minimum": 1},
        "follow": {"type": "boolean"},
        "ratio": {"type": "number"},
        "tags": {"type": "array", "items": {"type": "integer"}},
        "window": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
            "additionalProperties": False,
        },
    },
    "required": ["source", "since_seconds"],
    "additionalProperties": False,
}


def _tool(name: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"The {name} tool: search logs and read docs.",
            "parameters": schema or {"type": "object", "properties": {}},
        },
    }


def _mcp() -> MagicMock:
    mcp = MagicMock()
    mcp.get_tools.return_value = [_tool(LOGS, LOGS_SCHEMA), _tool(DOCS)]
    mcp.is_mcp_tool.side_effect = lambda name, user_id=None: name in (LOGS, DOCS)
    mcp.resource_count_for_user.return_value = 0
    mcp.prompt_count_for_user.return_value = 0
    return mcp


def _call(name: str, arguments: Any) -> dict[str, Any]:
    return {
        "id": "call-1",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _wire(session: Any, native: bool = False) -> list[dict[str, Any]]:
    caps = ModelCapabilities(supports_tool_search=native)
    with patch.object(session, "_get_capabilities", return_value=caps):
        return session._get_active_tools(caps) or []


def _names(tools: list[dict[str, Any]]) -> list[str]:
    return [tool["function"]["name"] for tool in tools]


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def test_unwrap_reads_the_declared_shape() -> None:
    assert unwrap_call({"name": f" {LOGS} ", "arguments": {"source": "fw"}}) == (
        LOGS,
        {"source": "fw"},
        "",
    )


@pytest.mark.parametrize(
    ("args", "inner"),
    [
        ({"name": LOGS, "arguments": '{"source": "fw"}'}, {"source": "fw"}),
        ({"name": LOGS, "source": "fw"}, {"source": "fw"}),
        ({"name": LOGS, "arguments": None}, {}),
        ({"name": LOGS, "arguments": ""}, {}),
    ],
    ids=["json-string", "flattened", "null", "empty-string"],
)
def test_unwrap_accepts_the_shapes_with_one_reading(args: Any, inner: Any) -> None:
    assert unwrap_call(args) == (LOGS, inner, "")


@pytest.mark.parametrize(
    "args",
    [{"arguments": {}}, {"name": "", "arguments": {}}, {"name": 7}, "text", None],
)
def test_unwrap_refuses_a_call_without_a_name(args: Any) -> None:
    name, inner, error = unwrap_call(args)
    assert (name, inner) == ("", None)
    assert error


@pytest.mark.parametrize("arguments", ["not json", "[1, 2]", 5, ["a"]])
def test_unwrap_refuses_arguments_that_are_not_an_object(arguments: Any) -> None:
    name, inner, error = unwrap_call({"name": LOGS, "arguments": arguments})
    assert (name, inner) == (LOGS, None)
    assert "must be a JSON object" in error


def test_repair_converts_strings_to_the_declared_type() -> None:
    notes: list[str] = []
    repaired = repair_arguments(
        LOGS_SCHEMA,
        {
            "source": "3600",
            "since_seconds": "3600",
            "follow": "False",
            "ratio": "0.5",
            "tags": '["1", 2]',
            "window": {"limit": "20"},
        },
        notes,
    )
    assert repaired == {
        "source": "3600",  # declared a string: untouched
        "since_seconds": 3600,
        "follow": False,
        "ratio": 0.5,
        "tags": [1, 2],
        "window": {"limit": 20},
    }
    assert "since_seconds: string to integer" in notes
    assert "window/limit: string to integer" in notes
    assert "tags/0: string to integer" in notes


def test_repair_leaves_what_it_cannot_read_for_validation() -> None:
    notes: list[str] = []
    args = {"source": "fw", "since_seconds": "an hour", "follow": "yes", "tags": "1,2"}
    assert repair_arguments(LOGS_SCHEMA, args, notes) == args
    assert notes == []
    assert "since_seconds" in validation_error(LOGS, LOGS_SCHEMA, args)


def test_validation_names_the_field_and_shows_the_schema() -> None:
    error = validation_error(LOGS, LOGS_SCHEMA, {"source": "fw", "since_seconds": 0})
    assert error.startswith(f"Invalid arguments for {LOGS}: at since_seconds:")
    assert json.dumps(LOGS_SCHEMA, separators=(",", ":")) in error
    assert validation_error(LOGS, LOGS_SCHEMA, {"source": "fw", "since_seconds": 60}) == ""


@pytest.mark.parametrize("schema", [None, {}, {"type": "no-such-type"}, {"properties": 5}])
def test_a_schema_the_validator_rejects_is_not_the_models_error(schema: Any) -> None:
    assert validation_error(LOGS, schema, {"anything": 1}) == ""


# ---------------------------------------------------------------------------
# The search manager
# ---------------------------------------------------------------------------


def _manager(delivery: str) -> ToolSearchManager:
    return ToolSearchManager(
        [_tool("bash"), _tool(LOGS, LOGS_SCHEMA), _tool(DOCS)],
        always_on_names={"bash"},
        delivery=delivery,
    )


def test_expand_delivery_is_unchanged() -> None:
    manager = _manager("expand")
    found = manager.search("logs")
    manager.expand_visible([LOGS])
    assert LOGS in _names(manager.get_visible_tools())
    assert (
        "Available tool servers:" in manager.get_search_tool_definition()["function"]["description"]
    )
    text = manager.format_search_results(found)
    assert text.endswith("These tools are now available for use.")
    assert "arguments schema" not in text
    # A tool already loaded is not returned again.
    assert LOGS not in _names(manager.search("logs"))


def test_dispatch_results_carry_schemas_and_the_list_does_not_grow() -> None:
    manager = _manager("dispatch")
    definition = json.dumps(manager.get_search_tool_definition())
    found = manager.search("logs")
    manager.expand_visible(_names(found))

    assert _names(manager.get_visible_tools()) == ["bash"]
    assert json.dumps(manager.get_search_tool_definition()) == definition
    assert "tool servers" not in definition.lower()
    text = manager.format_search_results(found)
    assert "arguments schema: " + json.dumps(LOGS_SCHEMA, separators=(",", ":")) in text
    assert "call call_tool with its exact name" in text
    assert "Tool servers:" in text
    # The schema lives in the transcript only, so a later search returns the tool again.
    assert LOGS in _names(manager.search("logs"))


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------


def test_off_by_default_the_wire_list_expands_and_call_tool_is_not_a_tool(tmp_db) -> None:
    session = make_session(mcp_client=_mcp(), tool_search="on")
    assert CALL_TOOL_NAME not in _names(_wire(session))
    session._exec_tool_search({"call_id": "s", "query": "logs"})
    assert LOGS in _names(_wire(session))

    item = session._prepare_tool(_call(CALL_TOOL_NAME, {"name": LOGS, "arguments": {}}))
    assert "is not available now" in item["error"]


@pytest.mark.parametrize("native", [False, True], ids=["client-side", "native-capable"])
def test_dispatch_keeps_the_wire_list_identical_across_a_search(tmp_db, native: bool) -> None:
    session = make_session(mcp_client=_mcp(), tool_search="on", tool_search_delivery="dispatch")
    caps = ModelCapabilities(supports_tool_search=native)

    before = json.dumps(_wire(session, native))
    _, output = session._exec_tool_search({"call_id": "s", "query": "logs"})
    after = json.dumps(_wire(session, native))

    assert before == after
    names = _names(json.loads(before))
    assert names[-2:] == ["tool_search", CALL_TOOL_NAME]
    assert LOGS not in names and DOCS not in names
    assert json.loads(before)[-1] == call_tool_definition()
    assert "arguments schema" in output
    # Nothing is handed to a provider's own deferred loading.
    assert session._get_deferred_names(caps) is None


def test_call_tool_prepares_the_inner_tool_under_its_own_name(tmp_db) -> None:
    session = make_session(mcp_client=_mcp(), tool_search="on", tool_search_delivery="dispatch")
    arguments = {"source": "fw-edge", "since_seconds": "3600", "follow": "true"}

    item = session._prepare_tool(_call(CALL_TOOL_NAME, {"name": LOGS, "arguments": arguments}))

    assert "error" not in item
    assert item["call_id"] == "call-1"
    assert item["func_name"] == LOGS
    assert item["approval_label"] == LOGS
    assert item["mcp_func_name"] == LOGS
    assert item["needs_approval"] is True
    assert item["header"].endswith("mcp:logs/search")
    # Repaired against the schema before anything downstream sees them.
    assert item["mcp_args"] == {"source": "fw-edge", "since_seconds": 3600, "follow": True}


def test_invalid_arguments_never_reach_approval_and_a_repeat_is_called_out(tmp_db) -> None:
    session = make_session(mcp_client=_mcp(), tool_search="on", tool_search_delivery="dispatch")
    call = _call(CALL_TOOL_NAME, {"name": LOGS, "arguments": {"source": "fw-edge"}})

    first = session._prepare_tool(call)
    assert first["func_name"] == LOGS
    assert first["needs_approval"] is False
    assert "execute" not in first
    assert "'since_seconds' is a required property" in first["error"]
    assert "failed" not in first["error"]

    second = session._prepare_tool(call)
    assert "has now failed 2 times with the same error" in second["error"]

    changed = _call(CALL_TOOL_NAME, {"name": LOGS, "arguments": {"source": "x", "extra": 1}})
    assert "has now failed" not in session._prepare_tool(changed)["error"]


@pytest.mark.parametrize("name", ["bash", "write_file", "tool_search", "mcp__gone__lookup"])
def test_call_tool_reaches_only_searchable_mcp_tools(tmp_db, name: str) -> None:
    session = make_session(mcp_client=_mcp(), tool_search="on", tool_search_delivery="dispatch")

    item = session._prepare_tool(_call(CALL_TOOL_NAME, {"name": name, "arguments": {}}))

    assert item["needs_approval"] is False
    assert "execute" not in item
    assert "cannot be called through call_tool" in item["error"]


def test_a_malformed_call_is_refused_with_the_shape(tmp_db) -> None:
    session = make_session(mcp_client=_mcp(), tool_search="on", tool_search_delivery="dispatch")

    item = session._prepare_tool(_call(CALL_TOOL_NAME, {"arguments": {"source": "fw"}}))

    assert item["func_name"] == CALL_TOOL_NAME
    assert "needs `name`" in item["error"]


def test_a_revoked_inner_tool_is_refused(tmp_db) -> None:
    session = make_session(mcp_client=_mcp(), tool_search="on", tool_search_delivery="dispatch")
    session.revoke_tools({DOCS})

    item = session._prepare_tool(_call(CALL_TOOL_NAME, {"name": DOCS, "arguments": {}}))

    assert "revoked" in item["error"]
    assert "execute" not in item


def test_under_a_persona_set_call_tool_follows_tool_search_and_discovery(tmp_db) -> None:
    persona = PersonaSnapshot(
        name="guard", prompt="", tools=frozenset({"tool_search", "memory"}), mcp=True, memory=True
    )
    session = make_session(
        mcp_client=_mcp(), persona_snapshot=persona, tool_search_delivery="dispatch"
    )
    assert _names(_wire(session))[-2:] == ["tool_search", CALL_TOOL_NAME]
    call = _call(CALL_TOOL_NAME, {"name": DOCS, "arguments": {}})

    # Not yet returned by a search: the persona set does not allow it.
    assert "cannot be called through call_tool" in session._prepare_tool(call)["error"]

    with patch.object(session, "_init_system_messages") as recompose:
        session._exec_tool_search({"call_id": "s", "query": "docs"})
    recompose.assert_not_called()
    assert session._prepare_tool(call)["func_name"] == DOCS
    assert "error" not in session._prepare_tool(call)


def test_a_hard_persona_set_has_neither_tool(tmp_db) -> None:
    persona = PersonaSnapshot(
        name="guard", prompt="", tools=frozenset({"memory"}), mcp=True, memory=True
    )
    session = make_session(
        mcp_client=_mcp(), persona_snapshot=persona, tool_search_delivery="dispatch"
    )

    names = _names(_wire(session))
    assert "tool_search" not in names and CALL_TOOL_NAME not in names
    item = session._prepare_tool(_call(CALL_TOOL_NAME, {"name": DOCS, "arguments": {}}))
    assert "is not available now" in item["error"]


def test_a_persona_opts_in_by_listing_call_tool(tmp_db) -> None:
    """With the deployment on expand, a persona that lists call_tool gets dispatch."""
    persona = PersonaSnapshot(
        name="meta",
        prompt="",
        tools=frozenset({"tool_search", CALL_TOOL_NAME, "memory"}),
        mcp=True,
        memory=True,
    )
    session = make_session(mcp_client=_mcp(), persona_snapshot=persona)

    before = json.dumps(_wire(session))
    session._exec_tool_search({"call_id": "s", "query": "logs"})
    assert json.dumps(_wire(session)) == before
    assert _names(json.loads(before))[-2:] == ["tool_search", CALL_TOOL_NAME]
    item = session._prepare_tool(
        _call(CALL_TOOL_NAME, {"name": LOGS, "arguments": {"source": "fw", "since_seconds": 5}})
    )
    assert item["func_name"] == LOGS and "error" not in item


def test_a_persona_without_call_tool_follows_the_deployment(tmp_db) -> None:
    persona = PersonaSnapshot(
        name="plain", prompt="", tools=frozenset({"tool_search", "memory"}), mcp=True, memory=True
    )
    session = make_session(mcp_client=_mcp(), persona_snapshot=persona)

    assert CALL_TOOL_NAME not in _names(_wire(session))
    session._exec_tool_search({"call_id": "s", "query": "logs"})
    assert LOGS in _names(_wire(session))


def test_call_tool_without_tool_search_is_still_a_hard_set(tmp_db) -> None:
    persona = PersonaSnapshot(
        name="odd", prompt="", tools=frozenset({CALL_TOOL_NAME, "memory"}), mcp=True, memory=True
    )
    session = make_session(mcp_client=_mcp(), persona_snapshot=persona)

    names = _names(_wire(session))
    assert "tool_search" not in names and CALL_TOOL_NAME not in names
