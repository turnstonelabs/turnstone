# Turnstone

[![CI](https://github.com/turnstonelabs/turnstone/actions/workflows/ci.yml/badge.svg)](https://github.com/turnstonelabs/turnstone/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/turnstone)](https://pypi.org/project/turnstone/)
[![Python](https://img.shields.io/pypi/pyversions/turnstone)](https://pypi.org/project/turnstone/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![Discord](https://img.shields.io/badge/Discord-join%20us-5865F2?logo=discord&logoColor=white)](https://discord.gg/Nh3bWMacaq)
[![Sponsor](https://img.shields.io/badge/Sponsor-%E2%9D%A4-db61a2?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/eous)

Self-hosted, local-first orchestration for tool-using AI agents. Give LLMs real tools — shell, files, search, web — and run them across your own cluster with direct HTTP routing and interactive interfaces. Your code, your models, your data stay on hardware you control: no telemetry, no phone-home.

<p align="center">
  <img src="docs/assets/hero.png" alt="Turnstone coordinator — parallel tool batches with judge-graded approval and child workstream tracking" width="960"/>
</p>

Named after the [Ruddy Turnstone](https://en.wikipedia.org/wiki/Ruddy_turnstone) (*Arenaria interpres*) — a shorebird that flips stones to discover what's hiding underneath.

**What is a harness?**

```
ℋ :  s_{n+1} ~ T(s_n)   for n < τ*,    T = ρ ∘ (M_W ∘ π, E)
```

[**the primer →**](PRIMER.md)

### Release Tracks

| Track | Install | Docker | Description |
|-------|---------|--------|-------------|
| **Stable** | `pip install turnstone` | `ghcr.io/turnstonelabs/turnstone:stable` | Production-grade. Bugfixes only. |
| **Experimental** | `pip install turnstone --pre` | `ghcr.io/turnstonelabs/turnstone:experimental` | New features. May have rough edges. |

See [docs/releasing.md](docs/releasing.md) for the full release process.

## What it does

Turnstone gives LLMs tools — shell, files, search, web, planning — and orchestrates multi-turn conversations where the model investigates, acts, and reports.

- **Local-first & private** — runs entirely on hardware you control, with no telemetry and no phone-home. Point it at local models (vLLM, llama.cpp) or commercial APIs you hold the keys to — your prompts and data never transit a third party you didn't choose.
- **Bring your own models** — OpenAI-compatible APIs (vLLM, llama.cpp, NIM), the Anthropic Messages API, and Google Gemini, mixed freely per role
- **Interactive sessions** — terminal CLI or browser UI with parallel workstreams
- **Cluster dashboard** — real-time view of every node and workstream, with a rendezvous routing proxy
- **Intent validation** — an LLM judge (your model) grades every tool call with a risk assessment and evidence before it runs
- **MCP support** — external tool servers with native deferred loading (Anthropic/OpenAI) or BM25 fallback
- **Team controls when you need them** — optional RBAC, SSO, tool policies, and audit logs, all stored in your own database

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
turnstone-console --port 8090
```

For PostgreSQL (recommended for production):

```bash
export TURNSTONE_DB_BACKEND=postgresql
export TURNSTONE_DB_URL="postgresql+psycopg://user:pass@localhost:5432/turnstone"
turnstone-server --port 8080 --base-url http://localhost:8000/v1
```

### Docker

One-line install — autodetects Ubuntu/Debian, Fedora/RHEL, Arch, and WSL,
installs git + Docker if missing, generates secrets, and starts the stack:

```bash
curl -fsSL https://raw.githubusercontent.com/turnstonelabs/turnstone/main/run.sh | bash
```

Or, if you already have Docker, clone the repo and run it yourself:

```bash
docker compose up
```

That builds one image and brings up a full local cluster — PostgreSQL, console,
Caddy, channel gateway, and 10 server nodes — with no `.env` required (it ships
with insecure dev defaults). Open the dashboard at https://localhost:8443 (Caddy
serves it over TLS with its own local CA — trust it once). Nodes boot without an
LLM; add model backends from the console UI.

For production (released images from ghcr.io, real secrets required), use the
bundled stack: `docker compose -f turnstone/deploy/compose.yaml up`.

See [QUICKSTART.md](QUICKSTART.md) for the install + troubleshooting walkthrough and [docs/docker.md](docs/docker.md) for Docker configuration.

### Programmatic (SDK)

```python
from turnstone.sdk import TurnstoneServer

with TurnstoneServer("http://localhost:8080", token="tok_xxx") as client:
    ws = client.create_workstream(name="demo")
    result = client.send_and_wait("Analyze the error logs", ws.ws_id, auto_approve=True)
    print(result.content)
```

## Tools

Built-in tools for shell, files, search, web, memory, notifications, and autonomous sub-agents — plus external tools via [MCP](https://modelcontextprotocol.io/) with native deferred loading. See [docs/tools.md](docs/tools.md) for the full reference and [docs/mcp-registry.md](docs/mcp-registry.md) for MCP configuration.

## Architecture

**Single-node**: Client → Server (direct HTTP + SSE). No external dependencies beyond the database.

**Multi-node**: Client → Console (rendezvous routing proxy) → Server nodes. The console picks the target node for each workstream via rendezvous (HRW) hashing over the live service registry — pure function of `(ws_id, live_nodes)`, no stored bucket state, deterministic across readers. A node join or drop only re-routes the keys that score highest on the affected node.

| Component | Purpose |
|-----------|---------|
| `turnstone` | Terminal CLI (REPL) |
| `turnstone-server` | Web UI + REST API + SSE events |
| `turnstone-console` | Cluster dashboard + routing proxy + admin panel |
| `turnstone-channel` | Channel gateway (Discord and Slack adapters) |
| `turnstone-admin` | User/token management CLI |
| `turnstone-eval` | Headless measurement — scores tool-use against expected actions |
| `turnstone-optimizer` | Prompt/tool optimizer (UCB self-modify loop over the eval substrate) |
| `turnstone-doctor` | LLM-backed cluster diagnostics |

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
| [Channels](docs/diagrams/png/16-channel-architecture.png) | Discord / Slack adapters + routing |
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
| MCP integration | [docs/mcp-registry.md](docs/mcp-registry.md) |

## Requirements

- Python 3.11+
- An OpenAI-compatible API endpoint, Anthropic API key, or Google Gemini API key
- Optional: Discord / Slack channel integrations (`pip install turnstone[discord,slack]`)
- [Git LFS](https://git-lfs.com/) for cloning (diagram PNGs)

## Support

Turnstone is free, Apache-2.0, and self-hosted — no paid tier, no telemetry, no upsell. If it saves you time or you'd like to help keep development moving, you can sponsor the project:

**[❤ Sponsor Turnstone →](https://github.com/sponsors/eous)**  ·  one-off via **[PayPal](https://paypal.me/eousphoros)**

Sponsorship is entirely optional and funds maintenance, new features, and infrastructure. Prefer to contribute in other ways? Filing issues, improving docs, and [pull requests](CONTRIBUTING.md) help just as much.

## Community

Questions, ideas, or want to show what you're building? Join us on Discord:
**[discord.gg/Nh3bWMacaq](https://discord.gg/Nh3bWMacaq)**.

## License

[Apache License 2.0](LICENSE), as of version 1.6.0. Versions 1.5.x and earlier remain under the Business Source License 1.1 they shipped with.
