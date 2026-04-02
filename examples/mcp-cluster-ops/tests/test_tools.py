"""Tests for MCP tool handlers with mocked SDK clients."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from turnstone.sdk import TurnResult

from mcp_cluster_ops.server import (
    _dispatch_parallel,
    _exec_on_node_sync,
    _list_nodes_impl,
)

_CONSOLE_KW: dict[str, Any] = {"base_url": "http://localhost:8090", "token": ""}
_CONSOLE_KW_AUTH: dict[str, Any] = {"base_url": "http://localhost:8090", "token": "tok_test"}


def _mock_console_ctx(mock_cls: MagicMock, mock_client: MagicMock) -> None:
    """Wire up a TurnstoneConsole mock as a context manager."""
    mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
    mock_cls.return_value.__exit__ = MagicMock(return_value=False)


def _mock_server_ctx(mock_cls: MagicMock, mock_server: MagicMock) -> None:
    """Wire up a TurnstoneServer mock as a context manager."""
    mock_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
    mock_cls.return_value.__exit__ = MagicMock(return_value=False)


# ---------------------------------------------------------------------------
# _list_nodes_impl
# ---------------------------------------------------------------------------


class TestListNodesImpl:
    def test_returns_nodes(self):
        mock_node_a = MagicMock()
        mock_node_a.model_dump.return_value = {"node_id": "a", "server_url": "http://a:8080"}
        mock_node_b = MagicMock()
        mock_node_b.model_dump.return_value = {"node_id": "b", "server_url": "http://b:8080"}

        mock_resp = MagicMock()
        mock_resp.nodes = [mock_node_a, mock_node_b]
        mock_resp.total = 2

        with patch("mcp_cluster_ops.server.TurnstoneConsole") as mock_cls:
            mock_client = MagicMock()
            mock_client.nodes.return_value = mock_resp
            _mock_console_ctx(mock_cls, mock_client)

            result = asyncio.run(_list_nodes_impl(_CONSOLE_KW))
            assert len(result) == 2
            assert result[0]["node_id"] == "a"
            assert result[1]["node_id"] == "b"

    def test_empty_cluster(self):
        mock_resp = MagicMock()
        mock_resp.nodes = []
        mock_resp.total = 0

        with patch("mcp_cluster_ops.server.TurnstoneConsole") as mock_cls:
            mock_client = MagicMock()
            mock_client.nodes.return_value = mock_resp
            _mock_console_ctx(mock_cls, mock_client)

            result = asyncio.run(_list_nodes_impl(_CONSOLE_KW))
            assert result == []

    def test_paginates_large_clusters(self):
        """Clusters with >100 nodes are fetched across multiple pages."""

        def _make_node(nid: str) -> MagicMock:
            m = MagicMock()
            m.model_dump.return_value = {"node_id": nid}
            return m

        page1_nodes = [_make_node(f"n-{i}") for i in range(100)]
        page2_nodes = [_make_node(f"n-{i}") for i in range(100, 150)]

        page1_resp = MagicMock()
        page1_resp.nodes = page1_nodes
        page1_resp.total = 150

        page2_resp = MagicMock()
        page2_resp.nodes = page2_nodes
        page2_resp.total = 150

        with patch("mcp_cluster_ops.server.TurnstoneConsole") as mock_cls:
            mock_client = MagicMock()
            mock_client.nodes.side_effect = [page1_resp, page2_resp]
            _mock_console_ctx(mock_cls, mock_client)

            result = asyncio.run(_list_nodes_impl(_CONSOLE_KW))
            assert len(result) == 150
            assert result[0]["node_id"] == "n-0"
            assert result[149]["node_id"] == "n-149"
            assert mock_client.nodes.call_count == 2
            # Verify offset was passed correctly
            mock_client.nodes.assert_any_call(limit=100, offset=0)
            mock_client.nodes.assert_any_call(limit=100, offset=100)


# ---------------------------------------------------------------------------
# _exec_on_node_sync
# ---------------------------------------------------------------------------


class TestExecOnNodeSync:
    def test_success(self):
        turn_result = TurnResult(
            ws_id="ws-123",
            tool_results=[("bash", "hello world")],
        )
        with (
            patch("mcp_cluster_ops.server.TurnstoneConsole") as mock_console_cls,
            patch("mcp_cluster_ops.server.TurnstoneServer") as mock_server_cls,
        ):
            mock_console = MagicMock()
            mock_console.route_create_workstream.return_value = {
                "ws_id": "ws-123",
                "node_url": "http://node-1:8080",
                "node_id": "node-1",
                "name": "ws-123",
            }
            _mock_console_ctx(mock_console_cls, mock_console)

            mock_server = MagicMock()
            mock_server.send_and_wait.return_value = turn_result
            _mock_server_ctx(mock_server_cls, mock_server)

            node_id, result = _exec_on_node_sync(_CONSOLE_KW_AUTH, "node-1", "echo hello", 60.0)
            assert node_id == "node-1"
            assert result.ok

            # Verify console created ws on the right node
            mock_console.route_create_workstream.assert_called_once_with(
                target_node="node-1",
                auto_approve=True,
            )

            # Verify server connected to the node URL with the token
            mock_server_cls.assert_called_once_with(
                base_url="http://node-1:8080",
                token="tok_test",
            )

            # Verify send_and_wait got the right ws_id
            call_kwargs = mock_server.send_and_wait.call_args
            assert call_kwargs.args[1] == "ws-123"

            # Verify workstream was closed
            mock_console.route_close.assert_called_once_with("ws-123")

    def test_timeout(self):
        turn_result = TurnResult(ws_id="ws-456", timed_out=True)
        with (
            patch("mcp_cluster_ops.server.TurnstoneConsole") as mock_console_cls,
            patch("mcp_cluster_ops.server.TurnstoneServer") as mock_server_cls,
        ):
            mock_console = MagicMock()
            mock_console.route_create_workstream.return_value = {
                "ws_id": "ws-456",
                "node_url": "http://node-1:8080",
                "node_id": "node-1",
            }
            _mock_console_ctx(mock_console_cls, mock_console)

            mock_server = MagicMock()
            mock_server.send_and_wait.return_value = turn_result
            _mock_server_ctx(mock_server_cls, mock_server)

            _, result = _exec_on_node_sync(_CONSOLE_KW, "node-1", "sleep 9999", 1.0)
            assert result.timed_out
            assert not result.ok
            # Workstream still closed even on timeout
            mock_console.route_close.assert_called_once_with("ws-456")

    def test_send_failure_still_closes_workstream(self):
        """Workstream must be closed even if send_and_wait raises."""
        with (
            patch("mcp_cluster_ops.server.TurnstoneConsole") as mock_console_cls,
            patch("mcp_cluster_ops.server.TurnstoneServer") as mock_server_cls,
        ):
            mock_console = MagicMock()
            mock_console.route_create_workstream.return_value = {
                "ws_id": "ws-789",
                "node_url": "http://node-1:8080",
                "node_id": "node-1",
            }
            _mock_console_ctx(mock_console_cls, mock_console)

            mock_server = MagicMock()
            mock_server.send_and_wait.side_effect = ConnectionError("lost connection")
            _mock_server_ctx(mock_server_cls, mock_server)

            with contextlib.suppress(ConnectionError):
                _exec_on_node_sync(_CONSOLE_KW, "node-1", "echo hi", 60.0)

            mock_console.route_close.assert_called_once_with("ws-789")

    def test_malformed_route_response_no_leak(self):
        """If route response is missing ws_id, no route_close is attempted."""
        with patch("mcp_cluster_ops.server.TurnstoneConsole") as mock_console_cls:
            mock_console = MagicMock()
            mock_console.route_create_workstream.return_value = {
                # Missing "ws_id" and "node_url"
                "node_id": "node-1",
            }
            _mock_console_ctx(mock_console_cls, mock_console)

            with pytest.raises(KeyError):
                _exec_on_node_sync(_CONSOLE_KW, "node-1", "echo hi", 60.0)

            # route_close must NOT be called — ws_id was never assigned
            mock_console.route_close.assert_not_called()


# ---------------------------------------------------------------------------
# _dispatch_parallel
# ---------------------------------------------------------------------------


class TestDispatchParallel:
    def test_parallel_success(self):
        def fake_exec(console_kw: Any, node_id: str, command: str, timeout: float) -> Any:
            return (
                node_id,
                TurnResult(tool_results=[("bash", f"output-{node_id}")]),
            )

        with patch("mcp_cluster_ops.server._exec_on_node_sync", side_effect=fake_exec):
            results = asyncio.run(
                _dispatch_parallel(_CONSOLE_KW, ["a", "b", "c"], "echo hi", 60.0, 8192)
            )
            assert len(results) == 3
            assert all(r["ok"] for r in results)
            outputs = {r["node"]: r["output"] for r in results}
            assert outputs["a"] == "output-a"
            assert outputs["b"] == "output-b"
            assert outputs["c"] == "output-c"

    def test_partial_failure(self):
        def fake_exec(console_kw: Any, node_id: str, command: str, timeout: float) -> Any:
            if node_id == "bad":
                raise ConnectionError("connection refused")
            return (node_id, TurnResult(tool_results=[("bash", "ok")]))

        with patch("mcp_cluster_ops.server._exec_on_node_sync", side_effect=fake_exec):
            results = asyncio.run(
                _dispatch_parallel(_CONSOLE_KW, ["good", "bad"], "echo hi", 60.0, 8192)
            )
            assert len(results) == 2
            good = next(r for r in results if r["node"] == "good")
            bad = next(r for r in results if r["node"] == "bad")
            assert good["ok"] is True
            assert bad["ok"] is False
            assert "connection refused" in bad["error"]

    def test_all_fail(self):
        def fake_exec(console_kw: Any, node_id: str, command: str, timeout: float) -> Any:
            raise RuntimeError(f"fail-{node_id}")

        with patch("mcp_cluster_ops.server._exec_on_node_sync", side_effect=fake_exec):
            results = asyncio.run(
                _dispatch_parallel(_CONSOLE_KW, ["a", "b"], "echo hi", 60.0, 8192)
            )
            assert all(not r["ok"] for r in results)
            assert "fail-a" in results[0]["error"]
            assert "fail-b" in results[1]["error"]
