"""Small PNG thumbnails of visual attachments, for the UI chip/preview.

``image`` → downscaled PNG; ``pdf`` → first page rendered (pypdfium2) then
downscaled. Audio and text have no thumbnail. Never raises — returns ``None`` on
any failure, and the UI falls back to a plain icon.
"""

from __future__ import annotations

from io import BytesIO

from turnstone.core.log import get_logger

log = get_logger(__name__)

_THUMB_MAX_PX = 160

# Cap decoded image size.  A few-KB compressed file can decode to enormous
# dimensions; PIL's default ceiling (~89M px) still permits a ~530MB RGB decode.
# Tighten it so a malicious upload can't OOM the node while we build a thumbnail.
_MAX_IMAGE_PIXELS = 40_000_000


def make_thumbnail(data: bytes, kind: str, *, max_px: int = _THUMB_MAX_PX) -> bytes | None:
    """Return a small PNG thumbnail for an ``image``/``pdf`` blob, else ``None``."""
    try:
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover - declared dependency; defensive
        log.warning("Pillow not installed; thumbnails unavailable")
        return None

    # Bound decoded pixels for both the image branch and the rasterized-PDF
    # branch (which also re-opens PNG bytes through PIL below).
    Image.MAX_IMAGE_PIXELS = _MAX_IMAGE_PIXELS
    try:
        if kind == "pdf":
            from turnstone.core.pdf import rasterize_pdf

            pages = rasterize_pdf(data, max_pages=1)
            if not pages:
                return None
            img = Image.open(BytesIO(pages[0]))
        elif kind == "image":
            img = Image.open(BytesIO(data))
        else:
            return None
        # Reject oversized images explicitly before any decode.  Pillow's
        # MAX_IMAGE_PIXELS only *raises* above 2x the cap; between the cap and 2x
        # it merely warns and decodes fully (a 40-80M px image → ~480MB RGB),
        # defeating the bound.  The header-declared size is known after open(),
        # so gate on it before exif_transpose / convert (both decode the pixels).
        # (Explicit check, not a warnings filter: make_thumbnail runs in a thread
        # and the global warnings state is not thread-safe.)
        px = img.size[0] * img.size[1]
        if px > _MAX_IMAGE_PIXELS:
            log.warning(
                "thumbnail rejected: %d×%d (%d px) exceeds %d-pixel cap",
                img.size[0],
                img.size[1],
                px,
                _MAX_IMAGE_PIXELS,
            )
            return None
        # Honour EXIF orientation so a phone photo's thumbnail isn't rotated:
        # Pillow doesn't auto-apply the tag and PNG can't carry it.  No-op for
        # the rasterized-PDF branch (its pages carry no EXIF).
        oriented = ImageOps.exif_transpose(img) or img
        rgb = oriented.convert("RGB")
        rgb.thumbnail((max_px, max_px))
        buf = BytesIO()
        rgb.save(buf, format="PNG")
        return buf.getvalue()
    except Image.DecompressionBombError as exc:
        log.warning("thumbnail rejected: image exceeds %d-pixel cap: %s", _MAX_IMAGE_PIXELS, exc)
        return None
    except Exception as exc:
        log.warning("thumbnail generation failed (kind=%s): %s", kind, exc)
        return None
