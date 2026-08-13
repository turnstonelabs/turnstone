"""Console database bootstrap configuration precedence."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

import turnstone.core.config as config_mod
from turnstone.console.server import _get_console_storage

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


_DB_ENV_VARS = (
    "TURNSTONE_DB_BACKEND",
    "TURNSTONE_DB_URL",
    "TURNSTONE_DB_PATH",
    "TURNSTONE_DB_POOL_SIZE",
    "TURNSTONE_DB_SSLMODE",
    "TURNSTONE_DB_SSLROOTCERT",
    "TURNSTONE_DB_SSLCERT",
    "TURNSTONE_DB_SSLKEY",
    "TURNSTONE_DB_LISTEN_URL",
    "TURNSTONE_CONFIG",
)


def _reset_config_cache() -> None:
    config_mod._cache = None
    config_mod._config_path = None


def _build_args(config_path: str | None) -> argparse.Namespace:
    config_mod.set_config_path(config_path or "/nonexistent/turnstone-console-test.toml")
    parser = argparse.ArgumentParser()
    config_mod.apply_config(parser, ["database"])
    return parser.parse_args([])


@pytest.fixture(autouse=True)
def _clean_database_configuration(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for variable in _DB_ENV_VARS:
        monkeypatch.delenv(variable, raising=False)
    _reset_config_cache()
    yield
    _reset_config_cache()


def test_config_toml_database_section_drives_console_storage(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "[database]\n"
        'backend = "postgresql"\n'
        'url = "postgresql+psycopg://from-config/db"\n'
        'path = "/ignored-for-postgresql"\n'
        "pool_size = 7\n"
        'sslmode = "verify-full"\n'
        'sslrootcert = "/certs/root.pem"\n'
        'sslcert = "/certs/client.pem"\n'
        'sslkey = "/certs/client.key"\n'
        'listen_url = "postgresql+psycopg://listener/db"\n'
    )

    with patch("turnstone.core.storage.init_storage") as init_storage:
        storage = _get_console_storage(_build_args(str(config)))

    assert storage is init_storage.return_value
    assert init_storage.call_args.args == ("postgresql",)
    assert init_storage.call_args.kwargs == {
        "path": "/ignored-for-postgresql",
        "url": "postgresql+psycopg://from-config/db",
        "pool_size": 7,
        "sslmode": "verify-full",
        "sslrootcert": "/certs/root.pem",
        "sslcert": "/certs/client.pem",
        "sslkey": "/certs/client.key",
        "listen_url": "postgresql+psycopg://listener/db",
    }


def test_environment_drives_console_storage_when_config_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TURNSTONE_DB_BACKEND", "postgresql")
    monkeypatch.setenv("TURNSTONE_DB_URL", "postgresql+psycopg://from-env/db")
    monkeypatch.setenv("TURNSTONE_DB_POOL_SIZE", "9")
    monkeypatch.setenv("TURNSTONE_DB_SSLMODE", "require")
    monkeypatch.setenv("TURNSTONE_DB_LISTEN_URL", "postgresql+psycopg://listener-env/db")

    with patch("turnstone.core.storage.init_storage") as init_storage:
        _get_console_storage(_build_args(None))

    assert init_storage.call_args.args == ("postgresql",)
    assert init_storage.call_args.kwargs["url"] == "postgresql+psycopg://from-env/db"
    assert init_storage.call_args.kwargs["pool_size"] == 9
    assert init_storage.call_args.kwargs["sslmode"] == "require"
    assert init_storage.call_args.kwargs["listen_url"] == "postgresql+psycopg://listener-env/db"


def test_config_values_win_over_environment_per_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TURNSTONE_DB_BACKEND", "sqlite")
    monkeypatch.setenv("TURNSTONE_DB_URL", "postgresql+psycopg://from-env/db")
    monkeypatch.setenv("TURNSTONE_DB_POOL_SIZE", "11")
    monkeypatch.setenv("TURNSTONE_DB_SSLMODE", "require")
    config = tmp_path / "config.toml"
    config.write_text(
        "[database]\n"
        'backend = "postgresql"\n'
        'url = "postgresql+psycopg://from-config/db"\n'
        "pool_size = 5\n"
        'sslmode = "verify-full"\n'
    )

    with patch("turnstone.core.storage.init_storage") as init_storage:
        _get_console_storage(_build_args(str(config)))

    assert init_storage.call_args.args == ("postgresql",)
    assert init_storage.call_args.kwargs["url"] == "postgresql+psycopg://from-config/db"
    assert init_storage.call_args.kwargs["pool_size"] == 5
    assert init_storage.call_args.kwargs["sslmode"] == "verify-full"


def test_explicit_empty_config_value_beats_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TURNSTONE_DB_URL", "postgresql+psycopg://from-env/db")
    monkeypatch.setenv("TURNSTONE_DB_LISTEN_URL", "postgresql+psycopg://listener-env/db")
    config = tmp_path / "config.toml"
    config.write_text('[database]\nbackend = "sqlite"\nurl = ""\nlisten_url = ""\n')

    with patch("turnstone.core.storage.init_storage") as init_storage:
        _get_console_storage(_build_args(str(config)))

    assert init_storage.call_args.kwargs["url"] == ""
    assert init_storage.call_args.kwargs["listen_url"] == ""
