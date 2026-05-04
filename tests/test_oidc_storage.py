"""Tests for OIDC identity and pending state storage CRUD (SQLite backend)."""

from __future__ import annotations

import time

import pytest

from turnstone.core.storage import StorageConflictError

# ---------------------------------------------------------------------------
# Atomic OIDC user provisioning
# ---------------------------------------------------------------------------


class TestCreateOIDCUser:
    def test_create_oidc_user_success(self, db):
        """Both rows present after one atomic call."""
        db.create_oidc_user(
            user_id="u-new",
            username="alice",
            display_name="Alice",
            password_hash="!oidc",
            issuer="https://idp.example.com",
            subject="sub-1",
            email="alice@example.com",
        )

        user = db.get_user("u-new")
        assert user is not None
        assert user["username"] == "alice"
        assert user["password_hash"] == "!oidc"

        identity = db.get_oidc_identity("https://idp.example.com", "sub-1")
        assert identity is not None
        assert identity["user_id"] == "u-new"
        assert identity["email"] == "alice@example.com"

    def test_create_oidc_user_username_conflict_rolls_back(self, db):
        """Pre-existing username -> StorageConflictError; identity NOT inserted."""
        db.create_user("u-existing", "alice", "Alice", "$2b$12$hash")

        with pytest.raises(StorageConflictError, match="username"):
            db.create_oidc_user(
                user_id="u-new",
                username="alice",
                display_name="Alice2",
                password_hash="!oidc",
                issuer="https://idp.example.com",
                subject="sub-1",
                email="alice2@example.com",
            )

        # The new user_id row must not exist.
        assert db.get_user("u-new") is None
        # The identity row must not exist.
        assert db.get_oidc_identity("https://idp.example.com", "sub-1") is None
        # The pre-existing user is untouched.
        existing = db.get_user("u-existing")
        assert existing is not None
        assert existing["password_hash"] == "$2b$12$hash"

    def test_create_oidc_user_identity_conflict_rolls_back(self, db):
        """Pre-existing (issuer, subject) -> StorageConflictError; user row rolled back."""
        db.create_user("u-other", "other", "Other", "!oidc")
        db.create_oidc_identity("https://idp.example.com", "sub-1", "u-other", "other@example.com")

        with pytest.raises(StorageConflictError, match="OIDC identity"):
            db.create_oidc_user(
                user_id="u-new",
                username="bob",
                display_name="Bob",
                password_hash="!oidc",
                issuer="https://idp.example.com",
                subject="sub-1",
                email="bob@example.com",
            )

        # The candidate user row was rolled back.
        assert db.get_user("u-new") is None
        assert db.get_user_by_username("bob") is None
        # The pre-existing identity still points at the original user.
        identity = db.get_oidc_identity("https://idp.example.com", "sub-1")
        assert identity is not None
        assert identity["user_id"] == "u-other"


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


# ---------------------------------------------------------------------------
# count_users / find_existing_usernames
# ---------------------------------------------------------------------------


class TestCountUsers:
    def test_count_users_empty(self, db):
        assert db.count_users() == 0

    def test_count_users_after_inserts(self, db):
        db.create_user("u1", "alice", "Alice", "h1")
        db.create_user("u2", "bob", "Bob", "h2")
        db.create_user("u3", "carol", "Carol", "h3")
        assert db.count_users() == 3


class TestFindExistingUsernames:
    def test_empty_input_returns_empty_set(self, db):
        db.create_user("u1", "alice", "Alice", "h1")
        assert db.find_existing_usernames([]) == set()

    def test_returns_subset_present_in_db(self, db):
        db.create_user("u1", "alice", "Alice", "h1")
        db.create_user("u2", "bob", "Bob", "h2")

        existing = db.find_existing_usernames(["alice", "bob", "carol", "dave"])
        assert existing == {"alice", "bob"}

    def test_no_matches_returns_empty_set(self, db):
        db.create_user("u1", "alice", "Alice", "h1")
        assert db.find_existing_usernames(["bob", "carol"]) == set()


# ---------------------------------------------------------------------------
# replace_oidc_roles
# ---------------------------------------------------------------------------


class TestReplaceOIDCRoles:
    def _seed_role(self, db, role_id):
        db.create_role(role_id, role_id, role_id, "perm.read", False, "")

    def test_inserts_added_roles(self, db):
        db.create_user("u1", "alice", "Alice", "h")
        self._seed_role(db, "role-a")
        self._seed_role(db, "role-b")

        added, removed = db.replace_oidc_roles("u1", {"role-a", "role-b"})

        assert added == {"role-a", "role-b"}
        assert removed == set()
        roles = {r["role_id"] for r in db.list_user_roles("u1")}
        assert roles == {"role-a", "role-b"}

    def test_removes_stale_oidc_roles(self, db):
        db.create_user("u1", "alice", "Alice", "h")
        self._seed_role(db, "role-a")
        self._seed_role(db, "role-b")
        db.assign_role("u1", "role-a", "oidc")
        db.assign_role("u1", "role-b", "oidc")

        added, removed = db.replace_oidc_roles("u1", {"role-a"})

        assert added == set()
        assert removed == {"role-b"}
        roles = {r["role_id"] for r in db.list_user_roles("u1")}
        assert roles == {"role-a"}

    def test_preserves_non_oidc_roles(self, db):
        """Manually-assigned and oidc-default rows are NOT touched."""
        db.create_user("u1", "alice", "Alice", "h")
        self._seed_role(db, "role-manual")
        self._seed_role(db, "role-default")
        self._seed_role(db, "role-oidc-old")
        db.assign_role("u1", "role-manual", "admin-ui")
        db.assign_role("u1", "role-default", "oidc-default")
        db.assign_role("u1", "role-oidc-old", "oidc")

        added, removed = db.replace_oidc_roles("u1", set())

        # Only the oidc-assigned row was diffed
        assert added == set()
        assert removed == {"role-oidc-old"}

        roles = {r["role_id"]: r["assigned_by"] for r in db.list_user_roles("u1")}
        assert roles == {
            "role-manual": "admin-ui",
            "role-default": "oidc-default",
        }

    def test_no_op_when_desired_matches_current(self, db):
        db.create_user("u1", "alice", "Alice", "h")
        self._seed_role(db, "role-a")
        db.assign_role("u1", "role-a", "oidc")

        added, removed = db.replace_oidc_roles("u1", {"role-a"})

        assert added == set()
        assert removed == set()
        assert {r["role_id"] for r in db.list_user_roles("u1")} == {"role-a"}

    def test_empty_user_no_oidc_history(self, db):
        db.create_user("u1", "alice", "Alice", "h")

        added, removed = db.replace_oidc_roles("u1", set())

        assert added == set()
        assert removed == set()

    def test_desired_role_blocked_by_admin_ui_assignment(self, db):
        """Desired role already held via admin-ui: untouched, no PK conflict."""
        db.create_user("u1", "alice", "Alice", "h")
        self._seed_role(db, "role-a")
        db.assign_role("u1", "role-a", "admin-ui")

        added, removed = db.replace_oidc_roles("u1", {"role-a"})

        assert added == set()
        assert removed == set()
        roles = {r["role_id"]: r["assigned_by"] for r in db.list_user_roles("u1")}
        assert roles == {"role-a": "admin-ui"}

    def test_desired_role_blocked_by_oidc_default_assignment(self, db):
        """Desired role already held via oidc-default fallback: untouched."""
        db.create_user("u1", "alice", "Alice", "h")
        self._seed_role(db, "role-a")
        db.assign_role("u1", "role-a", "oidc-default")

        added, removed = db.replace_oidc_roles("u1", {"role-a"})

        assert added == set()
        assert removed == set()
        roles = {r["role_id"]: r["assigned_by"] for r in db.list_user_roles("u1")}
        assert roles == {"role-a": "oidc-default"}

    def test_desired_role_added_alongside_blocked_role(self, db):
        """Mixed case: one desired role is blocked (admin-ui), the other inserts cleanly."""
        db.create_user("u1", "alice", "Alice", "h")
        self._seed_role(db, "role-a")
        self._seed_role(db, "role-b")
        db.assign_role("u1", "role-a", "admin-ui")

        added, removed = db.replace_oidc_roles("u1", {"role-a", "role-b"})

        assert added == {"role-b"}
        assert removed == set()
        roles = {r["role_id"]: r["assigned_by"] for r in db.list_user_roles("u1")}
        assert roles == {"role-a": "admin-ui", "role-b": "oidc"}

    def test_revoke_only_oidc_assigned_roles(self, db):
        """OIDC-assigned roles get revoked when not in desired; admin-ui rows survive."""
        db.create_user("u1", "alice", "Alice", "h")
        self._seed_role(db, "role-manual")
        self._seed_role(db, "role-oidc-old")
        self._seed_role(db, "role-default")
        db.assign_role("u1", "role-manual", "admin-ui")
        db.assign_role("u1", "role-oidc-old", "oidc")
        db.assign_role("u1", "role-default", "oidc-default")

        added, removed = db.replace_oidc_roles("u1", set())

        assert added == set()
        assert removed == {"role-oidc-old"}
        roles = {r["role_id"]: r["assigned_by"] for r in db.list_user_roles("u1")}
        assert roles == {"role-manual": "admin-ui", "role-default": "oidc-default"}
