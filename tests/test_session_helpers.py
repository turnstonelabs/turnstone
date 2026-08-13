"""Ownership boundaries for the shared direct-session test factories."""

from unittest.mock import patch

import pytest

from tests._session_helpers import make_registered_session, make_session
from turnstone.core.personas import PersonaSnapshot
from turnstone.core.workstream import WorkstreamKind


def test_generic_session_factory_does_not_register_a_workstream(tmp_db: str) -> None:
    from turnstone.core.storage import get_storage

    session = make_session(ws_id="generic-unregistered", user_id="owner")

    assert get_storage().get_workstream(session.ws_id) is None


def test_generic_session_uses_default_file_backed_sqlite_when_uninitialized(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from turnstone.core.storage import get_storage, is_storage_initialized, reset_storage

    reset_storage()
    monkeypatch.chdir(tmp_path)
    try:
        make_session(ws_id="generic-default-sqlite", user_id="owner")

        assert is_storage_initialized() is True
        assert get_storage()._path == str(tmp_path / ".turnstone.db")
        assert (tmp_path / ".turnstone.db").is_file()
    finally:
        reset_storage()


def test_generic_session_preserves_an_initialized_backend(
    tmp_db: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from turnstone.core.storage import get_storage

    configured_backend = get_storage()
    ambient_cwd = tmp_path / "ambient"
    ambient_cwd.mkdir()
    monkeypatch.chdir(ambient_cwd)

    make_session(ws_id="generic-configured-backend", user_id="owner")

    assert get_storage() is configured_backend
    assert not (ambient_cwd / ".turnstone.db").exists()


def test_generic_session_uses_global_auth_storage(tmp_db: str) -> None:
    session = make_session(ws_id="generic-auth-ephemeral", user_id="owner")

    with patch(
        "turnstone.core.session.get_storage",
        wraps=__import__("turnstone.core.session", fromlist=["get_storage"]).get_storage,
    ) as fallback:
        denied = session._require_model_skills_write(
            "call-1",
            "create",
            {"name": "example"},
        )

    assert denied is not None
    assert "permission denied" in denied["error"]
    fallback.assert_called()


def test_generic_session_uses_global_durability_for_commands(tmp_db: str) -> None:
    session = make_session(ws_id="generic-no-durability", user_id="owner")

    assert session.handle_command("/workstreams") is False


def test_registered_session_factory_requires_initialized_storage() -> None:
    from turnstone.core.storage import is_storage_initialized, reset_storage

    reset_storage()
    assert is_storage_initialized() is False

    with pytest.raises(RuntimeError, match="initialized test storage"):
        make_registered_session(ws_id="must-not-auto-initialize", user_id="owner")

    assert is_storage_initialized() is False


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({"user_id": "owner"}, {"user_id": "other"}),
        ({"project_id": "project-a"}, {"project_id": "project-b"}),
        (
            {"kind": WorkstreamKind.INTERACTIVE},
            {"kind": WorkstreamKind.COORDINATOR, "user_id": "owner"},
        ),
        (
            {"persona_snapshot": PersonaSnapshot("first", "", None, True, True)},
            {"persona_snapshot": PersonaSnapshot("other", "", None, True, True)},
        ),
    ],
)
def test_registered_session_factory_rejects_repeated_id_metadata_mismatch(
    tmp_db: str,
    first: dict[str, object],
    second: dict[str, object],
) -> None:
    ws_id = "registered-metadata-collision"
    make_registered_session(ws_id=ws_id, **first)

    with pytest.raises(RuntimeError, match="different metadata"):
        make_registered_session(ws_id=ws_id, **second)


def test_registered_session_factory_accepts_exact_repeated_metadata(tmp_db: str) -> None:
    from turnstone.core.storage import get_storage

    first = make_registered_session(ws_id="registered-same", user_id="owner")
    second = make_registered_session(ws_id="registered-same", user_id="owner")

    assert first.ws_id == second.ws_id
    assert get_storage().get_workstream(first.ws_id) is not None
