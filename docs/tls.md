# TLS / mTLS

Turnstone supports end-to-end transport encryption with mutual TLS (mTLS) for
inter-service communication, powered by [lacme](https://pypi.org/project/lacme/).

---

## Quick Start (Docker Compose)

```bash
docker compose -f compose.yaml -f deploy/docker-compose.tls.yml up
```

This:
1. Bootstraps an internal CA and issues certs for PostgreSQL
2. Starts the console with TLS enabled (internal CA + ACME server)
3. Server nodes auto-provision certs via the console's ACME endpoint
4. All inter-service communication uses mTLS

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
turnstone-admin tls-list --console-url http://console:8080 --auth-token $TOKEN
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
4. Requests service cert via ACME protocol (plain HTTP, JWS-signed)
5. Starts auto-renewal (24h interval, re-issues before expiry)
6. All subsequent inter-service communication uses mTLS

### Console Startup Flow

1. Read `tls.enabled` from ConfigStore
2. Initialize CA (load from DB or generate new root key)
3. Mount ACME responder at `/acme` (serves `/ca.pem` natively)
4. Issue console certs (internal + optional frontend)
5. Start CA-direct auto-renewal (no network, signs directly)
6. Register console URL in services table with heartbeat

---

## Troubleshooting

### Cert expired / mTLS connection refused

Certs are valid for 48 hours. If auto-renewal stopped (e.g. console was down),
restart the service to re-request a cert.

### "No console service found"

The console registers itself in the `services` table on startup. If the console
hasn't started or the registration expired (1 hour TTL), nodes can't discover
it. Use `--console-url` explicitly.

### Let's Encrypt for console frontend

Set `tls.acme_directory` to `https://acme-v02.api.letsencrypt.org/directory`
in the admin Settings tab. The console will request a publicly trusted cert
for its HTTPS endpoint. Internal mTLS still uses the private CA.

### Verifying the cert chain

```bash
openssl s_client -connect server:8080 -CAfile ca.pem
```
