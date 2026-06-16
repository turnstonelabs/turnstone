"""Tests for EXIF-orientation normalisation (turnstone.core.images)."""

from __future__ import annotations

from io import BytesIO

import pytest

from turnstone.core.images import normalize_image_orientation

Image = pytest.importorskip("PIL.Image")

_ORIENTATION_TAG = 0x0112  # EXIF orientation (standard tag id)


def _oriented_jpeg(orientation: int, size: tuple[int, int] = (4, 2)) -> bytes:
    img = Image.new("RGB", size, "red")
    exif = img.getexif()
    exif[_ORIENTATION_TAG] = orientation
    buf = BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def test_applies_rotation_and_strips_tag() -> None:
    # Orientation 6 = "rotate 90° for display": a 4×2 landscape becomes 2×4.
    data = _oriented_jpeg(6, size=(4, 2))
    out = normalize_image_orientation(data)
    assert out != data, "a rotated image must be re-encoded upright"
    img = Image.open(BytesIO(out))
    assert img.size == (2, 4), "the 90° rotation must be baked into the pixels"
    assert img.getexif().get(_ORIENTATION_TAG) in (None, 1), "the orientation tag must be cleared"


def test_passthrough_when_upright() -> None:
    data = _oriented_jpeg(1, size=(4, 2))
    assert normalize_image_orientation(data) == data, "identity orientation must not re-encode"


def test_passthrough_when_no_exif() -> None:
    buf = BytesIO()
    Image.new("RGB", (3, 3), "blue").save(buf, format="PNG")
    data = buf.getvalue()
    assert normalize_image_orientation(data) == data, "a tag-less image must pass through verbatim"


def test_never_raises_on_garbage() -> None:
    assert normalize_image_orientation(b"not an image") == b"not an image"
    assert normalize_image_orientation(b"") == b""
