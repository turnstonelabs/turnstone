# Turnstone Architecture

Turnstone is an AI orchestration platform with tool use, parallel workstreams, and persistent
memory. It connects to any OpenAI-compatible API (local vLLM, OpenAI, etc.) or
Anthropic's native Messages API via pluggable provider adapters, and gives the
model role-specific built-in tools plus external tools via MCP (Model Context
Protocol) for reading, writing, searching, and executing code.

The core design principle is a **UI-agnostic engine with pluggable frontends**.
The engine (`ChatSession`) drives the conversation loop -- streaming, tool
dispatch, retry, compaction -- while every user-facing interaction is delegated
through the `SessionUI` protocol. Any frontend implements that protocol and
plugs in.

## Entry Points

| Command | Module | Frontend | Purpose |
|---------|--------|----------|---------|
| `turnstone` | `turnstone.cli` | `TerminalUI` | Interactive terminal REPL |
| `turnstone-server` | `turnstone.server` | `WebUI` | Browser-based chat (HTTP + SSE) |
| `turnstone-console` | `turnstone.console.server` | ClusterCollector | Cluster dashboard (aggregates all nodes) |
| `turnstone-eval` | `turnstone.eval.cli` | `NullUI` | Headless measurement (scores tool-use against expected actions) |
| `turnstone-optimizer` | `turnstone.optimizer` | `NullUI` | Prompt/tool optimization (UCB self-modify loop over the eval substrate) |
| `turnstone-channel` | `turnstone.channels.cli` | ChannelAdapter | Channel gateway (Discord, Slack, etc.) |
| `turnstone-admin` | `turnstone.admin` | — | Offline user and API token management |
| `turnstone-doctor` | `turnstone.doctor` | — | LLM-backed cluster diagnostics |

---

## Module Map

```
turnstone/
  cli.py              Terminal frontend (TerminalUI, WorkstreamTerminalUI, REPL)
  server.py           Web frontend (WebUI, HTTP handler, static-file serving)
  eval.py             Evaluation harness (HeadlessSession, scoring, prompt optimization)
  core/
    session.py        ChatSession engine, generation ownership, tool dispatch
    session_manager.py Workstream lifecycle, deferred create, state publication
    session_ui_base.py Shared SSE state, concurrent approval cycles, verdict bookkeeping
    trajectory.py     Canonical provider-neutral Turn trajectory and effect metadata
    model_turn.py     ModelLane binding + the single lower/sample/re-ingest boundary
    model_backend_auth.py Per-call static/dynamic model-backend credential policy
    state_writer.py   Incarnation-fenced write-behind workstream state persistence
    providers/        LLM provider adapters (pluggable backend layer)
      _protocol.py    LLMProvider protocol, ModelCapabilities, StreamChunk, CompletionResult
      _openai.py      OpenAIProvider facade (re-exports Chat/Responses providers)
      _openai_chat.py       OpenAIChatCompletionsProvider — vLLM, llama.cpp, local compatible APIs
      _openai_responses.py  OpenAIResponsesProvider — commercial OpenAI Responses API
      _openai_common.py     Shared ModelCapabilities table + helpers
      _anthropic.py   AnthropicProvider — Anthropic Messages API, native streaming, thinking
      _google.py      GoogleProvider — Google Gemini via OpenAI-compat endpoint
      __init__.py     create_provider() + create_client() factory functions
    workstream.py     Workstream runtime state and worker ownership (WorkstreamState, Workstream)
    tools.py          Tool schema loader (JSON -> OpenAI function-calling format)
    mcp_client.py     MCPClientManager — MCP server connections, tool discovery, dynamic refresh
    tool_search.py    Dynamic tool search — BM25 index, session-scoped tool visibility
    watch.py          WatchRunner daemon — periodic command polling, condition DSL, result dispatch
    judge.py          Intent validation — heuristic rules + LLM judge, advisory verdicts
    model_registry.py ModelRegistry — immutable configs, atomic binding snapshots, fallback routing
    memory.py         Persistence facade + structured memory API (delegates to storage backend)
    config.py         Config file loader (config.toml), apply_config(), warn_migrated_settings()
    config_store.py   ConfigStore — database-backed settings with in-memory cache, thread-safe get/set
    settings_registry.py  SettingDef catalog, validation, type coercion, serialization
    storage/          Pluggable storage, atomic fork/create lifecycle, SQLite + PostgreSQL
    metrics.py        Prometheus-compatible metrics collector (MetricsCollector)
    healthcheck.py    BackendHealthMonitor — periodic probe + circuit breaker
    ratelimit.py      Per-IP token-bucket rate limiter (RateLimiter, TokenBucket)
    edit.py           File edit utilities (find_occurrences, pick_nearest)
    safety.py         Command safety validation (blocked patterns, sanitization)
    web.py            Web utilities (HTML stripping, SSRF prevention)
  api/
    schemas.py        Shared Pydantic v2 models (auth, errors, WorkstreamState)
    server_schemas.py Server endpoint request/response models
    console_schemas.py Console endpoint request/response models
    openapi.py        OpenAPI 3.1 spec builder
    server_spec.py    Server endpoint catalog → build_server_spec()
    console_spec.py   Console endpoint catalog → build_console_spec()
    docs.py           /openapi.json + /docs (Swagger UI) handler factories
  sdk/
    server.py         AsyncTurnstoneServer + TurnstoneServer (HTTP client)
    console.py        AsyncTurnstoneConsole + TurnstoneConsole (HTTP client)
    events.py         SSE event dataclasses with type registry
    _base.py          Shared httpx async client, auth, error handling
    _sync.py          Background event loop for sync wrappers
    _types.py         TurnResult + TurnstoneAPIError
  console/
    collector.py      ClusterCollector — aggregates state from all nodes via SSE
    scheduler.py      TaskScheduler — background cron/at scheduler, dispatches via HTTP
    server.py         Cluster dashboard HTTP server + SSE + CLI entry point
    static/           Cluster dashboard web UI (page-specific HTML, CSS, JS)
  channels/
    cli.py            Unified channel gateway entry point (turnstone-channel)
    _protocol.py      ChannelAdapter protocol
    _routing.py       ChannelRouter — channel/thread ↔ workstream mapping via HTTP
    _config.py        Base ChannelConfig dataclass
    discord/          Discord adapter (bot, cog, views, streaming, config)
    slack/            Slack adapter (Socket Mode bot, DM routing, approval buttons)
  shared_static/      Shared design system (base.css, auth.js, theme.js, toast.js, utils.js, kb.js)
    katex-0.18.1/    Vendored KaTeX math rendering library (MIT, woff2 fonts)
  ui/
    colors.py         ANSI color constants with NO_COLOR support
    markdown.py       Streaming terminal markdown renderer (line-buffered)
    spinner.py        Braille character spinner (daemon thread)
    static/
      index.html      Single-page app shell (links to CSS and JS)
      style.css       Page-specific UI styles (dashboard, markdown elements, approval blocks)
      renderer.js     Markdown + LaTeX renderer (tables, nested lists, blockquotes, KaTeX math)
      app.js          Split-pane UI (Pane class, binary layout tree, SSE, tool approval)
  tools/
    *.json            Role-specific tool schemas and synthetic tool surfaces
```

Both UIs share a common design system extracted into `turnstone/shared_static/`: design tokens, login overlay, toast notifications, theme toggle, keyboard shortcuts, and utility functions. Each UI imports `base.css` and the shared JS modules at `/shared/`, then adds only page-specific code at `/static/`.

---

## Core Loop

> See also: [Conversation Turn diagram](diagrams/png/04-conversation-turn.png)

A user message flows through the system as follows:

```
 User input
     |
     v
 ChatSession.send(user_input)
     |
     v
 _claim_generation()  ---------->  monotonic owner + fresh cancel event
     |                              initiating principal pinned to the owner
     v
 _refresh_model_from_registry() -> atomically replace ResolvedModelBinding
     |                              when the registry generation changed
     v
 _initialize_send_generation() --> append the canonical USER Turn and stage
     |                              ordered durable writes as one generation commit
     v
 _full_messages()  ------------>  system messages + canonical Turn trajectory
     |
     v
 _emit_state("thinking")
     |
     v
 _stream_response()  ------------->  model_turn(ModelLane, turns, on_chunk=...)
     |                                  primary/fallback lane walk; per-lane retry ladder
     v
 the on_chunk consumer  ----------->  generation-fenced display projection:
     |                                  on_reasoning_token() / on_content_token()
     |                                  tool-call deltas just flush the splitter
     |                                    (assembly lives in drain_stream, inside
     |                                    model_turn — the consumer never accumulates)
     |                                  track finish_reason (citations-footer gate)
     |                                  reject cancelled or superseded publication
     v
 ModelTurnResult.turn  ------------>  canonical ASSISTANT Turn, serving-lane
     |                                  provenance, usage, and wire facts
     v
 finish_reason check:
     +--- "length"  --> warn, discard partial tool_calls
     +--- "content_filter" --> warn
     v
 tool_calls present?
     |
     +--- No ---> commit assistant/status/idle for this generation -> return
     |
     +--- Yes --> _emit_state("running")
                    |
                    v
                  _execute_tools(tool_calls)  <--- four-phase pipeline (see below)
                    |
                    v
                  append tool results to self.messages
                    |
                    v
                  loop back to _full_messages()
```

Every mutable publication from a worker-owned turn carries its originating
generation. `_publish_for_generation()` admits short live/UI changes only
while that generation still owns the session. `_commit_for_generation()`
atomically changes bounded in-memory state and stages immutable persistence
closures; those closures run outside the generation lock but through a FIFO
ticket lane. The caller still waits for durability, while Stop, close, and a
force successor remain responsive and a newer accepted row cannot overtake an
older one.

### Tool Execution Pipeline

> See also: [Tool Pipeline diagram](diagrams/png/05-tool-pipeline.png)

Tool execution is a four-phase process:

```
Phase 1: PREPARE (serial)
  For each tool_call:
    _prepare_tool(tc)
      -> parse JSON arguments (with regex fallback for malformed JSON)
      -> dispatch to _prepare_{tool_name}(call_id, args)
      -> validate inputs, build preview text
      -> return item dict with: header, preview, needs_approval, execute fn

Phase 2: APPROVE (blocking per batch; reentrant across agents)
  _emit_state("attention")
  ui.approve_tools(items)
    -> apply policy, explicit auto-approval, and Smart Approvals
    -> register one ApprovalCycle with its own cycle_id/event/result
    -> publish one complete approve_request card
    -> resolve by cycle_id/call_id (legacy clients select the oldest cycle)
    -> return this cycle's (approved, feedback)
  _emit_state("running")

Phase 3: EXECUTE (parallel)
  _check_cancelled(generation)  <-- checkpoint before execution starts
  if len(items) == 1:
    run_one(items[0])
  else:
    ThreadPoolExecutor(max_workers=4).map(run_one, items)
  Bash tool streams stdout line-by-line via ui.on_tool_output_chunk(call_id, line)
    (cancel_event also checked per line — kills process group on cancel)
  Final output (stdout + stderr) delivered via ui.on_tool_result(call_id, name, output)

Phase 4: GUARD + ATOMIC FOLD
  compact/truncate results against the shared output budget
  run heuristic + optional LLM output guard
  re-check exact generation ownership after guard work
  the complete result batch + advisories + queued feedback folds under one
    generation commit; durable tool rows retain typed effect metadata
```

Parallel task agents can reach independent approval gates at the same time.
`SessionUIBase` therefore stores an insertion-ordered registry of
`ApprovalCycle` objects rather than one global pending event. A targeted click
can resolve only its cycle. Each prepared batch also carries one frozen Smart
Approval settings snapshot (enabled flag, confidence threshold, and verdict
wait), so concurrent gates cannot combine fields from different hot-reload
generations; a partially or inconsistently stamped batch fails closed to human
review. Workstream-wide Stop/close paths run an admission barrier, deny every
cycle owned by the cancelled operation, and leave a newly claimed successor's
cycles alone. Late judge verdicts are matched by both call ID and
judge-generation identity; stale verdicts remain audit-only and cannot
smart-approve a reused call ID.

### State Transitions

The engine emits state changes via `_emit_state()` which calls
`ui.on_state_change(state)`. Frontends use these to update indicators
(spinner, tab badges, status line).

```
  send() called
      |
      v
  "thinking"  --->  streaming response
      |
      v
  "running"   --->  tool execution
      |
      v
  "attention"  --->  waiting for user approval
      |
      v
  "running"   --->  executing approved tools
      |
      v
  "idle"       --->  no more tool calls, turn complete
      |
  (or "error"  --->  exception or KeyboardInterrupt)

  cancel() may be called from any state. Cooperative Stop sets the current
  generation's event, closes registered model streams, wakes retry backoff,
  aborts child model scopes, and denies that operation's approval cycles.

  Force Stop also releases the wedged worker slot so a successor can claim a
  new generation. The abandoned daemon thread may still unwind. Generation and
  stream-registration fences prevent abandoned send/model generations from
  publishing into the successor; quick slash-command workers remain a
  best-effort operator escape hatch and may finish an in-place mutation because
  they do not yet carry generation checkpoints.

  A cancelled partial assistant response is persisted with an explicit marker.
  Every unanswered tool call receives a synthetic TOOL Turn: effect_status is
  "unknown" when its outcome was not observed, "none" when it definitely never
  started, or a stronger staged receipt when the executor reported one.
```

---

## SessionUI Protocol

> See also: [Core Engine Classes diagram](diagrams/png/03-core-engine-classes.png)

Defined in `turnstone.core.session.SessionUI` as a `typing.Protocol`. Its
callbacks separate turn/stream lifecycle, tool interaction,
durable operator-context events, and governance results:

```python
class SessionUI(Protocol):
    def on_turn_start(self) -> None: ...
    def on_turn_committed(self) -> None: ...
    def on_stream_discarded(self) -> None: ...
    def on_thinking_start(self) -> None: ...
    def on_thinking_stop(self) -> None: ...
    def on_reasoning_token(self, text: str) -> None: ...
    def on_content_token(self, text: str) -> None: ...
    def on_stream_end(self) -> None: ...
    def approve_tools(self, items: list[dict]) -> tuple[bool, str | None]: ...
    def on_tool_result(
        self,
        call_id: str,
        name: str,
        output: str,
        *,
        is_error: bool = False,
        preview: dict | None = None,
    ) -> None: ...
    def on_tool_output_chunk(self, call_id: str, chunk: str) -> None: ...
    def on_status(self, usage: dict, context_window: int, effort: str) -> None: ...
    def on_info(self, message: str) -> None: ...
    def on_error(self, message: str) -> None: ...
    def on_system_turn(self, content: str, source: str, meta: dict | None = None) -> int | None: ...
    def on_compaction(self, payload: dict) -> int | None: ...
    def on_state_change(self, state: str) -> None: ...
    def on_rename(self, name: str) -> None: ...
    def on_intent_verdict(self, verdict: dict, judge_event: object | None = None) -> None: ...
    def on_output_warning(self, call_id: str, assessment: dict) -> None: ...
    def record_output_assessment(self, call_id: str, assessment: dict, **facts) -> None: ...
```

`on_turn_start` fires at the top of each iteration of the send-loop;
`on_turn_committed` fires immediately after `messages.append(assistant_msg)`.
`SessionUIBase` uses both to reset the per-turn inflight buffers
(`_ws_inflight_content` / `_ws_inflight_reasoning` / `_ws_inflight_seq`)
that fuel the SSE refresh-resume `in_progress_snapshot` event — see
the per-workstream events stream in
[`docs/api-reference.md`](api-reference.md#get-v1apiworkstreamsws_idevents).

`on_stream_discarded` removes a failed attempt's partial projection before a
mid-stream retry. `on_system_turn` and `on_compaction` return the assigned SSE
event ID when the frontend has one; persistence stamps the corresponding row
with that cursor so reconnect replay and `/history` agree. The `judge_event`
argument is the intent-judge generation identity used to reject stale verdicts
from a prior approval round.

`on_rename` is called by the `/name` command (on success) and after a successful `/resume` (if the resumed session has an alias or title). `WebUI.on_rename` broadcasts a `ws_rename` event on the global SSE channel and updates the in-memory `Workstream.name`; `TerminalUI.on_rename` is a no-op.

### Implementations

| Class | Module | Notes |
|-------|--------|-------|
| `TerminalUI` | `turnstone.cli` | ANSI colors, `MarkdownRenderer`, `Spinner`, readline-based `input()` for approval |
| `WebUI` | `turnstone.server` | SSE event queue per workstream + global broadcast, `threading.Event` for blocking on approval. `on_state_change` sends to both per-workstream and global SSE (the browser UI uses per-workstream `state_change` events to manage busy/idle transitions; `stream_end` only finalizes markdown rendering). |
| `ConsoleCoordinatorUI` | `turnstone.console.coordinator_ui` | Reuses `SessionUIBase`; mirrors lifecycle, approval, and verdict events onto the coordinator tree stream. |
| `NullUI` | `turnstone.eval.core` | Discards all output; `approve_tools` always returns `(True, None)` |

### WorkstreamTerminalUI

`WorkstreamTerminalUI` (in `turnstone.cli`) extends `TerminalUI` with workstream
awareness:

- **Output buffering**: When in background (`is_foreground` is False), tokens
  are appended to `_output_buffer` instead of written to stdout. When the user
  switches to this workstream, `flush_buffer()` replays them.

- **Approval blocking**: `approve_tools()` calls `_fg_event.wait()` when in
  background, blocking the worker thread until the workstream is foregrounded.
  This ensures the user sees the approval prompt in the correct context.

- **Foreground/background toggle**: `set_foreground(bool)` sets or clears
  `_fg_event` (a `threading.Event`). The manager calls this during `/ws <N>`
  switches.

---

## Workstream Architecture

Workstreams are parallel, independent chat sessions. Each has its own
`ChatSession`, `SessionUI`, canonical trajectory, worker slot, and durable
lifecycle. Interactive and coordinator workstreams use the same
`SessionManager`; kind-specific construction, cleanup, and event fan-out live
behind adapters.

### WorkstreamState

> See also: [Workstream States diagram](diagrams/png/09-workstream-states.png)

Defined in `turnstone.core.workstream.WorkstreamState` (5 states):

```
IDLE       waiting for user input
THINKING   LLM is streaming a response
RUNNING    tools are executing
ATTENTION  blocked on user approval or plan review
ERROR      last operation failed
```

### Data Model

```python
@dataclass
class Workstream:
    id: str  # full UUID hex identity
    name: str  # user-visible label
    kind: WorkstreamKind
    user_id: str
    parent_ws_id: str | None
    project_id: str | None
    state: WorkstreamState  # current state
    session: ChatSession | None  # the conversation engine
    ui: SessionUI | None  # frontend adapter
    worker_thread: threading.Thread | None
    worker_kind: str
    error_message: str
    last_active: float  # time.monotonic() timestamp, updated on every state change
    _fork_reservation_token: str  # private durable incarnation fence
    _state_revision: int
    _state_incarnation: int
    _lock: threading.Lock  # short per-workstream state/worker mutations
    _lifecycle_lock: threading.RLock  # birth versus terminal serialization
    _state_tail_lock: threading.Lock  # durable state/observer ordering
```

The durable incarnation token and lifecycle fields are internal and never appear in
public workstream/config projections. They distinguish successive objects that
reuse one logical ID. Manager-created rows receive the token at registration;
legacy rows acquire one atomically when rehydration, delete, or fork preflight
takes its authoritative snapshot. This prevents an old manager state
transition, buffered lifecycle-state write, stale delete authorization, or
fork operation from targeting a replacement incarnation.

### SessionManager

```python
class SessionManager:
    def create(self, *, user_id: str, defer_emit_created: bool = False, ...) -> Workstream: ...
    def commit_create(self, ws: Workstream) -> bool: ...
    def discard(self, ws_id: str, *, expected: Workstream | None = None, ...) -> bool: ...
    def open(self, ws_id: str) -> Workstream | None: ...
    def close(self, ws_id: str) -> bool: ...
    def delete_persisted(self, ws_id: str, *, delete_fn: Callable[[], bool], ...) -> bool: ...
    def close_idle(
        self, max_age_seconds: float
    ) -> list[str]: ...  # auto-close stale IDLE workstreams
    def reap_stale_creating_reservations(
        self, max_age_seconds: float = 2 * 60 * 60
    ) -> list[str]: ...  # hard-delete crash-abandoned hidden reservations
    def get(self, ws_id: str) -> Workstream | None: ...
    def list_all(self) -> list[Workstream]: ...
    def switch(self, ws_id: str) -> Workstream | None: ...
    def switch_by_index(self, index: int) -> Workstream | None: ...
    def set_state(self, ws_id, state, error_msg=""): ...
    def set_state_deferred(self, ws_id, state, *, deferred_persistence, ...): ...
```

`SessionKindAdapter` decouples kind-specific UI/session construction from lifecycle
policy. The manager owns exact-object admission, capacity, hidden creates,
open/close/delete ordering, state durability, and lifecycle events; adapters
own how an interactive or coordinator session is built and cleaned up.

### Atomic Create and Fork Lifecycle

Every manager create begins as a hidden durable reservation:

```
reserve exact Workstream object under the per-ID lane
    -> INSERT workstreams(state="creating") + private token atomically
    -> build UI and ChatSession outside the manager lock
    -> token-guard initial workstream configuration
    -> validate uploads and other fallible prepublication setup
    -> optional storage.clone_workstream(...) transaction
    -> prepare remaining alias/UI publication data against the same token
    -> CAS creating -> idle
    -> emit ws_created
    -> expose through get/list/open and allow worker dispatch
```

The HTTP create handler always uses `defer_emit_created=True`. Normal failure or
request cancellation before publication immediately calls `discard()` and
conditionally deletes only the row carrying the returned token; it cannot
delete a same-ID replacement. A `creating` row is excluded from ordinary
open/list surfaces, so other nodes cannot observe a half-built session.

Forking is a storage transaction, not a sequence of message copies. The
transaction reauthorizes the source, validates its current persona/project
envelope against the destination session's immutable
`ForkCloneExpectation`, compares the source token captured during canonical
preflight, verifies the destination token and emptiness, copies
the checkpoint-bounded canonical Turns and configuration, retains attachment
blob references, and binds the effective project. Any source drift, corrupt
attachment reference, or destination race aborts the whole transaction. Only
after the committed snapshot is adopted in memory does the normal
`creating -> idle -> ws_created` publication run.

Rehydration binds the private token before constructing the session, then
rechecks it after configuration and history are loaded; a delete/re-register
crossing retires the hybrid candidate and retries from a fresh snapshot.
Loaded hard-delete similarly compares the endpoint's authorized token with
both the local and current durable incarnations before making any terminal
mutation. It closes generation publication, drains every already-admitted
`_commit_for_generation` durability ticket and the state tail, then performs
the token-conditional delete. A stale request leaves a current successor
untouched; a stale local object or failed delete is retired without publishing
a false `ws_closed` event.

That drain covers manager-owned session durability admitted through the ticket
lane. Direct legacy storage helpers that mutate only by `ws_id` are not made
token-conditional by this refactor and must not be used as a same-ID reuse
fence; the incarnation token guarantees exact create/fork/delete target
selection, not a new transaction contract for every maintenance API.

#### Crash-Abandoned Create Recovery

`creating` is an internal storage lifecycle value, not a live
`WorkstreamState`. Server and console lifecycle maintenance run a recovery pass
at boot and then on an independent five-minute cadence, even when ordinary idle
eviction is disabled; the CLI runs the boot pass once per launch. A pass only
considers rows still in `state='creating'` whose `updated` timestamp is more than
two hours old, and excludes every ID in the manager's loaded snapshot, including
pending creates.

Service liveness is fetched before deletion. A row owned by a live remote node
is protected, while the current process's stable node ID deliberately does not
self-protect: after a restart, a predecessor's abandoned row can carry the same
ID. The manager snapshot and two-hour grace protect the current process's own
work. If liveness cannot be established, the pass deletes nothing.

For each eligible row, the storage backend atomically locks and rechecks state,
age, and the private incarnation token before using the complete hard-delete
path. Conversations, configuration, overrides, and attachment references and
refcounts are cleaned in the same transaction. An eligible legacy or corrupt
reservation without a token is still recoverable: the locked durable row is its
incarnation fence, and the backend logs a warning. Storage uncertainty rolls the
attempt back and is reported as no reaped IDs. The recovery path emits no
lifecycle event and never converts an unpublished reservation into a closed,
reopenable workstream.

### Idle Workstream Lifecycle

The web server's background lifecycle-maintenance thread calls
`SessionManager.close_idle()` when ordinary idle eviction is enabled (every
`timeout / 4`, max 5 min). Any loaded IDLE workstream whose `last_active` is
older than the configured timeout is closed; non-IDLE loaded workstreams are
not. A second storage pass closes old, unloaded rows left by dead process
incarnations. It protects rows whose `node_id` belongs to a currently
heartbeating peer and skips the pass entirely if service-liveness lookup fails.
On close, a `ws_closed` event is broadcast so browser clients remove the tab.
`--workstream-idle-timeout` controls this path (default: 120 minutes, 0 =
disable); the separate stale-create recovery above keeps running when it is 0.

**Workstream eviction at capacity:** When `SessionManager.create()` would
exceed `max_workstreams` (configurable via `[server].max_workstreams`, default
50), the oldest IDLE, worker-free workstream is considered for eviction. The
candidate is only a hint: the manager takes its per-ID and object lifecycle
lanes, rechecks IDLE/worker ownership and `send_barrier_active()` under the
workstream lock, installs a terminal tombstone, and only then swaps the
capacity slot. A command, queued send, claimed send drain, or turn admitted
before that claim makes the candidate ineligible. The
`turnstone_workstreams_evicted_total` counter increments only after a successful
claim; if no safe candidate remains, creation fails at capacity.

### CLI Workstreams

- `/ws list` -- show all workstreams with state indicators
- `/ws new [name]` -- create a new workstream and switch to it
- `/ws <N>` -- switch to workstream by 1-based index
- `/ws close [N]` -- close a workstream
- `/ws rename <name>` -- rename the active workstream

Background notifications: when a background workstream enters `ATTENTION`
state, `_bg_attention_notify` writes an ANSI escape sequence to stderr
(overwrites the line above the prompt) with the workstream name.

Status line: `_print_ws_status_line()` shows a compact status of all
non-idle background workstreams above the input prompt.

### Web Workstreams

- **Tab bar**: Each workstream renders as a tab with a colored state indicator
  (CSS `@keyframes pulse` animation per state). Clicking a tab switches the
  focused pane's workstream (or focuses an existing pane showing that ws).
- **Split panes**: The UI supports tiling multiple workstreams side-by-side or
  stacked via a binary layout tree. Each `Pane` instance encapsulates its own
  SSE connection, message area, input, and state (busy, approval, streaming).
  Split via right-click context menu, pane header buttons, or keyboard
  (`Ctrl+\`, `Ctrl+Shift+\`). Max 6 panes; no duplicate workstreams across panes.
  Layout persisted to `localStorage`.
- **Per-pane SSE**: `Pane.connectSSE(wsId)` opens
  `/v1/api/workstreams/{ws_id}/events` for each pane's event stream independently.
- **Global SSE**: `connectGlobalSSE()` opens `/v1/api/events/global` which
  receives `ws_state` broadcasts from all workstreams, used to update tab
  indicators and pane headers without switching.
- **New tab / close**: POST `/v1/api/workstreams/new`, POST `/v1/api/workstreams/{ws_id}/close`.

### Thread Safety

- `SessionManager._lock`: guards registries, visible order, pending creates,
  capacity accounting, and short manager admission only; storage and callbacks
  run outside it.
- `Workstream._lock`: guards one workstream's worker pair and short state
  mutations.
- The per-ID lifecycle lane orders create/open/close/hard-delete across object
  incarnations; `Workstream._lifecycle_lock` orders one object's birth against
  its terminal paths.
- `Workstream._state_tail_lock` orders accepted state persistence and observer
  events. `_state_revision` rejects superseded tails, while
  `_state_incarnation` and `StateWriter` prevent close/reopen ABA writes.
- `ChatSession._generation_lock` fences one turn's live mutations;
  `_durability_cond` tickets its deferred storage batches in admission order.
- `SessionUIBase._ws_lock` protects concurrent approval cycles, verdict caches,
  and SSE projection state; approval admission uses a separate condition so
  Stop never waits on database I/O.
- `WorkstreamTerminalUI._print_lock`: guards `_output_buffer` access.
- `WorkstreamTerminalUI._fg_event`: `threading.Event` that blocks background
  approval until the workstream is foregrounded.

---

## Tool System

### Schema Format

Each tool is a JSON file in `turnstone/tools/`. The file contains an OpenAI
function-calling schema (`name`, `description`, `parameters`) plus optional
turnstone metadata keys:

| Metadata Key | Type | Meaning |
|-------------|------|---------|
| `interactive` | `bool` | Explicitly include a shared tool on the interactive surface |
| `coordinator` | `bool` | Include the tool on the coordinator surface; without `interactive`, exclude it from interactive sessions |
| `task_agent` | `bool` | Include this tool when running as a task sub-agent |
| `auto_approve` | `bool` | Supply the tool-level automatic-approval default; prepare-time policy may refine it per action |
| `primary_key` | `str` | Fallback argument name for bare-string JSON recovery |
| `kind_variants` | `object` | Override descriptions or parameter schemas for a workstream kind |
| `cwd_note` / `workspace_note` | `str` | Append a session-specific path note without mutating the shared schema constants |

Example (`read_file.json`):

```json
{
  "name": "read_file",
  "description": "Read the contents of a file. ...",
  "parameters": {
    "type": "object",
    "properties": {
      "path": { "type": "string", "description": "..." },
      "offset": { "type": "integer", "description": "..." },
      "limit": { "type": "integer", "description": "..." }
    },
    "required": ["path"]
  },
  "task_agent": true,
  "auto_approve": true,
  "primary_key": "path"
}
```

At import time, `turnstone.core.tools._load_tools()` strips the metadata keys
from each schema and builds:

- `TOOLS` -- list of `{"type": "function", "function": {...}}` dicts for the API
- `INTERACTIVE_TOOLS` / `COORDINATOR_TOOLS` -- kind-filtered schemas with
  applicable variants already overlaid
- `TASK_AGENT_TOOLS` -- subset with `task_agent: true`
- `TASK_AUTO_TOOLS` -- set of tool names with `auto_approve: true`
- `PRIMARY_KEY_MAP` -- `{name: primary_key}` for JSON fallback recovery
- `merge_mcp_tools(builtin, mcp_tools)` -- merges built-in + MCP tools at session init

### Role-Specific Tool Surfaces

`TOOLS` is the union catalog used for introspection and schema documentation;
it is not handed wholesale to every model. The loader derives narrower,
immutable bases:

- `INTERACTIVE_TOOLS` combines local file/shell/search, web, memory, skills,
  watch/notification, preview, prompt, and delegated-agent capabilities.
- `COORDINATOR_TOOLS` focuses on spawning, inspecting, messaging, waiting for,
  cancelling, and closing workstreams, plus cluster visibility and the shared
  memory/skills surfaces.
- `TASK_AGENT_TOOLS` is the explicit metadata-selected subset suitable inside
  a delegated loop. It cannot recursively expose `task_agent`.

Kind variants narrow descriptions and enums before a session sees them. MCP
tools are then appended to the applicable session/task-agent base, and dynamic
tool search may expose only the relevant subset to the model. Approval is
decided from the prepared action, not merely its verb: for example, `skills`
has read, activation, and write actions with different policy gates. The JSON
schemas in `turnstone/tools/` are the authoritative catalog.

The tool name uses the `_agent` suffix — bare `task` collides with
chat-template channels on some local models.

### Prepare / Execute Pattern

Every tool has a `_prepare_{name}` method and a corresponding `_exec_{name}`
method on `ChatSession`:

```
_prepare_bash(call_id, args)   -> item dict with execute=self._exec_bash
_prepare_read_file(call_id, args) -> item dict with execute=self._exec_read_file
...
```

The prepare method validates inputs and builds the preview. The item dict
carries the validated data and a reference to the execute function. This
separation allows the UI to show previews before any side effects occur.

### Agent Tools

`task_agent` invokes `_run_agent()`, which runs a multi-turn loop with a
subset of tools and its own system prompt. The sub-agent runs independently,
then returns the final content as the tool result.

- **task_agent**: uses `self._task_tools` (`TASK_AGENT_TOOLS` + MCP tools)
- **Turn limit**: controlled by `agent_max_turns` (default: `-1`, unlimited).
  When a limit is set and reached, the agent is forced to synthesize a final
  response without tools. When unlimited, the loop only exits when the model
  stops calling tools or hits `finish_reason: "length"`.
- **Retry**: each API call in the agent loop uses the same retry+backoff logic
  as the main loop's per-lane ladder (`_model_turn_with_retry`).
- **Finish reason handling**: `finish_reason: "length"` stops the agent early
  and returns whatever content was generated. `finish_reason: "content_filter"`
  returns a placeholder.

### MCP Tool Integration

`MCPClientManager` (`turnstone/core/mcp_client.py`) connects to external MCP servers
and exposes their tools alongside built-in tools. The MCP SDK is fully async; turnstone
bridges this with a background asyncio event loop in a daemon thread.

**Configuration sources:** MCP servers can be defined in config files (TOML/JSON)
or in the database via the admin UI. Database-backed definitions are managed
through the console admin panel's MCP Servers tab and stored in the
`mcp_servers` table. On startup, `load_mcp_config(storage=)` uses
first-match-wins priority: DB rows (if any enabled) take precedence over
config files. The console can trigger a cluster-wide reload (`POST
/_internal/mcp-reload`) that causes each node to call `reconcile_sync()`,
which diffs the running MCP connections against the current DB state and
adds, removes, or reconnects servers as needed.

**Lifecycle:**
1. `create_mcp_client()` reads server configs from TOML/JSON and database
2. `MCPClientManager.start()` launches the background event loop thread
3. `_connect_all()` connects to each server (stdio subprocess or HTTP), runs
   `initialize()` + `list_tools()`, converts schemas to OpenAI format, detects
   `tools.listChanged` capability for push notification support
4. `ChatSession.__init__` receives the manager, builds `self._tools` (built-in + MCP),
   and registers a listener callback for tool-change notifications
5. `_prepare_tool()` routes MCP tools to `_prepare_mcp_tool()` / `_exec_mcp_tool()`
6. `_exec_mcp_tool()` calls `call_tool_sync()` which dispatches to the async loop
   via `asyncio.run_coroutine_threadsafe()`

**Tool refresh:** Two mechanisms keep tools up-to-date without restart:
- **Push:** Servers declaring `tools.listChanged` send `ToolListChangedNotification`;
  the registered `message_handler` triggers immediate single-server refresh.
- **Manual:** `/mcp refresh [server]` calls `refresh_sync()` for on-demand refresh
  (also attempts reconnection for disconnected servers).

When tools change, `_rebuild_tools()` creates new `_tools`/`_tool_map` objects
(copy-on-write for thread safety) and notifies listener callbacks. Each `ChatSession`
rebuilds its `_tools` and `_task_tools` lists and reconstructs `ToolSearchManager`
(preserving expanded tools).

**Tool naming:** `mcp__{server}__{tool}` — double underscore delimiter, validated
at connection time (server names with `__` are rejected).

**Resilience:** Each MCP server has an independent circuit breaker that opens
after 3 consecutive transport failures (timeouts, broken pipes, connection
resets). Cooldown uses capped exponential backoff (30 s base, 5 min max) with
per-server jitter to avoid thundering herd. Protocol-level errors (`McpError`)
from a healthy connection do not trip the breaker. When the cooldown expires
(half-open), the next operation attempt triggers automatic reconnection. Manual
`/mcp refresh` also clears the circuit on success. All sync bridge methods
(`call_tool_sync`, `read_resource_sync`, `get_prompt_sync`, `refresh_sync`)
cancel orphaned futures on timeout to prevent coroutine accumulation on the
background event loop. Push notification refreshes are debounced (5 s per
server) to protect against notification storms. Operators can force a
catalog refresh or full reconnect from the admin panel; reconnects clear
the circuit breaker and run a fresh handshake. Transport stream references
are pre-closed before stack teardown to work around the MCP SDK's anyio
cancel-scope CPU busy-loop (SDK #2147).

**Error isolation:** Per-server connection/refresh failures are caught and logged; other
servers are unaffected. Tool execution errors return error strings to the LLM
rather than crashing the session.

**Registry discovery:** The console admin panel provides a registry discovery
surface backed by the official MCP Registry (registry.modelcontextprotocol.io).
`MCPRegistryClient` (`turnstone/core/mcp_registry.py`) is a standalone httpx
async client that queries the registry's v0.1 API for server discovery. Search
results are annotated with installed status by cross-referencing the
`mcp_servers` table. Installation creates a DB row with `registry_name`,
`registry_version`, and `registry_meta` columns (migration 019), then triggers
cluster-wide node reload via `_notify_nodes_mcp_reload()`. The registry URL is
configurable via the `mcp.registry_url` setting for enterprise/private
registries.

### Provider Adapter Layer

> See also: [Core Engine Classes diagram](diagrams/png/03-core-engine-classes.png)

`ChatSession` is provider-agnostic and no longer owns mutable raw
provider/client/model handles. Its model state is one immutable
`ResolvedModelBinding`; all LLM communication passes through `model_turn()` and
the `LLMProvider` protocol (`turnstone/core/providers/_protocol.py`). The
in-memory history is canonical `Turn` IR. An OpenAI-like dict shape exists only
as a transient lowering bridge before each provider translates at the API
boundary.

```
ChatSession
    |
    +-- ResolvedModelBinding
    |       +-- ModelLane (provider, client, model, capabilities, params)
    |       +-- immutable ModelConfig snapshot
    |       +-- registry generation
    |
    v
model_turn(ModelLane, list[Turn])
    |
    +-- lowering.py: Turn IR -> repaired provider-neutral wire dicts
    |
    v
LLMProvider.create_streaming()  (the single transport call site)
    |
    +--- OpenAIProvider  --- OpenAI, vLLM, llama.cpp, any /v1/chat/completions API
    +--- AnthropicProvider --- Anthropic Messages API (native streaming, thinking)
    +--- GoogleProvider  --- Google Gemini via /v1beta/openai/ (extends OpenAIProvider)
```

**Protocol methods:**

| Method | Purpose |
|--------|---------|
| `create_streaming()` | The one transport: streaming request, yields normalized `StreamChunk` objects (single-shot callers accumulate via `drain_stream()` into a `CompletionResult`) |
| `get_capabilities()` | Per-model flags (`ModelCapabilities`) |
| `convert_tools()` | Translate OpenAI tool schemas to provider format |
| `retryable_error_names` | Exception class names that trigger retry |
| `extract_reasoning_text()` | Walk stored `provider_blocks`, return concatenated reasoning text for UI rehydration (per-provider block-type knowledge: Anthropic `thinking`, OpenAI Responses `reasoning`, OpenAI Chat synthetic `reasoning_text`) |

**Normalized data types:**

| Type | Fields |
|------|--------|
| `StreamChunk` | `content_delta`, `reasoning_delta`, `tool_call_deltas`, `info_delta`, `usage`, `finish_reason`, `provider_blocks` |
| `CompletionResult` | `content`, `tool_calls`, `finish_reason`, `usage`, `provider_blocks` |
| `ModelLane` | Frozen per-loop provider/client/model binding, capabilities, sampling knobs, registry reference, and backend-auth seam |
| `ResolvedModelBinding` | A `ModelLane`, its immutable `ModelConfig`, and the registry generation read in the same snapshot |
| `ModelTurnResult` | Canonical assistant `Turn`, tool-call dispatch mirror, serving-lane provenance, usage, and exact lowered wire facts |
| `ModelCapabilities` | `context_window`, `max_output_tokens`, `supports_temperature`, `token_param`, `thinking_mode`, `supports_effort`, `supports_web_search`, `supports_tool_search`, `supports_vision`, `supports_reasoning_replay`, `supports_verbosity`, `verbosity`, `supports_pro_mode`, `reasoning_mode` |
| `UsageInfo` | `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_tokens`, `cache_read_tokens` |

`ModelLane` is frozen: a fallback, model switch, or registry reload produces a
replacement lane rather than mutating one in place. A holder of an old lane
(for example, an in-flight compaction or sub-agent) therefore completes against
one coherent backend binding or is cancelled; it never observes a mixture of
old endpoint/client state and new capabilities/configuration. Per-call operator
toggles that are intentionally live, such as reasoning replay, are re-read by
`model_turn()` through the lane's registry reference.

**OpenAIProvider** (`_openai.py`): passes messages through unchanged (they are
already in OpenAI format), including multi-part content blocks (text + images)
in tool results. Model capability lookup covers GPT-5 through GPT-5.6,
O-series, and search models (`gpt-5-search-api`) — all with `supports_vision`.
For search models, injects `web_search_options` and removes the `web_search`
function tool (the model always searches). Citations from `url_citation`
annotations are formatted as footnotes. Pre-5.6 GPT-5 models request extended
prompt-cache retention (`prompt_cache_retention: "24h"`); GPT-5.6 uses
`prompt_cache_options.ttl: "30m"`. Cache reads and writes are extracted from
`cached_tokens` and `cache_write_tokens`. Unknown models get permissive
defaults with `supports_vision=False` and use SearxNG for web search. The
`openai-compatible` lane never consults this table at all — on either API
surface (the responses pin is served by a compat-mode
`OpenAIResponsesProvider`, mirroring `AnthropicProvider(compat=True)`): a
local server serves whatever the operator named it (vLLM
`--served-model-name` is a free string), so a prefix collision with a cloud
model id must not inherit that model's sampling/effort contract — every
local model gets the plain defaults, commercial prompt-cache controls are not
injected by model-name prefix, and anything beyond those defaults is declared
on the model definition (capabilities JSON + `server_compat`), matching the
`anthropic-compatible` lane.

**AnthropicProvider** (`_anthropic.py`): converts OpenAI-format messages to
Anthropic content blocks, maps `system`/`developer` roles to the `system`
parameter, groups consecutive `tool` result messages into user-role content
blocks (converting `image_url` parts to Anthropic's `image` source format),
and translates tool schemas from OpenAI function-calling format to
Anthropic's `input_schema` format. Supports both manual and adaptive thinking
modes, with effort parameter support for models like Claude Opus 4.6 and
Sonnet 4.6. Replaces the `web_search` function tool with Anthropic's native
`web_search_20250305` server-side tool — Claude decides when to search, the
API executes it, and results stream back as `server_tool_use` /
`web_search_tool_result` content blocks (emitted as `info_delta` for UI
display). Automatic prompt caching is enabled via top-level `cache_control:
{"type": "ephemeral"}` — the API places the cache breakpoint on the last
cacheable block and advances it as conversations grow (90% input cost
reduction on cache hits, 1.25x write on first turn). Cache metrics
(`cache_creation_input_tokens`, `cache_read_input_tokens`) are extracted from
the stream's usage events. The `anthropic` SDK is a core
dependency — the Anthropic provider is first-class alongside OpenAI.

**GoogleProvider** (`_google.py`): extends `OpenAIChatCompletionsProvider` for
the Gemini `/v1beta/openai/` endpoint. Uses a single default
`ModelCapabilities` (2M context window, 65K max output tokens,
`token_param=max_tokens`) since Google updates models frequently. No static
per-model capability table. Google's endpoint is wire-compatible with the
OpenAI SDK, so no extra dependency is needed.

**Factory functions** (`__init__.py`): `create_provider(name)` returns a
singleton provider instance (thread-safe). `create_client(name, base_url,
api_key)` creates the appropriate SDK client.

### Multi-Model Registry

`ModelRegistry` (`turnstone/core/model_registry.py`) manages named model
configurations so workstreams can use different LLM backends.

**Config format:**
```toml
[models.local]
base_url = "http://localhost:8000/v1"
model = "qwen3-32b"
max_concurrency = 1
# provider defaults to "openai"

[models.claude]
provider = "anthropic"
api_key = "sk-ant-..."
model = "claude-opus-4-6"
context_window = 200000

[models.openai]
base_url = "https://api.openai.com/v1"
api_key = "sk-..."
model = "gpt-5"
context_window = 400000

[models.gemini]
provider = "google"
model = "gemini-2.5-pro"

[model]
default = "local"
fallback = ["claude", "openai"]
agent_model = "claude"
```

Each `[models.*]` entry produces a `ModelConfig` with a `provider` field
(default: `"openai"`). Supported values: `"openai"`, `"anthropic"`, `"google"`,
`"openai-compatible"`, and `"anthropic-compatible"`.

**Atomic bindings and reloads:** `ModelConfig` is frozen. Registry
`resolve_binding()` acquires the registry lock once and returns the client,
model ID, config, provider, and monotonic registry generation from the same
snapshot. `resolve_model_binding()` turns those values into one frozen
`ResolvedModelBinding` for `ChatSession`. A completed `reload()` increments the
registry generation; each new send compares by equality and replaces the whole
binding on mismatch. Failed reload validation changes neither maps nor
generation. If only non-transport fields changed, compatible client pools can
remain warm, but the session still receives a new coherent config/lane.

**Per-alias admission:** Each registry alias owns a stable, hot-resizable
`ModelAdmission`. `max_concurrency = 0` is unlimited; a positive value limits
simultaneous generations for that alias in one process. Every registry-backed
role carries the same gate on its `ModelLane`, so main turns, judges, task
agents, perception, compaction, and background generation coordinate through
one FIFO. Two aliases never share a gate implicitly, even when their URLs are
identical. `model_turn()` materializes attachment fallbacks before admission,
then holds one lease across eager stream creation and the complete drain,
releasing before retry backoff. Admission wait is credited out of deadline
accounting, preventing queued judges from spending their request budget before
dispatch. The gate survives cap-only reloads in place; the field is excluded
from semantic `ModelConfig` equality so a capacity edit does not reset judges
or output-guard state.

Primary loops, recursive compaction, judges, title generation, audio, and task
agents all consume `ModelLane` rather than inspecting provider/client handles.
Fallback is a lane change, so retry classification and result provenance come
from the lane that actually served the call. A recursive compaction pins one
lane for all leaf summaries and the merge; a hot reload never splices two model
definitions into one summary transaction.

**Model-backend authentication:** A model definition's `auth_mode` is one of
`static`, `entra_obo`, `entra_app`, or `rfc8693_obo`. Dynamic modes keep only
authorization parameters (`obo_audience`, and RFC 8693 `obo_scopes`) in the
immutable `ModelConfig`; access tokens are never stored on the registry client
or lane. `model_backend_auth.resolve_model_backend_auth_token()` joins that
pinned config with the initiating generation's principal and the process-owned
mint client immediately before dispatch. `model_turn()` checks cancellation
before and after the potentially blocking mint, then installs the credential
on a per-call `client.with_options(api_key=...)` clone that reuses the cached
transport.

Delegated modes fail closed without an initiating user. App identity does not
require one. A keyless dynamic alias always fails if minting is unavailable;
an alias with an explicit static key may fall back only when the operator has
not enabled `model.auth_fail_closed`. Registry install/reload refuses dynamic
auth when the process lacks the protected token store, and grant-profile
mismatches are surfaced at the swap boundary. The default `static` path does
not invoke any OIDC/OBO machinery. See
[Settings](settings.md#model-backend-authentication) and
[OIDC](oidc.md#model-gateway-credentials) for operator configuration.

**Per-model sampling overrides:** Each model can specify `temperature`,
`max_tokens`, and `reasoning_effort` to override the global defaults from
ConfigStore. When unset (`NULL`), the global default is used.

**Per-model reasoning persistence:** Two booleans on `model_definitions`
(migration 052) control how reasoning text round-trips:

* `surface_persisted_reasoning` (default `True`) — gates whether stored
  reasoning text is surfaced on `/history` payloads for UI rehydration.
  **Storage of reasoning bytes happens regardless of this flag** — they
  ride in `provider_data` independently. Phase-1 admin UI label "Surface
  persisted reasoning."
* `replay_reasoning_to_model` (default `False`) — gates whether stored
  reasoning blocks are sent back to the provider on subsequent turns.
  Capability-gated: `ModelCapabilities.supports_reasoning_replay` must
  also be `True` for the wire path to actually replay (canonical OpenAI
  gpt-5*/o-series and Anthropic Claude entries set it; unknown / local-
  server models default to `False`).

Three reasoning paths are recognised:

| Path | Provider | Capture | Persist | Replay |
|------|----------|---------|---------|--------|
| 1 | Anthropic Messages API | `thinking_delta` | `provider_blocks` (`type="thinking"`) | Verbatim via `_provider_content` |
| 2 | OpenAI Responses (gpt-5*, o-series) | `response.reasoning_text.delta` events | `provider_blocks` (`type="reasoning"`) — only when `include=["reasoning.encrypted_content"]` | `ResponseReasoningItemParam` input items |
| 3 | OpenAI Chat Completions (vLLM, llama.cpp, Gemini-compat) | `delta.reasoning_content` Pydantic extras | Synthetic `{type: "reasoning_text", text, source}` block stamped at end-of-stream | None — no API surface for replay on Chat Completions |

Cross-provider safety is enforced by `ANTHROPIC_VALID_BLOCK_TYPES` (a
shape filter in `_anthropic.py:_convert_messages`): foreign blocks
(OpenAI `reasoning`, synthetic `reasoning_text`) fall through to the
text+tool_calls rebuild path rather than reaching Anthropic's input
boundary as malformed content.

```toml
[models.local]
base_url = "http://localhost:8000/v1"
model = "qwen3-32b"
temperature = 0.7
max_tokens = 8192

[models.o3]
base_url = "https://api.openai.com/v1"
api_key = "sk-..."
model = "o3"
reasoning_effort = "high"
# temperature omitted — uses global default
```

An optional `[models.*.capabilities]` sub-table overrides per-model
`ModelCapabilities` flags (useful for local models whose capabilities
cannot be detected programmatically):

```toml
[models.qwen-vl]
base_url = "http://localhost:8000/v1"
model = "qwen-3.5-vl"

[models.qwen-vl.capabilities]
supports_vision = true
```

**Anthropic-compatible local servers (vLLM `/v1/messages`):** the
`"anthropic-compatible"` provider drives local servers that expose
Anthropic's Messages API for arbitrary checkpoints — vLLM's
`/v1/messages` endpoint, which requires a release with thinking-block
support in the Anthropic endpoint (post-2026-02-28; verified against
v0.22.1rc1). The lane reuses `AnthropicProvider` in compat mode: same
wire translation as the real Anthropic lane, but every model resolves to
the `_ANTHROPIC_COMPAT_DEFAULT` capabilities (200K context, 64K output,
`token_param=max_tokens`, `thinking_mode=none`, no native
web_search/tool_search, no vision) — the static Claude table never
applies to local checkpoints. `base_url` is required — the server root
WITHOUT `/v1` (the Anthropic SDK appends `/v1/messages`); a trailing
`/v1` pasted out of openai-compatible habit is stripped automatically,
and an empty value fails at client construction rather than falling
back to the commercial endpoint. Set a
placeholder `api_key` (e.g. `"dummy"`) for unauthenticated servers. Tool calling
needs the server started with `--enable-auto-tool-choice
--tool-call-parser <family>` plus the matching reasoning parser.
Per-model capability overrides opt in to what the checkpoint actually
supports:

```toml
[models.vllm-claude]
provider = "anthropic-compatible"
base_url = "http://localhost:8000"   # no /v1 — the SDK appends /v1/messages
api_key = "dummy"
model = "deepseek-ai/DeepSeek-V4-Flash"

[models.vllm-claude.capabilities]
supports_vision = true                    # multimodal checkpoints only
supports_mid_conversation_system = true   # template-dependent
context_window = 131072
thinking_mode = "manual"                  # session effort knob drives the template toggle
thinking_param = "enable_thinking"        # Qwen/Gemma key; "thinking" for Granite/DeepSeek
```

Reasoning control does NOT use Anthropic's `thinking` request param —
the levers live in the chat template, reached through
`chat_template_kwargs` in the request body. Two channels, dynamic first:

* **Session effort knob (dynamic).** Set the model's thinking mode to
  "Effort-knob controlled" in the admin Models form (or
  `thinking_mode = "manual"` + `thinking_param` under
  `[models.*.capabilities]`) and the provider maps the session's
  reasoning-effort knob onto the template toggle per-request: effort
  `none` sends `{<thinking_param>: false}`, any other level sends
  `true` — the same contract as the real lane's manual mode. ("Always
  on" / `thinking_mode = "adaptive"` instead always sends `true`: the
  model self-regulates, so the knob never force-disables — mirroring
  the native adaptive branch.) The graded effort value always rides
  alongside the toggle: under `effort_param` when the operator names
  the template's key, else under the conventional fallback key
  (`reasoning_effort`) on the anthropic-compatible lane — the user's
  effort setting always reaches the wire, and a template that doesn't
  reference the kwarg ignores it. On the openai-compatible lane the
  undeclared-key case rides the flat top-level `reasoning_effort`
  param instead (the documented compat field), forwarded verbatim.
  Optional `reasoning_effort_values` / `default_reasoning_effort`
  validate the knob before it reaches the server; without declared
  values the knob is forwarded as-is. The knob is ordinal, and validation
  respects that: an off-list knob value rounds UP onto the declared
  list and a value above the ceiling rides the ceiling
  (`snap_reasoning_effort`) — asking for more effort than the model
  declares never falls back to a lower default tier.  The knob's
  `none` position is forwarded verbatim when the model declares an
  explicit `none` level (gpt-5.1+, grok-4.3) — omitting it there would
  leave a reasoning-on server default (e.g. gpt-5.5's `medium`) in
  charge of a knob that promises off — and omitted otherwise; `none`
  is never a snap target for other positions.
  `default_reasoning_effort` only catches values the ordinal snap
  cannot rank (custom strings). Declare values that match the
  template's documented vocabulary: for DeepSeek-V4, which officially
  accepts `high`/`max` (Think High is the default thinking tier;
  `low`/`medium` alias to `high`, `xhigh` to `max`), a
  `("high", "max")` values list reproduces the official aliasing
  exactly — `low`/`medium` round up to `high`, `xhigh` to `max` —
  and freeform passthrough matches it too. To map an undocumented
  template, probe with per-request `chat_template_kwargs` and compare
  `input_tokens`. Setting `effort_param` also suppresses the
  flat top-level `reasoning_effort` request param on the
  openai-compatible lane — the template channel replaces it, never
  doubles it. With the default `thinking_mode = "none"` nothing is
  injected and the server's template default decides.

  Upgrade note: before 1.7.0a7 the openai-compatible lane sent the
  toggle unconditionally `true` whenever thinking mode was enabled. A
  stored per-model `reasoning_effort = "none"` now disables thinking
  on such models — pick any real level (or clear the override) to keep
  it on. Also since 1.7.0a7 the effort level itself always reaches the
  wire on the local lanes (previously dropped unless
  `reasoning_effort_values` was declared): flat `reasoning_effort` on
  openai-compatible, the `effort_param`-or-fallback template key on
  anthropic-compatible when reasoning control is engaged.
* **Operator pin (static).** Entries under `{"chat_template_kwargs":
  ...}` in the admin Models extra-body field ride the SDK's
  `extra_body` unconditionally and win over the knob mapping on key
  collision — e.g. pin `{"enable_thinking": true}` to keep thinking on
  regardless of the session knob. (Server type and API surface remain
  openai-compatible-only knobs and stay hidden for this provider.)

The same knob mapping drives the `openai-compatible` lane's Chat
Completions requests — `merge_reasoning_template_kwargs` is shared by
both local-server lanes, so `thinking_mode`/`thinking_param`/
`effort_param` mean the same thing whichever endpoint serves the model.
Only the Responses API surface (native reasoning) ignores it.

The console surfaces this projection as an *effective effort ladder*:
the admin model form's per-model effort select and the skill
launch-config effort select annotate each position with what the
request will carry, in plain words — a position whose delivered level
matches its name stays plain ("Max"), a snapped position says so
("Low — sends high"), the adaptive lanes' none position warns
"thinking stays on", and budget detail lives in the tooltip. A
position is never labeled after a sibling that shares its wire (that
rendered "Max (= minimal)", implying a downgrade the wire doesn't
contain). Computed server-side by `providers/effort_ladder.py` from
the same mapping functions the providers use at request time and
shipped on `/v1/api/models` rows (every row carries `effort_ladder`,
empty when the capabilities column fails to parse) and
`POST /v1/api/admin/models/effort-ladder`. The ladder describes what
Turnstone sends — a server-side template may alias further (DeepSeek-V4
folds `low`/`medium` into its default `high` tier).

The `anthropic-compatible` lane never sends Anthropic's native
`thinking`/`output_config` params — they are not in vLLM's request
schema. The real `anthropic` provider is unaffected: official Claude
models keep native thinking, budget mapping, and `output_config`
effort. A gateway fronting *real* Claude on a Messages-shaped URL
(e.g. a LiteLLM `anthropic/` route to the Claude API) should use
`provider = "anthropic"` with a custom `base_url`, which keeps the
native thinking params.

Verified quirks of vLLM's Anthropic endpoint:

* The `thinking` request param is silently dropped — use
  `chat_template_kwargs` (above) to control reasoning.
* `stop_sequences` cut the raw stream wherever the text appears —
  including inside thinking — and report `end_turn` with
  `stop_sequence=None`. Turnstone does not send stop sequences from
  this provider.
* No cache telemetry: `usage` carries input/output token counts only
  (no `cache_creation_input_tokens` / `cache_read_input_tokens`).
* Images require a multimodal checkpoint — text-only models return a
  500 on image blocks, so `supports_vision` stays opt-in per model.
* Mid-conversation `role: "system"` turns are template-dependent —
  opt in per model via `supports_mid_conversation_system`.

**Database model definitions:** On server entry points, models can also be
defined in the `model_definitions` table (admin Models tab). DB models support
the same per-model sampling overrides and per-alias `max_concurrency`.
Config.toml models override DB models with the same alias in-memory (the DB
rows are never modified).

**Lifecycle:**
1. `load_model_registry()` loads DB model definitions (if storage available),
   then overlays `[models.*]` from config.toml, then builds a `"default"` entry
   from CLI `--base-url`/`--model`/`--api-key` args
2. The registry is passed to the session factory closure in both `cli.py` and
   `server.py`; each workstream resolves its model on creation
3. `ModelRegistry.get_client()` lazily creates SDK client instances via
   `create_client()` — `OpenAI` for the openai provider, `Anthropic` for
   the anthropic provider (thread-safe via `_client_lock`)
4. `ModelRegistry.get_provider()` lazily creates `LLMProvider` instances via
   `create_provider()` (also cached and thread-safe)
5. `/model` command shows available models; `/model <alias>` switches the
   active workstream's client, model, context window, and per-model sampling
   parameters
6. `_model_turn_with_fallback()` tries the primary lane, then each fallback
   alias's lane in order if the primary is unreachable
7. `_run_agent()` resolves `registry.agent_model` (if set) for task
   sub-agents, allowing a cheaper model for autonomous loops

**Per-workstream selection:** `POST /v1/api/workstreams/new` accepts an optional
`"model"` field, along with `skill` (skill name)
which can override the model before workstream creation.

### Tool Output Truncation

Tool execution results (bash, read_file, search) are truncated by
`_truncate_output()` when they exceed `tool_truncation` characters. Truncation
preserves the first half and last half of the output, with a message in
between:

```
... [N chars truncated — output exceeded LIMIT char limit] ...
```

The default limit is 50% of the context window in characters (computed as
`context_window * chars_per_token * 0.5`). For a 131K context window this is
~262K characters. Override with `--tool-truncation <chars>`.

This truncation message is visible to the model, so it knows output was cut.

During the send loop the limit is additionally capped by the remaining
context budget, and three guarantees apply when that budget reaches zero
(#883):

- **Structural floor** — orchestration handles (`spawn_workstream`,
  `spawn_batch`, `wait_for_workstream`, `tasks`) and error results are
  always admitted up to a guaranteed floor (2048 chars, head+tail beyond
  it), because a lost `ws_id` or a masked failure wedges the session.
- **Small-result pass** — results at or under the floor pass verbatim,
  funded from a bounded per-batch grace pool (2× the floor) so a wide
  batch of small results cannot collectively bypass budget accounting;
  past the pool they get the drop notice instead.
- **Honest drop notice** — a bulky non-structural result is replaced by an
  explicit `Error: tool result dropped — context budget exhausted…` notice
  stating the call ran but its output could not be admitted (never a
  successful-looking trim).

A zero budget also triggers one mid-turn auto-compaction before results are
sized. With `max_tokens ≥ context_window/4` the response reserve zeroes the
budget near 70% fullness — below the default 80% auto-compact threshold —
and without this trigger a session could idle in that band indefinitely
with every tool result floored or dropped. The trigger keys on the
exhausted budget itself, not on any threshold, so it composes with any
operator-set `auto_compact_pct`: with thresholds below the zero point the
ordinary owed-compaction paths fire first and this trigger degrades to a
backstop for the cases where they bailed or freed too little.

---

## Persistence

### Canonical Trajectory

The durable and in-memory conversation shape is the provider-neutral `Turn`
from `turnstone.core.trajectory`. It is flat and role-discriminated: portable
text and content-addressed `AttachmentRef` values form `content`; assistant
turns may carry byte-exact `ToolCall` arguments; tool turns link back by call
ID; and provenance, SSE cursors, effect records, and display-only facts live in
wire-invisible `TurnMeta`.

`ProviderNative` is the one opaque lane for reasoning and server-side tool
blocks that cannot be normalized safely. It replays only to its producing
provider; another provider rebuilds the request from neutral fields. Signed,
encrypted, and structured blocks remain opaque, while trust-boundary lowering
copies and defangs editable top-level text so native replay cannot resurrect a
forged session marker. Attachment bytes never ride in a `Turn`; each output
boundary resolves its ordered references from the blob store.

Storage rehydrates canonical Turns, and `model_turn()` is the sole lowering and
re-ingest boundary. OpenAI-like dict adapters remain a compatibility bridge for
legacy consumers, not a second source of trajectory truth.

### Storage Architecture

Persistence is managed by the `turnstone.core.storage` package — a pluggable
backend behind a `StorageBackend` protocol. The `memory.py` facade provides
backward-compatible module-level functions that delegate to the active backend.

```
ChatSession / SessionManager / HTTP lifecycle
        |
        +-- generation FIFO durability / StateWriter incarnation fence
        v
    memory.py + storage._registry
        ↓
  ┌─────────────┐    ┌──────────────────┐
  │ SQLiteBackend │    │ PostgreSQLBackend │
  │ (FTS5 search) │    │ (tsvector/ILIKE)  │
  └─────────────┘    └──────────────────┘
        ↓                     ↓
    storage._schema  (SQLAlchemy Core tables — single source of truth)
        ↓
    storage._migrate  (programmatic Alembic)
```

**SQLite** is the default (zero-config, single file at `.turnstone.db`).
**PostgreSQL** is the production backend (connection pooling, `tsvector`
full-text search). Select via `[database]` in `config.toml`, CLI flags, or
environment variables (`TURNSTONE_DB_BACKEND`, `TURNSTONE_DB_URL`).

Schema migrations are managed by Alembic and run automatically on startup.
Existing SQLite databases created before the migration system are auto-stamped
at the baseline revision.

### Tables

The complete schema includes governance, identity, project, attachment, model,
and operations tables. The lifecycle/trajectory core is:

```sql
structured_memories
  memory_id, name, type, scope, scope_id, content, timestamps, access counters

workstreams
  ws_id       TEXT PRIMARY KEY        -- logical workstream identity
  node_id     TEXT                    -- owning service cache / routing hint
  user_id     TEXT                    -- owner
  alias       TEXT UNIQUE
  title       TEXT
  name        TEXT NOT NULL
  state       TEXT NOT NULL           -- creating | live states | closed/deleted
  kind        TEXT NOT NULL           -- interactive | coordinator
  parent_ws_id, project_id, persona, skill_id, skill_version
  created, updated

conversations
  id            INTEGER PRIMARY KEY AUTOINCREMENT
  ws_id         TEXT NOT NULL
  timestamp     TEXT NOT NULL
  role          TEXT NOT NULL        -- user | assistant | tool | system
  content       TEXT
  tool_name     TEXT
  tool_call_id  TEXT                 -- links TOOL Turn to assistant call
  tool_calls    TEXT                 -- assistant ToolCall tuple as JSON
  provider_data TEXT                 -- opaque producer-native block lane
  _source       TEXT                 -- operator/compaction provenance
  event_id      BIGINT               -- per-workstream SSE resume cursor
  is_error      BOOLEAN
  attachments   TEXT                 -- ordered content-addressed refs
  meta          TEXT                 -- source, effect, preview side metadata

workstream_config
  ws_id       TEXT NOT NULL          -- composite PK with key
  key         TEXT NOT NULL
  value       TEXT
  -- private durable incarnation token also lives here but is filtered from
  -- every ordinary config read/snapshot

conversations_fts                    -- SQLite FTS5 virtual table (optional)
  content     (content=conversations, content_rowid=id)
```

Table definitions live in `storage/_schema.py` (SQLAlchemy Core `Table` objects)
and are the single source of truth for both backends and Alembic migrations.

### StorageBackend Protocol

| Method | Purpose |
|--------|---------|
| `register_workstream(..., fork_reservation_token=...)` | Atomically insert `creating` row plus private incarnation token; report collision |
| `ensure_workstream_incarnation_snapshot(ws_id)` | Lock and return one exact row plus its private token, installing a token atomically for legacy rows |
| `finalize_deferred_create(...)` | Apply alias/config/node writes only if row and token still match |
| `publish_deferred_create(ws_id, token)` | Compare-and-swap the exact reservation from `creating` to `idle` |
| `delete_workstream_if_fork_reserved(ws_id, token)` | Hard-delete only the exact durable incarnation that owns the token |
| `delete_stale_creating_reservations(...)` | Atomically reap eligible crash-abandoned reservations with complete dependent and attachment-refcount cleanup |
| `save_message(ws_id, role, content, ...)` | Persist one canonical-turn row and its side channels |
| `load_message_turns(ws_id, checkpointed=True)` | Rehydrate canonical `Turn` objects, bounded by the latest valid compaction checkpoint |
| `load_messages(ws_id, include_compaction=...)` | Materialized display/export projection; optionally surface compaction cards |
| `clone_workstream(source, destination, ..., expected_session=...)` | Transactionally compare source and destination incarnations, authorize, and copy canonical history/config/project/attachment ownership |
| `get_compaction_watermark/floor/checkpoint(...)` | Maintain resume checkpoints without deleting audit history |
| `update_workstream_state(...)` | Persist a lifecycle state after manager/state-writer fencing |
| `resolve_workstream(alias_or_id)` | Resolve alias, exact ID, or ID prefix |
| `search_history(...)` | Full-text search (FTS5 on SQLite, tsvector on PostgreSQL) |
| `close()` | Release resources (connection pool, engine) |

### Database Configuration

```toml
[database]
backend = "sqlite"                  # "sqlite" | "postgresql"
path = ".turnstone.db"              # SQLite file path
url = ""                            # PostgreSQL connection URL
pool_size = 2                       # PostgreSQL connection pool size (per process)
```

Environment variables: `TURNSTONE_DB_BACKEND`, `TURNSTONE_DB_URL`, `TURNSTONE_DB_PATH`,
`TURNSTONE_DB_POOL_SIZE`.

The default pool is intentionally small (2 base + 3 overflow = 5 per process)
because most database operations are short-burst queries. Atomic workstream
forks are the deliberate exception: their serializable history/configuration/
attachment clone can hold a connection for longer. For clusters with many nodes
sharing PostgreSQL, use [PgBouncer](pgbouncer.md) in transaction pooling mode
and size its server pool for expected concurrent fork traffic.

### Persistence and Resume

`ws_id` is the persistent conversation/lifecycle identity. There is no
separate `session_id`: `workstreams` holds lifecycle, owner, kind, hierarchy,
project, and display metadata, while `conversations` stores the append-only
trajectory. `node_id` is an owning-service hint; rendezvous/service liveness,
not that historical field alone, determines cluster routing and orphan safety.

`ChatSession.messages` is `list[Turn]`. Persistence serializes the neutral
fields, opaque provider-native lane, attachment references, SSE cursor, and
side metadata independently. Provider lowering is never stored as canonical
history. TOOL Turns may carry a wire-invisible typed `EffectStatus` in `meta`:
`committed`, `none`, `unknown`, `partial`, or `rolled_back`. Ordinary completed
results can leave the field unset; cancellation/compensation paths use it when
deterministic consumers must distinguish “definitely did nothing” from “may
have acted.” The prose result remains what the model sees.

**Auto-titling:** After the first complete exchange, auxiliary model work
generates a bounded title and stores it in `workstreams.title`. It runs through
the same immutable lane/`model_turn()` seam as other model-backed roles and is
cancelled or discarded if its workstream identity changes before publication.

**Resume flow:** `ChatSession.resume(ws_id)` calls
`load_message_turns(checkpointed=True)` and adopts canonical Turns:

- Current `user`, `assistant`, `tool`, and `system` rows map directly; legacy
  split tool-call/result rows are normalized by the reconstruction boundary.
- Tool results retain error, effect, preview, and attachment metadata; opaque
  provider-native blocks replay only to their producing provider.
- **Interrupted conversation repair:** If the last assistant message has
  `tool_calls` but fewer tool results than expected (conversation was
  interrupted mid-execution), the incomplete turn is stripped so the
  model can regenerate cleanly. Live cancellation normally prevents this shape
  by synthesizing explicit results for unanswered calls.
- **Compaction checkpoint:** the latest valid marker reconstructs as a
  provenance-tagged `[USER summary label, ASSISTANT summary] + [rows after
  watermark]` view. A missing or
  corrupt watermark fails safe to the full transcript. Export/audit callers
  request `checkpointed=False`, so compaction never erases source history.
- The `ChatSession` adopts the resumed `_ws_id`, so new messages continue
  in the same workstream.

**Config persistence:** LLM-affecting parameters (`temperature`,
`reasoning_effort`, `max_tokens`, `instructions`, and the persona
snapshot — see `docs/personas.md`) are persisted to the
`workstream_config` table on creation and whenever changed via slash
commands. `resume()` restores these values so resumed workstreams
behave identically to the original.

**`/clear` vs `/new`:** `/clear` wipes in-memory context but preserves
messages in the database for future resume. `/new` starts a fresh workstream
(new `_ws_id`), leaving the old workstream resumable.

**Resolution:** `resolve_workstream()` accepts aliases, exact workstream IDs,
or ID prefixes, enabling `turnstone --resume refactor` or `/resume abc12`.

**Workstream listing:** `list_workstreams_with_history()` only returns
workstreams that have at least one saved message (`WHERE EXISTS` on
`conversations`). Workstreams registered but never used (e.g., from process
startup) are invisible until a message is sent.

**Workstream pruning:** `prune_workstreams(retention_days, log_fn)` runs once
at startup (CLI and server). It deliberately excludes internal `creating`
reservations, which belong to the crash-recovery path above. It removes:

- Published workstreams with no messages (orphaned registrations)
- Unnamed workstreams (`alias IS NULL`) older than `retention_days` days (default 90)

Named (aliased) workstreams are never age-pruned. Configure with
`--retention-days N` (0 = disable age pruning).

---

## Error Handling and Retry

### API Retry

Every model call streams (#831); retry lives at two stacked layers:

- **Caller ladders** — `ChatSession._model_turn_with_retry()` (chat
  loop, one ladder per lane) and the agent `_api_call()` (drained via
  `model_turn`) use the same pattern: 4 total attempts (1 initial + 3 retries,
  `_MAX_RETRIES = 3`), exponential backoff base 1 second
  (`delay = 1s * 2^attempt`), `ui.on_info()` on retry, exception
  propagates on final failure. `_compact_messages()` wraps its drained
  call in the same loop.
- **`model_turn`'s drain ladder** — inside every single-shot call,
  mid-stream deaths (errors raised while draining, e.g.
  `IncompleteStreamError`) are re-issued up to 2 more times with a
  0.5s-base exponential backoff (±50% jitter); request-time failures
  keep the SDK's own retry policy. The two ladders stack
  multiplicatively on transient-shaped failures.
- **Retryable errors** are matched by class name against each
  provider's `retryable_error_names` (avoids importing
  backend-specific exception hierarchies): `RateLimitError`,
  `APITimeoutError`, `APIConnectionError`, `InternalServerError`,
  `ServiceUnavailableError`, `APIError`, plus the drained-transport
  errors `IncompleteStreamError` (stream ended with no terminal
  signal — for servers that never send one, declare
  `finish_reason_optional` in the model's capabilities JSON) and
  `ResponsesStreamFailedError` (transient in-band Responses failure).

### Finish Reason Handling

`_stream_response()` tracks `finish_reason` from the final streaming chunk:

- **`"length"`**: warns via `ui.on_error()` that the response was truncated.
  Any partial tool calls are discarded (their JSON would be malformed),
  causing the `send()` loop to exit cleanly.
- **`"content_filter"`**: warns via `ui.on_error()` that the response was
  blocked.

Agent sub-sessions (`_run_agent()`) check `finish_reason` on each
drained turn and stop the agent early on `"length"` or
`"content_filter"`.

`_compact_messages()` checks `finish_reason` on the compaction response and
warns if the summary was truncated.

### State Emission on Errors

- `send()` catches `KeyboardInterrupt` and generic `Exception`: calls
  `_emit_state("error")` before re-raising
- On interrupt: partial tool results and the originating assistant message
  are popped from `self.messages` to keep state consistent

### Web UI Resilience

- **SSE reconnect**: both `connectContentSSE()` and `connectGlobalSSE()` use
  exponential backoff on `onerror` -- starting at 1 second, doubling on each
  failure, capped at 30 seconds. On successful message, delay resets to 1s.
- **Disconnection indicator**: `#status-bar.disconnected` class turns the
  status text red and shows "Reconnecting..."
- **Fetch error handling**: all `fetch()` calls use `.catch()` to prevent
  unhandled promise rejections
- **Pending approval across tab switches**: `WebUI._pending_approval` stores
  the `approve_request` event payload while the session is blocked waiting
  for user response. On tab switch / reconnect the pane reloads history via
  REST `GET /history` and then reconnects SSE; the live approval event is
  re-injected. The server-side `project_history_messages` projection marks
  the trailing orphan tool-call turn `"pending": true` so `replayHistory`
  skips the false `✓ approved` badge; the live approval UI is rendered by
  the re-injected event instead.
- **Browser history integration**: `history.pushState` is called in
  `switchTab()` with `{turnstone: 'workstream', wsId}`. The initial state is
  seeded with `history.replaceState({turnstone: 'dashboard'})` on load. The
  `popstate` listener restores the correct tab or shows the dashboard,
  guarded by `_historyNavigation = true` to prevent re-entrant pushState.
- **Pane focus**: `mousedown` and `focusin` events on pane containers update
  `focusedPaneId`. Approval shortcuts (y/n/a) apply to the focused pane.
  `Ctrl+Alt+Arrow` cycles focus between panes.

### Eval Resilience

`_run_single_test()`: wraps `session.send_headless()` in a retry loop (3
attempts) to avoid transient API errors from poisoning evaluation scores.

### Backend Health Tracking

`BackendHealthTracker` (`turnstone/core/healthcheck.py`) records LLM backend
health passively from real request outcomes — there is no probe thread and no
circuit breaker, and requests are never blocked. Two states:

```
healthy  ──(failure_threshold consecutive failures)──>  degraded
degraded ──(any success)───────────────────────────>  healthy
```

- `record_success()` fires at the request-accepted instant: the streaming
  consumer's `on_stream_armed` hook, driven by the eager `cancel_ref` append
  every adapter performs at HTTP-response time.
- `record_failure()` fires once per lane's whole creation ladder, in
  `ChatSession._model_turn_with_fallback` / `_try_fallback_lane`. A mid-stream
  death (the stream armed, then died) records neither — it belongs to the
  re-issue ladder, not the fallback walk. `BackendAuthUnavailableError` and
  `WirePreparationError` also record nothing: an auth refusal is fail-closed
  configuration policy and a wire-preparation fault is session data — neither
  says anything about the backend.
- `is_degraded` is advisory ordering, not admission: the fallback walk tries
  non-degraded aliases first and degraded ones as a last resort, and the
  primary lane is always dialed.
- `HealthTrackerRegistry` keys trackers by `(provider, base_url)` so aliases
  sharing a backend share one tracker. The `/health` endpoint projects the
  same trackers: `"status": "ok"` when the backend is healthy, `"degraded"`
  otherwise.

### Rate Limiting

`RateLimiter` (`turnstone/core/ratelimit.py`) enforces per-client-IP request
limits using a token-bucket algorithm. Each IP gets a `TokenBucket` with
`requests_per_second` (refill rate) and `burst` (bucket capacity) from
`[ratelimit]` config.

- Applied via `RateLimitMiddleware` after authentication but before route dispatch.
- `/health` and `/metrics` are exempt (monitoring must always be reachable).
- **X-Forwarded-For support**: when `trusted_proxies` is configured (comma-separated
  CIDRs), the middleware parses the `X-Forwarded-For` header using the
  rightmost-untrusted approach. IPv4-mapped IPv6 addresses are normalized.
  The direct client IP must be in the trusted set before XFF is considered.
- On limit exceeded: HTTP 429 with `Retry-After` header and JSON body
  `{"error": "Rate limit exceeded", "retry_after": N}`.
- The `turnstone_ratelimit_rejected_total` counter is incremented on each
  rejection.

---

## User Identity and Authentication

Turnstone supports three authentication mechanisms, unified behind an
`AuthResult` dataclass that carries `user_id`, `scopes`, and `token_source`:

1. **API tokens** — database-backed, prefixed `ts_`, stored as SHA-256 hashes
   in the `api_tokens` table. Can be exchanged for JWTs via
   `POST /v1/api/auth/login`.
2. **JWTs** — short-lived HMAC-SHA256 session tokens (default 24h) issued after
   successful credential validation. Contain `sub` (user_id), `scopes`, and
   `src` (origin) in claims.

### Scope Model

Three hierarchical scopes control endpoint access:

| Scope | Grants | Endpoints |
|-------|--------|-----------|
| `read` | SSE streams, workstream listing, history | GET endpoints |
| `write` | `read` + send, command, workstream create/close | POST to `/api/workstreams/{ws_id}/send`, `/api/command`, etc. |
| `approve` | `write` + tool approval, admin operations | POST to `/api/workstreams/{ws_id}/approve`, `/api/admin/*` |

### Middleware Flow

`AuthMiddleware` (ASGI) intercepts every request:

1. **Public path check** — `/`, `/static/*`, `/shared/*`, `/health`,
   `/metrics`, `/openapi.json`, `/docs`, `/api/auth/*`, and `/api/auth/setup`
   are always allowed.
2. **Token extraction** — `Authorization: Bearer <token>` header first, then
   surface-scoped auth cookie (`turnstone_auth_server` on the node server,
   `turnstone_auth_console` on the console) as fallback.
3. **Token type detection** — dots in the token indicate JWT; `ts_` prefix
   indicates API token.
4. **Validation** — JWT signature check or API token hash lookup in storage.
5. **Scope check** — `required_scope(method, path)` determines the minimum
   scope; the request is rejected with 403 if the token lacks it.
6. **Context propagation** — on success, `ctx_user_id` is set so structured
   logging includes the authenticated identity on every log event.

### Architecture Split

- **Console** is the auth management hub — it hosts the admin endpoints for
  creating users, issuing API tokens, and managing channel mappings. User
  records and token hashes live in the shared storage backend. The console
  dashboard includes an **admin panel** for managing
  credentials, governance, MCP servers, models, node metadata, and runtime
  settings through the browser.
- **Server** is a JWT validator only — it validates tokens on each request but
  never creates users or tokens. Both processes share the same `jwt_secret`
  (via `TURNSTONE_JWT_SECRET` env var or `[auth].jwt_secret` config).
- **First-time setup** — both server and console expose
  `POST /v1/api/auth/setup`, a public endpoint that creates the initial admin
  user when no users exist. This avoids the chicken-and-egg problem of needing
  `approve` scope to create the first user via `/api/admin/users`.

### Auth Storage Tables

Three tables in `storage/_schema.py` support identity:

```sql
users
  user_id        TEXT PRIMARY KEY
  username       TEXT NOT NULL UNIQUE
  display_name   TEXT NOT NULL
  password_hash  TEXT NOT NULL       -- bcrypt
  created        TEXT NOT NULL

api_tokens
  token_id       TEXT PRIMARY KEY
  token_hash     TEXT NOT NULL UNIQUE  -- SHA-256 of raw token
  token_prefix   TEXT NOT NULL         -- first 8 chars for display
  user_id        TEXT NOT NULL
  name           TEXT NOT NULL         -- human-readable label
  scopes         TEXT NOT NULL         -- comma-separated
  created        TEXT NOT NULL
  expires        TEXT                  -- optional expiry timestamp

channel_users
  channel_type      TEXT NOT NULL      -- e.g. "slack", "discord"
  channel_user_id   TEXT NOT NULL      -- platform-specific user ID
  user_id           TEXT NOT NULL      -- FK to users
  PRIMARY KEY (channel_type, channel_user_id)
```

See [docs/security.md](security.md) for full security details including token
lifecycle, password hashing, and deployment hardening.

---

## Threading Model

### CLI

```
Main thread          Spinner thread (daemon)       ThreadPoolExecutor
+--------------+     +------------------+          +-----------------+
| REPL loop    |     | Braille animation|          | Tool execution  |
| input() ->   |     | 80ms tick to     |          | max_workers=4   |
|   send() ->  |     | stderr           |          | parallel tools  |
|   stream  -> |     | started/stopped  |          | run concurrently|
|   tools   -> |     | by TerminalUI    |          |                 |
+--------------+     +------------------+          +-----------------+
       |                    ^                              ^
       +-- on_thinking_start/stop -------------------------+
       +-- _execute_tools ---------------------------------+
```

Key constraint: `input()` blocks the main thread. The spinner writes to
stderr so it does not interfere with readline. Tool execution may use a
`ThreadPoolExecutor` with up to 4 workers for parallel tool calls.

### Server

```
Starlette ASGI app (served by uvicorn)
  |
  +-- Async request handlers (all under /v1/ prefix)
  |     POST /v1/api/workstreams/{ws_id}/send    -> starts worker thread per workstream
  |     POST /v1/api/workstreams/{ws_id}/approve -> resolves one ApprovalCycle
  |     POST /v1/api/workstreams/new             -> hidden create/commit, then optional worker
  |     GET  /v1/api/workstreams/{ws_id}/events  -> SSE via EventSourceResponse (per workstream)
  |     GET  /v1/api/events/global               -> SSE via EventSourceResponse (fan-out)
  |
  +-- ASGI middleware stack
  |     MetricsMiddleware -> CORSMiddleware -> AuthMiddleware -> RateLimitMiddleware
  |
  +-- Worker thread per workstream (daemon)
  |     Runs session.send() synchronously -- ChatSession is fully blocking
  |     Blocks on the addressed ApprovalCycle.event when human input is needed
  |     A force-cancel can abandon the slot; generation fences retire late output
  |
  +-- Background daemon threads
        Global SSE fan-out: reads global_queue, copies to per-client queues
        Idle cleanup: closes stale workstreams, cleans rate limiter buckets
```

Starlette handles all HTTP routing, CORS, and middleware. uvicorn runs
the ASGI application with async request handling. All API endpoints live
under the `/v1/` prefix via a Starlette `Mount`. An OpenAPI 3.1 spec is
generated from Pydantic v2 models and served at `/openapi.json`; Swagger
UI is available at `/docs`. SSE endpoints use `EventSourceResponse` from
`sse-starlette` with async generators that bridge sync `queue.Queue` via
`asyncio.get_running_loop().run_in_executor()`.

`ChatSession.send()` remains synchronous, running in daemon worker threads.
`SessionUIBase` keeps per-cycle `threading.Event` objects and per-listener
`queue.Queue` primitives. Several task-agent approval cycles may be live while
the workstream still has one main worker slot. The `_global_fanout_thread` and
`_idle_cleanup_thread` remain daemon threads because they interact with sync
primitives. A lifespan context manager handles startup/shutdown (health
monitor, MCP client, registry).

Each workstream's `WebUI` has:
- `_listeners` (per-client SSE queues, fan-out on `_enqueue()`)
- `_approval_cycles` (ordered `cycle_id -> ApprovalCycle`; each owns its event)
- `_global_queue` (class variable, shared, for state broadcasts)

The SSE handlers bridge these sync queues to async via
`run_in_executor()`, polling `queue.Queue.get(timeout=1)` while
`sse-starlette` handles keepalive pings automatically.

### Workstream Threading (CLI)

```
Main thread                  Background workstream thread
+------------------+         +---------------------------+
| REPL input()     |         | session.send()            |
| /ws commands     |         | streams response          |
| active workstream|         | executes tools            |
| send() inline   |         | approve_tools() ->        |
+------------------+         |   _fg_event.wait() BLOCKS |
       |                     +---------------------------+
       |                                ^
       +-- /ws <N> switch ------------->|
       |   old.set_foreground(False)    |
       |   new.set_foreground(True)     |
       |   new.flush_buffer()           |
       +-- _fg_event.set() unblocks --->+
```

When a background workstream needs approval, its `WorkstreamTerminalUI`
calls `_fg_event.wait()`, which blocks the worker thread until the user
switches to that workstream. The `_bg_attention_notify` callback writes a
bell + status line to stderr to alert the user.

### Cluster Console

```
Monitoring (2 daemon threads)        Control + Proxy (async Starlette)
+------------------+                 +----------------------------+
| Node discovery   |                 | POST /v1/api/cluster/      |
| Service registry |                 |   workstreams/new          |
| every 60 seconds |                 |   → POST to target server  |
+------------------+                 +----------------------------+
| SSE manager      |                 | GET /node/{node_id}/       |
| asyncio loop     |                 |   → httpx.AsyncClient      |
| 1 task per node  |                 |     proxy to server_url    |
| /events/global   |                 | GET /node/{id}/v1/api/workstreams/{ws_id}/events |
| snapshot+deltas  |                 |   → SSE stream proxy                              |
+------------------+                 | POST /node/{id}/v1/api/workstreams/{ws_id}/send   |
                                     |   → forwarded to server                           |
                                     +----------------------------+
```

The console HTTP layer is a Starlette/ASGI app served by uvicorn. The SSE
endpoint uses `EventSourceResponse` with the same listener queue pattern as
the main server. `ClusterCollector` runs two daemon threads: a discovery loop
that queries the service registry every 60 seconds, and an SSE manager that
runs a single asyncio event loop multiplexing persistent SSE connections to
all nodes via `GET /v1/api/events/global`. Each node delivers a full snapshot
on connect followed by real-time delta events — state changes, health
transitions, and aggregate metrics arrive sub-second instead of on a 15-second
poll cycle.

The console has two write-path capabilities:

1. **Workstream creation** — sends HTTP requests to target server nodes
   to create workstreams. Auto-selects the node with
   the most available capacity if no target is specified. When a `skill`
   field is present, the server resolves the skill BEFORE `mgr.create()`
   (applying the model override to the creation request) and snapshot-applies
   remaining settings (auto-approve, token budget, temperature, etc.) to the
   workstream config AFTER creation.

2. **Reverse proxy** — serves each node's server UI through the console port at
   `/node/{node_id}/`. Uses `httpx.AsyncClient` to proxy HTTP and SSE traffic.
   A JS shim is injected into the server's `app.js` to override `fetch()` and
   `EventSource()`, routing root-relative URLs through the proxy prefix. This
   eliminates the need for direct network access to individual server nodes.

The console also performs **version drift detection** — flagging when nodes
report different versions via the `/health` endpoint. The overview API includes
`version_drift` and `versions` fields; the dashboard shows a yellow warning
indicator when versions diverge.

Clicking a workstream row in the console opens the proxied server UI at
`/node/{node_id}/?ws_id=<id>` — the server's JS parses this on load and
auto-selects the workstream. See [docs/console.md](console.md) for the full
API reference.

---

## Conversation Compaction

When the prompt exceeds `auto_compact_pct` of the context window (default:
80%, configurable via `--auto-compact-pct`), `ChatSession` auto-compacts by
summarizing the selected trajectory into a structured summary. Manual
`/compact` claims a normal generation; automatic compaction remains owned by
the send generation that triggered it. Both use the same cancellation,
publication, and FIFO durability fences as a model turn.

Compaction pins one `ModelLane` for the complete operation. Blocks are packed
to an estimated window budget; a real provider overflow recursively
subdivides the batch and merges partial summaries rather than silently dropping
the newest messages. The summary preserves:

- Decisions made (architecture, libraries, approaches)
- Files read, created, or modified
- Exact identifiers, paths, and code snippets
- Important tool results
- Open tasks
- User preferences

Tool-call tails needed by an in-flight batch can be preserved verbatim. Auto
compaction can also carry the last real user request and a bounded verbatim
wind-down. Coordinator compaction appends exact task/child handle mappings from
storage instead of asking the summarizer to transcribe opaque IDs.

The final swap is one generation commit:

```
full in-memory trajectory
    -> [USER summary label, ASSISTANT summary, preserved tail]
       (both synthetic Turns carry source="compaction")
    -> successful compaction end event
    -> ordered durable checkpoint marker {watermark, token counts, trigger}
```

The marker does not replace or delete source rows. Its watermark says which
prefix the summary covers. Resume loads the latest valid summary plus rows
after that boundary; `/history`, export, search, and audit retain the full
transcript and omit or explicitly project checkpoint markers as appropriate.
A malformed checkpoint falls back to full reconstruction rather than risking
message loss.

Cancellation or generation supersession before the final commit leaves both
the live trajectory and checkpoint untouched. Typed `compaction` lifecycle
events (`start`, `progress`, exactly one `end`) let SSE clients correlate and
retire one run even when a force-abandoned predecessor finishes after a
successor generation begins. After a successful swap, `_read_files` is cleared
so edits require fresh file reads against content no longer present in the
bounded model context.

---

## Client SDK

> See also: [SDK Architecture diagram](diagrams/png/13-sdk-architecture.png) | [SDK Documentation](sdk.md)

The `turnstone/sdk/` package provides typed HTTP clients for programmatic access
to both the server and console APIs. It wraps REST endpoints with methods that
return Pydantic models, and SSE endpoints with async/sync iterators that yield
typed event dataclasses.

**Two client pairs** (sync + async):

- `TurnstoneServer` / `AsyncTurnstoneServer` — server API (workstreams, chat, streaming)
- `TurnstoneConsole` / `AsyncTurnstoneConsole` — console API (cluster overview, nodes, workstreams)

**Design**: async-first with thin sync wrappers. `_BaseClient` provides httpx
setup, auth headers, `_request()` (REST) and `_stream_sse()` (SSE). Sync
clients delegate through `_SyncRunner` which maintains a persistent background
event loop on a daemon thread.

**Event types**: standalone dataclasses in `events.py` with a type-registry
dispatch (`from_json()` on each event). Events are decoupled from server
internals — the SDK parses SSE frames directly from the `/v1/api/events`
streams.

**TypeScript SDK**: `sdk/typescript/` — separate npm package with the same API
surface. Zero browser dependencies, SSE via `fetch` + `ReadableStream` parsing.

```python
# Python quick start
from turnstone.sdk import TurnstoneServer

with TurnstoneServer("http://localhost:8080", token="tok_xxx") as client:
    ws = client.create_workstream(name="demo")
    result = client.send_and_wait("Hello!", ws.ws_id)
    print(result.content)
```

---

## Channel Integrations

> See also: [Channel Integrations guide](channels.md)

The `turnstone-channel` gateway connects external messaging platforms
(Discord and Slack today, with an adapter protocol for future platforms) to
the turnstone cluster via HTTP. Each
platform adapter implements the `ChannelAdapter` protocol and translates
between platform-native events and turnstone server API calls.

The `ChannelRouter` manages bidirectional routing: it maps platform
channel/thread IDs to turnstone workstream IDs, handles workstream
creation and stale-route recovery, and resolves platform users to
turnstone identities via the `channel_users` table. A persisted route is usable
only when its source still resolves in storage **and** is loaded in the owning
manager: direct mode checks the server's manager-authoritative active list,
while console mode uses the routed read-only live probe. Probe, routing, and
authorization uncertainty propagates instead of being treated as a stale miss.

When a saved source is not live, the router passes its ID as `resume_ws` on a
new workstream request. Despite that compatibility name, the server atomically
forks the source's saved conversation into a distinct destination ID; the
source remains unchanged. The old mapping stays durable until the replacement
and any initial message succeed, then moves to the fork. A fresh create is
attempted once only when the fork returns the exact source-not-found response
and a second authoritative storage lookup confirms that the source is gone;
ACL, conflict, routing, and storage failures leave the old route intact. The
clone and publication happen in one create lifecycle, eliminating the old
resume-then-send ordering gap.

Discord and Slack adapters ship today. See [channels.md](channels.md) for
setup instructions, configuration reference, and the adapter development
guide.

### Notification Subsystem

The `notify` tool enables the LLM to send notifications to users or
channels directly. The server calls the channel gateway
directly over HTTP for lower latency: `_exec_notify()` queries the
`services` database table for healthy channel gateways (heartbeat within
120 seconds), authenticates with a service JWT (`aud: turnstone-channel`),
and POSTs to `POST /v1/api/notify` on the first healthy gateway. The
payload includes the originating `ws_id` for reply routing. The gateway
validates the JWT, resolves the target (username lookup via
`channel_users` or direct `channel_type`+`channel_id`), and delegates to
`ChannelAdapter.send_notification()` which sends the message and tracks
the outgoing message ID → `(ws_id, target_user_id)` mapping. Delivery
retries up to 3 times with backoff, re-querying the service registry on
each attempt. See [Notification Flow diagram](diagrams/png/17-notify-flow.png).

**Bidirectional replies:** When a user replies to a notification DM, the
channel adapter (Discord or Slack) looks up the originating `ws_id` from the
tracked message ID, verifies the replying user matches the notification
recipient, and routes the reply to the workstream via `router.send_message()`.
The workstream's response is forwarded back to the DM via a temporary entry
in `_notify_reply_channels`. On `TurnCompleteEvent`, the response message is
itself tracked for further replies, enabling multi-turn DM conversations
without requiring the user to open the web UI. Tracking entries are capped
at 100 (FIFO eviction) and cleaned up on workstream close.

---

## Governance

> See also: [Governance documentation](governance.md) | [Governance Architecture diagram](diagrams/19-governance-architecture.puml)

Turnstone governance extends the Phase 1 auth system with role-based access
control (RBAC), tool execution policies, skills, usage tracking,
and audit logging. The permission model has two layers: legacy scopes
(`read`, `write`, `approve`) checked by `AuthMiddleware`, and granular
permissions checked per-endpoint by `require_permission()`. Three built-in
roles (admin, operator, viewer) are seeded by migration 008; custom roles
can be created with any permission subset. JWTs carry both `scopes` and
`permissions` claims for backward compatibility.

Tool policies use glob pattern matching (`fnmatch`) with priority-ordered
first-match-wins evaluation to control tool execution (allow/deny/ask).
Skills provide reusable system messages with `{{variable}}` substitution
plus session configuration (model, temperature, auto-approve, token budget,
etc.). Usage events are recorded per-LLM-request for token accounting.
An append-only audit log captures all admin mutations.

Skills are snapshot-applied once at workstream creation — not a live binding.
The `prompt_templates` table (which stores skills) supports auto-versioning,
and workstreams record which skill and version spawned them. Token budget
enforcement tracks consumption in `session.send()` with 80% warning and
100% approval gate via the `__budget_override__` synthetic tool name.

The console admin panel exposes these capabilities through permission-gated
administration surfaces rather than treating navigation visibility as
authorization.
Both Python and TypeScript SDKs expose governance methods on the console
client.

## Intent Validation

> See also: [Intent Validation guide](judge.md) | [Judge Architecture diagram](diagrams/png/22-judge-architecture.png)

Intent validation provides advisory risk assessments for tool calls that
require human approval. The system runs a two-tier evaluation pipeline
implemented in `turnstone/core/judge.py`:

1. **Heuristic tier** (synchronous, sub-millisecond) -- A priority-ordered
   rule table using fnmatch tool patterns and regex argument patterns. Four
   severity levels: critical (deny), high (review), medium (review), low
   (approve). First match wins. The heuristic verdict is attached to the
   `approve_request` SSE event immediately.

2. **LLM judge tier** (asynchronous daemon coordinator) -- A bounded worker
   set evaluates independent calls from the batch. Each evaluation receives
   conversation context and tool-call details, may use `read_file` /
   `list_directory` to gather evidence (with security-hardened path blocking),
   and produces a structured JSON verdict. `judge.parallel_evaluations`
   controls the per-batch width from 1 through 16; the judge alias's model
   admission gate remains the process-wide ceiling. If an LLM verdict has
   higher confidence than the heuristic, it replaces it via an
   `intent_verdict` SSE event.

The main judge is session-scoped (`IntentJudge`) and lazy-initialized on first
approval; each evaluation carries its own cancellation/generation identity.
Task-agent tool calls use the same intent pipeline in independent
`agent_gate` generations, so parallel siblings do not supersede each other's
judge work. Each human-gated batch is joined to its own `ApprovalCycle`, and a
late verdict must match that cycle's call ID and judge identity before it can
reach Smart Approvals. Superseded verdicts remain durable audit facts but are
withheld from live decision caches. Server and console behavior comes from the
database-backed `judge.*` settings; the interactive CLI reads its flags and
`config.toml` `[judge]` values. Self-consistency, cross-model, and
cross-provider bindings all use the same `ModelLane` / backend-auth seam.
Verdicts persist in `intent_verdicts` with the exact user or automatic
decision, enabling calibration.
The console exposes `GET /v1/api/admin/verdicts` for audit queries
(requires `admin.judge` permission).
