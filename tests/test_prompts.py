"""Tests for the system message composition harness (turnstone.prompts)."""

from __future__ import annotations

import pytest

from turnstone.prompts import (
    ClientType,
    SessionContext,
    compose_system_message,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_CTX = SessionContext(
    current_datetime="2026-03-31T14:22:00-07:00",
    timezone="PDT",
    username="sarah.chen",
)

_ALL_TOOLS: frozenset[str] = frozenset({"web_search", "read_file", "bash"})
_NO_TOOLS: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# 1. Assembly smoke test per client type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ct", [ClientType.WEB, ClientType.CLI, ClientType.CHAT])
def test_smoke_all_client_types(ct: ClientType) -> None:
    result = compose_system_message(
        client_type=ct,
        context=_VALID_CTX,
        available_tools=_ALL_TOOLS,
    )
    # BASE content present
    assert "resident engineer" in result
    # CONTEXT present
    assert "sarah.chen" in result
    assert "2026-03-31" in result


def test_smoke_web_has_mermaid() -> None:
    result = compose_system_message(
        client_type=ClientType.WEB,
        context=_VALID_CTX,
        available_tools=_ALL_TOOLS,
    )
    assert "Mermaid" in result
    assert "KaTeX" in result


def test_smoke_cli_no_mermaid() -> None:
    result = compose_system_message(
        client_type=ClientType.CLI,
        context=_VALID_CTX,
        available_tools=_ALL_TOOLS,
    )
    assert "Mermaid" not in result or "Do not use" in result


def test_smoke_chat_no_tables() -> None:
    result = compose_system_message(
        client_type=ClientType.CHAT,
        context=_VALID_CTX,
        available_tools=_ALL_TOOLS,
    )
    assert "Do not use them" in result


# ---------------------------------------------------------------------------
# 2. Required field validation
# ---------------------------------------------------------------------------


def test_missing_current_datetime() -> None:
    ctx = SessionContext(current_datetime="", timezone="PDT", username="alice")
    with pytest.raises(ValueError, match="current_datetime"):
        compose_system_message(ClientType.CLI, ctx, _NO_TOOLS)


def test_missing_timezone() -> None:
    ctx = SessionContext(
        current_datetime="2026-03-31T14:22:00-07:00",
        timezone="",
        username="alice",
    )
    with pytest.raises(ValueError, match="timezone"):
        compose_system_message(ClientType.CLI, ctx, _NO_TOOLS)


def test_missing_username() -> None:
    ctx = SessionContext(
        current_datetime="2026-03-31T14:22:00-07:00",
        timezone="PDT",
        username="",
    )
    with pytest.raises(ValueError, match="username"):
        compose_system_message(ClientType.CLI, ctx, _NO_TOOLS)


# ---------------------------------------------------------------------------
# 3. Unknown client type rejection
# ---------------------------------------------------------------------------


def test_unknown_client_type() -> None:
    with pytest.raises(ValueError, match="Unknown client_type"):
        compose_system_message("tablet", _VALID_CTX, _NO_TOOLS)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 4. Missing policy file
# ---------------------------------------------------------------------------


def test_missing_policy_file() -> None:
    with pytest.raises(FileNotFoundError, match="nonexistent"):
        compose_system_message(
            ClientType.CLI,
            _VALID_CTX,
            _ALL_TOOLS,
            policies=["nonexistent"],
        )


# ---------------------------------------------------------------------------
# 5. Module isolation — BASE must be environment-agnostic
# ---------------------------------------------------------------------------


def test_base_module_isolation() -> None:
    from turnstone.prompts import _load

    base = _load("base.md")
    for forbidden in ("Mermaid", "KaTeX", "terminal", "monospace", "Slack", "Discord"):
        assert forbidden not in base, f"BASE must not contain '{forbidden}'"


# ---------------------------------------------------------------------------
# 6. ENV mutual exclusion
# ---------------------------------------------------------------------------


def test_env_mutual_exclusion() -> None:
    web = compose_system_message(ClientType.WEB, _VALID_CTX, _ALL_TOOLS)
    # Web should have Mermaid.js but not "No diagram rendering" from CLI
    assert "Mermaid" in web
    assert "No diagram rendering" not in web


# ---------------------------------------------------------------------------
# 7. Policy tool gating — file-based (negative case)
# ---------------------------------------------------------------------------


def test_file_policy_gated_out() -> None:
    """web_search policy excluded when web_search tool is not available."""
    result = compose_system_message(
        ClientType.CLI,
        _VALID_CTX,
        frozenset({"read_file"}),  # no web_search
        policies=["web_search"],
    )
    assert "Web Search Policy" not in result


# ---------------------------------------------------------------------------
# 8. Policy tool gating — positive case
# ---------------------------------------------------------------------------


def test_file_policy_gated_in() -> None:
    """web_search policy included when web_search tool is available."""
    result = compose_system_message(
        ClientType.CLI,
        _VALID_CTX,
        frozenset({"web_search"}),
        policies=["web_search"],
    )
    assert "Web Search Policy" in result


# ---------------------------------------------------------------------------
# 9. Unconditional policy not gated
# ---------------------------------------------------------------------------


def test_unconditional_policy() -> None:
    """A DB policy with no tool_gate is always included."""
    db = [
        {
            "name": "custom_rule",
            "content": "## Custom Rule\nAlways be polite.",
            "tool_gate": "",
            "priority": 0,
            "enabled": True,
        }
    ]
    result = compose_system_message(
        ClientType.CLI,
        _VALID_CTX,
        _NO_TOOLS,
        db_policies=db,
    )
    assert "Always be polite" in result


# ---------------------------------------------------------------------------
# 10. DB policy override
# ---------------------------------------------------------------------------


def test_db_policy_overrides_file() -> None:
    """DB policy with same name as file policy wins."""
    db = [
        {
            "name": "web_search",
            "content": "## DB Web Search Override\nCustom content.",
            "tool_gate": "web_search",
            "priority": 0,
            "enabled": True,
        }
    ]
    result = compose_system_message(
        ClientType.CLI,
        _VALID_CTX,
        _ALL_TOOLS,
        policies=["web_search"],
        db_policies=db,
    )
    assert "DB Web Search Override" in result
    assert "Use local tools" not in result  # original file content


# ---------------------------------------------------------------------------
# 11. DB-only policy
# ---------------------------------------------------------------------------


def test_db_only_policy() -> None:
    """DB policy not in explicit list is still included."""
    db = [
        {
            "name": "extra_rule",
            "content": "## Extra\nDo not share secrets.",
            "tool_gate": "",
            "priority": 5,
            "enabled": True,
        }
    ]
    result = compose_system_message(
        ClientType.CLI,
        _VALID_CTX,
        _NO_TOOLS,
        db_policies=db,
    )
    assert "Do not share secrets" in result


# ---------------------------------------------------------------------------
# 12. Disabled DB policy
# ---------------------------------------------------------------------------


def test_disabled_db_policy() -> None:
    """DB policy with enabled=False is skipped."""
    db = [
        {
            "name": "disabled_rule",
            "content": "## Disabled\nThis should not appear.",
            "tool_gate": "",
            "priority": 0,
            "enabled": False,
        }
    ]
    result = compose_system_message(
        ClientType.CLI,
        _VALID_CTX,
        _NO_TOOLS,
        db_policies=db,
    )
    assert "This should not appear" not in result


# ---------------------------------------------------------------------------
# 13. ISO 8601 validation
# ---------------------------------------------------------------------------


def test_invalid_iso_datetime() -> None:
    ctx = SessionContext(
        current_datetime="not-a-date",
        timezone="PDT",
        username="alice",
    )
    with pytest.raises(ValueError, match="not valid ISO 8601"):
        compose_system_message(ClientType.CLI, ctx, _NO_TOOLS)


# ---------------------------------------------------------------------------
# 14. DB policy priority ordering
# ---------------------------------------------------------------------------


def test_db_policy_priority_ordering() -> None:
    """DB-only policies are assembled in priority order (ascending)."""
    db = [
        {
            "name": "second",
            "content": "SECOND_MARKER",
            "tool_gate": "",
            "priority": 10,
            "enabled": True,
        },
        {
            "name": "first",
            "content": "FIRST_MARKER",
            "tool_gate": "",
            "priority": 1,
            "enabled": True,
        },
    ]
    result = compose_system_message(
        ClientType.CLI,
        _VALID_CTX,
        _NO_TOOLS,
        db_policies=db,
    )
    first_pos = result.index("FIRST_MARKER")
    second_pos = result.index("SECOND_MARKER")
    assert first_pos < second_pos


# ---------------------------------------------------------------------------
# 15. TOOLS module excluded when no tools available
# ---------------------------------------------------------------------------


def test_tools_excluded_when_no_tools() -> None:
    """TOOLS module is not included when available_tools is empty."""
    result = compose_system_message(
        ClientType.CLI,
        _VALID_CTX,
        _NO_TOOLS,
    )
    assert "TOOL PATTERNS" not in result


def test_coordinator_kind_selects_coord_tools() -> None:
    """kind='coordinator' swaps in tools_coordinator.md with the right patterns."""
    coord_tools = frozenset(
        {
            "spawn_workstream",
            "send_to_workstream",
            "inspect_workstream",
            "close_workstream",
            "list_workstreams",
            "list_nodes",
            "list_skills",
            "task_list",
        }
    )
    result = compose_system_message(
        ClientType.WEB,
        _VALID_CTX,
        coord_tools,
        kind="coordinator",
    )
    # Coordinator tool patterns are present.
    assert "spawn_workstream" in result
    assert "inspect_workstream" in result
    assert "task_list" in result
    # IC tool patterns are NOT present — the model must not be instructed
    # to call tools it doesn't have.
    for phantom in (
        "read_file",
        "edit_file",
        "write_file",
        "bash",
        "plan_agent",
        "web_fetch",
        "web_search",
    ):
        assert phantom not in result, (
            f"coordinator prompt must not advertise phantom tool {phantom!r}"
        )


def test_coordinator_kind_uses_orchestrator_persona() -> None:
    """kind='coordinator' swaps in base_coordinator.md."""
    result = compose_system_message(
        ClientType.CLI,
        _VALID_CTX,
        frozenset({"spawn_workstream"}),
        kind="coordinator",
    )
    # IC-framing phrases from base.md should NOT appear.
    for ic_phrase in ("read before you edit", "commits you make"):
        assert ic_phrase not in result, f"coordinator persona leaked IC framing: {ic_phrase!r}"
    # Orchestrator-framing phrases from base_coordinator.md should appear.
    assert "orchestrate" in result
    assert "delegate" in result


def test_interactive_kind_default_still_loads_ic_tools() -> None:
    """Default kind='interactive' still loads tools.md (no regression)."""
    result = compose_system_message(
        ClientType.CLI,
        _VALID_CTX,
        _ALL_TOOLS,
    )
    assert "read_file" in result
    assert "bash" in result


def test_tools_included_when_tools_available() -> None:
    """TOOLS module is included when available_tools is non-empty."""
    result = compose_system_message(
        ClientType.CLI,
        _VALID_CTX,
        _ALL_TOOLS,
    )
    assert "TOOL PATTERNS" in result
