"""Tests for the bootstrap wizard module."""

from __future__ import annotations

import os
import socket
from pathlib import Path
from unittest.mock import MagicMock, patch

from turnstone.bootstrap import (
    SYSTEM_PROMPT,
    TOOLS,
    _BootstrapLLM,
    _FinishError,
    _mask_secrets,
    _tool_check_docker,
    _tool_check_port,
    _tool_finish,
    _tool_generate_secret,
    _tool_read_file,
    _tool_validate_api_key,
    _tool_write_compose,
    _tool_write_file,
    execute_tool,
)

# ---------------------------------------------------------------------------
# Tool function tests
# ---------------------------------------------------------------------------


class TestReadFile:
    def test_existing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        result = _tool_read_file(tmp_path, {"path": "test.txt"})
        assert result == "hello world"

    def test_missing_file(self, tmp_path: Path) -> None:
        result = _tool_read_file(tmp_path, {"path": "nope.txt"})
        assert "Error: file not found" in result

    def test_nested_path(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        f = sub / "nested.txt"
        f.write_text("nested content")
        result = _tool_read_file(tmp_path, {"path": "sub/nested.txt"})
        assert result == "nested content"

    def test_path_traversal_blocked(self, tmp_path: Path) -> None:
        result = _tool_read_file(tmp_path, {"path": "../../etc/passwd"})
        assert "escapes project directory" in result

    def test_absolute_path_blocked(self, tmp_path: Path) -> None:
        result = _tool_read_file(tmp_path, {"path": "/etc/passwd"})
        assert "escapes project directory" in result


class TestWriteFile:
    def test_write_confirmed(self, tmp_path: Path) -> None:
        with patch("builtins.input", return_value="y"):
            result = _tool_write_file(tmp_path, {"path": "out.txt", "content": "data\n"})
        assert "written successfully" in result
        assert (tmp_path / "out.txt").read_text() == "data\n"

    def test_write_declined(self, tmp_path: Path) -> None:
        with patch("builtins.input", return_value="n"):
            result = _tool_write_file(tmp_path, {"path": "out.txt", "content": "data\n"})
        assert "declined" in result
        assert not (tmp_path / "out.txt").exists()

    def test_write_creates_parent_dirs(self, tmp_path: Path) -> None:
        with patch("builtins.input", return_value="y"):
            result = _tool_write_file(tmp_path, {"path": "a/b/c.txt", "content": "deep\n"})
        assert "written successfully" in result
        assert (tmp_path / "a" / "b" / "c.txt").read_text() == "deep\n"

    def test_sh_files_are_executable(self, tmp_path: Path) -> None:
        with patch("builtins.input", return_value="y"):
            _tool_write_file(tmp_path, {"path": "setup.sh", "content": "#!/bin/bash\n"})
        mode = (tmp_path / "setup.sh").stat().st_mode
        assert mode & 0o110  # user + group executable, not world

    def test_path_traversal_blocked(self, tmp_path: Path) -> None:
        result = _tool_write_file(tmp_path, {"path": "../../escape.txt", "content": "bad\n"})
        assert "escapes project directory" in result

    def test_default_enter_confirms(self, tmp_path: Path) -> None:
        with patch("builtins.input", return_value=""):
            result = _tool_write_file(tmp_path, {"path": "ok.txt", "content": "ok\n"})
        assert "written successfully" in result

    def test_duplicate_write_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "dup.txt").write_text("same\n")
        result = _tool_write_file(tmp_path, {"path": "dup.txt", "content": "same\n"})
        assert "already exists" in result

    def test_different_content_still_prompts(self, tmp_path: Path) -> None:
        (tmp_path / "changed.txt").write_text("old\n")
        with patch("builtins.input", return_value="y"):
            result = _tool_write_file(tmp_path, {"path": "changed.txt", "content": "new\n"})
        assert "written successfully" in result
        assert (tmp_path / "changed.txt").read_text() == "new\n"


class TestWriteCompose:
    def test_writes_compose_file(self, tmp_path: Path) -> None:
        with patch("builtins.input", return_value="y"):
            result = _tool_write_compose(tmp_path, {})
        assert "written successfully" in result
        assert "ghcr.io" in result
        content = (tmp_path / "compose.yaml").read_text()
        assert "ghcr.io/turnstonelabs/turnstone" in content
        assert "TURNSTONE_IMAGE_TAG" in content

    def test_user_declines(self, tmp_path: Path) -> None:
        with patch("builtins.input", return_value="n"):
            result = _tool_write_compose(tmp_path, {})
        assert "declined" in result
        assert not (tmp_path / "compose.yaml").exists()

    def test_identical_content_skipped(self, tmp_path: Path) -> None:
        # Write it once
        with patch("builtins.input", return_value="y"):
            _tool_write_compose(tmp_path, {})
        # Second call should skip
        result = _tool_write_compose(tmp_path, {})
        assert "already exists" in result

    def test_no_build_blocks(self, tmp_path: Path) -> None:
        with patch("builtins.input", return_value="y"):
            _tool_write_compose(tmp_path, {})
        content = (tmp_path / "compose.yaml").read_text()
        assert "build:" not in content
        assert "dockerfile:" not in content.lower()

    def test_overwrites_different_content(self, tmp_path: Path) -> None:
        (tmp_path / "compose.yaml").write_text("old content\n")
        with patch("builtins.input", return_value="y"):
            result = _tool_write_compose(tmp_path, {})
        assert "written successfully" in result
        content = (tmp_path / "compose.yaml").read_text()
        assert "ghcr.io" in content

    def test_no_local_image_references(self, tmp_path: Path) -> None:
        with patch("builtins.input", return_value="y"):
            _tool_write_compose(tmp_path, {})
        content = (tmp_path / "compose.yaml").read_text()
        assert "turnstone:local" not in content


class TestGenerateSecret:
    def test_default_length(self) -> None:
        secret = _tool_generate_secret({})
        assert len(secret) == 64  # 32 bytes -> 64 hex chars

    def test_custom_length(self) -> None:
        secret = _tool_generate_secret({"length": 16})
        assert len(secret) == 32

    def test_uniqueness(self) -> None:
        s1 = _tool_generate_secret({})
        s2 = _tool_generate_secret({})
        assert s1 != s2

    def test_invalid_length_fallback(self) -> None:
        secret = _tool_generate_secret({"length": -1})
        assert len(secret) == 64  # falls back to 32 bytes

    def test_excessive_length_capped(self) -> None:
        secret = _tool_generate_secret({"length": 99999})
        assert len(secret) == 64  # falls back to 32 bytes


class TestCheckPort:
    def test_available_port(self) -> None:
        # Pick a random high port that's likely free
        result = _tool_check_port({"port": 59123})
        assert "AVAILABLE" in result or "IN USE" in result

    def test_in_use_port(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.listen(1)
            result = _tool_check_port({"port": port})
        assert "IN USE" in result

    def test_invalid_port(self) -> None:
        result = _tool_check_port({"port": -1})
        assert "Error" in result

    def test_port_zero(self) -> None:
        result = _tool_check_port({"port": 0})
        assert "Error" in result


class TestCheckDocker:
    def test_docker_installed(self) -> None:
        mock_docker = MagicMock()
        mock_docker.returncode = 0
        mock_docker.stdout = "24.0.7"

        mock_compose = MagicMock()
        mock_compose.returncode = 0
        mock_compose.stdout = "2.24.5"

        with patch("subprocess.run", side_effect=[mock_docker, mock_compose]):
            result = _tool_check_docker({})
        assert "Docker: installed" in result
        assert "Docker Compose: installed" in result

    def test_docker_not_installed(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = _tool_check_docker({})
        assert "NOT installed" in result or "NOT available" in result

    def test_docker_daemon_not_running(self) -> None:
        mock_docker = MagicMock()
        mock_docker.returncode = 1
        mock_docker.stderr = "Cannot connect to the Docker daemon"

        mock_compose = MagicMock()
        mock_compose.returncode = 1

        with patch("subprocess.run", side_effect=[mock_docker, mock_compose]):
            result = _tool_check_docker({})
        assert "NOT running" in result


class TestValidateApiKey:
    def test_openai_success(self) -> None:
        mock_client = MagicMock()
        mock_client.models.list.return_value = []
        with patch("openai.OpenAI", return_value=mock_client):
            result = _tool_validate_api_key({"provider": "openai", "api_key": "sk-test"})
        assert "Success" in result

    def test_openai_failure(self) -> None:
        with patch("openai.OpenAI") as mock_cls:
            mock_cls.return_value.models.list.side_effect = Exception("Invalid key")
            result = _tool_validate_api_key({"provider": "openai", "api_key": "bad"})
        assert "Failed" in result

    def test_unknown_provider(self) -> None:
        result = _tool_validate_api_key({"provider": "unknown", "api_key": "x"})
        assert "unknown" in result


class TestExecuteTool:
    def test_unknown_tool(self, tmp_path: Path) -> None:
        result = execute_tool("nonexistent", {}, tmp_path)
        assert "unknown tool" in result

    def test_dispatches_correctly(self, tmp_path: Path) -> None:
        f = tmp_path / "hello.txt"
        f.write_text("hi")
        result = execute_tool("read_file", {"path": "hello.txt"}, tmp_path)
        assert result == "hi"

    def test_finish_raises(self, tmp_path: Path) -> None:
        import pytest

        with pytest.raises(_FinishError, match="All done"):
            execute_tool("finish", {"summary": "All done"}, tmp_path)


class TestFinishTool:
    def test_raises_with_summary(self) -> None:
        import pytest

        with pytest.raises(_FinishError) as exc_info:
            _tool_finish({"summary": "Configured production deployment."})
        assert exc_info.value.summary == "Configured production deployment."

    def test_default_summary(self) -> None:
        import pytest

        with pytest.raises(_FinishError) as exc_info:
            _tool_finish({})
        assert exc_info.value.summary == "Setup complete."


# ---------------------------------------------------------------------------
# Secret masking tests
# ---------------------------------------------------------------------------


class TestMaskSecrets:
    def test_masks_api_key(self) -> None:
        text = "OPENAI_API_KEY=sk-1234567890abcdef"
        result = _mask_secrets(text)
        assert "sk-1" in result
        assert "cdef" in result
        assert "1234567890abcde" not in result

    def test_preserves_comments(self) -> None:
        text = "# OPENAI_API_KEY=sk-1234567890abcdef"
        result = _mask_secrets(text)
        assert result == text

    def test_preserves_short_values(self) -> None:
        text = "TOKEN=short"
        result = _mask_secrets(text)
        assert result == text

    def test_preserves_non_sensitive(self) -> None:
        text = "MODEL=gpt-5.4"
        result = _mask_secrets(text)
        assert result == text


# ---------------------------------------------------------------------------
# Message conversion tests (Anthropic)
# ---------------------------------------------------------------------------


class TestAnthropicConversion:
    """Test the Anthropic message/tool conversion inside _BootstrapLLM."""

    def _make_llm(self) -> _BootstrapLLM:
        return _BootstrapLLM("anthropic", MagicMock(), "test-model")

    def test_tool_format_conversion(self) -> None:
        """OpenAI tool format should convert to Anthropic format."""
        llm = self._make_llm()
        # The conversion happens inside _complete_anthropic; we test indirectly
        # by checking the tools passed to the mock client
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="hello")]
        mock_response.stop_reason = "end_turn"
        llm.client.messages.create.return_value = mock_response

        llm.complete(
            [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
            TOOLS[:1],  # Just read_file
        )

        call_kwargs = llm.client.messages.create.call_args[1]
        api_tools = call_kwargs["tools"]
        assert len(api_tools) == 1
        assert api_tools[0]["name"] == "read_file"
        assert "input_schema" in api_tools[0]
        assert "description" in api_tools[0]

    def test_system_message_extraction(self) -> None:
        """System message should be extracted to system parameter."""
        llm = self._make_llm()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="ok")]
        mock_response.stop_reason = "end_turn"
        llm.client.messages.create.return_value = mock_response

        llm.complete(
            [{"role": "system", "content": "test system"}, {"role": "user", "content": "hi"}],
            [],
        )

        call_kwargs = llm.client.messages.create.call_args[1]
        assert call_kwargs["system"] == "test system"
        # System should NOT appear in messages
        for msg in call_kwargs["messages"]:
            assert msg["role"] != "system"

    def test_tool_result_conversion(self) -> None:
        """OpenAI tool result messages should convert to Anthropic format."""
        llm = self._make_llm()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="got it")]
        mock_response.stop_reason = "end_turn"
        llm.client.messages.create.return_value = mock_response

        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "type": "function",
                        "function": {"name": "check_docker", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "tc_1",
                "content": "Docker: installed",
            },
        ]
        llm.complete(messages, TOOLS)

        call_kwargs = llm.client.messages.create.call_args[1]
        api_messages = call_kwargs["messages"]

        # Find the tool_result message
        tool_result_found = False
        for msg in api_messages:
            if msg["role"] == "user" and isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        assert block["tool_use_id"] == "tc_1"
                        assert block["content"] == "Docker: installed"
                        tool_result_found = True
        assert tool_result_found

    def test_tool_use_blocks_in_assistant(self) -> None:
        """Assistant messages with tool_calls should convert to content blocks."""
        llm = self._make_llm()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="ok")]
        mock_response.stop_reason = "end_turn"
        llm.client.messages.create.return_value = mock_response

        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "Let me check",
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "type": "function",
                        "function": {"name": "check_docker", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tc_1", "content": "ok"},
        ]
        llm.complete(messages, TOOLS)

        call_kwargs = llm.client.messages.create.call_args[1]
        api_messages = call_kwargs["messages"]

        # First message should be user "hi"
        assert api_messages[0]["role"] == "user"
        # Second should be assistant with content blocks
        assistant_msg = api_messages[1]
        assert assistant_msg["role"] == "assistant"
        assert isinstance(assistant_msg["content"], list)
        # Should have text block + tool_use block
        types = [b["type"] for b in assistant_msg["content"]]
        assert "text" in types
        assert "tool_use" in types


class TestOpenAICompletion:
    """Test the OpenAI path of _BootstrapLLM."""

    def test_text_response(self) -> None:
        llm = _BootstrapLLM("openai", MagicMock(), "gpt-5.4")
        mock_choice = MagicMock()
        mock_choice.message.content = "Hello!"
        mock_choice.message.tool_calls = None
        mock_choice.finish_reason = "stop"
        llm.client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])

        content, tool_calls, reason = llm.complete([{"role": "user", "content": "hi"}], TOOLS)
        assert content == "Hello!"
        assert tool_calls is None
        assert reason == "stop"

    def test_tool_call_response(self) -> None:
        llm = _BootstrapLLM("openai", MagicMock(), "gpt-5.4")

        mock_tc = MagicMock()
        mock_tc.id = "call_123"
        mock_tc.function.name = "check_docker"
        mock_tc.function.arguments = "{}"

        mock_choice = MagicMock()
        mock_choice.message.content = ""
        mock_choice.message.tool_calls = [mock_tc]
        mock_choice.finish_reason = "tool_calls"
        llm.client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])

        content, tool_calls, reason = llm.complete(
            [{"role": "user", "content": "check docker"}], TOOLS
        )
        assert tool_calls is not None
        assert len(tool_calls) == 1
        assert tool_calls[0]["function"]["name"] == "check_docker"
        assert tool_calls[0]["id"] == "call_123"

    def test_no_content(self) -> None:
        llm = _BootstrapLLM("openai", MagicMock(), "gpt-5.4")
        mock_choice = MagicMock()
        mock_choice.message.content = None
        mock_choice.message.tool_calls = None
        mock_choice.finish_reason = "stop"
        llm.client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])

        content, tool_calls, reason = llm.complete([{"role": "user", "content": "hi"}], [])
        assert content == ""
        assert tool_calls is None


# ---------------------------------------------------------------------------
# Conversation loop tests
# ---------------------------------------------------------------------------


class TestConversationLoop:
    def test_quit_exits(self) -> None:
        """User typing 'quit' should exit the loop."""
        llm = MagicMock(spec=_BootstrapLLM)
        llm.complete.return_value = ("What would you like?", None, "stop")

        with patch("builtins.input", return_value="quit"):
            from turnstone.bootstrap import _run_conversation

            _run_conversation(llm, Path("/tmp"))

    def test_tool_calls_executed(self, tmp_path: Path) -> None:
        """Tool calls should be executed and results fed back."""
        llm = MagicMock(spec=_BootstrapLLM)
        # First call: LLM returns a tool call
        llm.complete.side_effect = [
            (
                "",
                [
                    {
                        "id": "tc_1",
                        "type": "function",
                        "function": {"name": "generate_secret", "arguments": "{}"},
                    }
                ],
                "tool_calls",
            ),
            # Second call: LLM responds with text after seeing tool result
            ("Here's your secret!", None, "stop"),
        ]

        with patch("builtins.input", return_value="quit"):
            from turnstone.bootstrap import _run_conversation

            _run_conversation(llm, tmp_path)

        # Verify two calls were made
        assert llm.complete.call_count == 2
        # Verify tool result was fed back in second call's messages
        second_call_messages = llm.complete.call_args_list[1][0][0]
        tool_results = [m for m in second_call_messages if m.get("role") == "tool"]
        assert len(tool_results) == 1
        assert tool_results[0]["tool_call_id"] == "tc_1"
        # Result should be a 64-char hex string
        assert len(tool_results[0]["content"]) == 64

    def test_empty_input_skipped(self) -> None:
        """Empty user input should be skipped."""
        llm = MagicMock(spec=_BootstrapLLM)
        llm.complete.return_value = ("Ask me something.", None, "stop")

        call_count = 0

        def mock_input(prompt: str = "") -> str:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return ""  # Empty inputs
            return "quit"

        with patch("builtins.input", side_effect=mock_input):
            from turnstone.bootstrap import _run_conversation

            _run_conversation(llm, Path("/tmp"))

    def test_finish_tool_exits_loop(self, tmp_path: Path) -> None:
        """LLM calling finish tool should exit the conversation cleanly."""
        llm = MagicMock(spec=_BootstrapLLM)
        llm.complete.return_value = (
            "",
            [
                {
                    "id": "tc_fin",
                    "type": "function",
                    "function": {
                        "name": "finish",
                        "arguments": '{"summary": "All configured."}',
                    },
                }
            ],
            "tool_calls",
        )

        from turnstone.bootstrap import _run_conversation

        # Should return without needing user input
        _run_conversation(llm, tmp_path)
        assert llm.complete.call_count == 1


# ---------------------------------------------------------------------------
# Interactive startup tests
# ---------------------------------------------------------------------------


class TestProviderDefaults:
    def test_openai_default_model(self) -> None:
        from turnstone.bootstrap import _DEFAULT_MODELS

        assert _DEFAULT_MODELS["openai"] == "gpt-5.4"

    def test_anthropic_default_model(self) -> None:
        from turnstone.bootstrap import _DEFAULT_MODELS

        assert _DEFAULT_MODELS["anthropic"] == "claude-sonnet-4-6"


class TestSelectProvider:
    def test_openai_selection(self) -> None:
        """Selecting '1' should set up OpenAI."""
        mock_client = MagicMock()
        with (
            patch("builtins.input", side_effect=["1", ""]),
            patch("getpass.getpass", return_value="sk-test"),
            patch("openai.OpenAI", return_value=mock_client),
        ):
            from turnstone.bootstrap import _select_provider

            provider, client, model = _select_provider()
        assert provider == "openai"
        assert model == "gpt-5.4"

    def test_local_selection(self) -> None:
        """Selecting '3' should set up local/vLLM."""
        mock_client = MagicMock()
        # Ensure OPENAI_API_KEY is not in env so we hit the getpass path
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("builtins.input", side_effect=["3", "http://localhost:8000/v1", "my-model"]),
            patch("getpass.getpass", return_value="none"),
            patch("openai.OpenAI", return_value=mock_client),
        ):
            from turnstone.bootstrap import _select_provider

            provider, client, model = _select_provider()
        assert provider == "openai"
        assert model == "my-model"


# ---------------------------------------------------------------------------
# System prompt and tools sanity checks
# ---------------------------------------------------------------------------


class TestConstants:
    def test_system_prompt_not_empty(self) -> None:
        assert len(SYSTEM_PROMPT) > 500

    def test_system_prompt_mentions_turnstone(self) -> None:
        assert "Turnstone" in SYSTEM_PROMPT

    def test_all_tools_have_required_fields(self) -> None:
        for tool in TOOLS:
            assert tool["type"] == "function"
            func = tool["function"]
            assert "name" in func
            assert "description" in func
            assert "parameters" in func
            assert func["parameters"]["type"] == "object"

    def test_tool_count(self) -> None:
        assert len(TOOLS) == 8

    def test_all_tools_have_implementations(self) -> None:
        from turnstone.bootstrap import TOOL_FUNCTIONS

        for tool in TOOLS:
            name = tool["function"]["name"]
            assert name in TOOL_FUNCTIONS, f"Missing implementation for tool: {name}"
