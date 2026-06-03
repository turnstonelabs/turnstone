"""dict ↔ Turn adapter fidelity (the strangler bridge for the Turn migration).

The contract is byte-identical round-tripping — ``turn_to_dict(turn_from_dict(d))
== d`` — for every message-dict shape ``reconstruct_messages`` and the wire path
produce.  These tests pin that, plus the field mapping (``_``-side channels →
typed ``Turn`` fields).
"""

from __future__ import annotations

from typing import Any

import pytest

from turnstone.core.trajectory import (
    AttachmentRef,
    ProviderNative,
    RawContentBlock,
    Role,
    TextBlock,
    ToolCall,
    Turn,
    turn_from_dict,
    turn_to_dict,
    turns_from_dicts,
)

# Every shape reconstruct / the wire path emits, as the round-trip corpus.
_ROUNDTRIP: list[dict[str, Any]] = [
    {"role": "user", "content": "hello"},
    {"role": "user", "content": "with source", "_source": "user_interjection"},
    {"role": "user", "content": "ev", "_event_id": 42},
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "what's this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
        "_attachments_meta": [{"kind": "image", "filename": "x.png", "mime_type": "image/png"}],
    },
    {"role": "assistant", "content": "hi there"},
    {"role": "assistant", "content": ""},  # empty assistant (no text, no tools)
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": '{"x":1}'}},
        ],
    },
    {
        "role": "assistant",
        "content": "let me think",
        "_provider_content": [{"type": "thinking", "thinking": "hmm", "signature": "s"}],
        "_producer": "anthropic",
    },
    {
        # native lane WITHOUT a producer tag (the legacy bare-list path).
        "role": "assistant",
        "content": "x",
        "_provider_content": [{"type": "reasoning_text", "text": "r"}],
    },
    {"role": "tool", "tool_call_id": "c1", "content": "result"},
    {"role": "tool", "tool_call_id": "c2", "content": "boom", "is_error": True},
    {
        "role": "tool",
        "tool_call_id": "c3",
        "content": [
            {"type": "text", "text": "saw an image"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}},
        ],
    },
    {"role": "system", "content": "guard note", "_source": "output_guard"},
    {"role": "system", "content": "you are an assistant"},  # base prompt, no _source
]


@pytest.mark.parametrize("msg", _ROUNDTRIP, ids=range(len(_ROUNDTRIP)))
def test_dict_turn_dict_roundtrip(msg: dict[str, Any]) -> None:
    assert turn_to_dict(turn_from_dict(msg)) == msg


def test_turns_from_dicts_preserves_order_and_count() -> None:
    turns = turns_from_dicts(_ROUNDTRIP)
    assert len(turns) == len(_ROUNDTRIP)
    assert [t.role.value for t in turns] == [
        m["role"] if m["role"] != "developer" else "system" for m in _ROUNDTRIP
    ]


# --------------------------------------------------------------------------- #
# Field mapping — the side channels become typed Turn fields.
# --------------------------------------------------------------------------- #
def test_source_maps_to_source_field() -> None:
    t = turn_from_dict({"role": "system", "content": "n", "_source": "tool_error"})
    assert t.role is Role.SYSTEM
    assert t.source == "tool_error"


def test_provider_content_maps_to_native_lane() -> None:
    t = turn_from_dict(
        {
            "role": "assistant",
            "content": "x",
            "_provider_content": [{"type": "thinking", "thinking": "z"}],
            "_producer": "anthropic",
        }
    )
    assert isinstance(t.native, ProviderNative)
    assert t.native.producer == "anthropic"
    assert t.native.blocks == ({"type": "thinking", "thinking": "z"},)


def test_native_without_producer_defaults_empty_and_omits_on_emit() -> None:
    t = turn_from_dict(
        {"role": "assistant", "content": "x", "_provider_content": [{"type": "reasoning_text"}]}
    )
    assert t.native is not None and t.native.producer == ""
    # Empty producer is not re-emitted (matches the bare-list legacy dict).
    assert "_producer" not in turn_to_dict(t)


def test_tool_call_id_and_is_error_map_to_tool_fields() -> None:
    t = turn_from_dict({"role": "tool", "tool_call_id": "c1", "content": "e", "is_error": True})
    assert t.role is Role.TOOL
    assert t.tool_call_id == "c1"
    assert t.is_error is True


def test_event_id_maps_to_meta() -> None:
    t = turn_from_dict({"role": "user", "content": "x", "_event_id": 7})
    assert t.meta.event_id == 7


def test_tool_calls_map_to_typed_toolcalls() -> None:
    t = turn_from_dict(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}},
            ],
        }
    )
    assert t.tool_calls == (ToolCall(id="c1", name="f", arguments="{}"),)


def test_multipart_text_part_stays_textblock_image_is_raw() -> None:
    t = turn_from_dict(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "q"},
                {"type": "image_url", "image_url": {"url": "data:..."}},
            ],
        }
    )
    assert isinstance(t.content[0], TextBlock) and t.content[0].text == "q"
    assert isinstance(t.content[1], RawContentBlock)
    assert t.text == "q"  # only the text part contributes to FTS


def test_developer_collapses_to_system() -> None:
    t = turn_from_dict({"role": "developer", "content": "d"})
    assert t.role is Role.SYSTEM
    # Re-emits as system (wire-identical: providers treat system/developer alike).
    assert turn_to_dict(t)["role"] == "system"


def test_empty_content_roundtrips_to_empty_string() -> None:
    assert turn_to_dict(Turn(Role.ASSISTANT)) == {"role": "assistant", "content": ""}


def test_attachment_ref_emits_defensive_part() -> None:
    # AttachmentRef isn't produced pre-by-ref-wiring, but the emit path is defined.
    t = Turn(Role.USER, (AttachmentRef(attachment_id="abc", kind="image"),))
    assert turn_to_dict(t)["content"] == [{"type": "image", "attachment_id": "abc"}]
