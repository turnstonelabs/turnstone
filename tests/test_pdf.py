"""Tests for core.pdf text extraction (the no-native-PDF wire fallback)."""

from __future__ import annotations

from turnstone.core.pdf import extract_pdf_text, rasterize_pdf


def _minimal_pdf(text: str = "Hello PDF") -> bytes:
    """A valid one-page PDF with a single text line (xref offsets computed)."""
    stream = b"BT /F1 24 Tf 20 60 Td (" + text.encode("latin-1") + b") Tj ET"
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 144]"
        + b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length %d>>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    pdf = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n%s\nendobj\n" % (i, obj)
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        pdf += b"%010d 00000 n \n" % off
    pdf += b"trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref)
    return pdf


class TestExtractPdfText:
    def test_extracts_text(self) -> None:
        assert "Hello PDF" in extract_pdf_text(_minimal_pdf("Hello PDF"))

    def test_garbage_returns_empty_no_raise(self) -> None:
        assert extract_pdf_text(b"not a pdf at all") == ""

    def test_empty_returns_empty(self) -> None:
        assert extract_pdf_text(b"") == ""


class TestRasterizePdf:
    def test_renders_pages_to_png(self) -> None:
        pages = rasterize_pdf(_minimal_pdf("Hello PDF"))
        assert len(pages) == 1
        assert pages[0][:8] == b"\x89PNG\r\n\x1a\n"

    def test_garbage_returns_empty_no_raise(self) -> None:
        assert rasterize_pdf(b"not a pdf at all") == []
