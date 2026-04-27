# Coordinator API tour

Turnstone's **coordinator workstream** is a session hosted on the
console whose job is to orchestrate other workstreams.  It runs an LLM
that can spawn child workstreams on any node, watch their progress,
wait for them to finish, steer them mid-flight, and tear them down.
This doc walks the full lifecycle — one request, one response, and the
relevant SSE events at each step.

Aimed at integrators driving a coordinator from a custom UI or SDK
without reverse-engineering the built-in console page.  The shapes
here match the live OpenAPI spec served at `/openapi.json` and
rendered at `/docs` on every `turnstone-console` process.  Every
step references the operation id from that spec so doc updates track
schema changes.

> **Auth throughout.**  Every endpoint below sits behind bearer-token
> auth and the `admin.coordinator` permission.  A session-scoped JWT
> is minted per login (see [docs/oidc.md](oidc.md) / [docs/security.md](security.md));
> a service token may call the read paths but destructive governance
> paths (`/restrict`, `/stop_cascade`, `/close_all_children`) require
> the explicit `admin.coordinator` grant — a service-token owner
> match isn't enough.

---

## The 9 steps

> **URL convergence (1.5.0).** Pre-1.5 coord-only endpoints lived
> under `/v1/api/coordinator/...`. The Stage 2 verb-shape lift
> consolidated coord and interactive onto the unified
> `/v1/api/workstreams/{ws_id}/<verb>` tree; coord still distinguishes
> itself via the `kind=coordinator` row classifier rather than a
> separate URL space. The endpoints below reflect the post-lift
> surface served by `turnstone-console`.

| # | Action                       | Operation                                                   |
|---|------------------------------|-------------------------------------------------------------|
| 1 | Create                       | `POST /v1/api/workstreams/new`                              |
| 2 | Subscribe to events          | `GET /v1/api/workstreams/{ws_id}/events` (SSE)              |
| 3 | Send a user message          | `POST /v1/api/workstreams/{ws_id}/send`                     |
| 4 | Inspect children             | `GET /v1/api/workstreams/{ws_id}/children`                  |
| 5 | Inspect one workstream       | `GET /v1/api/cluster/ws/{ws_id}/detail`                     |
| 6 | Wait for fan-out             | model-side tool `wait_for_workstream`                       |
| 7 | Govern                       | `POST /v1/api/workstreams/{ws_id}/trust`                    |
|   |                              | `POST /v1/api/workstreams/{ws_id}/restrict`                 |
|   |                              | `POST /v1/api/workstreams/{ws_id}/stop_cascade`             |
|   |                              | `POST /v1/api/workstreams/{ws_id}/close_all_children`       |
| 8 | Approve / cancel             | `POST /v1/api/workstreams/{ws_id}/approve`                  |
|   |                              | `POST /v1/api/workstreams/{ws_id}/cancel`                   |
| 9 | Close                        | `POST /v1/api/workstreams/{ws_id}/close`                    |

Refer to `/openapi.json` (Swagger UI at `/docs`) on any
`turnstone-console` process for the authoritative operation ids and
schemas. Coordinator-only verbs (`/children`, `/trust`, `/restrict`,
`/stop_cascade`, `/close_all_children`) 404 against `kind=interactive`
rows; the shared verbs (`/send`, `/approve`, `/cancel`, `/events`,
`/history`, `/open`, `/close`, etc.) work on both kinds.

---

## 1. Create a coordinator

```http
POST /v1/api/workstreams/new
Content-Type: application/json
Authorization: Bearer <token>

{
  "name": "release-coord",
  "skill": "engineer-orchestrator",
  "initial_message": "audit /auth for CSRF handling across all active routes"
}
```

```http
HTTP/1.1 201 Created
Content-Type: application/json

{"ws_id": "a1b2c3d4e5f6...", "name": "release-coord"}
```

All three body fields are optional — an empty body still creates a
coordinator with an auto-generated name and no initial message.
Returns **503** with a remediation message when the cluster isn't
configured with a coordinator model; see
[`coordinator.model_alias`](settings.md) to set one.

**SSE implication:** the `ws_created` event fires on the cluster-wide
stream (`/v1/api/cluster/events`) once the row is committed.  Per-ws
subscribers (step 2) see the session warm up as token traffic starts.

---

## 2. Subscribe to the per-coordinator event stream

```http
GET /v1/api/workstreams/{ws_id}/events HTTP/1.1
Accept: text/event-stream
Authorization: Bearer <token>
```

One persistent SSE connection per browser tab / SDK caller — the
console fans each event out to every listener queue (cap 500 events
per queue, put_nowait drop on overflow).  Events come in flat JSON
with a `type` field.  The recurring shapes a UI has to handle:

| `type`              | Emitted when                                                                               | Payload highlights |
|---------------------|--------------------------------------------------------------------------------------------|--------------------|
| `thinking_start` / `thinking_stop` | Model has entered / exited a reasoning block                                | — |
| `reasoning`         | Reasoning-token stream chunk (when the model exposes it)                                   | `text` |
| `content`           | Assistant-content stream chunk                                                             | `text` |
| `stream_end`        | End of a single provider stream                                                            | — |
| `tool_result`       | A tool call completed (success or error)                                                   | `call_id`, `name`, `output`, `is_error?` |
| `tool_output_chunk` | Streaming tool output (e.g. long bash command)                                             | `call_id`, `chunk` |
| `approve_request`   | One or more tool calls need operator approval                                              | `items: [{call_id, header, preview, func_name, approval_label, needs_approval}]` |
| `approval_resolved` | Operator answered the approval prompt                                                      | `approved`, `feedback` |
| `state_change`      | Worker-thread state transition                                                             | `state` ∈ `running`, `thinking`, `attention`, `idle`, `error` |
| `status`            | Token usage + context-window snapshot (fires on every streaming tick)                      | `prompt_tokens`, `completion_tokens`, `total_tokens`, `context_window`, `pct`, `effort`, `cache_creation_tokens`, `cache_read_tokens` |
| `rename`            | Session's display name changed                                                             | `name` |
| `intent_verdict`    | Intent judge produced a verdict on a pending tool call                                     | `risk_level`, `recommendation`, `reasons` |
| `output_warning`    | Output guard flagged a tool result                                                         | `call_id`, `risk_level`, `flags` |
| `child_ws_created`  | A direct child of this coord was just created (fan-out from the cluster bus)              | `child_ws_id`, `node_id`, `name`, `parent_ws_id` (`ws_id` in the envelope is always the coord's own id) |
| `child_ws_state`    | A direct child transitioned state                                                          | `child_ws_id`, `state` |
| `child_ws_closed`   | A direct child closed                                                                      | `child_ws_id` |
| `child_ws_rename`   | A direct child's name changed                                                              | `child_ws_id`, `name` |
| `wait_started` / `wait_progress` / `wait_ended` | `wait_for_workstream` tool lifecycle (see §6)                  | `call_id`, `ws_ids`, `elapsed`, `results`, `complete` |
| `batch_started` / `batch_ended` | `spawn_batch` / `close_all_children` tool lifecycle                            | `call_id`, `op`, `total`/`succeeded`/`denied`/`closed`/`failed`/`skipped` |
| `info` / `error`    | Operational messages                                                                       | `message` |

**Reconnection contract:** a freshly-opened SSE connection receives
the current snapshot of any pending tool approval (`approve_request`
is re-sent if unresolved) and any in-flight `wait_*` / `batch_*`
indicator — so a tab refresh mid-approval doesn't strand the
operator.

---

## 3. Send the first user message

```http
POST /v1/api/workstreams/{ws_id}/send
Content-Type: application/json

{"message": "audit /auth for CSRF handling across all active routes"}
```

```http
HTTP/1.1 200 OK
{"status": "ok"}
```

The message is queued for the worker thread at its next tool-result
seam (so you can send follow-ups mid-conversation without corrupting
the in-progress turn).  On the SSE stream you'll see `state_change`
→ `thinking_start` → streaming `reasoning` / `content` / `tool_result`
events, finishing with `state_change → idle` or an
`approve_request` when the model invokes a gated tool.

---

## 4. Inspect direct children

```http
GET /v1/api/workstreams/{ws_id}/children HTTP/1.1
```

```json
{
  "items": [
    {"ws_id": "d4e5f6...", "name": "csrf-audit", "state": "running", "node_id": "gpu-3"},
    {"ws_id": "e1f2a3...", "name": "xss-audit",  "state": "idle",    "node_id": "gpu-1"}
  ],
  "truncated": false
}
```

The response key is `items`, not `children` — the endpoint shape
follows the cluster-wide workstream-list idiom rather than the
coordinator `list_workstreams` tool's (which uses `children`).
Rows include every state stored for the parent (`running`, `idle`,
`closed`, ...); the endpoint does not accept a state query param,
so clients should inspect each row's `state` field and filter
locally if they want to hide closed/deleted children.  Nested
coordinator rows are dropped server-side so only interactive
descendants appear.

---

## 5. Inspect one workstream (storage + live block + tail)

```http
GET /v1/api/cluster/ws/{ws_id}/detail?message_limit=20 HTTP/1.1
```

```json
{
  "persisted": { "ws_id": "...", "state": "running", "parent_ws_id": "...", "kind": "interactive", ... },
  "live":      { "state": "thinking", "tokens": 12843, "activity": "...", "pending_approval": null },
  "tail":      [ {"role": "assistant", "content": "...", "tokens": 128}, ... ]
}
```

Works for any workstream the caller has `admin.cluster.inspect` on,
not just children of a single coordinator — useful for a cluster
admin panel watching multiple coordinators at once.  `live` is
`null` when the owning node is unreachable or has dropped the row
from its dashboard cache; callers should degrade gracefully, not
treat it as an error.

For fan-out views, prefer
[`GET /v1/api/cluster/ws/live?ids=a,b,c`](bulk-endpoints.md) — it
collapses N per-row round-trips into one, returning the live block
for every id in a `{results, denied, truncated}` envelope.

---

## 6. Wait for fan-out (`wait_for_workstream`)

`wait_for_workstream` is a **model-side tool**, not an HTTP endpoint
— the coordinator's LLM invokes it with a list of child ws_ids, the
session's worker thread blocks inside the tool, and a sequence of
`wait_started` / `wait_progress` / `wait_ended` SSE events is emitted
for the UI to drive a "waiting on N children" indicator.

![wait_for_workstream sequence](diagrams/png/27-coordinator-wait-for-workstream.png)

Key properties:

- **Caps** — up to 32 ws_ids per call, up to 600 seconds per call.
  A coordinator that needs to wait on more children re-invokes the
  tool with a fresh timeout.
- **Modes** — `mode="any"` returns as soon as one child reaches a
  real terminal state (`idle` / `error` / `closed` / `deleted`);
  `mode="all"` waits for every polled child.
- **Progress throttling** — the poll loop runs every 500 ms but the
  SSE emission is diff-on-state-change plus a 5-second heartbeat.  A
  600 s wait generates O(dozens) of progress events, not 1200.
- **Denied rows** — an id the caller doesn't own (cross-tenant) or a
  missing row is reported as a `denied` state in the results dict;
  `mode="any"` won't satisfy on a pure-denied list (the LLM should
  treat it as a config error, not a completion).

Prefer `wait_for_workstream` over polling `inspect_workstream` in a
loop — a wait consumes one assistant turn regardless of how long the
children take, whereas each `inspect_workstream` poll costs a full
turn (plus judge, plus tokens).  On a fan-out of 3+ children this
rounds to a 10× token-efficiency win.

---

## 7. Governance — trust, restrict, stop_cascade, close_all_children

These four endpoints let an operator steer a live coordinator session
mid-flight.  All four emit an audit event tagged
`coordinator.<action>` via the dedicated audit executor so a cascade
burst can't starve audit writes.

### `POST /trust` — auto-approve own-subtree sends

```http
POST /v1/api/workstreams/{ws_id}/trust
{"send": true}
```

Flips `trust_send=true` on the live session.  Subsequent
`send_to_workstream` calls that target a ws_id in the coordinator's
own subtree skip the approval prompt; foreign ws_ids and other tool
calls still go through the normal flow.  Requires both
`admin.coordinator` AND `coordinator.trust.send` permissions (the
second grants a service token the opt-in it otherwise wouldn't get).

### `POST /restrict` — revoke tool access mid-session

```http
POST /v1/api/workstreams/{ws_id}/restrict
{"revoke": ["spawn_workstream", "delete_workstream"]}
```

Unions the names into the session's revoked-tools set.  Additive and
idempotent — calling twice with overlapping lists converges to the
union.  Revocations don't survive a session close/reopen; operators
opt in per session.  Cap 256 tool names per request, 128 chars each.

### `POST /stop_cascade` — cancel the subtree

```http
POST /v1/api/workstreams/{ws_id}/stop_cascade
{}
```

Cancels the coordinator's in-flight generation AND dispatches
`cancel_workstream` through the routing proxy for every direct
child in the in-memory registry.  Returns:

```json
{"status": "ok", "cancelled": ["child-1", "child-3"], "failed": [], "skipped": ["child-2"]}
```

Response uses the [cascade-mutation bulk shape](bulk-endpoints.md):
`cancelled` = accepted, `failed` = dispatch error worth retrying,
`skipped` = upstream 404 (already gone — stale registry entry or
the row was deleted between snapshot and dispatch).  Grandchildren
aren't touched directly; they sit behind their parent's cancel and
propagate via the child's SSE stream.

### `POST /close_all_children` — soft-close the direct fan-out

```http
POST /v1/api/workstreams/{ws_id}/close_all_children
{"reason": "audit round complete"}
```

Response:

```json
{"status": "ok", "closed": ["c-1", "c-2"], "failed": [], "skipped": []}
```

Soft-close cascade bounded by the same semaphore as `stop_cascade`.
The `reason` (up to 512 chars) propagates into each closed child's
audit + `workstream_config` for postmortem.  Unlike `stop_cascade`
this does NOT recurse into grandchildren — the model-facing tool
that pairs with this endpoint asks for a bounded teardown of the
coordinator's own fan-out.  For a full-subtree teardown, use
`stop_cascade`.

See [bulk-endpoints.md](bulk-endpoints.md) for why both endpoints
share the cascade-mutation shape and how it differs from the
`spawn_batch` / `cluster/ws/live` shape.

---

## 8. Approve / cancel

The `approve` endpoint is what resolves an `approve_request` SSE
event.  The coordinator's worker thread is blocked inside
`ui.approve_tools` waiting for this POST.

```http
POST /v1/api/workstreams/{ws_id}/approve
{"approved": true, "feedback": null, "always": false}
{"approved": false, "feedback": "spawn count looks too high — try 3 not 10"}
{"approved": true, "feedback": null, "always": true}    // always-approve this tool name
```

`cancel` drops the in-flight generation but leaves the coordinator
idle and open for a fresh `send`:

```http
POST /v1/api/workstreams/{ws_id}/cancel
{}
```

---

## 9. Close

```http
POST /v1/api/workstreams/{ws_id}/close
{}
```

Soft-closes the session — state persists, children keep running (use
`close_all_children` or `stop_cascade` first to wind them down), the
worker thread exits, SSE streams send a final `stream_end` and
disconnect.  The row is reopenable via
`POST /v1/api/workstreams/{ws_id}/open` so long as it hasn't been
deleted.

---

## Further reading

- [coordinator-skills.md](coordinator-skills.md) — writing a skill
  that runs on a coordinator session (orchestrator persona,
  workflow patterns, `SkillKind` classifier).
- [bulk-endpoints.md](bulk-endpoints.md) — the two bulk-shape
  idioms (`{results, denied, truncated}` vs
  `{<bucket>, failed, skipped}`) used by `cluster/ws/live`,
  `spawn_batch`, `stop_cascade`, and `close_all_children`.
- [architecture.md](architecture.md) — cluster-wide architecture
  including how coordinator sessions fit next to node-hosted
  interactive workstreams.
- The live OpenAPI spec (`/openapi.json` on any console process)
  and Swagger UI (`/docs`) — authoritative schemas for every
  endpoint above.
