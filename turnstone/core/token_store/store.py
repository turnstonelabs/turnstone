"""Encrypted token and captured-credential storage shared by OAuth consumers."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from turnstone.core.token_store import crypto as token_store_crypto

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.storage._protocol import StorageBackend

# Storage owns the reserved token-key namespace shared by consumers. Model
# mints create these keys and MCP adapters filter them; the store otherwise
# has no dependency on model authentication policy or orchestration.
MODEL_OBO_CACHE_PREFIX = "__model_obo__:"


MODEL_APP_CACHE_PREFIX = "__model_app__:"


# The pseudo-principal app-identity mints run as: they carry no user, so
# cache rows, cooldowns and the refusal-cause record all key under this
# one shared identity. Public because the session's heartbeat reads the
# model_app cause record under the same principal the mint records it as.
MODEL_APP_MINT_PRINCIPAL = "__app__"


SYNTHETIC_TOKEN_PREFIXES = (MODEL_OBO_CACHE_PREFIX, MODEL_APP_CACHE_PREFIX)


# ---------------------------------------------------------------------------
# Plaintext shape returned by ``TokenStore.get_user_token``
# ---------------------------------------------------------------------------


class UserTokenPlain(TypedDict):
    """Plaintext shape returned by ``TokenStore.get_user_token``.

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


class OIDCCredentialPlain(TypedDict):
    """Plaintext shape returned by ``TokenStore.get_oidc_credential``.

    Mirrors ``OIDCUserCredential`` (storage row shape) with the refresh
    token decrypted — the per-(user, issuer) credential that delegated-auth
    consumers redeem on demand.
    """

    user_id: str
    issuer: str
    refresh_token: str
    created: str
    last_refreshed: str


class UserTokenMetadata(TypedDict):
    """Non-secret subset of the stored token row for metadata consumers.

    Token ciphertext is intentionally absent: a list view never needs
    the access/refresh secrets. Token reads decrypt only when a consumer
    needs the credential.
    """

    user_id: str
    server_name: str
    expires_at: str | None
    scopes: str | None
    as_issuer: str
    audience: str
    created: str
    last_refreshed: str | None


class TokenStore:
    """Encrypt and decrypt token rows and captured OIDC credentials.

    Consumer-specific audit presentation is supplied through ``on_decrypt_failure``.
    SQL row and column names retain their existing storage contract.
    """

    def __init__(
        self,
        storage: StorageBackend,
        cipher: token_store_crypto.TokenCipher,
        *,
        on_decrypt_failure: Callable[[str, tuple[str, ...]], None] | None = None,
    ) -> None:
        self._storage = storage
        self._cipher = cipher
        self._on_decrypt_failure = on_decrypt_failure

    def _decrypt_failure(self, token_key: str, fingerprints: tuple[str, ...]) -> None:
        if self._on_decrypt_failure is not None:
            self._on_decrypt_failure(token_key, fingerprints)

    @property
    def cipher(self) -> token_store_crypto.TokenCipher:
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

    def get_user_token(self, user_id: str, server_name: str) -> UserTokenPlain | None:
        """Returns plaintext dict or None.

        Raises ``TokenDecryptError`` on key mismatch — caller MUST NOT
        auto-delete the row. The configured decrypt-failure hook receives the
        token identity and attempted key fingerprints.
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
        except token_store_crypto.TokenDecryptError as exc:
            self._decrypt_failure(server_name, exc.key_fingerprints_attempted)
            raise
        return UserTokenPlain(
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

    def upsert_oidc_credential(self, user_id: str, issuer: str, *, refresh_token: str) -> None:
        """Encrypt and create-or-replace the user's captured IdP refresh token."""
        refresh_ct = self._cipher.encrypt(refresh_token.encode("utf-8"))
        self._storage.upsert_oidc_user_credential(user_id, issuer, refresh_token_ct=refresh_ct)

    def get_oidc_credential(self, user_id: str, issuer: str) -> OIDCCredentialPlain | None:
        """Returns plaintext dict or None.

        Raises ``TokenDecryptError`` on key mismatch — caller MUST NOT
        auto-delete the row (same contract as ``get_user_token``).
        """
        row = self._storage.get_oidc_user_credential(user_id, issuer)
        if row is None:
            return None
        try:
            refresh_pt = self._cipher.decrypt(row["refresh_token_ct"]).decode("utf-8")
        except token_store_crypto.TokenDecryptError as exc:
            self._decrypt_failure(f"oidc:{issuer}", exc.key_fingerprints_attempted)
            raise
        return OIDCCredentialPlain(
            user_id=row["user_id"],
            issuer=row["issuer"],
            refresh_token=refresh_pt,
            created=row["created"],
            last_refreshed=row["last_refreshed"],
        )

    def update_oidc_credential_after_redeem(
        self, user_id: str, issuer: str, *, refresh_token: str, expected_current: str
    ) -> bool:
        """Rotation write-back: persist the newest refresh token after a
        redemption returned one (both verified grant legs rotate).
        Returns True when the row was updated.

        Value compare-and-swap on *expected_current* (the refresh token the mint
        read before redeeming): the write only lands when the STORED credential
        still decrypts to that value. This stops a rotation from clobbering a
        credential a concurrent LOGIN capture just refreshed — the capture writes
        the freshest token during the mint's in-flight redemption POST, so by the
        time this write-back runs the stored value already differs from
        ``expected_current`` and the stale rotated token is dropped instead of
        overwriting the fresh login one. (The compare can't be done on ciphertext
        — Fernet is non-deterministic — so it decrypts the current row. The mint
        holds the per-issuer credential lock, so the only racer is the unlocked
        capture, narrowing the residual window to this method's own read→write
        gap; a capture landing there self-heals on the user's next login.)
        """
        current = self.get_oidc_credential(user_id, issuer)
        if current is None or current["refresh_token"] != expected_current:
            return False
        refresh_ct = self._cipher.encrypt(refresh_token.encode("utf-8"))
        return self._storage.update_oidc_user_credential_refresh(
            user_id, issuer, refresh_token_ct=refresh_ct
        )

    def delete_oidc_credential(self, user_id: str, issuer: str) -> bool:
        """Remove the captured credential (logout-all / admin revoke)."""
        return self._storage.delete_oidc_user_credential(user_id, issuer)

    def list_user_token_metadata(self, user_id: str) -> list[UserTokenMetadata]:
        """Return non-secret metadata for every token row owned by ``user_id``.

        Storage layer projects the metadata columns at the SQL boundary
        (``list_mcp_user_token_metadata_by_user``) so ciphertext blobs
        never cross the wire on this list-view path. Rows arrive in
        ``created`` ASC order. Decrypt is intentionally skipped — the
        list view has no need for the secret material.
        """
        rows = self._storage.list_mcp_user_token_metadata_by_user(user_id)
        return [
            UserTokenMetadata(
                user_id=row["user_id"],
                server_name=row["server_name"],
                expires_at=row["expires_at"],
                scopes=row["scopes"],
                as_issuer=row["as_issuer"],
                audience=row["audience"],
                created=row["created"],
                last_refreshed=row["last_refreshed"],
            )
            for row in rows
        ]
