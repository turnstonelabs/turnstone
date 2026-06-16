"""Image-pixel utilities shared across the thumbnail, wire, and perception paths.

Currently: EXIF-orientation normalisation.  Kept separate from
:mod:`turnstone.core.thumbnails` (which downscales for the UI) — this operates on
full-resolution bytes at the read/wire boundary so every consumer of an
attachment sees the same upright pixels.
"""

from __future__ import annotations

from io import BytesIO

from turnstone.core.log import get_logger

log = get_logger(__name__)

# EXIF tag 0x0112 (274) — image orientation (1 = upright; 2-8 = flips/rotations).
_EXIF_ORIENTATION_TAG = 0x0112

# Mirror turnstone.core.thumbnails: bound decoded pixels so a small compressed
# file that expands to an enormous bitmap can't OOM the node during re-encode.
_MAX_IMAGE_PIXELS = 40_000_000


def normalize_image_orientation(data: bytes) -> bytes:
    """Bake an image's EXIF orientation into its pixels; return re-encoded bytes.

    Images with no orientation tag (or an identity orientation) are returned
    UNCHANGED — no decode/re-encode, so the pristine original is preserved and
    there is no per-send cost in the common case.  Never raises: any failure
    (Pillow missing, decode error, oversized) returns the original bytes.

    Why this exists: a phone photo stores landscape pixels plus an orientation
    tag.  Browsers honour the tag for ``<img>``, but Pillow (our thumbnails) and
    many vision-model image decoders do NOT — so the model literally perceives
    the photo rotated.  Normalising at the read/wire boundary makes every
    consumer (browser, thumbnail, model) see the same upright image.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover - declared dependency; defensive
        return data
    try:
        img = Image.open(BytesIO(data))
        orientation = img.getexif().get(_EXIF_ORIENTATION_TAG)
        if not orientation or orientation == 1:
            return data  # upright already — keep the original bytes verbatim
        if img.size[0] * img.size[1] > _MAX_IMAGE_PIXELS:
            log.warning("orientation normalize skipped: image exceeds pixel cap")
            return data
        fmt = img.format or "PNG"
        upright = ImageOps.exif_transpose(img)  # applies the rotation + drops the tag
        if upright is None:  # pragma: no cover - in_place=False never returns None
            return data
        buf = BytesIO()
        save_kwargs: dict[str, object] = {}
        if fmt in ("JPEG", "WEBP"):
            save_kwargs["quality"] = 90
        upright.save(buf, format=fmt, **save_kwargs)
        return buf.getvalue()
    except Exception as exc:
        log.warning("orientation normalize failed: %s", exc)
        return data
