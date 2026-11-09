"""Bootstrap secrets as references: resolved once at startup, or the process stops."""

from __future__ import annotations

import logging

import jwt
import pytest

import turnstone.core.auth as auth_module
import turnstone.core.secret_refs._resolver as resolver_module
import turnstone.core.session as session_module
from tests._secret_refs_helpers import install_fake_resolver
from tests._session_helpers import log_has_field
from turnstone.core.auth import JWT_AUD_CHANNEL
from turnstone.core.secret_refs import (
    SecretBackendError,
    SecretConfigError,
    resolve_bootstrap_secret,
)


def test_literal_and_empty_values_pass_through_without_a_resolver():
    assert resolver_module._instance is None
    assert resolve_bootstrap_secret("[auth] jwt_secret", "literal-value") == "literal-value"
    assert resolve_bootstrap_secret("[oidc] client_secret", "") == ""
    assert resolver_module._instance is None, "a literal must not build the resolver"


def test_a_resolvable_reference_returns_the_value(monkeypatch):
    install_fake_resolver(monkeypatch, {"/auth/jwt": "the-value"})
    assert resolve_bootstrap_secret("[auth] jwt_secret", "secret://fake/auth/jwt") == "the-value"


def test_a_store_that_is_down_with_nothing_cached_exits(monkeypatch, caplog):
    backend = install_fake_resolver(monkeypatch, {"/auth/jwt": "the-value"})
    backend.fail = SecretBackendError("store unreachable", retryable=True)
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exc:
        resolve_bootstrap_secret("[auth] jwt_secret", "secret://fake/auth/jwt")
    assert exc.value.code == 1
    assert any(log_has_field(r, "setting", "[auth] jwt_secret") for r in caplog.records)


def test_a_malformed_secrets_table_exits(monkeypatch):
    def broken_config():
        raise SecretConfigError("config.toml [secrets.vault] address: required")

    monkeypatch.setattr(resolver_module, "load_secrets_config", broken_config)
    with pytest.raises(SystemExit) as exc:
        resolve_bootstrap_secret("[auth] jwt_secret", "secret://vault/auth/jwt")
    assert exc.value.code == 1


def test_a_broken_config_file_read_here_first_exits_cleanly(monkeypatch):
    """The channel gateway reads config.toml for the first time inside the helper."""
    from turnstone.core.config import ConfigError

    def broken_file():
        raise ConfigError("/etc/turnstone/config.toml: Invalid statement (at line 1, column 1)")

    monkeypatch.setattr(resolver_module, "load_secrets_config", broken_file)
    with pytest.raises(SystemExit) as exc:
        resolve_bootstrap_secret("TURNSTONE_JWT_SECRET", "secret://file/run/turnstone/secrets/jwt")
    assert exc.value.code == 1


def test_a_malformed_reference_exits(monkeypatch):
    install_fake_resolver(monkeypatch, {})
    with pytest.raises(SystemExit) as exc:
        resolve_bootstrap_secret("[auth] jwt_secret", "secret://")
    assert exc.value.code == 1


def test_notify_tokens_are_signed_with_the_resolved_secret(monkeypatch):
    """The server's notify path shares the resolved value; the reference text is never the key."""
    monkeypatch.setattr(auth_module, "_resolved_jwt_secret", None)
    monkeypatch.setattr(session_module, "_notify_token_manager", None)
    monkeypatch.delenv("TURNSTONE_CHANNEL_AUTH_TOKEN", raising=False)
    install_fake_resolver(monkeypatch, {"/auth/jwt-signing-secret-for-notify": "r" * 48})
    reference = "secret://fake/auth/jwt-signing-secret-for-notify"
    monkeypatch.setenv("TURNSTONE_JWT_SECRET", reference)

    headers = session_module._notify_auth_headers()
    token = headers["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, "r" * 48, algorithms=["HS256"], audience=JWT_AUD_CHANNEL)
    assert claims["aud"] == JWT_AUD_CHANNEL
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, reference, algorithms=["HS256"], audience=JWT_AUD_CHANNEL)


def test_notify_goes_on_without_a_token_when_the_reference_cannot_be_resolved(monkeypatch, caplog):
    monkeypatch.setattr(auth_module, "_resolved_jwt_secret", None)
    monkeypatch.setattr(session_module, "_notify_token_manager", None)
    monkeypatch.delenv("TURNSTONE_CHANNEL_AUTH_TOKEN", raising=False)
    install_fake_resolver(monkeypatch, {})
    monkeypatch.setenv("TURNSTONE_JWT_SECRET", "secret://fake/auth/missing")
    with caplog.at_level(logging.WARNING):
        assert session_module._notify_auth_headers() == {}
    assert any("bootstrap_unresolved" in r.getMessage() for r in caplog.records)


def test_the_channel_gateway_resolves_its_secret_or_stops(monkeypatch):
    from turnstone.channels.cli import _gateway_jwt_secret

    install_fake_resolver(monkeypatch, {"/auth/jwt": "g" * 48})
    monkeypatch.setenv("TURNSTONE_JWT_SECRET", "secret://fake/auth/jwt")
    assert _gateway_jwt_secret() == "g" * 48
    monkeypatch.setenv("TURNSTONE_JWT_SECRET", "secret://fake/auth/missing")
    with pytest.raises(SystemExit):
        _gateway_jwt_secret()
    monkeypatch.delenv("TURNSTONE_JWT_SECRET", raising=False)
    assert _gateway_jwt_secret() == ""


def test_a_runtime_reference_may_not_resolve_to_a_bootstrap_secret(monkeypatch):
    """The file root and the Vault scope are the reach of everyone who may write a reference;
    a bootstrap secret kept there must not be theirs to read through a model key or MCP header."""
    from turnstone.core.secret_refs import SecretResolveError, resolve, resolve_mapping

    install_fake_resolver(
        monkeypatch, {"/auth/jwt": "J" * 48, "/alias/jwt": "J" * 48, "/models/key": "k" * 40}
    )
    assert resolve_bootstrap_secret("[auth] jwt_secret", "secret://fake/auth/jwt") == "J" * 48
    with pytest.raises(SecretResolveError, match="bootstrap secret"):
        resolve("secret://fake/alias/jwt")
    with pytest.raises(SecretResolveError, match="bootstrap secret"):
        resolve_mapping({"Authorization": "secret://fake/alias/jwt"})
    assert resolve("secret://fake/models/key") == "k" * 40


def test_a_literal_bootstrap_secret_is_protected_too(monkeypatch):
    from turnstone.core.secret_refs import SecretResolveError, resolve

    install_fake_resolver(monkeypatch, {"/alias/jwt": "L" * 48})
    assert resolve_bootstrap_secret("[auth] jwt_secret", "L" * 48) == "L" * 48
    with pytest.raises(SecretResolveError, match="bootstrap secret"):
        resolve("secret://fake/alias/jwt")


def test_a_cached_value_is_refused_once_the_bootstrap_secret_registers(monkeypatch):
    from turnstone.core.secret_refs import SecretResolveError, register_bootstrap_secret, resolve

    install_fake_resolver(monkeypatch, {"/alias/jwt": "C" * 48})
    assert resolve("secret://fake/alias/jwt") == "C" * 48
    register_bootstrap_secret("C" * 48)
    with pytest.raises(SecretResolveError, match="bootstrap secret"):
        resolve("secret://fake/alias/jwt")


def test_the_bootstrap_registry_does_not_leak_between_tests(monkeypatch):
    from turnstone.core.secret_refs import resolve

    install_fake_resolver(monkeypatch, {"/alias/jwt": "J" * 48})
    assert resolve("secret://fake/alias/jwt") == "J" * 48


def test_register_bootstrap_values_covers_a_tool_process(monkeypatch):
    """The CLI, the admin commands and doctor register both values before their first resolve."""
    from turnstone.core.bootstrap_secrets import register_bootstrap_values
    from turnstone.core.secret_refs import SecretResolveError, resolve

    monkeypatch.setattr(auth_module, "_resolved_jwt_secret", None)
    for name in ("ISSUER", "CLIENT_ID", "CLIENT_SECRET"):
        monkeypatch.delenv(f"TURNSTONE_OIDC_{name}", raising=False)
    monkeypatch.setattr("turnstone.core.config.load_config", lambda section=None: {})
    monkeypatch.setenv("TURNSTONE_JWT_SECRET", "t" * 48)
    monkeypatch.setenv("TURNSTONE_OIDC_ISSUER", "https://auth.example.com")
    monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_SECRET", "o" * 40)
    register_bootstrap_values()
    install_fake_resolver(
        monkeypatch, {"/alias/jwt": "t" * 48, "/alias/oidc": "o" * 40, "/key": "k" * 40}
    )
    with pytest.raises(SecretResolveError, match="bootstrap secret"):
        resolve("secret://fake/alias/jwt")
    with pytest.raises(SecretResolveError, match="bootstrap secret"):
        resolve("secret://fake/alias/oidc")
    assert resolve("secret://fake/key") == "k" * 40


def test_register_bootstrap_values_never_exits(monkeypatch):
    from turnstone.core.bootstrap_secrets import register_bootstrap_values

    monkeypatch.setattr(auth_module, "_resolved_jwt_secret", None)
    install_fake_resolver(monkeypatch, {})
    monkeypatch.setattr("turnstone.core.config.load_config", lambda section=None: {})
    monkeypatch.setenv("TURNSTONE_JWT_SECRET", "secret://fake/auth/missing-from-the-store")
    monkeypatch.setenv("TURNSTONE_OIDC_ISSUER", "https://auth.example.com")
    monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("TURNSTONE_OIDC_CLIENT_SECRET", "secret://fake/oidc/missing")
    register_bootstrap_values()  # logs, registers nothing, returns


def _first_call_lines(func, names: set[str]) -> dict[str, int]:
    """Line of the first call to each named function in *func*, from the AST, so a comment or a
    string that quotes a call cannot stand in for one."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    first: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
        if name in names and name not in first:
            first[name] = node.lineno
    return first


def _assert_registers_before_first_resolve(func, registrations: set[str]) -> None:
    resolves = {"load_model_registry", "create_mcp_client", "probe_api_key"}
    lines = _first_call_lines(func, registrations | resolves)
    first_resolve = min(lines[name] for name in resolves if name in lines)
    for name in registrations:
        assert name in lines, f"{func.__qualname__} never calls {name}"
        assert lines[name] < first_resolve, f"{func.__qualname__} calls {name} after a resolve"


def test_the_server_registers_both_bootstrap_secrets_before_its_first_resolve():
    """The guard refuses only registered values, so the order in main is the protection.
    A refactor that moved the OIDC load back into create_app would reopen the window."""
    import inspect

    import turnstone.server as server

    _assert_registers_before_first_resolve(server.main, {"load_jwt_secret", "load_oidc_config"})
    assert "oidc_config=oidc_config" in inspect.getsource(server.main)


def test_the_cli_registers_both_bootstrap_secrets_before_its_first_resolve():
    import turnstone.cli as cli

    _assert_registers_before_first_resolve(cli.main, {"register_bootstrap_values"})


def test_rerank_calibrate_registers_before_probing_a_stored_key():
    import turnstone.admin as admin

    _assert_registers_before_first_resolve(
        admin._cmd_rerank_calibrate, {"register_bootstrap_values"}
    )


def test_doctor_registers_before_loading_the_registry():
    import turnstone.doctor as doctor

    _assert_registers_before_first_resolve(
        doctor.resolve_doctor_brain, {"register_bootstrap_values"}
    )
