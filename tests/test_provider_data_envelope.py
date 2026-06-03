"""provider_data ``{producer, blocks}`` storage envelope (#5 sub-commit 2a).

Native blocks persist wrapped with the generating provider's name so the lowering layer
can later replay them verbatim only to their producer.  The envelope is storage-only:
``reconstruct_messages`` unwraps it back to a bare block list (every ``_provider_content``
consumer requires a plain list) and surfaces the producer on the ``_producer`` side
channel.  Legacy bare-list rows are dual-read unchanged.
"""

from __future__ import annotations

import json
from typing import Any

from turnstone.core.storage._utils import prepare_provider_data_for_save, wrap_provider_data

_BLOCKS = [{"type": "thinking", "thinking": "reasoning", "signature": "s"}]
_BLOCKS_JSON = json.dumps(_BLOCKS)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def test_wrap_adds_envelope() -> None:
    out = wrap_provider_data(_BLOCKS_JSON, "anthropic")
    assert out is not None
    assert json.loads(out) == {"producer": "anthropic", "blocks": _BLOCKS}


def test_wrap_no_producer_keeps_bare_list() -> None:
    assert wrap_provider_data(_BLOCKS_JSON, None) == _BLOCKS_JSON


def test_wrap_none_passthrough() -> None:
    assert wrap_provider_data(None, "anthropic") is None


def test_wrap_already_wrapped_is_unchanged() -> None:
    wrapped = json.dumps({"producer": "x", "blocks": _BLOCKS})
    assert wrap_provider_data(wrapped, "anthropic") == wrapped


def test_prepare_strips_orphan_then_wraps() -> None:
    # A tool_use with no matching tool_calls is an orphan (P1 mirror) → stripped,
    # and what survives is wrapped with the producer.
    pd = json.dumps(
        [
            {"type": "thinking", "thinking": "t"},
            {"type": "tool_use", "id": "c1", "name": "x", "input": {}},
        ]
    )
    out = prepare_provider_data_for_save("assistant", pd, None, "anthropic")
    assert out is not None
    env = json.loads(out)
    assert env["producer"] == "anthropic"
    assert [b["type"] for b in env["blocks"]] == ["thinking"]


# --------------------------------------------------------------------------- #
# round-trip via a real (ephemeral) backend: save → reconstruct dual-read
# --------------------------------------------------------------------------- #
def test_round_trip_surfaces_bare_blocks_and_producer(backend: Any) -> None:
    ws = "ws-env-1"
    backend.save_message(ws, "assistant", "hi", provider_data=_BLOCKS_JSON, producer="anthropic")
    a = next(m for m in backend.load_messages(ws, repair=False) if m["role"] == "assistant")
    assert a["_provider_content"] == _BLOCKS  # bare list — the consumer contract
    assert a["_producer"] == "anthropic"


def test_legacy_bare_list_dual_read(backend: Any) -> None:
    ws = "ws-env-2"
    # producer omitted → stored as a bare list (legacy shape), read back unchanged.
    backend.save_message(ws, "assistant", "hi", provider_data=_BLOCKS_JSON)
    a = next(m for m in backend.load_messages(ws, repair=False) if m["role"] == "assistant")
    assert a["_provider_content"] == _BLOCKS
    assert "_producer" not in a


def test_bulk_round_trip_with_producer(backend: Any) -> None:
    ws = "ws-env-3"
    backend.save_messages_bulk(
        [
            {
                "ws_id": ws,
                "role": "assistant",
                "content": "hi",
                "provider_data": _BLOCKS_JSON,
                "producer": "google",
            }
        ]
    )
    a = next(m for m in backend.load_messages(ws, repair=False) if m["role"] == "assistant")
    assert a["_provider_content"] == _BLOCKS
    assert a["_producer"] == "google"
