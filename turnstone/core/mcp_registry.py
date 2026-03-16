"""MCP Registry client — queries the official MCP Registry API for server discovery.

Standalone HTTP client with no dependencies on Turnstone storage/auth layers.
Uses httpx for async HTTP requests and returns typed dataclasses.

Registry API docs: https://registry.modelcontextprotocol.io/docs
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_REGISTRY_URL = "https://registry.modelcontextprotocol.io"
_SEARCH_PATH = "/v0.1/servers"
_REQUEST_TIMEOUT = 15.0
_MAX_LIMIT = 100

# Valid MCP server name pattern (must match _MCP_NAME_RE in console/server.py)
_MCP_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


class MCPRegistryError(Exception):
    """Error communicating with or parsing responses from the MCP Registry."""

    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Response dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegistryRemoteHeader:
    name: str
    description: str = ""
    is_required: bool = False
    is_secret: bool = False


@dataclass(frozen=True, slots=True)
class RegistryRemoteVariable:
    description: str = ""
    is_required: bool = False
    choices: list[str] | None = None
    default: str = ""


@dataclass(frozen=True, slots=True)
class RegistryRemote:
    type: str  # e.g. "streamable-http"
    url: str
    headers: list[RegistryRemoteHeader] = field(default_factory=list)
    variables: dict[str, RegistryRemoteVariable] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RegistryEnvVar:
    name: str
    description: str = ""
    is_required: bool = False
    is_secret: bool = False
    default: str = ""


@dataclass(frozen=True, slots=True)
class RegistryPackage:
    registry_type: str  # "npm" | "pypi" | "oci" | "nuget" | "mcpb"
    identifier: str
    version: str = ""
    transport_type: str = "stdio"
    environment_variables: list[RegistryEnvVar] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RegistryServerMeta:
    status: str = ""
    published_at: str = ""
    updated_at: str = ""
    is_latest: bool = False


@dataclass(frozen=True, slots=True)
class RegistryIcon:
    src: str
    mime_type: str = ""


@dataclass(frozen=True, slots=True)
class RegistryRepository:
    url: str = ""
    source: str = ""
    id: str = ""


@dataclass(frozen=True, slots=True)
class RegistryServer:
    name: str
    description: str = ""
    title: str = ""
    version: str = ""
    website_url: str = ""
    repository: RegistryRepository | None = None
    icons: list[RegistryIcon] = field(default_factory=list)
    remotes: list[RegistryRemote] = field(default_factory=list)
    packages: list[RegistryPackage] = field(default_factory=list)
    meta: RegistryServerMeta | None = None


@dataclass(frozen=True, slots=True)
class RegistrySearchResult:
    servers: list[RegistryServer]
    total_count: int = 0
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _safe_int(value: Any, default: int) -> int:
    """Cast to int with fallback."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _parse_remote_header(raw: dict[str, Any]) -> RegistryRemoteHeader:
    return RegistryRemoteHeader(
        name=str(raw.get("name", "")),
        description=str(raw.get("description", "")),
        is_required=bool(raw.get("isRequired", False)),
        is_secret=bool(raw.get("isSecret", False)),
    )


def _parse_remote_variable(raw: dict[str, Any]) -> RegistryRemoteVariable:
    choices_raw = raw.get("choices")
    choices = [str(c) for c in choices_raw] if isinstance(choices_raw, list) else None
    return RegistryRemoteVariable(
        description=str(raw.get("description", "")),
        is_required=bool(raw.get("isRequired", False)),
        choices=choices,
        default=str(raw.get("default", "")),
    )


def _parse_remote(raw: dict[str, Any]) -> RegistryRemote:
    headers_raw = raw.get("headers") or []
    variables_raw = raw.get("variables") or {}
    return RegistryRemote(
        type=str(raw.get("type", "")),
        url=str(raw.get("url", "")),
        headers=[_parse_remote_header(h) for h in headers_raw if isinstance(h, dict)],
        variables={
            str(k): _parse_remote_variable(v)
            for k, v in variables_raw.items()
            if isinstance(v, dict)
        },
    )


def _parse_env_var(raw: dict[str, Any]) -> RegistryEnvVar:
    return RegistryEnvVar(
        name=str(raw.get("name", "")),
        description=str(raw.get("description", "")),
        is_required=bool(raw.get("isRequired", False)),
        is_secret=bool(raw.get("isSecret", False)),
        default=str(raw.get("default", "")),
    )


def _parse_package(raw: dict[str, Any]) -> RegistryPackage:
    transport = raw.get("transport") or {}
    env_vars_raw = raw.get("environmentVariables") or []
    return RegistryPackage(
        registry_type=str(raw.get("registryType", "")),
        identifier=str(raw.get("identifier", "")),
        version=str(raw.get("version", "")),
        transport_type=str(transport.get("type", "stdio"))
        if isinstance(transport, dict)
        else "stdio",
        environment_variables=[_parse_env_var(e) for e in env_vars_raw if isinstance(e, dict)],
    )


def _parse_meta(raw: dict[str, Any]) -> RegistryServerMeta | None:
    meta_key = "io.modelcontextprotocol.registry/official"
    meta_data = raw.get(meta_key)
    if not isinstance(meta_data, dict):
        return None
    return RegistryServerMeta(
        status=str(meta_data.get("status", "")),
        published_at=str(meta_data.get("publishedAt", "")),
        updated_at=str(meta_data.get("updatedAt", "")),
        is_latest=bool(meta_data.get("isLatest", False)),
    )


def _parse_server_entry(raw: dict[str, Any]) -> RegistryServer:
    """Parse a single entry from the registry ``servers`` array."""
    server_data = raw.get("server") or {}
    packages_raw = raw.get("packages") or []
    meta_raw = raw.get("_meta") or {}

    repo_raw = server_data.get("repository")
    repository = None
    if isinstance(repo_raw, dict):
        repository = RegistryRepository(
            url=str(repo_raw.get("url", "")),
            source=str(repo_raw.get("source", "")),
            id=str(repo_raw.get("id", "")),
        )

    icons_raw = server_data.get("icons") or []
    remotes_raw = server_data.get("remotes") or []

    return RegistryServer(
        name=str(server_data.get("name", "")),
        description=str(server_data.get("description", "")),
        title=str(server_data.get("title", "")),
        version=str(server_data.get("version", "")),
        website_url=str(server_data.get("websiteUrl", "")),
        repository=repository,
        icons=[
            RegistryIcon(
                src=str(i.get("src", "")),
                mime_type=str(i.get("mimeType", "")),
            )
            for i in icons_raw
            if isinstance(i, dict)
        ],
        remotes=[_parse_remote(r) for r in remotes_raw if isinstance(r, dict)],
        packages=[_parse_package(p) for p in packages_raw if isinstance(p, dict)],
        meta=_parse_meta(meta_raw),
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class MCPRegistryClient:
    """Async HTTP client for the official MCP Registry API."""

    def __init__(
        self,
        base_url: str = DEFAULT_REGISTRY_URL,
        timeout: float = _REQUEST_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def search(
        self,
        q: str = "",
        limit: int = 20,
        cursor: str | None = None,
    ) -> RegistrySearchResult:
        """Search the registry for MCP servers.

        Args:
            q: Search query string (empty returns all).
            limit: Max results per page (1-100).
            cursor: Opaque cursor for pagination.

        Returns:
            RegistrySearchResult with parsed server entries.

        Raises:
            MCPRegistryError: On HTTP errors or response parse failures.
        """
        params: dict[str, str] = {"latest": "true"}
        if q:
            params["search"] = q
        params["limit"] = str(min(max(1, limit), _MAX_LIMIT))
        if cursor:
            params["cursor"] = cursor

        url = f"{self._base_url}{_SEARCH_PATH}"
        try:
            resp = await self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise MCPRegistryError(f"HTTP request failed: {exc}") from exc

        if resp.status_code != 200:
            raise MCPRegistryError(
                f"Registry returned {resp.status_code}: {resp.text[:500]}",
                status_code=resp.status_code,
            )

        try:
            data = resp.json()
        except (ValueError, TypeError) as exc:
            raise MCPRegistryError(f"Invalid JSON response: {exc}") from exc

        servers_raw = data.get("servers") or []
        metadata = data.get("metadata") or {}

        servers = [_parse_server_entry(entry) for entry in servers_raw if isinstance(entry, dict)]

        return RegistrySearchResult(
            servers=servers,
            total_count=_safe_int(metadata.get("count"), len(servers)),
            next_cursor=metadata.get("nextCursor") or None,
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> MCPRegistryClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


# ---------------------------------------------------------------------------
# Install helpers
# ---------------------------------------------------------------------------

# Supported package types and their command mappings
_PACKAGE_COMMANDS: dict[str, tuple[str, list[str]]] = {
    "npm": ("npx", ["-y"]),
    "pypi": ("uvx", []),
}


def resolve_install_config(
    server: RegistryServer,
    source: str,
    index: int = 0,
    variables: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Convert a RegistryServer entry into a config dict for ``create_mcp_server()``.

    Args:
        server: Parsed registry server.
        source: ``"remote"`` or ``"package"``.
        index: Which remote/package entry to use.
        variables: Values for URL template ``{var}`` placeholders.

    Returns:
        Dict with transport config plus ``registry_name``, ``registry_version``,
        ``registry_meta`` keys.

    Raises:
        MCPRegistryError: On unsupported package type or missing required variables.
        IndexError: If index is out of range.
    """
    variables = variables or {}

    # Build registry metadata snapshot
    meta_snapshot: dict[str, Any] = {
        "description": server.description,
        "title": server.title,
        "website_url": server.website_url,
    }
    if server.repository:
        meta_snapshot["repository"] = {
            "url": server.repository.url,
            "source": server.repository.source,
        }
    if server.icons:
        meta_snapshot["icons"] = [{"src": ic.src, "mime_type": ic.mime_type} for ic in server.icons]

    base: dict[str, Any] = {
        "registry_name": server.name,
        "registry_version": server.version,
        "registry_meta": meta_snapshot,
    }

    if source == "remote":
        if not server.remotes:
            raise MCPRegistryError("Server has no remote endpoints")
        if index < 0 or index >= len(server.remotes):
            raise IndexError(f"Remote index {index} out of range (0-{len(server.remotes) - 1})")

        remote = server.remotes[index]
        url = remote.url

        # Substitute URL template variables
        for var_name, var_def in remote.variables.items():
            placeholder = "{" + var_name + "}"
            if placeholder in url:
                value = variables.get(var_name, "")
                if not value and var_def.default:
                    value = var_def.default
                if not value and var_def.is_required:
                    raise MCPRegistryError(f"Required URL variable '{var_name}' not provided")
                url = url.replace(placeholder, value)

        # Build headers dict (required keys only — values provided by user at install time)
        headers: dict[str, str] = {}
        for h in remote.headers:
            if h.is_required:
                headers[h.name] = ""

        return {
            **base,
            "transport": "streamable-http",
            "url": url,
            "headers": headers,
            "env": {},
        }

    elif source == "package":
        if not server.packages:
            raise MCPRegistryError("Server has no installable packages")
        if index < 0 or index >= len(server.packages):
            raise IndexError(f"Package index {index} out of range (0-{len(server.packages) - 1})")

        pkg = server.packages[index]
        cmd_info = _PACKAGE_COMMANDS.get(pkg.registry_type)
        if cmd_info is None:
            raise MCPRegistryError(
                f"Unsupported package type: {pkg.registry_type!r}. "
                f"Supported types: {', '.join(sorted(_PACKAGE_COMMANDS))}"
            )

        command, base_args = cmd_info
        identifier = pkg.identifier
        if pkg.version and pkg.registry_type == "npm":
            # npm: npx -y @scope/pkg@version
            if "@" not in identifier.split("/")[-1]:
                identifier = f"{identifier}@{pkg.version}"
        elif pkg.version and pkg.registry_type == "pypi" and "==" not in identifier:
            # pypi: uvx pkg==version
            identifier = f"{identifier}=={pkg.version}"

        args = [*base_args, identifier]

        # Build env dict (keys only — values provided by user at install time)
        env: dict[str, str] = {}
        for ev in pkg.environment_variables:
            env[ev.name] = ev.default or ""

        return {
            **base,
            "transport": "stdio",
            "command": command,
            "args": args,
            "env": env,
        }

    else:
        raise MCPRegistryError(f"Invalid source: {source!r}. Must be 'remote' or 'package'.")


def sanitize_registry_name(name: str) -> str:
    """Convert a registry name (reverse-DNS with slashes) to a valid MCP server name.

    e.g. ``"ai.example/mcp-server"`` → ``"ai.example.mcp-server"``

    Raises:
        MCPRegistryError: If the sanitized name is empty or invalid.
    """
    # Replace / with .
    sanitized = name.replace("/", ".")
    # Strip characters not matching [a-zA-Z0-9._-]
    sanitized = re.sub(r"[^a-zA-Z0-9._-]", "", sanitized)
    # Truncate to 64 characters
    sanitized = sanitized[:64]
    # Strip leading/trailing dots and dashes
    sanitized = sanitized.strip(".-")

    if not sanitized:
        raise MCPRegistryError(f"Cannot derive a valid server name from '{name}'")
    if not _MCP_NAME_RE.match(sanitized):
        raise MCPRegistryError(f"Sanitized name '{sanitized}' is invalid")
    if "__" in sanitized:
        raise MCPRegistryError(f"Sanitized name '{sanitized}' contains reserved '__'")

    return sanitized


def registry_server_to_dict(server: RegistryServer) -> dict[str, Any]:
    """Convert a RegistryServer dataclass to a JSON-serializable dict."""
    result: dict[str, Any] = {
        "name": server.name,
        "description": server.description,
        "title": server.title,
        "version": server.version,
        "website_url": server.website_url,
        "icons": [{"src": ic.src, "mime_type": ic.mime_type} for ic in server.icons],
        "remotes": [
            {
                "type": r.type,
                "url": r.url,
                "headers": [
                    {
                        "name": h.name,
                        "description": h.description,
                        "is_required": h.is_required,
                        "is_secret": h.is_secret,
                    }
                    for h in r.headers
                ],
                "variables": {
                    k: {
                        "description": v.description,
                        "is_required": v.is_required,
                        "choices": v.choices,
                        "default": v.default,
                    }
                    for k, v in r.variables.items()
                },
            }
            for r in server.remotes
        ],
        "packages": [
            {
                "registry_type": p.registry_type,
                "identifier": p.identifier,
                "version": p.version,
                "transport_type": p.transport_type,
                "environment_variables": [
                    {
                        "name": ev.name,
                        "description": ev.description,
                        "is_required": ev.is_required,
                        "is_secret": ev.is_secret,
                        "default": ev.default,
                    }
                    for ev in p.environment_variables
                ],
            }
            for p in server.packages
        ],
        "installed": False,
    }
    if server.repository:
        result["repository"] = {
            "url": server.repository.url,
            "source": server.repository.source,
        }
    else:
        result["repository"] = {}
    if server.meta:
        result["meta"] = {
            "status": server.meta.status,
            "published_at": server.meta.published_at,
            "updated_at": server.meta.updated_at,
            "is_latest": server.meta.is_latest,
        }
    else:
        result["meta"] = {}
    return result
