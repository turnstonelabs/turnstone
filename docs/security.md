# Security and Authentication

Turnstone uses a layered authentication system with three token types,
hierarchical scopes, and a split architecture where the console manages
credentials while individual server nodes validate JWTs locally.

---

## Token Types

### Config-file tokens

Static tokens defined in `config.toml` or the `TURNSTONE_AUTH_TOKEN`
environment variable. Validated in-memory using `hmac.compare_digest`
(timing-safe). Each token maps to a role that determines its scopes.

```toml
[[auth.tokens]]
value = "tok_legacy"
role = "full"     # full → {read, write, approve}
```

Role mappings: `"read"` → `{read}`, `"full"` → `{read, write, approve}`.

Config tokens are sent directly as `Authorization: Bearer tok_legacy`
on every request. No JWT exchange is needed.

### API tokens

Database-backed tokens prefixed with `ts_`. Created via the admin CLI
(`turnstone-admin create-token`) or the console admin API. Stored as
SHA-256 hashes — the raw token is shown exactly once at creation and
never persisted in plaintext.

```
$ turnstone-admin create-token --user abc123 --scopes read,write --name "CI bot"
Token created: ts_a1b2c3d4e5f6...
(save this — it will not be shown again)
```

API tokens can be used directly as `Bearer ts_xxx` headers or exchanged
for a JWT via the login endpoint.

### JWTs

Short-lived session tokens (24 hours by default). Issued after
authenticating with username/password or by exchanging an API token.
HS256-signed with a shared secret. Validated locally on every service
node — no database call per request.

Claims:

| Claim | Description |
|-------|-------------|
| `sub` | User ID |
| `scopes` | Comma-separated scope list (`read,write,approve`) |
| `src` | Token source (`password`, `api_token`, `config`) |
| `iss` | Issuer — always `turnstone` |
| `aud` | Audience — `turnstone-server` or `turnstone-console` |
| `iat` | Issued-at timestamp |
| `exp` | Expiry timestamp |

The `aud` claim prevents cross-service token reuse — a JWT issued for the
console cannot be used to authenticate against a server node, and vice versa.
Tokens without an `aud` claim are accepted during the rollout window when
`audience` validation is not specified.

---

## Scope Model

Scopes are hierarchical — higher scopes imply all lower ones.

| Scope | Grants | Implies |
|-------|--------|---------|
| `read` | View workstreams, saved workstreams, history | — |
| `write` | Send messages, create/close workstreams | `read` |
| `approve` | Approve tool calls, admin endpoints | `read`, `write` |

### Path-to-scope mapping

| Method | Path pattern | Required scope |
|--------|-------------|----------------|
| GET | Any protected path | `read` |
| POST | `/api/send`, `/api/plan`, `/api/command` | `write` |
| POST | `/api/workstreams/new`, `/api/workstreams/close` | `write` |
| POST | `/api/cluster/workstreams/new` | `write` |
| POST | `/api/approve` | `approve` |
| Any | `/api/admin/*` | `approve` |

Public paths bypass authentication entirely: `/`, `/health`, `/metrics`,
`/static/*`, `/shared/*`, `/docs`, `/openapi.json`, `/api/auth/login`,
`/api/auth/logout`, `/api/auth/status`, `/api/auth/setup`.

---

## Login Flows

### Username and password

```
POST /v1/api/auth/login
Content-Type: application/json

{"username": "admin", "password": "s3cret"}
```

Returns a JWT in the response body and sets an `HttpOnly` session cookie.

### API token exchange

```
POST /v1/api/auth/login
Content-Type: application/json

{"token": "ts_a1b2c3d4e5f6..."}
```

The API token is hashed, looked up in the database, and exchanged for a
JWT with the token's scopes. This is the recommended flow for SDKs and
automated clients that need cookie-based sessions.

### Config-file tokens (direct)

Config tokens are validated per-request via `hmac.compare_digest`. No
login exchange is needed — include the token as a `Bearer` header:

```
Authorization: Bearer tok_legacy
```

### First-time setup

When no users exist in the database:

1. `GET /v1/api/auth/status` returns `{"setup_required": true}`
2. The UI presents a setup wizard
3. `POST /v1/api/auth/setup` creates the first admin user and returns a
   JWT in one atomic step (no auth required — this is a public endpoint)
4. The endpoint returns `409 Conflict` if setup has already been completed
   (i.e. users already exist in the database)
5. Subsequent admin requests require `approve` scope

The `/api/auth/setup` endpoint is available on both the server and
console. It validates input before creating the user:

- **username**: 1-64 ASCII characters
- **display_name**: required (non-empty)
- **password**: minimum 8 characters

```
POST /v1/api/auth/setup
Content-Type: application/json

{"username": "admin", "display_name": "Admin", "password": "strongpass"}
```

Response:

```json
{
  "status": "ok",
  "user_id": "u_abc123",
  "username": "admin",
  "role": "full",
  "scopes": "approve,read,write",
  "jwt": "eyJhbGciOiJIUzI1NiIs..."
}
```

The response also sets an `HttpOnly` session cookie containing the JWT,
so the browser is immediately authenticated after setup completes.

---

## Token Detection Order

The auth middleware inspects the `Authorization: Bearer <token>` header
and classifies the token:

1. **Contains `.`** → JWT → validate HS256 signature and expiry
2. **Starts with `ts_`** → API token → SHA-256 hash, database lookup
3. **Otherwise** → config-file token → `hmac.compare_digest` against
   each configured token

If a session cookie is present and no `Authorization` header is sent,
the cookie value is treated as a JWT (step 1).

---

## Password Storage

Passwords are hashed with **bcrypt** using a random salt per password.
Plaintext passwords are only accepted over HTTPS in production
deployments.

---

## Cookie Security

| Attribute | Value | Purpose |
|-----------|-------|---------|
| `HttpOnly` | `true` | Prevents JavaScript access |
| `SameSite` | `Lax` | CSRF protection |
| `Path` | `/` | Available to all routes |
| `Max-Age` | 24 hours | Matches JWT expiry |
| `Secure` | `true` (default) | Always set unless explicitly disabled for dev |

---

## JWT Configuration

| Setting | Config key | Env var | Default |
|---------|-----------|---------|---------|
| Signing secret | `[auth] jwt_secret` | `TURNSTONE_JWT_SECRET` | Auto-generated ephemeral (warning logged) |
| Expiry | `[auth] jwt_expiry_hours` | — | 24 hours |
| Algorithm | — | — | HS256 (not configurable) |
| Minimum secret length | — | — | 32 characters (warning if shorter) |

All service nodes that need to validate JWTs must share the same signing
secret. If no secret is configured, an ephemeral key is generated at
startup and a warning is logged — JWTs will not survive restarts or work
across nodes.

The bridge and console **require** `TURNSTONE_JWT_SECRET` when no
`--auth-token` is provided. They exit with an error if the secret is
missing, since ephemeral secrets would silently break inter-service
communication.

---

## Admin API Endpoints

All admin endpoints require `approve` scope.

### Users

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/api/admin/users` | Create user (username, display_name, password) |
| GET | `/v1/api/admin/users` | List all users |
| DELETE | `/v1/api/admin/users/{user_id}` | Delete user and cascade tokens |

### API tokens

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/api/admin/users/{user_id}/tokens` | Create API token (returns raw value once) |
| GET | `/v1/api/admin/users/{user_id}/tokens` | List tokens (prefix only, no hashes) |
| DELETE | `/v1/api/admin/tokens/{token_id}` | Revoke token |

---

## CLI Administration

The `turnstone-admin` command provides offline user and token management:

```
turnstone-admin create-user --username admin --name "Admin" [--password] [--token]
turnstone-admin create-token --user <user_id> --scopes read,write --name "CI bot"
turnstone-admin list-users
turnstone-admin list-tokens
turnstone-admin revoke-token <token_id>
```

When `--password` is omitted, the CLI prompts interactively. When
`--token` is passed to `create-user`, an API token is created alongside
the user and printed to stdout.

---

## Database Schema

```sql
CREATE TABLE users (
    user_id    TEXT PRIMARY KEY,
    username   TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created    TEXT NOT NULL
);

CREATE TABLE api_tokens (
    token_id     TEXT PRIMARY KEY,
    token_hash   TEXT NOT NULL,       -- SHA-256 of raw token
    token_prefix TEXT NOT NULL,       -- first 8 chars for display
    user_id      TEXT NOT NULL REFERENCES users(user_id),
    name         TEXT NOT NULL,
    scopes       TEXT NOT NULL,       -- comma-separated
    created      TEXT NOT NULL,
    expires      TEXT                 -- nullable, ISO 8601
);

CREATE UNIQUE INDEX ix_api_tokens_hash ON api_tokens(token_hash);

CREATE TABLE channel_users (
    channel_type    TEXT NOT NULL,
    channel_user_id TEXT NOT NULL,
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    created         TEXT NOT NULL,
    PRIMARY KEY (channel_type, channel_user_id)
);
```

The `sessions` and `workstreams` tables have a nullable `user_id`
column for attribution when auth is enabled.

---

## Revocation

- **API tokens**: Deleting a token via the admin API or CLI prevents new
  JWTs from being issued with that token. Existing JWTs derived from the
  token remain valid until they expire (at most 24 hours).
- **Config-file tokens**: Remove the token from `config.toml` and
  restart the service. No JWTs are involved, so revocation is immediate.
- **JWTs**: Cannot be individually revoked. Rely on short expiry (24h)
  and revoke the underlying credential to prevent renewal.

---

## Architecture

```
Console (cluster-wide)              Server (per-node)
┌──────────────────────┐           ┌──────────────────────┐
│ User/Token CRUD (DB) │           │ JWT validation only  │
│ Login: creds → JWT   │           │ (shared signing key) │
│ Admin API endpoints  │           │ Config tokens: hmac  │
│ Storage: users,      │           │ No auth DB needed    │
│   api_tokens tables  │           │                      │
└──────────────────────┘           └──────────────────────┘
```

The console owns the credential database and handles all user/token
CRUD.  Individual server nodes only need the JWT signing secret to
validate session tokens.  Config-file tokens are validated locally
without any database.

### Proxy auth forwarding

When the console proxies requests to server nodes (via `/node/{id}/...`
routes), it uses a dedicated **service proxy token** with
`aud: turnstone-server` and `write` scope.  The user's console JWT
(which has `aud: turnstone-console`) is **not** forwarded — it would be
rejected by the server's audience validation.

The proxy token is managed by a `ServiceTokenManager` that auto-rotates
1-hour JWTs, refreshing at 80% of lifetime.  If `--auth-token` is
provided, that static token is used instead.

### Service-to-service authentication

The bridge and console collector use `ServiceTokenManager` for
auto-rotating JWTs when communicating with server nodes:

| Service | Identity | Scope | Audience | Purpose |
|---------|----------|-------|----------|---------|
| Bridge | `bridge` | `approve` | `turnstone-server` | Tool approval proxy, message relay |
| Console collector | `console-collector` | `read` | `turnstone-server` | Node health polling |
| Console proxy | `console-proxy` | `write` | `turnstone-server` | Proxied API calls |
| Channel notify | `system` | `write` | `turnstone-channel` | Notification delivery to channel gateway |

Service tokens use 1-hour expiry with automatic refresh via
`ServiceTokenManager`.  The bridge injects auth headers per-request via
httpx event hooks to ensure rotated tokens are picked up on SSE
reconnects.

Note that the channel gateway uses a distinct JWT audience
(`turnstone-channel`) from the server (`turnstone-server`) and console
(`turnstone-console`).  A server-scoped JWT cannot authenticate to the
channel gateway endpoint, and vice versa.

---

## Configuration Reference

### config.toml

```toml
[auth]
enabled = true
jwt_secret = "your-secret-key-here"
jwt_expiry_hours = 24

[[auth.tokens]]
value = "tok_legacy"
role = "full"
```

### Environment variables

| Variable | Description |
|----------|-------------|
| `TURNSTONE_AUTH_ENABLED=1` | Enable authentication |
| `TURNSTONE_AUTH_TOKEN=tok_xxx` | Register a config-file token with `full` access |
| `TURNSTONE_JWT_SECRET=xxx` | JWT signing secret (must match across nodes) |
| `TURNSTONE_CORS_ORIGINS=` | CORS allowed origins (comma-separated; empty = same-origin only) |

---

## Login Rate Limiting

The `/api/auth/login` endpoint is protected by a dedicated
`LoginRateLimiter` (separate from the general API rate limiter).
Limits are enforced per-IP and per-username with a sliding window:

- **5 attempts** per **5-minute window** per key
- Failed logins record against both `ip:{client_ip}` and `user:{username}`
- Returns `429 Too Many Requests` with `Retry-After` header when exceeded
- Successful logins do not consume the budget

---

## CORS Policy

By default, no CORS headers are sent (same-origin only). To allow
cross-origin requests, set `TURNSTONE_CORS_ORIGINS`:

```bash
# Allow specific origins
TURNSTONE_CORS_ORIGINS=https://app.example.com,https://admin.example.com

# Allow all origins (development only)
TURNSTONE_CORS_ORIGINS=*
```

When the variable is empty or unset, the CORS middleware is not added
and browsers enforce same-origin policy.

---

## Security Properties

- **Timing-safe comparison** for config-file tokens via
  `hmac.compare_digest` — no timing side-channel.
- **Hash-based lookup** for API tokens — the database stores only
  SHA-256 hashes, eliminating timing attacks on token comparison.
- **Local JWT validation** — no network call or database query needed
  per request on server nodes.
- **One-time display** of raw API tokens at creation. The plaintext is
  never stored; `token_hash` never appears in API responses or logs.
- **Structured logging audit trail** — `ctx_user_id` is set on every
  authenticated request and injected into all log events.
- **Scope enforcement** at the middleware layer before any handler
  executes. Path-to-scope mapping is defined statically.
- **JWT audience isolation** — server and console JWTs have distinct
  `aud` claims, preventing cross-service token reuse.
- **Login brute-force protection** — per-IP and per-username rate
  limiting on the login endpoint.
- **Secure cookies by default** — `Secure` flag set unconditionally;
  24-hour max-age matches JWT expiry.
- **CORS restriction** — no CORS headers by default (same-origin only).
- **Service JWT auto-rotation** — 1-hour expiry with transparent
  refresh, eliminating long-lived static tokens for inter-service auth.
- **Secret strength validation** — warning logged when JWT secret is
  shorter than 32 characters.
