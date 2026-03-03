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
- **Cluster dashboard** — real-time view of all nodes, workstreams, and resource utilization
- **Cluster simulator** — test the stack at scale (up to 1000 nodes) without an LLM backend

```
External System → Message Queue → Bridge (per node) → Turnstone Server → LLM + Tools
                                      ↓
                                 Pub/Sub → Progress Events → External System
                                      ↓
                                 turnstone-console → Cluster Dashboard (browser)
```

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

Then open `http://localhost:8090` for the cluster-wide dashboard.

### Docker

```bash
cp .env.example .env  # edit LLM_BASE_URL, OPENAI_API_KEY, etc.
docker compose up     # starts redis + server + bridge + console
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

All frontends connect to any OpenAI-compatible API (vLLM, NVIDIA NIM/NGC, llama.cpp, OpenAI, etc.) and auto-detect the model.

## Architecture

```
turnstone/
├── core/              # UI-agnostic engine
│   ├── session.py     # ChatSession — multi-turn loop, tool dispatch, agents
│   ├── tools.py       # Tool definitions (auto-loaded from JSON)
│   ├── workstream.py  # WorkstreamManager — parallel independent sessions
│   ├── config.py      # Unified TOML config (~/.config/turnstone/config.toml)
│   ├── memory.py      # SQLite persistence (memories, conversations, FTS5)
│   ├── metrics.py     # Prometheus-compatible metrics collector
│   ├── edit.py        # File editing (fuzzy match, indentation)
│   ├── safety.py      # Path validation, sandbox checks
│   ├── sandbox.py     # Command sandboxing
│   └── web.py         # Web fetch/search helpers
├── mq/                # Message queue integration
│   ├── protocol.py    # Typed message dataclasses (JSON serialization)
│   ├── broker.py      # Abstract MessageBroker + RedisBroker
│   ├── bridge.py      # Bridge service (queue ↔ HTTP API, multi-node routing)
│   └── client.py      # TurnstoneClient — Python API for external systems
├── console/           # Cluster dashboard
│   ├── collector.py   # ClusterCollector — aggregates all nodes via Redis + HTTP
│   ├── server.py      # Dashboard HTTP server + SSE
│   └── static/        # Cluster dashboard web UI
├── tools/             # Tool schemas (one JSON file per tool)
├── ui/                # Frontend assets and terminal rendering
│   └── static/        # Web UI (HTML, CSS, JS)
├── sim/               # Cluster simulator
│   ├── cluster.py     # SimCluster — orchestrates N nodes + dispatchers
│   ├── node.py        # SimNode + SimWorkstream — protocol-compatible node
│   ├── engine.py      # LLM + tool execution simulation
│   ├── scenario.py    # 5 workload scenarios (steady, burst, node_failure, …)
│   ├── metrics.py     # Latency, throughput, utilization collection
│   └── cli.py         # CLI entry point (turnstone-sim)
├── cli.py             # Terminal frontend (+ /cluster commands for console)
├── server.py          # Web frontend (HTTP + SSE)
└── eval.py            # Evaluation and prompt optimization harness
docs/
├── architecture.md    # System architecture and threading model
├── api-reference.md   # Web server API and SSE event reference
├── console.md         # Cluster dashboard service (turnstone-console)
├── docker.md          # Docker Compose deployment and configuration
├── simulator.md       # Cluster simulator usage and scenarios
├── tools.md           # Tool schemas, execution pipeline, approval flow
├── eval.md            # Evaluation harness internals
└── diagrams/          # UML architecture diagrams (PlantUML sources + PNGs)
    └── png/           # Pre-rendered diagram images
```

### Architecture Diagrams

Detailed UML diagrams are available in [`docs/diagrams/`](docs/diagrams/):

| Diagram | Description |
|---------|-------------|
| [System Context](docs/diagrams/png/01-system-context.png) | Top-level components and external dependencies |
| [Package Structure](docs/diagrams/png/02-package-structure.png) | Python modules and dependency graph |
| [Core Engine Classes](docs/diagrams/png/03-core-engine-classes.png) | SessionUI protocol, ChatSession, WorkstreamManager |
| [Conversation Turn](docs/diagrams/png/04-conversation-turn.png) | Full message lifecycle through the engine |
| [Tool Pipeline](docs/diagrams/png/05-tool-pipeline.png) | Three-phase prepare/approve/execute |
| [MQ Protocol](docs/diagrams/png/06-mq-protocol.png) | 9 inbound + 19 outbound message types |
| [Message Routing](docs/diagrams/png/07-message-routing.png) | Multi-node routing scenarios |
| [Redis Key Schema](docs/diagrams/png/08-redis-key-schema.png) | All Redis keys, types, and TTLs |
| [Workstream States](docs/diagrams/png/09-workstream-states.png) | State machine transitions |
| [Simulator](docs/diagrams/png/10-simulator-architecture.png) | SimCluster, dispatchers, scenarios |
| [Console Data Flow](docs/diagrams/png/11-console-data-flow.png) | Dashboard data collection threads |
| [Deployment](docs/diagrams/png/12-deployment.png) | Docker Compose service topology |

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

14 built-in tools, 2 agent tools:

| Tool | Description | Auto-approved |
|------|-------------|:---:|
| `bash` | Execute shell commands | |
| `read_file` | Read file contents | yes |
| `write_file` | Write/create files | |
| `edit_file` | Fuzzy-match file editing | |
| `search` | Search files by name/content | yes |
| `math` | Sandboxed Python evaluation | |
| `man` | Read man pages | yes |
| `web_fetch` | Fetch URL content | |
| `web_search` | Search via Tavily API | |
| `remember` | Save persistent facts | yes |
| `recall` | Search memories and history | yes |
| `forget` | Remove a memory | yes |
| `task` | Spawn autonomous sub-agent | |
| `plan` | Explore codebase, write .plan.md | |

## Configuration

All entry points read `~/.config/turnstone/config.toml`. CLI flags override config values.

```toml
[api]
base_url = "http://localhost:8000/v1"
api_key = ""
tavily_key = ""

[model]
name = ""              # empty = auto-detect
temperature = 0.5
reasoning_effort = "medium"

[tools]
timeout = 30
skip_permissions = false

[server]
host = "0.0.0.0"
port = 8080

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

Per-workstream metrics are labeled by `ws_id` (bounded to 10 max workstreams).

## Requirements

- Python 3.11+
- An OpenAI-compatible API endpoint ([vLLM](https://github.com/vllm-project/vllm), [NVIDIA NIM](https://build.nvidia.com/), [llama.cpp](https://github.com/ggml-org/llama.cpp), etc.)
- Redis (for message queue bridge — `pip install turnstone[mq]`)

## License

[Business Source License 1.1](LICENSE) — free for all use except hosting as a managed service. Converts to Apache 2.0 on 2030-03-01.
