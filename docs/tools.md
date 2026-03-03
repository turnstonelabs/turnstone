# Tools Reference

turnstone exposes 14 tools to the LLM via the OpenAI function-calling interface.
Each tool is defined as a JSON file under `turnstone/tools/` and loaded at startup
by `turnstone/core/tools.py`.

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
| `TOOLS`             | All 14 tool definitions (sent to the model). |
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

- **What it does**: Searches the web via the Tavily API and returns ranked results with titles, URLs, and content snippets.
- **Auto-approve**: No -- requires user confirmation (makes network requests).
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

- **What it does**: Spawns a planning sub-agent with `AGENT_TOOLS` (read-only tools: `read_file`, `search`, `math`, `man`, `web_fetch`, `web_search`). The agent explores the codebase and writes a structured plan to `.plan-<session_id>.md` (unique per session, so concurrent workstreams never collide). If the `plan` tool has been called before in the same session, the prior plan is passed to the agent as context so it refines rather than restarts. After completion, the user is prompted to review and can accept, reject, or annotate the plan.
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
