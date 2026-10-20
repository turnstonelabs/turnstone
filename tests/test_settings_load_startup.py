"""Startup coverage for a node or console that can't read or apply its stored settings (#1406).

A host that started anyway would run on every setting's registry default: the node's startup
prune on the default retention, a console skipping the TLS it has enabled. The entry-point cases
run the real ``main()`` in a subprocess against a migrated SQLite database, with one storage read
made to fail and the server itself stubbed out; the lifespan and env seeding cases run in process.
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
import sqlalchemy as sa

from turnstone.core.config_store import ConfigStore
from turnstone.core.settings_registry import SETTINGS
from turnstone.core.storage import StorageUnavailableError
from turnstone.core.storage._sqlite import SQLiteBackend

if TYPE_CHECKING:
    from collections.abc import Callable

_REPO = Path(__file__).resolve().parents[1]
_SECRET = "test-jwt-secret-minimum-32-chars!"

# Runs an entry point, optionally with one SQLiteBackend read failing: with a storage error, or
# with a RuntimeError standing in for a bug. The server is stubbed (the node runs
# ``uvicorn.Server``, the console ``uvicorn.run``), so a host that starts prints SERVING and the
# lifespan mode it asked for, then exits 0 instead of serving; port 0 keeps a missed stub off a
# real port. The node's startup prune prints PRUNED.
_SCRIPT = """
import os
import sys

import uvicorn

import turnstone.core.memory
from turnstone.core.storage import StorageUnavailableError
from turnstone.core.storage._sqlite import SQLiteBackend

entry, read, bug = sys.argv[1], sys.argv[2], sys.argv[3] == "bug"


def unreadable(self, *args, **kwargs):
    if bug:
        raise RuntimeError("a bug in the read")
    raise StorageUnavailableError("database went away")


def serve(*args, **kwargs):
    print(f"SERVING lifespan={kwargs.get('lifespan')}")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if read:
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


def _run(
    tmp_path: Path,
    entry: str,
    unreadable: str = "",
    config: str = "",
    *,
    bug: bool = False,
    **extra_env: str,
) -> subprocess.CompletedProcess[str]:
    config_path = tmp_path / "config.toml"
    config_path.write_text(config, encoding="utf-8")
    config_path.chmod(0o600)
    env = {key: value for key, value in os.environ.items() if not key.startswith("TURNSTONE_")}
    env.update(
        {
            "TURNSTONE_CONFIG": str(config_path),
            "TURNSTONE_DB_BACKEND": "sqlite",
            "TURNSTONE_DB_PATH": str(tmp_path / "turnstone.db"),
            "TURNSTONE_JWT_SECRET": _SECRET,
            "TURNSTONE_TLS_PEM_DIR": str(tmp_path / "tls"),
            **extra_env,
        }
    )
    return subprocess.run(
        [sys.executable, "-c", _SCRIPT, entry, unreadable, "bug" if bug else ""],
        text=True,
        capture_output=True,
        cwd=_REPO,
        env=env,
        timeout=60,
        check=False,
    )


@pytest.fixture
def db_path(tmp_path: Path, sqlite_migrated_template: Path) -> Path:
    """The database ``_run`` points at, already migrated so a boot skips the migrations."""
    path = tmp_path / "turnstone.db"
    shutil.copyfile(sqlite_migrated_template, path)
    return path


def _unreadable(*args: object, **kwargs: object) -> dict[str, str]:
    raise StorageUnavailableError("database went away")


def _stored_tls(db_path: Path) -> list[tuple[str, str]]:
    with contextlib.closing(sqlite3.connect(db_path)) as con:
        return con.execute(
            "SELECT value, changed_by FROM system_settings WHERE key = 'tls.enabled'"
        ).fetchall()


@pytest.mark.usefixtures("db_path")
def test_node_exits_before_its_startup_prune_when_settings_cannot_load(tmp_path: Path) -> None:
    result = _run(tmp_path, "node", "get_system_settings_bulk")

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    # The documented event name, as the node's structured log renders it.
    assert '"event": "settings.load_failed"' in output
    assert "database went away" in output
    assert "PRUNED" not in result.stdout
    assert "SERVING" not in result.stdout


@pytest.mark.usefixtures("db_path")
def test_node_exits_when_its_model_definitions_cannot_load(tmp_path: Path) -> None:
    # A config.toml-only registry would hold the node degraded until a Sync.
    result = _run(tmp_path, "node", "list_model_definitions")

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert '"event": "model_registry.load_failed"' in output
    assert "database went away" in output
    assert "Traceback" not in output
    assert "SERVING" not in result.stdout


@pytest.mark.usefixtures("db_path")
def test_node_exits_naming_a_model_definition_a_restart_wont_fix(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "node",
        config=(
            "[models.bad]\n"
            'model = "m"\n'
            'base_url = "http://model.example.com/v1"\n'
            'api_key = "dummy"\n'
            "context_window = 8192\n"
            "max_concurrency = -1\n"
        ),
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert '"event": "model_registry.config_invalid"' in output
    assert "max_concurrency" in output
    assert "Traceback" not in output
    assert "SERVING" not in result.stdout


@pytest.mark.usefixtures("db_path")
@pytest.mark.parametrize(
    ("entry", "read", "event"),
    [
        ("node", "get_system_settings_bulk", "settings.load_failed"),
        ("node", "list_model_definitions", "model_registry.load_failed"),
        ("console", "get_system_settings_bulk", "settings.load_failed"),
    ],
)
def test_a_boot_error_that_is_not_a_storage_error_keeps_its_traceback(
    tmp_path: Path, entry: str, read: str, event: str
) -> None:
    # A bug, not a read a restart retries: the traceback shows where it failed.
    result = _run(tmp_path, entry, read, bug=True)

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "Traceback" in output
    assert "RuntimeError: a bug in the read" in output
    assert event not in output
    assert "SERVING" not in result.stdout


def test_console_exits_without_storing_tls_off_when_settings_cannot_load(
    tmp_path: Path, db_path: Path
) -> None:
    with contextlib.closing(SQLiteBackend(str(db_path), create_tables=False)) as storage:
        storage.upsert_system_setting(key="tls.enabled", value="true", changed_by="admin")

    result = _run(tmp_path, "console", "get_system_settings_bulk")

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "settings.load_failed" in output
    assert "SERVING" not in result.stdout
    assert _stored_tls(db_path) == [("true", "admin")]


def test_console_with_tls_enabled_exits_when_its_ca_cannot_load(
    tmp_path: Path, db_path: Path
) -> None:
    # Serving without it would let nodes that start meanwhile fall back to plain HTTP.
    with contextlib.closing(SQLiteBackend(str(db_path), create_tables=False)) as storage:
        storage.upsert_system_setting(key="tls.enabled", value="true", changed_by="admin")

    result = _run(tmp_path, "console", "load_tls_ca")

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "tls.init_failed" in output
    assert "database went away" in output
    assert "SERVING" not in result.stdout


def test_a_console_started_without_tls_can_take_it_from_the_environment_later(
    tmp_path: Path, db_path: Path
) -> None:
    # The startup sync used to store tls.enabled = false, and seeding skips a stored setting,
    # so TURNSTONE_TLS_ENABLED never applied after a first start without it (#1413).
    first = _run(tmp_path, "console")
    assert first.returncode == 0, first.stdout + first.stderr
    # lifespan="on": a lifespan that can't start ends the process (see the lifespan test below).
    assert "SERVING lifespan=on" in first.stdout
    assert _stored_tls(db_path) == []

    second = _run(tmp_path, "console", TURNSTONE_TLS_ENABLED="true")
    output = second.stdout + second.stderr
    assert second.returncode == 0, output
    assert "TLS enabled" in output
    assert _stored_tls(db_path) == [("true", "env")]


@pytest.mark.parametrize(
    ("error", "logged"),
    [
        pytest.param(StorageUnavailableError("database went away"), True, id="unreachable"),
        # The query fails after the connection opened: a failover, a dropped connection.
        pytest.param(
            sa.exc.OperationalError("SELECT", {}, Exception("connection dropped")),
            True,
            id="statement",
        ),
        pytest.param(RuntimeError("a bug in the read"), False, id="bug"),
    ],
)
def test_console_startup_ends_when_its_settings_cannot_load(
    tmp_path: Path,
    sqlite_backend_factory: Callable[[str], SQLiteBackend],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    logged: bool,
) -> None:
    # The lifespan loads its own store, a second read after main()'s. Through uvicorn with the
    # lifespan="on" main() passes, any error there ends startup, exit 3, before a socket binds.
    import uvicorn
    from starlette.applications import Starlette

    from turnstone.console.server import _lifespan

    def unreadable(*args: object, **kwargs: object) -> dict[str, str]:
        raise error

    storage = sqlite_backend_factory(str(tmp_path / "console.db"))
    monkeypatch.setattr(storage, "get_system_settings_bulk", unreadable)
    app = Starlette(lifespan=_lifespan)
    app.state.auth_storage = storage
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="on", log_config=None)
    )

    with (
        caplog.at_level("ERROR", logger="turnstone.console.server"),
        pytest.raises(SystemExit) as exc_info,
    ):
        asyncio.run(server.serve())

    assert exc_info.value.code == 3
    assert not server.started
    assert ("settings.load_failed" in caplog.text) is logged


@pytest.fixture
def _no_seed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in SETTINGS:
        monkeypatch.delenv("TURNSTONE_" + key.replace(".", "_").upper(), raising=False)


@pytest.mark.usefixtures("_no_seed_env")
def test_env_seeding_exits_on_values_their_settings_refuse_and_seeds_nothing(
    tmp_path: Path,
    sqlite_backend_factory: Callable[[str], SQLiteBackend],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Skipping a value would leave its setting at the default: TLS off while the operator meant it
    # on. Every refusal is logged, so one failed start names them all.
    from turnstone.console.server import _seed_config_from_env

    storage = sqlite_backend_factory(str(tmp_path / "console.db"))
    monkeypatch.setenv("TURNSTONE_TLS_ENABLED", "maybe")
    monkeypatch.setenv("TURNSTONE_RATELIMIT_ENABLED", "sometimes")
    monkeypatch.setenv("TURNSTONE_SESSION_RETENTION_DAYS", "30")

    with (
        caplog.at_level("ERROR", logger="turnstone.console.server"),
        pytest.raises(SystemExit) as exc_info,
    ):
        _seed_config_from_env(ConfigStore(storage), storage)

    assert exc_info.value.code == 1
    assert "config.seed_refused: tls.enabled from TURNSTONE_TLS_ENABLED" in caplog.text
    assert "config.seed_refused: ratelimit.enabled from TURNSTONE_RATELIMIT_ENABLED" in caplog.text
    for key in ("tls.enabled", "ratelimit.enabled", "session.retention_days"):
        assert storage.get_system_setting(key) is None


@pytest.mark.usefixtures("_no_seed_env")
def test_env_seeding_treats_an_empty_value_as_unset(
    tmp_path: Path,
    sqlite_backend_factory: Callable[[str], SQLiteBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A Compose ${X:-} passthrough of an unset variable gives an empty value: a bool setting
    # refuses it, and a string setting would store it over its default for good.
    from turnstone.console.server import _seed_config_from_env

    storage = sqlite_backend_factory(str(tmp_path / "console.db"))
    monkeypatch.setenv("TURNSTONE_RATELIMIT_ENABLED", "")
    monkeypatch.setenv("TURNSTONE_TOOLS_SEARXNG_URL", "  ")
    monkeypatch.setenv("TURNSTONE_TLS_ENABLED", "true")

    _seed_config_from_env(ConfigStore(storage), storage)

    assert storage.get_system_setting("ratelimit.enabled") is None
    assert storage.get_system_setting("tools.searxng_url") is None
    row = storage.get_system_setting("tls.enabled")
    assert row is not None
    assert (row["value"], row["changed_by"]) == ("true", "env")


@pytest.mark.usefixtures("_no_seed_env")
def test_env_seeding_names_the_setting_it_could_not_write(
    tmp_path: Path,
    sqlite_backend_factory: Callable[[str], SQLiteBackend],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from turnstone.console.server import _seed_config_from_env

    storage = sqlite_backend_factory(str(tmp_path / "console.db"))
    store = ConfigStore(storage)
    monkeypatch.setenv("TURNSTONE_TLS_ENABLED", "true")
    monkeypatch.setattr(storage, "upsert_system_setting", _unreadable)

    with (
        caplog.at_level("ERROR", logger="turnstone.console.server"),
        pytest.raises(SystemExit) as exc_info,
    ):
        _seed_config_from_env(store, storage)

    assert exc_info.value.code == 1
    assert (
        "config.seed_failed: tls.enabled from TURNSTONE_TLS_ENABLED: database went away"
        in caplog.text
    )


@pytest.mark.usefixtures("_no_seed_env")
@pytest.mark.parametrize(
    ("changed_by", "env_val", "reported"),
    [
        ("console-startup", "true", True),
        ("console-startup", "false", False),
        ("admin", "true", False),
    ],
)
def test_env_seeding_reports_a_variable_a_console_startup_row_shadows(
    tmp_path: Path,
    sqlite_backend_factory: Callable[[str], SQLiteBackend],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    changed_by: str,
    env_val: str,
    reported: bool,
) -> None:
    # Console starts before #1413 wrote these rows; a variable that agrees with the row, or an
    # admin's value, is the precedence working as designed.
    from turnstone.console.server import _seed_config_from_env

    storage = sqlite_backend_factory(str(tmp_path / "console.db"))
    storage.upsert_system_setting(key="tls.enabled", value="false", changed_by=changed_by)
    monkeypatch.setenv("TURNSTONE_TLS_ENABLED", env_val)

    with caplog.at_level("WARNING", logger="turnstone.console.server"):
        _seed_config_from_env(ConfigStore(storage), storage)

    row = storage.get_system_setting("tls.enabled")
    assert row is not None
    assert (row["value"], row["changed_by"]) == ("false", changed_by)
    assert ("config.env_ignored: tls.enabled" in caplog.text) is reported
