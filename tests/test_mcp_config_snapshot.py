"""MCP startup resolves configuration and ownership from one database snapshot."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, call, patch

import pytest

from turnstone.core.mcp_client import MCPClientManager, create_mcp_client, load_mcp_config

_STATIC_ROW = {
    "name": "db-static",
    "transport": "streamable-http",
    "url": "https://db.example.com/mcp",
    "headers": '{"X-Test": "database"}',
    "auth_type": "static",
}
_STATIC_CONFIG = {
    "db-static": {
        "type": "streamable-http",
        "url": "https://db.example.com/mcp",
        "headers": {"X-Test": "database"},
    }
}
_POOL_ROWS = [
    {"name": "user-pool", "auth_type": "oauth_user"},
    {"name": "obo-pool", "auth_type": "oauth_obo"},
]


@pytest.mark.parametrize(
    ("db_state", "file_source"),
    [
        ("empty", "json"),
        ("failed", "json"),
        ("empty", "toml"),
        ("failed", "toml"),
        ("pool", "json"),
        ("mixed", "json"),
        ("empty", "none"),
    ],
)
@pytest.mark.parametrize("user_token_sweep", [True, False], ids=["web", "cli"])
def test_factory_and_standalone_loader_resolve_config(
    tmp_path, db_state, file_source, user_token_sweep
):
    """Check source precedence and manager state before asserting the read count."""
    storage = MagicMock()
    rows = []
    if db_state in {"pool", "mixed"}:
        rows.extend(_POOL_ROWS)
    if db_state == "mixed":
        rows.append(_STATIC_ROW)
    storage.list_mcp_servers.return_value = rows
    if db_state == "failed":
        storage.list_mcp_servers.side_effect = RuntimeError("database unavailable")

    json_servers = {"json-server": {"command": "echo", "args": ["json"]}}
    toml_servers = {"toml-server": {"command": "echo", "args": ["toml"]}}
    config_path = None
    if file_source == "json":
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({"mcpServers": json_servers}), encoding="utf-8")
        config_path = str(path)
    toml_config = {"user_token_sweep_seconds": 123}
    if file_source != "none":
        toml_config["servers"] = toml_servers

    if db_state == "mixed":
        expected_servers = _STATIC_CONFIG
    elif db_state == "pool" or file_source == "none":
        expected_servers = {}
    else:
        expected_servers = json_servers if file_source == "json" else toml_servers
    expected_db_names = {"db-static"} if db_state == "mixed" else set()
    expected_user_names = {"user-pool"} if rows else set()
    expected_obo_names = {"obo-pool"} if rows else set()

    def check_before_start(manager):
        assert manager._server_configs == expected_servers
        assert manager._db_managed == expected_db_names
        assert manager._oauth_user_server_names == expected_user_names
        assert manager._obo_server_names == expected_obo_names
        assert manager._user_token_sweep_s == (123 if user_token_sweep else 0)

    with (
        patch("turnstone.core.mcp_client.load_config", return_value=toml_config),
        patch.object(
            MCPClientManager, "start", autospec=True, side_effect=check_before_start
        ) as start,
    ):
        manager = create_mcp_client(config_path, storage=storage, user_token_sweep=user_token_sweep)
        if expected_servers or expected_user_names or expected_obo_names:
            assert isinstance(manager, MCPClientManager)
            start.assert_called_once_with(manager)
        else:
            assert manager is None
            start.assert_not_called()
        storage.list_mcp_servers.assert_called_once_with(enabled_only=True)

        # Standalone callers still read storage and use the same source precedence.
        storage.list_mcp_servers.reset_mock()
        assert load_mcp_config(config_path, storage=storage) == expected_servers
        storage.list_mcp_servers.assert_called_once_with(enabled_only=True)


def test_factory_snapshot_is_local_to_each_invocation():
    """A later factory invocation must see updated database rows."""
    storage = MagicMock()
    storage.list_mcp_servers.side_effect = [[_STATIC_ROW], _POOL_ROWS]
    with (
        patch("turnstone.core.mcp_client.load_config", return_value={}),
        patch.object(MCPClientManager, "start", autospec=True) as start,
    ):
        first = create_mcp_client(storage=storage)
        second = create_mcp_client(storage=storage)

    assert first is not None
    assert first._server_configs == _STATIC_CONFIG
    assert first._db_managed == {"db-static"}
    assert first._oauth_user_server_names == set()
    assert first._obo_server_names == set()
    assert second is not None
    assert second is not first
    assert second._server_configs == {}
    assert second._db_managed == set()
    assert second._oauth_user_server_names == {"user-pool"}
    assert second._obo_server_names == {"obo-pool"}
    assert start.call_args_list == [call(first), call(second)]
    assert storage.list_mcp_servers.call_args_list == [call(enabled_only=True)] * 2


@pytest.mark.parametrize("read_fails", [False, True], ids=["empty", "failed"])
def test_factory_follows_toml_json_redirect_without_rereading_storage(tmp_path, read_fails):
    storage = MagicMock()
    storage.list_mcp_servers.return_value = []
    if read_fails:
        storage.list_mcp_servers.side_effect = RuntimeError("database unavailable")
    servers = {"redirected": {"command": "echo"}}
    path = tmp_path / "redirected.json"
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    with (
        patch("turnstone.core.mcp_client.load_config", return_value={"config_path": str(path)}),
        patch.object(MCPClientManager, "start", autospec=True) as start,
    ):
        manager = create_mcp_client(storage=storage)

    assert manager is not None
    assert manager._server_configs == servers
    assert manager._db_managed == set()
    assert manager._oauth_user_server_names == set()
    assert manager._obo_server_names == set()
    start.assert_called_once_with(manager)
    storage.list_mcp_servers.assert_called_once_with(enabled_only=True)
