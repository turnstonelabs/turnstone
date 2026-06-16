"""PDF helpers.

Text extraction for the no-native-PDF fallback: when a model lacks
``supports_pdf``, the wire resolver extracts the PDF's text here and sends it as
a text document rather than PDF bytes the model can't read.  Pure-local
(pypdfium2), no network, deterministic.

Re-run per wire build by design — there is intentionally no module-global cache
here.  A PDF re-parsed on every turn of a long conversation is wasteful, but the
principled place to memoize a *derived representation of a content-addressed
blob* is a durable derived-artifact store keyed by (source-hash, derivation)
that would serve every kind uniformly — not a per-module dict that happens to
hold PDFs.  See the attachments design brief; that store is deferred.
"""

from __future__ import annotations

import contextlib
import io

from turnstone.core.log import get_logger

log = get_logger(__name__)

# Bound the page walk so a pathological (small-bytes, many-pages) PDF can't block
# the sync send thread unbounded.
_MAX_PAGES = 100


def extract_pdf_text(data: bytes) -> str:
    """Best-effort text from a PDF; never raises.

    Returns ``""`` on a parse failure or a scanned PDF with no text layer.  Walks
    at most :data:`_MAX_PAGES` pages.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError:  # pragma: no cover - declared dependency; defensive
        log.warning("pypdfium2 not installed; PDF text extraction unavailable")
        return ""

    doc = None
    try:
        doc = pdfium.PdfDocument(data)
        parts: list[str] = []
        truncated = False
        for i, page in enumerate(doc):
            if i >= _MAX_PAGES:
                truncated = True
                page.close()
                break
            textpage = page.get_textpage()
            parts.append(textpage.get_text_range() or "")
            textpage.close()
            page.close()
        text = "\n\n".join(p.strip() for p in parts if p.strip())
        if truncated:
            text += f"\n\n[PDF truncated at {_MAX_PAGES} pages]"
        return text
    except Exception as exc:
        log.warning("PDF text extraction failed: %s", exc)
        return ""
    finally:
        if doc is not None:
            with contextlib.suppress(Exception):
                doc.close()


# Bound page count + payload for the rasterize fallback (images are far heavier
# than text).  Re-run per wire build — same no-cache rationale as extract_pdf_text.
_MAX_RASTER_PAGES = 10

# Clamp the rendered bitmap's longest side.  A PDF MediaBox may be up to
# 14400pt; at scale 2.0 that page renders to ~28800px (a multi-GB bitmap), so an
# attacker-supplied PDF could OOM the render thread.  Page *count* is bounded
# above; this bounds per-page *area*.
_MAX_RENDER_PX = 2000


def rasterize_pdf(
    data: bytes, *, max_pages: int = _MAX_RASTER_PAGES, scale: float = 2.0
) -> list[bytes]:
    """Render up to ``max_pages`` PDF pages to PNG bytes, one per page.

    For vision-capable models that can't read PDF natively.  Never raises —
    returns ``[]`` on a parse/render failure (the caller falls back to text
    extraction).  Needs pypdfium2 (render) + Pillow (PNG encode).
    """
    try:
        import pypdfium2 as pdfium
    except ImportError:  # pragma: no cover - declared dependency; defensive
        log.warning("pypdfium2 not installed; PDF rasterize unavailable")
        return []

    doc = None
    try:
        doc = pdfium.PdfDocument(data)
        pages: list[bytes] = []
        for i, page in enumerate(doc):
            if i >= max_pages:
                page.close()
                break
            # Clamp scale per page so the longest rendered side <= _MAX_RENDER_PX;
            # a normal page (<=~800pt) is unaffected, a giant MediaBox is shrunk.
            eff_scale = scale
            try:
                longest_pt = max(page.get_size())
                if longest_pt > 0:
                    eff_scale = min(scale, _MAX_RENDER_PX / longest_pt)
            except Exception:
                eff_scale = min(scale, 1.0)  # can't size the page → render small
            bitmap = page.render(scale=eff_scale)
            buf = io.BytesIO()
            bitmap.to_pil().save(buf, format="PNG")
            pages.append(buf.getvalue())
            with contextlib.suppress(Exception):
                bitmap.close()
            page.close()
        return pages
    except Exception as exc:
        log.warning("PDF rasterize failed: %s", exc)
        return []
    finally:
        if doc is not None:
            with contextlib.suppress(Exception):
                doc.close()
