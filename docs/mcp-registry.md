# MCP Registry Integration

Turnstone integrates with the [official MCP Registry](https://registry.modelcontextprotocol.io) to let administrators discover and install MCP servers directly from the console admin panel.

## Overview

The MCP Registry is maintained by the [Agentic AI Foundation](https://www.linuxfoundation.org/press/linux-foundation-announces-the-formation-of-the-agentic-ai-foundation) (Linux Foundation) and serves as the canonical discovery layer for MCP servers. Turnstone queries its REST API (v0.1) for server metadata and provides a one-click install flow.

Three sources of MCP servers coexist in Turnstone:

| Source | Badge | Description |
|--------|-------|-------------|
| **Config** | `CONFIG` (magenta) | Imported from `config.toml` or JSON file. Read-only in admin UI. |
| **Manual** | `MANUAL` (cyan) | Added through the admin UI or API. Full CRUD. |
| **Registry** | `REGISTRY` (green) | Installed from the MCP Registry. Tracked by `registry_name`. |

## Admin UI

The MCP admin tab has two views, toggled by a pill selector:

### Servers View

Lists all installed MCP servers regardless of source. Each server shows:

- **Source badge** — CONFIG, MANUAL, or REGISTRY
- **Transport badge** — stdio or streamable-http
- **Tool/resource/prompt counts** — aggregated across cluster nodes
- **Per-node connection status** — connected (magenta dot), error (red), disabled (gray)
- **Actions** — Edit / Delete (DB-managed servers only)

Clicking a server name opens the detail modal. For registry-installed servers, the detail modal includes a **Registry** section showing the registry name, installed version, description, and website link.

### Registry View

Search and browse the MCP Registry. Switching to this view auto-loads a listing. Type a query and press Enter or click Search to filter.

Each result card shows:

- **Server name and description**
- **Source type badges** — remote (streamable-http), npm, pypi
- **Version number**
- **Install / Installed / Update button**

#### Install flow

- **One-click**: Remote servers with no required headers or URL variables install immediately — no modal, no form. The server is added to the database, all cluster nodes are notified, and a toast confirms success.

- **Modal**: Servers that require configuration (API keys, headers, URL template variables) or offer multiple install sources (both remote and package) open an install modal with:
  - Source selector (radio group) — only shown when both remote and package are available
  - Dynamic form fields for required/optional configuration
  - Secret fields rendered as password inputs

## Configuration

### Registry URL

By default, Turnstone queries `https://registry.modelcontextprotocol.io`. Override this for enterprise or private registries:

**Via admin Settings tab:**

Set `mcp.registry_url` to your registry's base URL.

**Via config.toml:**

```toml
[mcp]
registry_url = "https://registry.internal.example.com"
```

The resolution order is: database setting > config.toml > default.

## API Endpoints

Both endpoints require `admin.mcp` permission.

### Search

```
GET /v1/api/admin/mcp-registry/search?search=github&limit=20&cursor=...
```

Query parameters:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `q` | string | `""` | Search query. Empty returns a browsable listing. |
| `limit` | integer | `20` | Results per page (max 100). |
| `cursor` | string | — | Opaque cursor from `next_cursor` for pagination. |

The response annotates each server with `installed`, `installed_server_id`, `installed_version`, and `update_available` by cross-referencing the `mcp_servers` table.

### Install

```
POST /v1/api/admin/mcp-registry/install
```

```json
{
  "registry_name": "io.example/mcp-server",
  "source": "remote",
  "index": 0,
  "name": "",
  "variables": {},
  "env": {"API_KEY": "sk-..."},
  "headers": {"Authorization": "Bearer ..."}
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `registry_name` | Yes | Server name from registry search results. |
| `source` | Yes | `"remote"` (streamable-http) or `"package"` (npm/pypi). |
| `index` | No | Which remote or package entry to use (default `0`). |
| `name` | No | Custom server name. Auto-derived from registry name if empty. |
| `variables` | No | Values for URL template `{var}` placeholders. |
| `env` | No | Environment variable values for package servers. |
| `headers` | No | Header values for remote servers. |

On success, the server is created in the database and all cluster nodes are automatically reloaded. Returns the created `McpServerDetail`.

Errors: `400` (validation), `404` (not found in registry), `409` (already installed or name collision), `502` (registry unreachable).

## SDK

### Python

```python
from turnstone.sdk.console import TurnstoneConsole

with TurnstoneConsole("http://localhost:8081", token="...") as client:
    # Search
    results = client.search_mcp_registry(q="github", limit=10)
    for srv in results.servers:
        print(f"{srv.name} v{srv.version} - {srv.description}")

    # Install a remote server
    detail = client.install_from_registry(
        "io.example/mcp-server",
        "remote",
        headers={"Authorization": "Bearer sk-..."},
    )
    print(f"Installed: {detail.name}")
```

### TypeScript

```typescript
import { TurnstoneConsole } from "@anthropic/turnstone-sdk";

const client = new TurnstoneConsole({
  baseUrl: "http://localhost:8081",
  token: "...",
});

// Search
const results = await client.searchMcpRegistry({ q: "github", limit: 10 });
for (const srv of results.servers) {
  console.log(`${srv.name} v${srv.version} - ${srv.description}`);
}

// Install
const detail = await client.installFromRegistry({
  registry_name: "io.example/mcp-server",
  source: "remote",
  headers: { Authorization: "Bearer sk-..." },
});
```

## Storage

Registry-installed servers are stored in the existing `mcp_servers` table with three additional columns (migration 019):

| Column | Type | Description |
|--------|------|-------------|
| `registry_name` | TEXT (nullable, unique) | Reverse-DNS name from the registry (e.g. `io.example/mcp-server`). |
| `registry_version` | TEXT | Version at time of install. |
| `registry_meta` | TEXT (JSON) | Snapshot of description, title, website, icons for display. |

The partial unique index on `registry_name` prevents duplicate installs while allowing multiple non-registry servers with `NULL` registry_name.

## Package Type Support

| Registry Type | Transport | Command | Status |
|--------------|-----------|---------|--------|
| Remote (streamable-http) | `streamable-http` | — (URL-based) | Supported |
| `npm` | `stdio` | `npx -y @scope/package@version` | Supported |
| `pypi` | `stdio` | `uvx package==version` | Supported |
| `oci` | — | — | Not supported (no runtime available) |
| `nuget` | — | — | Not supported |
| `mcpb` | — | — | Not supported |

For `npm` and `pypi` packages, the corresponding runtime (`node`/`npx` or `python`/`uvx`) must be available on the cluster nodes. Connection failures due to missing runtimes appear in the per-node MCP status display.
