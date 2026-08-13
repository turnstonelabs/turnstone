"""Lifecycle slash-command isolation across local and remote clients."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests._session_helpers import make_registered_session, make_session
from turnstone.prompts import ClientType

_REMOTE_CLIENT_TYPES = (ClientType.WEB, ClientType.CHAT, ClientType.SCHEDULED)
_CLI_ONLY_COMMANDS = ("/new", "/workstreams", "/resume secret-alias", "/delete secret-alias")
_CLI_ONLY_ERROR = "This workstream command is only available in the local CLI."


@pytest.mark.parametrize("client_type", _REMOTE_CLIENT_TYPES)
@pytest.mark.parametrize("command", _CLI_ONLY_COMMANDS)
def test_remote_lifecycle_command_is_inert_before_global_storage_access(
    tmp_db: str,
    client_type: ClientType,
    command: str,
) -> None:
    """Every non-CLI host refuses the legacy storage-global implementations.

    This guard belongs below HTTP because chat and scheduled hosts can invoke
    ``handle_command`` without crossing the web command endpoint.
    """
    ui = MagicMock()
    session = make_session(ui=ui, client_type=client_type, user_id="alice")

    with patch(
        "turnstone.core.storage._registry.get_storage",
        side_effect=AssertionError("remote command consulted global storage"),
    ) as fallback:
        assert session.handle_command(command) is False

    fallback.assert_not_called()
    ui.on_error.assert_called_once_with(_CLI_ONLY_ERROR)


def test_cli_workstreams_command_keeps_local_repl_behavior(tmp_db: str) -> None:
    ui = MagicMock()
    session = make_registered_session(ui=ui, client_type=ClientType.CLI)

    with patch("turnstone.core.session.list_workstreams_with_history", return_value=[]) as rows:
        assert session.handle_command("/workstreams") is False

    rows.assert_called_once_with(20)
    ui.on_info.assert_called_once_with("No saved workstreams.")


def test_cli_resume_command_keeps_local_repl_behavior(tmp_db: str) -> None:
    ui = MagicMock()
    session = make_registered_session(ui=ui, client_type=ClientType.CLI)
    session.resume = MagicMock(return_value=False)

    with patch("turnstone.core.session.resolve_workstream", return_value="target-ws") as resolve:
        assert session.handle_command("/resume target") is False

    resolve.assert_called_once_with("target")
    session.resume.assert_called_once_with("target-ws")
    ui.on_info.assert_called_once_with("Workstream target has no messages.")


def test_cli_delete_command_keeps_local_repl_behavior(tmp_db: str) -> None:
    ui = MagicMock()
    session = make_registered_session(ui=ui, client_type=ClientType.CLI, ws_id="current-ws")

    with (
        patch("turnstone.core.session.resolve_workstream", return_value="target-ws") as resolve,
        patch("turnstone.core.session.delete_workstream", return_value=True) as delete,
    ):
        assert session.handle_command("/delete target") is False

    resolve.assert_called_once_with("target")
    delete.assert_called_once_with("target-ws")
    ui.on_info.assert_called_once_with("Deleted workstream target")


def test_cli_new_retries_a_consumed_generated_id(
    tmp_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from turnstone.core.storage import get_storage

    storage = get_storage()
    consumed = "a" * 32
    replacement = "b" * 32
    assert storage.register_workstream(consumed) is True
    assert storage.delete_workstream(consumed) is True
    generated = iter((MagicMock(hex=consumed), MagicMock(hex=replacement)))
    monkeypatch.setattr("turnstone.core.session.uuid.uuid4", lambda: next(generated))
    session = make_registered_session(client_type=ClientType.CLI, ws_id="current-ws")

    assert session.handle_command("/new") is False

    assert session.ws_id == replacement
    assert storage.get_workstream(consumed) is None
    assert storage.get_workstream(replacement) is not None


@pytest.mark.parametrize("memory_enabled", [False, True])
def test_cli_new_recomposes_cached_prefix_after_identity_swap(
    tmp_db: str,
    memory_enabled: bool,
) -> None:
    session = make_registered_session(
        client_type=ClientType.CLI,
        ws_id="current-ws",
    )
    session._persona_memory = memory_enabled
    session._init_system_messages()
    before = session.system_messages[0]["content"]
    old_ws_id = session.ws_id
    old_epoch = session._system_prefix_epoch

    assert session.handle_command("/new") is False

    after = session.system_messages[0]["content"]
    assert session._system_prefix_epoch > old_epoch
    assert session.ws_id != old_ws_id
    assert session.ws_id in after
    assert old_ws_id not in after
    assert after != before


def test_nonfork_resume_rebinds_project_memory_context_before_recomposition(tmp_db: str) -> None:
    """A supported identity adoption must not retain the prior project's memory rung."""
    from turnstone.core.storage import get_storage

    storage = get_storage()
    assert storage is not None
    storage.create_project("source-project", "Source Project", "alice")
    storage.create_project("target-project", "Target Project", "alice")
    storage.register_workstream(
        "current-ws",
        user_id="alice",
        project_id="source-project",
    )
    storage.register_workstream(
        "target-ws",
        user_id="alice",
        project_id="target-project",
    )
    storage.save_message("target-ws", "user", "target history")
    storage.acquire_memory_index_snapshot("current-ws", "alice")

    session = make_registered_session(
        client_type=ClientType.CLI,
        user_id="alice",
        ws_id="current-ws",
        project_id="source-project",
    )
    assert session.resume("target-ws") is True

    assert session.ws_id == "target-ws"
    access = session._memory_access()
    assert access.project_id == "target-project"
    assert access.project_name == "Target Project"
    assert access.project_writable is True
    assert ("project", "target-project") in session._visible_scopes()
    assert ("project", "source-project") not in session._visible_scopes()
    prompt = "\n".join(str(message.get("content", "")) for message in session.system_messages)
    assert "Source Project" not in prompt

    generation = session._claim_generation(principal_id="alice")
    session._admit_memory_index_request(
        session._primary_lane(),
        my_generation=generation,
        principal_id="alice",
    )
    wire = list(session.system_messages)
    assert "Target Project" in str(wire)
    assert "Source Project" not in str(wire)
