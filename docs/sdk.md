# Turnstone Client SDK

> See also: [API Reference](api-reference.md) | [Architecture](architecture.md) | [SDK Class Diagram](diagrams/png/13-sdk-architecture.png)

Typed HTTP client libraries for programmatic access to the turnstone server and console APIs. Available in Python (sync + async) and TypeScript.

---

## Python SDK

The Python SDK is included in the `turnstone` package — no extra install required. It wraps the REST and SSE endpoints with typed methods that return Pydantic models directly.

### Quick Start

```python
from turnstone.sdk import TurnstoneServer

# Synchronous client — login with username/password
with TurnstoneServer("http://localhost:8080") as client:
    client.login(username="alice", password="s3cret")

    # Create a workstream
    ws = client.create_workstream(name="Analysis")

    # Send a message and wait for the full response
    result = client.send_and_wait("Summarize this codebase.", ws.ws_id)
    print(result.content)

    # Stream events in real time
    for event in client.stream_events(ws.ws_id):
        if event.type == "content":
            print(event.text, end="", flush=True)

    # Close when done
    client.close_workstream(ws.ws_id)
```

Alternatively, authenticate with an API token:

```python
with TurnstoneServer("http://localhost:8080") as client:
    client.login(token="ts_abc123...")
    ws = client.create_workstream(name="CI run")
    result = client.send_and_wait("Run the test suite.", ws.ws_id)
```

### Async Client

```python
import asyncio
from turnstone.sdk import AsyncTurnstoneServer

async def main():
    async with AsyncTurnstoneServer("http://localhost:8080") as client:
        await client.login(username="alice", password="s3cret")
        ws = await client.create_workstream(name="demo")
        async for event in client.stream_events(ws.ws_id):
            if event.type == "content":
                print(event.text, end="", flush=True)

asyncio.run(main())
```

### Server Client API

Both `TurnstoneServer` (sync) and `AsyncTurnstoneServer` (async) expose:

| Category | Method | Returns |
|----------|--------|---------|
| **Workstreams** | `list_workstreams()` | `ListWorkstreamsResponse` |
| | `dashboard()` | `DashboardResponse` |
| | `create_workstream(*, name, model, auto_approve, skill, initial_message, attachments)` | `CreateWorkstreamResponse` |
| | `close_workstream(ws_id)` | `StatusResponse` |
| **Attachments** | `upload_attachment(ws_id, filename, data, *, mime_type=...)` | `UploadAttachmentResponse` |
| | `list_attachments(ws_id)` | `ListAttachmentsResponse` |
| | `get_attachment_content(ws_id, attachment_id)` | `bytes` |
| | `delete_attachment(ws_id, attachment_id)` | `StatusResponse` |
| **Chat** | `send(message, ws_id)` | `SendResponse` |
| | `approve(*, ws_id, approved, feedback, always)` | `StatusResponse` |
| | `plan_feedback(*, ws_id, feedback)` | `StatusResponse` |
| | `command(*, ws_id, command)` | `StatusResponse` |
| | `cancel(ws_id, *, force=False)` | `StatusResponse` |
| **Streaming** | `stream_events(ws_id)` | `Iterator[ServerEvent]` |
| | `stream_global_events()` | `Iterator[ServerEvent]` |
| **High-level** | `send_and_wait(message, ws_id, *, timeout, on_event)` | `TurnResult` |
| **Saved** | `list_saved_workstreams()` | `ListSavedWorkstreamsResponse` |
| **Auth** | `login(username=..., password=...)` | `AuthLoginResponse` |
| | `login(token="ts_xxx")` | `AuthLoginResponse` |
| | `logout()` | `StatusResponse` |
| | `auth_status()` | `AuthStatusResponse` |
| **Health** | `health()` | `HealthResponse` |

### Console Client API

Both `TurnstoneConsole` (sync) and `AsyncTurnstoneConsole` (async) expose:

| Category | Method | Returns |
|----------|--------|---------|
| **Cluster** | `overview()` | `ClusterOverviewResponse` |
| | `nodes(*, sort, limit, offset)` | `ClusterNodesResponse` |
| | `workstreams(*, state, node, search, sort, page, per_page)` | `ClusterWorkstreamsResponse` |
| | `node_detail(node_id)` | `NodeDetailResponse` |
| | `snapshot()` | `ClusterSnapshotResponse` |
| | `create_workstream(*, node_id, name, model, initial_message, skill)` | `ConsoleCreateWsResponse` |
| **Schedules** | `list_schedules()` | `ListSchedulesResponse` |
| | `create_schedule(*, name, schedule_type, initial_message, ...)` | `ScheduleInfo` |
| | `get_schedule(task_id)` | `ScheduleInfo` |
| | `update_schedule(task_id, *, name=..., enabled=..., ...)` | `ScheduleInfo` |
| | `delete_schedule(task_id)` | `StatusResponse` |
| | `list_schedule_runs(task_id, *, limit=50)` | `ListScheduleRunsResponse` |
| **MCP Registry** | `search_mcp_registry(q="", *, limit=20, cursor=None)` | `RegistrySearchResponse` |
| | `install_from_registry(registry_name, source, *, index=0, name="", variables=None, env=None, headers=None)` | `McpServerDetail` |
| **Skill Discovery** | `discover_skills(q="", *, limit=20)` | `SkillDiscoverResponse` |
| | `install_skill(source, *, skill_id="", url="")` | `dict` |
| **Streaming** | `stream_cluster_events()` | `Iterator[ClusterEvent]` |
| **Auth** | `login(username=..., password=...)` / `login(token="ts_xxx")` | `AuthLoginResponse` |
| | `logout()` | `StatusResponse` |
| **Health** | `health()` | `ConsoleHealthResponse` |

### Event Types

SSE events are deserialized into typed dataclasses. Use `event.type` to discriminate.

**Per-workstream events** (from `stream_events(ws_id)`):

| Type | Class | Key Fields |
|------|-------|------------|
| `connected` | `ConnectedEvent` | `model`, `model_alias`, `skip_permissions` |
| `history` | `HistoryEvent` | `messages` |
| `content` | `ContentEvent` | `text` |
| `reasoning` | `ReasoningEvent` | `text` |
| `tool_info` | `ToolInfoEvent` | `items` |
| `approve_request` | `ApproveRequestEvent` | `items` |
| `tool_result` | `ToolResultEvent` | `call_id`, `name`, `output`, `is_error` |
| `tool_output_chunk` | `ToolOutputChunkEvent` | `call_id`, `chunk` |
| `status` | `StatusEvent` | `prompt_tokens`, `total_tokens`, `pct`, `effort`, `cache_creation_tokens`, `cache_read_tokens` |
| `plan_review` | `PlanReviewEvent` | `content` |
| `error` | `ErrorEvent` | `message` |
| `info` | `InfoEvent` | `message` |
| `stream_end` | `StreamEndEvent` | — |
| `cancelled` | `CancelledEvent` | — |

**Global events** (from `stream_global_events()`):

| Type | Class | Key Fields |
|------|-------|------------|
| `ws_state` | `WsStateEvent` | `ws_id`, `state`, `tokens`, `activity` |
| `ws_activity` | `WsActivityEvent` | `ws_id`, `activity`, `activity_state` |
| `ws_rename` | `WsRenameEvent` | `ws_id`, `name` |
| `ws_closed` | `WsClosedEvent` | `ws_id` |

**Cluster events** (from `stream_cluster_events()`):

| Type | Class | Key Fields |
|------|-------|------------|
| `node_joined` | `NodeJoinedEvent` | `node_id` |
| `node_lost` | `NodeLostEvent` | `node_id` |
| `cluster_state` | `ClusterStateEvent` | `ws_id`, `node_id`, `state`, `tokens` |
| `ws_created` | `ClusterWsCreatedEvent` | `ws_id`, `node_id`, `name` |
| `ws_closed` | `ClusterWsClosedEvent` | `ws_id` |
| `ws_rename` | `ClusterWsRenameEvent` | `ws_id`, `name` |
| `snapshot` | `ClusterSnapshotEvent` | `nodes`, `overview`, `timestamp` |

### TurnResult

The `send_and_wait()` method returns a `TurnResult` that aggregates the full response:

```python
result = client.send_and_wait("Hello", ws_id, timeout=60)
result.content      # Full text response
result.reasoning    # Chain-of-thought (if shown)
result.tool_results # List of (tool_name, output) tuples
result.errors       # Any error messages
result.ok           # True if no errors and not timed out
result.timed_out    # True if timeout expired
```

### Attachments

Upload files to a workstream and attach them to the next user turn:

```python
# Upload separately, then send a message — attachments auto-attach
with open("screenshot.png", "rb") as f:
    att = client.upload_attachment(ws.ws_id, "screenshot.png",
                                   f.read(),
                                   mime_type="image/png")
client.send("What's wrong in this screenshot?", ws.ws_id)

# Or attach at workstream-creation time (multipart upload)
from turnstone.sdk import AttachmentUpload

with open("notes.txt", "rb") as f:
    ws = client.create_workstream(
        name="triage",
        initial_message="Summarize the notes",
        attachments=[AttachmentUpload(data=f.read(),
                                      filename="notes.txt",
                                      mime_type="text/plain")],
    )
```

Limits: images ≤ 4 MiB (png/jpeg/gif/webp), text ≤ 512 KiB (UTF-8),
10 pending per (workstream, user). The SDK auto-generates `ws_id` on the
client so cluster-routed callers bind attachments to the owning node
before the request lands.

### Error Handling

Non-2xx responses raise `TurnstoneAPIError`:

```python
from turnstone.sdk import TurnstoneServer, TurnstoneAPIError

try:
    client.send("hi", "bad_ws_id")
except TurnstoneAPIError as e:
    print(e.status_code)  # 404
    print(e.message)      # "Unknown workstream"
```

---

## TypeScript SDK

Located at `sdk/typescript/`. Zero runtime dependencies for browsers; uses native `fetch` and `ReadableStream` for SSE parsing.

### Quick Start

```typescript
import { TurnstoneServer } from "@turnstone/sdk";

const client = new TurnstoneServer({ baseUrl: "http://localhost:8080" });

// Login with username/password or API token
await client.login({ username: "alice", password: "s3cret" });
// or: await client.login({ token: "ts_abc123..." });

// Create workstream and send message
const ws = await client.createWorkstream({ name: "demo" });
const result = await client.sendAndWait("Hello!", ws.ws_id);
console.log(result.content);

// Stream events
for await (const event of client.streamEvents(ws.ws_id)) {
  if (event.type === "content") {
    process.stdout.write(event.text);
  }
}
```

### Console Client

```typescript
import { TurnstoneConsole } from "@turnstone/sdk";

const client = new TurnstoneConsole({ baseUrl: "http://localhost:8090" });
await client.login({ username: "alice", password: "s3cret" });

const overview = await client.overview();
console.log(`Nodes: ${overview.nodes}, Workstreams: ${overview.workstreams}`);

// Search and install from the MCP Registry
const results = await client.searchMcpRegistry({ q: "github", limit: 10 });
const server = await client.installFromRegistry({
  registry_name: results.servers[0].name,
  source: "remote",
});

// Search and install skills from external registries
const skills = await client.discoverSkills({ q: "code review" });
const skill = await client.installSkill({
  source: "github",
  url: "https://github.com/owner/skill-repo",
});

// Stream cluster events
for await (const event of client.clusterEvents()) {
  console.log(event.type, event);
}
```

### Type Safety

All event types are modeled as a discriminated union:

```typescript
import { isContentEvent, isErrorEvent } from "@turnstone/sdk";
import type { ServerEvent } from "@turnstone/sdk";

function handleEvent(event: ServerEvent) {
  if (isContentEvent(event)) {
    // event is narrowed to ContentEvent
    console.log(event.text);
  } else if (isErrorEvent(event)) {
    console.error(event.message);
  }
}
```

### Custom Fetch

The client accepts a custom `fetch` implementation for testing or Node.js environments:

```typescript
const client = new TurnstoneServer({
  baseUrl: "http://localhost:8080",
  fetch: myCustomFetch,
});
```

---

## Architecture

```
turnstone/sdk/               Python SDK (sub-package)
  _base.py                   Shared httpx async client, auth, error handling
  _sync.py                   Background event loop for sync wrappers
  _types.py                  TurnResult + TurnstoneAPIError
  events.py                  38 SSE event dataclasses with type registry
  server.py                  AsyncTurnstoneServer + TurnstoneServer
  console.py                 AsyncTurnstoneConsole + TurnstoneConsole

sdk/typescript/              TypeScript SDK (npm package)
  src/base.ts                fetch wrapper, auth, SSE streaming
  src/server.ts              TurnstoneServer class
  src/console.ts             TurnstoneConsole class
  src/events.ts              Discriminated union events + type guards
  src/sse.ts                 ReadableStream SSE parser
  src/types.ts               Request/response interfaces
```

The Python SDK reuses Pydantic models from `turnstone/api/` directly — no schema duplication. The TypeScript SDK has hand-written interfaces matching those models.

Both SDKs follow the same design: typed methods for REST endpoints, async iterators for SSE streams, and a high-level `send_and_wait` method for simple request-response patterns.

---

## Authentication

When auth is enabled on the server, the SDK handles JWT-based authentication automatically.

### Login Flow

There are two ways to authenticate:

1. **Username + password** — calls `POST /v1/api/auth/login` with credentials. The server validates against the user database and returns a JWT.

2. **API token** — calls `POST /v1/api/auth/login` with a `ts_`-prefixed token string. The server looks up the token, resolves the associated user, and returns a JWT.

In both cases the server returns the JWT in the response body and as a `Set-Cookie` header. The SDK extracts the JWT and includes it as a `Bearer` token in the `Authorization` header on all subsequent requests.

```python
# Username + password
client.login(username="alice", password="s3cret")

# API token (created via admin API or turnstone-admin CLI)
client.login(token="ts_abc123...")
```

### Token Lifecycle

- JWTs have a configurable expiry (default: 24 hours).
- `client.auth_status()` returns the current user identity and scopes without refreshing the token.
- `client.logout()` clears the stored JWT from the client.
- If a request returns 401, the SDK raises `TurnstoneAPIError` — the caller is responsible for re-authenticating.

### Token Types

The SDK accepts any Bearer token — JWTs (from `ServiceTokenManager` or login) and API tokens (`ts_` prefix) are both supported. Use `token_factory` for auto-rotating JWTs or a static `token` for API tokens.
