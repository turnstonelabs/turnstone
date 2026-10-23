"""Image-pixel utilities shared across the upload, thumbnail, wire, and perception paths.

Currently: EXIF-orientation normalisation, the transparency flattening an image gets as it enters
a conversation, the JPEG scan check that every decoder of an uploaded image applies first, and the
orientation reader they all use instead of Pillow's EXIF parser.  Kept separate from
:mod:`turnstone.core.thumbnails` (which downscales for the UI) — this operates on full-resolution
bytes at the read/wire boundary so every consumer of an attachment sees the same upright pixels.
"""

from __future__ import annotations

import hashlib
import struct
import threading
from collections import OrderedDict
from io import BytesIO
from typing import TYPE_CHECKING

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from PIL import Image

log = get_logger(__name__)

# EXIF tag 0x0112 (274) — image orientation (1 = upright; 2-8 = flips/rotations).
_EXIF_ORIENTATION_TAG = 0x0112
_TURNED_ORIENTATIONS = frozenset(range(2, 9))

# The ways a TIFF header (the start of an EXIF block) may begin, as Pillow
# accepts them, and the byte order each declares.
_TIFF_BYTE_ORDER = {b"II*\x00": "<", b"II\x00*": "<", b"MM\x00*": ">", b"MM*\x00": ">"}

# struct formats of the integer types an orientation tag may be stored in, one
# value inside the entry: SHORT (the standard's), LONG, SSHORT and SLONG.
_INLINE_VALUE_FORMATS = {3: "H", 4: "L", 8: "h", 9: "l"}

# Mirror turnstone.core.thumbnails: bound decoded pixels so a small compressed
# file that expands to an enormous bitmap can't OOM the node during re-encode.
_MAX_IMAGE_PIXELS = 40_000_000

# A progressive JPEG may repeat scans, and decoding costs a pass over the whole
# picture per scan: a 1 MB file of repeated scans took 80 seconds to decode at
# 4096 by 4096, and decoding at a smaller scale doesn't help.  Encoders write
# about 10.  Counting FF DA (start of scan) marker pairs can only overcount,
# since byte stuffing keeps the pair out of compressed data, so the limit
# leaves room for metadata that happens to contain it.
MAX_JPEG_SCANS = 100

# How much normalize_image_orientation keeps of the turned images it re-encoded.
_NORMALIZED_BYTES = 64 * 1024 * 1024
_NORMALIZED_ENTRIES = 4096

# Modes whose pixels carry an alpha channel.  Other modes are transparent only through a
# ``transparency`` entry in ``info``, a PNG's tRNS chunk for example.
_ALPHA_MODES = frozenset({"RGBA", "LA", "PA", "RGBa", "La"})

# Modes flattened to grey rather than RGB, so a grey image keeps one channel.
_GREY_MODES = frozenset({"L", "LA", "La"})

# The greys a transparent image may be flattened onto, as 8-bit levels, in order of preference on
# a tie: white, black, then mid grey for content that has both light and dark edges.
_BACKGROUND_LEVELS = (255, 0, 128)

# An edge pixel blends with a background it has less contrast against than this: contrast as the
# accessibility guidelines measure it, on relative luminance, and their minimum for graphics and
# large text.  Every luminance blends with at least one of the three levels, and many with two.
_MIN_CONTRAST = 3.0

# An edge pixel vanishes into a background it has less contrast against than this.  No luminance
# comes this close to two of the levels, so each edge pixel vanishes into one of them at most.
_VANISH_CONTRAST = 1.5

# The opacity from which a pixel counts as content when finding where content meets transparency.
_CONTENT_ALPHA = 128

# How many edge pixels a background may blend with and still count as blending with none, while
# the other edge pixels outnumber them four to one.  A speck must not rule a background out, while
# a word in a contrasting colour, which has a hundred or more edge pixels even in small type, must,
# however much other content surrounds it, and so must a small icon that is nothing but the speck.
# A needless mid grey costs contrast, but a wrong white or black loses that word.
_STRAY_EDGE_PIXELS = 16

# The largest image flattened, in pixels; a 4K screen fits.  Flattening holds several full-size
# copies at once, and a WebP's encoder needs far more, so a larger transparent image is stored as
# uploaded.
_FLATTEN_MAX_PIXELS = 4096 * 2048

# The formats uploads and read_file accept.  Pillow is offered nothing else: some of its decoders
# accept any bytes, so a file that sniffs as WebP could otherwise be read as another format.
_FLATTEN_FORMATS = ("PNG", "JPEG", "WEBP")


def _linear(level: int) -> float:
    """The relative luminance of 8-bit sRGB ``level``, from 0 for black to 1 for white."""
    value = level / 255
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _contrast(a: float, b: float) -> float:
    """The contrast ratio of two relative luminances: 1 when equal, 21 for black and white."""
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


# An edge pixel's relative luminance comes from its linear R, G and B (the lookup table, applied to
# each channel, scales linear light to 8 bits) weighted as Rec. 709 weighs them.  Luma, weighted
# from the encoded values instead, counts a bright colour such as cyan as clear of white, on which
# it barely shows.
_LINEAR_LUT = [round(255 * _linear(level)) for level in range(256)]
_LUMINANCE_WEIGHTS = (0.2126, 0.7152, 0.0722, 0.0)


def _bins_within(level: int, ratio: float) -> slice:
    """The bins of a luminance histogram with less than ``ratio`` contrast against ``level``.

    One run of bins, since contrast only grows with distance either side of the level.
    """
    near = [b for b in range(256) if _contrast(b / 255, _linear(level)) < ratio]
    return slice(near[0], near[-1] + 1)


# For each background level, the bins of the edge pixels' luminance histogram that vanish into it,
# and those that blend with it.
_VANISHING_BINS = {level: _bins_within(level, _VANISH_CONTRAST) for level in _BACKGROUND_LEVELS}
_BLENDING_BINS = {level: _bins_within(level, _MIN_CONTRAST) for level in _BACKGROUND_LEVELS}


def too_many_jpeg_scans(data: bytes) -> bool:
    """Whether ``data`` is a JPEG with more scans than an encoder writes.

    Callers refuse or skip such a file before anything decodes it.
    """
    return data.startswith(b"\xff\xd8\xff") and data.count(b"\xff\xda") > MAX_JPEG_SCANS


def validate_image(data: bytes) -> None:
    """Verify and decode an image before admitting its bytes to model history.

    Share the format, pixel, JPEG-scan and one-image-at-a-time limits used by the image utilities.
    Verification checks container integrity; loading also catches broken compressed pixel data.
    Animated inputs are checked through their first frame, the frame used by vision consumers.
    Unlike best-effort flattening, an unreadable input raises so the caller can omit it.
    """
    from PIL import Image

    if too_many_jpeg_scans(data):
        raise ValueError("image exceeds the JPEG scan limit")
    with _flatten_slot:
        try:
            with Image.open(BytesIO(data), formats=_FLATTEN_FORMATS) as image:
                if image.width * image.height > _MAX_IMAGE_PIXELS:
                    raise ValueError("image exceeds the pixel limit")
                image.verify()
            with Image.open(BytesIO(data), formats=_FLATTEN_FORMATS) as image:
                image.load()
        except Exception as exc:
            raise ValueError("image data could not be decoded within the image limits") from exc


def exif_orientation(img: Image.Image) -> int:
    """The EXIF orientation an opened image declares: 1 is upright, 2-8 turn or flip.

    1 when it declares none.  Reads the EXIF block the image carries ahead of
    its pixels: a JPEG's APP1 segment, a PNG's eXIf chunk before IDAT, or a
    WebP's EXIF chunk.  Browsers read a JPEG's and a PNG's the same way, so the
    preview, the thumbnail and the model's copy agree.  Call it right after
    ``Image.open``, before anything decodes.  The tag counts when it holds one
    integer: a SHORT, as the standard writes it, or a LONG, SSHORT or SLONG.
    Reads only this tag, straight from the bytes, and callers turn the picture
    with :func:`turn_upright`, in place of ``getexif()`` and
    ``ImageOps.exif_transpose``: that transpose also rewrites the rest of the
    EXIF, and fails on a tag stored in a type other than the one Pillow
    expects.  A test keeps both calls out of turnstone/.
    """
    exif = img.info.get("exif")
    found = _tiff_orientation(exif) if isinstance(exif, bytes) else None
    return 1 if found is None else found


def _tiff_orientation(data: bytes) -> int | None:
    """The orientation tag in an EXIF block's first directory, or ``None``."""
    base = 0
    while data.startswith(b"Exif\x00\x00", base):
        base += 6
    order = _TIFF_BYTE_ORDER.get(data[base : base + 4])
    if order is None or len(data) < base + 8:
        return None
    (offset,) = struct.unpack_from(order + "L", data, base + 4)
    start = base + offset
    if len(data) < start + 2:
        return None
    (count,) = struct.unpack_from(order + "H", data, start)
    end = start + 2 + 12 * min(count, (len(data) - start - 2) // 12)
    found: int | None = None
    for tag, kind, n, value in struct.iter_unpack(
        order + "HHL4s", memoryview(data)[start + 2 : end]
    ):
        value_format = _INLINE_VALUE_FORMATS.get(kind)
        if tag == _EXIF_ORIENTATION_TAG and n == 1 and value_format is not None:
            # A repeated tag's last entry wins, as in Pillow.
            (found,) = struct.unpack_from(order + value_format, value)
    return found


def turn_upright(img: Image.Image, orientation: int) -> Image.Image:
    """``img`` turned or flipped as EXIF ``orientation`` says to show it.

    ``img`` itself for 1 and for values outside 2-8, as
    ``ImageOps.exif_transpose`` treats them.
    """
    from PIL import Image

    method = {
        2: Image.Transpose.FLIP_LEFT_RIGHT,
        3: Image.Transpose.ROTATE_180,
        4: Image.Transpose.FLIP_TOP_BOTTOM,
        5: Image.Transpose.TRANSPOSE,
        6: Image.Transpose.ROTATE_270,
        7: Image.Transpose.TRANSVERSE,
        8: Image.Transpose.ROTATE_90,
    }.get(orientation)
    return img if method is None else img.transpose(method)


def has_transparency(img: Image.Image) -> bool:
    """Whether an opened image declares an alpha channel or a transparent colour.

    Reads only what ``Image.open`` parsed ahead of the pixels.  Whether any pixel is actually
    transparent takes a decode, which :func:`flatten_transparency` does.
    """
    return img.mode in _ALPHA_MODES or "transparency" in img.info


def flatten_transparency(img: Image.Image) -> Image.Image | None:
    """``img`` flattened onto white, black or mid grey, whichever its content's edges stand out on.

    ``None`` when no pixel is transparent.  What has to stay visible is where content meets the
    transparent area: the content pixels next to a transparent one.  The background is the grey
    the fewest of those pixels blend with (:func:`_background_level`), so white or bright text
    goes onto black, dark text onto white, and an image with both onto mid grey, where each keeps
    some contrast instead of one vanishing.  Content enclosed by other content, such as text on an
    opaque panel, doesn't take part, and neither do faint pixels like a soft shadow.

    The result keeps the image's kind of colour, so a re-encoded copy stays about the size of the
    original: a palette image keeps its pixels' palette indices (:func:`_flatten_palette`), a grey
    image stays grey, and the rest become RGB.  Turning palette images into RGB made them half as
    big again.

    Vision models take RGB only, and providers remove an alpha channel each their own way: onto
    white, onto black, or by dropping it and showing whatever colour the transparent pixels store.
    Content drawn for one background then disappears on the others, so it is flattened here.
    """
    from PIL import Image

    rgba = img if img.mode == "RGBA" else img.convert("RGBA")  # convert copies an RGBA image
    edge_luminance = _edge_luminance(rgba)
    if edge_luminance is None:
        return None
    level = _background_level(edge_luminance)
    if img.mode == "P":
        return _flatten_palette(img, level)
    if img.mode in _GREY_MODES:
        flat = Image.new("L", rgba.size, level)
        flat.paste(rgba.convert("L"), mask=rgba)
        return flat
    flat = Image.new("RGB", rgba.size, (level, level, level))
    flat.paste(rgba, mask=rgba)
    return flat


def _edge_luminance(rgba: Image.Image) -> list[int] | None:
    """The relative luminance histogram of ``rgba``'s content pixels that border a transparent one.

    Bin ``b`` counts the pixels of luminance ``b / 255``.  ``None`` when no pixel is transparent.
    The masks and copies built here are freed when it returns, before the caller allocates the
    flattened copy.
    """
    from PIL import ImageChops

    alpha = rgba.getchannel("A")
    if alpha.getextrema()[0] == 255:
        return None
    content = alpha.point(lambda a: 255 if a >= _CONTENT_ALPHA else 0)
    if content.getbbox() is None:  # nothing that opaque: every visible pixel is content
        content = alpha.point(lambda a: 255 if a else 0)
    edge = ImageChops.subtract(content, _erode(content))
    linear = rgba.convert("RGB").point(_LINEAR_LUT * 3)  # a matrix conversion takes RGB only
    return linear.convert("L", matrix=_LUMINANCE_WEIGHTS).histogram(mask=edge)


def _erode(mask: Image.Image) -> Image.Image:
    """0/255 ``mask`` with every pixel beside a 0 set to 0, as ``MinFilter(3)`` would.

    A 3x3 box average reaches 255 only where all nine pixels are 255.  Pillow's rank filter holds
    the GIL while it runs, which stalls the event loop even when the flatten runs in a worker
    thread; the box blur releases it.
    """
    from PIL import ImageFilter

    return mask.filter(ImageFilter.BoxBlur(1)).point(lambda v: 255 if v == 255 else 0)


def _flatten_palette(img: Image.Image, level: int) -> Image.Image:
    """Palette image ``img`` with each palette entry composited over grey ``level``.

    The pixels keep their indices; only the palette and its transparency change.
    """
    palette = img.getpalette() or []
    opacity = [255] * (len(palette) // 3)
    transparency = img.info.get("transparency")
    if isinstance(transparency, int) and transparency < len(opacity):
        opacity[transparency] = 0  # a single transparent index
    elif isinstance(transparency, bytes):
        opacity[: len(transparency)] = transparency[: len(opacity)]  # an opacity per index
    flat = img.copy()
    flat.info.pop("transparency", None)
    flat.putpalette(
        [
            (alpha * value + (255 - alpha) * level + 127) // 255
            for index, alpha in enumerate(opacity)
            for value in palette[3 * index : 3 * index + 3]
        ]
    )
    return flat


def _background_level(edge_luminance: list[int]) -> int:
    """The level in :data:`_BACKGROUND_LEVELS` the fewest edge pixels vanish into, then blend with.

    ``edge_luminance`` is the edge pixels' luminance histogram.  Losing an edge outright is worse
    than dimming many, so the levels the fewest vanish into (:data:`_VANISH_CONTRAST`) come first,
    and of those, the one the fewest blend with (:data:`_MIN_CONTRAST`).  A pixel vanishes into one
    level at most, so content in two colours keeps both at 1.5:1 or more however unequal their
    shares, and content in one colour gets 3:1 or more, which every colour has against white or
    against black.  Both hold on the histogram's 8-bit bins: a real colour can come out a hair
    under, 2.98:1 at worst.  Up to :data:`_STRAY_EDGE_PIXELS` edge pixels count as none while the
    rest outnumber them four to one.  When edges cover the whole range, as a cut-out photo's do,
    no level is clear of them, and the one the fewest vanish into loses the least.
    """
    total = sum(edge_luminance)

    def count(bins: slice) -> int:
        near = sum(edge_luminance[bins])
        return 0 if near <= min(_STRAY_EDGE_PIXELS, (total - near) // 4) else near

    return min(  # the first of equals: white, black, grey
        _BACKGROUND_LEVELS,
        key=lambda level: (count(_VANISHING_BINS[level]), count(_BLENDING_BINS[level])),
    )


class _NormalizedCache:
    """The turned images :func:`normalize_image_orientation` re-encoded.

    Keyed by the original image's SHA-256.  Bounded by the bytes it holds and by
    entries; the least recently used go first.
    """

    def __init__(self, max_bytes: int, max_entries: int) -> None:
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._entries: OrderedDict[bytes, bytes] = OrderedDict()
        self._held = 0
        self._lock = threading.Lock()

    def get(self, key: bytes) -> bytes | None:
        with self._lock:
            value = self._entries.get(key)
            if value is not None:
                self._entries.move_to_end(key)
            return value

    def put(self, key: bytes, value: bytes) -> None:
        if len(value) > self._max_bytes:
            return
        with self._lock:
            if key in self._entries:
                self._held -= len(self._entries.pop(key))
            self._entries[key] = value
            self._held += len(value)
            while self._held > self._max_bytes or len(self._entries) > self._max_entries:
                _key, dropped = self._entries.popitem(last=False)
                self._held -= len(dropped)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._held = 0


_normalized = _NormalizedCache(_NORMALIZED_BYTES, _NORMALIZED_ENTRIES)


def normalize_image_orientation(data: bytes) -> bytes:
    """Bake an image's EXIF orientation into its pixels; return re-encoded bytes.

    Images with no orientation tag (or an identity orientation) are returned
    UNCHANGED — no decode or re-encode, so the pristine original is preserved.
    Every send rebuilds the request from the stored originals, so a turned
    image's re-encoded bytes are kept by the original's SHA-256 rather than
    worked out again.  Never raises: any failure (Pillow missing, decode error,
    oversized) returns the original bytes.

    Why this exists: a phone photo stores landscape pixels plus an orientation
    tag.  Browsers honour the tag for ``<img>``, but Pillow (our thumbnails) and
    many vision-model image decoders do NOT — so the model literally perceives
    the photo rotated.  Normalising at the read/wire boundary makes every
    consumer (browser, thumbnail, model) see the same upright image.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - declared dependency; defensive
        return data
    try:
        img = Image.open(BytesIO(data))
        orientation = exif_orientation(img)
        if orientation not in _TURNED_ORIENTATIONS:
            return data  # upright already — keep the original bytes verbatim
        if img.size[0] * img.size[1] > _MAX_IMAGE_PIXELS:
            log.warning("orientation normalize skipped: image exceeds pixel cap")
            return data
        key = hashlib.sha256(data).digest()
        kept = _normalized.get(key)
        if kept is not None:
            return kept  # the scan check passed when it was kept
        if too_many_jpeg_scans(data):
            log.warning(
                "orientation normalize skipped: JPEG has more than %d scans", MAX_JPEG_SCANS
            )
            return data
        fmt = img.format or "PNG"
        upright = turn_upright(img, orientation)
        buf = BytesIO()
        save_kwargs: dict[str, object] = {}
        if fmt in ("JPEG", "WEBP"):
            save_kwargs["quality"] = 90
        upright.save(buf, format=fmt, **save_kwargs)
        out = buf.getvalue()
        _normalized.put(key, out)
        return out
    except Exception as exc:
        log.warning("orientation normalize failed: %s", exc)
        return data


# Flattening an image at the cap holds a few hundred MiB, so a process flattens one image at a
# time and the rest wait their turn.
_flatten_slot = threading.BoundedSemaphore(1)


def flatten_image(data: bytes, *, max_bytes: int) -> bytes:
    """``data`` with its transparency flattened, for an image entering a conversation.

    Uploads and ``read_file`` images pass through here once, as they arrive, and are stored as it
    returns them, so the stored image is the one every model is sent.  Nothing already stored is
    flattened: a conversation keeps sending what it sent, and its history and cached prefix stay
    as they were.

    An image whose header declares no transparency comes back UNCHANGED, the same object, without
    a decode, and so does one whose alpha channel holds no transparent pixel, after one.  So do
    the images this leaves as uploaded: one over :data:`_FLATTEN_MAX_PIXELS`, an animated one, a
    colour-keyed one Pillow can't key exactly (:func:`_colour_key_is_exact`), and one that would
    re-encode larger than ``max_bytes``, the cap the original was checked against, since a stored
    image goes out with every later request.  A flattened image keeps its format, its colour
    profile and, for a WebP, whether it is lossless; its EXIF orientation is baked in, and the
    rest of its EXIF, its text chunks, its gamma and chromaticity and its resolution are dropped.
    One image is flattened at a time.  Never raises: any failure (Pillow missing, decode error)
    returns the original bytes.
    """
    try:
        from PIL import Image  # noqa: F401 - only to learn Pillow is there
    except ImportError:  # pragma: no cover - declared dependency; defensive
        return data
    try:
        img = _flattenable(data)
        if img is None:
            return data
        if img.size[0] * img.size[1] > _FLATTEN_MAX_PIXELS:
            # Not a warning: a large screen's screenshots get here, most with no transparent pixel.
            log.info("transparency flatten skipped: image exceeds the flatten pixel cap")
            return data
        orientation = exif_orientation(img)
        save_kwargs: dict[str, object] = {}
        if img.info.get("icc_profile"):
            save_kwargs["icc_profile"] = img.info["icc_profile"]
        if img.format == "WEBP":
            save_kwargs.update({"lossless": True} if _webp_is_lossless(data) else {"quality": 90})
        with _flatten_slot:
            flat = flatten_transparency(turn_upright(img, orientation))
            if flat is None:
                return data
            buf = BytesIO()
            flat.save(buf, format=img.format or "PNG", **save_kwargs)
        out = buf.getvalue()
        if len(out) > max_bytes:
            log.warning(
                "transparency flatten skipped: flattened image is %d bytes, over %d",
                len(out),
                max_bytes,
            )
            return data
        return out
    except Exception as exc:
        log.warning("transparency flatten failed: %s", exc)
        return data


def needs_flattening(data: bytes) -> bool:
    """Whether :func:`flatten_image` may change ``data``, judged from its header without a decode.

    False for an image ``flatten_image`` returns unchanged on its header's word: one that declares
    no transparency, and the ones it leaves as uploaded before decoding.  True for one whose
    alpha channel may yet prove fully opaque, since only a decode tells, and for one whose header
    can't be read, which ``flatten_image`` then returns unchanged itself.  A caller skips the wait
    for a turn to flatten when this is False.
    """
    try:
        img = _flattenable(data)
    except Exception:
        return True
    return img is not None and img.size[0] * img.size[1] <= _FLATTEN_MAX_PIXELS


def _flattenable(data: bytes) -> Image.Image | None:
    """``data`` opened, or ``None`` when its header shows :func:`flatten_image` would keep it.

    Kept: an image that declares no transparency, an animated one, a colour-keyed one Pillow
    can't key exactly, and a JPEG with more scans than an encoder writes.  Reads the header only.
    """
    from PIL import Image

    img = Image.open(BytesIO(data), formats=_FLATTEN_FORMATS)
    if not has_transparency(img) or getattr(img, "is_animated", False):
        return None
    if not _colour_key_is_exact(img, data):
        return None
    # Every decoder checks scans first.  A JPEG declares no transparency, so it never gets here.
    if too_many_jpeg_scans(data):
        log.warning("transparency flatten skipped: JPEG has more than %d scans", MAX_JPEG_SCANS)
        return None
    return img


def _colour_key_is_exact(img: Image.Image, data: bytes) -> bool:
    """Whether Pillow marks the transparent pixels of PNG ``data``, opened as ``img``, exactly.

    True for an alpha channel or a palette.  A colour key (a tRNS chunk on a grey or RGB image) is
    matched against Pillow's 8-bit samples, so only an 8-bit image's key is exact: a 16-bit
    image's is compared with clipped or truncated samples, and a 1-, 2- or 4-bit image's keeps the
    file's own scale.  Those are left as uploaded rather than flattened wrongly.
    """
    if img.mode in _ALPHA_MODES or img.mode == "P":
        return True
    # The bit depth of an IHDR chunk that comes first, as the standard puts it.  Pillow also opens
    # a file with another chunk ahead of it, whose byte there is something else.
    ihdr_first = data[12:16] == b"IHDR"
    return img.mode in ("L", "RGB") and img.format == "PNG" and ihdr_first and data[24] == 8


def _webp_is_lossless(data: bytes) -> bool:
    """Whether WebP ``data`` holds lossless image data: its first VP8 or VP8L chunk is VP8L.

    Walks the RIFF chunks after the 12-byte header, each payload padded to an even length, and
    into an animation frame's own chunks: a one-frame animation isn't animated, so it is
    flattened like a still.  Searching the bytes for the chunk name instead would also match
    metadata that contains it.
    """
    pos = 12
    while pos + 8 <= len(data):
        fourcc = data[pos : pos + 4]
        if fourcc in (b"VP8 ", b"VP8L"):
            return fourcc == b"VP8L"
        if fourcc == b"ANMF":
            pos += 8 + 16  # past the frame's 16-byte header, to its first chunk
            continue
        size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        pos += 8 + size + (size & 1)
    return False
