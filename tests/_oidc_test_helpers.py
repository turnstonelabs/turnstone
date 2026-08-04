"""Shared OIDC posture builder for the model-auth / OBO test surface.

One construction site for the posture the mint and write-validator suites
read, built as a REAL (frozen) ``OIDCConfig`` so an override for a field
the dataclass does not carry raises at the call site. Named with a leading
underscore so pytest does not collect it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from turnstone.core.oidc import OIDCConfig

if TYPE_CHECKING:
    from collections.abc import Iterator

# The issuer / token-endpoint pair the mint suites route their mock
# transports on.
ISSUER = "https://idp.test"
TOKEN_ENDPOINT = "https://idp.test/token"


def make_oidc_config(**overrides: Any) -> OIDCConfig:
    """A full, mintable OIDC posture; tests override the field under test,
    everything else rides the dataclass defaults."""
    defaults: dict[str, Any] = {
        "enabled": True,
        "issuer": ISSUER,
        "client_id": "cid",
        "client_secret": "csecret",
        "token_endpoint": TOKEN_ENDPOINT,
    }
    defaults.update(overrides)
    return OIDCConfig(**defaults)


def keyed_app_state() -> SimpleNamespace:
    """App-state stub satisfying ``ModelRegistry.reload``'s dynamic-auth key
    guard, for suites exercising reload mechanics rather than key policy."""
    return SimpleNamespace(mcp_token_store=object())


def mint_warn_state_reset() -> Iterator[None]:
    """Reset generator behind the mint suites' autouse fixtures: empties the
    process-global mint warn/dedup/cause state before AND after each test,
    so warn-dedup assertions are not order-dependent. Modules install it as
    ``yield from mint_warn_state_reset()`` in an autouse fixture.
    """
    # Lazy import: non-mint consumers of this helper module (the write-
    # validator suites) shouldn't pay the mcp_oauth import.
    from turnstone.core.mcp_oauth import reset_model_mint_warn_state_for_tests

    reset_model_mint_warn_state_for_tests()
    yield
    reset_model_mint_warn_state_for_tests()
