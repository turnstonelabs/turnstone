"""Tests for user identity storage operations (SQLite backend)."""

from __future__ import annotations

import pytest

from turnstone.core.storage._sqlite import SQLiteBackend


@pytest.fixture()
def db(tmp_path):
    """Create a fresh SQLite backend for each test."""
    return SQLiteBackend(str(tmp_path / "test.db"))


class TestUserCRUD:
    def test_create_and_get(self, db):
        db.create_user("u1", "admin", "Admin User", "$2b$hash")
        user = db.get_user("u1")
        assert user is not None
        assert user["user_id"] == "u1"
        assert user["username"] == "admin"
        assert user["display_name"] == "Admin User"
        assert user["password_hash"] == "$2b$hash"

    def test_get_nonexistent(self, db):
        assert db.get_user("missing") is None

    def test_get_by_username(self, db):
        db.create_user("u1", "admin", "Admin", "$2b$hash")
        user = db.get_user_by_username("admin")
        assert user is not None
        assert user["user_id"] == "u1"

    def test_get_by_username_nonexistent(self, db):
        assert db.get_user_by_username("nope") is None

    def test_create_duplicate_noop(self, db):
        db.create_user("u1", "admin", "First", "$2b$hash1")
        db.create_user("u1", "admin2", "Second", "$2b$hash2")
        user = db.get_user("u1")
        assert user is not None
        assert user["display_name"] == "First"

    def test_list_users(self, db):
        db.create_user("u1", "admin", "Admin", "$2b$h1")
        db.create_user("u2", "reader", "Reader", "$2b$h2")
        users = db.list_users()
        assert len(users) == 2
        assert "password_hash" not in users[0]

    def test_delete_user(self, db):
        db.create_user("u1", "admin", "Admin", "$2b$hash")
        assert db.delete_user("u1")
        assert db.get_user("u1") is None

    def test_delete_nonexistent(self, db):
        assert not db.delete_user("missing")

    def test_delete_cascades_tokens(self, db):
        db.create_user("u1", "admin", "Admin", "$2b$hash")
        db.create_api_token("t1", "hash1", "ts_abcde", "u1", "tok1", "read,write")
        db.create_api_token("t2", "hash2", "ts_fghij", "u1", "tok2", "read")
        assert len(db.list_api_tokens("u1")) == 2
        db.delete_user("u1")
        assert len(db.list_api_tokens("u1")) == 0


class TestApiTokenCRUD:
    def test_create_and_lookup_by_hash(self, db):
        db.create_user("u1", "admin", "Admin", "$2b$hash")
        db.create_api_token("t1", "tokenhash123", "ts_abcde", "u1", "My Token", "read,write")
        tok = db.get_api_token_by_hash("tokenhash123")
        assert tok is not None
        assert tok["token_id"] == "t1"
        assert tok["user_id"] == "u1"
        assert tok["scopes"] == "read,write"

    def test_lookup_missing_hash(self, db):
        assert db.get_api_token_by_hash("nonexistent") is None

    def test_list_tokens_excludes_hash(self, db):
        db.create_user("u1", "admin", "Admin", "$2b$hash")
        db.create_api_token("t1", "secret_hash", "ts_abcde", "u1", "tok1", "read")
        tokens = db.list_api_tokens("u1")
        assert len(tokens) == 1
        assert "token_hash" not in tokens[0]
        assert tokens[0]["token_prefix"] == "ts_abcde"

    def test_list_tokens_by_user(self, db):
        db.create_user("u1", "admin", "Admin", "$2b$hash")
        db.create_user("u2", "reader", "Reader", "$2b$hash")
        db.create_api_token("t1", "h1", "ts_a", "u1", "tok1", "read")
        db.create_api_token("t2", "h2", "ts_b", "u2", "tok2", "read")
        assert len(db.list_api_tokens("u1")) == 1
        assert len(db.list_api_tokens("u2")) == 1

    def test_delete_token(self, db):
        db.create_user("u1", "admin", "Admin", "$2b$hash")
        db.create_api_token("t1", "h1", "ts_a", "u1", "tok1", "read")
        assert db.delete_api_token("t1")
        assert db.get_api_token_by_hash("h1") is None

    def test_delete_nonexistent_token(self, db):
        assert not db.delete_api_token("missing")

    def test_token_with_expiry(self, db):
        db.create_user("u1", "admin", "Admin", "$2b$hash")
        db.create_api_token(
            "t1",
            "h1",
            "ts_a",
            "u1",
            "tok1",
            "read",
            expires="2030-01-01T00:00:00",
        )
        tok = db.get_api_token_by_hash("h1")
        assert tok is not None
        assert tok["expires"] == "2030-01-01T00:00:00"

    def test_token_without_expiry(self, db):
        db.create_user("u1", "admin", "Admin", "$2b$hash")
        db.create_api_token("t1", "h1", "ts_a", "u1", "tok1", "read")
        tok = db.get_api_token_by_hash("h1")
        assert tok is not None
        assert "expires" not in tok


class TestWorkstreamUserId:
    def test_register_workstream_with_user_id(self, db):
        db.register_workstream("ws1", user_id="u1")
        import sqlalchemy as sa

        from turnstone.core.storage._schema import workstreams

        with db._engine.connect() as conn:
            row = conn.execute(
                sa.select(workstreams.c.user_id).where(workstreams.c.ws_id == "ws1")
            ).fetchone()
            assert row is not None
            assert row[0] == "u1"

    def test_register_workstream_without_user_id(self, db):
        db.register_workstream("ws1")
        import sqlalchemy as sa

        from turnstone.core.storage._schema import workstreams

        with db._engine.connect() as conn:
            row = conn.execute(
                sa.select(workstreams.c.user_id).where(workstreams.c.ws_id == "ws1")
            ).fetchone()
            assert row is not None
            assert row[0] is None
