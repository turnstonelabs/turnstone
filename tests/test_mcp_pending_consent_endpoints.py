"""HTTP tests for the Phase 9 pending-consent endpoints.

Covers:
- ``GET /v1/api/mcp/oauth/pending`` (install gate + read path)
- ``DELETE /v1/api/mcp/oauth/pending/{server_name}`` (single clear)
- ``DELETE /v1/api/mcp/oauth/pending`` (bulk clear)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from turnstone.core.auth import AuthResult
from turnstone.core.mcp_oauth import (
    handle_mcp_oauth_clear_all_pending,
    handle_mcp_oauth_clear_pending,
    handle_mcp_oauth_list_pending,
)
from turnstone.core.storage._sqlite import SQLiteBackend

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response


class _InjectAuthMiddleware(BaseHTTPMiddleware):
    """Stamp a fixed authenticated user on every request."""

    def __init__(self, app: Any, user_id: str = "user-1") -> None:
        super().__init__(app)
        self._user_id = user_id

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id=self._user_id,
            scopes=frozenset({"write"}),
            token_source="config",
            permissions=frozenset({"read", "write"}),
        )
        return await call_next(request)


def _build_app(storage: SQLiteBackend, *, user_id: str = "user-1") -> Starlette:
    class _Mw(_InjectAuthMiddleware):
        def __init__(self, app: Any) -> None:
            super().__init__(app, user_id=user_id)

    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route("/api/mcp/oauth/pending", handle_mcp_oauth_list_pending),
                    Route(
                        "/api/mcp/oauth/pending",
                        handle_mcp_oauth_clear_all_pending,
                        methods=["DELETE"],
                    ),
                    Route(
                        "/api/mcp/oauth/pending/{server_name}",
                        handle_mcp_oauth_clear_pending,
                        methods=["DELETE"],
                    ),
                ],
            ),
        ],
        middleware=[Middleware(_Mw)],
    )
    app.state.auth_storage = storage
    return app


@pytest.fixture
def storage(tmp_path: Any) -> SQLiteBackend:
    backend = SQLiteBackend(str(tmp_path / "test.db"))
    backend.create_user("user-1", "user1", "User One", "hash")
    backend.create_user("user-2", "user2", "User Two", "hash")
    return backend


def _seed_oauth_server(backend: SQLiteBackend, *, name: str = "srv-x") -> None:
    backend.create_mcp_server(
        server_id="srv-id-" + name,
        name=name,
        transport="streamable-http",
        url="https://example.com/mcp",
        auth_type="oauth_user",
    )


def _seed_pending(
    backend: SQLiteBackend,
    *,
    user_id: str = "user-1",
    server_name: str = "srv-x",
    error_code: str = "mcp_consent_required",
    now_iso: str = "2026-05-11T12:00:00",
) -> None:
    backend.upsert_mcp_pending_consent(
        user_id=user_id,
        server_name=server_name,
        error_code=error_code,
        scopes_required=None,
        last_ws_id=None,
        last_tool_call_id=None,
        now_iso=now_iso,
    )


class TestListPending:
    def test_install_gate_short_circuits_on_no_oauth_servers(self, storage: SQLiteBackend) -> None:
        # Seed a pending row but NO oauth_user MCP server — the gate
        # must short-circuit to {pending: 0} regardless.
        _seed_pending(storage)
        client = TestClient(_build_app(storage))
        resp = client.get("/v1/api/mcp/oauth/pending")
        assert resp.status_code == 200
        assert resp.json() == {"pending": 0, "servers": []}

    def test_lists_pending_records_for_authenticated_user(self, storage: SQLiteBackend) -> None:
        _seed_oauth_server(storage)
        _seed_pending(storage)
        client = TestClient(_build_app(storage))
        resp = client.get("/v1/api/mcp/oauth/pending")
        assert resp.status_code == 200
        body = resp.json()
        assert body["pending"] == 1
        assert len(body["servers"]) == 1
        assert body["servers"][0]["server_name"] == "srv-x"
        assert body["servers"][0]["error_code"] == "mcp_consent_required"

    def test_does_not_leak_cross_user_records(self, storage: SQLiteBackend) -> None:
        _seed_oauth_server(storage)
        _seed_pending(storage, user_id="user-2")
        client = TestClient(_build_app(storage, user_id="user-1"))
        resp = client.get("/v1/api/mcp/oauth/pending")
        assert resp.status_code == 200
        assert resp.json() == {"pending": 0, "servers": []}


class TestClearPending:
    def test_delete_single(self, storage: SQLiteBackend) -> None:
        _seed_oauth_server(storage)
        _seed_pending(storage)
        client = TestClient(_build_app(storage))
        resp = client.delete("/v1/api/mcp/oauth/pending/srv-x")
        assert resp.status_code == 204
        assert storage.list_mcp_pending_consent_by_user("user-1") == []

    def test_delete_missing_still_returns_204(self, storage: SQLiteBackend) -> None:
        # Idempotent — must not leak cross-user existence info via 404.
        _seed_oauth_server(storage)
        client = TestClient(_build_app(storage))
        resp = client.delete("/v1/api/mcp/oauth/pending/never-existed")
        assert resp.status_code == 204

    def test_delete_does_not_touch_cross_user_rows(self, storage: SQLiteBackend) -> None:
        _seed_oauth_server(storage)
        _seed_pending(storage, user_id="user-1")
        _seed_pending(storage, user_id="user-2")
        client = TestClient(_build_app(storage, user_id="user-1"))
        resp = client.delete("/v1/api/mcp/oauth/pending/srv-x")
        assert resp.status_code == 204
        # User-2's row survives.
        assert len(storage.list_mcp_pending_consent_by_user("user-2")) == 1


class TestAuditTrail:
    def test_single_dismiss_audits(self, storage: SQLiteBackend) -> None:
        _seed_oauth_server(storage)
        _seed_pending(storage)
        client = TestClient(_build_app(storage))
        resp = client.delete("/v1/api/mcp/oauth/pending/srv-x")
        assert resp.status_code == 204

        events = storage.list_audit_events(limit=10)
        rows = [
            e for e in events if e.get("action") == "mcp_server.oauth.pending_consent_dismissed"
        ]
        assert len(rows) == 1
        detail = rows[0].get("detail")
        if isinstance(detail, str):
            import json as _json

            detail = _json.loads(detail)
        assert detail.get("mode") == "single"
        assert detail.get("cleared") == 1

    def test_single_dismiss_audits_even_when_no_row_existed(self, storage: SQLiteBackend) -> None:
        # Cross-tenant non-observability requires a 204 in the never-existed
        # case — the audit row distinguishes a real dismiss from a stuffed
        # attempt by recording ``cleared=0``.
        _seed_oauth_server(storage)
        client = TestClient(_build_app(storage))
        resp = client.delete("/v1/api/mcp/oauth/pending/never-existed")
        assert resp.status_code == 204

        events = storage.list_audit_events(limit=10)
        rows = [
            e for e in events if e.get("action") == "mcp_server.oauth.pending_consent_dismissed"
        ]
        assert len(rows) == 1
        detail = rows[0].get("detail")
        if isinstance(detail, str):
            import json as _json

            detail = _json.loads(detail)
        assert detail.get("mode") == "single"
        assert detail.get("cleared") == 0

    def test_bulk_dismiss_audits(self, storage: SQLiteBackend) -> None:
        _seed_oauth_server(storage)
        _seed_oauth_server(storage, name="srv-y")
        _seed_pending(storage, server_name="srv-x")
        _seed_pending(storage, server_name="srv-y")
        client = TestClient(_build_app(storage))
        resp = client.delete("/v1/api/mcp/oauth/pending")
        assert resp.status_code == 200
        assert resp.json() == {"cleared": 2}

        events = storage.list_audit_events(limit=10)
        rows = [
            e for e in events if e.get("action") == "mcp_server.oauth.pending_consent_dismissed"
        ]
        assert len(rows) == 1
        detail = rows[0].get("detail")
        if isinstance(detail, str):
            import json as _json

            detail = _json.loads(detail)
        assert detail.get("mode") == "bulk"
        assert detail.get("cleared") == 2


class TestClearAllPending:
    def test_bulk_clear(self, storage: SQLiteBackend) -> None:
        _seed_oauth_server(storage)
        _seed_oauth_server(storage, name="srv-y")
        _seed_pending(storage, server_name="srv-x")
        _seed_pending(storage, server_name="srv-y")
        client = TestClient(_build_app(storage))
        resp = client.delete("/v1/api/mcp/oauth/pending")
        assert resp.status_code == 200
        assert resp.json() == {"cleared": 2}
        assert storage.list_mcp_pending_consent_by_user("user-1") == []

    def test_bulk_clear_zero_when_empty(self, storage: SQLiteBackend) -> None:
        _seed_oauth_server(storage)
        client = TestClient(_build_app(storage))
        resp = client.delete("/v1/api/mcp/oauth/pending")
        assert resp.status_code == 200
        assert resp.json() == {"cleared": 0}
