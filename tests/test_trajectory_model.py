"""Unit tests for the canonical ``Turn`` model (turnstone.core.trajectory)."""

from __future__ import annotations

from turnstone.core.trajectory import (
    AttachmentRef,
    ProviderNative,
    Role,
    TextBlock,
    ToolCall,
    Turn,
    TurnMeta,
)


def test_role_values() -> None:
    assert {r.value for r in Role} == {"user", "assistant", "tool", "system"}
    # Constructible from the wire string (used by the row→Turn adapter).
    assert Role("assistant") is Role.ASSISTANT


def test_text_joins_only_textblocks() -> None:
    turn = Turn(
        Role.USER,
        (TextBlock("look at "), AttachmentRef("sha-1", "image"), TextBlock("this")),
    )
    # Attachment blocks contribute nothing to the FTS/text projection.
    assert turn.text == "look at this"


def test_user_helper() -> None:
    turn = Turn.user("hello")
    assert turn.role is Role.USER
    assert turn.content == (TextBlock("hello"),)
    assert turn.text == "hello"


def test_tool_helper_carries_error_flag() -> None:
    ok = Turn.tool("call_1", "result")
    err = Turn.tool("call_2", "boom", is_error=True)
    assert ok.role is Role.TOOL and ok.tool_call_id == "call_1" and ok.is_error is False
    assert err.is_error is True and err.text == "boom"


def test_assistant_with_tool_calls_and_native() -> None:
    tc = ToolCall(id="call_1", name="get_weather", arguments='{"city": "Paris"}')
    native = ProviderNative(producer="anthropic", blocks=({"type": "thinking"},))
    turn = Turn.assistant("on it", tool_calls=(tc,), native=native)
    assert turn.role is Role.ASSISTANT
    assert turn.tool_calls == (tc,)
    assert turn.native is native
    assert turn.text == "on it"


def test_assistant_empty_text_has_no_content_block() -> None:
    # A tool-only assistant turn carries no TextBlock.
    turn = Turn.assistant("", tool_calls=(ToolCall("call_1", "x", "{}"),))
    assert turn.content == ()
    assert turn.text == ""


def test_system_turn_source_marks_operator_context() -> None:
    base = Turn.system("base prompt")
    op = Turn.system("output-guard flagged this", source="output_guard")
    assert base.source is None  # base prompt
    assert op.source == "output_guard"  # operator-context turn


def test_turnmeta_defaults_are_independent() -> None:
    # default_factory must not share a single dict across instances.
    a = Turn.user("a")
    b = Turn.user("b")
    a.meta.extra["k"] = "v"
    assert b.meta.extra == {}
    assert isinstance(a.meta, TurnMeta)


def test_provider_native_defaults_to_empty_blocks() -> None:
    assert ProviderNative(producer="google").blocks == ()
