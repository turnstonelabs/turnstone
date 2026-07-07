"""Preview-content policy for the ``open_preview`` tool.

The preview pane renders tool-selected content — a fetched web page, a PDF, an
image, a data table, a text/markdown document — in a dedicated frontend pane
beside the conversation.  This module owns the pure policy so the session
executor and the serving route share one definition: the content-kind
vocabulary, how bytes + hints resolve to a kind, the per-kind size caps, the
serving MIME allowlist + response headers, and the small HTML mutations
(base-href injection, title extraction) applied to fetched pages at store
time.

Preview blobs are persisted content-addressed with attachment kind
``PREVIEW_BLOB_KIND``.  That kind is deliberately outside the model-visible
attachment vocabulary (image / text / pdf / audio): trajectory reconstruction
skips it, so a preview blob can never be lifted into a turn's content and
materialized onto the wire — the tool turn's ``meta.extra["preview"]``
descriptor is the only carrier, and it is frontend-facing only.
"""

from __future__ import annotations

import html
import re
from typing import Any

from turnstone.core.attachments import (
    ALLOWED_IMAGE_MIMES,
    IMAGE_SIZE_CAP,
    PDF_SIZE_CAP,
    TEXT_DOC_SIZE_CAP,
    sniff_image_mime,
    sniff_pdf_mime,
)

# Rendered-content kinds the pane knows how to display.  ``web`` is a fetched
# HTML document (sandboxed iframe); ``table`` is CSV/TSV/JSON parsed and
# rendered client-side; the rest map 1:1 onto native browser rendering.
PREVIEW_KINDS: frozenset[str] = frozenset({"web", "pdf", "image", "table", "text", "markdown"})

# Storage ``kind`` for preview blobs — see the module docstring for why this
# is not one of the model-visible attachment kinds.
PREVIEW_BLOB_KIND = "preview"

# Per-kind byte caps on the STORED preview content.  image/pdf/text reuse the
# attachment-subsystem caps so a previewable file and an uploadable file agree
# on "too big".  Fetched pages get their own cap (real-world pages fit well
# under it; over-cap pages error rather than truncate — a mid-tag cut renders
# garbage).  Tables get headroom over plain text: a few-MB CSV is a normal
# artifact of "bash produced data", and the client-side renderer row-caps.
PREVIEW_SIZE_CAPS: dict[str, int] = {
    "web": 4 * 1024 * 1024,
    "pdf": PDF_SIZE_CAP,
    "image": IMAGE_SIZE_CAP,
    "table": 2 * 1024 * 1024,
    "text": TEXT_DOC_SIZE_CAP,
    "markdown": TEXT_DOC_SIZE_CAP,
}

# MIME types the preview route will serve with a renderable Content-Type.
# Everything stored by ``open_preview`` lands in this set; the route still
# allowlists defensively so a non-preview blob addressed by id serves nothing
# renderable.  Parameterized types (``text/html; charset=utf-8``) match on the
# bare type.
PREVIEW_SERVE_MIMES: frozenset[str] = frozenset(
    {
        "text/html",
        "application/pdf",
        "text/plain",
        "text/csv",
        "text/tab-separated-values",
        "application/json",
        "text/markdown",
    }
    | set(ALLOWED_IMAGE_MIMES)
)

# Extension → (kind, stored mime).  Consulted after magic bytes and the
# transport MIME hint; keys are lowercase with the dot.
_EXT_KINDS: dict[str, tuple[str, str]] = {
    ".html": ("web", "text/html; charset=utf-8"),
    ".htm": ("web", "text/html; charset=utf-8"),
    ".pdf": ("pdf", "application/pdf"),
    ".csv": ("table", "text/csv; charset=utf-8"),
    ".tsv": ("table", "text/tab-separated-values; charset=utf-8"),
    ".json": ("table", "application/json"),
    ".md": ("markdown", "text/markdown; charset=utf-8"),
    ".markdown": ("markdown", "text/markdown; charset=utf-8"),
}

# Stored mime per kind when the kind is chosen first (explicit ``kind`` arg or
# a MIME-hint match): the inverse of ``_EXT_KINDS`` plus the text fallback.
_KIND_MIMES: dict[str, str] = {
    "web": "text/html; charset=utf-8",
    "pdf": "application/pdf",
    "table": "text/csv; charset=utf-8",
    "text": "text/plain; charset=utf-8",
    "markdown": "text/markdown; charset=utf-8",
}


def _is_utf8_text(data: bytes) -> bool:
    """True when *data* decodes as UTF-8 and carries no NUL (binary tell)."""
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _kind_from_mime(mime: str) -> tuple[str, str] | None:
    """Map a transport MIME hint to ``(kind, stored_mime)``, or ``None``."""
    bare = mime.split(";", 1)[0].strip().lower()
    if not bare:
        return None
    if "html" in bare:
        return "web", _KIND_MIMES["web"]
    if bare == "application/pdf":
        return "pdf", _KIND_MIMES["pdf"]
    if bare in ALLOWED_IMAGE_MIMES:
        return "image", bare
    if bare == "text/csv":
        return "table", "text/csv; charset=utf-8"
    if bare == "text/tab-separated-values":
        return "table", "text/tab-separated-values; charset=utf-8"
    if bare in ("application/json", "text/json"):
        return "table", "application/json"
    if bare == "text/markdown":
        return "markdown", _KIND_MIMES["markdown"]
    if bare.startswith("text/"):
        return "text", _KIND_MIMES["text"]
    return None


def resolve_preview_kind(
    mime_hint: str,
    name_hint: str,
    body: bytes,
    kind_override: str | None = None,
) -> tuple[str, str] | None:
    """Resolve ``(kind, stored_mime)`` for *body*, or ``None`` if unpreviewable.

    Precedence: explicit *kind_override* (the model's ``kind`` argument) →
    magic bytes (image / pdf — never extension-trusted, mirroring the upload
    classifier) → transport MIME hint → filename/URL extension → UTF-8 text
    fallback.  A binary body that matches nothing is not previewable.
    """
    if kind_override:
        if kind_override not in PREVIEW_KINDS:
            return None
        if kind_override == "image":
            sniffed_image = sniff_image_mime(body)
            return ("image", sniffed_image) if sniffed_image else None
        if kind_override == "pdf":
            return ("pdf", "application/pdf") if sniff_pdf_mime(body) else None
        # Text-family overrides (table / text / markdown) require text; web
        # is transcoded at store time (see the mime lane below) so a legacy
        # charset page can still be forced to render.
        if kind_override != "web" and not _is_utf8_text(body):
            return None
        if kind_override == "table":
            # Preserve a JSON payload's real type so the client parser branches.
            bare = mime_hint.split(";", 1)[0].strip().lower()
            ext = _name_ext(name_hint)
            if bare in ("application/json", "text/json") or ext == ".json":
                return "table", "application/json"
            if bare == "text/tab-separated-values" or ext == ".tsv":
                return "table", "text/tab-separated-values; charset=utf-8"
            return "table", _KIND_MIMES["table"]
        return kind_override, _KIND_MIMES[kind_override]

    sniffed = sniff_image_mime(body)
    if sniffed:
        return "image", sniffed
    if sniff_pdf_mime(body):
        return "pdf", "application/pdf"
    from_mime = _kind_from_mime(mime_hint)
    if from_mime:
        # Text-declared bytes that aren't text are misdeclared — reject rather
        # than serve binary under a text MIME.  ``web`` is exempt: legacy
        # charsets (windows-1252 / Shift-JIS pages) are not UTF-8 on the raw
        # bytes, and the executor transcodes web content to UTF-8 at store
        # time (charset-aware for fetches, replacement-decoded otherwise).
        if from_mime[0] in ("table", "text", "markdown") and not _is_utf8_text(body):
            return None
        return from_mime
    ext_match = _EXT_KINDS.get(_name_ext(name_hint))
    if ext_match:
        if ext_match[0] != "web" and not _is_utf8_text(body):
            return None
        return ext_match
    if _is_utf8_text(body):
        return "text", _KIND_MIMES["text"]
    return None


def _name_ext(name: str) -> str:
    """Lowercase extension of a path / URL tail (query and fragment stripped)."""
    tail = name.rsplit("/", 1)[-1].split("?", 1)[0].split("#", 1)[0]
    dot = tail.rfind(".")
    return tail[dot:].lower() if dot >= 0 else ""


# ``<base>`` / ``<head>`` / ``<html>`` / doctype openers in the first slice of
# the document — enough for any real page; scanning megabytes for a head that
# must appear early is wasted work.
_HEAD_SCAN_LIMIT = 65536
_BASE_TAG_RE = re.compile(r"<base[\s>/]", re.IGNORECASE)
_HEAD_OPEN_RE = re.compile(r"<head(?:\s[^>]*)?>", re.IGNORECASE)
_HTML_OPEN_RE = re.compile(r"<html(?:\s[^>]*)?>", re.IGNORECASE)
_DOCTYPE_RE = re.compile(r"<!doctype[^>]*>", re.IGNORECASE)


def inject_base_href(html_text: str, base_url: str) -> str:
    """Give a fetched page a ``<base href>`` so relative assets resolve.

    The stored bytes are what the fetch saw; without a base, every relative
    ``src``/``href`` inside the sandboxed iframe would resolve against the
    turnstone origin and 404.  A page that declares its own ``<base>`` is left
    alone.  Insertion goes right after the ``<head>`` opener when present,
    else after ``<html>`` / the doctype — the parser hoists the tag into the
    implied head from there.  Never ahead of the doctype: markup before
    ``<!doctype`` voids it and drops the whole preview into quirks mode.
    """
    head_slice = html_text[:_HEAD_SCAN_LIMIT]
    if _BASE_TAG_RE.search(head_slice):
        return html_text
    tag = f'<base href="{html.escape(base_url, quote=True)}">'
    m = _HEAD_OPEN_RE.search(head_slice) or _HTML_OPEN_RE.search(head_slice)
    if not m:
        m = _DOCTYPE_RE.search(head_slice)
    if m:
        return html_text[: m.end()] + tag + html_text[m.end() :]
    return tag + html_text


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def page_title(html_text: str) -> str | None:
    """The document's ``<title>`` text (unescaped, whitespace-collapsed), or None."""
    m = _TITLE_RE.search(html_text[:_HEAD_SCAN_LIMIT])
    if not m:
        return None
    title = " ".join(html.unescape(m.group(1)).split())
    return title[:200] or None


def build_preview_descriptor(
    *,
    kind: str,
    title: str,
    source: str,
    attachment_id: str,
    content_type: str,
    size: int,
) -> dict[str, Any]:
    """The structured descriptor that rides the tool turn's meta to the frontend.

    One shape on every boundary — the live ``tool_result`` SSE event, the
    persisted ``conversations.meta`` column, and the ``/history`` projection —
    so the pane renders identically live and on replay.
    """
    return {
        "kind": kind,
        "title": title,
        "source": source,
        "attachment_id": attachment_id,
        "content_type": content_type,
        "size": size,
    }


def preview_response_headers(bare_mime: str, filename: str) -> dict[str, str]:
    """Response headers for the preview serving route, per rendered MIME.

    ``text/html`` gets ``Content-Security-Policy: sandbox`` — the document
    renders (its subresources load) but scripts never run and its origin is
    opaque, so it can't touch the app origin's cookies or DOM; the embedding
    iframe carries the ``sandbox`` attribute too.  ``application/pdf`` gets no
    CSP: Chromium's PDF viewer refuses to paint inside a sandboxed context,
    and the response is inert media rendered by browser chrome, not an active
    document.  Everything else keeps the attachment endpoints' full
    ``default-src 'none'; sandbox`` posture.
    """
    # Header values must be latin-1 encodable (Starlette raises on anything
    # else), and page-title-derived filenames routinely carry em dashes / CJK
    # — fold to ASCII rather than 500 the route.
    safe_name = filename.replace('"', "").replace("\r", "").replace("\n", "")
    safe_name = safe_name.encode("ascii", errors="replace").decode("ascii") or "preview"
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'inline; filename="{safe_name}"',
        "Cache-Control": "private, no-store",
    }
    if bare_mime == "text/html":
        headers["Content-Security-Policy"] = "sandbox"
    elif bare_mime != "application/pdf":
        headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return headers
