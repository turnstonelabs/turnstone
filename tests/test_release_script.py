from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RELEASE_SCRIPT = ROOT / "scripts" / "release.sh"
INSTALL_SCRIPT = ROOT / "run.sh"
CHART_PATH = Path("deploy") / "helm" / "turnstone" / "Chart.yaml"
CHART = (
    'apiVersion: v2\nname: turnstone\nversion: 0.3.0\nappVersion: "1.7.0"\n'
    "dependencies:\n  - name: postgresql\n    version: ~18.12.0\n"
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _release_repo(
    tmp_path: Path,
    branch: str,
    chart: str | None = CHART,
    tags: tuple[str, ...] = (),
) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "turnstone").mkdir()
    shutil.copy2(RELEASE_SCRIPT, repo / "scripts" / "release.sh")
    (repo / "pyproject.toml").write_text('[project]\nversion = "1.8.0"\n')
    (repo / "turnstone" / "__init__.py").write_text('__version__ = "1.8.0"\n')
    (repo / "uv.lock").write_text("version = 1\n")
    if chart is not None:
        (repo / CHART_PATH).parent.mkdir(parents=True)
        (repo / CHART_PATH).write_text(chart)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_uv.chmod(0o755)

    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "Release Test")
    _git(repo, "config", "user.email", "release-test@example.com")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-m", "initial")
    for tag in tags:
        _git(repo, "tag", tag)
    if branch != "main":
        _git(repo, "switch", "-c", branch)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    return repo, env


def _run_release(repo: Path, env: dict[str, str], version: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/release.sh", version],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("branch", "version", "app_version"),
    [
        ("main", "1.8.1", "1.8.1"),
        # A pre-release keeps dev's chart on the newest stable release.
        ("dev", "1.9.0a1", "1.8.0"),
        ("stable/1.8", "1.8.2", "1.8.2"),
    ],
)
def test_release_script_accepts_version_for_branch(
    tmp_path: Path,
    branch: str,
    version: str,
    app_version: str,
) -> None:
    repo, env = _release_repo(tmp_path, branch, tags=("v1.8.0",))

    result = _run_release(repo, env, version)

    assert result.returncode == 0, result.stderr
    assert _git(repo, "tag", "--points-at", "HEAD").stdout.strip() == f"v{version}"
    assert f'version = "{version}"' in (repo / "pyproject.toml").read_text()
    assert f'__version__ = "{version}"' in (repo / "turnstone" / "__init__.py").read_text()
    # A new default image is a new chart: its patch version moves, its dependencies don't.
    chart = (repo / CHART_PATH).read_text()
    assert f'appVersion: "{app_version}"' in chart
    assert "\nversion: 0.3.1\n" in chart
    assert "\n    version: ~18.12.0\n" in chart
    assert str(CHART_PATH) in _git(repo, "show", "--name-only", "HEAD").stdout
    assert _git(repo, "status", "--porcelain").stdout == ""


def test_release_script_points_dev_chart_at_newest_stable_tag(tmp_path: Path) -> None:
    tags = ("v1.7.12", "v1.8.9", "v1.8.10", "v1.9.0a1", "v1.9.0rc1")
    repo, env = _release_repo(tmp_path, "dev", tags=tags)

    result = _run_release(repo, env, "1.9.0a2")

    assert result.returncode == 0, result.stderr
    # Version order, not string order, and pre-release tags never count.
    assert 'appVersion: "1.8.10"' in (repo / CHART_PATH).read_text()


def test_release_script_bumps_a_two_digit_chart_patch(tmp_path: Path) -> None:
    chart = CHART.replace("version: 0.3.0", "version: 0.3.9")
    repo, env = _release_repo(tmp_path, "main", chart=chart)

    result = _run_release(repo, env, "1.8.1")

    assert result.returncode == 0, result.stderr
    assert "\nversion: 0.3.10\n" in (repo / CHART_PATH).read_text()


def test_release_script_never_moves_the_app_version_back(tmp_path: Path) -> None:
    # A clone missing the newest stable tag must not downgrade dev's chart.
    chart = CHART.replace('appVersion: "1.7.0"', 'appVersion: "1.8.5"')
    repo, env = _release_repo(tmp_path, "dev", chart=chart, tags=("v1.8.4",))
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    result = _run_release(repo, env, "1.9.0a2")

    assert result.returncode == 1
    assert "appVersion would move back from 1.8.5 to 1.8.4; fetch tags first" in result.stderr
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before
    assert _git(repo, "status", "--porcelain").stdout == ""


def test_release_script_leaves_an_unchanged_chart_alone(tmp_path: Path) -> None:
    chart = CHART.replace('appVersion: "1.7.0"', 'appVersion: "1.8.0"')
    repo, env = _release_repo(tmp_path, "dev", chart=chart, tags=("v1.8.0",))

    result = _run_release(repo, env, "1.9.0a1")

    assert result.returncode == 0, result.stderr
    assert (repo / CHART_PATH).read_text() == chart
    assert str(CHART_PATH) not in _git(repo, "show", "--name-only", "HEAD").stdout


def test_release_script_requires_a_plain_chart_version(tmp_path: Path) -> None:
    chart = CHART.replace("version: 0.3.0", "version: 0.3.0-rc1")
    repo, env = _release_repo(tmp_path, "main", chart=chart)
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    result = _run_release(repo, env, "1.8.1")

    assert result.returncode == 1
    assert f"{CHART_PATH} version is not a plain X.Y.Z" in result.stderr
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before
    assert _git(repo, "status", "--porcelain").stdout == ""


def test_release_script_pre_release_requires_a_stable_tag(tmp_path: Path) -> None:
    repo, env = _release_repo(tmp_path, "dev", tags=("v1.9.0a1",))
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    result = _run_release(repo, env, "1.9.0a2")

    assert result.returncode == 1
    assert "no stable vX.Y.Z tag for the chart's appVersion" in result.stderr
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert _git(repo, "tag").stdout == "v1.9.0a1\n"


def test_real_chart_has_the_form_the_script_rewrites() -> None:
    # release.sh replaces whole lines, so a trailing comment would be lost.
    lines = (ROOT / CHART_PATH).read_text().splitlines()
    app_lines = [line for line in lines if line.startswith("appVersion:")]
    version_lines = [line for line in lines if line.startswith("version:")]
    assert len(app_lines) == 1
    assert re.fullmatch(r'appVersion: "[^"#\s]+"', app_lines[0])
    assert len(version_lines) == 1
    assert re.fullmatch(r"version: \d+\.\d+\.\d+", version_lines[0])


@pytest.mark.parametrize(
    "chart",
    [
        pytest.param(None, id="no-chart"),
        pytest.param("apiVersion: v2\nname: turnstone\nversion: 0.3.0\n", id="no-appversion"),
    ],
)
def test_release_script_requires_a_chart_app_version(tmp_path: Path, chart: str | None) -> None:
    repo, env = _release_repo(tmp_path, "main", chart=chart)
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    result = _run_release(repo, env, "1.8.1")

    assert result.returncode == 1
    assert f"{CHART_PATH} has no appVersion line" in result.stderr
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert _git(repo, "tag").stdout == ""


@pytest.mark.parametrize(
    ("branch", "version", "message"),
    [
        ("main", "1.9.0rc1", "pre-releases must be cut from dev"),
        ("dev", "1.9.0", "stable releases must be cut from main"),
        ("stable/1.8", "1.8.2rc1", "pre-releases must be cut from dev"),
        ("stable/1.7", "1.8.1", "1.8.1 belongs on stable/1.8"),
        ("feature/release", "1.8.1", "releases must be cut from main, dev, or stable/X.Y"),
    ],
)
def test_release_script_rejects_wrong_branch_without_mutation(
    tmp_path: Path,
    branch: str,
    version: str,
    message: str,
) -> None:
    repo, env = _release_repo(tmp_path, branch)
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    result = _run_release(repo, env, version)

    assert result.returncode == 1
    assert message in result.stderr
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert _git(repo, "tag").stdout == ""


def test_release_script_rejects_detached_head(tmp_path: Path) -> None:
    repo, env = _release_repo(tmp_path, "main")
    _git(repo, "switch", "--detach")

    result = _run_release(repo, env, "1.8.1")

    assert result.returncode == 1
    assert "releases must be cut from a branch, not detached HEAD" in result.stderr
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert _git(repo, "tag").stdout == ""


def test_installer_defaults_new_clones_to_stable_main() -> None:
    script = INSTALL_SCRIPT.read_text()

    assert 'SOURCE_BRANCH="${TURNSTONE_BRANCH:-main}"' in script
    assert 'git clone --depth 1 --branch "$SOURCE_BRANCH" "$REPO_URL" "$INSTALL_DIR"' in script
