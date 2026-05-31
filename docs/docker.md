# Docker Deployment

Turnstone ships two Docker Compose stacks:

| Stack | File | Use it for |
|-------|------|------------|
| **Dev cluster** | `compose.yaml` (repo root) | Clone-and-run. Builds locally, zero config, full 10-node cluster. |
| **Production** | `turnstone/deploy/compose.yaml` | Pip/pipx installs. Pulls released images from ghcr.io, requires real secrets. |

## Quick start — local cluster

```bash
git clone https://github.com/turnstonelabs/turnstone
cd turnstone
docker compose up
```

That builds one image and brings up the whole stack: PostgreSQL, the console,
Caddy, the channel gateway, and **10 server nodes** (`node-1`…`node-10`). No
`.env` is required — it ships with insecure dev defaults so it just works.

Open the dashboard at **https://localhost:8443**. It's served by Caddy with its
own local CA, so trust the root certificate once (or click through the browser
warning):

```bash
docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt
```

Create your first admin user (any node works — they share one database):

```bash
docker compose exec node-1 turnstone-admin create-user --username admin --name "Admin"
```

### Bring your own LLM

Nodes boot **without** an LLM and appear in the console immediately. Add real
model backends (OpenAI, Anthropic, or a local/vLLM endpoint) from the console
UI's **Models** tab. To set a node's bootstrap default instead, point
`LLM_BASE_URL` / `OPENAI_API_KEY` at an OpenAI-compatible endpoint in `.env`.

### Fewer nodes

Ten nodes is heavy on a laptop. Start a subset by naming the services (always
include `postgres`, `console`, and `caddy`):

```bash
docker compose up postgres console caddy channel node-1 node-2 node-3
```

## Why HTTPS-only?

The console's plain-HTTP port (8090) is **not** published to the host. A plain
HTTP/1.1 origin caps the browser at 6 connections, which starves the
dashboard's per-pane SSE streams. Caddy serves the browser over HTTP/2
(multiplexed) and proxies to `console:8090` on the internal network, so the cap
is gone. Everything goes through `https://localhost:8443`.

## Join a bare-metal host

PostgreSQL is published on `127.0.0.1:5432`, so a `turnstone-server` running
directly on the same machine — for example to use a local GPU — can join the
same cluster and show up in the console alongside the containerized nodes.

Put the secret and connection settings in `~/.config/turnstone/config.toml`
(secrets belong in this file, not the process environment — keep it `0600`,
the loader warns otherwise):

```toml
[auth]
jwt_secret = "dev-only-insecure-jwt-secret-change-me-for-real-deployments"

[database]
backend = "postgresql"
url = "postgresql+psycopg://turnstone:turnstone@localhost:5432/turnstone"

[api]
base_url = "http://localhost:8000/v1"   # your local model endpoint
api_key = "dummy"
```

Then start the server. The node identity isn't a secret, so it stays on the
command line:

```bash
chmod 600 ~/.config/turnstone/config.toml
TURNSTONE_NODE_ID=host-1 TURNSTONE_ADVERTISE_URL=http://host.docker.internal:8080 \
  turnstone-server --host 0.0.0.0 --port 8080
```

The host server registers itself in PostgreSQL; the console reaches it back via
`host.docker.internal`. The `jwt_secret` and DB credentials above are the
dev-stack defaults — match whatever you set in `.env` if you changed them. To
let a **different** machine join, start the stack with `POSTGRES_BIND=0.0.0.0`
and use the host's routable IP in the `url` and `TURNSTONE_ADVERTISE_URL` —
but **set a strong `POSTGRES_PASSWORD` first**, or you'll expose a database with
the insecure default password (and every user account + API-token hash in it) to
your network.

## Production stack

For a real deployment use the bundled stack, which pulls released images
instead of building:

```bash
docker compose -f turnstone/deploy/compose.yaml up
```

It's the same shape as the dev stack — Caddy-fronted console, channel, and a
PostgreSQL all share one database so the console discovers the node — but it
pulls released images, runs a single server node, and has **no baked-in
secrets**. Set these in `.env` first (`turnstone-bootstrap` generates them):

```bash
TURNSTONE_JWT_SECRET=<python -c "import secrets; print(secrets.token_hex(32))">
POSTGRES_PASSWORD=<a strong password>
```

The dashboard is at **https://localhost:8443** (Caddy, same as the dev stack);
the console's HTTP port isn't published. For a real domain and a publicly
trusted cert, edit `turnstone/deploy/Caddyfile` to point Caddy at Let's Encrypt
(see [tls.md](tls.md)). Pin the image with `TURNSTONE_IMAGE_TAG` (default:
`latest`).

### mTLS

Layer the TLS overlay on the production stack to enable mutual TLS between
services. A bootstrap container creates a CA and every service auto-provisions
certs via the console's ACME endpoint:

```bash
docker compose -f turnstone/deploy/compose.yaml -f deploy/docker-compose.tls.yml up
```

See [tls.md](tls.md) for details.

## Configuration

Everything is configured with environment variables in `.env` (copy from
[`.env.example`](../.env.example)). The dev stack needs none of them — they're
overrides.

### LLM backend

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BASE_URL` | `http://host.docker.internal:8000/v1` | Bootstrap OpenAI-compatible API URL (real backends go in the UI) |
| `OPENAI_API_KEY` | `dummy` | API key (`dummy` for local servers) |
| `TAVILY_API_KEY` | — | Web-search fallback (only for local/vLLM models; Anthropic/OpenAI use native search) |
| `MODEL` | — | Override the default model alias |

### Auth & database

| Variable | Default (dev / prod) | Description |
|----------|----------------------|-------------|
| `TURNSTONE_JWT_SECRET` | insecure default / **required** | JWT signing secret. Every service must share one value. |
| `TURNSTONE_DB_BACKEND` | `postgresql` | `sqlite` or `postgresql`. Multi-node discovery requires `postgresql`. |
| `TURNSTONE_DB_URL` | bundled Postgres | SQLAlchemy URL. Override to use an external database. |
| `POSTGRES_USER` | `turnstone` | PostgreSQL username |
| `POSTGRES_PASSWORD` | `turnstone` / **required** | PostgreSQL password |
| `POSTGRES_MAX_CONNECTIONS` | `300` | `max_connections` for the bundled Postgres |

> **Discovery needs a shared database.** Each server registers and heartbeats
> into a `services` table that the console polls. All services in these stacks
> point at the same PostgreSQL by default; SQLite-per-container can't see other
> containers.

> **Large clusters:** each process keeps a small pool (5 max). Beyond ~50 nodes,
> put [PgBouncer](pgbouncer.md) (transaction pooling) between turnstone and
> PostgreSQL.

### Ports

Both stacks publish the same two host ports (everything else is reached through
Caddy or proxied by the console):

| Variable | Default | Description |
|----------|---------|-------------|
| `CONSOLE_HTTPS_PORT` | `8443` | Host port for Caddy (dashboard HTTPS) |
| `POSTGRES_PORT` | `5432` | Host port for PostgreSQL (for bare-metal joins) |
| `POSTGRES_BIND` | `127.0.0.1` | Interface PostgreSQL binds on; set `0.0.0.0` for LAN access |

### Channel gateway

| Variable | Default | Description |
|----------|---------|-------------|
| `TURNSTONE_DISCORD_TOKEN` | — | Discord bot token (enables the Discord adapter) |
| `TURNSTONE_DISCORD_GUILD` | `0` | Restrict to one guild (0 = all) |
| `TURNSTONE_SLACK_TOKEN` | — | Slack Bot User OAuth token `xoxb-…` |
| `TURNSTONE_SLACK_APP_TOKEN` | — | Slack App-Level token `xapp-…` (with the Slack token) |

The channel runs HTTP-only with no adapters until a token is set, so it's safe
to leave running. See [Channel Integrations](channels.md) for app setup.

### Other

| Variable | Default | Description |
|----------|---------|-------------|
| `WORKSPACE_MOUNT` | empty volume | Host directory bind-mounted at `/workspace` for the model to read/write |
| `SKIP_PERMISSIONS` | — | Set to any value to auto-approve all tool calls (dev only) |
| `MCP_CONFIG` | — | Path to an MCP server config file |
| `TURNSTONE_IMAGE_TAG` | `latest` | ghcr.io image tag — production stack |

## Building

Both stacks install all entry points into a single image (`turnstone`,
`turnstone-server`, `turnstone-console`, `turnstone-channel`, `turnstone-admin`,
`turnstone-eval`, `turnstone-bootstrap`):

```bash
docker compose build            # build the dev image
docker compose build --no-cache # rebuild from scratch
```

## Volumes

| Volume | Purpose |
|--------|---------|
| `postgres-data` | PostgreSQL data directory |
| `turnstone-data` | `/data` per node (SQLite fallback, local state) |
| `workspace` | `/workspace` (unless `WORKSPACE_MOUNT` is set) |
| `caddy-data` / `caddy-config` | Caddy's local CA and config (dev stack) |

## Cleanup

```bash
docker compose down       # stop and remove containers
docker compose down -v    # also remove volumes (database, certs)
```
