"""Shared Docker config: key custody, Compose merging, and installer reruns."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import tomllib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from cryptography.fernet import Fernet

from turnstone.core.oauth.context import oauth_context
from turnstone.deploy.bootstrap_config import create_config

ROOT = Path(__file__).resolve().parents[1]
STACKS = (ROOT, ROOT / "turnstone/deploy")


@pytest.mark.parametrize("mode", ["local", "sso", "entra", "rfc8693"])
def test_generated_config_loads_in_runtime(tmp_path, monkeypatch, mode):
    from turnstone.console.server import create_app as create_console_app
    from turnstone.core import config
    from turnstone.core.oauth.oidc import load_oidc_config
    from turnstone.core.token_store.crypto import load_token_cipher_config
    from turnstone.server import create_app as create_server_app

    path = tmp_path / "config.toml"
    create_config(path, "https://turnstone.example.com:8443/")
    # The generated file is ready for local login. Operators can add SSO and
    # explicitly opt into delegation in its existing [oidc] section.
    capture = mode in {"entra", "rfc8693"}
    if mode != "local":
        with path.open("a") as stream:
            stream.write(
                'issuer = "https://identity.example.com"\n'
                'client_id = "test-client"\n'
                'client_secret = "test-secret"\n'
            )
            if capture:
                stream.write(f'capture_user_credential = true\nobo_grant_profile = "{mode}"\n')
    assert path.stat().st_mode & 0o777 == 0o600
    document = tomllib.loads(path.read_text())
    cipher = Fernet(document["security"]["mcp_token_encryption_key"])
    assert cipher.decrypt(cipher.encrypt(b"saved user token")) == b"saved user token"
    monkeypatch.setenv("TURNSTONE_CONFIG", str(path))
    monkeypatch.setattr(config, "_config_path", None)
    monkeypatch.setattr(config, "_cache", None)
    for key in os.environ:
        if key.startswith("TURNSTONE_OIDC_"):
            monkeypatch.delenv(key)
    assert load_token_cipher_config() is not None
    oidc = load_oidc_config()
    assert oidc.redirect_base == "https://turnstone.example.com:8443"
    assert oidc.enabled is (mode != "local")
    assert oidc.password_enabled
    assert oidc.capture_user_credential is capture
    assert ("offline_access" in oidc.scopes.split()) is capture
    assert oidc.obo_grant_profile == (mode if capture else "entra")
    if mode != "local":
        assert oidc.issuer == "https://identity.example.com"
        assert oidc.client_id == "test-client"
        assert oidc.client_secret == "test-secret"
    # Exercise both application constructors without starting services or IdP
    # discovery: each must select the same bootstrap file and auth settings.
    node = create_server_app(
        workstreams=MagicMock(),
        global_queue=queue.Queue(),
        global_listeners=[],
        global_listeners_lock=threading.Lock(),
        skip_permissions=False,
    )
    console = create_console_app(collector=MagicMock())
    assert oauth_context(node.state).oidc_config == oidc
    assert oauth_context(console.state).oidc_config == oidc


@pytest.mark.parametrize(
    "origin",
    [
        "http://turnstone.example.com",
        "https://",
        "https://user:pw@example.com",
        "https://example.com/path",
        "https://example.com?",
        "https://example.com#",
        "https://example.com?x=1",
        "https://example.com/#fragment",
        "https://example.com:invalid",
        "https://example.com:65536",
        "https://a\nb.com",
    ],
)
def test_invalid_origin_never_creates_file(tmp_path, origin):
    path = tmp_path / "config.toml"
    with pytest.raises(ValueError):
        create_config(path, origin)
    assert not path.exists()


@pytest.mark.parametrize("symlink", [False, True])
def test_generator_never_replaces_existing_key(tmp_path, symlink):
    saved = tmp_path / "saved.toml"
    saved.write_text("saved config and encryption key")
    target = tmp_path / "config.toml" if symlink else saved
    if symlink:
        target.symlink_to(saved)
    with pytest.raises(FileExistsError):
        create_config(target, "https://turnstone.example.com")
    assert saved.read_text() == "saved config and encryption key"


def test_failed_ownership_change_removes_new_file(tmp_path, monkeypatch):
    import pwd

    def refused(*_args):
        raise PermissionError("cannot set owner")

    monkeypatch.setattr(os, "fchown", refused)
    path = tmp_path / "config.toml"
    with pytest.raises(PermissionError):
        create_config(
            path, "https://turnstone.example.com", owner=pwd.getpwuid(os.getuid()).pw_name
        )
    assert not path.exists()


@pytest.mark.parametrize("stack", STACKS, ids=["dev", "production"])
def test_every_config_consumer_uses_same_readonly_file(stack):
    base = yaml.safe_load((stack / "compose.yaml").read_text())
    overlay = yaml.safe_load((stack / "compose.config.yaml").read_text())
    consumers = {
        name
        for name in base["services"]
        if name == "console" or name == "server" or name.startswith("node-")
    }
    assert set(overlay["services"]) == consumers
    for service in overlay["services"].values():
        assert service["environment"] == {"TURNSTONE_CONFIG": "/run/turnstone/config.toml"}
        assert service["volumes"] == [
            {
                "type": "bind",
                "source": "./config.toml",
                "target": "/run/turnstone/config.toml",
                "read_only": True,
                "bind": {"create_host_path": False, "selinux": "z"},
            }
        ]


def compose_config(stack, tmp_path, *overlays, automatic=False):
    if (
        not shutil.which("docker")
        or subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=15,
        ).returncode
    ):
        pytest.skip("Docker Compose is not installed")
    env_file = tmp_path / "compose-test.env"
    env_file.touch()
    command = [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "--profile",
        "extra",
    ]
    if not automatic:
        command.extend(["-f", str(stack / "compose.yaml")])
    for overlay in overlays:
        command.extend(["-f", str(stack / overlay)])
    result = subprocess.run(
        [*command, "config", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=stack,
        env={
            "PATH": os.environ["PATH"],
            "TURNSTONE_JWT_SECRET": "test-secret-" * 4,
            "POSTGRES_PASSWORD": "test-password",
        },
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["services"]


@pytest.mark.parametrize("stack", STACKS, ids=["dev", "production"])
def test_compose_merges_without_losing_identity_or_volumes(stack, tmp_path):
    base = compose_config(stack, tmp_path)
    configured = compose_config(stack, tmp_path, "compose.config.yaml")
    for name, service in base.items():
        actual = configured[name]
        original_env = service.get("environment", {})
        assert actual.get("environment", {}).items() >= original_env.items()
        assert all(volume in actual.get("volumes", []) for volume in service.get("volumes", []))
        mounts = [
            v for v in actual.get("volumes", []) if v["target"] == "/run/turnstone/config.toml"
        ]
        if name == "console" or name == "server" or name.startswith("node-"):
            assert "TURNSTONE_CONFIG" not in original_env
            assert mounts[0]["source"] == str(stack / "config.toml")
            assert mounts[0]["read_only"]
            assert not mounts[0].get("bind", {}).get("create_host_path", False)
        else:
            assert not mounts


def run_installer_functions(tmp_path, commands, *, accept=True):
    ask = 'ask() { return "$ASK_RESULT"; }; ' if accept is not None else ""
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; DOCKER=docker_mock; docker_mock() { cat >/dev/null; }; ' + ask + commands,
            "installer-test",
            str(ROOT / "run.sh"),
        ],
        env={
            "PATH": os.environ["PATH"],
            "TURNSTONE_DIR": str(tmp_path),
            "ASK_RESULT": "0" if accept else "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
        start_new_session=True,
    )
    return result


@pytest.mark.parametrize("node_count", [1, 3, 10])
def test_installer_keeps_config_when_node_count_changes(tmp_path, node_count):
    # A path containing spaces exercises both Bash and Compose path handling.
    install = tmp_path / "install with spaces"
    install.mkdir()
    for name in ("compose.yaml", "compose.config.yaml"):
        shutil.copyfile(ROOT / name, install / name)
    path = install / "config.toml"
    create_config(path, "https://turnstone.example.com")
    with path.open("a") as stream:
        stream.write(
            'issuer = "https://identity.example.com"\n'
            'client_id = "test-client"\n'
            'client_secret = "private-test-client-secret"\n'
            'capture_user_credential = true\nobo_grant_profile = "rfc8693"\n'
        )
    saved = path.read_bytes()
    result = run_installer_functions(
        install,
        f"NODE_COUNT=2; prepare_shared_config; write_compose_override; NODE_COUNT={node_count}; ask() {{ return 1; }}; prepare_shared_config; write_compose_override",
    )
    assert result.returncode == 0, result.stderr
    assert path.read_bytes() == saved
    assert "private-test-client-secret" not in result.stdout + result.stderr
    assert (
        saved.decode().split('mcp_token_encryption_key = "')[1].split('"')[0]
        not in result.stdout + result.stderr
    )
    services = compose_config(install, tmp_path, automatic=True)
    for name, service in services.items():
        if name == "console" or name.startswith("node-"):
            assert service["environment"]["TURNSTONE_CONFIG"] == "/run/turnstone/config.toml"
            assert any(
                v["source"] == str(path) and v["read_only"]
                for v in service["volumes"]
                if v["type"] == "bind"
            )
        if name.startswith("node-"):
            assert ("extra" in service.get("profiles", [])) == (int(name[5:]) > node_count)


def test_installer_decline_keeps_default_stack(tmp_path):
    result = run_installer_functions(
        tmp_path, "NODE_COUNT=10; prepare_shared_config; write_compose_override", accept=False
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / "compose.override.yaml").exists()


def test_noninteractive_installer_defaults_to_no_shared_config(tmp_path):
    result = run_installer_functions(
        tmp_path, "NODE_COUNT=10; prepare_shared_config; write_compose_override", accept=None
    )
    assert result.returncode == 0, result.stderr
    assert "assuming 'n'" in result.stderr
    assert not (tmp_path / "config.toml").exists()


@pytest.mark.parametrize("existing", [False, True])
def test_installer_private_staging_and_no_clobber(tmp_path, existing):
    path = tmp_path / "config.toml"
    if existing:
        path.write_text("existing config\n")
    result = run_installer_functions(
        tmp_path,
        r"""
docker_mock() {
    local stage=""
    while [ "$#" -gt 0 ]; do
        if [ "$1" = -v ]; then stage="${2%:/bootstrap:rw,z}"; break; fi
        shift
    done
    [ "$(stat -c %a "$stage")" = 777 ] || return 1
    [ "$(stat -c %a "$(dirname "$stage")")" = 700 ] || return 1
    (umask 077; printf 'new config\n' >"$stage/config.toml")
}
create_shared_config https://turnstone.example.com
""",
    )
    assert (result.returncode != 0) == existing, result.stderr
    assert path.read_text() == ("existing config\n" if existing else "new config\n")
    if not existing:
        assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".turnstone-bootstrap.*"))


@pytest.mark.parametrize(
    "filename",
    [
        "compose.override.yaml",
        "compose.override.yml",
        "docker-compose.override.yaml",
        "docker-compose.override.yml",
    ],
)
def test_installer_preserves_custom_override(tmp_path, filename):
    for name in ("compose.yaml", "compose.config.yaml"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    create_config(tmp_path / "config.toml", "https://turnstone.example.com")
    saved = (tmp_path / "config.toml").read_bytes()
    path = tmp_path / filename
    original = "services:\n  node-1:\n    environment: { CUSTOM: keep }\n"
    path.write_text(original)
    before = compose_config(tmp_path, tmp_path, automatic=True)
    result = run_installer_functions(
        tmp_path, "NODE_COUNT=3; prepare_shared_config; write_compose_override"
    )
    assert result.returncode == 0, result.stderr
    assert path.read_text() == original
    assert (tmp_path / "config.toml").read_bytes() == saved
    if filename != "compose.override.yaml":
        assert not (tmp_path / "compose.override.yaml").exists()
    assert compose_config(tmp_path, tmp_path, automatic=True) == before


@pytest.mark.parametrize("target_exists", [False, True])
def test_installer_preserves_override_symlinks(tmp_path, target_exists):
    target = tmp_path / "custom.yaml"
    if target_exists:
        target.write_text("# turnstone run.sh — saved deployment choices\n")
    path = tmp_path / "compose.override.yaml"
    path.symlink_to(target)
    result = run_installer_functions(
        tmp_path, "NODE_COUNT=3; prepare_shared_config; write_compose_override"
    )
    assert result.returncode == 0, result.stderr
    assert path.is_symlink()
    assert target.exists() == target_exists
    if target_exists:
        assert target.read_text() == "# turnstone run.sh — saved deployment choices\n"


def test_installer_never_regenerates_missing_saved_key(tmp_path):
    result = run_installer_functions(
        tmp_path, "NODE_COUNT=10; CONFIG_ENABLED=1; write_compose_override; prepare_shared_config"
    )
    assert result.returncode != 0
    assert "Restore it from backup" in result.stderr
    assert not (tmp_path / "config.toml").exists()


def test_installer_upgrades_legacy_node_override(tmp_path):
    path = tmp_path / "compose.override.yaml"
    path.write_text("# turnstone run.sh — node-count limiter (safe to delete)\nservices: {}\n")
    result = run_installer_functions(tmp_path, "NODE_COUNT=3; write_compose_override")
    assert result.returncode == 0, result.stderr
    assert len(yaml.safe_load(path.read_text())["services"]) == 7
