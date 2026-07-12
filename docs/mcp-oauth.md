# MCP OAuth — per-user authorization for MCP servers

Turnstone supports **per-(user, MCP server) OAuth 2.1 + PKCE** delegation so each Turnstone user authorizes a remote MCP server with their own identity, rather than sharing a single bearer token across the deployment. This is the right shape for MCP servers that expose user-specific data (a personal CRM, an email inbox, a calendar) and for MCP servers that want per-user audit attribution.

Per-user OAuth is opt-in per `mcp_servers` row. Local-auth Turnstone installs with no `oauth_user` rows exercise zero new code paths — the entire feature is dark by default.

> **Note**: This is a separate authorization layer from Turnstone's own user authentication. A user who logs into Turnstone with a local username + password can still authorize a per-server OAuth MCP server. OIDC SSO and per-server OAuth are orthogonal.

---

## When to use which `auth_type`

The MCP server admin form exposes three authorization modes ("Multitenant Authorization"):

| `auth_type` | What it means | When to use |
|---|---|---|
| `none` | No headers attached. Open MCP server (or one gated by network policy only). | Internal MCP servers on a trusted network. |
| `static` | One static bearer token, configured per server, sent on every request from every user. | Service-to-service MCP servers where per-user attribution doesn't matter, or single-tenant deployments. |
| `oauth_user` *(recommended for user-data servers)* | Each user authorizes separately via OAuth 2.1 + PKCE; Turnstone stores per-user tokens encrypted at rest. | MCP servers that expose user-specific data or that want per-user audit attribution. |
| `oauth_obo` *(sign-in passthrough)* | Each user's Turnstone **org sign-in** (OIDC) mints a per-server access token on demand — no separate per-server consent. One captured credential per user covers every `oauth_obo` server. | Enterprise deployments where the identity provider governs access (Entra, Keycloak) and you want zero per-user connect clicks. See the dedicated section below. |

Switching `auth_type` away from `oauth_user` / `oauth_obo` orphans (oauth_user) or purges (on transition, per below) existing per-user rows. Use the admin **bulk-revoke** / **flush cache** affordance on the server row to clear them, or let them expire naturally.

---

## Prerequisites for `auth_type=oauth_user`

1. **Encryption key**. Tokens are stored encrypted with Fernet. Set `[security] mcp_token_encryption_key` in `config.toml` (Turnstone won't start with an `oauth_user` row configured but no key installed). Rotate via `MultiFernet` — add the new key first, then later remove the old one once all rows have been re-encrypted.

2. **MCP server publishes RFC 9728 PRM and RFC 8414 AS metadata** *or* you configure the AS URL override on the server row. PKCE S256 is mandatory; Turnstone refuses to connect to authorization servers that don't advertise `code_challenge_methods_supported: ["S256"]`.

3. **OAuth client registration**. Two paths:
   - **Pre-registered** (most common): you create an OAuth client at the authorization server (manually, via admin console, or via Terraform), then paste the `client_id` / `client_secret` into the Turnstone admin form.
   - **Dynamic client registration** (RFC 7591): if the AS supports it and you select that mode in the admin form, Turnstone registers a client at first use and persists the `client_id` automatically.

4. **Redirect URI** registered at the authorization server: `https://your-turnstone-host/v1/api/mcp/oauth/callback`.

---

## Configuration

### Per-server fields (admin UI)

| Field | Required | Description |
|---|---|---|
| Server URL | Yes | The MCP server's `streamable-http` base URL. |
| Multitenant Authorization | Yes | `none` / `static` / `oauth_user` (recommended). |
| Authorization Server URL | No | Override for RFC 9728 PRM discovery. Set when your AS endpoint differs from the MCP server URL (e.g., corporate AS protecting a third-party MCP). When unset, Turnstone falls back to PRM discovery against the MCP server itself. |
| Client Registration | Yes (oauth_user) | `preregistered` or `dynamic`. |
| Client ID | Yes (preregistered) | OAuth 2.0 client ID. Stored unencrypted. |
| Client Secret | Optional (write-only) | OAuth 2.0 client secret (confidential client). Encrypted at rest. Written but never re-read by the API; field stays masked. |
| Scopes | No | Space-separated default scope set requested at the authorize endpoint. Per-tool step-up may union additional scopes from a server's `insufficient_scope` response. |
| Audience | No | RFC 8707 `resource=` parameter sent on every authorize and token request. Defaults to the MCP server URL when unset. Validate against the `aud` claim in returned JWT tokens. |

### Encryption key

```toml
[security]
mcp_token_encryption_key = "base64-fernet-key"
# For rotation, list the keys in priority order — first is used for new
# writes, all are tried for reads.
# mcp_token_encryption_keys = ["new-key", "old-key"]
```

Keep this in `config.toml` rather than environment variables. An in-process LLM with shell-tool access can read the server's environment via `env` / `os.environ` and exfiltrate any secret stored there; secrets in `config.toml` are only loaded into the server at startup and never re-read on a tool-driven path, so a prompt-injection attack against the agent cannot reach them.

---

## `auth_type=oauth_obo` — single-credential sign-in passthrough

Where `oauth_user` makes each user complete a **separate** browser consent per MCP server, `oauth_obo` reuses the user's Turnstone **org sign-in** (OIDC). Turnstone captures one refresh credential per user at login and, on each tool call, mints a short-lived access token scoped to that server's audience. There is no per-server connect step, and one credential covers every `oauth_obo` server. This is the right shape when your identity provider already governs who may reach each backend (an Entra tenant with Entra-protected MCP servers; a Keycloak realm with token exchange).

Access is governed **downstream** by the IdP: a user can only mint a token for a server their delegated permissions allow. Removing that grant at the IdP cuts the user off regardless of their Turnstone state.

### Deployment configuration (`[oidc]` in `config.toml`)

`oauth_obo` requires OIDC SSO to be configured (it is the credential source), plus:

```toml
[oidc]
# ... your existing issuer / client_id / client_secret ...
capture_user_credential = true          # persist the IdP refresh token at login
obo_grant_profile = "entra"             # "entra" | "rfc8693" — how tokens are minted
```

- **`capture_user_credential`** (default `false`): when enabled, Turnstone appends `offline_access` to the login scopes and stores the returned refresh token, encrypted with the same `[security] mcp_token_encryption_key` as `oauth_user` tokens. **The encryption key is required** — Turnstone refuses to start with an `oauth_obo` row (or capture enabled) and no key.
- **`obo_grant_profile`** picks the mint mechanism (the IdP determines which one is valid; this is deployment-wide, not per-server):
  - **`entra`** — redeems the user's refresh token directly for a token scoped to `<audience>/.default`. `oauth_scopes` on the server row is **not used** (the admin form rejects it under this profile).
  - **`rfc8693`** — a refresh grant for a subject token, then an RFC 8693 token exchange for the server audience. Per-server `oauth_scopes` **are** sent on the exchange (some IdPs require the audience scope explicitly).

### Adding an `oauth_obo` server

In the admin MCP form, choose **Sign-in passthrough** and set **Audience** (required — the downstream resource the token is minted for, e.g. `api://<app-id>` on Entra or the client id on Keycloak). The client-id / secret / registration fields do not apply and are hidden.

### Identity-provider setup

**Entra (`obo_grant_profile = "entra"`):**
1. Turnstone's app registration must hold **delegated permissions** to each MCP server's exposed API, with **admin consent granted** (or the MCP app listed in Turnstone's `preAuthorizedApplications`).
2. Set the server row's Audience to the MCP app's Application ID URI (`api://<guid>`).
3. **Gotcha (verified):** admin-consent issued *immediately* after creating the app/service principal can silently skip a not-yet-propagated resource — the only symptom is `AADSTS65001` at mint time. Verify the delegated grant landed (`az ad app permission list-grants` / the portal's *API permissions* blade shows *Granted*), or grant it explicitly per resource. A missing grant surfaces in Turnstone as a re-login prompt on the affected server (same rail as a revoked credential), and the `mcp_server.oauth.obo_mint_rejected` log line carries the raw `AADSTS…` text.

**Keycloak / RFC 8693 (`obo_grant_profile = "rfc8693"`):**
1. Enable **standard token exchange** on Turnstone's client.
2. Grant the audience: add an audience client scope for each MCP client and attach it to Turnstone's client (optional scopes must be requested — set the server row's Scopes to that scope, or the exchange returns *"Requested audience not available"*).
3. Set the server row's Audience to the downstream client id.

### Revocation & custody

The captured credential is a single per-user secret that can mint for every `oauth_obo` server, so treat it like any long-lived credential:

- **Cut off one user:** unlink their OIDC identity in the admin console (**Users → OIDC identities → delete**). This revokes the captured credential **and** purges their minted cache rows, so future mints fail and cached tokens are dropped. (Warmed in-memory sessions on server nodes self-expire at the access-token TTL; there is no cross-node per-user session-kill.) Removing the user's access at the IdP is the authoritative cut-off.
- **Flush a server's minted tokens** (e.g. after narrowing its audience): the server row's **flush cache** action drops all users' cached tokens for that server. This is **not** a revocation — users re-mint on next use from their still-valid sign-in. It is surfaced honestly (audit `mcp_server.oauth.obo_cache_flushed`, response `effect: cache_flush_remints`) so it is never mistaken for cutting access.
- Per-server revocation in the `oauth_user` sense does not exist for `oauth_obo` — the credential is issuer-scoped and IdP-governed. Revoke at the IdP.

> **Interim for Entra without OBO:** if you don't want host-side minting, admin consent + `preAuthorizedApplications` on each MCP app registration removes the second consent prompt for the plain `oauth_user` flow too (a tenant-config change, no Turnstone code). Tracked in issue #682. It does not remove the per-server connect clicks or per-(user, server) token custody — that is what `oauth_obo` is for.

---

## Lifecycle

1. **First tool call** for a user against an `oauth_user` MCP server: pool dispatch finds no stored token, returns `mcp_consent_required` to the agent. Dashboard renders an inline "Connect" action card.

2. **User clicks Connect**: opens `/v1/api/mcp/oauth/start?server=<name>` in a popup. Browser redirects through the AS authorize endpoint, user grants consent, AS redirects back to `/v1/api/mcp/oauth/callback`. Turnstone exchanges code → tokens via PKCE, validates audience, encrypts, persists in `mcp_user_tokens`, redirects user back to the originating URL.

3. **Subsequent tool calls** by the same user against the same server reuse the persisted token via the per-(user, server) session pool. Tokens auto-refresh via the refresh-token grant when expired; failed refresh emits `mcp_consent_required` to drive re-consent.

4. **Step-up scope**: when a tool call hits `403` with `WWW-Authenticate: error="insufficient_scope"`, Turnstone emits `mcp_insufficient_scope` with the parsed scope set; the dashboard offers a "Connect with additional scopes" affordance that opens `/v1/api/mcp/oauth/start?server=<name>&scopes=<extra>` so the union of original + new scopes flows into the AS authorize request.

5. **User revoke** (settings modal): `DELETE /v1/api/mcp/oauth/connections/{server_name}` runs the authoritative local delete + best-effort RFC 7009 upstream revoke (fire-and-forget, capped at 256 concurrent in-flight tasks).

6. **Admin bulk-revoke** (Phase 9): `POST /v1/api/admin/mcp-servers/{name}/bulk-revoke` drops every user's token for the server. Upstream RFC 7009 revoke is intentionally **not** attempted in bulk (avoids N upstream HTTP calls per admin click); tokens at the AS expire naturally. Use the per-user revoke endpoint if you need guaranteed upstream invalidation.

---

## Admin status indicators

The MCP Servers admin tab shows per-server status pills (Phase 9):

- **Consented users count** — distinct users with a non-expired token for this server. Surfaced as a `bulk-revoke (N)` button when ≥1; clicking it opens a confirmation dialog. Hidden when 0.
- **Last refresh** — timestamp + outcome (`ok` / `error:ClassName`) of the most recent manual or auto-reconnect refresh. Per node. Absent until at least one refresh has occurred (renders as "never" in the admin UI).

Additional indicators (circuit-breaker state, encryption-key mismatch) are exposed via `get_server_status` on the API but do not yet have a dedicated admin pill — operators see them today via the per-server status text + error tooltip and in audit logs. A future phase may surface these as discrete pills.

---

## Auth-type transitions

| From | To | What happens |
|---|---|---|
| `none` / `static` → `oauth_user` | — | New code path activates for this server. Existing static headers (if any) are no longer sent. Users must authorize on first use. |
| `oauth_user` → `none` / `static` | — | Existing `mcp_user_tokens` rows are **orphaned** — inert without a matching `auth_type`. Use admin bulk-revoke to drop them, or let them expire. Switching back to `oauth_user` later re-activates the orphaned rows if they haven't been deleted. |
| OAuth `client_id` or `client_secret` rotated | — | Existing tokens may stop refreshing if the AS treats them as bound to the previous client. Bulk-revoke after rotation. |
| `oauth_user` ↔ `oauth_obo` | — | The per-user rows are **purged** on the flip (they mean different things: per-server AS refresh tokens vs. minted cache). A flip into `oauth_obo` also clears the stale `oauth_scopes` (they were the AS-consent scopes; the mint leg would otherwise send them). |
| `oauth_obo` → `none` / `static` | — | Minted cache rows are purged. |
| `oauth_obo` **audience** or **URL** changed | — | Minted cache rows for the old audience/URL are **purged** (tokens are audience/URL-bound), forcing a fresh mint. |

The orphan-by-default behavior (for `oauth_user` → `none`/`static`) is chosen so switching back is non-destructive. Transitions **into or out of** `oauth_obo`, and audience/URL edits, purge instead — the bindings are semantic and a stale row must never be served.

---

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `mcp_consent_required` even after consenting | Token persistence failed, or refresh-token rejected by AS | Check audit log for `mcp_server.oauth.persist_failed` or `mcp_server.oauth.token_revoked`. Re-consent via settings modal. |
| `mcp_token_undecryptable_key_unknown` | Encryption key rotated without keeping the previous key in the keyring | Add the previous key back to `mcp_token_encryption_keys` until all rows have been re-encrypted, then drop. |
| `mcp_oauth_url_insecure` | MCP server URL is `http://` (not `https://`) on a non-loopback host | Use `https://`. Per-user bearers must not transit cleartext. |
| Tools fail in scheduled / Discord / Slack runs | OAuth-MCP requires browser-based consent | Users must pre-consent via the web UI. Phase 9 dashboard badge surfaces deferred consents from these runs on next login. |
| Circuit breaker open repeatedly | Transport-level errors on the MCP server (DNS, TLS, 5xx) | Check the per-server error pill; auth errors do not trip the breaker. |
| **`oauth_obo`**: every tool call fails, log shows `obo_misconfigured` | Server row has no Audience, or `obo_grant_profile` is unset/unknown | Set the Audience on the server row; set `[oidc] obo_grant_profile` to `entra` or `rfc8693`. |
| **`oauth_obo`**: `obo_mint_rejected` with `AADSTS65001` | Turnstone's app lacks the (admin-consented) delegated grant to this MCP app — often admin consent that didn't propagate | Grant + admin-consent the delegated permission for this resource; verify it shows *Granted*. See the Entra gotcha above. |
| **`oauth_obo`**: "Sign in to Turnstone again" on one server | Captured credential missing/rejected, or a Conditional Access challenge | User re-logs into Turnstone (re-captures the credential). If it persists, check the IdP grant / CA policy. |
| **`oauth_obo`**: tools don't appear at all for a user | User has not signed in since `capture_user_credential` was enabled (no credential captured) | User logs out and back in via OIDC so the refresh credential is captured. |

See also: `docs/operations/mcp-oauth-headless.md` for the cron / channel-driven run caveat.
