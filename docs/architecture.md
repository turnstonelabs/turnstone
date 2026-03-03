# Turnstone Architecture

Turnstone is an AI orchestration platform with tool use, parallel workstreams, and persistent
memory. It connects to any OpenAI-compatible API (local vLLM, OpenAI, etc.) and
gives the model 14 built-in tools plus external tools via MCP (Model Context
Protocol) for reading, writing, searching, planning, and executing code.

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
| `turnstone-bridge` | `turnstone.mq.bridge` | Bridge | Message queue ↔ HTTP API bridge |
| `turnstone-console` | `turnstone.console.server` | ClusterCollector | Cluster dashboard (aggregates all nodes) |
| `turnstone-eval` | `turnstone.eval` | `NullUI` | Headless evaluation and prompt optimization |

---

## Module Map

```
turnstone/
  cli.py              Terminal frontend (TerminalUI, WorkstreamTerminalUI, REPL)
  server.py           Web frontend (WebUI, HTTP handler, static-file serving)
  eval.py             Evaluation harness (HeadlessSession, scoring, prompt optimization)
  core/
    session.py        ChatSession engine, SessionUI protocol, tool dispatch
    workstream.py     Parallel workstream manager (WorkstreamState, Workstream, WorkstreamManager)
    tools.py          Tool schema loader (JSON -> OpenAI function-calling format)
    mcp_client.py     MCPClientManager — MCP server connections, tool discovery, async-sync bridge
    memory.py         SQLite persistence (conversations, memories, FTS5 search)
    metrics.py        Prometheus-compatible metrics collector (MetricsCollector)
    edit.py           File edit utilities (find_occurrences, pick_nearest)
    safety.py         Command safety validation (blocked patterns, sanitization)
    sandbox.py        Math code sandboxing (AST validation, subprocess execution)
    web.py            Web utilities (HTML stripping, SSRF prevention)
  mq/
    protocol.py       Inbound/outbound message dataclasses (JSON serialization)
    broker.py         Abstract MessageBroker protocol + RedisBroker
    bridge.py         Bridge service (queue ↔ turnstone-server HTTP API)
    client.py         TurnstoneClient library + TurnResult for external systems
  console/
    collector.py      ClusterCollector — aggregates state from all nodes via Redis + HTTP
    server.py         Cluster dashboard HTTP server + SSE + CLI entry point
    static/           Cluster dashboard web UI (HTML, CSS, JS)
  ui/
    colors.py         ANSI color constants with NO_COLOR support
    markdown.py       Streaming terminal markdown renderer (line-buffered)
    spinner.py        Braille character spinner (daemon thread)
    static/
      index.html      Single-page app shell (links to CSS and JS)
      style.css       All UI styles (dark/light themes, dashboard, approval blocks)
      app.js          All client-side JavaScript (SSE, workstreams, dashboard, markdown)
  tools/
    *.json            14 tool schemas (OpenAI function-calling format + turnstone metadata)
```

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
 _full_messages()  ------------>  system_messages + self.messages
     |
     v
 _emit_state("thinking")
     |
     v
 _create_stream_with_retry()  ---->  client.chat.completions.create(stream=True)
     |                                  up to 3 retries (4 total attempts), exponential backoff
     v
 _stream_response(stream)  -------->  dispatch tokens to UI:
     |                                  on_reasoning_token() / on_content_token()
     |                                  accumulate tool_calls from deltas
     |                                  track finish_reason
     v
 finish_reason check:
     +--- "length"  --> warn, discard partial tool_calls
     +--- "content_filter" --> warn
     v
 tool_calls present?
     |
     +--- No ---> _print_status_line() -> _emit_state("idle") -> return
     |
     +--- Yes --> _emit_state("running")
                    |
                    v
                  _execute_tools(tool_calls)  <--- three-phase pipeline (see below)
                    |
                    v
                  append tool results to self.messages
                    |
                    v
                  loop back to _full_messages()
```

### Tool Execution Pipeline

> See also: [Tool Pipeline diagram](diagrams/png/05-tool-pipeline.png)

Tool execution is a three-phase process:

```
Phase 1: PREPARE (serial)
  For each tool_call:
    _prepare_tool(tc)
      -> parse JSON arguments (with regex fallback for malformed JSON)
      -> dispatch to _prepare_{tool_name}(call_id, args)
      -> validate inputs, build preview text
      -> return item dict with: header, preview, needs_approval, execute fn

Phase 2: APPROVE (serial, blocking)
  _emit_state("attention")
  ui.approve_tools(items)
    -> display all headers and previews
    -> if any need approval and not auto_approve: prompt user
    -> return (approved, feedback)
  _emit_state("running")

Phase 3: EXECUTE (parallel)
  if len(items) == 1:
    run_one(items[0])
  else:
    ThreadPoolExecutor(max_workers=4).map(run_one, items)
  Bash tool streams stdout line-by-line via ui.on_tool_output_chunk(call_id, line)
  Final output (stdout + stderr) delivered via ui.on_tool_result(call_id, name, output)
  call_id links tool_info items → streaming chunks → final result
  For plan tool: post-execution gate via ui.on_plan_review()
```

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
  "attention"  --->  waiting for user approval / plan review
      |
      v
  "running"   --->  executing approved tools
      |
      v
  "idle"       --->  no more tool calls, turn complete
      |
  (or "error"  --->  exception or KeyboardInterrupt)
```

---

## SessionUI Protocol

> See also: [Core Engine Classes diagram](diagrams/png/03-core-engine-classes.png)

Defined in `turnstone.core.session.SessionUI` as a `typing.Protocol` with 14
methods. Every frontend must implement all of them.

```python
class SessionUI(Protocol):
    def on_thinking_start(self) -> None: ...
    def on_thinking_stop(self) -> None: ...
    def on_reasoning_token(self, text: str) -> None: ...
    def on_content_token(self, text: str) -> None: ...
    def on_stream_end(self) -> None: ...
    def approve_tools(self, items: list[dict]) -> tuple[bool, str | None]: ...
    def on_tool_result(self, call_id: str, name: str, output: str) -> None: ...
    def on_tool_output_chunk(self, call_id: str, chunk: str) -> None: ...
    def on_status(self, usage: dict, context_window: int, effort: str) -> None: ...
    def on_plan_review(self, content: str) -> str: ...
    def on_info(self, message: str) -> None: ...
    def on_error(self, message: str) -> None: ...
    def on_state_change(self, state: str) -> None: ...
    def on_rename(self, name: str) -> None: ...  # propagate alias to tab/UI label
```

`on_rename` is called by the `/name` command (on success) and after a successful `/resume` (if the resumed session has an alias or title). `WebUI.on_rename` broadcasts a `ws_rename` event on the global SSE channel and updates the in-memory `Workstream.name`; `TerminalUI.on_rename` is a no-op.

### Three Implementations

| Class | Module | Notes |
|-------|--------|-------|
| `TerminalUI` | `turnstone.cli` | ANSI colors, `MarkdownRenderer`, `Spinner`, readline-based `input()` for approval |
| `WebUI` | `turnstone.server` | SSE event queue per workstream, `threading.Event` for blocking on approval/plan |
| `NullUI` | `turnstone.eval` | Discards all output; `approve_tools` always returns `(True, None)` |

### WorkstreamTerminalUI

`WorkstreamTerminalUI` (in `turnstone.cli`) extends `TerminalUI` with workstream
awareness:

- **Output buffering**: When in background (`is_foreground` is False), tokens
  are appended to `_output_buffer` instead of written to stdout. When the user
  switches to this workstream, `flush_buffer()` replays them.

- **Approval blocking**: `approve_tools()` and `on_plan_review()` call
  `_fg_event.wait()` when in background, blocking the worker thread until the
  workstream is foregrounded. This ensures the user sees the approval prompt
  in the correct context.

- **Foreground/background toggle**: `set_foreground(bool)` sets or clears
  `_fg_event` (a `threading.Event`). The manager calls this during `/ws <N>`
  switches.

---

## Workstream Architecture

Workstreams are parallel, independent chat sessions. Each has its own
`ChatSession`, `SessionUI`, message history, and worker thread.

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
    id: str                              # uuid hex, 8 chars
    name: str                            # user-visible label
    state: WorkstreamState               # current state
    session: ChatSession | None          # the conversation engine
    ui: SessionUI | None                 # frontend adapter
    worker_thread: threading.Thread | None
    error_message: str
    last_active: float                   # time.monotonic() timestamp, updated on every state change
    _lock: threading.Lock                # per-workstream state lock
```

### WorkstreamManager

```python
class WorkstreamManager:
    MAX_WORKSTREAMS = 10

    def __init__(self, session_factory: Callable[[SessionUI], ChatSession]): ...
    def create(self, name="", ui_factory=None) -> Workstream: ...
    def close(self, ws_id: str) -> bool: ...
    def close_idle(self, max_age_seconds: float) -> list[str]: ...  # auto-close stale IDLE workstreams
    def get(self, ws_id: str) -> Workstream | None: ...
    def get_active(self) -> Workstream | None: ...
    def list_all(self) -> list[Workstream]: ...
    def switch(self, ws_id: str) -> Workstream | None: ...
    def switch_by_index(self, index: int) -> Workstream | None: ...
    def set_state(self, ws_id, state, error_msg=""): ...  # updates last_active
```

The `session_factory` pattern decouples session creation from configuration.
The factory captures shared config (client, model, temperature, etc.) and
accepts only a `SessionUI`, so the manager can create sessions without knowing
API details.

### Idle Workstream Lifecycle

The web server runs a background `_idle_cleanup_thread` (daemon) that calls
`WorkstreamManager.close_idle()` periodically (every `timeout / 4`, max 5 min).
Any IDLE workstream whose `last_active` is older than the configured timeout is
closed; non-IDLE workstreams (THINKING, RUNNING, ATTENTION, ERROR) are never
touched. The last workstream is always preserved even if expired. On close, a
`ws_closed` event is broadcast on the global SSE channel so browser clients
remove the tab immediately. Controlled by `--workstream-idle-timeout` (default:
120 minutes, 0 = disable).

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
  (CSS `@keyframes pulse` animation per state).
- **Per-tab SSE**: `connectContentSSE(wsId)` opens
  `/api/events?ws_id=<id>` for the active tab's event stream.
- **Global SSE**: `connectGlobalSSE()` opens `/api/events/global` which
  receives `ws_state` broadcasts from all workstreams, used to update tab
  indicators without switching.
- **New tab / close**: POST `/api/workstreams/new`, POST `/api/workstreams/close`.

### Thread Safety

- `WorkstreamManager._lock`: guards `_workstreams` dict and `_order` list on
  all create/close/switch/list operations.
- `Workstream._lock`: guards per-workstream state mutations in `set_state()`.
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
| `agent` | `bool` | Include this tool when running as a plan/task sub-agent |
| `task_agent` | `bool` | Include this tool when running as a task sub-agent |
| `auto_approve` | `bool` | Tool is read-only; skip user approval |
| `primary_key` | `str` | Fallback argument name for bare-string JSON recovery |

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
  "agent": true,
  "task_agent": true,
  "auto_approve": true,
  "primary_key": "path"
}
```

At import time, `turnstone.core.tools._load_tools()` strips the metadata keys
from each schema and builds:

- `TOOLS` -- list of `{"type": "function", "function": {...}}` dicts for the API
- `AGENT_TOOLS` -- subset with `agent: true`
- `TASK_AGENT_TOOLS` -- subset with `task_agent: true`
- `AGENT_AUTO_TOOLS` / `TASK_AUTO_TOOLS` -- sets of tool names with `auto_approve: true`
- `PRIMARY_KEY_MAP` -- `{name: primary_key}` for JSON fallback recovery
- `merge_mcp_tools(builtin, mcp_tools)` -- merges built-in + MCP tools at session init

### 14 Tools by Category

**Read-only (auto-approve)**:
- `read_file` -- read file contents with optional offset/limit
- `search` -- ripgrep-based codebase search
- `man` -- read man pages
- `recall` -- retrieve stored memories

**Write (requires approval)**:
- `bash` -- execute shell commands (with safety checks via `turnstone.core.safety`)
- `write_file` -- create or overwrite a file
- `edit_file` -- string replacement in an existing file (requires prior `read_file`)
- `math` -- execute Python in sandboxed subprocess (via `turnstone.core.sandbox`)
- `web_fetch` -- fetch a URL (with SSRF protection via `turnstone.core.web`)
- `web_search` -- search the web via Tavily API

**Agent (delegated sub-sessions)**:
- `task` -- delegate to a sub-agent with full tool access (`TASK_AGENT_TOOLS`)
- `plan` -- explore codebase and write a structured plan (`AGENT_TOOLS`)

**Memory (persistent key-value store)**:
- `remember` -- save a fact
- `forget` -- delete a fact

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

`task` and `plan` invoke `_run_agent()`, which runs a multi-turn loop with
a subset of tools and its own system prompt. The sub-agent runs
independently, then returns the final content as the tool result.

- **task**: uses `self._task_tools` (`TASK_AGENT_TOOLS` + MCP tools)
- **plan**: uses `self._agent_tools` (`AGENT_TOOLS` + MCP tools). Writes output
  to `.plan-<session_id>.md` — unique per `ChatSession` so concurrent workstreams
  don't collide. On repeat invocations the prior `plan` tool call and its result
  are forwarded from `self.messages` so the agent refines the existing plan rather
  than starting over. Planning instructions are injected as a developer message
  prepended to the agent's conversation.
- **Turn limit**: controlled by `agent_max_turns` (default: `-1`, unlimited).
  When a limit is set and reached, the agent is forced to synthesize a final
  response without tools. When unlimited, the loop only exits when the model
  stops calling tools or hits `finish_reason: "length"`.
- **Retry**: each API call in the agent loop uses the same retry+backoff logic
  as the main `_create_stream_with_retry()`.
- **Finish reason handling**: `finish_reason: "length"` stops the agent early
  and returns whatever content was generated. `finish_reason: "content_filter"`
  returns a placeholder.

### MCP Tool Integration

`MCPClientManager` (`turnstone/core/mcp_client.py`) connects to external MCP servers
and exposes their tools alongside built-in tools. The MCP SDK is fully async; turnstone
bridges this with a background asyncio event loop in a daemon thread.

**Lifecycle:**
1. `create_mcp_client()` reads server configs from TOML or JSON
2. `MCPClientManager.start()` launches the background event loop thread
3. `_connect_all()` connects to each server (stdio subprocess or HTTP), runs
   `initialize()` + `list_tools()`, converts schemas to OpenAI format
4. `ChatSession.__init__` receives the manager and builds `self._tools` (built-in + MCP)
5. `_prepare_tool()` routes MCP tools to `_prepare_mcp_tool()` / `_exec_mcp_tool()`
6. `_exec_mcp_tool()` calls `call_tool_sync()` which dispatches to the async loop
   via `asyncio.run_coroutine_threadsafe()`

**Tool naming:** `mcp__{server}__{tool}` — double underscore delimiter, validated
at connection time (server names with `__` are rejected).

**Error isolation:** Per-server connection failures are caught and logged; other
servers still connect. Tool execution errors return error strings to the LLM
rather than crashing the session.

### Tool Output Truncation

Tool execution results (bash, read_file, search, math, man) are truncated by
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

---

## Persistence

### Database

SQLite via `turnstone.core.memory`. Database file: `.turnstone.db` in the
current working directory (overridable via `memory.db_override` for eval
isolation).

### Tables

```sql
memories
  key      TEXT PRIMARY KEY
  value    TEXT NOT NULL
  created  TEXT NOT NULL
  updated  TEXT NOT NULL

sessions
  session_id  TEXT PRIMARY KEY
  alias       TEXT UNIQUE            -- user-assigned short name (nullable)
  title       TEXT                   -- LLM-generated title (nullable)
  created     TEXT NOT NULL
  updated     TEXT NOT NULL          -- bumped on every save_message()

conversations
  id            INTEGER PRIMARY KEY AUTOINCREMENT
  session_id    TEXT NOT NULL
  timestamp     TEXT NOT NULL
  role          TEXT NOT NULL        -- user | assistant | tool_call | tool_result
  content       TEXT
  tool_name     TEXT
  tool_args     TEXT
  tool_call_id  TEXT                 -- links tool_call ↔ tool_result for resume

conversations_fts                    -- FTS5 virtual table
  content     (content=conversations, content_rowid=id)
```

The `tool_call_id` column was added via schema migration (`ALTER TABLE`) for
backwards compatibility with existing databases.

### Key Functions

| Function | Purpose |
|----------|---------|
| `open_db()` | Open/create database, run migrations, initialize tables |
| `load_memories()` | Return all `(key, value)` pairs sorted by key |
| `save_message(session_id, role, content, ...)` | Log a message to conversations (accepts `tool_call_id`) |
| `search_history(query, limit)` | Full-text search via FTS5 (falls back to LIKE) |
| `search_history_recent(limit)` | Return most recent messages |
| `register_session(session_id, title)` | Create a sessions row (no-op if exists) |
| `update_session_title(session_id, title)` | Set/update LLM-generated title |
| `set_session_alias(session_id, alias)` | Set user-friendly alias (returns False if taken) |
| `get_session_name(session_id)` | Return alias if set, else title, else None |
| `resolve_session(alias_or_id)` | Resolve alias, exact id, or id prefix to full session_id |
| `list_sessions(limit)` | List sessions with ≥1 message, ordered by updated DESC |
| `load_session_messages(session_id)` | Reconstruct OpenAI message format from DB rows |
| `delete_session(session_id)` | Delete session and all its messages |
| `prune_sessions(retention_days, log_fn)` | Remove empty sessions and old unnamed sessions; called at startup |
| `normalize_key(key)` | Normalize memory keys (`lower`, replace `-`/` ` with `_`) |
| `fts5_query(query)` | Convert plain text to safe FTS5 query (quoted terms) |

### Session Persistence and Resume

Each `ChatSession` generates a 12-char hex `_session_id` on creation and
registers it in the `sessions` table. Messages are saved to `conversations`
as they happen via `save_message()`.

**Auto-titling:** After the first complete exchange (user message + assistant
response), a background thread calls the LLM with a title-generation prompt
(`reasoning_effort: "low"`, `max_completion_tokens: 200`). The generated
title (3-8 words) is stored in `sessions.title`.

**Resume flow:** `ChatSession.resume_session(session_id)` calls
`load_session_messages()` which reconstructs the OpenAI message format from
database rows:

- `user` and `assistant` rows map directly
- Consecutive `tool_call` rows are grouped into one assistant message's
  `tool_calls` array, paired with subsequent `tool_result` rows via
  `tool_call_id` (or positional matching for legacy data)
- **Interrupted session repair:** If the last assistant message has
  `tool_calls` but fewer tool results than expected (session was
  interrupted mid-execution), the incomplete turn is stripped so the
  LLM can re-generate cleanly
- The session adopts the old `_session_id`, so new messages continue in
  the same session

**Config persistence:** LLM-affecting parameters (`temperature`,
`reasoning_effort`, `max_tokens`, `instructions`, `creative_mode`) are
persisted to the `session_config` table on creation and whenever changed
via slash commands. `resume_session()` restores these values so resumed
sessions behave identically to the original.

**`/clear` vs `/new`:** `/clear` wipes in-memory context but preserves
messages in the database for future resume. `/new` starts a fresh session
(new `_session_id`), leaving the old session resumable.

**Resolution:** `resolve_session()` accepts aliases, exact session IDs, or
session ID prefixes, enabling `turnstone --resume refactor` or `/resume abc12`.

**Session listing:** `list_sessions()` only returns sessions that have at
least one saved message (`WHERE EXISTS` on `conversations`). Sessions
registered but never used (e.g., from process startup) are invisible until
a message is sent.

**Session pruning:** `prune_sessions(retention_days, log_fn)` runs once at
startup (CLI and server). It removes:
- Sessions with no messages (orphaned registrations)
- Unnamed sessions (`alias IS NULL`) older than `retention_days` days (default 90)

Named (aliased) sessions are never age-pruned. Configure with
`--session-retention-days N` (0 = disable age pruning).

---

## Error Handling and Retry

### API Retry

`ChatSession._create_stream_with_retry()` (streaming path) and the agent
`_api_call()` (non-streaming) both use the same retry pattern:

- **Retries**: 4 total attempts (1 initial + 3 retries, `_MAX_RETRIES = 3`)
- **Backoff**: exponential, base 1 second (`delay = 1s * 2^attempt`)
- **Retryable errors**: `RateLimitError`, `APITimeoutError`,
  `APIConnectionError`, `InternalServerError`, `ServiceUnavailableError`,
  `APIError` (matched by class name to avoid importing backend-specific
  exception hierarchies)
- On retry: `ui.on_info()` notification
- On final failure: exception propagates

`_compact_messages()` also wraps its non-streaming API call in the same
retry loop.

### Finish Reason Handling

`_stream_response()` tracks `finish_reason` from the final streaming chunk:

- **`"length"`**: warns via `ui.on_error()` that the response was truncated.
  Any partial tool calls are discarded (their JSON would be malformed),
  causing the `send()` loop to exit cleanly.
- **`"content_filter"`**: warns via `ui.on_error()` that the response was
  blocked.

Agent sub-sessions (`_run_agent()`) check `finish_reason` on each
non-streaming response and stop the agent early on `"length"` or
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
  for user response. On SSE reconnect (e.g., switching back to the tab),
  the event is re-injected after history replay. `_build_history` marks the
  pending tool call as `"pending": true` so `replayHistory` skips the
  false `✓ approved` badge; the live approval UI is rendered by the
  re-injected event instead.
- **Browser history integration**: `history.pushState` is called in
  `switchTab()` with `{turnstone: 'workstream', wsId}`. The initial state is
  seeded with `history.replaceState({turnstone: 'dashboard'})` on load. The
  `popstate` listener restores the correct tab or shows the dashboard,
  guarded by `_historyNavigation = true` to prevent re-entrant pushState.

### Eval Resilience

`_run_single_test()`: wraps `session.send_headless()` in a retry loop (3
attempts) to avoid transient API errors from poisoning evaluation scores.

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
ThreadedHTTPServer (ThreadingMixIn + HTTPServer, daemon_threads=True)
  |
  +-- Thread per HTTP request
  |     POST /api/send      -> worker thread per workstream
  |     POST /api/approve   -> unblocks WebUI._approval_event
  |     POST /api/plan      -> unblocks WebUI._plan_event
  |     POST /api/workstreams/new -> creates workstream + worker
  |     GET  /api/events    -> SSE long-poll (per workstream)
  |     GET  /api/events/global -> SSE long-poll (fan-out)
  |
  +-- Worker thread per workstream
  |     Runs session.send() in a loop
  |     Blocks on WebUI._approval_event / _plan_event
  |
  +-- Global SSE fan-out
        WebUI._global_queue shared across all WebUI instances
        Global SSE endpoint drains this queue
```

`ThreadingMixIn` ensures each HTTP request (including long-lived SSE
connections) gets its own thread. This is necessary because SSE connections
block indefinitely, and POST requests must be handled concurrently.

Each workstream's `WebUI` has:
- `_event_queue` (per-workstream SSE events)
- `_approval_event` / `_plan_event` (`threading.Event` for blocking)
- `_global_queue` (class variable, shared, for state broadcasts)

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

### Message Queue Bridge

```
Main thread              Global SSE thread         Per-WS SSE threads (×N)
+------------------+     +------------------+      +-------------------+
| Inbound loop     |     | GET /events/glob |      | GET /events?ws_id |
| BLPOP on Redis   |     | Parse SSE data   |      | Parse SSE data    |
| Dispatch to      |     | Forward state    |      | Forward content,  |
|   handler        |     |   changes        |      |   tool results    |
| POST to server   |     | Detect turn      |      | Handle approval   |
| Publish ACK      |     |   completion     |      |   forwarding      |
+------------------+     +------------------+      +-------------------+
       |                         |                         |
       +-- Redis inbound queue   +-- Redis pub/sub         +-- Redis pub/sub
           (RPUSH/BLPOP)             (PUBLISH)                 (PUBLISH)
                                                               + response queue
                                                                 (BLPOP on
                                                                  approval)
```

**Approval flow:** When a per-WS SSE thread receives an `approve_request`, it checks
the workstream's `auto_approve_tools` set. If all requested tools are in the set, the
bridge auto-approves via `POST /api/approve`. Otherwise, it publishes an
`ApprovalRequestEvent` to the outbound channel with a `request_id`, then blocks on
`BLPOP` of a Redis response queue (`turnstone:resp:{request_id}`) until the client pushes
a response or the approval timeout (default 300s) expires.

**Completion detection:** The bridge tracks which `correlation_id` maps to which
`ws_id` for active sends. When the global SSE reports `ws_state → idle` for a tracked
workstream, the bridge emits a synthetic `TurnCompleteEvent` with the correlation ID.

**Multi-node routing:** Each bridge has a `node_id` (defaults to hostname) and BLPOPs
from both `turnstone:inbound:{node_id}` (directed, priority) and `turnstone:inbound` (shared).
Messages with `target_node` set are pushed to the target's per-node queue. Messages
for existing workstreams are auto-routed via `turnstone:ws:{ws_id}` ownership keys in Redis.
If a bridge picks up a shared-queue message for a workstream owned by another node, it
re-routes to that node's queue (1 extra hop). Bridges publish heartbeats to
`turnstone:node:{node_id}` with configurable TTL for node discovery.

### Cluster Console

```
Event subscriber          Node discovery          Poll loop
+------------------+     +------------------+    +-------------------+
| SUBSCRIBE on     |     | SCAN node:* keys |    | For each node:    |
| events:cluster   |     | every 15 seconds |    |   GET /api/dash   |
| Apply state      |     | Add/remove nodes |    |   GET /health     |
| changes to       |     | Emit join/lost   |    | ThreadPoolExecutor|
| in-memory model  |     | events           |    | (50 workers)      |
+------------------+     +------------------+    +-------------------+
       |                         |                         |
       +-- Redis pub/sub         +-- Redis SCAN            +-- HTTP to each
           (SUBSCRIBE)               (every 15s)               server (every 10s)
```

The console is read-only — it never writes to Redis queues or sends commands to servers.
Real-time events provide instant state transitions; periodic polling provides full data
consistency (tokens, context ratios, activity strings). Clicking a workstream row in the
console opens the node's server UI with `?ws_id=<id>` for direct deep linking — the
server parses this on load and auto-selects the workstream. See [docs/console.md](console.md)
for the full API reference.

---

## Conversation Compaction

When the prompt exceeds `auto_compact_pct` of the context window (default:
80%, configurable via `--auto-compact-pct`), `ChatSession` auto-compacts by
summarizing the entire conversation into a structured summary
(`_compact_messages`). The summary model call uses `compact_max_tokens`
(default: 32768, configurable via `--compact-max-tokens`). The summary
preserves:

- Decisions made (architecture, libraries, approaches)
- Files read, created, or modified
- Exact identifiers, paths, and code snippets
- Important tool results
- Open tasks
- User preferences

After compaction, `_read_files` is cleared to force re-reads before edits,
since file contents are no longer in the message history.
