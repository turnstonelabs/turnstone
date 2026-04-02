# OpenID Connect (OIDC) Single Sign-On

Turnstone supports OpenID Connect for federated authentication, allowing
users to log in with their existing corporate identity provider instead of
managing a separate password. OIDC is opt-in: when configured, the login
screen shows a "Continue with SSO" button alongside the existing
username/password form. When not configured, the login experience is
unchanged.

Any OIDC-compliant provider works: Google, Okta, Azure AD, Keycloak,
Auth0, OneLogin, and others that publish a
`.well-known/openid-configuration` discovery document.

---

## Prerequisites

1. A registered **confidential** OIDC client at your identity provider
2. The client's redirect URI must include:
   `https://your-turnstone-host/v1/api/auth/oidc/callback`
3. A local admin user must exist in Turnstone (complete the initial setup
   wizard before enabling OIDC)

---

## Configuration

OIDC is configured via environment variables (preferred) or the `[oidc]`
section of `config.toml`. Environment variables take precedence when both
are set.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TURNSTONE_OIDC_ISSUER` | Yes | — | Issuer URL (e.g. `https://accounts.google.com`). Must serve `/.well-known/openid-configuration`. |
| `TURNSTONE_OIDC_CLIENT_ID` | Yes | — | OAuth 2.0 client ID from your provider |
| `TURNSTONE_OIDC_CLIENT_SECRET` | Yes | — | OAuth 2.0 client secret (confidential client) |
| `TURNSTONE_OIDC_SCOPES` | No | `openid email profile` | Space-separated OAuth scopes to request |
| `TURNSTONE_OIDC_PROVIDER_NAME` | No | `SSO` | Display name for the login button (e.g. "Google", "Okta") |
| `TURNSTONE_OIDC_ROLE_CLAIM` | No | — | ID token claim containing role/group values (see [Role Mapping](#role-mapping)) |
| `TURNSTONE_OIDC_ROLE_MAP` | No | — | Mapping from claim values to Turnstone role IDs (see [Role Mapping](#role-mapping)) |
| `TURNSTONE_OIDC_PASSWORD_ENABLED` | No | `true` | Set to `false` to hide the password form and block all username/password logins (including admin). API tokens continue to work. |
| `TURNSTONE_OIDC_REDIRECT_BASE` | No | — | Externally-reachable origin for the OIDC redirect URI (e.g. `https://app.example.com`). Recommended when running behind a reverse proxy. When unset, derived from the request Host header. |

OIDC is enabled when all three required fields (issuer, client ID, client
secret) are non-empty. If any is missing, OIDC is silently disabled and
the login screen shows only the password form.

### Reverse Proxy / Load Balancer

When Turnstone runs behind a reverse proxy, the internal `Host` header may
not match the externally-reachable URL. Set `TURNSTONE_OIDC_REDIRECT_BASE`
to the public origin so the redirect URI sent to the identity provider is
correct:

```bash
TURNSTONE_OIDC_REDIRECT_BASE=https://app.example.com
```

The resulting callback URL will be
`https://app.example.com/v1/api/auth/oidc/callback` — register this as the
authorized redirect URI in your identity provider.

### config.toml alternative

```toml
[oidc]
issuer = "https://accounts.google.com"
client_id = "your-client-id"
client_secret = "your-client-secret"
scopes = "openid email profile"
provider_name = "Google"
role_claim = "groups"
password_enabled = true
redirect_base = "https://app.example.com"

[oidc.role_map]
admin = "builtin-admin"
engineering = "builtin-operator"
```

---

## Provider-Specific Setup

### Google

1. Go to [Google Cloud Console](https://console.cloud.google.com/) >
   **APIs & Services** > **Credentials**
2. Click **Create Credentials** > **OAuth 2.0 Client ID**
3. Application type: **Web application**
4. Add authorized redirect URI:
   `https://your-turnstone-host/v1/api/auth/oidc/callback`
5. Copy the **Client ID** and **Client secret**

```bash
TURNSTONE_OIDC_ISSUER=https://accounts.google.com
TURNSTONE_OIDC_CLIENT_ID=123456789.apps.googleusercontent.com
TURNSTONE_OIDC_CLIENT_SECRET=GOCSPX-...
TURNSTONE_OIDC_PROVIDER_NAME=Google
```

### Okta

1. In the Okta Admin Console, go to **Applications** > **Create App
   Integration**
2. Sign-in method: **OIDC - OpenID Connect**
3. Application type: **Web Application**
4. Add sign-in redirect URI:
   `https://your-turnstone-host/v1/api/auth/oidc/callback`
5. Note the **Issuer** (your Okta domain, e.g.
   `https://dev-123456.okta.com`)

```bash
TURNSTONE_OIDC_ISSUER=https://dev-123456.okta.com
TURNSTONE_OIDC_CLIENT_ID=0oaXXXXXXXXXXXXX
TURNSTONE_OIDC_CLIENT_SECRET=...
TURNSTONE_OIDC_PROVIDER_NAME=Okta
TURNSTONE_OIDC_ROLE_CLAIM=groups
TURNSTONE_OIDC_ROLE_MAP="admin:builtin-admin,everyone:builtin-operator"
```

### Azure AD (Entra ID)

1. In the Azure Portal, go to **App registrations** > **New registration**
2. Redirect URI: **Web** >
   `https://your-turnstone-host/v1/api/auth/oidc/callback`
3. Under **Certificates & secrets**, create a new **Client secret** and
   copy the value immediately
4. The issuer URL is
   `https://login.microsoftonline.com/{tenant-id}/v2.0`

```bash
TURNSTONE_OIDC_ISSUER=https://login.microsoftonline.com/YOUR_TENANT_ID/v2.0
TURNSTONE_OIDC_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
TURNSTONE_OIDC_CLIENT_SECRET=...
TURNSTONE_OIDC_PROVIDER_NAME="Azure AD"
TURNSTONE_OIDC_ROLE_CLAIM=roles
TURNSTONE_OIDC_ROLE_MAP="Admin:builtin-admin,User:builtin-operator"
```

### Keycloak

1. In the Keycloak Admin Console, select your **Realm**
2. Go to **Clients** > **Create client**
3. Client type: **OpenID Connect**
4. Set **Client authentication** to **On** (confidential)
5. Add valid redirect URI:
   `https://your-turnstone-host/v1/api/auth/oidc/callback`
6. The issuer URL is
   `https://keycloak.example.com/realms/your-realm`

```bash
TURNSTONE_OIDC_ISSUER=https://keycloak.example.com/realms/your-realm
TURNSTONE_OIDC_CLIENT_ID=turnstone
TURNSTONE_OIDC_CLIENT_SECRET=...
TURNSTONE_OIDC_PROVIDER_NAME=Keycloak
TURNSTONE_OIDC_ROLE_CLAIM=realm_access.roles
TURNSTONE_OIDC_ROLE_MAP="admin:builtin-admin,operator:builtin-operator"
```

---

## Role Mapping

OIDC role mapping assigns Turnstone roles to users based on claims in the
ID token. This is optional — without it, OIDC users are provisioned with
the `builtin-viewer` role (read-only access) by default.

### Configuration

Set `TURNSTONE_OIDC_ROLE_CLAIM` to the name of the claim in the ID token
that contains the user's group or role memberships. Then set
`TURNSTONE_OIDC_ROLE_MAP` to map claim values to Turnstone role IDs.

The role map is a comma-separated list of `claim_value:turnstone_role`
pairs:

```bash
TURNSTONE_OIDC_ROLE_CLAIM=groups
TURNSTONE_OIDC_ROLE_MAP="admin:builtin-admin,engineering:builtin-operator,viewer:builtin-viewer"
```

### Behavior

- **Synced on every login**: roles are added when new claim values appear,
  and OIDC-assigned roles are revoked when the corresponding claim value
  is no longer present. Roles assigned manually (or by other sources) are
  never touched — only roles with `assigned_by="oidc"` are subject to
  revocation.
- **List or string**: the claim value can be a JSON array
  (`["admin", "engineering"]`) or a single string (`"admin"`). Both are
  handled correctly.
- **Unknown values**: claim values not present in the role map are silently
  ignored.
- **Missing roles**: if the role map references a Turnstone role ID that
  does not exist in the database, the assignment is skipped (no error).
- **Evaluated on every login**: roles are checked and applied each time
  the user authenticates via OIDC, so new group memberships are picked
  up on the next login.

### Built-in Roles

| Role ID | Permissions |
|---------|-------------|
| `builtin-admin` | All permissions |
| `builtin-operator` | read, write, workstreams.create, workstreams.close |
| `builtin-viewer` | read |

---

## User Provisioning

When a user logs in via OIDC for the first time, Turnstone automatically
creates a local user account:

1. The OIDC identity (`issuer` + `sub` claim) is stored in the
   `oidc_identities` table and linked to the new user
2. The **username** is derived from the `preferred_username` claim,
   falling back to the email local part, with deduplication if needed
3. The **display name** comes from the `name` claim, falling back to
   `preferred_username` or email
4. The user's password hash is set to a sentinel value (`!oidc`) — OIDC
   users cannot log in with a password

On subsequent logins, the existing user is matched by `(issuer, sub)` and
the `last_login` timestamp is updated. Role mapping is re-evaluated on
every login.

---

## OIDC-Only Mode

To enforce OIDC for all logins and hide the password form, set:

```bash
TURNSTONE_OIDC_PASSWORD_ENABLED=false
```

In this mode the login screen shows only the "Continue with SSO" button.
The password form, token toggle, and sign-in button are all hidden.
All username/password logins are blocked at the API level, including
admin accounts.

The first admin account must be created via the setup wizard (with a
password) before OIDC is enabled. The setup wizard always works
regardless of this setting because it is only available when zero users
exist in the database.

API token login (`POST /v1/api/auth/login` with a `ts_` token)
continues to work regardless of this setting. JWTs and API tokens are
the supported authentication methods. OIDC-only mode affects
password-based authentication only.

---

## Login Flow

Both the server and console support OIDC login. The flow is identical:

1. The browser fetches `GET /v1/api/auth/status` at page load
2. If the response includes `oidc_enabled: true`, the login screen shows
   a "Continue with {provider_name}" button
3. Clicking the button navigates to `GET /v1/api/auth/oidc/authorize`
4. Turnstone generates a state token, nonce, and PKCE verifier, stores
   them in the database, and redirects the browser to the identity
   provider's authorization endpoint
5. The user authenticates at the identity provider
6. The IdP redirects back to
   `GET /v1/api/auth/oidc/callback?code=...&state=...`
7. Turnstone validates the state, exchanges the authorization code for
   tokens using the PKCE verifier, validates the ID token against the
   provider's JWKS public keys, provisions or matches the user, and
   issues a Turnstone JWT
8. The browser is redirected to `/?oidc_success=1` with the JWT set in
   an `HttpOnly` session cookie
9. The browser JavaScript detects the `oidc_success` query parameter,
   strips it from the URL, hides the login overlay, and calls
   `onLoginSuccess()` to initialize the application

---

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/v1/api/auth/oidc/authorize` | Public | Redirects to identity provider |
| GET | `/v1/api/auth/oidc/callback` | Public | Handles IdP callback, issues JWT |

Both endpoints are public (no authentication required) because they are
part of the login flow itself.

### Auth status response

When OIDC is enabled, `GET /v1/api/auth/status` includes additional
fields:

```json
{
  "auth_enabled": true,
  "has_users": true,
  "setup_required": false,
  "oidc_enabled": true,
  "oidc_provider_name": "Google",
  "password_enabled": true
}
```

---

## Database Schema

Migration 018 creates two tables:

```sql
CREATE TABLE oidc_identities (
    issuer      TEXT NOT NULL,
    subject     TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    email       TEXT NOT NULL DEFAULT '',
    created     TEXT NOT NULL,
    last_login  TEXT NOT NULL,
    PRIMARY KEY (issuer, subject)
);

CREATE INDEX idx_oidc_identities_user_id ON oidc_identities(user_id);

CREATE TABLE oidc_pending_states (
    state          TEXT PRIMARY KEY,
    nonce          TEXT NOT NULL,
    code_verifier  TEXT NOT NULL,
    audience       TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
```

The `oidc_identities` table links an OIDC subject (identified by
`issuer` + `subject`) to a Turnstone `user_id`. A single user can have
multiple OIDC identities (e.g. from different providers).

The `oidc_pending_states` table stores authorization flow state for
callback validation. Entries are automatically cleaned up after 5 minutes.

---

## Security Notes

- **Authorization Code Flow with PKCE**: the recommended OAuth 2.0 flow
  for web applications. PKCE prevents authorization code interception
  attacks even without a client secret (though the client secret is still
  used for additional security).
- **ID token validation**: all tokens are validated using the provider's
  JWKS public keys (RS256 or ES256). The signature, issuer, audience,
  and expiry are all checked.
- **State parameter**: a cryptographically random state token prevents
  CSRF attacks on the callback endpoint. The state is stored server-side
  and verified on callback.
- **Nonce**: a random nonce is included in the authorization request and
  verified in the ID token to prevent replay attacks.
- **Client secret**: never leaves the server — it is only used in the
  server-to-IdP token exchange, not exposed to the browser.
- **OIDC users cannot use password login**: the sentinel password hash
  (`!oidc`) ensures `verify_password()` always rejects password attempts
  for OIDC-provisioned users.
- **Rate limiting**: the callback endpoint shares the login rate limiter
  (5 attempts per 5-minute window per IP).
- **State TTL**: pending authorization states expire after 5 minutes.
  Expired states are lazily cleaned up on each callback.
- **Setup guard**: OIDC login requires at least one local admin user to
  exist. This ensures the initial admin account is always created via the
  setup wizard with a password, not hijacked by an external identity.

---

## Troubleshooting

### "OIDC not configured"

All three required environment variables must be set:
`TURNSTONE_OIDC_ISSUER`, `TURNSTONE_OIDC_CLIENT_ID`, and
`TURNSTONE_OIDC_CLIENT_SECRET`. Check that none are empty or
whitespace-only.

### "Login session expired"

The authorization flow must complete within 5 minutes. If the user takes
too long at the identity provider, the pending state expires. Try again.

### "Initial setup required"

OIDC login is blocked until at least one local admin user exists.
Complete the setup wizard first (navigate to the Turnstone URL and follow
the prompts to create an admin user with a password).

### Discovery fails at startup

Check that the issuer URL is reachable from the Turnstone server and
serves a valid `/.well-known/openid-configuration` document. The server
logs the discovery attempt at startup:

```
OIDC discovery failed for https://your-issuer.example.com: ...
```

OIDC is automatically disabled when discovery fails. Restart the server
after fixing the connectivity issue.

### Redirect URI mismatch

The redirect URI configured at the identity provider must exactly match
`https://your-host/v1/api/auth/oidc/callback`. Common issues:

- **Scheme mismatch**: the redirect uses `https://` — make sure TLS is
  configured or a reverse proxy sets the `X-Forwarded-Proto` header
- **Port mismatch**: if running on a non-standard port, include it in
  the redirect URI
- **Path mismatch**: the path must include the `/v1` API version prefix

### User not assigned expected roles

Check that:

1. `TURNSTONE_OIDC_ROLE_CLAIM` matches the exact claim name in the ID
   token (case-sensitive)
2. `TURNSTONE_OIDC_ROLE_MAP` maps the correct claim values to valid
   Turnstone role IDs
3. The roles referenced in the map exist in the database (check the
   admin panel > Roles tab)
4. The identity provider is configured to include the claim in the ID
   token (some providers require explicit scope or claim configuration)
