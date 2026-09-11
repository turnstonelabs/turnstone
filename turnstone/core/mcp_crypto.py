"""MCP secret storage, decrypt-failure auditing, and host keyring startup policy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from turnstone.core.audit import record_audit
from turnstone.core.log import get_logger
from turnstone.core.storage._protocol import USER_SCOPED_AUTH_TYPES
from turnstone.core.token_store import crypto as token_store_crypto
from turnstone.core.token_store.store import TokenStore

if TYPE_CHECKING:
    from turnstone.core.storage._protocol import StorageBackend
log = get_logger(__name__)


class MCPTokenStore(TokenStore):
    """Compose generic encrypted tokens with MCP secrets and audit subjects."""

    def __init__(
        self,
        storage: StorageBackend,
        cipher: token_store_crypto.TokenCipher,
        *,
        node_id: str = "",
        audit_storage: StorageBackend | None = None,
    ) -> None:
        self._node_id = node_id
        self._audit_storage = audit_storage
        super().__init__(storage, cipher, on_decrypt_failure=self._audit_decrypt_failure)

    def set_oauth_client_secret(self, server_id: str, plaintext_secret: str | None) -> bool:
        """Encrypt plaintext and persist via the dedicated storage writer.

        Pass ``None`` to clear the column.  Empty string is encrypted
        normally (Fernet accepts empty plaintext); callers that treat
        empty as "clear" must convert to ``None`` at their API boundary
        first — the admin form does this before invoking the helper.

        Returns ``False`` when ``server_id`` does not exist.
        """
        if plaintext_secret is None:
            return self._storage.set_mcp_oauth_client_secret_ct(server_id, None)
        secret_ct = self._cipher.encrypt(plaintext_secret.encode("utf-8"))
        return self._storage.set_mcp_oauth_client_secret_ct(server_id, secret_ct)

    def get_oauth_client_secret(self, server_id: str) -> str | None:
        """Decrypt and return the per-server OAuth client secret, or None.

        Returns ``None`` when the row is missing or the column is NULL.
        Raises :class:`TokenDecryptError` on key mismatch — the caller
        decides whether to treat that as a missing-secret case (e.g. log +
        prompt re-consent) or surface as a configuration failure.
        """
        secret_ct = self._storage.get_mcp_oauth_client_secret_ct(server_id)
        if secret_ct is None:
            return None
        return self._cipher.decrypt(secret_ct).decode("utf-8")

    def _audit_decrypt_failure(self, server_name: str, fingerprints: tuple[str, ...]) -> None:
        """Best-effort audit emit on decrypt failure (no-op when unconfigured).

        Uses ``server_id`` (PK UUID) as ``resource_id`` so admin-driven
        server renames don't break event correlation. Falls back to
        ``server_name`` when the lookup misses.
        """
        if self._audit_storage is None:
            return
        resource_id = server_name
        try:
            row = self._audit_storage.get_mcp_server_by_name(server_name)
        except Exception:
            row = None
        if row is not None:
            resource_id = str(row.get("server_id") or server_name)
        try:
            record_audit(
                self._audit_storage,
                user_id="",
                action="mcp_server.oauth.token_decrypt_failure",
                resource_type="mcp_server",
                resource_id=resource_id,
                detail={
                    "server_name": server_name,
                    "key_fingerprints_attempted": list(fingerprints),
                    "node_id": self._node_id,
                },
            )
        except Exception:
            log.warning(
                "mcp_server.oauth.audit_emit_failed",
                action="token_decrypt_failure",
                server_name=server_name,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Lifespan integration
# ---------------------------------------------------------------------------


def initialize_mcp_crypto_state(app_state: object, *, node_id: str = "") -> None:
    """Validate Fernet key config + install :class:`TokenCipher` /
    :class:`MCPTokenStore` on ``app_state``.

    Called from the server / console lifespan after OIDC initialization.

    Behavior:

    1. ``token_store_crypto.load_token_cipher_config()`` — wrapped in try/except. Raises
       :class:`SystemExit(1)` on :class:`TokenKeyConfigError` after
       logging.
    2. Counts ``mcp_servers`` rows with a user-scoped ``auth_type``
       (``oauth_user`` or ``oauth_obo``; see ``USER_SCOPED_AUTH_TYPES``). If
       any exist AND no key is configured, raises ``SystemExit(1)``.
       Same enforcement when ``[oidc] capture_user_credential`` is
       enabled (the captured IdP credential must be encrypted at rest),
       and when the host's model registry holds a dynamic-auth alias
       (``entra_obo``/``entra_app`` mint-cache rows are encrypted with
       the same cipher). The console's registry loads later; its
       equivalent check lives in the coordinator bootstrap.
    3. On success, sets ``app_state.mcp_token_cipher`` and
       ``app_state.mcp_token_store`` (both possibly ``None`` when no
       key + no user-scoped rows).

    The helper is shared by ``turnstone/server.py:_lifespan`` and
    ``turnstone/console/server.py:_lifespan``.  A separate
    :func:`close_mcp_crypto_state` mirrors :func:`close_oidc_state` for
    parity even though the cipher itself owns no resources.
    """
    from turnstone.core.storage import get_storage

    try:
        cipher_cfg = token_store_crypto.load_token_cipher_config()
    except token_store_crypto.TokenKeyConfigError as exc:
        log.error("mcp_server.oauth.key_config_invalid: %s", exc)
        raise SystemExit(1) from exc

    storage = get_storage()
    # Both pool-backed types persist encrypted per-user rows (oauth_user:
    # tokens + refresh; oauth_obo: minted-token cache), so both force the
    # key requirement — USER_SCOPED_AUTH_TYPES is the single source of truth
    # (defined in the storage protocol and consumed by MCP policy).
    user_scoped_count = sum(
        1 for row in storage.list_mcp_servers() if row.get("auth_type") in USER_SCOPED_AUTH_TYPES
    )
    # Dynamic model auth needs the same cipher: both mints persist encrypted
    # cache rows. The REGISTRY is the only truthful oracle — config.toml
    # overrides the DB for a same-named alias, so a raw ``model_definitions``
    # probe would demand a key for a row the registry resolves as static, and
    # SystemExit on a false positive bricks every host. Nodes have a registry
    # before this runs; the console does NOT, so its equivalent enforcement
    # lives in ``_load_and_bootstrap_coord_subsystem``, which re-checks after
    # its registry loads and reports through ``coord_registry_error``.
    model_registry = getattr(app_state, "registry", None) or getattr(
        app_state, "coord_registry", None
    )
    dynamic_model_auth = bool(
        model_registry is not None
        and hasattr(model_registry, "has_dynamic_auth")
        and model_registry.has_dynamic_auth()
    )

    if (user_scoped_count > 0 or dynamic_model_auth) and cipher_cfg is None:
        log.error(
            "mcp.oauth: %d user-scoped MCP server(s), dynamic_model_auth=%s, but %s",
            user_scoped_count,
            dynamic_model_auth,
            token_store_crypto.STARTUP_KEY_REQUIRED_HINT,
        )
        raise SystemExit(1)

    # Same enforcement for single-credential capture (issue #551): the
    # captured IdP refresh token must never be persisted unencrypted. This is
    # gated ONLY on the operator's ``capture_user_credential`` opt-in — NOT on
    # ``oidc_config.enabled``. Enabled reflects whether OIDC *discovery*
    # succeeded, which is transient: a node that boots while the IdP is briefly
    # unreachable comes up enabled=False (discovery_retryable=True), and
    # runtime rediscovery re-enables OIDC later — at which point the very first
    # login's capture step would persist a refresh token. Gating the key
    # requirement on ``enabled`` would silently skip the loud boot-time failure
    # exactly when the IdP is down at boot, then quietly no-op capture forever
    # once OIDC heals. The opt-in flag is a static config value (preserved
    # across the discovery-failure ``dataclasses.replace``), so keying on it
    # makes the requirement independent of discovery state. Runs after OIDC
    # init (see docstring), so app_state.oidc_config is set.
    oidc_config = getattr(app_state, "oidc_config", None)
    if (
        oidc_config is not None
        and getattr(oidc_config, "capture_user_credential", False)
        and cipher_cfg is None
    ):
        log.error(
            "oidc.capture: [oidc] capture_user_credential is enabled but %s",
            token_store_crypto.STARTUP_KEY_REQUIRED_HINT,
        )
        raise SystemExit(1)

    if cipher_cfg is None:
        # No oauth_user rows + no key configured: zero new code paths
        # exercised; install None sentinels so callers can fast-path.
        app_state.mcp_token_cipher = None  # type: ignore[attr-defined]
        app_state.mcp_token_store = None  # type: ignore[attr-defined]
        log.debug("mcp_server.oauth.disabled (no key configured, no oauth_user rows)")
        return

    cipher = token_store_crypto.TokenCipher(cipher_cfg)
    app_state.mcp_token_cipher = cipher  # type: ignore[attr-defined]
    app_state.mcp_token_store = MCPTokenStore(  # type: ignore[attr-defined]
        storage,
        cipher,
        node_id=node_id,
        audit_storage=storage,
    )
    log.info(
        "mcp_server.oauth.cipher_installed",
        keys=len(cipher.key_fingerprints),
        active_fp=cipher.key_fingerprints[0],
    )


def close_mcp_crypto_state(app_state: object) -> None:
    """Drop references to the cipher / token store on shutdown.

    Mirrors :func:`turnstone.core.oauth.oidc.close_oidc_state` for parity.
    The cipher itself owns no network resources, so this is a simple
    attribute clear.
    """
    if hasattr(app_state, "mcp_token_store"):
        app_state.mcp_token_store = None
    if hasattr(app_state, "mcp_token_cipher"):
        app_state.mcp_token_cipher = None
