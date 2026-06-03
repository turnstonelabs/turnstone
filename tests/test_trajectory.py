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
    Role,
    TextBlock,
    ToolCall,
    Turn,
    materialize_attachments,
    resolve_attachment_parts,
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
            {"type": "image", "attachment_id": "sha256:aaaa"},
        ],
        "_attachments_meta": [{"kind": "image", "filename": "x.png", "mime_type": "image/png"}],
    },
    {
        # All-text multipart list (the unreadable-attachment placeholder path)
        # stays a list — must not collapse to a joined string.
        "role": "user",
        "content": [
            {"type": "text", "text": "read this"},
            {"type": "text", "text": "[unreadable attachment: bad.bin]"},
        ],
    },
    {
        # By-reference attachments: the canonical content form (id, never bytes).
        "role": "user",
        "content": [
            {"type": "text", "text": "what's this?"},
            {"type": "image", "attachment_id": "sha256:abc"},
            {"type": "document", "attachment_id": "sha256:def"},
        ],
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
            {"type": "image", "attachment_id": "sha256:bbbb"},
        ],
    },
    {"role": "system", "content": "guard note", "_source": "output_guard"},
    {"role": "system", "content": "you are an assistant"},  # base prompt, no _source
    # Operator turn with structured per-kind meta (the watch-result card source).
    {
        "role": "system",
        "content": "ci failed",
        "_source": "watch_triggered",
        "_source_meta": {"watch_name": "ci", "command": "make test", "poll_count": 3},
    },
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
def test_source_meta_maps_to_meta_extra() -> None:
    # The per-kind operator meta rides a single ``_source_meta`` dict and lands
    # in ``Turn.meta.extra["source_meta"]`` (and round-trips back out).
    t = turn_from_dict(
        {
            "role": "system",
            "content": "ci failed",
            "_source": "watch_triggered",
            "_source_meta": {"watch_name": "ci", "poll_count": 3},
        }
    )
    assert t.meta.extra["source_meta"] == {"watch_name": "ci", "poll_count": 3}
    assert turn_to_dict(t)["_source_meta"] == {"watch_name": "ci", "poll_count": 3}


def test_empty_source_meta_omitted() -> None:
    # An empty meta dict is not carried (no key on the Turn, none re-emitted).
    t = turn_from_dict(
        {"role": "system", "content": "n", "_source": "correction", "_source_meta": {}}
    )
    assert "source_meta" not in t.meta.extra
    assert "_source_meta" not in turn_to_dict(t)


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


def test_multipart_text_textblock_placeholder_attachmentref() -> None:
    t = turn_from_dict(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "q"},
                {"type": "image", "attachment_id": "sha256:abc"},
            ],
        }
    )
    assert isinstance(t.content[0], TextBlock) and t.content[0].text == "q"
    assert isinstance(t.content[1], AttachmentRef)
    assert t.text == "q"  # only the text part contributes to FTS


def test_developer_collapses_to_system() -> None:
    t = turn_from_dict({"role": "developer", "content": "d"})
    assert t.role is Role.SYSTEM
    # Re-emits as system (wire-identical: providers treat system/developer alike).
    assert turn_to_dict(t)["role"] == "system"


def test_empty_content_roundtrips_to_empty_string() -> None:
    assert turn_to_dict(Turn(Role.ASSISTANT)) == {"role": "assistant", "content": ""}


def test_attachment_ref_is_the_canonical_non_text_form() -> None:
    # By-reference placeholders ``{type: image|document, attachment_id}`` are the
    # canonical non-text content; a resolved inline ``image_url`` (no id) never
    # reaches turn_from_dict on the canonical path and is dropped if it does.
    t = turn_from_dict(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "q"},
                {"type": "image", "attachment_id": "sha256:abc"},
                {"type": "document", "attachment_id": "sha256:def"},
                {"type": "image_url", "image_url": {"url": "data:..."}},
            ],
        }
    )
    assert [type(b).__name__ for b in t.content] == ["TextBlock", "AttachmentRef", "AttachmentRef"]
    assert t.content[1].attachment_id == "sha256:abc"  # type: ignore[union-attr]
    assert t.content[2].kind == "document"  # type: ignore[union-attr]


def test_attachment_ref_emits_placeholder() -> None:
    t = Turn(Role.USER, (AttachmentRef(attachment_id="abc", kind="image"),))
    assert turn_to_dict(t)["content"] == [{"type": "image", "attachment_id": "abc"}]


def test_resolve_attachment_parts_materializes_and_drops_missing() -> None:
    # The dict-side resolver: placeholders → inline parts; a pruned id is dropped.
    part = {"type": "image_url", "image_url": {"url": "data:img"}}
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image", "attachment_id": "a1"},
                {"type": "image", "attachment_id": "gone"},
            ],
        }
    ]
    out = resolve_attachment_parts(messages, {"a1": part})
    assert out[0]["content"] == [{"type": "text", "text": "look"}, part]


def test_resolve_attachment_parts_identity_when_no_placeholders() -> None:
    messages = [{"role": "user", "content": "hi"}]
    assert resolve_attachment_parts(messages, {}) is messages


def test_materialize_attachments_collects_ids_and_substitutes() -> None:
    part = {"type": "image_url", "image_url": {"url": "data:img"}}
    seen_ids: list[list[str]] = []

    def _resolve(ids: list[str]) -> dict[str, dict[str, object]]:
        seen_ids.append(ids)
        return {"a1": part}

    messages = [
        {"role": "user", "content": [{"type": "image", "attachment_id": "a1"}]},
    ]
    out = materialize_attachments(messages, _resolve)
    assert seen_ids == [["a1"]]  # collected the placeholder id
    assert out[0]["content"] == [part]


def test_materialize_attachments_noop_without_resolver_or_placeholders() -> None:
    messages = [{"role": "user", "content": "hi"}]
    assert materialize_attachments(messages, None) is messages
    assert materialize_attachments(messages, lambda ids: {}) is messages
