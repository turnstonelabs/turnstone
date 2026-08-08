"""Lifecycle slash-command isolation across local and remote clients."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests._session_helpers import make_session
from turnstone.prompts import ClientType

_REMOTE_CLIENT_TYPES = (ClientType.WEB, ClientType.CHAT, ClientType.SCHEDULED)
_CLI_ONLY_COMMANDS = ("/workstreams", "/resume secret-alias", "/delete secret-alias")
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

    with (
        patch(
            "turnstone.core.session.list_workstreams_with_history",
            side_effect=AssertionError("remote command enumerated global workstreams"),
        ) as list_rows,
        patch(
            "turnstone.core.session.resolve_workstream",
            side_effect=AssertionError("remote command resolved a global alias"),
        ) as resolve,
        patch(
            "turnstone.core.session.delete_workstream",
            side_effect=AssertionError("remote command deleted a global workstream"),
        ) as delete,
    ):
        assert session.handle_command(command) is False

    list_rows.assert_not_called()
    resolve.assert_not_called()
    delete.assert_not_called()
    ui.on_error.assert_called_once_with(_CLI_ONLY_ERROR)


def test_cli_workstreams_command_keeps_local_repl_behavior(tmp_db: str) -> None:
    ui = MagicMock()
    session = make_session(ui=ui, client_type=ClientType.CLI)

    with patch("turnstone.core.session.list_workstreams_with_history", return_value=[]) as rows:
        assert session.handle_command("/workstreams") is False

    rows.assert_called_once_with(limit=20)
    ui.on_info.assert_called_once_with("No saved workstreams.")


def test_cli_resume_command_keeps_local_repl_behavior(tmp_db: str) -> None:
    ui = MagicMock()
    session = make_session(ui=ui, client_type=ClientType.CLI)
    session.resume = MagicMock(return_value=False)

    with patch("turnstone.core.session.resolve_workstream", return_value="target-ws") as resolve:
        assert session.handle_command("/resume target") is False

    resolve.assert_called_once_with("target")
    session.resume.assert_called_once_with("target-ws")
    ui.on_info.assert_called_once_with("Workstream target has no messages.")


def test_cli_delete_command_keeps_local_repl_behavior(tmp_db: str) -> None:
    ui = MagicMock()
    session = make_session(ui=ui, client_type=ClientType.CLI, ws_id="current-ws")

    with (
        patch("turnstone.core.session.resolve_workstream", return_value="target-ws") as resolve,
        patch("turnstone.core.session.delete_workstream", return_value=True) as delete,
    ):
        assert session.handle_command("/delete target") is False

    resolve.assert_called_once_with("target")
    delete.assert_called_once_with("target-ws")
    ui.on_info.assert_called_once_with("Deleted workstream target")


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

    session = make_session(
        client_type=ClientType.CLI,
        user_id="alice",
        ws_id="current-ws",
        project_id="source-project",
    )
    stale_cache_key = ("source-only memory query", "", 17)
    session._mem_search_cache[stale_cache_key] = [{"scope_id": "source-project"}]
    stale_touch_key = ("project", "source-project", "old-memory")
    session._touched_memory_keys.add(stale_touch_key)

    assert session.resume("target-ws") is True

    assert session.ws_id == "target-ws"
    assert session._project_id == "target-project"
    assert session._project_name == "Target Project"
    assert session._project_writable is True
    assert ("project", "target-project") in session._visible_scopes()
    assert ("project", "source-project") not in session._visible_scopes()
    assert stale_cache_key not in session._mem_search_cache
    assert stale_touch_key not in session._touched_memory_keys
    prompt = "\n".join(str(message.get("content", "")) for message in session.system_messages)
    assert "Target Project" in prompt
    assert "Source Project" not in prompt
