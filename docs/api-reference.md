# turnstone Web Server API Reference

## Overview

> See also: [MQ Protocol diagram](diagrams/png/06-mq-protocol.png) | [Message Routing diagram](diagrams/png/07-message-routing.png) | [Redis Key Schema diagram](diagrams/png/08-redis-key-schema.png)

`turnstone-server` exposes a browser-based chat UI backed by a
**Starlette** ASGI application served by **uvicorn**. The server uses
**Server-Sent Events (SSE)** via `sse-starlette` for real-time streaming
and **HTTP POST** for user actions.

All API responses use `Content-Type: application/json` unless otherwise noted.
CORS headers (`Access-Control-Allow-Origin: *`) are included on every response.

The server supports multiple concurrent **workstreams** (tabs), each backed by
an independent `ChatSession` and event queue.

---

## API Versioning

All API endpoints use the `/v1/` prefix. Non-API endpoints (`/`, `/health`, `/metrics`, `/openapi.json`, `/docs`, `/static/*`, `/shared/*`) are unversioned.

### Interactive Documentation

- **OpenAPI spec**: `GET /openapi.json` — machine-readable OpenAPI 3.1 schema
- **Swagger UI**: `GET /docs` — interactive API explorer (loads from CDN)

### Client SDKs

Typed client libraries for programmatic access to both the server and console APIs.

**Python** (included in the `turnstone` package):

```python
from turnstone.sdk import TurnstoneServer

with TurnstoneServer("http://localhost:8080", token="tok_xxx") as client:
    ws = client.create_workstream(name="demo")
    result = client.send_and_wait("Hello!", ws.ws_id)
    print(result.content)
```

Async variant: `AsyncTurnstoneServer` / `AsyncTurnstoneConsole`.

**TypeScript** (`sdk/typescript/`):

```typescript
import { TurnstoneServer } from "@turnstone/sdk";

const client = new TurnstoneServer({ baseUrl: "http://localhost:8080", token: "tok_xxx" });
const ws = await client.createWorkstream({ name: "demo" });
const result = await client.sendAndWait("Hello!", ws.ws_id);
console.log(result.content);
```

---

## Authentication

When auth is enabled (`[auth].enabled = true` or `TURNSTONE_AUTH_ENABLED=1`), all API endpoints except public paths require a valid token.

### Sending Credentials

Include a token in one of two ways:

- **Bearer header**: `Authorization: Bearer <token>`
- **Cookie**: `turnstone_auth=<token>` (set automatically by the login endpoint)

The server accepts three token types:

| Type | Format | Example |
|------|--------|---------|
| JWT | Base64 segments separated by dots | `eyJhbG...` |
| API token | `ts_` prefix + 64 hex chars | `ts_a1b2c3d4...` |
| Config token | Arbitrary string from `config.toml` | `my-secret-token` |

JWTs are the recommended credential for browser sessions. API tokens are suitable for programmatic access and CI/CD. Config tokens are a simple option for single-node deployments.

### `POST /v1/api/auth/login`

Authenticate with credentials and receive a JWT. Accepts two credential formats:

**Username + password:**

```json
{"username": "alice", "password": "hunter2"}
```

**API token:**

```json
{"token": "ts_a1b2c3d4e5f6..."}
```

**Response (success):** `200`

```json
{
  "status": "ok",
  "role": "full",
  "scopes": "approve,read,write",
  "jwt": "eyJhbGciOiJIUzI1NiIs...",
  "user_id": "u_abc123"
}
```

The response also sets a `turnstone_auth` HttpOnly cookie containing the JWT.

**Response (failure):** `401`

```json
{"error": "Invalid credentials"}
```

---

### `POST /v1/api/auth/logout`

Clears the `turnstone_auth` cookie. No request body required.

**Response:** `200`

```json
{"status": "ok"}
```

The response includes a `Set-Cookie` header that expires the auth cookie.

---

### `GET /v1/api/auth/status`

Returns the current authentication state. Works with or without a valid token.

**Response (authenticated):** `200`

```json
{
  "authenticated": true,
  "user_id": "u_abc123",
  "scopes": ["approve", "read", "write"],
  "source": "jwt"
}
```

**Response (not authenticated):** `200`

```json
{
  "authenticated": false,
  "user_id": null,
  "scopes": [],
  "source": null
}
```

**Response (auth disabled):** `200`

```json
{
  "authenticated": false,
  "auth_enabled": false
}
```

---

### `POST /v1/api/auth/setup`

Creates the first admin user when no users exist in the database. This is a
public endpoint (no authentication required) that only succeeds when auth is
enabled and the user database is empty. Both the server and console expose
this endpoint.

**Request body:**

```json
{
  "username": "admin",
  "display_name": "Admin",
  "password": "strongpass"
}
```

| Field          | Type   | Required | Validation                  |
|----------------|--------|----------|-----------------------------|
| `username`     | string | yes      | 1-64 ASCII characters       |
| `display_name` | string | yes      | Non-empty                   |
| `password`     | string | yes      | Minimum 8 characters        |

**Response (success):** `200`

```json
{
  "status": "ok",
  "user_id": "u_abc123",
  "username": "admin",
  "role": "full",
  "scopes": "approve,read,write",
  "jwt": "eyJhbGciOiJIUzI1NiIs..."
}
```

The response also sets a `turnstone_auth` HttpOnly cookie containing the JWT.

**Response (already set up):** `409`

```json
{"error": "Setup already completed"}
```

Returned when one or more users already exist in the database.

**Response (auth disabled):** `400`

```json
{"error": "Auth is not enabled"}
```

---

## Endpoints

### `GET /`

Serves the embedded single-page application (HTML, CSS, and JavaScript inlined
in a single document). The SPA connects to the SSE and POST endpoints listed
below.

**Response:** `text/html; charset=utf-8`

---

### `GET /v1/api/events?ws_id=<id>`

Opens a Server-Sent Events stream scoped to a single workstream. The connection
remains open indefinitely; the server pushes events as they occur.

**Query parameters:**

| Parameter | Type   | Required | Description                |
|-----------|--------|----------|----------------------------|
| `ws_id`   | string | yes      | Workstream identifier      |

**Error:** Returns `404` with `{"error": "Unknown workstream"}` if `ws_id` is
not recognized.

#### Connection lifecycle

1. **`connected`** -- sent immediately on connect.

```json
{
  "type": "connected",
  "model": "kappa_20b_131k",
  "model_alias": "default",
  "skip_permissions": false
}
```

`skip_permissions` reflects the workstream's current auto-approve state. It is
`true` if the server was started with `--skip-permissions` or if the user chose
"Always approve" via the approval prompt during the session.

2. **`history`** -- replays the full conversation history so the client can
   rebuild its UI.

```json
{
  "type": "history",
  "messages": [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hi there!", "tool_calls": null},
    {"role": "tool", "content": "..."}
  ]
}
```

Each message in the `messages` array has:

| Field        | Type              | Description                                   |
|--------------|-------------------|-----------------------------------------------|
| `role`       | string            | `"user"`, `"assistant"`, or `"tool"`          |
| `content`    | string or null    | Text content of the message                   |
| `tool_calls` | array or null     | Present only on assistant messages with calls  |

Each entry in `tool_calls`:

| Field       | Type   | Description                        |
|-------------|--------|------------------------------------|
| `name`      | string | Function name (e.g. `"bash"`)      |
| `arguments` | string | JSON-encoded argument string       |

#### Streaming events

After the initial `connected` and `history` frames, the server streams
real-time events as the model generates a response:

**`thinking_start`** -- the model has begun generating (shown as a spinner).

```json
{"type": "thinking_start"}
```

**`thinking_stop`** -- the spinner phase is over.

```json
{"type": "thinking_stop"}
```

**`reasoning`** -- a chunk of chain-of-thought reasoning text.

```json
{"type": "reasoning", "text": "Let me think about this..."}
```

**`content`** -- a chunk of the assistant's visible reply.

```json
{"type": "content", "text": "Here is the answer: "}
```

**`stream_end`** -- the model has finished generating. The client should
finalize any in-progress assistant message.

```json
{"type": "stream_end"}
```

**`tool_info`** -- one or more tool calls that were auto-approved (no user
action required).

```json
{
  "type": "tool_info",
  "items": [
    {
      "call_id": "call_abc123",
      "header": "bash: ls -la",
      "preview": "",
      "func_name": "bash",
      "approval_label": "bash",
      "needs_approval": false,
      "error": null
    }
  ]
}
```

**`approve_request`** -- one or more tool calls that require user approval. The
client must respond via `POST /v1/api/approve`.

```json
{
  "type": "approve_request",
  "items": [
    {
      "call_id": "call_def456",
      "header": "bash: rm -rf /tmp/build",
      "preview": "",
      "func_name": "bash",
      "approval_label": "bash",
      "needs_approval": true,
      "error": null
    }
  ]
}
```

Each item in `items` (shared by `tool_info` and `approve_request`):

| Field            | Type        | Description                                      |
|------------------|-------------|--------------------------------------------------|
| `call_id`        | string      | Unique tool call ID (links chunks to results)    |
| `header`         | string      | Human-readable header line for the tool call     |
| `preview`        | string      | Diff or argument preview (may be empty)          |
| `func_name`      | string      | Function name (e.g. `"bash"`, `"edit_file"`)     |
| `approval_label` | string      | Display label for the approval prompt            |
| `needs_approval` | bool        | Whether this call requires explicit approval     |
| `error`          | string/null | Error description if the call was malformed      |

**`tool_output_chunk`** -- incremental streaming output from a bash tool execution. Sent line-by-line as stdout is produced. The `call_id` identifies the specific tool invocation (multiple bash tools may run in parallel).

```json
{"type": "tool_output_chunk", "call_id": "call_abc123", "chunk": "Building project...\n"}
```

**`tool_result`** -- final output from a completed tool execution. The `call_id` matches the corresponding `tool_info`/`approve_request` item and any preceding `tool_output_chunk` events. For bash tools, this arrives after all streaming chunks and includes both stdout and stderr.

```json
{"type": "tool_result", "call_id": "call_abc123", "name": "bash", "output": "file1.py\nfile2.py\n"}
```

**`status`** -- token usage statistics, sent after each model turn.

```json
{
  "type": "status",
  "prompt_tokens": 1024,
  "completion_tokens": 256,
  "total_tokens": 1280,
  "context_window": 131072,
  "pct": 1.0,
  "effort": "medium"
}
```

| Field               | Type   | Description                                  |
|---------------------|--------|----------------------------------------------|
| `prompt_tokens`     | int    | Tokens in the prompt                         |
| `completion_tokens` | int    | Tokens generated by the model                |
| `total_tokens`      | int    | `prompt_tokens + completion_tokens`          |
| `context_window`    | int    | Total context window size in tokens          |
| `pct`               | float  | Percentage of context window used            |
| `effort`            | string | Reasoning effort level (`low`/`medium`/`high`) |

**`plan_review`** -- the model is proposing a plan and wants feedback. The
client must respond via `POST /v1/api/plan`.

```json
{"type": "plan_review", "content": "Step 1: ...\nStep 2: ..."}
```

**`info`** -- an informational message (e.g. command output).

```json
{"type": "info", "message": "Session cleared."}
```

**`error`** -- an error message.

```json
{"type": "error", "message": "Error: connection timed out"}
```

**`busy_error`** -- sent when a new message arrives while the model is already
processing.

```json
{"type": "busy_error", "message": "Already processing a request. Please wait."}
```

**`clear_ui`** -- instructs the client to clear all displayed messages (sent
after `/clear` or `/new` commands).

```json
{"type": "clear_ui"}
```

**`cancelled`** -- the generation was cancelled by the user (via the Stop
button or `POST /v1/api/cancel`). The client should finalize any in-progress
assistant message with whatever partial content was streamed.

```json
{"type": "cancelled"}
```

**`intent_verdict`** -- delivered asynchronously when the LLM judge completes
its evaluation of a pending tool call. Only sent when intent validation is
enabled (`--judge` or `[judge] enabled = true`). The `call_id` correlates with
the item in the preceding `approve_request` event.

```json
{
  "type": "intent_verdict",
  "verdict_id": "f7e8d9c0b1a2",
  "call_id": "call_abc123",
  "func_name": "bash",
  "intent_summary": "Install Express.js web framework via npm",
  "risk_level": "medium",
  "confidence": 0.85,
  "recommendation": "review",
  "reasoning": "The command installs express from npm. This is a well-known package but will modify node_modules and package.json.",
  "evidence": ["Checked package.json -- express is not currently a dependency"],
  "tier": "llm",
  "judge_model": "gpt-5",
  "latency_ms": 2340
}
```

| Field            | Type       | Description                                            |
|------------------|------------|--------------------------------------------------------|
| `verdict_id`     | string     | Unique verdict identifier                              |
| `call_id`        | string     | Tool call ID (matches `approve_request` item)          |
| `func_name`      | string     | Tool function name                                     |
| `intent_summary` | string     | One-sentence description of the tool call's intent     |
| `risk_level`     | string     | `"low"`, `"medium"`, `"high"`, or `"critical"`         |
| `confidence`     | float      | 0.0--1.0 confidence in the assessment                  |
| `recommendation` | string     | `"approve"`, `"review"`, or `"deny"`                   |
| `reasoning`      | string     | Evidence-based explanation                             |
| `evidence`       | list       | Supporting evidence (file excerpts, rule names)        |
| `tier`           | string     | Always `"llm"` for this event                          |
| `judge_model`    | string     | Model that produced the verdict                        |
| `latency_ms`     | int        | Evaluation time in milliseconds                        |

When intent validation is active, the `approve_request` event is also extended:
each item in `items` gains a `verdict` field containing the heuristic verdict
(same schema as above but with `tier: "heuristic"`), and the event gains a
top-level `judge_pending` boolean indicating whether an LLM verdict is in
flight.

#### Keepalive

The server sends an SSE comment every 5 seconds when no events are pending:

```
: keepalive

```

This prevents proxies and browsers from closing the connection due to
inactivity.

#### Multi-consumer fan-out

Each SSE connection to a workstream receives its own delivery queue.  Events
produced by the worker thread are fanned out to all registered listener queues,
so multiple consumers (browser, bridge, console proxy, SDK) can connect
simultaneously and each receives every event.  On reconnect the client receives
a full history replay, so no catch-up mechanism is needed.

---

### `GET /v1/api/events/global`

Opens a Server-Sent Events stream that broadcasts state-change events across
all workstreams. This is used by the tab bar to display per-workstream activity
indicators.

**Events:**

```json
{"type": "ws_state", "ws_id": "abc123", "state": "thinking"}
```

| Field   | Type   | Description              |
|---------|--------|--------------------------|
| `ws_id` | string | Workstream identifier    |
| `state` | string | Current workstream state |

Possible `state` values:

| State       | Description                                     |
|-------------|-------------------------------------------------|
| `idle`      | No active processing                            |
| `thinking`  | Model is generating a response                  |
| `running`   | Tool execution in progress                      |
| `attention` | Waiting for user input (approval or plan review)|
| `error`     | An error occurred                               |

**Fan-out pattern:** Each connected client receives its own bounded queue
(`maxsize=500`). A dedicated fan-out thread reads from the shared global queue
and copies each event to every client queue. If a client queue is full, the
event is silently dropped for that client.

**Keepalive:** Same as `/v1/api/events` -- an SSE comment every 5 seconds.

---

### `GET /v1/api/workstreams`

Returns a list of all active workstreams.

**Response:**

```json
{
  "workstreams": [
    {"id": "abc123", "name": "default", "state": "idle"},
    {"id": "def456", "name": "hacker-news", "state": "thinking"}
  ]
}
```

Each workstream object:

| Field        | Type        | Description                                            |
|--------------|-------------|--------------------------------------------------------|
| `id`         | string      | Unique workstream routing identifier                   |
| `name`       | string      | Display name (alias if set, otherwise `ws-xxxx`)       |
| `state`      | string      | Current state (see state values above)                 |

---

### `GET /v1/api/workstreams/saved`

Returns a list of saved workstreams from the database, ordered by most recently
updated.

**Response:**

```json
{
  "workstreams": [
    {
      "ws_id": "a1b2c3d4e5f6",
      "alias": "refactor",
      "title": "JWT Authentication Refactor",
      "created": "2026-03-01 10:00:00",
      "updated": "2026-03-01 11:30:00",
      "message_count": 42
    }
  ]
}
```

Each saved workstream object:

| Field           | Type        | Description                                |
|-----------------|-------------|--------------------------------------------|
| `ws_id`         | string      | Unique workstream identifier               |
| `alias`         | string/null | User-assigned short name                   |
| `title`         | string/null | LLM-generated title                        |
| `created`       | string      | ISO timestamp of workstream creation       |
| `updated`       | string      | ISO timestamp of last message              |
| `message_count` | int         | Number of messages in the workstream       |

---

### `POST /v1/api/send`

Sends a user message to a workstream. Spawns a daemon worker thread that calls
`session.send()` and streams results back via the SSE channel.

**Request body:**

```json
{"message": "Explain how the server works", "ws_id": "abc123"}
```

| Field     | Type   | Required | Description             |
|-----------|--------|----------|-------------------------|
| `message` | string | yes      | The user's message text |
| `ws_id`   | string | yes      | Target workstream ID    |

**Response (success):**

```json
{"status": "ok"}
```

**Response (busy):** Returned if the workstream's worker thread is still alive
from a previous request. Also pushes a `busy_error` event to the SSE stream.

```json
{"status": "busy"}
```

**Error responses:**

| Status | Body                               | Condition              |
|--------|------------------------------------|------------------------|
| 400    | `{"error": "Empty message"}`       | Message is empty       |
| 404    | `{"error": "Unknown workstream"}`  | `ws_id` not found      |

---

### `POST /v1/api/approve`

Responds to a tool approval request. The SSE stream must have previously sent
an `approve_request` event for the given workstream.

**Request body:**

```json
{"approved": true, "feedback": null, "always": false, "ws_id": "abc123"}
```

| Field      | Type        | Required | Description                                      |
|------------|-------------|----------|--------------------------------------------------|
| `approved` | bool        | yes      | `true` to approve, `false` to deny               |
| `feedback` | string/null | no       | Optional feedback text (sent as denial reason)    |
| `always`   | bool        | no       | If `true` and `approved`, enables auto-approve    |
| `ws_id`    | string      | yes      | Target workstream ID                              |

When `always` is `true` and `approved` is `true`, the workstream's WebUI
instance sets `auto_approve = True`, causing all subsequent tool calls to be
automatically approved without prompting.

**Response:**

```json
{"status": "ok"}
```

**Error:** `404` with `{"error": "Unknown workstream"}` if `ws_id` is invalid.

---

### `POST /v1/api/plan`

Responds to a plan review dialog. The SSE stream must have previously sent a
`plan_review` event for the given workstream.

**Request body:**

```json
{"feedback": "", "ws_id": "abc123"}
```

| Field      | Type   | Required | Description                                             |
|------------|--------|----------|---------------------------------------------------------|
| `feedback` | string | yes      | Feedback text; empty string means approval              |
| `ws_id`    | string | yes      | Target workstream ID                                    |

To approve the plan, send an empty string for `feedback`. To reject or request
changes, send a non-empty feedback string (e.g. `"reject"` or specific
revision instructions).

**Response:**

```json
{"status": "ok"}
```

**Error:** `404` with `{"error": "Unknown workstream"}` if `ws_id` is invalid.

---

### `POST /v1/api/command`

Executes a slash command in the given workstream.

**Request body:**

```json
{"command": "/clear", "ws_id": "abc123"}
```

| Field     | Type   | Required | Description                        |
|-----------|--------|----------|------------------------------------|
| `command` | string | yes      | The slash command (e.g. `/clear`)  |
| `ws_id`   | string | yes      | Target workstream ID               |

If the command is `/clear` or `/new`, the server pushes a `clear_ui` SSE event
to instruct the client to reset its message display. If the command is
`/resume`, the server pushes `clear_ui` followed by a `history` event
containing the resumed session's messages.

**Response:**

```json
{"status": "ok"}
```

**Error responses:**

| Status | Body                               | Condition            |
|--------|------------------------------------|----------------------|
| 400    | `{"error": "Empty command"}`       | Command is empty     |
| 404    | `{"error": "Unknown workstream"}`  | `ws_id` not found    |

---

### `POST /v1/api/cancel`

Cancels the active generation in a workstream. Sets a cooperative cancellation
flag that is checked at multiple points in the generation loop (per streaming
chunk, before tool execution, inside bash commands). The session transitions to
`idle` state and preserves any partial content already streamed.

If the workstream is waiting for tool approval or plan review, the pending
prompt is automatically denied/rejected to unblock the worker thread.

Calling this endpoint when the workstream is already idle is a harmless no-op.

**Request body:**

```json
{"ws_id": "abc123"}
```

| Field  | Type   | Required | Description          |
|--------|--------|----------|----------------------|
| `ws_id`| string | yes      | Target workstream ID |

**Response:**

```json
{"status": "ok"}
```

**Error responses:**

| Status | Body                               | Condition              |
|--------|------------------------------------|------------------------|
| 400    | `{"error": "No session"}`          | Session not initialized|
| 404    | `{"error": "Unknown workstream"}`  | `ws_id` not found      |

---

### `POST /v1/api/workstreams/new`

Creates a new workstream. The server supports up to 10 concurrent workstreams.

**Request body:**

```json
{"name": "my-ws", "model": "openai"}
```

All fields are optional. The body can be empty or an empty JSON object.

| Field            | Type   | Default | Description                                                    |
|------------------|--------|---------|----------------------------------------------------------------|
| `name`           | string | auto    | Workstream display name                                        |
| `model`          | string | default | Model alias from the registry (`[models.*]`)                   |
| `auto_approve`   | bool   | false   | Auto-approve all tool calls for this workstream                |
| `resume_ws`      | string | ""      | Workstream ID to resume atomically during creation (empty = fresh)|
| `template`       | string | ""      | Prompt template name (replaces default templates; 400 if not found)|
| `ws_template`    | string | ""      | Workstream template name. Applies model, temperature, reasoning effort, max tokens, auto-approve policy, and token budget. Returns 400 if not found or disabled. |

> **Template precedence:** When `ws_template` is specified, its model override takes effect before workstream creation. Both `template` (prompt template) and `ws_template` (workstream template) can be used together — `ws_template` controls the behavioral profile while `template` sets the system message text. If `ws_template` defines its own system prompt or prompt template reference, that takes precedence over the `template` parameter.

**Response (success):**

```json
{"ws_id": "ghi789", "name": "ws-3", "resumed": false, "message_count": 0}
```

| Field           | Type   | Description                                         |
|-----------------|--------|-----------------------------------------------------|
| `ws_id`         | string | Unique ID of the new workstream                     |
| `name`          | string | Auto-generated workstream name                      |
| `resumed`       | bool   | Whether a previous session was successfully resumed |
| `message_count` | int    | Number of messages in the resumed session (0 if fresh) |

**Error (limit reached):**

```json
{"error": "Maximum of 10 workstreams reached"}
```

Status code: `400`

---

### `POST /v1/api/workstreams/close`

Closes and removes a workstream. The last remaining workstream cannot be
closed.

**Request body:**

```json
{"ws_id": "abc123"}
```

| Field   | Type   | Required | Description               |
|---------|--------|----------|---------------------------|
| `ws_id` | string | yes      | Workstream ID to close    |

**Response (success):**

```json
{"status": "ok"}
```

**Error (last workstream):**

```json
{"error": "Cannot close last workstream"}
```

Status code: `400`

---

### `GET /v1/api/watches`

List active watches on this server node. Optionally filter by workstream.
Requires `write` scope.

**Query parameters:**

| Parameter | Type   | Required | Description                        |
|-----------|--------|----------|------------------------------------|
| `ws_id`   | string | no       | Filter to watches for this workstream. If omitted, returns all watches on the node. |

**Response:**

```json
{
  "watches": [
    {
      "watch_id": "abc123def456...",
      "ws_id": "ws-1",
      "node_id": "host_a1b2",
      "name": "pr-review",
      "command": "gh pr view --json state",
      "interval_secs": 300.0,
      "stop_on": "data[\"state\"] == \"MERGED\"",
      "max_polls": 100,
      "poll_count": 5,
      "last_output": "{\"state\": \"OPEN\"}",
      "last_poll": "2026-03-09T12:00:00",
      "next_poll": "2026-03-09T12:05:00",
      "active": 1,
      "created": "2026-03-09T11:30:00"
    }
  ]
}
```

---

### `POST /v1/api/watches/{watch_id}/cancel`

Cancel an active watch. Sets `active=0` and clears `next_poll`.
Requires `write` scope. Verifies node ownership in multi-node deployments.

**Path parameters:**

| Parameter  | Type   | Description     |
|------------|--------|-----------------|
| `watch_id` | string | Watch ID to cancel |

**Response (success):**

```json
{"status": "ok", "watch_id": "abc123def456..."}
```

**Error (not found):**

```json
{"error": "Watch not found"}
```

Status code: `404`

**Error (wrong node):**

```json
{"error": "Watch belongs to another node"}
```

Status code: `403`

---

### `GET /v1/api/memories`

List structured memories with optional filters. Requires `read` scope.

**Query parameters:**

| Parameter  | Type   | Required | Default | Description                  |
|------------|--------|----------|---------|------------------------------|
| `type`     | string | no       | `""`    | Filter by memory type (user, project, feedback, reference) |
| `scope`    | string | no       | `""`    | Filter by scope (global, workstream, user) |
| `scope_id` | string | no       | `""`    | Scope qualifier. Auto-resolved for `scope=user` when auth is active. |
| `limit`    | int    | no       | `100`   | Max results (capped at 200)  |

**Response:**

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

Save or upsert a structured memory. Requires `write` scope. Returns `201` on
create, `200` on update.

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

**Error responses:**

| Status | Condition                                              |
|--------|--------------------------------------------------------|
| 400    | Missing name, empty content, invalid type/scope, name too long, content too long |

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

**Response:**

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

**Error:** `400` with `{"error": "query is required"}` if `query` is empty.

---

### `DELETE /v1/api/memories/{name}`

Delete a memory by name and scope. Requires `write` scope.

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

**Error (not found):** `404`

```json
{"error": "Memory 'deployment_process' not found"}
```

---

### `GET /v1/api/admin/memories` (Console)

List structured memories across all scopes. Requires `admin.memories`
permission.

**Query parameters:**

| Parameter  | Type   | Required | Default | Description                  |
|------------|--------|----------|---------|------------------------------|
| `type`     | string | no       | `""`    | Filter by type               |
| `scope`    | string | no       | `""`    | Filter by scope              |
| `scope_id` | string | no       | `""`    | Filter by scope ID           |
| `limit`    | int    | no       | `100`   | Max results (capped at 200)  |

**Response:** `200` -- same schema as `GET /v1/api/memories`.

---

### `GET /v1/api/admin/memories/search` (Console)

Search memories by query. Requires `admin.memories` permission.

**Query parameters:**

| Parameter  | Type   | Required | Default | Description                   |
|------------|--------|----------|---------|-------------------------------|
| `q`        | string | yes      | --      | Search query                  |
| `type`     | string | no       | `""`    | Filter by type                |
| `scope`    | string | no       | `""`    | Filter by scope               |
| `scope_id` | string | no       | `""`    | Filter by scope ID            |
| `limit`    | int    | no       | `20`    | Max results (capped at 50)    |

**Response:** `200` -- same schema as `GET /v1/api/memories`.

**Error:** `400` with `{"error": "q is required"}` if `q` is empty.

---

### `GET /v1/api/admin/memories/{memory_id}` (Console)

Get a single memory by ID. Requires `admin.memories` permission.

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

**Error (not found):** `404`

```json
{"error": "Memory not found"}
```

---

### `DELETE /v1/api/admin/memories/{memory_id}` (Console)

Delete a memory by ID. Records an audit event (`memory.delete`). Requires
`admin.memories` permission.

**Path parameters:**

| Parameter   | Type   | Description            |
|-------------|--------|------------------------|
| `memory_id` | string | Memory UUID            |

**Response (success):** `200`

```json
{"status": "ok"}
```

**Error (not found):** `404`

```json
{"error": "Memory not found"}
```

---

### `GET /v1/api/admin/verdicts` (Console)

List intent validation verdicts from the `intent_verdicts` table. This endpoint
is on the **console** server and requires the `admin.judge` permission.

**Query parameters:**

| Parameter    | Type   | Required | Description                                        |
|--------------|--------|----------|----------------------------------------------------|
| `ws_id`      | string | no       | Filter by workstream ID                            |
| `since`      | string | no       | ISO timestamp lower bound                          |
| `until`      | string | no       | ISO timestamp upper bound                          |
| `risk_level` | string | no       | Filter by risk level (`low`/`medium`/`high`/`critical`) |
| `limit`      | int    | no       | Max results (default 100, max 500)                 |
| `offset`     | int    | no       | Pagination offset (default 0)                      |

**Response:**

```json
{
  "verdicts": [
    {
      "verdict_id": "a1b2c3d4e5f6",
      "ws_id": "ws-1",
      "call_id": "call_abc123",
      "func_name": "bash",
      "func_args": "{\"command\": \"npm install express\"}",
      "intent_summary": "Package installation: npm install express",
      "risk_level": "medium",
      "confidence": 0.70,
      "recommendation": "review",
      "reasoning": "Command installs a software package which may modify the environment.",
      "evidence": "[\"Matched rule: package-install\"]",
      "tier": "heuristic",
      "judge_model": "",
      "latency_ms": 0,
      "user_decision": "approved",
      "created": "2026-03-13T10:00:00"
    }
  ],
  "total": 42
}
```

---

### `GET /v1/api/admin/settings` (Console)

List all settings with their effective values, defaults, and metadata. Requires
the `admin.settings` permission.

**Response:** `200`

```json
{
  "settings": [
    {
      "key": "model.temperature",
      "value": 0.7,
      "source": "storage",
      "type": "float",
      "description": "Sampling temperature",
      "section": "model",
      "is_secret": false,
      "node_id": "",
      "changed_by": "admin",
      "updated": "2026-03-14T10:00:00",
      "restart_required": false
    }
  ]
}
```

---

### `GET /v1/api/admin/settings/schema` (Console)

Return the full registry catalog (all defined settings with metadata). Requires
the `admin.settings` permission. Useful for building dynamic admin UIs.

**Response:** `200`

```json
{
  "schema": [
    {
      "key": "model.temperature",
      "type": "float",
      "default": 0.5,
      "description": "Sampling temperature",
      "section": "model",
      "is_secret": false,
      "min_value": 0.0,
      "max_value": 2.0,
      "choices": null,
      "restart_required": false
    }
  ]
}
```

---

### `PUT /v1/api/admin/settings/{key}` (Console)

Update a setting. Requires the `admin.settings` permission. The value is
validated against the registry definition (type coercion, range checks, choices).
Secret settings (`is_secret=true`) return `403`.

**Path parameters:**

| Parameter | Type   | Description |
|-----------|--------|-------------|
| `key`     | string | Dotted setting key (e.g. `model.temperature`) |

**Request body:**

```json
{
  "value": 0.7,
  "node_id": ""
}
```

| Field     | Type   | Required | Default | Description |
|-----------|--------|----------|---------|-------------|
| `value`   | any    | yes      | --      | New value (type-coerced against registry) |
| `node_id` | string | no       | `""`    | Node ID for per-node override |

**Response (success):** `200`

```json
{
  "key": "model.temperature",
  "value": 0.7,
  "source": "storage",
  "type": "float",
  "description": "Sampling temperature",
  "section": "model",
  "is_secret": false,
  "node_id": "",
  "changed_by": "admin",
  "updated": "",
  "restart_required": false
}
```

**Errors:**

| Status | Condition |
|--------|-----------|
| 400    | Unknown key, invalid value, type mismatch, out of range, missing `value` field |
| 403    | Secret setting (must use config.toml or env) |

---

### `DELETE /v1/api/admin/settings/{key}` (Console)

Reset a setting to its registry default by removing it from storage. Requires
the `admin.settings` permission.

**Path parameters:**

| Parameter | Type   | Description |
|-----------|--------|-------------|
| `key`     | string | Dotted setting key |

**Query parameters:**

| Parameter | Type   | Required | Default | Description |
|-----------|--------|----------|---------|-------------|
| `node_id` | string | no       | `""`    | Node ID (empty = global) |

**Response (success):** `200`

```json
{"status": "ok", "key": "model.temperature", "default": 0.5}
```

**Response (not found):** `404`

```json
{"error": "Setting 'model.temperature' has no stored value"}
```

---

### `OPTIONS` (any path)

Handles CORS preflight requests.

**Response headers:**

```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET, POST, OPTIONS
Access-Control-Allow-Headers: Content-Type
```

Status code: `200` with an empty body.

---

## Error Handling

| Condition                          | Behavior                                                   |
|------------------------------------|------------------------------------------------------------|
| Malformed or unparseable JSON body | Treated as an empty dict `{}`; missing fields use defaults |
| Unknown `ws_id`                    | `404` with `{"error": "Unknown workstream"}`               |
| Unknown path (GET or POST)         | `404` with plain-text body `Not found`                     |
| Empty `message` on `/v1/api/send`     | `400` with `{"error": "Empty message"}`                    |
| Empty `command` on `/v1/api/command`  | `400` with `{"error": "Empty command"}`                    |
| Rate limit exceeded                | `429` with `Retry-After` header (see below)                |

### `429 Too Many Requests`

Returned when the per-IP rate limiter rejects a request. `/health` and
`/metrics` are exempt.

**Response headers:**

```
Retry-After: 2
```

**Response body:**

```json
{"error": "Rate limit exceeded", "retry_after": 2}
```

| Field         | Type   | Description                                    |
|---------------|--------|------------------------------------------------|
| `error`       | string | `"Rate limit exceeded"`                        |
| `retry_after` | number | Seconds until the client should retry          |

---

## SSE Reconnection

The embedded JavaScript client implements exponential backoff for SSE
reconnection:

| Parameter          | Value                                     |
|--------------------|-------------------------------------------|
| Base delay         | 1 second                                  |
| Backoff multiplier | 2x on each consecutive failure            |
| Maximum delay      | 30 seconds                                |
| Reset              | Delay resets to 1 second on first success |

On reconnect, the server replays the full conversation history via the
`history` event, so the client can rebuild its UI state without data loss. The
same reconnection strategy applies to both the per-workstream SSE stream
(`/v1/api/events`) and the global state stream (`/v1/api/events/global`).

---

## Observability

### `GET /health`

Returns server health status. Always returns `200 OK` while the server process
is running. `"status": "degraded"` indicates the server is up but the LLM
backend is unreachable. Suitable for load-balancer health checks and Kubernetes
liveness probes.

**Response:** `application/json`

```json
{
  "status": "ok",
  "version": "0.4.0",
  "node_id": "worker-01_a3f2",
  "uptime_seconds": 3614.72,
  "model": "llama-3.1-70b-instruct",
  "workstreams": {
    "total": 2,
    "idle": 1,
    "thinking": 1,
    "running": 0,
    "attention": 0,
    "error": 0
  },
  "backend": {
    "status": "up",
    "circuit_state": "closed"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | `"ok"` or `"degraded"` (degraded when backend unreachable) |
| `version` | string | turnstone server version |
| `node_id` | string | Server-generated node identity (`{hostname}_{4hex}`) |
| `uptime_seconds` | number | Seconds since the server process started |
| `model` | string | Model name detected or configured at startup |
| `workstreams.total` | integer | Total active workstreams |
| `workstreams.idle` | integer | Workstreams waiting for user input |
| `workstreams.thinking` | integer | Workstreams with LLM currently streaming |
| `workstreams.running` | integer | Workstreams executing tools |
| `workstreams.attention` | integer | Workstreams blocked on approval or plan review |
| `workstreams.error` | integer | Workstreams in error state |
| `backend.status` | string | `"up"` or `"down"` — LLM backend reachability |
| `backend.circuit_state` | string | `"closed"`, `"open"`, or `"half_open"` |

---

### `GET /metrics`

Returns operational metrics in **Prometheus text exposition format v0.0.4**.
Compatible with Prometheus `scrape_configs`, VictoriaMetrics, Grafana Agent,
and any other OpenMetrics-compatible collector.

**Response:** `text/plain; version=0.0.4; charset=utf-8`

#### Prometheus scrape config example

```yaml
scrape_configs:
  - job_name: turnstone
    static_configs:
      - targets: ["localhost:8080"]
    metrics_path: /metrics
```

#### Metrics reference

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `turnstone_build_info` | gauge | `version`, `model` | Always 1; carries version/model as labels |
| `turnstone_uptime_seconds` | gauge | — | Seconds since server start |
| `turnstone_workstreams_active_total` | gauge | — | Number of active workstreams |
| `turnstone_workstreams_by_state` | gauge | `state` | Workstream count per state (`idle`, `thinking`, `running`, `attention`, `error`) |
| `turnstone_http_requests_total` | counter | `method`, `endpoint`, `status_code` | Total HTTP requests handled |
| `turnstone_http_request_duration_seconds` | histogram | `method`, `endpoint` | Request latency distribution (11 buckets: 5ms–10s) |
| `turnstone_messages_sent_total` | counter | — | User messages dispatched to the AI |
| `turnstone_tokens_total` | counter | `type` | Tokens consumed (`type="prompt"` or `type="completion"`) |
| `turnstone_tool_calls_total` | counter | `tool` | Tool executions by name (e.g. `tool="bash"`) |
| `turnstone_errors_total` | counter | — | Errors reported by workstreams |
| `turnstone_context_window_used_ratio` | gauge | — | Last known fraction of context window in use (0.0–1.0) |
| `turnstone_sse_connections_active` | gauge | — | Number of open SSE connections |
| `turnstone_ratelimit_rejected_total` | counter | — | Requests rejected by the per-IP rate limiter |
| `turnstone_backend_up` | gauge | — | LLM backend reachability (1 = up, 0 = down) |
| `turnstone_circuit_state` | gauge | — | Circuit breaker state (0 = closed, 1 = open, 2 = half_open) |
| `turnstone_workstreams_evicted_total` | counter | — | Workstreams auto-evicted when at capacity |

#### Example output

```
# HELP turnstone_build_info Server version and model info
# TYPE turnstone_build_info gauge
turnstone_build_info{version="0.2.0",model="llama-3.1-70b-instruct"} 1
# HELP turnstone_uptime_seconds Server uptime in seconds
# TYPE turnstone_uptime_seconds gauge
turnstone_uptime_seconds 3614.72
# HELP turnstone_workstreams_active_total Number of active workstreams
# TYPE turnstone_workstreams_active_total gauge
turnstone_workstreams_active_total 1
# HELP turnstone_http_requests_total Total HTTP requests handled
# TYPE turnstone_http_requests_total counter
turnstone_http_requests_total{method="GET",endpoint="/health",status_code="200"} 42
turnstone_http_requests_total{method="GET",endpoint="/metrics",status_code="200"} 7
turnstone_http_requests_total{method="POST",endpoint="/v1/api/send",status_code="200"} 18
# HELP turnstone_tokens_total Total tokens consumed
# TYPE turnstone_tokens_total counter
turnstone_tokens_total{type="prompt"} 84320
turnstone_tokens_total{type="completion"} 12150
# HELP turnstone_tool_calls_total Total tool executions by name
# TYPE turnstone_tool_calls_total counter
turnstone_tool_calls_total{tool="bash"} 7
turnstone_tool_calls_total{tool="read_file"} 3
```
