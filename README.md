# Turnstone

[![CI](https://github.com/turnstonelabs/turnstone/actions/workflows/ci.yml/badge.svg)](https://github.com/turnstonelabs/turnstone/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/turnstone)](https://pypi.org/project/turnstone/)
[![Python](https://img.shields.io/pypi/pyversions/turnstone)](https://pypi.org/project/turnstone/)
[![License](https://img.shields.io/badge/license-BSL--1.1-blue)](LICENSE)

Multi-node AI orchestration platform. Deploy tool-using AI agents across a cluster of servers, driven by message queues or interactive interfaces.

Named after the [Ruddy Turnstone](https://en.wikipedia.org/wiki/Ruddy_turnstone) — a bird that flips rocks to expose what's hiding underneath.

## What it does

Turnstone gives LLMs tools — shell, files, search, web, planning — and orchestrates multi-turn conversations where the model investigates, acts, and reports. It runs as:

- **Interactive sessions** — terminal CLI or browser UI with parallel workstreams
- **Queue-driven agents** — trigger workstreams via message queue, stream progress, approve or auto-approve tool use
- **Multi-node clusters** — generic work load-balances across nodes, directed work routes to a specific server
- **Cluster dashboard** — real-time view of all nodes and workstreams, reverse proxy for server UIs
- **Intent validation** — an LLM judge evaluates every tool call before approval, presenting risk assessments and evidence-based recommendations so users can make informed decisions instead of blindly approving raw tool calls
- **Governance & compliance** — RBAC, OIDC SSO (Okta, Azure AD, Google, Keycloak), tool policies, prompt templates, workstream templates, usage tracking, and append-only audit logs
- **Cluster simulator** — test the stack at scale (up to 1000 nodes) without an LLM backend

Works with any OpenAI-compatible API (vLLM, llama.cpp, NVIDIA NIM) or Anthropic's native Messages API. Supports [MCP](https://modelcontextprotocol.io/) for external tool servers with native deferred tool loading on Anthropic and OpenAI APIs (BM25 fallback for local models).

<p align="center">
  <img src="docs/diagrams/architecture-overview.svg" alt="Turnstone system architecture — data flow from clients through gateways, Redis MQ, cluster nodes, to LLM providers" width="960"/>
</p>

## Quickstart

### Interactive (terminal)

```bash
pip install turnstone
turnstone --base-url http://localhost:8000/v1
```

### Interactive (browser)

```bash
turnstone-server --port 8080 --base-url http://localhost:8000/v1
```

### Queue-driven (programmatic)

```bash
pip install turnstone[mq]
turnstone-bridge --server-url http://localhost:8080 --redis-host localhost
```

```python
from turnstone.mq import TurnstoneClient

with TurnstoneClient() as client:
    # Generic — any available node picks it up
    result = client.send_and_wait("Analyze the error logs", auto_approve=True)
    print(result.content)

    # Directed — must run on a specific server
    result = client.send_and_wait(
        "Check disk I/O on this server",
        target_node="server-12",
        auto_approve=True,
    )
```

### Cluster dashboard

```bash
pip install turnstone[console]
turnstone-console --redis-host localhost --port 8090
```

Then open `http://localhost:8090` for the cluster-wide dashboard. Create workstreams from the console and interact with any node's server UI through the built-in reverse proxy — no direct server port access required.

### Docker

```bash
cp .env.example .env  # edit LLM_BASE_URL, OPENAI_API_KEY, etc.
docker compose up     # starts redis + server + bridge + console (SQLite)
```

For production with PostgreSQL:

```bash
# Requires POSTGRES_PASSWORD and DB_BACKEND=postgresql in .env (or exported)
docker compose --profile production up  # adds PostgreSQL, uses it as database
```

Console dashboard at http://localhost:8090. See [docs/docker.md](docs/docker.md) for configuration, scaling, and profiles.

### Simulator

Test the multi-node stack at scale without an LLM backend:

```bash
docker compose --profile sim up redis console sim
```

Or standalone:

```bash
pip install turnstone[sim]
turnstone-sim --nodes 100 --scenario steady --duration 60 --mps 10
```

See [docs/simulator.md](docs/simulator.md) for scenarios, CLI reference, and metrics.

## Architecture

### Diagrams

Detailed UML diagrams are available in [`docs/diagrams/`](docs/diagrams/):

| Diagram | Description |
|---------|-------------|
| [System Context](docs/diagrams/png/01-system-context.png) | Top-level components and external dependencies |
| [Package Structure](docs/diagrams/png/02-package-structure.png) | Python modules and dependency graph |
| [Core Engine Classes](docs/diagrams/png/03-core-engine-classes.png) | SessionUI protocol, ChatSession, LLMProvider, WorkstreamManager |
| [Conversation Turn](docs/diagrams/png/04-conversation-turn.png) | Full message lifecycle through the engine (provider-agnostic) |
| [Tool Pipeline](docs/diagrams/png/05-tool-pipeline.png) | Three-phase prepare/approve/execute |
| [MQ Protocol](docs/diagrams/png/06-mq-protocol.png) | 9 inbound + 19 outbound message types |
| [Message Routing](docs/diagrams/png/07-message-routing.png) | Multi-node routing scenarios |
| [Redis Key Schema](docs/diagrams/png/08-redis-key-schema.png) | All Redis keys, types, and TTLs |
| [Workstream States](docs/diagrams/png/09-workstream-states.png) | State machine transitions |
| [Simulator](docs/diagrams/png/10-simulator-architecture.png) | SimCluster, dispatchers, scenarios |
| [Console Data Flow](docs/diagrams/png/11-console-data-flow.png) | Dashboard data collection threads |
| [Deployment](docs/diagrams/png/12-deployment.png) | Docker Compose service topology |
| [SDK Architecture](docs/diagrams/png/13-sdk-architecture.png) | Python + TypeScript client libraries |
| [Storage Architecture](docs/diagrams/png/14-storage-architecture.png) | Pluggable database backends (SQLite + PostgreSQL) |
| [Auth Architecture](docs/diagrams/png/15-auth-architecture.png) | JWT, scopes, token types, login flows |
| [Channel Architecture](docs/diagrams/png/16-channel-architecture.png) | Discord/Slack adapter protocol and routing |
| [Notify Flow](docs/diagrams/png/17-notify-flow.png) | Channel notification dispatch |
| [Watch Architecture](docs/diagrams/png/18-watch-architecture.png) | Periodic command polling daemon |
| [Governance Architecture](docs/diagrams/png/19-governance-architecture.png) | RBAC, policies, audit, usage enforcement flow |
| [WS Template Architecture](docs/diagrams/png/21-ws-template-architecture.png) | Workstream template application and lifecycle |
| [Judge Architecture](docs/diagrams/png/22-judge-architecture.png) | Intent validation two-tier evaluation pipeline |
| [OIDC Architecture](docs/diagrams/png/25-oidc-architecture.png) | OIDC SSO authorization code flow with PKCE |

### Governance

Turnstone includes a built-in governance layer for enterprise deployments — manage who can do what, which tools run unattended, and where every token goes.

- **RBAC** — 15 granular permissions, 3 built-in roles (admin / operator / viewer), custom roles, privilege escalation prevention
- **OIDC SSO** — single sign-on via any OpenID Connect provider (Okta, Azure AD, Google, Keycloak); Authorization Code Flow with PKCE, auto-provisioning, claim-based role mapping with demotion propagation; see [docs/oidc.md](docs/oidc.md)
- **Tool policies** — glob-pattern rules (`allow` / `deny` / `ask`) with priority ordering; automate approvals or lock down dangerous tools
- **Prompt templates** — reusable system messages with `{{variable}}` substitution and categories
- **Usage tracking** — per-request token and tool metrics, aggregation by day / model / user, automatic 90-day pruning
- **Audit logging** — append-only event trail for all admin mutations, IP-aware, 365-day retention

All governance features are managed through the console admin panel (13 tabs) and the full REST API. Runtime settings (model, tools, rate limiting, health, judge, memory) are configurable via the admin Settings tab — no config file edits or restarts needed for most changes. See [docs/governance.md](docs/governance.md) for setup and [docs/settings.md](docs/settings.md) for the settings reference.

### Intent Validation (LLM Judge)

Every tool call that requires human approval is evaluated by an intent validation judge that provides a structured risk assessment alongside the approval prompt — so instead of "approve this bash command?", users see a verdict with risk level, confidence, recommendation, and reasoning.

The system uses a two-tier evaluation pipeline:

1. **Heuristic tier** (instant, free) — 36 pattern-based rules classify tool calls by severity. Catches destructive commands (`rm -rf /`, `DROP TABLE`), privilege escalation (`sudo`), credential access, supply chain risks, browser data export, cloud infrastructure mutations, and more. Results appear immediately.
2. **LLM judge tier** (async) — A full LLM evaluation runs in the background with access to `read_file` and `list_directory` for evidence gathering. The judge can inspect files that a write would overwrite, check directory contents before a delete, and cite specific evidence in its reasoning. Results update the UI progressively when ready.

The judge defaults to the same model as the session (self-consistency) but can be configured to use a separate model — useful when running a small local model for tasks but wanting a commercial model for safety evaluation.

```toml
[judge]
enabled = true           # on by default
model = ""               # empty = same as session model
provider = ""            # empty = same as session provider
timeout = 60.0           # generous for local models
```

Verdicts are persisted for audit and exposed via Prometheus metrics (`turnstone_judge_verdicts_total`, `turnstone_judge_llm_latency_seconds`).

Skills are also scanned at install time — the scanner evaluates content, supply chain, vulnerability, and declared capability risk across four independent axes. Results populate `scan_status` (tier) and `scan_report` (structured JSON breakdown) on the skill record so administrators can assess risk before enabling a skill.

See [docs/judge.md](docs/judge.md) for the full guide.

## Multi-node routing

Each Turnstone server runs a bridge process. Bridges share a Redis instance for coordination:

| Redis Key | Purpose |
|-----------|---------|
| `turnstone:inbound` | Shared work queue — generic tasks, any node |
| `turnstone:inbound:{node_id}` | Per-node queue — directed tasks |
| `turnstone:ws:{ws_id}` | Workstream ownership — auto-routes follow-ups |
| `turnstone:node:{node_id}` | Node heartbeat + metadata for discovery |
| `turnstone:events:{ws_id}` | Per-workstream event pub/sub |
| `turnstone:events:global` | Global event pub/sub |
| `turnstone:events:cluster` | Cluster-wide state changes (for turnstone-console) |

**Routing rules:**
1. Message has `target_node` → routes to that node's queue
2. Message has `ws_id` → looks up owner, routes to owning node
3. Neither → shared queue, next available bridge picks it up

Bridges BLPOP from their per-node queue (priority) then the shared queue. Directed work always takes precedence.

## Tools

15 built-in tools, 2 agent tools, plus external tools via MCP:

| Tool | Description | Auto-approved |
|------|-------------|:---:|
| `bash` | Execute shell commands | |
| `read_file` | Read file contents (text or images with vision models) | yes |
| `write_file` | Write/create files | |
| `edit_file` | Fuzzy-match file editing | |
| `search` | Search files by name/content | yes |
| `math` | Sandboxed Python evaluation | |
| `man` | Read man pages | yes |
| `web_fetch` | Fetch URL content | |
| `web_search` | Web search (provider-native or Tavily) | |
| `memory` | Structured persistent memory (save/search/delete/list) | yes |
| `recall` | Search conversation history | yes |
| `notify` | Send notifications to linked channels | yes |
| `watch` | Periodic command polling with conditions | |
| `task` | Spawn autonomous sub-agent | |
| `plan` | Explore codebase, write .plan.md | |
| `mcp__*` | External tools from MCP servers | |

When the total tool count exceeds a configurable threshold (default 20), MCP tools are automatically deferred using native `defer_loading` on Anthropic and OpenAI APIs, or a transparent client-side BM25 search for local models. The LLM discovers deferred tools on demand via a `tool_search` capability — no configuration needed beyond `--tool-search auto` (the default).

### MCP Tool Servers

Turnstone supports the [Model Context Protocol](https://modelcontextprotocol.io/) (MCP) for connecting external tool servers. MCP tools are discovered at startup, converted to OpenAI function-calling format, and merged with built-in tools. Each MCP tool is prefixed with `mcp__{server}__{tool}` to avoid name collisions. Tool lists stay fresh via push notifications (`tools.listChanged`), periodic polling for servers without push, and manual `/mcp refresh`.

Configure via `config.toml` or `--mcp-config`:

```toml
[mcp.servers.github]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]

[mcp.servers.github.env]
GITHUB_TOKEN = "ghp_..."
```

Or use a standard MCP JSON config file:

```bash
turnstone --mcp-config ~/.config/turnstone/mcp.json
turnstone-server --mcp-config ~/.config/turnstone/mcp.json
```

Use `/mcp` in the REPL to list connected tools, `/mcp refresh` to re-fetch tool lists from servers. MCP tools require user approval by default (overridden by `--skip-permissions` or UI auto-approve).

### Multi-Model and Multi-Provider Support

Turnstone supports multiple model backends per server instance, including different LLM providers. `ChatSession` delegates all API communication to pluggable `LLMProvider` adapters — the internal message format stays OpenAI-like, and each provider translates at the API boundary. Define named models in `config.toml` and select per-workstream or switch mid-session with `/model <alias>`.

```toml
[models.local]
base_url = "http://localhost:8000/v1"
model = "qwen3-32b"
# provider defaults to "openai" (works with vLLM, llama.cpp, etc.)

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
default = "local"              # which model to use by default
fallback = ["claude", "openai"]  # try these if the primary is unreachable
agent_model = "claude"         # optional: separate model for plan/task sub-agents
```

Supported providers: `"openai"` (default -- OpenAI, vLLM, llama.cpp, any OpenAI-compatible API) and `"anthropic"` (Anthropic Messages API, requires `pip install turnstone[anthropic]`).

Use `/model` to show available models, `/model claude` to switch. Workstreams created via the API accept an optional `model` parameter.

## Configuration

All entry points read `~/.config/turnstone/config.toml`. CLI flags override config values.

```toml
[api]
base_url = "http://localhost:8000/v1"
api_key = ""
tavily_key = ""        # only needed for local/vLLM models without native search

[model]
name = ""              # empty = auto-detect
temperature = 0.5
reasoning_effort = "medium"
default = "default"    # model alias for new workstreams
fallback = []          # ordered list of fallback model aliases
agent_model = ""       # model alias for plan/task sub-agents

[tools]
timeout = 30
skip_permissions = false
search = "auto"            # "auto" (enable when >threshold tools), "on", "off"
search_threshold = 20      # min tools before tool search activates
search_max_results = 5     # max tools returned per search query

[server]
host = "0.0.0.0"
port = 8080
max_workstreams = 10       # auto-evicts oldest idle when full

[redis]
host = "localhost"
port = 6379
password = ""

[bridge]
server_url = "http://localhost:8080"
node_id = ""           # empty = hostname_xxxx

[console]
host = "0.0.0.0"
port = 8090
url = "http://localhost:8090"  # used by CLI /cluster commands
poll_interval = 10

[health]
backend_probe_interval = 30
backend_probe_timeout = 5
circuit_breaker_threshold = 5
circuit_breaker_cooldown = 60

[ratelimit]
enabled = true
requests_per_second = 10.0
burst = 20

[database]
backend = "sqlite"     # "sqlite" (default) or "postgresql"
path = ".turnstone.db" # SQLite file path (relative to working directory)
# url = "postgresql+psycopg://user:pass@host:5432/turnstone"  # PostgreSQL
# pool_size = 5        # PostgreSQL connection pool size

[judge]
enabled = true         # intent validation for tool approvals (--no-judge to disable)
model = ""             # empty = same as session model (self-consistency)
provider = ""          # empty = same as session provider
timeout = 60.0         # LLM judge timeout in seconds
confidence_threshold = 0.7

[mcp]
config_path = ""       # path to MCP JSON config file (alternative to TOML sections)
refresh_interval = 14400  # periodic refresh for servers without push notifications (seconds, 0 to disable)

[mcp.servers.example]  # one section per MCP server
command = "npx"
args = ["-y", "@modelcontextprotocol/server-example"]
# type = "stdio"       # "stdio" (default) or "http"
# url = ""             # for HTTP transport
```

Precedence: CLI args > environment variables > config.toml > defaults.

## Workstreams

Parallel independent conversations, each with its own session and state:

| Symbol | State | Meaning |
|--------|-------|---------|
| `·` | idle | Waiting for input |
| `◌` | thinking | Model is generating |
| `▸` | running | Tool execution in progress |
| `◆` | attention | Waiting for approval |
| `✖` | error | Something went wrong |

Idle workstreams are automatically cleaned up after 2 hours (configurable). In multi-node deployments, workstream ownership is tracked in Redis — follow-up messages auto-route to the owning node.

## Monitoring

`/metrics` endpoint exposes Prometheus-format metrics:

- `turnstone_tokens_total{direction}` — prompt/completion token counters
- `turnstone_tool_calls_total{tool}` — per-tool invocation counts
- `turnstone_workstream_context_ratio{ws_id}` — per-workstream context utilization
- `turnstone_http_request_duration_seconds` — request latency histogram
- `turnstone_workstreams_by_state{state}` — workstream state gauges
- `turnstone_sse_connections_active` — current open SSE connections
- `turnstone_ratelimit_rejected_total` — requests rejected by rate limiter
- `turnstone_backend_up` — LLM backend reachability (0/1)
- `turnstone_circuit_state` — circuit breaker state (0=closed, 1=open, 2=half_open)
- `turnstone_workstreams_evicted_total` — workstreams auto-evicted at capacity
- `turnstone_judge_verdicts_total{tier,risk_level}` — intent validation verdicts by tier and risk
- `turnstone_judge_llm_latency_seconds` — LLM judge evaluation latency histogram
- `turnstone_judge_enabled` — whether the intent validation judge is active (0/1)

Per-workstream metrics are labeled by `ws_id` (bounded to 10 max workstreams).

### Health & Rate Limiting

**Health degradation.** A background `BackendHealthMonitor` probes the LLM backend every `backend_probe_interval` seconds. When the backend is unreachable, `/health` reports `"status": "degraded"` (HTTP 200) and the `turnstone_backend_up` gauge drops to 0.

**Circuit breaker.** After `circuit_breaker_threshold` consecutive probe failures the circuit opens (CLOSED -> OPEN). While open, `ChatSession._create_stream_with_retry` skips the backend entirely and returns an error. After `circuit_breaker_cooldown` seconds the circuit enters HALF_OPEN, allowing a single probe. A successful probe closes the circuit; a failure re-opens it.

**Per-IP rate limiting.** When `[ratelimit].enabled` is true, each client IP is tracked with a token-bucket limiter (`requests_per_second` / `burst`). Rate limiting is applied in `do_GET`/`do_POST` after authentication but before route dispatch. `/health` and `/metrics` are exempt. Requests that exceed the limit receive HTTP 429 with a `Retry-After` header.

**Workstream eviction.** When `WorkstreamManager.create()` would exceed `max_workstreams`, the oldest IDLE workstream is automatically evicted and the `turnstone_workstreams_evicted_total` counter is incremented. Configure via `[server].max_workstreams` (default 10).

## Requirements

- Python 3.11+
- An OpenAI-compatible API endpoint ([vLLM](https://github.com/vllm-project/vllm), [NVIDIA NIM](https://build.nvidia.com/), [llama.cpp](https://github.com/ggml-org/llama.cpp), etc.) or an Anthropic API key
- Redis (for message queue bridge — `pip install turnstone[mq]`)
- Anthropic provider (optional — `pip install turnstone[anthropic]`)
- PostgreSQL (optional, for production — `pip install turnstone[postgres]`)
- [Git LFS](https://git-lfs.com/) (for cloning — diagram PNGs are stored in LFS)

## License

[Business Source License 1.1](LICENSE) — free for all use except hosting as a managed service. Converts to Apache 2.0 on 2030-03-01.
