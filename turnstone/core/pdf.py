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
