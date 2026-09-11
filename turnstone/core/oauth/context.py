"""Typed process state shared by OAuth consumers and host authentication."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncio

    import httpx

    from turnstone.core.oauth.oidc import OIDCConfig
    from turnstone.core.oauth.runtime import OAuthRuntime
    from turnstone.core.oauth.tokens import _RefreshBackoffState
    from turnstone.core.storage._protocol import StorageBackend
    from turnstone.core.token_store.crypto import TokenCipher
    from turnstone.core.token_store.store import TokenStore, UserTokenPlain


@dataclass
class TokenCoordination:
    """Locks and backoff owned by one event loop, never shared across loops."""

    locks: dict[tuple[str, str], asyncio.Lock] = field(default_factory=dict)
    backoff: dict[tuple[str, str], _RefreshBackoffState] = field(default_factory=dict)
    loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)


@dataclass
class OAuthContext:
    """One host's authoritative configuration, storage and OAuth-loop state.

    Hosts replace the immutable OIDC configuration here after discovery. The
    OAuth runtime reads this same holder, including after browser-side recovery.
    Browser/JWKS clients and MCP transport/state remain with their own hosts.
    """

    storage: StorageBackend | None = None
    token_store: TokenStore | None = None
    cipher: TokenCipher | None = None
    oidc_config: OIDCConfig | None = None
    rediscover_gate: threading.Lock = field(default_factory=threading.Lock, repr=False)
    rediscover_last: float | None = None
    coordination: TokenCoordination = field(default_factory=TokenCoordination)
    token_memo: dict[tuple[str, str], UserTokenPlain] = field(default_factory=dict)
    metadata_cache: dict[str, Any] = field(default_factory=dict)
    http_client: httpx.AsyncClient | None = field(default=None, repr=False)
    runtime: OAuthRuntime | None = field(default=None, repr=False)


_context_create_lock = threading.Lock()


def oauth_context(host: Any) -> OAuthContext:
    """Return the host's shared holder, creating inert state before first use."""
    if isinstance(host, OAuthContext):
        return host
    with _context_create_lock:
        context = getattr(host, "oauth_context", None)
        if not isinstance(context, OAuthContext):
            context = OAuthContext(storage=getattr(host, "auth_storage", None))
            host.oauth_context = context
        elif context.storage is None:
            context.storage = getattr(host, "auth_storage", None)
        return context
