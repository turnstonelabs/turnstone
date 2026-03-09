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
        # Mock provider to report vision support
        mock_caps = MagicMock()
        mock_caps.supports_vision = True
        session._provider.get_capabilities = MagicMock(return_value=mock_caps)

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
        session._provider.get_capabilities = MagicMock(return_value=mock_caps)

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
        session._provider.get_capabilities = MagicMock(return_value=mock_caps)

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
        session._provider.get_capabilities = MagicMock(return_value=mock_caps)

        item = {"call_id": "c4", "path": str(tmp_path / "nope.png"), "offset": None, "limit": None}
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
        # Ensure provider returns a real ModelCapabilities (not MagicMock)
        session._provider.get_capabilities = MagicMock(return_value=ModelCapabilities())
        caps = session._get_capabilities()
        assert caps.supports_vision is True

    def test_no_override_uses_provider_default(self, tmp_db):
        """Without config override, provider defaults are used."""
        session = _make_session()
        caps = session._get_capabilities()
        # Default OpenAI provider for unknown model → no vision
        assert caps.supports_vision is False
