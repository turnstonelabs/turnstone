# Tools Reference

turnstone exposes 15 built-in tools plus any number of external MCP tools to the
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
| `TOOLS`             | All 15 tool definitions (sent to the model). |
| `AGENT_TOOLS`       | Tools with `agent: true` -- available to plan sub-agents. Read-only tools. |
| `TASK_AGENT_TOOLS`  | Tools with `task_agent: true` -- available to task sub-agents. Includes write operations. |
| `AGENT_AUTO_TOOLS`  | Set of tool names with `auto_approve: true` -- no user confirmation needed. |
| `TASK_AUTO_TOOLS`   | Same as `AGENT_AUTO_TOOLS` (identical filter). |
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
- If `auto_approve` is `True` on the session (headless mode), all tools are
  approved automatically.

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
- `remember` -- writes to persistent memory database (lightweight, always auto-approved)
- `recall` -- reads from persistent memory database
- `forget` -- deletes from persistent memory database (lightweight, always auto-approved)
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
| `remember`   | `key`       |
| `recall`     | `query`     |
| `forget`     | `key`       |
| `notify`     | `message`   |

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

Read the contents of a file, returning numbered lines.

| Parameter | Type    | Required | Description |
|-----------|---------|----------|-------------|
| `path`    | string  | yes      | Absolute or relative file path. |
| `offset`  | integer | no       | Line number to start from (1-based, default: 1). |
| `limit`   | integer | no       | Maximum number of lines to read. Omit for full file. |

- **What it does**: Reads the file and returns content with line numbers. Must be called before `edit_file` on the same path (the session tracks which files have been read).
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

### remember

Save a persistent memory that persists across sessions.

| Parameter | Type   | Required | Description |
|-----------|--------|----------|-------------|
| `key`     | string | yes      | Short identifier (e.g. `user_name`). |
| `value`   | string | yes      | Content to remember. |

- **What it does**: Stores a key-value pair in the SQLite memory database. Memories persist across sessions and are included in the system prompt on startup.
- **Auto-approve**: Yes.
- **Agent availability**: Not available to sub-agents (top-level only).

---

### recall

Search memories and past conversations.

| Parameter | Type    | Required | Description |
|-----------|---------|----------|-------------|
| `query`   | string  | no       | Search term or phrase. Omit to list all memories. |
| `limit`   | integer | no       | Max conversation results to return (default 20). |

- **What it does**: With no query, lists all saved memories. With a query, searches both the memory store and conversation history using FTS5 full-text search.
- **Auto-approve**: Yes.
- **Agent availability**: Not available to sub-agents (top-level only).

---

### forget

Remove a persistent memory by key.

| Parameter | Type   | Required | Description |
|-----------|--------|----------|-------------|
| `key`     | string | yes      | The memory key to remove (e.g. `user_name`). |

- **What it does**: Deletes the memory entry with the given key from the SQLite database.
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
| `remember`   | Memory     | Yes          | No    | No         | `key`       |
| `recall`     | Memory     | Yes          | No    | No         | `query`     |
| `forget`     | Memory     | Yes          | No    | No         | `key`       |
| `notify`     | Notify     | Yes          | Yes   | Yes        | `message`   |

---

## MCP Tools (External)

Turnstone supports the [Model Context Protocol](https://modelcontextprotocol.io/)
(MCP) for connecting external tool servers — GitHub, databases, filesystems, or any
MCP-compatible service.

### How it works

1. **Configuration**: MCP servers are defined in `config.toml` under `[mcp.servers.*]`
   sections, or via a standard MCP JSON config file (`--mcp-config`).

2. **Discovery**: At startup, `MCPClientManager` connects to each configured server
   (via stdio subprocess or HTTP), performs the MCP `initialize` handshake, and calls
   `tools/list` to discover available tools.

3. **Schema conversion**: Each MCP tool's `inputSchema` is converted to OpenAI
   function-calling format. The tool name is prefixed: `mcp__{server}__{tool}`.

4. **Merging**: MCP tools are appended after the 14 built-in tools via
   `merge_mcp_tools()`. Built-in tools appear first, giving them natural LLM priority.

5. **Dispatch**: When the LLM calls an MCP tool, `_prepare_mcp_tool()` builds a
   generic approval preview and `_exec_mcp_tool()` calls `MCPClientManager.call_tool_sync()`,
   which dispatches the call to the background asyncio event loop.

### Approval behavior

MCP tools **require user approval by default** (`needs_approval: True`). turnstone
does not auto-approve MCP tools based on their schema, since it cannot guarantee
that external tools are read-only. However, global overrides such as
`--skip-permissions` or the UI's "always allow" setting will auto-approve all
tools, including MCP tools.

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
