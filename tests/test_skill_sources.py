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
    _split_skills_sh_id,
    fetch_skill_from_github,
)


class TestSplitSkillsShId:
    """skills.sh canonical id parsing."""

    def test_three_segments(self) -> None:
        assert _split_skills_sh_id("owner/repo/leaf") == ("owner", "repo", "leaf")

    def test_strips_surrounding_slashes(self) -> None:
        assert _split_skills_sh_id("/owner/repo/leaf/") == ("owner", "repo", "leaf")

    def test_rejects_two_segments(self) -> None:
        with pytest.raises(SkillSourceError, match="expected"):
            _split_skills_sh_id("owner/repo")

    def test_rejects_four_segments(self) -> None:
        with pytest.raises(SkillSourceError, match="expected"):
            _split_skills_sh_id("a/b/c/d")

    def test_rejects_empty_segment(self) -> None:
        with pytest.raises(SkillSourceError, match="expected"):
            _split_skills_sh_id("owner//leaf")

    def test_rejects_internal_whitespace(self) -> None:
        with pytest.raises(SkillSourceError, match="expected"):
            _split_skills_sh_id("owner/repo/leaf with space")

    def test_rejects_url_hostile_chars(self) -> None:
        with pytest.raises(SkillSourceError, match="expected"):
            _split_skills_sh_id("owner/repo/leaf?query=x")


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
    async def test_search_derives_source_url_when_missing(self) -> None:
        # Real skills.sh /api/search responses don't carry source_url.
        # We synthesise one from the canonical id so the discover-UI
        # "already installed" check matches what download_skill persists.
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "skills": [
                {"id": "owner/repo/leaf", "name": "leaf"},
            ]
        }

        with patch("turnstone.core.skill_sources.httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_response)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = instance

            client = SkillsShClient()
            results = await client.search()

        assert len(results) == 1
        assert results[0].source_url == "https://skills.sh/skills/owner/repo/leaf"

    @pytest.mark.anyio
    async def test_download_skill_success(self) -> None:
        skill_md = """---
name: leaf
description: A leaf skill
author: Owner
tags: [demo]
---
body
"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "files": [
                {"path": "SKILL.md", "contents": skill_md},
                {"path": "scripts/run.sh", "contents": "#!/bin/sh\necho hi\n"},
                # Filtered out: not in _RESOURCE_DIRS
                {"path": "README.md", "contents": "ignored"},
            ]
        }

        with patch("turnstone.core.skill_sources.httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_response)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = instance

            client = SkillsShClient()
            package = await client.download_skill("owner/repo/leaf")

            # Confirm the request hit /api/download/{owner}/{repo}/{skill}
            url_called = instance.get.call_args.args[0]
            assert url_called == "https://skills.sh/api/download/owner/repo/leaf"

        assert package.parsed.name == "leaf"
        assert package.parsed.author == "Owner"
        assert package.listing.id == "owner/repo/leaf"
        assert package.listing.source == "skills.sh"
        assert package.listing.source_url == "https://skills.sh/skills/owner/repo/leaf"
        assert package.resources == {"scripts/run.sh": "#!/bin/sh\necho hi\n"}

    @pytest.mark.anyio
    async def test_download_skill_invalid_id(self) -> None:
        client = SkillsShClient()
        with pytest.raises(SkillSourceError, match="expected 'owner/repo/skill-name'"):
            await client.download_skill("not-a-three-segment-id")

    @pytest.mark.anyio
    async def test_download_skill_404(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"

        with patch("turnstone.core.skill_sources.httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_response)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = instance

            client = SkillsShClient()
            with pytest.raises(SkillNotFoundError, match="has no skill"):
                await client.download_skill("owner/repo/missing")

    @pytest.mark.anyio
    async def test_download_skill_no_skill_md(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "files": [{"path": "scripts/run.sh", "contents": "x"}],
        }

        with patch("turnstone.core.skill_sources.httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_response)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = instance

            client = SkillsShClient()
            with pytest.raises(SkillNotFoundError, match="no SKILL.md"):
                await client.download_skill("owner/repo/leaf")

    @pytest.mark.anyio
    async def test_download_skill_empty_files(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"files": []}

        with patch("turnstone.core.skill_sources.httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_response)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = instance

            client = SkillsShClient()
            with pytest.raises(SkillSourceError, match="returned no files"):
                await client.download_skill("owner/repo/leaf")

    @pytest.mark.anyio
    async def test_download_skill_files_not_a_list(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"files": "oops"}

        with patch("turnstone.core.skill_sources.httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_response)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = instance

            client = SkillsShClient()
            with pytest.raises(SkillSourceError, match="returned no files"):
                await client.download_skill("owner/repo/leaf")

    @pytest.mark.anyio
    async def test_download_skill_oversized_skill_md(self) -> None:
        from turnstone.core.skill_sources import _MAX_SKILL_MD_SIZE

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "files": [{"path": "SKILL.md", "contents": "x" * (_MAX_SKILL_MD_SIZE + 1)}],
        }

        with patch("turnstone.core.skill_sources.httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_response)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = instance

            client = SkillsShClient()
            with pytest.raises(SkillSourceError, match="exceeds size cap"):
                await client.download_skill("owner/repo/leaf")

    @pytest.mark.anyio
    async def test_download_skill_resource_cap(self) -> None:
        from turnstone.core.skill_sources import _MAX_RESOURCE_FILES

        skill_md = """---
name: leaf
description: caps test
---
"""
        # 12 valid resources — only the first _MAX_RESOURCE_FILES should land.
        files = [{"path": "SKILL.md", "contents": skill_md}]
        for i in range(_MAX_RESOURCE_FILES + 2):
            files.append({"path": f"scripts/r{i}.sh", "contents": f"#{i}\n"})

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"files": files}

        with patch("turnstone.core.skill_sources.httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_response)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = instance

            client = SkillsShClient()
            package = await client.download_skill("owner/repo/leaf")

        assert len(package.resources) == _MAX_RESOURCE_FILES

    @pytest.mark.anyio
    async def test_download_skill_filters_non_text_extension(self) -> None:
        skill_md = """---
name: leaf
description: ext test
---
"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "files": [
                {"path": "SKILL.md", "contents": skill_md},
                # Right dir, wrong extension — must be dropped.
                {"path": "scripts/binary.exe", "contents": "MZ..."},
                # Right dir + right extension — kept.
                {"path": "scripts/keep.sh", "contents": "echo\n"},
            ]
        }

        with patch("turnstone.core.skill_sources.httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_response)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = instance

            client = SkillsShClient()
            package = await client.download_skill("owner/repo/leaf")

        assert package.resources == {"scripts/keep.sh": "echo\n"}


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
