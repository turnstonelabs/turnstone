"""Tests for turnstone.core.session — ChatSession construction."""

import json
from unittest.mock import MagicMock, patch

from turnstone.core.session import ChatSession


class NullUI:
    """UI adapter that discards all output. Used for testing."""

    def on_thinking_start(self):
        pass

    def on_thinking_stop(self):
        pass

    def on_reasoning_token(self, text):
        pass

    def on_content_token(self, text):
        pass

    def on_stream_end(self):
        pass

    def approve_tools(self, items):
        return True, None

    def on_tool_result(self, call_id, name, output):
        pass

    def on_tool_output_chunk(self, call_id, chunk):
        pass

    def on_status(self, usage, context_window, effort):
        pass

    def on_plan_review(self, content):
        return ""

    def on_info(self, message):
        pass

    def on_error(self, message):
        pass

    def on_state_change(self, state):
        pass

    def on_rename(self, name):
        pass


def _make_session(
    mock_openai_client=None,
    instructions=None,
    **kwargs,
):
    """Helper to construct a ChatSession with minimal setup."""
    client = mock_openai_client or MagicMock()
    defaults = dict(
        client=client,
        model="test-model",
        ui=NullUI(),
        instructions=instructions,
        temperature=0.5,
        max_tokens=4096,
        tool_timeout=30,
    )
    defaults.update(kwargs)
    return ChatSession(**defaults)


class TestChatSessionConstruction:
    def test_system_messages_created(self, tmp_db):
        session = _make_session()
        assert len(session.system_messages) >= 1
        # At least one system message
        roles = [m["role"] for m in session.system_messages]
        assert "system" in roles

    def test_instructions_appended_to_system_message(self, tmp_db):
        session = _make_session(instructions="Always be concise.")
        sys_msgs = [m for m in session.system_messages if m["role"] == "system"]
        assert len(sys_msgs) >= 1
        assert "Always be concise." in sys_msgs[0]["content"]

    def test_full_messages_returns_system_plus_conversation(self, tmp_db):
        session = _make_session()
        # Initially no conversation messages
        full = session._full_messages()
        assert len(full) == len(session.system_messages)

        # Add a user message
        session.messages.append({"role": "user", "content": "hello"})
        full = session._full_messages()
        assert len(full) == len(session.system_messages) + 1
        assert full[-1]["role"] == "user"

    def test_msg_char_count_content_only(self, tmp_db):
        session = _make_session()
        msg = {"role": "assistant", "content": "hello world"}
        assert session._msg_char_count(msg) == 11

    def test_msg_char_count_with_tool_calls(self, tmp_db):
        session = _make_session()
        msg = {
            "role": "assistant",
            "content": "hi",
            "tool_calls": [
                {
                    "id": "tc_1",
                    "function": {
                        "name": "bash",
                        "arguments": '{"command": "ls"}',
                    },
                }
            ],
        }
        # "hi" (2) + "bash" (4) + '{"command": "ls"}' (17) = 23
        assert session._msg_char_count(msg) == 23

    def test_msg_char_count_none_content(self, tmp_db):
        session = _make_session()
        msg = {"role": "assistant", "content": None}
        assert session._msg_char_count(msg) == 0

    def test_reasoning_effort_stored(self, tmp_db):
        session = _make_session(reasoning_effort="high")
        assert session.reasoning_effort == "high"

    def test_default_reasoning_effort(self, tmp_db):
        session = _make_session()
        assert session.reasoning_effort == "medium"


# ---------------------------------------------------------------------------
# Tests — _exec_plan (session-scoped plan files + existing-plan re-read)
# ---------------------------------------------------------------------------


class TestPlanExec:
    """Tests for _exec_plan: unique session-scoped plan file and existing-plan injection."""

    def _run_plan(self, session, prompt, agent_return="# Plan\n\nDo the thing."):
        """Invoke _exec_plan with _run_agent patched to avoid LLM calls.

        Returns (call_id_returned, content_returned, captured_messages) where
        captured_messages is the agent_messages list passed to _run_agent.
        """
        captured = {}

        def fake_run_agent(messages, **kwargs):
            captured["messages"] = list(messages)
            return agent_return

        item = {"call_id": "test-call-1", "prompt": prompt}
        with patch.object(session, "_run_agent", side_effect=fake_run_agent):
            call_id, content = session._exec_plan(item)

        return call_id, content, captured.get("messages", [])

    def test_plan_file_uses_ws_id(self, tmp_db, tmp_path, monkeypatch):
        """Plan file is named .plan-<ws_id>.md, not .plan.md."""
        monkeypatch.chdir(tmp_path)
        session = _make_session()
        self._run_plan(session, "add feature")
        expected = tmp_path / f".plan-{session._ws_id}.md"
        assert expected.exists(), f"Expected {expected} to be created"
        assert not (tmp_path / ".plan.md").exists()

    def test_plan_file_contains_agent_output(self, tmp_db, tmp_path, monkeypatch):
        """Written plan file contains the agent's output verbatim."""
        monkeypatch.chdir(tmp_path)
        session = _make_session()
        plan_content = "## Goal\n\nAdd a new endpoint."
        self._run_plan(session, "add endpoint", agent_return=plan_content)
        plan_file = tmp_path / f".plan-{session._ws_id}.md"
        assert plan_file.read_text() == plan_content

    def test_two_sessions_produce_different_files(self, tmp_db, tmp_path, monkeypatch):
        """Two ChatSession instances never collide on the same plan file."""
        monkeypatch.chdir(tmp_path)
        s1 = _make_session()
        s2 = _make_session()
        assert s1._ws_id != s2._ws_id
        self._run_plan(s1, "feature A")
        self._run_plan(s2, "feature B")
        files = list(tmp_path.glob(".plan-*.md"))
        assert len(files) == 2

    def _seed_prior_plan(self, session, prior_prompt, prior_content):
        """Simulate a completed plan tool call in session.messages."""
        tc_id = "call_prior_plan"
        session.messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": tc_id,
                        "type": "function",
                        "function": {
                            "name": "plan",
                            "arguments": json.dumps({"prompt": prior_prompt}),
                        },
                    }
                ],
            }
        )
        session.messages.append(
            {
                "role": "tool",
                "tool_call_id": tc_id,
                "content": prior_content,
            }
        )

    def test_no_prior_plan_no_extra_messages(self, tmp_db, tmp_path, monkeypatch):
        """First invocation: no prior plan in history, agent gets no tool pair."""
        monkeypatch.chdir(tmp_path)
        session = _make_session()
        _, _, messages = self._run_plan(session, "build something")
        roles = [m["role"] for m in messages]
        assert "tool" not in roles

    def test_prior_plan_from_messages_injected(self, tmp_db, tmp_path, monkeypatch):
        """Second invocation: prior plan from session.messages arrives as real tool result."""
        monkeypatch.chdir(tmp_path)
        session = _make_session()
        self._seed_prior_plan(session, "build feature X", "## Goal\n\nOriginal plan.")

        _, _, messages = self._run_plan(session, "also handle edge case Y")

        # The real assistant tool_calls message is forwarded
        assistant_with_tc = [
            m for m in messages if m["role"] == "assistant" and m.get("tool_calls")
        ]
        assert len(assistant_with_tc) == 1
        assert assistant_with_tc[0]["tool_calls"][0]["function"]["name"] == "plan"

        # The real tool result is forwarded with its original content
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert "Original plan." in tool_msgs[0]["content"]

    def test_prior_plan_appears_before_user_prompt(self, tmp_db, tmp_path, monkeypatch):
        """The prior plan tool pair appears before the new user prompt."""
        monkeypatch.chdir(tmp_path)
        session = _make_session()
        self._seed_prior_plan(session, "original", "Old plan.")

        _, _, messages = self._run_plan(session, "refinement prompt")

        tool_idx = next(i for i, m in enumerate(messages) if m["role"] == "tool")
        user_idx = next(i for i, m in enumerate(messages) if m["role"] == "user")
        assert tool_idx < user_idx

    def test_exec_plan_returns_content(self, tmp_db, tmp_path, monkeypatch):
        """_exec_plan returns (call_id, agent_output)."""
        monkeypatch.chdir(tmp_path)
        session = _make_session()
        agent_output = "## Goal\n\nBuild it."
        call_id, content, _ = self._run_plan(session, "do stuff", agent_return=agent_output)
        assert call_id == "test-call-1"
        assert content == agent_output
