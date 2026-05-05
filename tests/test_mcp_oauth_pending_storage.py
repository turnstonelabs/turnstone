"""Smoke tests for the new OAuth-MCP storage tables.

Phase 2 only adds the schema — token CRUD lands in Phase 3 and pending-
state CRUD in Phase 4.  These tests verify the tables exist after
``init_storage`` and accept the documented row shape via raw SQL.
"""

from __future__ import annotations

import sqlalchemy as sa

from turnstone.core.storage._schema import mcp_oauth_pending, mcp_user_tokens


class TestMcpUserTokensTable:
    def test_table_exists_and_accepts_row(self, backend) -> None:
        with backend._engine.connect() as conn:
            conn.execute(
                sa.insert(mcp_user_tokens),
                {
                    "user_id": "u1",
                    "server_name": "srv-a",
                    "access_token_ct": b"\x00ciphertext-a",
                    "refresh_token_ct": b"\x00ciphertext-r",
                    "expires_at": "2026-05-04T12:00:00",
                    "scopes": "openid profile",
                    "as_issuer": "https://auth.example.com",
                    "audience": "https://mcp.example.com",
                    "created": "2026-05-04T11:00:00",
                    "last_refreshed": None,
                },
            )
            conn.commit()
            row = conn.execute(
                sa.select(mcp_user_tokens).where(
                    (mcp_user_tokens.c.user_id == "u1") & (mcp_user_tokens.c.server_name == "srv-a")
                )
            ).one()
        assert row.access_token_ct == b"\x00ciphertext-a"
        assert row.refresh_token_ct == b"\x00ciphertext-r"
        assert row.scopes == "openid profile"
        assert row.audience == "https://mcp.example.com"

    def test_composite_pk_distinguishes_user_server(self, backend) -> None:
        """Same user, different server => two rows; same (user, server) => conflict."""
        with backend._engine.connect() as conn:
            conn.execute(
                sa.insert(mcp_user_tokens),
                [
                    {
                        "user_id": "u1",
                        "server_name": "srv-a",
                        "access_token_ct": b"a",
                        "refresh_token_ct": None,
                        "expires_at": None,
                        "scopes": None,
                        "as_issuer": "https://auth.example.com",
                        "audience": "https://a.example.com",
                        "created": "2026-05-04T11:00:00",
                        "last_refreshed": None,
                    },
                    {
                        "user_id": "u1",
                        "server_name": "srv-b",
                        "access_token_ct": b"b",
                        "refresh_token_ct": None,
                        "expires_at": None,
                        "scopes": None,
                        "as_issuer": "https://auth.example.com",
                        "audience": "https://b.example.com",
                        "created": "2026-05-04T11:00:00",
                        "last_refreshed": None,
                    },
                ],
            )
            conn.commit()
            count = conn.execute(sa.select(sa.func.count()).select_from(mcp_user_tokens)).scalar()
        assert count == 2


class TestMcpOauthPendingTable:
    def test_table_exists_and_accepts_row(self, backend) -> None:
        with backend._engine.connect() as conn:
            conn.execute(
                sa.insert(mcp_oauth_pending),
                {
                    "state": "rand-state-xyz",
                    "user_id": "u1",
                    "server_name": "srv-a",
                    "code_verifier": "verifier-blob",
                    "return_url": "/admin/mcp-servers",
                    "created_at": "2026-05-04T11:00:00",
                },
            )
            conn.commit()
            row = conn.execute(
                sa.select(mcp_oauth_pending).where(mcp_oauth_pending.c.state == "rand-state-xyz")
            ).one()
        assert row.user_id == "u1"
        assert row.server_name == "srv-a"
        assert row.return_url == "/admin/mcp-servers"

    def test_state_pk_unique(self, backend) -> None:
        """A second insert with the same state value raises IntegrityError."""
        with backend._engine.connect() as conn:
            conn.execute(
                sa.insert(mcp_oauth_pending),
                {
                    "state": "dup-state",
                    "user_id": "u1",
                    "server_name": "srv-a",
                    "code_verifier": "v",
                    "return_url": "/x",
                    "created_at": "2026-05-04T11:00:00",
                },
            )
            conn.commit()
        import pytest
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError), backend._engine.connect() as conn:
            conn.execute(
                sa.insert(mcp_oauth_pending),
                {
                    "state": "dup-state",
                    "user_id": "u2",
                    "server_name": "srv-b",
                    "code_verifier": "v",
                    "return_url": "/y",
                    "created_at": "2026-05-04T11:01:00",
                },
            )
            conn.commit()
