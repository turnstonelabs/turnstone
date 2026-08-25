"""End-to-end startup coverage for the interactive CLI entry point."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_cli_starts_with_fresh_database(tmp_path: Path) -> None:
    """A fresh CLI session must reach the prompt and shut down cleanly."""
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    config_path.chmod(0o600)

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
            "-m",
            "turnstone.cli",
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
