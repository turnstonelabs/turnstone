"""Token-at-rest encryption for OAuth-MCP.

Uses cryptography.fernet (AES-128-CBC + HMAC-SHA256, 256-bit total key
material, encrypt-then-MAC). Single-key chosen for v1; rotation supported
via cryptography.fernet.MultiFernet.

Operator note: when rotating keys, place the NEW key first in
``mcp_token_encryption_keys``. MultiFernet writes with the first key and
tries each in order on read. Old keys can be retired once all rows are
re-encrypted by a future operator-driven migration.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from turnstone.core.audit import record_audit
from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from turnstone.core.storage._protocol import StorageBackend

log = get_logger(__name__)

# Constants
_KEY_BYTES = 32  # Fernet requires 32 bytes
_KEY_FINGERPRINT_BYTES = 8  # short hex prefix for audit/error fields

# Operator-facing hint for malformed/missing keys.
_KEY_GEN_HINT = (
    "regenerate with: python -c 'from cryptography.fernet import Fernet; "
    "print(Fernet.generate_key().decode())'"
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MCPCryptoError(Exception):
    """Base class for MCP token-at-rest encryption errors."""


class MCPTokenDecryptError(MCPCryptoError):
    """No installed key can decrypt the ciphertext.

    Maps to RFC's ``mcp_token_undecryptable_key_unknown`` error class.
    Carries ``key_fingerprints_attempted: tuple[str, ...]`` for audit.

    Critical: callers MUST NOT auto-delete the row on this error.
    The row is still valid; this node just doesn't have the right key.
    """

    def __init__(self, message: str, *, key_fingerprints_attempted: tuple[str, ...]) -> None:
        super().__init__(message)
        self.key_fingerprints_attempted = key_fingerprints_attempted


class MCPTokenKeyConfigError(MCPCryptoError):
    """Key material in config.toml is malformed or missing."""


# ---------------------------------------------------------------------------
# Plaintext shape returned by ``MCPTokenStore.get_user_token``
# ---------------------------------------------------------------------------


class MCPUserTokenPlain(TypedDict):
    """Plaintext shape returned by ``MCPTokenStore.get_user_token``.

    Mirrors ``MCPUserToken`` (storage row shape) minus the ``_ct`` suffix
    on token columns and with plaintext bytes-decoded values.
    """

    user_id: str
    server_name: str
    access_token: str
    refresh_token: str | None
    expires_at: str | None
    scopes: str | None
    as_issuer: str
    audience: str
    created: str
    last_refreshed: str | None


# ---------------------------------------------------------------------------
# Config dataclass + loader
# ---------------------------------------------------------------------------


@dataclass(frozen=True, repr=False)
class MCPTokenCipherConfig:
    """Validated key material loaded from config.toml.

    ``keys`` are raw 32-byte secrets; the first is the encryption key,
    all are tried in order on read. The cipher wrapper re-encodes them
    via ``base64.urlsafe_b64encode`` for ``Fernet(...)`` at construction
    time.

    ``__repr__`` is overridden to redact the raw key bytes — the default
    dataclass repr would emit them verbatim into logs / tracebacks.
    """

    keys: tuple[bytes, ...]

    def __repr__(self) -> str:
        return f"MCPTokenCipherConfig(keys=<{len(self.keys)} key(s) redacted>)"


def _validate_key(raw: str, *, label: str) -> bytes:
    """Decode + validate a single base64 url-safe key. Raises ``MCPTokenKeyConfigError``."""
    if not isinstance(raw, str) or not raw.strip():
        raise MCPTokenKeyConfigError(f"{label}: key is empty or not a string. {_KEY_GEN_HINT}")
    try:
        decoded = base64.urlsafe_b64decode(raw.encode("ascii"))
    except Exception as exc:
        raise MCPTokenKeyConfigError(
            f"{label}: not valid base64 url-safe ({exc}). {_KEY_GEN_HINT}"
        ) from exc
    if len(decoded) != _KEY_BYTES:
        raise MCPTokenKeyConfigError(
            f"{label}: decoded key must be exactly {_KEY_BYTES} bytes, "
            f"got {len(decoded)}. {_KEY_GEN_HINT}"
        )
    return decoded


def _key_fingerprint(key: bytes) -> str:
    """Stable, non-reversible 8-hex prefix of SHA-256(key)."""
    digest = hashlib.sha256(key).hexdigest()
    return digest[: _KEY_FINGERPRINT_BYTES * 2]


def load_mcp_token_cipher_config() -> MCPTokenCipherConfig | None:
    """Read ``[security] mcp_token_encryption_keys`` (plural) or
    ``mcp_token_encryption_key`` (singular) from config.toml.

    Plural takes precedence when both are present. Returns ``None`` when
    neither key is configured (caller decides whether that's fatal).
    Raises ``MCPTokenKeyConfigError`` on malformed key material.
    """
    from turnstone.core.config import load_config

    sec_cfg = load_config("security")
    raw_list_value = sec_cfg.get("mcp_token_encryption_keys")
    raw_single_value = sec_cfg.get("mcp_token_encryption_key")

    raw_keys: list[str]
    if isinstance(raw_list_value, list) and raw_list_value:
        raw_keys = []
        for idx, item in enumerate(raw_list_value):
            if not isinstance(item, str):
                raise MCPTokenKeyConfigError(
                    f"mcp_token_encryption_keys[{idx}]: must be a string. {_KEY_GEN_HINT}"
                )
            raw_keys.append(item)
    elif raw_list_value is not None and not isinstance(raw_list_value, list):
        raise MCPTokenKeyConfigError(
            f"mcp_token_encryption_keys: must be a list of base64 url-safe strings. {_KEY_GEN_HINT}"
        )
    elif isinstance(raw_single_value, str) and raw_single_value.strip():
        raw_keys = [raw_single_value]
    else:
        return None

    decoded_keys: list[bytes] = []
    for idx, raw in enumerate(raw_keys):
        label = (
            f"mcp_token_encryption_keys[{idx}]" if len(raw_keys) > 1 else "mcp_token_encryption_key"
        )
        decoded_keys.append(_validate_key(raw, label=label))
    return MCPTokenCipherConfig(keys=tuple(decoded_keys))


# ---------------------------------------------------------------------------
# Cipher wrapper
# ---------------------------------------------------------------------------


class MCPTokenCipher:
    """Encrypt/decrypt with one or more Fernet keys.

    First key in ``cfg.keys`` is the encryption key. All keys are tried
    (in declared order) for decryption. On total decryption failure,
    raises ``MCPTokenDecryptError`` with the fingerprints attempted.
    """

    def __init__(self, cfg: MCPTokenCipherConfig) -> None:
        if not cfg.keys:
            raise MCPTokenKeyConfigError(
                f"MCPTokenCipher requires at least one key. {_KEY_GEN_HINT}"
            )
        self._cfg = cfg
        self._fingerprints = tuple(_key_fingerprint(k) for k in cfg.keys)
        # Re-encode raw bytes to the base64-url-safe form Fernet expects.
        fernets = [Fernet(base64.urlsafe_b64encode(k)) for k in cfg.keys]
        self._encrypter = fernets[0]
        self._multi = MultiFernet(fernets)

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt ``plaintext`` with the active (first) key."""
        return self._encrypter.encrypt(plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Try every installed key in declared order.

        Raises ``MCPTokenDecryptError`` carrying the fingerprints
        attempted when all fail.
        """
        try:
            return self._multi.decrypt(ciphertext)
        except InvalidToken as exc:
            raise MCPTokenDecryptError(
                "no installed key can decrypt the ciphertext",
                key_fingerprints_attempted=self._fingerprints,
            ) from exc

    @property
    def key_fingerprints(self) -> tuple[str, ...]:
        """Stable fingerprints of installed keys, in declared order.

        Useful for audit events and operator-facing error messages.
        """
        return self._fingerprints


# ---------------------------------------------------------------------------
# Token store: ciphertext-aware CRUD layered on the storage protocol
# ---------------------------------------------------------------------------


class MCPTokenStore:
    """Encrypt/decrypt OAuth tokens at the storage boundary.

    Wraps a :class:`StorageBackend`'s ciphertext-only token CRUD with a
    plaintext-facing API. ``audit_storage`` + ``node_id`` are optional;
    when both are set, decrypt failures are recorded as audit events
    under ``mcp_server.oauth.token_decrypt_failure`` with the
    fingerprints attempted.
    """

    def __init__(
        self,
        storage: StorageBackend,
        cipher: MCPTokenCipher,
        *,
        node_id: str = "",
        audit_storage: StorageBackend | None = None,
    ) -> None:
        self._storage = storage
        self._cipher = cipher
        self._node_id = node_id
        self._audit_storage = audit_storage

    @property
    def cipher(self) -> MCPTokenCipher:
        """The underlying cipher (exposed for callers that need to encrypt
        non-token blobs, e.g., the MCP-server admin form's
        ``oauth_client_secret`` plaintext input)."""
        return self._cipher

    def create_user_token(
        self,
        user_id: str,
        server_name: str,
        *,
        access_token: str,
        refresh_token: str | None,
        expires_at: str | None,
        scopes: str | None,
        as_issuer: str,
        audience: str,
    ) -> None:
        """Encrypt the access (and optional refresh) token and persist."""
        access_ct = self._cipher.encrypt(access_token.encode("utf-8"))
        refresh_ct = self._cipher.encrypt(refresh_token.encode("utf-8")) if refresh_token else None
        self._storage.create_mcp_user_token(
            user_id,
            server_name,
            access_token_ct=access_ct,
            refresh_token_ct=refresh_ct,
            expires_at=expires_at,
            scopes=scopes,
            as_issuer=as_issuer,
            audience=audience,
        )

    def get_user_token(self, user_id: str, server_name: str) -> MCPUserTokenPlain | None:
        """Returns plaintext dict or None.

        Raises ``MCPTokenDecryptError`` on key mismatch — caller MUST NOT
        auto-delete the row. If ``audit_storage`` + ``node_id`` are
        configured, emits ``mcp_server.oauth.token_decrypt_failure``
        audit event.
        """
        row = self._storage.get_mcp_user_token(user_id, server_name)
        if row is None:
            return None
        try:
            access_pt = self._cipher.decrypt(row["access_token_ct"]).decode("utf-8")
            refresh_pt: str | None
            if row["refresh_token_ct"] is not None:
                refresh_pt = self._cipher.decrypt(row["refresh_token_ct"]).decode("utf-8")
            else:
                refresh_pt = None
        except MCPTokenDecryptError as exc:
            self._audit_decrypt_failure(server_name, exc.key_fingerprints_attempted)
            raise
        return MCPUserTokenPlain(
            user_id=row["user_id"],
            server_name=row["server_name"],
            access_token=access_pt,
            refresh_token=refresh_pt,
            expires_at=row["expires_at"],
            scopes=row["scopes"],
            as_issuer=row["as_issuer"],
            audience=row["audience"],
            created=row["created"],
            last_refreshed=row["last_refreshed"],
        )

    def update_user_token_after_refresh(
        self,
        user_id: str,
        server_name: str,
        *,
        access_token: str,
        refresh_token: str | None,
        expires_at: str | None,
    ) -> bool:
        """Atomic write of new tokens after a refresh-grant exchange.

        Returns True when a row was updated.

        ``refresh_token=None`` CLEARS the column — it does NOT preserve
        the existing value.  Per RFC 6749 §6, an authorization server MAY
        omit ``refresh_token`` from the refresh response; in that case
        the OAuth-flow caller MUST pre-resolve whether to keep the
        existing refresh token or drop it before invoking this method.
        This API has no "leave unchanged" sentinel.
        """
        access_ct = self._cipher.encrypt(access_token.encode("utf-8"))
        refresh_ct = self._cipher.encrypt(refresh_token.encode("utf-8")) if refresh_token else None
        return self._storage.update_mcp_user_token_after_refresh(
            user_id,
            server_name,
            access_token_ct=access_ct,
            refresh_token_ct=refresh_ct,
            expires_at=expires_at,
        )

    def delete_user_token(self, user_id: str, server_name: str) -> bool:
        """Delete the user-token row. Returns True if existed."""
        return self._storage.delete_mcp_user_token(user_id, server_name)

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
        Raises :class:`MCPTokenDecryptError` on key mismatch — the caller
        decides whether to treat that as a missing-secret case (e.g. log +
        prompt re-consent) or surface as a configuration failure.
        """
        secret_ct = self._storage.get_mcp_oauth_client_secret_ct(server_id)
        if secret_ct is None:
            return None
        return self._cipher.decrypt(secret_ct).decode("utf-8")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
    """Validate Fernet key config + install :class:`MCPTokenCipher` /
    :class:`MCPTokenStore` on ``app_state``.

    Called from the server / console lifespan after OIDC initialization.

    Behavior:

    1. ``load_mcp_token_cipher_config()`` — wrapped in try/except. Raises
       :class:`SystemExit(1)` on :class:`MCPTokenKeyConfigError` after
       logging.
    2. Counts ``mcp_servers`` rows with ``auth_type='oauth_user'``. If
       any exist AND no key is configured, raises ``SystemExit(1)``.
    3. On success, sets ``app_state.mcp_token_cipher`` and
       ``app_state.mcp_token_store`` (both possibly ``None`` when no
       key + no oauth_user rows).

    The helper is shared by ``turnstone/server.py:_lifespan`` and
    ``turnstone/console/server.py:_lifespan``.  A separate
    :func:`close_mcp_crypto_state` mirrors :func:`close_oidc_state` for
    parity even though the cipher itself owns no resources.
    """
    from turnstone.core.storage import get_storage

    try:
        cipher_cfg = load_mcp_token_cipher_config()
    except MCPTokenKeyConfigError as exc:
        log.error("mcp_server.oauth.key_config_invalid: %s", exc)
        raise SystemExit(1) from exc

    storage = get_storage()
    oauth_user_count = sum(
        1 for row in storage.list_mcp_servers() if row.get("auth_type") == "oauth_user"
    )

    if oauth_user_count > 0 and cipher_cfg is None:
        log.error(
            "mcp.oauth: %d server(s) configured with auth_type='oauth_user' but no "
            "[security] mcp_token_encryption_key in config.toml. Generate a key with: "
            "python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())' "
            "and add it to your config.toml.",
            oauth_user_count,
        )
        raise SystemExit(1)

    if cipher_cfg is None:
        # No oauth_user rows + no key configured: zero new code paths
        # exercised; install None sentinels so callers can fast-path.
        app_state.mcp_token_cipher = None  # type: ignore[attr-defined]
        app_state.mcp_token_store = None  # type: ignore[attr-defined]
        log.debug("mcp_server.oauth.disabled (no key configured, no oauth_user rows)")
        return

    cipher = MCPTokenCipher(cipher_cfg)
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

    Mirrors :func:`turnstone.core.oidc.close_oidc_state` for parity.
    The cipher itself owns no network resources, so this is a simple
    attribute clear.
    """
    if hasattr(app_state, "mcp_token_store"):
        app_state.mcp_token_store = None
    if hasattr(app_state, "mcp_token_cipher"):
        app_state.mcp_token_cipher = None


# ---------------------------------------------------------------------------
# Re-exports for callers that don't need the storage backend
# ---------------------------------------------------------------------------

__all__ = [
    "MCPCryptoError",
    "MCPTokenCipher",
    "MCPTokenCipherConfig",
    "MCPTokenDecryptError",
    "MCPTokenKeyConfigError",
    "MCPTokenStore",
    "MCPUserTokenPlain",
    "close_mcp_crypto_state",
    "initialize_mcp_crypto_state",
    "load_mcp_token_cipher_config",
]
