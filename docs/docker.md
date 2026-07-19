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

PostgreSQL, the console's ACME endpoint (`:8090`), and SearxNG (`:8081`) are
published on `127.0.0.1`, so a `turnstone-server` running directly on the same
machine — for example to use a local GPU — can join the same cluster (enrolling
its mTLS cert and running `web_search`) and show up in the console alongside the
containerized nodes.

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
TURNSTONE_NODE_ID=host-1 \
  TURNSTONE_ADVERTISE_URL=http://host.docker.internal:8080 \
  TURNSTONE_CONSOLE_URL=http://localhost:8090 \
  TURNSTONE_SEARXNG_URL=http://localhost:8081 \
  turnstone-server --host 0.0.0.0 --port 8080
```

The host server registers itself in PostgreSQL; the console reaches it back via
`host.docker.internal`. `TURNSTONE_CONSOLE_URL` points the node at the console's
published ACME endpoint so it can enroll its mTLS certificate (needed only when
the cluster runs mTLS; harmless otherwise), and `TURNSTONE_SEARXNG_URL` points
`web_search` at the published SearxNG. The `jwt_secret` and DB credentials above
are the dev-stack defaults — match whatever you set in `.env` if you changed them.

To let a server on a **different** machine join, start the stack with
`TURNSTONE_HOST_IP=<this host's LAN IP>` — that binds PostgreSQL, the console
ACME endpoint, and SearxNG to that interface. Then on the remote box set the
three URLs above to that IP, and set `TURNSTONE_ADVERTISE_URL` to the **remote**
box's own IP (the address the console dials back). **Set a strong
`POSTGRES_PASSWORD` first** — `TURNSTONE_HOST_IP` exposes the database (and every
user account + API-token hash in it), the console API, and the unauthenticated
SearxNG to your network.

To run the bare-metal node as a hardened, persistent service instead of by hand,
use the systemd units in [`deploy/systemd/`](../deploy/systemd/).

## Production stack

For a real deployment use the bundled stack, which pulls released images
instead of building:

```bash
docker compose -f turnstone/deploy/compose.yaml up
```

It's the same shape as the dev stack — Caddy-fronted console, channel, and a
PostgreSQL all share one database so the console discovers the node — but it
pulls released images, runs a single server node, and has **no baked-in
secrets**. Set these in `.env` first (generate with `openssl rand -hex 32`):

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
| `TURNSTONE_SEARXNG_URL` | `http://searxng:8080` | SearxNG URL for the `web_search` tool (local/vLLM models only; Anthropic/OpenAI use native search). Defaults to the bundled `searxng` service; set to an external instance's URL. To turn web search off, clear `tools.searxng_url` in the admin Settings tab. |
| `SEARXNG_IMAGE_TAG` | `latest` | Tag for the bundled `searxng/searxng` image |
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

Both stacks publish Caddy (dashboard) and PostgreSQL; the dev stack additionally
publishes the console's ACME endpoint and SearxNG on localhost so a bare-metal
node can enroll its cert and run `web_search`. Everything else is reached through
Caddy or proxied by the console:

| Variable | Default | Description |
|----------|---------|-------------|
| `CONSOLE_HTTPS_PORT` | `8443` | Host port for Caddy (dashboard HTTPS) |
| `SEARXNG_HTTPS_PORT` | `8444` | Host port for the SearxNG UI via Caddy (dev: localhost-only; prod: opt-in) |
| `POSTGRES_PORT` | `5432` | Host port for PostgreSQL (for bare-metal joins) |
| `SEARXNG_API_PORT` | `8081` | Host port for the SearxNG API a bare-metal node's `web_search` dials (dev stack) |
| `TURNSTONE_HOST_IP` | `127.0.0.1` | Interface PostgreSQL, the console ACME endpoint, and SearxNG bind on (dev stack). Set to this host's LAN IP so a bare-metal node on **another machine** can reach them — set a strong `POSTGRES_PASSWORD` first (it also exposes the DB and the unauthenticated SearxNG to your network). |
| `POSTGRES_BIND` | `127.0.0.1` | Production stack (`turnstone/deploy/compose.yaml`) only: interface PostgreSQL binds on; set to the host's LAN IP for remote joins. |

### Channel gateway

| Variable | Default | Description |
|----------|---------|-------------|
| `TURNSTONE_DISCORD_TOKEN` | — | Discord bot token (enables the Discord adapter) |
| `TURNSTONE_DISCORD_GUILD` | `0` | Restrict to one guild (0 = all) |
| `TURNSTONE_SLACK_TOKEN` | — | Slack Bot User OAuth token `xoxb-…` |
| `TURNSTONE_SLACK_APP_TOKEN` | — | Slack App-Level token `xapp-…` (with the Slack token) |

The channel runs HTTP-only with no adapters until a token is set, so it's safe
to leave running. See [Channel Integrations](channels.md) for app setup.

### Web search (SearxNG)

The `web_search` tool for local/vLLM models is backed by a self-hosted
[SearxNG](https://searxng.org) metasearch service, bundled into both stacks as the
`searxng` service. The Turnstone nodes reach it over the internal docker network at
`http://searxng:8080` — its API port is **not** published. Its config —
[`turnstone/deploy/searxng/settings.yml`](../turnstone/deploy/searxng/settings.yml),
mounted read-only — enables the JSON API and leaves the rate limiter off (the
limiter would need a separate Valkey/Redis instance). A `searxng-cache` volume
persists its favicon + internal cache across restarts. Commercial providers
(Anthropic, OpenAI) use their own native search and never touch this service.

Point at an existing SearxNG instead of the bundled one with `TURNSTONE_SEARXNG_URL`,
or narrow the engines via `tools.searxng_engines` in the admin Settings tab (e.g.
`duckduckgo,wikipedia`).

**SearxNG web UI.** Caddy can also serve SearxNG's own search/Preferences UI on a
dedicated port. The dev stack publishes it at **`https://localhost:8444`** bound to
localhost only; the production stack does **not** publish it by default (uncomment
the `8444` port on the `caddy` service to opt in). Change the port with
`SEARXNG_HTTPS_PORT`. **SearxNG has no authentication** — never bind this to a public
interface, or anyone who can reach it can search through your instance.

> **AGPL note for operators.** SearxNG is licensed AGPL-3.0. Kept on the internal
> network (or bound to localhost), no external user interacts with it — so the AGPL
> §13 (remote network interaction) source-offer obligation does not attach. If you
> publish SearxNG to remote users (bind its port to a public interface, or front it
> with your own reverse proxy) you become the operator of a network-reachable AGPL
> service and must offer its corresponding source; that is trivially satisfied by
> linking to upstream <https://github.com/searxng/searxng>. Turnstone's own license is
> unaffected: it talks to SearxNG over HTTP as a separate process (mere aggregation),
> not by linking.

### Other

| Variable | Default | Description |
|----------|---------|-------------|
| `WORKSPACE_MOUNT` | empty volume | Host directory bind-mounted at `/workspace` for the model to read/write |
| `TURNSTONE_WORKSPACE` | `/workspace` (image env) | Directory named as the user's workspace in the model's tool descriptions; informational only — see [Working directory](#working-directory) |
| `SKIP_PERMISSIONS` | — | Set to any value to auto-approve all tool calls (dev only) |
| `MCP_CONFIG` | — | Path to an MCP server config file |
| `TURNSTONE_IMAGE_TAG` | `latest` | ghcr.io image tag — production stack |

## Building

Both stacks install all entry points into a single image (`turnstone`,
`turnstone-server`, `turnstone-console`, `turnstone-channel`, `turnstone-admin`,
`turnstone-eval`, `turnstone-optimizer`, `turnstone-doctor`):

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

## Working directory

Node processes run with `/data` as their working directory (the image's
`WORKDIR`), and that is where the model's shell commands execute and
relative file paths resolve — **not** `/workspace`. The shell and file
tool descriptions state both paths (the working directory, and the
workspace named by `TURNSTONE_WORKSPACE`), so the model knows to look in
`/workspace` for your files without being told each session.

To make tools start inside the mount instead, override the working
directory on the node services:

```yaml
services:
  turnstone-node:
    working_dir: /workspace
```

Two caveats before overriding:

- **SQLite fallback**: when a node runs without PostgreSQL, its fallback
  database `.turnstone.db` is created in the process working directory.
  Changing `working_dir` on an existing SQLite-fallback deployment makes
  the node create a fresh database inside the mount and your prior state
  appears lost (it is still in the `turnstone-data` volume under `/data`).
  The stock compose stacks use PostgreSQL and are unaffected.
- Migrations (`entrypoint.sh`) run in the same working directory, so the
  same SQLite caveat applies to them.

## Cleanup

```bash
docker compose down       # stop and remove containers
docker compose down -v    # also remove volumes (database, certs)
```
