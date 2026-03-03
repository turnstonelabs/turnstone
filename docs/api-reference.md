# turnstone Web Server API Reference

## Overview

> See also: [MQ Protocol diagram](diagrams/png/06-mq-protocol.png) | [Message Routing diagram](diagrams/png/07-message-routing.png) | [Redis Key Schema diagram](diagrams/png/08-redis-key-schema.png)

`turnstone-server` exposes a browser-based chat UI backed by a Python stdlib HTTP
server (`socketserver.ThreadingMixIn` + `http.server.HTTPServer`). The server
uses **Server-Sent Events (SSE)** for real-time streaming and **HTTP POST** for
user actions.

All API responses use `Content-Type: application/json` unless otherwise noted.
CORS headers (`Access-Control-Allow-Origin: *`) are included on every response.

The server supports multiple concurrent **workstreams** (tabs), each backed by
an independent `ChatSession` and event queue.

---

## Endpoints

### `GET /`

Serves the embedded single-page application (HTML, CSS, and JavaScript inlined
in a single document). The SPA connects to the SSE and POST endpoints listed
below.

**Response:** `text/html; charset=utf-8`

---

### `GET /api/events?ws_id=<id>`

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
client must respond via `POST /api/approve`.

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
client must respond via `POST /api/plan`.

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

#### Keepalive

The server sends an SSE comment every 5 seconds when no events are pending:

```
: keepalive

```

This prevents proxies and browsers from closing the connection due to
inactivity.

#### Generation mechanism

Each new SSE connection to a workstream increments an internal
`_sse_generation` counter. The previous SSE handler detects the generation
mismatch and exits its event loop, ensuring only one active SSE connection per
workstream at a time. The event queue is drained of stale events before the new
connection begins streaming.

---

### `GET /api/events/global`

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

**Keepalive:** Same as `/api/events` -- an SSE comment every 5 seconds.

---

### `GET /api/workstreams`

Returns a list of all active workstreams.

**Response:**

```json
{
  "workstreams": [
    {"id": "abc123", "name": "default", "state": "idle", "session_id": "a1b2c3d4e5f6"},
    {"id": "def456", "name": "hacker-news", "state": "thinking", "session_id": "c5d6e7f8a9b0"}
  ]
}
```

Each workstream object:

| Field        | Type        | Description                                            |
|--------------|-------------|--------------------------------------------------------|
| `id`         | string      | Unique workstream routing identifier                   |
| `name`       | string      | Display name (alias if set, otherwise `ws-xxxx`)       |
| `state`      | string      | Current state (see state values above)                 |
| `session_id` | string/null | Session ID of the workstream's `ChatSession`, used for deduplication against `/api/sessions` |

---

### `GET /api/sessions`

Returns a list of saved sessions from the database, ordered by most recently
updated.

**Response:**

```json
{
  "sessions": [
    {
      "session_id": "a1b2c3d4e5f6",
      "alias": "refactor",
      "title": "JWT Authentication Refactor",
      "created": "2026-03-01 10:00:00",
      "updated": "2026-03-01 11:30:00",
      "message_count": 42
    }
  ]
}
```

Each session object:

| Field           | Type        | Description                                |
|-----------------|-------------|--------------------------------------------|
| `session_id`    | string      | Unique 12-char hex session identifier      |
| `alias`         | string/null | User-assigned short name                   |
| `title`         | string/null | LLM-generated title                        |
| `created`       | string      | ISO timestamp of session creation          |
| `updated`       | string      | ISO timestamp of last message              |
| `message_count` | int         | Number of messages in the session          |

---

### `POST /api/send`

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

### `POST /api/approve`

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

### `POST /api/plan`

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

### `POST /api/command`

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

### `POST /api/workstreams/new`

Creates a new workstream. The server supports up to 10 concurrent workstreams.

**Request body:**

```json
{"name": "my-ws", "model": "openai"}
```

All fields are optional. The body can be empty or an empty JSON object.

| Field          | Type   | Default | Description                                    |
|----------------|--------|---------|------------------------------------------------|
| `name`         | string | auto    | Workstream display name                        |
| `model`        | string | default | Model alias from the registry (`[models.*]`)   |
| `auto_approve` | bool   | false   | Auto-approve all tool calls for this workstream |

**Response (success):**

```json
{"ws_id": "ghi789", "name": "ws-3"}
```

| Field   | Type   | Description                        |
|---------|--------|------------------------------------|
| `ws_id` | string | Unique ID of the new workstream    |
| `name`  | string | Auto-generated workstream name     |

**Error (limit reached):**

```json
{"error": "Maximum of 10 workstreams reached"}
```

Status code: `400`

---

### `POST /api/workstreams/close`

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
| Empty `message` on `/api/send`     | `400` with `{"error": "Empty message"}`                    |
| Empty `command` on `/api/command`  | `400` with `{"error": "Empty command"}`                    |

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
(`/api/events`) and the global state stream (`/api/events/global`).

---

## Observability

### `GET /health`

Returns server health status. Always returns `200 OK` while the server process
is running. Suitable for load-balancer health checks and Kubernetes liveness
probes.

**Response:** `application/json`

```json
{
  "status": "ok",
  "version": "0.2.0",
  "uptime_seconds": 3614.72,
  "model": "llama-3.1-70b-instruct",
  "workstreams": {
    "total": 2,
    "idle": 1,
    "thinking": 1,
    "running": 0,
    "attention": 0,
    "error": 0
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | Always `"ok"` while server is running |
| `version` | string | turnstone server version |
| `uptime_seconds` | number | Seconds since the server process started |
| `model` | string | Model name detected or configured at startup |
| `workstreams.total` | integer | Total active workstreams |
| `workstreams.idle` | integer | Workstreams waiting for user input |
| `workstreams.thinking` | integer | Workstreams with LLM currently streaming |
| `workstreams.running` | integer | Workstreams executing tools |
| `workstreams.attention` | integer | Workstreams blocked on approval or plan review |
| `workstreams.error` | integer | Workstreams in error state |

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
turnstone_http_requests_total{method="POST",endpoint="/api/send",status_code="200"} 18
# HELP turnstone_tokens_total Total tokens consumed
# TYPE turnstone_tokens_total counter
turnstone_tokens_total{type="prompt"} 84320
turnstone_tokens_total{type="completion"} 12150
# HELP turnstone_tool_calls_total Total tool executions by name
# TYPE turnstone_tool_calls_total counter
turnstone_tool_calls_total{tool="bash"} 7
turnstone_tool_calls_total{tool="read_file"} 3
```
