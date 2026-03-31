# Bootstrap Wizard

Interactive, AI-guided setup for Turnstone deployments. Instead of manually
editing `.env` files and reading deployment docs, the wizard walks you through
every decision conversationally and generates all the config files for you.

## Quick Start

```bash
turnstone-bootstrap
```

That's it — no flags, no arguments. The wizard prompts for everything.

## How It Works

1. **Pick a model** — Choose OpenAI, Anthropic, or a local/vLLM endpoint to
   power the wizard. Local endpoints auto-detect available models.
2. **Answer questions** — The AI walks you through deployment mode, LLM
   provider, database, authentication, ports, and optional features.
3. **Review generated files** — Each file is previewed before writing. You
   confirm or reject every write.
4. **Start the stack** — The wizard prints the exact `docker compose` command
   and a `setup.sh` script to create your first admin user, roles, and policies.

## What Gets Generated

| File | Purpose |
|------|---------|
| `.env` | All environment variables for `compose.yaml` |
| `setup.sh` | Post-start script: creates admin user, roles, tool policies, prompt templates via the API |
| `docker-compose.override.yaml` | Only if customizations beyond env vars are needed |

## Requirements

- **Python 3.11+** with turnstone installed (`pip install turnstone`)
- **An LLM API key** — for the wizard itself (OpenAI, Anthropic, or a local
  model). This can differ from the LLM your deployment will use.
- **Docker & Docker Compose** — needed to run the stack. The wizard detects
  whether Docker is installed and gives platform-specific install instructions
  if it's missing. You can still generate config files without Docker.

## Deployment Modes

The wizard supports two deployment modes:

- **Single-node production** (`docker compose --profile production up`) —
  1 server + console + PostgreSQL. Good for most use cases.
- **Multi-node cluster** (`docker compose --profile cluster up`) —
  10-node server fleet + console + PostgreSQL. For high-throughput or
  HA deployments.

## Example Session

```
$ turnstone-bootstrap

 Turnstone Bootstrap Wizard  v0.5.4
 ────────────────────────────────────────────────

 Which provider for this wizard?
   [1] OpenAI
   [2] Anthropic
   [3] OpenAI-compatible (local/vLLM)

 > 3

 Base URL [http://localhost:8000/v1]:
 API key (press Enter for 'none'):

 Querying http://localhost:8000/v1 for available models...
 Found model: Qwen/Qwen3-32B

 Connected to Qwen/Qwen3-32B. Handing off to AI assistant...

> (AI walks you through the rest interactively)
```

## Tips

- **Re-run safely** — running the wizard again detects your existing `.env`
  and offers to update it rather than overwriting.
- **Duplicate writes are skipped** — if the LLM tries to write the same file
  twice with identical content, it's silently ignored.
- **Type `quit` to exit** at any time during the conversation.
- **Ctrl+C** is handled gracefully — press once to interrupt, twice to exit.

## See Also

- [Docker Deployment](docker.md) — manual compose setup and profiles
- [Security](security.md) — auth architecture and token types
- [Governance](governance.md) — roles, policies, and templates
