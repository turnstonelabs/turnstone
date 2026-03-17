"""Skill discovery source clients — skills.sh API + GitHub fetcher.

Provides :class:`SkillsShClient` for searching the skills.sh registry
and :func:`fetch_skill_from_github` for fetching SKILL.md from GitHub repos.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from turnstone.core.skill_parser import ParsedSkill, parse_skill_md

logger = logging.getLogger(__name__)

DEFAULT_DISCOVERY_URL = "https://skills.sh"

_GITHUB_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[a-zA-Z0-9_-]+)/(?P<repo>[a-zA-Z0-9._-]+)"
    r"(?:/(?:tree|blob)/(?P<branch>[^/]+)(?:/(?P<path>.+))?)?$"
)

_MAX_RESOURCE_FILES = 10
_MAX_RESOURCE_SIZE = 100 * 1024  # 100KB per file
_MAX_SKILL_MD_SIZE = 256 * 1024  # 256KB generous cap for SKILL.md
_RESOURCE_DIRS = ("scripts", "references", "assets")
_TEXT_EXTENSIONS = frozenset(
    {".md", ".txt", ".sh", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini"}
)


@dataclass(frozen=True)
class SkillListing:
    """A skill discovered from an external source."""

    id: str  # "owner/repo/skill-name" or registry ID
    name: str
    description: str = ""
    author: str = ""
    source: str = ""  # "skills.sh" | "github"
    source_url: str = ""
    install_count: int = 0
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SkillPackage:
    """A fully resolved skill ready for installation."""

    listing: SkillListing
    parsed: ParsedSkill
    resources: dict[str, str] = field(default_factory=dict)  # path → content


class SkillSourceError(Exception):
    """Error communicating with a skill source."""


class SkillNotFoundError(SkillSourceError):
    """Skill definition (SKILL.md) not found at the source."""


class SkillsShClient:
    """Async client for the skills.sh discovery API."""

    def __init__(self, base_url: str = "") -> None:
        self._base_url = (base_url or DEFAULT_DISCOVERY_URL).rstrip("/")

    async def search(self, query: str = "", *, limit: int = 20) -> list[SkillListing]:
        """Search for skills matching *query*."""
        params: dict[str, str | int] = {"limit": min(limit, 100)}
        if query:
            params["q"] = query

        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
            try:
                resp = await client.get(f"{self._base_url}/api/search", params=params)
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise SkillSourceError(f"skills.sh returned {exc.response.status_code}") from exc
            except httpx.HTTPError as exc:
                raise SkillSourceError(f"skills.sh request failed: {exc}") from exc

        data = resp.json()
        results: list[SkillListing] = []
        for item in data.get("skills", data.get("results", [])):
            results.append(
                SkillListing(
                    id=str(item.get("id", item.get("name", ""))),
                    name=str(item.get("name", "")),
                    description=str(item.get("description", "")),
                    author=str(item.get("author", "")),
                    source="skills.sh",
                    source_url=str(item.get("source_url", item.get("url", ""))),
                    install_count=int(item.get("install_count", item.get("installs", 0))),
                    tags=[str(t) for t in item.get("tags", []) if isinstance(t, str)],
                )
            )
        return results

    async def resolve_github_url(self, skill_id: str) -> str:
        """Resolve a skills.sh skill ID to its GitHub URL."""
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
            try:
                resp = await client.get(f"{self._base_url}/api/skills/{quote(skill_id, safe='')}")
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise SkillSourceError(f"Failed to resolve skill {skill_id}: {exc}") from exc

        data = resp.json()
        url = str(data.get("source_url", data.get("github_url", data.get("url", ""))))
        if not url:
            raise SkillSourceError(f"No source URL for skill {skill_id}")
        return url


def _parse_github_url(url: str) -> tuple[str, str, str, str]:
    """Parse a GitHub URL into (owner, repo, branch, path).

    Returns ("", "", "", "") if URL doesn't match.
    """
    m = _GITHUB_URL_RE.match(url)
    if not m:
        return ("", "", "", "")
    return (
        m.group("owner"),
        m.group("repo"),
        m.group("branch") or "main",
        m.group("path") or "",
    )


def _find_resource_files(
    tree_items: list[dict[str, Any]], skill_md_dir: str
) -> list[dict[str, str]]:
    """Filter tree items to resource files relative to a SKILL.md directory."""
    resource_files: list[dict[str, str]] = []
    for item in tree_items:
        if item.get("type") != "blob":
            continue
        item_path: str = item.get("path", "")
        rel_path = item_path
        if skill_md_dir:
            if not item_path.startswith(f"{skill_md_dir}/"):
                continue
            rel_path = item_path[len(skill_md_dir) + 1 :]
        elif "/" in item_path:
            continue  # root-level SKILL.md: only root-level resources
        first_seg = rel_path.split("/")[0] if "/" in rel_path else ""
        if first_seg not in _RESOURCE_DIRS:
            continue
        ext = os.path.splitext(rel_path)[1].lower()
        if ext not in _TEXT_EXTENSIONS:
            continue
        size = item.get("size", 0)
        if size > _MAX_RESOURCE_SIZE:
            continue
        resource_files.append({"path": rel_path, "full_path": item_path})
    return resource_files[:_MAX_RESOURCE_FILES]


async def _fetch_resource_contents(
    client: httpx.AsyncClient,
    raw_base: str,
    resource_files: list[dict[str, str]],
) -> dict[str, str]:
    """Fetch content for a list of resource files."""
    resources: dict[str, str] = {}
    for rf in resource_files:
        try:
            resp = await client.get(f"{raw_base}/{rf['full_path']}")
            if resp.status_code == 200:
                resources[rf["path"]] = resp.text
        except httpx.HTTPError:
            continue
    return resources


async def fetch_skill_from_github(url: str) -> SkillPackage:
    """Fetch a SKILL.md and bundled resources from a GitHub repository.

    Tries the following paths in order:
    1. Direct path from URL (if it points to a SKILL.md)
    2. ``SKILL.md`` at repo root
    3. ``skills/{name}/SKILL.md`` for monorepos (inferred from path)

    Uses ``TURNSTONE_GITHUB_TOKEN`` env var for authenticated requests
    (60 → 5000 req/hr rate limit headroom).
    """
    owner, repo, branch, path = _parse_github_url(url)
    if not owner:
        raise SkillSourceError(f"Could not parse GitHub URL: {url}")

    headers: dict[str, str] = {"Accept": "application/vnd.github.v3+json"}
    token = os.environ.get("TURNSTONE_GITHUB_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # When branch isn't specified in URL, try main then master
    branch_explicit = bool(_GITHUB_URL_RE.match(url) and _GITHUB_URL_RE.match(url).group("branch"))  # type: ignore[union-attr]
    branches_to_try = [branch] if branch_explicit else ["main", "master"]

    api_base = f"https://api.github.com/repos/{owner}/{repo}"

    # Determine SKILL.md path candidates
    path = path.rstrip("/")
    candidates: list[str] = []
    if path:
        if path.endswith("SKILL.md"):
            candidates.append(path)
        else:
            candidates.append(f"{path}/SKILL.md")
    candidates.append("SKILL.md")
    # Try skills/{last_segment}/SKILL.md for monorepos
    if path:
        last_seg = path.rsplit("/", 1)[-1]
        candidates.append(f"skills/{last_seg}/SKILL.md")

    # De-duplicate preserving order
    seen: set[str] = set()
    unique_candidates: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique_candidates.append(c)

    skill_md_content = ""
    skill_md_dir = ""
    resolved_branch = branch
    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0, headers=headers) as client:
        # Try each branch × candidate combination
        for try_branch in branches_to_try:
            raw_base = f"https://raw.githubusercontent.com/{owner}/{repo}/{try_branch}"
            for candidate in unique_candidates:
                try:
                    resp = await client.get(f"{raw_base}/{candidate}")
                    if resp.status_code == 200:
                        content_len = int(resp.headers.get("content-length", "0"))
                        if content_len > _MAX_SKILL_MD_SIZE:
                            continue
                        skill_md_content = resp.text[:_MAX_SKILL_MD_SIZE]
                        # Directory containing the SKILL.md
                        parts = candidate.rsplit("/", 1)
                        skill_md_dir = parts[0] if len(parts) > 1 else ""
                        resolved_branch = try_branch
                        break
                except httpx.HTTPError:
                    continue
            if skill_md_content:
                break

        if not skill_md_content:
            raise SkillNotFoundError(
                f"SKILL.md not found in {owner}/{repo} (tried {unique_candidates})"
            )

        parsed = parse_skill_md(skill_md_content)

        # Fetch bundled resources via GitHub API tree endpoint
        resources: dict[str, str] = {}
        raw_base = f"https://raw.githubusercontent.com/{owner}/{repo}/{resolved_branch}"
        try:
            tree_resp = await client.get(
                f"{api_base}/git/trees/{resolved_branch}",
                params={"recursive": "1"},
            )
            if tree_resp.status_code == 200 and len(tree_resp.content) < 2 * 1024 * 1024:
                tree_data = tree_resp.json()
                rf = _find_resource_files(tree_data.get("tree", []), skill_md_dir)
                resources = await _fetch_resource_contents(client, raw_base, rf)
        except httpx.HTTPError:
            logger.debug("Failed to fetch resource tree for %s/%s", owner, repo)

    # Build a per-skill source URL pointing to the specific subdirectory
    if skill_md_dir:
        specific_url = f"https://github.com/{owner}/{repo}/tree/{resolved_branch}/{skill_md_dir}"
    else:
        specific_url = url

    listing = SkillListing(
        id=f"{owner}/{repo}/{parsed.name}",
        name=parsed.name,
        description=parsed.description,
        author=parsed.author,
        source="github",
        source_url=specific_url,
        tags=parsed.tags,
    )

    return SkillPackage(listing=listing, parsed=parsed, resources=resources)


_MAX_SKILLS_PER_REPO = 50


async def fetch_skills_from_github_repo(url: str) -> list[SkillPackage]:
    """Scan a GitHub repo for all SKILL.md files and return each as a package.

    Used when a repo-level URL has no root SKILL.md (monorepo pattern).
    """
    owner, repo, branch, url_path = _parse_github_url(url)
    if not owner:
        raise SkillSourceError(f"Could not parse GitHub URL: {url}")
    url_path = url_path.rstrip("/")

    headers: dict[str, str] = {"Accept": "application/vnd.github.v3+json"}
    token = os.environ.get("TURNSTONE_GITHUB_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    branch_explicit = bool(_GITHUB_URL_RE.match(url) and _GITHUB_URL_RE.match(url).group("branch"))  # type: ignore[union-attr]
    branches_to_try = [branch] if branch_explicit else ["main", "master"]

    api_base = f"https://api.github.com/repos/{owner}/{repo}"

    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0, headers=headers) as client:
        # Find the tree with all SKILL.md files
        tree_data: dict[str, Any] = {}
        resolved_branch = branch
        for try_branch in branches_to_try:
            try:
                resp = await client.get(
                    f"{api_base}/git/trees/{try_branch}",
                    params={"recursive": "1"},
                )
                if resp.status_code == 200 and len(resp.content) < 2 * 1024 * 1024:
                    tree_data = resp.json()
                    resolved_branch = try_branch
                    break
            except httpx.HTTPError:
                continue

        if not tree_data:
            raise SkillSourceError(f"Could not fetch repo tree for {owner}/{repo}")

        # Find all SKILL.md files in the tree (filtered to URL path if provided)
        skill_md_paths: list[str] = []
        tree_items = tree_data.get("tree", [])
        for item in tree_items:
            if item.get("type") != "blob":
                continue
            p: str = item.get("path", "")
            if not (p.endswith("/SKILL.md") or p == "SKILL.md"):
                continue
            if url_path and not p.startswith(f"{url_path}/") and p != url_path:
                continue
            skill_md_paths.append(p)

        if not skill_md_paths:
            raise SkillNotFoundError(f"No SKILL.md files found in {owner}/{repo}")

        # Cap to prevent abuse
        skill_md_paths = skill_md_paths[:_MAX_SKILLS_PER_REPO]

        raw_base = f"https://raw.githubusercontent.com/{owner}/{repo}/{resolved_branch}"

        packages: list[SkillPackage] = []
        for skill_md_path in skill_md_paths:
            # Determine directory containing this SKILL.md
            parts = skill_md_path.rsplit("/", 1)
            skill_md_dir = parts[0] if len(parts) > 1 else ""

            # Fetch SKILL.md content
            try:
                resp = await client.get(f"{raw_base}/{skill_md_path}")
                if resp.status_code != 200:
                    continue
                content = resp.text[:_MAX_SKILL_MD_SIZE]
            except httpx.HTTPError:
                continue

            # Parse — skip if invalid
            try:
                parsed = parse_skill_md(content)
            except ValueError:
                logger.debug("Skipping invalid SKILL.md at %s", skill_md_path)
                continue

            # Collect resources for this skill
            rf = _find_resource_files(tree_items, skill_md_dir)
            resources = await _fetch_resource_contents(client, raw_base, rf)

            specific_url = (
                f"https://github.com/{owner}/{repo}/tree/{resolved_branch}/{skill_md_dir}"
                if skill_md_dir
                else url
            )
            listing = SkillListing(
                id=f"{owner}/{repo}/{parsed.name}",
                name=parsed.name,
                description=parsed.description,
                author=parsed.author,
                source="github",
                source_url=specific_url,
                tags=parsed.tags,
            )
            packages.append(SkillPackage(listing=listing, parsed=parsed, resources=resources))

    return packages
