"""Tests for ``turnstone.core.mcp_crypto`` cipher + config loading.

Covers token-at-rest encryption for OAuth-MCP.
"""

from __future__ import annotations

import base64

import pytest
from cryptography.fernet import Fernet

from turnstone.core.mcp_crypto import (
    MCPTokenCipher,
    MCPTokenCipherConfig,
    MCPTokenDecryptError,
    MCPTokenKeyConfigError,
    _key_fingerprint,
    _validate_key,
    load_mcp_token_cipher_config,
)


def _new_raw_key() -> bytes:
    """Return a fresh 32-byte Fernet key as raw bytes (post-base64-decode)."""
    return base64.urlsafe_b64decode(Fernet.generate_key())


# ---------------------------------------------------------------------------
# Cipher round-trip
# ---------------------------------------------------------------------------


class TestCipherRoundTrip:
    def test_round_trip_single_key(self) -> None:
        cipher = MCPTokenCipher(MCPTokenCipherConfig(keys=(_new_raw_key(),)))
        plaintext = b"access_token_12345"
        ct = cipher.encrypt(plaintext)
        assert ct != plaintext
        assert cipher.decrypt(ct) == plaintext

    def test_round_trip_unicode_token(self) -> None:
        cipher = MCPTokenCipher(MCPTokenCipherConfig(keys=(_new_raw_key(),)))
        # Tokens may legitimately carry UTF-8 bytes (e.g. JWT with
        # non-ASCII claim values).  Round-trip a multi-byte sequence.
        plaintext = "tok_é中💯".encode()
        ct = cipher.encrypt(plaintext)
        assert cipher.decrypt(ct) == plaintext

    def test_wrong_key_raises_decrypt_error(self) -> None:
        cipher_a = MCPTokenCipher(MCPTokenCipherConfig(keys=(_new_raw_key(),)))
        cipher_b = MCPTokenCipher(MCPTokenCipherConfig(keys=(_new_raw_key(),)))
        ct = cipher_a.encrypt(b"secret")
        with pytest.raises(MCPTokenDecryptError) as exc_info:
            cipher_b.decrypt(ct)
        # Audit-trail correlation: error must carry the fingerprints of
        # the keys actually attempted, not a placeholder.
        assert exc_info.value.key_fingerprints_attempted
        assert exc_info.value.key_fingerprints_attempted == cipher_b.key_fingerprints


# ---------------------------------------------------------------------------
# Rotation (MultiFernet behavior)
# ---------------------------------------------------------------------------


class TestRotation:
    def test_rotation_forward(self) -> None:
        """Encrypt with a new-only cipher, decrypt with a [v2, v1] cluster.

        Mirrors the operational situation where a node already has the
        rotated key list installed and a peer just wrote a row under v2.
        """
        v1 = _new_raw_key()
        v2 = _new_raw_key()
        new_only = MCPTokenCipher(MCPTokenCipherConfig(keys=(v2,)))
        cluster = MCPTokenCipher(MCPTokenCipherConfig(keys=(v2, v1)))
        ct = new_only.encrypt(b"hello")
        assert cluster.decrypt(ct) == b"hello"

    def test_rotation_backward_keeps_old_decryptable(self) -> None:
        """A row written under the OLD key (v1) must still decrypt after
        rotation places v2 first and keeps v1 as fallback."""
        v1 = _new_raw_key()
        v2 = _new_raw_key()
        old_only = MCPTokenCipher(MCPTokenCipherConfig(keys=(v1,)))
        rotated = MCPTokenCipher(MCPTokenCipherConfig(keys=(v2, v1)))
        ct = old_only.encrypt(b"legacy")
        assert rotated.decrypt(ct) == b"legacy"


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def _patch_load_config(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    """Override ``turnstone.core.config.load_config`` to return ``payload``
    when the ``"security"`` section is requested."""

    def fake(section: str | None = None) -> dict:
        if section == "security":
            return payload
        return {}

    import turnstone.core.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "load_config", fake)


class TestLoadConfig:
    def test_load_singular_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        key = Fernet.generate_key().decode()
        _patch_load_config(monkeypatch, {"mcp_token_encryption_key": key})
        cfg = load_mcp_token_cipher_config()
        assert cfg is not None
        assert len(cfg.keys) == 1

    def test_load_plural_overrides_singular(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plural = [Fernet.generate_key().decode(), Fernet.generate_key().decode()]
        _patch_load_config(
            monkeypatch,
            {
                "mcp_token_encryption_keys": plural,
                "mcp_token_encryption_key": Fernet.generate_key().decode(),
            },
        )
        cfg = load_mcp_token_cipher_config()
        assert cfg is not None
        assert len(cfg.keys) == 2  # plural wins, singular ignored

    def test_load_returns_none_when_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_load_config(monkeypatch, {})
        assert load_mcp_token_cipher_config() is None

    def test_load_empty_plural_falls_through_to_singular(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Operator wrote ``mcp_token_encryption_keys = []`` AND set a
        singular value: empty plural is treated as absent."""
        key = Fernet.generate_key().decode()
        _patch_load_config(
            monkeypatch,
            {"mcp_token_encryption_keys": [], "mcp_token_encryption_key": key},
        )
        cfg = load_mcp_token_cipher_config()
        assert cfg is not None
        assert len(cfg.keys) == 1

    def test_load_invalid_base64_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_load_config(monkeypatch, {"mcp_token_encryption_key": "###not-base64###"})
        with pytest.raises(MCPTokenKeyConfigError) as exc_info:
            load_mcp_token_cipher_config()
        # Operator-facing hint is part of every error message.
        assert "regenerate with:" in str(exc_info.value)

    def test_load_wrong_length_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 24 raw bytes → 32 base64 chars; not 32 raw bytes after decode.
        short_key = base64.urlsafe_b64encode(b"\x00" * 24).decode()
        _patch_load_config(monkeypatch, {"mcp_token_encryption_key": short_key})
        with pytest.raises(MCPTokenKeyConfigError) as exc_info:
            load_mcp_token_cipher_config()
        assert "32 bytes" in str(exc_info.value)

    def test_load_non_list_plural_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_load_config(monkeypatch, {"mcp_token_encryption_keys": "single-string-not-list"})
        with pytest.raises(MCPTokenKeyConfigError) as exc_info:
            load_mcp_token_cipher_config()
        assert "list" in str(exc_info.value).lower()

    def test_load_non_string_in_plural_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_load_config(monkeypatch, {"mcp_token_encryption_keys": [12345]})
        with pytest.raises(MCPTokenKeyConfigError):
            load_mcp_token_cipher_config()


# ---------------------------------------------------------------------------
# Fingerprint stability
# ---------------------------------------------------------------------------


class TestFingerprint:
    def test_key_fingerprint_stable_and_short(self) -> None:
        key = _new_raw_key()
        fp1 = _key_fingerprint(key)
        fp2 = _key_fingerprint(key)
        assert fp1 == fp2
        # 8 bytes -> 16 hex characters.
        assert len(fp1) == 16
        assert all(c in "0123456789abcdef" for c in fp1)

    def test_different_keys_have_different_fingerprints(self) -> None:
        fp1 = _key_fingerprint(_new_raw_key())
        fp2 = _key_fingerprint(_new_raw_key())
        assert fp1 != fp2

    def test_cipher_fingerprints_match_keys(self) -> None:
        v1 = _new_raw_key()
        v2 = _new_raw_key()
        cipher = MCPTokenCipher(MCPTokenCipherConfig(keys=(v1, v2)))
        assert cipher.key_fingerprints == (
            _key_fingerprint(v1),
            _key_fingerprint(v2),
        )


# ---------------------------------------------------------------------------
# Direct ``_validate_key`` — exercises edge cases not reachable via loader
# ---------------------------------------------------------------------------


class TestValidateKey:
    def test_empty_string_rejected(self) -> None:
        with pytest.raises(MCPTokenKeyConfigError):
            _validate_key("", label="x")

    def test_whitespace_only_rejected(self) -> None:
        with pytest.raises(MCPTokenKeyConfigError):
            _validate_key("   ", label="x")

    def test_label_propagated_in_error(self) -> None:
        with pytest.raises(MCPTokenKeyConfigError) as exc_info:
            _validate_key("###", label="my_label_42")
        assert "my_label_42" in str(exc_info.value)


# ---------------------------------------------------------------------------
# MCPTokenCipher constructor guard
# ---------------------------------------------------------------------------


class TestCipherConstructorGuard:
    def test_empty_keys_rejected(self) -> None:
        with pytest.raises(MCPTokenKeyConfigError):
            MCPTokenCipher(MCPTokenCipherConfig(keys=()))
