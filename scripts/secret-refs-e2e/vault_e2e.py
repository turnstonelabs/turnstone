"""Live end-to-end exercise of ``secret://`` references against a Vault-compatible store.

Run by ``vault_e2e.sh``, which starts an ephemeral Vault or OpenBao development server,
configures it with the server's own CLI and writes a config.toml that holds only ``[secrets]``.
Drives the REAL code: ``SecretResolver`` and the Vault backend, ``load_model_registry`` over a
SQLite store, and the MCP client's header resolution. The checks read the backend's private
token state to see which logins happened; the store itself answers for what a token can do.

  V1 approle login from config.toml, and a KV v2 field read; the token's lease comes from the
     login's answer
  V2 a missing field and a missing secret are definitive refusals
  V3 jwt login with a workload token the store verifies against a public key
  V4 a denied path: one re-login, a definitive refusal, and the replaced token revoked at the
     store while the new one keeps working
  V5 a token whose lease has ended is replaced by a login before the read, not after a 403
  V6 rotation: the cached value until invalidate(), the new version after it
  V7 the process-wide resolver from config.toml: a model registry loaded from SQLite gets the
     value and leaves out the definition the store denies; an MCP header resolves
  V8 a field written from a file keeps the file's trailing newline in the store and is read
     without it; a field with a trailing space is refused without being quoted, and the model
     registry leaves its definition out
  V9 a sealed store: the last value is served, a reference never resolved is refused as
     retryable, and it resolves once the store is unsealed

Env (set by vault_e2e.sh): TURNSTONE_CONFIG, STORE_FLAVOR, STORE_ADDR, STORE_ROOT_TOKEN,
STORE_CONTAINER, STORE_CLI, STORE_UNSEAL_KEY, STORE_WORK, SHORT_ROLE_ID, SHORT_SECRET_ID.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx2
import jwt

from turnstone.core.config import config_file_path, load_config
from turnstone.core.mcp_client import MCPClientManager
from turnstone.core.model_registry import load_model_registry
from turnstone.core.secret_refs import SecretResolveError, SecretResolver, get_resolver
from turnstone.core.secret_refs._config import parse_secrets_config
from turnstone.core.storage._sqlite import SQLiteBackend

RESULTS: list[tuple[str, str]] = []
ENV: dict[str, str] = {}

OPENAI = "secret://vault/turnstone/openai#api_key"
MCP = "secret://vault/turnstone/mcp#authorization"
DENIED = "secret://vault/other/denied#api_key"
NEWLINE = "secret://vault/turnstone/newline#api_key"
PADDED = "secret://vault/turnstone/padded#api_key"


def record(ok: bool, msg: str) -> None:
    status = "VERIFIED" if ok else "FAILED"
    RESULTS.append((status, msg))
    print(f"[{status:>8}] {msg}")


def refusal(resolver: SecretResolver, reference: str) -> SecretResolveError | None:
    """The resolver's refusal of *reference*, or None when it resolved."""
    try:
        resolver.fetch(reference)
    except SecretResolveError as exc:
        return exc
    return None


def store_cli(*args: str) -> str:
    """Run the store's CLI in the container as root, as an operator would."""
    addr = "http://127.0.0.1:8200"
    root = ENV["STORE_ROOT_TOKEN"]
    command = ["docker", "exec", "-e", f"VAULT_ADDR={addr}", "-e", f"BAO_ADDR={addr}"]
    command += ["-e", f"VAULT_TOKEN={root}", "-e", f"BAO_TOKEN={root}"]
    command += [ENV["STORE_CONTAINER"], ENV["STORE_CLI"], *args]
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout


def token_status(token: str) -> int:
    """The store's answer to a token lookup with *token*: 200 while the token is valid."""
    response = httpx2.get(
        f"{ENV['STORE_ADDR']}/v1/auth/token/lookup-self",
        headers={"X-Vault-Token": token},
        timeout=5,
        trust_env=False,  # the store is on loopback; a proxy in the environment must not answer
    )
    return response.status_code


def raw_field(path: str, field: str) -> str:
    """A field as the store holds it, read with the root token, outside Turnstone."""
    response = httpx2.get(
        f"{ENV['STORE_ADDR']}/v1/secret/data/{path}",
        headers={"X-Vault-Token": ENV["STORE_ROOT_TOKEN"]},
        timeout=5,
        trust_env=False,
    )
    response.raise_for_status()
    value: str = response.json()["data"]["data"][field]
    return value


def vault_resolver(**vault: Any) -> SecretResolver:
    """A resolver over the store with the given login, beside the one config.toml configures."""
    section = {"address": ENV["STORE_ADDR"], **vault}
    return SecretResolver(parse_secrets_config({"vault": section}))


def backend(resolver: SecretResolver) -> Any:
    return resolver._backends["vault"]


def v1_approle_read(resolver: SecretResolver) -> None:
    value = resolver.fetch(OPENAI)
    b = backend(resolver)
    lease_left = b._expires_at - time.monotonic() if b._expires_at is not None else -1.0
    ok = value == "sk-e2e-v1" and b._logins_done == 1 and 3500 < lease_left <= 3600
    record(
        ok,
        f"V1 approle login + KV v2 read: value_ok={value == 'sk-e2e-v1'} "
        f"logins={b._logins_done} lease_left={lease_left:.0f}s (token_ttl 1h)",
    )


def v2_definitive_refusals(resolver: SecretResolver) -> None:
    field = refusal(resolver, "secret://vault/turnstone/openai#no_such_field")
    absent = refusal(resolver, "secret://vault/turnstone/absent#api_key")
    ok = (
        field is not None
        and "is not present" in str(field)
        and not field.retryable
        and absent is not None
        and "not found" in str(absent)
        and not absent.retryable
    )
    record(ok, f"V2 definitive refusals: missing field={field!s} | missing secret={absent!s}")


def v3_jwt_login() -> None:
    work = Path(ENV["STORE_WORK"])
    now = int(time.time())
    claims = {"sub": "turnstone-e2e", "aud": "turnstone-e2e", "iat": now, "nbf": now - 5}
    token = jwt.encode(
        {**claims, "exp": now + 600}, (work / "jwt.key").read_text(), algorithm="RS256"
    )
    jwt_file = work / "workload.jwt"
    jwt_file.write_text(token + "\n")  # projected tokens end with a newline; the login strips it
    resolver = vault_resolver(auth="jwt", role="turnstone", jwt_path=str(jwt_file))
    try:
        value = resolver.fetch(OPENAI)
        logins = backend(resolver)._logins_done
    except SecretResolveError as exc:
        record(False, f"V3 jwt login: {exc}")
        return
    finally:
        resolver.close()
    record(value == "sk-e2e-v1" and logins == 1, f"V3 jwt login: value_ok={value == 'sk-e2e-v1'}")


def v4_denied_path(resolver: SecretResolver) -> None:
    b = backend(resolver)
    replaced, logins_before = b._token, b._logins_done
    refused = refusal(resolver, DENIED)
    fresh = b._token
    replaced_status, fresh_status = token_status(replaced), token_status(fresh)
    still_reads = resolver.fetch("secret://vault/turnstone/rotating#api_key") == "sk-rotate-v1"
    ok = (
        refused is not None
        and "permission denied" in str(refused)
        and not refused.retryable
        and b._logins_done == logins_before + 1
        and fresh != replaced
        and replaced_status == 403
        and fresh_status == 200
        and still_reads
    )
    record(
        ok,
        f"V4 denied path: refusal={refused!s} re-logins={b._logins_done - logins_before} "
        f"replaced token answers {replaced_status} (revoked), new token {fresh_status}, "
        f"permitted paths still read={still_reads}",
    )


def v5_lease_end() -> None:
    resolver = vault_resolver(
        auth="approle", role_id=ENV["SHORT_ROLE_ID"], secret_id=ENV["SHORT_SECRET_ID"]
    )
    b = backend(resolver)
    relogins: list[str] = []
    relogin = b._relogin

    def counting_relogin(token: str) -> str:
        relogins.append(token)
        return str(relogin(token))

    b._relogin = counting_relogin
    try:
        first = resolver.fetch(OPENAI)
        expiring = b._token
        lease = b._expires_at - time.monotonic() if b._expires_at is not None else -1.0
        time.sleep(max(lease, 0.0) + 1.5)
        resolver.invalidate()
        second = resolver.fetch(OPENAI)
    finally:
        resolver.close()
    ok = (
        0 < lease <= 5
        and first == second == "sk-e2e-v1"
        and b._logins_done == 2
        and b._token != expiring
        and not relogins
    )
    record(
        ok,
        f"V5 lease end: lease={lease:.1f}s logins={b._logins_done} "
        f"re-logins after a 403={len(relogins)} (want 0)",
    )


def v6_rotation(resolver: SecretResolver) -> None:
    reference = "secret://vault/turnstone/rotating#api_key"
    before = resolver.fetch(reference)
    store_cli("kv", "put", "secret/turnstone/rotating", "api_key=sk-rotate-v2")
    cached = resolver.fetch(reference)
    resolver.invalidate()
    after = resolver.fetch(reference)
    ok = before == cached == "sk-rotate-v1" and after == "sk-rotate-v2"
    record(ok, f"V6 rotation: before={before} cached={cached} after invalidate={after}")


def v7_v8_process_wide() -> None:
    resolver = get_resolver()  # built from the harness's config.toml
    storage = SQLiteBackend(str(Path(ENV["STORE_WORK"]) / "e2e.db"))
    try:
        for definition_id, alias, api_key in (
            ("d1", "stored", OPENAI),
            ("d2", "denied", DENIED),
            ("d3", "newline", NEWLINE),
            ("d4", "padded", PADDED),
        ):
            storage.create_model_definition(
                definition_id,
                alias,
                "e2e-model",
                base_url="https://llm.example.com/v1",
                api_key=api_key,
                context_window=8192,
            )
        registry = load_model_registry(storage=storage)
    finally:
        storage.close()
    stored = registry.get_config("stored").api_key if "stored" in registry.list_aliases() else ""
    skipped = registry.skipped_aliases
    mcp_cfg = {
        "type": "streamable-http",
        "url": "https://mcp.example.com/mcp",
        "headers": {"Authorization": MCP},
    }
    resolved = asyncio.run(MCPClientManager({})._with_resolved_headers("e2e", mcp_cfg))
    ok = (
        stored == "sk-e2e-v1"
        and "permission denied" in skipped.get("denied", "")
        and resolved["headers"]["Authorization"] == "Bearer mcp-e2e-v1"
        and mcp_cfg["headers"]["Authorization"] == MCP
    )
    record(
        ok,
        f"V7 config.toml resolver, registry and MCP: stored key_ok={stored == 'sk-e2e-v1'} "
        f"denied skipped={skipped.get('denied', '<loaded>')!r} "
        f"mcp header_ok={resolved['headers']['Authorization'] == 'Bearer mcp-e2e-v1'}",
    )

    newline_held = raw_field("turnstone/newline", "api_key")
    aliases = registry.list_aliases()
    newline_key = registry.get_config("newline").api_key if "newline" in aliases else ""
    padded_held = raw_field("turnstone/padded", "api_key")
    refused = refusal(resolver, PADDED)
    reason = skipped.get("padded", "<loaded>")
    ok = (
        newline_held == "sk-e2e-newline\n"
        and newline_key == "sk-e2e-newline"
        and padded_held == "sk-e2e-padded "
        and refused is not None
        and "cannot carry" in str(refused)
        and not refused.retryable
        and "sk-e2e-padded" not in str(refused)
        and "cannot carry" in reason
        and "sk-e2e-padded" not in reason
    )
    record(
        ok,
        f"V8 values a header cannot carry: the store holds the file's newline="
        f"{newline_held.endswith(chr(10))} and the key reads without it="
        f"{newline_key == 'sk-e2e-newline'}; trailing space refusal={refused!s}; "
        f"registry skipped it={'cannot carry' in reason}",
    )


def v9_sealed(resolver: SecretResolver) -> None:
    known = resolver.fetch(OPENAI)
    store_cli("operator", "seal")
    try:
        resolver.invalidate()
        served = resolver.fetch(OPENAI)
        refused = refusal(resolver, MCP)
    finally:
        store_cli("operator", "unseal", ENV["STORE_UNSEAL_KEY"])
    resolver.invalidate()  # clears the cool-down, so the next resolve asks the store
    back = resolver.fetch(MCP)
    ok = (
        served == known == "sk-e2e-v1"
        and refused is not None
        and refused.retryable
        and back == "Bearer mcp-e2e-v1"
    )
    record(
        ok,
        f"V9 sealed store: cached value served={served == known} never-resolved refusal="
        f"{refused!s} (retryable={refused.retryable if refused else None}) "
        f"after unseal={back == 'Bearer mcp-e2e-v1'}",
    )


def main() -> int:
    names = (
        "TURNSTONE_CONFIG",
        "STORE_FLAVOR",
        "STORE_ADDR",
        "STORE_ROOT_TOKEN",
        "STORE_CONTAINER",
        "STORE_CLI",
        "STORE_UNSEAL_KEY",
        "STORE_WORK",
        "SHORT_ROLE_ID",
        "SHORT_SECRET_ID",
    )
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        print(f"Missing env: {', '.join(missing)}; run via vault_e2e.sh")
        return 2
    ENV.update({n: os.environ[n] for n in names})
    if config_file_path() != Path(ENV["TURNSTONE_CONFIG"]):
        print(f"refusing to run: this process would read {config_file_path()}")
        return 2

    print(f"store: {ENV['STORE_FLAVOR']} at {ENV['STORE_ADDR']}")
    resolver = SecretResolver(parse_secrets_config(load_config("secrets")))
    try:
        v1_approle_read(resolver)
        v2_definitive_refusals(resolver)
        v3_jwt_login()
        v4_denied_path(resolver)
        v5_lease_end()
        v6_rotation(resolver)
        v7_v8_process_wide()
        v9_sealed(resolver)
    finally:
        resolver.close()

    print(f"\n=== summary ({ENV['STORE_FLAVOR']}) ===")
    for status, msg in RESULTS:
        print(f"  {status:>8}  {msg}")
    return 0 if all(s == "VERIFIED" for s, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
