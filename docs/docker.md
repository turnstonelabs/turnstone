# Docker Deployment

Docker Compose stack for running the full turnstone platform or the simulator.

## Quick Start

```bash
# Copy and edit environment config
cp .env.example .env

# Full stack (needs an LLM API on the host)
docker compose up

# Simulator only (no LLM needed)
docker compose --profile sim up redis console sim
```

Console dashboard: http://localhost:8090

> See also: [Deployment diagram](diagrams/png/12-deployment.png)

## Services

| Service | Port | Profile | Description |
|---------|------|---------|-------------|
| `redis` | 6379 | default | Message broker, pub/sub, node registry |
| `server` | 8080 | default | Web UI + chat workstreams + LLM |
| `bridge` | — | default | Redis-to-HTTP bridge (multi-node routing) |
| `console` | 8090 | default | Cluster dashboard |
| `sim` | — | sim | Multi-node cluster simulator |

## Profiles

**Default** (no flag) — starts `redis`, `server`, `bridge`, `console`. Requires an OpenAI-compatible LLM API running on the host (default: `http://localhost:8000/v1`).

```bash
docker compose up
```

**Sim** — adds the simulator. Can run alongside the full stack or standalone with just Redis and the console:

```bash
# Sim + console (no LLM needed)
docker compose --profile sim up redis console sim

# Everything including sim
docker compose --profile sim up
```

## Configuration

All configuration is via environment variables in `.env` (copy from `.env.example`):

### LLM Backend

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BASE_URL` | `http://host.docker.internal:8000/v1` | OpenAI-compatible API URL |
| `OPENAI_API_KEY` | `dummy` | API key (`dummy` for local servers) |
| `TAVILY_API_KEY` | — | Web search API key (optional) |

### Redis

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_PASSWORD` | — | Redis auth password (empty = no auth) |
| `REDIS_PORT` | `6379` | Host port mapping |

### Server

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVER_PORT` | `8080` | Host port mapping |
| `SKIP_PERMISSIONS` | — | Set to any value to auto-approve all tools |

### Console

| Variable | Default | Description |
|----------|---------|-------------|
| `CONSOLE_PORT` | `8090` | Host port mapping |
| `CONSOLE_POLL_INTERVAL` | `10` | Node polling interval (seconds) |

### Auth

| Variable | Default | Description |
|----------|---------|-------------|
| `TURNSTONE_AUTH_ENABLED` | — | Set to `1` to require Bearer token auth |
| `TURNSTONE_AUTH_TOKEN` | — | Shared auth token for server/bridge/console |

### Simulator

| Variable | Default | Description |
|----------|---------|-------------|
| `SIM_NODES` | `100` | Number of simulated nodes |
| `SIM_SCENARIO` | `steady` | Scenario: `steady`, `burst`, `node_failure`, `directed`, `lifecycle` |
| `SIM_DURATION` | `60` | Duration in seconds |
| `SIM_MPS` | `5.0` | Messages per second (steady scenario) |
| `SIM_LOG_LEVEL` | `INFO` | Log verbosity |
| `SIM_SEED` | — | Random seed for reproducibility |
| `SIM_METRICS_FILE` | — | Write JSON report to file |

## Scaling

Scale to multiple server/bridge pairs:

```bash
docker compose up --scale server=3 --scale bridge=3
```

Each bridge auto-generates a unique node ID from its container hostname. When scaling `server`, remove the host port mapping (or use a reverse proxy) to avoid port conflicts.

## Volumes

| Volume | Mount | Purpose |
|--------|-------|---------|
| `redis-data` | `/data` | Redis persistence |
| `turnstone-data` | `/data` | SQLite database (`.turnstone.db`) |

## Building

The image uses a multi-stage Dockerfile:

```bash
# Build all services
docker compose build

# Rebuild without cache
docker compose build --no-cache
```

All five entry points are installed in a single image: `turnstone-server`, `turnstone-bridge`, `turnstone-console`, `turnstone-sim`, `turnstone-eval`.

## Cleanup

```bash
# Stop and remove containers
docker compose down

# Stop, remove containers and volumes
docker compose down -v
```
