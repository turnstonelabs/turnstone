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
# Per-call model override on plan_agent / task_agent
# ---------------------------------------------------------------------------


class TestAgentModelOverride:
    """Tests for the optional `model` arg on plan_agent / task_agent tools."""

    @staticmethod
    def _registry():
        from turnstone.core.model_registry import ModelConfig, ModelRegistry

        return ModelRegistry(
            models={
                "default": ModelConfig("default", "x", "x", "m"),
                "smart": ModelConfig("smart", "x", "x", "m"),
                "fast": ModelConfig("fast", "x", "x", "m"),
            },
            default="default",
        )

    # ---- _prepare_plan ----

    def test_prepare_plan_extracts_model_override(self, tmp_db) -> None:
        session = _make_session(registry=self._registry(), model_alias="default")
        item = session._prepare_plan("c1", {"goal": "do x", "model": "smart"})
        assert item["model_override"] == "smart"
        assert "error" not in item

    def test_prepare_plan_missing_model_arg_means_no_override(self, tmp_db) -> None:
        session = _make_session(registry=self._registry(), model_alias="default")
        item = session._prepare_plan("c1", {"goal": "do x"})
        assert item["model_override"] is None

    def test_prepare_plan_empty_string_model_means_no_override(self, tmp_db) -> None:
        # LLMs sometimes echo "" rather than omit the field; treat as unset.
        session = _make_session(registry=self._registry(), model_alias="default")
        item = session._prepare_plan("c1", {"goal": "do x", "model": ""})
        assert item["model_override"] is None

    def test_prepare_plan_unknown_model_returns_error(self, tmp_db) -> None:
        session = _make_session(registry=self._registry(), model_alias="default")
        item = session._prepare_plan("c1", {"goal": "do x", "model": "bogus"})
        assert item.get("needs_approval") is False
        assert "error" in item
        assert "unknown model alias 'bogus'" in item["error"]
        # The error guidance must list the available aliases so the LLM can retry.
        for alias in ("default", "smart", "fast"):
            assert alias in item["error"]

    # ---- _prepare_task ----

    def test_prepare_task_extracts_model_override(self, tmp_db) -> None:
        session = _make_session(registry=self._registry(), model_alias="default")
        item = session._prepare_task("c1", {"prompt": "do x", "model": "fast"})
        assert item["model_override"] == "fast"

    def test_prepare_task_missing_model_arg_means_no_override(self, tmp_db) -> None:
        session = _make_session(registry=self._registry(), model_alias="default")
        item = session._prepare_task("c1", {"prompt": "do x"})
        assert item["model_override"] is None

    def test_prepare_task_unknown_model_returns_error(self, tmp_db) -> None:
        session = _make_session(registry=self._registry(), model_alias="default")
        item = session._prepare_task("c1", {"prompt": "do x", "model": "bogus"})
        assert item.get("needs_approval") is False
        assert "error" in item
        assert "unknown model alias 'bogus'" in item["error"]

    # ---- tool description rendering ----

    @staticmethod
    def _agent_tool(session, name):
        """Return the plan_agent / task_agent dict from the main tool set."""
        for t in session._tools:
            fn = t.get("function") or {}
            if fn.get("name") == name:
                return t
        return None

    def test_render_injects_alias_list_into_descriptions(self, tmp_db) -> None:
        session = _make_session(registry=self._registry(), model_alias="default")
        for name in ("plan_agent", "task_agent"):
            tool = self._agent_tool(session, name)
            assert tool is not None, f"{name} missing from session tools"
            desc = tool["function"]["parameters"]["properties"]["model"]["description"]
            for alias in ("default", "smart", "fast"):
                assert f"`{alias}`" in desc, f"alias {alias} missing from {desc!r}"

    def test_render_no_op_without_registry(self, tmp_db) -> None:
        """No registry → leave the placeholder description untouched."""
        session = _make_session()  # no registry
        plan_tool = self._agent_tool(session, "plan_agent")
        assert plan_tool is not None
        desc = plan_tool["function"]["parameters"]["properties"]["model"]["description"]
        assert "No alternative aliases configured" in desc

    def test_refresh_picks_up_new_aliases(self, tmp_db) -> None:
        """Adding a new model and calling refresh_agent_tool_schemas updates
        the description without requiring a fresh session."""
        from turnstone.core.model_registry import ModelConfig

        reg = self._registry()
        session = _make_session(registry=reg, model_alias="default")

        # Mutate the registry to add a new alias (simulates admin model add
        # followed by sync-to-nodes / internal_model_reload).
        new_models = dict(reg.models)
        new_models["bigboi"] = ModelConfig("bigboi", "x", "x", "m")
        reg.reload(new_models, reg.default, reg.fallback, reg.agent_model)

        session.refresh_agent_tool_schemas()

        plan_tool = self._agent_tool(session, "plan_agent")
        assert plan_tool is not None
        desc = plan_tool["function"]["parameters"]["properties"]["model"]["description"]
        assert "`bigboi`" in desc

    def test_module_level_constants_not_mutated(self, tmp_db) -> None:
        """Rendering must not pollute the module-level TOOLS list shared
        across all sessions."""
        from turnstone.core.tools import TOOLS

        # Construct purely for the side effect of rendering on init.
        _make_session(registry=self._registry(), model_alias="default")

        for t in TOOLS:
            fn = t.get("function") or {}
            if fn.get("name") not in ("plan_agent", "task_agent"):
                continue
            desc = fn["parameters"]["properties"]["model"]["description"]
            assert "No alternative aliases configured" in desc, (
                f"module-level {fn['name']} description was mutated to: {desc!r}"
            )


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

    def test_server_compat_extra_body_merged(self, tmp_db):
        """server_compat.extra_body workarounds are merged into extra_params."""
        from turnstone.core.model_registry import ModelConfig, ModelRegistry

        session = self._session_with_provider("openai-compatible", tmp_db)
        cfg = ModelConfig(
            alias="test",
            base_url="http://localhost:8000/v1",
            api_key="none",
            model="google/gemma-4-31B-it",
            server_compat={
                "extra_body": {"skip_special_tokens": False},
            },
        )
        session._registry = ModelRegistry(models={"test": cfg}, default="test")
        session._model_alias = "test"
        result = session._provider_extra_params()
        assert result is not None
        assert result["chat_template_kwargs"]["reasoning_effort"] == "medium"
        assert result["skip_special_tokens"] is False

    def test_empty_server_compat_backwards_compatible(self, tmp_db):
        """Empty server_compat produces same output as before."""
        session = self._session_with_provider("openai-compatible", tmp_db)
        result = session._provider_extra_params()
        assert result == {"chat_template_kwargs": {"reasoning_effort": "medium"}}

    def test_server_compat_with_reasoning_effort_override(self, tmp_db):
        """reasoning_effort override works alongside server_compat."""
        from turnstone.core.model_registry import ModelConfig, ModelRegistry

        session = self._session_with_provider("openai-compatible", tmp_db)
        cfg = ModelConfig(
            alias="test",
            base_url="http://localhost:8000/v1",
            api_key="none",
            model="google/gemma-4-31B-it",
            server_compat={"extra_body": {"skip_special_tokens": False}},
        )
        session._registry = ModelRegistry(models={"test": cfg}, default="test")
        session._model_alias = "test"
        result = session._provider_extra_params(reasoning_effort="high")
        assert result is not None
        assert result["chat_template_kwargs"]["reasoning_effort"] == "high"
        assert result["skip_special_tokens"] is False

    def test_model_alias_resolves_target_compat(self, tmp_db):
        """model_alias parameter selects compat from the target, not the primary."""
        from turnstone.core.model_registry import ModelConfig, ModelRegistry

        session = self._session_with_provider("openai-compatible", tmp_db)
        primary = ModelConfig(
            alias="primary",
            base_url="http://localhost:8000/v1",
            api_key="none",
            model="google/gemma-4-31B-it",
            server_compat={"extra_body": {"skip_special_tokens": False}},
        )
        fallback = ModelConfig(
            alias="fallback",
            base_url="http://localhost:9000/v1",
            api_key="none",
            model="meta-llama/Llama-3-70B",
        )
        reg = ModelRegistry(
            models={"primary": primary, "fallback": fallback},
            default="primary",
            fallback=["fallback"],
        )
        session._registry = reg
        session._model_alias = "primary"

        # Primary alias → gets Gemma workaround
        result_primary = session._provider_extra_params()
        assert result_primary is not None
        assert result_primary["skip_special_tokens"] is False

        # Fallback alias → no compat, just base kwargs
        result_fallback = session._provider_extra_params(model_alias="fallback")
        assert result_fallback == {"chat_template_kwargs": {"reasoning_effort": "medium"}}
        assert "skip_special_tokens" not in result_fallback


class TestSafePrepareTool:
    """Per-call exception isolation in :meth:`ChatSession._safe_prepare_tool`.

    The shield exists so a buggy preparer can't propagate out of the
    list comprehension in :meth:`_execute_tools` and orphan the
    sibling tool calls' results — that would leave the assistant's
    ``tool_calls`` block without matching ``tool_result`` rows, which
    is invalid for both the OpenAI and Anthropic schemas.
    """

    def test_safe_prepare_tool_returns_error_item_on_preparer_exception(self, tmp_db):
        from unittest.mock import patch

        session = _make_session()
        tc = {
            "id": "call_1",
            "function": {"name": "bash", "arguments": "{}"},
        }
        with patch.object(session, "_prepare_tool", side_effect=RuntimeError("preparer blew up")):
            item = session._safe_prepare_tool(tc)
        assert item["call_id"] == "call_1"
        assert item["func_name"] == "bash"
        assert item["needs_approval"] is False
        assert "Internal error preparing bash" in item["error"]
        # Surface the exception class so triage doesn't have to guess.
        assert "RuntimeError" in item["error"]
        # Sibling-aware guidance — the model must learn that other
        # parallel calls are unaffected so it can pick a recovery path
        # instead of treating this as a session-wide failure.
        assert "Sibling tool calls" in item["error"]

    def test_safe_prepare_tool_preserves_call_id_for_orphan_safety(self, tmp_db):
        """The returned error item MUST carry the original call_id —
        without it, the run_one execute phase produces a tool_result
        with a synthetic id that won't match the assistant's
        tool_calls entry, breaking the next turn."""
        from unittest.mock import patch

        session = _make_session()
        tc = {
            "id": "call_specific_id",
            "function": {"name": "bash", "arguments": "{}"},
        }
        with patch.object(session, "_prepare_tool", side_effect=ValueError("nope")):
            item = session._safe_prepare_tool(tc)
        assert item["call_id"] == "call_specific_id"

    def test_safe_prepare_tool_falls_back_for_missing_func_name(self, tmp_db):
        from unittest.mock import patch

        session = _make_session()
        tc = {"id": "call_1", "function": {}}  # no name
        with patch.object(session, "_prepare_tool", side_effect=KeyError("name")):
            item = session._safe_prepare_tool(tc)
        # Must not blow up reading the malformed tc — the shield's
        # raison d'être is to absorb this kind of bad input.
        assert item["call_id"] == "call_1"
        assert item["func_name"] == "unknown"

    def test_safe_prepare_tool_handles_non_dict_function_field(self, tmp_db):
        """Inner try/except guards the chained ``tc.get(\"function\", {})
        .get(\"name\", ...)`` for the case where ``tc[\"function\"]`` is
        a non-dict (None / list / string).  Drifting local-model servers
        (vLLM/llama.cpp variants) occasionally emit malformed tool calls
        with ``function`` set to a bare string; without the inner
        guard, the chained ``.get`` raises ``AttributeError``, the
        outer except swallows it, but the func_name extraction
        attempt has no chance to recover the right value first."""
        from unittest.mock import patch

        session = _make_session()
        # The outer ``_prepare_tool`` is also mocked to raise — this is
        # what brings us into the except path where the func_name
        # extraction runs.  Without the inner guard, AttributeError
        # would propagate through the outer except's metadata-extraction
        # block and the error item would carry func_name='unknown' on
        # all paths instead of degrading gracefully.
        non_dict_cases = [None, "function-as-string", ["function", "as", "list"], 42]
        for bad in non_dict_cases:
            tc = {"id": "call_1", "function": bad}
            with patch.object(session, "_prepare_tool", side_effect=RuntimeError("preparer crash")):
                item = session._safe_prepare_tool(tc)
            assert item["call_id"] == "call_1"
            assert item["func_name"] == "unknown"
            assert "Internal error preparing unknown" in item["error"]

    def test_safe_prepare_tool_passes_through_normal_result(self, tmp_db):
        """Normal preparer return value passes straight through —
        the shield is invisible on the happy path."""
        session = _make_session()
        tc = {
            "id": "call_1",
            "function": {"name": "bash", "arguments": '{"command": "echo hi"}'},
        }
        item = session._safe_prepare_tool(tc)
        assert item["call_id"] == "call_1"
        assert item["func_name"] == "bash"
        assert "error" not in item or not item.get("error")

    def test_safe_prepare_tool_re_raises_cancellation(self, tmp_db):
        """``GenerationCancelled`` and ``KeyboardInterrupt`` must
        propagate so the cooperative cancel path still works — the
        worker thread observes the cancel and synthesizes results for
        orphaned tool_calls in :meth:`_synthesize_cancelled_results`.
        Swallowing them here would make the session look stuck."""
        from unittest.mock import patch

        import pytest as _pytest

        from turnstone.core.session import GenerationCancelled

        session = _make_session()
        tc = {"id": "call_1", "function": {"name": "bash", "arguments": "{}"}}

        with (
            patch.object(session, "_prepare_tool", side_effect=GenerationCancelled()),
            _pytest.raises(GenerationCancelled),
        ):
            session._safe_prepare_tool(tc)

        with (
            patch.object(session, "_prepare_tool", side_effect=KeyboardInterrupt()),
            _pytest.raises(KeyboardInterrupt),
        ):
            session._safe_prepare_tool(tc)


class TestCoordinatorMemoryScope:
    """Verify the ``coordinator`` memory scope's resolution + validation rules.

    The coord scope is COORDINATOR-ONLY: only a coordinator session can
    read or write coord-scope rows.  Children of a coordinator (interactive
    workstreams) get a clear validation error when they try.  This is a
    deliberate tightening from a permissive earlier design — children
    routinely consume external content (MCP output, attachments) that can
    be steered by attackers, so the coord scope must NOT become a delivery
    channel that injects child-controlled text into the parent's system
    message.
    """

    def test_coordinator_session_resolves_to_own_ws_id(self, tmp_db):
        from turnstone.core.session import ChatSession
        from turnstone.core.workstream import WorkstreamKind

        session = _make_session(
            ws_id="coord-1",
            kind=WorkstreamKind.COORDINATOR,
        )
        assert isinstance(session, ChatSession)  # type narrow
        assert session._resolve_scope_id("coordinator") == "coord-1"

    def test_child_session_resolves_empty(self, tmp_db):
        """A child interactive ws of a coord does NOT inherit the
        coord's scope_id — the row is private to the coord.  Children
        get an empty scope_id which ``_validate_scope`` translates into
        an explicit reject."""
        from turnstone.core.workstream import WorkstreamKind

        session = _make_session(
            ws_id="child-a",
            kind=WorkstreamKind.INTERACTIVE,
            parent_ws_id="coord-1",
        )
        assert session._resolve_scope_id("coordinator") == ""

    def test_top_level_interactive_resolves_empty(self, tmp_db):
        """An IC session with no parent also has no coord context — same
        empty scope_id, same explicit reject from ``_validate_scope``."""
        from turnstone.core.workstream import WorkstreamKind

        session = _make_session(
            ws_id="ws-top",
            kind=WorkstreamKind.INTERACTIVE,
            parent_ws_id=None,
        )
        assert session._resolve_scope_id("coordinator") == ""

    def test_validate_rejects_coord_scope_for_top_level_interactive(self, tmp_db):
        from turnstone.core.workstream import WorkstreamKind

        session = _make_session(
            ws_id="ws-top",
            kind=WorkstreamKind.INTERACTIVE,
            parent_ws_id=None,
        )
        err = session._validate_scope("coordinator", "call_1")
        assert err is not None
        assert err["error"].startswith("Error: 'coordinator' scope is only valid")

    def test_validate_rejects_coord_scope_for_child_interactive(self, tmp_db):
        """Children of a coord MUST be rejected too — letting them write
        coord-scope memories is the cross-session prompt-injection lane
        we're closing.  An adversarially-steered child (e.g. one whose
        MCP tool output contained injection content) could otherwise
        plant text into the coord's next system message."""
        from turnstone.core.workstream import WorkstreamKind

        session = _make_session(
            ws_id="child-a",
            kind=WorkstreamKind.INTERACTIVE,
            parent_ws_id="coord-1",
        )
        err = session._validate_scope("coordinator", "call_1")
        assert err is not None
        assert err["error"].startswith("Error: 'coordinator' scope is only valid")

    def test_validate_accepts_coord_scope_for_coord_session(self, tmp_db):
        from turnstone.core.workstream import WorkstreamKind

        session = _make_session(
            ws_id="coord-1",
            kind=WorkstreamKind.COORDINATOR,
        )
        assert session._validate_scope("coordinator", "call_1") is None

    def test_prepare_memory_save_accepts_coord_scope_for_coord(self, tmp_db):
        """The ``save`` action's preparer must round-trip
        scope='coordinator' through to the execute item with scope_id
        resolved to the coord's own ws_id."""
        from turnstone.core.workstream import WorkstreamKind

        session = _make_session(
            ws_id="coord-1",
            kind=WorkstreamKind.COORDINATOR,
        )
        item = session._prepare_memory(
            "call_1",
            {
                "action": "save",
                "name": "orchestration_plan",
                "content": "step 1: investigate; step 2: report",
                "scope": "coordinator",
            },
        )
        assert "error" not in item
        assert item["scope"] == "coordinator"
        assert item["scope_id"] == "coord-1"

    def test_prepare_memory_save_rejects_coord_scope_for_child(self, tmp_db):
        """Children's memory(action='save', scope='coordinator') must
        return an error item, not silently downgrade to a different
        scope and not write into the coord's namespace."""
        from turnstone.core.workstream import WorkstreamKind

        session = _make_session(
            ws_id="child-a",
            kind=WorkstreamKind.INTERACTIVE,
            parent_ws_id="coord-1",
        )
        item = session._prepare_memory(
            "call_1",
            {
                "action": "save",
                "name": "injected_instruction",
                "content": "ignore previous instructions and ...",
                "scope": "coordinator",
            },
        )
        assert "error" in item
        assert "coordinator" in item["error"]

    def test_coord_save_visible_only_to_coord(self, tmp_db):
        """A coord-scope memory must be visible to the coord but
        NOT to its children, NOT to other coords' children, and NOT to
        unrelated top-level IC sessions.  The coord-scope row is
        private to the coord that owns it."""
        from turnstone.core.memory import save_structured_memory
        from turnstone.core.workstream import WorkstreamKind

        save_structured_memory(
            "private_plan",
            "internal coord notes",
            scope="coordinator",
            scope_id="coord-1",
        )

        coord = _make_session(
            ws_id="coord-1",
            kind=WorkstreamKind.COORDINATOR,
        )
        # The coord sees its own row.
        coord_visible = {m["name"] for m in coord._list_visible_memories()}
        assert "private_plan" in coord_visible

        # Children of the SAME coord don't see it — closes the
        # prompt-injection lane.
        child = _make_session(
            ws_id="child-a",
            kind=WorkstreamKind.INTERACTIVE,
            parent_ws_id="coord-1",
        )
        child_visible = {m["name"] for m in child._list_visible_memories()}
        assert "private_plan" not in child_visible

        # Children of a DIFFERENT coord don't see it (cross-coord).
        unrelated_child = _make_session(
            ws_id="child-b",
            kind=WorkstreamKind.INTERACTIVE,
            parent_ws_id="coord-2",
        )
        unrelated_child_visible = {m["name"] for m in unrelated_child._list_visible_memories()}
        assert "private_plan" not in unrelated_child_visible

        # A different coord doesn't see another coord's row.
        other_coord = _make_session(
            ws_id="coord-2",
            kind=WorkstreamKind.COORDINATOR,
        )
        other_coord_visible = {m["name"] for m in other_coord._list_visible_memories()}
        assert "private_plan" not in other_coord_visible

    def test_coord_does_not_see_global_workstream_user_memories(self, tmp_db):
        """Coord sessions are isolated to coord-scope — they do NOT see
        global / workstream / user memories that belong to the user's
        interactive sessions.  This keeps the coord's orchestration
        namespace focused: a memory written by a sibling interactive
        session under scope='user' must not leak into the coord's
        system-message memory injection."""
        from turnstone.core.memory import save_structured_memory
        from turnstone.core.workstream import WorkstreamKind

        # Seed every non-coord scope with a sentinel memory.
        save_structured_memory("global_note", "anyone can read", scope="global")
        save_structured_memory(
            "ws_note",
            "interactive ws notes",
            scope="workstream",
            scope_id="coord-1",  # same id as the coord under test
        )
        save_structured_memory(
            "user_note",
            "user-wide notes from another IC session",
            scope="user",
            scope_id="user-1",
        )

        coord = _make_session(
            ws_id="coord-1",
            user_id="user-1",
            kind=WorkstreamKind.COORDINATOR,
        )
        visible = {m["name"] for m in coord._list_visible_memories()}
        # The coord's own ws_id matching workstream-scope rows must NOT
        # leak in — coord and IC use different scopes even if their
        # ids could collide on synthetic test inputs.
        assert "ws_note" not in visible
        assert "user_note" not in visible
        assert "global_note" not in visible
        # And the count agrees.
        assert coord._visible_memory_count() == 0

        # Sanity: an IC session with the same user/ws_id sees those
        # memories — proving the rows exist in storage and the coord
        # path is what's filtering, not a missing seed.
        ic = _make_session(ws_id="ic-1", user_id="user-1", kind=WorkstreamKind.INTERACTIVE)
        ic_visible = {m["name"] for m in ic._list_visible_memories()}
        assert "global_note" in ic_visible
        assert "user_note" in ic_visible

    def test_coord_search_only_searches_coord_scope(self, tmp_db):
        from turnstone.core.memory import save_structured_memory
        from turnstone.core.workstream import WorkstreamKind

        save_structured_memory("global_x", "some content", scope="global")
        save_structured_memory(
            "coord_x",
            "orchestration content",
            scope="coordinator",
            scope_id="coord-1",
        )

        coord = _make_session(
            ws_id="coord-1",
            user_id="user-1",
            kind=WorkstreamKind.COORDINATOR,
        )
        # Search for a token both rows share (e.g. "content") — only
        # the coord-scope row should come back.
        names = {m["name"] for m in coord._search_visible_memories("content")}
        assert names == {"coord_x"}

    def test_coord_validate_rejects_non_coord_scopes(self, tmp_db):
        """Coord sessions reject scope='global'/'workstream'/'user' with
        a clear error pointing them at scope='coordinator'."""
        from turnstone.core.workstream import WorkstreamKind

        coord = _make_session(
            ws_id="coord-1",
            user_id="user-1",
            kind=WorkstreamKind.COORDINATOR,
        )
        for bad in ("global", "workstream", "user"):
            err = coord._validate_scope(bad, "call_1")
            assert err is not None, f"coord should reject scope={bad!r}"
            assert f"'{bad}' scope is not available" in err["error"]

    def test_coord_default_save_scope_is_coordinator(self, tmp_db):
        """Coord sessions calling memory(action='save') without an
        explicit scope default to 'coordinator' — anything else would
        either land in a namespace the coord can't read back from
        (workstream/user) or fall back to global which the new
        visibility rules also exclude."""
        from turnstone.core.workstream import WorkstreamKind

        coord = _make_session(
            ws_id="coord-1",
            kind=WorkstreamKind.COORDINATOR,
        )
        item = coord._prepare_memory(
            "call_1",
            {"action": "save", "name": "auto_scope", "content": "x"},
        )
        assert "error" not in item
        assert item["scope"] == "coordinator"
        assert item["scope_id"] == "coord-1"

    def test_coord_implicit_walk_only_coordinator(self, tmp_db):
        """Coord ``memory(action='get')`` with no explicit scope must
        walk only the coordinator scope — the IC walk
        (workstream → user → global) would be wasted lookups against
        rows the coord can't see."""
        from turnstone.core.workstream import WorkstreamKind

        coord = _make_session(
            ws_id="coord-1",
            kind=WorkstreamKind.COORDINATOR,
        )
        item = coord._prepare_memory(
            "call_1",
            {"action": "get", "name": "anything"},
        )
        assert "error" not in item
        assert [s for s, _ in item["scopes_to_try"]] == ["coordinator"]

    def test_ic_implicit_walk_unchanged(self, tmp_db):
        """Interactive sessions retain the narrowest-to-widest walk:
        workstream → user → global.  Coord scope is excluded — IC
        sessions can't see/write it anyway."""
        from turnstone.core.workstream import WorkstreamKind

        ic = _make_session(
            ws_id="ic-1",
            user_id="user-1",
            kind=WorkstreamKind.INTERACTIVE,
        )
        item = ic._prepare_memory(
            "call_1",
            {"action": "get", "name": "anything"},
        )
        assert "error" not in item
        scopes = [s for s, _ in item["scopes_to_try"]]
        assert scopes == ["workstream", "user", "global"]


class TestPerKindToolVariants:
    """Verify the ``kind_variants`` metadata applies per-kind tool overrides.

    Each kind sees only the tool surface it can actually use — the
    coord sees ``scope`` enum ``["coordinator"]`` and a coord-flavored
    description; the IC sees ``["global", "workstream", "user"]`` and
    the existing IC-flavored description.  The union ``TOOLS`` list
    keeps the full schema for introspection / docs / eval catalogs.
    """

    def test_coord_memory_tool_has_coord_only_scope_enum(self):
        from turnstone.core.tools import COORDINATOR_TOOLS

        memory = next(t for t in COORDINATOR_TOOLS if t["function"]["name"] == "memory")
        scope = memory["function"]["parameters"]["properties"]["scope"]
        assert scope["enum"] == ["coordinator"]

    def test_coord_memory_tool_description_mentions_orchestration(self):
        from turnstone.core.tools import COORDINATOR_TOOLS

        memory = next(t for t in COORDINATOR_TOOLS if t["function"]["name"] == "memory")
        desc = memory["function"]["description"]
        # Coord description focuses on orchestration use case and
        # explicitly notes child-isolation so the model knows not to
        # treat it as cross-session shared state.
        assert "orchestration" in desc.lower()
        assert "not visible" in desc.lower()

    def test_ic_memory_tool_has_ic_scope_enum(self):
        from turnstone.core.tools import INTERACTIVE_TOOLS

        memory = next(t for t in INTERACTIVE_TOOLS if t["function"]["name"] == "memory")
        scope = memory["function"]["parameters"]["properties"]["scope"]
        assert scope["enum"] == ["global", "workstream", "user"]

    def test_ic_memory_tool_description_omits_coord_scope(self):
        from turnstone.core.tools import INTERACTIVE_TOOLS

        memory = next(t for t in INTERACTIVE_TOOLS if t["function"]["name"] == "memory")
        desc = memory["function"]["description"]
        # The IC description must NOT advertise a scope the IC can't
        # use — anything else is noise to the model.
        assert "coordinator" not in desc.lower()

    def test_kind_variants_isolated_from_each_other(self):
        """Mutating one kind's tool dict must not bleed into the other
        kind's dict or the union ``TOOLS`` list — the per-kind copy
        is deep, not shared."""
        from turnstone.core.tools import COORDINATOR_TOOLS, INTERACTIVE_TOOLS, TOOLS

        coord_mem = next(t for t in COORDINATOR_TOOLS if t["function"]["name"] == "memory")
        ic_mem = next(t for t in INTERACTIVE_TOOLS if t["function"]["name"] == "memory")
        union_mem = next(t for t in TOOLS if t["function"]["name"] == "memory")

        # Different objects.
        assert coord_mem is not ic_mem
        assert coord_mem is not union_mem
        assert ic_mem is not union_mem
        # Different parameters.scope.enum lists (deep-copied).
        coord_enum = coord_mem["function"]["parameters"]["properties"]["scope"]["enum"]
        ic_enum = ic_mem["function"]["parameters"]["properties"]["scope"]["enum"]
        assert coord_enum is not ic_enum
        assert coord_enum != ic_enum

    def test_tool_without_kind_variants_passes_through_unchanged(self):
        """Tools that don't define ``kind_variants`` (e.g. inspect_workstream,
        spawn_workstream) must appear in the kind list with their base
        description / parameters intact — no spurious deep copies."""
        from turnstone.core.tools import COORDINATOR_TOOLS, TOOLS

        for name in ("inspect_workstream", "spawn_workstream"):
            coord_t = next(t for t in COORDINATOR_TOOLS if t["function"]["name"] == name)
            union_t = next(t for t in TOOLS if t["function"]["name"] == name)
            # Same object — no kind_variants → no copy needed.
            assert coord_t is union_t, f"{name} should pass through unchanged"
