"""LLM-guided interactive setup wizard for Turnstone deployments.

Entry point: turnstone-bootstrap

Walks users through configuring a single-node or multi-node Turnstone
deployment via a conversational AI assistant. Generates .env files,
docker-compose overrides, and post-start setup scripts.
"""

from __future__ import annotations

import getpass
import json
import os
import secrets
import socket
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from turnstone import __version__
from turnstone.ui.colors import BOLD, CYAN, DIM, GREEN, RED, RESET, YELLOW
from turnstone.ui.markdown import MarkdownRenderer
from turnstone.ui.spinner import Spinner

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-5.4",
    "anthropic": "claude-sonnet-4-6",
}

_SENSITIVE_PATTERNS = (
    "API_KEY",
    "PASSWORD",
    "SECRET",
    "TOKEN",
    "DISCORD_TOKEN",
)

# ---------------------------------------------------------------------------
# System prompt — encodes Turnstone architecture knowledge for the LLM
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are the Turnstone setup wizard, an expert assistant that helps users \
configure a Turnstone deployment interactively.

## About Turnstone
Turnstone is a multi-node AI orchestration platform. A deployment consists of:
- **Server** (turnstone-server): Web UI + chat workstreams + LLM interaction (port 8080)
- **Bridge** (turnstone-bridge): Redis-to-HTTP bridge for multi-node routing
- **Console** (turnstone-console): Cluster dashboard + admin panel (port 8090)
- **Redis**: Message broker, pub/sub, node registry
- **PostgreSQL** (production): Persistent database (dev can use SQLite)
- **Channel** (optional): Discord/Slack gateway

## Deployment Profiles (compose.yaml)
- **Default** (no flag): redis + console only (infrastructure, good for running external servers)
- **Production** (`--profile production`): redis + 1 server + 1 bridge + console + PostgreSQL + channel (single node)
- **Cluster** (`--profile cluster`): 10-node server/bridge fleet + PostgreSQL + channel + console (multi-node)
- **ddgCluster** (`--profile ddgCluster`): Cluster + DuckDuckGo Search MCP sidecar (web search via MCP, no API key needed)

## Environment Variables (.env)
The compose.yaml reads these from a `.env` file:

### LLM Provider (required)
- `LLM_BASE_URL` — OpenAI-compatible API endpoint (default: http://host.docker.internal:8000/v1). \
For local models (vLLM, llama.cpp, Ollama), this points to the local server. \
From inside Docker, use `http://host.docker.internal:<port>/v1` to reach the host machine.
- `OPENAI_API_KEY` — API key for the LLM provider. For local models that don't require \
authentication, set this to `dummy` (the compose.yaml defaults to `dummy` if unset). \
For commercial providers (OpenAI, Anthropic-via-proxy), use the real key.
- `MODEL` — Model name (optional, auto-detected if blank)
- `TAVILY_API_KEY` — Web search API key (optional)

### Database
- `DB_BACKEND` — `sqlite` (default) or `postgresql`
- `DATABASE_URL` — PostgreSQL connection string (production/cluster only)
- `POSTGRES_USER` — PostgreSQL username (default: turnstone)
- `POSTGRES_PASSWORD` — PostgreSQL password (required for production/cluster)

### Redis
- `REDIS_PASSWORD` — Redis password (optional but recommended)
- `REDIS_PORT` — Redis port (default: 6379)

### Authentication
- `TURNSTONE_AUTH_ENABLED` — Enable auth (`true`/empty)
- `TURNSTONE_JWT_SECRET` — JWT signing secret (required if auth enabled)
- `TURNSTONE_AUTH_TOKEN` — Static bearer token for inter-service auth

### Ports
- `SERVER_PORT` — Server port (default: 8080)
- `CONSOLE_PORT` — Console port (default: 8090)

### Channel Gateway (optional)
- `TURNSTONE_DISCORD_TOKEN` — Discord bot token
- `TURNSTONE_DISCORD_GUILD` — Restrict to single guild ID

### MCP Integration (optional)
- `MCP_CONFIG` — Path to MCP server config inside the container \
(e.g., `/etc/turnstone/mcp-ddg.json`). When set, servers connect to configured MCP servers on startup.
- The `ddgCluster` profile runs a DuckDuckGo Search MCP sidecar (Python) that provides \
`duckduckgo_web_search` and `duckduckgo_fetch_content` tools to every node. No API key required. \
The sidecar uses MCP streamable-http transport with DNS rebinding protection disabled \
(required for Docker internal networking) and binds to 0.0.0.0:3000 via FastMCP settings. \
Safe search is disabled by default.

### Cluster
- `HEARTBEAT_TTL` — Bridge heartbeat TTL in seconds (default: 60)
- `APPROVAL_TIMEOUT` — Tool approval timeout in seconds (default: 3600)

## Auth Setup Flow
After the stack starts, the first admin user is created via:
`POST /v1/api/auth/setup` with `{"username", "display_name", "password"}`
This is a one-time endpoint that only works when zero users exist.

Subsequent governance setup (roles, policies, templates) uses the console admin API \
with the JWT returned from setup.

## Built-in Roles
- **Admin** (`builtin-admin`): Full access — read, write, approve, all admin.* permissions
- **Operator** (`builtin-operator`): read, write, workstreams.create, workstreams.close
- **Viewer** (`builtin-viewer`): read only

## Tool Policies
Glob-pattern rules for tool execution. Actions: `allow`, `deny`, `ask`. \
First match by priority wins. Example: `{"name": "Block bash", "tool_pattern": "bash*", \
"action": "deny", "priority": 100}`

## Prompt Templates
Reusable system message templates with `{{variable}}` placeholders. \
Categories like "engineering", "analysis", etc.

## Your Task
Walk the user through setting up their deployment step by step:

1. **First**: Call `check_docker` and `read_file` on `.env` to detect existing state.
2. **Deployment mode**: Ask if they want single-node (`--profile production`) or multi-node \
(`--profile cluster`). Explain trade-offs.
3. **LLM provider for the deployment**: Which LLM backend their Turnstone will use \
(may differ from this wizard's model). Ask for base URL, API key, model name.
4. **Database**: SQLite (dev/simple) vs PostgreSQL (production/cluster). \
PostgreSQL is required for cluster mode.
5. **Security**: Recommend enabling auth for any non-local deployment. \
Use `generate_secret` for JWT secret, Redis password, auth token, and Postgres password. \
Ask for initial admin username and password.
6. **Ports**: Check defaults with `check_port`, suggest alternatives if conflicts.
7. **Optional features**: Discord integration, web search (Tavily key), \
DuckDuckGo Search MCP (for cluster — uses `ddgCluster` profile with \
`MCP_CONFIG=/etc/turnstone/mcp-ddg.json`, no API key needed).
8. **Generate .env**: Call `write_file` with the complete `.env` content.
9. **Generate setup.sh**: Call `write_file` with a post-start script that creates the admin \
user and any roles/policies/templates the user wants.
10. **Finish**: Call the `finish` tool with a summary of what was configured and the \
exact commands to run next (e.g., `docker compose --profile production up -d` then `./setup.sh`).

## Rules
- Be concise. Ask 1-2 questions at a time, not a wall of options.
- NEVER echo API keys or passwords back to the user in your text responses.
- ALWAYS use `generate_secret` for passwords and secrets — never invent them.
- When writing files, use `write_file` — the user will see a preview and confirm.
- If an existing .env is detected, summarize what's configured and ask what to change.
- For cluster mode, the compose.yaml has a fixed 10-node fleet — no override needed.
- For cluster + DuckDuckGo Search, use `--profile ddgCluster` instead of `--profile cluster`. \
Set `MCP_CONFIG=/etc/turnstone/mcp-ddg.json` in `.env`. No API key needed. \
The DuckDuckGo MCP sidecar starts automatically and all cluster nodes connect to it. \
Note: the MCP SDK's DNS rebinding protection must be disabled for Docker-internal networking \
(the compose.yaml handles this), and the server must bind to 0.0.0.0 (not 127.0.0.1) to be \
reachable from other containers.
- The `DATABASE_URL` for docker compose internal networking uses the hostname `postgres` \
(e.g., `postgresql://turnstone:<password>@postgres:5432/turnstone`).
- For local LLM backends (vLLM, llama.cpp, Ollama, etc.), set `OPENAI_API_KEY=dummy` in the \
.env file — local servers typically don't require authentication. The `LLM_BASE_URL` should \
use `host.docker.internal` to reach the host machine from inside Docker \
(e.g., `http://host.docker.internal:8000/v1`).
- If Docker is NOT installed, tell the user they need to install it before proceeding. \
Give them the install command for their platform: \
Linux: `curl -fsSL https://get.docker.com | sh`, \
macOS: "Install Docker Desktop from https://docs.docker.com/desktop/install/mac-install/", \
Windows: "Install Docker Desktop from https://docs.docker.com/desktop/install/windows-install/". \
You can still generate the config files — they just can't start the stack until Docker is installed.

"""

# ---------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling format)
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the contents of a file relative to the project directory. "
                "Returns the file content or an error if the file doesn't exist."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to the project root.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write content to a file. The user will be shown a preview and "
                "asked to confirm before the write happens. The file is created "
                "if it doesn't exist."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to the project root.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full file content to write.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_secret",
            "description": (
                "Generate a cryptographically secure random hex string for use "
                "as JWT secrets, passwords, auth tokens, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "length": {
                        "type": "integer",
                        "description": (
                            "Number of random bytes. Output will be 2x this in "
                            "hex characters. Default: 32."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_port",
            "description": "Check if a TCP port is available (not in use) on localhost.",
            "parameters": {
                "type": "object",
                "properties": {
                    "port": {
                        "type": "integer",
                        "description": "Port number to check.",
                    },
                },
                "required": ["port"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_api_key",
            "description": (
                "Test an API key by making a lightweight request to the provider. "
                "Returns success/failure and any error message."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "enum": ["openai", "anthropic"],
                        "description": "Provider name.",
                    },
                    "api_key": {
                        "type": "string",
                        "description": "API key to validate.",
                    },
                    "base_url": {
                        "type": "string",
                        "description": (
                            "API base URL. Only needed for OpenAI-compatible endpoints."
                        ),
                    },
                },
                "required": ["provider", "api_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_docker",
            "description": (
                "Check if Docker and Docker Compose are installed and the Docker "
                "daemon is running. Returns version info or error details."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "Call this tool when the bootstrap setup is complete and all files "
                "have been written. Displays a final summary and exits the wizard. "
                "You MUST call this after writing all config files and printing "
                "the next-steps summary."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": (
                            "A short summary of what was configured (deployment mode, "
                            "files written, next commands to run)."
                        ),
                    },
                },
                "required": ["summary"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _mask_secrets(text: str) -> str:
    """Mask sensitive values in text for display preview."""
    lines = text.split("\n")
    masked: list[str] = []
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            key_upper = key.strip().upper()
            if any(pat in key_upper for pat in _SENSITIVE_PATTERNS) and len(value) > 8:
                masked.append(f"{key}={value[:4]}****{value[-4:]}")
                continue
        masked.append(line)
    return "\n".join(masked)


def _resolve_safe(project_dir: Path, raw_path: str) -> Path | None:
    """Resolve a path and verify it stays within project_dir. Returns None if unsafe."""
    resolved = (project_dir / raw_path).resolve()
    if not resolved.is_relative_to(project_dir.resolve()):
        return None
    return resolved


def _tool_read_file(project_dir: Path, args: dict[str, Any]) -> str:
    """Read a file relative to the project directory."""
    raw = str(args["path"])
    path = _resolve_safe(project_dir, raw)
    if path is None:
        return f"Error: path escapes project directory: {raw}"
    try:
        content: str = path.read_text(encoding="utf-8")
        return content
    except FileNotFoundError:
        return f"Error: file not found: {args['path']}"
    except (OSError, UnicodeDecodeError) as exc:
        return f"Error reading {args['path']}: {exc}"


def _tool_write_file(project_dir: Path, args: dict[str, Any]) -> str:
    """Write a file with user confirmation."""
    raw = str(args["path"])
    path = _resolve_safe(project_dir, raw)
    if path is None:
        return f"Error: path escapes project directory: {raw}"
    content = args["content"]

    # Skip if file already exists with identical content
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
            if existing == content:
                return f"File already exists with identical content: {args['path']}"
        except (OSError, UnicodeDecodeError):
            pass

    line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)

    # Show preview
    print(f"\n{YELLOW} Writing {args['path']} ({line_count} lines){RESET}")
    print(f"{DIM}{'─' * 50}{RESET}")
    preview = _mask_secrets(content)
    for line in preview.split("\n")[:50]:
        print(f"  {DIM}{line}{RESET}")
    if line_count > 50:
        print(f"  {DIM}... ({line_count - 50} more lines){RESET}")
    print(f"{DIM}{'─' * 50}{RESET}")

    try:
        choice = input(f"{BOLD}Write this file? [Y/n]{RESET} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "User cancelled the write."
    if choice in ("n", "no"):
        return "User declined to write file."

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    # Make .sh files executable
    if path.suffix == ".sh":
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)

    return f"File written successfully: {args['path']}"


def _tool_generate_secret(args: dict[str, Any]) -> str:
    """Generate a cryptographically secure random hex string."""
    length = args.get("length", 32)
    if not isinstance(length, int) or length < 1 or length > 128:
        length = 32
    return secrets.token_hex(length)


def _tool_check_port(args: dict[str, Any]) -> str:
    """Check if a TCP port is available on localhost."""
    port = args["port"]
    if not isinstance(port, int) or port < 1 or port > 65535:
        return f"Error: invalid port number: {port}"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            result = sock.connect_ex(("127.0.0.1", port))
            if result == 0:
                return f"Port {port} is IN USE (something is already listening)."
            return f"Port {port} is AVAILABLE."
    except OSError as exc:
        return f"Error checking port {port}: {exc}"


def _tool_validate_api_key(args: dict[str, Any]) -> str:
    """Validate an API key with a lightweight request."""
    provider = args["provider"]
    api_key = args["api_key"]
    base_url = args.get("base_url")

    if provider == "openai":
        try:
            from openai import OpenAI

            kwargs: dict[str, Any] = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            oai_client = OpenAI(**kwargs)
            oai_client.models.list()
            return "Success: API key is valid."
        except Exception as exc:
            return f"Failed: {exc}"

    elif provider == "anthropic":
        try:
            import anthropic

            ant_client = anthropic.Anthropic(api_key=api_key)
            ant_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
            return "Success: API key is valid."
        except Exception as exc:
            return f"Failed: {exc}"

    return f"Error: unknown provider '{provider}'"


def _tool_check_docker(args: dict[str, Any]) -> str:
    """Check Docker and Docker Compose availability."""
    results: list[str] = []

    # Check Docker
    try:
        proc = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            results.append(f"Docker: installed (version {proc.stdout.strip()})")
        else:
            stderr = proc.stderr.strip()
            if "Cannot connect" in stderr or "Is the docker daemon running" in stderr:
                results.append("Docker: installed but daemon is NOT running")
            else:
                results.append(f"Docker: error — {stderr}")
    except FileNotFoundError:
        results.append("Docker: NOT installed")
    except subprocess.TimeoutExpired:
        results.append("Docker: timed out (daemon may be unresponsive)")

    # Check Docker Compose
    try:
        proc = subprocess.run(
            ["docker", "compose", "version", "--short"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            results.append(f"Docker Compose: installed (version {proc.stdout.strip()})")
        else:
            results.append("Docker Compose: NOT available")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        results.append("Docker Compose: NOT available")

    return "\n".join(results)


class _FinishError(Exception):
    """Raised by the finish tool to signal the wizard is done."""

    def __init__(self, summary: str) -> None:
        self.summary = summary


def _tool_finish(args: dict[str, Any]) -> str:
    """Signal that the bootstrap wizard is complete."""
    raise _FinishError(args.get("summary", "Setup complete."))


# Tool dispatch table
TOOL_FUNCTIONS: dict[str, Any] = {
    "read_file": _tool_read_file,
    "write_file": _tool_write_file,
    "generate_secret": _tool_generate_secret,
    "check_port": _tool_check_port,
    "validate_api_key": _tool_validate_api_key,
    "check_docker": _tool_check_docker,
    "finish": _tool_finish,
}

# Tools that need the project_dir argument
_PROJECT_DIR_TOOLS = frozenset({"read_file", "write_file"})


def execute_tool(name: str, args: dict[str, Any], project_dir: Path) -> str:
    """Execute a tool and return the result string.

    Raises _FinishError when the finish tool is called.
    """
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return f"Error: unknown tool '{name}'"
    try:
        if name in _PROJECT_DIR_TOOLS:
            result: str = fn(project_dir, args)
        else:
            result = fn(args)
        return result
    except _FinishError:
        raise
    except Exception as exc:
        return f"Error executing {name}: {exc}"


# ---------------------------------------------------------------------------
# _BootstrapLLM — thin wrapper over OpenAI / Anthropic SDKs
# ---------------------------------------------------------------------------


class _BootstrapLLM:
    """Provider-agnostic wrapper for non-streaming tool-calling completions."""

    def __init__(self, provider: str, client: Any, model: str) -> None:
        self.provider = provider
        self.client = client
        self.model = model

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]] | None, str]:
        """Run a completion and return (content, tool_calls, stop_reason)."""
        if self.provider == "anthropic":
            return self._complete_anthropic(messages, tools)
        return self._complete_openai(messages, tools)

    # -- OpenAI path --------------------------------------------------------

    def _complete_openai(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]] | None, str]:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools if tools else None,
        )
        choice = resp.choices[0]
        content = choice.message.content or ""
        tool_calls = None
        if choice.message.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in choice.message.tool_calls
            ]
        return content, tool_calls, choice.finish_reason or "stop"

    # -- Anthropic path -----------------------------------------------------

    def _complete_anthropic(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]] | None, str]:
        # Extract system message
        system_text = ""
        api_messages: list[dict[str, Any]] = []
        for msg in messages:
            if msg["role"] == "system":
                system_text = msg["content"]
            elif msg["role"] == "tool":
                # Convert OpenAI tool result to Anthropic format
                api_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg["tool_call_id"],
                                "content": msg["content"],
                            }
                        ],
                    }
                )
            elif msg["role"] == "assistant" and msg.get("tool_calls"):
                # Convert assistant tool_calls to Anthropic content blocks
                blocks: list[dict[str, Any]] = []
                if msg.get("content"):
                    blocks.append({"type": "text", "text": msg["content"]})
                for tc in msg["tool_calls"]:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["function"]["name"],
                            "input": json.loads(tc["function"]["arguments"]),
                        }
                    )
                api_messages.append({"role": "assistant", "content": blocks})
            else:
                api_messages.append(msg)

        # Merge consecutive same-role messages (Anthropic requires alternation)
        merged: list[dict[str, Any]] = []
        for msg in api_messages:
            if merged and merged[-1]["role"] == msg["role"]:
                # Merge content
                prev = merged[-1]
                prev_content = prev["content"]
                new_content = msg["content"]
                if isinstance(prev_content, str) and isinstance(new_content, str):
                    prev["content"] = prev_content + "\n" + new_content
                elif isinstance(prev_content, str):
                    prev["content"] = [{"type": "text", "text": prev_content}] + (
                        new_content if isinstance(new_content, list) else [new_content]
                    )
                elif isinstance(new_content, str):
                    prev["content"] = prev_content + [{"type": "text", "text": new_content}]
                else:
                    prev["content"] = prev_content + new_content
            else:
                merged.append(msg)
        api_messages = merged

        # Convert tools
        api_tools = [
            {
                "name": t["function"]["name"],
                "description": t["function"]["description"],
                "input_schema": t["function"]["parameters"],
            }
            for t in tools
        ]

        resp = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system_text,
            messages=api_messages,
            tools=api_tools if api_tools else [],
        )

        # Parse response
        content_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in resp.content:
            if block.type == "text":
                content_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    {
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": json.dumps(block.input),
                        },
                    }
                )

        content = "\n".join(content_parts)
        return (
            content,
            tool_calls if tool_calls else None,
            resp.stop_reason or "end_turn",
        )


# ---------------------------------------------------------------------------
# Interactive startup (Phase 1: before LLM)
# ---------------------------------------------------------------------------


def _print_banner() -> None:
    print(f"\n{BOLD}{CYAN} Turnstone Bootstrap Wizard{RESET}  {DIM}v{__version__}{RESET}")
    print(f" {DIM}{'─' * 48}{RESET}")
    print()
    print(" This wizard uses an AI model to walk you through")
    print(" setting up a Turnstone deployment. You'll need an")
    print(" API key for one of the supported providers.")
    print()


def _select_provider() -> tuple[str, Any, str]:
    """Interactive provider/model/key selection. Returns (provider, client, model)."""
    print(f" {BOLD}Which provider for this wizard?{RESET}")
    print(f"   {CYAN}[1]{RESET} OpenAI")
    print(f"   {CYAN}[2]{RESET} Anthropic")
    print(f"   {CYAN}[3]{RESET} OpenAI-compatible (local/vLLM)")
    print()

    while True:
        try:
            choice = input(f" {BOLD}>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            sys.exit(0)
        if choice in ("1", "2", "3"):
            break
        print(f" {RED}Please enter 1, 2, or 3.{RESET}")

    if choice == "1":
        return _setup_openai()
    elif choice == "2":
        return _setup_anthropic()
    else:
        return _setup_local()


def _prompt_api_key(env_var: str, label: str) -> str:
    """Prompt for an API key, checking env var first."""
    env_val = os.environ.get(env_var, "")
    if env_val:
        prefix = env_val[:4] + "..." if len(env_val) > 4 else env_val
        print(f"\n Found {CYAN}${env_var}{RESET} in environment ({DIM}{prefix}{RESET})")
        try:
            use_env = input(f" Use it? {BOLD}[Y/n]{RESET} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            sys.exit(0)
        if use_env not in ("n", "no"):
            return env_val

    print(f"\n {label}")
    try:
        key = getpass.getpass(" API key: ")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        sys.exit(0)
    if not key.strip():
        print(f" {RED}API key cannot be empty.{RESET}")
        sys.exit(1)
    return key.strip()


def _prompt_model(provider: str) -> str:
    """Prompt for model name with a sensible default."""
    default = _DEFAULT_MODELS.get(provider, "")
    prompt = f" Model {DIM}[{default}]{RESET}: " if default else " Model name: "
    try:
        model = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        sys.exit(0)
    return model or default


def _setup_openai() -> tuple[str, Any, str]:
    from openai import OpenAI

    api_key = _prompt_api_key("OPENAI_API_KEY", "Enter your OpenAI API key:")
    model = _prompt_model("openai")
    client = OpenAI(api_key=api_key)
    return "openai", client, model


def _setup_anthropic() -> tuple[str, Any, str]:
    try:
        import anthropic
    except ImportError:
        print(f"\n {RED}The 'anthropic' package is not installed.{RESET}")
        print(f" Install it with: {CYAN}pip install turnstone[anthropic]{RESET}")
        sys.exit(1)

    api_key = _prompt_api_key("ANTHROPIC_API_KEY", "Enter your Anthropic API key:")
    model = _prompt_model("anthropic")
    client = anthropic.Anthropic(api_key=api_key)
    return "anthropic", client, model


def _detect_models(client: Any) -> list[str]:
    """Query /v1/models and return a sorted list of model IDs."""
    try:
        resp = client.models.list()
        models = sorted(m.id for m in resp.data)
        return models
    except Exception:
        return []


def _setup_local() -> tuple[str, Any, str]:
    from openai import OpenAI

    print("\n Enter the base URL of your OpenAI-compatible endpoint.")
    default_url = "http://localhost:8000/v1"
    try:
        url = input(f" Base URL {DIM}[{default_url}]{RESET}: ").strip() or default_url
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        sys.exit(0)

    # Local endpoints often don't need a real key
    env_key = os.environ.get("OPENAI_API_KEY", "")
    if env_key:
        api_key = env_key
        print(f" Using {CYAN}$OPENAI_API_KEY{RESET} from environment.")
    else:
        print(" API key (press Enter for 'none'):")
        try:
            api_key = getpass.getpass(" API key: ") or "none"
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            sys.exit(0)

    client = OpenAI(api_key=api_key, base_url=url)

    # Try to auto-detect available models
    print(f"\n {DIM}Querying {url} for available models...{RESET}")
    available = _detect_models(client)

    if len(available) == 1:
        model = available[0]
        print(f" Found model: {CYAN}{model}{RESET}")
    elif available:
        print(f" Found {len(available)} model(s):")
        for i, m in enumerate(available, 1):
            print(f"   {CYAN}[{i}]{RESET} {m}")
        print()
        try:
            choice = input(f" Select model {DIM}[1]{RESET}: ").strip() or "1"
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            sys.exit(0)
        try:
            idx = int(choice) - 1
            model = available[idx] if 0 <= idx < len(available) else choice
        except ValueError:
            model = choice  # Treat as literal model name
    else:
        print(f" {YELLOW}Could not auto-detect models.{RESET}")
        try:
            model = input(" Model name: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            sys.exit(0)
        if not model:
            print(f" {RED}Model name is required for local endpoints.{RESET}")
            sys.exit(1)

    return "openai", client, model


def _validate_connection(llm: _BootstrapLLM) -> bool:
    """Validate the LLM connection with a minimal request."""
    try:
        content, _, _ = llm.complete(
            [
                {"role": "system", "content": "Reply with exactly: ok"},
                {"role": "user", "content": "ping"},
            ],
            [],
        )
        return True
    except Exception as exc:
        print(f"\n {RED}Connection failed: {exc}{RESET}")
        return False


# ---------------------------------------------------------------------------
# Conversation loop
# ---------------------------------------------------------------------------


def _run_conversation(
    llm: _BootstrapLLM,
    project_dir: Path,
) -> None:
    """Main LLM-driven conversation loop."""
    renderer = MarkdownRenderer()

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "I'd like to set up Turnstone. Please start by checking if "
                "Docker is available and if there's an existing .env configuration."
            ),
        },
    ]

    _max_retries = 3
    retries = 0

    while True:
        # Get LLM response
        with Spinner("Thinking"):
            try:
                content, tool_calls, reason = llm.complete(messages, TOOLS)
            except KeyboardInterrupt:
                print(f"\n{DIM}(Interrupted. Type 'quit' to exit.){RESET}")
                messages.append(
                    {"role": "user", "content": "The user interrupted. Ask what they need."}
                )
                continue
            except Exception as exc:
                retries += 1
                if retries >= _max_retries:
                    print(f"\n{RED}LLM error after {_max_retries} attempts: {exc}{RESET}")
                    print("Please check your connection and try again.")
                    return
                print(f"\n{RED}LLM error: {exc}{RESET}")
                print(f"{DIM}Retrying ({retries}/{_max_retries})...{RESET}")
                continue

        retries = 0  # Reset on success

        # Build assistant message
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": content or ""}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        # Print text content
        if content:
            rendered = renderer.feed(content + "\n")
            flushed = renderer.flush()
            print(rendered + flushed, end="")

        # Execute tool calls
        if tool_calls:
            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError as exc:
                    result = f"Error: invalid JSON arguments: {exc}"
                    args = {}
                else:
                    print(f"  {DIM}[{name}]{RESET}", end="")
                    if name in ("read_file", "write_file") and "path" in args:
                        print(f" {DIM}{args['path']}{RESET}")
                    elif name == "check_port" and "port" in args:
                        print(f" {DIM}:{args['port']}{RESET}")
                    else:
                        print()
                    try:
                        result = execute_tool(name, args, project_dir)
                    except _FinishError as fin:
                        print(f"\n{GREEN}{BOLD} Setup complete!{RESET}\n")
                        rendered = renderer.feed(fin.summary + "\n")
                        flushed = renderer.flush()
                        print(rendered + flushed, end="")
                        return

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    }
                )
            continue  # Let LLM process tool results

        # No tool calls — prompt user
        print()
        try:
            user_input = input(f"{BOLD}>{RESET} ").strip()
        except EOFError:
            print("\nGoodbye!")
            return
        except KeyboardInterrupt:
            print(f"\n{DIM}(Press Ctrl+C again to quit, or type your response.){RESET}")
            try:
                user_input = input(f"{BOLD}>{RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                return

        if user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            return

        if not user_input:
            continue

        messages.append({"role": "user", "content": user_input})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for turnstone-bootstrap CLI."""
    _print_banner()

    # Phase 1: Interactive provider selection
    provider, client, model = _select_provider()
    llm = _BootstrapLLM(provider, client, model)

    # Validate connection
    print(f"\n {DIM}Validating connection...{RESET}")
    if not _validate_connection(llm):
        print(f" {RED}Could not connect to the model. Please check your settings.{RESET}")
        sys.exit(1)

    print(f"\n {GREEN}Connected to {BOLD}{model}{RESET}{GREEN}.{RESET}")
    print(f" {DIM}Handing off to AI assistant...{RESET}\n")

    # Phase 2: LLM-driven conversation
    project_dir = Path.cwd()
    try:
        _run_conversation(llm, project_dir)
    except KeyboardInterrupt:
        print("\nGoodbye!")
        sys.exit(0)


if __name__ == "__main__":
    main()
