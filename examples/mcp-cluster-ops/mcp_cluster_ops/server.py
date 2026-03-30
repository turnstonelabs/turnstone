"""MCP server for Turnstone cluster operations.

Exposes tools to execute commands on specific nodes in a Turnstone cluster.
Uses the SDK client (``TurnstoneServer``) for direct node targeting via HTTP.

Usage::

    mcp-cluster-ops              # via entry point
    python -m mcp_cluster_ops    # via module

Configure in ``~/.config/turnstone/config.toml``::

    [mcp.servers.cluster-ops]
    command = "mcp-cluster-ops"

    [mcp.servers.cluster-ops.env]
    TURNSTONE_SERVER_URL = "http://localhost:8080"

Environment variables
---------------------
TURNSTONE_SERVER_URL        Server URL (default: http://localhost:8080)
TURNSTONE_API_TOKEN         API token for authentication (default: none)
MCP_CLUSTER_OPS_TIMEOUT     Default command timeout in seconds (default: 120)
MCP_CLUSTER_OPS_MAX_OUTPUT  Max output bytes per node (default: 8192, 0=unlimited)

Performance notes
-----------------
Remote agents are told to reply with only "ok" or "failed" — the raw bash
output is captured directly from the ToolResultEvent, bypassing the costly
"agent reads output then re-generates output as completion tokens" round-trip.

All multi-node dispatches run in parallel via ``asyncio.gather`` so total
wall time is bounded by the slowest node, not the sum of all nodes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import Context, FastMCP
from turnstone.sdk import TurnResult, TurnstoneServer

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT = int(os.environ.get("MCP_CLUSTER_OPS_TIMEOUT", "120"))
_DEFAULT_MAX_OUTPUT = int(os.environ.get("MCP_CLUSTER_OPS_MAX_OUTPUT", "8192"))
_MAX_CONCURRENT_NODES = int(os.environ.get("MCP_CLUSTER_OPS_MAX_NODES", "32"))
_MAX_COMMAND_LEN = int(os.environ.get("MCP_CLUSTER_OPS_MAX_COMMAND", "65536"))
_MIN_TIMEOUT = 5
_MAX_TIMEOUT = 3600

# ---------------------------------------------------------------------------
# Helpers (pure functions, easily testable)
# ---------------------------------------------------------------------------


def _server_kwargs() -> dict[str, Any]:
    """Build TurnstoneServer connection kwargs from environment variables."""
    kwargs: dict[str, Any] = {
        "base_url": os.environ.get("TURNSTONE_SERVER_URL", "http://localhost:8080"),
    }
    token = os.environ.get("TURNSTONE_API_TOKEN")
    if token:
        kwargs["token"] = token
    return kwargs


def _exec_prompt(command: str) -> str:
    """Build the prompt sent to the remote agent.

    Instructs it to run the command and reply minimally so that the raw
    bash output (captured via ToolResultEvent) is the primary result,
    avoiding token waste from re-transcription.
    """
    return (
        "Execute this shell command using the bash tool:\n"
        f"  {command}\n\n"
        "After the tool completes, reply with only 'ok' or 'failed'.\n"
        "Do NOT repeat, quote, or summarise the command output in your reply."
    )


def _extract_output(result: TurnResult) -> str:
    """Extract useful output from a TurnResult.

    Prefers raw bash ToolResultEvent output (zero LLM re-transcription cost)
    over agent content.  Falls back through tool results and content.
    """
    bash_outputs = [out for name, out in result.tool_results if name == "bash"]
    if bash_outputs:
        return "\n".join(bash_outputs)
    content: str = result.content
    if content:
        return content
    if result.tool_results:
        return str(result.tool_results[0][1])
    return ""


def _truncate(text: str, max_bytes: int) -> str:
    """Truncate *text* to at most *max_bytes* UTF-8 bytes.

    Appends a marker when truncation occurs.  Handles multi-byte characters
    safely by decoding with ``errors='ignore'``.

    Pass ``max_bytes=0`` to disable truncation.
    """
    if max_bytes <= 0:
        return text
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    omitted = len(encoded) - len(truncated.encode("utf-8"))
    return truncated + f"\n... [truncated: {omitted} bytes omitted]"


def _clamp_timeout(timeout: int) -> float:
    """Clamp timeout to a safe range."""
    return float(max(_MIN_TIMEOUT, min(timeout, _MAX_TIMEOUT)))


def _validate_command(command: str) -> str | None:
    """Validate a command string.  Returns an error message or None."""
    if not command.strip():
        return "command must be a non-empty string"
    if len(command) > _MAX_COMMAND_LEN:
        return f"command too long ({len(command)} chars, max {_MAX_COMMAND_LEN})"
    return None


def _format_node_result(
    node_id: str,
    result: TurnResult,
    max_output: int,
) -> dict[str, Any]:
    """Format a single node's TurnResult for JSON output."""
    raw = _extract_output(result)
    output = _truncate(raw, max_output)
    entry: dict[str, Any] = {
        "node": node_id,
        "ok": result.ok,
    }
    if result.timed_out:
        entry["timed_out"] = True
    if result.ok:
        entry["output"] = output
    else:
        entry["output"] = output or None
        if result.errors:
            entry["error"] = "; ".join(result.errors)
    return entry


# ---------------------------------------------------------------------------
# Core dispatch functions (testable with mocked TurnstoneServer)
# ---------------------------------------------------------------------------


def _exec_on_node_sync(
    server_kw: dict[str, Any],
    node_id: str,
    command: str,
    timeout: float,
) -> tuple[str, TurnResult]:
    """Dispatch *command* to *node_id* and block until complete.

    Runs inside ``asyncio.to_thread`` so it does not block the event loop.
    Each call creates its own ``TurnstoneServer`` client to avoid state
    conflicts between concurrent dispatches.
    """
    prompt = _exec_prompt(command)
    with TurnstoneServer(**server_kw) as client:
        result = client.send_and_wait(
            message=prompt,
            target_node=node_id,
            auto_approve=True,
            timeout=timeout,
        )
    return node_id, result


async def _dispatch_parallel(
    server_kw: dict[str, Any],
    node_ids: list[str],
    command: str,
    timeout: float,
    max_output: int,
) -> list[dict[str, Any]]:
    """Dispatch *command* to all *node_ids* concurrently.

    Total wall time is bounded by the slowest node.
    """
    tasks = [
        asyncio.to_thread(_exec_on_node_sync, server_kw, nid, command, timeout) for nid in node_ids
    ]
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    results: list[dict[str, Any]] = []
    for nid, outcome in zip(node_ids, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            if not isinstance(outcome, Exception):
                raise outcome  # propagate KeyboardInterrupt, SystemExit, etc.
            results.append({"node": nid, "ok": False, "error": str(outcome)})
        else:
            _, turn_result = outcome
            results.append(_format_node_result(nid, turn_result, max_output))
    return results


def _list_nodes_sync(server_kw: dict[str, Any]) -> list[dict[str, Any]]:
    """List active cluster nodes (blocking)."""
    with TurnstoneServer(**server_kw) as client:
        nodes: list[dict[str, Any]] = client.list_nodes()
        return nodes


async def _list_nodes_impl(server_kw: dict[str, Any]) -> list[dict[str, Any]]:
    """List active cluster nodes."""
    return await asyncio.to_thread(_list_nodes_sync, server_kw)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(server: FastMCP[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    """Lifespan context — stores server connection kwargs for tool handlers."""
    kw = _server_kwargs()
    yield {"server_kwargs": kw}


mcp = FastMCP(
    "turnstone-cluster-ops",
    instructions=(
        "Tools for executing commands across a Turnstone AI cluster. "
        "Use list_nodes first to discover available nodes, then run_on_node "
        "to execute commands on specific nodes or run_on_all_nodes for "
        "cluster-wide operations."
    ),
    lifespan=_lifespan,
)


@mcp.tool()
async def list_nodes(ctx: Context[Any, Any, Any]) -> str:
    """List all active nodes in the Turnstone cluster.

    Call this before dispatching work to discover available node IDs.
    Returns a JSON array of node metadata objects.
    """
    server_kw: dict[str, Any] = ctx.request_context.lifespan_context["server_kwargs"]
    nodes = await _list_nodes_impl(server_kw)
    return json.dumps(nodes, indent=2)


@mcp.tool()
async def run_on_node(
    node_id: str,
    command: str,
    ctx: Context[Any, Any, Any],
    timeout: int = _DEFAULT_TIMEOUT,
) -> str:
    """Execute a shell command on a specific node and return the raw output.

    Use list_nodes first to discover available node IDs.

    Args:
        node_id: Target node ID (e.g. 'worker-1.example.com').
        command: Shell command to execute on the target node.
        timeout: Timeout in seconds (default: 120).
    """
    node_id = node_id.strip()
    if not node_id:
        return json.dumps({"error": "node_id must be a non-empty string"})
    cmd_err = _validate_command(command)
    if cmd_err:
        return json.dumps({"error": cmd_err})

    server_kw: dict[str, Any] = ctx.request_context.lifespan_context["server_kwargs"]
    max_output = _DEFAULT_MAX_OUTPUT

    log.info("run_on_node node=%s cmd=%r", node_id, command)
    _, result = await asyncio.to_thread(
        _exec_on_node_sync, server_kw, node_id, command, _clamp_timeout(timeout)
    )
    formatted = _format_node_result(node_id, result, max_output)
    return json.dumps(formatted, indent=2)


@mcp.tool()
async def run_on_nodes(
    node_ids: list[str],
    command: str,
    ctx: Context[Any, Any, Any],
    timeout: int = _DEFAULT_TIMEOUT,
) -> str:
    """Execute a shell command on specific nodes in parallel.

    Results are collected from each node.  Total wall time is bounded by
    the slowest node rather than the sum.

    Args:
        node_ids: List of node IDs to target.
        command: Shell command to execute.
        timeout: Timeout per node in seconds (default: 120).
    """
    cmd_err = _validate_command(command)
    if cmd_err:
        return json.dumps({"error": cmd_err})

    server_kw: dict[str, Any] = ctx.request_context.lifespan_context["server_kwargs"]
    max_output = _DEFAULT_MAX_OUTPUT

    clean_ids = list(dict.fromkeys(nid.strip() for nid in node_ids if nid.strip()))
    if not clean_ids:
        return json.dumps({"error": "node_ids must be a non-empty list"})
    if len(clean_ids) > _MAX_CONCURRENT_NODES:
        return json.dumps(
            {"error": f"Too many nodes ({len(clean_ids)}), max is {_MAX_CONCURRENT_NODES}"}
        )

    log.info("run_on_nodes nodes=%s cmd=%r", clean_ids, command)
    results = await _dispatch_parallel(
        server_kw, clean_ids, command, _clamp_timeout(timeout), max_output
    )
    return json.dumps(results, indent=2)


@mcp.tool()
async def run_on_all_nodes(
    command: str,
    ctx: Context[Any, Any, Any],
    timeout: int = _DEFAULT_TIMEOUT,
) -> str:
    """Execute a shell command on ALL active nodes in parallel.

    Discovers nodes automatically, then dispatches in parallel.  Useful for
    cluster-wide operations like checking disk usage, GPU status, or
    running processes.

    Args:
        command: Shell command to execute on every node.
        timeout: Timeout per node in seconds (default: 120).
    """
    cmd_err = _validate_command(command)
    if cmd_err:
        return json.dumps({"error": cmd_err})

    server_kw: dict[str, Any] = ctx.request_context.lifespan_context["server_kwargs"]
    max_output = _DEFAULT_MAX_OUTPUT

    nodes = await _list_nodes_impl(server_kw)
    if not nodes:
        return json.dumps({"error": "No active nodes found in cluster"})

    node_ids = list(
        dict.fromkeys(
            nid.strip() for n in nodes if (nid := n.get("node_id") or n.get("id")) and nid.strip()
        )
    )
    if not node_ids:
        return json.dumps({"error": "No nodes with identifiable IDs found"})
    if len(node_ids) > _MAX_CONCURRENT_NODES:
        return json.dumps(
            {"error": f"Too many nodes ({len(node_ids)}), max is {_MAX_CONCURRENT_NODES}"}
        )
    log.info("run_on_all_nodes nodes=%s cmd=%r", node_ids, command)
    results = await _dispatch_parallel(
        server_kw, node_ids, command, _clamp_timeout(timeout), max_output
    )
    return json.dumps(results, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP cluster-ops server via stdio transport."""
    logging.basicConfig(level=logging.INFO)
    mcp.run(transport="stdio")
