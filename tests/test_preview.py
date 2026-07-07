"""Unit tests for the preview-content policy module (``turnstone/core/preview.py``).

Pure-function coverage: kind resolution precedence (magic bytes → MIME hint →
extension → UTF-8 fallback), the explicit ``kind`` override lanes, base-href
injection, title extraction, and the per-MIME serving headers the route
attaches.  The tool executor and the HTTP route are covered separately
(``test_open_preview_tool.py`` / ``test_server_attachments_endpoints.py``).
"""

from __future__ import annotations

from turnstone.core.attachments import IMAGE_SIZE_CAP, PDF_SIZE_CAP, TEXT_DOC_SIZE_CAP
from turnstone.core.preview import (
    PREVIEW_BLOB_KIND,
    PREVIEW_KINDS,
    PREVIEW_SERVE_MIMES,
    PREVIEW_SIZE_CAPS,
    build_preview_descriptor,
    inject_base_href,
    page_title,
    preview_response_headers,
    resolve_preview_kind,
)

PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xfc\xcf"
    b"\xc0\xc0\xc0\x00\x00\x00\x05\x00\x01\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
)
PDF_MIN = b"%PDF-1.4 fake body"
HTML_DOC = b"<html><head><title>Acme Pricing</title></head><body>hi</body></html>"


class TestResolvePreviewKind:
    def test_magic_bytes_win_over_everything(self):
        # A PNG claiming to be CSV by both MIME and extension is an image.
        assert resolve_preview_kind("text/csv", "data.csv", PNG_1x1) == ("image", "image/png")
        assert resolve_preview_kind("text/plain", "doc.txt", PDF_MIN) == (
            "pdf",
            "application/pdf",
        )

    def test_mime_hint_html(self):
        kind, mime = resolve_preview_kind("text/html; charset=iso-8859-1", "page", HTML_DOC)
        assert kind == "web"
        assert mime == "text/html; charset=utf-8"

    def test_mime_hint_families(self):
        assert resolve_preview_kind("text/csv", "x", b"a,b\n1,2")[0] == "table"
        assert resolve_preview_kind("application/json", "x", b"[]") == (
            "table",
            "application/json",
        )
        assert resolve_preview_kind("text/markdown", "x", b"# hi")[0] == "markdown"
        assert resolve_preview_kind("text/x-log", "x", b"line")[0] == "text"

    def test_extension_fallback_when_no_mime(self):
        assert resolve_preview_kind("", "report.html", HTML_DOC)[0] == "web"
        assert resolve_preview_kind("", "data.tsv", b"a\tb")[0] == "table"
        assert resolve_preview_kind("", "notes.md", b"# t")[0] == "markdown"
        # URL tails strip query/fragment before the extension check.
        assert resolve_preview_kind("", "https://x.io/a.csv?dl=1#f", b"a,b")[0] == "table"

    def test_utf8_text_fallback(self):
        assert resolve_preview_kind("", "LICENSE", b"MIT License") == (
            "text",
            "text/plain; charset=utf-8",
        )

    def test_binary_is_not_previewable(self):
        assert resolve_preview_kind("", "blob.bin", b"\x00\x01\x02\x03" * 8) is None
        # Text-DECLARED binary is misdeclared, not previewable text.
        assert resolve_preview_kind("text/plain", "x", b"\x00\xff" * 8) is None
        assert resolve_preview_kind("application/octet-stream", "x", b"\x00" * 32) is None

    def test_override_validates_bytes(self):
        # image override on non-image bytes fails rather than mislabeling.
        assert resolve_preview_kind("", "x", b"not an image", "image") is None
        assert resolve_preview_kind("", "x", PNG_1x1, "image") == ("image", "image/png")
        assert resolve_preview_kind("", "x", b"not a pdf", "pdf") is None
        # Text-family override on binary bytes fails.
        assert resolve_preview_kind("", "x", b"\x00\x01", "text") is None

    def test_override_forces_view(self):
        # kind='text' on an HTML doc = view source.
        assert resolve_preview_kind("text/html", "p.html", HTML_DOC, "text")[0] == "text"
        # kind='table' keeps the real payload type for the client parser.
        assert resolve_preview_kind("application/json", "d", b"[1]", "table") == (
            "table",
            "application/json",
        )
        assert resolve_preview_kind("", "d.tsv", b"a\tb", "table") == (
            "table",
            "text/tab-separated-values; charset=utf-8",
        )
        assert resolve_preview_kind("", "d.txt", b"a,b", "table") == (
            "table",
            "text/csv; charset=utf-8",
        )

    def test_unknown_override_rejected(self):
        assert resolve_preview_kind("text/plain", "x", b"hi", "hologram") is None


class TestHtmlHelpers:
    def test_base_href_inserted_after_head(self):
        out = inject_base_href("<html><head><meta x></head></html>", "https://a.io/p/q")
        assert out.startswith('<html><head><base href="https://a.io/p/q">')

    def test_base_href_prepended_without_head(self):
        out = inject_base_href("<p>bare</p>", "https://a.io/")
        assert out.startswith('<base href="https://a.io/">')

    def test_existing_base_untouched(self):
        doc = '<head><base href="https://original/"></head>'
        assert inject_base_href(doc, "https://other/") == doc

    def test_base_href_attribute_escaped(self):
        out = inject_base_href("<head></head>", 'https://a.io/"><script>x</script>')
        assert "<script>" not in out
        assert "&quot;&gt;&lt;script&gt;" in out

    def test_page_title_extraction(self):
        assert page_title(HTML_DOC.decode()) == "Acme Pricing"
        assert page_title("<title>a &amp; b\n   c</title>") == "a & b c"
        assert page_title("<p>no title</p>") is None
        assert page_title("<title></title>") is None


class TestServingPolicy:
    def test_html_gets_bare_sandbox_csp(self):
        h = preview_response_headers("text/html", "page.html")
        assert h["Content-Security-Policy"] == "sandbox"
        assert h["X-Content-Type-Options"] == "nosniff"
        assert h["Cache-Control"] == "private, no-store"
        assert h["Content-Disposition"].startswith("inline;")

    def test_pdf_gets_no_csp(self):
        h = preview_response_headers("application/pdf", "doc.pdf")
        assert "Content-Security-Policy" not in h
        assert h["X-Content-Type-Options"] == "nosniff"

    def test_other_kinds_keep_full_csp(self):
        for mime in ("image/png", "text/csv", "text/plain"):
            h = preview_response_headers(mime, "f")
            assert h["Content-Security-Policy"] == "default-src 'none'; sandbox"

    def test_filename_header_injection_stripped(self):
        h = preview_response_headers("text/plain", 'a"\r\nX-Evil: 1')
        assert "\r" not in h["Content-Disposition"]
        assert "\n" not in h["Content-Disposition"]
        assert '"' not in h["Content-Disposition"].split("filename=")[1].strip('"')

    def test_serve_allowlist_covers_every_stored_kind(self):
        for mime in (
            "text/html",
            "application/pdf",
            "image/png",
            "image/webp",
            "text/csv",
            "text/tab-separated-values",
            "application/json",
            "text/markdown",
            "text/plain",
        ):
            assert mime in PREVIEW_SERVE_MIMES

    def test_caps_reuse_attachment_constants(self):
        assert PREVIEW_SIZE_CAPS["image"] == IMAGE_SIZE_CAP
        assert PREVIEW_SIZE_CAPS["pdf"] == PDF_SIZE_CAP
        assert PREVIEW_SIZE_CAPS["text"] == TEXT_DOC_SIZE_CAP
        assert set(PREVIEW_SIZE_CAPS) == set(PREVIEW_KINDS)

    def test_blob_kind_is_outside_model_vocabulary(self):
        assert PREVIEW_BLOB_KIND not in ("image", "text", "pdf", "audio")

    def test_descriptor_shape(self):
        d = build_preview_descriptor(
            kind="web",
            title="T",
            source="https://a.io",
            attachment_id="abc",
            content_type="text/html; charset=utf-8",
            size=7,
        )
        assert d == {
            "kind": "web",
            "title": "T",
            "source": "https://a.io",
            "attachment_id": "abc",
            "content_type": "text/html; charset=utf-8",
            "size": 7,
        }


class TestReviewHardening:
    """Pins for the review-round fixes (2026-07-07)."""

    def test_filename_folds_to_latin1_safe_ascii(self):
        # Starlette encodes header values latin-1; em dashes / CJK titles
        # must fold, not 500 the serving route.
        h = preview_response_headers("text/html", "Docs — v1.7 日本語.html")
        h["Content-Disposition"].encode("latin-1")  # must not raise
        h2 = preview_response_headers("text/plain", "——")
        h2["Content-Disposition"].encode("latin-1")
        assert (
            'filename="preview"' in h2["Content-Disposition"]
            or "filename=" in h2["Content-Disposition"]
        )

    def test_base_href_never_precedes_doctype(self):
        doc = "<!DOCTYPE html><body>no head</body>"
        out = inject_base_href(doc, "https://a.io/")
        assert out.startswith("<!DOCTYPE html>")
        assert '<base href="https://a.io/">' in out
        # <html> without <head> also keeps document order.
        doc2 = "<!doctype html><html lang=en><body>x</body></html>"
        out2 = inject_base_href(doc2, "https://a.io/")
        assert out2.startswith("<!doctype html><html lang=en>")
        assert out2.index("<base") > out2.index("<html")

    def test_legacy_charset_web_pages_stay_previewable(self):
        # windows-1252 / iso-8859-1 bytes are not UTF-8; web kind must not
        # reject them (the executor transcodes at store time).
        latin1_html = "<html><body>café</body></html>".encode("latin-1")
        assert resolve_preview_kind("text/html; charset=iso-8859-1", "p", latin1_html) == (
            "web",
            "text/html; charset=utf-8",
        )
        # Extension lane and explicit override agree.
        assert resolve_preview_kind("", "page.html", latin1_html)[0] == "web"
        assert resolve_preview_kind("", "page.bin", latin1_html, "web")[0] == "web"
        # Non-web text kinds stay strict.
        assert resolve_preview_kind("text/csv", "d.csv", latin1_html) is None
