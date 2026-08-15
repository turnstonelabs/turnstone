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
| `general`   | General knowledge, architecture, patterns                  |
| `feedback`  | Corrections, lessons learned, things to avoid              |
| `reference` | Reference material, documentation, specifications         |

### Memory scopes

| Scope         | Visibility                                                      |
|---------------|-----------------------------------------------------------------|
| `global`      | Visible to all workstreams and users                            |
| `workstream`  | Visible only within the originating workstream                  |
| `user`        | Follows the authenticated user across workstreams               |
| `coordinator` | Coordinator sessions only; follows the acting user              |
| `project`     | Shared by workstreams attached to one active project             |

A memory's identity is the tuple `(name, scope, scope_id)`. Saving a memory
with the same identity upserts -- updating content while preserving the ID.

### Inherited target and coordinator scope

Name-based operations use one inherited target when `scope` is omitted:

- An attached active project selects `project` for `save`, `get`, and
  `delete`.
- Read-only project access permits `get`, but `save` and `delete` fail. They do
  not fall back to a broader namespace.
- Without a project, interactive sessions select `global`; coordinator
  sessions select `coordinator`.

A valid explicit scope selects exactly that scope. `search` and `list` are the
only actions that span every visible scope when `scope` is omitted.

Each coordinator's private `coordinator` namespace is keyed by the acting
user's `user_id`. It is durable -- every coordinator session that user runs
(including concurrent ones) shares one orchestration namespace, so procedures
and lessons survive close/reopen.

Isolation is bidirectional and enforced by session kind, not by secrecy of
the scope id:

- A coordinator session sees its acting user's `coordinator` scope and, when
  attached, the shared `project` scope. It never sees
  `global`/`workstream`/`user` memories.
- Interactive sessions -- including a coordinator's own children, which share
  its `user_id` -- are rejected from the `coordinator` scope on every memory
  action. Children cannot plant rows the parent coordinator would read.
- The REST memory API (`/v1/api/memories`) does not accept the `coordinator`
  scope at all; the scope is written exclusively through a coordinator
  session's own memory tool.

Coordinator sessions require an authenticated user identity -- an anonymous
coordinator cannot be constructed, so the scope id is always a real user.

### BM25 relevance injection

On every conversation turn, the system:

1. Resolves the acting principal and their live project access
2. Fetches up to `fetch_limit` memories across that visibility envelope
3. Extracts context from the last 3 user messages
4. Scores memories against that context using a BM25 index
5. Injects the top `relevance_k` memories into the system message as
   `<memories>` XML tags
6. Appends a hint telling the model how many memories are in scope

This means the model always has its most relevant memories available without
explicit recall -- but can still use `memory(action='search')` for deeper
lookup.

The persona memory lever gates this pathway: a workstream whose persona
turns memory off receives no relevance injection at all -- the steps
above run only when memory is enabled for the session. See
[Personas](personas.md).

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

The `memory` tool supports five actions:

### save

Store or update a memory.

Every save is a complete write for the relevance summary: `description` must
be supplied and contain non-whitespace text on both creation and update.
Content-only updates are rejected.

```json
{
  "action": "save",
  "name": "project_architecture",
  "content": "The project uses a hexagonal architecture with...",
  "description": "Core architecture patterns",
  "type": "general",
  "scope": "global"
}
```

| Parameter     | Required | Default     | Description                              |
|---------------|----------|-------------|------------------------------------------|
| `name`        | yes      | --          | Snake_case identifier (max 256 chars)    |
| `content`     | yes      | --          | Memory content (max `max_content` chars) |
| `description` | yes      | --          | Non-empty relevance summary, required on create and update |
| `type`        | no       | `"general"` | One of: user, general, feedback, reference |
| `scope`       | no       | inherited   | Kind-valid scope; see inherited target above |

### get

Retrieve the full content of one memory by name.

```json
{
  "action": "get",
  "name": "project_architecture",
  "scope": "project"
}
```

| Parameter | Required | Default   | Description                |
|-----------|----------|-----------|----------------------------|
| `name`    | yes      | --        | Memory name to retrieve    |
| `scope`   | no       | inherited | Exact scope to query       |

### search

Find memories by query (BM25 full-text search).

```json
{
  "action": "search",
  "query": "authentication patterns",
  "type": "general",
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
| `scope`    | no       | inherited | Exact scope to delete    |

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

Without `scope`, the response is restricted to `global` plus the authenticated
caller's `user` namespace. The public API accepts only `global`, `user`, and
`workstream`; internal `project` and `coordinator` namespaces remain available
through the session tool and admin API. Explicit `workstream` access requires
its persisted owner (or a service token).

**Query parameters:**

| Parameter  | Type   | Required | Default | Description                  |
|------------|--------|----------|---------|------------------------------|
| `type`     | string | no       | `""`    | Filter by memory type        |
| `scope`    | string | no       | `""`    | Filter by scope              |
| `scope_id` | string | no       | `""`    | Filter by scope ID           |
| `limit`    | int    | no       | `100`   | Max results (1-200)          |

When `scope=user`, the authenticated user's ID is used automatically and a
different supplied ID is rejected. `scope=workstream` requires `scope_id`.

**Response:** `200`

```json
{
  "memories": [
    {
      "memory_id": "a1b2c3d4-e5f6-...",
      "name": "project_architecture",
      "description": "Core architecture patterns",
      "type": "general",
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

`description` is mandatory for both creates and updates and must contain
non-whitespace text. The API rejects content-only updates.

**Request body:**

```json
{
  "name": "deployment_process",
  "content": "Deploy via GitHub Actions. Staging auto-deploys on push to main.",
  "description": "CI/CD deployment workflow",
  "type": "general",
  "scope": "global",
  "scope_id": ""
}
```

| Field        | Type   | Required | Default     | Description                          |
|--------------|--------|----------|-------------|--------------------------------------|
| `name`       | string | yes      | --          | Memory name (max 256 chars)          |
| `content`    | string | yes      | --          | Memory content (max 65536 chars)     |
| `description`| string | yes      | --          | Non-empty relevance summary, required on create and update |
| `type`       | string | no       | unset       | user, general, feedback, or reference |
| `scope`      | string | no       | `"global"`  | One of: global, workstream, user     |
| `scope_id`   | string | no       | `""`        | Scope qualifier (auto-resolved for user scope) |

**Response (created):** `201`

```json
{
  "memory_id": "a1b2c3d4-e5f6-...",
  "name": "deployment_process",
  "description": "CI/CD deployment workflow",
  "type": "general",
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
| 400    | Invalid input, scope, scope ID, or limit |
| 403    | Cross-user or non-owner workstream access |
| 404    | Explicit workstream does not exist |
| 500    | Storage mutation failed |

---

### `POST /v1/api/memories/search`

Search memories by query. Uses POST for the request body but is non-mutating
(requires only `read` scope).

An omitted scope searches the same caller-bound `global` + `user` envelope as
the list endpoint. It never means every row in the table.

**Request body:**

```json
{
  "query": "authentication",
  "type": "general",
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
| `limit`    | int    | no       | `20`    | Max results (1-50)             |

**Response:** `200`

```json
{
  "memories": [
    {
      "memory_id": "a1b2c3d4-e5f6-...",
      "name": "auth_patterns",
      "description": "Authentication architecture",
      "type": "general",
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

Deletes are atomic: the row used for the success result and audit event is the
row actually removed. A storage failure returns `500`, not a false `404`.

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
      "type": "general",
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
  "type": "general",
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
        mem_type="general",
        scope="global",
    )
    print(mem.memory_id)

    # Search memories
    results = client.search_memories("authentication", mem_type="general", limit=10)
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
    result = admin.search_memories("architecture", mem_type="general")

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
  type: "general",
  scope: "global",
});

// Search memories
const results = await client.searchMemories({
  query: "authentication",
  type: "general",
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
