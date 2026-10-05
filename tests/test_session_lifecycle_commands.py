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


def test_cli_delete_command_keeps_local_repl_behavior(tmp_db: str) -> None:
    ui = MagicMock()
    session = make_registered_session(ui=ui, client_type=ClientType.CLI, ws_id="current-ws")

    storage = MagicMock()
    storage.delete_workstream.return_value = True
    with (
        patch("turnstone.core.session.resolve_workstream", return_value="target-ws") as resolve,
        patch("turnstone.core.session.get_storage", return_value=storage),
    ):
        assert session.handle_command("/delete target") is False

    resolve.assert_called_once_with("target")
    storage.delete_workstream.assert_called_once_with("target-ws")
    ui.on_info.assert_called_once_with("Deleted workstream target")


def test_rehydrate_binds_the_project_memory_of_its_workstream(tmp_db: str) -> None:
    """A session built without the project (the CLI's factory drops it) takes the row's."""
    from turnstone.core.storage import get_storage

    storage = get_storage()
    assert storage is not None
    storage.create_project("target-project", "Target Project", "alice")
    storage.register_workstream("target-ws", user_id="alice", project_id="target-project")
    storage.save_message("target-ws", "user", "target history")

    session = make_session(client_type=ClientType.CLI, user_id="alice", ws_id="target-ws")
    assert session.rehydrate() is True

    access = session._memory_access()
    assert access.project_id == "target-project"
    assert access.project_name == "Target Project"
    assert ("project", "target-project") in session._visible_scopes()
