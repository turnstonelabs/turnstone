"""Tests for the shared message reconstruction logic."""

import itertools
import json

from turnstone.core.storage._utils import reconstruct_messages

_row_ids = itertools.count(1)


def _row(
    role,
    content=None,
    tool_name=None,
    tc_id=None,
    pdata=None,
    tool_calls=None,
    source=None,
):
    """Build an 8-element conversation row tuple (id, role, ...).

    Trailing ``source`` is the persisted twin of the in-memory ``_source``
    side-channel.  (The ``_reminders`` column that used to ride here was
    dropped in migration 060 — operator context lives in ``system`` turns.)
    """
    return (
        next(_row_ids),
        role,
        content,
        tool_name,
        tc_id,
        pdata,
        tool_calls,
        source,
    )


class TestAssistantWithToolCalls:
    """Assistant messages with tool_calls JSON are self-contained."""

    def test_assistant_with_tool_calls_and_content(self):
        tc = json.dumps(
            [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"/tmp/x"}'},
                }
            ]
        )
        rows = [
            _row("assistant", "Let me check that.", tool_calls=tc),
            _row("tool", "file contents", tc_id="call_1"),
        ]
        msgs = reconstruct_messages(rows, "ws1")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "assistant"
        assert msgs[0]["content"] == "Let me check that."
        assert len(msgs[0]["tool_calls"]) == 1
        assert msgs[0]["tool_calls"][0]["function"]["name"] == "read_file"
        assert msgs[1]["role"] == "tool"
        assert msgs[1]["tool_call_id"] == "call_1"

    def test_assistant_with_multiple_tool_calls(self):
        tc = json.dumps(
            [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"ls"}'},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"pwd"}'},
                },
            ]
        )
        rows = [
            _row("assistant", tool_calls=tc),
            _row("tool", "files", tc_id="call_1"),
            _row("tool", "/home", tc_id="call_2"),
        ]
        msgs = reconstruct_messages(rows, "ws1")
        assert len(msgs) == 3
        assert len(msgs[0]["tool_calls"]) == 2
        assert msgs[1]["role"] == "tool"
        assert msgs[2]["role"] == "tool"

    def test_assistant_without_tool_calls(self):
        rows = [_row("assistant", "Hello there.")]
        msgs = reconstruct_messages(rows, "ws1")
        assert len(msgs) == 1
        assert msgs[0]["role"] == "assistant"
        assert msgs[0]["content"] == "Hello there."
        assert "tool_calls" not in msgs[0]


class TestMultipleTurns:
    """Multiple assistant turns with tool calls stay separate."""

    def test_two_tool_call_turns(self):
        tc1 = json.dumps(
            [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"ls"}'},
                }
            ]
        )
        tc2 = json.dumps(
            [
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"cat file1"}'},
                }
            ]
        )
        rows = [
            _row("assistant", "I'll run two commands.", tool_calls=tc1),
            _row("tool", "file1\nfile2", tc_id="call_1"),
            _row("assistant", "Now reading.", tool_calls=tc2),
            _row("tool", "contents", tc_id="call_2"),
        ]
        msgs = reconstruct_messages(rows, "ws1")
        assert len(msgs) == 4
        assert msgs[0]["content"] == "I'll run two commands."
        assert len(msgs[0]["tool_calls"]) == 1
        assert msgs[2]["content"] == "Now reading."
        assert len(msgs[2]["tool_calls"]) == 1

    def test_denied_tool_calls_with_commentary(self):
        """Two denied tool batches with assistant commentary in between."""
        tc1 = json.dumps(
            [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"find /"}'},
                }
            ]
        )
        tc2 = json.dumps(
            [
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"curl ..."}'},
                }
            ]
        )
        rows = [
            _row("assistant", tool_calls=tc1),
            _row("tool", "Denied by user", tc_id="call_1"),
            _row("assistant", "Interesting! Let me try something else."),
            _row("assistant", tool_calls=tc2),
            _row("tool", "Denied by user", tc_id="call_2"),
        ]
        msgs = reconstruct_messages(rows, "ws1")
        assert len(msgs) == 5
        assert msgs[0]["role"] == "assistant"
        assert msgs[0]["tool_calls"][0]["function"]["name"] == "bash"
        assert msgs[1]["role"] == "tool"
        assert msgs[2]["role"] == "assistant"
        assert msgs[2]["content"] == "Interesting! Let me try something else."
        assert "tool_calls" not in msgs[2]
        assert msgs[3]["role"] == "assistant"
        assert msgs[3]["tool_calls"][0]["function"]["name"] == "bash"
        assert msgs[4]["role"] == "tool"


class TestEdgeCases:
    """Edge cases in message reconstruction."""

    def test_incomplete_turn_repair(self):
        """Trailing tool_calls without enough tool_results are stripped."""
        tc = json.dumps(
            [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"ls"}'},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"cat x"}'},
                },
            ]
        )
        rows = [
            _row("user", "hello"),
            _row("assistant", "Let me check.", tool_calls=tc),
            # Only 1 tool result for 2 tool_calls
            _row("tool", "file1", tc_id="call_1"),
        ]
        msgs = reconstruct_messages(rows, "ws1")
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    def test_empty_rows(self):
        msgs = reconstruct_messages([], "ws1")
        assert msgs == []

    def test_provider_data_preserved(self):
        pdata = json.dumps([{"type": "text", "text": "hello"}])
        rows = [_row("assistant", "hello", pdata=pdata)]
        msgs = reconstruct_messages(rows, "ws1")
        assert msgs[0]["_provider_content"] == [{"type": "text", "text": "hello"}]

    def test_user_message(self):
        rows = [_row("user", "hello world")]
        msgs = reconstruct_messages(rows, "ws1")
        assert len(msgs) == 1
        assert msgs[0] == {"role": "user", "content": "hello world"}

    def test_none_content_becomes_empty_string(self):
        rows = [_row("user", None)]
        msgs = reconstruct_messages(rows, "ws1")
        assert msgs[0]["content"] == ""

    def test_tool_without_tc_id_uses_empty_string(self):
        rows = [
            _row(
                "assistant",
                tool_calls=json.dumps(
                    [{"id": "c1", "type": "function", "function": {"name": "x", "arguments": ""}}]
                ),
            ),
            _row("tool", "output", tc_id=None),
        ]
        msgs = reconstruct_messages(rows, "ws1")
        assert msgs[1]["tool_call_id"] == ""

    def test_unknown_role_ignored(self):
        """Genuinely unknown roles are dropped; ``system``/``developer`` are
        first-class and reconstructed (see TestSystemTurns)."""
        rows = [
            _row("user", "hi"),
            _row("weird", "???"),
            _row("assistant", "hello"),
        ]
        msgs = reconstruct_messages(rows, "ws1")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"


class TestMidConversationOrphanRepair:
    """Mid-conversation orphaned tool_calls are LEFT bare at load — the
    send-time repair (``lowering.repair_wire_messages``, covered in
    ``test_lowering.py``) fills them.  Load is trailing-strip only."""

    def test_all_orphaned_not_synthesized_at_load(self):
        """Assistant has 2 unanswered tool_calls then a user turn: left bare."""
        tc = json.dumps(
            [
                {"id": "c1", "function": {"name": "bash", "arguments": "{}"}},
                {"id": "c2", "function": {"name": "write_file", "arguments": "{}"}},
            ]
        )
        rows = [
            _row("user", "do stuff"),
            _row("assistant", "Running...", tool_calls=tc),
            _row("user", "never mind"),
        ]
        msgs = reconstruct_messages(rows, "ws1")
        # No synthesis at load — the orphan is mid-conversation (not trailing),
        # so the strip leaves it; the send pass synthesizes it.
        assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
        assert not any(m["role"] == "tool" for m in msgs)

    def test_partial_results_not_synthesized_at_load(self):
        """A present result is kept; the missing sibling is NOT filled at load."""
        tc = json.dumps(
            [
                {"id": "c1", "function": {"name": "bash", "arguments": "{}"}},
                {"id": "c2", "function": {"name": "write_file", "arguments": "{}"}},
            ]
        )
        rows = [
            _row("user", "do stuff"),
            _row("assistant", "", tool_calls=tc),
            _row("tool", "file1.txt", tool_name="bash", tc_id="c1"),
            _row("user", "skip the write"),
        ]
        msgs = reconstruct_messages(rows, "ws1")
        # Real c1 result kept; c2 left orphaned (synthesized at send, not here).
        assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "user"]
        tool_msgs = [m for m in msgs if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "c1"
        assert tool_msgs[0]["content"] == "file1.txt"

    def test_complete_results_no_synthesis(self):
        """All tool_calls have results — no synthesis needed."""
        tc = json.dumps(
            [
                {"id": "c1", "function": {"name": "bash", "arguments": "{}"}},
            ]
        )
        rows = [
            _row("user", "do it"),
            _row("assistant", "", tool_calls=tc),
            _row("tool", "done", tool_name="bash", tc_id="c1"),
            _row("user", "thanks"),
        ]
        msgs = reconstruct_messages(rows, "ws1")
        assert len(msgs) == 4
        tool_msgs = [m for m in msgs if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].get("is_error") is not True

    def test_trailing_orphan_stripped_not_synthesized(self):
        """Trailing orphan is handled by the existing strip repair, not synthesis."""
        tc = json.dumps(
            [
                {"id": "c1", "function": {"name": "bash", "arguments": "{}"}},
            ]
        )
        rows = [
            _row("user", "do it"),
            _row("assistant", "Running...", tool_calls=tc),
        ]
        msgs = reconstruct_messages(rows, "ws1")
        # Trailing strip removes the assistant message entirely
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"


class TestSystemTurns:
    """First-class operator-context system turns round-trip and respect repair."""

    def test_system_row_reconstructed(self):
        rows = [
            _row("user", "hi"),
            _row("assistant", "hello"),
            _row(
                "system",
                "User sent while you worked: also update the changelog",
                source="user_interjection",
            ),
        ]
        msgs = reconstruct_messages(rows, "ws1")
        assert len(msgs) == 3
        assert msgs[2] == {
            "role": "system",
            "content": "User sent while you worked: also update the changelog",
            "_source": "user_interjection",
        }

    def test_developer_row_reconstructed_as_system(self):
        # A developer row is kept (not dropped) but collapses into role=system
        # (zero writers; providers treat system/developer identically, so the
        # wire is unaffected).
        rows = [
            _row("user", "hi"),
            _row("assistant", "x"),
            _row("developer", "be terse", source="output_guard"),
        ]
        msgs = reconstruct_messages(rows, "ws1")
        assert msgs[2]["role"] == "system"
        assert msgs[2]["content"] == "be terse"
        assert msgs[2]["_source"] == "output_guard"

    def test_system_turn_without_source(self):
        # Missing source shouldn't happen for operator context, but must not crash.
        rows = [_row("user", "hi"), _row("assistant", "x"), _row("system", "note")]
        msgs = reconstruct_messages(rows, "ws1")
        assert msgs[2]["role"] == "system"
        assert "_source" not in msgs[2]

    def test_system_row_with_meta_column_reconstructed(self):
        # A system row carrying the JSON ``meta`` column (migration 060) rehydrates
        # the structured operator meta onto the dict as ``_source_meta`` — the
        # source the FE watch-result card derives from.  Full 11-tuple:
        # (id, role, content, tool_name, tc_id, pdata, tool_calls, source,
        #  event_id, is_error, meta).
        meta_json = json.dumps({"watch_name": "ci", "command": "make test", "poll_count": 3})
        row = (
            1,
            "system",
            "ci failed",
            None,
            None,
            None,
            None,
            "watch_triggered",
            None,
            False,
            meta_json,
        )
        msgs = reconstruct_messages([row], "ws1")
        assert msgs[0] == {
            "role": "system",
            "content": "ci failed",
            "_source": "watch_triggered",
            "_source_meta": {"watch_name": "ci", "command": "make test", "poll_count": 3},
        }

    def test_legacy_short_row_without_meta_column_valid(self):
        # A pre-meta 8-tuple row reconstructs fine (defensive length check) and
        # carries no ``_source_meta`` key.
        rows = [_row("system", "old note", source="output_guard")]
        msgs = reconstruct_messages(rows, "ws1")
        assert msgs[0]["_source"] == "output_guard"
        assert "_source_meta" not in msgs[0]

    def test_malformed_meta_column_dropped_not_crashed(self):
        # A non-JSON / non-object meta column is dropped (the human-readable body
        # still rides ``content``), never raised.
        row = (
            1,
            "system",
            "note",
            None,
            None,
            None,
            None,
            "output_guard",
            None,
            False,
            "{bad json",
        )
        msgs = reconstruct_messages([row], "ws1")
        assert "_source_meta" not in msgs[0]

    def test_trailing_system_after_incomplete_assistant_strips_both(self):
        """A nudge appended after an interrupted tool-call turn must not leave
        the orphaned assistant — the strip walks through the trailing system
        turn (regression guard for the repair-loop fix)."""
        tc = json.dumps([{"id": "c1", "function": {"name": "bash", "arguments": "{}"}}])
        rows = [
            _row("user", "do it"),
            _row("assistant", "Running...", tool_calls=tc),  # no tool result
            _row("system", "tool was cancelled", source="tool_error"),
        ]
        msgs = reconstruct_messages(rows, "ws1")
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    def test_trailing_system_after_complete_turn_kept(self):
        """A system turn after a complete tool batch is preserved (no strip)."""
        tc = json.dumps([{"id": "c1", "function": {"name": "bash", "arguments": "{}"}}])
        rows = [
            _row("user", "do it"),
            _row("assistant", "Running...", tool_calls=tc),
            _row("tool", "ok", tc_id="c1"),
            _row("system", "you repeated a call", source="repeat"),
        ]
        msgs = reconstruct_messages(rows, "ws1")
        assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "system"]

    def test_trailing_system_after_text_turn_kept(self):
        rows = [
            _row("user", "hi"),
            _row("assistant", "all done"),
            _row("system", "wrapping up?", source="completion"),
        ]
        msgs = reconstruct_messages(rows, "ws1")
        assert [m["role"] for m in msgs] == ["user", "assistant", "system"]

    def test_system_between_tool_use_and_result_not_double_synthesized(self):
        """A system turn between an assistant(tool_calls) and its real result
        must not make the answered call look orphaned — pass 2 looks through
        system turns, so no duplicate synthetic cancellation is spliced."""
        tc = json.dumps([{"id": "c1", "function": {"name": "bash", "arguments": "{}"}}])
        rows = [
            _row("user", "do it"),
            _row("assistant", "", tool_calls=tc),
            _row("system", "guard note", source="output_guard"),
            _row("tool", "ok", tc_id="c1"),
            _row("user", "thanks"),
        ]
        msgs = reconstruct_messages(rows, "ws1")
        tool_msgs = [m for m in msgs if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "c1"
        assert tool_msgs[0].get("is_error") is not True
        assert [m["role"] for m in msgs] == ["user", "assistant", "system", "tool", "user"]

    def test_orphan_past_system_left_bare_at_load(self):
        """When a call IS orphaned with a system turn after the real results,
        load leaves it bare (no splice).  The send-time repair's contiguous
        insertion past system turns is covered in ``test_lowering.py``."""
        tc = json.dumps(
            [
                {"id": "c1", "function": {"name": "bash", "arguments": "{}"}},
                {"id": "c2", "function": {"name": "bash", "arguments": "{}"}},
            ]
        )
        rows = [
            _row("user", "do it"),
            _row("assistant", "", tool_calls=tc),
            _row("tool", "ok", tc_id="c1"),
            _row("system", "guard note", source="output_guard"),
            _row("user", "skip c2"),  # c2 never resulted
        ]
        msgs = reconstruct_messages(rows, "ws1")
        # No synthetic c2 at load — only the real c1 result.
        assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "system", "user"]
        assert msgs[2]["tool_call_id"] == "c1" and msgs[2].get("is_error") is not True


_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xfc\xcf"
    b"\xc0\xc0\xc0\x00\x00\x00\x05\x00\x01\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
)


class TestRoleAgnosticAttachments:
    """``attachments_by_msg`` (keyed by row id) rebuilds multipart content for
    BOTH user and tool rows — the latter is how persisted tool vision output
    (read_file on an image) survives reload."""

    def test_user_row_multipart(self):
        urow = _row("user", "look")
        atts = {
            urow[0]: [
                {
                    "attachment_id": "i1",
                    "kind": "image",
                    "mime_type": "image/png",
                    "filename": "x.png",
                    "content": _PNG,
                }
            ]
        }
        msgs = reconstruct_messages([urow], "ws1", atts)
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"][0] == {"type": "text", "text": "look"}
        assert msgs[0]["content"][1]["type"] == "image_url"
        assert msgs[0]["_attachments_meta"][0]["filename"] == "x.png"

    def test_tool_row_multipart_image(self):
        tc = json.dumps([{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}])
        arow = _row("assistant", None, tool_calls=tc)
        trow = _row("tool", "Image file: dog.png", tc_id="c1")
        atts = {
            trow[0]: [
                {
                    "attachment_id": "i1",
                    "kind": "image",
                    "mime_type": "image/png",
                    "filename": "dog.png",
                    "content": _PNG,
                }
            ]
        }
        msgs = reconstruct_messages([arow, trow], "ws1", atts)
        tool_msg = next(m for m in msgs if m["role"] == "tool")
        assert isinstance(tool_msg["content"], list)
        assert tool_msg["content"][0] == {"type": "text", "text": "Image file: dog.png"}
        assert tool_msg["content"][1]["type"] == "image_url"
        # Tool rows do NOT carry _attachments_meta (that's a user-display sibling).
        assert "_attachments_meta" not in tool_msg

    def test_tool_row_without_attachments_stays_string(self):
        trow = _row("tool", "plain", tc_id="c1")
        msgs = reconstruct_messages([trow], "ws1", None, repair=False)
        assert msgs[0]["content"] == "plain"
