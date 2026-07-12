"""Tests for ``turnstone-admin create-admin`` (issue #824).

``create-user`` creates a role-less user; the web UI derives a login's scopes
purely from assigned roles, so that account logs in read-only and hits
"Forbidden: token lacks 'approve' scope" on any admin action.  ``create-admin``
assigns the built-in admin role — mirroring the web setup wizard
(``POST /api/auth/setup``) — and promotes an existing role-less user, which is
the recovery path for anyone already stuck.

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
from turnstone.core.storage import init_storage, reset_storage

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


def _migrated_storage(db_path: str) -> Any:
    """Return a fully-migrated storage singleton (seeds the ``builtin-admin`` role)."""
    return init_storage("sqlite", path=db_path, run_migrations=True)


def _has_admin_role(storage: Any, user_id: str) -> bool:
    return any(r.get("role_id") == "builtin-admin" for r in storage.list_user_roles(user_id))


def _login_scopes(storage: Any, user_id: str) -> frozenset[str]:
    """Scopes a password login would grant this user — the real lockout surface."""
    return _permissions_to_scopes(_load_user_permissions(storage, user_id))


def test_create_admin_fresh_user_gets_approve_scope(tmp_path: Path) -> None:
    db_path = str(tmp_path / "admin.db")
    storage = _migrated_storage(db_path)

    _cmd_create_admin(_db_args(db_path, username="admin", name="Admin", password="hunter2!pw"))

    user = storage.get_user_by_username("admin")
    assert user is not None
    assert _has_admin_role(storage, user["user_id"])
    # The exact bug surface: a web login for this account must carry `approve`.
    assert "approve" in _login_scopes(storage, user["user_id"])


def test_create_admin_defaults_display_name_to_username(tmp_path: Path) -> None:
    db_path = str(tmp_path / "admin.db")
    storage = _migrated_storage(db_path)

    _cmd_create_admin(_db_args(db_path, username="root", name="", password="hunter2!pw"))

    user = storage.get_user_by_username("root")
    assert user is not None
    assert user["display_name"] == "root"


def test_create_admin_promotes_existing_read_only_user(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #824 recovery path: a role-less create-user account, then create-admin."""
    db_path = str(tmp_path / "admin.db")
    storage = _migrated_storage(db_path)

    # Reproduce the locked-out account exactly (role-less create-user).
    _cmd_create_user(_db_args(db_path, username="admin", name="Admin", password="hunter2!pw"))
    user = storage.get_user_by_username("admin")
    assert user is not None
    assert not _has_admin_role(storage, user["user_id"])
    assert "approve" not in _login_scopes(storage, user["user_id"])  # locked out

    # Unstick without recreating the user.
    _cmd_create_admin(_db_args(db_path, username="admin"))

    assert _has_admin_role(storage, user["user_id"])
    assert "approve" in _login_scopes(storage, user["user_id"])
    assert "Granted the admin role" in capsys.readouterr().out


def test_create_admin_already_admin_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = str(tmp_path / "admin.db")
    storage = _migrated_storage(db_path)

    _cmd_create_admin(_db_args(db_path, username="admin", name="Admin", password="hunter2!pw"))
    capsys.readouterr()  # drop first-run output

    _cmd_create_admin(_db_args(db_path, username="admin"))

    user = storage.get_user_by_username("admin")
    assert user is not None
    admin_rows = [
        r for r in storage.list_user_roles(user["user_id"]) if r.get("role_id") == "builtin-admin"
    ]
    assert len(admin_rows) == 1  # not duplicated
    assert "already an admin" in capsys.readouterr().out


def test_create_admin_short_password_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = str(tmp_path / "admin.db")
    storage = _migrated_storage(db_path)

    with pytest.raises(SystemExit) as exc_info:
        _cmd_create_admin(_db_args(db_path, username="admin", name="Admin", password="short"))

    assert exc_info.value.code == 1
    assert "at least 8" in capsys.readouterr().err
    assert storage.get_user_by_username("admin") is None  # nothing created


def test_create_admin_invalid_username_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = str(tmp_path / "admin.db")
    _migrated_storage(db_path)

    with pytest.raises(SystemExit) as exc_info:
        _cmd_create_admin(_db_args(db_path, username="bad user!", name="X", password="hunter2!pw"))

    assert exc_info.value.code == 1
    assert "invalid username" in capsys.readouterr().err
