"""Tests for turnstone.core.skill_sources."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from turnstone.core.skill_sources import (
    SkillNotFoundError,
    SkillSourceError,
    SkillsShClient,
    _parse_github_url,
    fetch_skill_from_github,
)


class TestParseGitHubUrl:
    """GitHub URL parsing."""

    def test_simple_repo(self) -> None:
        owner, repo, branch, path, explicit = _parse_github_url("https://github.com/owner/repo")
        assert owner == "owner"
        assert repo == "repo"
        assert branch == "main"
        assert path == ""
        assert explicit is False

    def test_repo_with_branch(self) -> None:
        owner, repo, branch, path, explicit = _parse_github_url(
            "https://github.com/owner/repo/tree/develop"
        )
        assert branch == "develop"
        assert path == ""
        assert explicit is True

    def test_repo_with_path(self) -> None:
        owner, repo, branch, path, _explicit = _parse_github_url(
            "https://github.com/owner/repo/tree/main/skills/code-review"
        )
        assert owner == "owner"
        assert repo == "repo"
        assert branch == "main"
        assert path == "skills/code-review"

    def test_blob_url(self) -> None:
        owner, repo, branch, path, _explicit = _parse_github_url(
            "https://github.com/owner/repo/blob/main/SKILL.md"
        )
        assert branch == "main"
        assert path == "SKILL.md"

    def test_invalid_url(self) -> None:
        owner, repo, branch, path, _explicit = _parse_github_url("https://gitlab.com/owner/repo")
        assert owner == ""


class TestSkillsShClient:
    """SkillsShClient with mocked httpx."""

    @pytest.mark.anyio
    async def test_search_basic(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "skills": [
                {
                    "id": "test/skill",
                    "name": "test-skill",
                    "description": "A test skill",
                    "author": "tester",
                    "source_url": "https://github.com/test/skill",
                    "install_count": 42,
                    "tags": ["test"],
                }
            ]
        }

        with patch("turnstone.core.skill_sources.httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_response)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = instance

            client = SkillsShClient()
            results = await client.search(query="test", limit=10)

        assert len(results) == 1
        assert results[0].name == "test-skill"
        assert results[0].install_count == 42
        assert results[0].source == "skills.sh"

    @pytest.mark.anyio
    async def test_search_empty(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"skills": []}

        with patch("turnstone.core.skill_sources.httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_response)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = instance

            client = SkillsShClient()
            results = await client.search()

        assert results == []

    @pytest.mark.anyio
    async def test_search_http_error(self) -> None:
        with patch("turnstone.core.skill_sources.httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            instance.get = AsyncMock(side_effect=httpx.ConnectTimeout("timeout"))
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = instance

            client = SkillsShClient()
            with pytest.raises(SkillSourceError, match="request failed"):
                await client.search(query="test")

    @pytest.mark.anyio
    async def test_search_server_error(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=mock_response)
        )

        with patch("turnstone.core.skill_sources.httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_response)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = instance

            client = SkillsShClient()
            with pytest.raises(SkillSourceError, match="returned 500"):
                await client.search()

    @pytest.mark.anyio
    async def test_custom_base_url(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"skills": []}

        with patch("turnstone.core.skill_sources.httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_response)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = instance

            client = SkillsShClient(base_url="https://custom.registry.io")
            await client.search()

            # Verify the URL uses the custom base
            call_args = instance.get.call_args
            assert "custom.registry.io" in str(call_args)

    @pytest.mark.anyio
    async def test_resolve_github_url(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"source_url": "https://github.com/owner/skill-repo"}

        with patch("turnstone.core.skill_sources.httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_response)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = instance

            client = SkillsShClient()
            url = await client.resolve_github_url("owner/skill")

        assert url == "https://github.com/owner/skill-repo"


class TestFetchSkillFromGithub:
    """GitHub fetch with mocked httpx."""

    @pytest.mark.anyio
    async def test_invalid_url(self) -> None:
        with pytest.raises(SkillSourceError, match="Could not parse"):
            await fetch_skill_from_github("https://gitlab.com/bad/url")

    @pytest.mark.anyio
    async def test_skill_md_not_found(self) -> None:
        with patch("turnstone.core.skill_sources.httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            not_found = MagicMock()
            not_found.status_code = 404
            instance.get = AsyncMock(return_value=not_found)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = instance

            with pytest.raises(SkillNotFoundError, match="SKILL.md not found"):
                await fetch_skill_from_github("https://github.com/owner/repo")

    @pytest.mark.anyio
    async def test_fetch_success(self) -> None:
        skill_content = """\
---
name: test-skill
description: A test skill
author: Test Author
tags: [test]
---

# Test Skill

Instructions here.
"""
        tree_data = {"tree": []}

        def mock_get(url, **kwargs):
            resp = MagicMock()
            if "raw.githubusercontent.com" in url and "SKILL.md" in url:
                resp.status_code = 200
                resp.text = skill_content
            elif "api.github.com" in url and "git/trees" in url:
                resp.status_code = 200
                resp.json.return_value = tree_data
            else:
                resp.status_code = 404
            return resp

        with patch("turnstone.core.skill_sources.httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            instance.get = AsyncMock(side_effect=mock_get)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = instance

            package = await fetch_skill_from_github("https://github.com/owner/repo")

        assert package.parsed.name == "test-skill"
        assert package.parsed.author == "Test Author"
        assert package.listing.source == "github"
        assert package.listing.id == "owner/repo/test-skill"
        assert package.resources == {}
