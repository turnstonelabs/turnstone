# Docker Deployment

Docker Compose stack for running the full turnstone platform.

## Quick Start

```bash
# Copy and edit environment config
cp .env.example .env

# Full stack (needs an LLM API on the host)
docker compose up
```

Console dashboard: http://localhost:8090

> See also: [Deployment diagram](diagrams/png/12-deployment.png)

## Services

| Service | Port | Profile | Description |
|---------|------|---------|-------------|
| `server` | 8080 | default | Web UI + chat workstreams + LLM |
| `console` | 8090 | default | Cluster dashboard |
| `channel` | — | production | Channel gateway (Discord, Slack, etc.) |
| `server-1`…`server-10` | — | cluster | 10-node server fleet (PostgreSQL required) |

## Profiles

**Default** (no flag) — starts `server` and `console`. Requires an OpenAI-compatible LLM API running on the host (default: `http://localhost:8000/v1`).

```bash
docker compose up
```

**Production** — adds PostgreSQL and the channel gateway. Requires `POSTGRES_PASSWORD` and (for Discord) `TURNSTONE_DISCORD_TOKEN`:

```bash
docker compose --profile production up
```

**Cluster** — 10-node server fleet sharing PostgreSQL. Access all nodes via the console at `:8090`. Requires `POSTGRES_PASSWORD`:

```bash
docker compose --profile cluster up
```

## Configuration

All configuration is via environment variables in `.env` (copy from `.env.example`):

### LLM Backend

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BASE_URL` | `http://host.docker.internal:8000/v1` | OpenAI-compatible API URL |
| `OPENAI_API_KEY` | `dummy` | API key (`dummy` for local servers) |
| `TAVILY_API_KEY` | — | Web search API key (only needed for local/vLLM models; Anthropic and OpenAI search models use native search) |

### Server

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVER_PORT` | `8080` | Host port mapping |
| `SKIP_PERMISSIONS` | — | Set to any value to auto-approve all tools |

### Console

| Variable | Default | Description |
|----------|---------|-------------|
| `CONSOLE_PORT` | `8090` | Host port mapping |

### Auth

Auth is always enabled. `TURNSTONE_JWT_SECRET` is required.

| Variable | Default | Description |
|----------|---------|-------------|
| `TURNSTONE_JWT_SECRET` | — | Secret key for signing JWTs (required) |

### Database

| Variable | Default | Description |
|----------|---------|-------------|
| `TURNSTONE_DB_BACKEND` | `sqlite` | Storage backend: `sqlite` or `postgresql` |
| `TURNSTONE_DB_URL` | — | Database URL (e.g. `postgresql://user:pass@db:5432/turnstone`). For SQLite, defaults to `/data/.turnstone.db` |
| `TURNSTONE_DB_POOL_SIZE` | `2` | PostgreSQL connection pool size per process (default: 2 base + 3 overflow = 5 max) |

The database stores workstream history, user accounts, and API tokens. When using JWT auth, a database backend is required for user storage.

> **Large clusters:** Each turnstone process maintains a small connection pool (5 max). At hundreds of nodes this adds up — use [PgBouncer](pgbouncer.md) in transaction pooling mode between turnstone and PostgreSQL.

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

The channel service runs in the `production` profile. When `TURNSTONE_DISCORD_TOKEN` is set, the Discord adapter connects to the Discord Gateway and routes messages to the server via HTTP. See [Channel Integrations](channels.md) for full setup instructions including Discord application creation and user account linking.

## Scaling

For multi-node testing, use the `cluster` profile which provides 10 server instances with unique node IDs (`node-1` through `node-10`), resource limits, and shared PostgreSQL:

```bash
POSTGRES_PASSWORD=secret docker compose --profile cluster up
```

The default `server` also runs alongside the cluster nodes (11 total). All nodes are accessible via the console dashboard at `:8090`.

For production clusters beyond ~50 nodes, add PgBouncer between turnstone services and PostgreSQL. See [PgBouncer Connection Pooling](pgbouncer.md) for Docker Compose and Helm configuration.

## Volumes

| Volume | Mount | Purpose |
|--------|-------|---------|
| `turnstone-data` | `/data` | SQLite database (`.turnstone.db`) |

## Building

The image uses a multi-stage Dockerfile:

```bash
# Build all services
docker compose build

# Rebuild without cache
docker compose build --no-cache
```

All entry points are installed in a single image: `turnstone-server`, `turnstone-console`, `turnstone-channel`, `turnstone-admin`, `turnstone-eval`.

## Cleanup

```bash
# Stop and remove containers
docker compose down

# Stop, remove containers and volumes
docker compose down -v
```
