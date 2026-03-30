"""Tests for MCP tool handlers with mocked TurnstoneServer."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

from turnstone.sdk import TurnResult

from mcp_cluster_ops.server import (
    _dispatch_parallel,
    _exec_on_node_sync,
    _list_nodes_impl,
)

# ---------------------------------------------------------------------------
# _list_nodes_impl
# ---------------------------------------------------------------------------


class TestListNodesImpl:
    def test_returns_nodes(self):
        nodes = [{"node_id": "a", "model": "gpt-5"}, {"node_id": "b", "model": "gpt-5"}]
        with patch("mcp_cluster_ops.server.TurnstoneServer") as mock_cls:
            mock_client = MagicMock()
            mock_client.list_nodes.return_value = nodes
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)

            result = asyncio.run(_list_nodes_impl({"host": "localhost"}))
            assert result == nodes

    def test_empty_cluster(self):
        with patch("mcp_cluster_ops.server.TurnstoneServer") as mock_cls:
            mock_client = MagicMock()
            mock_client.list_nodes.return_value = []
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)

            result = asyncio.run(_list_nodes_impl({"host": "localhost"}))
            assert result == []


# ---------------------------------------------------------------------------
# _exec_on_node_sync
# ---------------------------------------------------------------------------


class TestExecOnNodeSync:
    def test_success(self):
        turn_result = TurnResult(
            tool_results=[("bash", "hello world")],
        )
        with patch("mcp_cluster_ops.server.TurnstoneServer") as mock_cls:
            mock_client = MagicMock()
            mock_client.send_and_wait.return_value = turn_result
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)

            node_id, result = _exec_on_node_sync(
                {"host": "localhost"}, "node-1", "echo hello", 60.0
            )
            assert node_id == "node-1"
            assert result.ok
            mock_client.send_and_wait.assert_called_once()
            call_kwargs = mock_client.send_and_wait.call_args
            assert call_kwargs.kwargs["target_node"] == "node-1"
            assert call_kwargs.kwargs["auto_approve"] is True

    def test_timeout(self):
        turn_result = TurnResult(timed_out=True)
        with patch("mcp_cluster_ops.server.TurnstoneServer") as mock_cls:
            mock_client = MagicMock()
            mock_client.send_and_wait.return_value = turn_result
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)

            _, result = _exec_on_node_sync({"host": "localhost"}, "node-1", "sleep 9999", 1.0)
            assert result.timed_out
            assert not result.ok


# ---------------------------------------------------------------------------
# _dispatch_parallel
# ---------------------------------------------------------------------------


class TestDispatchParallel:
    def test_parallel_success(self):
        def fake_exec(server_kw: Any, node_id: str, command: str, timeout: float) -> Any:
            return (node_id, TurnResult(tool_results=[("bash", f"output-{node_id}")]))

        with patch("mcp_cluster_ops.server._exec_on_node_sync", side_effect=fake_exec):
            results = asyncio.run(
                _dispatch_parallel(
                    {"host": "localhost"},
                    ["a", "b", "c"],
                    "echo hi",
                    60.0,
                    8192,
                )
            )
            assert len(results) == 3
            assert all(r["ok"] for r in results)
            outputs = {r["node"]: r["output"] for r in results}
            assert outputs["a"] == "output-a"
            assert outputs["b"] == "output-b"

    def test_partial_failure(self):
        def fake_exec(server_kw: Any, node_id: str, command: str, timeout: float) -> Any:
            if node_id == "bad":
                raise ConnectionError("connection refused")
            return (node_id, TurnResult(tool_results=[("bash", "ok")]))

        with patch("mcp_cluster_ops.server._exec_on_node_sync", side_effect=fake_exec):
            results = asyncio.run(
                _dispatch_parallel(
                    {"host": "localhost"},
                    ["good", "bad"],
                    "echo hi",
                    60.0,
                    8192,
                )
            )
            assert len(results) == 2
            good = next(r for r in results if r["node"] == "good")
            bad = next(r for r in results if r["node"] == "bad")
            assert good["ok"] is True
            assert bad["ok"] is False
            assert "connection refused" in bad["error"]

    def test_all_fail(self):
        def fake_exec(server_kw: Any, node_id: str, command: str, timeout: float) -> Any:
            raise RuntimeError(f"fail-{node_id}")

        with patch("mcp_cluster_ops.server._exec_on_node_sync", side_effect=fake_exec):
            results = asyncio.run(
                _dispatch_parallel(
                    {"host": "localhost"},
                    ["a", "b"],
                    "echo hi",
                    60.0,
                    8192,
                )
            )
            assert all(not r["ok"] for r in results)
            assert "fail-a" in results[0]["error"]
            assert "fail-b" in results[1]["error"]
