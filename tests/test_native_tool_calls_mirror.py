"""Characterization + unit tests for the native↔tool_calls mirror (precondition P1).

The persisted (and in-memory) form of an ``assistant`` turn must never carry a *client*
tool-call block in its verbatim native lane (``provider_data`` / ``_provider_content``)
without a matching ``tool_calls`` entry — otherwise a same-provider resume replays an
orphan ``tool_use`` / ``function_call`` with no ``tool_result`` and the API rejects it
(the truncated-mid-tool_use hole).  ``normalize_native_for_save`` enforces this at the
persistence boundary; ``strip_orphan_client_tool_blocks`` is the in-memory equivalent.

These tests pin the behaviour so the later removal of the Anthropic ``pc_tool_ids``
fallback (which masks this today) is provably safe.
"""

from __future__ import annotations

import json
from typing import Any

from turnstone.core.storage._utils import (
    normalize_native_for_save,
    strip_orphan_client_tool_blocks,
)

_THINKING = {"type": "thinking", "thinking": "reasoning text", "signature": "sig-1"}
_TOOL_USE = {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "Paris"}}
_FUNCTION_CALL = {"type": "function_call", "call_id": "call_1", "name": "x", "arguments": "{}"}
_GOOGLE_FN = {
    "type": "function",
    "id": "call_1",
    "function": {"name": "x"},
    "thought_signature": "ts",
}
_SERVER_TOOL_USE = {"type": "server_tool_use", "id": "srv_1", "name": "web_search", "input": {}}
_WEB_SEARCH_RESULT = {"type": "web_search_tool_result", "content": "...", "tool_use_id": "srv_1"}

_TOOL_CALLS_JSON = json.dumps(
    [{"id": "call_1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]
)


def _types(provider_data: str | None) -> list[str]:
    assert provider_data is not None
    return [b["type"] for b in json.loads(provider_data)]


# --------------------------------------------------------------------------- #
# strip_orphan_client_tool_blocks (the in-memory primitive)
# --------------------------------------------------------------------------- #
def test_strip_removes_each_provider_client_tool_call_shape() -> None:
    blocks = [_THINKING, _TOOL_USE, _FUNCTION_CALL, _GOOGLE_FN]
    kept = strip_orphan_client_tool_blocks(blocks)
    assert kept == [_THINKING]


def test_strip_keeps_server_tool_and_reasoning_blocks() -> None:
    blocks = [_THINKING, _SERVER_TOOL_USE, _WEB_SEARCH_RESULT]
    assert strip_orphan_client_tool_blocks(blocks) == blocks


def test_strip_does_not_mutate_input() -> None:
    blocks = [_THINKING, _TOOL_USE]
    strip_orphan_client_tool_blocks(blocks)
    assert blocks == [_THINKING, _TOOL_USE]


def test_strip_ignores_non_dict_entries() -> None:
    blocks: list[Any] = ["raw", 42, _TOOL_USE]
    assert strip_orphan_client_tool_blocks(blocks) == ["raw", 42]


# --------------------------------------------------------------------------- #
# normalize_native_for_save (the persistence-boundary chokepoint)
# --------------------------------------------------------------------------- #
def test_normalize_strips_orphan_tool_use_when_no_tool_calls() -> None:
    out = normalize_native_for_save("assistant", json.dumps([_THINKING, _TOOL_USE]), None)
    assert _types(out) == ["thinking"]


def test_normalize_keeps_blocks_when_tool_calls_present() -> None:
    pdata = json.dumps([_THINKING, _TOOL_USE])
    # Mirror holds (a matching tool_calls entry exists) → untouched.
    assert normalize_native_for_save("assistant", pdata, _TOOL_CALLS_JSON) == pdata


def test_normalize_keeps_server_tool_blocks_when_no_tool_calls() -> None:
    pdata = json.dumps([_SERVER_TOOL_USE, _WEB_SEARCH_RESULT])
    # Server-side blocks have no client tool_result to orphan → identity.
    assert normalize_native_for_save("assistant", pdata, None) == pdata


def test_normalize_returns_none_when_only_orphan_blocks() -> None:
    assert normalize_native_for_save("assistant", json.dumps([_TOOL_USE]), None) is None


def test_normalize_non_assistant_is_identity() -> None:
    pdata = json.dumps([_TOOL_USE])
    assert normalize_native_for_save("tool", pdata, None) == pdata


def test_normalize_empty_and_malformed_pass_through() -> None:
    assert normalize_native_for_save("assistant", None, None) is None
    assert normalize_native_for_save("assistant", "not json", None) == "not json"
    assert normalize_native_for_save("assistant", json.dumps({"k": "v"}), None) == json.dumps(
        {"k": "v"}
    )


def test_normalize_treats_empty_list_tool_calls_as_absent() -> None:
    # An empty "[]" / "null" tool_calls must NOT count as "present".
    out = normalize_native_for_save("assistant", json.dumps([_THINKING, _TOOL_USE]), "[]")
    assert _types(out) == ["thinking"]


# --------------------------------------------------------------------------- #
# Integration: the save path (both save_message and the bulk path) enforces it.
# --------------------------------------------------------------------------- #
def test_save_message_drops_orphan_native_tool_use(backend: Any) -> None:
    ws = "ws-mirror-1"
    pdata = json.dumps([_THINKING, _TOOL_USE])
    backend.save_message(
        ws, "assistant", "truncated mid tool_use", provider_data=pdata, tool_calls=None
    )
    # repair=False so we inspect the raw stored row (the save chokepoint), not the
    # reconstruct-time trailing-incomplete-turn strip.
    asst = next(m for m in backend.load_messages(ws, repair=False) if m["role"] == "assistant")
    types = [b["type"] for b in asst.get("_provider_content", [])]
    assert "thinking" in types  # reasoning preserved
    assert "tool_use" not in types  # orphan stripped at save → safe to resume


def test_save_message_keeps_native_tool_use_with_matching_tool_calls(backend: Any) -> None:
    ws = "ws-mirror-2"
    pdata = json.dumps([_THINKING, _TOOL_USE])
    backend.save_message(ws, "assistant", "", provider_data=pdata, tool_calls=_TOOL_CALLS_JSON)
    # repair=False so we inspect the raw stored row (the save chokepoint), not the
    # reconstruct-time trailing-incomplete-turn strip.
    asst = next(m for m in backend.load_messages(ws, repair=False) if m["role"] == "assistant")
    types = [b["type"] for b in asst.get("_provider_content", [])]
    assert "tool_use" in types  # mirror holds → native lane intact


def test_save_messages_bulk_drops_orphan_native_tool_use(backend: Any) -> None:
    ws = "ws-mirror-3"
    backend.save_messages_bulk(
        [
            {"ws_id": ws, "role": "user", "content": "hi"},
            {
                "ws_id": ws,
                "role": "assistant",
                "content": "truncated",
                "provider_data": json.dumps([_THINKING, _TOOL_USE]),
                "tool_calls": None,
            },
        ]
    )
    # repair=False so we inspect the raw stored row (the save chokepoint), not the
    # reconstruct-time trailing-incomplete-turn strip.
    asst = next(m for m in backend.load_messages(ws, repair=False) if m["role"] == "assistant")
    types = [b["type"] for b in asst.get("_provider_content", [])]
    assert types == ["thinking"]
