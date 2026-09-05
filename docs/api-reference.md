# turnstone Web Server API Reference

## Overview

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

Auth is always enabled. All API endpoints except public paths require a valid token.

### Sending Credentials

Include a token in one of two ways:

- **Bearer header**: `Authorization: Bearer <token>`
- **Cookie**: the surface-scoped auth cookie — `turnstone_auth_server` on
  turnstone-server, `turnstone_auth_console` on turnstone-console (set
  automatically by the login endpoint). The names differ so the two surfaces,
  when co-hosted on one origin, don't overwrite each other's session.

The server accepts two token types:

| Type | Format | Example |
|------|--------|---------|
| JWT | Base64 segments separated by dots | `eyJhbG...` |
| API token | `ts_` prefix + 64 hex chars | `ts_a1b2c3d4...` |

JWTs are the recommended credential for browser sessions. API tokens are suitable for programmatic access and CI/CD.

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

The response also sets a surface-scoped HttpOnly cookie containing the JWT
(`turnstone_auth_server` on turnstone-server, `turnstone_auth_console` on turnstone-console).

**Response (failure):** `401`

```json
{"error": "Invalid credentials"}
```

---

### `POST /v1/api/auth/logout`

Clears the surface-scoped auth cookie (`turnstone_auth_server` /
`turnstone_auth_console`). No request body required.

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

The response also sets a surface-scoped HttpOnly cookie containing the JWT
(`turnstone_auth_server` on turnstone-server, `turnstone_auth_console` on turnstone-console).

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

### `GET /v1/api/workstreams/{ws_id}/events`

Opens a Server-Sent Events stream scoped to a single workstream. The connection
remains open indefinitely; the server pushes events as they occur.

**Path parameters:**

| Parameter | Type   | Required | Description                |
|-----------|--------|----------|----------------------------|
| `ws_id`   | string | yes      | Workstream identifier      |

**Error:** Returns `404` with `{"error": "Unknown workstream"}` if `ws_id` is
not recognized.

#### Connection lifecycle

1. **`connected`** -- sent in the synthetic replay for a fresh connection (and
   after an announced replay gap). A cursor reconnect whose buffered gap is
   fully covered receives only the missing buffered events, so this preamble is
   not duplicated.

```json
{
  "type": "connected",
  "model": "kappa_20b_131k",
  "model_alias": "default",
  "skip_permissions": false
}
```

`skip_permissions` reflects the workstream's blanket auto-approve state. It is
`true` if the server was started with `--skip-permissions` or the workstream was
created with blanket approval. "Approve + Always" now remembers only the tool
names from the resolved cycle and does not flip this field.

2. **REST history bootstrap** -- the SSE stream does not carry the full
   transcript. Before opening a pane's initial event stream, fetch
   `GET /v1/api/workstreams/{ws_id}/history?limit=100` (limit is clamped to
   1--500). This also works for a saved workstream that is not loaded in the
   manager.

```json
{
  "ws_id": "abc123",
  "messages": [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hi there!", "tool_calls": null},
    {"role": "tool", "content": "..."},
    {"role": "system", "source": "compaction", "content": "Summary..."}
  ],
  "cursor": null,
  "handoff_token": "opaque-live-revision"
}
```

`cursor` is normally `null`. When history intentionally trims a still-running
trailing turn that the event ring can reconstruct, open the SSE URL with
`?last_event_id=<cursor>` (or send `Last-Event-ID`) so the buffered delta fills
that turn without double-rendering it.

For a loaded workstream, `messages` is the requested tail projection of one
authoritative total accepted conversation-row prefix. It includes user,
assistant, tool, and system rows, including compaction checkpoints projected as
`role: "system", source: "compaction"` and cancellation-generated partial
assistant or synthesized tool-result markers when present.

`handoff_token` is non-null exactly when the workstream's session is live on
the serving node (a pane opened it there); it identifies the exact total
prefix used for that render. Pass it once on the initial SSE URL as
`history_token`. The token is opaque and process-local: do not parse,
persist, or reuse it. Admission of any later conversation row changes the
token; moving a row from the pending journal to durable storage does not. The
server validates the token while atomically registering the listener. A
mismatch emits `history_resync` and closes the stream; fetch history again
instead of replaying from a numeric cursor. A native reconnect's
`Last-Event-ID` header takes priority and follows the normal ring-replay
path.

A null `handoff_token` on a 200 is the cold storage-only read: the workstream
is not loaded on the serving node, so there is no live writer and no splice
to witness. The payload may seed a render and a token-less stream bootstrap
(the server converges the pane through `clear_ui`), never a cursor handoff.
`/history` never loads a session — reading an archived transcript leaves the
session pool untouched.

Each message in the `messages` array has:

| Field        | Type              | Description                                   |
|--------------|-------------------|-----------------------------------------------|
| `role`       | string            | `"user"`, `"assistant"`, `"tool"`, or `"system"` |
| `content`    | string or null    | Text content of the message                   |
| `tool_calls` | array or null     | Present only on assistant messages with calls  |
| `source`     | string (optional) | Operator-context or marker source, including `"compaction"` |
| `meta`       | object (optional) | Structured display metadata for the source     |
| `attachments` | array (optional) | Accepted attachment metadata: `attachment_id`, `kind`, `filename`, and `mime_type` |
| `sender` | string (optional) | Authenticated participant attributed to an accepted user row |
| `client_send_ids` | string[] (optional) | Optimistic-send correlation tokens carried by an accepted user row; never idempotency keys |
| `tool_call_id` | string (tool only) | Provider correlation id for the corresponding assistant call; ids may be reused across turns |
| `tool_name` | string (tool only) | Function name for the tool result |
| `event_id` | integer (optional) | Accepted SSE row identity used for replay deduplication |
| `is_error` | bool (tool only) | Final error disposition |
| `effect_status` | string (optional) | Persisted effect disposition when known |
| `preview` | object (optional) | Content-addressed preview descriptor; does not contain preview bytes |
| `reasoning`  | string (optional) | Concatenated reasoning / chain-of-thought text on assistant turns whose `provider_data` carried reasoning-bearing blocks (Anthropic `thinking`, OpenAI Responses `reasoning`, or synthetic `reasoning_text` from local-model servers). Present only when the active model's `surface_persisted_reasoning` flag is True. |

Each entry in `tool_calls`:

| Field       | Type   | Description                        |
|-------------|--------|------------------------------------|
| `name`      | string | Function name (e.g. `"bash"`)      |
| `arguments` | string | JSON-encoded argument string       |

#### Streaming events

After the synthetic replay or cursor delta, the server streams real-time events
as the model generates a response:

Typed accepted-user projection is capability-gated. Add `?user_turn=1` to every
per-workstream SSE URL to receive `user_turn`; the embedded panes and both SDKs
do this automatically. A raw client that omits the capability receives a
`replay_truncated` frame with reason `user_turn_projection_unsupported`, whose
SSE id is anchored immediately before the unrepresented row. It must fetch and
render `/history` before reconnecting. This backward-compatible repair frame
does not expose the row content, and a failed history fetch must retain the
pre-row cursor so the repair signal repeats.

Final accepted-tool projection is separately capability-gated. Browser panes
add `tool_turn=1` to every per-workstream SSE URL, including every manual and
native reconnect. A capable listener receives a second `tool_result` with
`accepted: true`, the row's `_event_id`, and the final scalar text, error,
preview, and effect fields that entered accepted history. Reducers replace the
earlier executor receipt in place. A client that omits `tool_turn=1` receives a
redacted `replay_truncated` frame with reason
`tool_turn_projection_unsupported`, anchored at the cursor immediately before
the accepted row, and must rebuild from `/history`.

This accepted projection is a transcript-consistency mechanism, not a wire
confidentiality boundary. The preliminary `tool_result` is deliberately sent
as soon as execution completes and can precede post-execution output transforms;
do not treat `accepted: true` as proof that earlier frames contained the same
text.

**`user_turn`** -- the canonical accepted user row. Every upgraded listener on
the shared workstream receives the event, including peer browsers, so peers can
render the turn without refetching all history. The originating pane uses
`client_send_ids` only to replace or mark its exact optimistic bubble; peers
render the row once by SSE event id. Reusing a client token still admits and
emits a distinct turn.

```json
{
  "type": "user_turn",
  "ws_id": "abc123",
  "content": "Inspect this file",
  "attachments": [
    {
      "attachment_id": "a1",
      "kind": "text",
      "filename": "notes.txt",
      "mime_type": "text/plain"
    }
  ],
  "sender": "user-123",
  "client_send_ids": ["browserSend_42"],
  "_event_id": 17
}
```

`client_send_ids` is empty for callers that did not provide a correlation
token. `_event_id` is the accepted row's monotonic SSE identity and is the
deduplication key; `client_send_ids` is not. Correlation tokens are not
credentials. When both identities are known, an upgraded pane settles a local
optimistic bubble only when the event's `sender` matches that viewer; peer rows
still render canonically without touching local optimistic state.

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

**`state_change`** -- the worker thread transitioned to a new state. Drives
the client's busy-mode (composer in send vs. stop, spinner indicators,
auto-focus on idle). Sent live during normal operation AND on every fresh
SSE subscribe (so a mid-stream page refresh restores the correct composer
state without waiting for the next live transition).

```json
{"type": "state_change", "state": "running"}
```

| Field    | Type   | Description                                                          |
|----------|--------|----------------------------------------------------------------------|
| `state`  | string | One of `"running"`, `"thinking"`, `"attention"`, `"idle"`, `"error"` |

**`in_progress_snapshot`** -- one-shot replay of the in-progress turn's
content + reasoning text-so-far when this client connects mid-stream.
Lets a refreshing browser tab restore partial assistant text immediately
instead of waiting for the response to complete. Yielded once after the
kind-specific replay preamble and pending-cycle snapshot, only when at least one
of `content` / `reasoning` is non-empty. Both halves render into the same
assistant bubble the live `content` / `reasoning` events would target;
clients should treat the snapshot as idempotent (skip overwrite if the
current local buffer is already a superset prefix — covers EventSource
auto-reconnect re-replays).

```json
{
  "type": "in_progress_snapshot",
  "content": "Here is the answer so far: it depends on ",
  "reasoning": "The user is asking about a comparison; let me think about..."
}
```

| Field        | Type   | Description                                                |
|--------------|--------|------------------------------------------------------------|
| `content`    | string | Joined assistant content text accumulated this turn        |
| `reasoning`  | string | Joined reasoning / chain-of-thought text accumulated       |

**`agent_context`** -- latest prompt usage for one running `task_agent`, sent
after each of that agent's model turns when the provider reports usage. The
parent call ID targets the existing task-agent card; clients should replace the
prior reading for that parent rather than append a new badge. No event is sent
when usage is unavailable. A matching `tool_result` (`call_id` equals
`parent_call_id`) is the terminal signal: discard the reading then. Fresh and
truncated recovery rebuild the active set from the synthetic readings they
include, so an omitted parent is no longer active; completed readings are not
persisted in conversation history.

```json
{
  "type": "agent_context",
  "parent_call_id": "call_task_abc123",
  "prompt_tokens": 41000,
  "context_window": 128000
}
```

| Field             | Type   | Description                                      |
|-------------------|--------|--------------------------------------------------|
| `parent_call_id`  | string | Parent `task_agent` call whose card owns the badge |
| `prompt_tokens`   | int    | Prompt tokens used by the agent's latest model turn |
| `context_window`  | int    | Resolved context window for the agent's model lane |

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
client must respond via `POST /v1/api/workstreams/{ws_id}/approve`. Parallel
task agents can leave several approval rounds pending on one workstream at the
same time, so clients should echo the event's `cycle_id` (or one member
`call_id`) when resolving it.

```json
{
  "type": "approve_request",
  "cycle_id": "cycle_789",
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

`cycle_id` identifies this approval round. It is stable across reconnect
replay and is also carried by the corresponding `approval_resolved` event.

**`approval_resolved`** -- one identified approval cycle was answered. Clients
use `cycle_id` (or `call_ids`) to dismiss only that prompt when several remain
live.

```json
{
  "type": "approval_resolved",
  "cycle_id": "cycle_789",
  "call_ids": ["call_def456"],
  "approved": true,
  "feedback": "",
  "always": false
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

**`tool_result`** -- output from a completed tool execution. The first event is the executor receipt. With `tool_turn=1`, a later event carrying `accepted: true` is the canonical accepted-history replacement and includes `_event_id`; `preview` and `effect_status` are present when persisted. The `call_id` matches the corresponding `tool_info`/`approve_request` item and any preceding `tool_output_chunk` events, but clients must scope reused ids to the newest rendered tool batch. For bash tools, the receipt arrives after all streaming chunks and includes both stdout and stderr. The `is_error` field is `true` when the tool execution failed (e.g. bash exit code >= 2 or signal, file not found, timeout). Exit code 1 is ambiguous (e.g. `grep` no-match) and is not flagged. User denials are tracked separately via a `denied` flag. Clients should use `is_error` instead of text-prefix heuristics.

```json
{"type": "tool_result", "call_id": "call_abc123", "name": "bash", "output": "file1.py\nfile2.py\n", "is_error": false}
{"type": "tool_result", "accepted": true, "_event_id": 42, "call_id": "call_abc123", "name": "bash", "output": "file1.py\nfile2.py\n", "is_error": false, "effect_status": "unknown"}
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
  "effort": "medium",
  "cache_creation_tokens": 800,
  "cache_read_tokens": 200
}
```

| Field                    | Type   | Description                                          |
|--------------------------|--------|------------------------------------------------------|
| `prompt_tokens`          | int    | Tokens in the prompt                                 |
| `completion_tokens`      | int    | Tokens generated by the model                        |
| `total_tokens`           | int    | `prompt_tokens + completion_tokens`                  |
| `context_window`         | int    | Total context window size in tokens                  |
| `pct`                    | float  | Percentage of context window used                    |
| `effort`                 | string | Reasoning effort level (`low`/`medium`/`high`)       |
| `cache_creation_tokens`  | int    | Tokens written to prompt cache (Anthropic + OpenAI)  |
| `cache_read_tokens`      | int    | Tokens served from prompt cache (Anthropic + OpenAI) |

**`info`** -- an informational message (e.g. command output).

```json
{"type": "info", "message": "Session cleared."}
```

**`compaction`** -- context-compaction lifecycle. `target` is
`"workstream"` for manual `/compact` and foreground auto-compaction, or
`"task_agent"` for a delegated agent's transient model context. Task-agent
events also carry `parent_call_id`, which keys the existing task card. A
missing `target` means `"workstream"`. `phase: "start"` opens the operation
(`trigger` is `"manual"` or `"auto"`; auto adds `where` — e.g. `"mid-turn"`
— and, when the percentage threshold actually fired, `pct`; the
context-overflow retry path compacts without a `pct` since no threshold was
evaluated).
`phase: "progress"` reports chunked summarization (`part`/`total`/`depth`,
where depth 0 summarizes transcript batches and deeper levels merge partial
summaries), a transient-error retry wait (`retry_in` seconds + `error`), or
`warning: "summary_truncated"`. `phase: "end"` settles it: `ok: true`
carries `before_tokens`/`after_tokens`; workstream ends also carry the produced
`summary`, while task-agent ends deliberately omit it because that summary is
private transient model context;
`ok: false` carries a `reason`
(`"not_enough_messages"` / `"irreducible"` / `"empty_summary"` /
`"cancelled"` / `"error"`) and a human-readable `message`. A workstream
`reason: "error"` end is paired with a typed `error` event, so its `notice`
is false and the end only tears down the compaction card. A task-agent error
has no workstream-level `error` twin: its targeted end carries `notice: true`
so the message renders inside the parent task card. Failed ends always carry
the emitter-computed `notice` verdict; clients display `message` only when it
is true rather than reconstructing policy from `reason`, `trigger`, and
`superseded`. Superseded failures and automatic cancellation stay silent.
Every end (ok or failed) carries `trigger`, and every event carries
`compaction_id` — an
opaque session-local integer correlating the start/progress/end of one
compaction attempt. It is independent of the send generation, so concurrent
task agents and repeated compactions in one turn receive distinct IDs (a client
that force-stopped one compaction can use it to ignore stragglers from the
abandoned run). End events also carry `superseded`: `true` marks
a force-abandoned compaction retiring after a successor generation took
over (an OK end's result card still stands: the history swap happened).
Superseded start/progress events are never emitted.
Exactly one `start` and one `end` are emitted per admitted attempt,
so clients can key an in-progress affordance (progress bar) on the pair. A
successful **workstream** end is also persisted: the summary replays from
`/history` as a
`role: "system"`, `source: "compaction"` entry whose `meta` carries
`{watermark, before_tokens, after_tokens, trigger}` and whose `event_id`
matches the end event's id (dedup across repaint + replay). Task-agent
compaction is nested progress only: fresh/truncated SSE recovery includes its
latest active lifecycle edge, and the matching parent `tool_result` retires it.
Its summary is used only as the task's next private model context: it is absent
from the workstream transcript, persistence, task recall, and every event.

```json
{"type": "compaction", "target": "workstream", "phase": "start", "compaction_id": 7, "trigger": "auto", "where": "mid-turn", "pct": 80}
{"type": "compaction", "target": "workstream", "phase": "progress", "compaction_id": 7, "part": 2, "total": 5, "depth": 0}
{"type": "compaction", "target": "workstream", "phase": "end", "ok": true, "compaction_id": 7, "trigger": "auto",
 "before_tokens": 128400, "after_tokens": 9200, "summary": "## Decisions\n..."}
{"type": "compaction", "target": "task_agent", "parent_call_id": "call_task_abc123",
 "phase": "start", "compaction_id": 8, "trigger": "auto", "where": "mid-turn", "pct": 80}
{"type": "compaction", "target": "task_agent", "parent_call_id": "call_task_abc123",
 "phase": "end", "ok": true, "compaction_id": 8, "trigger": "auto",
 "before_tokens": 115000, "after_tokens": 18000}
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

**`clear_ui`** -- instructs the client to clear displayed messages and re-fetch
history after an identity or transcript-boundary change, including `/clear`,
dedicated rewind/retry, successful fork publication, and opening saved history.

```json
{"type": "clear_ui"}
```

**`history_resync`** -- the history rendered before this stream opened no
longer names the live accepted conversation-row prefix. The server closes the
stream after this event. Keep the current transcript visible, fetch `/history`
again, render the successful response, and open a new stream with its new
one-shot handoff token. A numeric event cursor cannot prove that a complete row
was rendered and is not a substitute for this repair.

```json
{"type": "history_resync", "ws_id": "abc123", "reason": "handoff_mismatch"}
```

`ws_id` is present for registration-time handoff mismatches; on an already
scoped live stream, clients may infer it from the stream when omitted.

`reason` is a free string. `workstream_gone` means the workstream's durable
row was deleted out from under a live session (by another node, or by
startup cleanup); the follow-up `/history` fetch answers 503/404 rather than
minting a new token, and new sends are refused.

**`cancelled`** -- a cancel request was acknowledged (via the Stop button or
`POST /v1/api/workstreams/{ws_id}/cancel`). This signals that cancellation is in
progress, not that it is complete. The worker thread may still be finishing.
Clear any in-progress assistant rendering, but keep the composer disabled until
the workstream emits a terminal `state_change` (`idle` in the normal cancel
path, or `error`). `stream_end` only closes assistant rendering: it may already
have arrived before Stop reaches an approval or tool phase, so it is not a
cancellation-completion signal.

The `cancelled` event is not itself a history row. If cancellation accepts a
partial assistant response or synthesizes tool-result receipts to close
outstanding calls, those assistant/tool rows appear in `/history` and advance
the same handoff prefix.

```json
{"type": "cancelled"}
```

**`intent_verdict`** -- delivered asynchronously when the LLM judge completes
its evaluation of a pending tool call. Only sent when intent validation is
enabled (`judge.enabled` through Admin → Judge or the admin settings API). The
interactive CLI instead uses `--judge` or `[judge] enabled = true`. The
`call_id` correlates with the item in the preceding `approve_request` event.

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
so multiple consumers (browser, console proxy, SDK) can connect
simultaneously and each receives every event.  On reconnect the client receives
either the event-ring delta after its cursor or a synthetic recovery replay.
The synthetic replay includes `connected`, cached `status`, every pending
approval cycle, the current `state_change`, an optional
`in_progress_snapshot` with partial content/reasoning, and the latest
`agent_context` reading and active targeted `compaction` edge for each running
task agent. A matching `tool_result` ends both transient states. Conversation
history stays on the REST `/history` endpoint; completed task-agent readings
and compactions are not retained.

---

### `GET /v1/api/workstreams/{ws_id}/history`

Returns the tail of the reconstructed conversation without opening the
workstream. The endpoint works for a live session and for a saved workstream
that is not loaded in the manager. Cross-kind, tenant, and private-project
visibility checks run before storage reconstruction.

| Query parameter | Type | Default | Description |
|-----------------|------|---------|-------------|
| `limit` | integer | `100` | Tail row limit, clamped to 1--500 |

The response is
`{"ws_id": ..., "messages": [...], "cursor": ..., "handoff_token": ...}`
using the message shape and total-prefix contract documented in the event-stream
bootstrap above. `cursor` is normally `null`; when non-null, pass it as
`last_event_id` on the initial `/events` request. For a loaded workstream, pass
the non-null `handoff_token` from the history just rendered on that same initial
request. A missing, invisible, or wrong-kind workstream returns the endpoint's
ordinary `404` shape.

If the durable prefix cannot be loaded, the endpoint returns:

```json
{"error": "History temporarily unavailable"}
```

Status code: `503`. This response is not authoritative and carries no usable
handoff token. Keep any current transcript, do not open a tokenless replacement
stream, and retry the history read.

---

### `GET /v1/api/events/global`

Opens a Server-Sent Events stream that broadcasts state-change events across
all workstreams. This is used by the tab bar to display per-workstream activity
indicators.

**Events:**

```json
{"type": "ws_state", "ws_id": "abc123", "state": "thinking", "persistence_state": "healthy"}
```

| Field               | Type   | Description |
|---------------------|--------|-------------|
| `ws_id`             | string | Workstream identifier |
| `state`             | string | Current workstream state |
| `persistence_state` | string | Sanitized history-save state: `healthy`, `pending`, `retrying`, or `conflict`; omitted by older nodes means `healthy` |

Possible `state` values:

| State       | Description                                     |
|-------------|-------------------------------------------------|
| `idle`      | No active processing                            |
| `thinking`  | Model is generating a response                  |
| `running`   | Tool execution in progress                      |
| `attention` | Waiting for user input (approval or plan review)|
| `error`     | An error occurred                               |

**Fan-out pattern:** Each connected client receives its own bounded queue
(`maxsize=1000`). A dedicated fan-out thread reads from the shared global queue
and copies each event to every client queue. If a client queue is full, the
event is silently dropped for that client.

**Keepalive:** Same as `/v1/api/workstreams/{ws_id}/events` -- an SSE comment every 5 seconds.

---

### `GET /v1/api/workstreams`

Returns a list of all active workstreams.

**Response:**

```json
{
  "workstreams": [
    {"ws_id": "abc123", "name": "default", "state": "idle", "persistence_state": "healthy"},
    {"ws_id": "def456", "name": "hacker-news", "state": "thinking", "persistence_state": "retrying"}
  ]
}
```

Each workstream object:

| Field               | Type   | Description |
|---------------------|--------|-------------|
| `ws_id`             | string | Unique workstream routing identifier |
| `name`              | string | Display name (alias if set, otherwise `ws-xxxx`) |
| `state`             | string | Current state (see state values above) |
| `persistence_state` | string | Sanitized history-save state; defaults to `healthy` for older or unloaded rows |

The persistence state intentionally carries no retry counts, timestamps,
storage errors, commit keys, or conversation content. `pending` means an
accepted row awaits its first durable save, `retrying` means automatic repair is
active, and `conflict` requires operator intervention.

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

### `GET /v1/api/skills`

Returns a summary list of all available skills. This is a read-only
endpoint (requires `read` scope) that exposes skill names and categories
without revealing skill content. Useful for populating skill selectors
in UIs or discovering available skills before creating a workstream.

**Response:**

```json
{
  "skills": [
    {"name": "safety-guidelines", "category": "safety", "is_default": true, "origin": "manual"},
    {"name": "mcp__server__code", "category": "", "is_default": false, "origin": "mcp"}
  ]
}
```

Each skill summary:

| Field        | Type   | Description                                          |
|--------------|--------|------------------------------------------------------|
| `name`       | string | Skill name (used in `skill` field on workstream creation) |
| `category`   | string | Skill category                                       |
| `is_default` | bool   | Whether skill is auto-applied to all sessions        |
| `origin`     | string | Skill origin: `manual` or `mcp`                      |

> **Note:** For full skill management (create, update, delete, view content),
> use the admin endpoints at `GET /v1/api/admin/skills` (requires `admin.skills` permission).

---

### `GET /v1/api/personas`

Returns the enabled personas offered by the workstream-creation pickers.
Authenticated for any logged-in user and deliberately gated by **no**
`persona.*` permission — selecting a persona at creation is a user
action, while the `persona.*` perms gate authoring. Display fields only;
the levers (base prompt, tool set, MCP/memory toggles) stay server-side.

**Response:**

```json
{
  "personas": [
    {"name": "engineer", "display_name": "Engineer", "description": "The stock interactive workstream: full tools, MCP, and memory.", "applies_to_kinds": ["interactive"], "is_default": true},
    {"name": "researcher", "display_name": "Researcher", "description": "Answers questions with evidence — reads and cites, loads tools to verify when needed.", "applies_to_kinds": ["interactive"], "is_default": false}
  ],
  "total": 2
}
```

Each persona summary:

| Field              | Type   | Description                                                      |
|--------------------|--------|------------------------------------------------------------------|
| `name`             | string | Persona slug (used in the `persona` field on workstream creation) |
| `display_name`     | string | Human-readable label for pickers                                 |
| `description`      | string | Short description of the persona's intent                        |
| `applies_to_kinds` | array  | Workstream kinds the persona applies to (`interactive` / `coordinator`) |
| `is_default`       | bool   | Whether this is the default persona for its kind                 |

> **Note:** For full persona management (create, edit, archive), use the
> admin endpoints at `/v1/api/admin/personas` (requires the
> `persona.{create,read,write}` permissions).

---

### `POST /v1/api/workstreams/{ws_id}/send`

Sends a user message to a workstream. Spawns a daemon worker thread that calls
`session.send()` and streams results back via the SSE channel.

**Path parameters:**

| Parameter | Type   | Required | Description          |
|-----------|--------|----------|----------------------|
| `ws_id`   | string | yes      | Target workstream ID |

**Request body:**

```json
{"message": "Explain how the server works", "attachment_ids": ["a1"], "client_send_id": "browserSend_42"}
```

| Field            | Type       | Required | Description                                          |
|------------------|------------|----------|------------------------------------------------------|
| `message`        | string     | yes      | The user's message text                              |
| `attachment_ids` | string[]   | no       | Staged uploads to attach (omit = auto-consume; `[]` = none) |
| `client_send_id` | string     | no       | Opaque optimistic-UI correlation token matching `[A-Za-z0-9_-]{1,128}`; echoed in `user_turn` and history, never used for idempotency |

The token correlates delivery only. Reusing the same value does not collapse or
deduplicate requests: each accepted send remains a distinct history row and
`user_turn` event. A live `message_queued` event carrying the token can prove
server acceptance before the POST response arrives, including when that HTTP
acknowledgement is lost.

**Response.** Every 200 body carries `attached_ids` and
`dropped_attachment_ids` (empty lists when no attachments are involved):

- `{"status": "ok", ...}` — a fresh turn was dispatched.
- `{"status": "queued", "priority", "msg_id", ...}` — folded into the live
  turn's interjection queue; delivered at the next tool-result seam.
  `DELETE .../send` with the `msg_id` retracts it before delivery.
- `{"status": "queued", "deferred": true, ...}` — parked on the deferred-send
  list (a command window holds the slot, or earlier deferred sends are
  pending) and dispatched as its own full-fidelity send afterwards; see the
  defer contract under `POST /v1/api/command`.
- `{"status": "queue_full", ...}` — the send was refused with retry-shortly
  semantics: the live worker's interjection queue is at capacity, the
  deferred-send list hit its saturation bound (10 pending — the same
  backpressure contract), or the deferred-send drain could not be started
  under resource exhaustion (the message was **not** accepted; nothing is
  parked).
- `{"status": "attachments_busy", ...}` — attachments can't ride a queued
  turn; the staged uploads survive for a retry once the worker idles.

**Error responses:**

| Status | Body                                            | Condition                              |
|--------|-------------------------------------------------|----------------------------------------|
| 400    | `{"error": "message is required"}`              | Message is empty                       |
| 400    | `{"error": "client_send_id must match ..."}`    | Correlation token is invalid           |
| 404    | `{"error": "Unknown workstream"}`               | `ws_id` not found (or closed mid-send) |
| 409    | `{"status": "cross_user_interjection", ...}`    | Another participant's turn is in flight |

---

### `POST /v1/api/workstreams/{ws_id}/approve`

Responds to a tool approval request. The SSE stream must have previously sent
an `approve_request` event for the given workstream.

**Path parameters:**

| Parameter | Type   | Required | Description          |
|-----------|--------|----------|----------------------|
| `ws_id`   | string | yes      | Target workstream ID |

**Request body:**

```json
{
  "approved": true,
  "feedback": null,
  "always": false,
  "cycle_id": "cycle_789"
}
```

| Field      | Type        | Required | Description                                                   |
|------------|-------------|----------|---------------------------------------------------------------|
| `approved` | bool        | yes      | `true` to approve, `false` to deny                            |
| `feedback` | string/null | no       | Optional feedback text (sent as denial reason)                 |
| `always`   | bool        | no       | If approved, remember this round's tool names for this session |
| `cycle_id` | string      | no       | Resolve this exact approval round                              |
| `call_id`  | string      | no       | Resolve the round containing this tool call                    |

When `always` is `true` and `approved` is `true`, the workstream's WebUI
adds the tool names from the resolved round to its per-tool auto-approve set.
It does not enable blanket approval for unrelated tools.

Use `cycle_id` when possible. `call_id` is useful for a UI organized around
individual tool rows. If neither selector is supplied, the oldest unresolved
round is selected for compatibility with older clients. A selector that no
longer matches returns `409` with the current oldest `current_cycle_id` and
`current_call_id`; the server never silently redirects a stale click to another
round.

**Response:**

```json
{"status": "ok", "cycle_id": "cycle_789"}
```

`cycle_id` is `null` if no pending round was resolved. An invalid workstream
returns `404`; a stale `cycle_id` or `call_id` returns `409`.

---

### `POST /v1/api/command`

Executes a slash command in the given workstream. Commands run on the
workstream's worker slot (mutual exclusion against sends, a running
compaction, and each other) — the endpoint is **not** unconditionally
synchronous:

- **Quick commands** (everything except `/compact`): the endpoint waits for
  completion, so `{"status": "ok"}` means the command ran. A command still
  running after 25 s answers `{"status": "running"}` — the worker keeps
  going, its output reaches the pane via SSE, and the post-command pane
  refreshes below still fire when it completes. (The bound sits under
  common 30 s client/proxy timeouts — the console proxy's included — so
  the degraded answer actually reaches bounded callers.)
- **`/compact`**: dispatched fire-and-forget — `{"status": "ok"}` means the
  compaction *started*. A large context can legitimately compact for many
  minutes; progress streams as `compaction` SSE events (see the event
  reference) and the persisted marker row lands on completion. Do not read
  `/history` expecting the compacted transcript immediately after the
  response.
- **Busy refusal**: if a turn or another command holds the worker slot, the
  command is refused with HTTP **409** `{"status": "busy", "error": ...}` and
  did **not** run. Retry after the current turn finishes. (The old inline
  endpoint executed commands unconditionally mid-turn; the 409 makes the
  refusal loud for callers that only check the HTTP status.)

While a command holds the slot — and afterwards, while earlier deferred
sends are still waiting (the pending list is the order authority: a fresh
send never overtakes a message already acknowledged) — `POST .../send`
requests are **deferred**: the server answers `{"status": "queued",
"deferred": true, "msg_id": ...}` immediately and dispatches the message
as an ordinary full-fidelity send (attachments and sender identity
included) in arrival order once the slot frees — it is never routed
through the mid-turn interjection queue (no length cap, no cross-user
rejection). The response arrives within normal round-trip time, so
timeout-bounded clients (SDKs, proxies, the coordinator) need no special
handling. To retract a deferred send before it dispatches, issue the same
`DELETE .../send` with its `msg_id` used for queued interjections —
`{"status": "removed"}` confirms it will not dispatch; `"not_found"` means
it already dispatched (or is dispatching). Retracting a deferred send
discards any attachments it carried; re-attach to send them again. When a
deferred send dispatches, panes receive a `message_dispatched` event
(`msg_id`, plus `folded: true` when it folded into a live turn's
interjection queue rather than spawning its own turn) so queued-message
UI can settle the right way.

Durability: deferred sends are **node-local and in-memory** (the same
lifetime as the interjection queue). `"queued"` is at-most-once intake, not
durable acceptance — if the workstream is closed or the node restarts before
the window ends, the message is dropped. Anything that must survive a
restart should be re-sent after confirming dispatch (the turn appears on the
SSE stream / in `/history`).

**Request body:**

```json
{"command": "/clear", "ws_id": "abc123"}
```

| Field     | Type   | Required | Description                        |
|-----------|--------|----------|------------------------------------|
| `command` | string | yes      | The slash command (e.g. `/clear`)  |
| `ws_id`   | string | yes      | Target workstream ID               |

`/clear` pushes a `clear_ui` SSE event to instruct the client to reset its
message display and re-fetch the transcript via `GET .../history` (there is no
SSE event that carries the messages themselves). The follow-up is emitted by
the command worker itself, so it fires even when the endpoint already answered
`{"status": "running"}`.

The remote command surface deliberately rejects lifecycle helpers
`/new`, `/workstreams`, `/resume`, and `/delete`; those commands are local-CLI
only because their legacy implementations enumerate or mutate storage without
the HTTP tenancy gates. Remote callers must use the dedicated create, open,
fork (`resume_ws` on create), close, and delete endpoints. `/rewind` and
`/retry` likewise use their path-keyed endpoints rather than `/command`.

**Response:**

```json
{"status": "ok"}
```

or `{"status": "running"}` as above.

**Error responses:**

| Status | Body                                | Condition                                        |
|--------|-------------------------------------|--------------------------------------------------|
| 400    | `{"error": "Empty command"}`        | Command is empty                                 |
| 404    | `{"error": "Unknown workstream"}`   | `ws_id` not found                                |
| 409    | `{"status": "busy", "error": ...}`  | A turn/command holds the worker                  |
| 503    | `{"status": "error", "error": ...}` | The command worker could not be started (resource exhaustion) — the command did **not** run; retry shortly |

---

### `POST /v1/api/workstreams/{ws_id}/cancel`

Cancels the workstream's current generation. Stop propagates to the primary or
fallback model stream, model-backed attachment processing, parallel task-agent
and foreground tool model calls, intent and output-guard judges, tracked bash
subprocesses, and every approval cycle owned by that generation. Pending plan
review is rejected as well. The worker preserves any assistant content already
streamed and synthesizes honest cancelled tool results where needed so the
saved conversation remains replayable.

The cooperative response is immediate: `status: ok` acknowledges the request,
not completion. A running workstream emits `cancelled`, then transitions to
`idle` after its worker unwinds. Depending on where Stop arrived,
`stream_end` may have been emitted before the cancel request or may arrive while
the worker is unwinding; clients use the terminal `state_change`, not
`stream_end`, to become ready. An idle cancel is a harmless no-op and emits no
misleading cancellation event. Detached background shells and watches are
independent resources and are not stopped by this endpoint.

**Force cancel:** When `force` is `true`, the server releases the stuck worker
slot immediately, emits `stream_end`/`idle`, and lets a successor turn start.
The abandoned daemon still owns its already-started external effects until it
reaches a cancellation checkpoint. Send/model generations are fenced from late
history and UI publication, but quick slash-command workers do not yet have
generation checkpoints and may finish an in-place mutation concurrently with a
successor. Use force cancel only when cooperative cancellation has not resolved
within a few seconds — it is recovery from a wedged worker, not confirmation
that every in-flight external side effect was rolled back.

**Path parameters:**

| Parameter | Type   | Required | Description          |
|-----------|--------|----------|----------------------|
| `ws_id`   | string | yes      | Target workstream ID |

**Request body:**

```json
{"force": false}
```

| Field  | Type   | Required | Description          |
|--------|--------|----------|----------------------|
| `force`| bool   | no       | Abandon stuck worker immediately (default: `false`) |

The body is optional. Because cancel is a recovery verb, an empty or malformed
JSON body is treated as `force: false` rather than blocking Stop.

**Response:**

```json
{
  "status": "ok",
  "dropped": {
    "was_running": true,
    "pending_approval": {"tool_names": ["bash"], "call_id": "call_abc123"},
    "queued_messages": {"count": 1, "first_preview": "follow up after the build"}
  }
}
```

`dropped` is a best-effort, credential-redacted snapshot of affected pending
work. Fields are omitted when they were not observable. Coordinator sessions
currently return an empty object.

**Error responses:**

| Status | Body                               | Condition              |
|--------|------------------------------------|------------------------|
| 400    | `{"error": "No session"}`          | Session not initialized|
| 404    | `{"error": "Unknown workstream"}`  | `ws_id` not found      |

---

### `POST /v1/api/workstreams/new`

Creates a new workstream, subject to the configured
`server.max_workstreams` capacity.

The endpoint accepts **either** `application/json` (legacy shape) **or**
`multipart/form-data` when you want to upload attachments at creation
time.  Multipart requests carry one `meta` field containing the JSON body
shown below plus zero-or-more `file` parts; each file is validated and
reserved onto the new workstream's first turn before the dispatch worker
runs, so queued multimodal turns cannot lose files to racing sends.  If
validation fails the fresh workstream is rolled back so no published row or
phantom create/close event leaks.

**Request body:**

```json
{"name": "my-ws", "model": "openai", "initial_message": "Start the review"}
```

All fields are optional; send an empty JSON object for a defaults-only create.
An absent or malformed JSON body returns `400`.

| Field             | Type          | Default | Description                                                    |
|-------------------|---------------|---------|----------------------------------------------------------------|
| `name`            | string        | auto    | Workstream display name                                        |
| `model`           | string        | default | Model alias from the registry                                  |
| `auto_approve`    | bool          | false   | Auto-approve all tool calls for this workstream                |
| `auto_approve_tools` | string/array | `""` | Tool names to auto-approve even when `auto_approve` is false; accepts comma-separated text or an array |
| `user_id`         | string        | `""`  | Owner override honored only for a trusted `console` service identity carrying the `service` scope; ordinary callers remain bound to their authenticated identity |
| `resume_ws`       | string        | `""`    | Source workstream ID or alias to fork atomically into this new ID |
| `resume_ws_exact` | bool          | false   | Require `resume_ws` to match an exact source ID; never resolve an alias or prefix |
| `required_node_id` | string/null | none | Require execution on this node. Omission inherits the fork source's requirement; a fresh direct create without it stays flexible. |
| `skill`           | string        | `""`    | Skill name. Applies its system prompt and session configuration. Returns 400 if missing/disabled; ignored for a fork because the source configuration is cloned. |
| `persona`         | string        | `""`    | Persona slug; empty selects the kind's default. A fork keeps the source persona. |
| `judge_model`     | string        | `""`    | Optional judge model alias                                     |
| `initial_message` | string        | `""`    | First user message to dispatch after publication               |
| `ws_id`           | 32-hex string | generated | Caller-selected destination ID; required by the cluster multipart routing path |
| `project_id`      | string/null   | none    | Project to attach. A fork always inherits the source's effective project. |
| `notify_targets`  | string/array  | `[]`    | Completion-notification targets                                |
| `client_type`     | string        | `web`   | Client surface label (`web`, `cli`, `chat`, or `scheduled`)    |
| `parent_ws_id`    | string/null   | none    | Owning coordinator ID for a coordinator-spawned child          |

A deleted workstream ID remains reserved while a channel route references it.
Creating a new workstream with that ID returns `409`; channel recovery forks or
starts a conversation under a new ID before updating the association.

> **Skill behavior:** When `skill` is specified, the skill's content is injected as a system message and its session config fields (model, temperature, auto-approve, token budget, etc.) override system defaults for the new workstream.

#### Fork behavior (`resume_ws`)

Despite the compatibility field name, `resume_ws` does not reopen or move the
source workstream. It creates a distinct destination ID and atomically clones
the source's checkpoint-bounded conversation, saved session configuration,
persona, effective project, and attachment references. The source remains
unchanged. Use `POST .../{ws_id}/open` when you want to rehydrate the original
ID instead.

An explicit `required_node_id` applies to the new destination and can differ
from the source requirement. Omission (including `null`) inherits it. A
same-ID reopen never changes the requirement. The receiving node returns
`409` with `code: "wrong_execution_node"` and `required_node_id` before
constructing executable state if it is not the required node. Node IDs accept
1–256 letters, digits, dots, underscores, and hyphens; empty requirements are
invalid. Requirements survive restart, soft close, idle timeout, and eviction.

Set `resume_ws_exact: true` when recovering a stored canonical ID. A missing
exact source returns `404` even if another workstream has that ID as its alias.
The console preserves this requirement after resolving an ordinary alias, so
the node cannot substitute another source if the original disappears in transit.

The clone transaction rechecks source visibility, private-project membership
and attachability, persona/project construction context, destination ownership
and emptiness, and attachment integrity. A caller cannot use `project_id` to
re-file or declassify the fork. Uploads cannot be combined with `resume_ws`;
fork first, then use the ordinary attachment endpoint. Concurrent source-history
writes serialize wholly before or after the clone snapshot; access,
construction-context, or destination conflicts fail the whole fork rather than
publishing a mixed result.

#### Publication and rollback

Creation first reserves the ID durably with internal state `creating`. That
reservation is hidden from list, saved, resolve, open, and cluster-event
surfaces while the session is constructed, uploads are validated, and an
optional fork transaction commits. The final durable `creating` to `idle`
compare-and-set happens before `ws_created`, audit, initial-message dispatch,
or any state event. A normal pre-publication failure immediately and
conditionally deletes the exact token-bearing reservation and emits no
lifecycle event; if cleanup itself fails, the original HTTP error is retained
and `ws.create.rollback_failed` is logged, leaving the row hidden rather than
advertising a half-create.

Long-lived server and console processes also run hidden-reservation recovery at
boot and every five minutes, independently of ordinary idle eviction. It only
considers rows still in internal `state='creating'` and older than two hours,
excluding IDs currently loaded or pending in the manager. A live remote owner
protects its rows; the current process's stable node ID does not self-protect,
so a restart can recover its predecessor's residue. Failure to establish
service liveness, or a storage failure, deletes nothing. Eligible rows are
atomically hard-deleted with their dependent records and attachment refcounts;
an eligible tokenless legacy or corrupt reservation is locked, recovered, and
logged as a warning. Retention pruning leaves `creating` rows to this path. The
value is not a live `WorkstreamState`, and recovery neither publishes nor
closes it.

**Response (success):**

```json
{
  "ws_id": "ghi789",
  "name": "ws-3",
  "resumed": false,
  "message_count": 0,
  "attachment_ids": []
}
```

| Field           | Type   | Description                                         |
|-----------------|--------|-----------------------------------------------------|
| `ws_id`         | string | Unique ID of the new workstream                     |
| `name`          | string | Assigned workstream display name                    |
| `resumed`       | bool   | Whether the requested source was successfully forked |
| `message_count` | int    | Messages cloned into the destination (0 if fresh/empty) |
| `attachment_ids` | string[] | Attachments saved by this create request          |
| `initial_message_status` | string | Present ONLY when the workstream was created but its `initial_message` could not be delivered: `"queue_full"` (a raced live worker's interjection queue was at capacity — resend via `/send`; any uploads stay staged) or `"refused_closed"` (the workstream was closed mid-create). Absent whenever the message was dispatched. |

For compatibility, `resumed: true` means the requested fork completed; the
source was not resumed in place.

**Selected errors:**

| Status | Condition |
|--------|-----------|
| 400 | Invalid body/upload/persona/skill, attachments combined with `resume_ws`, or required project missing |
| 403 | Destination project attach denied |
| 404 | Fork source missing or not visible (same shape prevents an existence oracle) |
| 409 | Caller-selected ID collision, source availability/construction context changed during fork, or destination reservation was superseded |
| 413 | Upload exceeds the configured request/file cap |
| 429 | Workstream manager is at capacity; retry after capacity frees |
| 503 | Storage/factory/model configuration unavailable, or the fork transaction failed operationally |
| 500 | Unexpected create failure; response includes a correlation ID for server logs |

---

### `POST /v1/api/workstreams/{ws_id}/close`

Closes and removes a workstream. The last remaining workstream cannot be
closed.

**Path parameters:**

| Parameter | Type   | Required | Description            |
|-----------|--------|----------|------------------------|
| `ws_id`   | string | yes      | Workstream ID to close |

**Request body:**

The body must be valid JSON. If you are not supplying any optional
fields, send `{}` — an empty / non-JSON body is rejected with a
`400`.

| Field    | Type   | Required | Description                                              |
|----------|--------|----------|----------------------------------------------------------|
| `reason` | string | no       | Optional close reason persisted to `workstream_config`.  |

The `reason` is capped at **512 UTF-8 bytes** (multibyte-safe — the
cap holds for CJK and emoji payloads), and the output guard's
credential-redaction pass strips secrets before the value is
persisted. A non-string `reason` is silently coerced to empty and
the close proceeds without writing the field.

**Response (success):**

```json
{"status": "ok"}
```

**Error (last workstream):**

```json
{"error": "Cannot close last workstream"}
```

Status code: `400`

**Error (conversation persistence unresolved):**

```json
{"error": "workstream has unresolved persistence"}
```

Status code: `409`. At least one accepted live conversation row still requires
idempotent persistence reconciliation. The workstream remains loaded and no
history is discarded; retry the close after storage recovers.

---

### `POST /v1/api/workstreams/{ws_id}/attachments`

Upload an image or text document and attach it to the caller's next user
turn on this workstream.

- Images (png/jpeg/gif/webp) are capped at **4 MiB** and validated via
  magic-byte sniff on upload.
- Text documents (any `text/*` MIME, allow-listed application MIMEs, or
  known text extensions) are capped at **512 KiB** and must be UTF-8.
- Per-(workstream, user) pending cap is **10** attachments.

The attachment moves through three states: `pending → reserved →
consumed`.  Reservation tokens are threaded through
`POST /v1/api/workstreams/{ws_id}/send` so a queued multimodal turn cannot lose its file to
an overlapping send.

Ownership failures are masked as `404` so non-owners cannot enumerate
workstream existence.

**Content-Type:** `multipart/form-data` with a single `file` field.

**Response (success):** `200`

```json
{
  "attachment_id": "att_abc123",
  "kind": "image",
  "mime_type": "image/png",
  "size_bytes": 73240,
  "filename": "screenshot.png",
  "state": "pending"
}
```

**Errors:**

| Code | Meaning                                                 |
|------|---------------------------------------------------------|
| 400  | Missing/invalid form, unsupported MIME, not UTF-8, etc. |
| 403  | Auth/scope failure                                      |
| 404  | Workstream not found / not owned by caller              |
| 409  | Pending-cap reached                                     |
| 413  | Payload exceeds size cap                                |

---

### `GET /v1/api/workstreams/{ws_id}/attachments`

List the caller's **pending** (unconsumed) attachments for this
workstream.  Ownership failures are masked as `404`.

**Response:** `200`

```json
{
  "attachments": [
    {
      "attachment_id": "att_abc123",
      "kind": "image",
      "mime_type": "image/png",
      "size_bytes": 73240,
      "filename": "screenshot.png",
      "state": "pending"
    }
  ]
}
```

---

### `GET /v1/api/workstreams/{ws_id}/attachments/{attachment_id}/content`

Returns the raw bytes of an attachment with its stored `Content-Type`.
Useful for previewing an image or replaying a document.  Ownership
failures are masked as `404`.

**Response:** `200` — binary body, original `Content-Type`.

---

### `DELETE /v1/api/workstreams/{ws_id}/attachments/{attachment_id}`

Remove a pending attachment.  Consumed attachments return `404` (they
are part of a committed conversation turn).  Ownership failures are also
masked as `404`.

**Response:** `200`

```json
{"deleted": "att_abc123"}
```

---

### `POST /v1/api/workstreams/{ws_id}/delete`

Permanently delete a saved workstream and all its messages from storage.

**Path parameters:**

| Parameter | Type   | Description          |
|-----------|--------|----------------------|
| `ws_id`   | string | Workstream ID        |

**Response (success):** `200`

```json
{"deleted": "a1b2c3d4"}
```

**Response (not found):** `404`

```json
{"error": "Workstream not found"}
```

---

### `POST /v1/api/workstreams/{ws_id}/open`

Load a saved workstream into memory with its original `ws_id`. If the
workstream is already loaded, returns immediately with `already_loaded: true`.
An internal `creating` reservation is not openable and returns the ordinary
not-found shape until publication completes.

**Path parameters:**

| Parameter | Type   | Description          |
|-----------|--------|----------------------|
| `ws_id`   | string | Workstream ID        |

**Response (success):** `200`

```json
{"ws_id": "a1b2c3d4", "name": "refactor"}
```

**Response (already loaded):** `200`

```json
{"ws_id": "a1b2c3d4", "name": "refactor", "already_loaded": true}
```

---

### `POST /v1/api/workstreams/{ws_id}/title`

Set a workstream title manually. The title is stored as the workstream alias.

**Path parameters:**

| Parameter | Type   | Description          |
|-----------|--------|----------------------|
| `ws_id`   | string | Workstream ID        |

**Request body:**

```json
{"title": "JWT Authentication Refactor"}
```

| Field   | Type   | Required | Description            |
|---------|--------|----------|------------------------|
| `title` | string | yes      | New workstream title   |

**Response (success):** `200`

```json
{"status": "ok", "title": "JWT Authentication Refactor"}
```

**Response (conflict):** `409`

```json
{"error": "That name is already used by another workstream"}
```

---

### `POST /v1/api/workstreams/{ws_id}/refresh-title`

Regenerate the workstream title via LLM based on conversation content.

**Path parameters:**

| Parameter | Type   | Description          |
|-----------|--------|----------------------|
| `ws_id`   | string | Workstream ID        |

**Response (success):** `200`

```json
{"status": "ok"}
```

---

### `GET /v1/api/admin/settings`

List `interface.*` settings with their current values and sources. Requires
`read` scope on the server.

**Response:** `200`

```json
{
  "settings": [
    {
      "key": "interface.close_tab_action",
      "value": "last_used",
      "source": "default",
      "type": "str",
      "description": "Determines which workstream to switch to after closing a tab."
    }
  ]
}
```

---

### `POST|PUT /v1/api/admin/settings/{key}`

Update an `interface.*` setting. Only keys in the `interface` section are
accepted; other keys return `400`.

**Path parameters:**

| Parameter | Type   | Description                         |
|-----------|--------|-------------------------------------|
| `key`     | string | Setting key (e.g. `interface.theme`) |

**Request body:**

```json
{"value": "light"}
```

| Field   | Type | Required | Description    |
|---------|------|----------|----------------|
| `value` | any  | yes      | New value      |

**Response (success):** `200`

```json
{"status": "ok", "key": "interface.theme", "value": "light"}
```

**Error:** `400` if the key is not in the `interface` section.

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

List body-free memory metadata with optional filters. Requires `read` scope. Without
`scope`, returns only `global` plus the authenticated caller's `user`
namespace. The public endpoint accepts `global`, `workstream`, and `user`;
explicit workstream access is owner-bound.

**Query parameters:**

| Parameter  | Type   | Required | Default | Description                  |
|------------|--------|----------|---------|------------------------------|
| `type`     | string | no       | `""`    | Filter by memory type (user, general, feedback, reference) |
| `scope`    | string | no       | `""`    | Filter by scope (global, workstream, user) |
| `scope_id` | string | no       | `""`    | Scope qualifier. Auto-resolved for `scope=user` when auth is active. |
| `limit`    | int    | no       | `100`   | Max results (1-200)          |

**Response:**

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

Save or upsert a structured memory. Requires `write` scope. Returns `201` on
create, `200` on update. Every write must include an authored `description`
that normalizes to 1-512 characters on one line; content-only updates are
rejected.

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

The response is body-free; use the exact-name GET endpoint when content is
needed.

**Error responses:**

| Status | Condition                                              |
|--------|--------------------------------------------------------|
| 400    | Invalid input, public scope, scope ID, or limit |
| 403    | Cross-user or non-owner workstream access |
| 404    | Explicit workstream does not exist |
| 500    | Storage mutation failed |

---

### `POST /v1/api/memories/search`

Search body-free memory metadata by query. Uses POST for the request body but is non-mutating
(requires only `read` scope). An omitted scope searches only `global` plus the
authenticated caller's `user` namespace.

**Request body:**

```json
{
  "query": "authentication",
  "type": "general",
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
| `limit`    | int    | no       | `20`    | Max results (1-50)             |

**Response:**

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

**Error:** `400` with `{"error": "query is required"}` if `query` is empty.

---

### `GET /v1/api/memories/{name}`

Fetch one live full memory body by exact name and scope. Requires `read` scope
and records the access. List and search do not update access metadata.

| Parameter  | Location | Required | Default    | Description         |
|------------|----------|----------|------------|---------------------|
| `name`     | path     | yes      | --         | Memory name         |
| `scope`    | query    | no       | `"global"` | Scope of the memory |
| `scope_id` | query    | no       | `""`       | Scope qualifier     |

**Response (success):** `200` -- the full memory schema, including `content`.

**Response (not found):** `404`

```json
{"error": "Memory 'auth_patterns' not found"}
```

---

### `DELETE /v1/api/memories/{name}`

Delete a memory by name and scope. Requires `write` scope. The delete returns
success only for the row atomically removed and records the authenticated
actor in the audit log.

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

List body-free memory metadata across all scopes. Requires `admin.memories`
permission.

**Query parameters:**

| Parameter  | Type   | Required | Default | Description                  |
|------------|--------|----------|---------|------------------------------|
| `type`     | string | no       | `""`    | Filter by type               |
| `scope`    | string | no       | `""`    | Filter by scope              |
| `scope_id` | string | no       | `""`    | Filter by scope ID           |
| `limit`    | int    | no       | `100`   | Max results (capped at 200)  |

**Response:** `200` -- `AdminMemorySummary`, the public body-free metadata plus
`scope_label`, a human-readable label for `scope_id` (empty when there is no
scope ID, with the raw ID as fallback).

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

**Response:** `200` -- the same `AdminMemorySummary` schema as
`GET /v1/api/admin/memories`.

**Error:** `400` with `{"error": "q is required"}` if `q` is empty.

---

### `GET /v1/api/admin/memories/{memory_id}` (Console)

Get a single memory body by ID and record an access. Requires
`admin.memories` permission.

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
the counters include the completed GET touch.

**Error (not found):** `404`

```json
{"error": "Memory not found"}
```

Storage-operation failures return `500`; unavailable storage returns `503`.

---

### `PATCH /v1/api/admin/memories/{memory_id}` (Console)

Replace the authored one-line index hook (1-512 characters) without changing
the memory body. Records `memory.description_update`. Existing immutable
snapshots remain unchanged.

```json
{"description": "Updated retrieval hook"}
```

Returns the updated body-free `AdminMemorySummary`:

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

### `GET /v1/api/admin/memories/index-health` (Console)

Return derived, persistent health for every visibility envelope possible in
the live workstream/project topology. The result is independent of whether a
snapshot row already exists and reports the configured character budget, worst
complete live index, overage, and legacy descriptions that need editing.

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

Calculation failures return `500`; unavailable storage returns `503`.

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

### `GET /v1/api/admin/output-assessments` (Console)

List output guard assessments from the `output_assessments` table. This endpoint
is on the **console** server and requires the `admin.judge` permission.

**Query parameters:**

| Parameter    | Type   | Required | Description                                        |
|--------------|--------|----------|----------------------------------------------------|
| `ws_id`      | string | no       | Filter by workstream ID                            |
| `risk_level` | string | no       | Filter by risk level (`low`/`medium`/`high`)       |
| `since`      | string | no       | ISO timestamp lower bound                          |
| `until`      | string | no       | ISO timestamp upper bound                          |
| `limit`      | int    | no       | Max results (default 100, max 500)                 |
| `offset`     | int    | no       | Pagination offset (default 0)                      |

**Response:**

```json
{
  "assessments": [
    {
      "assessment_id": "a1b2c3d4e5f6",
      "ws_id": "ws-1",
      "call_id": "call_abc123",
      "func_name": "bash",
      "flags": "[\"credential_leak\"]",
      "risk_level": "high",
      "annotations": "[\"API key detected (sk-proj-...)\"]",
      "output_length": 1024,
      "redacted": 1,
      "created": "2026-03-16T10:00:00"
    }
  ],
  "total": 7
}
```

---

### `POST /v1/api/admin/skills/{skill_id}/rescan` (Console)

Re-scan a skill's content for security signals using the current scanner
version. Requires the `admin.skills` permission.

**Path parameters:**

| Parameter  | Type   | Description |
|------------|--------|-------------|
| `skill_id` | string | Skill (prompt template) ID |

**Response:**

```json
{
  "risk_level": "medium",
  "scan_report": "{\"composite\": 1.75, \"details\": {...}}",
  "scan_version": "1"
}
```

**Error:** `404` if skill not found.

---

### `GET /v1/api/admin/skills/discover` (Console)

Search external skill registries for available skills. Requires the
`admin.skills` permission.

**Query parameters:**

| Parameter | Type   | Default | Description |
|-----------|--------|---------|-------------|
| `q`       | string | `""`    | Search query |
| `limit`   | int    | `20`    | Max results (1–100) |

**Response:**

```json
{
  "skills": [
    {
      "id": "owner/repo/skill-name",
      "name": "skill-name",
      "description": "A skill description",
      "author": "Author Name",
      "source": "skills.sh",
      "source_url": "https://github.com/owner/repo",
      "install_count": 42,
      "tags": ["coding", "review"],
      "installed": false
    }
  ]
}
```

**Error:** `502` if the registry is unreachable.

---

### `POST /v1/api/admin/skills/install` (Console)

Install a skill from an external source (skills.sh registry or GitHub).
Requires the `admin.skills` permission.

**Request body:**

```json
{
  "source": "github",
  "url": "https://github.com/owner/skill-repo"
}
```

Or for skills.sh:

```json
{
  "source": "skills.sh",
  "skill_id": "owner/skill-name"
}
```

**Response:** Same as `GET /v1/api/admin/skills/{skill_id}` — the created
skill object.

**Errors:** `400` invalid source or missing fields, `404` SKILL.md not found,
`409` skill already installed (duplicate source_url or name), `502` source
unreachable.

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

### MCP Servers

| Method | Path | Description |
|--------|------|-------------|
| GET | `/v1/api/admin/mcp-servers` | List all MCP server definitions with live node status. Query: `?reveal=true` to show env/header secrets. |
| POST | `/v1/api/admin/mcp-servers` | Create an MCP server definition. Body: `{name, transport, command?, args?, url?, headers?, env?, auto_approve?, enabled?}` |
| GET | `/v1/api/admin/mcp-servers/{server_id}` | Get a single MCP server with per-node connection status. |
| PUT | `/v1/api/admin/mcp-servers/{server_id}` | Update an MCP server definition. Partial updates supported. |
| DELETE | `/v1/api/admin/mcp-servers/{server_id}` | Delete an MCP server definition. |
| POST | `/v1/api/admin/mcp-servers/reload` | Tell all cluster nodes to re-read the `mcp_servers` DB table and reconcile (add new, remove stale, reconnect changed). |
| POST | `/v1/api/admin/mcp-servers/import` | Import servers from a pasted JSON config. Body: `{config: {mcpServers: {...}}}`. Skips existing names. |

Permission: `admin.mcp`

Secrets (`env`, `headers` fields) are masked with `***` by default. Use `?reveal=true` on GET endpoints to see actual values.

---

### MCP Registry

#### Search Registry

`GET /v1/api/admin/mcp-registry/search`

Search the official MCP Registry for available servers. Permission: `admin.mcp`.

**Query parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `search` | string | `""` | Search query. Empty returns a browsable listing. |
| `limit` | integer | `20` | Results per page (max 100). |
| `cursor` | string | — | Opaque cursor for pagination. |

**Response:** `200`

```json
{
  "servers": [
    {
      "name": "io.example/mcp-server",
      "description": "...",
      "title": "Example Server",
      "version": "1.0.0",
      "website_url": "https://example.com",
      "repository": {"url": "...", "source": "github"},
      "icons": [],
      "remotes": [{"type": "streamable-http", "url": "...", "headers": [...], "variables": {...}}],
      "packages": [{"registry_type": "npm", "identifier": "@example/server", "version": "1.0.0", "transport_type": "stdio", "environment_variables": [...]}],
      "meta": {"status": "active", "is_latest": true},
      "installed": false,
      "installed_server_id": "",
      "installed_version": "",
      "update_available": false
    }
  ],
  "total": 100,
  "next_cursor": "abc123"
}
```

**Errors:** `502` (registry unreachable).

#### Install from Registry

`POST /v1/api/admin/mcp-registry/install`

Install an MCP server from the registry. Auto-reloads all cluster nodes. Permission: `admin.mcp`.

**Request body:**

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

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `registry_name` | string | yes | Server name from registry search results. |
| `source` | string | yes | `"remote"` (streamable-http) or `"package"` (npm/pypi). |
| `index` | integer | no (default `0`) | Which remote or package entry to use. |
| `name` | string | no | Custom server name. Auto-derived from registry name if empty. |
| `variables` | object | no | Values for URL template `{var}` placeholders. |
| `env` | object | no | Environment variable values for package servers. |
| `headers` | object | no | Header values for remote servers. |

**Response:** Same as `POST /v1/api/admin/mcp-servers` (McpServerDetail).

**Errors:** `400` (validation), `404` (not in registry), `409` (already installed or name collision), `502` (registry unreachable).

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
| Malformed, absent, or non-object body on an endpoint that requires a JSON object | `400`; cancel is the deliberate recovery-verb exception and treats it as `force: false` |
| Unknown `ws_id`                    | `404` with `{"error": "Unknown workstream"}`               |
| Unknown path (GET or POST)         | `404` with plain-text body `Not found`                     |
| Empty `message` on `/v1/api/workstreams/{ws_id}/send` | `400` with `{"error": "Empty message"}`            |
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

Per-workstream events carry monotonic SSE IDs and are retained in a bounded
ring. Native `Last-Event-ID` and the `?last_event_id=N` query fallback both
resume after the last applied event. If the ring covers the gap, only missing
events are replayed. If it does not, the server emits:

```json
{
  "type": "replay_truncated",
  "ws_id": "abc123",
  "lost_count": 4,
  "earliest_available_id": 91
}
```

The clients then refetch `/history`, adopt its optional resume cursor, and
reconnect; an in-progress snapshot covers partial text on the synthetic path.
This REST snapshot plus cursor/delta split prevents both missing turns and
double-rendering across refreshes, ring eviction, and process restart. The
global state stream has its own snapshot/replay floor rather than conversation
history.

`history_resync` is a stronger repair signal than `replay_truncated`: it means
the one-shot token no longer names the accepted row prefix used for the rendered
history. The server closes that stream. Clients retain the current transcript,
fetch and render `/history` again, then reconnect with the new cursor/token pair;
numeric replay alone is insufficient. If the repair read returns `503`, clients
must keep the repair latched and must not open a cursorless or tokenless stream.

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
turnstone_http_requests_total{method="POST",endpoint="/v1/api/workstreams/{ws_id}/send",status_code="200"} 18
# HELP turnstone_tokens_total Total tokens consumed
# TYPE turnstone_tokens_total counter
turnstone_tokens_total{type="prompt"} 84320
turnstone_tokens_total{type="completion"} 12150
# HELP turnstone_tool_calls_total Total tool executions by name
# TYPE turnstone_tool_calls_total counter
turnstone_tool_calls_total{tool="bash"} 7
turnstone_tool_calls_total{tool="read_file"} 3
```

---

## Console Routing Proxy Endpoints

These endpoints are served by the console (`turnstone-console`) and proxy
requests to the required execution node, or use placement overrides and
rendezvous (HRW) hashing for flexible workstreams. In multi-node deployments, clients (SDK, channel
gateway) talk to the console instead of individual server nodes.

### `POST /v1/api/route/workstreams/new`

Create a workstream through the console routing layer. The JSON body accepts
the ordinary create fields plus `target_node`:

| Field | Routing behavior |
|-------|------------------|
| `ws_id` | Optional 32-hex destination. It is preserved; without an explicit or inherited requirement it is the rendezvous key. A 503 never replaces a caller-selected ID. |
| `resume_ws` | Optional source ID or saved alias for an atomic fork. The console authorizes and canonicalizes the source before routing. Its node requirement is inherited unless explicitly replaced on the new ID. For flexible forks without a destination ID, the source is the placement key. |
| `resume_ws_exact` | Optional boolean, default false. Requires an exact source ID in `resume_ws`; disables alias and prefix resolution. |
| `target_node` | Optional required execution node, including when `ws_id` or `resume_ws` is supplied. |
| `required_node_id` | Same durable requirement as direct create. Must match `target_node` when both are supplied. |

Without any placement field, the console generates a destination ID and routes
it by rendezvous. Multipart callers must pre-allocate the destination and put
the **same** 32-hex value in both `?ws_id=<32-hex>` and the multipart
`meta.ws_id` field. An explicit requirement takes precedence over hashing; the console
buffers the body, parses only `meta` to require the same destination ID, then
forwards the original bytes and boundary unchanged. The node uses `meta.ws_id`
as the destination identity. Metadata-only multipart forks use the common JSON
fork path after parsing, including canonicalization and inherited requirements.
Uploads cannot be combined with a fork; fork first, then upload.

The response extends the node create response with three required fields:
`node_url`, authoritative `node_id`, and `routing_strategy`.
`routing_strategy` is `target_node` for an explicit requirement, `resume` for
inherited affinity or source placement, and `rendezvous` for destination-ID
placement. The node-returned destination `ws_id` is authoritative for the response,
storage binding lookup, and audit record; the fork source is never reported as
the created destination.

The JSON body must be an object. `ws_id`, `resume_ws`, and `target_node` must be
strings when supplied; malformed placement fields return `400`. A missing fork
source returns the same generic `404` as other missing workstreams. If a node
returns `200` without an object containing a valid destination `ws_id`, the
console returns a bounded `502` instead of exposing or trusting the malformed
payload.

A required node missing from live membership returns `503` with
`code: "required_node_unavailable"` and `required_node_id`. An unreachable
registered node can return an upstream `502`. Neither condition permits a
fallback to another node. Ordinary route resolution reads the durable
requirement before cached placement, including after the node disappears.
Private-project requirements follow the same visibility rules as history.

### `GET /v1/api/route/workstreams/{ws_id}/live`

Probe the routed node without opening or rehydrating the
workstream. The console asks that node's manager-authoritative active list and
returns only:

```json
{"ws_id": "abc123", "live": true}
```

Missing, unloaded, still-`creating`, and caller-invisible workstreams all
produce `live: false`. Routing, upstream, and authorization uncertainty returns
an error instead of a false miss, so callers can preserve an existing route.
This includes an unavailable required node: channel recovery retains its
saved association and retries when that node returns.

### `POST /v1/api/route/workstreams/{ws_id}/send`

Proxy a message to the workstream's assigned server node. `DELETE` on the same
path dequeues a queued send.

### `POST /v1/api/route/workstreams/{ws_id}/approve`

Proxy an approval response, including optional `cycle_id` / `call_id`, to the
workstream's assigned server node.

### `POST /v1/api/route/workstreams/{ws_id}/cancel`

Cancel generation on a workstream. The request and response have the same
`force` / `dropped` shape as the node endpoint.

### `POST /v1/api/route/command`

Send a conversation-local slash command. This legacy route still takes
`ws_id` in the JSON body.

### `POST /v1/api/route/workstreams/{ws_id}/{rewind|retry}`

Proxy a dedicated conversation-modification request.

### `POST /v1/api/route/workstreams/{ws_id}/close`

Close a workstream.

The console also exposes path-keyed routed attachment endpoints and
`POST /v1/api/route/workstreams/delete` for coordinator-driven hard deletion.

### `GET /v1/api/route?ws_id=X`

Look up which server node owns a workstream. Returns `{"node_url": "...", "node_id": "..."}`.
Used by channel adapters to open direct SSE connections to the correct server node.

### `GET /metrics` (Console)

Prometheus metrics for the console routing layer. Includes:
`turnstone_router_requests_total`, `turnstone_router_request_duration_seconds`,
`turnstone_router_membership_size`, `turnstone_router_refresh_total`.
