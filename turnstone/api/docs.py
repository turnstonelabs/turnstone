"""OpenAPI spec endpoint and Swagger UI handler factories."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from starlette.responses import HTMLResponse, Response

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request


def make_openapi_handler(spec: dict[str, Any]) -> Callable[..., Awaitable[Response]]:
    """Create a /openapi.json handler that serves a pre-built spec."""
    cached = json.dumps(spec, indent=2)

    async def openapi_json(request: Request) -> Response:
        return Response(cached, media_type="application/json")

    return openapi_json


def make_docs_handler(
    openapi_path: str = "/openapi.json",
) -> Callable[..., Awaitable[HTMLResponse]]:
    """Create a /docs handler that serves SwaggerUI loaded from CDN."""
    html = (
        '<!DOCTYPE html><html lang="en"><head>'
        "<title>turnstone API</title>"
        '<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5.18.2/swagger-ui.css">'
        "</head><body>"
        '<div id="swagger-ui"><p style="padding:2rem;font-family:sans-serif;color:#666">'
        "Loading API documentation&hellip;</p></div>"
        '<script src="https://unpkg.com/swagger-ui-dist@5.18.2/swagger-ui-bundle.js"></script>'
        "<script>SwaggerUIBundle({url:" + json.dumps(openapi_path) + ","
        'dom_id:"#swagger-ui",deepLinking:true})</script>'
        "</body></html>"
    )

    async def docs(request: Request) -> HTMLResponse:
        return HTMLResponse(html)

    return docs
