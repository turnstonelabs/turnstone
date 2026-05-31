# TLS / mTLS

Turnstone supports end-to-end transport encryption with mutual TLS (mTLS) for
inter-service communication, powered by [lacme](https://pypi.org/project/lacme/).

---

## Quick Start (Docker Compose)

```bash
docker compose -f turnstone/deploy/compose.yaml -f deploy/docker-compose.tls.yml up
```

This:
1. Bootstraps an internal CA and issues certs for PostgreSQL
2. Starts the console with TLS enabled (internal CA + ACME server)
3. Server nodes auto-provision certs via the console's ACME endpoint
4. All inter-service communication uses mTLS

---

## Browser access (dashboard HTTPS)

The mTLS above secures **service-to-service** traffic (node↔node, collector and
routing proxy → nodes). The **console dashboard itself serves plain HTTP** — and
must, because it is the cluster's ACME bootstrap endpoint: new nodes fetch
`/acme/ca.pem` and provision their first cert over HTTP, before they have the CA
to verify TLS. So the console cannot be HTTPS-only on its port.

To put the **browser → console** hop on HTTPS, terminate TLS at a reverse proxy
in front of the console. The dev stack (root `compose.yaml`) ships a `caddy`
service that does exactly this — and it's the only published entry point, so the
dashboard is HTTPS by default:

```bash
docker compose up
# dashboard: https://localhost:${CONSOLE_HTTPS_PORT:-8443}
```

The production stack (`turnstone/deploy/compose.yaml`) bundles the same `caddy`
service, so the dashboard is HTTPS there too. For a real domain and a publicly
trusted cert, point Caddy at Let's Encrypt by editing `turnstone/deploy/Caddyfile`.

```
browser  --h2 / HTTPS-->  caddy:443  --h1.1 / HTTP-->  console:8090
```

Caddy uses its **own local CA** (`tls internal`, see `turnstone/deploy/Caddyfile`), so the
setup is self-contained with no dependency on the console's ACME path. Trust the
local root once to silence the browser warning:

```bash
docker compose exec caddy \
  cat /data/caddy/pki/authorities/local/root.crt   # import into your OS/browser
```

**Can Caddy get its cert from the console's internal CA instead?** Technically
yes — the console exposes a real ACME directory (`/acme/directory`) with
auto-approval, so Caddy's `tls { ca http://console:8090/acme/directory }` would
mint a cert for any name. It's not recommended as the default: lacme's ACME
responder is built for turnstone's own client (interop with Caddy's client is
unverified), it couples Caddy startup to the console, and the browser must trust
a private CA either way — so it buys nothing over `tls internal`. For a publicly
trusted cert (no warning), point Caddy at Let's Encrypt with a real domain
instead.

---

## Architecture

```
Console (CA + ACME Server)
  +-- CertificateAuthority (owns root key, signs certs)
  +-- ACMEResponder (mounted at /acme, RFC 8555)
  +-- GET /acme/ca.pem (root cert for node bootstrapping)
                  |
                  | ACME protocol (auto-approve, no challenge validation)
      +-----------+-----------+
      |           |           |
  Server(s)       Channel GW
  (auto-cert       (mTLS
   + renewal)       client)
```

**Two cert paths on the console:**
- **Internal cert** (mTLS): Always from the internal CA. Used for cluster
  service mesh communication.
- **Frontend cert** (HTTPS): From an external ACME CA (e.g. Let's Encrypt)
  if `tls.acme_directory` is set, otherwise self-issued from the internal CA.

---

## Configuration

### Settings (ConfigStore / Admin Settings tab)

| Setting | Default | Description |
|---------|---------|-------------|
| `tls.enabled` | `false` | Master switch for internal mTLS |
| `tls.acme_directory` | `""` | External ACME CA URL for console frontend cert |

### Bootstrap Config (config.toml)

These are needed before storage is available:

```toml
[database]
sslmode = "prefer"   # disable, allow, prefer, require, verify-full
sslrootcert = ""     # path to CA cert
sslcert = ""         # path to client cert
sslkey = ""          # path to client key
```

### Hardcoded Defaults

| Parameter | Value | Notes |
|-----------|-------|-------|
| CA common name | "Turnstone CA" | |
| CA validity | 10 years | |
| Cert validity | 48 hours | Short-lived, auto-renewed |
| Renewal interval | 24 hours | Half of validity |
| ACME auto-approve | true | Internal network, no challenge validation |

---

## CLI

### Offline Bootstrap

Create a CA and infrastructure certs without a running console:

```bash
# Bootstrap CA + PostgreSQL certs
turnstone-admin tls-bootstrap --out /certs --issue postgres

# Output:
#   /certs/ca.pem              (CA root certificate)
#   /certs/certs/postgres/     (PostgreSQL cert + key)
```

The output directory is chmod 0700 (contains the CA private key).

### Online Cert Issuance

Request certs from a running console's ACME endpoint:

```bash
# Download CA root cert (TOFU — verify fingerprint)
turnstone-admin tls-ca-cert --out ca.pem --console-url http://console:8080

# Request a cert for a domain
turnstone-admin tls-issue worker-1.internal --out /certs --console-url http://console:8080

# List issued certs
turnstone-admin tls-list --console-url http://console:8080
```

### Console URL Discovery

If `--console-url` is not provided, the CLI discovers it from the `services`
table in the shared database. The console registers itself on startup.

---

## Admin UI

The **TLS** tab in the console admin panel (System group) shows:
- CA status (common name, certificate count)
- Certificate table (domain, SANs, issued, expires)
- Force-renew and delete actions per certificate

---

## SDK

### Python

```python
from turnstone.sdk import TurnstoneServer

client = TurnstoneServer(
    base_url="https://server:8080",
    token="tok_xxx",
    ca_cert="/path/to/ca.pem",
    client_cert="/path/to/cert.pem",
    client_key="/path/to/key.pem",
)
```

### TypeScript

```typescript
import { TurnstoneServer } from "@turnstone/sdk";
import { Agent } from "undici";
import * as fs from "fs";

const agent = new Agent({
  connect: {
    ca: fs.readFileSync("/path/to/ca.pem"),
    cert: fs.readFileSync("/path/to/cert.pem"),
    key: fs.readFileSync("/path/to/key.pem"),
  },
});

const client = new TurnstoneServer({
  baseUrl: "https://server:8080",
  token: "tok_xxx",
  // Node.js 18+ uses undici under the hood
  fetch: (url, init) =>
    fetch(url, { ...init, dispatcher: agent } as RequestInit),
});
```

---

## How It Works

### Node Bootstrap Flow

1. Node starts, connects to shared database (plain connection)
2. Discovers console URL from `services` table
3. Fetches CA root cert from `http://console/acme/ca.pem` (plain HTTP, TOFU)
4. Requests a service cert via ACME (plain HTTP, JWS-signed). The cert's
   primary domain / SAN is the node's **advertised host** (the host of
   `TURNSTONE_ADVERTISE_URL`, e.g. `node-1`) — the name peers actually dial,
   not the container hostname. This makes mTLS hostname verification succeed
   and keys the cert by a stable name that survives container recreation.
5. Starts auto-renewal (24h interval, re-issues before expiry) **scoped to its
   own certificate**. Each node renews only its own cert; the shared store is
   never swept wholesale. Renewed certs are hot-swapped into the live HTTPS
   listener with no restart.
6. All subsequent inter-service communication uses mTLS

### Console Startup Flow

1. Read `tls.enabled` from ConfigStore
2. Initialize CA (load from DB or generate new root key)
3. Mount ACME responder at `/acme` (serves `/ca.pem` natively)
4. Issue console certs (internal + optional frontend)
5. Start CA-direct auto-renewal (no network, signs directly), scoped to the
   console's own cert, plus a periodic GC that reclaims cert rows for
   long-departed nodes
6. Register console URL in services table with heartbeat

---

## Troubleshooting

### Cert expired / mTLS connection refused

Certs are valid for 48 hours. If auto-renewal stopped (e.g. console was down),
restart the service to re-request a cert.

### Collector/proxy can't reach a node (TLS hostname mismatch)

mTLS verifies a node's advertised host against the cert's SANs. Each node's
cert is issued for the host in its `TURNSTONE_ADVERTISE_URL`, so that name is
always a SAN automatically — you do **not** need to set `TURNSTONE_TLS_SANS`
per node. Only set `TURNSTONE_TLS_SANS` to add *extra* names (e.g. a node
fronted under a second hostname). Symptom if this is wrong: the console
dashboard shows nodes as unreachable and `openssl s_client` reports the served
cert's SANs don't include the dialed name.

### "No console service found"

The console registers itself in the `services` table on startup. If the console
hasn't started or the registration expired (1 hour TTL), nodes can't discover
it. Use `--console-url` explicitly.

### Browser HTTPS to the console

The console serves plain HTTP (it's the ACME bootstrap endpoint — see
[Browser access](#browser-access-dashboard-https)). Put browser traffic on
HTTPS by terminating TLS at a reverse proxy; the `cluster` profile's `caddy`
service does this with Caddy's local CA. For a publicly trusted cert, front the
console with a proxy pointed at Let's Encrypt using a real domain. The
`tls.acme_directory` setting only governs the console's internal/frontend cert
material — it does **not** make the console listen on HTTPS itself.

### Verifying the cert chain

```bash
openssl s_client -connect server:8080 -CAfile ca.pem
```
