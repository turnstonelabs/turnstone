"""Tests for attachment thumbnail generation (image downscale + pdf first page)."""

from __future__ import annotations

from io import BytesIO

import pytest

from turnstone.core.thumbnails import make_thumbnail

PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xfc\xcf"
    b"\xc0\xc0\xc0\x00\x00\x00\x05\x00\x01\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
)
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _minimal_pdf(text: str = "Hi") -> bytes:
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


class TestMakeThumbnail:
    def test_image_thumbnail_is_png(self) -> None:
        out = make_thumbnail(PNG_1x1, "image")
        assert out is not None and out[:8] == _PNG_MAGIC

    def test_image_thumbnail_honours_exif_orientation(self) -> None:
        pil = pytest.importorskip("PIL.Image")
        src = pil.new("RGB", (40, 20), "red")  # landscape source
        exif = src.getexif()
        exif[0x0112] = 6  # "rotate 90° for display" → the thumbnail should be portrait
        buf = BytesIO()
        src.save(buf, format="JPEG", exif=exif)
        out = make_thumbnail(buf.getvalue(), "image")
        assert out is not None
        thumb = pil.open(BytesIO(out))
        assert thumb.height > thumb.width, "thumbnail must reflect the applied EXIF rotation"

    def test_pdf_thumbnail_is_png(self) -> None:
        out = make_thumbnail(_minimal_pdf(), "pdf")
        assert out is not None and out[:8] == _PNG_MAGIC

    def test_audio_has_no_thumbnail(self) -> None:
        assert make_thumbnail(b"RIFFfake", "audio") is None

    def test_garbage_image_returns_none(self) -> None:
        assert make_thumbnail(b"not an image", "image") is None

    @pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
    def test_oversized_image_rejected(self, monkeypatch) -> None:
        # An image past the pixel cap must be rejected WITHOUT decoding it.  Use a
        # size in the (cap, 2*cap] window — Pillow only *warns* there and would
        # decode fully, so this guards the explicit size check, not Pillow's >2x
        # raise.
        from io import BytesIO

        from PIL import Image

        monkeypatch.setattr("turnstone.core.thumbnails._MAX_IMAGE_PIXELS", 50)
        buf = BytesIO()
        Image.new("RGB", (6, 10)).save(buf, format="PNG")  # 60 px, in (50, 100]
        assert make_thumbnail(buf.getvalue(), "image") is None

    def test_at_cap_image_still_renders(self, monkeypatch) -> None:
        # Exactly at the cap is allowed (boundary is strictly greater-than).
        from io import BytesIO

        from PIL import Image

        monkeypatch.setattr("turnstone.core.thumbnails._MAX_IMAGE_PIXELS", 64)
        buf = BytesIO()
        Image.new("RGB", (8, 8)).save(buf, format="PNG")  # 64 px == cap
        out = make_thumbnail(buf.getvalue(), "image")
        assert out is not None and out[:8] == _PNG_MAGIC
