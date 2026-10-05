"""End-to-end startup coverage for the interactive CLI entry point."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from cryptography.fernet import Fernet

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.storage._sqlite import SQLiteBackend


@pytest.mark.parametrize("oauth_pool", [False, True], ids=["empty", "web-user-pool"])
def test_cli_starts_with_fresh_database(
    tmp_path: Path, sqlite_backend_factory: Callable[[str], SQLiteBackend], oauth_pool: bool
) -> None:
    """A fresh CLI session must reach the prompt and shut down cleanly."""
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    config_path.chmod(0o600)

    db_path = tmp_path / "turnstone.db"
    if oauth_pool:
        storage = sqlite_backend_factory(str(db_path))
        storage.create_mcp_server(
            server_id="web-oauth",
            name="web-oauth",
            transport="streamable-http",
            url="https://mcp.example.com/mcp",
            auth_type="oauth_user",
            oauth_client_id="client-abc",
        )
        storage.close()

    entrypoint = ["-m", "turnstone.cli"]
    if oauth_pool:
        entrypoint = [
            "-c",
            """
from turnstone.core.mcp_client import MCPClientManager
from turnstone.cli import main

original_start = MCPClientManager.start

def check_start(manager):
    original_start(manager)
    assert manager._user_token_sweep_s == 0
    assert manager._user_token_sweep_task is None
    print("CLI token sweep disabled")

MCPClientManager.start = check_start
main()
""",
        ]

    env = {key: value for key, value in os.environ.items() if not key.startswith("TURNSTONE_")}
    env.update(
        {
            "OPENAI_API_KEY": "dummy",
            "TURNSTONE_DB_BACKEND": "sqlite",
            "TURNSTONE_DB_PATH": str(db_path),
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            *entrypoint,
            "--config",
            str(config_path),
            "--model",
            "startup-test-model",
            "--retention-days",
            "0",
            "--no-judge",
        ],
        input="/exit\n",
        text=True,
        capture_output=True,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        timeout=60,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Type /help for commands" in result.stdout
    assert "Goodbye." in result.stdout
    if oauth_pool:
        assert "CLI token sweep disabled" in result.stdout


@pytest.mark.parametrize("failure", ["interrupt", "error"])
def test_a_failure_before_the_prompt_still_tears_the_cli_down(tmp_path: Path, failure: str) -> None:
    """The per-prompt work (closing parked tabs) runs under the loop's error handling."""
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    config_path.chmod(0o600)
    script = """
import sys
from turnstone import cli

failure = sys.argv.pop(1)
calls = []
original_close_all = cli._close_all_sessions
original_parked = cli._close_parked_tabs

def close_all(manager):
    print("TEARDOWN", flush=True)
    original_close_all(manager)

def parked(manager, tabs):
    calls.append(1)
    if len(calls) == 2:
        raise KeyboardInterrupt if failure == "interrupt" else RuntimeError("close failed")
    return original_parked(manager, tabs)

cli._close_all_sessions = close_all
cli._close_parked_tabs = parked
cli.main()
"""
    env = {key: value for key, value in os.environ.items() if not key.startswith("TURNSTONE_")}
    env.update(
        {
            "OPENAI_API_KEY": "dummy",
            "TURNSTONE_DB_BACKEND": "sqlite",
            "TURNSTONE_DB_PATH": str(tmp_path / "turnstone.db"),
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            failure,
            "--config",
            str(config_path),
            "--model",
            "startup-test-model",
            "--retention-days",
            "0",
            "--no-judge",
        ],
        input="\n\n/exit\n",
        text=True,
        capture_output=True,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        timeout=60,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "TEARDOWN" in result.stdout and "Goodbye." in result.stdout
    if failure == "error":
        # The CLI says what failed and keeps going to the next prompt.
        assert "Error: close failed" in result.stdout


def test_a_slash_command_that_fails_or_is_interrupted_returns_to_the_prompt(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    config_path.chmod(0o600)
    script = """
from turnstone import cli
from turnstone.core.session import ChatSession

original = ChatSession.handle_command

def handle_command(self, line):
    if line == "/boom":
        raise RuntimeError("boom failed")
    if line == "/stop":
        raise KeyboardInterrupt
    return original(self, line)

ChatSession.handle_command = handle_command
cli.main()
"""
    env = {key: value for key, value in os.environ.items() if not key.startswith("TURNSTONE_")}
    env.update(
        {
            "OPENAI_API_KEY": "dummy",
            "TURNSTONE_DB_BACKEND": "sqlite",
            "TURNSTONE_DB_PATH": str(tmp_path / "turnstone.db"),
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            "--config",
            str(config_path),
            "--model",
            "startup-test-model",
            "--retention-days",
            "0",
            "--no-judge",
        ],
        input="/new\n/boom\n/stop\n/exit\n",
        text=True,
        capture_output=True,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        timeout=60,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Error: boom failed" in result.stdout, output
    assert "Interrupted." in result.stdout, output
    # Both returned to the prompt: the /exit after them still ran.
    assert "Goodbye." in result.stdout, output


@pytest.mark.parametrize("oauth_pool", [False, True], ids=["model-only", "web-user-pool"])
def test_cli_model_auth_with_mcp_disabled_persona(
    tmp_path: Path, sqlite_backend_factory: Callable[[str], SQLiteBackend], oauth_pool: bool
) -> None:
    """Actual CLI startup mints independently of MCP and joins OAuth before crypto closes."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'''[security]
mcp_token_encryption_key = "{Fernet.generate_key().decode()}"
[model]
default = "gateway"
[models.gateway]
model = "runtime-cli"
base_url = "https://model.test/v1"
auth_mode = "entra_app"
obo_audience = "api://cli"
''',
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    db_path = tmp_path / "turnstone.db"
    if oauth_pool:
        storage = sqlite_backend_factory(str(db_path))
        storage.create_mcp_server(
            server_id="web-oauth",
            name="web-oauth",
            transport="streamable-http",
            url="https://mcp.example.com/mcp",
            auth_type="oauth_user",
            oauth_client_id="client-abc",
        )
        storage.close()
    script = """
import asyncio
import threading
import httpx
from turnstone import cli
from turnstone.core import mcp_crypto
from turnstone.core.mcp_client import MCPClientManager
from turnstone.core.oauth import http, oidc
from turnstone.core.oauth.context import oauth_context
from turnstone.core.personas import PersonaSnapshot
from tests._oidc_test_helpers import make_oidc_config

async def initialize(state):
    oauth_context(state).oidc_config = make_oidc_config()

def make_client():
    loop = asyncio.get_running_loop()
    def post(request):
        assert asyncio.get_running_loop() is loop
        assert threading.current_thread().name == "oauth-loop"
        assert request.url.path == "/token"
        return httpx.Response(200, json={"access_token": "cli-minted", "expires_in": 3600})
    return httpx.AsyncClient(transport=httpx.MockTransport(post))

original_start = MCPClientManager.start
def start(manager):
    original_start(manager)
    assert manager._user_token_sweep_s == 0
    assert manager._user_token_sweep_task is None
    print("CLI token sweep disabled")

original_close = mcp_crypto.close_mcp_crypto_state
def close(state):
    context = oauth_context(state)
    assert context.token_store is not None
    assert context.runtime is not None
    assert not context.runtime._thread.is_alive()
    assert context.http_client.is_closed
    assert not any(t.name == "mcp-loop" for t in threading.enumerate())
    original_close(state)
    print("OAuth joined before crypto close")

class Session(cli.ChatSession):
    def __init__(self, *args, **kwargs):
        kwargs["persona_snapshot"] = PersonaSnapshot("no-mcp", "Test", None, False, False)
        super().__init__(*args, **kwargs)
        assert self._mcp_client is None
        assert self._model_token_client is not None
        assert self._model_backend_auth_token("gateway") == "cli-minted"
        print("CLI model auth with MCP disabled")

oidc.initialize_oidc_state = initialize
http.json_http_client = make_client
MCPClientManager.start = start
mcp_crypto.close_mcp_crypto_state = close
cli.ChatSession = Session
cli.main()
"""
    env = {key: value for key, value in os.environ.items() if not key.startswith("TURNSTONE_")}
    env.update(
        OPENAI_API_KEY="dummy",
        TURNSTONE_DB_BACKEND="sqlite",
        TURNSTONE_DB_PATH=str(db_path),
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            "--config",
            str(config_path),
            "--model",
            "startup-test-model",
            "--retention-days",
            "0",
            "--no-judge",
        ],
        input="/exit\n",
        text=True,
        capture_output=True,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CLI model auth with MCP disabled" in result.stdout
    assert "OAuth joined before crypto close" in result.stdout
    assert "Goodbye." in result.stdout
    assert ("CLI token sweep disabled" in result.stdout) == oauth_pool
