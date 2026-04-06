"""Starlette web-request helpers shared across HTTP servers."""

from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from starlette.middleware import Middleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse


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
