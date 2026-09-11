# Shared OAuth storage

MCP authorization and dynamic model authentication use the same encrypted token
store and deployment keyring. Configure the keyring on every process that reads
or writes the shared database, including the console and all server nodes.
Provider setup remains in the [MCP OAuth](mcp-oauth.md) and [OIDC](oidc.md) guides.

## Deployment keyring

Generate a Fernet key in an environment with Turnstone's dependencies installed:

```sh
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Store it in the shared bootstrap `config.toml`:

```toml
[security]
mcp_token_encryption_key = "base64-fernet-key"
# For rotation, use an ordered list instead:
# mcp_token_encryption_keys = ["new-key", "old-key"]
```

These configuration names remain unchanged even when only model authentication
uses the store. They are accepted only from `config.toml`, not environment
variables or database-backed runtime settings. A nonempty plural list takes
precedence over the singular key. The first key encrypts new writes; all keys
are tried when reading existing ciphertext. Rows carry no key identifier.
Restart each process after changing its bootstrap file.

Credential capture and user-scoped MCP servers require the key at startup.
Dynamic model authentication requires it too: a node without the required key
refuses to start, while a console whose model registry needs it withholds the
coordinator subsystem and reports the configuration error.

Retain a private backup of the keyring separately from database backups. Losing
the necessary key makes stored tokens, captured credentials, and MCP client
secrets unreadable. A decryption failure preserves the row so restoring the
correct keyring can recover access.

Keep the file outside the mounted workspace when tools should not read service
credentials. File-based configuration avoids inherited environment variables,
but a shell running as the same OS user can still read the file. A read-only
container mount prevents changes, not reads; use filesystem/process isolation
where that boundary matters. See [Docker bootstrap config](docker.md#shared-bootstrap-config).

## Key rotation

1. Distribute `["old-key", "new-key"]` to every reader and writer, and restart
   them. Writes still use the old key while every process learns the new one.
2. Once all processes have both keys, promote the new key with
   `["new-key", "old-key"]` and restart them. During this rollout every process
   can read ciphertext written with either key.
3. Keep the old key until every ciphertext that must remain readable has been
   replaced or removed. This includes rarely used refresh credentials, cached
   tokens, and stored MCP client secrets. Retained database backups may also
   need the old key.

Loading a new keyring or reading a row does **not** re-encrypt it. New captures,
token writes, and client-secret updates use the first key, but there is no
automatic full-store re-encryption pass. The storage rename in migration 077
does not rewrite ciphertext either. Do not retire the old key merely because
all processes have restarted successfully.

## Stored credentials and token identities

| Storage | Contents and identity |
| --- | --- |
| `oauth_tokens` | Encrypted access tokens and optional refresh tokens, keyed by `(user_id, token_key)`. MCP uses the server name as the key. Model caches use reserved `__model_obo__:` and `__model_app__:` keys derived from the owning model alias. `token_key` is an opaque identity; audience and scopes are separate fields. |
| `oidc_user_credentials` | One encrypted captured IdP refresh credential per `(user_id, issuer)`, shared by delegated MCP and model mints. |
| `mcp_servers.oauth_client_secret_ct` | An MCP server's optional encrypted OAuth client secret, using the same keyring. |
| `mcp_oauth_pending` | Short-lived MCP browser authorization state with a ten-minute TTL. This is separate from `mcp_pending_consent`, which records consent needed by non-interactive work. |

An `oauth_user` token row holds a user's per-server grant. An `oauth_obo` or
dynamic model row is a mint cache; deleting it permits a later mint from the
surviving credential. App-identity model rows belong to the shared `__app__`
pseudo-user. Legacy synthetic keys retain their existing values through the
rename. Access-token expiry and refresh behavior are unchanged; the rename
does not add a cache-cleanup sweep.

## Credential lifecycle

- **Login capture:** enabling `[oidc] capture_user_credential` requests an offline
  credential during sign-in and stores it encrypted. Disabling capture stops
  new captures; it does not revoke stored credentials. Delegated MCP and model
  mints share the captured credential and persist refresh-token rotation.
- **MCP disconnect:** the user's connections page deletes the local `oauth_user`
  grant and attempts upstream revocation. Sign-in passthrough and model mint
  caches are excluded from that page. MCP admin purges delete token rows and
  pending browser authorization states in one transaction; that purge does not
  delete pending-consent rows or captured OIDC credentials.
- **Model edits and deletion:** changes to alias, audience, scopes, or auth mode
  purge the affected alias's delegated and app token caches. Model purges delete
  only token rows; they leave MCP authorization states and captured credentials
  alone. Other aliases' tokens survive.
- **Identity unlink:** unlinking an OIDC identity deletes its captured credential,
  purges the user's delegated MCP/model token caches, and requests model-memo
  eviction on the console and registered nodes. Independent `oauth_user` grants
  and shared app-identity caches survive. Existing upstream access tokens may
  remain valid until expiry; the identity provider governs downstream access.
- **User deletion:** deletes that user's database token rows and captured
  credentials. App-identity rows under `__app__` belong to the application and
  survive another user's deletion.

## Migration 077

Migration 077 renames `mcp_user_tokens` to `oauth_tokens` and its `server_name`
column to `token_key`. It preserves keys, ciphertext, expiry, and timestamps on
SQLite and PostgreSQL, with matching downgrade support. The MCP HTTP API still
uses `server_name` in its connections responses and routes.

Processes running older code cannot query the renamed table until their code
is upgraded. During an upgrade, OAuth operations on those processes can fail
between migration and process replacement. See the [changelog](../CHANGELOG.md)
for the release migration notice.
