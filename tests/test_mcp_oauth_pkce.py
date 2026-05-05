"""PKCE pair-generation tests for the MCP OAuth flow.

Verifies the contract documented in RFC 7636 §4.1 and §4.2:

- ``code_verifier`` is a high-entropy 43..128 character urlsafe-base64 string.
- ``code_challenge`` is the BASE64URL-NO-PADDING encoding of
  ``SHA256(verifier)``.
"""

from __future__ import annotations

import base64
import hashlib
import string

from turnstone.core.mcp_oauth import generate_pkce_pair

_URLSAFE_CHARS = set(string.ascii_letters + string.digits + "-_")


class TestGeneratePkcePair:
    def test_returns_tuple_of_strings(self) -> None:
        verifier, challenge = generate_pkce_pair()
        assert isinstance(verifier, str)
        assert isinstance(challenge, str)

    def test_verifier_length_in_rfc_range(self) -> None:
        for _ in range(20):
            verifier, _ = generate_pkce_pair()
            assert 43 <= len(verifier) <= 128

    def test_verifier_is_urlsafe(self) -> None:
        for _ in range(20):
            verifier, _ = generate_pkce_pair()
            assert all(ch in _URLSAFE_CHARS for ch in verifier)

    def test_challenge_matches_sha256_of_verifier(self) -> None:
        for _ in range(20):
            verifier, challenge = generate_pkce_pair()
            digest = hashlib.sha256(verifier.encode("ascii")).digest()
            expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
            assert challenge == expected

    def test_challenge_has_no_padding(self) -> None:
        for _ in range(20):
            _, challenge = generate_pkce_pair()
            assert "=" not in challenge

    def test_pairs_are_unique(self) -> None:
        pairs = {generate_pkce_pair() for _ in range(50)}
        # 50 random draws shouldn't collide; if they do we have a much
        # bigger problem than this assertion.
        assert len(pairs) == 50
