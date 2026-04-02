# Security and Authentication

Turnstone uses a layered authentication system with three token types,
hierarchical scopes, and a split architecture where the console manages
credentials while individual server nodes validate JWTs locally.

---

## Token Types

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
| `src` | Token source (`password`, `api_token`, `config`, `oidc`) |
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
`/api/auth/logout`, `/api/auth/status`, `/api/auth/setup`,
`/api/auth/oidc/authorize`, `/api/auth/oidc/callback`.

### RBAC (Granular Permissions)

> See also: [Governance documentation](governance.md)

Scopes provide coarse endpoint-level access control. For finer-grained
enforcement, the governance layer adds 15 named permissions checked
per-endpoint by `require_permission()`. Permissions are bundled into
roles; users are assigned roles via the `user_roles` join table.

At login, `_load_user_permissions()` aggregates all permissions from
the user's assigned roles. `_permissions_to_scopes()` derives legacy
scopes for backward compatibility (e.g., any `admin.*` permission
implies the `approve` scope). The JWT carries both `scopes` and
`permissions` claims.

Three built-in roles are seeded by migration 008:

| Role | Permissions |
|------|-------------|
| admin | All 15 permissions |
| operator | read, write, workstreams.create, workstreams.close |
| viewer | read |

Custom roles can be created with any subset of the valid permissions.
Role creation and update validate permissions against a static allowlist.
Self-assignment is blocked, and assigning a role requires the caller to
hold a superset of the target role's permissions.

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

### OIDC SSO (Single Sign-On)

Turnstone supports OIDC Authorization Code Flow with PKCE for
single sign-on with external identity providers (Okta, Azure AD,
Google, etc.). SSO is opt-in — enabled when the three required
environment variables are set. Users are auto-provisioned on first
login.

#### Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `TURNSTONE_OIDC_ISSUER` | Yes | OIDC issuer URL (e.g., `https://accounts.google.com`) |
| `TURNSTONE_OIDC_CLIENT_ID` | Yes | Client ID from the identity provider |
| `TURNSTONE_OIDC_CLIENT_SECRET` | Yes | Client secret (confidential client) |
| `TURNSTONE_OIDC_SCOPES` | No | OIDC scopes (default: `openid email profile`) |
| `TURNSTONE_OIDC_PROVIDER_NAME` | No | Display name for the SSO button (default: `SSO`) |
| `TURNSTONE_OIDC_ROLE_CLAIM` | No | Claim name in the ID token for role mapping (e.g., `groups`) |
| `TURNSTONE_OIDC_ROLE_MAP` | No | Comma-separated `claim_value:role_id` pairs (e.g., `admin:builtin-admin,eng:builtin-operator`) |
| `TURNSTONE_OIDC_PASSWORD_ENABLED` | No | Set to `false` to hide password login and force SSO-only |

OIDC is enabled when all three required variables (`ISSUER`,
`CLIENT_ID`, `CLIENT_SECRET`) are set.

#### Login flow

1. User clicks "Continue with [Provider]" on the login page
2. `GET /v1/api/auth/oidc/authorize` generates state, nonce, and PKCE
   challenge, stores them in the database, and redirects to the IdP
3. User authenticates at the identity provider
4. IdP redirects to `/v1/api/auth/oidc/callback` with `code` + `state`
5. Server validates state, exchanges the authorization code (with PKCE
   verifier), and validates the ID token (JWKS signature, issuer,
   audience, nonce)
6. Provisions or matches the user by `(issuer, sub)` — never by
   username or email
7. Issues a JWT (`src: oidc`), sets a session cookie, and redirects to
   the application

#### Security measures

- **PKCE (S256)** — prevents authorization code interception
- **State parameter** — one-time use, 5-minute TTL, database-backed
  (multi-node safe)
- **Nonce** — prevents ID token replay
- **JWKS validation** — asymmetric algorithm allowlist (RS/ES/PS
  256-512), HMAC excluded
- **Algorithm allowlist enforced** — the signing key is resolved from
  the JWKS by ``kid``; PyJWK infers the key's algorithm from the JWKS
  ``alg``/``kty`` fields; the token header's ``alg`` must be in the
  allowlist AND match the key type, preventing algorithm confusion
- **Identity matching by (issuer, sub) only** — prevents account
  takeover via email or username reuse
- **`password_enabled=false` enforced server-side** — not just a UI
  toggle
- **Rate limiting** on both authorize and callback endpoints
- **OIDC-provisioned users cannot password-login** — the password hash
  is set to the `!oidc` sentinel, which never matches bcrypt verify

#### Role mapping

When `TURNSTONE_OIDC_ROLE_CLAIM` is set (e.g., `groups`), the server
reads that claim from the ID token and maps values to Turnstone roles
via `TURNSTONE_OIDC_ROLE_MAP`. Roles are synced on every login:
matching claim values are added, and stale OIDC-assigned roles are
revoked. Roles assigned manually (not by OIDC) are never touched.

If no role mapping is configured, OIDC users are provisioned with the
`builtin-viewer` role by default.

#### OIDC-only mode

Setting `TURNSTONE_OIDC_PASSWORD_ENABLED=false` hides the password
form on the login page and blocks password-based login at the API
level. The setup wizard always works regardless of this setting — the
first admin user is created with a password before OIDC is relevant.
API tokens are unaffected by this setting.

#### Known limitations

- **No session revocation** — deprovisioned IdP users retain their JWT
  until the 24-hour expiry
- **Single IdP** — configuration supports one issuer (the database
  schema supports multiple for future expansion)
- **Redirect URI** — defaults to request Host header; deployments behind
  reverse proxies should set `TURNSTONE_OIDC_REDIRECT_BASE` to the
  externally-reachable origin to pin the redirect URI

---

## Token Detection Order

The auth middleware inspects the `Authorization: Bearer <token>` header
and classifies the token:

1. **Contains `.`** → JWT → validate HS256 signature and expiry
2. **Starts with `ts_`** → API token → SHA-256 hash, database lookup

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
| Minimum secret length | — | — | 32 characters (exits if shorter) |

All services require `TURNSTONE_JWT_SECRET` and exit at startup if it is
missing or shorter than 32 characters.

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
│ Admin API endpoints  │           │ No auth DB needed    │
│ Storage: users,      │           │                      │
│   api_tokens tables  │           │                      │
└──────────────────────┘           └──────────────────────┘
```

The console owns the credential database and handles all user/token
CRUD.  Individual server nodes only need the JWT signing secret to
validate session tokens.

### Proxy auth forwarding

When the console proxies requests to server nodes (via `/node/{id}/...`
routes), it mints a **short-lived user-scoped JWT** with
`aud: turnstone-server` carrying the real user's `user_id`, `scopes`,
and `permissions`.  The user's console JWT (which has
`aud: turnstone-console`) is **not** forwarded directly — it would be
rejected by the server's audience validation.  Instead, the console
re-signs a new JWT targeted at the server audience.

Each proxied request gets a fresh JWT (5-minute expiry).  This ensures:

- **Audit attribution** — the upstream server records the real user in
  `ctx_user_id` and audit events, not a generic service identity.
- **Scope narrowing** — a read-only console user's proxied request
  carries only `read` scope, not the full `{read, write, approve}` set.
  The server enforces this as defense in depth.
- **Permission forwarding** — granular RBAC permissions from the
  console JWT are carried through to the server.

The JWT `src` claim is set to `"console-proxy"`, allowing servers to
distinguish proxied requests from direct logins in audit logs.

When no user context is available (auth disabled, or internal requests),
the proxy falls back to a `ServiceTokenManager` with service identity
`console-proxy` and full scopes.

### Service-to-service authentication

The console collector uses `ServiceTokenManager` for auto-rotating
JWTs when communicating with server nodes:

| Service | Identity | Scope | Audience | Purpose |
|---------|----------|-------|----------|---------|
| Console collector | `console-collector` | `read` | `turnstone-server` | Node health polling |
| Console proxy (fallback) | `console-proxy` | `approve` | `turnstone-server` | Proxied API calls when no user context |
| Channel notify | `system` | `write` | `turnstone-channel` | Notification delivery to channel gateway |

Service tokens use 1-hour expiry with automatic refresh via
`ServiceTokenManager`.

### User identity in MQ-dispatched workstreams

When the console creates a workstream (the normal path), the
authenticated user's `user_id` is forwarded in the HTTP payload when
calling the server's `POST /v1/api/workstreams/new`.  The server
accepts a `user_id` from the request body **only when the caller is a
trusted service** — identified by `token_source` matching
`console-proxy` or `console`.  Regular API callers cannot
override `user_id`; the server always uses their JWT identity.

Note that the channel gateway uses a distinct JWT audience
(`turnstone-channel`) from the server (`turnstone-server`) and console
(`turnstone-console`).  A server-scoped JWT cannot authenticate to the
channel gateway endpoint, and vice versa.

---

## Configuration Reference

### config.toml

```toml
[auth]
jwt_secret = "your-secret-key-here"
jwt_expiry_hours = 24
```

### Environment variables

Auth is always enabled. `TURNSTONE_JWT_SECRET` is required.

| Variable | Description |
|----------|-------------|
| `TURNSTONE_JWT_SECRET=xxx` | JWT signing secret (required, must match across nodes) |
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
- **OIDC PKCE enforcement** — S256 code challenge on every
  authorization request prevents code interception in transit.
- **OIDC state/nonce in database** — one-time-use, TTL-bounded tokens
  stored in the database, safe for multi-node deployments.
- **OIDC JWKS-only validation** — ID tokens are verified using the
  provider's published JWKS keys with asymmetric algorithms only;
  HMAC-based algorithms are rejected to prevent algorithm confusion.
- **OIDC identity binding by (issuer, sub)** — user matching uses the
  immutable subject identifier, not email or username, preventing
  account takeover via IdP attribute changes.
