"""Skill discovery source clients — skills.sh API + GitHub fetcher.

Provides :class:`SkillsShClient` for searching the skills.sh registry
and :func:`fetch_skill_from_github` for fetching SKILL.md from GitHub repos.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from turnstone.core.log import get_logger
from turnstone.core.skill_parser import ParsedSkill, parse_skill_md

log = get_logger(__name__)

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
# Per-segment charset for skills.sh ids — matches what GitHub permits in
# owner/repo/path-segment names. Rejects whitespace, control chars, and any
# URL-hostile content that would break the dedupe-by-source_url contract.
_SKILLS_SH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _accept_resource(rel_path: str, byte_size: int) -> bool:
    """Gate predicate shared by GitHub and skills.sh resource ingestion."""
    first_seg = rel_path.split("/", 1)[0]
    if first_seg not in _RESOURCE_DIRS:
        return False
    ext = os.path.splitext(rel_path)[1].lower()
    if ext not in _TEXT_EXTENSIONS:
        return False
    return byte_size <= _MAX_RESOURCE_SIZE


def _split_skills_sh_id(skill_id: str) -> tuple[str, str, str]:
    """Split a skills.sh canonical id into (owner, repo, skill_name).

    skills.sh ids are 3-segment paths like ``tavily-ai/skills/tavily-search``
    that map directly to its REST routes (``/api/download/[owner]/[repo]/[skill]``).
    """
    parts = skill_id.strip().strip("/").split("/")
    if len(parts) != 3 or not all(_SKILLS_SH_SEGMENT_RE.match(p) for p in parts):
        raise SkillSourceError(
            f"Invalid skills.sh skill id {skill_id!r}: expected 'owner/repo/skill-name'"
        )
    return parts[0], parts[1], parts[2]


def _skills_sh_source_url(base_url: str, skill_id: str) -> str:
    """Canonical, dedup-stable URL for a skills.sh skill.

    Normalizes the skill_id (strips outer whitespace and slashes) so
    `search()` and `download_skill()` agree on the persisted URL even
    when upstream returns sloppy ids — the discover-UI's
    "already installed" check matches against this exact string.
    """
    canonical = skill_id.strip().strip("/")
    return f"{base_url.rstrip('/')}/skills/{canonical}"


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
            skill_id = str(item.get("id", item.get("name", "")))
            # The /api/search response does not carry source_url. Derive a
            # canonical, dedup-stable URL from the skill id so the discover
            # UI's "already installed" check matches what download_skill
            # persists.
            raw_source_url = str(item.get("source_url", item.get("url", ""))).strip()
            source_url = raw_source_url or (
                _skills_sh_source_url(self._base_url, skill_id) if skill_id else ""
            )
            results.append(
                SkillListing(
                    id=skill_id,
                    name=str(item.get("name", "")),
                    description=str(item.get("description", "")),
                    author=str(item.get("author", "")),
                    source="skills.sh",
                    source_url=source_url,
                    install_count=int(item.get("install_count", item.get("installs", 0))),
                    tags=[str(t) for t in item.get("tags", []) if isinstance(t, str)],
                )
            )
        return results

    async def download_skill(self, skill_id: str) -> SkillPackage:
        """Download a skill bundle from skills.sh and return a ready-to-install package.

        Hits ``/api/download/{owner}/{repo}/{skill}`` (the unauthenticated
        install endpoint) which returns ``{"files": [{"path", "contents"}, ...]}``
        with the SKILL.md and any bundled resources inline. No GitHub round-trip.
        """
        owner, repo, name = _split_skills_sh_id(skill_id)

        url = f"{self._base_url}/api/download/{owner}/{repo}/{name}"
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
            try:
                resp = await client.get(url)
            except httpx.HTTPError as exc:
                raise SkillSourceError(f"Failed to download skill {skill_id}: {exc}") from exc

        if resp.status_code == 404:
            raise SkillNotFoundError(f"skills.sh has no skill {skill_id}")
        if resp.status_code >= 400:
            raise SkillSourceError(
                f"skills.sh download failed for {skill_id}: HTTP {resp.status_code}"
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise SkillSourceError(
                f"skills.sh returned non-JSON for {skill_id}: {resp.text[:200]}"
            ) from exc

        files = payload.get("files")
        if not isinstance(files, list) or not files:
            raise SkillSourceError(f"skills.sh returned no files for {skill_id}")

        skill_md_content = ""
        resource_pairs: list[tuple[str, str]] = []
        for entry in files:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path", "")).strip().lstrip("/")
            contents = entry.get("contents")
            if not path or not isinstance(contents, str):
                continue
            if path == "SKILL.md":
                # Measure UTF-8 bytes (not code points). `len(str)` is a
                # *lower* bound on encoded size — multi-byte chars (emoji,
                # CJK) inflate by up to 4×, so a code-point check would let
                # an oversized SKILL.md slip past the cap. Strict encoding
                # surfaces lone surrogates as a clear error.
                try:
                    skill_md_size = len(contents.encode("utf-8"))
                except UnicodeEncodeError as exc:
                    raise SkillSourceError(
                        f"SKILL.md for {skill_id} contains invalid Unicode: {exc}"
                    ) from exc
                if skill_md_size > _MAX_SKILL_MD_SIZE:
                    raise SkillSourceError(
                        f"SKILL.md for {skill_id} exceeds size cap ({_MAX_SKILL_MD_SIZE} bytes)"
                    )
                skill_md_content = contents
            else:
                resource_pairs.append((path, contents))

        if not skill_md_content:
            raise SkillNotFoundError(f"skills.sh bundle for {skill_id} has no SKILL.md")

        parsed = parse_skill_md(skill_md_content)

        resources: dict[str, str] = {}
        for path, contents in resource_pairs:
            if len(resources) >= _MAX_RESOURCE_FILES:
                break
            if not _accept_resource(path, len(contents.encode("utf-8"))):
                continue
            resources[path] = contents

        # Reconstruct from validated parts so the listing carries the
        # canonical id, never the raw caller input.
        canonical_id = f"{owner}/{repo}/{name}"
        listing = SkillListing(
            id=canonical_id,
            name=parsed.name or name,
            description=parsed.description,
            author=parsed.author,
            source="skills.sh",
            source_url=_skills_sh_source_url(self._base_url, canonical_id),
            install_count=0,
            tags=list(parsed.tags),
        )
        return SkillPackage(listing=listing, parsed=parsed, resources=resources)


def _parse_github_url(url: str) -> tuple[str, str, str, str, bool]:
    """Parse a GitHub URL into (owner, repo, branch, path, branch_explicit).

    Returns ("", "", "", "", False) if URL doesn't match.
    """
    m = _GITHUB_URL_RE.match(url)
    if not m:
        return ("", "", "", "", False)
    return (
        m.group("owner"),
        m.group("repo"),
        m.group("branch") or "main",
        m.group("path") or "",
        bool(m.group("branch")),
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
        if not _accept_resource(rel_path, item.get("size", 0)):
            continue
        resource_files.append({"path": rel_path, "full_path": item_path})
    return resource_files[:_MAX_RESOURCE_FILES]


def _check_rate_limit(resp: httpx.Response) -> None:
    """Raise SkillSourceError with guidance if GitHub rate limit is hit."""
    if resp.status_code == 403:
        remaining = resp.headers.get("x-ratelimit-remaining", "")
        if remaining == "0":
            raise SkillSourceError(
                "GitHub API rate limit exceeded. "
                "Set TURNSTONE_GITHUB_TOKEN env var for higher limits (5000 req/hr)."
            )
    remaining = resp.headers.get("x-ratelimit-remaining", "")
    if remaining and remaining.isdigit() and int(remaining) < 10:
        log.warning("GitHub API rate limit low: %s remaining", remaining)


_FETCH_CONCURRENCY = 5


async def _fetch_resource_contents(
    client: httpx.AsyncClient,
    raw_base: str,
    resource_files: list[dict[str, str]],
) -> dict[str, str]:
    """Fetch content for a list of resource files (concurrent)."""
    if not resource_files:
        return {}
    sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def _fetch_one(rf: dict[str, str]) -> tuple[str, str] | None:
        async with sem:
            try:
                resp = await client.get(f"{raw_base}/{rf['full_path']}")
                if resp.status_code == 200:
                    return rf["path"], resp.text
            except httpx.HTTPError:
                pass  # best-effort fetch, skip on failure
        return None

    results = await asyncio.gather(*[_fetch_one(rf) for rf in resource_files])
    return {path: content for r in results if r is not None for path, content in [r]}


async def fetch_skill_from_github(url: str) -> SkillPackage:
    """Fetch a SKILL.md and bundled resources from a GitHub repository.

    Tries the following paths in order:
    1. Direct path from URL (if it points to a SKILL.md)
    2. ``SKILL.md`` at repo root
    3. ``skills/{name}/SKILL.md`` for monorepos (inferred from path)

    Uses ``TURNSTONE_GITHUB_TOKEN`` env var for authenticated requests
    (60 → 5000 req/hr rate limit headroom).
    """
    owner, repo, branch, path, branch_explicit = _parse_github_url(url)
    if not owner:
        raise SkillSourceError(f"Could not parse GitHub URL: {url}")

    headers: dict[str, str] = {"Accept": "application/vnd.github.v3+json"}
    token = os.environ.get("TURNSTONE_GITHUB_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # When branch isn't specified in URL, try main then master
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
    _timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=_timeout, headers=headers
    ) as client:
        # Try each branch × candidate combination
        for try_branch in branches_to_try:
            raw_base = f"https://raw.githubusercontent.com/{owner}/{repo}/{try_branch}"
            for candidate in unique_candidates:
                try:
                    resp = await client.get(f"{raw_base}/{candidate}")
                    if resp.status_code == 200:
                        if len(resp.content) > _MAX_SKILL_MD_SIZE:
                            continue
                        skill_md_content = resp.text
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
            _check_rate_limit(tree_resp)
            if tree_resp.status_code == 200 and len(tree_resp.content) < 2 * 1024 * 1024:
                tree_data = tree_resp.json()
                rf = _find_resource_files(tree_data.get("tree", []), skill_md_dir)
                resources = await _fetch_resource_contents(client, raw_base, rf)
        except httpx.HTTPError:
            log.debug("Failed to fetch resource tree for %s/%s", owner, repo)

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
    owner, repo, branch, url_path, branch_explicit = _parse_github_url(url)
    if not owner:
        raise SkillSourceError(f"Could not parse GitHub URL: {url}")
    url_path = url_path.rstrip("/")

    headers: dict[str, str] = {"Accept": "application/vnd.github.v3+json"}
    token = os.environ.get("TURNSTONE_GITHUB_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    branches_to_try = [branch] if branch_explicit else ["main", "master"]

    api_base = f"https://api.github.com/repos/{owner}/{repo}"

    _timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=_timeout, headers=headers
    ) as client:
        # Find the tree with all SKILL.md files
        tree_data: dict[str, Any] = {}
        resolved_branch = branch
        for try_branch in branches_to_try:
            try:
                resp = await client.get(
                    f"{api_base}/git/trees/{try_branch}",
                    params={"recursive": "1"},
                )
                _check_rate_limit(resp)
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

        # Fetch all SKILL.md files concurrently
        sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

        async def _fetch_skill_md(p: str) -> tuple[str, str] | None:
            async with sem:
                try:
                    r = await client.get(f"{raw_base}/{p}")
                    if r.status_code == 200 and len(r.content) <= _MAX_SKILL_MD_SIZE:
                        return p, r.text
                except httpx.HTTPError:
                    pass  # best-effort fetch, skip on failure
            return None

        md_results = await asyncio.gather(*[_fetch_skill_md(p) for p in skill_md_paths])

        packages: list[SkillPackage] = []
        for result in md_results:
            if result is None:
                continue
            skill_md_path, content = result

            # Determine directory containing this SKILL.md
            parts = skill_md_path.rsplit("/", 1)
            skill_md_dir = parts[0] if len(parts) > 1 else ""

            # Parse — skip if invalid
            try:
                parsed = parse_skill_md(content)
            except ValueError:
                log.debug("Skipping invalid SKILL.md at %s", skill_md_path)
                continue

            # Collect resources for this skill (concurrent via helper)
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
