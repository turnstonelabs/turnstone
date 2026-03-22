"""Tests for OIDC identity and pending state storage CRUD (SQLite backend)."""

from __future__ import annotations

import time

import pytest

# ---------------------------------------------------------------------------
# OIDC Identity CRUD
# ---------------------------------------------------------------------------


class TestOIDCIdentityCRUD:
    def test_create_and_get_oidc_identity(self, db):
        db.create_oidc_identity("https://idp.example.com", "sub-123", "u1", "alice@example.com")
        identity = db.get_oidc_identity("https://idp.example.com", "sub-123")
        assert identity is not None
        assert identity["issuer"] == "https://idp.example.com"
        assert identity["subject"] == "sub-123"
        assert identity["user_id"] == "u1"
        assert identity["email"] == "alice@example.com"
        assert identity["created"] != ""
        assert identity["last_login"] != ""

    def test_get_oidc_identity_not_found(self, db):
        assert db.get_oidc_identity("https://unknown.example.com", "sub-999") is None

    def test_create_oidc_identity_idempotent(self, db):
        """Creating twice with same (issuer, subject) does not error (OR IGNORE)."""
        db.create_oidc_identity("https://idp.example.com", "sub-123", "u1", "alice@example.com")
        db.create_oidc_identity("https://idp.example.com", "sub-123", "u2", "bob@example.com")

        identity = db.get_oidc_identity("https://idp.example.com", "sub-123")
        assert identity is not None
        # OR IGNORE preserves the first insert
        assert identity["user_id"] == "u1"
        assert identity["email"] == "alice@example.com"

    def test_update_oidc_identity_login(self, db):
        db.create_oidc_identity("https://idp.example.com", "sub-123", "u1", "alice@example.com")

        before = db.get_oidc_identity("https://idp.example.com", "sub-123")
        assert before is not None
        original_login = before["last_login"]

        # Small sleep to ensure timestamp differs
        time.sleep(0.05)

        result = db.update_oidc_identity_login("https://idp.example.com", "sub-123")
        assert result is True

        after = db.get_oidc_identity("https://idp.example.com", "sub-123")
        assert after is not None
        assert after["last_login"] >= original_login

    def test_update_oidc_identity_login_nonexistent(self, db):
        result = db.update_oidc_identity_login("https://idp.example.com", "sub-999")
        assert result is False

    def test_list_oidc_identities_for_user(self, db):
        """Two identities for same user, list returns both."""
        db.create_oidc_identity("https://idp1.example.com", "sub-A", "u1", "alice@idp1.com")
        db.create_oidc_identity("https://idp2.example.com", "sub-B", "u1", "alice@idp2.com")

        identities = db.list_oidc_identities_for_user("u1")
        assert len(identities) == 2
        issuers = {i["issuer"] for i in identities}
        assert issuers == {"https://idp1.example.com", "https://idp2.example.com"}

    def test_list_oidc_identities_for_user_empty(self, db):
        assert db.list_oidc_identities_for_user("u-none") == []

    def test_list_oidc_identities_excludes_other_users(self, db):
        db.create_oidc_identity("https://idp.example.com", "sub-1", "u1", "alice@example.com")
        db.create_oidc_identity("https://idp.example.com", "sub-2", "u2", "bob@example.com")

        identities = db.list_oidc_identities_for_user("u1")
        assert len(identities) == 1
        assert identities[0]["user_id"] == "u1"

    def test_delete_oidc_identity(self, db):
        db.create_oidc_identity("https://idp.example.com", "sub-123", "u1", "alice@example.com")
        assert db.delete_oidc_identity("https://idp.example.com", "sub-123") is True
        assert db.get_oidc_identity("https://idp.example.com", "sub-123") is None

    def test_delete_oidc_identity_nonexistent(self, db):
        assert db.delete_oidc_identity("https://idp.example.com", "sub-999") is False

    def test_delete_oidc_identity_only_deletes_target(self, db):
        """Deleting one identity does not affect others."""
        db.create_oidc_identity("https://idp.example.com", "sub-1", "u1", "a@example.com")
        db.create_oidc_identity("https://idp.example.com", "sub-2", "u1", "b@example.com")

        db.delete_oidc_identity("https://idp.example.com", "sub-1")

        assert db.get_oidc_identity("https://idp.example.com", "sub-1") is None
        assert db.get_oidc_identity("https://idp.example.com", "sub-2") is not None


# ---------------------------------------------------------------------------
# OIDC Pending State
# ---------------------------------------------------------------------------


class TestOIDCPendingState:
    def test_create_and_pop_pending_state(self, db):
        db.create_oidc_pending_state(
            state="state-abc",
            nonce="nonce-xyz",
            code_verifier="verifier-123",
            audience="server",
        )

        result = db.pop_oidc_pending_state("state-abc")
        assert result is not None
        assert result["state"] == "state-abc"
        assert result["nonce"] == "nonce-xyz"
        assert result["code_verifier"] == "verifier-123"
        assert result["audience"] == "server"
        assert result["created_at"] != ""

    def test_pop_pending_state_not_found(self, db):
        assert db.pop_oidc_pending_state("nonexistent-state") is None

    def test_pop_pending_state_expired(self, db):
        """Create with old timestamp, pop returns None."""
        # Insert a row with an old created_at timestamp directly
        import sqlalchemy as sa

        from turnstone.core.storage._schema import oidc_pending_states

        with db._engine.connect() as conn:
            conn.execute(
                sa.insert(oidc_pending_states),
                {
                    "state": "state-old",
                    "nonce": "nonce-old",
                    "code_verifier": "verifier-old",
                    "audience": "server",
                    "created_at": "2020-01-01T00:00:00",
                },
            )
            conn.commit()

        # Default max_age_seconds=300, so a 2020 timestamp is expired
        result = db.pop_oidc_pending_state("state-old")
        assert result is None

    def test_pop_pending_state_consumed(self, db):
        """Pop twice -> second returns None (one-time use)."""
        db.create_oidc_pending_state(
            state="state-once",
            nonce="nonce-1",
            code_verifier="verifier-1",
            audience="server",
        )

        first = db.pop_oidc_pending_state("state-once")
        assert first is not None

        second = db.pop_oidc_pending_state("state-once")
        assert second is None

    def test_pop_pending_state_custom_max_age(self, db):
        """Custom max_age_seconds allows longer-lived states."""
        db.create_oidc_pending_state(
            state="state-long",
            nonce="nonce-long",
            code_verifier="verifier-long",
            audience="server",
        )

        # With very short max_age, it might still be valid since we just created it
        result = db.pop_oidc_pending_state("state-long", max_age_seconds=600)
        assert result is not None

    def test_create_pending_state_duplicate_raises(self, db):
        """Duplicate state insertion raises IntegrityError (no silent drop)."""
        import sqlalchemy.exc

        db.create_oidc_pending_state("state-dup", "nonce-1", "verifier-1", "server")
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            db.create_oidc_pending_state("state-dup", "nonce-2", "verifier-2", "server")

    def test_cleanup_expired_states(self, db):
        """Create expired + fresh, cleanup removes only expired."""
        import sqlalchemy as sa

        from turnstone.core.storage._schema import oidc_pending_states

        # Insert an expired state directly with old timestamp
        with db._engine.connect() as conn:
            conn.execute(
                sa.insert(oidc_pending_states),
                {
                    "state": "state-expired",
                    "nonce": "nonce-old",
                    "code_verifier": "verifier-old",
                    "audience": "server",
                    "created_at": "2020-01-01T00:00:00",
                },
            )
            conn.commit()

        # Insert a fresh state via normal API
        db.create_oidc_pending_state("state-fresh", "nonce-new", "verifier-new", "server")

        # Cleanup with default 300s max age
        deleted = db.cleanup_expired_oidc_states()
        assert deleted == 1

        # Fresh state should still exist
        result = db.pop_oidc_pending_state("state-fresh")
        assert result is not None

    def test_cleanup_expired_states_none_expired(self, db):
        """Cleanup with no expired states returns 0."""
        db.create_oidc_pending_state("state-1", "nonce-1", "verifier-1", "server")
        deleted = db.cleanup_expired_oidc_states()
        assert deleted == 0

    def test_cleanup_expired_states_all_expired(self, db):
        """Cleanup with all expired states removes all."""
        import sqlalchemy as sa

        from turnstone.core.storage._schema import oidc_pending_states

        with db._engine.connect() as conn:
            for i in range(3):
                conn.execute(
                    sa.insert(oidc_pending_states),
                    {
                        "state": f"state-{i}",
                        "nonce": f"nonce-{i}",
                        "code_verifier": f"verifier-{i}",
                        "audience": "server",
                        "created_at": "2020-01-01T00:00:00",
                    },
                )
            conn.commit()

        deleted = db.cleanup_expired_oidc_states()
        assert deleted == 3

    def test_cleanup_expired_states_custom_max_age(self, db):
        """Custom max_age_seconds affects what counts as expired."""
        from datetime import UTC, datetime, timedelta

        import sqlalchemy as sa

        from turnstone.core.storage._schema import oidc_pending_states

        # Insert a state created 60 seconds ago
        old_ts = (datetime.now(UTC) - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S")
        with db._engine.connect() as conn:
            conn.execute(
                sa.insert(oidc_pending_states),
                {
                    "state": "state-1",
                    "nonce": "nonce-1",
                    "code_verifier": "verifier-1",
                    "audience": "server",
                    "created_at": old_ts,
                },
            )
            conn.commit()

        # With default max_age=300s the 60s-old state is NOT expired
        deleted = db.cleanup_expired_oidc_states(max_age_seconds=300)
        assert deleted == 0

        # With max_age=30s the 60s-old state IS expired
        deleted = db.cleanup_expired_oidc_states(max_age_seconds=30)
        assert deleted == 1

    def test_pop_expired_cleans_up_row(self, db):
        """Popping an expired state should delete the row (not leave orphan)."""
        import sqlalchemy as sa

        from turnstone.core.storage._schema import oidc_pending_states

        with db._engine.connect() as conn:
            conn.execute(
                sa.insert(oidc_pending_states),
                {
                    "state": "state-cleanup",
                    "nonce": "nonce-c",
                    "code_verifier": "verifier-c",
                    "audience": "server",
                    "created_at": "2020-01-01T00:00:00",
                },
            )
            conn.commit()

        # Pop returns None (expired)
        assert db.pop_oidc_pending_state("state-cleanup") is None

        # Row should be gone (cleaned up even though expired)
        with db._engine.connect() as conn:
            count = conn.execute(
                sa.select(sa.func.count())
                .select_from(oidc_pending_states)
                .where(oidc_pending_states.c.state == "state-cleanup")
            ).scalar()
            assert count == 0
