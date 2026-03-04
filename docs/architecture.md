# Turnstone Architecture

Turnstone is an AI orchestration platform with tool use, parallel workstreams, and persistent
memory. It connects to any OpenAI-compatible API (local vLLM, OpenAI, etc.) or
Anthropic's native Messages API via pluggable provider adapters, and gives the
model 14 built-in tools plus external tools via MCP (Model Context Protocol) for
reading, writing, searching, planning, and executing code.

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
    providers/        LLM provider adapters (pluggable backend layer)
      _protocol.py    LLMProvider protocol, ModelCapabilities, StreamChunk, CompletionResult
      _openai.py      OpenAIProvider — OpenAI, vLLM, llama.cpp, any compatible API
      _anthropic.py   AnthropicProvider — Anthropic Messages API, native streaming, thinking
      __init__.py     create_provider() + create_client() factory functions
    workstream.py     Parallel workstream manager (WorkstreamState, Workstream, WorkstreamManager)
    tools.py          Tool schema loader (JSON -> OpenAI function-calling format)
    mcp_client.py     MCPClientManager — MCP server connections, tool discovery, async-sync bridge
    model_registry.py ModelRegistry — named model configs, lazy client creation, fallback routing
    memory.py         Persistence facade (delegates to storage backend)
    storage/          Pluggable storage: StorageBackend protocol, SQLite + PostgreSQL
    metrics.py        Prometheus-compatible metrics collector (MetricsCollector)
    healthcheck.py    BackendHealthMonitor — periodic probe + circuit breaker
    ratelimit.py      Per-IP token-bucket rate limiter (RateLimiter, TokenBucket)
    edit.py           File edit utilities (find_occurrences, pick_nearest)
    safety.py         Command safety validation (blocked patterns, sanitization)
    sandbox.py        Math code sandboxing (AST validation, subprocess execution)
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
    events.py         27 SSE event dataclasses with type registry
    _base.py          Shared httpx async client, auth, error handling
    _sync.py          Background event loop for sync wrappers
    _types.py         TurnResult + TurnstoneAPIError
  mq/
    protocol.py       Inbound/outbound message dataclasses (JSON serialization)
    broker.py         Abstract MessageBroker protocol + RedisBroker
    bridge.py         Bridge service (queue ↔ turnstone-server HTTP API)
    client.py         TurnstoneClient library + TurnResult for MQ-based access
  console/
    collector.py      ClusterCollector — aggregates state from all nodes via Redis + HTTP
    server.py         Cluster dashboard HTTP server + SSE + CLI entry point
    static/           Cluster dashboard web UI (page-specific HTML, CSS, JS)
  shared_static/      Shared design system (base.css, auth.js, theme.js, toast.js, utils.js, kb.js)
  ui/
    colors.py         ANSI color constants with NO_COLOR support
    markdown.py       Streaming terminal markdown renderer (line-buffered)
    spinner.py        Braille character spinner (daemon thread)
    static/
      index.html      Single-page app shell (links to CSS and JS)
      style.css       Page-specific UI styles (dashboard layout, approval blocks)
      app.js          Page-specific client-side JavaScript (SSE, workstreams, markdown)
  tools/
    *.json            14 tool schemas (OpenAI function-calling format + turnstone metadata)
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
 _full_messages()  ------------>  system_messages + self.messages
     |
     v
 _emit_state("thinking")
     |
     v
 _create_stream_with_retry()  ---->  provider.create_streaming(client, model, messages, ...)
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

**Workstream eviction at capacity:** When `WorkstreamManager.create()` would
exceed `max_workstreams` (configurable via `[server].max_workstreams`, default
10), the oldest IDLE workstream is automatically evicted to make room. The
`turnstone_workstreams_evicted_total` counter is incremented on each eviction.
If no IDLE workstream is available the create request fails as before.

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
  `/v1/api/events?ws_id=<id>` for the active tab's event stream.
- **Global SSE**: `connectGlobalSSE()` opens `/v1/api/events/global` which
  receives `ws_state` broadcasts from all workstreams, used to update tab
  indicators without switching.
- **New tab / close**: POST `/v1/api/workstreams/new`, POST `/v1/api/workstreams/close`.

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
- `web_search` -- search the web (provider-native for Anthropic/OpenAI, Tavily fallback for local models)

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

### Provider Adapter Layer

> See also: [Core Engine Classes diagram](diagrams/png/03-core-engine-classes.png)

`ChatSession` is provider-agnostic — it delegates all LLM communication to an
`LLMProvider` protocol (`turnstone/core/providers/_protocol.py`). Internally,
messages use an OpenAI-like format; each provider translates at the API boundary.

```
ChatSession
    |
    v
LLMProvider (protocol)
    |
    +--- OpenAIProvider  --- OpenAI, vLLM, llama.cpp, any /v1/chat/completions API
    +--- AnthropicProvider --- Anthropic Messages API (native streaming, thinking)
```

**Protocol methods:**

| Method | Purpose |
|--------|---------|
| `create_streaming()` | Streaming request, yields normalized `StreamChunk` objects |
| `create_completion()` | Non-streaming request, returns `CompletionResult` |
| `get_capabilities()` | Per-model flags (`ModelCapabilities`) |
| `convert_tools()` | Translate OpenAI tool schemas to provider format |
| `retryable_error_names` | Exception class names that trigger retry |

**Normalized data types:**

| Type | Fields |
|------|--------|
| `StreamChunk` | `content_delta`, `reasoning_delta`, `tool_call_deltas`, `info_delta`, `usage`, `finish_reason` |
| `CompletionResult` | `content`, `tool_calls`, `finish_reason`, `usage` |
| `ModelCapabilities` | `context_window`, `max_output_tokens`, `supports_temperature`, `token_param`, `thinking_mode`, `supports_effort`, `supports_web_search` |
| `UsageInfo` | `prompt_tokens`, `completion_tokens`, `total_tokens` |

**OpenAIProvider** (`_openai.py`): passes messages through unchanged (they are
already in OpenAI format). Model capability lookup table covers
GPT-5/5.1/5.2, O-series, and search models (`gpt-5-search-api`).
For search models, injects `web_search_options` and removes the `web_search`
function tool (the model always searches). Citations from `url_citation`
annotations are formatted as footnotes. Unknown models (local servers) get
permissive defaults and use Tavily for web search.

**AnthropicProvider** (`_anthropic.py`): converts OpenAI-format messages to
Anthropic content blocks, maps `system`/`developer` roles to the `system`
parameter, groups consecutive `tool` result messages into user-role content
blocks, and translates tool schemas from OpenAI function-calling format to
Anthropic's `input_schema` format. Supports both manual and adaptive thinking
modes, with effort parameter support for models like Claude Opus 4.6 and
Sonnet 4.6. Replaces the `web_search` function tool with Anthropic's native
`web_search_20250305` server-side tool — Claude decides when to search, the
API executes it, and results stream back as `server_tool_use` /
`web_search_tool_result` content blocks (emitted as `info_delta` for UI
display). The `anthropic` SDK is imported lazily so it remains an optional
dependency (`pip install turnstone[anthropic]`).

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

[model]
default = "local"
fallback = ["claude", "openai"]
agent_model = "claude"
```

Each `[models.*]` entry produces a `ModelConfig` with a `provider` field
(default: `"openai"`). Supported values: `"openai"` and `"anthropic"`.

**Lifecycle:**
1. `load_model_registry()` reads `[models.*]` sections from config.toml and
   builds a `"default"` entry from CLI `--base-url`/`--model`/`--api-key` args
2. The registry is passed to the session factory closure in both `cli.py` and
   `server.py`; each workstream resolves its model on creation
3. `ModelRegistry.get_client()` lazily creates SDK client instances via
   `create_client()` — `OpenAI` for the openai provider, `Anthropic` for
   the anthropic provider (thread-safe via `_client_lock`)
4. `ModelRegistry.get_provider()` lazily creates `LLMProvider` instances via
   `create_provider()` (also cached and thread-safe)
5. `/model` command shows available models; `/model <alias>` switches the
   active workstream's client, model, and context window
6. `_create_stream_with_retry()` tries the primary model, then each fallback
   alias in order if the primary is unreachable
7. `_run_agent()` resolves `registry.agent_model` (if set) for plan/task
   sub-agents, allowing a cheaper model for autonomous loops

**Per-workstream selection:** `POST /v1/api/workstreams/new` accepts an optional
`"model"` field. The bridge `CreateWorkstreamMessage` carries the same field
through the MQ protocol.

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

### Storage Architecture

Persistence is managed by the `turnstone.core.storage` package — a pluggable
backend behind a `StorageBackend` protocol. The `memory.py` facade provides
backward-compatible module-level functions that delegate to the active backend.

```
session.py / server.py / cli.py
        ↓
    memory.py  (facade — silent-failure wrappers)
        ↓
    storage._registry  (singleton factory)
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
  provider_data TEXT                 -- raw provider content (e.g. Anthropic encrypted)

session_config
  session_id  TEXT NOT NULL          -- composite PK with key
  key         TEXT NOT NULL
  value       TEXT

conversations_fts                    -- SQLite FTS5 virtual table (optional)
  content     (content=conversations, content_rowid=id)
```

Table definitions live in `storage/_schema.py` (SQLAlchemy Core `Table` objects)
and are the single source of truth for both backends and Alembic migrations.

### StorageBackend Protocol

| Method | Purpose |
|--------|---------|
| `register_session(session_id, title)` | Create a sessions row (no-op if exists) |
| `save_message(session_id, role, content, ...)` | Log a message to conversations |
| `load_session_messages(session_id)` | Reconstruct OpenAI message format from DB rows |
| `list_sessions(limit)` | List sessions with >=1 message, ordered by updated DESC |
| `delete_session(session_id)` | Delete session and all its messages |
| `prune_sessions(retention_days)` | Remove empty sessions and old unnamed sessions |
| `resolve_session(alias_or_id)` | Resolve alias, exact id, or id prefix to full session_id |
| `save_session_config(session_id, config)` | Persist session configuration key/value pairs |
| `load_session_config(session_id)` | Retrieve session configuration |
| `set_session_alias(session_id, alias)` | Set user-friendly alias (returns False if taken) |
| `get_session_name(session_id)` | Return alias if set, else title, else None |
| `update_session_title(session_id, title)` | Set/update LLM-generated title |
| `kv_get(key)` / `kv_set(key, value)` / `kv_delete(key)` | Generic key-value store (backs memories table) |
| `kv_list()` / `kv_search(query)` | List or search key-value pairs |
| `search_history(query, limit)` | Full-text search (FTS5 on SQLite, tsvector on PostgreSQL) |
| `search_history_recent(limit)` | Return most recent messages |
| `close()` | Release resources (connection pool, engine) |

### Database Configuration

```toml
[database]
backend = "sqlite"                  # "sqlite" | "postgresql"
path = ".turnstone.db"              # SQLite file path
url = ""                            # PostgreSQL connection URL
pool_size = 5                       # PostgreSQL connection pool size
```

Environment variables: `TURNSTONE_DB_BACKEND`, `TURNSTONE_DB_URL`, `TURNSTONE_DB_PATH`.

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

### Health Monitor & Circuit Breaker

`BackendHealthMonitor` (`turnstone/core/healthcheck.py`) runs a daemon thread
that probes the LLM backend by calling `client.models.list()` every
`backend_probe_interval` seconds (default 30). Probe results drive a three-state
circuit breaker:

```
CLOSED  ──(N consecutive failures)──>  OPEN
OPEN    ──(cooldown expires)────────>  HALF_OPEN
HALF_OPEN ──(probe succeeds)────────>  CLOSED
HALF_OPEN ──(probe fails)──────────>  OPEN
```

- `record_success()` / `record_failure()` update `_consecutive_failures` and
  transition the `_state` (`CircuitState` enum: `CLOSED`, `OPEN`, `HALF_OPEN`).
- `acquire_request_permit()` returns `False` when the circuit is `OPEN` or when
  in `HALF_OPEN` and the single probe permit has already been consumed. Causes
  `ChatSession._create_stream_with_retry` to skip the backend and surface an
  error immediately.
- The `/health` endpoint reads the monitor's state: `"status": "ok"` when the
  circuit is closed, `"status": "degraded"` when open or half-open.

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
  |     POST /v1/api/send      -> starts worker thread per workstream
  |     POST /v1/api/approve   -> unblocks WebUI._approval_event
  |     POST /v1/api/plan      -> unblocks WebUI._plan_event
  |     POST /v1/api/workstreams/new -> creates workstream + worker
  |     GET  /v1/api/events    -> SSE via EventSourceResponse (per workstream)
  |     GET  /v1/api/events/global -> SSE via EventSourceResponse (fan-out)
  |
  +-- ASGI middleware stack
  |     MetricsMiddleware -> CORSMiddleware -> AuthMiddleware -> RateLimitMiddleware
  |
  +-- Worker thread per workstream (daemon)
  |     Runs session.send() synchronously -- ChatSession is fully blocking
  |     Blocks on WebUI._approval_event / _plan_event (threading.Event)
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
WebUI keeps `threading.Event` and `queue.Queue` primitives (unchanged from
the sync era). The `_global_fanout_thread` and `_idle_cleanup_thread` remain
as daemon threads since they interact with sync primitives. A lifespan
context manager handles startup/shutdown (health monitor, MCP client,
registry).

Each workstream's `WebUI` has:
- `_event_queue` (per-workstream SSE events, `queue.Queue`)
- `_approval_event` / `_plan_event` (`threading.Event` for blocking)
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

### Message Queue Bridge

```
Main thread              Global SSE thread         Per-WS SSE threads (×N)
+------------------+     +------------------+      +-------------------+
| Inbound loop     |     | GET /events/glob |      | GET /events?ws_id |
| BLPOP on Redis   |     | Parse SSE via    |      | Parse SSE via     |
|                  |     |   httpx-sse      |      |   httpx-sse       |
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
bridge auto-approves via `POST /v1/api/approve`. Otherwise, it publishes an
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
Monitoring (3 daemon threads)        Control + Proxy (async Starlette)
+------------------+                 +----------------------------+
| Event subscriber |                 | POST /v1/api/cluster/      |
| SUBSCRIBE on     |                 |   workstreams/new          |
| events:cluster   |                 |   → LPUSH to Redis         |
+------------------+                 |     inbound:{node_id}      |
| Node discovery   |                 +----------------------------+
| SCAN node:* keys |                 | GET /node/{node_id}/       |
| every 15 seconds |                 |   → httpx.AsyncClient      |
+------------------+                 |     proxy to server_url    |
| Poll loop        |                 | GET /node/{id}/v1/api/events |
| GET /v1/api/dash |                 |   → SSE stream proxy       |
| GET /health      |                 | POST /node/{id}/v1/api/send  |
| ThreadPoolExec   |                 |   → forwarded to server    |
+------------------+                 +----------------------------+
```

The console HTTP layer is a Starlette/ASGI app served by uvicorn. The SSE
endpoint uses `EventSourceResponse` with the same listener queue pattern as
the main server. `ClusterCollector`'s background threads (event subscriber,
node discovery, poll loop) use sync Redis clients and `ThreadPoolExecutor`
for parallel HTTP polling.

The console has two write-path capabilities:

1. **Workstream creation** — pushes `CreateWorkstreamMessage` to Redis inbound
   queues targeting specific nodes. The bridge on each node picks up the message
   and creates the workstream on the local server. Auto-selects the node with
   the most available capacity if no target is specified.

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

---

## Client SDK

> See also: [SDK Architecture diagram](diagrams/png/13-sdk-architecture.png) | [SDK Documentation](sdk.md)

The `turnstone/sdk/` package provides typed HTTP clients for programmatic access
to both the server and console APIs. It wraps REST endpoints with methods that
return Pydantic models, and SSE endpoints with async/sync iterators that yield
typed event dataclasses.

**Two client pairs** (sync + async):

- `TurnstoneServer` / `AsyncTurnstoneServer` — server API (workstreams, chat, streaming, sessions)
- `TurnstoneConsole` / `AsyncTurnstoneConsole` — console API (cluster overview, nodes, workstreams)

**Design**: async-first with thin sync wrappers. `_BaseClient` provides httpx
setup, auth headers, `_request()` (REST) and `_stream_sse()` (SSE). Sync
clients delegate through `_SyncRunner` which maintains a persistent background
event loop on a daemon thread.

**Event types**: 27 standalone dataclasses in `events.py` with a type-registry
pattern matching `OutboundEvent.from_json()` from `mq/protocol.py`. Events are
decoupled from the MQ package so SDK consumers don't need the `redis` dependency.

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
