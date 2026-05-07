"""Storage CRUD tests for the per-(user, server) MCP OAuth pending-state table.

Validates the storage-protocol additions for the per-(user, server)
OAuth flow:

- ``create_mcp_oauth_pending_state``
- ``pop_mcp_oauth_pending_state`` (atomic, with TTL)
- ``cleanup_expired_mcp_oauth_pending_states``
- ``get_mcp_oauth_client_secret_ct``
"""

from __future__ import annotations

import sqlalchemy as sa


class TestCreateAndPop:
    def test_round_trip(self, backend) -> None:
        backend.create_mcp_oauth_pending_state(
            "state-1",
            "user-a",
            "srv-x",
            "verifier-blob",
            "/admin/mcp-servers",
        )
        row = backend.pop_mcp_oauth_pending_state("state-1", max_age_seconds=600)
        assert row is not None
        assert row["state"] == "state-1"
        assert row["user_id"] == "user-a"
        assert row["server_name"] == "srv-x"
        assert row["code_verifier"] == "verifier-blob"
        assert row["return_url"] == "/admin/mcp-servers"

    def test_pop_consumes_row(self, backend) -> None:
        backend.create_mcp_oauth_pending_state("s2", "u", "s", "v", "/r")
        first = backend.pop_mcp_oauth_pending_state("s2")
        assert first is not None
        # Second pop must miss — row was consumed.
        second = backend.pop_mcp_oauth_pending_state("s2")
        assert second is None

    def test_pop_missing_returns_none(self, backend) -> None:
        assert backend.pop_mcp_oauth_pending_state("never-existed") is None


class TestTTL:
    def test_pop_rejects_expired_row(self, backend) -> None:
        backend.create_mcp_oauth_pending_state("old-state", "u", "s", "v", "/r")
        # Backdate it so it's older than the TTL window.
        with backend._engine.connect() as conn:
            conn.execute(
                sa.text(
                    "UPDATE mcp_oauth_pending SET created_at = '2020-01-01T00:00:00' "
                    "WHERE state = 'old-state'"
                )
            )
            conn.commit()

        # Default TTL is 600s — the row is decades old.
        row = backend.pop_mcp_oauth_pending_state("old-state")
        assert row is None

        # Even though pop returned None, the row must have been wiped — a
        # second pop with a giant TTL must still see nothing.
        again = backend.pop_mcp_oauth_pending_state("old-state", max_age_seconds=10**9)
        assert again is None

    def test_pop_accepts_fresh_row(self, backend) -> None:
        backend.create_mcp_oauth_pending_state("fresh", "u", "s", "v", "/r")
        row = backend.pop_mcp_oauth_pending_state("fresh", max_age_seconds=600)
        assert row is not None
        assert row["state"] == "fresh"


class TestCleanup:
    def test_cleanup_deletes_only_expired(self, backend) -> None:
        backend.create_mcp_oauth_pending_state("old", "u", "s", "v", "/r")
        backend.create_mcp_oauth_pending_state("new", "u", "s", "v", "/r")
        with backend._engine.connect() as conn:
            conn.execute(
                sa.text(
                    "UPDATE mcp_oauth_pending SET created_at = '2020-01-01T00:00:00' "
                    "WHERE state = 'old'"
                )
            )
            conn.commit()

        deleted = backend.cleanup_expired_mcp_oauth_pending_states(max_age_seconds=600)
        assert deleted == 1
        # Old gone, new still around.
        assert backend.pop_mcp_oauth_pending_state("old") is None
        survivor = backend.pop_mcp_oauth_pending_state("new")
        assert survivor is not None

    def test_cleanup_no_rows(self, backend) -> None:
        assert backend.cleanup_expired_mcp_oauth_pending_states() == 0


class TestGetOAuthClientSecretCt:
    def test_returns_none_when_unset(self, backend) -> None:
        backend.create_mcp_server(
            server_id="srv-id",
            name="srv-x",
            transport="streamable-http",
            url="https://mcp.example.com/sse",
            auth_type="oauth_user",
        )
        assert backend.get_mcp_oauth_client_secret_ct("srv-id") is None

    def test_returns_ciphertext_after_set(self, backend) -> None:
        backend.create_mcp_server(
            server_id="srv-id",
            name="srv-x",
            transport="streamable-http",
            url="https://mcp.example.com/sse",
            auth_type="oauth_user",
        )
        ct = b"\x00\xff\x42encrypted-blob"
        ok = backend.set_mcp_oauth_client_secret_ct("srv-id", ct)
        assert ok is True
        out = backend.get_mcp_oauth_client_secret_ct("srv-id")
        assert out == ct

    def test_returns_none_for_missing_server(self, backend) -> None:
        assert backend.get_mcp_oauth_client_secret_ct("does-not-exist") is None


def _create_user_token_row(
    backend,
    *,
    user_id: str,
    server_name: str,
    created: str,
) -> None:
    """Insert a token row + backdate ``created`` so ordering is deterministic.

    The storage helper stamps ``created`` from ``datetime.now(UTC)``; for
    multi-row ordering tests we backdate via raw SQL so the inserts stay
    independent of clock resolution.
    """
    backend.create_mcp_user_token(
        user_id,
        server_name,
        access_token_ct=b"ct-access",
        refresh_token_ct=b"ct-refresh",
        expires_at="2026-05-04T12:00:00",
        scopes="openid",
        as_issuer="https://auth.example.com",
        audience="https://mcp.example.com",
    )
    with backend._engine.connect() as conn:
        conn.execute(
            sa.text(
                "UPDATE mcp_user_tokens SET created = :created "
                "WHERE user_id = :uid AND server_name = :sn"
            ),
            {"created": created, "uid": user_id, "sn": server_name},
        )
        conn.commit()


class TestListMCPUserTokenMetadataByUser:
    def test_list_mcp_user_token_metadata_by_user_empty(self, backend) -> None:
        assert backend.list_mcp_user_token_metadata_by_user("nobody") == []

    def test_list_mcp_user_token_metadata_by_user_single_server(self, backend) -> None:
        _create_user_token_row(
            backend, user_id="u1", server_name="srv-a", created="2026-05-01T00:00:00"
        )
        rows = backend.list_mcp_user_token_metadata_by_user("u1")
        assert len(rows) == 1
        assert rows[0]["user_id"] == "u1"
        assert rows[0]["server_name"] == "srv-a"
        assert rows[0]["as_issuer"] == "https://auth.example.com"
        assert rows[0]["audience"] == "https://mcp.example.com"
        assert rows[0]["scopes"] == "openid"
        # Projection MUST omit ciphertext columns — the SQL no longer
        # selects them, so the TypedDict has no key.
        assert "access_token_ct" not in rows[0]
        assert "refresh_token_ct" not in rows[0]

    def test_list_mcp_user_token_metadata_by_user_multiple_servers(self, backend) -> None:
        _create_user_token_row(
            backend, user_id="u1", server_name="srv-c", created="2026-05-03T00:00:00"
        )
        _create_user_token_row(
            backend, user_id="u1", server_name="srv-a", created="2026-05-01T00:00:00"
        )
        _create_user_token_row(
            backend, user_id="u1", server_name="srv-b", created="2026-05-02T00:00:00"
        )
        rows = backend.list_mcp_user_token_metadata_by_user("u1")
        assert [r["server_name"] for r in rows] == ["srv-a", "srv-b", "srv-c"]

    def test_list_mcp_user_token_metadata_by_user_isolates_by_user(self, backend) -> None:
        _create_user_token_row(
            backend, user_id="user-a", server_name="srv-a", created="2026-05-01T00:00:00"
        )
        _create_user_token_row(
            backend, user_id="user-a", server_name="srv-b", created="2026-05-02T00:00:00"
        )
        _create_user_token_row(
            backend, user_id="user-b", server_name="srv-a", created="2026-05-03T00:00:00"
        )
        rows_a = backend.list_mcp_user_token_metadata_by_user("user-a")
        assert {r["server_name"] for r in rows_a} == {"srv-a", "srv-b"}
        assert all(r["user_id"] == "user-a" for r in rows_a)

        rows_b = backend.list_mcp_user_token_metadata_by_user("user-b")
        assert len(rows_b) == 1
        assert rows_b[0]["user_id"] == "user-b"
        assert rows_b[0]["server_name"] == "srv-a"
