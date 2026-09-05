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
| | `create_workstream(*, name, model, auto_approve, resume_ws, skill, persona, initial_message, project_id, attachments, ...)` | `CreateWorkstreamResponse` |
| | `close_workstream(ws_id)` | `StatusResponse` |
| **Attachments** | `upload_attachment(ws_id, filename, data, *, mime_type=...)` | `UploadAttachmentResponse` |
| | `list_attachments(ws_id)` | `ListAttachmentsResponse` |
| | `get_attachment_content(ws_id, attachment_id)` | `bytes` |
| | `delete_attachment(ws_id, attachment_id)` | `StatusResponse` |
| **Chat** | `send(message, ws_id, *, attachment_ids=None, client_send_id=None)` | `SendResponse` |
| | `approve(*, ws_id, approved, feedback, always, cycle_id, call_id)` | `ApproveResponse` |
| | `command(*, ws_id, command)` | `StatusResponse` |
| | `cancel(ws_id, *, force=False)` | `CancelResponse` |
| **History** | `get_history(ws_id, *, limit=100)` | `WorkstreamHistoryResponse` |
| **Streaming** | `stream_events(ws_id, *, last_event_id=None, history_token=None)` | `Iterator[ServerEvent]` |
| | `stream_global_events()` | `Iterator[ServerEvent]` |
| **High-level** | `send_and_wait(message, ws_id, *, timeout, on_event)` | `TurnResult` |
| **Saved** | `list_saved_workstreams()` | `ListSavedWorkstreamsResponse` |
| **Auth** | `login(username=..., password=...)` | `AuthLoginResponse` |
| | `login(token="ts_xxx")` | `AuthLoginResponse` |
| | `logout()` | `StatusResponse` |
| | `auth_status()` | `AuthStatusResponse` |
| **Health** | `health()` | `HealthResponse` |

### Console Client API

Use `required_node_id="host-1"` with direct or routed creation to preserve an
execution requirement across close/reopen and restart. Console `node_id` and
routed `target_node` selections express the same requirement; automatic
placement leaves fresh conversations flexible. A fork (`resume_ws`) inherits
the source requirement unless an explicit destination is supplied. For example:

```python
copy = console.route_create_workstream(
    resume_ws=saved_ws_id,
    resume_ws_exact=True,
    target_node="node-1",
)
```

This creates a new conversation and leaves the original saved. It does not
move a running session or transfer node-local files.

Both `TurnstoneConsole` (sync) and `AsyncTurnstoneConsole` (async) expose:

| Category | Method | Returns |
|----------|--------|---------|
| **Cluster** | `overview()` | `ClusterOverviewResponse` |
| | `nodes(*, sort, limit, offset)` | `ClusterNodesResponse` |
| | `workstreams(*, state, node, search, sort, page, per_page)` | `ClusterWorkstreamsResponse` |
| | `node_detail(node_id)` | `NodeDetailResponse` |
| | `snapshot()` | `ClusterSnapshotResponse` |
| | `create_workstream(*, node_id, name, model, initial_message, skill, persona, resume_ws)` | `ConsoleCreateWsResponse` |
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
| `user_turn` | `UserTurnEvent` | `ws_id`, `content`, `attachments`, `sender`, `source`, `client_send_ids`, `_event_id` |
| `content` | `ContentEvent` | `text` |
| `reasoning` | `ReasoningEvent` | `text` |
| `tool_info` | `ToolInfoEvent` | `items` |
| `approve_request` | `ApproveRequestEvent` | `cycle_id`, `items` |
| `tool_result` | `ToolResultEvent` | `call_id`, `name`, `output`, `is_error`, `preview`, `accepted`, `effect_status`, `_event_id` |
| `tool_output_chunk` | `ToolOutputChunkEvent` | `call_id`, `chunk` |
| `status` | `StatusEvent` | `prompt_tokens`, `total_tokens`, `pct`, `effort`, `cache_creation_tokens`, `cache_read_tokens` |
| `error` | `ErrorEvent` | `message` |
| `info` | `InfoEvent` | `message` |
| `stream_end` | `StreamEndEvent` | — |
| `state_change` | `StateChangeEvent` | `state` ∈ `running`/`thinking`/`attention`/`idle`/`error` |
| `in_progress_snapshot` | `InProgressSnapshotEvent` | `content`, `reasoning` (one-shot mid-stream refresh resume) |
| `agent_context` | `AgentContextEvent` | `parent_call_id`, `prompt_tokens`, `context_window` (replace the latest reading for a running task agent; discard it on the matching `tool_result`) |
| `compaction` | `CompactionEvent` | `target`, `parent_call_id`, `compaction_id`, `phase`, `ok`, `notice`; events without `target` are workstream events, while `task_agent` is transient nested progress and omits its private `summary` |
| `approval_resolved` | `ApprovalResolvedEvent` | `cycle_id`, `call_ids`, `approved`, `feedback`, `always` |
| `cancelled` | `CancelledEvent` | — |
| `history_resync` | `HistoryResyncEvent` | `reason`, optional `ws_id` |

The Python server `send()` and console `coordinator_send()` methods accept an
optional `client_send_id`; TypeScript `send()` accepts the equivalent
`options.clientSendId`. Values match `[A-Za-z0-9_-]{1,128}`. The value is an
opaque optimistic-UI correlation token, not an idempotency key: reusing it
still creates distinct accepted turns and events.
Every upgraded listener on the shared workstream receives `UserTurnEvent`.
Originating panes use `client_send_ids` only to settle the exact optimistic
bubble, while peers render the accepted row once by `_event_id`. A
`message_queued` event carrying the token can establish acceptance even if the
POST acknowledgement is lost. History projects the same correlation alongside
the accepted user row. These tokens are not credentials: when sender and viewer
identities are both known, only a matching sender may settle local optimistic
state; a peer event still renders its canonical row.

The typed projection is negotiated with `?user_turn=1` on the per-workstream
SSE URL. Python `stream_events()` / `send_and_wait()` and TypeScript
`streamEvents()` / `sendAndWait()` set it automatically. Raw consumers that
omit it receive a backward-compatible `replay_truncated` repair signal instead
of the user row and must rebuild from `/history`; its pre-row cursor keeps the
repair retryable if that history request fails.

The browser-only final-tool upsert capability is `?tool_turn=1`. The bundled
Python and TypeScript SDK streaming helpers and channel adapters intentionally
do not negotiate it yet: they retain the executor-receipt `tool_result`
contract and do not own a transcript reducer. `ToolResultEvent` can deserialize
the accepted fields for direct/custom capable clients. Raw capable clients must
deduplicate `_event_id` and replace the newest matching call occurrence; raw
incapable clients receive the pre-row `tool_turn_projection_unsupported` repair
frame and rebuild from history. That staging deliberately prices in two costs
for incapable consumers. A raw client that treats every `replay_truncated`
frame as a rebuild trigger refetches `/history` once per accepted tool row —
one fetch per tool call on a long agentic turn; a client that wants tool
results incrementally should negotiate `tool_turn=1` and reduce, and the
bundled helpers (which ignore the frame rather than rebuild) stay correct
because their receipt-only view never depends on the accepted projection.
Second, only the accepted event carries post-execution output transforms, so a
receipt-rendering consumer (for example, a channel adapter posting the
executor receipt into a thread) keeps the pre-transform text; the accepted
projection is a transcript-consistency mechanism, not a wire confidentiality
boundary — see the API reference note on the preliminary `tool_result`.

Current servers bootstrap conversation history through
`GET /v1/api/workstreams/{ws_id}/history` before the SSE stream; they do not
emit a `history` event. `HistoryEvent` remains deserializable only for
compatibility with older servers. `get_history()` exposes the current REST
bootstrap response, including its optional cursor and one-shot handoff token.

### Caller-managed history handoff

The SDK supplies typed handshake primitives but intentionally does not own a
transcript renderer or reconnect policy. After rendering a successful history
response, pass its cursor and token to exactly one initial stream:

```python
from turnstone.sdk import HistoryResyncEvent

history = client.get_history(ws_id)
render(history.messages)

for event in client.stream_events(
    ws_id,
    last_event_id=history.cursor,
    history_token=history.handoff_token,
):
    if isinstance(event, HistoryResyncEvent):
        # Stop this stream. The caller chooses when to fetch, render, and
        # reconnect with a new history response.
        break
    apply_live_event(event)
```

`history_resync` means numeric replay cannot prove that the rendered limited
tail came from the same total accepted conversation-row prefix. Stop the
stream, fetch and render history again, and use only the new cursor/token pair.
A 503 history response raises `TurnstoneAPIError`; it is not authoritative, so
retain any existing transcript and do not open a tokenless replacement stream.

**Global events** (from `stream_global_events()`):

| Type | Class | Key Fields |
|------|-------|------------|
| `ws_state` | `WsStateEvent` | `ws_id`, `state`, `tokens`, `activity`, `persistence_state` |
| `ws_activity` | `WsActivityEvent` | `ws_id`, `activity`, `activity_state` |
| `ws_rename` | `WsRenameEvent` | `ws_id`, `name` |
| `ws_closed` | `WsClosedEvent` | `ws_id` |

**Cluster events** (from `stream_cluster_events()`):

| Type | Class | Key Fields |
|------|-------|------------|
| `node_joined` | `NodeJoinedEvent` | `node_id` |
| `node_lost` | `NodeLostEvent` | `node_id` |
| `cluster_state` | `ClusterStateEvent` | `ws_id`, `node_id`, `state`, `tokens`, `persistence_state` |
| `ws_created` | `ClusterWsCreatedEvent` | `ws_id`, `node_id`, `name`, `persistence_state` |
| `ws_closed` | `ClusterWsClosedEvent` | `ws_id` |
| `ws_rename` | `ClusterWsRenameEvent` | `ws_id`, `name` |
| `snapshot` | `ClusterSnapshotEvent` | `nodes`, `overview`, `timestamp` |

Operator-facing workstream rows and rich state events expose only the sanitized
`persistence_state`: `healthy`, `pending`, `retrying`, or `conflict`. SDK types
treat it as optional for compatibility with older nodes; an omitted value means
`healthy`. Retry counts, storage errors, commit keys, and conversation content
are never part of this status surface.

### TurnResult

The `send_and_wait()` method returns a `TurnResult` that aggregates the full response:

```python
result = client.send_and_wait("Hello", ws_id, timeout=60)
result.content  # Full text response
result.reasoning  # Chain-of-thought (if shown)
result.tool_results  # List of (tool_name, output) tuples
result.errors  # Any error messages
result.ok  # True if no errors and not timed out
result.timed_out  # True if timeout expired
```

### Attachments

Upload files to a workstream and attach them to the next user turn:

```python
# Upload separately, then send a message — attachments auto-attach
with open("screenshot.png", "rb") as f:
    att = client.upload_attachment(ws.ws_id, "screenshot.png", f.read(), mime_type="image/png")
client.send("What's wrong in this screenshot?", ws.ws_id)

# Or attach at workstream-creation time (multipart upload)
from turnstone.sdk import AttachmentUpload

with open("notes.txt", "rb") as f:
    ws = client.create_workstream(
        name="triage",
        initial_message="Summarize the notes",
        attachments=[AttachmentUpload(data=f.read(), filename="notes.txt", mime_type="text/plain")],
    )
```

Limits: images ≤ 4 MiB (png/jpeg/gif/webp), text ≤ 512 KiB (UTF-8),
10 pending per (workstream, user). The SDK auto-generates `ws_id` on the
client so cluster-routed callers bind attachments to the owning node
before the request lands.

### Forking a workstream

`resume_ws` is the API's compatibility name for an atomic fork. It creates a
new workstream ID while the source remains unchanged:

```python
fork = client.create_workstream(
    resume_ws=ws.ws_id,
    name="analysis-branch",
    initial_message="Try the alternative plan.",
)
assert fork.resumed
```

The server transaction clones the source's checkpoint-bounded history, saved
session configuration, persona, project, and attachment references. Do not
combine `resume_ws` with `attachments`; fork first, then upload to the new ID.
To rehydrate the original ID rather than branch it, call the server's
`POST /v1/api/workstreams/{ws_id}/open` endpoint.

### Error Handling

Non-2xx responses raise `TurnstoneAPIError`:

```python
from turnstone.sdk import TurnstoneServer, TurnstoneAPIError

try:
    client.send("hi", "bad_ws_id")
except TurnstoneAPIError as e:
    print(e.status_code)  # 404
    print(e.message)  # "Unknown workstream"
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

// Render history, then use its one-shot hints on the initial stream.
const history = await client.getHistory(ws.ws_id);
render(history.messages);
for await (const event of client.streamEvents(ws.ws_id, {
  lastEventId: history.cursor ?? undefined,
  historyToken: history.handoff_token ?? undefined,
})) {
  if (event.type === "history_resync") break; // caller refetches and reconnects
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
  events.py                  Typed SSE event dataclasses with type registry
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
