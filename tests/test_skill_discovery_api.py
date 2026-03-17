"""Tests for skill discovery admin API endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

from turnstone.console.server import admin_skill_discover, admin_skill_install
from turnstone.core.auth import AuthResult
from turnstone.core.skill_parser import ParsedSkill
from turnstone.core.skill_sources import (
    SkillListing,
    SkillNotFoundError,
    SkillPackage,
    SkillSourceError,
)
from turnstone.core.storage._sqlite import SQLiteBackend

# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


class _InjectAuthMiddleware(BaseHTTPMiddleware):
    """Inject an admin auth result with admin.skills permission."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id="test-user",
            scopes=frozenset({"approve"}),
            token_source="config",
            permissions=frozenset({"read", "write", "approve", "admin.skills"}),
        )
        return await call_next(request)


class _InjectAuthNoSkillsMiddleware(BaseHTTPMiddleware):
    """Inject an auth result WITHOUT admin.skills permission."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id="test-user",
            scopes=frozenset({"approve"}),
            token_source="jwt",
            permissions=frozenset({"read", "write", "approve"}),
        )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ROUTES = [
    Mount(
        "/v1",
        routes=[
            Route("/api/admin/skills/discover", admin_skill_discover),
            Route(
                "/api/admin/skills/install",
                admin_skill_install,
                methods=["POST"],
            ),
        ],
    ),
]


@pytest.fixture
def storage(tmp_path):
    return SQLiteBackend(str(tmp_path / "test.db"))


@pytest.fixture
def client(storage):
    app = Starlette(
        routes=_ROUTES,
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.auth_storage = storage
    return TestClient(app)


@pytest.fixture
def client_no_perm(storage):
    app = Starlette(
        routes=_ROUTES,
        middleware=[Middleware(_InjectAuthNoSkillsMiddleware)],
    )
    app.state.auth_storage = storage
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_listing(
    name: str = "test-skill",
    skill_id: str = "owner/repo/test-skill",
) -> SkillListing:
    return SkillListing(
        id=skill_id,
        name=name,
        description="A test skill",
        author="Test Author",
        source="skills.sh",
        source_url="https://github.com/owner/repo",
        install_count=42,
        tags=["test"],
    )


def _sample_package(
    name: str = "test-skill",
    source_url: str = "https://github.com/owner/repo",
) -> SkillPackage:
    return SkillPackage(
        listing=SkillListing(
            id=f"owner/repo/{name}",
            name=name,
            description="A test skill",
            author="Test Author",
            source="github",
            source_url=source_url,
            tags=["test"],
        ),
        parsed=ParsedSkill(
            name=name,
            description="A test skill",
            content="# Test Skill\n\nInstructions here.",
            tags=["test"],
            author="Test Author",
            version="1.0.0",
        ),
        resources={"scripts/setup.sh": "#!/bin/bash\necho hello"},
    )


# ---------------------------------------------------------------------------
# Tests: Discover
# ---------------------------------------------------------------------------


class TestSkillDiscover:
    def test_search_basic(self, client: TestClient) -> None:
        listings = [_sample_listing()]

        with patch("turnstone.core.skill_sources.SkillsShClient") as mock_cls:
            instance = mock_cls.return_value
            instance.search = AsyncMock(return_value=listings)

            resp = client.get("/v1/api/admin/skills/discover?q=test")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["skills"]) == 1
        assert data["skills"][0]["name"] == "test-skill"
        assert data["skills"][0]["installed"] is False

    def test_search_empty_results(self, client: TestClient) -> None:
        with patch("turnstone.core.skill_sources.SkillsShClient") as mock_cls:
            instance = mock_cls.return_value
            instance.search = AsyncMock(return_value=[])

            resp = client.get("/v1/api/admin/skills/discover", params={"q": "test"})

        assert resp.status_code == 200
        assert resp.json()["skills"] == []

    def test_search_empty_query_rejected(self, client: TestClient) -> None:
        resp = client.get("/v1/api/admin/skills/discover")
        assert resp.status_code == 400

    def test_search_permission_denied(self, client_no_perm: TestClient) -> None:
        resp = client_no_perm.get("/v1/api/admin/skills/discover")
        assert resp.status_code == 403

    def test_search_marks_installed(self, client: TestClient, storage: SQLiteBackend) -> None:
        # Pre-install a skill with matching source_url
        storage.create_prompt_template(
            template_id="existing-id",
            name="test-skill",
            category="general",
            content="existing content",
            variables="[]",
            is_default=False,
            org_id="",
            created_by="admin",
            source_url="https://github.com/owner/repo",
        )

        listings = [_sample_listing()]

        with patch("turnstone.core.skill_sources.SkillsShClient") as mock_cls:
            instance = mock_cls.return_value
            instance.search = AsyncMock(return_value=listings)

            resp = client.get("/v1/api/admin/skills/discover?q=test")

        assert resp.status_code == 200
        assert resp.json()["skills"][0]["installed"] is True

    def test_search_source_error(self, client: TestClient) -> None:
        with patch("turnstone.core.skill_sources.SkillsShClient") as mock_cls:
            instance = mock_cls.return_value
            instance.search = AsyncMock(side_effect=SkillSourceError("timeout"))

            resp = client.get("/v1/api/admin/skills/discover?q=test")

        assert resp.status_code == 502
        assert "timeout" in resp.json()["error"]


# ---------------------------------------------------------------------------
# Tests: Install
# ---------------------------------------------------------------------------


class TestSkillInstall:
    def test_install_from_github(self, client: TestClient) -> None:
        package = _sample_package()

        with patch(
            "turnstone.core.skill_sources.fetch_skill_from_github", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = package

            resp = client.post(
                "/v1/api/admin/skills/install",
                json={"source": "github", "url": "https://github.com/owner/repo"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "test-skill"
        assert data["origin"] == "source"
        assert data["readonly"] is True
        assert data["source_url"] == "https://github.com/owner/repo"

    def test_install_from_skills_sh(self, client: TestClient) -> None:
        package = _sample_package()

        with (
            patch("turnstone.core.skill_sources.SkillsShClient") as mock_cls,
            patch(
                "turnstone.core.skill_sources.fetch_skill_from_github", new_callable=AsyncMock
            ) as mock_fetch,
        ):
            instance = mock_cls.return_value
            instance.resolve_github_url = AsyncMock(return_value="https://github.com/owner/repo")
            mock_fetch.return_value = package

            resp = client.post(
                "/v1/api/admin/skills/install",
                json={"source": "skills.sh", "skill_id": "owner/test-skill"},
            )

        assert resp.status_code == 200
        assert resp.json()["name"] == "test-skill"

    def test_install_invalid_source(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/api/admin/skills/install",
            json={"source": "invalid"},
        )
        assert resp.status_code == 400

    def test_install_missing_url(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/api/admin/skills/install",
            json={"source": "github"},
        )
        assert resp.status_code == 400

    def test_install_missing_skill_id(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/api/admin/skills/install",
            json={"source": "skills.sh"},
        )
        assert resp.status_code == 400

    def test_install_duplicate_source_url(self, client: TestClient, storage: SQLiteBackend) -> None:
        # Pre-install
        storage.create_prompt_template(
            template_id="existing-id",
            name="existing-skill",
            category="general",
            content="content",
            variables="[]",
            is_default=False,
            org_id="",
            created_by="admin",
            source_url="https://github.com/owner/repo",
        )

        package = _sample_package()

        with patch(
            "turnstone.core.skill_sources.fetch_skill_from_github", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = package

            resp = client.post(
                "/v1/api/admin/skills/install",
                json={"source": "github", "url": "https://github.com/owner/repo"},
            )

        assert resp.status_code == 409

    def test_install_duplicate_name(self, client: TestClient, storage: SQLiteBackend) -> None:
        # Pre-install with same name but different source_url
        storage.create_prompt_template(
            template_id="existing-id",
            name="test-skill",
            category="general",
            content="content",
            variables="[]",
            is_default=False,
            org_id="",
            created_by="admin",
            source_url="https://github.com/other/repo",
        )

        package = _sample_package(source_url="https://github.com/owner/different-repo")

        with patch(
            "turnstone.core.skill_sources.fetch_skill_from_github", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = package

            resp = client.post(
                "/v1/api/admin/skills/install",
                json={"source": "github", "url": "https://github.com/owner/different-repo"},
            )

        assert resp.status_code == 409

    def test_install_not_found(self, client: TestClient) -> None:
        with (
            patch(
                "turnstone.core.skill_sources.fetch_skill_from_github", new_callable=AsyncMock
            ) as mock_fetch,
            patch(
                "turnstone.core.skill_sources.fetch_skills_from_github_repo",
                new_callable=AsyncMock,
            ) as mock_batch,
        ):
            mock_fetch.side_effect = SkillNotFoundError("SKILL.md not found")
            mock_batch.side_effect = SkillNotFoundError("No SKILL.md files found")

            resp = client.post(
                "/v1/api/admin/skills/install",
                json={"source": "github", "url": "https://github.com/owner/repo"},
            )

        assert resp.status_code == 404

    def test_install_source_error_returns_502(self, client: TestClient) -> None:
        with patch(
            "turnstone.core.skill_sources.fetch_skill_from_github", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.side_effect = SkillSourceError("connection timeout")

            resp = client.post(
                "/v1/api/admin/skills/install",
                json={"source": "github", "url": "https://github.com/owner/repo"},
            )

        assert resp.status_code == 502

    def test_install_permission_denied(self, client_no_perm: TestClient) -> None:
        resp = client_no_perm.post(
            "/v1/api/admin/skills/install",
            json={"source": "github", "url": "https://github.com/owner/repo"},
        )
        assert resp.status_code == 403

    def test_install_stores_resources(self, client: TestClient, storage: SQLiteBackend) -> None:
        package = _sample_package()

        with patch(
            "turnstone.core.skill_sources.fetch_skill_from_github", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = package

            resp = client.post(
                "/v1/api/admin/skills/install",
                json={"source": "github", "url": "https://github.com/owner/repo"},
            )

        assert resp.status_code == 200
        skill_id = resp.json()["template_id"]
        resources = storage.list_skill_resources(skill_id)
        assert len(resources) == 1
        assert resources[0]["path"] == "scripts/setup.sh"
