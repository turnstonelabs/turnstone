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

    # Non-seed slug/display name: the migration ships a real ``scribe``, so a
    # fixture named ``scribe`` would collide on a migrated DB.
    get_storage().create_persona(
        {
            "persona_id": "p1",
            "name": "test-scribe",
            "display_name": "Test Scribe",
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
        assert c.post("/v1/api/admin/personas", json={"name": "x"}).status_code == 403
        assert (
            c.patch("/v1/api/admin/personas/" + seeded, json={"enabled": False}).status_code == 403
        )

    def test_admin_verbs_succeed_with_grant(self, tmp_db: Any, seeded: str) -> None:
        c = _client(tmp_db, _ALL)
        assert c.get("/v1/api/admin/personas").status_code == 200
        assert c.get("/v1/api/admin/personas/" + seeded).status_code == 200
        created = c.post(
            "/v1/api/admin/personas",
            json={"name": "test-writer", "base_prompt": "W", "tool_allowlist": []},
        )
        assert created.status_code == 200
        assert created.json()["tool_allowlist"] == []
        patched = c.patch("/v1/api/admin/personas/" + seeded, json={"display_name": "Scribe 2"})
        assert patched.status_code == 200
        assert patched.json()["display_name"] == "Scribe 2"

    def test_picker_needs_no_persona_perm(self, tmp_db: Any, seeded: str) -> None:
        # Selection at creation must work for users with ZERO persona.*
        # grants — the feed is authenticated-only, display fields only.
        c = _client(tmp_db, set())
        resp = c.get("/v1/api/personas")
        assert resp.status_code == 200
        rows = resp.json()["personas"]
        assert [r["name"] for r in rows] == ["test-scribe"]
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
        assert [r["name"] for r in rows] == ["test-scribe"]
        assert rows[0]["enabled"] is False


class TestRouteContracts:
    def test_invariant_violations_are_400(self, tmp_db: Any, seeded: str) -> None:
        c = _client(tmp_db, _ALL)
        # Duplicate slug (the seeded fixture owns ``test-scribe``).
        assert c.post("/v1/api/admin/personas", json={"name": "test-scribe"}).status_code == 400
        # Bad slug shape.
        assert c.post("/v1/api/admin/personas", json={"name": "Not A Slug"}).status_code == 400
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
        assert c.patch("/v1/api/admin/personas/nope", json={"enabled": False}).status_code == 404

    def test_no_delete_route(self, tmp_db: Any, seeded: str) -> None:
        c = _client(tmp_db, _ALL)
        assert c.delete("/v1/api/admin/personas/" + seeded).status_code == 405


class TestRbacCrossPerm:
    """Single-permission clients pin each handler to its OWN persona.* verb.

    The success-path suite grants all three perms (``_ALL``), so a handler
    accidentally wired to the wrong verb (read gating a write, say) still
    passes there.  A read-only and a write-only client expose that drift: read
    can list/get but not create/patch, write can patch but not list.
    """

    def test_read_only_client(self, tmp_db: Any, seeded: str) -> None:
        c = _client(tmp_db, {"persona.read"})
        assert c.get("/v1/api/admin/personas").status_code == 200
        assert c.get("/v1/api/admin/personas/" + seeded).status_code == 200
        post = c.post("/v1/api/admin/personas", json={"name": "test-new"})
        assert post.status_code == 403
        assert "persona.create" in post.json()["error"]
        patch = c.patch("/v1/api/admin/personas/" + seeded, json={"display_name": "X"})
        assert patch.status_code == 403
        assert "persona.write" in patch.json()["error"]

    def test_write_only_client(self, tmp_db: Any, seeded: str) -> None:
        c = _client(tmp_db, {"persona.write"})
        patch = c.patch("/v1/api/admin/personas/" + seeded, json={"display_name": "X2"})
        assert patch.status_code == 200
        assert patch.json()["display_name"] == "X2"
        # persona.write does NOT satisfy the read gate on the list.
        assert c.get("/v1/api/admin/personas").status_code == 403


class TestArchiveAndDefaultFlipHttp:
    """The archive + default-flip lifecycle end-to-end at the HTTP edge — the
    layer the storage-level default tests can't see (route wiring + response
    projection + the permless picker's enabled filter)."""

    def test_default_flip_demotes_incumbent(self, tmp_db: Any, seeded: str) -> None:
        from turnstone.core.storage import get_storage

        # An incumbent interactive default alongside the (non-default) seeded
        # persona; flipping the seeded one must demote the incumbent.
        get_storage().create_persona(
            {
                "persona_id": "p2",
                "name": "test-eng",
                "display_name": "Test Eng",
                "applies_to_kinds": ["interactive"],
                "is_default": True,
            }
        )
        c = _client(tmp_db, _ALL)
        resp = c.patch("/v1/api/admin/personas/" + seeded, json={"is_default": True})
        assert resp.status_code == 200
        assert resp.json()["is_default"] is True
        # Exactly one default per kind after the flip — the incumbent demoted.
        rows = c.get("/v1/api/admin/personas").json()["personas"]
        defaults = [r["name"] for r in rows if r["is_default"]]
        assert defaults == ["test-scribe"]
        incumbent = get_storage().get_persona("p2")
        assert incumbent is not None and incumbent["is_default"] is False

    def test_archive_non_default_hides_from_picker_keeps_in_admin(
        self, tmp_db: Any, seeded: str
    ) -> None:
        c = _client(tmp_db, _ALL)
        resp = c.patch("/v1/api/admin/personas/" + seeded, json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False
        # Gone from the permless picker feed…
        picker = _client(tmp_db, set())
        assert picker.get("/v1/api/personas").json()["personas"] == []
        # …but still present in the admin list (include_disabled).
        rows = c.get("/v1/api/admin/personas").json()["personas"]
        assert [r["name"] for r in rows] == ["test-scribe"]
        assert rows[0]["enabled"] is False

    def test_unset_default_directly_is_400(self, tmp_db: Any, seeded: str) -> None:
        c = _client(tmp_db, _ALL)
        # Promote to default, then try to unset the flag directly.
        c.patch("/v1/api/admin/personas/" + seeded, json={"is_default": True})
        resp = c.patch("/v1/api/admin/personas/" + seeded, json={"is_default": False})
        assert resp.status_code == 400
        assert "cannot unset is_default directly" in resp.json()["error"]


class TestOrgIdGuard:
    def test_create_null_org_id_stored_empty(self, tmp_db: Any) -> None:
        # An explicit JSON null org_id must persist as "" — ``str(None)`` would
        # store the literal "None" and silently scope the persona to a bogus org.
        from turnstone.core.storage import get_storage

        c = _client(tmp_db, _ALL)
        resp = c.post("/v1/api/admin/personas", json={"name": "test-orgless", "org_id": None})
        assert resp.status_code == 200
        assert resp.json()["org_id"] == ""
        stored = get_storage().get_persona(resp.json()["persona_id"])
        assert stored is not None and stored["org_id"] == ""


class TestProductionRoutes:
    """The hand-built Starlette app in this module can't catch route-table
    drift in ``console/server.create_app``.  Introspect the real table."""

    def test_persona_handlers_registered_with_methods(self) -> None:
        from unittest.mock import MagicMock

        from starlette.routing import Mount, Route

        from turnstone.console.collector import ClusterCollector
        from turnstone.console.server import create_app

        app = create_app(collector=ClusterCollector(storage=MagicMock()))

        def _walk(routes: Any, prefix: str = "") -> Any:
            for r in routes:
                if isinstance(r, Mount):
                    yield from _walk(r.routes, prefix + r.path)
                elif isinstance(r, Route):
                    yield prefix + r.path, frozenset(r.methods or ()), r.endpoint.__name__

        persona_routes = [row for row in _walk(app.routes) if "/personas" in row[0]]
        reg = {(path, name): methods for path, methods, name in persona_routes}

        admin = "/v1/api/admin/personas"
        admin_one = "/v1/api/admin/personas/{persona_id}"
        assert "GET" in reg[(admin, "admin_list_personas")]
        assert "POST" in reg[(admin, "admin_create_persona")]
        assert "GET" in reg[(admin_one, "admin_get_persona")]
        assert "PATCH" in reg[(admin_one, "admin_update_persona")]
        # The permless picker feed is registered (creation surface).
        assert "GET" in reg[("/v1/api/personas", "list_personas_endpoint")]
        # Archive-only contract: NO DELETE anywhere on the persona surface.
        all_methods: set[str] = set().union(*reg.values())
        assert "DELETE" not in all_methods
