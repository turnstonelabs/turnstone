"""Starlette web-request helpers shared across HTTP servers."""

from __future__ import annotations

import contextlib
import json
import os
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from starlette.middleware import Middleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse


def skill_summary_rows(storage: Any) -> list[dict[str, Any]]:
    """Build the public picker payload for ``/v1/api/skills``.

    Shared between ``turnstone/server.py`` (standalone node) and
    ``turnstone/console/server.py`` (console-managed cluster), both of
    which expose ``/v1/api/skills`` to user-facing UI.  Bodies were
    character-identical before #571 added the ``hidden_from_menu``
    filter — extraction here keeps the two surfaces from drifting on
    every future spec-uplift field (#572's ``argument_hint`` for
    autocomplete is the next likely caller).

    Filters:
    * ``enabled=False`` rows are dropped.
    * ``hidden_from_menu=True`` rows are dropped (SKILL.md spec
      ``user-invocable: false`` — model still sees the skill via the
      ``skills`` tool, but the user picker hides it).

    Filtering happens in Python rather than SQL to match the
    pre-existing ``enabled`` pattern; pushing to SQL would be a
    separate change to ``list_prompt_templates``.
    """
    rows = storage.list_prompt_templates()
    skills: list[dict[str, Any]] = []
    for r in rows:
        if not r.get("enabled", True):
            continue
        if r.get("hidden_from_menu"):
            continue
        tags: list[str] = []
        with contextlib.suppress(ValueError, TypeError):
            tags = json.loads(r.get("tags", "[]"))
        skills.append(
            {
                "name": r["name"],
                "category": r.get("category", ""),
                "description": r.get("description", ""),
                "tags": tags,
                "is_default": r.get("is_default", False),
                "activation": r.get("activation", "named"),
                "origin": r.get("origin", "manual"),
                "author": r.get("author", ""),
                "version": r.get("version", "1.0.0"),
            }
        )
    return skills


async def read_json_or_400(request: Request) -> dict[str, Any] | JSONResponse:
    """Parse a JSON request body, returning a 400 response on failure.

    Callers should check ``isinstance(result, JSONResponse)`` and return
    it early when the parse fails::

        body = await read_json_or_400(request)
        if isinstance(body, JSONResponse):
            return body
    """
    from starlette.responses import JSONResponse as _JSONResponse

    try:
        body: dict[str, Any] = await request.json()
        return body
    except (ValueError, json.JSONDecodeError):
        return _JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    except Exception:
        import structlog

        structlog.get_logger(__name__).warning("read_json_or_400.unexpected", exc_info=True)
        return _JSONResponse({"error": "Failed to read request body"}, status_code=500)


async def read_multipart_create_or_400(
    request: Request,
    *,
    meta_field: str = "meta",
    file_field: str = "file",
    max_files: int = 10,
    max_per_file_bytes: int | None = None,
    max_total_bytes: int | None = None,
) -> tuple[dict[str, Any], list[tuple[str, str, bytes]]] | JSONResponse:
    """Parse a multipart create-with-attachments body.

    Expects one ``meta`` field (JSON-encoded object with create metadata)
    and zero-or-more ``file`` parts (standard UploadFile objects).  Returns
    ``(meta_dict, [(filename, content_type, bytes), ...])`` on success or a
    ``JSONResponse`` (400/413) on failure.

    Enforces a cheap ``Content-Length`` pre-check against *max_total_bytes*
    when the header is sensible, and (when ``max_per_file_bytes`` is set) a
    generic per-file cap as a defense-in-depth gate.  The caller still
    classifies each file and applies any kind-specific cap on top.
    """
    from starlette.datastructures import UploadFile
    from starlette.responses import JSONResponse as _JSONResponse

    if max_total_bytes is not None:
        cl_raw = request.headers.get("content-length")
        if cl_raw:
            try:
                cl = int(cl_raw)
            except ValueError:
                cl = -1
            if cl > int(max_total_bytes * 1.1):
                return _JSONResponse(
                    {
                        "error": (
                            f"Request body too large ({cl:,} bytes by Content-Length); "
                            f"cap is {max_total_bytes:,} bytes."
                        ),
                        "code": "too_large",
                    },
                    status_code=413,
                )

    try:
        form = await request.form()
    except Exception:
        import structlog

        structlog.get_logger(__name__).warning(
            "read_multipart_create_or_400.parse_failed", exc_info=True
        )
        return _JSONResponse({"error": "Invalid multipart body"}, status_code=400)

    meta_raw = form.get(meta_field)
    if not isinstance(meta_raw, str):
        return _JSONResponse({"error": f"Missing '{meta_field}' JSON field"}, status_code=400)
    try:
        meta: dict[str, Any] = json.loads(meta_raw)
    except (ValueError, json.JSONDecodeError):
        return _JSONResponse({"error": f"'{meta_field}' field must be valid JSON"}, status_code=400)
    if not isinstance(meta, dict):
        return _JSONResponse({"error": f"'{meta_field}' must be a JSON object"}, status_code=400)

    uploads = [v for v in form.getlist(file_field) if isinstance(v, UploadFile)]
    if len(uploads) > max_files:
        return _JSONResponse(
            {
                "error": (f"Too many files ({len(uploads)}); max {max_files} per request"),
                "code": "too_many",
            },
            status_code=400,
        )

    files: list[tuple[str, str, bytes]] = []
    running_total = 0
    try:
        for upload in uploads:
            filename = upload.filename or ""
            content_type = upload.content_type or "application/octet-stream"
            try:
                data = await upload.read()
            except Exception:
                return _JSONResponse({"error": "Failed to read upload"}, status_code=400)
            if max_per_file_bytes is not None and len(data) > max_per_file_bytes:
                return _JSONResponse(
                    {
                        "error": (
                            f"File too large ({len(data):,} bytes); "
                            f"cap is {max_per_file_bytes:,} bytes."
                        ),
                        "code": "too_large",
                    },
                    status_code=413,
                )
            running_total += len(data)
            if max_total_bytes is not None and running_total > max_total_bytes:
                return _JSONResponse(
                    {
                        "error": (
                            f"Request body too large ({running_total:,} bytes total); "
                            f"cap is {max_total_bytes:,} bytes."
                        ),
                        "code": "too_large",
                    },
                    status_code=413,
                )
            files.append((filename, content_type, data))
    finally:
        for upload in uploads:
            await upload.close()

    return meta, files


async def read_multipart_file_or_400(
    request: Request,
    field: str = "file",
    max_bytes: int | None = None,
) -> tuple[str, str, bytes] | JSONResponse:
    """Parse a single multipart-upload file field.

    Returns ``(filename, content_type, bytes)`` on success or a
    ``JSONResponse`` (400/413) on failure.  When ``max_bytes`` is set
    and a sensible ``Content-Length`` header arrives, a 413 is returned
    before the body is parsed (cheap gate against grossly oversized
    uploads).  Otherwise the body is fully buffered (Starlette spools
    large uploads to disk beyond ~1 MiB) and re-checked against
    ``max_bytes`` post-read.
    """
    from starlette.datastructures import UploadFile
    from starlette.responses import JSONResponse as _JSONResponse

    # Cheap pre-read gate: if Content-Length grossly exceeds max_bytes,
    # reject without parsing the body.  A 10% slack absorbs multipart
    # framing overhead.  Missing / malformed Content-Length falls through
    # to the post-read check.
    if max_bytes is not None:
        cl_raw = request.headers.get("content-length")
        if cl_raw:
            try:
                cl = int(cl_raw)
            except ValueError:
                cl = -1
            if cl > int(max_bytes * 1.1):
                return _JSONResponse(
                    {
                        "error": (
                            f"File too large ({cl:,} bytes by Content-Length); "
                            f"cap is {max_bytes:,} bytes."
                        ),
                        "code": "too_large",
                    },
                    status_code=413,
                )

    try:
        form = await request.form()
    except Exception:
        import structlog

        structlog.get_logger(__name__).warning(
            "read_multipart_file_or_400.parse_failed", exc_info=True
        )
        return _JSONResponse({"error": "Invalid multipart body"}, status_code=400)

    upload = form.get(field)
    if not isinstance(upload, UploadFile):
        return _JSONResponse({"error": f"Missing '{field}' file field"}, status_code=400)

    filename = upload.filename or ""
    content_type = upload.content_type or "application/octet-stream"
    try:
        data = await upload.read()
    except Exception:
        return _JSONResponse({"error": "Failed to read upload"}, status_code=400)
    finally:
        await upload.close()

    if max_bytes is not None and len(data) > max_bytes:
        return _JSONResponse(
            {
                "error": (f"File too large ({len(data):,} bytes); cap is {max_bytes:,} bytes."),
                "code": "too_large",
            },
            status_code=413,
        )

    return filename, content_type, data


def require_storage_or_503(
    request: Request,
) -> tuple[Any, JSONResponse | None]:
    """Return ``(storage, None)`` or ``(None, JSONResponse(503))``.

    Usage::

        storage, err = require_storage_or_503(request)
        if err:
            return err
    """
    from starlette.responses import JSONResponse as _JSONResponse

    storage = getattr(request.app.state, "auth_storage", None)
    if storage is None:
        return None, _JSONResponse({"error": "Storage not available"}, status_code=503)
    return storage, None


def auth_user_id(request: Request) -> str:
    """Return the authenticated user's id (empty string when absent).

    Reads ``request.state.auth_result.user_id`` set by
    :class:`AuthMiddleware`. Both node and console processes carry the
    same ``AuthResult`` shape, so this helper is kind-agnostic — useful
    for handlers lifted into :mod:`turnstone.core.session_routes`.
    """
    auth = getattr(getattr(request, "state", None), "auth_result", None)
    return str(getattr(auth, "user_id", "") or "")


def resolve_workstream_owner(
    request: Request,
    ws_id: str,
    *,
    mgr: Any | None = None,
    not_found_label: str = "Workstream not found",
) -> tuple[str, JSONResponse | None]:
    """Resolve ``ws_id`` to its owner; 404 when the row doesn't exist.

    Turnstone is a trusted-team tool: scope-level auth (e.g.
    ``admin.coordinator``) is the primary gate and row-level OWNERSHIP
    is still not enforced here. The one row-level check this performs
    is PROJECT tenancy: a workstream attached to a *private* project is
    only reachable by the project's owner/members, the workstream's own
    creator, service-scope callers, and ``admin.cluster.inspect``
    holders — everyone else gets a 403 (see
    :class:`turnstone.core.auth.WorkstreamProjectVisibility`). Returns
    ``(owner_user_id, None)`` on success — the persisted owner id,
    which attachments should be filed under so existing storage shape
    is preserved. Falls back to the caller's own uid when the row has
    no recorded owner.

    When ``mgr`` is provided and the workstream is live in memory,
    trust its cached ``user_id`` / ``project_id`` instead of
    round-tripping storage for the ROW — but note the project gate
    itself may still hit storage (one memoized ``get_project`` +
    membership lookup when the row names a project), and it fails
    CLOSED: during a transient DB outage a project-attached workstream
    403s rather than risking a leak. That is a deliberate trade — a
    DB outage already degrades sends/persistence, and failing open on
    a private-tenancy check is the worse failure. Project-less
    workstreams keep the zero-storage fast path.

    ``not_found_label`` is the message body the 404 carries — the
    interactive surface uses "Workstream not found"; coord uses
    "coordinator not found" so error strings stay readable per kind.
    """
    from starlette.responses import JSONResponse as _JSONResponse

    from turnstone.core.auth import WorkstreamProjectVisibility

    caller = auth_user_id(request)
    owner: str | None = None
    project_id = ""

    if mgr is not None:
        ws_mem = mgr.get(ws_id)
        if ws_mem is not None:
            owner = ws_mem.user_id or ""
            raw_pid = getattr(ws_mem, "project_id", "")
            project_id = raw_pid if isinstance(raw_pid, str) else ""

    if owner is None:
        # Not in memory — storage resolves persisted-but-not-loaded rows.
        from turnstone.core.memory import get_workstream_row

        row = get_workstream_row(ws_id)
        if row is None:
            return "", _JSONResponse({"error": not_found_label}, status_code=404)
        owner = row.get("user_id") or ""
        project_id = row.get("project_id") or ""

    if project_id:
        visibility = WorkstreamProjectVisibility.for_request(request)
        if not visibility.ws_visible(project_id, ws_owner=owner):
            return "", _JSONResponse(
                {"error": "Forbidden: workstream belongs to a private project"},
                status_code=403,
            )

    return owner or caller, None


def parse_cors_origins() -> list[str] | None:
    """Parse ``TURNSTONE_CORS_ORIGINS`` env var into a list of origin strings.

    Returns ``None`` when the variable is unset or empty (meaning: no CORS
    middleware, same-origin only).
    """
    cors_env = os.environ.get("TURNSTONE_CORS_ORIGINS", "").strip()
    if not cors_env:
        return None
    return [o.strip() for o in cors_env.split(",") if o.strip()]


def cors_middleware(origins: list[str]) -> Middleware:
    """Build a Starlette ``CORSMiddleware`` entry for the given *origins*."""
    from starlette.middleware import Middleware as _Middleware
    from starlette.middleware.cors import CORSMiddleware

    return _Middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )


# ---------------------------------------------------------------------------
# Static asset cache-busting
# ---------------------------------------------------------------------------

# Matches src="/static/..." and href="/shared/..." (and vice-versa) but skips
# vendored libraries whose directory names already contain a version number
# (e.g. katex-0.16.44/, hljs-11.11.1/) and URLs that already have a query
# string (prevents double-append if called twice).
_ASSET_RE = re.compile(
    r'(?P<attr>(?:src|href)=")'
    r"(?P<path>/(?:static|shared)/)"
    r"(?!(?:katex|hljs|hls|mermaid)-\d)"
    r'(?P<file>[^"?]+)"'
)


def version_html(html: str) -> str:
    """Inject ``?v=VERSION`` into ``/static/`` and ``/shared/`` asset URLs.

    Vendored libraries with version-bearing directory names are skipped.
    URLs that already contain a query string are left unchanged.
    Called once at startup when loading HTML into memory.
    """
    from turnstone import __version__

    def _repl(m: re.Match[str]) -> str:
        return f'{m.group("attr")}{m.group("path")}{m.group("file")}?v={__version__}"'

    return _ASSET_RE.sub(_repl, html)
