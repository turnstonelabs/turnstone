"""Storage CRUD tests for the Phase 9 ``mcp_pending_consent`` table.

Validates protocol additions backing the dashboard pending-consent badge:

- ``upsert_mcp_pending_consent`` — insert + on-conflict refresh
- ``list_mcp_pending_consent_by_user`` — read path
- ``delete_mcp_pending_consent`` — single-row clear
- ``delete_all_mcp_pending_consent_by_user`` — bulk clear
- ``count_mcp_consented_users_by_server`` — admin status pill
- ``any_oauth_user_mcp_servers`` — install-level gate
"""

from __future__ import annotations


def _iso(ts: str = "2026-05-11T12:00:00") -> str:
    return ts


class TestUpsertAndList:
    def test_insert_round_trip(self, backend) -> None:
        backend.upsert_mcp_pending_consent(
            user_id="user-a",
            server_name="srv-x",
            error_code="mcp_consent_required",
            scopes_required="read write",
            last_ws_id="ws-1",
            last_tool_call_id="tool-1",
            now_iso=_iso(),
        )
        rows = backend.list_mcp_pending_consent_by_user("user-a")
        assert len(rows) == 1
        r = rows[0]
        assert r["user_id"] == "user-a"
        assert r["server_name"] == "srv-x"
        assert r["error_code"] == "mcp_consent_required"
        assert r["scopes_required"] == "read write"
        assert r["last_ws_id"] == "ws-1"
        assert r["last_tool_call_id"] == "tool-1"
        assert r["occurrence_count"] == 1
        assert r["first_seen_at"] == r["last_seen_at"]

    def test_upsert_bumps_count_and_refreshes_recency(self, backend) -> None:
        backend.upsert_mcp_pending_consent(
            user_id="user-a",
            server_name="srv-x",
            error_code="mcp_consent_required",
            scopes_required=None,
            last_ws_id=None,
            last_tool_call_id=None,
            now_iso="2026-05-11T12:00:00",
        )
        backend.upsert_mcp_pending_consent(
            user_id="user-a",
            server_name="srv-x",
            error_code="mcp_insufficient_scope",
            scopes_required="read",
            last_ws_id="ws-2",
            last_tool_call_id="tool-2",
            now_iso="2026-05-11T13:00:00",
        )
        rows = backend.list_mcp_pending_consent_by_user("user-a")
        assert len(rows) == 1
        r = rows[0]
        # Recency fields refreshed to the second call's values; count bumped.
        assert r["occurrence_count"] == 2
        assert r["error_code"] == "mcp_insufficient_scope"
        assert r["scopes_required"] == "read"
        assert r["last_ws_id"] == "ws-2"
        assert r["last_tool_call_id"] == "tool-2"
        assert r["last_seen_at"] == "2026-05-11T13:00:00"
        # first_seen_at preserved — that's the load-bearing audit value.
        assert r["first_seen_at"] == "2026-05-11T12:00:00"

    def test_list_orders_by_last_seen_desc(self, backend) -> None:
        backend.upsert_mcp_pending_consent(
            user_id="user-a",
            server_name="srv-old",
            error_code="mcp_consent_required",
            scopes_required=None,
            last_ws_id=None,
            last_tool_call_id=None,
            now_iso="2026-05-11T10:00:00",
        )
        backend.upsert_mcp_pending_consent(
            user_id="user-a",
            server_name="srv-new",
            error_code="mcp_consent_required",
            scopes_required=None,
            last_ws_id=None,
            last_tool_call_id=None,
            now_iso="2026-05-11T11:00:00",
        )
        rows = backend.list_mcp_pending_consent_by_user("user-a")
        assert [r["server_name"] for r in rows] == ["srv-new", "srv-old"]

    def test_per_user_isolation(self, backend) -> None:
        backend.upsert_mcp_pending_consent(
            user_id="user-a",
            server_name="srv",
            error_code="mcp_consent_required",
            scopes_required=None,
            last_ws_id=None,
            last_tool_call_id=None,
            now_iso=_iso(),
        )
        assert backend.list_mcp_pending_consent_by_user("user-b") == []


class TestDelete:
    def test_delete_single(self, backend) -> None:
        backend.upsert_mcp_pending_consent(
            user_id="user-a",
            server_name="srv-x",
            error_code="mcp_consent_required",
            scopes_required=None,
            last_ws_id=None,
            last_tool_call_id=None,
            now_iso=_iso(),
        )
        assert backend.delete_mcp_pending_consent("user-a", "srv-x") is True
        assert backend.list_mcp_pending_consent_by_user("user-a") == []
        # Second delete returns False (no row).
        assert backend.delete_mcp_pending_consent("user-a", "srv-x") is False

    def test_delete_missing_returns_false(self, backend) -> None:
        assert backend.delete_mcp_pending_consent("never", "missing") is False

    def test_delete_all_by_user(self, backend) -> None:
        for name in ("srv-a", "srv-b", "srv-c"):
            backend.upsert_mcp_pending_consent(
                user_id="user-a",
                server_name=name,
                error_code="mcp_consent_required",
                scopes_required=None,
                last_ws_id=None,
                last_tool_call_id=None,
                now_iso=_iso(),
            )
        # Cross-user row that must NOT be touched.
        backend.upsert_mcp_pending_consent(
            user_id="user-b",
            server_name="srv-z",
            error_code="mcp_consent_required",
            scopes_required=None,
            last_ws_id=None,
            last_tool_call_id=None,
            now_iso=_iso(),
        )
        assert backend.delete_all_mcp_pending_consent_by_user("user-a") == 3
        assert backend.list_mcp_pending_consent_by_user("user-a") == []
        assert len(backend.list_mcp_pending_consent_by_user("user-b")) == 1


class TestCountConsentedUsersByServer:
    def _seed_server(self, backend, name: str = "srv-x") -> None:
        backend.create_mcp_server(
            server_id="srv-id-" + name,
            name=name,
            transport="streamable-http",
            command="",
            args="[]",
            url="https://example.com/mcp",
            headers="{}",
            env="{}",
            auto_approve=False,
            enabled=True,
            created_by="admin",
        )
        backend.update_mcp_server("srv-id-" + name, auth_type="oauth_user")

    def test_counts_distinct_non_expired_users(self, backend) -> None:
        self._seed_server(backend)
        future = "2099-01-01T00:00:00"
        backend.create_mcp_user_token(
            "alice",
            "srv-x",
            access_token_ct=b"ct",
            refresh_token_ct=None,
            expires_at=future,
            scopes=None,
            as_issuer="https://as.example.com",
            audience="https://example.com/mcp",
        )
        backend.create_mcp_user_token(
            "bob",
            "srv-x",
            access_token_ct=b"ct",
            refresh_token_ct=None,
            expires_at=None,  # null treated as non-expired
            scopes=None,
            as_issuer="https://as.example.com",
            audience="https://example.com/mcp",
        )
        # Different server — must not count.
        self._seed_server(backend, name="srv-y")
        backend.create_mcp_user_token(
            "carol",
            "srv-y",
            access_token_ct=b"ct",
            refresh_token_ct=None,
            expires_at=future,
            scopes=None,
            as_issuer="https://as.example.com",
            audience="https://example.com/mcp",
        )
        assert backend.count_mcp_consented_users_by_server("srv-x") == 2
        assert backend.count_mcp_consented_users_by_server("srv-y") == 1

    def test_excludes_expired(self, backend) -> None:
        self._seed_server(backend)
        backend.create_mcp_user_token(
            "alice",
            "srv-x",
            access_token_ct=b"ct",
            refresh_token_ct=None,
            expires_at="2020-01-01T00:00:00",  # well in the past
            scopes=None,
            as_issuer="https://as.example.com",
            audience="https://example.com/mcp",
        )
        assert backend.count_mcp_consented_users_by_server("srv-x") == 0

    def test_zero_when_no_rows(self, backend) -> None:
        assert backend.count_mcp_consented_users_by_server("missing") == 0


class TestInstallGate:
    def test_any_oauth_user_returns_false_on_empty(self, backend) -> None:
        assert backend.any_oauth_user_mcp_servers() is False

    def test_any_oauth_user_ignores_static_rows(self, backend) -> None:
        backend.create_mcp_server(
            server_id="srv-1",
            name="static-only",
            transport="streamable-http",
            command="",
            args="[]",
            url="https://example.com",
            headers='{"Authorization": "Bearer x"}',
            env="{}",
            auto_approve=False,
            enabled=True,
            created_by="admin",
        )
        assert backend.any_oauth_user_mcp_servers() is False

    def test_any_oauth_user_returns_true_when_one_exists(self, backend) -> None:
        backend.create_mcp_server(
            server_id="srv-2",
            name="oauth-srv",
            transport="streamable-http",
            command="",
            args="[]",
            url="https://example.com",
            headers="{}",
            env="{}",
            auto_approve=False,
            enabled=True,
            created_by="admin",
        )
        backend.update_mcp_server("srv-2", auth_type="oauth_user")
        assert backend.any_oauth_user_mcp_servers() is True
