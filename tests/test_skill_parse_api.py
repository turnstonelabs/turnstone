"""Tests for the SKILL.md parse admin API endpoint.

The endpoint is a thin permission-checked wrapper around
``turnstone.core.skill_parser.parse_skill_md``.  These tests cover the
routing, auth, and error-handling layers — parser semantics live in
``test_skill_parser.py``.
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
    from collections.abc import Iterator

    from starlette.requests import Request
    from starlette.responses import Response

from turnstone.console.server import admin_parse_skill
from turnstone.core.auth import AuthResult


class _InjectAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id="test-user",
            scopes=frozenset({"approve"}),
            token_source="config",
            permissions=frozenset({"read", "write", "approve", "admin.skills"}),
        )
        return await call_next(request)


class _InjectAuthNoSkillsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id="test-user",
            scopes=frozenset({"approve"}),
            token_source="jwt",
            permissions=frozenset({"read", "write", "approve"}),
        )
        return await call_next(request)


_ROUTES = [
    Mount(
        "/v1",
        routes=[
            Route("/api/admin/skills/parse", admin_parse_skill, methods=["POST"]),
        ],
    ),
]


@pytest.fixture
def client() -> TestClient:
    app = Starlette(
        routes=_ROUTES,
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    return TestClient(app)


@pytest.fixture
def client_no_perm() -> TestClient:
    app = Starlette(
        routes=_ROUTES,
        middleware=[Middleware(_InjectAuthNoSkillsMiddleware)],
    )
    return TestClient(app)


_FULL_SKILL = """\
---
name: code-review
description: Automated code review skill
author: Test Author
version: 2.0.0
tags: [python, review, quality]
allowed-tools: [read_file, list_directory]
license: MIT
compatibility: ">=0.7"
---

# Code Review

Review code for best practices.
"""

_MINIMAL_SKILL = """\
---
name: minimal
---

Just some content.
"""


class TestParseSkill:
    def test_parses_full_frontmatter(self, client: TestClient) -> None:
        resp = client.post("/v1/api/admin/skills/parse", json={"raw": _FULL_SKILL})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "code-review"
        assert data["description"] == "Automated code review skill"
        assert data["author"] == "Test Author"
        assert data["version"] == "2.0.0"
        assert data["tags"] == ["python", "review", "quality"]
        assert data["allowed_tools"] == ["read_file", "list_directory"]
        assert data["license"] == "MIT"
        assert data["compatibility"] == ">=0.7"
        assert "# Code Review" in data["content"]
        # Frontmatter should not leak into the body.
        assert "name: code-review" not in data["content"]
        # ParsedSkill carries raw_frontmatter (the full YAML dict) but the
        # handler whitelists fields by hand to avoid leaking arbitrary keys.
        # Pin that contract — a future refactor to dataclasses.asdict would
        # silently break it without this assertion.
        assert "raw_frontmatter" not in data

    def test_parses_minimal_frontmatter(self, client: TestClient) -> None:
        resp = client.post("/v1/api/admin/skills/parse", json={"raw": _MINIMAL_SKILL})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "minimal"
        assert data["description"] == "Just some content."
        assert data["version"] == "1.0.0"
        assert data["tags"] == []
        assert data["allowed_tools"] == []
        assert data["license"] == ""

    def test_anthropic_nested_metadata_tags(self, client: TestClient) -> None:
        # Anthropic-style skill puts tags under metadata.tags rather than
        # at the top level — the parser must handle both layouts.
        raw = """\
---
name: nested-meta
description: A skill using nested metadata
metadata:
  tags: [alpha, beta]
  author: Anthropic
  version: 3.1.4
---

Body.
"""
        resp = client.post("/v1/api/admin/skills/parse", json={"raw": raw})
        assert resp.status_code == 200
        data = resp.json()
        assert data["tags"] == ["alpha", "beta"]
        assert data["author"] == "Anthropic"
        assert data["version"] == "3.1.4"

    def test_unquoted_colon_in_description(self, client: TestClient) -> None:
        # Common cross-client mistake: ``description: Use when: the user...``
        # The parser retries with the description value quoted.
        raw = """\
---
name: colon-desc
description: Use when: the user asks for a review
---

Body.
"""
        resp = client.post("/v1/api/admin/skills/parse", json={"raw": raw})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "colon-desc"
        assert "Use when" in data["description"]

    def test_missing_name_returns_400(self, client: TestClient) -> None:
        raw = """\
---
description: No name field
---

Body.
"""
        resp = client.post("/v1/api/admin/skills/parse", json={"raw": raw})
        assert resp.status_code == 400
        assert "name" in resp.json()["error"].lower()

    def test_missing_raw_returns_400(self, client: TestClient) -> None:
        resp = client.post("/v1/api/admin/skills/parse", json={})
        assert resp.status_code == 400
        assert "raw" in resp.json()["error"].lower()

    def test_blank_raw_returns_400(self, client: TestClient) -> None:
        resp = client.post("/v1/api/admin/skills/parse", json={"raw": "   \n"})
        assert resp.status_code == 400

    def test_invalid_yaml_returns_400(self, client: TestClient) -> None:
        # YAML that the malformed-description retry can't fix.
        raw = "---\nname: [not, valid, here\n---\nBody.\n"
        resp = client.post("/v1/api/admin/skills/parse", json={"raw": raw})
        assert resp.status_code == 400

    def test_requires_admin_skills_permission(self, client_no_perm: TestClient) -> None:
        resp = client_no_perm.post("/v1/api/admin/skills/parse", json={"raw": _MINIMAL_SKILL})
        assert resp.status_code == 403

    def test_oversized_content_length_returns_413(self, client: TestClient) -> None:
        # Content-Length pre-check rejects oversized bodies before they're
        # buffered into memory.  Caps worker memory against an admin-token
        # holder spraying multi-GB JSON.  The threshold is generous (~4×
        # the per-string cap) so payload here must clearly exceed it.
        oversized = "a" * 200_000
        resp = client.post("/v1/api/admin/skills/parse", json={"raw": oversized})
        assert resp.status_code == 413

    def test_oversized_raw_chunked_returns_413(self, client: TestClient) -> None:
        # When the client sends Transfer-Encoding: chunked there is no
        # Content-Length header, so the pre-check is skipped and the
        # application-layer cap is the only line of defence.  httpx switches
        # to chunked when the body is a generator.
        def _gen() -> Iterator[bytes]:
            yield b'{"raw":"' + b"a" * 33_000 + b'"}'

        resp = client.post(
            "/v1/api/admin/skills/parse",
            content=_gen(),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 413
        assert "raw" in resp.json()["error"].lower()
