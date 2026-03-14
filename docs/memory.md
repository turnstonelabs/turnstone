# Structured Memory

> See also: [Memory Architecture diagram](diagrams/png/23-memory-architecture.png)

The structured memory system gives the AI persistent, typed, scoped memories
that survive across sessions and workstreams. Memories are automatically
surfaced in the system message via BM25 relevance scoring, so the model has
contextual recall without explicit search.

## Overview

Each memory has three dimensions:

- **Type** -- categorizes the memory's purpose
- **Scope** -- controls visibility boundaries
- **Name** -- unique identifier within a scope (snake_case, normalized)

### Memory types

| Type        | Purpose                                                    |
|-------------|------------------------------------------------------------|
| `user`      | User preferences, conventions, working style               |
| `project`   | Project-specific knowledge, architecture, patterns         |
| `feedback`  | Corrections, lessons learned, things to avoid              |
| `reference` | Reference material, documentation, specifications         |

### Memory scopes

| Scope        | Visibility                                                |
|--------------|-----------------------------------------------------------|
| `global`     | Visible to all workstreams and users                      |
| `workstream` | Visible only within the originating workstream            |
| `user`       | Follows the authenticated user across workstreams         |

A memory's identity is the tuple `(name, scope, scope_id)`. Saving a memory
with the same identity upserts -- updating content while preserving the ID.

### BM25 relevance injection

On every conversation turn, the system:

1. Fetches up to `fetch_limit` memories visible in the current scope
2. Extracts context from the last 3 user messages
3. Scores memories against that context using a BM25 index
4. Injects the top `relevance_k` memories into the system message as
   `<memories>` XML tags
5. Appends a hint telling the model how many memories are in scope

This means the model always has its most relevant memories available without
explicit recall -- but can still use `memory(action='search')` for deeper
lookup.

### Nudges

The metacognition layer can nudge the model to save memories at appropriate
moments (e.g., after a correction or when resuming a workstream). Nudges are
rate-limited by `nudge_cooldown` and can be disabled entirely.

---

## Configuration

### config.toml

```toml
[memory]
relevance_k = 5          # top-k memories injected per turn
fetch_limit = 50          # max memories fetched from storage for scoring
max_content = 32768       # max content length per memory (characters)
nudge_cooldown = 300      # minimum seconds between memory nudges
nudges = true             # enable/disable metacognitive nudges
```

All fields are optional. Defaults are shown above.

---

## Tool Usage

The `memory` tool supports four actions:

### save

Store or update a memory.

```json
{
  "action": "save",
  "name": "project_architecture",
  "content": "The project uses a hexagonal architecture with...",
  "description": "Core architecture patterns",
  "type": "project",
  "scope": "global"
}
```

| Parameter     | Required | Default     | Description                              |
|---------------|----------|-------------|------------------------------------------|
| `name`        | yes      | --          | Snake_case identifier (max 256 chars)    |
| `content`     | yes      | --          | Memory content (max `max_content` chars) |
| `description` | no       | `""`        | Short description for relevance matching |
| `type`        | no       | `"project"` | One of: user, project, feedback, reference |
| `scope`       | no       | `"global"`  | One of: global, workstream, user         |

### search

Find memories by query (BM25 full-text search).

```json
{
  "action": "search",
  "query": "authentication patterns",
  "type": "project",
  "limit": 10
}
```

| Parameter | Required | Default | Description                          |
|-----------|----------|---------|--------------------------------------|
| `query`   | yes      | --      | Search query                         |
| `type`    | no       | `""`    | Filter by type                       |
| `scope`   | no       | `""`    | Filter by scope                      |
| `limit`   | no       | `20`    | Max results (capped at 50)           |

### delete

Remove a memory by name.

```json
{
  "action": "delete",
  "name": "outdated_pattern",
  "scope": "global"
}
```

| Parameter  | Required | Default    | Description              |
|------------|----------|------------|--------------------------|
| `name`     | yes      | --         | Memory name to delete    |
| `scope`    | no       | `"global"` | Scope of the memory      |

### list

List all memories with optional filters.

```json
{
  "action": "list",
  "type": "feedback",
  "limit": 50
}
```

| Parameter | Required | Default | Description                |
|-----------|----------|---------|----------------------------|
| `type`    | no       | `""`    | Filter by type             |
| `scope`   | no       | `""`    | Filter by scope            |
| `limit`   | no       | `20`    | Max results (capped at 50) |

---

## Server API

Four endpoints on the server for programmatic memory access.

### `GET /v1/api/memories`

List memories with optional filters.

**Query parameters:**

| Parameter  | Type   | Required | Default | Description                  |
|------------|--------|----------|---------|------------------------------|
| `type`     | string | no       | `""`    | Filter by memory type        |
| `scope`    | string | no       | `""`    | Filter by scope              |
| `scope_id` | string | no       | `""`    | Filter by scope ID           |
| `limit`    | int    | no       | `100`   | Max results (capped at 200)  |

When `scope=user` and `scope_id` is omitted, the authenticated user's ID is
used automatically.

**Response:** `200`

```json
{
  "memories": [
    {
      "memory_id": "a1b2c3d4-e5f6-...",
      "name": "project_architecture",
      "description": "Core architecture patterns",
      "type": "project",
      "scope": "global",
      "scope_id": "",
      "content": "The project uses a hexagonal architecture...",
      "created": "2026-03-10T10:00:00",
      "updated": "2026-03-12T14:30:00"
    }
  ],
  "total": 1
}
```

---

### `POST /v1/api/memories`

Save or upsert a structured memory.

**Request body:**

```json
{
  "name": "deployment_process",
  "content": "Deploy via GitHub Actions. Staging auto-deploys on push to main.",
  "description": "CI/CD deployment workflow",
  "type": "project",
  "scope": "global",
  "scope_id": ""
}
```

| Field        | Type   | Required | Default     | Description                          |
|--------------|--------|----------|-------------|--------------------------------------|
| `name`       | string | yes      | --          | Memory name (max 256 chars)          |
| `content`    | string | yes      | --          | Memory content (max 65536 chars)     |
| `description`| string | no       | `""`        | Short description for search ranking |
| `type`       | string | no       | `"project"` | One of: user, project, feedback, reference |
| `scope`      | string | no       | `"global"`  | One of: global, workstream, user     |
| `scope_id`   | string | no       | `""`        | Scope qualifier (auto-resolved for user scope) |

**Response (created):** `201`

```json
{
  "memory_id": "a1b2c3d4-e5f6-...",
  "name": "deployment_process",
  "description": "CI/CD deployment workflow",
  "type": "project",
  "scope": "global",
  "scope_id": "",
  "content": "Deploy via GitHub Actions...",
  "created": "2026-03-14T10:00:00",
  "updated": "2026-03-14T10:00:00"
}
```

**Response (updated):** `200` -- same schema, returned when a memory with the
same `(name, scope, scope_id)` already existed.

**Errors:**

| Status | Condition                          |
|--------|------------------------------------|
| 400    | Missing name, empty content, invalid type/scope, content too long |

---

### `POST /v1/api/memories/search`

Search memories by query. Uses POST for the request body but is non-mutating
(requires only `read` scope).

**Request body:**

```json
{
  "query": "authentication",
  "type": "project",
  "scope": "",
  "scope_id": "",
  "limit": 20
}
```

| Field      | Type   | Required | Default | Description                    |
|------------|--------|----------|---------|--------------------------------|
| `query`    | string | yes      | --      | Search query                   |
| `type`     | string | no       | `""`    | Filter by type                 |
| `scope`    | string | no       | `""`    | Filter by scope                |
| `scope_id` | string | no       | `""`    | Filter by scope ID             |
| `limit`    | int    | no       | `20`    | Max results (capped at 50)     |

**Response:** `200`

```json
{
  "memories": [
    {
      "memory_id": "a1b2c3d4-e5f6-...",
      "name": "auth_patterns",
      "description": "Authentication architecture",
      "type": "project",
      "scope": "global",
      "scope_id": "",
      "content": "JWT tokens with HS256...",
      "created": "2026-03-10T10:00:00",
      "updated": "2026-03-12T14:30:00"
    }
  ],
  "total": 1
}
```

---

### `DELETE /v1/api/memories/{name}`

Delete a memory by name and scope.

**Path parameters:**

| Parameter | Type   | Description          |
|-----------|--------|----------------------|
| `name`    | string | Memory name          |

**Query parameters:**

| Parameter  | Type   | Required | Default    | Description         |
|------------|--------|----------|------------|---------------------|
| `scope`    | string | no       | `"global"` | Scope of the memory |
| `scope_id` | string | no       | `""`       | Scope qualifier     |

**Response (success):** `200`

```json
{"status": "ok", "name": "deployment_process"}
```

**Response (not found):** `404`

```json
{"error": "Memory 'deployment_process' not found"}
```

---

## Console Admin API

Four admin endpoints for cross-workstream memory management. All require the
`admin.memories` permission.

### `GET /v1/api/admin/memories`

List memories across all scopes (no automatic scope resolution).

**Query parameters:**

| Parameter  | Type   | Required | Default | Description                  |
|------------|--------|----------|---------|------------------------------|
| `type`     | string | no       | `""`    | Filter by type               |
| `scope`    | string | no       | `""`    | Filter by scope              |
| `scope_id` | string | no       | `""`    | Filter by scope ID           |
| `limit`    | int    | no       | `100`   | Max results (capped at 200)  |

**Response:** `200`

```json
{
  "memories": [
    {
      "memory_id": "a1b2c3d4-e5f6-...",
      "name": "project_architecture",
      "description": "Core architecture patterns",
      "type": "project",
      "scope": "global",
      "scope_id": "",
      "content": "The project uses...",
      "created": "2026-03-10T10:00:00",
      "updated": "2026-03-12T14:30:00"
    }
  ],
  "total": 1
}
```

---

### `GET /v1/api/admin/memories/search`

Search memories by query (uses query parameters, not POST body).

**Query parameters:**

| Parameter  | Type   | Required | Default | Description                   |
|------------|--------|----------|---------|-------------------------------|
| `q`        | string | yes      | --      | Search query                  |
| `type`     | string | no       | `""`    | Filter by type                |
| `scope`    | string | no       | `""`    | Filter by scope               |
| `scope_id` | string | no       | `""`    | Filter by scope ID            |
| `limit`    | int    | no       | `20`    | Max results (capped at 50)    |

**Response:** `200` -- same schema as `GET /v1/api/admin/memories`.

---

### `GET /v1/api/admin/memories/{memory_id}`

Get a single memory by ID.

**Path parameters:**

| Parameter   | Type   | Description            |
|-------------|--------|------------------------|
| `memory_id` | string | Memory UUID            |

**Response (success):** `200`

```json
{
  "memory_id": "a1b2c3d4-e5f6-...",
  "name": "project_architecture",
  "description": "Core architecture patterns",
  "type": "project",
  "scope": "global",
  "scope_id": "",
  "content": "The project uses...",
  "created": "2026-03-10T10:00:00",
  "updated": "2026-03-12T14:30:00"
}
```

**Response (not found):** `404`

```json
{"error": "Memory not found"}
```

---

### `DELETE /v1/api/admin/memories/{memory_id}`

Delete a memory by ID. Records an audit event (`memory.delete`).

**Path parameters:**

| Parameter   | Type   | Description            |
|-------------|--------|------------------------|
| `memory_id` | string | Memory UUID            |

**Response (success):** `200`

```json
{"status": "ok"}
```

**Response (not found):** `404`

```json
{"error": "Memory not found"}
```

---

## SDK

### Python

The server SDK uses `mem_type` (not `type`) to avoid shadowing the Python
builtin.

```python
from turnstone.sdk import TurnstoneServer

with TurnstoneServer("http://localhost:8080", token="tok_xxx") as client:
    # Save a memory
    mem = client.save_memory(
        "api_conventions",
        "All endpoints use /v1/ prefix. JSON responses.",
        description="API design patterns",
        mem_type="project",
        scope="global",
    )
    print(mem.memory_id)

    # Search memories
    results = client.search_memories("authentication", mem_type="project", limit=10)
    for m in results.memories:
        print(f"{m['name']}: {m['description']}")

    # List memories
    all_mems = client.list_memories(mem_type="feedback", limit=50)

    # Delete a memory
    client.delete_memory("api_conventions", scope="global")
```

Console admin SDK:

```python
from turnstone.sdk import TurnstoneConsole

with TurnstoneConsole("http://localhost:9090", token="tok_xxx") as admin:
    # List all memories (admin view, no scope auto-resolution)
    result = admin.list_memories(scope="global", limit=100)

    # Search
    result = admin.search_memories("architecture", mem_type="project")

    # Get by ID
    mem = admin.get_memory("a1b2c3d4-e5f6-...")

    # Delete by ID
    admin.delete_memory("a1b2c3d4-e5f6-...")
```

### TypeScript

```typescript
import { TurnstoneServer } from "@turnstone/sdk";

const client = new TurnstoneServer({
  baseUrl: "http://localhost:8080",
  token: "tok_xxx",
});

// Save a memory
const mem = await client.saveMemory({
  name: "api_conventions",
  content: "All endpoints use /v1/ prefix. JSON responses.",
  description: "API design patterns",
  type: "project",
  scope: "global",
});

// Search memories
const results = await client.searchMemories({
  query: "authentication",
  type: "project",
  limit: 10,
});

// List memories
const all = await client.listMemories({ type: "feedback", limit: 50 });

// Delete a memory
await client.deleteMemory("api_conventions", { scope: "global" });
```

Console admin SDK:

```typescript
import { TurnstoneConsole } from "@turnstone/sdk";

const admin = new TurnstoneConsole({
  baseUrl: "http://localhost:9090",
  token: "tok_xxx",
});

// List, search, get, delete by ID
const mems = await admin.listMemories({ scope: "global" });
const found = await admin.searchMemories({ q: "auth", limit: 20 });
const one = await admin.getMemory("a1b2c3d4-e5f6-...");
await admin.deleteMemory("a1b2c3d4-e5f6-...");
```

---

## Storage

Memories are stored in the `structured_memories` table (migration 013).
The unique constraint on `(name, scope, scope_id)` ensures upsert semantics.
The name is normalized on save: lowercased, hyphens and spaces replaced with
underscores.

## Architecture

See [Memory Architecture diagram](diagrams/png/23-memory-architecture.png) for
the full data flow covering the session tool path, API path, admin path, and
BM25 relevance injection.
