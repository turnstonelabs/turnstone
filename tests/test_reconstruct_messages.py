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
):
    """Build a 7-element conversation row tuple (id, role, ...)."""
    return (next(_row_ids), role, content, tool_name, tc_id, pdata, tool_calls)


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
        rows = [
            _row("user", "hi"),
            _row("system", "you are helpful"),
            _row("assistant", "hello"),
        ]
        msgs = reconstruct_messages(rows, "ws1")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"


class TestMidConversationOrphanRepair:
    """Mid-conversation orphaned tool_calls get synthetic tool results."""

    def test_all_orphaned_mid_conversation(self):
        """Assistant has 2 tool_calls, no tool results, then user message."""
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
        # Should have: user, assistant, tool(c1), tool(c2), user
        assert len(msgs) == 5
        assert msgs[2]["role"] == "tool"
        assert msgs[2]["tool_call_id"] == "c1"
        assert msgs[2]["is_error"] is True
        assert msgs[3]["role"] == "tool"
        assert msgs[3]["tool_call_id"] == "c2"
        assert msgs[4]["role"] == "user"

    def test_partial_results_mid_conversation(self):
        """2 tool_calls, 1 result present, 1 missing — synthesize only the missing one."""
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
        # Should have: user, assistant, tool(c1 real), tool(c2 synthetic), user
        assert len(msgs) == 5
        assert msgs[2]["role"] == "tool"
        assert msgs[2]["tool_call_id"] == "c1"
        assert msgs[2]["content"] == "file1.txt"
        assert msgs[2].get("is_error") is not True
        assert msgs[3]["role"] == "tool"
        assert msgs[3]["tool_call_id"] == "c2"
        assert msgs[3]["is_error"] is True
        assert msgs[4]["role"] == "user"

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
