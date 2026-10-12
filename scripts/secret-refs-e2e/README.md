# secret-refs e2e: `secret://` references against Vault and OpenBao

`vault_e2e.sh` starts an ephemeral development server (in memory, nothing kept) of Vault and
then of OpenBao, configures each with the server's own CLI, runs `vault_e2e.py` against the
real Turnstone code, runs the opt-in live test (`tests/test_secret_refs_live.py`) against the
same server, and removes the container. Headless, about half a minute per store once the
images are pulled. Manual test tooling, not run in CI.

```bash
./scripts/secret-refs-e2e/vault_e2e.sh            # both stores
./scripts/secret-refs-e2e/vault_e2e.sh openbao    # one of them
```

Needs docker and openssl on the host. The images are pinned (`hashicorp/vault:2.1.2`,
`openbao/openbao:2.5.0`; `VAULT_IMAGE` / `OPENBAO_IMAGE` override them), and the server listens on
`127.0.0.1:8210` (`PORT`). The driver runs on `PYTHON` when it is set, otherwise on the repo's
`.venv/bin/python`, otherwise on `uv run --extra test python`.

The process reads a config.toml the script writes into a private temporary directory (only
`[secrets]`, with an approle login), through `TURNSTONE_CONFIG`. The driver refuses to run when
the config loader would read any other file, so an operator's own config.toml is never read.

## What the store holds

- KV v2 at the development server's `secret/` mount: `turnstone/openai` (`api_key`),
  `turnstone/mcp` (`authorization`, a whole `Bearer ...` value), `turnstone/rotating`,
  `turnstone/newline` (written with `api_key=@file` from a file that ends in a newline),
  `turnstone/padded` (a value with a trailing space), and `other/denied`, outside the policy.
- Policy `turnstone-read`: read on `secret/data/turnstone/*` only.
- approle roles `turnstone` (token_ttl 1h) and `turnstone-short` (token_ttl 5 s); jwt auth that
  trusts a key pair generated for the run, with role `turnstone` (audience `turnstone-e2e`).

## Checks

- **V1** approle login from config.toml and a KV v2 field read; the token's lease comes from
  the login's answer (about 3600 s).
- **V2** a missing field and a missing secret are definitive (not retryable) refusals.
- **V3** jwt login with a workload token the store verifies against the public key.
- **V4** a denied path: exactly one re-login, a definitive refusal, the replaced token revoked
  at the store (its `lookup-self` answers 403) and the new one valid, permitted paths still
  read.
- **V5** a token whose lease has ended is replaced by a login before the read, with no 403
  round trip.
- **V6** rotation: the cached value inside the TTL, the new KV version after `invalidate()`.
- **V7** the process-wide resolver built from config.toml: `load_model_registry` over SQLite
  gets the value and leaves out the denied definition with the reason; an MCP header resolves
  while the stored config keeps the reference.
- **V8** `field=@file` keeps the file's trailing newline in the store, and the key reads without
  it (both backends drop one trailing line break); a value with a trailing space, which no header
  can carry, is refused without being quoted, and the registry leaves its definition out.
- **V9** a sealed store: the last value is served, a reference never resolved is refused as
  retryable, and it resolves after the unseal.

The checks read the backend's private token state (`_token`, `_logins_done`, `_expires_at`) to
see which logins happened; whether a token is still valid is asked of the store.

## Results

RUN 2026-10-11, ALL VERIFIED on Vault 2.1.2 and OpenBao 2.5.0: V1–V9, and the live test passed
against each. V4 was the first live run of the revoke after a re-login
(`POST /v1/auth/token/revoke-self`): the replaced token answers 403 on both stores. V8
confirmed that a field written from a file keeps the file's trailing newline in the store, which
is why the Vault backend now drops one, as the file backend does.

Negative controls, Vault, each an in-memory mutant run through the script (`PYTHON` pointed at
a wrapper that patches the code before running the driver): a resolver that refuses only empty
values fails V8; a Vault backend that keeps the trailing newline fails V8; a backend that never
revokes fails V4; a backend that ignores the lease fails V1 and V5.

Gotchas: the development banner with the unseal key is printed a moment after the API answers,
so the script polls the log for it. `SKIP_SETCAP=true` lets the images start without the
`IPC_LOCK` capability.
