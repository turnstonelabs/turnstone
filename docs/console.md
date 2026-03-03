# Cluster Dashboard (turnstone-console)

`turnstone-console` is a standalone monitoring service that provides cluster-wide visibility across all turnstone nodes. It connects to the shared Redis broker, discovers nodes via heartbeat keys, polls each node's HTTP API for workstream data, and subscribes to a cluster event channel for real-time state changes.

The console is read-only — it observes but does not own workstreams or drive LLM sessions.

## Architecture

> See also: [Console Data Flow diagram](diagrams/png/11-console-data-flow.png)

```
turnstone-server ──→ turnstone-bridge ──→ Redis ──→ turnstone-console ──→ Browser
     (per node)         (per node)       (shared)      (one instance)
```

Each bridge publishes state changes to `{prefix}:events:cluster` on Redis pub/sub. The console subscribes once to that channel for real-time updates and periodically polls each node's `GET /api/dashboard` for full workstream snapshots.

### Data Sources

| Source | Method | Frequency | Data |
|--------|--------|-----------|------|
| Redis heartbeats | `SCAN turnstone:node:*` | Every 15s | Node discovery (node_id, server_url, started) |
| Redis pub/sub | `SUBSCRIBE turnstone:events:cluster` | Real-time | State changes, creates, closes, renames |
| Node HTTP API | `GET {server_url}/api/dashboard` | Every 10s | Full workstream list with tokens, context, activity |
| Node HTTP API | `GET {server_url}/health` | Every 10s | Node health status |

### Redis Key: Cluster Event Channel

Bridges publish to `{prefix}:events:cluster` whenever a workstream state change, creation, closure, or rename occurs. Events include `node_id` so the console can attribute them to the correct node.

Event types on the cluster channel:

| Event | Fields | Trigger |
|-------|--------|---------|
| `cluster_state` | ws_id, state, node_id, tokens, context_ratio, activity | Workstream state transition |
| `ws_created` | ws_id, name, node_id | New workstream created |
| `ws_closed` | ws_id | Workstream closed |
| `ws_rename` | ws_id, name | Workstream renamed |

---

## ClusterCollector

The collector (`turnstone/console/collector.py`) maintains an in-memory snapshot of all nodes and workstreams. Three daemon threads handle data acquisition:

1. **Event subscriber** — subscribes to `{prefix}:events:cluster` via `RedisBroker.subscribe_cluster()`. Applies state changes, creates, closes, and renames to the in-memory model immediately.

2. **Node discovery** — scans heartbeat keys every 15 seconds via `broker.list_nodes()`. Adds newly discovered nodes, removes expired ones, emits `node_joined` / `node_lost` events to SSE listeners.

3. **Poll loop** — fetches `GET /api/dashboard` and `GET /health` from each known node every 10 seconds. Uses `ThreadPoolExecutor(max_workers=50)` for parallelism. Each poll replaces the node's workstream list with the authoritative server data.

### Thread Safety

All reads and writes to the node/workstream map are protected by a single `threading.Lock`. Query methods acquire the lock, copy data, and release before returning.

### Scale Considerations

- **10,000 workstreams** at ~500 bytes each = ~5 MB in memory
- **1,000 nodes** polled in parallel with 50 threads at ~100ms each = ~2 second poll cycle
- **Filtering and pagination** run in-memory on the full workstream list — sub-millisecond at this scale
- **SSE fan-out** uses the same per-client queue pattern as the per-node server — backed-up clients get events dropped, not blocking

---

## HTTP API

### `GET /api/cluster/overview`

Cluster-wide state counts and aggregate metrics.

```json
{
  "nodes": 847,
  "workstreams": 4219,
  "states": {"running": 1847, "thinking": 312, "attention": 89, "idle": 1940, "error": 31},
  "aggregate": {"total_tokens": 12400000, "total_tool_calls": 34200}
}
```

### `GET /api/cluster/nodes?sort=activity&limit=100&offset=0`

Paginated node list. Sort options: `activity` (default, by running+attention count), `tokens`, `name`.

```json
{
  "nodes": [
    {
      "node_id": "db-west-04",
      "server_url": "http://10.0.3.4:8080",
      "ws_total": 6, "ws_running": 4, "ws_thinking": 0, "ws_attention": 1, "ws_idle": 1, "ws_error": 0,
      "total_tokens": 48200,
      "started": 1709294400.0,
      "reachable": true,
      "health": {}
    }
  ],
  "total": 847
}
```

### `GET /api/cluster/workstreams?state=running&node=db-west-04&search=perf&page=1&per_page=50`

Filtered, paginated workstream list. All query parameters are optional. `per_page` is capped at 200.

```json
{
  "workstreams": [
    {
      "id": "a1b2c3d4", "name": "perf-db-west", "state": "running", "node": "db-west-04",
      "title": "Query latency analysis", "tokens": 24100, "context_ratio": 0.18,
      "activity": "bash: EXPLAIN ANALYZE...", "activity_state": "tool", "tool_calls": 42
    }
  ],
  "total": 1847, "page": 1, "per_page": 50, "pages": 37
}
```

### `GET /api/cluster/node/{node_id}`

Single node detail with all its workstreams.

```json
{
  "node_id": "db-west-04",
  "server_url": "http://10.0.3.4:8080",
  "health": {"status": "ok", "version": "0.2.0", "model": "kappa_20b_131k"},
  "workstreams": [...],
  "aggregate": {"total_tokens": 48200, "total_tool_calls": 156}
}
```

### `GET /api/cluster/events`

Server-Sent Events stream for real-time cluster updates.

```
data: {"type":"cluster_state","ws_id":"a1b2","node_id":"db-west-04","state":"running"}
data: {"type":"ws_created","ws_id":"e5f6","node_id":"api-east-01","name":"new-task"}
data: {"type":"ws_closed","ws_id":"a1b2"}
data: {"type":"node_joined","node_id":"db-west-05"}
data: {"type":"node_lost","node_id":"db-west-03"}
```

Keepalive comments (`: keepalive\n\n`) are sent every 5 seconds. Clients should reconnect on error with exponential backoff.

### `GET /health`

```json
{"status": "ok", "service": "turnstone-console", "nodes": 847, "workstreams": 4219}
```

---

## Browser Dashboard

The web UI has three views, toggled client-side:

### 1. Cluster Overview (landing)

- **State cards** — 5 clickable cards (running, thinking, attention, idle, error) with count and colored top border. Clicking filters to that state.
- **Aggregate bar** — total tokens and tool calls across the cluster.
- **Node table** — columns: NODE, WS, RUN, ATTN, TOKENS, HEALTH. Sorted by activity. Clickable rows drill down to node detail.

### 2. Node Drill-down

Breadcrumb: `Cluster > db-west-04`. Shows the node's workstreams in a table matching the per-node dashboard layout (STATE, NAME, NODE, TASK, TOKENS, CTX) with activity sub-lines. Includes a link to the node's own dashboard (`http://{server_url}/`).

**Deep linking:** Clicking a workstream row opens the node's server UI in a new tab with `?ws_id=<id>`, which auto-selects that workstream. A `↗` indicator appears on hover to signal the external navigation. Rows without a `server_url` are non-interactive.

### 3. Filtered Workstreams

Breadcrumb: `Cluster > Running` or `Cluster > db-west-04`. Server-side paginated workstream table. NODE column values are clickable to filter further. Pagination controls at bottom. Workstream rows are deep-linkable when `server_url` is available (injected by the collector from the parent node).

All three views receive live updates via SSE — state cards update counts, node rows update metrics, workstream rows update state indicators.

---

## CLI Commands

The `/cluster` command in the turnstone CLI queries the console's HTTP API. Requires `--console-url` or `[console] url` in config.toml.

| Command | Description |
|---------|-------------|
| `/cluster status` | Cluster overview — node/workstream counts, state breakdown, aggregate stats |
| `/cluster nodes` | Node table — WS, RUN, ATTN, TOKENS per node |
| `/cluster workstreams [state] [node=X]` | Filtered workstream list with state, name, node, tokens, context |
| `/cluster node <id>` | Single node's workstreams with activity details |

---

## Configuration

CLI flags for `turnstone-console`:

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `0.0.0.0` | Bind host |
| `--port` | `8090` | HTTP port |
| `--redis-host` | `localhost` | Redis host |
| `--redis-port` | `6379` | Redis port |
| `--redis-password` | `$REDIS_PASSWORD` | Redis password |
| `--redis-db` | `0` | Redis DB |
| `--poll-interval` | `10` | Node polling interval (seconds) |
| `--log-level` | `INFO` | Log level |

Config file (`~/.config/turnstone/config.toml`):

```toml
[console]
host = "0.0.0.0"
port = 8090
url = "http://localhost:8090"   # used by CLI /cluster commands
poll_interval = 10

[redis]
host = "localhost"
port = 6379
password = "my-redis-password"
```

---

## Deployment

```bash
# Start Redis
redis-server

# Start turnstone servers (one per node)
turnstone-server --port 8080

# Start bridges (one per server)
turnstone-bridge --server-url http://localhost:8080 --node-id node-a

# Start cluster console (one instance)
turnstone-console --redis-host localhost --port 8090
```

Open `http://localhost:8090` for the cluster dashboard.
