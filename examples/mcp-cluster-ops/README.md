# MCP Cluster Ops

An MCP server that exposes tools for executing commands across a [Turnstone](https://github.com/turnstonelabs/turnstone) cluster. Serves as a reference implementation for both MCP server patterns and Turnstone SDK usage.

## How it works

This server uses Turnstone's SDK client (`TurnstoneServer`) to dispatch shell commands to specific nodes via HTTP. Remote agents execute the command and the raw bash output is captured directly from the `ToolResultEvent` stream — bypassing the costly "agent reads output → re-generates output as completion tokens" round-trip.

Multi-node dispatches run in parallel via `asyncio.gather`, so total wall time is bounded by the slowest node rather than the sum.

## Tools

| Tool | Description |
|------|-------------|
| `list_nodes` | Discover active nodes in the cluster |
| `run_on_node` | Execute a command on a specific node |
| `run_on_nodes` | Execute a command on selected nodes in parallel |
| `run_on_all_nodes` | Execute a command on ALL active nodes in parallel |

## Prerequisites

- A running Turnstone cluster (at least one `turnstone-server`)
- Python 3.11+

## Installation

```bash
# From the turnstone repo root:
pip install -e ./examples/mcp-cluster-ops
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TURNSTONE_SERVER_URL` | `http://localhost:8080` | Server URL |
| `TURNSTONE_API_TOKEN` | _(none)_ | API token for authentication |
| `MCP_CLUSTER_OPS_TIMEOUT` | `120` | Default command timeout (seconds, clamped 5-3600) |
| `MCP_CLUSTER_OPS_MAX_OUTPUT` | `8192` | Max output bytes per node (0 = unlimited) |
| `MCP_CLUSTER_OPS_MAX_NODES` | `32` | Max concurrent node dispatches |
| `MCP_CLUSTER_OPS_MAX_COMMAND` | `65536` | Max command string length |

### Register with Turnstone

**TOML** (`~/.config/turnstone/config.toml`):

```toml
[mcp.servers.cluster-ops]
command = "mcp-cluster-ops"

[mcp.servers.cluster-ops.env]
TURNSTONE_SERVER_URL = "http://turnstone.example.com:8080"
```

**JSON** (via `--mcp-config`):

```json
{
  "mcpServers": {
    "cluster-ops": {
      "command": "mcp-cluster-ops",
      "env": {
        "TURNSTONE_SERVER_URL": "http://turnstone.example.com:8080"
      }
    }
  }
}
```

## Usage Examples

Once registered, the tools appear in any Turnstone session. The model can:

```
> Check disk usage across the cluster

[calls list_nodes → discovers node-1, node-2, node-3]
[calls run_on_all_nodes with "df -h /"]

node-1: /dev/sda1  500G  320G  180G  64%  /
node-2: /dev/sda1  500G  410G   90G  82%  /
node-3: /dev/sda1  1.0T  200G  800G  20%  /
```

## Security Considerations

**This MCP server grants the calling agent shell access to cluster nodes.**

- Commands are executed with `auto_approve=True` and the privileges of the
  Turnstone server process on the target node.
- Command output (which may contain secrets, credentials, or sensitive data)
  is returned through the MCP tool result and becomes part of the LLM context.
- The security boundary is at the MCP host layer -- use Turnstone's tool
  policy system to restrict which agents can invoke these tools.
- Set `TURNSTONE_API_TOKEN` via your environment or a secrets manager -- avoid
  hardcoding tokens in config files.

## Development

```bash
cd examples/mcp-cluster-ops

# Run tests
pip install -e ".[test]"
pytest

# Lint
pip install -e ".[dev]"
ruff check mcp_cluster_ops/
mypy --strict mcp_cluster_ops/
```
