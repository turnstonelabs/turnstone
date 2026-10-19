"""Startup coverage for a node or console whose stored settings can't be read (#1406).

A host that started anyway would run on every setting's registry default: the node's startup
prune on the default retention, the console's TLS sync storing ``false`` over the operator's
``true``. The entry-point cases run the real ``main()`` in a subprocess against a fresh SQLite
database, with one storage read made to fail and the server itself stubbed out.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from turnstone.core.config_store import ConfigStore
from turnstone.core.settings_registry import SETTINGS
from turnstone.core.storage._sqlite import SQLiteBackend

if TYPE_CHECKING:
    from collections.abc import Callable

_REPO = Path(__file__).resolve().parents[1]
_SECRET = "test-jwt-secret-minimum-32-chars!"

# Runs an entry point with one SQLiteBackend read failing. The server is stubbed (the node
# runs ``uvicorn.Server``, the console ``uvicorn.run``), so a host that starts anyway prints
# SERVING and exits 0 instead of serving; port 0 keeps a missed stub off a real port. The
# node's startup prune prints PRUNED.
_SCRIPT = """
import os
import sys

import uvicorn

import turnstone.core.memory
from turnstone.core.storage._sqlite import SQLiteBackend


def unreadable(self, *args, **kwargs):
    raise RuntimeError("database went away")


def serve(*args, **kwargs):
    print("SERVING")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


entry, read = sys.argv[1], sys.argv[2]
setattr(SQLiteBackend, read, unreadable)
uvicorn.run = serve
uvicorn.Server.run = serve
turnstone.core.memory.prune_workstreams = lambda **kwargs: print("PRUNED")
sys.argv = [entry, "--host", "127.0.0.1", "--port", "0"]
if entry == "node":
    from turnstone.server import main
else:
    from turnstone.console.server import main
main()
"""


def _run(tmp_path: Path, entry: str, unreadable: str) -> subprocess.CompletedProcess[str]:
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    config_path.chmod(0o600)
    env = {key: value for key, value in os.environ.items() if not key.startswith("TURNSTONE_")}
    env.update(
        {
            "TURNSTONE_CONFIG": str(config_path),
            "TURNSTONE_DB_BACKEND": "sqlite",
            "TURNSTONE_DB_PATH": str(tmp_path / "turnstone.db"),
            "TURNSTONE_JWT_SECRET": _SECRET,
        }
    )
    return subprocess.run(
        [sys.executable, "-c", _SCRIPT, entry, unreadable],
        text=True,
        capture_output=True,
        cwd=_REPO,
        env=env,
        timeout=60,
        check=False,
    )


def _unreadable(*args: object, **kwargs: object) -> dict[str, str]:
    raise RuntimeError("database went away")


def test_node_exits_before_its_startup_prune_when_settings_cannot_load(tmp_path: Path) -> None:
    result = _run(tmp_path, "node", "get_system_settings_bulk")

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "settings.load_failed" in output
    assert "database went away" in output
    assert "PRUNED" not in result.stdout
    assert "SERVING" not in result.stdout


def test_node_exits_when_its_model_definitions_cannot_load(tmp_path: Path) -> None:
    # A config.toml-only registry would hold the node degraded until a Sync.
    result = _run(tmp_path, "node", "list_model_definitions")

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "database went away" in output
    assert "SERVING" not in result.stdout


def test_console_exits_without_storing_tls_off_when_settings_cannot_load(
    tmp_path: Path, sqlite_migrated_template: Path
) -> None:
    db_path = tmp_path / "turnstone.db"
    shutil.copyfile(sqlite_migrated_template, db_path)
    with contextlib.closing(SQLiteBackend(str(db_path), create_tables=False)) as storage:
        storage.upsert_system_setting(key="tls.enabled", value="true", changed_by="admin")

    result = _run(tmp_path, "console", "get_system_settings_bulk")

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "settings.load_failed" in output
    assert "SERVING" not in result.stdout
    with contextlib.closing(sqlite3.connect(db_path)) as con:
        rows = con.execute(
            "SELECT value, changed_by FROM system_settings WHERE key = 'tls.enabled'"
        ).fetchall()
    assert rows == [("true", "admin")]


def test_console_startup_ends_when_its_settings_cannot_load(
    tmp_path: Path,
    sqlite_backend_factory: Callable[[str], SQLiteBackend],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The lifespan loads its own store, a second read after main()'s.
    from starlette.applications import Starlette

    from turnstone.console.server import _lifespan

    storage = sqlite_backend_factory(str(tmp_path / "console.db"))
    monkeypatch.setattr(storage, "get_system_settings_bulk", _unreadable)
    app = Starlette()
    app.state.auth_storage = storage

    async def start() -> None:
        async with _lifespan(app):
            pass

    with (
        caplog.at_level("ERROR", logger="turnstone.console.server"),
        pytest.raises(SystemExit) as exc_info,
    ):
        asyncio.run(start())

    assert exc_info.value.code == 1
    assert "settings.load_failed" in " ".join(record.message for record in caplog.records)


@pytest.fixture
def _no_seed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in SETTINGS:
        monkeypatch.delenv("TURNSTONE_" + key.replace(".", "_").upper(), raising=False)


@pytest.mark.usefixtures("_no_seed_env")
def test_env_seeding_skips_a_value_the_setting_refuses(
    tmp_path: Path,
    sqlite_backend_factory: Callable[[str], SQLiteBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from turnstone.console.server import _seed_config_from_env

    storage = sqlite_backend_factory(str(tmp_path / "console.db"))
    monkeypatch.setenv("TURNSTONE_TLS_ENABLED", "maybe")

    _seed_config_from_env(ConfigStore(storage), storage)

    assert storage.get_system_setting("tls.enabled") is None


@pytest.mark.usefixtures("_no_seed_env")
def test_env_seeding_stops_on_a_storage_failure(
    tmp_path: Path,
    sqlite_backend_factory: Callable[[str], SQLiteBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from turnstone.console.server import _seed_config_from_env

    storage = sqlite_backend_factory(str(tmp_path / "console.db"))
    store = ConfigStore(storage)
    monkeypatch.setenv("TURNSTONE_TLS_ENABLED", "true")
    monkeypatch.setattr(storage, "upsert_system_setting", _unreadable)

    with pytest.raises(RuntimeError, match="database went away"):
        _seed_config_from_env(store, storage)
