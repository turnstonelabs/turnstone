# Tools Reference

turnstone exposes 18 built-in tools plus any number of external MCP tools to the
LLM via the OpenAI function-calling interface. Built-in tools are defined as JSON
files under `turnstone/tools/` and loaded at startup by `turnstone/core/tools.py`.
MCP tools are discovered from configured MCP servers at startup by
`turnstone/core/mcp_client.py`.

---

## Tool Schema Format

Each JSON file in `turnstone/tools/` contains a standard OpenAI function-calling
schema plus turnstone-specific metadata keys:

```json
{
  "name": "tool_name",
  "description": "What the tool does.",
  "parameters": {
    "type": "object",
    "properties": { ... },
    "required": ["param1"]
  },
  "agent": true,
  "task_agent": true,
  "auto_approve": true,
  "primary_key": "param1"
}
```

**Metadata keys** (stripped before sending the schema to the model):

| Key            | Type | Meaning |
|----------------|------|---------|
| `agent`        | bool | Tool is available to plan/task sub-agents (read-only subset). |
| `task_agent`   | bool | Tool is available to task sub-agents (broader subset). |
| `auto_approve` | bool | Tool runs without user confirmation (read-only, safe operations). |
| `primary_key`  | str  | When the model sends a bare string instead of JSON args, map it to this parameter name. |

---

## Derived Tool Sets

`turnstone/core/tools.py` loads all JSON files and derives these collections:

| Name                | Description |
|---------------------|-------------|
| `TOOLS`             | All 17 tool definitions (sent to the model). |
| `AGENT_TOOLS`       | Tools with `agent: true` -- available to plan sub-agents. Read-only tools. |
| `TASK_AGENT_TOOLS`  | Tools with `task_agent: true` -- available to task sub-agents. Includes write operations. |
| `AGENT_AUTO_TOOLS`  | Set of tool names with `auto_approve: true` -- no user confirmation needed. |
| `TASK_AUTO_TOOLS`   | Same as `AGENT_AUTO_TOOLS` (identical filter). |
| `BUILTIN_TOOL_NAMES`| Frozenset of all 17 built-in tool names. Used by tool search to distinguish always-on tools from deferrable MCP tools. |
| `PRIMARY_KEY_MAP`   | Dict mapping tool name to its `primary_key` parameter name. |

---

## Execution Pipeline

> See also: [Tool Pipeline diagram](diagrams/png/05-tool-pipeline.png)

Tool execution follows a three-phase pipeline inside `ChatSession._execute_tools()`:

### Phase 1: Prepare

`_prepare_tool(tc)` is called for each tool call returned by the model.

- Parses the JSON arguments (with fallback for malformed JSON).
- If JSON parsing fails entirely, uses `PRIMARY_KEY_MAP` to map a bare string
  to the correct parameter.
- Dispatches to the matching `_prepare_{func_name}()` handler. There are 17
  built-in tools plus `tool_search` (synthetic, client-side BM25 fallback) and
  the generic `_prepare_mcp_tool()` handler for MCP tools.
- Validates arguments and builds a preview dict containing:
  - `call_id`, `func_name`, `header`, `preview` (for display)
  - `needs_approval` (bool)
  - `execute` (callable to run the tool)
  - `error` (set if validation fails; tool will not execute)

### Phase 2: Approve

All prepared items are sent to the UI via `ui.approve_tools(items)`.

- The UI displays each tool's header and preview to the user.
- Items where `needs_approval` is `False` (auto-approved tools) are shown
  but do not block execution.
- Items where `needs_approval` is `True` require the user to accept or deny.
- The user can provide feedback alongside their approval (e.g. "y, use full path").
- Choosing "always" (key `a`) adds the pending tool names to `auto_approve_tools`,
  so that specific tool type is auto-approved going forward (other tool types still
  prompt). This is per-tool, not blanket.
- If `auto_approve` is `True` on the session (via `--skip-permissions` or workstream
  template), all tools are approved automatically.

### Phase 3: Execute

Each item's `execute` callable is invoked:

- Single tool calls run directly on the current thread.
- Multiple tool calls run in parallel via `ThreadPoolExecutor(max_workers=4)`.
- Errored or denied items return their error/denial message without executing.
- The `bash` tool streams stdout incrementally: each line calls
  `ui.on_tool_output_chunk(call_id, line)` as it is produced, then the final
  combined output (stdout + stderr) is delivered via `ui.on_tool_result(call_id, name, output)`.
  The `call_id` links `tool_info`/`approve_request` items to their streaming chunks and
  final result, enabling correct routing when multiple bash tools run in parallel.
  Other tools deliver results atomically via `ui.on_tool_result(call_id, name, output)` only.
- Special post-execution gate for `plan`: the plan output is shown to the user
  for review, and the user can reject or annotate it.

---

## Tool Approval Flow

**Auto-approved** (no user confirmation needed at runtime):
- `read_file` -- reads files, no side effects
- `search` -- grep-style search, no side effects
- `man` -- reads man pages, no side effects
- `memory` -- structured persistent memory (save/search/delete/list)
- `recall` -- searches conversation history
- `notify` -- sends notifications to linked channels (time-sensitive, auto-approved for urgency)

**Requires user confirmation** (write operations, network access, side effects):
- `bash` -- arbitrary command execution
- `write_file` -- creates or overwrites files
- `edit_file` -- modifies file content
- `math` -- sandboxed computation (confirmation required despite being sandboxed)
- `web_fetch` -- fetches a URL (SSRF-protected, but makes network requests)
- `web_search` -- web search via Tavily API (makes network requests)
- `task` -- spawns an autonomous sub-agent
- `plan` -- spawns a planning sub-agent, plus post-execution review gate

Note: The JSON schema metadata key `auto_approve` controls membership in
`AGENT_AUTO_TOOLS`/`TASK_AUTO_TOOLS` (used for agent sub-sessions). The actual
runtime approval behavior is determined by the `needs_approval` field set in
each `_prepare_*` method on `ChatSession`. These two mechanisms can differ.

---

## Primary Key Fallback

When the model sends a bare string instead of a JSON object as tool arguments
(common with smaller models), the `primary_key` mapping rescues the call:

```
Model sends:  bash("ls -la")
              raw_args = "ls -la"   (not valid JSON)

PRIMARY_KEY_MAP["bash"] = "command"
Result:       args = {"command": "ls -la"}
```

Every tool defines a `primary_key`. The mapping is:

| Tool         | primary_key |
|--------------|-------------|
| `bash`       | `command`   |
| `read_file`  | `path`      |
| `write_file` | `content`   |
| `edit_file`  | `old_string`|
| `search`     | `query`     |
| `math`       | `code`      |
| `man`        | `page`      |
| `web_fetch`  | `url`       |
| `web_search` | `query`     |
| `task`       | `prompt`    |
| `plan`       | `prompt`    |
| `memory`     | `name`      |
| `recall`     | `query`     |
| `notify`     | `message`   |
| `read_resource` | `uri`    |
| `use_prompt` | `name`     |

---

## File Operations

### bash

Execute a bash command and return stdout + stderr.

| Parameter | Type   | Required | Description |
|-----------|--------|----------|-------------|
| `command` | string | yes      | The bash command to execute. |

- **What it does**: Runs the command in a subprocess with a configurable timeout. Commands are sanitized and checked against a blocklist (e.g. `rm -rf /`).
- **Auto-approve**: No -- requires user confirmation.
- **Agent availability**: `task_agent` only (not available to plan sub-agents).

---

### read_file

Read the contents of a file, returning numbered lines for text files or
base64-encoded image data for supported image formats.

| Parameter | Type    | Required | Description |
|-----------|---------|----------|-------------|
| `path`    | string  | yes      | Absolute or relative file path. |
| `offset`  | integer | no       | Line number to start from (1-based, default: 1). Text files only. |
| `limit`   | integer | no       | Maximum number of lines to read. Omit for full file. Text files only. |

- **What it does**: For text files, reads and returns content with line numbers. For image files (PNG, JPEG, GIF, WebP, BMP, TIFF, ICO), returns image data as multi-part content when the model supports vision, or a text description when it does not. SVG files are read as text. Images larger than 4 MB are rejected. Must be called before `edit_file` on the same path (the session tracks which files have been read).
- **Vision support**: Controlled by `ModelCapabilities.supports_vision`. All commercial OpenAI and Anthropic models have vision enabled. Local models (vLLM, llama.cpp, NIM) default to off — enable via `[models.*.capabilities] supports_vision = true` in config.toml.
- **Auto-approve**: Yes.
- **Agent availability**: `agent` and `task_agent`.

---

### write_file

Write content to a file, creating it if needed.

| Parameter | Type   | Required | Description |
|-----------|--------|----------|-------------|
| `path`    | string | yes      | Absolute or relative file path. |
| `content` | string | yes      | The full file content to write. |

- **What it does**: Creates or overwrites the file at the given path. Parent directories are created as needed.
- **Auto-approve**: No -- requires user confirmation.
- **Agent availability**: `task_agent` only.

---

### edit_file

Replace an exact string in a file with new content.

| Parameter    | Type    | Required | Description |
|--------------|---------|----------|-------------|
| `path`       | string  | yes      | Absolute or relative file path. |
| `old_string` | string  | yes      | The exact text to find and replace. |
| `new_string` | string  | yes      | The replacement text. |
| `near_line`  | integer | no       | Disambiguate when `old_string` matches multiple locations. |

- **What it does**: Finds `old_string` in the file and replaces it with `new_string`. Fails if the string is not found or matches multiple locations (unless `near_line` is provided to pick the nearest match). Requires a prior `read_file` call on the same path.
- **Auto-approve**: No -- requires user confirmation.
- **Agent availability**: `task_agent` only.

---

### search

Search file contents for a regex pattern.

| Parameter | Type   | Required | Description |
|-----------|--------|----------|-------------|
| `query`   | string | yes      | Regex pattern (extended regex). |
| `path`    | string | no       | File or directory to search in (default: current directory). |

- **What it does**: Recursively searches for the pattern using `grep -rn`. Returns matching lines with file paths and line numbers.
- **Auto-approve**: Yes.
- **Agent availability**: `agent` and `task_agent`.

---

## Computation

### math

Execute Python code for math and computation in a sandbox.

| Parameter | Type   | Required | Description |
|-----------|--------|----------|-------------|
| `code`    | string | yes      | Python code to execute. Must use `print()` for output. |

- **What it does**: Runs Python code in a sandboxed environment with pre-imported libraries: `sympy`, `numpy`, `scipy`, `math`, `fractions`, `itertools`, `functools`, `collections`, `decimal`, `operator`, `random`, `re`, `string`. Common sympy names (`symbols`, `solve`, `simplify`, `sqrt`, `Matrix`, etc.) are pre-imported.
- **Auto-approve**: No -- requires user confirmation.
- **Agent availability**: `agent` and `task_agent`.

---

## Information

### man

Read a man page.

| Parameter | Type   | Required | Description |
|-----------|--------|----------|-------------|
| `page`    | string | yes      | The man page name (e.g. `grep`, `socket`, `printf`). |
| `section` | string | no       | Manual section (e.g. `1` commands, `2` syscalls, `3` library). |

- **What it does**: Returns the full formatted manual entry. Preferred over `bash('man ...')` or `web_search` for command/API documentation.
- **Auto-approve**: Yes.
- **Agent availability**: `agent` and `task_agent`.

---

### web_fetch

Fetch a URL and extract specific information from it.

| Parameter  | Type   | Required | Description |
|------------|--------|----------|-------------|
| `url`      | string | yes      | The URL to fetch (must start with `http://` or `https://`). |
| `question` | string | yes      | What to extract or answer from the page content. |

- **What it does**: Fetches the URL, strips HTML to plain text, and uses the LLM to extract the answer to the question from the page content. Protected against SSRF (blocks private/internal IPs).
- **Auto-approve**: No -- requires user confirmation (makes network requests).
- **Agent availability**: `agent` and `task_agent`.

---

### web_search

Search the web using a text query.

| Parameter     | Type    | Required | Description |
|---------------|---------|----------|-------------|
| `query`       | string  | yes      | The search query. |
| `max_results` | integer | no       | Max results to return (default 5, max 20). |
| `topic`       | string  | no       | Search topic: `general`, `news`, or `finance` (default `general`). |

- **What it does**: Searches the web and returns ranked results with titles, URLs, and content snippets. Uses provider-native search when available:
  - **Anthropic**: Replaced at the API boundary with Anthropic's `web_search_20250305` server-side tool. Claude decides when to search; the API executes it and returns results with citations inline. No Tavily key needed.
  - **OpenAI search models** (`gpt-5-search-api`): Replaced with `web_search_options` parameter. The model always searches and returns `url_citation` annotations.
  - **Local/vLLM models**: Falls back to the Tavily API. Requires `tavily_key` in `config.toml` or `$TAVILY_API_KEY`.
- **Auto-approve**: Yes (auto-approved for all tool dispatch paths).
- **Agent availability**: `agent` and `task_agent`.

---

## Agent

### task

Delegate a general-purpose task to an autonomous sub-agent.

| Parameter | Type   | Required | Description |
|-----------|--------|----------|-------------|
| `prompt`  | string | yes      | Complete task description for the sub-agent. |

- **What it does**: Spawns a sub-agent that inherits the `TASK_AGENT_TOOLS` set (read, write, edit, search, bash, math, man, web tools, memory tools). The sub-agent runs autonomously to completion. Use for work that requires file modifications or command execution.
- **Auto-approve**: No -- requires user confirmation.
- **Agent availability**: Not available to sub-agents (top-level only).

---

### plan

Plan before implementing -- an autonomous agent explores the codebase and writes a structured plan.

| Parameter | Type   | Required | Description |
|-----------|--------|----------|-------------|
| `prompt`  | string | yes      | What to plan -- the goal, constraints, and scope. |

- **What it does**: Spawns a planning sub-agent with `AGENT_TOOLS` (read-only tools: `read_file`, `search`, `math`, `man`, `web_fetch`, `web_search`). The agent explores the codebase and writes a structured plan to `.plan-<ws_id>.md` (unique per workstream, so concurrent workstreams never collide). If the `plan` tool has been called before in the same session, the prior plan is passed to the agent as context so it refines rather than restarts. After completion, the user is prompted to review and can accept, reject, or annotate the plan.
- **Auto-approve**: No -- requires user confirmation, plus post-execution review gate.
- **Agent availability**: Not available to sub-agents (top-level only).

---

## Memory

### memory

Structured persistent memory across sessions with typed, scoped entries.

| Parameter     | Type    | Required | Description |
|---------------|---------|----------|-------------|
| `action`      | string  | yes      | `save`, `search`, `delete`, or `list`. |
| `name`        | string  | save/delete | Short snake_case identifier for the memory. |
| `content`     | string  | save     | Memory content to store. |
| `description` | string  | no       | Short description for relevance matching (recommended for `save`). |
| `type`        | string  | no       | Memory type: `user`, `project`, `feedback`, or `reference`. Default: `project`. |
| `scope`       | string  | no       | Memory scope: `global`, `workstream`, or `user`. Default: `global`. |
| `query`       | string  | search   | Search query for finding memories. |
| `limit`       | integer | no       | Max results for `search` or `list`. Default: 20. |

- **What it does**: Manages structured persistent memories in the database. Memories persist across sessions, have a type classification (user preferences, project knowledge, feedback, reference material) and a scope (global across all workstreams, private to a workstream, or following a user). Relevant memories are included in the system prompt on startup.
- **Auto-approve**: Yes.
- **Agent availability**: Not available to sub-agents (top-level only).

---

### recall

Search conversation history for past messages and tool results.

| Parameter | Type    | Required | Description |
|-----------|---------|----------|-------------|
| `query`   | string  | yes      | Search term or phrase to find in conversation history. |
| `limit`   | integer | no       | Max results to return (default 20). |

- **What it does**: Searches conversation history across sessions using FTS5 full-text search. Returns matching messages, tool calls, and tool results with timestamps and workstream context.
- **Auto-approve**: Yes.
- **Agent availability**: Not available to sub-agents (top-level only).

---

## Notifications

### notify

Send a notification to a user or channel on an external platform.

| Parameter      | Type   | Required | Description |
|----------------|--------|----------|-------------|
| `message`      | string | yes      | Notification content (plain text, max 2000 chars). |
| `username`     | string | no       | Turnstone username — sends to all linked channels. |
| `channel_type` | string | no       | Platform for direct targeting (`discord`). |
| `channel_id`   | string | no       | Platform-specific channel or user ID for direct targeting. |
| `title`        | string | no       | Optional short title (rendered as bold prefix). |

Provide either `username` for user-based targeting or `channel_type` +
`channel_id` for direct targeting. Do not combine both.

- **What it does**: Sends a notification via the channel gateway's HTTP endpoint (`POST /v1/api/notify`). The server queries the `services` table for healthy channel gateways, authenticates with a service JWT (`aud: turnstone-channel`), and delivers to the first healthy gateway. On failure, retries up to 2 additional times with backoff (1s, 3s). Rate-limited to 5 notifications per turn (counter only increments on success).
- **Auto-approve**: Yes — notifications are time-sensitive and auto-approved so the model can alert users urgently.
- **Agent availability**: `agent` and `task_agent`.

> See [Channel Integrations: Notifications](channels.md#notifications)
> for the full delivery flow, service registry details, and security
> measures.

---

### watch

Set up periodic polling of a shell command within the current workstream.
Results are injected back into the conversation as synthetic user messages,
triggering the model to respond and act. Use for monitoring CI/CD pipelines,
PR reviews, deployments, file changes, etc.

| Parameter   | Type    | Required | Description |
|-------------|---------|----------|-------------|
| `action`    | string  | yes      | `create`, `list`, or `cancel`. |
| `command`   | string  | create   | Shell command to poll periodically. |
| `poll_every`| string  | no       | Poll interval as duration (`30s`, `5m`, `1h`). Default: `5m`. |
| `stop_on`   | string  | no       | Python expression for stop condition (see below). Omit for change detection. |
| `name`      | string  | create   | Human-readable watch name (e.g. `pr-review`). Used as identifier for cancel. |
| `max_polls` | integer | no       | Max poll cycles before auto-cancel. Default: 100. |

**Actions:**

- `create` — Start a new watch. Requires approval (same as bash — runs shell
  commands). Persists to the `watches` table; the server-level `WatchRunner`
  daemon polls every 15 seconds for due watches.
- `list` — Show all active watches in this workstream. Auto-approved.
- `cancel` — Stop a watch by name or ID prefix. Auto-approved.

**Stop condition DSL** — The `stop_on` parameter accepts a Python expression
evaluated after each poll. Available variables:

| Variable      | Type       | Description |
|---------------|------------|-------------|
| `output`      | `str`      | stdout (+stderr) of the command. |
| `data`        | `Any`      | `json.loads(output)`, or `None` if not valid JSON. |
| `exit_code`   | `int`      | Process exit code. |
| `prev_output` | `str|None` | Previous poll's stdout (`None` on first poll). |
| `changed`     | `bool`     | `True` if output differs from previous poll. |

Safe builtins: `len`, `str`, `int`, `float`, `bool`, `abs`, `min`, `max`,
`any`, `all`, `isinstance`, `sorted`. No `import`, `open`, `exec`, or
`eval`. Security model: equivalent to `bash` — the model already has shell
access.

**Examples:**
```
data["state"] == "MERGED"
"error" in output
exit_code != 0
changed and "ready" in output.lower()
data.get("mergedAt") is not None
```

**Lifecycle:**

1. Model calls `watch(action="create", ...)` — persisted to SQLite.
2. `WatchRunner` daemon polls for due watches every 15s.
3. Each poll runs the command, evaluates the condition.
4. When the condition fires (or max polls reached), the result is injected
   as a synthetic user message and the watch auto-cancels.
5. If the workstream was evicted, it is restored before injection.
6. Watches survive server restart (overdue watches fire once on recovery).

**Constraints:**

- Max 5 active watches per workstream.
- Poll interval: 10s–24h.
- Output truncated at 64 KB.
- Max 5 consecutive watch dispatches per worker thread (depth guard).
- Duplicate names rejected within the same workstream.

- **Auto-approve**: `create` requires approval; `list` and `cancel` are auto-approved.
- **Agent availability**: Main session only — not available to plan/task sub-agents.

> See [Watch Architecture](diagrams/png/18-watch-architecture.png) for the
> full poll → evaluate → dispatch flow.

---

### skill

Discover and activate skills at runtime during a conversation. The model can
search for available skills and load one by name, replacing the current active
skill. This enables model-driven skill selection without requiring the user to
pre-configure skills at workstream creation.

| Parameter | Type   | Required | Description |
|-----------|--------|----------|-------------|
| `action`  | string | yes      | `load` or `search`. |
| `name`    | string | load     | Skill name to activate. |
| `query`   | string | no       | Search query for finding skills (for `search` action). |

**Actions:**

- `load` — Activate a skill by name. Calls `set_skill()` which handles content
  rendering with `{{model}}`/`{{ws_id}}`/`{{node_id}}` variables, system message
  reinitialization, and config persistence. Returns the skill name, description,
  and security scan tier. Warns on high/critical scan status.
- `search` — Find available skills by query. Uses BM25 relevance ranking over
  name, description, tags, and category (same `BM25Index` used by memory
  relevance and tool search). Returns up to 10 results with name, description,
  category, scan status, and activation type.

- **Auto-approve**: `load` requires approval (changes session behavior); `search`
  is auto-approved (read-only).
- **Agent availability**: Main session only — not available to plan/task sub-agents.

---

## Summary Table

| Tool         | Category   | Auto-approve | agent | task_agent | primary_key |
|--------------|------------|--------------|-------|------------|-------------|
| `bash`       | File Ops   | No           | No    | Yes        | `command`   |
| `read_file`  | File Ops   | Yes          | Yes   | Yes        | `path`      |
| `write_file` | File Ops   | No           | No    | Yes        | `content`   |
| `edit_file`  | File Ops   | No           | No    | Yes        | `old_string`|
| `search`     | File Ops   | Yes          | Yes   | Yes        | `query`     |
| `math`       | Compute    | No           | Yes   | Yes        | `code`      |
| `man`        | Info       | Yes          | Yes   | Yes        | `page`      |
| `web_fetch`  | Info       | No           | Yes   | Yes        | `url`       |
| `web_search` | Info       | No           | Yes   | Yes        | `query`     |
| `task`       | Agent      | No           | No    | No         | `prompt`    |
| `plan`       | Agent      | No           | No    | No         | `prompt`    |
| `memory`     | Memory     | Yes          | No    | No         | `name`      |
| `recall`     | Memory     | Yes          | No    | No         | `query`     |
| `notify`     | Notify     | Yes          | Yes   | Yes        | `message`   |
| `watch`      | Monitor    | No (create)  | No    | No         | `command`   |
| `read_resource`| MCP      | No           | Yes   | Yes        | `uri`       |
| `use_prompt` | MCP        | No           | Yes   | Yes        | `name`      |
| `skill`      | Skills     | No (load)    | No    | No         | `name`      |
| `tool_search`| Search     | Yes          | No    | No         | `query`     |

---

## Dynamic Tool Search

When many MCP tools are connected, the total tool count can grow large enough to
consume significant context window tokens and reduce model accuracy. Dynamic tool
search addresses this by deferring tools the model is unlikely to need on the
current turn and letting it search for them on demand.

### Three-tier approach

Tool search uses the best available mechanism for each provider:

1. **Anthropic (native)** -- Models that support it receive `defer_loading: true`
   on deferred tool definitions plus the `tool_search_tool_bm25_20251119` server-side
   search tool. Anthropic's API handles search and expansion transparently.

2. **OpenAI GPT-5.4+ (native)** -- Models with hosted tool search receive
   `defer_loading: true` on deferred definitions. The API handles search internally.

3. **vLLM / llama.cpp / NIM (client-side BM25)** -- A synthetic `tool_search`
   function tool is injected into the tool list. When the model calls it,
   `_exec_tool_search()` runs a pure-Python BM25 index over tool names and
   descriptions, then expands the matched tools into the visible set.

### Configuration

Tool search is configured in `config.toml` under the `[tools]` section:

```toml
[tools]
search = "auto"           # "auto", "on", or "off"
search_threshold = 20     # minimum total tool count to activate
search_max_results = 5    # max tools returned per search call
```

CLI flags override the config file:

- `--tool-search {auto,on,off}` -- force tool search on or off, or let turnstone
  decide based on threshold (default: `auto`).
- `--tool-search-threshold N` -- minimum tool count to activate (default: 20).
- `--tool-search-max-results N` -- max results per search (default: 5).

### How it works

1. **Threshold check**: At session startup, if the total tool count (built-in + MCP)
   is below the threshold, tool search stays off and all tools are sent to the model
   directly.

2. **Partitioning**: When active, tools are split into two sets:
   - **Always-on** -- the 17 built-in tools (members of `BUILTIN_TOOL_NAMES`).
     These are always visible to the model.
   - **Deferred** -- all MCP tools. These are not sent in the tool list unless
     the model searches for them.

3. **Search and expand**: When the model calls `tool_search` (client-side) or the
   provider's native search returns results, the matched tools are added to the
   visible set via `expand_visible()`. Once expanded, a tool stays visible for
   the remainder of the session.

4. **Multi-turn persistence**: Expanded tools are never removed. This avoids
   confusing the model when it references a tool it discovered in an earlier turn.

### Agent exemption

Plan and task sub-agents do not use tool search. They operate on scoped tool
sets (`AGENT_TOOLS` for plan agents, `TASK_AGENT_TOOLS` for task agents) with
MCP tools merged in. Tool search is only active for the top-level session,
where the model can interactively search for tools it needs.

---

## MCP Tools (External)

> See also: [MCP Architecture diagram](diagrams/png/20-mcp-architecture.png)

Turnstone supports the [Model Context Protocol](https://modelcontextprotocol.io/)
(MCP) for connecting external tool servers — GitHub, databases, filesystems, or any
MCP-compatible service.

### How it works

1. **Configuration**: MCP servers are defined in `config.toml` under `[mcp.servers.*]`
   sections, or via a standard MCP JSON config file (`--mcp-config`).

2. **Discovery**: At startup, `MCPClientManager` connects to each configured server
   (via stdio subprocess or HTTP), performs the MCP `initialize` handshake, and calls
   `tools/list` to discover available tools. During the handshake, the manager checks
   each server's capabilities for `tools.listChanged` support (push notifications).

3. **Schema conversion**: Each MCP tool's `inputSchema` is converted to OpenAI
   function-calling format. The tool name is prefixed: `mcp__{server}__{tool}`.

4. **Merging**: MCP tools are appended after the 17 built-in tools via
   `merge_mcp_tools()`. Built-in tools appear first, giving them natural LLM priority.
   When dynamic tool search is active, MCP tools are deferred rather than directly
   visible -- the model discovers them via search as needed (see
   [Dynamic Tool Search](#dynamic-tool-search) above).

5. **Dispatch**: When the LLM calls an MCP tool, `_prepare_mcp_tool()` builds a
   generic approval preview and `_exec_mcp_tool()` calls `MCPClientManager.call_tool_sync()`,
   which dispatches the call to the background asyncio event loop.

### Approval behavior

MCP tools **require user approval by default** (`needs_approval: True`). turnstone
does not auto-approve MCP tools based on their schema, since it cannot guarantee
that external tools are read-only. However, global overrides such as
`--skip-permissions` will auto-approve all tools, including MCP tools. The
interactive "Always" button adds specific tool types to the per-tool auto-approve
set. The web UI and server use `approval_label` for MCP tools, giving
per-prompt/per-resource granularity. The CLI and bridge use `func_name`, which
gives per-tool-type granularity (e.g., all `use_prompt` calls).

### Sub-agent availability

MCP tools are available to:
- **Main session** — full access
- **Task sub-agents** — via `self._task_tools` (merged list)
- **Plan sub-agents** — via `self._agent_tools` (merged list)

### Naming convention

MCP tool names follow the pattern `mcp__{server}__{original}`:

- `mcp__github__search_repos` — `search_repos` tool from `github` server
- `mcp__postgres__query` — `query` tool from `postgres` server

Server names must not contain `__` (double underscore), which is reserved as the
delimiter. Servers with `__` in their name are rejected at connection time.

### Configuration

**TOML** (`~/.config/turnstone/config.toml`):

```toml
[mcp.servers.github]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]

[mcp.servers.github.env]
GITHUB_TOKEN = "ghp_..."

[mcp.servers.remote]
type = "http"
url = "https://mcp.example.com/mcp"
```

**JSON** (standard `mcpServers` format, via `--mcp-config`):

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_TOKEN": "ghp_..."}
    }
  }
}
```

### Introspection

Use the `/mcp` slash command to list all connected MCP tools:

```
/mcp
MCP tools (3):
  mcp__github__search_repos  [MCP: github] Search GitHub repositories
  mcp__github__create_issue  [MCP: github] Create a GitHub issue
  mcp__postgres__query       [MCP: postgres] Run a SQL query
```

### Dynamic tool refresh

MCP tool lists stay up-to-date without restart through three mechanisms:

1. **Push notifications** -- MCP servers that declare `tools.listChanged: true` in
   their capabilities send `notifications/tools/list_changed` when their tool list
   changes. `MCPClientManager` registers a `message_handler` on each `ClientSession`
   that triggers an immediate refresh for that server.

2. **Periodic timer** -- Servers that do *not* support push notifications are polled
   on a configurable interval (default 4 hours). The timer is staggered using a
   launch-time seed (`monotonic_ns ^ pid`) so cluster nodes don't all hit MCP
   servers simultaneously. Configure via `[mcp] refresh_interval` in `config.toml`
   or `--mcp-refresh-interval SECONDS` on the CLI. Set to `0` to disable.

3. **Manual** -- `/mcp refresh` re-fetches tools from all servers immediately.
   `/mcp refresh <server>` targets a single server. If a server has disconnected,
   manual refresh attempts reconnection.

When tools change, `MCPClientManager` rebuilds its merged tool list using copy-on-write
(new list/dict objects assigned atomically) and notifies all active `ChatSession`
instances via registered listener callbacks. Each session rebuilds its `_tools`,
`_task_tools`, `_agent_tools`, and reconstructs its `ToolSearchManager` (if active),
preserving the set of previously expanded (discovered) tools.

```toml
[mcp]
refresh_interval = 14400  # seconds (default 4h), 0 to disable
```

```
/mcp refresh
MCP refresh complete:
  github: +1 added
    + mcp__github__create_pr
  postgres: no changes

/mcp refresh github
MCP refresh complete:
  github: no changes
```

---

## MCP Resources

MCP servers can expose **resources** -- named data items (files, database rows,
API responses) addressable by URI. turnstone discovers resources at startup and
makes them available to the model via the `read_resource` built-in tool.

### Discovery

During the MCP `initialize` handshake, `MCPClientManager` checks each server's
capabilities for the `resources` capability. For servers that declare it:

1. `list_resources` fetches static resources (fixed URIs).
2. `list_resource_templates` fetches URI templates (parameterized patterns like
   `db://tables/{table}/rows/{id}`).

Both are stored as `{uri, name, description, mimeType, server}` dicts and
merged into a unified catalog.

### Resource catalog in system message

The first 50 resources are injected into the system message as an XML-delimited
block so the model knows what URIs are available:

```xml
<mcp-resources>
  file:///project/README.md  Project readme
  db://users/schema  User table schema
</mcp-resources>
Use read_resource(uri='...') to access the resources listed above.
```

### read_resource tool

| Parameter | Type   | Required | Description |
|-----------|--------|----------|-------------|
| `uri`     | string | yes      | The resource URI to read. |

- **What it does**: Reads the resource from its MCP server via `MCPClientManager.read_resource_sync()`. Returns text content for text resources or base64-encoded data for binary resources. Output is truncated by the standard tool output limiter.
- **Auto-approve**: No -- requires user confirmation (reads external data).
- **Agent availability**: `agent` and `task_agent`.

### Capability guards

The `read_resource` tool schema is always loaded (it is a built-in JSON schema),
but resource discovery only runs for servers that declare the `resources`
capability. Servers without the capability contribute zero resources to the
catalog.

### Refresh

Resource lists stay current through the same three-tier mechanism as tool lists:

1. **Push** -- Servers declaring `resources.listChanged: true` send
   `notifications/resources/list_changed`, triggering an immediate refresh.
2. **Periodic** -- Servers without push are polled on the configured refresh
   interval (default 4 hours, same timer as tools).
3. **Manual** -- `/mcp refresh` re-fetches resources alongside tools.

---

## MCP Prompts

MCP servers can also expose **prompts** -- reusable message templates with
optional arguments. turnstone discovers prompts at startup for servers that
declare the `prompts` capability.

### Discovery

Prompt discovery mirrors resource discovery: `list_prompts` is called during
the `initialize` handshake. Each prompt is stored with its prefixed name
(`mcp__{server}__{prompt}`), description, and argument schema.

### use_prompt tool

| Parameter   | Type   | Required | Description |
|-------------|--------|----------|-------------|
| `name`      | string | yes      | The prompt name (e.g. `mcp__server__prompt_name`). |
| `arguments` | object | no       | Key-value argument pairs for the prompt. Values must be strings. |

- **What it does**: Invokes an MCP prompt by name via `MCPClientManager.get_prompt_sync()`, expanding it into messages. Returns the expanded prompt content formatted as `[role]: content` blocks joined with blank lines. The prompt catalog is listed in the system message so the model knows which prompts are available. Output is truncated by the standard tool output limiter.
- **Auto-approve**: No -- requires user confirmation (invokes external prompt servers).
- **Agent availability**: `agent` and `task_agent`.

### Invocation

`MCPClientManager.get_prompt_sync()` calls the server's `get_prompt` method
with the provided arguments and returns the expanded messages. The `use_prompt`
built-in tool exposes this to the model as a function call.

### Governance Sync

Discovered MCP prompts are automatically synced into the `prompt_templates`
table (which stores skills) as first-class governed skills:

- **Origin tracking**: MCP-sourced skills have `origin="mcp"` and
  `mcp_server` set to the server name. Manual skills have
  `origin="manual"`.
- **Read-only**: MCP-sourced skills are `readonly=True`. The admin API
  returns 403 on update/delete attempts. The admin UI disables edit/delete
  buttons and shows an origin badge.
- **Precedence**: If a manual skill and MCP prompt share the same name,
  the manual skill wins and the MCP prompt is skipped (with a log
  warning).
- **Lifecycle**: Skills are created on connect, updated on prompt list
  refresh, and removed when the MCP server no longer exposes the prompt.
  The sync runs automatically on connect, on `PromptListChangedNotification`,
  and on manual `/mcp refresh`.
- **Schema**: Migration 009 adds `origin`, `mcp_server`, and `readonly`
  columns to the `prompt_templates` table.

The `use_prompt` tool allows the model to invoke any discovered MCP prompt at
runtime. A catalog of up to 30 prompts is injected into the system message
inside `<mcp-prompts>` XML tags so the model can discover available prompts.

---

## MCP UI Visibility

MCP server, resource, and prompt counts are surfaced across the UI:

- **Server `/health` endpoint**: Returns `mcp.servers`, `mcp.resources`,
  `mcp.prompts` when MCP is configured
- **Server UI**: Magenta status badge in the header showing server count,
  with resource/prompt counts in tooltip
- **Console cluster status bar**: MCP metrics (servers/resources/prompts)
  with magenta LED dot indicator, shown after a divider from workstream
  metrics
- **Console node detail**: Per-node MCP summary showing server, resource,
  and prompt counts
- **Console collector**: Aggregates MCP counts across all nodes in the
  cluster overview

MCP indicators use the `--magenta` design token for consistent theming
across light and dark modes.
