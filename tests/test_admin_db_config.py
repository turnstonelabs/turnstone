"""Tests for turnstone-admin DB configuration precedence.

Locks in the alignment with turnstone-server:
  CLI / config.toml [database]  >  TURNSTONE_DB_* env  >  hardcoded default

The motivation is to keep DB secrets in config.toml (see
feedback_secrets_not_in_env) rather than forcing operators to export
TURNSTONE_DB_URL before every admin invocation.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

import turnstone.core.config as config_mod
from turnstone.admin import _get_storage


def _reset_cache() -> None:
    config_mod._cache = None
    config_mod._config_path = None


def _build_args(config_path: str | None) -> argparse.Namespace:
    """Build an args namespace the way admin.main() does.

    Skips ``add_config_arg`` (which reads ``sys.argv``) — the test
    constructs the args programmatically instead.
    """
    config_mod.set_config_path(config_path or "/nonexistent/turnstone-admin-test.toml")
    parser = argparse.ArgumentParser()
    config_mod.apply_config(parser, ["database"])
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list-users")
    return parser.parse_args(["list-users"])


@pytest.fixture(autouse=True)
def _clear_db_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clean slate: no TURNSTONE_DB_* env vars unless a test sets them."""
    for var in (
        "TURNSTONE_DB_BACKEND",
        "TURNSTONE_DB_URL",
        "TURNSTONE_DB_PATH",
        "TURNSTONE_DB_POOL_SIZE",
        "TURNSTONE_DB_SSLMODE",
        "TURNSTONE_DB_SSLROOTCERT",
        "TURNSTONE_DB_SSLCERT",
        "TURNSTONE_DB_SSLKEY",
        "TURNSTONE_CONFIG",
    ):
        monkeypatch.delenv(var, raising=False)
    _reset_cache()
    yield
    _reset_cache()


def test_defaults_to_sqlite_when_neither_config_nor_env_set() -> None:
    args = _build_args(None)
    with patch("turnstone.core.storage.init_storage") as init:
        _get_storage(args)
    assert init.call_args.args == ("sqlite",)
    assert init.call_args.kwargs["url"] == ""
    assert init.call_args.kwargs["path"] == ""
    assert init.call_args.kwargs["pool_size"] == 2


def test_config_toml_database_section_drives_init_storage(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[database]\n"
        'backend = "postgresql"\n'
        'url = "postgresql+psycopg://fromconfig:x@host/db"\n'
        "pool_size = 5\n"
        'sslmode = "verify-full"\n'
        'sslrootcert = "/etc/ssl/ca.pem"\n'
        'sslcert = "/etc/ssl/client.pem"\n'
        'sslkey = "/etc/ssl/client.key"\n'
    )
    args = _build_args(str(cfg))
    with patch("turnstone.core.storage.init_storage") as init:
        _get_storage(args)
    assert init.call_args.args == ("postgresql",)
    kw = init.call_args.kwargs
    assert kw["url"] == "postgresql+psycopg://fromconfig:x@host/db"
    assert kw["pool_size"] == 5
    assert kw["sslmode"] == "verify-full"
    assert kw["sslrootcert"] == "/etc/ssl/ca.pem"
    assert kw["sslcert"] == "/etc/ssl/client.pem"
    assert kw["sslkey"] == "/etc/ssl/client.key"


def test_env_used_as_fallback_when_config_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TURNSTONE_DB_BACKEND", "postgresql")
    monkeypatch.setenv("TURNSTONE_DB_URL", "postgresql+psycopg://fromenv:x@host/db")
    monkeypatch.setenv("TURNSTONE_DB_POOL_SIZE", "7")
    monkeypatch.setenv("TURNSTONE_DB_SSLMODE", "require")

    args = _build_args(None)
    with patch("turnstone.core.storage.init_storage") as init:
        _get_storage(args)
    assert init.call_args.args == ("postgresql",)
    kw = init.call_args.kwargs
    assert kw["url"] == "postgresql+psycopg://fromenv:x@host/db"
    assert kw["pool_size"] == 7
    assert kw["sslmode"] == "require"


def test_config_toml_wins_over_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """config.toml beats env — operators should put secrets in TOML."""
    monkeypatch.setenv("TURNSTONE_DB_BACKEND", "sqlite")
    monkeypatch.setenv("TURNSTONE_DB_URL", "postgresql+psycopg://fromenv:x@host/db")
    monkeypatch.setenv("TURNSTONE_DB_SSLMODE", "require")

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[database]\n"
        'backend = "postgresql"\n'
        'url = "postgresql+psycopg://fromconfig:x@host/db"\n'
        'sslmode = "verify-full"\n'
    )
    args = _build_args(str(cfg))
    with patch("turnstone.core.storage.init_storage") as init:
        _get_storage(args)
    assert init.call_args.args == ("postgresql",)
    kw = init.call_args.kwargs
    assert kw["url"] == "postgresql+psycopg://fromconfig:x@host/db"
    assert kw["sslmode"] == "verify-full"


def test_partial_config_falls_through_to_env_per_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A key missing from [database] should fall back to its env var."""
    monkeypatch.setenv("TURNSTONE_DB_SSLMODE", "require")
    monkeypatch.setenv("TURNSTONE_DB_POOL_SIZE", "9")

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[database]\nbackend = "postgresql"\nurl = "postgresql+psycopg://fromconfig:x@host/db"\n'
    )
    args = _build_args(str(cfg))
    with patch("turnstone.core.storage.init_storage") as init:
        _get_storage(args)
    kw = init.call_args.kwargs
    assert kw["url"] == "postgresql+psycopg://fromconfig:x@host/db"
    assert kw["sslmode"] == "require"
    assert kw["pool_size"] == 9


def test_empty_string_in_config_beats_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`url = ""` in config.toml beats an env var.

    Locks in the `is not None` guard — a falsy-but-present TOML value
    should NOT silently fall through to the env fallback.
    """
    monkeypatch.setenv("TURNSTONE_DB_URL", "postgresql+psycopg://fromenv:x@host/db")
    cfg = tmp_path / "config.toml"
    cfg.write_text('[database]\nbackend = "sqlite"\nurl = ""\n')
    args = _build_args(str(cfg))
    with patch("turnstone.core.storage.init_storage") as init:
        _get_storage(args)
    assert init.call_args.kwargs["url"] == ""


def test_main_threads_config_toml_through_real_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: ``turnstone-admin --config <toml> list-users`` honors TOML.

    Covers the ``add_config_arg`` -> ``apply_config`` -> ``_get_storage``
    chain that the programmatic ``_build_args`` helper skips.
    """
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[database]\nbackend = "postgresql"\nurl = "postgresql+psycopg://fromcli:x@host/db"\n'
    )
    monkeypatch.setattr("sys.argv", ["turnstone-admin", "--config", str(cfg), "list-users"])

    fake_storage = patch("turnstone.core.storage.init_storage").start()
    fake_storage.return_value.list_users.return_value = []
    try:
        from turnstone.admin import main

        main()
    finally:
        patch.stopall()

    assert fake_storage.call_args.args == ("postgresql",)
    assert fake_storage.call_args.kwargs["url"] == "postgresql+psycopg://fromcli:x@host/db"


def test_get_storage_initializes_real_sqlite_backend(tmp_path: Path) -> None:
    """Drives the real ``init_storage`` boundary on a fresh sqlite file.

    Mock-only tests would miss a kwarg-name typo (sslmode -> ssl_mode).
    This test trips on any such drift because Alembic + the backend
    actually run.
    """
    from turnstone.core.storage import reset_storage

    db_file = tmp_path / "admin.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[database]\nbackend = "sqlite"\npath = "{db_file}"\n')
    args = _build_args(str(cfg))

    reset_storage()
    try:
        storage = _get_storage(args)
        assert storage.list_users() == []
    finally:
        reset_storage()
