"""Opt-in live check of the Vault-compatible backend against a real store.

Skipped unless ``TURNSTONE_LIVE_VAULT_ADDR`` is set. The store needs a KV v2
mount (default ``secret``) holding the secret named by
``TURNSTONE_LIVE_VAULT_PATH`` (default ``turnstone/openai``) with the field
``TURNSTONE_LIVE_VAULT_FIELD`` (default ``api_key``), and an approle whose
``TURNSTONE_LIVE_VAULT_ROLE_ID`` / ``TURNSTONE_LIVE_VAULT_SECRET_ID`` may read it.
``TURNSTONE_LIVE_VAULT_EXPECT`` pins the expected value when set. Run it against
both Vault and OpenBao development servers before changing the wire handling.
"""

from __future__ import annotations

import os

import pytest

from turnstone.core.secret_refs import SecretResolveError, SecretResolver
from turnstone.core.secret_refs._config import parse_secrets_config

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("TURNSTONE_LIVE_VAULT_ADDR"),
        reason="set TURNSTONE_LIVE_VAULT_ADDR (and the approle variables) to run",
    ),
]


def _resolver() -> SecretResolver:
    cfg = parse_secrets_config(
        {
            "vault": {
                "address": os.environ["TURNSTONE_LIVE_VAULT_ADDR"],
                "auth": "approle",
                "role_id": os.environ["TURNSTONE_LIVE_VAULT_ROLE_ID"],
                "secret_id": os.environ["TURNSTONE_LIVE_VAULT_SECRET_ID"],
                "mount": os.environ.get("TURNSTONE_LIVE_VAULT_MOUNT", "secret"),
            }
        }
    )
    return SecretResolver(cfg)


def test_reads_a_kv2_field_and_reports_a_missing_one() -> None:
    resolver = _resolver()
    path = os.environ.get("TURNSTONE_LIVE_VAULT_PATH", "turnstone/openai")
    field = os.environ.get("TURNSTONE_LIVE_VAULT_FIELD", "api_key")
    try:
        value = resolver.resolve(f"secret://vault/{path}#{field}")
        expected = os.environ.get("TURNSTONE_LIVE_VAULT_EXPECT")
        if expected is not None:
            assert value == expected
        assert value
        with pytest.raises(SecretResolveError, match="not present"):
            resolver.resolve(f"secret://vault/{path}#no-such-field-{field}")
        with pytest.raises(SecretResolveError, match="not found") as info:
            resolver.resolve("secret://vault/turnstone/does-not-exist#x")
        assert info.value.retryable is False
    finally:
        resolver.close()
