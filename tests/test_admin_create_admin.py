"""Tests for ``turnstone-admin create-admin`` (issue #824).

``create-user`` assigns the viewer role for read access. ``create-admin`` assigns the built-in
admin role, mirroring the web setup wizard, and can promote either a viewer or a historical
account without roles. Accounts without any effective permissions cannot log in with a password.

Each test drives the real ``_cmd_create_admin`` handler against a real,
fully-migrated SQLite DB: the ``builtin-admin`` role is seeded by migration
008, so the DB must be migrated (not just ``create_all``-built) for the role
to exist.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any

import pytest

from turnstone.admin import _cmd_create_admin, _cmd_create_user
from turnstone.core.auth import _load_user_permissions, _permissions_to_scopes
from turnstone.core.storage import reset_storage
from turnstone.core.storage._migrate import run_migrations

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _reset_storage_singleton() -> Iterator[None]:
    """Keep the module-global storage singleton from leaking across tests."""
    reset_storage()
    yield
    reset_storage()


def _db_args(db_path: str, **overrides: Any) -> argparse.Namespace:
    """Build the Namespace ``_cmd_create_admin`` (and ``_cmd_create_user``) expect.

    Pins every DB field so ``_get_storage`` resolves to the tmp sqlite file and
    never leaks a ``TURNSTONE_DB_*`` env var (it only falls back when the attr
    ``is None``).  ``token``/``scopes`` are only read by ``_cmd_create_user``.
    """
    base: dict[str, Any] = {
        "username": "admin",
        "name": "",
        "password": "",
        "token": False,
        "scopes": "read,write,approve",
        "db_backend": "sqlite",
        "db_path": db_path,
        "db_url": "",
        "db_pool_size": 2,
        "db_sslmode": "",
        "db_sslrootcert": "",
        "db_sslcert": "",
        "db_sslkey": "",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def _migrated_storage(sqlite_backend_factory: Any) -> Any:
    """Own seeded stores independently of the CLI's storage singleton."""

    def create(db_path: str) -> Any:
        storage = sqlite_backend_factory(db_path, create_tables=False)
        run_migrations(storage, "sqlite")
        return storage

    return create


def _has_admin_role(storage: Any, user_id: str) -> bool:
    return any(r.get("role_id") == "builtin-admin" for r in storage.list_user_roles(user_id))


def _login_scopes(storage: Any, user_id: str) -> frozenset[str]:
    """Scopes a password login would grant this user — the real lockout surface."""
    return _permissions_to_scopes(_load_user_permissions(storage, user_id))


def test_create_admin_fresh_user_gets_approve_scope(tmp_path: Path, _migrated_storage) -> None:
    db_path = str(tmp_path / "admin.db")
    storage = _migrated_storage(db_path)

    _cmd_create_admin(_db_args(db_path, username="admin", name="Admin", password="hunter2!pw"))

    user = storage.get_user_by_username("admin")
    assert user is not None
    assert _has_admin_role(storage, user["user_id"])
    # The exact bug surface: a web login for this account must carry `approve`.
    assert "approve" in _login_scopes(storage, user["user_id"])


def test_create_admin_defaults_display_name_to_username(tmp_path: Path, _migrated_storage) -> None:
    db_path = str(tmp_path / "admin.db")
    storage = _migrated_storage(db_path)

    _cmd_create_admin(_db_args(db_path, username="root", name="", password="hunter2!pw"))

    user = storage.get_user_by_username("root")
    assert user is not None
    assert user["display_name"] == "root"


def test_create_admin_promotes_existing_read_only_user(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], _migrated_storage
) -> None:
    """Issue #824 recovery path: a read-only create-user account, then create-admin."""
    db_path = str(tmp_path / "admin.db")
    storage = _migrated_storage(db_path)

    # A viewer cannot administer the installation until explicitly promoted.
    _cmd_create_user(_db_args(db_path, username="admin", name="Admin", password="hunter2!pw"))
    user = storage.get_user_by_username("admin")
    assert user is not None
    assert not _has_admin_role(storage, user["user_id"])
    assert "approve" not in _login_scopes(storage, user["user_id"])  # locked out

    # Unstick without recreating the user.
    reset_storage()  # Each CLI invocation owns a separate storage lifetime.
    _cmd_create_admin(_db_args(db_path, username="admin"))

    assert _has_admin_role(storage, user["user_id"])
    assert "approve" in _login_scopes(storage, user["user_id"])
    assert "Granted the admin role" in capsys.readouterr().out


def test_create_admin_already_admin_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], _migrated_storage
) -> None:
    db_path = str(tmp_path / "admin.db")
    storage = _migrated_storage(db_path)

    _cmd_create_admin(_db_args(db_path, username="admin", name="Admin", password="hunter2!pw"))
    capsys.readouterr()  # drop first-run output

    reset_storage()
    _cmd_create_admin(_db_args(db_path, username="admin"))

    user = storage.get_user_by_username("admin")
    assert user is not None
    admin_rows = [
        r for r in storage.list_user_roles(user["user_id"]) if r.get("role_id") == "builtin-admin"
    ]
    assert len(admin_rows) == 1  # not duplicated
    assert "already an admin" in capsys.readouterr().out


def test_create_admin_short_password_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], _migrated_storage
) -> None:
    db_path = str(tmp_path / "admin.db")
    storage = _migrated_storage(db_path)

    with pytest.raises(SystemExit) as exc_info:
        _cmd_create_admin(_db_args(db_path, username="admin", name="Admin", password="short"))

    assert exc_info.value.code == 1
    assert "at least 8" in capsys.readouterr().err
    assert storage.get_user_by_username("admin") is None  # nothing created


def test_create_admin_invalid_username_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], _migrated_storage
) -> None:
    db_path = str(tmp_path / "admin.db")
    _migrated_storage(db_path)

    with pytest.raises(SystemExit) as exc_info:
        _cmd_create_admin(_db_args(db_path, username="bad user!", name="X", password="hunter2!pw"))

    assert exc_info.value.code == 1
    assert "invalid username" in capsys.readouterr().err


def test_create_user_assigns_explicit_viewer(tmp_path: Path, _migrated_storage) -> None:
    db_path = str(tmp_path / "viewer.db")
    storage = _migrated_storage(db_path)
    _cmd_create_user(_db_args(db_path, username="reader", password="reader-password"))
    user = storage.get_user_by_username("reader")
    assert user is not None
    assert [r["role_id"] for r in storage.list_user_roles(user["user_id"])] == ["builtin-viewer"]
    assert storage.get_user_permissions(user["user_id"]) == {"read"}


@pytest.mark.parametrize("failure", ["assignment", "empty_permissions"])
def test_create_user_rolls_back_incomplete_viewer_provisioning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
    _migrated_storage,
) -> None:
    db_path = str(tmp_path / "viewer.db")
    storage = _migrated_storage(db_path)
    if failure == "assignment":
        monkeypatch.setattr("turnstone.admin._get_storage", lambda _args: storage)

        def unavailable(*_args: Any) -> None:
            raise RuntimeError("storage unavailable")

        monkeypatch.setattr(storage, "assign_role", unavailable)
    else:
        storage.set_role_overrides("builtin-viewer", set(), {"read"})
    with pytest.raises(SystemExit) as exc:
        _cmd_create_user(
            _db_args(db_path, username="reader", password="reader-password", token=True)
        )
    assert exc.value.code == 1
    assert storage.get_user_by_username("reader") is None
    assert "Created user" not in capsys.readouterr().out


def test_create_admin_can_recover_historical_roleless_user(
    tmp_path: Path, _migrated_storage
) -> None:
    db_path = str(tmp_path / "historical.db")
    storage = _migrated_storage(db_path)
    storage.create_user("historical", "historical", "Historical", "unused-password-hash")
    assert storage.get_user_permissions("historical") == set()
    assert _login_scopes(storage, "historical") == frozenset()
    _cmd_create_admin(_db_args(db_path, username="historical"))
    assert _has_admin_role(storage, "historical")
