"""Endpoint tests for the personas surface (guard 12 + route contracts).

RBAC: the console admin CRUD is gated per-verb on ``persona.{create,read,
write}``; the picker feed (``GET /v1/api/personas``) is authenticated but
deliberately gated by NO persona permission — selecting a persona at
creation is a user action, authoring is the admin surface.  No DELETE
route exists (archive-only).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

from turnstone.console.server import (
    admin_create_persona,
    admin_get_persona,
    admin_list_personas,
    admin_update_persona,
)
from turnstone.core.auth import AuthResult
from turnstone.server import list_personas_endpoint


class _InjectAuthMiddleware(BaseHTTPMiddleware):
    """Injects an AuthResult whose permissions the test controls via
    ``app.state.test_permissions`` (empty set = authenticated, no grants)."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id="test-user",
            scopes=frozenset({"approve"}),
            token_source="config",
            permissions=frozenset(request.app.state.test_permissions),
        )
        return await call_next(request)


def _client(tmp_db: Any, permissions: set[str]) -> TestClient:
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route("/api/personas", list_personas_endpoint),
                    Route("/api/admin/personas", admin_list_personas),
                    Route("/api/admin/personas", admin_create_persona, methods=["POST"]),
                    Route("/api/admin/personas/{persona_id}", admin_get_persona),
                    Route(
                        "/api/admin/personas/{persona_id}",
                        admin_update_persona,
                        methods=["PATCH"],
                    ),
                ],
            ),
        ],
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.test_permissions = permissions
    # require_storage_or_503 reads the console's app-scoped handle; the picker
    # endpoint reads the global registry (tmp_db initialized it) — point both
    # at the same backend.
    from turnstone.core.storage import get_storage

    app.state.auth_storage = get_storage()
    return TestClient(app)


_ALL = {"persona.create", "persona.read", "persona.write"}


@pytest.fixture
def seeded(tmp_db: Any) -> str:
    from turnstone.core.storage import get_storage

    get_storage().create_persona(
        {
            "persona_id": "p1",
            "name": "scribe",
            "display_name": "Scribe",
            "tool_allowlist": [],
            "mcp_enabled": False,
            "applies_to_kinds": ["interactive"],
        }
    )
    return "p1"


class TestRbac:
    def test_admin_verbs_403_without_grant(self, tmp_db: Any, seeded: str) -> None:
        c = _client(tmp_db, set())
        assert c.get("/v1/api/admin/personas").status_code == 403
        assert c.get("/v1/api/admin/personas/" + seeded).status_code == 403
        assert (
            c.post("/v1/api/admin/personas", json={"name": "x"}).status_code == 403
        )
        assert (
            c.patch(
                "/v1/api/admin/personas/" + seeded, json={"enabled": False}
            ).status_code
            == 403
        )

    def test_admin_verbs_succeed_with_grant(self, tmp_db: Any, seeded: str) -> None:
        c = _client(tmp_db, _ALL)
        assert c.get("/v1/api/admin/personas").status_code == 200
        assert c.get("/v1/api/admin/personas/" + seeded).status_code == 200
        created = c.post(
            "/v1/api/admin/personas",
            json={"name": "writer", "base_prompt": "W", "tool_allowlist": []},
        )
        assert created.status_code == 200
        assert created.json()["tool_allowlist"] == []
        patched = c.patch(
            "/v1/api/admin/personas/" + seeded, json={"display_name": "Scribe 2"}
        )
        assert patched.status_code == 200
        assert patched.json()["display_name"] == "Scribe 2"

    def test_picker_needs_no_persona_perm(self, tmp_db: Any, seeded: str) -> None:
        # Selection at creation must work for users with ZERO persona.*
        # grants — the feed is authenticated-only, display fields only.
        c = _client(tmp_db, set())
        resp = c.get("/v1/api/personas")
        assert resp.status_code == 200
        rows = resp.json()["personas"]
        assert [r["name"] for r in rows] == ["scribe"]
        assert set(rows[0]) == {
            "name",
            "display_name",
            "description",
            "applies_to_kinds",
            "is_default",
        }

    def test_picker_excludes_archived(self, tmp_db: Any, seeded: str) -> None:
        from turnstone.core.storage import get_storage

        get_storage().update_persona(seeded, enabled=False)
        c = _client(tmp_db, set())
        assert c.get("/v1/api/personas").json()["personas"] == []
        # ...but the admin list still shows it (include_disabled).
        admin = _client(tmp_db, _ALL)
        rows = admin.get("/v1/api/admin/personas").json()["personas"]
        assert [r["name"] for r in rows] == ["scribe"]
        assert rows[0]["enabled"] is False


class TestRouteContracts:
    def test_invariant_violations_are_400(self, tmp_db: Any, seeded: str) -> None:
        c = _client(tmp_db, _ALL)
        # Duplicate slug.
        assert (
            c.post("/v1/api/admin/personas", json={"name": "scribe"}).status_code == 400
        )
        # Bad slug shape.
        assert (
            c.post("/v1/api/admin/personas", json={"name": "Not A Slug"}).status_code
            == 400
        )
        # Default persona can't be archived.
        c.patch("/v1/api/admin/personas/" + seeded, json={"is_default": True})
        resp = c.patch("/v1/api/admin/personas/" + seeded, json={"enabled": False})
        assert resp.status_code == 400
        assert "archived" in resp.json()["error"]

    def test_patch_null_flags_leave_persona_unchanged(self, tmp_db: Any, seeded: str) -> None:
        # Clients built from UpdatePersonaRequest (every flag boolean|null)
        # serialize unset fields as explicit null — a rename must not archive
        # the persona or flip its levers as a side effect.
        c = _client(tmp_db, _ALL)
        resp = c.patch(
            "/v1/api/admin/personas/" + seeded,
            json={
                "display_name": "Renamed",
                "enabled": None,
                "mcp_enabled": None,
                "memory_enabled": None,
                "is_default": None,
                "applies_to_kinds": None,
            },
        )
        assert resp.status_code == 200
        row = resp.json()
        assert row["display_name"] == "Renamed"
        assert row["enabled"] is True  # NOT archived by the null
        assert row["mcp_enabled"] is False  # seeded value preserved
        assert row["applies_to_kinds"] == ["interactive"]

    def test_list_carries_tool_inventory(self, tmp_db: Any, seeded: str) -> None:
        c = _client(tmp_db, _ALL)
        inv = c.get("/v1/api/admin/personas").json()["tool_inventory"]
        assert "read_file" in inv["interactive"]
        assert "spawn_workstream" in inv["coordinator"]
        # tool_search is synthetic but listed — its membership decides
        # whether an authored set is soft or hard.
        assert "tool_search" in inv["interactive"]
        assert "tool_search" in inv["coordinator"]

    def test_missing_persona_is_404(self, tmp_db: Any) -> None:
        c = _client(tmp_db, _ALL)
        assert c.get("/v1/api/admin/personas/nope").status_code == 404
        assert (
            c.patch("/v1/api/admin/personas/nope", json={"enabled": False}).status_code
            == 404
        )

    def test_no_delete_route(self, tmp_db: Any, seeded: str) -> None:
        c = _client(tmp_db, _ALL)
        assert c.delete("/v1/api/admin/personas/" + seeded).status_code == 405
