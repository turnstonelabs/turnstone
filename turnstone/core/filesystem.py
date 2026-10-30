"""Read-only, node-workspace file browsing for the interactive shell.

Roots are node-shared, not per-workstream sandboxes. Workstream access gates
the HTTP surface; paths are then confined to the selected root: the operator's
workspace (or process working directory), /data, or /tmp. No file bytes enter
a conversation.
"""

from __future__ import annotations

import asyncio
import errno
import os
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from starlette.responses import JSONResponse, Response

from turnstone.core.auth import require_permission
from turnstone.core.config import get_workspace_dir
from turnstone.core.file_responses import file_content_response, file_preview_response
from turnstone.core.preview import (
    PREVIEW_SIZE_CAPS,
    resolve_preview_kind,
    transcode_text,
)
from turnstone.core.workstream import WorkstreamKind

if TYPE_CHECKING:
    from collections.abc import Iterator

    from starlette.requests import Request

    from turnstone.core.session_routes import FilesystemHandlers, SessionEndpointConfig

DIRECTORY_ENTRY_CAP = 1000
DOWNLOAD_SIZE_CAP = 64 * 1024 * 1024
_SNIFF_SIZE = 8192
_FIXED_ROOTS = {"data": "/data", "tmp": "/tmp"}


class FilesystemError(Exception):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def _root_path(root_id: str) -> Path:
    if root_id not in ("workspace", *_FIXED_ROOTS):
        raise FilesystemError("Unknown filesystem root", 400)
    configured = (
        get_workspace_dir() or os.getcwd() if root_id == "workspace" else _FIXED_ROOTS[root_id]
    )
    return Path(configured).expanduser().absolute()


def _root(root_id: str) -> Path:
    try:
        root = _root_path(root_id).resolve(strict=True)
        if not root.is_dir():
            raise OSError("not a directory")
        return root
    except (OSError, RuntimeError) as exc:
        raise FilesystemError("Selected filesystem root is unavailable", 503) from exc


def _path(raw: str) -> str:
    if raw in ("", "."):
        return ""
    if len(raw) > 4096 or "\0" in raw or any(p in ("", ".", "..") for p in raw.split("/")):
        raise FilesystemError("Expected a relative workspace path", 400)
    return raw


def _os_error(exc: OSError) -> FilesystemError:
    if exc.errno in (errno.ELOOP, errno.EACCES, errno.EPERM):
        return FilesystemError("File is not accessible", 403)
    if exc.errno in (errno.ENOENT, errno.ENOTDIR):
        return FilesystemError("File or directory not found", 404)
    return FilesystemError("File or directory could not be read", 503)


@contextmanager
def _open_path(root: Path, path: str, *, directory: bool) -> Iterator[int]:
    """Walk descriptor-relative paths without following any user-selected link.

    O_NONBLOCK keeps a regular-file-to-FIFO race from blocking the server;
    fstat rejects special files before any read. Keeping parent descriptors
    open makes directory replacement unable to redirect a later open outside
    the workspace.
    """
    with ExitStack() as stack:
        try:
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, fd)
            parts = path.split("/") if path else []
            for index, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if index < len(parts) - 1 or directory:
                    flags |= os.O_DIRECTORY
                fd = os.open(part, flags, dir_fd=fd)
                stack.callback(os.close, fd)
            mode = os.fstat(fd).st_mode
            if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
                raise FilesystemError("Only regular files and directories are accessible", 400)
            yield fd
        except OSError as exc:
            raise _os_error(exc) from exc


def _preview_type(name: str, sample: bytes) -> tuple[str, str]:
    # A UTF-8 code point may straddle the sniff boundary. Drop only that final
    # incomplete sequence so a large source file is not mistaken for binary.
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.reason == "unexpected end of data":
            sample = sample[: exc.start]
    # The shared classifier also accepts URLs; quote literal '?' and '#' in filenames.
    return resolve_preview_kind("", quote(name), sample) or ("binary", "application/octet-stream")


def list_directory(raw_path: str, *, root_id: str = "workspace") -> dict[str, Any]:
    path, root = _path(raw_path), _root(root_id)
    entries: list[dict[str, Any]] = []
    truncated = False
    with _open_path(root, path, directory=True) as fd, os.scandir(fd) as scan:
        for index, entry in enumerate(scan):
            if index >= DIRECTORY_ENTRY_CAP:
                truncated = True
                break
            try:
                # Undecodable Unix filenames cannot round-trip through JSON.
                entry.name.encode("utf-8")
                info = entry.stat(follow_symlinks=False)
                rel = path + "/" + entry.name if path else entry.name
                item: dict[str, Any] = {"name": entry.name, "path": rel, "modified": info.st_mtime}
                if stat.S_ISDIR(info.st_mode):
                    item["kind"] = "directory"
                elif stat.S_ISREG(info.st_mode):
                    with _open_path(root, rel, directory=False) as file_fd:
                        info = os.fstat(file_fd)
                        kind, mime = _preview_type(entry.name, os.read(file_fd, _SNIFF_SIZE))
                    item.update(
                        kind="file", size=info.st_size, preview_kind=kind, content_type=mime
                    )
                else:
                    continue
                entries.append(item)
            except (OSError, FilesystemError, UnicodeEncodeError):
                # A disappearing/inaccessible entry must not hide the rest of
                # a directory. Opening it directly still reports the failure.
                continue
    entries.sort(
        key=lambda item: (item["kind"] != "directory", item["name"].casefold(), item["name"])
    )
    return {
        "root": str(root),
        "root_id": root_id,
        "path": path,
        "parent": path.rpartition("/")[0] if path else None,
        "entries": entries,
        "truncated": truncated,
    }


def read_file(
    raw_path: str, *, preview: bool, root_id: str = "workspace"
) -> tuple[bytes, str, str]:
    path, root = _path(raw_path), _root(root_id)
    with _open_path(root, path, directory=False) as fd:
        size = os.fstat(fd).st_size
        sample = os.read(fd, _SNIFF_SIZE)
        kind, mime = _preview_type(path, sample)
        if preview and kind == "binary":
            raise FilesystemError("File is not previewable; download it to open locally", 415)
        cap = PREVIEW_SIZE_CAPS[kind] if preview else DOWNLOAD_SIZE_CAP
        if size > cap:
            raise FilesystemError(
                f"File exceeds the {cap // 1024 // 1024 or cap // 1024} {'MiB' if cap >= 1024 * 1024 else 'KiB'} {'preview' if preview else 'download'} limit",
                413,
            )
        chunks = [sample]
        total = len(sample)
        while total <= cap:
            chunk = os.read(fd, min(64 * 1024, cap + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > cap:
            raise FilesystemError("File exceeds the size limit", 413)
        data = b"".join(chunks)
    if preview:
        resolved = resolve_preview_kind("", quote(path), data)
        if resolved is None:
            raise FilesystemError("File is not previewable; download it to open locally", 415)
        kind, mime = resolved
        if kind in ("web", "table", "text", "markdown"):
            data = transcode_text(data, mime).encode("utf-8")
        if len(data) > PREVIEW_SIZE_CAPS[kind]:
            raise FilesystemError("File exceeds the preview size limit", 413)
    return data, mime, path.rsplit("/", 1)[-1]


def resolve_file(raw_path: str) -> dict[str, Any]:
    """Locate a tool path in the current node's browsable roots."""
    if not raw_path or len(raw_path) > 4096 or "\0" in raw_path:
        raise FilesystemError("Expected a file path", 400)
    expanded = os.path.expanduser(raw_path)
    absolute = Path(os.path.abspath(expanded))
    candidates: list[tuple[int, str, Path, str]] = []
    for root_id in ("workspace", *_FIXED_ROOTS):
        try:
            root = _root(root_id)
            aliases = {_root_path(root_id), root}
        except (FilesystemError, OSError):
            continue
        for alias in aliases:
            if absolute.is_relative_to(alias):
                relative = absolute.relative_to(alias).as_posix()
                candidates.append((len(alias.parts), root_id, root, relative))
    if not candidates:
        raise FilesystemError("This file is outside the browsable roots", 403)
    _, root_id, root, relative = max(candidates, key=lambda item: item[0])
    path = _path(relative)
    # Root aliases are allowed; user-selected symlinks below them are not.
    # Compare before opening so normalization of a link followed by '..'
    # cannot silently select a different file from the tool's target.
    if Path(os.path.realpath(expanded)) != root / path:
        raise FilesystemError("File is not accessible through a symbolic link", 403)
    with _open_path(root, path, directory=False) as fd:
        info = os.fstat(fd)
        kind, mime = _preview_type(path, os.read(fd, _SNIFF_SIZE))
    result = list_directory(path.rpartition("/")[0], root_id=root_id)
    # A direct link must still select its file when the directory is capped.
    if not any(entry["path"] == path for entry in result["entries"]):
        result["entries"].append(
            {
                "name": path.rsplit("/", 1)[-1],
                "path": path,
                "kind": "file",
                "size": info.st_size,
                "modified": info.st_mtime,
                "preview_kind": kind,
                "content_type": mime,
            }
        )
        result["entries"].sort(
            key=lambda item: (item["kind"] != "directory", item["name"].casefold(), item["name"])
        )
    result["selected"] = path
    return result


def make_filesystem_handlers(cfg: SessionEndpointConfig) -> FilesystemHandlers:
    from turnstone.core.session_routes import FilesystemHandlers

    async def gate(request: Request) -> Response | None:
        if cfg.permission_gate is not None:
            err = cfg.permission_gate(request)
            if err is not None:
                return err
        err = require_permission(request, "files.read")
        if err is not None:
            return err
        mgr, err = cfg.manager_lookup(request)
        if err is not None:
            return err
        if cfg.attachment_owner_resolver is None or mgr is None:
            return JSONResponse({"error": "Filesystem access is unavailable"}, status_code=503)
        ws_id = request.path_params["ws_id"]
        ws = mgr.loaded(ws_id)
        if ws is None or ws.kind != WorkstreamKind.INTERACTIVE or ws._closed:
            return JSONResponse(
                {
                    "error": "Workstream is not active on this node. Reopen the conversation to browse its files."
                },
                status_code=409,
            )
        _owner, err = await asyncio.to_thread(cfg.attachment_owner_resolver, request, ws_id, mgr)
        return err

    async def listing(request: Request) -> Response:
        err = await gate(request)
        if err is not None:
            return err
        try:
            result = await asyncio.to_thread(
                list_directory,
                request.query_params.get("path", ""),
                root_id=request.query_params.get("root", "workspace"),
            )
            return JSONResponse(result, headers={"Cache-Control": "private, no-store"})
        except FilesystemError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)

    async def serve(request: Request, *, preview: bool) -> Response:
        err = await gate(request)
        if err is not None:
            return err
        try:
            data, mime, filename = await asyncio.to_thread(
                read_file,
                request.query_params.get("path", ""),
                preview=preview,
                root_id=request.query_params.get("root", "workspace"),
            )
        except FilesystemError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)
        if preview:
            return file_preview_response(
                data,
                mime,
                filename,
                allow_remote_assets=request.query_params.get("assets") == "1",
                probe=request.query_params.get("probe") == "1",
            )
        return file_content_response(data, "application/octet-stream", filename, download=True)

    async def preview(request: Request) -> Response:
        return await serve(request, preview=True)

    async def content(request: Request) -> Response:
        return await serve(request, preview=False)

    async def resolve(request: Request) -> Response:
        err = await gate(request)
        if err is not None:
            return err
        try:
            result = await asyncio.to_thread(resolve_file, request.query_params.get("path", ""))
            return JSONResponse(result, headers={"Cache-Control": "private, no-store"})
        except FilesystemError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)

    return FilesystemHandlers(list=listing, preview=preview, content=content, resolve=resolve)
