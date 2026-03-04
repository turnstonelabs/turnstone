# Cluster Dashboard (turnstone-console)

`turnstone-console` is a cluster management service that provides cluster-wide visibility and control across all turnstone nodes. It connects to the shared Redis broker, discovers nodes via heartbeat keys, polls each node's HTTP API for workstream data, and subscribes to a cluster event channel for real-time state changes.

The console also supports **workstream creation** (dispatched via MQ to target nodes) and a **reverse proxy** that serves each node's server UI through the console port — so users only need network access to the console, not to individual server nodes.

## Architecture

> See also: [Console Data Flow diagram](diagrams/png/11-console-data-flow.png)

```
                        ┌── Redis ←── turnstone-bridge ←── turnstone-server
                        │    (MQ)        (per node)           (per node)
turnstone-console ──────┤
   (one instance)       │
                        └── turnstone-server (direct HTTP proxy)
        │
        ▼
     Browser
```

Data flows in two directions:

- **Inbound (monitoring):** Bridges publish state changes to `{prefix}:events:cluster` on Redis pub/sub. The console subscribes for real-time updates and periodically polls each node's `GET /api/dashboard` for full workstream snapshots.
- **Outbound (control):** The console pushes `CreateWorkstreamMessage` to Redis inbound queues targeting specific nodes. Bridges pick up these messages and create workstreams on their local servers.
- **Proxy (pass-through):** The console reverse-proxies each node's server UI at `/node/{node_id}/`, forwarding HTTP and SSE traffic so the browser never contacts server nodes directly.

### Data Sources

| Source | Method | Direction | Data |
|--------|--------|-----------|------|
| Redis heartbeats | `SCAN turnstone:node:*` | Read | Node discovery (node_id, server_url, started) |
| Redis pub/sub | `SUBSCRIBE turnstone:events:cluster` | Read | State changes, creates, closes, renames |
| Node HTTP API | `GET {server_url}/api/dashboard` | Read | Full workstream list with tokens, context, activity |
| Node HTTP API | `GET {server_url}/health` | Read | Node health status |
| Redis inbound queue | `RPUSH turnstone:inbound:{node_id}` | Write | Workstream creation commands |
| Node HTTP API | `GET/POST {server_url}/*` | Proxy | Server UI, API requests, SSE streams |

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
  "aggregate": {"total_tokens": 12400000, "total_tool_calls": 34200},
  "version_drift": true,
  "versions": ["0.3.0", "0.3.1"]
}
```

`version_drift` is `true` when nodes report different versions. `versions` lists all unique version strings sorted alphabetically.

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
      "health": {"status": "ok", "version": "0.3.0"},
      "version": "0.3.0"
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

### `POST /api/cluster/workstreams/new`

Create a new workstream on a target node. Dispatches a `CreateWorkstreamMessage` through the Redis MQ pipeline — the bridge on the target node picks it up and creates the workstream on the server. Requires `"full"` auth role.

Request:

```json
{
  "node_id": "db-west-04",
  "name": "perf-analysis",
  "model": "gpt-5"
}
```

All fields are optional:
- `node_id` — targeting mode:
  - **omitted or `"auto"`** — console picks the reachable node with the most available capacity (max_ws - ws_total) and pushes to its directed queue.
  - **`"pool"`** — pushes to the shared inbound queue; the next available bridge picks it up (true general-pool dispatch).
  - **specific node ID** — pushes to that node's directed queue.
- `name` — workstream display name. Auto-generated if omitted.
- `model` — model alias from the target node's registry. Uses the node's default model if omitted.

Response:

```json
{
  "status": "ok",
  "correlation_id": "a1b2c3d4e5f6",
  "target_node": "db-west-04"
}
```

Creation is asynchronous — the response confirms the MQ message was dispatched. A `ws_created` event on the cluster SSE stream confirms the workstream was actually created.

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
{
  "status": "ok",
  "service": "turnstone-console",
  "nodes": 847,
  "workstreams": 4219,
  "version_drift": false,
  "versions": ["0.3.0"]
}
```

---

## Reverse Proxy

The console reverse-proxies each node's server UI at `/node/{node_id}/`. This allows users to interact with any node's workstreams through the console port alone — individual server ports do not need to be exposed to the office network.

### Proxy Routes

| Route | Behavior |
|-------|----------|
| `GET /node/{node_id}/` | Fetches the server's `index.html`, rewrites static asset paths, injects a console-return banner and a JS proxy shim |
| `GET /node/{node_id}/static/{path}` | Proxies static files; injects a JS shim into `app.js` |
| `GET /node/{node_id}/api/{path}` | Proxies GET API requests; detects SSE endpoints and streams them |
| `POST /node/{node_id}/api/{path}` | Proxies POST API requests with body forwarding |
| `GET /node/{node_id}/{path}` | Proxies non-API endpoints (health, metrics) |

### URL Rewriting

The server UI uses root-relative URLs (`/api/send`, `/static/app.js`, etc.). Since `<base>` tags cannot rewrite root-relative URLs, the console uses a JS shim approach:

1. **HTML rewriting** — when serving `index.html`, replaces `href="/static/"` and `src="/static/"` with the proxy prefix (`/node/{node_id}/static/`).

2. **JS shim injection** — when serving `app.js`, prepends an IIFE that overrides `window.fetch()` and `window.EventSource()` to prepend the proxy prefix to any root-relative URL. This intercepts all API calls and SSE connections transparently.

3. **Console-return banner** — injects a thin inline-styled `<div>` after `<body>` with a "← Console" link and the node ID, providing navigation back to the dashboard.

### SSE Proxy

SSE streams (`/api/events`, `/api/events/global`) are proxied by creating a per-connection `httpx.AsyncClient(timeout=None)`, streaming the upstream response via `aiter_text()`, parsing SSE framing (`\n\n` delimiters), and re-emitting events through `EventSourceResponse`. Each proxied SSE stream requires its own httpx client since the shared client's 30-second timeout would kill long-lived connections.

### Authentication

The proxy forwards requests to server nodes using the console's `--auth-token`. The console's own auth middleware also checks proxy routes — `POST` requests to proxy write endpoints (`/api/send`, `/api/approve`, etc.) require the `"full"` auth role, preventing read-only tokens from escalating to write operations.

---

## Browser Dashboard

The web UI has four views, toggled client-side:

### 1. Cluster Overview (landing)

- **State cards** — 5 clickable cards (running, thinking, attention, idle, error) with count and colored top border. Clicking filters to that state.
- **Aggregate bar** — total tokens and tool calls across the cluster.
- **Node table** — columns: NODE, WS, RUN, ATTN, TOKENS, VER, LOAD. Sorted by activity. Clickable rows drill down to node detail. Version column shows per-node version; hidden on mobile.
- **Version drift indicator** — when nodes report different versions, the status bar shows a yellow "DRIFT" warning with a tooltip listing all versions. Node groups show "mixed" with a yellow badge when their members disagree.
- **"+ new" button** — opens the workstream creation modal (see below).

### 2. Node Drill-down

Breadcrumb: `Cluster > db-west-04`. Shows the node's workstreams in a table matching the per-node dashboard layout (STATE, NAME, MODEL, NODE, TASK, TOKENS, CTX) with activity sub-lines. Includes a link to the node's proxied server UI.

**Proxy deep-linking:** Clicking a workstream row opens the node's server UI in a new tab via the proxy at `/node/{node_id}/?ws_id=<id>`, which auto-selects that workstream. Users do not need direct network access to the server node.

### 3. Filtered Workstreams

Breadcrumb: `Cluster > Running` or `Cluster > db-west-04`. Server-side paginated workstream table. NODE column values are clickable to filter further. Pagination controls at bottom. Workstream rows use proxy deep-links.

### 4. Workstream Creation Modal

Triggered by the "+ new" header button. A modal dialog with:

- **Node selector** — dropdown with three targeting modes: "Auto (best available)" picks the node with the most headroom, "General pool (any node)" pushes to the shared queue for any bridge to pick up, or a specific node from the list (showing capacity).
- **Name** — optional text input. Auto-generated if left empty.
- **Model** — optional text input for a model alias from the target node's registry.

On submit, `POST /api/cluster/workstreams/new` dispatches the creation request. A toast confirms success; the SSE stream delivers the `ws_created` event to update the dashboard.

All four views receive live updates via SSE — state cards update counts, node rows update metrics, workstream rows update state indicators.

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
| `--auth-token` | `$TURNSTONE_AUTH_TOKEN` | Bearer token for server node communication and proxy |
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
turnstone-console --redis-host localhost --port 8090 --auth-token "$TURNSTONE_AUTH_TOKEN"
```

Open `http://localhost:8090` for the cluster dashboard. Create workstreams via the "+ new" button. Click any workstream to open the proxied server UI — no direct access to server ports required.
