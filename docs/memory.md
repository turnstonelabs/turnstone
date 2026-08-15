# Structured Memory

> See also: [Memory Architecture diagram](diagrams/png/23-memory-architecture.png)

The structured memory system gives the AI persistent, typed, scoped memories
that survive across sessions and workstreams. The model receives a complete,
body-free metadata index at its first admitted turn, then uses explicit
`memory(action='get')` calls to read live bodies.

## Overview

Each memory has four index dimensions:

- **Type** -- categorizes the memory's purpose
- **Scope** -- controls visibility boundaries
- **Name** -- unique identifier within a scope (snake_case, normalized)
- **Description** -- a required authored retrieval hook (1-512 characters)

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

### Name normalization and legacy rows

Public name inputs may use supported Latin letters that fold to ASCII, ASCII
digits, Unicode space separators, Unicode hyphens, and single underscores.
Save, exact-name get, and exact-name delete normalize that input to lowercase
ASCII `snake_case`; the stored key must be at most 256 characters after
normalization. Other characters and leading, trailing, or repeated underscores
are rejected.

Legacy rows whose stored names do not satisfy the current rule remain visible
in list, search, and memory-index summaries; those response surfaces do not
pretend every historical name is canonical. Public exact-name operations still
enforce current normalization and do not perform compatibility lookup or
automatic rename. Admin ID-based routes can inspect the body, update the
description, or delete a legacy row, but they cannot rename or recreate it.
Before deleting, preserve the body and confirm an authorized surface can create
the replacement in the same scope: the public API supports `global`,
`workstream`, and `user`; `project` requires an attached writable session; and
`coordinator` requires the acting coordinator session. Deletion is irreversible.

### Inherited target and coordinator scope

Name-based operations use one inherited target when `scope` is omitted:

- Any stored project attachment pins `save`, `get`, and `delete` to `project`.
  If the project is missing or archived, or the acting principal's access has
  been revoked, the operation fails closed and never falls back to another
  namespace.
- Authorized read-only project access permits `get`, but `save` and `delete`
  fail. Authorized project reactivation restores eligibility.
- Only when no project attachment is stored do interactive sessions select
  `global` and coordinator sessions select `coordinator`.

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

### Immutable index and live pointers

At the first model turn admitted for an acting principal, the session resolves
that principal's live project access and captures every visible memory's
`scope`, `type`, `name`, and `description`. The rendered `<memory-index>` is
stored durably. It records the attached project ID and name only when that
project is active and readable by the first acting principal. A missing,
archived, or access-revoked attachment produces empty project witnesses and no
project rows. The stored attachment remains pinned for name-based operations,
which fail closed until access or project state permits them. Bodies never enter
the index.

The snapshot is bound to the current durable workstream row and addressed by
its workstream ID. The first model admission binds that workstream to the acting
principal's exact visibility envelope; every later turn reuses the same bytes
for prompt-cache stability and an honest shared conversation ledger. Saving,
editing, or deleting a memory therefore does not rewrite an existing snapshot:
new entries can be absent and deleted entries can remain listed. Project names
remain in the enclosing session context; the immutable index records the stable
project ID so a rename cannot rewrite historical prompt state.

After each genuine user turn, BM25 scores the complete live metadata set and
persists up to `relevance_k` exact `(scope, name)` pointers as a first-class
system turn. These pointers are also body-free. The model must use
`memory(action='get', name=..., scope=...)` to verify current access and read
content. This keeps later conversation history truthful without invalidating
the initial cached prefix.

Only an explicit full-body `get` updates `last_accessed` and `access_count`.
Index capture, pointers, `list`, `search`, saves, and deletes do not count as
accesses. The persona memory lever gates the index, pointers, tool, and
memory-directed nudges together. See [Personas](personas.md).

The default complete-index soft budget is 65,536 characters. It does not
truncate or hide entries. The registered
`memory.model_index_over_budget_notice` setting defaults to `false`. When an
operator opts in, over-budget prose is appended only to a successful model
`memory.save` tool result; REST and SDK save responses remain unchanged. The
console always shows persistent health derived from live memories and every
visibility envelope possible in the current workstream/project topology,
regardless of that setting. The calculation does not depend on an index
snapshot already existing. Legacy rows with invalid descriptions remain
represented by an explicit sentinel until an administrator authors a valid
hook.

Separately from that soft character budget, each memory-bearing model request
checks the complete final system prefix against the selected lane's usable
input capacity (context window minus the output reservation). This is a prefix
fit guard; tool definitions, conversation history, and the current user input
remain subject to the normal provider request checks. A refusal occurs before
provider dispatch and before attachment or perception work. Before first
capture, the candidate transaction does not commit a new snapshot, so the user
can consolidate or delete memories and retry, or select a larger-context model.
If another worker wins the first-capture race, its durable snapshot governs the
retry. Once a snapshot is bound, edits cannot shrink it for that workstream;
select a larger-context model, or start a new workstream after memory cleanup.

### Nudges

The metacognition layer can nudge the model to save memories at appropriate
moments (e.g., after a correction or when resuming a workstream). Nudges are
rate-limited by `nudge_cooldown`. Setting `memory.nudges=false` suppresses both
live memory pointers and memory-directed nudges; it does not remove the
immutable initial index or the memory tool.

---

## Configuration

Server and console sessions read memory settings live from the database-backed
ConfigStore. Configure them through the Admin Settings UI or settings API; a
`config.toml [memory]` section is not consumed by server or console, and the
standalone CLI currently uses `MemoryConfig` defaults.

| Setting | Default | Purpose |
|---------|---------|---------|
| `memory.relevance_k` | `5` | Maximum body-free metadata pointers persisted after a user turn |
| `memory.index_budget_chars` | `65536` | Complete-index soft character budget; never truncates |
| `memory.model_index_over_budget_notice` | `false` | Add over-budget prose to successful model-tool saves |
| `memory.max_content` | `32768` | Maximum memory body size in characters |
| `memory.nudge_cooldown` | `300` | Minimum seconds between memory-directed nudges |
| `memory.nudges` | `true` | Enable live pointers and memory-directed nudges; does not remove the index or tool |

---

## Tool Usage

The `memory` tool supports five actions:

### save

Store or update a memory.

Every save is a complete write for the index hook: `description` must be
supplied on both creation and update, normalize to one non-empty line, and be
at most 512 characters. Content-only updates are rejected.

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
| `name`        | yes      | --          | Canonical snake_case identifier (max 256 chars) |
| `content`     | yes      | --          | Memory content (max `max_content` chars) |
| `description` | yes      | --          | Authored index hook (1-512 normalized characters), required on every write |
| `type`        | no       | `"general"` | One of: user, general, feedback, reference |
| `scope`       | no       | inherited   | Kind-valid scope; see inherited target above |

### get

Retrieve the live full content of one memory by name. This is the only tool
action that records an access.

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

Find memories by name or authored description. Results contain metadata only;
follow a result with an exact `get` to read its body.

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

List all memories with optional filters. Results contain metadata only.

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

Five endpoints on the server for programmatic memory access. List and search
return metadata summaries; only the exact-name GET endpoint returns a body and
records an access.

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
      "created": "2026-03-10T10:00:00",
      "updated": "2026-03-12T14:30:00",
      "last_accessed": "",
      "access_count": 0
    }
  ],
  "total": 1
}
```

---

### `POST /v1/api/memories`

Save or upsert a structured memory.

`description` is mandatory for both creates and updates, normalizes to one
non-empty line, and is limited to 512 characters. The API rejects content-only
updates.

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
| `name`       | string | yes      | --          | Latin alias; stored snake_case key is max 256 chars after normalization |
| `content`    | string | yes      | --          | Memory content (max 65536 chars)     |
| `description`| string | yes      | --          | Authored one-line index hook (1-512 characters), required on every write |
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
  "created": "2026-03-14T10:00:00",
  "updated": "2026-03-14T10:00:00",
  "last_accessed": "",
  "access_count": 0
}
```

The save response is a metadata summary; fetch the exact name with `GET` when
the body is needed.

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
      "created": "2026-03-10T10:00:00",
      "updated": "2026-03-12T14:30:00",
      "last_accessed": "",
      "access_count": 0
    }
  ],
  "total": 1
}
```

---

### `GET /v1/api/memories/{name}`

Fetch one live memory body by exact name and scope. This is the only public
memory read that updates `last_accessed` and `access_count`; list and search do
not.

**Path parameters:**

| Parameter | Type   | Description |
|-----------|--------|-------------|
| `name`    | string | Raw Latin alias normalized to the exact stored name |

**Query parameters:**

| Parameter  | Type   | Required | Default    | Description         |
|------------|--------|----------|------------|---------------------|
| `scope`    | string | no       | `"global"` | Scope of the memory |
| `scope_id` | string | no       | `""`       | Scope qualifier     |

**Response (success):** `200`

```json
{
  "memory_id": "a1b2c3d4-e5f6-...",
  "name": "auth_patterns",
  "description": "Authentication architecture",
  "type": "general",
  "scope": "global",
  "scope_id": "",
  "content": "JWT tokens with HS256...",
  "created": "2026-03-10T10:00:00",
  "updated": "2026-03-12T14:30:00",
  "last_accessed": "2026-03-12T14:31:00",
  "access_count": 1
}
```

**Response (not found):** `404`

```json
{"error": "Memory 'auth_patterns' not found"}
```

---

### `DELETE /v1/api/memories/{name}`

Delete a memory by name and scope.

Deletes are atomic: the row used for the success result and audit event is the
row actually removed. A storage failure returns `500`, not a false `404`.

**Path parameters:**

| Parameter | Type   | Description          |
|-----------|--------|----------------------|
| `name`    | string | Raw Latin alias normalized to the exact stored name |

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

Six admin endpoints provide cross-workstream memory management and index
health. All require the `admin.memories` permission.

### `GET /v1/api/admin/memories`

List memories across all scopes (no automatic scope resolution).

**Query parameters:**

| Parameter  | Type   | Required | Default | Description                  |
|------------|--------|----------|---------|------------------------------|
| `type`     | string | no       | `""`    | Filter by type               |
| `scope`    | string | no       | `""`    | Filter by scope              |
| `scope_id` | string | no       | `""`    | Filter by scope ID           |
| `limit`    | int    | no       | `100`   | Max results (capped at 200)  |

**Response:** `200`. Each item is an `AdminMemorySummary`: the public
body-free metadata plus `scope_label`, a human-readable label for `scope_id`
(empty when there is no scope ID, with the raw ID as fallback).

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
      "scope_label": "",
      "created": "2026-03-10T10:00:00",
      "updated": "2026-03-12T14:30:00",
      "last_accessed": "",
      "access_count": 0
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

**Response:** `200` -- the same `AdminMemorySummary` schema as
`GET /v1/api/admin/memories`.

---

### `GET /v1/api/admin/memories/{memory_id}`

Get a single memory body by ID and record an access.

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
  "scope_label": "",
  "content": "The project uses...",
  "created": "2026-03-10T10:00:00",
  "updated": "2026-03-12T14:30:00",
  "last_accessed": "2026-03-12T14:31:00",
  "access_count": 1
}
```

The response is `AdminMemoryInfo` (`AdminMemorySummary` plus `content`), and
the counters include the completed GET touch. Storage-operation failures return
`500`; unavailable storage returns `503`.

**Response (not found):** `404`

```json
{"error": "Memory not found"}
```

---

### `PATCH /v1/api/admin/memories/{memory_id}`

Replace a memory's authored index hook without changing its body. The
description normalizes to one non-empty line and is limited to 512 characters.
Existing immutable snapshots remain unchanged; future visibility envelopes
capture the edited hook. The operation records a
`memory.description_update` audit event.

```json
{"description": "Updated retrieval hook"}
```

The response is the updated `AdminMemorySummary` and never includes the body:

```json
{
  "memory_id": "a1b2c3d4-e5f6-...",
  "name": "project_architecture",
  "description": "Updated retrieval hook",
  "type": "general",
  "scope": "global",
  "scope_id": "",
  "scope_label": "",
  "created": "2026-03-10T10:00:00",
  "updated": "2026-03-14T09:00:00",
  "last_accessed": "",
  "access_count": 0
}
```

Invalid descriptions return `400`, missing memories return `404`, storage
operation failures return `500`, and unavailable storage returns `503`.

---

### `GET /v1/api/admin/memories/index-health`

Return persistent, derived health for every visibility envelope possible in the
live workstream/project topology. The endpoint does not rely on existing
snapshot rows: it compares the complete live index for each possible envelope
against `memory.index_budget_chars` and reports legacy descriptions that need
editing.

```json
{
  "budget_chars": 65536,
  "over_budget": false,
  "max_char_count": 18420,
  "max_entry_count": 210,
  "over_by_chars": 0,
  "invalid_description_count": 0,
  "envelope_count": 3
}
```

The console governance page displays the over-budget or invalid-description
state as a persistent banner rather than a transient notification.
Calculation failures return `500`; unavailable storage returns `503`.

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
builtin. Omit it to preserve an existing memory's type on update and use the
server default on insert; pass `mem_type="general"` explicitly when an update
should reclassify the memory to `general`.

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

    # Fetch one live body (and record the access)
    body = client.get_memory("api_conventions", scope="global")

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

    # Repair an index hook and inspect persistent index health
    admin.update_memory_description("a1b2c3d4-e5f6-...", "API conventions and endpoint structure")
    health = admin.memory_index_health()

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

// Fetch one live body (and record the access)
const body = await client.getMemory("api_conventions", { scope: "global" });

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
await admin.updateMemoryDescription(
  "a1b2c3d4-e5f6-...",
  "API conventions and endpoint structure",
);
const health = await admin.memoryIndexHealth();
await admin.deleteMemory("a1b2c3d4-e5f6-...");
```

---

## Storage

Memories are stored in the `structured_memories` table (migration 013). The
unique constraint on `(name, scope, scope_id)` ensures upsert semantics. The
name is normalized on save: lowercased, hyphens and spaces replaced with
underscores.

Immutable rendered indexes are stored in `memory_index_snapshots` (migration
072), keyed by workstream ID. The first admitted acting principal and exact
visibility-envelope JSON are stored as witnesses, along with `project_id` and
entry/character/invalid-description counts. Workstream hard deletion removes
the snapshot and workstream-scoped memories in the same transaction before the
ID becomes reusable. Registration also clears stale rows on a successful new
insert so a later same-ID workstream cannot inherit memory state.

Migration 072 is an irreversible access-history semantics boundary. It resets
legacy `structured_memories.last_accessed` and `access_count` values because
those fields change from recording automatic prompt injection to recording
explicit full-body `get` operations only. Values from before and after the
upgrade are not comparable, and downgrading cannot reconstruct the cleared
history. Export the memory metadata before upgrading if that history must be
retained externally.

The same migration removes every global and node-specific
`memory.fetch_limit` setting. It deliberately does not map that retired token
fetch cap to another setting: `memory.relevance_k` limits body-free live
pointers, while `memory.index_budget_chars` is a character-based soft budget
for the complete metadata index.

## Architecture

See [Memory Architecture diagram](diagrams/png/23-memory-architecture.png) for
the full data flow covering the session tool path, API path, admin path, and
immutable index plus live metadata-pointer flow.
