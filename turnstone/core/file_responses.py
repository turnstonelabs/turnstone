"""HTTP response policy shared by stored previews and live filesystem files.

Callers own authorization, byte acquisition, type detection, and size limits.
These builders own the serving policy once those steps have succeeded.
"""

from __future__ import annotations

from starlette.responses import JSONResponse, Response

from turnstone.core.preview import PREVIEW_SERVE_MIMES, preview_response_headers
from turnstone.core.web_helpers import latin1_safe_filename


def file_preview_response(
    body: bytes,
    mime: str,
    filename: str,
    *,
    allow_remote_assets: bool = False,
    probe: bool = False,
    unsupported_message: str = "File is not previewable; download it to open locally",
) -> Response:
    """Serve a renderable MIME with the shared sandbox and cache policy.

    A probe passes the same MIME check and carries the same hardening headers,
    but no bytes. It avoids fetching a full body through the node proxy merely
    to discover whether a browser image or iframe can load it.
    """
    bare_mime = mime.split(";", 1)[0].strip().lower()
    if bare_mime not in PREVIEW_SERVE_MIMES:
        return JSONResponse({"error": unsupported_message}, status_code=415)
    headers = preview_response_headers(bare_mime, filename, allow_remote_assets=allow_remote_assets)
    if probe:
        return Response(status_code=204, headers=headers)
    return Response(body, media_type=mime, headers=headers)


def file_content_response(
    body: bytes,
    mime: str,
    filename: str,
    *,
    download: bool = False,
) -> Response:
    """Serve original content with a safe filename and inert response policy.

    The source chooses the MIME: text uploads use text/plain; filesystem
    downloads use application/octet-stream. Both share the same hardening,
    while stored content remains inline and explicit downloads are attachments.
    """
    disposition = "attachment" if download else "inline"
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Content-Disposition": f'{disposition}; filename="{latin1_safe_filename(filename)}"',
        "Cache-Control": "private, no-store",
    }
    return Response(body, media_type=mime, headers=headers)
