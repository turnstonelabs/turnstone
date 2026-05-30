"""Tests for ``turnstone-admin export`` (issue #613, chunk 2).

Drives the real ``_cmd_export`` handler through a real seeded SQLite DB
(NOT a stubbed ``export_workstream``).  The handler builds its storage
via ``_get_storage(args)``, so each test constructs an ``argparse.Namespace``
whose DB attributes resolve to a tmp sqlite file, seeds that same file,
then invokes the command.

Seeding uses ``run_migrations=False`` (create_all builds the schema);
``_cmd_export`` re-inits the same path with ``run_migrations=True`` (the
admin default).  On SQLite the resulting "table already exists" Alembic
error is swallowed as non-fatal, so the seeded rows survive — this mirrors
the real CLI invocation path exactly.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from typing import TYPE_CHECKING

import pytest

from turnstone.admin import _cmd_export
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


def _export_args(db_path: str, ws_id: str, *, children: bool, output: str) -> argparse.Namespace:
    """Build the Namespace ``_cmd_export`` (via ``_get_storage``) expects.

    ``_get_storage`` reads each DB field with ``getattr(args, name, None)``
    and only falls back to the env var when the attribute ``is None``.
    Pinning the string fields to ``""`` therefore short-circuits any
    ``TURNSTONE_DB_*`` env leakage; ``db_backend``/``db_path`` point the
    backend at the tmp sqlite file.
    """
    return argparse.Namespace(
        ws_id=ws_id,
        children=children,
        output=output,
        db_backend="sqlite",
        db_path=db_path,
        db_url="",
        db_pool_size=2,
        db_sslmode="",
        db_sslrootcert="",
        db_sslcert="",
        db_sslkey="",
    )


def _seed_interactive(db_path: str, ws_id: str) -> list[str]:
    """Seed one interactive workstream; return the seeded message roles in order."""
    st = init_storage("sqlite", path=db_path, run_migrations=False)
    st.register_workstream(ws_id, user_id="u1", title="Solo", kind="interactive")
    roles = ["user", "assistant", "user", "assistant"]
    st.save_message(ws_id, "user", "first question")
    st.save_message(ws_id, "assistant", "first answer")
    st.save_message(ws_id, "user", "second question")
    st.save_message(ws_id, "assistant", "second answer")
    return roles


def _seed_coordinator(db_path: str, parent: str, children: list[str]) -> None:
    """Seed a coordinator parent plus the given child workstreams."""
    st = init_storage("sqlite", path=db_path, run_migrations=False)
    st.register_workstream(parent, user_id="u1", title="Coord", kind="coordinator")
    st.save_message(parent, "user", "coordinate")
    st.save_message(parent, "assistant", "spawning children")
    for child in children:
        st.register_workstream(
            child, user_id="u1", title=f"Child {child}", kind="interactive", parent_ws_id=parent
        )
        st.save_message(child, "user", "do work")
        st.save_message(child, "assistant", "work done")


def test_export_interactive_to_file(tmp_path: Path) -> None:
    db_path = str(tmp_path / "admin.db")
    seeded_roles = _seed_interactive(db_path, "ws_solo")
    out_file = tmp_path / "out.json"

    _cmd_export(_export_args(db_path, "ws_solo", children=False, output=str(out_file)))

    payload = json.loads(out_file.read_bytes())
    top_keys = sorted(payload.keys())
    actual_roles = [m["role"] for m in payload["messages"]]
    assert top_keys == ["messages"]
    assert actual_roles == seeded_roles


def test_export_to_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = str(tmp_path / "admin.db")
    seeded_roles = _seed_interactive(db_path, "ws_solo")

    _cmd_export(_export_args(db_path, "ws_solo", children=False, output="-"))

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    has_messages = "messages" in payload
    actual_roles = [m["role"] for m in payload["messages"]]
    assert has_messages
    assert actual_roles == seeded_roles


def test_export_children_zip_to_file(tmp_path: Path) -> None:
    db_path = str(tmp_path / "admin.db")
    _seed_coordinator(db_path, "ws_parent", ["ws_kid_a", "ws_kid_b"])
    out_file = tmp_path / "bundle.zip"

    _cmd_export(_export_args(db_path, "ws_parent", children=True, output=str(out_file)))

    with zipfile.ZipFile(out_file) as zf:
        names = sorted(zf.namelist())
    expected_names = [
        "children/ws_kid_a.json",
        "children/ws_kid_b.json",
        "ws_parent.json",
    ]
    assert names == expected_names


def test_export_unknown_ws_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = str(tmp_path / "admin.db")
    # Seed an unrelated workstream so the DB/schema exist but the queried id does not.
    _seed_interactive(db_path, "ws_present")

    with pytest.raises(SystemExit) as exc_info:
        _cmd_export(_export_args(db_path, "ws_absent", children=False, output="-"))

    exit_code = exc_info.value.code
    captured = capsys.readouterr()
    stderr_has_not_found = "not found" in captured.err
    assert exit_code == 1
    assert stderr_has_not_found


def test_export_children_zip_to_stdout(
    tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    db_path = str(tmp_path / "admin.db")
    _seed_coordinator(db_path, "ws_parent", ["ws_kid_a"])

    # Under pytest ``sys.stdout.isatty()`` is False, so the zip is written
    # to ``sys.stdout.buffer`` as raw bytes (the pipe-friendly path).
    _cmd_export(_export_args(db_path, "ws_parent", children=True, output="-"))

    captured = capsysbinary.readouterr()
    starts_with_zip_magic = captured.out.startswith(b"PK")
    assert starts_with_zip_magic


def test_export_children_zip_to_tty_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = str(tmp_path / "admin.db")
    _seed_coordinator(db_path, "ws_parent", ["ws_kid_a"])
    # Force the "stdout is a terminal" branch: refuse to dump zip bytes.
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)

    with pytest.raises(SystemExit) as exc_info:
        _cmd_export(_export_args(db_path, "ws_parent", children=True, output="-"))

    exit_code = exc_info.value.code
    captured = capsys.readouterr()
    stderr_has_refuse = "Refusing" in captured.err
    assert exit_code == 1
    assert stderr_has_refuse
