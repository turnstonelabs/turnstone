"""Direct file browsing is an overridable admin/operator permission."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config


def _config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[1] / "turnstone/core/storage/migrations"),
    )
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _roles(engine: sa.Engine) -> dict[str, set[str]]:
    with engine.connect() as conn:
        return {
            role_id: set(permissions.split(","))
            for role_id, permissions in conn.execute(
                sa.text("SELECT role_id, permissions FROM roles")
            )
        }


def test_files_read_updates_only_builtin_baselines_and_round_trips(tmp_path):
    path = tmp_path / "files-permission.db"
    cfg = _config(path)
    command.upgrade(cfg, "085")
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO roles "
                    "(role_id, name, display_name, permissions, builtin, org_id, created, updated) "
                    "VALUES ('custom', 'custom', 'Custom', 'read,write', 0, '', '', '')"
                )
            )
        before = _roles(engine)
        command.upgrade(cfg, "086")
        after = _roles(engine)
        assert after == {
            role: perms
            | ({"files.read"} if role in ("builtin-admin", "builtin-operator") else set())
            for role, perms in before.items()
        }
        command.downgrade(cfg, "085")
        assert _roles(engine) == before
        command.upgrade(cfg, "086")
        assert _roles(engine) == after
    finally:
        engine.dispose()


def test_file_permission_upgrade_preserves_explicit_role_overrides(
    tmp_path, sqlite_backend_factory
):
    path = tmp_path / "files-overrides.db"
    cfg = _config(path)
    command.upgrade(cfg, "085")
    storage = sqlite_backend_factory(str(path), create_tables=False)
    # Existing overrides, including an explicit deny of the new permission,
    # must remain authoritative when the baseline gains it.
    storage.set_role_overrides("builtin-admin", {"model.skills.write"}, {"files.read"})
    storage.set_role_overrides("builtin-operator", {"admin.roles"}, {"write"})
    before = {
        role: storage.list_role_overrides(role) for role in ("builtin-admin", "builtin-operator")
    }
    command.upgrade(cfg, "086")
    assert {role: storage.list_role_overrides(role) for role in before} == before
    admin = storage.effective_role_permissions("builtin-admin")
    operator = storage.effective_role_permissions("builtin-operator")
    assert "files.read" in admin["baseline"]
    assert "files.read" not in admin["effective"]
    assert "model.skills.write" in admin["effective"]
    assert "files.read" in operator["effective"]
    assert "admin.roles" in operator["effective"]
    assert "write" not in operator["effective"]
