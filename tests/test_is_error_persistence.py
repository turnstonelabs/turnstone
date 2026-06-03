"""is_error persistence (canonical-trajectory storage cut #5, sub-commit 1).

Tool-result error state used to be an in-memory-only message key; it is now a
persisted `conversations.is_error` column so a reload preserves it.  These exercise
the round-trip on an ephemeral backend (`_schema` create_all → save → SELECT →
reconstruct); the actual `upgrade()` path is covered by test_migration_060.py.
"""

from __future__ import annotations

import json
from typing import Any

_TC = json.dumps([{"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}])


def test_tool_is_error_persists(backend: Any) -> None:
    ws = "ws-iserr-1"
    backend.save_message(ws, "user", "do it")
    backend.save_message(ws, "assistant", "", tool_calls=_TC)
    backend.save_message(ws, "tool", "boom", tool_call_id="c1", is_error=True)
    tool = next(m for m in backend.load_messages(ws, repair=False) if m["role"] == "tool")
    assert tool.get("is_error") is True


def test_tool_without_error_has_no_flag(backend: Any) -> None:
    ws = "ws-iserr-2"
    backend.save_message(ws, "tool", "ok", tool_call_id="c1")
    tool = next(m for m in backend.load_messages(ws, repair=False) if m["role"] == "tool")
    # Only set when True (matches the in-memory convention; consumers use .get()).
    assert "is_error" not in tool


def test_non_tool_rows_never_carry_is_error(backend: Any) -> None:
    ws = "ws-iserr-4"
    backend.save_message(ws, "user", "hi")
    backend.save_message(ws, "assistant", "hello")
    msgs = backend.load_messages(ws, repair=False)
    assert all("is_error" not in m for m in msgs)


def test_bulk_preserves_is_error(backend: Any) -> None:
    ws = "ws-iserr-3"
    backend.save_messages_bulk(
        [
            {
                "ws_id": ws,
                "role": "tool",
                "content": "boom",
                "tool_call_id": "c1",
                "is_error": True,
            },
            {"ws_id": ws, "role": "tool", "content": "ok", "tool_call_id": "c2"},
        ]
    )
    by_id = {
        m["tool_call_id"]: m for m in backend.load_messages(ws, repair=False) if m["role"] == "tool"
    }
    assert by_id["c1"].get("is_error") is True
    assert "is_error" not in by_id["c2"]
