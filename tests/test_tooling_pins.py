"""Developer tooling pins that must agree with what CI runs."""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_dev_extra_ruff_matches_pre_commit_hook() -> None:
    # CI's lint job runs ruff through pre-commit, while developers run the dev extra's ruff. A newer
    # ruff brings new checks, so the two copies disagree about a clean tree when they drift.
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    pins = [
        spec
        for spec in project["project"]["optional-dependencies"]["dev"]
        if spec.startswith("ruff")
    ]
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    (hook,) = (
        repo
        for repo in config["repos"]
        if repo["repo"] == "https://github.com/astral-sh/ruff-pre-commit"
    )
    assert pins == [f"ruff=={hook['rev'].removeprefix('v')}"]
