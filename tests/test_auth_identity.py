"""Tests for user identity, API tokens, JWT, and scoped auth."""

from __future__ import annotations

import time

import pytest

from turnstone.core.auth import (
    AuthResult,
    _authenticate_token,
    check_request,
    create_jwt,
    generate_token,
    hash_password,
    hash_token,
    parse_scopes,
    required_scope,
    token_prefix,
    validate_jwt,
    verify_password,
)

# ---------------------------------------------------------------------------
# AuthResult
# ---------------------------------------------------------------------------


class TestAuthResult:
    def test_frozen(self):
        r = AuthResult(user_id="u1", scopes=frozenset({"read"}), token_source="config")
        with pytest.raises(AttributeError):
            r.user_id = "u2"  # type: ignore[misc]

    def test_has_scope(self):
        r = AuthResult(user_id="", scopes=frozenset({"read", "write"}), token_source="config")
        assert r.has_scope("read")
        assert r.has_scope("write")
        assert not r.has_scope("approve")

    def test_empty_scopes(self):
        r = AuthResult(user_id="", scopes=frozenset(), token_source="config")
        assert not r.has_scope("read")


# ---------------------------------------------------------------------------
# Token generation and hashing
# ---------------------------------------------------------------------------


class TestTokenHelpers:
    def test_generate_token_format(self):
        tok = generate_token()
        assert tok.startswith("ts_")
        assert len(tok) == 3 + 64  # ts_ + 64 hex chars

    def test_generate_token_unique(self):
        tokens = {generate_token() for _ in range(10)}
        assert len(tokens) == 10

    def test_hash_token_deterministic(self):
        assert hash_token("ts_abc") == hash_token("ts_abc")

    def test_hash_token_hex(self):
        h = hash_token("test")
        assert len(h) == 64  # SHA-256 hex
        int(h, 16)  # valid hex

    def test_token_prefix(self):
        assert token_prefix("ts_abcdefgh1234") == "ts_abcde"


# ---------------------------------------------------------------------------
# Password hashing (bcrypt)
# ---------------------------------------------------------------------------


class TestPasswordHashing:
    def test_hash_and_verify(self):
        pw = "hunter2"
        hashed = hash_password(pw)
        assert verify_password(pw, hashed)

    def test_wrong_password(self):
        hashed = hash_password("correct")
        assert not verify_password("wrong", hashed)

    def test_hash_is_different_each_time(self):
        h1 = hash_password("same")
        h2 = hash_password("same")
        assert h1 != h2  # different salts


# ---------------------------------------------------------------------------
# Scope parsing
# ---------------------------------------------------------------------------


class TestParseScopes:
    def test_single_scope(self):
        assert parse_scopes("read") == frozenset({"read"})

    def test_hierarchy_write(self):
        assert parse_scopes("write") == frozenset({"read", "write"})

    def test_hierarchy_approve(self):
        assert parse_scopes("approve") == frozenset({"read", "write", "approve"})

    def test_comma_separated(self):
        assert parse_scopes("read,write") == frozenset({"read", "write"})

    def test_redundant_scopes(self):
        # approve already includes read,write
        assert parse_scopes("read,approve") == frozenset({"read", "write", "approve"})

    def test_empty_string(self):
        assert parse_scopes("") == frozenset()

    def test_invalid_scope_filtered(self):
        assert parse_scopes("bogus") == frozenset()

    def test_mixed_valid_invalid(self):
        assert parse_scopes("read,bogus,approve") == frozenset({"read", "write", "approve"})


# ---------------------------------------------------------------------------
# JWT create / validate
# ---------------------------------------------------------------------------


class TestJWT:
    SECRET = "test-secret-key-for-jwt-min-32b!"

    def test_round_trip(self):
        scopes = frozenset({"read", "write"})
        token = create_jwt("user123", scopes, "database", self.SECRET, expiry_hours=1)
        result = validate_jwt(token, self.SECRET)
        assert result is not None
        assert result.user_id == "user123"
        assert result.scopes == frozenset({"read", "write"})

    def test_expired_token(self):
        import jwt

        payload = {
            "sub": "user1",
            "scopes": "read",
            "src": "database",
            "iat": int(time.time()) - 7200,
            "exp": int(time.time()) - 3600,
        }
        token = jwt.encode(payload, self.SECRET, algorithm="HS256")
        assert validate_jwt(token, self.SECRET) is None

    def test_invalid_signature(self):
        token = create_jwt("user1", frozenset({"read"}), "db", self.SECRET)
        assert validate_jwt(token, "wrong-secret-key-for-jwt-min-32b") is None

    def test_malformed_token(self):
        assert validate_jwt("not.a.jwt", self.SECRET) is None

    def test_contains_dots(self):
        """JWTs contain dots, used for detection."""
        token = create_jwt("u1", frozenset({"read"}), "db", self.SECRET)
        assert "." in token


# ---------------------------------------------------------------------------
# required_scope
# ---------------------------------------------------------------------------


class TestRequiredScope:
    def test_get_read(self):
        assert required_scope("GET", "/api/workstreams") == "read"

    def test_post_write(self):
        assert required_scope("POST", "/api/workstreams/abc/send") == "write"

    def test_post_approve(self):
        assert required_scope("POST", "/api/workstreams/abc/approve") == "approve"

    def test_admin_prefix(self):
        assert required_scope("GET", "/api/admin/users") == "approve"
        assert required_scope("POST", "/api/admin/users") == "approve"
        assert required_scope("DELETE", "/api/admin/users/abc") == "approve"

    def test_versioned_path(self):
        assert required_scope("POST", "/v1/api/workstreams/abc/send") == "write"
        assert required_scope("POST", "/v1/api/workstreams/abc/approve") == "approve"

    def test_proxy_write(self):
        assert required_scope("POST", "/node/n1/api/workstreams/abc/send") == "write"

    def test_proxy_approve(self):
        assert required_scope("POST", "/node/n1/api/workstreams/abc/approve") == "approve"


# ---------------------------------------------------------------------------
# _authenticate_token
# ---------------------------------------------------------------------------


class TestAuthenticateToken:
    def test_jwt_token(self):
        secret = "test-secret-key-for-jwt-min-32b!"
        jwt_tok = create_jwt("user1", frozenset({"read", "write"}), "db", secret)
        result = _authenticate_token(jwt_tok, jwt_secret=secret)
        assert result is not None
        assert result.user_id == "user1"
        assert result.token_source == "db"

    def test_api_token_with_storage(self):
        """API tokens are looked up by hash in storage."""
        raw = generate_token()

        class MockStorage:
            def get_api_token_by_hash(self, token_hash):
                expected = hash_token(raw)
                if token_hash == expected:
                    return {
                        "token_id": "tid",
                        "token_prefix": "ts_abcde",
                        "user_id": "user1",
                        "name": "test",
                        "scopes": "read,write",
                        "created": "2026-01-01T00:00:00",
                    }
                return None

        result = _authenticate_token(raw, storage=MockStorage())
        assert result is not None
        assert result.user_id == "user1"
        assert result.has_scope("write")
        assert result.token_source == "database"

    def test_api_token_expired(self):
        """Expired API tokens are rejected."""
        raw = generate_token()

        class MockStorage:
            def get_api_token_by_hash(self, token_hash):
                return {
                    "token_id": "tid",
                    "token_prefix": "ts_abcde",
                    "user_id": "user1",
                    "name": "test",
                    "scopes": "read",
                    "created": "2020-01-01T00:00:00",
                    "expires": "2020-01-02T00:00:00",
                }

        result = _authenticate_token(raw, storage=MockStorage())
        assert result is None

    def test_unknown_token(self):
        result = _authenticate_token("unknown")
        assert result is None


# ---------------------------------------------------------------------------
# check_request with scopes
# ---------------------------------------------------------------------------


class TestCheckRequestScopes:
    _SECRET = "test-secret-key-for-jwt-min-32b!"

    def test_jwt_read_on_write_403(self):
        jwt_tok = create_jwt("u1", frozenset({"read"}), "test", self._SECRET)
        allowed, status, msg, _ = check_request(
            "POST",
            "/api/workstreams/abc/send",
            f"Bearer {jwt_tok}",
            jwt_secret=self._SECRET,
        )
        assert not allowed
        assert status == 403
        assert "write" in msg

    def test_jwt_read_on_approve_403(self):
        jwt_tok = create_jwt("u1", frozenset({"read"}), "test", self._SECRET)
        allowed, status, msg, _ = check_request(
            "POST",
            "/api/workstreams/abc/approve",
            f"Bearer {jwt_tok}",
            jwt_secret=self._SECRET,
        )
        assert not allowed
        assert status == 403
        assert "approve" in msg

    def test_jwt_full_on_approve_ok(self):
        jwt_tok = create_jwt("u1", frozenset({"read", "write", "approve"}), "test", self._SECRET)
        allowed, status, msg, result = check_request(
            "POST",
            "/api/workstreams/abc/approve",
            f"Bearer {jwt_tok}",
            jwt_secret=self._SECRET,
        )
        assert allowed
        assert result is not None
        assert result.has_scope("approve")

    def test_jwt_with_scopes(self):
        jwt_tok = create_jwt("u1", frozenset({"read", "write"}), "db", self._SECRET)
        allowed, status, msg, result = check_request(
            "POST",
            "/api/workstreams/abc/send",
            f"Bearer {jwt_tok}",
            jwt_secret=self._SECRET,
        )
        assert allowed
        assert result is not None
        assert result.user_id == "u1"

    def test_jwt_insufficient_scope(self):
        jwt_tok = create_jwt("u1", frozenset({"read"}), "db", self._SECRET)
        allowed, status, msg, _ = check_request(
            "POST",
            "/api/workstreams/abc/send",
            f"Bearer {jwt_tok}",
            jwt_secret=self._SECRET,
        )
        assert not allowed
        assert status == 403

    def test_admin_path_requires_approve(self):
        jwt_tok = create_jwt("u1", frozenset({"read"}), "test", self._SECRET)
        allowed, status, msg, _ = check_request(
            "GET",
            "/v1/api/admin/users",
            f"Bearer {jwt_tok}",
            jwt_secret=self._SECRET,
        )
        assert not allowed
        assert status == 403
