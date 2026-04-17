# MCP Cluster Ops

An MCP server that exposes tools for executing commands across a Turnstone cluster. Serves as a reference implementation for both MCP server patterns and Turnstone SDK usage.

> [!NOTE]
> **Superseded by the built-in coordinator workstream in Turnstone 1.5.**
>
> This MCP side-car is the pre-1.5 pattern for cluster-wide orchestration.
> Turnstone 1.5 promotes coordinator behaviour to a first-class workstream
> kind hosted inside `turnstone-console` — no external MCP server to
> install or operate, proper per-user audit attribution, and a dedicated
> UI at `/coordinator/{ws_id}`.
>
> The extension continues to work for 1.4-and-earlier clusters. On 1.5+:
> grant the `admin.coordinator` permission, set `coordinator.model_alias`
> in the admin Settings tab, and create sessions via the dashboard's
> "new coordinator" button or `POST /v1/api/coordinator/new`. Full
> removal of this example (including docker / compose references) is
> planned once 1.5 is confirmed in production.
>
> | Concern | Built-in coordinator (1.5+) | This MCP extension (1.4-and-earlier) |
> |---|---|---|
> | Install | None — shipped in-tree | `pip install -e examples/mcp-cluster-ops` + MCP client config |
> | Auth | Real creator's `user_id` + `admin.coordinator` permission | Shared service token |
> | Audit | `coordinator.create` / `close` / `cancel` events on the console; `src="coordinator"` preserved on upstream hops | Service identity only |
> | UI | `/coordinator/{ws_id}` one-pane HTML | No UI — model-only |
> | Tool approvals | Inline approval bar in the coordinator pane | MCP approval flow |
> | Configuration | `coordinator.model_alias`, `coordinator.max_active`, `coordinator.reasoning_effort`, `coordinator.session_jwt_ttl_seconds` | MCP server config file |
>
> Minimal 1.5 migration:
>
> ```bash
> curl -X POST https://console.example/v1/api/coordinator/new \
>   -H "Authorization: Bearer $TOKEN" \
>   -H "Content-Type: application/json" \
>   -d '{"name":"planner","initial_message":"Spawn a worker to check the build"}'
> ```
>
> The response carries `ws_id`; open
> `https://console.example/coordinator/{ws_id}` to watch the session.

## How it works

This server uses the Turnstone console SDK (`TurnstoneConsole`) for node discovery and routing, and `TurnstoneServer` for per-node SSE streaming. The dispatch flow for each command is:

1. **Route** — `TurnstoneConsole.route_create_workstream(target_node=..., auto_approve=True)` creates a workstream pinned to the target node via the console's hash-ring routing proxy, returning `ws_id` and `node_url`.
2. **Execute** — `TurnstoneServer(node_url, token=...)` connects directly to the node's SSE stream using the same `TURNSTONE_API_TOKEN`. `send_and_wait(prompt, ws_id)` runs the command and the raw bash output is captured from the `ToolResultEvent` — bypassing the costly "agent reads output then re-generates output as completion tokens" round-trip.
3. **Cleanup** — `TurnstoneConsole.route_close(ws_id)` closes the workstream.

Multi-node dispatches run in parallel via `asyncio.gather`, so total wall time is bounded by the slowest node rather than the sum.

## Tools

| Tool | Description |
|------|-------------|
| `list_nodes` | Discover active nodes in the cluster |
| `run_on_node` | Execute a command on a specific node |
| `run_on_nodes` | Execute a command on selected nodes in parallel |
| `run_on_all_nodes` | Execute a command on ALL active nodes in parallel |

## Prerequisites

- A running Turnstone cluster with at least one `turnstone-server` and a `turnstone-console`
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
| `TURNSTONE_CONSOLE_URL` | `http://localhost:8090` | Console URL for node discovery and routing |
| `TURNSTONE_API_TOKEN` | _(none)_ | API token / JWT for authentication |
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
TURNSTONE_CONSOLE_URL = "http://console.example.com:8090"
```

**JSON** (via `--mcp-config`):

```json
{
  "mcpServers": {
    "cluster-ops": {
      "command": "mcp-cluster-ops",
      "env": {
        "TURNSTONE_CONSOLE_URL": "http://console.example.com:8090"
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
