# Turnstone

[![CI](https://github.com/turnstonelabs/turnstone/actions/workflows/ci.yml/badge.svg)](https://github.com/turnstonelabs/turnstone/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/turnstone)](https://pypi.org/project/turnstone/)
[![Python](https://img.shields.io/pypi/pyversions/turnstone)](https://pypi.org/project/turnstone/)
[![License](https://img.shields.io/badge/license-BSL--1.1-blue)](LICENSE)

Multi-node AI orchestration platform. Deploy tool-using AI agents across a cluster of servers with direct HTTP routing, interactive interfaces, and enterprise governance.

> **Beta — Use at your own risk.** APIs, configuration formats, and database schemas may change between versions without migration paths.

Named after the [Ruddy Turnstone](https://en.wikipedia.org/wiki/Ruddy_turnstone) (*Arenaria interpres*) — a shorebird that flips stones to discover what's hiding underneath.

## What it does

Turnstone gives LLMs tools — shell, files, search, web, planning — and orchestrates multi-turn conversations where the model investigates, acts, and reports.

- **Interactive sessions** — terminal CLI or browser UI with parallel workstreams
- **Cluster dashboard** — real-time view of all nodes and workstreams with console routing proxy
- **Intent validation** — LLM judge evaluates every tool call with risk assessments and evidence
- **Governance** — RBAC, OIDC SSO, tool policies, skills, usage tracking, audit logs
- **Multi-provider** — OpenAI-compatible APIs (vLLM, llama.cpp, NIM) and Anthropic Messages API
- **MCP support** — external tool servers with native deferred loading (Anthropic/OpenAI) or BM25 fallback

<p align="center">
  <img src="docs/diagrams/architecture-overview.svg" alt="Turnstone system architecture" width="960"/>
</p>

## Quickstart

```bash
pip install turnstone

# Terminal REPL
turnstone --base-url http://localhost:8000/v1

# Browser UI
turnstone-server --port 8080 --base-url http://localhost:8000/v1

# Cluster dashboard
pip install turnstone[console]
turnstone-console --port 8090
```

### Docker

```bash
cp .env.example .env  # edit LLM_BASE_URL, OPENAI_API_KEY, etc.
docker compose --profile production up
```

See [QUICKSTART.md](QUICKSTART.md) for the bootstrap wizard and [docs/docker.md](docs/docker.md) for Docker configuration and profiles.

### Programmatic (SDK)

```python
from turnstone.sdk import TurnstoneServer

with TurnstoneServer("http://localhost:8080", token="tok_xxx") as client:
    ws = client.create_workstream(name="demo")
    result = client.send_and_wait("Analyze the error logs", ws.ws_id, auto_approve=True)
    print(result.content)
```

## Tools

Built-in tools for shell, files, search, web, memory, notifications, and autonomous sub-agents — plus external tools via [MCP](https://modelcontextprotocol.io/) with native deferred loading. See [docs/tools.md](docs/tools.md) for the full reference and [docs/mcp.md](docs/mcp.md) for MCP configuration.

## Architecture

**Single-node**: Client → Server (direct HTTP + SSE). No external dependencies beyond the database.

**Multi-node**: Client → Console (hash ring routing proxy) → Server nodes. The console maintains a 65536-entry bucket cache for O(1) workstream routing. A rebalancer daemon redistributes buckets when nodes join or leave.

| Component | Purpose |
|-----------|---------|
| `turnstone` | Terminal CLI (REPL) |
| `turnstone-server` | Web UI + REST API + SSE events |
| `turnstone-console` | Cluster dashboard + routing proxy + admin panel |
| `turnstone-channel` | Channel gateway (Discord, with adapters for Slack/Teams planned) |
| `turnstone-admin` | User/token management CLI |
| `turnstone-eval` | Eval harness for prompt/tool optimization |
| `turnstone-bootstrap` | LLM-guided setup wizard |

### Diagrams

UML diagrams in [`docs/diagrams/`](docs/diagrams/):

| Diagram | Description |
|---------|-------------|
| [System Context](docs/diagrams/png/01-system-context.png) | Components and external dependencies |
| [Package Structure](docs/diagrams/png/02-package-structure.png) | Python modules and dependency graph |
| [Core Engine](docs/diagrams/png/03-core-engine-classes.png) | SessionUI, ChatSession, LLMProvider |
| [Conversation Turn](docs/diagrams/png/04-conversation-turn.png) | Message lifecycle through the engine |
| [Tool Pipeline](docs/diagrams/png/05-tool-pipeline.png) | Prepare / approve / execute |
| [Workstream States](docs/diagrams/png/09-workstream-states.png) | State machine transitions |
| [Console Data Flow](docs/diagrams/png/11-console-data-flow.png) | Dashboard data collection |
| [Deployment](docs/diagrams/png/12-deployment.png) | Docker Compose topology |
| [Auth](docs/diagrams/png/15-auth-architecture.png) | JWT, scopes, login flows |
| [Channels](docs/diagrams/png/16-channel-architecture.png) | Discord adapter + routing |
| [Judge](docs/diagrams/png/22-judge-architecture.png) | Intent validation pipeline |
| [OIDC](docs/diagrams/png/25-oidc-architecture.png) | SSO authorization code flow |

## Documentation

| Topic | Link |
|-------|------|
| Configuration reference | [docs/settings.md](docs/settings.md) |
| API reference | [docs/api-reference.md](docs/api-reference.md) |
| Docker deployment | [docs/docker.md](docs/docker.md) |
| Intent validation (judge) | [docs/judge.md](docs/judge.md) |
| Governance & RBAC | [docs/governance.md](docs/governance.md) |
| OIDC SSO | [docs/oidc.md](docs/oidc.md) |
| TLS / mTLS | [docs/tls.md](docs/tls.md) |
| Channel integrations | [docs/channels.md](docs/channels.md) |
| Console dashboard | [docs/console.md](docs/console.md) |
| Eval harness | [docs/eval.md](docs/eval.md) |
| Tools reference | [docs/tools.md](docs/tools.md) |
| MCP integration | [docs/mcp.md](docs/mcp.md) |

## Requirements

- Python 3.11+
- An OpenAI-compatible API endpoint or Anthropic API key
- Optional: PostgreSQL (`pip install turnstone[postgres]`), Anthropic (`pip install turnstone[anthropic]`)
- [Git LFS](https://git-lfs.com/) for cloning (diagram PNGs)

## License

[Business Source License 1.1](LICENSE) — free for all use except hosting as a managed service. Converts to Apache 2.0 on 2030-03-01.
