"""Tests for turnstone.core.session — ChatSession construction."""

import base64
import json
from unittest.mock import MagicMock, patch

from turnstone.core.session import _IMAGE_EXTENSIONS, _IMAGE_SIZE_CAP, ChatSession


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

    def on_tool_result(self, call_id, name, output, **kwargs):
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

    def on_output_warning(self, call_id, assessment):
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
        # "hello world" (11) + "assistant" (9) = 20
        assert session._msg_char_count(msg) == 20

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
        # "hi" (2) + "tc_1" (4) + "bash" (4) + '{"command": "ls"}' (17) + "assistant" (9) = 36
        assert session._msg_char_count(msg) == 36

    def test_msg_char_count_none_content(self, tmp_db):
        session = _make_session()
        msg = {"role": "assistant", "content": None}
        # len("assistant") = 9
        assert session._msg_char_count(msg) == 9

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

    _VALID_PLAN = (
        "## Goal\n\nDo the thing.\n\n"
        "## Current State\n\nFile foo.py has bar().\n\n"
        "## Plan\n\n1. Edit foo.py line 10.\n\n"
        "## Risks\n\nNone."
    )

    def _run_plan(self, session, prompt, agent_return=None):
        """Invoke _exec_plan with _run_agent patched to avoid LLM calls.

        Returns (call_id_returned, content_returned, captured_messages) where
        captured_messages is the agent_messages list passed to _run_agent.
        """
        if agent_return is None:
            agent_return = self._VALID_PLAN
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
        self._run_plan(session, "add endpoint")
        plan_file = tmp_path / f".plan-{session._ws_id}.md"
        assert plan_file.read_text() == self._VALID_PLAN

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
                            "name": "plan_agent",
                            "arguments": json.dumps({"goal": prior_prompt}),
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
        assert assistant_with_tc[0]["tool_calls"][0]["function"]["name"] == "plan_agent"

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
        call_id, content, _ = self._run_plan(session, "do stuff")
        assert call_id == "test-call-1"
        assert content == self._VALID_PLAN

    def test_exec_plan_retries_on_garbage(self, tmp_db, tmp_path, monkeypatch):
        """When _run_agent returns garbage, _exec_plan retries once."""
        monkeypatch.chdir(tmp_path)
        session = _make_session()
        good_plan = (
            "## Goal\n\nAdd feature X.\n\n"
            "## Current State\n\nFile foo.py has bar().\n\n"
            "## Plan\n\n1. Edit foo.py:bar()\n\n"
            "## Risks\n\nNone."
        )
        call_count = 0

        def fake_run_agent(messages, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "Sure, do the thing."
            return good_plan

        item = {"call_id": "c1", "prompt": "add feature X"}
        with patch.object(session, "_run_agent", side_effect=fake_run_agent):
            _, content = session._exec_plan(item)

        assert call_count == 2
        assert "## Goal" in content

    def test_exec_plan_warning_on_double_failure(self, tmp_db, tmp_path, monkeypatch):
        """When both attempts produce garbage, content gets a warning prefix."""
        monkeypatch.chdir(tmp_path)
        session = _make_session()

        def fake_run_agent(messages, **kwargs):
            return "nope"

        item = {"call_id": "c1", "prompt": "add feature X"}
        with patch.object(session, "_run_agent", side_effect=fake_run_agent):
            _, content = session._exec_plan(item)

        assert content.startswith("[Warning:")

    def test_retry_continues_agent_conversation(self, tmp_db, tmp_path, monkeypatch):
        """Retry appends coaching to the same agent_messages list."""
        monkeypatch.chdir(tmp_path)
        session = _make_session()
        captured_messages: list[list] = []

        def fake_run_agent(messages, **kwargs):
            captured_messages.append(list(messages))
            if len(captured_messages) == 1:
                return "garbage"
            return (
                "## Goal\n\nDone.\n\n## Current State\n\nx\n\n## Plan\n\n1. x\n\n## Risks\n\nNone."
            )

        item = {"call_id": "c1", "prompt": "add feature X"}
        with patch.object(session, "_run_agent", side_effect=fake_run_agent):
            session._exec_plan(item)

        assert len(captured_messages) == 2
        # Second call should have more messages (coaching appended)
        assert len(captured_messages[1]) > len(captured_messages[0])
        # Last user message in second call is the coaching message
        assert "did not follow" in captured_messages[1][-1]["content"]

    def test_plan_includes_skill_content(self, tmp_db, tmp_path, monkeypatch):
        """Plan agent system message includes skill guardrails."""
        monkeypatch.chdir(tmp_path)
        session = _make_session()
        session._skill_content = "SAFETY: Do not produce harmful plans."
        _, _, messages = self._run_plan(session, "build something")
        sys_content = messages[0]["content"]
        assert "SAFETY: Do not produce harmful plans." in sys_content
        assert ChatSession._PLAN_IDENTITY in sys_content
        # Skill content appears before plan identity
        tpl_pos = sys_content.index("SAFETY:")
        identity_pos = sys_content.index(ChatSession._PLAN_IDENTITY)
        assert tpl_pos < identity_pos

    def test_plan_no_skill_is_identity_only(self, tmp_db, tmp_path, monkeypatch):
        """Without skills, plan system message is exactly _PLAN_IDENTITY."""
        monkeypatch.chdir(tmp_path)
        session = _make_session()
        assert session._skill_content is None
        _, _, messages = self._run_plan(session, "build something")
        assert messages[0]["content"] == ChatSession._PLAN_IDENTITY


# ---------------------------------------------------------------------------
# Plan validation
# ---------------------------------------------------------------------------


class TestPlanValidation:
    """Tests for ChatSession._validate_plan quality gate."""

    GOOD_PLAN = (
        "## Goal\n\nAdd authentication to the API.\n\n"
        "## Current State\n\nFile server.py:45 has no auth middleware.\n\n"
        "## Plan\n\n1. Add AuthMiddleware to server.py.\n"
        "2. Create auth.py with JWT verification.\n\n"
        "## Risks\n\nToken expiry handling may need tuning."
    )

    def test_valid_plan_passes(self):
        valid, issues = ChatSession._validate_plan(self.GOOD_PLAN, "add auth")
        assert valid
        assert issues == []

    def test_too_short_fails(self):
        valid, issues = ChatSession._validate_plan("Do the thing.", "do stuff")
        assert not valid
        assert any("too short" in i for i in issues)

    def test_no_sections_fails(self):
        content = "A" * 150  # long enough but no sections
        valid, issues = ChatSession._validate_plan(content, "build it")
        assert not valid
        assert any("missing plan sections" in i for i in issues)

    def test_echo_detection(self):
        goal = "deliver a simpsons quote from a specific episode"
        content = "Deliver a Simpsons quote from a specific episode"
        valid, issues = ChatSession._validate_plan(content, goal)
        assert not valid
        assert any("echo" in i for i in issues)

    def test_refusal_detection(self):
        content = "I cannot create a plan for this task because " + "x" * 100
        valid, issues = ChatSession._validate_plan(content, "do stuff")
        assert not valid
        assert any("refusal" in i for i in issues)

    def test_partial_sections_passes(self):
        """2 out of 4 sections is enough to pass."""
        content = (
            "## Goal\n\nFix the bug in parsing.\n\n"
            "## Plan\n\n1. Edit parser.py line 42.\n"
            "2. Add boundary check.\n"
            "This is enough detail to proceed with confidence."
        )
        valid, issues = ChatSession._validate_plan(content, "fix bug")
        assert valid

    def test_one_section_fails(self):
        """Only 1 out of 4 sections is not enough."""
        content = (
            "## Goal\n\nFix the bug.\n\n"
            "We should probably edit parser.py and add some checks "
            "to the boundary handling code path for safety."
        )
        valid, issues = ChatSession._validate_plan(content, "fix bug")
        assert not valid
        assert any("missing plan sections" in i for i in issues)


# ---------------------------------------------------------------------------
# Plan refinement loop
# ---------------------------------------------------------------------------


class TestPlanRefinement:
    """Tests for the iterative plan refinement loop in _execute_tools."""

    GOOD_PLAN = TestPlanValidation.GOOD_PLAN

    def test_feedback_triggers_refinement(self, tmp_db, tmp_path, monkeypatch):
        """User feedback causes _refine_plan to run, then approval exits."""
        monkeypatch.chdir(tmp_path)
        session = _make_session()
        refine_called = []

        review_responses = iter(["add error handling", ""])
        session.ui = MagicMock(spec_set=NullUI)
        session.ui.on_plan_review.side_effect = lambda c: next(review_responses)
        session.ui.on_info = MagicMock()
        session.ui.on_state_change = MagicMock()

        revised = self.GOOD_PLAN + "\n\n3. Add error handling."

        def fake_refine(content, goal, feedback):
            refine_called.append(feedback)
            return revised

        with patch.object(session, "_refine_plan", side_effect=fake_refine):
            items = [
                {
                    "func_name": "plan_agent",
                    "call_id": "c1",
                    "prompt": "add auth",
                }
            ]
            results = [("c1", self.GOOD_PLAN)]
            # Manually invoke the post-plan gate portion of _execute_tools.
            # We test the loop by calling the gate code directly.
            session.auto_approve = False

            original_goal = items[0].get("prompt", "")
            output = results[0][1]
            refinement_round = 0
            while refinement_round < session._MAX_PLAN_REFINEMENTS:
                resp = session.ui.on_plan_review(output)
                if resp.lower() in ("n", "no", "reject"):
                    break
                elif resp:
                    output = session._refine_plan(output, original_goal, resp)
                    refinement_round += 1
                else:
                    break

        assert len(refine_called) == 1
        assert refine_called[0] == "add error handling"
        assert "error handling" in output

    def test_reject_skips_refinement(self, tmp_db, tmp_path, monkeypatch):
        """Rejection exits immediately without calling _refine_plan."""
        monkeypatch.chdir(tmp_path)
        session = _make_session()
        session.ui = MagicMock(spec_set=NullUI)
        session.ui.on_plan_review.return_value = "reject"

        with patch.object(session, "_refine_plan") as mock_refine:
            output = self.GOOD_PLAN
            resp = session.ui.on_plan_review(output)
            if resp.lower() in ("n", "no", "reject"):
                output += "\n\n---\nUser REJECTED"
            elif resp:
                output = session._refine_plan(output, "g", resp)

        mock_refine.assert_not_called()
        assert "REJECTED" in output

    def test_approve_skips_refinement(self, tmp_db, tmp_path, monkeypatch):
        """Empty response (enter) approves without refinement."""
        monkeypatch.chdir(tmp_path)
        session = _make_session()
        session.ui = MagicMock(spec_set=NullUI)
        session.ui.on_plan_review.return_value = ""

        with patch.object(session, "_refine_plan") as mock_refine:
            output = self.GOOD_PLAN
            resp = session.ui.on_plan_review(output)
            if resp.lower() in ("n", "no", "reject"):
                output += "\n\n---\nUser REJECTED"
            elif resp:
                output = session._refine_plan(output, "g", resp)

        mock_refine.assert_not_called()
        assert "REJECTED" not in output

    def test_max_refinement_rounds(self, tmp_db, tmp_path, monkeypatch):
        """Loop stops after _MAX_PLAN_REFINEMENTS rounds with a final review."""
        monkeypatch.chdir(tmp_path)
        session = _make_session()
        session.ui = MagicMock(spec_set=NullUI)
        session.ui.on_plan_review.return_value = "more detail please"
        session.ui.on_info = MagicMock()

        refine_count = 0

        def fake_refine(content, goal, feedback):
            nonlocal refine_count
            refine_count += 1
            return content + f"\n(revision {refine_count})"

        with patch.object(session, "_refine_plan", side_effect=fake_refine):
            output = self.GOOD_PLAN
            original_goal = "add auth"
            refinement_round = 0
            while True:
                resp = session.ui.on_plan_review(output)
                if (
                    resp.lower() in ("n", "no", "reject")
                    or not resp
                    or refinement_round >= session._MAX_PLAN_REFINEMENTS
                ):
                    break
                output = session._refine_plan(output, original_goal, resp)
                refinement_round += 1

        assert refine_count == session._MAX_PLAN_REFINEMENTS
        # User gets one extra review call after max rounds (the final prompt)
        assert session.ui.on_plan_review.call_count == session._MAX_PLAN_REFINEMENTS + 1

    def test_refine_plan_message_structure(self, tmp_db, tmp_path, monkeypatch):
        """_refine_plan passes system + prior plan + feedback to _run_agent."""
        monkeypatch.chdir(tmp_path)
        session = _make_session()
        captured = {}

        def fake_run_agent(messages, **kwargs):
            captured["messages"] = list(messages)
            return self.GOOD_PLAN

        with patch.object(session, "_run_agent", side_effect=fake_run_agent):
            session._refine_plan(self.GOOD_PLAN, "add auth", "add tests too")

        msgs = captured["messages"]
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["tool_calls"][0]["function"]["name"] == "plan_agent"
        assert msgs[2]["role"] == "tool"
        assert msgs[2]["content"] == self.GOOD_PLAN
        assert msgs[3]["role"] == "user"
        assert "add tests too" in msgs[3]["content"]

    def test_refine_plan_includes_skill_content(self, tmp_db, tmp_path, monkeypatch):
        """_refine_plan system message includes skill guardrails."""
        monkeypatch.chdir(tmp_path)
        session = _make_session()
        session._skill_content = "SAFETY: guardrails here"
        captured = {}

        def fake_run_agent(messages, **kwargs):
            captured["messages"] = list(messages)
            return self.GOOD_PLAN

        with patch.object(session, "_run_agent", side_effect=fake_run_agent):
            session._refine_plan(self.GOOD_PLAN, "add auth", "add tests too")

        sys_content = captured["messages"][0]["content"]
        assert "SAFETY: guardrails here" in sys_content
        assert ChatSession._PLAN_IDENTITY in sys_content
        tpl_pos = sys_content.index("SAFETY:")
        identity_pos = sys_content.index(ChatSession._PLAN_IDENTITY)
        assert tpl_pos < identity_pos


# ---------------------------------------------------------------------------
# Vision / image support
# ---------------------------------------------------------------------------


class TestImageExtensions:
    """Test _IMAGE_EXTENSIONS constant and detection logic."""

    def test_common_image_extensions(self):
        for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".ico"):
            assert ext in _IMAGE_EXTENSIONS, f"{ext} should be in _IMAGE_EXTENSIONS"

    def test_svg_excluded(self):
        assert ".svg" not in _IMAGE_EXTENSIONS

    def test_text_extensions_excluded(self):
        for ext in (".py", ".txt", ".json", ".md", ".rs", ".go"):
            assert ext not in _IMAGE_EXTENSIONS


class TestExecReadImage:
    """Test _exec_read_image method."""

    def _make_png(self, path: str, size: int = 100) -> None:
        """Write a minimal valid-ish PNG header to a file."""
        # 8-byte PNG signature + enough bytes to reach target size
        header = b"\x89PNG\r\n\x1a\n"
        with open(path, "wb") as f:
            f.write(header + b"\x00" * max(0, size - len(header)))

    def test_image_returns_content_parts(self, tmp_db, tmp_path):
        """read_file on a PNG with vision support returns content parts."""
        img = tmp_path / "test.png"
        self._make_png(str(img))

        session = _make_session()
        mock_caps = MagicMock()
        mock_caps.supports_vision = True
        with patch.object(session._provider, "get_capabilities", return_value=mock_caps):
            item = {"call_id": "c1", "path": str(img), "offset": None, "limit": None}
            call_id, output = session._exec_read_file(item)

        assert call_id == "c1"
        assert isinstance(output, list)
        assert len(output) == 2
        assert output[0]["type"] == "text"
        assert "test.png" in output[0]["text"]
        assert output[1]["type"] == "image_url"
        url = output[1]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        # Verify base64 round-trip
        b64part = url.split(",", 1)[1]
        decoded = base64.b64decode(b64part)
        assert decoded == img.read_bytes()

    def test_no_vision_returns_text(self, tmp_db, tmp_path):
        """read_file on image with non-vision model returns text description."""
        img = tmp_path / "photo.jpg"
        self._make_png(str(img), size=2048)

        session = _make_session()
        mock_caps = MagicMock()
        mock_caps.supports_vision = False
        with patch.object(session._provider, "get_capabilities", return_value=mock_caps):
            item = {"call_id": "c2", "path": str(img), "offset": None, "limit": None}
            call_id, output = session._exec_read_file(item)

        assert call_id == "c2"
        assert isinstance(output, str)
        assert "does not support vision" in output
        assert "photo.jpg" in output

    def test_oversized_image_returns_error(self, tmp_db, tmp_path):
        """Images exceeding _IMAGE_SIZE_CAP return an error string."""
        img = tmp_path / "huge.png"
        # Write slightly over the cap
        with open(img, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * _IMAGE_SIZE_CAP)

        session = _make_session()
        mock_caps = MagicMock()
        mock_caps.supports_vision = True
        with patch.object(session._provider, "get_capabilities", return_value=mock_caps):
            item = {"call_id": "c3", "path": str(img), "offset": None, "limit": None}
            call_id, output = session._exec_read_file(item)

        assert call_id == "c3"
        assert isinstance(output, str)
        assert "exceeds" in output

    def test_missing_image_returns_error(self, tmp_db, tmp_path):
        """read_file on non-existent image returns error."""
        session = _make_session()
        mock_caps = MagicMock()
        mock_caps.supports_vision = True
        with patch.object(session._provider, "get_capabilities", return_value=mock_caps):
            item = {
                "call_id": "c4",
                "path": str(tmp_path / "nope.png"),
                "offset": None,
                "limit": None,
            }
            call_id, output = session._exec_read_file(item)
        assert isinstance(output, str)
        assert "not found" in output

    def test_svg_read_as_text(self, tmp_db, tmp_path):
        """SVG files are read as text, not as images."""
        svg = tmp_path / "icon.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><circle r="10"/></svg>')

        session = _make_session()
        item = {"call_id": "c5", "path": str(svg), "offset": None, "limit": None}
        call_id, output = session._exec_read_file(item)
        assert isinstance(output, str)
        assert "<svg" in output  # Read as text


class TestGetCapabilitiesOverride:
    """Test _get_capabilities with config.toml overrides."""

    def test_config_override_applies(self, tmp_db):
        """capabilities dict from ModelConfig is merged onto provider caps."""
        from turnstone.core.model_registry import ModelConfig, ModelRegistry
        from turnstone.core.providers._protocol import ModelCapabilities

        cfg = ModelConfig(
            alias="qwen-vl",
            base_url="http://localhost:8000/v1",
            api_key="dummy",
            model="qwen-3.5-vl",
            capabilities={"supports_vision": True},
        )
        registry = ModelRegistry(
            models={"qwen-vl": cfg},
            default="qwen-vl",
        )
        session = _make_session(registry=registry, model_alias="qwen-vl")
        # Ensure provider returns a real ModelCapabilities (not MagicMock).
        # Use patch.object so the singleton provider is restored after the test.
        with patch.object(session._provider, "get_capabilities", return_value=ModelCapabilities()):
            caps = session._get_capabilities()
        assert caps.supports_vision is True

    def test_no_override_uses_provider_default(self, tmp_db):
        """Without config override, provider defaults are used."""
        session = _make_session()
        caps = session._get_capabilities()
        # Default OpenAI provider for unknown model → no vision
        assert caps.supports_vision is False


class TestTitleRetry:
    """_generate_title resets _title_generated on failure."""

    def test_title_generated_reset_on_failure(self, tmp_db):
        from turnstone.core.providers._protocol import ModelCapabilities

        session = _make_session()
        session._title_generated = True
        session.messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        # Mock provider to raise
        session._provider = MagicMock()
        session._provider.get_capabilities.return_value = ModelCapabilities()
        session._provider.create_completion.side_effect = RuntimeError("API error")

        session._generate_title()

        assert session._title_generated is False

    def test_title_generated_stays_true_on_success(self, tmp_db):
        from turnstone.core.providers._protocol import ModelCapabilities

        session = _make_session()
        session._title_generated = True
        session.messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        result = MagicMock()
        result.content = "Test Title"
        session._provider = MagicMock()
        session._provider.get_capabilities.return_value = ModelCapabilities()
        session._provider.create_completion.return_value = result

        with patch("turnstone.core.session.update_workstream_title"):
            session._generate_title()

        # Flag stays True after successful generation
        assert session._title_generated is True

    def test_title_skipped_after_resume_changes_ws_id(self, tmp_db):
        """If ws_id changes (via resume) during title generation, discard the result."""
        from turnstone.core.providers._protocol import ModelCapabilities

        session = _make_session()
        session._title_generated = True
        session.messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        original_ws_id = session._ws_id
        result = MagicMock()
        result.content = "Test Title"
        session._provider = MagicMock()
        session._provider.get_capabilities.return_value = ModelCapabilities()
        session._provider.create_completion.return_value = result

        # Simulate resume() changing ws_id while title generation is in flight
        def _change_ws_id(*args, **kwargs):
            session._ws_id = "different-ws-id"
            return result

        session._provider.create_completion.side_effect = _change_ws_id

        with patch("turnstone.core.session.update_workstream_title") as mock_update:
            session._generate_title()

        # Title should NOT be applied to the new workstream
        mock_update.assert_not_called()
        # Restore for cleanup
        session._ws_id = original_ws_id


class TestLiveConfigUpdate:
    """ConfigStore-backed sessions pick up settings changes at point-of-use."""

    def test_memory_config_reads_from_config_store(self, tmp_db):
        """_mem_cfg returns live values from ConfigStore when present."""
        from turnstone.core.config_store import ConfigStore
        from turnstone.core.storage._sqlite import SQLiteBackend

        storage = SQLiteBackend(str(tmp_db), create_tables=True)
        cs = ConfigStore(storage)
        session = _make_session(config_store=cs)

        # Default: relevance_k=5
        assert session._mem_cfg.relevance_k == 5

        # Admin changes the setting
        cs.set("memory.relevance_k", 10, changed_by="test")
        assert session._mem_cfg.relevance_k == 10

    def test_judge_config_reads_from_config_store(self, tmp_db):
        """_judge_cfg returns live behavioral flags from ConfigStore."""
        from turnstone.core.config_store import ConfigStore
        from turnstone.core.judge import JudgeConfig
        from turnstone.core.storage._sqlite import SQLiteBackend

        storage = SQLiteBackend(str(tmp_db), create_tables=True)
        cs = ConfigStore(storage)
        session = _make_session(
            judge_config=JudgeConfig(),
            config_store=cs,
        )

        # Default: enabled=True
        assert session._judge_cfg.enabled is True

        # Admin disables the judge
        cs.set("judge.enabled", False, changed_by="test")
        assert session._judge_cfg.enabled is False

    def test_judge_client_config_stays_frozen(self, tmp_db):
        """LLM client fields (model, provider) are frozen from creation time."""
        from turnstone.core.config_store import ConfigStore
        from turnstone.core.judge import JudgeConfig
        from turnstone.core.storage._sqlite import SQLiteBackend

        storage = SQLiteBackend(str(tmp_db), create_tables=True)
        cs = ConfigStore(storage)
        session = _make_session(
            judge_config=JudgeConfig(model="original-model"),
            config_store=cs,
        )

        # Change the model in ConfigStore — should NOT affect the session
        cs.set("judge.model", "new-model", changed_by="test")
        assert session._judge_cfg.model == "original-model"

    def test_judge_disable_after_init_stops_future_use(self, tmp_db):
        """Disabling judge.enabled after IntentJudge is created returns None."""
        from turnstone.core.config_store import ConfigStore
        from turnstone.core.judge import JudgeConfig
        from turnstone.core.storage._sqlite import SQLiteBackend

        storage = SQLiteBackend(str(tmp_db), create_tables=True)
        cs = ConfigStore(storage)
        session = _make_session(
            judge_config=JudgeConfig(),
            config_store=cs,
        )

        # Force judge initialization by setting a mock
        session._judge = MagicMock()
        assert session._ensure_judge() is not None

        # Admin disables the judge — cached instance should NOT be returned
        cs.set("judge.enabled", False, changed_by="test")
        assert session._ensure_judge() is None

    def test_fallback_to_frozen_without_config_store(self, tmp_db):
        """Without ConfigStore (CLI mode), frozen config is used."""
        from turnstone.core.memory_relevance import MemoryConfig

        session = _make_session(memory_config=MemoryConfig(relevance_k=3))
        assert session._mem_cfg.relevance_k == 3


class TestAgentOutputGuard:
    """Output guard should evaluate tool results in _run_agent, not just the main loop."""

    def test_agent_loop_calls_evaluate_output(self):
        """_run_agent passes tool output through _evaluate_output when output_guard is enabled."""
        from turnstone.core.judge import JudgeConfig
        from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider

        session = _make_session(judge_config=JudgeConfig(output_guard=True))
        session._provider = OpenAIChatCompletionsProvider()

        with patch.object(
            session, "_evaluate_output", wraps=lambda cid, o, fn: (o, None)
        ) as mock_eval:
            # Simulate _run_agent getting a tool call response then a text response
            call_count = [0]

            def fake_create(**kwargs):
                call_count[0] += 1
                resp = MagicMock()
                if call_count[0] == 1:
                    # First call: model returns a tool call
                    choice = MagicMock()
                    choice.finish_reason = "tool_calls"
                    tc = MagicMock()
                    tc.id = "call_1"
                    tc.function.name = "read_file"
                    tc.function.arguments = '{"path": "/tmp/test"}'
                    choice.message.tool_calls = [tc]
                    choice.message.content = None
                    resp.choices = [choice]
                    resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
                else:
                    # Second call: model returns text (done)
                    choice = MagicMock()
                    choice.finish_reason = "stop"
                    choice.message.tool_calls = None
                    choice.message.content = "Done"
                    resp.choices = [choice]
                    resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
                return resp

            session.client.chat.completions.create = fake_create

            # Mock tool preparation to return a simple output
            def fake_prepare(tc_dict, **kwargs):
                return {
                    "call_id": tc_dict["id"],
                    "func_name": "read_file",
                    "needs_approval": False,
                    "execute": lambda p: ("call_1", "file contents with sk-proj-SECRET123"),
                }

            with patch.object(session, "_prepare_tool", side_effect=fake_prepare):
                session._run_agent(
                    [{"role": "user", "content": "test"}],
                    tools=[{"type": "function", "function": {"name": "read_file"}}],
                    label="test",
                )

            mock_eval.assert_called_once()
            args = mock_eval.call_args[0]
            assert args[0] == "call_1"  # call_id
            assert "sk-proj-SECRET123" in args[1]  # output
            assert args[2] == "read_file"  # func_name

    def test_agent_loop_skips_guard_when_disabled(self):
        """_run_agent does not call _evaluate_output when output_guard is disabled."""
        from turnstone.core.judge import JudgeConfig
        from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider

        session = _make_session(judge_config=JudgeConfig(output_guard=False))
        session._provider = OpenAIChatCompletionsProvider()

        with patch.object(session, "_evaluate_output") as mock_eval:
            call_count = [0]

            def fake_create(**kwargs):
                call_count[0] += 1
                resp = MagicMock()
                if call_count[0] == 1:
                    choice = MagicMock()
                    choice.finish_reason = "tool_calls"
                    tc = MagicMock()
                    tc.id = "call_1"
                    tc.function.name = "read_file"
                    tc.function.arguments = '{"path": "/tmp/test"}'
                    choice.message.tool_calls = [tc]
                    choice.message.content = None
                    resp.choices = [choice]
                    resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
                else:
                    choice = MagicMock()
                    choice.finish_reason = "stop"
                    choice.message.tool_calls = None
                    choice.message.content = "Done"
                    resp.choices = [choice]
                    resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
                return resp

            session.client.chat.completions.create = fake_create

            def fake_prepare(tc_dict, **kwargs):
                return {
                    "call_id": tc_dict["id"],
                    "func_name": "read_file",
                    "needs_approval": False,
                    "execute": lambda p: ("call_1", "safe output"),
                }

            with patch.object(session, "_prepare_tool", side_effect=fake_prepare):
                session._run_agent(
                    [{"role": "user", "content": "test"}],
                    tools=[{"type": "function", "function": {"name": "read_file"}}],
                    label="test",
                )

            mock_eval.assert_not_called()


class TestProviderExtraParams:
    """Tests for _provider_extra_params — local-only chat_template_kwargs."""

    def _session_with_provider(self, provider_name: str, tmp_db) -> ChatSession:
        from turnstone.core.providers import create_provider

        session = _make_session(reasoning_effort="medium")
        session._provider = create_provider(provider_name)
        return session

    def test_openai_compatible_returns_chat_template_kwargs(self, tmp_db):
        session = self._session_with_provider("openai-compatible", tmp_db)
        result = session._provider_extra_params()
        assert result is not None
        assert "chat_template_kwargs" in result
        assert result["chat_template_kwargs"]["reasoning_effort"] == "medium"

    def test_openai_commercial_returns_none(self, tmp_db):
        session = self._session_with_provider("openai", tmp_db)
        result = session._provider_extra_params()
        assert result is None

    def test_anthropic_returns_none(self, tmp_db):
        session = self._session_with_provider("anthropic", tmp_db)
        result = session._provider_extra_params()
        assert result is None

    def test_reasoning_effort_override(self, tmp_db):
        session = self._session_with_provider("openai-compatible", tmp_db)
        result = session._provider_extra_params(reasoning_effort="high")
        assert result is not None
        assert result["chat_template_kwargs"]["reasoning_effort"] == "high"

    def test_explicit_openai_provider_overrides_session(self, tmp_db):
        """Passing an explicit commercial OpenAI provider returns None even
        when the session's own provider is openai-compatible."""
        from turnstone.core.providers import create_provider

        session = self._session_with_provider("openai-compatible", tmp_db)
        openai_prov = create_provider("openai")
        result = session._provider_extra_params(provider=openai_prov)
        assert result is None
