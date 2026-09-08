# Docker Deployment

Turnstone ships two Docker Compose stacks:

| Stack | File | Use it for |
|-------|------|------------|
| **Dev cluster** | `compose.yaml` (repo root) | Clone-and-run. Builds locally, zero config, full 10-node cluster. |
| **Production** | `turnstone/deploy/compose.yaml` | Pip/pipx installs. Pulls released images from ghcr.io, requires real secrets. |

## Quick start — local cluster

```bash
git clone --branch main https://github.com/turnstonelabs/turnstone
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
docker compose exec node-1 turnstone-admin create-admin --username admin --name "Admin"
```

### Bring your own LLM

Nodes boot **without** an LLM and appear in the console immediately. Add
model backends (OpenAI, Anthropic, or a local/vLLM endpoint) from the console
UI's **Models** tab, or as `[models.*]` entries in a node's `config.toml`. A
definition that leaves `api_key` empty falls back to `OPENAI_API_KEY` from
`.env`; a local server needs no key at all.

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

[models.local]
base_url = "http://localhost:8000/v1"   # your local model endpoint
model = "qwen3-32b"
```

If the cluster uses OAuth, SSO, or model gateway authentication, also copy
the applicable `[security]` and `[oidc]` sections from its
[shared bootstrap config](#shared-bootstrap-config) into this host's file.
Keep the same encryption key and authentication settings as the console and
containerized nodes, while retaining this host's database/model settings.
The Compose overlay only mounts the file into the Compose services; joined
hosts need their own private copy, updated whenever those shared settings
change.

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

Keep `TURNSTONE_NODE_ID` stable across restarts when conversations require this
host. Without it, the server generates a new identity on each start. Give each
execution environment a unique ID: a host server and a container with different
files or devices should not share one. A returning identity may advertise a
new URL; the console resolves that URL from service registration.

Selecting **Specific node** in the launcher persists an execution requirement.
If that node is unavailable, the conversation waits for it. **Continue
elsewhere** creates a separate conversation from saved history on the chosen
node; local files and running tools are not transferred. Automatic placement
remains flexible. Existing conversations are left unbound during migration
because historical placement does not establish explicit intent; fork one to
a specific node to create a bound continuation.

Deploy the updated console and server code with migration 076 before relying
on node requirements. Older server versions do not enforce the new field.

The host server registers itself in PostgreSQL; the console reaches it back via
`host.docker.internal`. `TURNSTONE_CONSOLE_URL` points the node at the console's
published ACME endpoint so it can enroll its mTLS certificate (needed only when
the cluster runs mTLS; harmless otherwise), and `TURNSTONE_SEARXNG_URL` points
`web_search` at the published SearxNG. The `jwt_secret` and DB credentials above
are the dev-stack defaults — match whatever you set in `.env` if you changed them.

To let a server on a **different** machine join, start the stack with
both `TURNSTONE_HOST_IP=<this host's LAN IP>` and
`TURNSTONE_ACME_EXTERNAL_URL=http://<this host's LAN IP>:8090/acme`. The first
binds PostgreSQL, the console ACME endpoint, and SearxNG to that interface; the
second makes every URL in the ACME directory routable from the remote node (the
full value must include the `/acme` mount). Set the same
`TURNSTONE_ACME_EXTERNAL_URL` on the remote node so its authenticated ACME
client can pin that credential destination. Set `TURNSTONE_CONSOLE_URL` and
`TURNSTONE_SEARXNG_URL` to the compose host's IP, but set
`TURNSTONE_ADVERTISE_URL=http://192.0.2.10:8080` to the **remote** box's own
address. A resolvable DNS name works too. IPv6 literals must be bracketed in
URLs, for example `http://[2001:db8::10]:8080`; Turnstone enrolls literal
addresses as IP SANs rather than numeric DNS SANs.

Use a trusted LAN or VPN address and firewall `:8090` to enrolling nodes. ACME
signing routes require a dedicated short-lived service JWT, but direct bootstrap
is still plain HTTP/TOFU: a bearer token provides authentication, not transport
confidentiality or protection from an active on-path attacker. **Set a strong
`POSTGRES_PASSWORD` first** — `TURNSTONE_HOST_IP` also exposes the database (and
every user account + API-token hash in it), the console API, and the
unauthenticated SearxNG to your network.

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

The overlay publishes the console's plain-HTTP bootstrap/API port on
`TURNSTONE_CONSOLE_HTTP_BIND` (default `127.0.0.1`). For a cross-host node, set
that to a trusted LAN/VPN address, set `TURNSTONE_ACME_EXTERNAL_URL` to the same
address plus `/acme`, and firewall the port to enrolling nodes.

See [tls.md](tls.md) for details.

## Configuration

Container wiring and ports are configured in `.env` (copy from
[`.env.example`](../.env.example)). The dev stack needs none of them. Bootstrap
settings such as SSO client credentials and token encryption keys use the
shared TOML file below.

### Shared bootstrap config

Both stacks ship an optional `compose.config.yaml` overlay. It mounts one
`config.toml`, beside that stack's `compose.yaml`, read-only at
`/run/turnstone/config.toml` in the console and **every server node**. Each
consumer selects that file with `TURNSTONE_CONFIG`. The channel gateway and
supporting services do not receive it. The default stack needs no TOML file.
The bind mount refuses a missing source instead of creating a directory.

Use the same file for the authentication features your deployment needs:

| Feature | Shared config and next step |
|---|---|
| Local login, API-key models, static MCP credentials | The default install needs no shared authentication config. |
| Personal MCP accounts (`oauth_user`) | Set the encryption key and HTTPS `redirect_base`; follow [MCP authorization](mcp-oauth.md#remote-docker-setup). Local login is sufficient. |
| SSO login | Set the `[oidc]` issuer, client ID, client secret, and `redirect_base`; follow [OIDC setup](oidc.md). SSO alone does not capture refresh credentials. |
| SSO delegation to MCP/model backends | Add the encryption key, opt into `capture_user_credential`, and choose the IdP's `obo_grant_profile`; configure [MCP passthrough](mcp-oauth.md#auth_typeoauth_obo--single-credential-sign-in-passthrough) or [delegated models](oidc.md#model-gateway-credentials). |
| Application identity for model gateways (`entra_app`) | Use the encryption key and Entra OIDC registration with the `entra` grant profile; configure [model gateway credentials](oidc.md#model-gateway-credentials). User credential capture is unnecessary. |

Each feature also needs its provider registration and permissions. Configure
MCP servers and model definitions in their admin tabs; model audiences are
permitted through the runtime setting `model.auth_audience_allowlist`.

**Using `run.sh`:** accept the optional OAuth/SSO shared-config prompt. If no
`config.toml` exists, the installer asks for the dashboard's HTTPS origin and
generates one Fernet key, with commented SSO and delegation fields. A new file
keeps local password login available; SSO and credential capture remain off.
Create your local admin before filling in SSO credentials, then follow the
applicable guide above and restart the services after editing the file.

The installer saves the mount alongside the node count in
`compose.override.yaml`, so later plain `docker compose up -d` commands retain
both choices. Reruns preserve the file and key; a missing previously enabled
file requires restoring the backup. An override written by you is left
intact; use the manual overlay flow below to incorporate it.
This includes `compose.override.yml` and `docker-compose.override.*` files.
To disable the installer-managed mount, remove its generated
`compose.override.yaml`, rerun `run.sh` to reselect your node count, and
decline shared config; retain the TOML/key backup.

**Manual setup:** work in the directory containing your stack's `compose.yaml`.
For the dev stack, build the image with `docker compose build` and set:

```bash
turnstone_image=turnstone:local
```

For production, work in `turnstone/deploy/` (or the directory where you copied
the bundled deployment files), provide its required `.env`, and select the
same image tag as the stack:

```bash
turnstone_image="$(docker compose config --images | grep -m 1 '^ghcr.io/turnstonelabs/turnstone:')"
docker pull "$turnstone_image"
```

Use deployment files and an image from the same Turnstone release. The
generator below requires the `turnstone.deploy.bootstrap_config` module.
If an older pinned tag reports that the module is missing, upgrade the image
or prepare `config.toml` using the fields and key-generation command in
[`turnstone.example.toml`](../turnstone.example.toml), then follow the
existing-file ownership instructions below. Preserve any existing key.

This recipe uses GNU `mv` for its `-nT` options. On macOS, use `gmv` from
[GNU coreutils](https://formulae.brew.sh/formula/coreutils) for the move step;
the system `mv` does not support `-T`.

To create a **new** config, replace the HTTPS origin in this command with the
address users actually open. The helper writes privately as the image's
`turnstone` account, including when Docker maps container UIDs on the host:

```bash
bootstrap_dir="$(mktemp -d "$PWD/.turnstone-bootstrap.XXXXXX")"
mkdir -m 0777 -- "$bootstrap_dir/output"
docker run --rm --network none --user root --entrypoint python \
  -v "$bootstrap_dir/output:/bootstrap:rw,z" "$turnstone_image" \
  -m turnstone.deploy.bootstrap_config /bootstrap/config.toml \
  https://turnstone.example.com --owner turnstone
mv -nT -- "$bootstrap_dir/output/config.toml" ./config.toml
rm -rf -- "$bootstrap_dir"
```

The outer directory stays private (`0700`); only its writable inner directory
is exposed to the bootstrap container. Other host users cannot traverse the
outer directory, and the generated key file is `0600` throughout.

The final move uses GNU `mv`'s no-clobber mode and keeps an existing file. To
reuse an existing configuration, place it at `./config.toml` instead; keep its
encryption key.
[`turnstone.example.toml`](../turnstone.example.toml) lists the bootstrap fields.
The file must be readable by the image's `turnstone` user, with mode `0600`.
For an existing file with different ownership on ordinary or rootless
Docker, adjust it through the container:

```bash
docker run --rm --network none --user root --entrypoint sh \
  -v "$PWD/config.toml:/run/turnstone/config.toml:rw,z" "$turnstone_image" \
  -c 'chown turnstone:turnstone /run/turnstone/config.toml && chmod 600 /run/turnstone/config.toml'
```

With rootful `userns-remap`, an existing host-owned file can lie outside the
container's UID/GID mapping, so the container cannot change its owner. Use
host root to assign the service user's mapped UID/GID and mode `0600` instead;
derive those IDs from your daemon's mapping and the image's `turnstone`
account. See [Docker's bind-mount ownership requirements](https://docs.docker.com/engine/security/userns-remap/#user-namespace-known-limitations).

Enable the overlay when starting the stack:

```bash
docker compose -f compose.yaml -f compose.config.yaml up -d
```

Keep those `-f` arguments on subsequent commands. If you have an existing
`compose.override.yaml`, include it too: explicit `-f` arguments disable the
automatic override selection. Do not replace an override containing other
deployment choices.

Back up `config.toml` privately with the database. Its host owner may be a
mapped container UID, so host-side edits and backups may require `sudo`.
After editing only this file, **restart the console and all active server
nodes** using `docker compose restart` with those service names and the same
Compose files. A running container can retain the old file after an editor's
atomic save; restarting the container remounts it and reloads the settings.
When adding or changing Compose mounts or environment variables, recreate
the affected services with `up -d --force-recreate` instead; a restart does
not apply changes to the Compose configuration. Do not generate a new key
on each node or on each restart.

The key stays out of `.env`, runtime settings, source control, and image build
contexts. A read-only mount protects against writes, but a shell running as
the service user can still read it; file-backed secrets are not a sandbox.
Use the dashboard's HTTPS origin for `redirect_base`. Register the appropriate
callback with each provider: `/v1/api/auth/oidc/callback` for SSO login and
`/v1/api/mcp/oauth/callback` for per-user MCP authorization. Both belong on the
console's public origin, but they serve different registrations. See
[OIDC setup](oidc.md) and [MCP authorization](mcp-oauth.md) for the remaining
provider and consent steps.

### LLM backend

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | `dummy` | API key used by model definitions that leave `api_key` empty, and by `${OPENAI_API_KEY}` placeholders in a definition |
| `TURNSTONE_SEARXNG_URL` | `http://searxng:8080` | SearxNG URL for the `web_search` tool (local/vLLM models only; Anthropic/OpenAI use native search). Defaults to the bundled `searxng` service; set to an external instance's URL. To turn web search off, clear `tools.searxng_url` in the admin Settings tab. |
| `SEARXNG_IMAGE_TAG` | `latest` | Tag for the bundled `searxng/searxng` image |

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

> **Lifecycle upgrade:** the release that introduces hidden deferred-create
> reservations must be deployed as a coordinated cohort across every server
> sharing PostgreSQL; older processes do not understand `state='creating'`.
> Drain create traffic until the cohort is upgraded. See
> [PgBouncer: deferred workstream creation](pgbouncer.md#upgrade-note-deferred-workstream-creation).

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
| `TURNSTONE_CONSOLE_HTTP_BIND` | `127.0.0.1` | Production TLS-overlay interface for the console's plain-HTTP bootstrap/API listener. Use only a trusted LAN/VPN address and firewall it to enrolling nodes. |
| `TURNSTONE_ACME_EXTERNAL_URL` | request-derived | Canonical externally reachable ACME responder base, including the final `/acme` mount (for example `http://192.0.2.1:8090/acme`). Set it on the console and clients for cross-host mTLS: the console advertises it, while clients pin it as an allowed enrollment-JWT destination. A reverse-proxy prefix is supported only when the proxy maps it to Turnstone's internal `/acme` mount. |
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
