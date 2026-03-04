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
| `channel` | — | production | Channel gateway (Discord, Slack, etc.) |
| `sim` | — | sim | Multi-node cluster simulator |

## Profiles

**Default** (no flag) — starts `redis`, `server`, `bridge`, `console`. Requires an OpenAI-compatible LLM API running on the host (default: `http://localhost:8000/v1`).

```bash
docker compose up
```

**Production** — adds PostgreSQL and the channel gateway. Requires `POSTGRES_PASSWORD` and (for Discord) `TURNSTONE_DISCORD_TOKEN`:

```bash
docker compose --profile production up
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
| `TAVILY_API_KEY` | — | Web search API key (only needed for local/vLLM models; Anthropic and OpenAI search models use native search) |

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
| `TURNSTONE_AUTH_ENABLED` | — | Set to `1` to require authentication |
| `TURNSTONE_AUTH_TOKEN` | — | Config-file token for server/bridge/console (backward compat, works alongside JWT) |
| `TURNSTONE_JWT_SECRET` | — | Secret key for signing JWTs (required when using user identity / JWT auth) |

### Database

| Variable | Default | Description |
|----------|---------|-------------|
| `TURNSTONE_DB_BACKEND` | `sqlite` | Storage backend: `sqlite` or `postgresql` |
| `TURNSTONE_DB_URL` | — | Database URL (e.g. `postgresql://user:pass@db:5432/turnstone`). For SQLite, defaults to `/data/.turnstone.db` |

The database stores workstream history, user accounts, and API tokens. When using JWT auth, a database backend is required for user storage.

> **First-time setup:** After deploying with auth enabled, create an initial admin user by running `turnstone-admin create-user` inside the container:
>
> ```bash
> docker compose exec server turnstone-admin create-user --username admin --name "Admin"
> ```
>
> You will be prompted to set a password. Use it to log in via the UI or SDK, then create additional users through the admin API. Pass `--token --scopes read,write,approve` to also generate an initial API token.

### Channel Gateway

| Variable | Default | Description |
|----------|---------|-------------|
| `TURNSTONE_DISCORD_TOKEN` | — | Discord bot token (required to enable Discord adapter) |
| `TURNSTONE_DISCORD_GUILD` | `0` | Restrict to a single Discord guild (0 = all guilds) |

The channel service runs in the `production` profile. When `TURNSTONE_DISCORD_TOKEN` is set, the Discord adapter connects to the Discord Gateway and routes messages through Redis MQ to the bridge and server. See [Channel Integrations](channels.md) for full setup instructions including Discord application creation and user account linking.

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

All entry points are installed in a single image: `turnstone-server`, `turnstone-bridge`, `turnstone-console`, `turnstone-channel`, `turnstone-admin`, `turnstone-sim`, `turnstone-eval`.

## Cleanup

```bash
# Stop and remove containers
docker compose down

# Stop, remove containers and volumes
docker compose down -v
```
