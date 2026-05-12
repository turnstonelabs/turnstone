"""Integration tests for the Phase 9 admin bulk-revoke endpoint.

POST /v1/api/admin/mcp-servers/{name}/bulk-revoke clears every user's
OAuth token for a server (admin-side counterpart to the per-user
DELETE /v1/api/mcp/oauth/connections/{server_name} that shipped in
Phase 8).

Coverage:
- requires ``admin.mcp`` permission (401/403 without).
- 404 when the named server is missing.
- 400 when the server's ``auth_type`` is not ``oauth_user``.
- 200 + ``rows_deleted`` + ``consented_users_before`` on success.
- Audit row written with
  ``upstream_revoke_outcome="bulk_admin_no_upstream"``.
- Token rows are gone from ``mcp_user_tokens`` post-call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from turnstone.console.server import admin_mcp_bulk_revoke
from turnstone.core.auth import AuthResult
from turnstone.core.storage._sqlite import SQLiteBackend

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response


class _InjectAdminMcp(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id="admin-user",
            scopes=frozenset({"approve"}),
            token_source="config",
            permissions=frozenset({"read", "write", "approve", "admin.mcp"}),
        )
        return await call_next(request)


class _InjectNoAdminMcp(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id="regular-user",
            scopes=frozenset({"approve"}),
            token_source="jwt",
            permissions=frozenset({"read", "write", "approve"}),
        )
        return await call_next(request)


def _build_app(storage: SQLiteBackend, *, with_admin_mcp: bool = True) -> Starlette:
    mw = _InjectAdminMcp if with_admin_mcp else _InjectNoAdminMcp
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route(
                        "/api/admin/mcp-servers/{name}/bulk-revoke",
                        admin_mcp_bulk_revoke,
                        methods=["POST"],
                    ),
                ],
            ),
        ],
        middleware=[Middleware(mw)],
    )
    app.state.auth_storage = storage
    return app


@pytest.fixture
def storage(tmp_path: Any) -> SQLiteBackend:
    return SQLiteBackend(str(tmp_path / "test.db"))


def _seed_oauth_server(
    backend: SQLiteBackend,
    *,
    name: str = "srv-oauth",
    server_id: str = "srv-oauth-id",
) -> None:
    backend.create_mcp_server(
        server_id=server_id,
        name=name,
        transport="streamable-http",
        url="https://example.com/mcp",
        auth_type="oauth_user",
    )


def _seed_static_server(
    backend: SQLiteBackend,
    *,
    name: str = "srv-static",
    server_id: str = "srv-static-id",
) -> None:
    backend.create_mcp_server(
        server_id=server_id,
        name=name,
        transport="streamable-http",
        url="https://example.com/mcp",
        auth_type="static",
    )


def _seed_user_tokens(backend: SQLiteBackend, server_name: str, users: int) -> None:
    for i in range(users):
        backend.create_mcp_user_token(
            f"user-{i}",
            server_name,
            access_token_ct=b"ct",
            refresh_token_ct=None,
            expires_at=None,
            scopes=None,
            as_issuer="https://as.example.com",
            audience="https://example.com/mcp",
        )


def test_requires_admin_mcp_permission(storage: SQLiteBackend) -> None:
    _seed_oauth_server(storage)
    client = TestClient(_build_app(storage, with_admin_mcp=False))
    resp = client.post("/v1/api/admin/mcp-servers/srv-oauth/bulk-revoke")
    assert resp.status_code == 403


def test_404_on_missing_server(storage: SQLiteBackend) -> None:
    client = TestClient(_build_app(storage))
    resp = client.post("/v1/api/admin/mcp-servers/never-existed/bulk-revoke")
    assert resp.status_code == 404
    assert resp.json() == {"error": "No such server"}


def test_400_on_static_server(storage: SQLiteBackend) -> None:
    _seed_static_server(storage)
    client = TestClient(_build_app(storage))
    resp = client.post("/v1/api/admin/mcp-servers/srv-static/bulk-revoke")
    assert resp.status_code == 400
    body = resp.json()
    assert "oauth_user" in body["error"]


def test_400_on_invalid_server_name(storage: SQLiteBackend) -> None:
    # double-underscore is reserved for the prefixed-tool-name encoding.
    client = TestClient(_build_app(storage))
    resp = client.post("/v1/api/admin/mcp-servers/bad__name/bulk-revoke")
    assert resp.status_code == 400


def test_200_on_success_with_no_consented_users(storage: SQLiteBackend) -> None:
    _seed_oauth_server(storage)
    client = TestClient(_build_app(storage))
    resp = client.post("/v1/api/admin/mcp-servers/srv-oauth/bulk-revoke")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["rows_deleted"] == 0
    assert body["consented_users_before"] == 0


def test_200_clears_all_user_tokens(storage: SQLiteBackend) -> None:
    _seed_oauth_server(storage)
    _seed_user_tokens(storage, "srv-oauth", users=3)
    # Token for another server must survive the bulk-revoke.
    _seed_oauth_server(storage, name="srv-other", server_id="srv-other-id")
    _seed_user_tokens(storage, "srv-other", users=2)

    client = TestClient(_build_app(storage))
    resp = client.post("/v1/api/admin/mcp-servers/srv-oauth/bulk-revoke")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["rows_deleted"] == 3
    assert body["consented_users_before"] == 3

    # Target server's tokens are gone; bystander's tokens survive.
    assert storage.count_mcp_consented_users_by_server("srv-oauth") == 0
    assert storage.count_mcp_consented_users_by_server("srv-other") == 2


def test_audits_with_bulk_admin_no_upstream(storage: SQLiteBackend) -> None:
    _seed_oauth_server(storage)
    _seed_user_tokens(storage, "srv-oauth", users=2)
    client = TestClient(_build_app(storage))
    resp = client.post("/v1/api/admin/mcp-servers/srv-oauth/bulk-revoke")
    assert resp.status_code == 200

    # Pull the most-recent audit row for the bulk_revoked action and
    # verify it carries the deferral marker.
    events = storage.list_audit_events(limit=10)
    bulk_rows = [e for e in events if e.get("action") == "mcp_server.oauth.bulk_revoked"]
    assert len(bulk_rows) == 1
    detail = bulk_rows[0].get("detail")
    if isinstance(detail, str):
        import json as _json

        detail = _json.loads(detail)
    assert detail.get("upstream_revoke_outcome") == "bulk_admin_no_upstream"
    assert detail.get("rows_deleted") == 2
    assert detail.get("consented_users_before") == 2
    assert detail.get("name") == "srv-oauth"
