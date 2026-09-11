"""Deployment-wide encryption and keyring configuration for OAuth secrets."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

# Constants
_KEY_BYTES = 32  # Fernet requires 32 bytes


_KEY_FINGERPRINT_BYTES = 8  # short hex prefix for audit/error fields


# Operator-facing hint for malformed/missing keys.
_KEY_GEN_HINT = (
    "regenerate with: python -c 'from cryptography.fernet import Fernet; "
    "print(Fernet.generate_key().decode())'"
)


# Operator-facing tail for the two startup key-requirement SystemExit logs
# (user-scoped servers / credential capture) — one copy so the guidance
# can't drift between them.
STARTUP_KEY_REQUIRED_HINT = (
    "no [security] mcp_token_encryption_keys (rotation list) or "
    "mcp_token_encryption_key (single) in config.toml. Generate a key with: "
    "python -c 'from cryptography.fernet import Fernet; "
    "print(Fernet.generate_key().decode())' "
    "and add it to your config.toml."
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TokenCryptoError(Exception):
    """Base class for token-at-rest encryption errors."""


class TokenDecryptError(TokenCryptoError):
    """No installed key can decrypt the ciphertext.

    Carries ``key_fingerprints_attempted: tuple[str, ...]`` for audit.

    Critical: callers MUST NOT auto-delete the row on this error.
    The row is still valid; this node just doesn't have the right key.
    """

    def __init__(self, message: str, *, key_fingerprints_attempted: tuple[str, ...]) -> None:
        super().__init__(message)
        self.key_fingerprints_attempted = key_fingerprints_attempted


class TokenKeyConfigError(TokenCryptoError):
    """Key material in config.toml is malformed or missing."""


# ---------------------------------------------------------------------------
# Config dataclass + loader
# ---------------------------------------------------------------------------


@dataclass(frozen=True, repr=False)
class TokenCipherConfig:
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
        return f"TokenCipherConfig(keys=<{len(self.keys)} key(s) redacted>)"


def _validate_key(raw: str, *, label: str) -> bytes:
    """Decode + validate a single base64 url-safe key. Raises ``TokenKeyConfigError``."""
    if not isinstance(raw, str) or not raw.strip():
        raise TokenKeyConfigError(f"{label}: key is empty or not a string. {_KEY_GEN_HINT}")
    try:
        decoded = base64.urlsafe_b64decode(raw.encode("ascii"))
    except Exception as exc:
        raise TokenKeyConfigError(
            f"{label}: not valid base64 url-safe ({exc}). {_KEY_GEN_HINT}"
        ) from exc
    if len(decoded) != _KEY_BYTES:
        raise TokenKeyConfigError(
            f"{label}: decoded key must be exactly {_KEY_BYTES} bytes, "
            f"got {len(decoded)}. {_KEY_GEN_HINT}"
        )
    return decoded


def _key_fingerprint(key: bytes) -> str:
    """Stable, non-reversible 8-hex prefix of SHA-256(key)."""
    digest = hashlib.sha256(key).hexdigest()
    return digest[: _KEY_FINGERPRINT_BYTES * 2]


def load_token_cipher_config() -> TokenCipherConfig | None:
    """Read ``[security] mcp_token_encryption_keys`` (plural) or
    ``mcp_token_encryption_key`` (singular) from config.toml.

    Plural takes precedence when both are present. Returns ``None`` when
    neither key is configured (caller decides whether that's fatal).
    Raises ``TokenKeyConfigError`` on malformed key material.
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
                raise TokenKeyConfigError(
                    f"mcp_token_encryption_keys[{idx}]: must be a string. {_KEY_GEN_HINT}"
                )
            raw_keys.append(item)
    elif raw_list_value is not None and not isinstance(raw_list_value, list):
        raise TokenKeyConfigError(
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
    return TokenCipherConfig(keys=tuple(decoded_keys))


# ---------------------------------------------------------------------------
# Cipher wrapper
# ---------------------------------------------------------------------------


class TokenCipher:
    """Encrypt/decrypt with one or more Fernet keys.

    First key in ``cfg.keys`` is the encryption key. All keys are tried
    (in declared order) for decryption. On total decryption failure,
    raises ``TokenDecryptError`` with the fingerprints attempted.
    """

    def __init__(self, cfg: TokenCipherConfig) -> None:
        if not cfg.keys:
            raise TokenKeyConfigError(f"TokenCipher requires at least one key. {_KEY_GEN_HINT}")
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

        Raises ``TokenDecryptError`` carrying the fingerprints
        attempted when all fail.
        """
        try:
            return self._multi.decrypt(ciphertext)
        except InvalidToken as exc:
            raise TokenDecryptError(
                "no installed key can decrypt the ciphertext",
                key_fingerprints_attempted=self._fingerprints,
            ) from exc

    @property
    def key_fingerprints(self) -> tuple[str, ...]:
        """Stable fingerprints of installed keys, in declared order.

        Useful for audit events and operator-facing error messages.
        """
        return self._fingerprints
