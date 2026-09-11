"""End-to-end startup coverage for the interactive CLI entry point."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

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
